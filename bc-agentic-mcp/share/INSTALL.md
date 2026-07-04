# bc-agentic-mcp — Install Kit

Everything needed to run the Business Central agentic MCP on another machine.

## Kit contents

| Folder / file | Purpose |
|---|---|
| `server/` | The pip-installable MCP server (includes the Mission Control web cockpit) |
| `agents/` | Custom agent files (`bc-orchestrator`, `bc-implementer`, `bc-reviewer`) for `.github/agents/` |
| `instructions/` | Routing + gate instruction files for `.github/instructions/` |
| `install.ps1` | One-command installer (Windows PowerShell) |

## Quick install (Windows)

```powershell
.\install.ps1 -TargetRepo "C:\src\YourALRepo" -SpecsRoot "C:\bc-workspaces" `
              -OrgUrl "https://dev.azure.com/yourorg" -Project "YourProject"
```

`-TargetRepo` = **your own clone** of the AL repository on **your** machine — the kit
never references the sharer's computer. `-OrgUrl`/`-Project` become the default ADO
connection (baked into the generated MCP config as env vars). Set your personal PAT
once: `$env:AZURE_DEVOPS_EXT_PAT = "<pat>"` — secrets are never written to files.

Then merge the generated `mcp.json.generated` into VS Code's MCP config
(`Ctrl+Shift+P` → **MCP: Open User Configuration**) and reload.

## Manual install (any OS)

1. `python -m venv .venv && .venv/bin/pip install ./server`  (Python 3.10+)
2. Copy `agents/*.agent.md` → `<your AL repo>/.github/agents/`
3. Copy `instructions/*.instructions.md` → `<your AL repo>/.github/instructions/`
4. Register the server in `mcp.json`:

```jsonc
{
  "servers": {
    "bc-agentic-mcp": {
      "type": "stdio",
      "command": "<venv python>",
      "args": [
        "-m", "bc_agentic_mcp.server",
        "--project-root", "<your AL repo>",
        "--specs-root", "<folder for governance artifacts>"   // optional but recommended
      ]
    }
  }
}
```

## Use it

- In VS Code chat, select the **bc-orchestrator** agent and give it a work item:
  *"Refine and deliver work item 12345"* — it drives the full gated lifecycle
  (capture → spec → plan → **human approval** → implement → tests → verify → PR).
- Or start the web cockpit:

```powershell
bc-agentic-mission-control --project-root "<your AL repo>" --specs-root "<workspaces>" --open
```

## Prerequisites on the target machine

- Python 3.10+ and VS Code with GitHub Copilot (agent mode)
- For ADO capture tools: a PAT in the `AZURE_DEVOPS_EXT_PAT` environment variable
- For compile/test tools: AL extension + a local BC container (optional — the
  planning tier works without them)
