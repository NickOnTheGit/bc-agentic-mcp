"""Load bundled reference material.

Usage:
    from bc_agentic_mcp.references import load_reference

    guidelines = load_reference("al_guidelines")
"""
from pathlib import Path

_REF_DIR = Path(__file__).parent


def load_reference(name: str) -> str:
    """Load a reference file by name (without .md extension).

    Args:
        name: The stem of the reference file (e.g. "al_guidelines").

    Returns:
        The full text content of the reference file.

    Raises:
        FileNotFoundError: If no matching .md file exists.
    """
    path = _REF_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Reference '{name}' not found. Available: {_list_available()}"
        )
    return path.read_text(encoding="utf-8")


def _list_available() -> list:
    """List all available reference files (stems, excluding __init__)."""
    return [p.stem for p in _REF_DIR.glob("*.md") if p.stem != "__init__"]
