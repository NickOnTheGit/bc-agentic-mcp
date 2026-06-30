"""State file management for .specs/state.json. See spec Section 3.1."""
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone


@dataclass
class SpecEntry:
    name: str
    phase: str = "specify"
    created: str = ""
    outcome: Optional[str] = None
    task_progress: Dict[str, int] = field(
        default_factory=lambda: {"total": 0, "completed": 0, "failed": 0, "blocked": 0, "pending": 0}
    )
    approval_status: Dict[str, str] = field(
        default_factory=lambda: {
            "spec": "pending",
            "design": "pending",
            "tasks": "pending",
            "implement": "pending",
            "complete": "pending",
        }
    )
    last_activity: str = ""
    diagnostics: Dict[str, int] = field(default_factory=lambda: {"errors": 0, "warnings": 0})


@dataclass
class SpecState:
    active_spec: Optional[str] = None
    total_specs: int = 0
    specs: Dict[str, SpecEntry] = field(default_factory=dict)


class StateManager:
    """Manages .specs/state.json for tracking spec lifecycle."""

    def __init__(self, specs_dir: Path):
        self.specs_dir = Path(specs_dir)
        self.state_path = self.specs_dir / "state.json"

    def init(self) -> SpecState:
        """Create state file if it doesn't exist."""
        if not self.state_path.exists():
            self.specs_dir.mkdir(parents=True, exist_ok=True)
            state = SpecState()
            self._write(state)
            return state
        return self.load()

    def load(self) -> SpecState:
        """Load state from disk."""
        if not self.state_path.exists():
            raise FileNotFoundError(
                f"State file not found: {self.state_path}. Run bc_init first."
            )
        data = json.loads(self.state_path.read_text())
        state = SpecState(
            active_spec=data.get("active_spec"),
            total_specs=data.get("total_specs", 0),
        )
        for name, entry in data.get("specs", {}).items():
            state.specs[name] = SpecEntry(**entry)
        return state

    def _write(self, state: SpecState) -> None:
        """Write state to disk atomically (temp file + replace)."""
        data = {
            "active_spec": state.active_spec,
            "total_specs": state.total_specs,
            "specs": {name: asdict(entry) for name, entry in state.specs.items()},
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, default=str))
        tmp_path.replace(self.state_path)

    def add_spec(self, name: str, tdd_type: str) -> SpecEntry:
        """Register a new spec."""
        state = self.load()
        now = datetime.now(timezone.utc).isoformat()
        entry = SpecEntry(name=name, phase="specify", created=now, last_activity=now)
        state.specs[name] = entry
        state.total_specs = len(state.specs)
        self._write(state)
        return entry

    def get_spec(self, name: str) -> SpecEntry:
        """Get a spec by name."""
        state = self.load()
        if name not in state.specs:
            raise KeyError(f"Spec '{name}' not found")
        return state.specs[name]

    def update_phase(self, name: str, phase: str) -> None:
        """Update a spec's current phase."""
        state = self.load()
        if name not in state.specs:
            raise KeyError(f"Spec '{name}' not found")
        state.specs[name].phase = phase
        state.specs[name].last_activity = datetime.now(timezone.utc).isoformat()
        self._write(state)

    def set_active(self, name: str) -> None:
        """Set the active spec."""
        state = self.load()
        if name not in state.specs:
            raise KeyError(f"Spec '{name}' not found")
        state.active_spec = name
        self._write(state)

    def archive_spec(self, name: str, outcome: str) -> None:
        """Close out a spec."""
        state = self.load()
        if name not in state.specs:
            raise KeyError(f"Spec '{name}' not found")
        state.specs[name].phase = "closed"
        state.specs[name].outcome = outcome
        state.specs[name].last_activity = datetime.now(timezone.utc).isoformat()
        self._write(state)
