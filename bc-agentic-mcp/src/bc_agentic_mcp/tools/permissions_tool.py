"""bc_check_permission_coverage — verify a table grant already covers new API fields.

BC permissions are table-level, so adding fields to an API page needs no permission change
when a set already grants the required access on that table. This tool answers that
deterministically from the actual permission-set files, so the agent never blindly edits a
permission set nor wrongly assumes one is needed.
"""
from typing import Any, Dict, Optional

from bc_agentic_mcp import permissions


async def handle_check_permission_coverage(
    project_root: str,
    table: str,
    required_access: str = "R",
    source_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Report which permission sets grant ``required_access`` (e.g. 'R', 'M', 'RM') on ``table``.

    ``source_root`` defaults to ``project_root``. If nothing covers it, a permission change is
    genuinely required; otherwise adding fields is already permitted.
    """
    root = source_root or project_root
    return permissions.find_coverage(root, table, required_access)
