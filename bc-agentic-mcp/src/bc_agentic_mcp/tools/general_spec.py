"""general_spec — build a correct machine spec + TDD for NON-API BC items.

The planner's original path assumed every item was a rentalMutation API extension. This builder
handles the general case (table field, page, upgrade codeunit, enum, ...) by routing on the
deterministic work classification, grounding objects in the real repo, and rendering an
externalized template. Keeps the API path (write_spec) untouched.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import object_resolver, planner_render
from bc_agentic_mcp.spec_loader import validate_spec_contract


def _read_data_per_company(root: Path, rel_path: Optional[str]) -> bool:
    """Read DataPerCompany from a table object; AL default is true when omitted."""
    if not rel_path:
        return True
    try:
        content = (root / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    match = re.search(r"(?im)^\s*DataPerCompany\s*=\s*(\w+)", content)
    if not match:
        return True
    return match.group(1).strip().lower() != "false"


def _table_data_per_company(root: Path, table_ref: Optional[str]) -> bool:
    """DataPerCompany for a table referenced by REL PATH or NAME.

    Name lookups resolve through the object index (an in-repo table like FeatureSAN
    answers with its real ``DataPerCompany = false``); unknown/base tables keep the
    AL default (true -> per-company). Deriving scope from the table itself is the
    G-0008 rule — never copied from a neighbouring upgrade codeunit.
    """
    if not table_ref:
        return True
    if ".al" in str(table_ref).lower():
        return _read_data_per_company(root, table_ref)
    try:
        from bc_agentic_mcp import object_index
        objects = object_index.refresh(root, max_age_seconds=300).get("objects") or {}
        ref = str(table_ref).strip().lower()
        hit = objects.get(ref)
        # Name keys collide across kinds (enum FeatureSAN shadows table FeatureSAN —
        # observed live): when the name-key hit is not a table, search for the TABLE
        # object of that name explicitly.
        if not (hit and hit.get("kind") in ("table", "tableextension")):
            hit = next((v for v in objects.values()
                        if v.get("kind") in ("table", "tableextension")
                        and str(v.get("name", "")).lower() == ref), None)
        if hit:
            props = " ".join(((hit.get("detail") or {}).get("props")) or [])
            m = re.search(r"(?i)DataPerCompany\s*=\s*(\w+)", props)
            if m:
                return m.group(1).strip().lower() != "false"
            return _read_data_per_company(root, hit.get("rel"))
    except Exception:
        pass
    return True


def _goal(work: Dict[str, Any], objects: List[Dict[str, Any]], fields: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    table = next((o["name"] for o in objects if o["kind"] == "table" and o.get("name")), None)
    if fields and table:
        parts.append(f"Add field(s) {', '.join(f['name'] for f in fields)} to table {table}")
    elif fields:
        parts.append(f"Add field(s) {', '.join(f['name'] for f in fields)}")
    if any(o["kind"] == "page" for o in objects):
        page = next((o["name"] for o in objects if o["kind"] == "page"), "the page")
        parts.append(f"show the field on page {page}")
    removals = [o for o in objects if o.get("action") == "remove" and o.get("name")]
    if removals:
        parts.append("remove " + ", ".join(f"{o['kind']} {o['name']}" for o in removals))
    if "upgrade" in work["work_types"]:
        parts.append("deliver a data upgrade that populates existing records"
                     if "removal" not in work["work_types"]
                     else "deliver a data upgrade that cleans up existing records")
    return "; ".join(parts) or "Implement the requested Business Central change."


def _impl_notes(work: Dict[str, Any], objects: List[Dict[str, Any]]) -> str:
    notes: List[str] = []
    for o in objects:
        if not o.get("resolved") and o.get("action") == "modify":
            notes.append(f"- WARNING: {o['kind']} `{o.get('name')}` was NOT found in the repo — verify the name/id before implementing.")
    removals = [o for o in objects if o.get("action") == "remove" and o.get("name")]
    if removals:
        notes.append("- Removal work: delete the object(s) "
                     + ", ".join(f"`{o['kind']} {o['name']}`" for o in removals)
                     + " and every reference to them; the module must compile clean afterwards.")
    if any(o["kind"] == "table" and o.get("action") != "remove" for o in objects):
        notes.append("- Add the field with an id above the current maximum; set Editable per the item; add ENU caption/tooltip inline.")
    if any(o["kind"] == "page" and o.get("action") != "remove" for o in objects):
        notes.append("- Add the field to the page repeater in the intended position.")
    if "upgrade" in work["work_types"]:
        notes.append("- Register the upgrade with a unique tag; scope it to the table's DataPerCompany (false -> per-database, true -> per-company); make it idempotent (Get before Modify).")
    return "\n".join(notes) or "- Follow the existing conventions of the touched objects."


def _upgrade_notes(work: Dict[str, Any]) -> str:
    if "upgrade" in work["work_types"]:
        return ("- A data upgrade codeunit is required. **Derive its scope from the target table's "
                "`DataPerCompany`**: `false` = shared -> per-database (runs once, no company guard); "
                "`true` -> per-company. Use a unique upgrade tag and make the write idempotent.")
    return "- No data migration required."


def _testing_notes(work: Dict[str, Any]) -> str:
    lines = ["- Compile against the target container symbols."]
    if any(w in work["work_types"] for w in ("table-field", "page")):
        lines.append("- Verify the new field is visible/persisted and honours its Editable setting.")
    if "upgrade" in work["work_types"]:
        lines.append("- Run the upgrade in a container and assert existing records are populated exactly once (idempotent on re-run).")
    return "\n".join(lines)


def build(
    root: Path,
    specs_dir: Path,
    spec_name: str,
    human_bullets: str,
    clarifications: str,
    references: List[str],
    idempotency_key: str,
    template: str,
    work: Dict[str, Any],
    context_sha: Optional[str] = None,
    spec_type: str = "feature",
) -> Dict[str, Any]:
    root = Path(root).resolve()
    try:
        from bc_agentic_mcp import symbols as _symbols
        _lookup = _symbols.make_lazy_symbol_lookup(root)
    except Exception:
        _lookup = None

    # P0: refinement is AUTHORITATIVE grounding. The item's verified claims
    # (item_refinement.json, written by bc_refine_item after confronting the ticket
    # with code reality) supply table targets the bullet extractor may have missed —
    # the machine must never re-ask a human for what refinement already proved.
    try:
        import json as _json
        refinement_path = specs_dir / "item_refinement.json"
        if refinement_path.exists():
            known = {(str(o.get("kind", "")).lower(), str(o.get("name", "")).lower())
                     for o in work["objects"]}
            for claim in _json.loads(refinement_path.read_text(encoding="utf-8")).get("claims", []):
                for table in claim.get("tables", []):
                    key = ("table", str(table.get("name", "")).lower())
                    if table.get("name") and key not in known:
                        known.add(key)
                        work["objects"].append({
                            "kind": "table", "name": table["name"],
                            "id": table.get("number"), "action": "modify",
                            "grounding": "refinement",
                        })
    except Exception:
        pass  # refinement merge is additive; its absence never blocks the build

    # ALIAS corrections (orchestrator judgment): tickets routinely name objects WRONG
    # (observed live: 'page RealtyObjectFacilitiesFDN' for the real page 11030034
    # FacilitiesOfRealtyObjectFDN). The refine step surfaces the mismatch; the caller
    # records the correction as a bullet line:  ALIAS: <ticket-name> = <real-name>
    # Mentions of the ticket name are renamed (or dropped when the real object is
    # already extracted) BEFORE resolution — corrections to inputs, never transcribed.
    alias_map = {
        m.group(1).strip().lower(): m.group(2).strip()
        for m in re.finditer(
            r"(?im)^[-*\s]*ALIAS\s*:\s*\"?([A-Za-z0-9_]+)\"?\s*=\s*\"?([A-Za-z0-9_]+)\"?\s*$",
            human_bullets or "")
    }
    if alias_map:
        seen = {(str(o.get("kind", "")).lower(), str(o.get("name", "")).lower())
                for o in work["objects"]}
        kept = []
        for o in work["objects"]:
            real = alias_map.get(str(o.get("name", "")).lower())
            if real:
                key = (str(o.get("kind", "")).lower(), real.lower())
                if key in seen and str(o.get("name", "")).lower() != real.lower():
                    continue  # real object already extracted — drop the alias duplicate
                o["name"] = real
                seen.add(key)
            kept.append(o)
        work["objects"] = kept

    resolved = object_resolver.resolve(root, work["objects"], symbol_lookup=_lookup)

    objects_to_modify: List[Dict[str, Any]] = []
    objects_to_create: List[Dict[str, Any]] = []
    allowed_files: List[str] = []
    for o in resolved:
        if o.get("action") == "modify" and o.get("target"):
            objects_to_modify.append({
                "type": o["kind"].title(), "target": o["target"], "name": o.get("name"),
                "object_id": o.get("object_id"), "change": f"Extend {o.get('name')}", "resolved": True,
            })
            allowed_files.append(o["target"])
        elif o.get("action") == "remove" and (o.get("path") or o.get("target")):
            # Decommission: the object's file is IN scope (it will be deleted/emptied) and
            # rides in objects_to_modify (schema-safe) with an explicit Remove change.
            # The DECLARED bullet path is authoritative: after the deletion lands, the
            # resolver can no longer find the object in the repo and falls back to
            # symbol-package internal paths (src/…) — which corrupted allowed_files and
            # blocked the commit gate (observed live on Bug 267600 re-review).
            remove_target = o.get("path") or o.get("target")
            objects_to_modify.append({
                "type": o["kind"].title(), "target": remove_target, "name": o.get("name"),
                "object_id": o.get("object_id"), "change": f"Remove {o.get('name')}", "resolved": True,
            })
            allowed_files.append(remove_target)
        else:
            if o.get("action") in ("modify", "remove"):
                # Unresolved modify/remove WITH an explicit .al path in the item is
                # grounded by the human — honor the path (observed live: permissionset
                # '2C-ALG-PAGINA ALLEN' is not in the object index, so the explicit
                # target was silently dropped from scope and the write got
                # scope_violation). Without a path it surfaces below as a BLOCKING
                # open question — never a phantom create.
                explicit = o.get("path")
                if explicit:
                    objects_to_modify.append({
                        "type": str(o.get("kind", "object")).title(), "target": explicit,
                        "name": o.get("name"), "object_id": o.get("object_id"),
                        "change": f"{'Remove' if o.get('action') == 'remove' else 'Extend'} {o.get('name')}",
                        "resolved": True,
                    })
                    allowed_files.append(explicit)
                    o["target"] = explicit
                    o["resolved"] = True
                continue
            create_target = o.get("path")  # explicit .al path in the item wins
            if not create_target and o.get("action") == "create" and o.get("kind") == "codeunit" \
                    and o.get("name") and o.get("subtype") == "upgrade":
                # Deterministic default placement applies to UPGRADE codeunits only —
                # forcing every new codeunit into _Upgrade\ made feature/helper codeunits
                # inherit a mandatory upgrade contract they can never satisfy (observed live).
                create_target = f"extensions\\BaseApp\\src\\_Upgrade\\{o.get('name')}.Codeunit.al"
            if create_target:
                allowed_files.append(create_target)
                # Reflect the derived target back into the resolver entry — REVIEW.md
                # renders from `resolved`, and a create with a known path must never
                # print as 'UNRESOLVED: confirm target path' (blocks the approval gate).
                o["target"] = create_target
                o["resolved"] = True
            objects_to_create.append({
                "type": o["kind"].title(),
                "name": o.get("name"),
                "subtype": o.get("subtype"),
                "change": "Create",
                "resolved": bool(o.get("resolved")) or bool(create_target),
                "target": create_target,
            })

    object_ids: List[str] = []
    for idx, obj in enumerate(objects_to_modify, start=1):
        oid = f"OBJ-{idx:03d}"
        obj["id"] = oid
        object_ids.append(oid)
    for idx, obj in enumerate(objects_to_create, start=len(object_ids) + 1):
        oid = f"OBJ-{idx:03d}"
        obj["id"] = oid
        object_ids.append(oid)

    # Fail-closed grounding gate: a modification we cannot ground in the repo must not
    # silently flow downstream as a phantom "create". Surface it so plan/tasks can block.
    unresolved_mods = [o for o in resolved
                       if o.get("action") == "modify" and not o.get("target")]
    unresolved_removes = [o for o in resolved
                          if o.get("action") == "remove" and not o.get("target")]
    incomplete_creates = [
        o for o in resolved
        if o.get("action") == "create" and not o.get("name")
    ]
    unresolved_create_targets = [
        o for o in resolved
        if o.get("action") == "create"
        and o.get("kind") == "codeunit"
        and o.get("name")
        and o.get("subtype") == "upgrade"  # only upgrade codeunits demand a derived target
        and not any(
            (c.get("type") == "Codeunit")
            and (c.get("name") == o.get("name"))
            and c.get("target")
            for c in objects_to_create
        )
    ]
    implies_modify = any(
        w in ("table-field", "page", "tableextension", "pageextension")
        for w in work["work_types"]
    )
    open_questions: List[Dict[str, Any]] = []
    grounding_status = "grounded"
    if unresolved_mods or unresolved_removes or (implies_modify and not objects_to_modify) or incomplete_creates or unresolved_create_targets:
        grounding_status = "needs_grounding"
        for o in unresolved_mods:
            open_questions.append({
                "id": f"OQ-{len(open_questions) + 1:03d}",
                "question": (f"Could not ground {o.get('kind')} '{o.get('name')}' in the repo. "
                             "Confirm the exact object name/id before implementation."),
                "blocking": True,
            })
        for o in unresolved_removes:
            open_questions.append({
                "id": f"OQ-{len(open_questions) + 1:03d}",
                "question": (f"Removal requested for {o.get('kind')} '{o.get('name')}' but it was NOT "
                             "found in the repo — confirm it still exists (it may already be removed "
                             "upstream) before planning the removal."),
                "blocking": True,
            })
        for o in incomplete_creates:
            kind = o.get("kind", "object")
            if o.get("subtype") == "upgrade":
                q = (
                    "Upgrade is requested but no concrete upgrade codeunit name/id was provided. "
                    "Confirm the upgrade object (name/id) and expected scope (per-company/per-database)."
                )
            else:
                q = (
                    f"A new {kind} is requested but no concrete name was provided. "
                    "Confirm the object name/id before implementation."
                )
            open_questions.append({
                "id": f"OQ-{len(open_questions) + 1:03d}",
                "question": q,
                "blocking": True,
            })
        for o in unresolved_create_targets:
            open_questions.append({
                "id": f"OQ-{len(open_questions) + 1:03d}",
                "question": (
                    f"Could not determine a concrete target file for create {o.get('kind')} '{o.get('name')}'. "
                    "Confirm the exact file target so scope can be enforced deterministically."
                ),
                "blocking": True,
            })
        if not open_questions:
            open_questions.append({
                "id": "OQ-001",
                "question": ("The item implies modifying an existing table/page, but no target "
                             "object resolved in the repo. Confirm the target object name/id."),
                "blocking": True,
            })

    data_model = [
        {"field": f["name"], "al_type": f["al_type"], "editable": f["editable"],
         "read": True, "update": True}
        for f in work["fields"]
    ]

    table_name = next((o["name"] for o in resolved if o["kind"] == "table" and o.get("name")), None) or "the target table"
    page_names = [o["name"] for o in resolved if o["kind"] == "page" and o.get("name")]

    requirements: List[Dict[str, Any]] = []
    acceptance_tests: List[Dict[str, Any]] = []

    def _add(statement: str, criterion: str, ears: str = "ubiquitous") -> None:
        rid = f"REQ-{len(requirements) + 1:03d}"
        tid = f"AT-{len(acceptance_tests) + 1:03d}"
        acceptance_tests.append({"id": tid, "requirement_ref": rid, "statement": criterion})
        requirements.append({"id": rid, "ears_type": ears, "statement": statement,
                             "acceptance_tests": [tid], "evidence_refs": []})

    for f in work["fields"]:
        not_editable = "" if f["editable"] else ", and the field is not editable"
        _add(
            f"The system shall expose a field '{f['name']}' of type {f['al_type']}"
            + ("" if f["editable"] else " (non-editable)") + f" on table {table_name}.",
            f"GIVEN the extension is installed, THEN table {table_name} exposes field '{f['name']}' "
            f"of type {f['al_type']}{not_editable}.",
        )
        for page in page_names:
            _add(
                f"The system shall display field '{f['name']}' on page {page}.",
                f"GIVEN page {page} is opened, THEN the column '{f['name']}' is visible.",
            )
    for o in resolved:
        if o.get("action") == "remove" and o.get("name"):
            _add(
                f"The system shall no longer contain {o['kind']} {o['name']} nor any reference to it.",
                f"GIVEN the extension is compiled and deployed, THEN {o['kind']} {o['name']} is removed, "
                "no references to it remain, and the module compiles clean.",
            )
    if "upgrade" in work["work_types"]:
        if "removal" in work["work_types"]:
            _add(
                "When the extension is upgraded, the system shall clean up the obsolete data "
                "exactly as specified (delete only the matching records).",
                "GIVEN existing obsolete records prior to the upgrade, WHEN the upgrade runs, THEN only "
                "the matching records are deleted, AND re-running the upgrade changes nothing (idempotent).",
                ears="event",
            )
        else:
            _add(
                "When the extension is upgraded, the system shall populate the new field for all "
                "existing records per the specified mapping.",
                "GIVEN existing records prior to the upgrade, WHEN the upgrade runs, THEN each record is "
                "populated with its mapped value, AND re-running the upgrade leaves the data unchanged (idempotent).",
                ears="event",
            )
    if not requirements:  # never emit a spec with no measurable acceptance criteria
        _add(
            f"The system shall implement the change described for {spec_name}.",
            "GIVEN the change is implemented, THEN it satisfies the human requirements and compiles "
            "against the target symbols.",
        )

    # Declared test shapes (TEST happy:/negative:/edge:/... lines) are the SPEC-TIME
    # test pyramid: they land as acceptance tests with an explicit path_shape so the
    # review quality gate can refuse an under-tested plan BEFORE the human gate.
    from bc_agentic_mcp import work_extraction as _wx_tests
    declared = _wx_tests.extract_declared_tests(human_bullets)
    if declared:
        rid = f"REQ-{len(requirements) + 1:03d}"
        test_ids: List[str] = []
        for d in declared:
            tid = f"AT-{len(acceptance_tests) + 1:03d}"
            acceptance_tests.append({
                "id": tid, "requirement_ref": rid,
                "statement": f"[{d['shape']}] {d['scenario']}",
                "path_shape": d["shape"],
            })
            test_ids.append(tid)
        requirements.append({
            "id": rid, "ears_type": "ubiquitous",
            "statement": "The change shall be proven by the declared test scenarios "
                         "(happy / negative / edge shapes as listed).",
            "acceptance_tests": test_ids, "evidence_refs": [],
        })

    # EXPLICIT TEST-PLAN LINES are handled ABOVE by extract_declared_tests (canonical:
    # structured path_shape consumed by the review quality gate and the feature test
    # matrix). A second regex parser here duplicated every declared test (observed live
    # on facility-code-filter: AT-004/005 + AT-006/007 twins) — removed.

    # Bugfix lane: the symptom MUST be pinned by a regression requirement — a test that
    # reproduces the bug (red on pre-fix code) and proves the fix (green after). EARS
    # 'unwanted' shape; bc_generate_tests turns it into the mandatory regression slice.
    if spec_type == "bugfix":
        symptom = ""
        try:
            rc = json.loads((specs_dir / "root_cause.json").read_text(encoding="utf-8"))
            symptom = str(rc.get("symptom", "")).strip()
        except (OSError, json.JSONDecodeError):
            pass
        symptom = symptom or f"the defect described for {spec_name}"
        _add(
            f"If the pre-fix scenario recurs ({symptom}), then the system shall behave "
            "correctly as specified by the fix.",
            f"GIVEN the exact scenario that produced the bug ({symptom}), WHEN it is executed "
            "after the fix, THEN the correct behavior occurs — this test MUST fail on pre-fix "
            "code and pass after (record with layer='al-regression').",
            ears="unwanted",
        )

    upgrade_contract: Dict[str, Any] = {}
    upgrade_contracts: List[Dict[str, Any]] = []
    if "upgrade" in work["work_types"]:
        upgrade_creates = [o for o in objects_to_create
                           if str(o.get("subtype") or "").lower() == "upgrade" and o.get("target")]
        # Per-create contracts: each upgrade codeunit bullet declares ITS data target
        # (data target table: "X" inside the same bullet); scope is derived from the
        # table's own DataPerCompany — repo file first, then the object index (an
        # in-repo table like FeatureSAN resolves with its real properties), and the
        # AL default (true -> per-company) only when the table is a base app table.
        bullet_blocks = re.split(r"(?m)^\s*-\s*", human_bullets)
        for o in upgrade_creates:
            name = str(o.get("name") or "")
            block = next((b for b in bullet_blocks if name and name in b), human_bullets)
            m = re.search(r'data target table:\s*"?([A-Za-z0-9 _.&-]+?)"?\s*(?:$|[\r\n;(])',
                          block, re.IGNORECASE | re.MULTILINE)
            table_target = m.group(1).strip() if m else None
            dpc = _table_data_per_company(root, table_target)
            # The REPO's real upgrade tag (SAN framework enum value, e.g.
            # EMP_239597_UpdVeraSpaceDetTypeFacFilterFDN) wins over the generated
            # placeholder — the write gate greps the CODE for this exact string, and
            # real implementations carry the enum tag, never '<spec>_upgrade_vN'
            # (observed live: user's finished 239597 blocked on the invented tag).
            tag_m = re.search(r"\b(EMP_\d+_[A-Za-z0-9_]+)\b", block)
            tag_value = tag_m.group(1) if tag_m else None
            if not tag_value:
                # Tag registration often lives in a SEPARATE bullet (the enumextension
                # one) — when the whole item declares exactly ONE repo tag, it belongs
                # to this sole upgrade codeunit.
                all_tags = sorted(set(re.findall(r"\b(EMP_\d+_[A-Za-z0-9_]+)\b", human_bullets)))
                if len(all_tags) == 1 and len(upgrade_creates) == 1:
                    tag_value = all_tags[0]
            upgrade_contracts.append({
                "codeunit_target": o.get("target"),
                "table_target": table_target,
                "data_per_company": dpc,
                "required_scope": "per-company" if dpc else "per-database",
                "idempotency_tag": (tag_value
                                     or f"{spec_name}_upgrade_v{len(upgrade_contracts) + 1}"),
            })
        # Legacy single contract stays for one-target specs (backward compatible).
        table_target = next((o.get("target") for o in objects_to_modify if str(o.get("type", "")).lower() == "table"), None)
        if not table_target:
            m = re.search(r'data target table:\s*"?([A-Za-z0-9 _.&-]+?)"?\s*(?:$|[\r\n;(])',
                          human_bullets, re.IGNORECASE | re.MULTILINE)
            if m:
                table_target = m.group(1).strip()
        dpc = _table_data_per_company(root, table_target)
        required_scope = "per-company" if dpc else "per-database"
        legacy_tag_m = re.search(r"\b(EMP_\d+_[A-Za-z0-9_]+)\b", human_bullets)
        upgrade_contract = {
            "table_target": table_target,
            "data_per_company": dpc,
            "required_scope": required_scope,
            "idempotency_tag": (legacy_tag_m.group(1) if legacy_tag_m
                                 else f"{spec_name}_upgrade_v1"),
        }

    goal = _goal(work, resolved, work["fields"])
    scope = (f"Work type(s): {', '.join(work['work_types'])}. "
             f"Modify {len(objects_to_modify)} object(s); create {len(objects_to_create)}.")

    context = {
        "spec_name": spec_name, "goal": goal, "scope": scope,
        "work_types": ", ".join(work["work_types"]),
        "human_bullets": human_bullets, "clarifications": clarifications or "None",
        "objects_table": planner_render.objects_table(resolved),
        "fields_table": planner_render.fields_table(work["fields"]),
        "impl_notes": _impl_notes(work, resolved),
        "upgrade_notes": _upgrade_notes(work),
        "testing_notes": _testing_notes(work),
        "references": "\n".join(f"- {u}" for u in references) or "- None",
    }
    tdd = planner_render.render_tdd(context)
    (specs_dir / "TDD.md").write_text(tdd, encoding="utf-8")

    spec_json = {
        "schema_version": "2.0",
        "spec_id": spec_name,
        "spec_name": spec_name,
        "spec_type": spec_type,
        "status": grounding_status,
        "version": 1,
        "idempotency_key": idempotency_key,
        "module": root.name,
        "source": {"references": references,
                   "description_sha256": hashlib.sha256(human_bullets.encode("utf-8")).hexdigest()},
        "summary": {"goal": goal, "in_scope": work["work_types"], "out_of_scope": []},
        "work_types": work["work_types"],
        "user_stories": [],
        "requirements": requirements,
        "data_model": data_model,
        "objects_to_create": objects_to_create,
        "objects_to_modify": objects_to_modify,
        "business_rules": [],
        "acceptance_tests": acceptance_tests,
        "evidence": [],
        "assumptions": [],
        "open_questions": open_questions,
        "event_subscribers": [],
        "scope_boundaries": {
            # Deterministic: derive allowed root(s) from the ACTUAL target paths — a
            # hardcoded root (repo folder name) can never coherently match
            # allowed_files roots and trips the scope validator.
            "allowed_extensions": sorted(
                {
                    p.replace("\\", "/").split("/")[0]
                    for p in allowed_files
                    if p and p.replace("\\", "/").split("/")[0]
                }
            ) or [root.name],
            "allowed_files": allowed_files,
            "forbidden_patterns": [],
            "scope_mode": "strict",
        },
        "bc_metadata": {"id_ranges": [], "mandatory_affixes": [], "analyzers": [], "api_versions": []},
        "traceability": {
            "requirement_to_test": {r["id"]: r["acceptance_tests"] for r in requirements},
            "requirement_to_object": {r["id"]: list(object_ids) for r in requirements},
            "field_to_object": {f["name"]: table_name for f in work["fields"]},
        },
        "references": references,
    }
    if upgrade_contract:
        spec_json["upgrade_contract"] = upgrade_contract
    if upgrade_contracts:
        spec_json["upgrade_contracts"] = upgrade_contracts
    contract_issues = validate_spec_contract(spec_json, strict_schema=True)
    if contract_issues:
        return {
            "status": "blocked_invalid_generated_spec",
            "reason": "Generated spec contract failed validation: " + "; ".join(contract_issues),
            "machine_spec_path": None,
            "tdd_path": str(specs_dir / "TDD.md"),
            "references": references,
        }
    from bc_agentic_mcp import provenance
    provenance.stamp(spec_json, context_sha)
    spec_path = specs_dir / "spec.json"
    spec_path.write_text(json.dumps(spec_json, indent=2))

    from bc_agentic_mcp.state import StateManager
    sm = StateManager(specs_root(root))
    sm.init()
    try:
        sm.get_spec(spec_name)
    except KeyError:
        sm.add_spec(spec_name, template or "tdd")

    return {
        "tdd_path": str(specs_dir / "TDD.md"),
        "machine_spec_path": str(spec_path),
        "status": grounding_status,
        "open_questions": open_questions,
        "summary": {"title": spec_name, "work_types": work["work_types"],
                    "status": grounding_status},
        "references": references,
    }
