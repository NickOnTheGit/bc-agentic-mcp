"""Mission Control — local web cockpit for the bc-agentic-mcp lifecycle.

Run with:  bc-agentic-mission-control --project-root <AL repo> [--specs-root <base>]

The cockpit gives a human a live view of every item's gated lifecycle
(phase pipeline, artifacts to review, open clarifications, pending approvals)
and drives the DETERMINISTIC tools (capture, advance, auto-clarify, answers,
decisions) through the real MCP server over stdio — the same protocol path an
agent uses, so every gate/audit/timeline rule still applies.

Judgment steps (writing spec/design/code) intentionally stay with the coding
agent (bc-orchestrator); the UI surfaces a ready-to-paste prompt for those.
"""
