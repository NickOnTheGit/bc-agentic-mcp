"""Shared spec loading with existence guard."""
import json
from pathlib import Path
from typing import Dict, Any
from bc_agentic_mcp.errors import MCPError, ErrorCode


def load_spec(specs_dir: Path) -> Dict[str, Any]:
    """Load spec.json, raising MCPError with guidance if missing."""
    spec_path = specs_dir / "spec.json"
    if not spec_path.exists():
        raise MCPError(
            ErrorCode.CLIENT_ERROR,
            f"spec.json not found at {spec_path}",
            hint="Run bc_write_spec first to create the specification.",
        )
    return json.loads(spec_path.read_text())
