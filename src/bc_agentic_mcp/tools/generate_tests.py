"""bc_generate_tests — scaffold AL test codeunits from the spec.
See spec Section 3.11.
"""
import json
from pathlib import Path
from typing import Dict, Any


async def handle_generate_tests(
    project_root: str,
    spec_name: str,
) -> Dict[str, Any]:
    """Generate an AL test codeunit scaffold (Subtype = Test) for the spec."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name
    spec = json.loads((specs_dir / "spec.json").read_text())

    rules = spec.get("business_rules", [])
    test_lines = [
        f"codeunit 50900 \"{spec_name} Tests\"",
        "{",
        "    Subtype = Test;",
        "",
    ]
    for i, rule in enumerate(rules, start=1):
        desc = rule.get("description", rule.get("id", f"Rule {i}"))
        test_lines.extend(
            [
                "    [Test]",
                f"    procedure Test_{i:03d}()",
                "    var",
                "        LibraryAssert: Codeunit \"Library Assert\";",
                "    begin",
                f"        // [SCENARIO] {desc}",
                "        // [GIVEN] (arrange)",
                "        // [WHEN] (act)",
                "        // [THEN] (assert)",
                "        LibraryAssert.IsTrue(true, 'TODO: implement');",
                "    end;",
                "",
            ]
        )
    if not rules:
        test_lines.extend(
            [
                "    [Test]",
                "    procedure Test_Placeholder()",
                "    begin",
                "        // [SCENARIO] No business rules defined yet.",
                "    end;",
                "",
            ]
        )
    test_lines.append("}")

    tests_dir = specs_dir / "generated"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_path = tests_dir / f"{spec_name}.Test.al"
    test_path.write_text("\n".join(test_lines), encoding="utf-8")

    return {
        "test_path": str(test_path),
        "test_count": max(len(rules), 1),
        "status": "scaffold_generated",
    }
