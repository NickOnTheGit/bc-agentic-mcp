"""bc_feedback — record human feedback for a spec. See spec Section 3.15."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any


async def handle_feedback(
    project_root: str,
    spec_name: str,
    feedback: str,
    rating: int = 0,
) -> Dict[str, Any]:
    """Append free-form feedback to the spec's feedback log."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name
    specs_dir.mkdir(parents=True, exist_ok=True)

    feedback_path = specs_dir / "FEEDBACK.md"
    entry = (
        f"\n## {datetime.now(timezone.utc).isoformat()}"
        f" (rating: {rating})\n\n{feedback}\n"
    )
    with open(feedback_path, "a", encoding="utf-8") as f:
        if feedback_path.stat().st_size == 0:
            f.write(f"# Feedback: {spec_name}\n")
        f.write(entry)

    return {
        "feedback_path": str(feedback_path),
        "status": "recorded",
    }
