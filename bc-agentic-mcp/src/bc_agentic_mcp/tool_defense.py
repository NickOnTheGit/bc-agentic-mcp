"""Tool poisoning defenses. See CVE-2025-54136, CVE-2025-54135.

Provides hash-pinning for tool definitions to detect unauthorized changes
(poisoning attacks) between sessions.

Usage:
    from bc_agentic_mcp.tool_defense import verify_manifest, save_manifest
    results = verify_manifest(specs_dir / ".integrity", tool_defs)
    if any(v == "changed" for v in results.values()):
        # alert: tool definitions modified since last approval
"""
import hashlib
import json
from pathlib import Path
from typing import Dict, List


def compute_tool_hash(tool_def: Dict) -> str:
    """Hash a tool definition for integrity verification.

    Hashes: name + description + inputSchema.
    This is the deterministic fingerprint that must not change
    between sessions without human re-approval.

    Args:
        tool_def: Dict with keys 'name', 'description', 'inputSchema'.

    Returns:
        First 16 characters of the SHA-256 hex digest.
    """
    canonical = json.dumps(
        {
            "name": tool_def.get("name"),
            "description": tool_def.get("description"),
            "inputSchema": tool_def.get("inputSchema"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def save_manifest(manifest_dir: Path, tools: List[Dict]) -> Dict[str, str]:
    """Save the tool manifest hash map.

    Args:
        manifest_dir: Directory to write tool_manifest.json.
        tools: List of tool definition dicts.

    Returns:
        Dict mapping {tool_name: hash}.
    """
    manifest = {t["name"]: compute_tool_hash(t) for t in tools}
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "tool_manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    return manifest


def verify_manifest(manifest_dir: Path, tools: List[Dict]) -> Dict[str, str]:
    """Verify tool definitions haven't changed since last approval.

    Compares current hashes against the saved manifest.
    A "changed" result means hash mismatch — potential rug pull.

    Args:
        manifest_dir: Directory containing tool_manifest.json.
        tools: List of current tool definition dicts.

    Returns:
        Dict of {tool_name: "ok" | "changed" | "new"}.
        - "ok": hash matches stored manifest.
        - "changed": hash differs from stored (potential poisoning).
        - "new": tool not in stored manifest (first seen).
    """
    manifest_path = manifest_dir / "tool_manifest.json"
    if not manifest_path.exists():
        # First run: no manifest to compare against
        return {t["name"]: "new" for t in tools}

    stored = json.loads(manifest_path.read_text())
    results = {}
    for t in tools:
        name = t["name"]
        current_hash = compute_tool_hash(t)
        if name not in stored:
            results[name] = "new"
        elif stored[name] != current_hash:
            results[name] = "changed"
        else:
            results[name] = "ok"
    return results
