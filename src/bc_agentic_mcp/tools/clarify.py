"""bc_clarify — structured clarification questions. See spec Section 3.3."""
import re
from pathlib import Path
from typing import Dict, Any, List, Optional


async def handle_clarify(
    project_root: str,
    spec_name: str,
    context: str,
    analysis: Optional[str] = None,
    specific_concern: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate structured clarification questions from human bullets."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name
    specs_dir.mkdir(parents=True, exist_ok=True)

    if not analysis:
        analysis_path = specs_dir / "analysis.md"
        if analysis_path.exists():
            analysis = analysis_path.read_text(encoding="utf-8")

    questions = _detect_ambiguities(context, analysis, specific_concern)

    clar_path = specs_dir / "clarifications.md"
    lines = [
        f"# Clarifications for: {spec_name}",
        "",
        "Edit this file to provide answers, then call bc_write_spec.",
        "",
    ]
    for q in questions:
        lines.append(f"## {q['id']}: {q['question']}")
        if q.get("options"):
            for opt in q["options"]:
                lines.append(f"- [ ] {opt}")
        lines.append("_Answer:_ ")
        lines.append("")

    clar_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "file_path": str(clar_path),
        "questions": questions,
        "instructions": "Edit this file to provide answers, then call bc_write_spec.",
    }


def _detect_ambiguities(
    context: str,
    analysis: Optional[str] = None,
    specific_concern: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Heuristic detection of common ambiguities in requirement text."""
    questions: List[Dict[str, Any]] = []

    if specific_concern:
        questions.append(
            {
                "id": "Q-001",
                "question": specific_concern,
                "type": "text",
            }
        )
        return questions

    checks = [
        (r"\bnotify\b", "How should the user be notified? A) Page message B) Email C) Job Queue log"),
        (r"\bvalidate\b", "What validation rules apply? List specific conditions."),
        (r"\bbackground\b", "Should this run in background (Job Queue)? Yes/No"),
        (r"\bdate\b", "Are there any date constraints (past, future, range)?"),
        (r"\buser\b.*\bselect", "Which page should the user select from?"),
        (r"\ballow\b.*\bdelete", "Should deletion be: A) Always allowed B) Conditional C) Never"),
    ]

    for pattern, question in checks:
        if re.search(pattern, context, re.IGNORECASE):
            qid = f"Q-{len(questions) + 1:03d}"
            questions.append(
                {
                    "id": qid,
                    "question": question,
                    "type": "choice" if ")" in question else "text",
                }
            )

    if not questions:
        questions.append(
            {
                "id": "Q-001",
                "question": "Are there any constraints or edge cases not mentioned in the bullets above?",
                "type": "text",
            }
        )

    return questions
