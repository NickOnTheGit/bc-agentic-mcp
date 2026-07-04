"""End-to-end regression for the rental mutation description and Microsoft AL MCP path."""
import json
import tempfile
from pathlib import Path

import pytest

import bc_agentic_mcp.tools.analyze as analyze_module
from bc_agentic_mcp.al_mcp_client import ALMcpResult
from bc_agentic_mcp.tools.init import handle_init
from bc_agentic_mcp.tools.write_spec import handle_write_spec


RENTAL_MUTATION_DESCRIPTION = """- Add on-hold fields to Rental Mutation
- Expose the new fields on both rentalMutation API pages
- Keep the existing completion and reopen logic unchanged
- Avoid workspace-wide scanning for analysis; keep it bounded"""


def _write_minimal_al_project(root: Path) -> None:
    (root / "app.json").write_text(
        json.dumps(
            {
                "id": "test-app-id",
                "name": "EmpireHousing",
                "publisher": "Test",
                "version": "1.0.0.0",
                "idRanges": [{"from": 50000, "to": 50099}],
            }
        ),
        encoding="utf-8",
    )
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "RentalMutation.Table.al").write_text(
        'table 50000 "Rental Mutation" { fields { field(1; "Code"; Code[20]) { } } }',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_rental_mutation_description_uses_microsoft_backend(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_minimal_al_project(root)

        init_result = await handle_init(project_root=str(root), module_name="EmpireHousing")
        assert init_result["success"] is True

        spec_result = await handle_write_spec(
            project_root=str(root),
            spec_name="wi264484-rental-mutation",
            human_bullets=RENTAL_MUTATION_DESCRIPTION,
            idempotency_key="wi264484-rental-mutation-1",
        )
        tdd_path = root / ".specs" / "wi264484-rental-mutation" / "TDD.md"
        assert tdd_path.exists()
        assert "Add on-hold fields to Rental Mutation" in tdd_path.read_text(encoding="utf-8")

        async def fake_microsoft_backend(project_root: Path, depth: str = "basic") -> ALMcpResult:
            assert project_root.resolve() == root.resolve()
            assert depth == "basic"
            return ALMcpResult(
                summary={
                    "name": project_root.name,
                    "path": str(project_root),
                    "source": "microsoft-al-mcp",
                    "packages_loaded": True,
                    "depth": depth,
                },
                objects=[],
                dependencies={"package_load": ["loaded"]},
                diagnostics=[{"status": "ok"}],
            )

        monkeypatch.setattr(analyze_module, "analyze_project_with_microsoft", fake_microsoft_backend)

        analysis_result = await analyze_module.handle_analyze_module(
            module_path=root,
            spec_name="wi264484-rental-mutation",
            depth="basic",
        )

        assert analysis_result["analysis_backend"] == "microsoft-al-mcp"
        assert analysis_result["module_summary"]["source"] == "microsoft-al-mcp"
        assert analysis_result["module_summary"]["depth"] == "basic"
        assert analysis_result["dependencies"]["package_load"] == ["loaded"]
        assert analysis_result["diagnostics"] == [{"status": "ok"}]
        assert (root / ".specs" / "wi264484-rental-mutation" / "analysis.md").exists()