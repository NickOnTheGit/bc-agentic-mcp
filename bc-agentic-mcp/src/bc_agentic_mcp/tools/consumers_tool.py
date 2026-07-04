"""bc_find_consumers — locate who CONSUMES an AL symbol (business-logic discovery)."""
from typing import Any, Dict, Optional

from bc_agentic_mcp import consumers


async def handle_find_consumers(
    project_root: str,
    symbol: str,
    source_root: Optional[str] = None,
    exclude_definition: bool = True,
    max_files: Optional[int] = None,
) -> Dict[str, Any]:
    """Scan the AL tree under ``source_root`` (default ``project_root``) for consumers of
    ``symbol`` and classify the enclosing objects, so business-logic tests target real
    derived behaviour rather than field I/O.
    """
    root = source_root or project_root
    return consumers.find_consumers(
        root, symbol, exclude_definition=exclude_definition, max_files=max_files
    )
