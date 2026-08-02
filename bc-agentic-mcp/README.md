# bc-agentic-mcp

Agentic MCP server for Business Central AL development — from human bullets to
AppSource-ready code through a gated, evidence-first lifecycle.

- State lives in a `.specs/` workspace (colocated or external via `--specs-root`)
- Scope / ID / schema / traceability enforcement is server-side, independent of model quality
- Human gates: plan approval, PR review, merge — never skipped
- Includes **Mission Control**, a local web cockpit for the whole lifecycle

## Install

```bash
pip install .
```

## Run the MCP server (stdio)

```bash
bc-agentic-mcp --project-root /path/to/al/extension [--specs-root /path/to/workspaces]
```

Register in VS Code `mcp.json`:

```jsonc
{
  "servers": {
    "bc-agentic-mcp": {
      "command": "python",
      "args": [
        "-m", "bc_agentic_mcp.server",
        "--project-root", "<AL repo>",
        "--specs-root", "<workspaces dir>"
      ]
    }
  }
}
```

## Run Mission Control (web cockpit)

```bash
bc-agentic-mission-control --project-root <AL repo> [--specs-root <workspaces dir>] --open
```

Then use the browser UI to launch items (capture ADO context), watch live phase
progress, answer clarifications, approve plan gates, advance deterministic
steps, and review every artifact. Judgment steps (spec/design/code) stay with
the `bc-orchestrator` agent — the cockpit hands you the exact prompt.

Mission Control binds to loopback by default. Remote binding requires
`BC_MISSION_CONTROL_TOKEN`; API requests must send a bearer token, and remote
agent dispatch is disabled unless `BC_MISSION_CONTROL_ALLOW_REMOTE_DISPATCH=1`
is explicitly enabled. Set `BC_MCP_SECURITY_SECRET` when signed approvals,
runtime evidence, and knowledge-read receipts must survive server restarts.

## Agent files

`agents/` contains the custom agent definitions to copy into a consuming
repository's `.github/agents/`:

- `bc-implementer.agent.md` — implementation through `bc_implement_write` only
- `bc-reviewer.agent.md` — independent review via `bc_review`

## Test

```bash
pip install -e ".[dev]"
pytest -v
```
