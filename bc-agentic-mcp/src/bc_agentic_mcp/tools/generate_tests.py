"""bc_generate_tests — scaffold layered AL test codeunits from the spec.

Emits happy-path, negative, boundary, business-logic and API-contract test
skeletons (see ``testing_playbook``) instead of a single TODO stub, plus a
human-readable PLAN.md. See spec Section 3.11.
"""
import json
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, List, Optional

from bc_agentic_mcp.spec_loader import load_spec
from bc_agentic_mcp import test_app
from bc_agentic_mcp import testing_playbook as playbook


def _schema_reality_gate(specs_dir: Path, spec: Dict[str, Any]) -> Dict[str, Any]:
    """D1: tests must target deployed reality, not fiction.

    When the spec declares API-facing data_model fields, a fresh reconcile report
    (written by ``bc_reconcile_target``) must exist and every requested field must
    either exist in the deployed schema or be explicitly declared as new
    (``to_be_created: true`` on the field). Observed failure this prevents: item
    tests generated against a field name that does not exist in the container.
    """
    data_model = spec.get("data_model", []) or []
    is_api = any(
        "api" in str((obj.get("type") if isinstance(obj, dict) else obj)).lower()
        for obj in spec.get("objects", []) or []
    )
    if not data_model or not is_api:
        return {"ok": True, "reason": "no API-facing data_model fields — gate not applicable"}

    declared_new = {
        str(f.get("name", "")).strip().lower()
        for f in data_model
        if isinstance(f, dict) and f.get("to_be_created")
    }
    needing_check = [
        str(f.get("name", "")).strip()
        for f in data_model
        if isinstance(f, dict) and str(f.get("name", "")).strip().lower() not in declared_new
    ]
    if not needing_check:
        return {"ok": True, "reason": "all fields declared to_be_created — nothing to reconcile"}

    report_path = specs_dir / "reconcile_report.json"
    if not report_path.exists():
        return {
            "ok": False,
            "reason": (
                "No reconcile report found. Run bc_reconcile_target (with spec_name) so the "
                "spec's fields are checked against the deployed schema before tests are "
                "generated — or mark genuinely new fields with to_be_created: true."
            ),
            "fields_needing_check": needing_check,
            "next_action": {
                "tool": "bc_reconcile_target",
                "params_hint": {"requested": needing_check, "spec_name": specs_dir.name},
            },
        }
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": f"reconcile report unreadable: {exc}"}
    deployed_missing = [
        f for f in needing_check
        if f.lower() in {str(n).lower() for n in report.get("new", [])}
    ]
    if deployed_missing:
        return {
            "ok": False,
            "reason": (
                "These spec fields do NOT exist in the deployed schema and are not declared "
                "to_be_created — tests generated against them would be fiction: "
                + ", ".join(deployed_missing)
            ),
            "missing_fields": deployed_missing,
        }
    return {"ok": True, "reason": "all fields reconciled against deployed schema"}


def _proc(name: str, scenario: str, hint: str, body: str) -> List[str]:
    lines = [
        "    [Test]",
        f"    procedure {name}()",
        "    var",
        "        LibraryAssert: Codeunit \"Library Assert\";",
        "    begin",
        f"        // [SCENARIO] {scenario}",
    ]
    if hint:
        lines.append(f"        // [HINT] {hint}")
    lines.extend(["        " + body, "    end;", ""])
    return lines


def _sanitize(text: str, fallback: str) -> str:
    keep = [c if c.isalnum() else " " for c in (text or "")]
    words = "".join(keep).split()
    ident = "".join(w[:1].upper() + w[1:] for w in words)[:60]
    return ident or fallback


async def handle_generate_tests(
    project_root: str,
    spec_name: str,
    test_app_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a layered AL test codeunit scaffold (Subtype = Test) for the spec.

    D2: the REAL test extension is resolved (app.json with test-framework deps) and a
    free object ID is allocated from its own idRanges — the scaffold compiles in the
    app it will actually run in. The scaffold copy under .specs/ stays as evidence;
    landing it in the app goes through bc_implement_write (the sanctioned write path).
    """
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
    spec = load_spec(specs_dir)

    # D1: schema-reality gate — refuse to scaffold tests against fields that do not
    # exist in the deployed schema (unless declared new). Fiction tests waste a full
    # compile/publish cycle before failing with AL0118/AL0132.
    gate = _schema_reality_gate(specs_dir, spec)
    if not gate["ok"]:
        return {
            "status": "blocked_schema_reality",
            "blocked": True,
            "reason": gate["reason"],
            **{k: v for k, v in gate.items() if k not in ("ok", "reason")},
        }

    plan = playbook.build_test_plan(spec)
    rules = spec.get("business_rules", []) or []

    # D2: resolve the real test app + allocate a real object ID from ITS idRanges.
    target = test_app.resolve_target(root, hint=test_app_hint)
    object_id = target.get("object_id") if target["status"] == "resolved" else None
    scaffold_id = object_id or 50900  # fallback keeps the scaffold compilable standalone

    test_lines = [
        f"codeunit {scaffold_id} \"{spec_name} Tests\"",
        "{",
        "    Subtype = Test;",
        "",
    ]

    categories: Dict[str, int] = {}
    total = 0

    # Every scaffold FAILS until implemented — a vacuous `IsTrue(true)` (or an
    # `asserterror Fail`) would pass trivially and be mistaken for real coverage.
    # Happy path — one per business rule, keeping the historical Test_NNN names.
    if rules:
        test_lines.append("    // --- happy-path ---")
        for i, rule in enumerate(rules, start=1):
            desc = rule.get("description", rule.get("id", f"Rule {i}"))
            test_lines.extend(_proc(
                f"Test_{i:03d}", desc,
                "Arrange valid input, act, assert the expected result persists.",
                "LibraryAssert.Fail('TODO: implement happy-path assertions');",
            ))
            total += 1
        categories["happy-path"] = len(rules)
    else:
        test_lines.append("    // --- happy-path ---")
        test_lines.extend(_proc(
            "Test_Placeholder", "No business rules defined yet.",
            "", "LibraryAssert.Fail('TODO: define business rules, then implement tests');",
        ))
        total += 1
        categories["happy-path"] = 1

    # Negative / boundary / business-logic / api-contract — each fails until implemented.
    layer_bodies = {
        "negative": "LibraryAssert.Fail('TODO: assert the invalid action is rejected — asserterror <action>');",
        "boundary": "LibraryAssert.Fail('TODO: assert the edge value is handled');",
        "business-logic": "LibraryAssert.Fail('TODO: assert the derived behaviour');",
        "api-contract": "LibraryAssert.Fail('TODO: exercise via API (HTTP) test');",
    }
    prefix = {"negative": "Neg", "boundary": "Bnd", "business-logic": "Biz", "api-contract": "Api"}
    for layer in ("negative", "boundary", "business-logic", "api-contract"):
        cases = plan.get(layer, [])
        if not cases:
            continue
        test_lines.append(f"    // --- {layer} ---")
        for j, case in enumerate(cases, start=1):
            ident = f"{prefix[layer]}_{j:03d}_{_sanitize(case.get('scenario', ''), 'Case')}"
            test_lines.extend(_proc(
                ident, case.get("scenario", ""), case.get("hint", ""), layer_bodies[layer],
            ))
            total += 1
        categories[layer] = len(cases)

    test_lines.append("}")

    tests_dir = specs_dir / "generated"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_path = tests_dir / f"{spec_name}.Test.al"
    test_path.write_text("\n".join(test_lines), encoding="utf-8")

    plan_path = tests_dir / "PLAN.md"
    plan_md = playbook.render_plan_md(spec_name, plan)
    plan_md += (
        "\n## Validation Slices\n"
        "\n"
        "### Item Slice (mandatory)\n"
        "- Implement tests that directly prove the item's acceptance criteria.\n"
        "- Record these as item-scoped empiric evidence in the local container.\n"
        "- This slice is the primary sign-off proof.\n"
        "\n"
        "### Regression Slice (mandatory)\n"
        "- Add a targeted regression subset for nearby consumers or adjacent flows impacted by the item.\n"
        "- Record these runs separately as regression evidence (`validation_mode=regression`).\n"
        "- Do not substitute unrelated broad-suite output for this slice.\n"
        "\n"
        "### API Slice (conditional)\n"
        "- Only required when the spec touches an API surface.\n"
        "- Cover contract behavior: method restrictions, concurrency, malformed payloads, and unchanged-state assertions.\n"
    )
    plan_path.write_text(plan_md, encoding="utf-8")

    suggested_minimums = {
        "item": sum(1 for k in ("happy-path", "negative", "boundary") if categories.get(k, 0) > 0),
        "regression": 1 if categories.get("business-logic", 0) > 0 else 0,
        "api": 1 if categories.get("api-contract", 0) > 0 else 0,
    }

    result: Dict[str, Any] = {
        "test_path": str(test_path),
        "plan_path": str(plan_path),
        # Backward-compatible: happy-path count.
        "test_count": max(len(rules), 1),
        "total_procedures": total,
        "categories": categories,
        "validation_slices": ["item", "regression", "api-if-applicable"],
        "suggested_minimums": suggested_minimums,
        "status": "scaffold_generated",
        "target": target,
    }
    if target["status"] == "resolved" and object_id:
        rel_target = (
            Path(target["app_folder"]) / "src" / f"{spec_name.title().replace('-', '')}.Test.al"
        )
        try:
            suggested = str(rel_target.relative_to(root))
        except ValueError:
            suggested = str(rel_target)
        result["object_id"] = object_id
        result["suggested_file_path"] = suggested
        result["next_action"] = {
            "tool": "bc_implement_write",
            "reason": (
                f"Land the scaffold in the REAL test app '{target['app_name']}' "
                f"(object id {object_id} allocated from its idRanges) via the sanctioned write path."
            ),
            "params_hint": {"spec_name": spec_name, "file_path": suggested},
        }
    elif target["status"] == "ambiguous":
        result["next_action"] = {
            "tool": "bc_generate_tests",
            "reason": "Multiple test apps found — re-run with test_app_hint naming one.",
            "params_hint": {
                "spec_name": spec_name,
                "test_app_hint": "<one of: "
                + ", ".join(c["app_name"] or c["app_folder"] for c in target["candidates"][:4])
                + ">",
            },
        }
    return result
