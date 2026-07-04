"""bc_write_spec — generate TDD and machine spec from human bullets.
See spec Section 3.4.
"""
import hashlib
import json
import re
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, Optional, List

from bc_agentic_mcp.validation import validate_spec_name, validate_idempotency_key
from bc_agentic_mcp.config import discover_analyzers
from bc_agentic_mcp.spec_loader import validate_spec_contract


_URL_PATTERN = re.compile(r"https?://\S+")
_FIELD_PATTERN = re.compile(r"^Subprocess[A-Za-z0-9_]+$")


def _extract_urls(text: str) -> List[str]:
    return sorted({m.group(0).rstrip('.,)') for m in _URL_PATTERN.finditer(text)})


def _extract_api_name(text: str) -> Optional[str]:
    lines = [line.strip() for line in text.splitlines()]
    for idx, line in enumerate(lines):
        if "extend the following api" in line.lower() and idx + 1 < len(lines):
            candidate = lines[idx + 1].strip()
            if candidate:
                return candidate
    return None


def _extract_requested_fields(text: str) -> List[str]:
    fields: List[str] = []
    for raw in text.splitlines():
        line = raw.strip().rstrip(",")
        if _FIELD_PATTERN.match(line):
            fields.append(line)
    return fields


_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_SPLIT = re.compile(r"(?<!^)(?=[A-Z])")


def _field_tokens(text: str) -> set:
    tokens = set()
    for chunk in _TOKEN_SPLIT.split(text or ""):
        if not chunk:
            continue
        for part in _CAMEL_SPLIT.split(chunk):
            cleaned = part.lower().strip()
            if cleaned:
                tokens.add(cleaned)
    return tokens


def _page_is_in_family(content: str, requested_fields: List[str]) -> bool:
    """True if the page already maps a sibling field sharing >=2 tokens with a request."""
    request_token_sets = [_field_tokens(f) for f in requested_fields]
    for match in re.finditer(r'Rec\."([^"]+)"', content):
        field_tokens = _field_tokens(match.group(1))
        if any(len(field_tokens & rs) >= 2 for rs in request_token_sets):
            return True
    return False


def _find_api_targets(
    root: Path,
    api_name: Optional[str],
    requested_fields: Optional[List[str]] = None,
) -> List[str]:
    """Find the API page files for an entity, scoped to the right field family.

    Entity-precise: prefer pages whose `EntityName` equals the requested API name.
    Family-aware: when multiple pages share the entity, keep only those that already
    expose a sibling field (so a different projection of the same entity is excluded).
    Falls back to filename matching only when no EntityName match exists.
    """
    if not api_name:
        return []

    name_lower = api_name.lower()
    src_dir = root / "src"
    if not src_dir.exists():
        return []

    entity_matches: List[str] = []
    filename_matches: List[str] = []
    contents: Dict[str, str] = {}
    for al_file in sorted(src_dir.rglob("*.al")):
        rel = str(al_file.relative_to(root))
        try:
            content = al_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        contents[rel] = content
        match = re.search(r"(?im)^\s*EntityName\s*=\s*'([^']+)'", content)
        if match and match.group(1).strip().lower() == name_lower:
            entity_matches.append(rel)
        elif name_lower in al_file.name.lower():
            filename_matches.append(rel)

    if entity_matches and requested_fields:
        family = [rel for rel in entity_matches if _page_is_in_family(contents.get(rel, ""), requested_fields)]
        if family:
            return family
    return entity_matches if entity_matches else filename_matches


def _api_mapping_expr(al_type: str, field: str) -> str:
    """Pattern-recognized mapping style: enums/options use Format(); primitives direct."""
    normalized = (al_type or "").strip().lower()
    if normalized.startswith("enum") or normalized.startswith("option"):
        return f'Format(Rec."{field}")'
    return f'Rec."{field}"'


_AL_TYPE_RULES = (
    ("OnHoldTill", "Date"),
    ("OnHoldIndication", "Boolean"),
    ("OnHoldUser", "Code[50]"),
    ("OnHoldTeam", "Code[20]"),
    ("Remark", "Text[250]"),
)

_BUGFIX_MARKERS = ("defect", "fix the", "regression", "incorrect", "error when")


def _camel(name: str) -> str:
    return (name[:1].lower() + name[1:]) if name else name


def _infer_al_type(field: str) -> str:
    for suffix, al_type in _AL_TYPE_RULES:
        if field.endswith(suffix):
            return al_type
    return "Text[250]"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _input_contract_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _detect_spec_type(text: str) -> str:
    lowered = text.lower()
    for marker in _BUGFIX_MARKERS:
        if marker in lowered:
            return "bugfix"
    return "feature"


def _parse_user_stories(text: str) -> List[Dict[str, str]]:
    match = re.search(
        r"as\s+(.*?)\s+i\s+want\s+(?:to\s+be\s+able\s+to\s+)?(.*?),?\s+so\s+that\s+(.*?)(?:\r?\n|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    return [
        {
            "id": "US-001",
            "as_a": " ".join(match.group(1).split()),
            "i_want": " ".join(match.group(2).split()),
            "so_that": " ".join(match.group(3).split()),
        }
    ]


def _collect_object_evidence(root: Path, api_targets: List[str]) -> List[Dict[str, str]]:
    """Produce one evidence row per API target with a real field-mapping excerpt."""
    evidence: List[Dict[str, str]] = []
    for idx, rel in enumerate(api_targets, start=2):
        excerpt = "API page declares field mappings."
        try:
            content = (root / rel).read_text(encoding="utf-8", errors="replace")
            match = re.search(r'field\(\s*"[^"]+"\s*;\s*Rec\.[^\n]+', content)
            if match:
                excerpt = match.group(0).strip()
        except OSError:
            pass
        evidence.append(
            {
                "id": f"EV-{idx:03d}",
                "source_type": "local_code",
                "source": rel,
                "excerpt": excerpt,
                "confidence": "high",
                "status": "supported",
            }
        )
    return evidence


# --- Business Central project-fact parsing (id ranges, affixes, API metadata) ---

_OBJECT_HEADER = re.compile(
    r'(?im)^\s*(page|pageextension|table|tableextension|codeunit|enum|enumextension|report|query|xmlport|interface)'
    r'\s+(\d+)\s+("[^"]+"|[A-Za-z0-9_]+)'
)


def _load_id_ranges(root: Path) -> List[Dict[str, int]]:
    app_json = root / "app.json"
    if not app_json.exists():
        return []
    try:
        data = json.loads(app_json.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    ranges: List[Dict[str, int]] = []
    for r in data.get("idRanges") or []:
        if isinstance(r, dict) and "from" in r and "to" in r:
            ranges.append({"from": int(r["from"]), "to": int(r["to"])})
    single = data.get("idRange")
    if isinstance(single, dict) and "from" in single and "to" in single:
        ranges.append({"from": int(single["from"]), "to": int(single["to"])})
    return ranges


def _load_mandatory_affixes(root: Path) -> List[str]:
    cfg = root / "AppSourceCop.json"
    if not cfg.exists():
        return []
    try:
        data = json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    affixes = data.get("mandatoryAffixes") or []
    return [str(a) for a in affixes] if isinstance(affixes, list) else []


def _prop(content: str, name: str) -> Optional[str]:
    match = re.search(rf'(?im)^\s*{name}\s*=\s*([^;]+);', content)
    return match.group(1).strip() if match else None


def _parse_object_header(content: str) -> Optional[Dict[str, Any]]:
    match = _OBJECT_HEADER.search(content)
    if not match:
        return None
    return {
        "kind": match.group(1).lower(),
        "id": int(match.group(2)),
        "name": match.group(3).strip('"'),
    }


def _parse_api_metadata(content: str) -> Dict[str, Any]:
    def _is_false(value: Optional[str]) -> bool:
        return value is not None and value.strip().lower() == "false"

    modify = _prop(content, "ModifyAllowed")
    editable = _prop(content, "Editable")
    intent = _prop(content, "DataAccessIntent")
    perms = _prop(content, "Permissions")

    writable = True
    if _is_false(modify) or _is_false(editable):
        writable = False
    if intent and intent.strip().lower() == "readonly":
        writable = False
    if perms:
        granted = re.search(r'=\s*([rimdRIMD]+)', perms)
        letters = granted.group(1).upper() if granted else ""
        if letters and not any(c in letters for c in "IMD"):
            writable = False

    return {
        "api_version": _prop(content, "APIVersion"),
        "api_group": _prop(content, "APIGroup"),
        "api_publisher": _prop(content, "APIPublisher"),
        "entity_name": _prop(content, "EntityName"),
        "entity_set_name": _prop(content, "EntitySetName"),
        "source_table": _prop(content, "SourceTable"),
        "page_type": _prop(content, "PageType"),
        "odata_key_fields": _prop(content, "ODataKeyFields"),
        "has_application_area": bool(re.search(r"(?im)ApplicationArea\s*=", content)),
        "modify_allowed": modify,
        "insert_allowed": _prop(content, "InsertAllowed"),
        "delete_allowed": _prop(content, "DeleteAllowed"),
        "data_access_intent": intent,
        "permissions": perms,
        "writable": writable,
    }


async def handle_write_spec(
    project_root: str,
    spec_name: str,
    human_bullets: str,
    analysis: Optional[str] = None,
    clarifications: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    template: str = "tdd",
) -> Dict[str, Any]:
    """Generate TDD and machine spec from human bullets.

    For V1, generates a scaffold that the model fills in; the structure and
    file locations are server-enforced.
    """
    validate_spec_name(spec_name)
    validate_idempotency_key(idempotency_key)

    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
    specs_dir.mkdir(parents=True, exist_ok=True)

    # PLANNING source of truth = the captured item-context bundle (description + comments +
    # related), if present. Caller bullets are kept as a SUPPLEMENT — they carry the
    # orchestrator's synthesis (precedents, refinement-validated targets) and every object
    # they introduce must still resolve against the repo index, so nothing ungrounded can
    # sneak in. Discarding them would force planning from raw ticket prose alone.
    from bc_agentic_mcp import item_context as _ic
    _ctx = _ic.context_source(str(root), spec_name)
    # Delivery lane from CAPTURED identity: a Bug gets a bugfix-typed spec carrying a
    # mandatory symptom-regression requirement (test red before fix, green after).
    _lane = _ic.lane(str(root), spec_name)
    context_sha = _ctx["sha"] if _ctx else None
    if _ctx and _ctx["text"].strip():
        caller_bullets = (human_bullets or "").strip()
        human_bullets = _ctx["text"]
        if caller_bullets:
            human_bullets += (
                "\n\n## Orchestrator grounding (resolver-verified supplement)\n"
                + caller_bullets
            )

    # Idempotency (keyed on the context hash so a changed item auto-regenerates — no stale reuse)
    idem_dir = specs_root(root) / ".idempotency"
    idem_dir.mkdir(parents=True, exist_ok=True)
    idem_key_path = idem_dir / f"{spec_name}.key"
    input_contract = {
        "spec_name": spec_name,
        "human_bullets": human_bullets,
        "analysis": analysis or "",
        "clarifications": clarifications or "",
        "template": template,
        "context_sha": context_sha or "",
    }
    contract_hash = _input_contract_hash(input_contract)
    effective_key = f"{idempotency_key}|{context_sha or ''}|{contract_hash}"
    if idem_key_path.exists():
        stored = idem_key_path.read_text().strip()
        if stored == effective_key:
            tdd_path = specs_dir / "TDD.md"
            spec_path = specs_dir / "spec.json"
            if tdd_path.exists() and spec_path.exists():
                return {
                    "tdd_path": str(tdd_path),
                    "machine_spec_path": str(spec_path),
                    "summary": {"title": spec_name, "status": "already exists (idempotent)"},
                    "references": [],
                }

    # NOTE: the key is deliberately NOT persisted here. A timed-out/failed build
    # that had already recorded its key turned every retry into a ghost replay of
    # nothing (observed live: spec-a9 timeout poisoned the key while spec.json
    # stayed at the previous run). Persist ONLY next to a successful artifact.
    def _persist_idem_key() -> None:
        try:
            idem_key_path.write_text(effective_key)
        except OSError:
            pass

    if not analysis:
        analysis_path = specs_dir / "analysis.md"
        if analysis_path.exists():
            analysis = analysis_path.read_text(encoding="utf-8")

    api_name = _extract_api_name(human_bullets)
    requested_fields = _extract_requested_fields(human_bullets)
    references = _extract_urls(human_bullets)

    # Route (Anthropic routing pattern): a non-API item uses the general builder so the plan
    # describes the ACTUAL work (table/field/page/upgrade), not a hardcoded API template.
    from bc_agentic_mcp import work_extraction as _wx
    from bc_agentic_mcp.tools import general_spec as _general_spec
    _work = _wx.summarize(human_bullets)
    if "api" not in _work["work_types"] and not api_name:
        _result = _general_spec.build(
            root, specs_dir, spec_name, human_bullets, clarifications or "",
            references, idempotency_key, template, _work, context_sha=context_sha,
            spec_type="bugfix" if _lane == "bugfix" else "feature",
        )
        if not str(_result.get("status", "")).startswith("blocked"):
            _persist_idem_key()
        return _result

    api_targets = _find_api_targets(root, api_name, requested_fields)

    objects_to_modify = [
        {
            "type": "API Page",
            "target": target,
            "change": f"Expose and allow updates for requested subprocess fields on {api_name}",
        }
        for target in api_targets
    ]

    grounding_status = "grounded" if api_targets else "needs_grounding"
    open_questions: List[Dict[str, Any]] = []
    if not api_targets:
        open_questions.append(
            {
                "id": "OQ-001",
                "question": (
                    "Could not ground a concrete API page target from the provided request. "
                    "Confirm the exact API object name/path before planning or implementation."
                ),
                "blocking": True,
            }
        )

    business_rules = []
    if requested_fields:
        business_rules.append(
            {
                "id": "BR-001",
                "description": "Expose new rental-mutation subprocess on-hold fields as API attributes",
                "fields": requested_fields,
            }
        )
        business_rules.append(
            {
                "id": "BR-002",
                "description": "Allow read and update of mapped fields through the rentalMutation API",
                "mode": "read-write",
            }
        )

    goal = (
        f"Extend API {api_name} so external apps can read/update rental mutation subprocess on-hold attributes"
        if api_name
        else "Implement requested API extension based on item description"
    )
    scope = (
        f"Modify existing API pages ({len(api_targets)} targets) and map {len(requested_fields)} fields without introducing unrelated changes"
    )
    objects_summary = "\n".join(f"- Modify: {target}" for target in api_targets) or "- No API targets inferred"
    data_model_summary = "\n".join(f"- {name}" for name in requested_fields) or "- No requested fields inferred"
    references_summary = "\n".join(f"- {u}" for u in references) or "- None"

    tdd_content = f"""# Technical Design Document: {spec_name}

## 1. Overview
**Goal:** {goal}
**Scope:** {scope}

### Human Requirements
{human_bullets}

### Clarifications
{clarifications or 'None'}

## 2. Key Decisions
- Reuse existing rentalMutation API pages and add field mappings only.
- Keep field behavior aligned with current API patterns (direct Rec mappings where applicable).

## 3. Objects
{objects_summary}

## 4. Data Model
{data_model_summary}

## 5. Business Logic
- API supports read and update for requested subprocess fields.
- No additional workflow/state machine changes are implied unless explicitly requested.

## 6. Integration Points
- API endpoint family: {api_name or 'unspecified API'}
- External app consumer: Maintenance App (as described in requirements)

## 7. Upgrade Considerations
- No data migration expected for API-only field exposure.
- Validate backward compatibility for existing rentalMutation consumers.

## 8. Testing Strategy
- Verify all requested fields are readable via API.
- Verify all requested fields are updatable via API.
- Verify unchanged behavior on existing rentalMutation attributes.

## 9. References
{references_summary}
"""
    tdd_path = specs_dir / "TDD.md"
    tdd_path.write_text(tdd_content, encoding="utf-8")

    # --- Bulletproof machine spec construction (schema v2.0) ---
    description_sha = _sha256(human_bullets)
    spec_type = "bugfix" if _lane == "bugfix" else _detect_spec_type(human_bullets)
    user_stories = _parse_user_stories(human_bullets)
    story_ref = user_stories[0]["id"] if user_stories else None

    lowered = human_bullets.lower()
    op_read = True  # exposing an API attribute implies it is readable
    op_update = "update" in lowered

    # Stable object ids + version + fields_added + BC object facts.
    id_ranges = _load_id_ranges(root)
    mandatory_affixes = _load_mandatory_affixes(root)
    object_ids = [f"OBJ-{i + 1:03d}" for i in range(len(objects_to_modify))]
    for oid, obj in zip(object_ids, objects_to_modify):
        version_match = re.search(r"[\\/]v(\d+)[\\/]", obj["target"].lower())
        obj["id"] = oid
        obj["version"] = f"v{version_match.group(1)}" if version_match else None
        obj["fields_added"] = list(requested_fields)
        obj["evidence_refs"] = []
        header: Optional[Dict[str, Any]] = None
        api_meta: Dict[str, Any] = {}
        try:
            obj_content = (root / obj["target"]).read_text(encoding="utf-8", errors="replace")
            header = _parse_object_header(obj_content)
            api_meta = _parse_api_metadata(obj_content)
        except OSError:
            pass
        if header:
            obj["object_kind"] = header["kind"]
            obj["object_id"] = header["id"]
            obj["object_name"] = header["name"]
            obj["id_in_range"] = (
                any(r["from"] <= header["id"] <= r["to"] for r in id_ranges)
                if id_ranges
                else None
            )
        obj["api"] = api_meta
        obj["writable"] = api_meta.get("writable", True)

    # Evidence rows: description-sourced field set + per-object local-code rows.
    evidence: List[Dict[str, str]] = []
    if requested_fields:
        wi_url = next((u for u in references if "workitem" in u.lower()), "")
        evidence.append(
            {
                "id": "EV-001",
                "source_type": "work_item" if wi_url else "description",
                "source": wi_url or "human_description",
                "excerpt": ", ".join(requested_fields),
                "confidence": "high",
                "status": "supported",
            }
        )
    object_evidence = _collect_object_evidence(root, api_targets)
    evidence.extend(object_evidence)
    object_ev_ids = [e["id"] for e in object_evidence]
    base_ev_ids = (["EV-001"] if requested_fields else []) + object_ev_ids

    for obj in objects_to_modify:
        obj["evidence_refs"] = object_ev_ids or (["EV-001"] if requested_fields else [])

    for br in business_rules:
        br.setdefault("ears_type", "ubiquitous")
        br["evidence_refs"] = base_ev_ids or (["EV-001"] if requested_fields else [])

    api_label = api_name or "target"
    src_table = "RentalMutationHSG" if (api_name and "rentalmutation" in api_name.lower()) else ""

    requirements: List[Dict[str, Any]] = []
    acceptance_tests: List[Dict[str, Any]] = []
    req_to_test: Dict[str, List[str]] = {}
    if requested_fields:
        req_ev = base_ev_ids or ["EV-001"]
        # AT-001: read/availability; AT-002 (regression) appended last.
        acceptance_tests.append(
            {
                "id": "AT-001",
                "requirement_ref": "REQ-002",
                "type": "read",
                "given": f"A {api_label} record exists",
                "when": "An external app reads the record via the API",
                "then": "All requested subprocess on-hold attributes are returned",
            }
        )
        requirements.append(
            {
                "id": "REQ-001",
                "story_ref": story_ref,
                "ears_type": "ubiquitous",
                "statement": f"The {api_label} API shall expose the requested subprocess on-hold fields as API attributes.",
                "fields": list(requested_fields),
                "evidence_refs": req_ev,
                "acceptance_tests": ["AT-001"],
                "status": "supported",
            }
        )
        req_to_test["REQ-001"] = ["AT-001"]
        requirements.append(
            {
                "id": "REQ-002",
                "story_ref": story_ref,
                "ears_type": "event",
                "statement": f"When an external app sends a GET request to the {api_label} API, the API shall return the current values of the subprocess on-hold fields.",
                "fields": list(requested_fields),
                "evidence_refs": req_ev,
                "acceptance_tests": ["AT-001"],
                "status": "supported",
            }
        )
        req_to_test["REQ-002"] = ["AT-001"]

        next_req = 3
        if op_update:
            update_id = f"REQ-{next_req:03d}"
            acceptance_tests.append(
                {
                    "id": "AT-002",
                    "requirement_ref": update_id,
                    "type": "update",
                    "given": f"A {api_label} record exists",
                    "when": "An external app updates a subprocess on-hold attribute via PATCH",
                    "then": "A subsequent GET returns the updated value",
                }
            )
            requirements.append(
                {
                    "id": update_id,
                    "story_ref": story_ref,
                    "ears_type": "event",
                    "statement": f"When an external app sends a PATCH request to the {api_label} API, the API shall persist the provided subprocess on-hold field values.",
                    "fields": list(requested_fields),
                    "evidence_refs": req_ev,
                    "acceptance_tests": ["AT-002"],
                    "status": "supported",
                }
            )
            req_to_test[update_id] = ["AT-002"]
            next_req += 1

        regression_id = f"REQ-{next_req:03d}"
        regression_test = f"AT-{len(acceptance_tests) + 1:03d}"
        acceptance_tests.append(
            {
                "id": regression_test,
                "requirement_ref": regression_id,
                "type": "regression",
                "given": "Existing API consumers query the endpoint",
                "when": "The new fields are added",
                "then": "Previously exposed attributes remain unchanged",
            }
        )
        requirements.append(
            {
                "id": regression_id,
                "story_ref": story_ref,
                "ears_type": "ubiquitous",
                "statement": f"The {api_label} API shall preserve the existing behavior of all previously exposed attributes.",
                "fields": [],
                "evidence_refs": object_ev_ids or req_ev,
                "acceptance_tests": [regression_test],
                "status": "supported",
            }
        )
        req_to_test[regression_id] = [regression_test]

        # Standard API contract shapes (spec-time test pyramid): every API item must
        # declare a NEGATIVE and an EDGE scenario up front — the review quality gate
        # refuses plans whose declared shapes are incomplete (Bug 267600 lesson).
        next_req += 1
        negative_id = f"REQ-{next_req:03d}"
        negative_test = f"AT-{len(acceptance_tests) + 1:03d}"
        acceptance_tests.append(
            {
                "id": negative_test,
                "requirement_ref": negative_id,
                "type": "negative",
                "path_shape": "negative",
                "given": f"A {api_label} record exists",
                "when": "An external app sends an invalid value for a subprocess on-hold attribute",
                "then": "The API rejects the request with an error and the record is unchanged",
            }
        )
        requirements.append(
            {
                "id": negative_id,
                "story_ref": story_ref,
                "ears_type": "unwanted",
                "statement": f"If an invalid value is provided, then the {api_label} API shall reject the request with an error.",
                "fields": list(requested_fields),
                "evidence_refs": req_ev,
                "acceptance_tests": [negative_test],
                "status": "supported",
            }
        )
        req_to_test[negative_id] = [negative_test]

        next_req += 1
        edge_id = f"REQ-{next_req:03d}"
        edge_test = f"AT-{len(acceptance_tests) + 1:03d}"
        acceptance_tests.append(
            {
                "id": edge_test,
                "requirement_ref": edge_id,
                "type": "edge",
                "path_shape": "edge",
                "given": f"A {api_label} record exists",
                "when": "The same request is sent twice (repeat/idempotency)",
                "then": "The second response equals the first and no duplicate state is created",
            }
        )
        requirements.append(
            {
                "id": edge_id,
                "story_ref": story_ref,
                "ears_type": "event",
                "statement": f"When the same request is repeated, the {api_label} API shall return an identical result without side effects.",
                "fields": [],
                "evidence_refs": req_ev,
                "acceptance_tests": [edge_test],
                "status": "supported",
            }
        )
        req_to_test[edge_id] = [edge_test]

    data_model: List[Dict[str, Any]] = []
    for field in requested_fields:
        field_type = _infer_al_type(field)
        source_field = field
        data_model.append(
            {
                "field": field,
                "source_field": source_field,
                "al_type": field_type,
                "source_table": src_table,
                "api_attribute": _camel(field),
                "api_mapping_expr": _api_mapping_expr(field_type, source_field),
                "read": op_read,
                "update": op_update,
                "evidence_refs": (["EV-001"] if requested_fields else []) + object_ev_ids,
            }
        )

    assumptions: List[Dict[str, Any]] = []
    if any(field.endswith("OnHoldTill") for field in requested_fields):
        assumptions.append(
            {
                "id": "AS-001",
                "statement": "On-hold date fields are exposed as direct mappings without additional date validation.",
                "rationale": "Existing OnHold/OnHoldDate API fields in the codebase are passthrough mappings.",
                "reversible": True,
                "evidence_refs": object_ev_ids or (["EV-001"] if requested_fields else []),
            }
        )

    traceability = {
        "requirement_to_test": req_to_test,
        "requirement_to_object": {r["id"]: list(object_ids) for r in requirements},
        "field_to_object": {field: list(object_ids) for field in requested_fields},
    }

    spec_json = {
        "schema_version": "2.0",
        "spec_id": spec_name,
        "spec_name": spec_name,
        "spec_type": spec_type,
        "status": grounding_status,
        "version": 1,
        "idempotency_key": idempotency_key,
        "module": root.name,
        "source": {
            "references": references,
            "description_sha256": description_sha,
        },
        "summary": {
            "goal": goal,
            "in_scope": ([f"Extend {api_name} API"] if api_name else []),
            "out_of_scope": [],
        },
        "user_stories": user_stories,
        "requirements": requirements,
        "data_model": data_model,
        "objects_to_create": [],
        "objects_to_modify": objects_to_modify,
        "business_rules": business_rules,
        "acceptance_tests": acceptance_tests,
        "evidence": evidence,
        "assumptions": assumptions,
        "open_questions": open_questions,
        "event_subscribers": [],
        "scope_boundaries": {
            # Deterministic: derive the allowed root(s) from the ACTUAL target paths so
            # the scope-coherence validator can never flag a hardcoded/mismatched root
            # (observed: hardcoded "extensions" vs repos whose apps live under "src").
            "allowed_extensions": sorted(
                {
                    p.replace("\\", "/").split("/")[0]
                    for p in api_targets
                    if p and p.replace("\\", "/").split("/")[0]
                }
            ) or ["extensions"],
            "allowed_files": api_targets,
            "forbidden_patterns": [],
            "scope_mode": "permissive",
        },
        "bc_metadata": {
            "id_ranges": id_ranges,
            "mandatory_affixes": mandatory_affixes,
            "analyzers": discover_analyzers(root),
            "api_versions": sorted({o["version"] for o in objects_to_modify if o.get("version")}),
        },
        "traceability": traceability,
        "references": references,
    }
    spec_path = specs_dir / "spec.json"
    from bc_agentic_mcp import provenance as _prov
    _prov.stamp(spec_json, context_sha)
    contract_issues = validate_spec_contract(spec_json, strict_schema=True)
    if contract_issues:
        return {
            "status": "blocked_invalid_generated_spec",
            "reason": "Generated spec contract failed validation: " + "; ".join(contract_issues),
            "tdd_path": str(tdd_path),
            "machine_spec_path": None,
            "references": references,
        }
    spec_path.write_text(json.dumps(spec_json, indent=2))
    _persist_idem_key()  # only a SUCCESSFUL build may claim its idempotency key

    # Register with state manager so bc_status can track it
    from bc_agentic_mcp.state import StateManager
    sm = StateManager(specs_root(root))
    sm.init()  # ensures state.json exists
    try:
        sm.get_spec(spec_name)
    except KeyError:
        sm.add_spec(spec_name, template or "tdd")

    return {
        "tdd_path": str(tdd_path),
        "machine_spec_path": str(spec_path),
        "summary": {"title": spec_name},
        "references": references,
    }
