from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


_EXACT_PATH_KEYS = {
    "path",
    "file",
    "filepath",
    "file_path",
    "project_root",
    "artifact",
    "artifacts",
    "evidence",
    "evidence_path",
    "report_path",
    "spec_path",
    "manifest_path",
    "timeline_path",
    "output_file_path",
    "input_file_path",
}


def _looks_like_external_ref(value: str) -> bool:
    lower = value.lower().strip()
    return (
        lower.startswith("http://")
        or lower.startswith("https://")
        or lower.startswith("vstfs://")
        or lower.startswith("file://")
        or lower.startswith("untitled:")
    )


def _is_path_key(key: str) -> bool:
    k = key.lower()
    if k in _EXACT_PATH_KEYS:
        return True
    return k.endswith("_path") or k.endswith("_file") or k.endswith("_dir")


def _normalize_path_text(value: str, base_dir: Path) -> str:
    text = value.strip()
    if not text or _looks_like_external_ref(text):
        return value

    p = Path(text)
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    else:
        p = p.resolve()

    # Always emit Windows-compatible forward slashes for consistency.
    return p.as_posix()


def _normalize_node(node: Any, base_dir: Path, parent_key: str = "") -> Any:
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if isinstance(v, str) and _is_path_key(k):
                out[k] = _normalize_path_text(v, base_dir)
            else:
                out[k] = _normalize_node(v, base_dir, k)
        return out

    if isinstance(node, list):
        if _is_path_key(parent_key):
            normalized = []
            for item in node:
                if isinstance(item, str):
                    normalized.append(_normalize_path_text(item, base_dir))
                else:
                    normalized.append(_normalize_node(item, base_dir, parent_key))
            return normalized
        return [_normalize_node(item, base_dir, parent_key) for item in node]

    return node


def enforce_response_paths(result: Any, base_dir: Path) -> Any:
    """Return a copy of tool output where path-bearing fields are absolute.

    This is MCP-level enforcement and runs at the shared tool wrapper, so any
    tool returning dict/list payloads gets deterministic path normalization.
    """
    if not isinstance(result, (dict, list)):
        return result
    return _normalize_node(result, base_dir)
