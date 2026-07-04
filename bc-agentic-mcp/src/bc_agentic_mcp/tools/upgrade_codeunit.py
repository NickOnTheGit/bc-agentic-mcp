"""bc_upgrade_codeunit — scaffold an AL upgrade codeunit from the spec.
See spec Section 3.12.
"""
import json
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any

from bc_agentic_mcp.spec_loader import load_spec


async def handle_upgrade_codeunit(
    project_root: str,
    spec_name: str,
) -> Dict[str, Any]:
    """Generate an upgrade codeunit scaffold (Subtype = Upgrade)."""
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
    spec = load_spec(specs_dir)

    module = spec.get("module", root.name)
    lines = [
        f"codeunit 50901 \"{spec_name} Upgrade\"",
        "{",
        "    Subtype = Upgrade;",
        "",
        "    trigger OnUpgradePerCompany()",
        "    var",
        "        UpgradeTag: Codeunit \"Upgrade Tag\";",
        "    begin",
        f"        if UpgradeTag.HasUpgradeTag(GetTag()) then",
        "            exit;",
        "        // TODO: DataTransfer / InitValue migration for new fields.",
        "        UpgradeTag.SetUpgradeTag(GetTag());",
        "    end;",
        "",
        "    local procedure GetTag(): Code[250]",
        "    begin",
        f"        exit('{module}-{spec_name}-upgrade-1');",
        "    end;",
        "}",
    ]

    out_dir = specs_dir / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    upgrade_path = out_dir / f"{spec_name}.Upgrade.al"
    upgrade_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "upgrade_path": str(upgrade_path),
        "status": "scaffold_generated",
    }
