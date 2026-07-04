"""bc_extract_references — surface the wiki/related-item references a work item points to.

The item's linked wiki and related items are authoritative for WHICH object/API to change;
they must be consulted before target selection. This tool makes them explicit so they cannot
be silently skipped (the failure mode of inferring the target from codebase convention alone).
"""
from typing import Any, Dict

from bc_agentic_mcp import item_references


async def handle_extract_references(text: str) -> Dict[str, Any]:
    """Extract wiki pages, related work items and links from work-item text."""
    refs = item_references.extract_references(text)
    refs["checklist"] = item_references.render_reference_checklist(refs)
    return refs
