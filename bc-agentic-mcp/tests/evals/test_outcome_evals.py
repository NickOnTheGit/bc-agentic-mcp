"""Outcome evals — the system's own regression suite for prompts and pipeline verdicts.

Two guarantees no unit test gives:

1. PROMPT CI: agent files and tool prompts are load-bearing behavior. Any edit
   must be DELIBERATE — this test fails until the hash manifest is regenerated,
   forcing an explicit acknowledgment (and a place to hang future prompt evals):

       python -m tests.evals.regen_prompt_hashes

2. PIPELINE REPLAY: a golden item driven through the deterministic engines
   end-to-end must keep producing the same composite verdicts (enforcement
   engine roll-up + verification gate). If a refactor silently changes what
   the gates demand, this fails before any real item does.
"""
import hashlib
import json
from pathlib import Path

import pytest

from bc_agentic_mcp import checkpoints, enforcement, timeline, verification
from bc_agentic_mcp import item_context

PROJECT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).parent / "prompt_hashes.json"

PROMPT_GLOBS = [
    ("agents", "*.agent.md"),
    ("src/bc_agentic_mcp/prompts", "*.md"),
    ("src/bc_agentic_mcp/templates", "*.md"),
]


def current_prompt_hashes() -> dict:
    out = {}
    for folder, pattern in PROMPT_GLOBS:
        base = PROJECT / folder
        if not base.is_dir():
            continue
        for path in sorted(base.glob(pattern)):
            rel = str(path.relative_to(PROJECT)).replace("\\", "/")
            body = path.read_bytes().replace(b"\r\n", b"\n")  # EOL-insensitive
            out[rel] = hashlib.sha256(body).hexdigest()[:16]
    return out


def test_prompt_files_change_only_deliberately():
    if not MANIFEST.exists():
        pytest.fail(
            "prompt_hashes.json missing — generate it once:\n"
            "  python -m tests.evals.regen_prompt_hashes"
        )
    pinned = json.loads(MANIFEST.read_text(encoding="utf-8"))
    current = current_prompt_hashes()
    drifted = {
        name: (pinned.get(name, "<new>"), current.get(name, "<deleted>"))
        for name in set(pinned) | set(current)
        if pinned.get(name) != current.get(name)
    }
    assert not drifted, (
        "Prompt/agent files changed without acknowledgment (prompt CI):\n"
        + "\n".join(f"  {k}: {v[0]} -> {v[1]}" for k, v in sorted(drifted.items()))
        + "\nIf the change is intended, regenerate: python -m tests.evals.regen_prompt_hashes"
    )


@pytest.fixture()
def golden_item(tmp_path, monkeypatch):
    """A complete, healthy item built through PUBLIC APIs only (no fixture rot)."""
    monkeypatch.setenv("BC_AGENTIC_SPECS_ROOT", str(tmp_path / "ws"))
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Thing.Table.al").write_text(
        'table 50100 Thing { fields { field(1; "No."; Code[20]) { } } }', encoding="utf-8")
    spec = "wi1-golden"
    item_context.capture(str(root), spec, item_id="1",
                         description="Add field to Thing table per acceptance criteria.")
    checkpoints.write_charter(
        root, spec, purpose="Golden replay item",
        acceptance_criteria=["Field exists on Thing", "Existing behavior unchanged"])
    timeline.record_phase(root, spec, "bc_capture_item_context")
    return root, spec


def test_replay_engine_rollup_shape_is_stable(golden_item):
    root, spec = golden_item
    status = enforcement.engine_status(root, spec)
    # The seven engines are the contract — a rename/removal here breaks every consumer.
    assert set(status["engines"]) == {
        "timeline", "refinement", "root_cause", "traceability",
        "code_context", "quality", "clarifications",
    }
    assert status["engines"]["timeline"]["ok"] is True
    assert status["all_ok"] is False  # spec/quality engines have not run — must block
    assert status["next_actions"], "blocking engines must prescribe next actions"
    assert all({"engine", "tool"} <= set(a) for a in status["next_actions"])


def test_replay_verification_gate_fails_closed_then_passes(golden_item):
    root, spec = golden_item
    gate1 = verification.gate(root, spec)
    assert gate1["passed"] is False
    assert any("Uncovered criterion" in b for b in gate1["blockers"])
    # Cover both criteria with passing evidence at the strongest layer (1-based indices).
    for i, name in enumerate(["ItemTest_HappyAndEdge", "RegressionSuite"]):
        checkpoints.append_checkpoint(
            root, spec, kind="test",
            summary=f"{name} passed",
            details={"name": name, "result": "pass", "covers": [i + 1],
                     "layer": "al-regression" if i else "al-container",
                     "validation_mode": "regression" if i else "item",
                     "evidence": "container run acctest",
                     "evidence_source": "internal"},
        )
    gate2 = verification.gate(root, spec)
    digest = gate2["digest"]
    assert digest["coverage_pct"] == 100.0
    # Composite verdicts stay comparable run over run (the replay contract).
    assert set(digest) >= {"criteria_count", "coverage_pct", "required_strength_label"}
