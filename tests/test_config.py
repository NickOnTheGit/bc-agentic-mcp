"""Tests for configuration and AL client discovery."""
import tempfile
from pathlib import Path
from bc_agentic_mcp.config import (
    discover_app_json,
    ALToolStatus,
    ServerConfig,
)


class TestDiscoverAppJson:
    def test_finds_in_project_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            app_json = root / "app.json"
            app_json.write_text('{"id": "test", "name": "Test"}')
            found = discover_app_json(root)
            assert found == app_json

    def test_finds_in_parent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            app_json = root / "app.json"
            app_json.write_text("{}")
            subdir = root / "src" / "Tables"
            subdir.mkdir(parents=True)
            found = discover_app_json(subdir)
            assert found == app_json

    def test_returns_none_when_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            found = discover_app_json(Path(d))
            assert found is None


class TestALToolStatus:
    def test_default_unavailable(self):
        status = ALToolStatus()
        assert status.available is False
        assert status.mode == "spec-only"

    def test_available_when_path_set(self):
        status = ALToolStatus(altool_path="/fake/altool")
        assert status.available is True
        assert status.mode == "full"


class TestServerConfig:
    def test_defaults(self):
        config = ServerConfig(project_root=Path("/tmp/test"))
        assert config.project_root == Path("/tmp/test")
        assert config.per_tool_rate == 30
        assert config.per_session_rate == 120
        assert config.max_compile_attempts == 3
        assert config.approval_timeout_minutes == 60
