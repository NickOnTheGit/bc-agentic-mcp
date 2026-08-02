"""Tests for bc_implement engine."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp import security
from bc_agentic_mcp.tools.implement import (
    handle_implement,
    _load_implementer_prompt,
    _build_implementation_instructions,
    _update_tasks_md,
)


@pytest.fixture
def spec_project():
    """Minimal spec project with scope boundaries."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-spec"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "test-spec",
            "scope_boundaries": {"allowed_extensions": ["Test"]},
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))
        (specs_dir / "TASKS.md").write_text("# Tasks\n- [ ] T-001 Implement something\n")
        yield root


@pytest.fixture
def spec_project_with_al():
    """Spec project with scope that allows src/ extension."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-spec"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "test-spec",
            "scope_boundaries": {"allowed_extensions": ["src"]},
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))
        (specs_dir / "TASKS.md").write_text("# Tasks\n- [ ] T-001 Implement something\n")
        yield root


def test_load_implementer_prompt_returns_string():
    """_load_implementer_prompt should return the prompt content."""
    prompt = _load_implementer_prompt()
    assert isinstance(prompt, str)
    # If the file exists, it should contain content
    prompt_path = Path(__file__).resolve().parent.parent / "src" / "bc_agentic_mcp" / "prompts" / "implementer.md"
    if prompt_path.exists():
        assert len(prompt) > 0
        assert "Role" in prompt


def test_build_implementation_instructions():
    """_build_implementation_instructions should include scope boundaries."""
    # Create a minimal TASKS.md
    with tempfile.TemporaryDirectory() as d:
        tasks_path = Path(d) / "TASKS.md"
        tasks_path.write_text("# Tasks\n")
        spec = {
            "scope_boundaries": {
                "allowed_files": ["src/MyTable.Table.al"],
                "forbidden_patterns": ["DeleteAll"],
            }
        }
        instructions = _build_implementation_instructions(tasks_path, "TASK-001", spec)
        assert "TASK-001" in instructions
        assert "MyTable.Table.al" in instructions
        assert "DeleteAll" in instructions


@pytest.mark.asyncio
async def test_handle_implement_dry_run():
    """dry_run=True should skip file creation."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {"spec_name": "test-feature", "scope_boundaries": {"allowed_files": [], "forbidden_patterns": []}}
        (specs_dir / "spec.json").write_text(json.dumps(spec))
        tasks = "# Tasks\n- [ ] TASK-001 Implement something\n"
        (specs_dir / "TASKS.md").write_text(tasks)

        result = await handle_implement(
            project_root=str(root),
            spec_name="test-feature",
            task_ids=["TASK-001"],
            dry_run=True,
        )
        assert result["tasks_executed"] == 1
        assert result["results"][0]["status"] == "dry_run_skipped"


@pytest.mark.asyncio
async def test_handle_implement_ready_for_model_when_not_dry():
    """Non-dry-run should prepare context for model."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "test-feature",
            "scope_boundaries": {"allowed_files": [], "allowed_extensions": [], "forbidden_patterns": []},
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))
        tasks = "# Tasks\n- [ ] TASK-001 Implement something\n"
        (specs_dir / "TASKS.md").write_text(tasks)

        result = await handle_implement(
            project_root=str(root),
            spec_name="test-feature",
            task_ids=["TASK-001"],
            dry_run=False,
        )
        assert result["tasks_executed"] == 1
        assert result["results"][0]["status"] == "ready_for_model"
        assert "context" in result["results"][0]


@pytest.mark.asyncio
async def test_handle_implement_without_tasks_returns_no_results():
    """No task_ids should be blocked with explicit guidance."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {"spec_name": "test-feature", "scope_boundaries": {"allowed_files": [], "forbidden_patterns": []}}
        (specs_dir / "spec.json").write_text(json.dumps(spec))

        result = await handle_implement(
            project_root=str(root),
            spec_name="test-feature",
            task_ids=[],
        )
        assert result["status"] == "blocked_no_tasks_selected"
        assert result["tasks_executed"] == 0


def test_update_tasks_md_marks_complete():
    """_update_tasks_md should mark tasks as complete."""
    with tempfile.TemporaryDirectory() as d:
        specs_dir = Path(d)
        tasks_md = specs_dir / "TASKS.md"
        tasks_md.write_text("# Tasks\n- [ ] T-001 Implement\n- [ ] T-002 Test\n")
        _update_tasks_md(specs_dir, ["T-001"])
        content = tasks_md.read_text()
        assert "- [x] T-001" in content
        assert "- [ ] T-002" in content


def test_update_tasks_md_marks_failed():
    """_update_tasks_md should mark tasks as failed when failed=True."""
    with tempfile.TemporaryDirectory() as d:
        specs_dir = Path(d)
        tasks_md = specs_dir / "TASKS.md"
        tasks_md.write_text("# Tasks\n- [ ] T-001 Implement\n")
        _update_tasks_md(specs_dir, ["T-001"], failed=True)
        content = tasks_md.read_text()
        assert "- [x] T-001 (FAILED)" in content


class TestPhase2CodeExecution:
    """Tests for Phase 2 of bc_implement (code execution path)."""

    @staticmethod
    def _approve(root, spec="test-spec", phase="tasks"):
        d = Path(root) / ".specs" / spec / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        token = security.issue_approval(
            project_root=Path(root), spec_name=spec, phase=phase, status="approve",
            artifact_path="", artifact_sha256="", summary="test approval",
            idempotency_key=f"test-{spec}-{phase}",
        )
        (d / f"{phase}.md").write_text(
            f"**Status:** approve\n**Approval token:** {token}\n", encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_phase2_with_valid_code_writes_file(self, spec_project_with_al):
        """Providing code to bc_implement should write the file with no crash."""
        self._approve(spec_project_with_al)
        result = await handle_implement(
            project_root=str(spec_project_with_al),
            spec_name="test-spec",
            code='table 50000 "TestTable" { fields { field(1; "Name"; Text[50]) { Caption = \'Name\'; DataClassification = CustomerContent; } } }',
            file_path="src/Tables/TestTable.Table.al",
            attempt=1,
        )
        # Should attempt write — at minimum, not crash
        assert "status" in result
        # File should exist on disk
        target = spec_project_with_al / "src/Tables/TestTable.Table.al"
        assert target.exists()

    @pytest.mark.asyncio
    async def test_phase2_blocked_without_approval(self, spec_project_with_al):
        """Poka-yoke: without an approved decision, Phase 2 must refuse to write."""
        result = await handle_implement(
            project_root=str(spec_project_with_al),
            spec_name="test-spec",
            code='table 50000 "TestTable" { }',
            file_path="src/Tables/TestTable.Table.al",
            attempt=1,
        )
        assert result.get("status") == "blocked_needs_approval"
        assert not (spec_project_with_al / "src/Tables/TestTable.Table.al").exists()

    @pytest.mark.asyncio
    async def test_phase2_scope_violation_rejected(self, spec_project):
        """Code targeting a file outside scope must be rejected."""
        self._approve(spec_project)
        result = await handle_implement(
            project_root=str(spec_project),
            spec_name="test-spec",
            code="table 50000 X {}",
            file_path="src/Tables/ForeignTable.Table.al",
            attempt=1,
        )
        assert result.get("status") in ("scope_violation", "rejected")

    @pytest.mark.asyncio
    async def test_phase2_create_rejects_file_not_in_allowed_files(self):
        """Create writes must honor explicit allowed_files as a strict allowlist."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            specs_dir = root / ".specs" / "test-spec"
            specs_dir.mkdir(parents=True)
            spec = {
                "spec_name": "test-spec",
                "scope_boundaries": {
                    "allowed_extensions": ["src"],
                    "allowed_files": ["src/Tables/OnlyAllowed.Table.al"],
                },
            }
            (specs_dir / "spec.json").write_text(json.dumps(spec))
            (specs_dir / "TASKS.md").write_text("# Tasks\n- [ ] T-001 Implement something\n")
            self._approve(root)
            result = await handle_implement(
                project_root=str(root),
                spec_name="test-spec",
                code='table 50000 "OtherTable" { }',
                file_path="src/Tables/OtherTable.Table.al",
                attempt=1,
            )
            assert result.get("status") in ("scope_violation", "rejected")

    @pytest.mark.asyncio
    async def test_phase2_missing_file_path_rejected(self, spec_project):
        """Phase 2 requires file_path when code is provided."""
        result = await handle_implement(
            project_root=str(spec_project),
            spec_name="test-spec",
            code="table 50000 X {}",
            attempt=1,
        )
        assert result.get("status") in ("error", "missing_file_path")

    @pytest.mark.asyncio
    async def test_phase1_unchanged_without_code(self, spec_project):
        """Without code parameter, Phase 1 behavior must be preserved."""
        result = await handle_implement(
            project_root=str(spec_project),
            spec_name="test-spec",
            task_ids=["T-001"],
        )
        assert result["tasks_executed"] == 1
        assert result["results"][0]["status"] == "ready_for_model"

    @pytest.mark.asyncio
    async def test_phase2_upgrade_contract_blocks_missing_scope_or_tag(self):
        """Upgrade-target writes must satisfy required trigger scope and idempotency tag."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            specs_dir = root / ".specs" / "test-spec"
            specs_dir.mkdir(parents=True)
            spec = {
                "spec_name": "test-spec",
                "scope_boundaries": {
                    "allowed_extensions": ["src"],
                    "allowed_files": ["src/_Upgrade/MyUpgrade.Codeunit.al"],
                    "scope_mode": "strict",
                },
                "objects_to_create": [
                    {
                        "type": "Codeunit",
                        "name": "MyUpgrade",
                        "subtype": "upgrade",
                        "target": "src/_Upgrade/MyUpgrade.Codeunit.al",
                    }
                ],
                "upgrade_contract": {
                    "table_target": "src/Tables/My.Table.al",
                    "data_per_company": False,
                    "required_scope": "per-database",
                    "idempotency_tag": "my_upgrade_tag_v1",
                },
            }
            (specs_dir / "spec.json").write_text(json.dumps(spec))
            (specs_dir / "TASKS.md").write_text("# Tasks\n- [ ] T-001 Implement upgrade\n")
            self._approve(root)

            result = await handle_implement(
                project_root=str(root),
                spec_name="test-spec",
                code='codeunit 50100 "MyUpgrade" { trigger UpgradePerCompanySAN() begin end; }',
                file_path="src/_Upgrade/MyUpgrade.Codeunit.al",
                attempt=1,
            )
            assert result.get("status") == "blocked_upgrade_contract"
