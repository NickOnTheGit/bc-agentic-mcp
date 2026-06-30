"""Tests for tool poisoning defenses."""
import json
from pathlib import Path

import pytest

from bc_agentic_mcp.tool_defense import (
    compute_tool_hash,
    save_manifest,
    verify_manifest,
)


def test_same_tool_same_hash():
    """Identical tool definitions must produce the same hash."""
    t1 = {"name": "bc_init", "description": "Initialize", "inputSchema": {"type": "object"}}
    t2 = {"name": "bc_init", "description": "Initialize", "inputSchema": {"type": "object"}}
    assert compute_tool_hash(t1) == compute_tool_hash(t2)


def test_different_description_different_hash():
    """Different descriptions must produce different hashes."""
    t1 = {"name": "bc_init", "description": "A", "inputSchema": {"type": "object"}}
    t2 = {"name": "bc_init", "description": "B", "inputSchema": {"type": "object"}}
    assert compute_tool_hash(t1) != compute_tool_hash(t2)


def test_changed_tool_detected(tmp_path):
    """verify_manifest must detect changed tool definitions."""
    manifest_dir = tmp_path / ".integrity"

    tools = [{"name": "bc_test", "description": "Original", "inputSchema": {"type": "object"}}]

    # Save manifest with original definition
    save_manifest(manifest_dir, tools)
    results = verify_manifest(manifest_dir, tools)
    assert results == {"bc_test": "ok"}

    # Modify the tool description (simulating poisoning)
    poisoned = [{"name": "bc_test", "description": "MODIFIED", "inputSchema": {"type": "object"}}]
    results = verify_manifest(manifest_dir, poisoned)
    assert results["bc_test"] == "changed"


def test_new_tool_detected(tmp_path):
    """verify_manifest must flag tools not in stored manifest."""
    manifest_dir = tmp_path / ".integrity"

    # No manifest exists yet
    tools = [{"name": "bc_new", "description": "New tool", "inputSchema": {"type": "object"}}]
    results = verify_manifest(manifest_dir, tools)
    assert results == {"bc_new": "new"}

    # After saving, it should be "ok"
    save_manifest(manifest_dir, tools)
    results = verify_manifest(manifest_dir, tools)
    assert results == {"bc_new": "ok"}
