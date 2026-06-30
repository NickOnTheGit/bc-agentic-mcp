"""Tests for state file management."""
import json
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.state import SpecState, StateManager


@pytest.fixture
def temp_specs_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestSpecState:
    def test_default_state(self):
        state = SpecState()
        assert state.active_spec is None
        assert state.total_specs == 0
        assert state.specs == {}


class TestStateManager:
    def test_init_creates_state_file(self, temp_specs_dir):
        sm = StateManager(temp_specs_dir)
        sm.init()
        state_path = temp_specs_dir / "state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["active_spec"] is None
        assert data["total_specs"] == 0
        assert data["specs"] == {}

    def test_init_idempotent(self, temp_specs_dir):
        sm = StateManager(temp_specs_dir)
        sm.init()
        sm2 = StateManager(temp_specs_dir)
        sm2.init()  # should not overwrite
        state = sm2.load()
        assert state.total_specs == 0

    def test_add_spec(self, temp_specs_dir):
        sm = StateManager(temp_specs_dir)
        sm.init()
        sm.add_spec("test-feature", "TDD")
        state = sm.load()
        assert state.total_specs == 1
        assert "test-feature" in state.specs
        assert state.specs["test-feature"].phase == "specify"

    def test_update_phase(self, temp_specs_dir):
        sm = StateManager(temp_specs_dir)
        sm.init()
        sm.add_spec("test-feature", "TDD")
        sm.update_phase("test-feature", "plan")
        state = sm.load()
        assert state.specs["test-feature"].phase == "plan"

    def test_set_active(self, temp_specs_dir):
        sm = StateManager(temp_specs_dir)
        sm.init()
        sm.add_spec("test-feature", "TDD")
        sm.set_active("test-feature")
        state = sm.load()
        assert state.active_spec == "test-feature"

    def test_get_nonexistent_raises(self, temp_specs_dir):
        sm = StateManager(temp_specs_dir)
        sm.init()
        with pytest.raises(KeyError, match="not found"):
            sm.get_spec("nonexistent")

    def test_archive_spec(self, temp_specs_dir):
        sm = StateManager(temp_specs_dir)
        sm.init()
        sm.add_spec("test-feature", "TDD")
        sm.archive_spec("test-feature", "merged")
        state = sm.load()
        assert state.specs["test-feature"].phase == "closed"
        assert state.specs["test-feature"].outcome == "merged"
