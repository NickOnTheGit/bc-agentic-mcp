"""Tests for authorization + gate + detectors + review (Layers 1-3)."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from bc_agentic_mcp import authorization, gate, detectors, review, timeline
from bc_agentic_mcp import checkpoints as memory


def _approve(root: Path, spec: str = "s1", phase: str = "tasks") -> None:
    d = root / ".specs" / spec / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{phase}.md").write_text("**Status:** approve\n", encoding="utf-8")


def _green_engines(root: Path, spec: str = "s1") -> None:
    """Materialize the engine artifacts (timeline/refinement/traceability/code-context/quality) all green."""
    d = root / ".specs" / spec
    (d / "context" / "code").mkdir(parents=True, exist_ok=True)
    spec_json = {
        "requirements": [{"id": "REQ-001", "acceptance_tests": ["AT-001"]}],
        "acceptance_tests": [{"id": "AT-001", "requirement_ref": "REQ-001"}],
    }
    (d / "spec.json").write_text(json.dumps(spec_json), encoding="utf-8")
    (d / "context" / "code" / "code_context.json").write_text("{}", encoding="utf-8")
    sha = hashlib.sha256((d / "spec.json").read_bytes()).hexdigest()
    (d / "quality.json").write_text(json.dumps({"errors": 0, "spec_sha": sha}), encoding="utf-8")
    # refinement engine: ran, no problems found (claims verified against code reality)
    (d / "item_refinement.json").write_text(json.dumps({
        "findings": {"counts": {"mismatches": 0, "conflicts": 0}},
        "critique": "verified", "generated_at": "2099-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    timeline.record_phase(root, spec, "implemented")


def _charter(root: Path, spec: str = "s1", operations=None) -> None:
    memory.write_charter(
        root, spec, purpose="test", operations=operations or {"read": False, "update": False}
    )


# --- authorization ---------------------------------------------------------

def test_authorization_true_when_gating_phase_approved(tmp_path):
    _approve(tmp_path)
    assert authorization.implementation_authorized(tmp_path, "s1") is True


def test_authorization_false_without_approval(tmp_path):
    (tmp_path / ".specs" / "s1").mkdir(parents=True)
    assert authorization.implementation_authorized(tmp_path, "s1") is False


def test_authorization_false_when_rejected(tmp_path):
    d = tmp_path / ".specs" / "s1" / "approvals"
    d.mkdir(parents=True)
    (d / "tasks.md").write_text("**Status:** reject\n", encoding="utf-8")
    assert authorization.implementation_authorized(tmp_path, "s1") is False


def test_authorized_specs_lists_only_approved(tmp_path):
    _approve(tmp_path, "s1")
    (tmp_path / ".specs" / "s2").mkdir(parents=True)
    assert authorization.authorized_specs(tmp_path) == ["s1"]


# --- gate ------------------------------------------------------------------

def test_gate_allows_when_no_code_files(tmp_path):
    result = gate.check(str(tmp_path), ["README.md", "notes.txt"])
    assert result["allowed"] is True


def test_gate_blocks_unapproved_code_change(tmp_path):
    (tmp_path / ".specs" / "s1").mkdir(parents=True)
    result = gate.check(str(tmp_path), ["extensions/BaseApp/src/X.Table.al"], spec_name="s1")
    assert result["allowed"] is False
    assert result["blocked"] == ["extensions/BaseApp/src/X.Table.al"]


def test_gate_allows_approved_code_change(tmp_path):
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")
    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="s1")
    assert result["allowed"] is True


def test_gate_blocks_approved_but_engine_not_run(tmp_path):
    _approve(tmp_path, "s1")  # approved but engines never ran
    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="s1")
    assert result["allowed"] is False
    assert "Engine enforcement failed" in result["reason"]


def test_gate_any_spec_authorized_when_no_spec_given(tmp_path):
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")
    assert gate.check(str(tmp_path), ["src/X.Table.al"])["allowed"] is True


def test_gate_install_hook_rejects_non_git_dir(tmp_path):
    assert gate.install_hook(str(tmp_path))["installed"] is False


def test_gate_install_hook_writes_pre_push_too(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    out = gate.install_hook(str(tmp_path))
    assert out["installed"] is True
    assert (tmp_path / ".git" / "hooks" / "pre-commit").exists()
    assert (tmp_path / ".git" / "hooks" / "pre-push").exists()


# --- gate: one-feature model (feature-scope commits) -------------------------

def _feature_fixture(root: Path) -> None:
    """Feature 'feature-99001-demo' with approved scoped children s-a and s-b."""
    fd = root / ".specs" / "feature-99001-demo"
    (fd / "context").mkdir(parents=True, exist_ok=True)
    (fd / "feature_plan.json").write_text("{}", encoding="utf-8")
    (fd / "context" / "feature.json").write_text(json.dumps({
        "children": [
            {"id": "1", "title": "A", "state": "Active"},
            {"id": "2", "title": "B", "state": "Active"},
        ]
    }), encoding="utf-8")
    for spec, cid, fname in (("s-a", "1", "src/FileA.Table.al"), ("s-b", "2", "src/FileB.Page.al")):
        d = root / ".specs" / spec
        (d / "context").mkdir(parents=True, exist_ok=True)
        (d / "context" / "manifest.json").write_text(json.dumps({"item_id": cid}), encoding="utf-8")
        (d / "spec.json").write_text(json.dumps(
            {"scope_boundaries": {"allowed_files": [fname]}}), encoding="utf-8")
        _approve(root, spec)


def _git_on_branch(root: Path, branch: str) -> None:
    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", branch], cwd=str(root), check=True,
                   capture_output=True, text=True)


def test_gate_allows_cross_child_commit_inside_feature_union(tmp_path):
    """Integration fixes spanning two child items pass when files sit in the approved union."""
    _feature_fixture(tmp_path)
    result = gate.check(str(tmp_path), ["src/FileA.Table.al", "src/FileB.Page.al"])
    assert result["allowed"] is True, result["reason"]
    assert result["feature_spec"] == "feature-99001-demo"
    assert result["member_specs"] == ["s-a", "s-b"]


def test_gate_blocks_cross_child_commit_with_file_outside_union(tmp_path):
    _feature_fixture(tmp_path)
    result = gate.check(str(tmp_path), ["src/FileA.Table.al", "src/Rogue.Codeunit.al"])
    assert result["allowed"] is False
    assert "Scope hygiene violation" in result["reason"]


def test_gate_blocks_feature_union_commit_from_unrelated_branch(tmp_path):
    _feature_fixture(tmp_path)
    _git_on_branch(tmp_path, "user/unrelated-item")
    result = gate.check(str(tmp_path), ["src/FileA.Table.al", "src/FileB.Page.al"])
    assert result["allowed"] is False
    assert "Branch/feature mismatch" in result["reason"]


def test_gate_allows_feature_union_commit_from_feature_branch(tmp_path):
    _feature_fixture(tmp_path)
    _git_on_branch(tmp_path, "feature/99001-demo-work")
    result = gate.check(str(tmp_path), ["src/FileA.Table.al", "src/FileB.Page.al"])
    assert result["allowed"] is True, result["reason"]


def test_gate_allows_single_child_fix_from_feature_branch(tmp_path):
    """A one-item fix staged from the FEATURE branch (child-branch name mismatch) still passes."""
    _feature_fixture(tmp_path)
    _git_on_branch(tmp_path, "feature/99001/some-other-child")
    result = gate.check(str(tmp_path), ["src/FileA.Table.al"])
    assert result["allowed"] is True, result["reason"]
    assert result["member_specs"] == ["s-a"]


def test_pushed_files_extracts_changed_paths(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)

    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    f = src / "X.Table.al"
    f.write_text("table 50000 X {}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    f.write_text("table 50000 X { fields { } }", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    local_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(tmp_path), check=True, capture_output=True, text=True
    ).stdout.strip()
    remote_sha = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=str(tmp_path), check=True, capture_output=True, text=True
    ).stdout.strip()
    updates = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    files = gate._pushed_files(tmp_path, updates)
    assert "src/X.Table.al" in files


def test_gate_cli_returns_nonzero_when_blocked(tmp_path):
    (tmp_path / ".specs" / "s1").mkdir(parents=True)
    rc = gate.main(["--project-root", str(tmp_path), "--files", "src/X.Table.al"])
    assert rc == 1


def test_gate_cli_range_mode_uses_git_diff(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-b", "feature/s1"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")

    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    f = src / "X.Table.al"
    f.write_text("table 50000 X {}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    f.write_text("table 50000 X { fields {} }", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=str(tmp_path), check=True, capture_output=True, text=True
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(tmp_path), check=True, capture_output=True, text=True
    ).stdout.strip()

    rc = gate.main(["--project-root", str(tmp_path), "--from-ref", base, "--to-ref", head])
    assert rc == 0


def test_gate_blocks_on_protected_branch(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-b", "master"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")

    # On default protected branches (main/master), gate must block AL implementation commits.
    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="s1")
    assert result["allowed"] is False
    assert "protected" in result["reason"].lower()


def test_gate_blocks_out_of_scope_staged_code_files(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-b", "feature/s1"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")

    spec_path = tmp_path / ".specs" / "s1" / "spec.json"
    spec_json = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_json["scope_boundaries"] = {
        "allowed_files": ["src/Allowed.Table.al"],
        "allowed_extensions": [],
        "scope_mode": "strict",
        "forbidden_patterns": [],
    }
    spec_path.write_text(json.dumps(spec_json), encoding="utf-8")
    sha = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    (tmp_path / ".specs" / "s1" / "quality.json").write_text(json.dumps({"errors": 0, "spec_sha": sha}), encoding="utf-8")

    result = gate.check(str(tmp_path), ["src/Other.Table.al"], spec_name="s1")
    assert result["allowed"] is False
    assert "scope hygiene violation" in result["reason"].lower()
    assert result["blocked"] == ["src/Other.Table.al"]


def test_gate_blocks_when_branch_does_not_match_spec(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-b", "feature/other-task"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    _approve(tmp_path, "facility-code-filter")
    _green_engines(tmp_path, "facility-code-filter")

    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="facility-code-filter")
    assert result["allowed"] is False
    assert "branch/spec mismatch" in result["reason"].lower()


def test_gate_allows_when_branch_matches_spec(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-b", "feature/wi-123-facility-code-filter"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    _approve(tmp_path, "facility-code-filter")
    _green_engines(tmp_path, "facility-code-filter")

    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="facility-code-filter")
    assert result["allowed"] is True


def test_gate_blocks_when_no_approved_scoped_spec_matches(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-b", "feature/split"], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")
    _approve(tmp_path, "s2")
    _green_engines(tmp_path, "s2")

    s1 = tmp_path / ".specs" / "s1" / "spec.json"
    s1_json = json.loads(s1.read_text(encoding="utf-8"))
    s1_json["scope_boundaries"] = {
        "allowed_files": ["src/AllowedA.Table.al"],
        "allowed_extensions": [],
        "scope_mode": "strict",
        "forbidden_patterns": [],
    }
    s1.write_text(json.dumps(s1_json), encoding="utf-8")

    s2 = tmp_path / ".specs" / "s2" / "spec.json"
    s2_json = json.loads(s2.read_text(encoding="utf-8"))
    s2_json["scope_boundaries"] = {
        "allowed_files": ["src/AllowedB.Table.al"],
        "allowed_extensions": [],
        "scope_mode": "strict",
        "forbidden_patterns": [],
    }
    s2.write_text(json.dumps(s2_json), encoding="utf-8")

    result = gate.check(str(tmp_path), ["src/Other.Table.al"])
    assert result["allowed"] is False
    assert "do not match any approved spec scope" in result["reason"]


def test_gate_blocks_when_changes_match_multiple_approved_scopes(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "checkout", "-b", "feature/split"], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")
    _approve(tmp_path, "s2")
    _green_engines(tmp_path, "s2")

    for spec in ("s1", "s2"):
        sp = tmp_path / ".specs" / spec / "spec.json"
        data = json.loads(sp.read_text(encoding="utf-8"))
        data["scope_boundaries"] = {
            "allowed_files": ["src/Shared.Table.al"],
            "allowed_extensions": [],
            "scope_mode": "strict",
            "forbidden_patterns": [],
        }
        sp.write_text(json.dumps(data), encoding="utf-8")

    result = gate.check(str(tmp_path), ["src/Shared.Table.al"])
    assert result["allowed"] is False
    assert "match multiple approved spec scopes" in result["reason"]


def test_gate_blocks_when_clarification_unanswered(tmp_path):
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")
    clar = tmp_path / ".specs" / "s1" / "clarifications.md"
    clar.write_text(
        "# Clarifications for: s1\n\n"
        "## Q-001: Confirm target\n"
        "_Answer:_ \n",
        encoding="utf-8",
    )

    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="s1")
    assert result["allowed"] is False
    assert "clarification enforcement failed" in result["reason"]


def test_gate_blocks_when_clarification_has_no_al_evidence(tmp_path):
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")
    clar = tmp_path / ".specs" / "s1" / "clarifications.md"
    clar.write_text(
        "# Clarifications for: s1\n\n"
        "## Q-001: Confirm target\n"
        "_Answer:_ Use per-database because it seems right.\n",
        encoding="utf-8",
    )

    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="s1")
    assert result["allowed"] is False
    assert "lacks AL file evidence" in result["reason"]


def test_gate_allows_when_clarification_has_al_evidence(tmp_path):
    _approve(tmp_path, "s1")
    _green_engines(tmp_path, "s1")
    clar = tmp_path / ".specs" / "s1" / "clarifications.md"
    clar.write_text(
        "# Clarifications for: s1\n\n"
        "## Q-001: Confirm target\n"
        "_Answer:_ Modify extensions/BaseApp/src/Housing/X.Table.al and use per-database scope.\n",
        encoding="utf-8",
    )

    result = gate.check(str(tmp_path), ["src/X.Table.al"], spec_name="s1")
    assert result["allowed"] is True


# --- detectors -------------------------------------------------------------

_V0100 = [{
    "code": "V0100", "message": "per-company upgrade of shared table",
    "severity": "warning", "sourceLocation": {"file": "src/Upg.Codeunit.al", "line": 1},
}]


def test_detect_upgrade_scope_from_diagnostics(tmp_path):
    _charter(tmp_path)
    findings = detectors.detect(tmp_path, "s1", diagnostics=_V0100)
    assert any(f["detector"] == "upgrade_scope" for f in findings)


def test_scan_and_record_creates_and_dedups(tmp_path):
    _charter(tmp_path)
    first = detectors.scan_and_record(tmp_path, "s1", diagnostics=_V0100)
    assert first["recorded"] == 1
    second = detectors.scan_and_record(tmp_path, "s1", diagnostics=_V0100)
    assert second["recorded"] == 0  # de-duped within the same un-reflected window
    kinds = [c["kind"] for c in memory.load_checkpoints(tmp_path, "s1")]
    assert "mistake" in kinds


def test_detect_unpaired_bootstrap_mines_sibling_consensus(tmp_path):
    """ActivateFeature() without CreateFeatureIfNotFound() fires only when the
    sibling suites establish the pairing convention (pipeline build 257447:
    fresh container had no FeatureSAN row; long-lived local one masked it)."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    paired = ("codeunit 1 A {\n  procedure Init()\n  begin\n"
              "    Lib.CreateFeatureIfNotFound();\n    Lib.ActivateFeature();\n  end;\n}")
    (tests_dir / "SiblingA.Codeunit.al").write_text(paired, encoding="utf-8")
    (tests_dir / "SiblingB.Codeunit.al").write_text(paired.replace("codeunit 1 A", "codeunit 2 B"), encoding="utf-8")
    (tests_dir / "Offender.Codeunit.al").write_text(
        "codeunit 3 C {\n  procedure T()\n  begin\n    Lib.ActivateFeature();\n  end;\n}",
        encoding="utf-8")

    d = tmp_path / ".specs" / "s1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.json").write_text(json.dumps({
        "scope_boundaries": {"allowed_files": ["tests/Offender.Codeunit.al"]}}), encoding="utf-8")

    findings = detectors._detect_unpaired_bootstrap(tmp_path, "s1")
    assert len(findings) == 1
    assert findings[0]["code"] == "FRESH-ENV"
    assert "Offender" in findings[0]["summary"]

    # no consensus (single paired sibling) -> silent
    (tests_dir / "SiblingB.Codeunit.al").unlink()
    assert detectors._detect_unpaired_bootstrap(tmp_path, "s1") == []

    # offender pairs the calls -> silent
    (tests_dir / "SiblingB.Codeunit.al").write_text(paired.replace("codeunit 1 A", "codeunit 2 B"), encoding="utf-8")
    (tests_dir / "Offender.Codeunit.al").write_text(paired.replace("codeunit 1 A", "codeunit 3 C"), encoding="utf-8")
    assert detectors._detect_unpaired_bootstrap(tmp_path, "s1") == []


def test_scan_noop_without_charter(tmp_path):
    result = detectors.scan_and_record(tmp_path, "nospec", diagnostics=_V0100)
    assert result["recorded"] == 0


def test_detect_unapproved_implementation(tmp_path):
    _charter(tmp_path)
    specdir = tmp_path / ".specs" / "s1"
    (specdir / "TASKS.md").write_text("# Tasks\n- [x] T-001 done\n", encoding="utf-8")
    findings = detectors.detect(tmp_path, "s1", diagnostics=[])
    assert any(f["detector"] == "unapproved_implementation" for f in findings)


def test_detect_unapproved_cleared_by_approval(tmp_path):
    _charter(tmp_path)
    specdir = tmp_path / ".specs" / "s1"
    (specdir / "TASKS.md").write_text("# Tasks\n- [x] T-001 done\n", encoding="utf-8")
    _approve(tmp_path, "s1")
    findings = detectors.detect(tmp_path, "s1", diagnostics=[])
    assert not any(f["detector"] == "unapproved_implementation" for f in findings)


# --- review ----------------------------------------------------------------

def test_review_packet_has_checklist_and_charter(tmp_path):
    _charter(tmp_path, operations={"read": True, "update": True})
    packet = review.build_review_packet(tmp_path, "s1", changed_files=["a.al"])
    assert packet["checklist"]
    assert any(item["id"] == "upgrade_scope" for item in packet["checklist"])
    assert packet["charter"]["operations"] == {"read": True, "update": True}


def test_review_records_findings_as_checkpoints(tmp_path):
    _charter(tmp_path)
    out = review.handle_review(
        str(tmp_path), "s1",
        findings=[{"id": "upgrade_scope", "kind": "mistake", "severity": "error",
                   "summary": "per-company upgrade of a shared table"}],
    )
    assert out["findings_recorded"] == 1
    summaries = [c["summary"] for c in memory.load_checkpoints(tmp_path, "s1")]
    assert any("reviewer:upgrade_scope" in s for s in summaries)


def test_review_returns_packet_when_no_findings(tmp_path):
    _charter(tmp_path)
    out = review.handle_review(str(tmp_path), "s1")
    assert "checklist" in out
