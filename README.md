# bc-agentic-mcp

Agentic MCP server for Business Central AL development.

Builds a standalone, portable MCP server that enables agentic BC AL development
from human bullets to AppSource-ready code. State lives in a `.specs/` directory;
prompts are editable Markdown files; scope/ID/schema enforcement is server-side
and independent of model quality.

## Install (dev)

```bash
pip install -e ".[dev]"
```

## Run

```bash
bc-agentic-mcp --project-root /path/to/al/extension
```

## Test

```bash
pytest -v
```
