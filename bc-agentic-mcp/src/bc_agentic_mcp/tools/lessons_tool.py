"""bc_lessons — summarize the auto-improver lessons learned across past specs."""
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, Optional

from bc_agentic_mcp import lessons as lessons_store


async def handle_lessons(project_root: str, write_file: bool = False) -> Dict[str, Any]:
    """Return the lessons summary; optionally write SUMMARY.md."""
    root = Path(project_root).resolve()
    result: Dict[str, Any] = {"summary": lessons_store.summarize_lessons(root)}
    try:
        from bc_agentic_mcp import team_lessons
        result["team"] = team_lessons.status()
    except Exception:
        pass
    if write_file:
        path = specs_root(root) / ".lessons" / "SUMMARY.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(lessons_store.render_lessons_summary(root), encoding="utf-8")
        result["summary_path"] = str(path)
    return result


async def handle_promote_lesson(
    project_root: str,
    lesson_id: Optional[str] = None,
    message: Optional[str] = None,
    match: Optional[Dict[str, str]] = None,
    severity: str = "warning",
    to_article: bool = False,
    domain: Optional[str] = None,
) -> Dict[str, Any]:
    """Promote a lesson to the cross-project store so it applies to every repo.

    Either promote an existing project lesson by ``lesson_id`` or record a new global
    lesson directly from ``message``/``match``. With ``to_article=True`` the lesson
    ALSO graduates into a repo-layer knowledge article (markdown with Best Practice /
    Anti Pattern bodies) that the index-aware review worklist can surface.
    """
    root = Path(project_root).resolve()
    result: Dict[str, Any]
    lesson: Optional[Dict[str, Any]] = None
    if lesson_id:
        promoted = lessons_store.promote_lesson(root, lesson_id)
        if promoted is None:
            return {"promoted": False, "reason": f"lesson {lesson_id} not found"}
        lesson = promoted
        result = {"promoted": True, "lesson": promoted}
    elif message:
        lesson = lessons_store.record_global_lesson(
            message=message, match=match or {}, severity=severity
        )
        result = {"promoted": True, "lesson": lesson}
    else:
        return {"promoted": False, "reason": "provide 'lesson_id' or 'message'"}
    if to_article and lesson:
        from bc_agentic_mcp import knowledge
        result["article"] = knowledge.graduate_lesson_to_article(
            root, lesson, domain=domain or "lessons"
        )
    return result

