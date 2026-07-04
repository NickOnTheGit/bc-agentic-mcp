"""bc_feedback — record human feedback for a spec. See spec Section 3.15."""
from datetime import datetime, timezone
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, Optional

from bc_agentic_mcp import lessons as lessons_store


async def handle_feedback(
    project_root: str,
    spec_name: str,
    feedback: str,
    rating: int = 0,
    lesson_message: Optional[str] = None,
    lesson_match: Optional[Dict[str, str]] = None,
    lesson_severity: str = "warning",
) -> Dict[str, Any]:
    """Append free-form feedback and optionally teach a durable lesson."""
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
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

    result: Dict[str, Any] = {
        "feedback_path": str(feedback_path),
        "status": "recorded",
    }

    if lesson_message:
        lesson = lessons_store.record_human_lesson(
            root,
            message=lesson_message,
            match=lesson_match,
            severity=lesson_severity,
        )
        result["lesson_id"] = lesson["id"]
        result["lessons_learned"] = True

    return result
