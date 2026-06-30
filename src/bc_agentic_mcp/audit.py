"""Audit logging. See spec Section 4.5."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


class AuditLogger:
    """Writes structured audit logs to .specs/.audit/log.jsonl."""

    def __init__(self, specs_dir: Path):
        self.audit_dir = Path(specs_dir) / ".audit"
        self.log_path = self.audit_dir / "log.jsonl"

    def log(
        self,
        tool: str,
        session_id: str,
        success: bool,
        spec_name: Optional[str] = None,
        duration_ms: int = 0,
        task_id: Optional[str] = None,
        files: Optional[List[str]] = None,
    ) -> None:
        """Append an audit entry."""
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "session_id": session_id,
            "success": success,
            "duration_ms": duration_ms,
        }
        if spec_name:
            entry["spec_name"] = spec_name
        if task_id:
            entry["task_id"] = task_id
        if files:
            entry["files_touched"] = files

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
