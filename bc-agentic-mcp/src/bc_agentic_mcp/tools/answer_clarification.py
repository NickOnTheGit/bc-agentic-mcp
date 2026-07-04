"""bc_answer_clarification — MCP-fenced path for answering clarification questions.

Closes the file-edit escape hatch: before this tool existed, the only way to answer
clarifications was to edit clarifications.md directly with generic editor tools, which
bypassed the MCP enforcement loop entirely.

Contract:
  - Reads existing clarifications.md (must exist; call bc_clarify first if not).
  - Writes the provided answers into the matching _Answer:_ lines atomically.
  - Validates each answer is non-empty, not uncertain, and contains AL file evidence.
  - Returns enforcement status for the clarifications engine after writing.
  - On partial failure (some answers invalid) writes nothing and returns the issues.
"""
import re
from pathlib import Path
from typing import Dict, Any, List

from bc_agentic_mcp.workspace import specs_root

_QUESTION_RE = re.compile(r"^(##\s+)(Q-\d{3})(:.*)", re.MULTILINE)
_ANSWER_LINE_RE = re.compile(r"^(_Answer:_)\s*(.*)$", re.MULTILINE)
_UNSURE_RE = re.compile(r"\b(tbd|unknown|unsure|not sure|maybe|n/?a)\b", re.IGNORECASE)
_AL_EVIDENCE_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.al\b", re.IGNORECASE)


def _validate_answer(qid: str, text: str) -> List[str]:
    issues: List[str] = []
    stripped = text.strip()
    if not stripped:
        issues.append(f"{qid}: answer is empty")
        return issues
    if _UNSURE_RE.search(stripped):
        issues.append(f"{qid}: answer is uncertain (contains tbd/unknown/unsure/maybe/n/a)")
    if not _AL_EVIDENCE_RE.search(stripped):
        issues.append(f"{qid}: answer lacks AL file evidence (e.g. src/Tables/MyTable.al)")
    return issues


async def handle_answer_clarification(
    project_root: str,
    spec_name: str,
    answers: Dict[str, str],
) -> Dict[str, Any]:
    """Write answers into clarifications.md through the MCP-fenced path.

    Args:
        project_root: ERP AL repository root.
        spec_name: Name of the spec (folder under .specs/).
        answers: Dict mapping question ID (e.g. "Q-901") to answer text.
                 Each answer MUST reference at least one .al file path as evidence.

    Returns:
        {ok, written, issues, enforcement_status, next_action}
    """
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
    clar_path = specs_dir / "clarifications.md"

    if not clar_path.exists():
        return {
            "ok": False,
            "error": "clarifications.md not found — call bc_clarify first to generate questions",
            "next_action": {
                "tool": "bc_clarify",
                "reason": "Generate clarification questions before answering them",
                "params_hint": {"spec_name": spec_name, "project_root": project_root, "context": "<requirement text>"},
            },
        }

    if not answers:
        return {
            "ok": False,
            "error": "answers dict is empty — provide at least one {question_id: answer} entry",
        }

    # Validate all answers before writing anything (fail-fast, atomic)
    validation_issues: List[str] = []
    for qid, text in answers.items():
        validation_issues.extend(_validate_answer(qid, text))

    if validation_issues:
        return {
            "ok": False,
            "error": "Answer validation failed — nothing written",
            "issues": validation_issues,
            "hint": (
                "Each answer must: (1) be non-empty, (2) not contain tbd/unknown/unsure/maybe/n/a, "
                "(3) reference at least one .al file path as evidence (e.g. src/Tables/MyTable.al). "
                "Fix the issues above, then call bc_answer_clarification again."
            ),
        }

    # Write answers into the file
    content = clar_path.read_text(encoding="utf-8")
    updated = content

    written: List[str] = []
    not_found: List[str] = []

    for qid, answer_text in answers.items():
        # Find the block for this question id and replace its _Answer:_ line
        # Pattern: the ## Q-NNN heading, then lines until next ## or EOF, capture _Answer:_ line.
        # The answer tail must stay ON THE SAME LINE: with an EMPTY current answer, a
        # newline-crossing \s* let [^\n]* swallow the NEXT question's heading line —
        # deleting that question and orphaning its answer (observed live on Bug 267600:
        # answering Q-902 ate the '## Q-001:' heading, ping-ponging the plan gate forever).
        block_pattern = re.compile(
            rf"(##\s+{re.escape(qid)}:.*?\n(?:(?!##\s+Q-)[\s\S])*?)(_Answer:_)[ \t]*[^\n]*",
            re.MULTILINE,
        )
        match = block_pattern.search(updated)
        if match:
            old_str = match.group(0)
            new_str = match.group(1) + "_Answer:_ " + answer_text.strip()
            updated = updated.replace(old_str, new_str, 1)
            written.append(qid)
        else:
            not_found.append(qid)

    if not written:
        return {
            "ok": False,
            "error": f"No matching question IDs found in clarifications.md. Provided: {list(answers.keys())}",
            "not_found": not_found,
        }

    clar_path.write_text(updated, encoding="utf-8")

    # Re-run clarifications enforcement to report final status
    enforcement_status: Dict[str, Any] = {}
    try:
        from bc_agentic_mcp import enforcement
        enforcement_status = enforcement._clarifications_status(specs_dir)
    except Exception:
        pass

    result: Dict[str, Any] = {
        "ok": enforcement_status.get("ok", True),
        "written": written,
        "file_path": str(clar_path),
    }
    if not_found:
        result["not_found"] = not_found
        result["warning"] = f"Some question IDs were not found in the file: {not_found}"
    if enforcement_status:
        result["enforcement_status"] = enforcement_status

    if enforcement_status.get("ok"):
        result["next_action"] = {
            "tool": "bc_quality_check",
            "reason": "Clarifications are resolved — run quality check to clear the remaining enforcement blocker",
            "params_hint": {"spec_name": spec_name, "project_root": project_root},
        }
    else:
        remaining = enforcement_status.get("reason", "")
        result["next_action"] = {
            "tool": "bc_answer_clarification",
            "reason": f"Some clarifications still unresolved: {remaining}",
            "params_hint": {"spec_name": spec_name, "project_root": project_root, "answers": {}},
        }

    return result
