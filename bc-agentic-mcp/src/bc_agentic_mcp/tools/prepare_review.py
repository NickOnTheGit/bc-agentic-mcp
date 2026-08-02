"""bc_prepare_review — single-entry spec prompt workflow."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.tools.init import handle_init
from bc_agentic_mcp.tools.write_spec import handle_write_spec
from bc_agentic_mcp.tools.plan_design import handle_plan_design
from bc_agentic_mcp.tools.breakdown_tasks import handle_breakdown_tasks
from bc_agentic_mcp import lessons as lessons_store
from bc_agentic_mcp import checkpoints as memory

_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_SPLIT = re.compile(r"(?<!^)(?=[A-Z])")
_REQUESTED_FIELD_LINE = re.compile(r"^Subprocess[A-Za-z0-9_]+$")

# Point-in-time / environment-state findings that must NOT become durable lessons:
# they depend on transient state (e.g. git freshness) and would mislead if replayed later.
_TRANSIENT_FINDING_CODES = {"BC-STALE-SOURCE"}

def _normalize_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    for chunk in _WORD_SPLIT.split(text):
        if not chunk:
            continue
        for part in _CAMEL_SPLIT.split(chunk):
            cleaned = part.lower().strip()
            if cleaned:
                tokens.append(cleaned)
    return tokens


def _extract_api_hint(context: str) -> Optional[str]:
    lines = [line.strip() for line in context.splitlines()]
    for idx, line in enumerate(lines):
        if "extend the following api" in line.lower() and idx + 1 < len(lines):
            candidate = lines[idx + 1].strip()
            if candidate:
                return candidate.lower()
    return None


def _extract_requested_fields(context: str) -> List[str]:
    fields: List[str] = []
    for raw in context.splitlines():
        line = raw.strip().rstrip(",")
        if _REQUESTED_FIELD_LINE.match(line):
            fields.append(line)
    return fields


def _extract_version_token(path_text: str) -> Optional[str]:
    match = re.search(r"[\\/]v(\d+)[\\/]", path_text.lower())
    if match:
        return f"v{match.group(1)}"
    return None


def _infer_expected_api_versions(project_root: Path, api_hint: Optional[str]) -> List[str]:
    if not api_hint:
        return []
    src_dir = project_root / "src"
    if not src_dir.exists():
        return []

    versions = set()
    for al_file in sorted(src_dir.rglob("*.al")):
        file_name = al_file.name.lower()
        if api_hint in file_name:
            rel = str(al_file.relative_to(project_root))
            token = _extract_version_token(rel)
            if token:
                versions.add(token)
    return sorted(versions)


def _infer_example_group(name: str, source_field: str) -> Optional[str]:
    probe = f"{name} {source_field}".lower()
    if "leavingtenant" in probe:
        return "leavingtenant"
    if "newrental" in probe:
        return "newrental"
    return None


def _collect_code_examples(
    project_root: Path,
    context: str,
    max_examples: int = 5,
    api_hint: Optional[str] = None,
    preferred_terms: Optional[List[str]] = None,
    expected_versions: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Find nearby AL code patterns that can be copied or slightly adapted."""
    src_dir = Path(project_root) / "src"
    if not src_dir.exists():
        return []

    context_tokens = set(_normalize_tokens(context))
    preferred_tokens = set(_normalize_tokens(" ".join(preferred_terms or [])))
    candidates: List[Dict[str, Any]] = []

    for al_file in sorted(src_dir.rglob("*.al")):
        content = al_file.read_text(encoding="utf-8", errors="replace")
        file_tokens = set(_normalize_tokens(content))

        # Prefer concrete object patterns already in the codebase.
        for match in re.finditer(r'field\(\s*"?([^";]+)"?\s*;\s*Rec\.(?:"([^"]+)"|([A-Za-z0-9_]+))', content):
            alias = match.group(1)
            source_field = match.group(2) or match.group(3) or alias
            field_tokens = set(_normalize_tokens(alias + " " + source_field))
            overlap = len((context_tokens & field_tokens) or (context_tokens & file_tokens))
            preferred_overlap = len(preferred_tokens & field_tokens)
            file_has_api_hint = bool(api_hint and api_hint in str(al_file).lower())
            if file_has_api_hint and overlap == 0:
                overlap = 1
            score = overlap + (preferred_overlap * 10) + (50 if file_has_api_hint else 0)
            if preferred_tokens and preferred_overlap == 0 and not file_has_api_hint:
                continue
            if api_hint and not file_has_api_hint and not preferred_overlap:
                continue
            if overlap:
                candidates.append(
                    {
                        "file": str(al_file.relative_to(project_root)),
                        "name": alias,
                        "source_field": source_field,
                        "kind": "api-mapping",
                        "score": str(score),
                    }
                )

        for match in re.finditer(r'field\(\d+;\s*([A-Za-z0-9_]+)\s*;', content):
            field_name = match.group(1)
            field_tokens = set(_normalize_tokens(field_name))
            overlap = len(context_tokens & field_tokens)
            preferred_overlap = len(preferred_tokens & field_tokens)
            file_has_api_hint = bool(api_hint and api_hint in str(al_file).lower())
            if file_has_api_hint and overlap == 0:
                overlap = 1
            score = overlap + (preferred_overlap * 10) + (50 if file_has_api_hint else 0)
            if preferred_tokens and preferred_overlap == 0 and not file_has_api_hint:
                continue
            if api_hint and not file_has_api_hint and not preferred_overlap:
                continue
            if overlap:
                candidates.append(
                    {
                        "file": str(al_file.relative_to(project_root)),
                        "name": field_name,
                        "source_field": field_name,
                        "kind": "table-field",
                        "score": str(score),
                    }
                )

    candidates.sort(key=lambda item: (int(item.get("score", "0")), item["file"], item["name"]), reverse=True)
    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in candidates:
        marker = (item["file"], item["name"], item["source_field"])
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)

    required_versions = set(expected_versions or [])
    required_groups = set()
    for term in preferred_terms or []:
        lower = term.lower()
        if "subprocessleavingtenant" in lower:
            required_groups.add("leavingtenant")
        if "subprocessnewrental" in lower:
            required_groups.add("newrental")

    selected: List[Dict[str, str]] = []
    used_markers = set()
    covered_versions = set()
    covered_groups = set()

    def _pick(match_fn, max_pick: Optional[int] = None) -> None:
        nonlocal covered_versions, covered_groups
        picked = 0
        for item in deduped:
            marker = (item["file"], item["name"], item["source_field"])
            if marker in used_markers:
                continue
            version = _extract_version_token(item["file"])
            group = _infer_example_group(item["name"], item["source_field"])
            if not match_fn(version, group):
                continue
            selected.append(item)
            used_markers.add(marker)
            if version:
                covered_versions.add(version)
            if group:
                covered_groups.add(group)
            picked += 1
            if max_pick is not None and picked >= max_pick:
                return
            if len(selected) >= max_examples:
                return

    # Ensure at least one example per expected version when possible.
    for version in sorted(required_versions):
        _pick(lambda v, _g, req=version: v == req, max_pick=1)
        if len(selected) >= max_examples:
            break

    # Ensure subgroup examples (LeavingTenant/NewRental) when possible.
    if len(selected) < max_examples:
        for group in sorted(required_groups):
            _pick(lambda _v, g, req=group: g == req, max_pick=1)
            if len(selected) >= max_examples:
                break

    # Fill remaining slots by score order.
    if len(selected) < max_examples:
        _pick(lambda _v, _g: True)

    return selected[:max_examples]


def _evaluate_quality_gates(
    spec: Dict[str, Any],
    requested_fields: List[str],
    api_hint: Optional[str],
    task_count: int,
    total_estimated_objects: int,
    require_object_targets: bool,
    code_examples: Optional[List[Dict[str, str]]] = None,
    expected_versions: Optional[List[str]] = None,
    has_code_context: bool = True,
) -> Dict[str, Any]:
    objects_to_modify = spec.get("objects_to_modify", [])
    objects_to_create = spec.get("objects_to_create", [])
    requirements = spec.get("requirements", [])
    acceptance_tests = spec.get("acceptance_tests", [])
    from bc_agentic_mcp import ears as _ears
    ears_result = _ears.lint(requirements)
    from bc_agentic_mcp import traceability as _trace
    trace = _trace.build_trace(requirements, acceptance_tests)
    business_rules = spec.get("business_rules", [])
    allowed_files = spec.get("scope_boundaries", {}).get("allowed_files", [])

    covered_fields = set()
    for rule in business_rules:
        for field in rule.get("fields", []):
            covered_fields.add(field)

    has_any_objects = bool(objects_to_modify or objects_to_create)
    has_api_target = True
    if api_hint:
        has_api_target = any(api_hint in str(path).lower() for path in allowed_files)

    field_coverage_ok = True
    if requested_fields:
        field_coverage_ok = all(field in covered_fields for field in requested_fields)

    tasks_ok = True
    if require_object_targets:
        # A grounded plan needs real object targets plus at least a real task and its test.
        # Do NOT require an extra upgrade task — items that do not touch data have none.
        tasks_ok = total_estimated_objects > 0 and task_count >= 2

    requested_groups = set()
    for field in requested_fields:
        lower = field.lower()
        if "subprocessleavingtenant" in lower:
            requested_groups.add("leavingtenant")
        if "subprocessnewrental" in lower:
            requested_groups.add("newrental")

    example_groups = set()
    for example in code_examples or []:
        probe = f"{example.get('name', '')} {example.get('source_field', '')}".lower()
        if "leavingtenant" in probe:
            example_groups.add("leavingtenant")
        if "newrental" in probe:
            example_groups.add("newrental")

    group_coverage_in_examples = True
    if requested_groups:
        group_coverage_in_examples = requested_groups.issubset(example_groups)

    expected_versions = expected_versions or []
    example_versions = set()
    for example in code_examples or []:
        token = _extract_version_token(example.get("file", ""))
        if token:
            example_versions.add(token)
    version_coverage_in_examples = True
    if expected_versions:
        version_coverage_in_examples = set(expected_versions).issubset(example_versions)

    # SPEC-TIME test pyramid (Bug 267600 lesson): the declared acceptance tests must
    # already cover happy AND negative AND edge shapes — catching the deficit here is
    # cheap; catching it at the evidence gate wastes a container cycle.
    from bc_agentic_mcp import verification as _verification
    shape_counts = {"happy": 0, "negative": 0, "edge": 0}
    for at in acceptance_tests:
        declared_shape = str(at.get("path_shape") or "").strip().lower()
        if declared_shape in shape_counts:
            shape_counts[declared_shape] += 1
            continue
        if declared_shape in ("regression", "api"):
            continue  # separate validation classes govern these
        text = " ".join(str(at.get(k) or "") for k in ("statement", "given", "when", "then", "type"))
        counted = _verification.classify_test_paths([text])
        for shape, n in counted.items():
            shape_counts[shape] += n
    test_shapes_ok = (not acceptance_tests) or all(shape_counts[s] > 0 for s in ("happy", "negative", "edge"))

    failures: List[str] = []
    if not requirements:
        failures.append("Acceptance criteria cannot be empty: the spec defines no requirements to verify.")
    if not acceptance_tests:
        failures.append("No acceptance tests: at least one measurable acceptance test is required.")
    if requirements and not ears_result["ok"]:
        failures.append(
            f"{len(ears_result['violations'])} requirement(s) are not in EARS syntax "
            "(each must contain 'shall' and follow a When/While/Where/If/ubiquitous shape)."
        )
    if not has_code_context:
        failures.append(
            "Code read-context is missing or blocked: build it on clean, latest source "
            "(bc_read_code_context) before the plan is review-ready."
        )
    if requirements and trace["uncovered"]:
        failures.append(
            f"{len(trace['uncovered'])} requirement(s) have no acceptance test (uncovered): "
            f"{', '.join(trace['uncovered'][:5])}."
        )
    if trace["orphaned"]:
        failures.append(
            f"{len(trace['orphaned'])} acceptance test(s) are orphaned (cover a missing/none "
            f"requirement): {', '.join(trace['orphaned'][:5])}."
        )
    if require_object_targets and not has_any_objects:
        failures.append("No concrete objects were identified for modification/creation.")
    if not has_api_target:
        failures.append("No allowed target files matched the requested API name.")
    if not field_coverage_ok:
        failures.append("Not all requested fields are covered by generated business rules.")
    if require_object_targets and not tasks_ok:
        failures.append("Task plan is too small for implementation (expected object-linked tasks).")
    if require_object_targets and not group_coverage_in_examples:
        failures.append(
            "Code examples do not cover all requested subprocess groups (LeavingTenant/NewRental)."
        )
    if require_object_targets and not version_coverage_in_examples:
        failures.append("Code examples do not cover all expected API versions.")
    if not test_shapes_ok:
        missing = [s for s in ("happy", "negative", "edge") if shape_counts[s] == 0]
        failures.append(
            "Declared test shapes are incomplete (missing: " + ", ".join(missing) + "; "
            f"counts happy:{shape_counts['happy']}, negative:{shape_counts['negative']}, "
            f"edge:{shape_counts['edge']}). Declare the missing scenarios as "
            "'TEST negative: …' / 'TEST edge: …' lines in the spec bullets and regenerate — "
            "the test pyramid is enforced at spec time, not discovered at the evidence gate."
        )

    return {
        "pass": not failures,
        "failures": failures,
        "checks": {
            "has_any_objects": has_any_objects,
            "has_acceptance_criteria": bool(requirements),
            "has_acceptance_tests": bool(acceptance_tests),
            "ears_ok": ears_result["ok"],
            "traceability_ok": trace["ok"],
            "has_code_context": has_code_context,
            "has_api_target": has_api_target,
            "field_coverage_ok": field_coverage_ok,
            "tasks_ok": tasks_ok,
            "group_coverage_in_examples": group_coverage_in_examples,
            "version_coverage_in_examples": version_coverage_in_examples,
            "test_shapes_declared": test_shapes_ok,
        },
        "test_shape_counts": shape_counts,
        "traceability": trace,
    }


def _write_quality_gate_file(specs_dir: Path, quality_gate: Dict[str, Any]) -> Path:
    path = specs_dir / "quality_gate.json"
    path.write_text(json.dumps(quality_gate, indent=2), encoding="utf-8")
    return path


def _append_enforcement_to_review(review_path: Path, quality_gate: Dict[str, Any], quality_path: Path) -> None:
    content = review_path.read_text(encoding="utf-8")
    content += "\n\n## Deterministic Enforcement\n\n"
    content += f"- Gate passed: {quality_gate.get('pass', False)}\n"
    for key, val in quality_gate.get("checks", {}).items():
        content += f"- {key}: {val}\n"
    if quality_gate.get("failures"):
        content += "- Failures:\n"
        for failure in quality_gate["failures"]:
            content += f"  - {failure}\n"
    content += f"- Quality artifact: {quality_path}\n"
    review_path.write_text(content, encoding="utf-8")


def _type_reason(field: str, al_type: str) -> str:
    if field.endswith("OnHoldTill"):
        return "Name ends with 'OnHoldTill' so it represents the date a hold lasts until."
    if field.endswith("OnHoldIndication"):
        return "An 'Indication' is a yes/no flag, so a Boolean is the correct type."
    if field.endswith("OnHoldUser"):
        return "A user reference is stored as a code value."
    if field.endswith("OnHoldTeam"):
        return "A team reference is stored as a short code value."
    if field.endswith("Remark"):
        return "A free-text remark is stored as text."
    return "Defaulted to text for an unclassified field name."


def _render_decision_rationale(spec: Dict[str, Any]) -> str:
    """Human-readable chain of thought explaining every spec decision and its basis."""
    name = spec.get("spec_name", "")
    objects = spec.get("objects_to_modify", [])
    data_model = spec.get("data_model", [])
    _obj_types = " ".join(str(o.get("type", "")).lower() for o in objects + spec.get("objects_to_create", []))
    is_api = ("api" in spec.get("work_types", [])) or ("api" in _obj_types)
    requirements = spec.get("requirements", [])
    assumptions = spec.get("assumptions", [])
    evidence_by_id = {e["id"]: e for e in spec.get("evidence", [])}

    lines: List[str] = []
    lines.append(f"## Decision Rationale (Human Chain of Thought): {name}")
    lines.append("")
    lines.append(
        "This section explains, in plain language, every decision made to turn the "
        "request into the machine spec. Each decision states its basis so a reviewer "
        "can audit the reasoning without reading code."
    )
    lines.append("")

    lines.append("### 1. How we understood the request")
    stories = spec.get("user_stories") or []
    if stories:
        story = stories[0]
        lines.append(
            f"- We read the request as a user story: as **{story['as_a']}**, you want to "
            f"**{story['i_want']}**, so that **{story['so_that']}**."
        )
    else:
        lines.append("- No explicit user story was present; we worked from the described intent.")
    spec_type = spec.get("spec_type", "feature")
    type_reason = (
        "the description contained bug/defect indicators."
        if spec_type == "bugfix"
        else "the description asks for new/extended capability, not a defect fix."
    )
    lines.append(f"- We classified this as a **{spec_type}** because {type_reason}")
    fields = [d["field"] for d in data_model]
    if fields:
        lines.append(f"- We identified **{len(fields)}** requested fields to expose: {', '.join(fields)}.")
    lines.append("")

    lines.append("### 2. Why we chose these implementation targets")
    if objects:
        lines.append(
            "We searched the local codebase for API pages matching the requested endpoint "
            "and found concrete files to modify:"
            if is_api else
            "We grounded the plan in the real repo — each target below was resolved to an actual file:"
        )
        for obj in objects:
            ev_ids = obj.get("evidence_refs", [])
            ev = evidence_by_id.get(ev_ids[0]) if ev_ids else None
            version = obj.get("version") or "n/a"
            entry = f"- `{obj['target']}` (version {version})"
            if ev:
                entry += f" — evidence {ev['id']}: `{ev['excerpt']}`"
            lines.append(entry)
        lines.append("")
        lines.append(
            ("Rationale: modifying existing API pages keeps the blast radius minimal and "
             "reuses the established field-mapping pattern instead of inventing a new one.")
            if is_api else
            ("Rationale: extending the existing objects keeps the blast radius minimal and "
             "follows the conventions already present in those files.")
        )
    else:
        lines.append(
            "- No concrete API target files were found, which is why the planner did not "
            "proceed to a ready spec."
        )
    lines.append("")

    lines.append("### 3. How we decided each field's type and behavior")
    if data_model:
        lines.append("| Field | AL type | API attribute | Read | Update | Why this type |")
        lines.append("|-------|---------|---------------|------|--------|---------------|")
        for d in data_model:
            lines.append(
                f"| {d['field']} | {d['al_type']} | {d.get('api_attribute', '—')} | {d.get('read', '')} | "
                f"{d.get('update', '')} | {_type_reason(d['field'], d['al_type'])} |"
            )
    else:
        lines.append("- No fields were requested explicitly.")
    lines.append("")

    lines.append("### 4. Why we wrote these requirements (EARS)")
    for req in requirements:
        lines.append(f"- **{req['id']}** ({req['ears_type']}): {req['statement']}")
        lines.append(
            f"  - Verified by {', '.join(req.get('acceptance_tests', [])) or 'n/a'}; "
            f"backed by evidence {', '.join(req.get('evidence_refs', [])[:3]) or 'n/a'}."
        )
    lines.append("")
    lines.append("We used EARS notation so each requirement is unambiguous and directly testable.")
    lines.append("")

    lines.append("### 5. Assumptions we made (and why they are safe)")
    if assumptions:
        for a in assumptions:
            lines.append(f"- **{a['id']}**: {a['statement']}")
            lines.append(
                f"  - Rationale: {a['rationale']} Reversible: {a.get('reversible', True)}."
            )
    else:
        lines.append("- No assumptions were necessary; all decisions were directly evidenced.")
    lines.append("")

    lines.append("### 6. What we deliberately did not decide")
    open_questions = spec.get("open_questions", [])
    if open_questions:
        for q in open_questions:
            lines.append(f"- {q}")
    else:
        lines.append("- Nothing remained unresolved; the spec is complete for implementation.")
    lines.append("")

    lines.append("### 7. Decision log")
    lines.append("| Decision | Rationale | Evidence |")
    lines.append("|----------|-----------|----------|")
    lines.append(f"| Classify as {spec_type} | Matches the nature of the description | description |")
    if objects:
        first_obj_ev = next((o["evidence_refs"][0] for o in objects if o.get("evidence_refs")), "n/a")
        lines.append(
            f"| Modify {len(objects)} {'API page' if is_api else 'object'}(s) | Reuse existing pattern, minimal blast radius | {first_obj_ev} |"
        )
    if data_model:
        lines.append(
            f"| Map {len(data_model)} field(s) for read/update | Requested read/update semantics | EV-001 |"
        )
    if assumptions:
        a0 = assumptions[0]
        a0_ev = a0["evidence_refs"][0] if a0.get("evidence_refs") else "n/a"
        lines.append(f"| No extra date validation | Matches existing passthrough APIs | {a0_ev} |")
    lines.append("")
    return "\n".join(lines)
_EARS_TYPES = {"ubiquitous", "state", "event", "optional", "unwanted", "complex"}
_PLACEHOLDER_TOKENS = ("to be filled", "(to be filled)", "tbd", "todo")


def _recognize_patterns(spec: Dict[str, Any], project_root: Path) -> tuple:
    """Planning-time pattern recognition — check the whole codebase BEFORE asking.

    Recognizes:
    1. Multi-version mirroring: every page exposing the same EntityName must be in
       scope, so new fields are mirrored across all API versions.
    2. API mapping style per field (enum Format() vs primitive direct).

    Returns (patterns, findings). Any entity page found outside scope is an error,
    so the planner catches incomplete scope before implementation.
    """
    patterns: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    objects = spec.get("objects_to_modify", [])
    data_model = spec.get("data_model", [])

    entity = ""
    for obj in objects:
        candidate = (obj.get("api", {}) or {}).get("entity_name")
        if candidate:
            entity = candidate.strip().strip("'\"").lower()
            break

    # Discover every page in the repo that exposes this entity (check everywhere),
    # scoped to the same field family so different projections are not over-included.
    request_token_sets = [set(_normalize_tokens(d.get("field", ""))) for d in data_model]

    def _in_family(content: str) -> bool:
        for match in re.finditer(r'Rec\.\"([^\"]+)\"', content):
            field_tokens = set(_normalize_tokens(match.group(1)))
            if any(len(field_tokens & rs) >= 2 for rs in request_token_sets):
                return True
        return False

    repo_targets: List[str] = []
    src = Path(project_root) / "src"
    if entity and src.exists():
        for al_file in sorted(src.rglob("*.al")):
            try:
                content = al_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r"(?im)^\s*EntityName\s*=\s*'([^']+)'", content)
            if not (match and match.group(1).strip().lower() == entity):
                continue
            if request_token_sets and not _in_family(content):
                continue
            repo_targets.append(str(al_file.relative_to(project_root)))

    allowed = set(spec.get("scope_boundaries", {}).get("allowed_files", []))
    if repo_targets:
        missing = [t for t in repo_targets if t not in allowed]
        patterns.append(
            {
                "pattern": "multi_version_mirroring",
                "detail": f"Entity '{entity}' is exposed by {len(repo_targets)} page(s); new fields must be mirrored to all of them.",
                "targets": repo_targets,
            }
        )
        if missing:
            findings.append(
                {
                    "severity": "error",
                    "code": "PATTERN-MIRROR",
                    "message": (
                        f"Entity '{entity}' is also exposed in {missing}, which are not in scope. "
                        "Mirror the change there too (multi-version API pattern)."
                    ),
                    "refs": [],
                }
            )

    if data_model:
        patterns.append(
            {
                "pattern": "api_mapping_style",
                "detail": "Primitives map directly as Rec.\"Field\"; enums/options use Format(Rec.\"Field\").",
                "mapping": {d["field"]: d.get("api_mapping_expr") for d in data_model},
            }
        )

    return patterns, findings


def _render_patterns(spec_name: str, patterns: List[Dict[str, Any]]) -> str:
    lines = [
        f"# Recognized Patterns: {spec_name}",
        "",
        "Patterns mined from the codebase at planning time so implementation is mechanical.",
        "",
    ]
    for p in patterns:
        lines.append(f"## {p['pattern']}")
        lines.append(p.get("detail", ""))
        if p.get("targets"):
            lines.append("")
            lines.append("Targets:")
            for t in p["targets"]:
                lines.append(f"- {t}")
        if p.get("mapping"):
            lines.append("")
            lines.append("Field mapping:")
            for field, expr in p["mapping"].items():
                lines.append(f"- {field} -> {expr}")
        lines.append("")
    return "\n".join(lines)


_PERMISSIONSET_OBJECT = re.compile(r"(?im)^\s*permissionset(?:extension)?\s+\d+\s+")


def _git_root(start: Path) -> Optional[Path]:
    """Walk up from `start` to find the enclosing git working tree, if any."""
    current = Path(start).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _clarification_answer(specs_dir: Path, question_id: str) -> str:
    """Return the recorded answer for a clarification question ('' if unanswered).

    Answers are MULTI-LINE (Gemini-Flash run, 2026-07-06): everything from the
    `_Answer:_` marker to the next `## ` heading belongs to the answer — the old
    first-line-only read hid evidence and TEST-shape declarations from every
    consumer (validator, answer-folding), recreating the livelock for natural
    multi-line answers.
    """
    clar = specs_dir / "clarifications.md"
    if not clar.exists():
        return ""
    try:
        lines = clar.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_section = False
    capturing = False
    collected: List[str] = []
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            in_section = question_id in line
            continue
        if not in_section:
            continue
        m = re.match(r"^_Answer:_\s*(.*)$", line.strip())
        if m:
            capturing = True
            if m.group(1).strip():
                collected.append(m.group(1).strip())
            continue
        if capturing and line.strip():
            collected.append(line.strip())
    return "\n".join(collected).strip()


def _answered_question_ids(specs_dir: Path) -> List[str]:
    """All question ids in clarifications.md that carry a non-empty answer, in file order."""
    clar = specs_dir / "clarifications.md"
    if not clar.exists():
        return []
    try:
        text = clar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    ids = []
    for qid in dict.fromkeys(re.findall(r"\b(Q-\d+)\b", text)):
        if _clarification_answer(specs_dir, qid):
            ids.append(qid)
    return ids


def _run_git(root: Path, *args: str, timeout: int = 15) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _source_freshness_findings(project_root: Path) -> List[Dict[str, Any]]:
    """Preflight: ensure the local ERP AL checkout is up to date before implementing.

    Best-effort and fail-open: only reports BC-STALE-SOURCE when it can DEFINITIVELY
    determine the working tree is behind its upstream. If git, a remote, or an upstream
    is unavailable, it stays silent (never blocks on inability to check).
    """
    findings: List[Dict[str, Any]] = []
    if not shutil.which("git"):
        return findings
    root = _git_root(project_root)
    if root is None:
        return findings
    # Refresh remote-tracking refs (non-destructive); ignore failure/offline.
    _run_git(root, "fetch", "--quiet", timeout=25)
    upstream = _run_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream:
        return findings
    counts = _run_git(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    if not counts:
        return findings
    parts = counts.split()
    if len(parts) != 2:
        return findings
    try:
        behind = int(parts[1])
    except ValueError:
        return findings
    if behind > 0:
        findings.append(
            {
                "severity": "error",
                "code": "BC-STALE-SOURCE",
                "message": (
                    f"Local ERP AL checkout is {behind} commit(s) behind {upstream}. "
                    "Pull the latest before implementing — otherwise you may recreate objects/fields "
                    "that already exist upstream (see wi-264484)."
                ),
                "refs": [],
            }
        )
    return findings


def _write_item_charter(project_root: Path, spec_name: str, human_bullets: str, spec: Dict[str, Any]) -> None:
    """Pin the item's intent to disk (immutable core memory) so it cannot drift."""
    data_model = spec.get("data_model", [])
    requirements = spec.get("requirements", [])
    purpose = next((ln.strip() for ln in (human_bullets or "").splitlines() if ln.strip()), "")
    if not purpose:
        purpose = str((spec.get("summary") or {}).get("goal") or "").strip()
    if not purpose:
        purpose = f"Implement spec '{spec_name}' as declared in requirements and scope boundaries."
    operations = {
        "read": any(d.get("read") for d in data_model),
        "update": any(d.get("update") for d in data_model),
    }
    criteria = [r.get("statement", "") for r in requirements if r.get("statement")]
    memory.write_charter(
        project_root,
        spec_name,
        purpose=purpose,
        operations=operations,
        acceptance_criteria=criteria,
    )


def _render_charter_confirmation(project_root: Path, spec_name: str) -> str:
    """Human-gate intent confirmation: the purpose/operations the reviewer must validate
    FIRST. The spec-review gate exists to catch a wrong purpose, so it leads the packet.
    """
    charter = memory.load_charter(project_root, spec_name)
    if not charter:
        return ""
    ops = charter.get("operations", {})
    ops_line = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in ops.items()) or "(unspecified)"
    lines = [
        "# ✋ Confirm the Intent (Charter) — review this FIRST",
        "",
        "Approving this plan means you agree the item's purpose and scope below are correct.",
        "**If any line is wrong, REJECT** — the entire plan is built on this intent.",
        "",
        f"- **Purpose:** {charter.get('purpose', '(none)')}",
        f"- **Operations in scope:** {ops_line}",
        "- **Acceptance criteria:**",
    ]
    lines.extend(f"  - {c}" for c in charter.get("acceptance_criteria", []) or ["(none captured)"])
    lines += [
        "",
        "> Common failure this guards against: treating a `read + update` item as read-only",
        "> (or vice-versa). Verify `Operations in scope` matches the work item before approving.",
        "",
        "---",
    ]
    return "\n".join(lines)


def _permission_grant_for_table(project_root: Path, table_name: str) -> Optional[str]:
    """Return the tabledata grant letters for ``table_name`` declared in any
    permission SET object in the project (e.g. 'r', 'Rm', 'RIMD'), or None.

    Only permissionset/permissionsetextension objects are inspected, never the
    page-level ``Permissions`` property, so an API page's own permission line is
    not mistaken for the app-level entitlement.
    """
    table = (table_name or "").strip().strip('"').strip("'")
    if not table:
        return None
    src = Path(project_root) / "src"
    if not src.exists():
        return None
    grant_pattern = re.compile(
        r'(?im)tabledata\s+"?' + re.escape(table) + r'"?\s*=\s*([A-Za-z]+)'
    )
    for al_file in sorted(src.rglob("*.al")):
        try:
            content = al_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _PERMISSIONSET_OBJECT.search(content):
            continue
        match = grant_pattern.search(content)
        if match:
            return match.group(1)
    return None


def _table_level_dataclassification(project_root: Path, target: str) -> Optional[str]:
    """If ``target`` is a table/tableextension file that declares a table-level
    DataClassification (before the first field), return its value; new fields
    inherit it, so AppSourceCop AS0016 is satisfied without per-field declaration.
    """
    if not target or not target.lower().endswith((".table.al", ".tableext.al")):
        return None
    try:
        content = (Path(project_root) / target).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    first_field = content.lower().find("field(")
    head = content[:first_field] if first_field != -1 else content
    match = re.search(r"(?im)^\s*DataClassification\s*=\s*([A-Za-z]+)\s*;", head)
    return match.group(1) if match else None


def _run_spec_analysis(
    spec: Dict[str, Any],
    design_text: str,
    tasks_text: str,
    project_root: Optional[Path] = None,
) -> tuple:
    """Layer-1 deterministic reviewer: cross-artifact + Business Central policy checks.

    Returns (findings, no_errors). Findings are dicts with severity/code/message/refs.
    """
    findings: List[Dict[str, Any]] = []
    objects = spec.get("objects_to_modify", [])
    requirements = spec.get("requirements", [])
    data_model = spec.get("data_model", [])
    bc = spec.get("bc_metadata", {})
    id_ranges = bc.get("id_ranges", [])
    affixes = bc.get("mandatory_affixes", [])
    allowed = spec.get("scope_boundaries", {}).get("allowed_files", [])

    def add(severity: str, code: str, message: str, refs: Optional[List[str]] = None) -> None:
        findings.append({"severity": severity, "code": code, "message": message, "refs": refs or []})

    # BC-IDRANGE: object IDs must fall inside the app idRanges.
    for obj in objects:
        object_id = obj.get("object_id")
        if object_id is not None and id_ranges and obj.get("id_in_range") is False:
            add(
                "error",
                "BC-IDRANGE",
                f"Object {obj.get('object_name')} (id {object_id}) is outside the app idRanges.",
                [obj.get("id")],
            )

    # BC-READONLY: update required but no writable target exists.
    update_required = any(d.get("update") for d in data_model) or any(
        "persist" in r.get("statement", "").lower() or "patch" in r.get("statement", "").lower()
        for r in requirements
    )
    if update_required and objects:
        writable_targets = [o for o in objects if o.get("writable", True)]
        if not writable_targets:
            names = ", ".join((o.get("object_name") or o.get("target") or "?") for o in objects)
            add(
                "error",
                "BC-READONLY",
                (
                    "Update is required but every target API page is read-only "
                    "(ModifyAllowed=false / DataAccessIntent=ReadOnly / Permissions=R): "
                    f"{names}. A writable API page is needed, or the requirement must be revised."
                ),
            )
        else:
            for obj in objects:
                if not obj.get("writable", True):
                    add(
                        "warning",
                        "BC-READONLY-PARTIAL",
                        f"Target {obj.get('object_name') or obj.get('target')} is read-only; updates will not apply there.",
                        [obj.get("id")],
                    )
            # BC-PERMISSION: a writable page still needs the app permission set to
            # grant Modify on its source table, or PATCH fails at runtime.
            if project_root is not None:
                checked: set = set()
                fallback_table = next(
                    (d.get("source_table", "") for d in data_model if d.get("source_table")),
                    "",
                )
                for obj in writable_targets:
                    table = ((obj.get("api", {}) or {}).get("source_table") or fallback_table or "")
                    table = table.strip().strip('"').strip("'")
                    key = table.lower()
                    if not table or key in checked:
                        continue
                    checked.add(key)
                    grant = _permission_grant_for_table(project_root, table)
                    if grant and "m" not in grant.lower():
                        add(
                            "warning",
                            "BC-PERMISSION",
                            (
                                f"Update is required but the app permission set grants read-only access "
                                f"(tabledata {table} = {grant}); bump it to include Modify (e.g. = RM) so PATCH succeeds."
                            ),
                        )

    # BC-BACKCOMPAT: backward-compatibility requirement must exist.
    if requirements and not any(
        "preserve the existing behavior" in r.get("statement", "").lower() for r in requirements
    ):
        add("error", "BC-BACKCOMPAT", "No requirement preserves existing API behavior (backward compatibility).")

    # BC-SCOPE: scope must be bounded and every target inside it.
    if objects and not allowed:
        add("error", "BC-SCOPE", "scope_boundaries.allowed_files is empty; implementation scope is unbounded.")
    for obj in objects:
        if allowed and obj.get("target") not in allowed:
            add("error", "BC-SCOPE", f"Target {obj.get('target')} is not within allowed_files.", [obj.get("id")])

    # XART: cross-artifact presence in design and tasks.
    for obj in objects:
        target = obj.get("target", "")
        if target and target not in (design_text or ""):
            add("warning", "XART-DESIGN", f"Target {target} is not referenced in DESIGN.md.", [obj.get("id")])
        if target and target not in (tasks_text or ""):
            add("error", "XART-TASKS", f"Target {target} has no implementation task in TASKS.md.", [obj.get("id")])

    # ALCops/LinterCop-derived API page rules.
    for obj in objects:
        api = obj.get("api", {}) or {}
        is_api = (api.get("page_type") or "").strip().lower() == "api" or bool(api.get("entity_name"))
        if not is_api:
            continue
        odata = api.get("odata_key_fields") or ""
        if odata and "systemid" not in odata.lower():
            add(
                "error",
                "ALC-ODATAKEY",
                f"API page {obj.get('object_name') or obj.get('target')} must set ODataKeyFields = SystemId (ALCops LC0061).",
                [obj.get("id")],
            )
        if api.get("has_application_area"):
            add(
                "info",
                "ALC-APPAREA",
                f"ApplicationArea is not applicable to API page {obj.get('object_name') or obj.get('target')} (ALCops LC0060).",
                [obj.get("id")],
            )

    # Recommend enabling the Microsoft breaking-change analyzer when absent.
    analyzers = [a.lower() for a in bc.get("analyzers", [])]
    if objects and "appsourcecop" not in analyzers:
        add(
            "info",
            "BC-ANALYZER",
            "Enable AppSourceCop (and ALCops) to catch breaking changes and API issues at compile time.",
        )

    # Table-target scope: checks below are only relevant when a table/tableextension is
    # part of the implementation scope. Pure API-page mapping may expose long, readable
    # API attribute names while still binding to existing short table fields.
    table_targets = [
        o.get("target")
        for o in objects
        if str(o.get("type", "")).lower() in ("table", "tableextension")
        or str(o.get("target", "")).lower().endswith((".table.al", ".tableext.al"))
    ]
    table_targets += [f for f in allowed if str(f).lower().endswith((".table.al", ".tableext.al"))]

    # BC-FIELDLEN: table field names must be <= 30 chars (AL0468 / SQL safety). New fields
    # that are too long will not deploy cleanly and diverge from the codebase naming.
    if data_model and table_targets:
        too_long = [
            str(d.get("source_field") or d.get("field") or "")
            for d in data_model
            if len(str(d.get("source_field") or d.get("field") or "")) > 30
        ]
        if too_long:
            preview = ", ".join(f"{f} ({len(f)})" for f in too_long[:5]) + ("..." if len(too_long) > 5 else "")
            add(
                "warning",
                "BC-FIELDLEN",
                (
                    "Table field names exceed the 30-character AL0468 limit (SQL safety); "
                    f"abbreviate to match codebase conventions: {preview}."
                ),
            )

    # BC-AFFIX: new table fields may need a mandatory affix (declared in the table app).
    if affixes and data_model and table_targets:
        missing = [
            sf
            for sf in (str(d.get("source_field") or d.get("field") or "") for d in data_model)
            if sf and not any(a.lower() in sf.lower() for a in affixes)
        ]
        if missing:
            preview = ", ".join(missing[:5]) + ("..." if len(missing) > 5 else "")
            add(
                "info",
                "BC-AFFIX",
                f"When declared in the table extension, new fields may require affix {affixes}: {preview}.",
            )

    # BC-DATACLASS: only relevant when this change adds fields to a table in scope.
    # Pure API-page mapping reuses existing table fields, so the reminder is noise.
    if data_model and table_targets:
        inherited = None
        if project_root is not None:
            for target in table_targets:
                inherited = _table_level_dataclassification(project_root, target)
                if inherited:
                    break
        if inherited:
            add(
                "info",
                "BC-DATACLASS",
                f"New table fields inherit the table-level DataClassification = {inherited} "
                "(AppSourceCop AS0016 satisfied).",
            )
        else:
            add(
                "info",
                "BC-DATACLASS",
                "New table fields must set DataClassification (AppSourceCop AS0016).",
            )

    no_errors = not any(f["severity"] == "error" for f in findings)
    return findings, no_errors


def _render_analysis(spec_name: str, findings: List[Dict[str, Any]]) -> str:
    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    infos = [f for f in findings if f["severity"] == "info"]
    lines = [
        f"# Spec Analysis: {spec_name}",
        "",
        "Deterministic cross-artifact and Business Central policy analysis (Layer 1 reviewer).",
        "",
        f"- Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(infos)}",
        "",
    ]
    for label, items in (("Errors", errors), ("Warnings", warnings), ("Info", infos)):
        if not items:
            continue
        lines.append(f"## {label}")
        for f in items:
            ref = f" (refs: {', '.join(str(r) for r in f['refs'])})" if f.get("refs") else ""
            lines.append(f"- [{f['code']}] {f['message']}{ref}")
        lines.append("")
    return "\n".join(lines)


def _render_requirements_checklist(spec: Dict[str, Any], no_placeholders: bool) -> str:
    requirements = spec.get("requirements", [])
    traceability = spec.get("traceability", {})
    open_questions = spec.get("open_questions", [])
    assumptions = spec.get("assumptions", [])

    no_markers = no_placeholders and not open_questions
    all_ears = bool(requirements) and all(r.get("ears_type") in _EARS_TYPES for r in requirements)
    measurable = bool(requirements) and all(r.get("acceptance_tests") for r in requirements)
    no_speculation = not open_questions and all(a.get("reversible", True) for a in assumptions)
    req_ids = {r.get("id") for r in requirements}
    r2o = traceability.get("requirement_to_object", {})
    r2t = traceability.get("requirement_to_test", {})
    traceable = bool(req_ids) and all(r2o.get(rid) and r2t.get(rid) for rid in req_ids)

    def box(value: bool) -> str:
        return "[x]" if value else "[ ]"

    return "\n".join(
        [
            "## Requirements Checklist (Unit Tests for English)",
            "",
            f"- {box(no_markers)} No clarification markers or open questions remain",
            f"- {box(all_ears)} Every requirement is testable and unambiguous (EARS)",
            f"- {box(measurable)} Every requirement has measurable acceptance criteria",
            f"- {box(no_speculation)} No speculative or non-reversible assumptions",
            f"- {box(traceable)} Every requirement traces to an object and a test",
        ]
    )


def _scan_for_placeholders(paths: List[Path]) -> bool:
    """Return True when NO forbidden placeholder token is present in any file."""
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for token in _PLACEHOLDER_TOKENS:
            if token in text:
                return False
    return True


def _evaluate_schema_invariants(spec: Dict[str, Any]) -> tuple:
    """Enforce the bulletproof machine-spec invariants (Section 4B.2 of the plan)."""
    checks: Dict[str, bool] = {}
    failures: List[str] = []

    requirements = spec.get("requirements", [])
    data_model = spec.get("data_model", [])
    objects = spec.get("objects_to_modify", []) + spec.get("objects_to_create", [])
    acceptance_tests = spec.get("acceptance_tests", [])
    traceability = spec.get("traceability", {})
    req_ids = {r.get("id") for r in requirements}

    every_ears = bool(requirements) and all(r.get("ears_type") in _EARS_TYPES for r in requirements)
    checks["every_requirement_is_ears"] = every_ears
    if requirements and not every_ears:
        failures.append("Not every requirement uses an EARS pattern.")

    every_has_test = bool(requirements) and all(r.get("acceptance_tests") for r in requirements)
    checks["every_requirement_has_test"] = every_has_test
    if requirements and not every_has_test:
        failures.append("Not every requirement is linked to an acceptance test.")

    every_has_ev = bool(requirements) and all(r.get("evidence_refs") for r in requirements)
    checks["all_required_claims_evidenced"] = every_has_ev
    if requirements and not every_has_ev:
        failures.append("Not every requirement has evidence references.")

    field_to_object = traceability.get("field_to_object", {})
    every_field_mapped = all(field_to_object.get(d.get("field")) for d in data_model) if data_model else True
    checks["every_field_mapped_to_object"] = every_field_mapped
    if data_model and not every_field_mapped:
        failures.append("Not every data-model field maps to an object.")

    every_obj_ev = bool(objects) and all(o.get("evidence_refs") for o in objects)
    checks["every_object_has_evidence"] = every_obj_ev
    if objects and not every_obj_ev:
        failures.append("Not every object has evidence references.")

    tests_valid = bool(acceptance_tests) and all(t.get("requirement_ref") in req_ids for t in acceptance_tests)
    checks["acceptance_tests_reference_requirements"] = tests_valid
    if acceptance_tests and not tests_valid:
        failures.append("Some acceptance tests reference unknown requirements.")

    no_insufficient = all(r.get("status") != "insufficient" for r in requirements)
    checks["no_insufficient_requirements"] = no_insufficient
    if not no_insufficient:
        failures.append("Spec contains insufficient (unresolved) requirements.")

    checks["evidence_matrix_present"] = bool(spec.get("evidence"))
    if not spec.get("evidence"):
        failures.append("Evidence matrix is empty.")

    r2o = traceability.get("requirement_to_object", {})
    r2t = traceability.get("requirement_to_test", {})
    trace_complete = bool(req_ids) and all(r2o.get(rid) and r2t.get(rid) for rid in req_ids)
    checks["traceability_complete"] = trace_complete
    if req_ids and not trace_complete:
        failures.append("Traceability maps are incomplete.")

    checks["schema_version_present"] = bool(spec.get("schema_version"))
    if not spec.get("schema_version"):
        failures.append("Machine spec is missing schema_version.")

    return checks, failures


def _write_clarification_file(
    specs_dir: Path,
    spec_name: str,
    questions: List[Dict[str, Any]],
    code_examples: List[Dict[str, str]],
    inferred_rules: Dict[str, Dict[str, str]],
    quality_gate: Optional[Dict[str, Any]] = None,
) -> Path:
    specs_dir.mkdir(parents=True, exist_ok=True)
    clar_path = specs_dir / "clarifications.md"
    # A recorded human answer is INPUT DATA — regenerating the file must never wipe
    # it. That includes questions NOT in this write's list: the Q-902 path used to
    # rewrite the file with only Q-902, erasing the answered Q-001 (observed live —
    # a two-writer ping-pong that deadlocked the plan gate). EVERY not-asked question
    # is preserved VERBATIM (answered or not): dropping an unanswered foreign heading
    # makes the answer tool report not_found for it on the next call (same ping-pong,
    # one step later).
    existing: List[Dict[str, str]] = []
    if clar_path.exists():
        try:
            current = clar_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            current = []
        qid, qtext = "", ""
        for line in current:
            m = re.match(r"^##\s+(Q-\d+):\s*(.*)$", line.strip())
            if m:
                if qid:  # previous question had no answer line content
                    existing.append({"id": qid, "question": qtext, "answer": ""})
                qid, qtext = m.group(1), m.group(2)
                continue
            if qid:
                am = re.match(r"^_Answer:_\s*(.*)$", line.strip())
                if am is not None:
                    existing.append({"id": qid, "question": qtext,
                                     "answer": am.group(1).strip()})
                    qid = ""
        if qid:
            existing.append({"id": qid, "question": qtext, "answer": ""})
    asked_ids = {str(q["id"]) for q in questions}
    preserved = [e for e in existing if e["id"] not in asked_ids]

    lines = [
        f"# Clarifications for: {spec_name}",
        "",
        "Review these questions before implementation.",
        "",
    ]
    for question in questions:
        lines.append(f"## {question['id']}: {question['question']}")
        prior = _clarification_answer(specs_dir, str(question["id"]))
        lines.append(f"_Answer:_ {prior}" if prior else "_Answer:_ ")
        lines.append("")
    for entry in preserved:
        lines.append(f"## {entry['id']}: {entry['question']}")
        lines.append(f"_Answer:_ {entry['answer']}" if entry["answer"] else "_Answer:_ ")
        lines.append("")

    if quality_gate and quality_gate.get("failures"):
        lines.extend([
            "## Deterministic Quality Gate Failures",
            "",
        ])
        for failure in quality_gate["failures"]:
            lines.append(f"- {failure}")
        lines.append("")

    if code_examples:
        lines.extend([
            "## Existing Code Examples to Reuse",
            "",
            "Use these concrete patterns already present in the AL codebase as copy/adapt references:",
            "",
        ])
        for example in code_examples:
            lines.append(
                f"- {example['kind']}: {example['name']} -> {example['source_field']} ({example['file']})"
            )
        lines.append("")

    if inferred_rules:
        lines.extend([
            "## Inferred Rule From Existing ERP AL",
            "",
        ])
        for rule in inferred_rules.values():
            lines.append(f"- Rule: {rule['rule']}")
            lines.append(f"- Reason: {rule['reason']}")
            lines.append(f"- Evidence: {rule['evidence']}")
            lines.append("")

    clar_path.write_text("\n".join(lines), encoding="utf-8")
    return clar_path


def _infer_on_hold_till_rule(project_root: Path) -> Optional[Dict[str, str]]:
    """Infer likely OnHoldTill behavior from existing AL conventions.

    First-principles rule: if existing APIs expose OnHoldDate as direct mappings
    without validation hooks, default to pass-through (no additional date validation).
    """
    src_dir = Path(project_root) / "src"
    if not src_dir.exists():
        return None

    mappings: List[str] = []
    validations: List[str] = []
    for al_file in sorted(src_dir.rglob("*.al")):
        content = al_file.read_text(encoding="utf-8", errors="replace")
        lower = content.lower()

        if "onholddate" in lower or "on hold date" in lower:
            if re.search(r'field\("onHoldDate"\s*;\s*Rec\.', content):
                mappings.append(str(al_file.relative_to(project_root)))
            if "onvalidate" in lower and ("onholddate" in lower or "on hold date" in lower):
                validations.append(str(al_file.relative_to(project_root)))

    if mappings and not validations:
        return {
            "rule": "no_validation_inferred",
            "reason": "Existing OnHoldDate API fields are exposed as direct mappings without date validation hooks.",
            "evidence": ", ".join(sorted(set(mappings))[:3]),
        }
    return None


def _infer_subprocess_completion_rule(project_root: Path) -> Optional[Dict[str, str]]:
    """Infer subprocess completion behavior from existing Rental Mutation logic."""
    src_dir = Path(project_root) / "src"
    if not src_dir.exists():
        return None

    evidence_files: List[str] = []
    found_milestone_derivation = False
    found_auto_completion_gate = False

    for al_file in sorted(src_dir.rglob("*.al")):
        content = al_file.read_text(encoding="utf-8", errors="replace")
        lower = content.lower()
        rel = str(al_file.relative_to(project_root))

        if "setleavingtenantsubproccompletedwhenmilestonesarecompleted" in lower:
            found_milestone_derivation = True
            evidence_files.append(rel)

        if (
            "leavingtenantsubproccompleted := true" in lower
            and "newrentalsubproccompleted := true" in lower
        ):
            found_milestone_derivation = True
            evidence_files.append(rel)

        if (
            "if not rentalmutation.leavingtenantsubproccompleted then" in lower
            and "if not rentalmutation.newrentalsubproccompleted then" in lower
        ):
            found_auto_completion_gate = True
            evidence_files.append(rel)

    if found_milestone_derivation and found_auto_completion_gate:
        unique_evidence = sorted(set(evidence_files))
        return {
            "rule": "subprocess_completion_derived_from_milestones",
            "reason": (
                "Existing Rental Mutation logic derives subprocess completion from milestone fields "
                "and uses both completion flags as gate checks for auto-completion flow."
            ),
            "evidence": ", ".join(unique_evidence[:3]),
        }
    return None


def _infer_business_rules(project_root: Path) -> Dict[str, Dict[str, str]]:
    rules: Dict[str, Dict[str, str]] = {}
    on_hold = _infer_on_hold_till_rule(project_root)
    if on_hold:
        rules["on_hold_till"] = on_hold

    completion = _infer_subprocess_completion_rule(project_root)
    if completion:
        rules["subprocess_completion"] = completion

    return rules


def _detect_clarification_questions(
    context: str,
    inferred_rules: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Return only high-signal clarification questions.

    Unlike bc_clarify, this helper only returns questions when the text looks
    under-specified. If the description is clear enough, it returns [].
    """
    context_lower = context.lower()
    questions: List[Dict[str, Any]] = []

    checks = [
        (
            ["notify", "notification"],
            "How should the user be notified when this change completes?",
        ),
        (
            ["validate", "validation"],
            "What exact validation rules should apply?",
        ),
        (
            ["background", "job queue"],
            "Should this run in the background via Job Queue?",
        ),
        # NOTE: two ITEM-SPECIFIC questions (SubprocessLeavingTenantOnHoldTill on-hold
        # semantics keyed on the bare word 'date'; subprocess-completion keyed on
        # 'subprocess'+'completed') were DELETED here 2026-07-04. They were scaffolding
        # for one past item, wrongly generalized: the keys never matched their OWN
        # item's text but fired verbatim on unrelated items (observed live: wi267598,
        # a rental-proposal bug, was asked about another item's on-hold fields).
        # Hardcoded checks must stay ITEM-AGNOSTIC; item-specific ambiguity belongs to
        # the clarify engine's spec-derived questions.
        (
            ["delete", "remove"],
            "Should deletion/removal be always allowed, conditional, or forbidden?",
        ),
        (
            ["select"],
            "Which page or record should the user select from?",
        ),
    ]

    for keywords, question in checks:
        if all(re.search(rf"\b{re.escape(keyword)}\b", context_lower) for keyword in keywords):
            questions.append(
                {
                    "id": f"Q-{len(questions) + 1:03d}",
                    "question": question,
                    "type": "text",
                }
            )

    return questions


async def handle_prepare_review(
    project_root: str,
    spec_name: str,
    human_bullets: str,
    idempotency_key: str,
    analysis: Optional[str] = None,
    clarifications: Optional[str] = None,
    template: str = "tdd",
) -> Dict[str, Any]:
    """Single-entry workflow: questions or review packet."""
    root = Path(project_root).resolve()
    await handle_init(project_root=str(root), module_name=root.name)
    api_hint = _extract_api_hint(human_bullets)
    requested_fields = _extract_requested_fields(human_bullets)
    expected_versions = _infer_expected_api_versions(root, api_hint)

    code_examples = _collect_code_examples(
        root,
        human_bullets,
        api_hint=api_hint,
        preferred_terms=requested_fields,
        expected_versions=expected_versions,
    )
    inferred_rules = _infer_business_rules(root)

    questions = _detect_clarification_questions(human_bullets, inferred_rules=inferred_rules)
    if questions:
        specs_dir = specs_root(root) / spec_name
        # An ANSWERED question must not re-block the packet: the clarifications engine
        # already validates answer quality (evidence-grounded, no uncertainty) — only
        # genuinely open questions send the planner back (observed live: answered
        # Q-001 deadlocked prepare_review against a stale-REVIEW approval gate).
        open_questions = [q for q in questions
                          if not _clarification_answer(specs_dir, str(q["id"])).strip()]
        if open_questions:
            clar_path = _write_clarification_file(
                specs_dir=specs_dir,
                spec_name=spec_name,
                questions=questions,
                code_examples=code_examples,
                inferred_rules=inferred_rules,
            )
            return {
                "status": "needs_clarification",
                "questions": open_questions,
                "code_examples": code_examples,
                "inferred_rules": inferred_rules,
                "file_path": str(clar_path),
            }

    # ANSWER CONSUMPTION (e2e walk finding 2026-07-05, Q-901 livelock): an answered
    # clarification is spec INPUT, not a checkbox. The old flow validated the answer,
    # recorded it, then regenerated the spec from the ORIGINAL bullets — the same
    # quality-gate failure returned the same question forever, and an agent obeying
    # the machine span in place. Fold every recorded answer into the bullets so the
    # generator actually sees what the human said. Embedded 'TEST <shape>:' phrases
    # are split onto their own lines for the line-anchored shape parser.
    _answers_dir = specs_root(root) / spec_name
    _test_decl = re.compile(r"\s*(?=TEST\s+(?:happy|negative|edge|regression|api)\s*:)", re.IGNORECASE)
    _folded: List[str] = []
    for _qid in _answered_question_ids(_answers_dir):
        _ans = _clarification_answer(_answers_dir, _qid)
        if not _ans.strip():
            continue
        for _line in _test_decl.split(_ans.strip()):
            if _line.strip():
                _folded.append(_line.strip())
    if _folded:
        human_bullets = human_bullets + "\n\nClarification answers (authoritative):\n" + "\n".join(
            f"- {a}" for a in _folded)

    spec_result = await handle_write_spec(
        project_root=str(root),
        spec_name=spec_name,
        human_bullets=human_bullets,
        analysis=analysis,
        clarifications=clarifications,
        idempotency_key=idempotency_key,
        template=template,
    )
    if spec_result.get("status", "").startswith("blocked"):
        return {
            "status": "blocked_invalid_spec_contract",
            "message": spec_result.get("reason", "Spec generation blocked by contract gate."),
            "spec_result": spec_result,
        }
    design_result = await handle_plan_design(
        project_root=str(root),
        spec_name=spec_name,
        machine_spec_path=spec_result["machine_spec_path"],
    )
    if design_result.get("status", "").startswith("blocked"):
        specs_dir = specs_root(root) / spec_name
        reason = design_result.get("reason", "Design generation blocked by spec contract gate.")
        quality_gate = {
            "pass": False,
            "failures": [reason],
            "checks": {"design_generation_ok": False},
        }
        quality_path = _write_quality_gate_file(specs_dir=specs_dir, quality_gate=quality_gate)
        questions = [
            {
                "id": "Q-901",
                "question": "Please confirm the exact AL object/file targets so deterministic scope can be enforced.",
                "type": "text",
            }
        ]
        clar_path = _write_clarification_file(
            specs_dir=specs_dir,
            spec_name=spec_name,
            questions=questions,
            code_examples=code_examples,
            inferred_rules=inferred_rules,
            quality_gate=quality_gate,
        )
        return {
            "status": "needs_clarification",
            "questions": questions,
            "code_examples": code_examples,
            "inferred_rules": inferred_rules,
            "quality_gate": quality_gate,
            "quality_gate_path": str(quality_path),
            "file_path": str(clar_path),
            "design_result": design_result,
        }
    tasks_result = await handle_breakdown_tasks(
        project_root=str(root),
        spec_name=spec_name,
        design_path=design_result["design_path"],
    )
    if tasks_result.get("status", "").startswith("blocked"):
        specs_dir = specs_root(root) / spec_name
        reason = tasks_result.get("reason", "Task breakdown blocked by spec contract gate.")
        quality_gate = {
            "pass": False,
            "failures": [reason],
            "checks": {"task_breakdown_ok": False},
        }
        quality_path = _write_quality_gate_file(specs_dir=specs_dir, quality_gate=quality_gate)
        questions = [
            {
                "id": "Q-901",
                "question": "Please confirm the exact AL object/file targets so deterministic scope can be enforced.",
                "type": "text",
            }
        ]
        clar_path = _write_clarification_file(
            specs_dir=specs_dir,
            spec_name=spec_name,
            questions=questions,
            code_examples=code_examples,
            inferred_rules=inferred_rules,
            quality_gate=quality_gate,
        )
        return {
            "status": "needs_clarification",
            "questions": questions,
            "code_examples": code_examples,
            "inferred_rules": inferred_rules,
            "quality_gate": quality_gate,
            "quality_gate_path": str(quality_path),
            "file_path": str(clar_path),
            "design_result": design_result,
            "tasks_result": tasks_result,
        }

    spec = json.loads(Path(spec_result["machine_spec_path"]).read_text(encoding="utf-8"))
    # --- Phase 0.5: Code read-context (precedents/conventions on latest+clean source) ---
    from bc_agentic_mcp import code_context as _code_context
    _cc = _code_context.handle_read_code_context(str(root), spec_name)
    has_code_context = _cc.get("status") == "ok"
    if not has_code_context:
        # The gate ASKS Q-902 ("rerun on clean tree OR explicitly allow a bypass") —
        # so an ANSWERED Q-902 must actually satisfy it, else the question is a
        # deterministic dead-end: answered bypass, same refusal, forever.
        q902_answer = _clarification_answer(specs_root(root) / spec_name, "Q-902")
        if q902_answer:
            has_code_context = True
            _cc = {**_cc, "bypass": {"question": "Q-902", "answer": q902_answer}}
    # --- Durable core memory: pin the item's purpose/operations so intent can't drift ---
    _write_item_charter(root, spec_name, human_bullets, spec)
    quality_gate = _evaluate_quality_gates(
        spec=spec,
        requested_fields=requested_fields,
        api_hint=api_hint,
        task_count=int(tasks_result.get("task_count", 0)),
        total_estimated_objects=int(tasks_result.get("total_estimated_objects", 0)),
        require_object_targets=bool(api_hint or requested_fields),
        code_examples=code_examples,
        expected_versions=expected_versions,
        has_code_context=has_code_context,
    )
    specs_dir = specs_root(root) / spec_name
    analysis_path: Optional[Path] = None

    if api_hint or requested_fields:
        schema_checks, schema_failures = _evaluate_schema_invariants(spec)
        review_path_value = tasks_result.get("review_path")
        placeholder_paths = [
            specs_dir / "TDD.md",
            specs_dir / "DESIGN.md",
            specs_dir / "TASKS.md",
        ]
        if review_path_value:
            placeholder_paths.append(Path(review_path_value))
        no_placeholders = _scan_for_placeholders(placeholder_paths)
        schema_checks["no_placeholders_remaining"] = no_placeholders
        if not no_placeholders:
            schema_failures.append("Generated artifacts contain placeholder markers.")

        design_text = ""
        tasks_text = ""
        try:
            design_text = (specs_dir / "DESIGN.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        try:
            tasks_text = (specs_dir / "TASKS.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        findings, analysis_no_errors = _run_spec_analysis(spec, design_text, tasks_text, root)

        # --- Planning-time pattern recognition (check everywhere before asking) ---
        patterns, pattern_findings = _recognize_patterns(spec, root)
        findings.extend(pattern_findings)
        # --- Preflight: verify the local ERP AL checkout is current before changes ---
        findings.extend(_source_freshness_findings(root))
        patterns_path = specs_dir / "PATTERNS.md"
        patterns_path.write_text(_render_patterns(spec_name, patterns), encoding="utf-8")
        (specs_dir / "patterns.json").write_text(json.dumps(patterns, indent=2), encoding="utf-8")

        # --- Auto-improver: learn from findings and apply confirmed lessons ---
        current_api = ""
        for obj in spec.get("objects_to_modify", []):
            entity = (obj.get("api", {}) or {}).get("entity_name")
            if entity:
                current_api = entity.strip().strip("'\"").lower()
                break
        if not current_api:
            current_api = (api_hint or "").lower()

        for finding in findings:
            if finding["code"] in _TRANSIENT_FINDING_CODES:
                continue
            if finding["severity"] in ("error", "warning"):
                lessons_store.record_observation(
                    root,
                    code=finding["code"],
                    message=finding["message"],
                    severity=finding["severity"],
                    match={"api": current_api},
                    spec_name=spec_name,
                )

        present_codes = {finding["code"] for finding in findings}
        for lesson in lessons_store.applicable_lessons(
            root, api=current_api, keywords_text=human_bullets
        ):
            if lesson["code"] in present_codes or lesson["code"] in _TRANSIENT_FINDING_CODES:
                continue
            # Replayed lessons are ADVISORY context, not fresh findings — they must never
            # fail the gate (the current deterministic analysis is the gating authority).
            # A stale learned lesson (e.g. a page that USED to be read-only) would otherwise
            # block a spec that has already fixed the condition.
            lesson_severity = "warning" if lesson["severity"] == "error" else lesson["severity"]
            findings.append(
                {
                    "severity": lesson_severity,
                    "code": f"LESSON:{lesson['code']}",
                    "message": f"[learned] {lesson['message']}",
                    "refs": [lesson["id"]],
                }
            )

        analysis_no_errors = not any(f["severity"] == "error" for f in findings)
        analysis_path = specs_dir / "ANALYSIS.md"
        analysis_path.write_text(_render_analysis(spec_name, findings), encoding="utf-8")
        (specs_dir / "analysis.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
        schema_checks["analysis_no_errors"] = analysis_no_errors
        for finding in findings:
            if finding["severity"] == "error":
                schema_failures.append(f"[{finding['code']}] {finding['message']}")

        quality_gate["checks"].update(schema_checks)
        quality_gate["failures"].extend(schema_failures)
        quality_gate["pass"] = not quality_gate["failures"]
        # --- Durable checkpoint: record the gate outcome for later re-anchoring ---
        memory.append_checkpoint(
            root,
            spec_name,
            kind="gate",
            summary=f"quality gate pass={quality_gate['pass']}",
            details={"failures": quality_gate["failures"][:10]},
        )

    quality_path = _write_quality_gate_file(specs_dir=specs_dir, quality_gate=quality_gate)
    trace_path: Optional[Path] = None
    try:
        from bc_agentic_mcp import traceability as _trace
        _tr = quality_gate.get("traceability")
        if _tr is not None:
            trace_path = specs_dir / "TRACE.md"
            trace_path.write_text(_trace.render_trace_md(_tr, spec_name), encoding="utf-8")
    except Exception:
        trace_path = None

    if not quality_gate["pass"]:
        if any("Code read-context is missing or blocked" in f for f in quality_gate.get("failures", [])):
            # EXPLICIT question contract (human feedback 2026-07-04: "not descriptive,
            # not user friendly"): plain words, the ACTUAL file names (fetched live
            # when the gate didn't carry them), and what each choice means in
            # consequences — never "file list unavailable".
            repo_info = (_cc or {}).get("repo") or {}
            reasons = "; ".join(repo_info.get("reasons") or []) or "the working folder is not clean"
            branch = ((repo_info.get("status") or {}).get("branch")) or "unknown branch"
            dirty_files = list(((repo_info.get("status") or {}).get("files") or []))
            if not dirty_files:
                try:
                    import subprocess as _sp
                    out = _sp.run(["git", "status", "--porcelain"], cwd=str(root),
                                  capture_output=True, text=True, timeout=15,
                                  stdin=_sp.DEVNULL)
                    dirty_files = [line[3:].strip() for line in
                                   (out.stdout or "").splitlines() if line.strip()]
                except Exception:
                    dirty_files = []
            dirty = "\n".join(f"  - {f}" for f in dirty_files[:10]) or "  - (could not list them — run `git status` to see)"
            questions = [
                {
                    "id": "Q-902",
                    "question": (
                        f"Your working folder has changes that are not committed yet "
                        f"(branch: {branch}; technical reason: {reasons}).\n"
                        f"These files are modified:\n{dirty}\n"
                        "Why this matters: the plan was built from files that could still change, "
                        "so the review might not describe what actually gets shipped.\n"
                        "You have two choices:\n"
                        "  1. Commit or stash those files, then regenerate this packet — the safest option.\n"
                        "  2. Answer this question explaining why those files belong to THIS item "
                        "(for example: they are this item's own files, mid-integration) — the plan "
                        "then proceeds and the commit happens right after."
                    ),
                    "type": "text",
                }
            ]
        else:
            # MASKED-FAILURE LIVELOCK FIX (e2e walk 2026-07-05): the gate knew exactly
            # what was wrong (e.g. 'declare TEST negative/edge lines') but asked a
            # GENERIC file-list question — answering it could never fix the failure,
            # so agents obeying the machine span forever. The question must carry the
            # ACTUAL failures; the answer is folded into the bullets on the next run.
            failure_lines = "\n".join(f"  - {f}" for f in quality_gate.get("failures", [])[:6]) \
                or "  - (no failure detail — report this as a bug)"
            questions = [
                {
                    "id": "Q-901",
                    "question": (
                        "The plan quality gate failed with these exact findings:\n"
                        f"{failure_lines}\n"
                        "Answer this question with the missing information stated as spec "
                        "bullets (for example 'TEST negative: …' / 'TEST edge: …' lines, or "
                        "the exact .al file paths in scope). Your answer is folded into the "
                        "spec input verbatim and the packet regenerates from it."
                    ),
                    "type": "text",
                }
            ]
        clar_path = _write_clarification_file(
            specs_dir=specs_dir,
            spec_name=spec_name,
            questions=questions,
            code_examples=code_examples,
            inferred_rules=inferred_rules,
            quality_gate=quality_gate,
        )
        return {
            "status": "needs_clarification",
            "questions": questions,
            "code_examples": code_examples,
            "inferred_rules": inferred_rules,
            "quality_gate": quality_gate,
            "quality_gate_path": str(quality_path),
            "trace_path": str(trace_path) if trace_path else None,
            "analysis_path": str(analysis_path) if analysis_path else None,
            "file_path": str(clar_path),
        }

    if not tasks_result.get("review_path"):
        return {
            "status": "blocked_invalid_spec_contract",
            "message": "Review artifact path is missing from task breakdown output.",
            "design_result": design_result,
            "tasks_result": tasks_result,
        }

    review_path = Path(tasks_result["review_path"])

    if code_examples or inferred_rules:
        review_text = review_path.read_text(encoding="utf-8")
        if code_examples:
            review_text += "\n\n## Existing Code Examples to Reuse\n\n"
            review_text += "Use these concrete patterns already present in the AL codebase as copy/adapt references.\n\n"
            for example in code_examples:
                review_text += (
                    f"- {example['kind']}: {example['name']} -> {example['source_field']} ({example['file']})\n"
                )

        if inferred_rules:
            review_text += "\n## Inferred Rule From Existing ERP AL\n\n"
            for rule in inferred_rules.values():
                review_text += f"- Rule: {rule['rule']}\n"
                review_text += f"- Reason: {rule['reason']}\n"
                review_text += f"- Evidence: {rule['evidence']}\n"
        review_path.write_text(review_text, encoding="utf-8")

    rationale_text = _render_decision_rationale(spec)
    # Live decision story (ALL lanes, human ask 2026-07-04): checkpointed decisions,
    # ticket-vs-code corrections, and the bug's root-cause reasoning — in plain language.
    from bc_agentic_mcp.checkpoints import plain_language_decisions
    live_log = plain_language_decisions(root, spec_name)
    if live_log != "(no recorded decisions yet)":
        rationale_text += ("\n### 8. Decisions made along the way (live log)\n\n"
                           + live_log + "\n")
    rationale_path = specs_dir / "RATIONALE.md"
    rationale_path.write_text(rationale_text, encoding="utf-8")
    checklist_text = _render_requirements_checklist(
        spec, quality_gate["checks"].get("no_placeholders_remaining", True)
    )
    analysis_summary = ""
    if analysis_path and analysis_path.exists():
        analysis_summary = analysis_path.read_text(encoding="utf-8")
    patterns_summary = ""
    patterns_file = specs_dir / "PATTERNS.md"
    if patterns_file.exists():
        patterns_summary = patterns_file.read_text(encoding="utf-8")
    review_with_rationale = review_path.read_text(encoding="utf-8")
    review_with_rationale += "\n\n" + checklist_text + "\n"
    if patterns_summary:
        review_with_rationale += "\n\n" + patterns_summary + "\n"
    if analysis_summary:
        review_with_rationale += "\n\n" + analysis_summary + "\n"
    review_with_rationale += "\n\n" + rationale_text + "\n"
    # Lead the human-review packet with the intent confirmation so the gate validates
    # PURPOSE + OPERATIONS before anything else.
    charter_confirmation = _render_charter_confirmation(root, spec_name)
    if charter_confirmation:
        review_with_rationale = charter_confirmation + "\n\n" + review_with_rationale
    review_path.write_text(review_with_rationale, encoding="utf-8")

    _append_enforcement_to_review(review_path=review_path, quality_gate=quality_gate, quality_path=quality_path)

    # Vendor health check: surface drift or missing vendor in the packet.
    from bc_agentic_mcp import knowledge as _knowledge
    vendor_health = _knowledge.check_vendor_health(root)
    vendor_health_warnings = vendor_health.get("errors") or []

    return {
        "status": "ready_for_review",
        "review_path": str(review_path),
        "spec_path": spec_result["machine_spec_path"],
        "design_path": design_result["design_path"],
        "tasks_path": tasks_result["tasks_path"],
        "rationale_path": str(rationale_path),
        "analysis_path": str(analysis_path) if analysis_path else None,
        "patterns_path": str(specs_dir / "PATTERNS.md"),
        "charter_path": str(specs_dir / "CHARTER.md"),
        "code_examples": code_examples,
        "inferred_rules": inferred_rules,
        "quality_gate": quality_gate,
        "quality_gate_path": str(quality_path),
        "trace_path": str(trace_path) if trace_path else None,
        "vendor_health": vendor_health,
        "vendor_health_warnings": vendor_health_warnings,
    }