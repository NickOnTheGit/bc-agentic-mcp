# Repository Copilot Instructions

## Path Reporting (Mandatory)
- Always return absolute file paths when mentioning existing files.
- On Windows, use full drive-rooted paths using forward slashes.
- Do not return relative-only paths, shortened paths, or ellipsis paths.
- If the file is in this repository, include both:
  - Absolute path
  - Workspace-relative path in parentheses
- Verify file existence before reporting a path.
- If the absolute path cannot be determined, say so explicitly and do not output a partial path.

## Response Check
Before sending a final answer that includes file locations, ensure all reported paths are absolute and complete.

## BC MCP workflow routing (mandatory)
- Any Business Central / bc-agentic-mcp / AL / spec / refinement task must be driven by the
  `bc-orchestrator` custom agent and the gated MCP lifecycle. Do not shortcut it.
- Every workflow step goes through its `bc_*` tool. Never hand-produce an artifact a tool owns
  (spec, design, tasks, clarification answers, implementation).
- Follow `bc_status.next_actions` in order; resolve blockers only via the named tool.
- Never edit spec-scoped files (`*.al`, `spec.json`, `charter.json`, `TDD.md`, `DESIGN.md`,
  `TASKS.md`, `clarifications.md`, anything under `.specs/` or `.workspaces/`) with generic
  editors — a `PreToolUse` guard blocks this. Use `bc_implement` / `bc_write_spec` /
  `bc_answer_clarification` after human approval.
- If an MCP tool's output is insufficient, say so explicitly and ask before doing manual work.
