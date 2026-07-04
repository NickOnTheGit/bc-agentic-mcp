---
name: bc-mcp-validate
description: 'Live validation that the bc-orchestrator agent can talk to the bc-agentic-mcp server and that the workflow gates actually fire. Run this after changing the agent, the MCP server, or VS Code settings.'
tools:
  - 'mcp_bc-agentic-mc__health'
  - 'mcp_bc-agentic-mc_bc_status'
  - 'mcp_bc-agentic-mc_bc_implement'
  - 'mcp_bc-agentic-mc_bc_recall'
  - 'mcp_bc-agentic-mc_bc_timeline'
---
# BC MCP live validation

Run these checks in order and report a PASS/FAIL table. Do NOT edit any files. Do NOT
approve anything. This is a read-only / dry-run probe of the live MCP contract.

Use spec_name `crm-contract-composition-business-event` unless the user names another spec.

1. **MCP reachable** — call `mcp_bc-agentic-mc__health`. PASS if `status == "ok"`.
2. **Status is prescriptive** — call `bc_status` with the spec_name. PASS if the response
   contains `enforcement` and a `next_actions` (or `blocking`) structure. Report the FIRST
   next_action `{tool, params_hint}`.
3. **Poka-yoke write gate fires** — call `bc_implement` with the spec_name and `dry_run: true`
   and NO code. PASS if it refuses / returns `blocked_needs_approval` (or an equivalent
   approval/precondition block). FAIL if it would write without an approved gate.
4. **Durable memory** — call `bc_recall` for the spec. PASS if it returns the charter/purpose
   (or a clear "no charter yet" for a spec that never reached prepare_review).
5. **Timeline** — call `bc_timeline` for the spec. PASS if it returns an ordered phase digest.

Output:
- A markdown table: Check | Result | Evidence (one line).
- If any check FAILs, state the most likely cause (MCP down, wrong spec, gate misconfigured,
  agent not loaded) and the single next action to fix it.
- Confirm at the end: "All reported file paths are absolute and complete."
