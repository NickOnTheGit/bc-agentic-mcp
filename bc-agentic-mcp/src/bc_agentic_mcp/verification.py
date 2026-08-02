"""Verification / coverage proof for a spec: map every acceptance criterion (from the
Charter) to the test(s) that exercised it and their result, so "done" means every part
of the item's description is covered by a passing test — not a claim.

Test results are recorded as durable ``kind="test"`` checkpoints; the verification report
is derived from the Charter's acceptance_criteria + those results.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import security


def check_knowledge_coverage(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """Check whether BCQuality articles were applied during the review phase.

    ``required`` is True only when the review packet contained ≥1 article
    (packet_article_count > 0, persisted to review_packet_meta.json).
    ``ok`` requires a knowledge_trace.json with ≥1 article applied.
    """
    from bc_agentic_mcp.workspace import specs_root as _sr
    spec_dir = _sr(Path(project_root)) / spec_name

    # Read packet metadata to know exactly which articles were in the review packet.
    packet_article_count = 0
    packet_article_paths: List[str] = []
    packet_id = ""
    vendor_commit = ""
    metadata_error = ""
    meta_path = spec_dir / "review_packet_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise ValueError("metadata is not an object")
            packet_article_count = int(meta.get("packet_article_count") or 0)
            packet_article_paths = [str(p) for p in meta.get("packet_article_paths") or []]
            packet_id = str(meta.get("packet_id") or "")
            vendor_commit = str((meta.get("vendor_health") or {}).get("commit") or "")
            if meta.get("knowledge_error"):
                metadata_error = "knowledge retrieval failed while building the review packet"
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            metadata_error = f"review packet metadata unreadable: {exc}"

    required = packet_article_count > 0 or bool(packet_article_paths) or bool(metadata_error)
    if metadata_error:
        return {
            "required": True,
            "ok": False,
            "reason": metadata_error,
            "packet_article_count": packet_article_count,
            "packet_article_paths": packet_article_paths,
            "articles_applied": [],
        }

    # Read the signed trace to know if the current packet's articles were actually read.
    trace_path = spec_dir / "knowledge_trace.json"
    applied: List[str] = []
    receipts: List[str] = []
    trace_error = ""
    if trace_path.exists():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            if not isinstance(trace, dict):
                raise ValueError("trace is not an object")
            applied = [str(p) for p in trace.get("articles_applied") or []]
            receipts = [str(r) for r in trace.get("knowledge_receipts") or []]
            if packet_id and trace.get("packet_id") != packet_id:
                trace_error = "knowledge trace belongs to a different review packet"
            elif set(applied) != set(packet_article_paths):
                trace_error = "knowledge trace does not cover exactly the current packet articles"
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            trace_error = f"knowledge trace unreadable: {exc}"

    receipt_validation = {"ok": True}
    if required and not trace_error:
        from bc_agentic_mcp import knowledge
        receipt_validation = knowledge.validate_knowledge_receipts(
            Path(project_root),
            spec_name,
            packet_id,
            packet_article_paths,
            vendor_commit,
            receipts,
        )
    ok = (not required) or (not trace_error and bool(applied) and receipt_validation.get("ok", False))
    reason = ""
    if required and not ok:
        detail = trace_error or str(receipt_validation.get("reason") or "") or (
            "Call bc_get_knowledge_article for each article with the current packet_id."
        )
        reason = (
            f"BCQuality knowledge coverage missing — review packet had "
            f"{packet_article_count} article(s). {detail}"
        )
    return {
        "required": required,
        "ok": ok,
        "reason": reason,
        "packet_article_count": packet_article_count,
        "packet_article_paths": packet_article_paths,
        "articles_applied": applied,
    }

_PASS_TOKENS = {"pass", "passed", "true", "ok", "success"}
# Evidence-location tokens that mean "ran in a local BC container". The container
# NAME is environment-specific — extend via BC_MCP_CONTAINER_TOKENS (comma-separated)
# instead of baking one team's container names in (hardcode sweep 2026-07-06).
_LOCAL_CONTAINER_TOKENS = tuple(
    t.strip().lower()
    for t in (
        "local container,container,docker,bccontainerhelper,run-testsinbccontainer,"
        + os.environ.get("BC_MCP_CONTAINER_TOKENS", "acctest")
    ).split(",")
    if t.strip()
)

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_WIN_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\")
_WIN_PATH_TOKEN_RE = re.compile(r"[A-Za-z]:\\[^;\r\n]+")
_FILE_URI_RE = re.compile(r"file:///[^\s;]+", re.IGNORECASE)
_EXECUTION_PROOF_RE = re.compile(
    r"(passed\s*=\s*\d+\s*/\s*\d+|exit\s*=\s*\d+|all_passed)",
    re.IGNORECASE,
)

_API_TAXONOMY_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "business_validation": (
        "invalid",
        "boundary",
        "length",
        "type",
        "relation",
        "date",
        "remark",
        "persist",
    ),
    "transport_protocol": (
        "malformed",
        "content-type",
        "unsupported method",
        "method not allowed",
        "badrequest",
        "payload",
    ),
    "authz": (
        "unauthorized",
        "forbidden",
        "auth",
        "credential",
        "permission",
        "401",
        "403",
    ),
    "concurrency": (
        "etag",
        "if-match",
        "precondition",
        "stale",
        "conflict",
        "412",
        "409",
    ),
    "resource_identity": (
        "not found",
        "404",
        "invalid id",
        "company",
        "record id",
        "cross-company",
        "missing resource",
    ),
}


def _extract_local_path_candidates(text: str) -> List[str]:
    """Extract absolute local path candidates from free-text evidence strings."""
    out: List[str] = []
    raw = str(text or "")
    for token in _WIN_PATH_TOKEN_RE.findall(raw):
        p = token.strip().strip('"').strip("'")
        if p:
            out.append(p)
    for uri in _FILE_URI_RE.findall(raw):
        local = uri[len("file:///"):].replace("/", "\\")
        p = local.strip().strip('"').strip("'")
        if p:
            out.append(p)
    # Stable de-dup while preserving order.
    seen = set()
    unique: List[str] = []
    for p in out:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        unique.append(p)
    return unique


def _validate_evidence_json_scenarios(artifact_paths: List[str]) -> Dict[str, Any]:
    """Validate evidence JSON structure for human-readable scenario documentation.

    Requirement: each scenario in a referenced API JSON artifact must include
    the full agreed schema and non-empty documentation fields.
    """
    json_paths = [p for p in artifact_paths if str(p).lower().endswith(".json")]
    if not json_paths:
        return {
            "ok": False,
            "checked": [],
            "missing": ["(no-json-artifact)"],
            "invalid_json": [],
        }

    checked: List[str] = []
    missing: List[str] = []
    invalid_json: List[str] = []
    missing_schema: List[str] = []
    required_fields = (
        "name",
        "scenarioDescription",
        "validates",
        "method",
        "endpoint",
        "body",
        "expected",
        "actual",
        "statusCode",
        "passed",
        "responseMessage",
        "responseBody",
    )
    for p in json_paths:
        checked.append(p)
        try:
            raw = Path(p).read_text(encoding="utf-8")
            doc = json.loads(raw)
        except Exception:
            invalid_json.append(p)
            continue

        scenarios = doc.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            missing.append(p)
            continue

        has_all = True
        for s in scenarios:
            if not isinstance(s, dict):
                has_all = False
                break
            for field in required_fields:
                if field not in s:
                    has_all = False
                    break
            if not has_all:
                break
            desc = str(s.get("scenarioDescription") or "").strip()
            validates = str(s.get("validates") or "").strip()
            if not desc or not validates:
                has_all = False
                break
        if not has_all:
            missing.append(p)
            missing_schema.append(p)

    return {
        "ok": not missing and not invalid_json,
        "checked": checked,
        "missing": missing,
        "missing_schema": missing_schema,
        "invalid_json": invalid_json,
    }


def _validate_api_exhaustive_taxonomy(artifact_paths: List[str]) -> Dict[str, Any]:
    """Validate that exhaustive API evidence includes all taxonomy categories."""
    json_paths = [p for p in artifact_paths if str(p).lower().endswith(".json")]
    observed: set[str] = set()

    for p in json_paths:
        try:
            raw = Path(p).read_text(encoding="utf-8")
            doc = json.loads(raw)
        except Exception:
            continue
        scenarios = doc.get("scenarios")
        if not isinstance(scenarios, list):
            continue
        for s in scenarios:
            if not isinstance(s, dict):
                continue
            text = " ".join(
                [
                    str(s.get("name") or ""),
                    str(s.get("scenarioDescription") or ""),
                    str(s.get("validates") or ""),
                ]
            ).lower()
            for category, needles in _API_TAXONOMY_KEYWORDS.items():
                if any(n in text for n in needles):
                    observed.add(category)

    required = set(_API_TAXONOMY_KEYWORDS.keys())
    missing = sorted(required - observed)
    return {
        "ok": not missing,
        "required": sorted(required),
        "observed": sorted(observed),
        "missing": missing,
    }

# Deterministic evidence-strength ladder. These are evidence TIERS (generic proof
# strength), not project data: a claim < a compile < an executed test < a live runtime run.
_LAYER_STRENGTH: Dict[str, int] = {
    "": 1,
    "heuristic": 1,
    "static": 1,
    "claim": 1,
    "claimed": 1,
    "empiric-compile": 2,
    "compile": 2,
    "al-unit": 3,
    "al-regression": 3,
    "unit": 3,
    "integration": 3,
    "empiric-runtime": 4,
    "runtime": 4,
    "api": 4,
    "e2e": 4,
}
STRENGTH_LABELS = {0: "none", 1: "claim", 2: "compile", 3: "executed-test", 4: "live-runtime"}

_EMPIRIC_ITEM_LAYERS = {"al-unit", "unit", "integration", "empiric-runtime", "runtime", "e2e"}
_REGRESSION_LAYERS = {"al-regression", "integration", "e2e"}
_API_LAYERS = {"api", "empiric-runtime", "runtime", "e2e"}

# Deterministic PATH-SHAPE classification of executed test names (When...Expect...
# convention). Token-aware: names are split on camelCase + separators so substrings
# can never lie ('min' must not match 'terMINology' — observed in the first cut).
# Edge outranks negative outranks happy; unmatched non-empty names are happy paths.
_NEGATIVE_TOKENS = {
    "error", "errors", "fail", "fails", "failed", "failure", "invalid", "unauthorized",
    "forbidden", "blocked", "reject", "rejected", "denied", "without", "missing",
    "cannot", "prevent", "prevented", "notallowed", "nopermission",
    # 'refused' tests are negative paths — the first cut classified
    # CreationRefused tests as happy (caught on wi267598's PR-TESTS.md).
    "refuse", "refused", "refuses", "refusal",
}
_EDGE_TOKENS = {
    "twice", "double", "repeat", "repeated", "empty", "blank", "zero", "max", "min",
    "boundary", "boundaries", "limit", "limits", "duplicate", "duplicates",
    "concurrent", "already", "again", "deleted", "stale",
}
_EDGE_PREFIXES = ("idempot", "reentr")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NONWORD_RE = re.compile(r"[^A-Za-z0-9]+")


def _name_tokens(name: str) -> "set[str]":
    spaced = _CAMEL_SPLIT_RE.sub(" ", str(name or ""))
    return {t.lower() for t in _NONWORD_RE.split(spaced) if t}


def classify_test_paths(names: List[str]) -> Dict[str, int]:
    """Count happy / negative / edge shapes across executed test names."""
    counts = {"happy": 0, "negative": 0, "edge": 0}
    for name in names or []:
        tokens = _name_tokens(name)
        if not tokens:
            continue
        if (tokens & _EDGE_TOKENS) or any(t.startswith(_EDGE_PREFIXES) for t in tokens):
            counts["edge"] += 1
        elif tokens & _NEGATIVE_TOKENS:
            counts["negative"] += 1
        else:
            counts["happy"] += 1
    return counts


_PATHS_EVIDENCE_RE = re.compile(r"paths=happy:(\d+),negative:(\d+),edge:(\d+)")


def normalize_layer(layer: str) -> int:
    """Map a free-text layer name to its deterministic strength tier (1..4)."""
    return _LAYER_STRENGTH.get(str(layer or "").strip().lower(), 1)


def evidence_strength(layer: str, evidence: Any) -> int:
    """Strength of a single passing test.

    Empiric tiers (>= compile) require non-empty evidence; an empiric claim with NO
    captured evidence is downgraded to a bare 'claim' so evidence-free assertions cannot
    masquerade as proof. Deterministic.
    """
    tier = normalize_layer(layer)
    has_evidence = bool(str(evidence).strip()) if not isinstance(evidence, dict) else bool(evidence)
    if tier >= 2 and not has_evidence:
        return 1
    return tier


def required_strength_for_operations(operations: Any) -> int:
    """Deterministically derive the minimum evidence tier an item should reach.

    Generic policy (no task data): a mutation (update/create/delete) must be proven at
    runtime (4); any other declared operation needs at least an executed test (3);
    a spec with no operations only needs a claim (1).
    """
    ops: Dict[str, bool] = {}
    if isinstance(operations, dict):
        ops = {str(k).lower(): bool(v) for k, v in operations.items()}
    elif isinstance(operations, list):
        ops = {str(o).lower(): True for o in operations}
    if any(ops.get(k) for k in ("update", "create", "insert", "delete", "modify")):
        return 4
    if any(ops.values()):
        return 3
    return 1


def record_test(
    project_root: Path,
    spec_name: str,
    *,
    name: str,
    result: str,
    covers: Union[str, List[int]],
    layer: str = "",
    evidence: str = "",
    executed_tests: Optional[List[Dict[str, Any]]] = None,
    failures: Optional[List[Dict[str, Any]]] = None,
    evidence_receipt: str = "",
    trusted_source: str = "internal",
) -> Dict[str, Any]:
    """Record a test result as durable evidence.

    ``covers`` is either the string ``"all"`` or a list of 1-based acceptance-criterion
    indices (matching the order in the Charter). ``result`` is e.g. "pass"/"fail".
    ``executed_tests`` (optional) persists the EXPLICIT per-test list
    [{codeunit, test, shape, result}] — without it only the aggregate count survives
    and the PR template cannot name what each test validates (observed live: the
    golden template said '8/8' while the reviewer had to open the test file).
    """
    if evidence_receipt:
        if not security.verify_evidence(
            evidence_receipt,
            project_root=Path(project_root),
            spec_name=spec_name,
            name=name,
            result=result,
            covers=covers,
            layer=layer,
            evidence=evidence,
        ):
            raise ValueError("runtime evidence receipt is invalid or does not match the result")
        trusted_source = "server"
    details: Dict[str, Any] = {
        "result": str(result),
        "covers": covers,
        "layer": layer,
        "evidence": evidence,
        "evidence_source": trusted_source,
    }
    if evidence_receipt:
        details["evidence_receipt"] = evidence_receipt
    if executed_tests:
        details["executed_tests"] = executed_tests
    if failures:
        # WHY it failed travels WITH the evidence — 'passed=6/7' alone forced a
        # blind re-run just to learn the assert message (observed live on 66188).
        details["failures"] = [
            {"test": str(f.get("test", "?")),
             "error": str(f.get("error", ""))[:400]}
            for f in failures
        ]
    return memory.append_checkpoint(
        project_root,
        spec_name,
        kind="test",
        summary=name,
        details=details,
    )


def _is_pass(result: Any) -> bool:
    return str(result).strip().lower() in _PASS_TOKENS


def _covers(detail_covers: Any, index: int) -> bool:
    if isinstance(detail_covers, str):
        return detail_covers.strip().lower() == "all"
    if isinstance(detail_covers, list):
        return index in detail_covers
    return False


def _trusted_test_record(project_root: Path, spec_name: str, checkpoint: Dict[str, Any]) -> bool:
    """Accept only internal records or receipts issued by an execution tool."""
    details = checkpoint.get("details") or {}
    source = str(details.get("evidence_source") or "")
    if source == "internal":
        return True
    if source != "server":
        return False
    return bool(security.verify_evidence(
        str(details.get("evidence_receipt") or ""),
        project_root=Path(project_root),
        spec_name=spec_name,
        name=str(checkpoint.get("summary") or ""),
        result=str(details.get("result") or ""),
        covers=details.get("covers"),
        layer=str(details.get("layer") or ""),
        evidence=str(details.get("evidence") or ""),
    ))


def build_verification(
    project_root: Path,
    spec_name: str,
    min_required_strength: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the coverage matrix: each acceptance criterion -> covering passing test(s).

    When ``min_required_strength`` is None it is derived from the Charter's operations, so
    an item that declares a mutation is only ``fully_validated_strict`` if every criterion
    is backed by runtime evidence. The legacy ``fully_validated`` (coverage only) is kept.
    """
    charter = memory.load_charter(project_root, spec_name)
    criteria = list((charter or {}).get("acceptance_criteria", []))
    operations = (charter or {}).get("operations", {})
    tests = [
        c for c in memory.load_checkpoints(project_root, spec_name)
        if c.get("kind") == "test" and _trusted_test_record(project_root, spec_name, c)
    ]
    spec_json: Optional[Dict[str, Any]] = None
    try:
        from bc_agentic_mcp.workspace import specs_root as _sr
        _spec_path = _sr(project_root) / spec_name / "spec.json"
        if _spec_path.exists():
            spec_json = json.loads(_spec_path.read_text(encoding="utf-8"))
    except Exception:
        spec_json = None

    # Deterministic requirement->acceptance-test trace (OpenFastTrace model) from spec.json.
    spec_trace = None
    try:
        import json as _json
        from bc_agentic_mcp import traceability as _trace
        from bc_agentic_mcp.workspace import specs_root as _sr
        _spec_path = _sr(project_root) / spec_name / "spec.json"
        if _spec_path.exists():
            spec_trace = _trace.trace_spec(_json.loads(_spec_path.read_text(encoding="utf-8")))
    except Exception:
        spec_trace = None

    required = (
        required_strength_for_operations(operations)
        if min_required_strength is None
        else int(min_required_strength)
    )

    rows: List[Dict[str, Any]] = []
    for index, criterion in enumerate(criteria, start=1):
        covering = [t for t in tests if _covers((t.get("details") or {}).get("covers"), index)]
        passing = [t for t in covering if _is_pass((t.get("details") or {}).get("result"))]
        strengths = [
            evidence_strength(
                str((t.get("details") or {}).get("layer") or ""),
                str((t.get("details") or {}).get("evidence") or ""),
            )
            for t in passing
        ]
        strength = max(strengths) if strengths else 0
        rows.append(
            {
                "index": index,
                "criterion": criterion,
                "covered_by": [t.get("summary") for t in covering],
                "passing_tests": [t.get("summary") for t in passing],
                "validated": bool(passing),
                "strength": strength,
                "strength_label": STRENGTH_LABELS.get(strength, "none"),
                "meets_evidence_bar": strength >= required,
            }
        )

    validated = sum(1 for r in rows if r["validated"])
    fully_validated = bool(criteria) and validated == len(criteria)
    fully_validated_strict = bool(criteria) and all(r["meets_evidence_bar"] for r in rows)
    validation_classes = validation_class_status(project_root, spec_name, tests, spec_json)
    return {
        "spec_name": spec_name,
        "criteria_count": len(criteria),
        "validated_count": validated,
        "coverage_pct": round(100.0 * validated / len(criteria), 1) if criteria else 0.0,
        "fully_validated": fully_validated,
        "required_strength": required,
        "required_strength_label": STRENGTH_LABELS.get(required, "none"),
        "fully_validated_strict": fully_validated_strict,
        "rows": rows,
        "uncovered": [r["criterion"] for r in rows if not r["validated"]],
        "evidence_gaps": [
            r["criterion"] for r in rows if r["validated"] and not r["meets_evidence_bar"]
        ],
        "tests_recorded": len(tests),
        "traceability": spec_trace,
        "validation_classes": validation_classes,
    }


def _is_api_item(spec_json: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(spec_json, dict):
        return False
    objects = spec_json.get("objects") or []
    operations = spec_json.get("operations") or []
    for obj in objects:
        if isinstance(obj, dict) and "api" in str(obj.get("type") or "").lower():
            return True
    for op in operations:
        text = str(op.get("type") if isinstance(op, dict) else op).lower()
        if "api" in text or "odata" in text:
            return True
    return False


def _touched_object_names(spec_json: Optional[Dict[str, Any]]) -> "set[str]":
    """Names this spec touches, plus the extends-targets of touched extension objects."""
    names: "set[str]" = set()
    if not isinstance(spec_json, dict):
        return names
    for key in ("objects_to_modify", "objects_to_create"):
        for obj in spec_json.get(key) or []:
            if isinstance(obj, dict) and obj.get("name"):
                names.add(str(obj["name"]))
    return names


def api_pages_touching(project_root: Path, spec_json: Optional[Dict[str, Any]]) -> List[str]:
    """API pages (from the persistent object index) that reference an object this spec
    touches — or the extends-target of a touched extension object.

    The Bug 267600 lesson made this a WALL: the spec had no API objects of its own, yet
    page FeatureManagementAPI20OPN could PATCH the very enum the spec was removing a
    value from (HousingFeatureHSG extends FeatureSAN; the API sources FeatureSAN).
    'The spec is not an API item' is not the same as 'no API touches the artifact'.
    """
    touched = _touched_object_names(spec_json)
    if not touched:
        return []
    try:
        from bc_agentic_mcp import object_index
        data = object_index.refresh(Path(project_root), max_age_seconds=300)
    except Exception:
        return []  # index unavailable -> no forced requirement (fail-open, evidence-first)
    objects, files = data.get("objects") or {}, data.get("files") or {}
    # Expand touched names with extends-targets (enumextension X extends FeatureSAN).
    expanded = set(touched)
    for name in touched:
        hit = objects.get(name.lower())
        target = ((hit or {}).get("detail") or {}).get("extends")
        if target:
            expanded.add(str(target))
    out: List[str] = []
    for rel, payload in files.items():
        for obj in payload.get("objects") or []:
            if obj.get("kind") != "page":
                continue
            detail = obj.get("detail") or {}
            props = " ".join(detail.get("props") or [])
            if "pagetype = api" not in props.lower():
                continue
            refs = set(payload.get("refs") or [])
            # SourceTable is the strongest signal: the API serves that artifact directly.
            source_m = re.search(r"SourceTable\s*=\s*\"?([A-Za-z0-9_ ]+?)\"?(?:\s|$)", props)
            if source_m:
                refs.add(source_m.group(1).strip())
            hits = expanded & refs
            if hits:
                out.append(f"page {obj.get('number')} {obj.get('name')} ({rel}) -> {', '.join(sorted(hits))}")
    return sorted(out)


def validation_class_status(
    project_root: Path,
    spec_name: str,
    tests: List[Dict[str, Any]],
    spec_json: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Deterministic layered-validation classes for approval gating.

    heuristic: code/context grounding exists
    empiric-item: passing local-container item evidence exists
    regression: passing targeted regression evidence exists
    api-contract: only for API items
    """
    heuristic_ok = False
    heuristic_reason = ""
    path_counts = {"happy": 0, "negative": 0, "edge": 0}
    try:
        from bc_agentic_mcp import enforcement
        eng = enforcement.engine_status(project_root, spec_name)
        heuristic_ok = bool((eng.get("engines") or {}).get("code_context", {}).get("ok"))
        if not heuristic_ok:
            heuristic_reason = ((eng.get("engines") or {}).get("code_context", {}) or {}).get("reason", "missing code context grounding")
    except Exception as exc:
        heuristic_reason = f"code context status unavailable: {exc}"

    local_evidence = local_container_evidence(project_root, spec_name)
    empiric_item_tests = []
    regression_tests = []
    api_tests = []
    for t in tests:
        details = t.get("details") or {}
        if not _is_pass(details.get("result")):
            continue
        layer = str(details.get("layer") or "").strip().lower()
        summary = str(t.get("summary") or "")
        evidence = str(details.get("evidence") or "")
        haystack = f"{summary} {evidence}".lower()
        candidates = _extract_local_path_candidates(evidence)
        existing = [p for p in candidates if Path(p).exists()]
        has_local_marker = any(tok in haystack for tok in _LOCAL_CONTAINER_TOKENS)
        has_direct_execution_proof = bool(_EXECUTION_PROOF_RE.search(evidence)) or bool(existing)
        if layer in _EMPIRIC_ITEM_LAYERS and has_local_marker and has_direct_execution_proof:
            empiric_item_tests.append(summary)
        if layer in _REGRESSION_LAYERS:
            regression_tests.append(summary)
        # Path-shape counts recorded by the runner (paths=happy:N,negative:N,edge:N).
        pm = _PATHS_EVIDENCE_RE.search(evidence)
        if pm:
            path_counts["happy"] += int(pm.group(1))
            path_counts["negative"] += int(pm.group(2))
            path_counts["edge"] += int(pm.group(3))
        if layer in _API_LAYERS and ("api" in haystack or "http://" in haystack or "https://" in haystack or "odata" in haystack):
            api_tests.append(summary)

    classes: Dict[str, Dict[str, Any]] = {
        "heuristic": {
            "required": True,
            "ok": heuristic_ok,
            "reason": "" if heuristic_ok else (heuristic_reason or "missing code context grounding"),
        },
        "empiric-item": {
            "required": True,
            "ok": bool(empiric_item_tests) and bool(local_evidence.get("ok")),
            "reason": "" if (empiric_item_tests and local_evidence.get("ok")) else "missing item-scoped local-container proof",
            "examples": empiric_item_tests[:5],
        },
        "regression": {
            "required": True,
            "ok": bool(regression_tests),
            "reason": "" if regression_tests else "missing targeted regression evidence (record tests with layer='al-regression')",
            "examples": regression_tests[:5],
        },
        # PATH-COVERAGE: every lane (feature / PBI / bug) must prove happy AND
        # negative AND edge shapes in its executed empiric tests — an item
        # validated only by happy paths lets everything else escape.
        "path-coverage": {
            "required": True,
            "ok": path_counts["happy"] > 0 and path_counts["negative"] > 0 and path_counts["edge"] > 0,
            "reason": "" if (path_counts["happy"] > 0 and path_counts["negative"] > 0 and path_counts["edge"] > 0)
                      else (f"executed test shapes incomplete (happy:{path_counts['happy']}, "
                            f"negative:{path_counts['negative']}, edge:{path_counts['edge']}) — "
                            "author the missing shapes and re-run"),
            "counts": path_counts,
        },
    }
    api_required = _is_api_item(spec_json)
    api_touch: List[str] = []
    if not api_required:
        # Reverse lookup: an existing API page reading/patching a touched object forces
        # the api-contract class even when the spec itself declares no API work.
        api_touch = api_pages_touching(project_root, spec_json)
        api_required = bool(api_touch)
    classes["api-contract"] = {
        "required": api_required,
        "ok": (not api_required) or bool(api_tests),
        "reason": "" if ((not api_required) or api_tests) else (
            "missing API contract evidence — "
            + ("API surfaces touch this spec's objects: " + "; ".join(api_touch[:3])
               if api_touch else "API item")
        ),
        "examples": api_tests[:5],
    }
    if api_touch:
        classes["api-contract"]["touching_api_pages"] = api_touch[:10]
    # KNOWLEDGE-COVERAGE: BCQuality articles in the review packet must be applied.
    # Required only when packet_article_count > 0 (fail-safe-open when no review packet).
    kc = check_knowledge_coverage(project_root, spec_name)
    classes["knowledge-coverage"] = {
        "required": kc["required"],
        "ok": kc["ok"],
        "reason": kc["reason"],
        "packet_article_count": kc["packet_article_count"],
        "articles_applied": kc["articles_applied"][:5],
    }
    return classes


def gate(
    project_root: Path,
    spec_name: str,
    min_required_strength: Optional[int] = None,
) -> Dict[str, Any]:
    """Deterministic fail-closed verification gate.

    Returns ``{"passed": bool, "blockers": [...], "digest": ...}``. A gate PASSES only when
    there is at least one acceptance criterion, every criterion is covered by a passing test
    (coverage), and every criterion meets the derived evidence bar (no weak/evidence-free
    claims). This is what an approval/closeout step must consult — reporting is not enough.

    FEATURE specs (one PR per feature): the feature's proof IS its children's proof —
    the gate aggregates every live child's gate instead of demanding item evidence
    from the feature folder itself (which only carries planning artifacts).
    """
    from bc_agentic_mcp.workspace import specs_root as _specs_root
    root = Path(project_root).resolve()
    if (_specs_root(root) / spec_name / "feature_plan.json").exists():
        return _feature_gate(root, spec_name, min_required_strength)
    digest = build_verification(project_root, spec_name, min_required_strength)
    blockers: List[str] = []
    if digest["criteria_count"] == 0:
        blockers.append("No acceptance criteria in the Charter — nothing to verify.")
    for criterion in digest.get("uncovered", []):
        blockers.append(f"Uncovered criterion: {criterion}")
    for criterion in digest.get("evidence_gaps", []):
        blockers.append(
            f"Weak evidence (below '{digest.get('required_strength_label')}'): {criterion}"
        )
    for name, state in (digest.get("validation_classes") or {}).items():
        if state.get("required") and not state.get("ok"):
            blockers.append(f"Missing {name} validation: {state.get('reason')}")
    passed = bool(digest["criteria_count"]) and not blockers
    return {"passed": passed, "blockers": blockers, "digest": digest}


def _feature_gate(
    root: Path,
    spec_name: str,
    min_required_strength: Optional[int] = None,
) -> Dict[str, Any]:
    """Aggregate the verification gates of every live child item.

    Passes only when every child passes; the digest carries per-child digests plus
    rolled-up numbers so the PR description can be built from recorded evidence.
    """
    from bc_agentic_mcp.tools.feature import feature_children_specs
    children = feature_children_specs(root, spec_name)
    blockers: List[str] = []
    child_digests: Dict[str, Any] = {}
    external_children: List[str] = []
    rows: List[Dict[str, Any]] = []
    criteria_count = 0
    tests_recorded = 0
    coverage_values: List[float] = []
    strength_labels: List[str] = []
    checked = 0
    for child in children:
        item_spec = child.get("item_spec")
        if not item_spec:
            if str(child.get("state", "")).lower() == "done":
                # Delivered OUTSIDE this feature branch (e.g. a technical-design
                # item completed before the work started) — this PR ships nothing
                # for it, so there is nothing to verify here.
                external_children.append(
                    f"{child.get('id')} ({str(child.get('title', '?')).strip()}) — Done before this PR")
                continue
            blockers.append(f"Child {child.get('id')} ({child.get('title', '?')}) has no captured item spec.")
            continue
        sub = gate(root, item_spec, min_required_strength)
        checked += 1
        d = sub["digest"]
        child_digests[item_spec] = d
        criteria_count += int(d.get("criteria_count") or 0)
        tests_recorded += int(d.get("tests_recorded") or 0)
        try:
            coverage_values.append(float(d.get("coverage_pct") or 0.0))
        except (TypeError, ValueError):
            pass
        if d.get("required_strength_label"):
            strength_labels.append(str(d["required_strength_label"]))
        for row in d.get("rows", []) or []:
            merged = dict(row)
            merged["criterion"] = f"[{item_spec}] {row.get('criterion', '')}"
            rows.append(merged)
        if not sub["passed"]:
            blockers.extend(f"[{item_spec}] {b}" for b in sub["blockers"][:3])
    if checked == 0:
        blockers.append("Feature has no verifiable children — capture and verify the items first.")
    digest = {
        "feature": spec_name,
        "children": sorted(child_digests),
        "child_digests": child_digests,
        "external_children": external_children,
        "rows": rows,
        "criteria_count": criteria_count,
        "tests_recorded": tests_recorded,
        "coverage_pct": round(min(coverage_values), 1) if coverage_values else 0.0,
        "required_strength_label": max(set(strength_labels), key=strength_labels.count) if strength_labels else None,
    }
    return {"passed": not blockers, "blockers": blockers, "digest": digest}


def local_container_evidence(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """Return whether there is at least one passing test with local-container evidence.

    This is a deterministic metadata check on recorded ``kind='test'`` checkpoints.
    """
    tests = [
        c for c in memory.load_checkpoints(project_root, spec_name)
        if c.get("kind") == "test" and _trusted_test_record(project_root, spec_name, c)
    ]
    matches: List[str] = []
    missing_full_refs: List[str] = []
    invalid_local_paths: List[str] = []
    missing_scenario_docs: List[str] = []
    invalid_evidence_json: List[str] = []
    invalid_api_evidence_schema: List[str] = []
    missing_taxonomy_categories: List[str] = []
    for t in tests:
        details = t.get("details") or {}
        if not _is_pass(details.get("result")):
            continue
        evidence = str(details.get("evidence") or "")
        layer = str(details.get("layer") or "")
        summary = str(t.get("summary") or "")
        haystack = f"{summary} {layer} {evidence}".lower()
        if any(tok in haystack for tok in _LOCAL_CONTAINER_TOKENS):
            # Two accepted evidence modes for local container validation:
            # 1) API mode: endpoint URL + local artifact path; when JSON artifact is
            #    referenced, scenarioDescription+validates must be present for each
            #    scenario row.
            # 2) Non-API mode: execution-proof evidence (e.g., passed=X/Y and/or
            #    exit=0) for runs that do not expose HTTP endpoint artifacts.
            has_endpoint_url = bool(_URL_RE.search(evidence))
            candidates = _extract_local_path_candidates(evidence)
            existing = [p for p in candidates if Path(p).exists()]
            has_artifact_ref = bool(existing)
            artifact_doc = _validate_evidence_json_scenarios(existing) if has_artifact_ref else {
                "ok": False,
                "missing": [],
                "invalid_json": [],
            }
            has_execution_proof = bool(_EXECUTION_PROOF_RE.search(evidence))
            is_api_evidence = has_endpoint_url or "api" in haystack or "odata" in haystack
            has_scenario_docs = bool(artifact_doc.get("ok"))
            is_exhaustive = (
                "exhaustive" in haystack
                or any("exhaustive" in Path(p).name.lower() for p in existing)
            )
            taxonomy = _validate_api_exhaustive_taxonomy(existing) if (is_api_evidence and is_exhaustive) else {
                "ok": True,
                "missing": [],
            }
            if is_api_evidence:
                if has_endpoint_url and has_artifact_ref and has_scenario_docs and taxonomy.get("ok"):
                    matches.append(summary or "(unnamed test)")
                else:
                    missing_full_refs.append(summary or "(unnamed test)")
                    if candidates and not existing:
                        invalid_local_paths.append(summary or "(unnamed test)")
                    if has_artifact_ref and not has_scenario_docs:
                        missing_scenario_docs.append(summary or "(unnamed test)")
                        if artifact_doc.get("invalid_json"):
                            invalid_evidence_json.append(summary or "(unnamed test)")
                        if artifact_doc.get("missing_schema"):
                            invalid_api_evidence_schema.append(summary or "(unnamed test)")
                    if taxonomy.get("missing"):
                        missing_taxonomy_categories.append(summary or "(unnamed test)")
            elif has_execution_proof or has_artifact_ref:
                matches.append(summary or "(unnamed test)")
            else:
                missing_full_refs.append(summary or "(unnamed test)")
                if candidates and not existing:
                    invalid_local_paths.append(summary or "(unnamed test)")
    return {
        "ok": bool(matches),
        "count": len(matches),
        "examples": matches[:5],
        "missing_full_refs": missing_full_refs,
        "invalid_local_paths": invalid_local_paths,
        "missing_scenario_docs": missing_scenario_docs,
        "invalid_evidence_json": invalid_evidence_json,
        "invalid_api_evidence_schema": invalid_api_evidence_schema,
        "missing_taxonomy_categories": missing_taxonomy_categories,
    }


def render_verification_md(digest: Dict[str, Any]) -> str:
    strict = digest.get("fully_validated_strict")
    req_label = digest.get("required_strength_label", "")
    lines = [
        f"# Verification Report: {digest.get('spec_name', '')}",
        "",
        "Proof that every acceptance criterion of the item is covered by a passing test.",
        "",
        f"**Coverage: {digest['validated_count']}/{digest['criteria_count']} "
        f"({digest['coverage_pct']}%) — fully validated: "
        f"{'YES' if digest['fully_validated'] else 'NO'}**",
    ]
    if "required_strength_label" in digest:
        lines += [
            "",
            f"**Evidence bar: every criterion must reach `{req_label}` — "
            f"strict pass: {'YES' if strict else 'NO'}**",
        ]
    classes = digest.get("validation_classes") or {}
    if classes:
        lines += [
            "",
            "## Validation Classes",
            "",
            "| Class | Required | Status | Reason |",
            "|---|---|---|---|",
        ]
        for name, state in classes.items():
            required = "yes" if state.get("required") else "no"
            status = "✅" if state.get("ok") else ("—" if not state.get("required") else "❌")
            reason = str(state.get("reason") or "").replace("|", "\\|") or "—"
            lines.append(f"| {name} | {required} | {status} | {reason} |")
    lines += [
        "",
        "| # | Acceptance criterion | Validated by (passing test) | Evidence | Status |",
        "|---|---|---|---|---|",
    ]
    for row in digest["rows"]:
        tests = ", ".join(row["passing_tests"]) or "—"
        status = "✅" if row["validated"] else "❌ UNCOVERED"
        if row["validated"] and not row.get("meets_evidence_bar", True):
            status = "⚠️ WEAK EVIDENCE"
        criterion = str(row["criterion"]).replace("|", "\\|")
        strength = row.get("strength_label", "")
        lines.append(f"| {row['index']} | {criterion} | {tests} | {strength} | {status} |")
    if digest["uncovered"]:
        lines += ["", "## Uncovered criteria (must not ship)"]
        lines += [f"- {c}" for c in digest["uncovered"]]
    if digest.get("evidence_gaps"):
        lines += ["", f"## Weak evidence (below `{req_label}` bar — strengthen before shipping)"]
        lines += [f"- {c}" for c in digest["evidence_gaps"]]
    lines.append("")
    return "\n".join(lines)
