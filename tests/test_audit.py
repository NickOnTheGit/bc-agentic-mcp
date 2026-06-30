"""Tests for audit logging."""
import json
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.audit import AuditLogger


@pytest.fixture
def temp_audit_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestAuditLogger:
    def test_log_writes_entry(self, temp_audit_dir):
        audit = AuditLogger(temp_audit_dir)
        audit.log("bc_init", "ses_001", success=True, spec_name=None, duration_ms=100)
        log_path = temp_audit_dir / ".audit" / "log.jsonl"
        assert log_path.exists()
        entries = [json.loads(line) for line in log_path.read_text().strip().split("\n")]
        assert len(entries) == 1
        assert entries[0]["tool"] == "bc_init"
        assert entries[0]["success"] is True

    def test_log_multiple_entries(self, temp_audit_dir):
        audit = AuditLogger(temp_audit_dir)
        audit.log("bc_init", "s1", True, None, 50)
        audit.log("bc_implement", "s1", False, "test-spec", 4500, files=["src/Test.al"])
        log_path = temp_audit_dir / ".audit" / "log.jsonl"
        entries = [json.loads(line) for line in log_path.read_text().strip().split("\n")]
        assert len(entries) == 2
        assert entries[1]["tool"] == "bc_implement"
        assert entries[1]["files_touched"] == ["src/Test.al"]

    def test_log_creates_directory(self, temp_audit_dir):
        audit = AuditLogger(temp_audit_dir)
        audit.log("bc_init", "s1", True, None, 100)
        assert (temp_audit_dir / ".audit").is_dir()
