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

## Deterministic Validation Pattern

Use the MCP tools with derived values from the active spec and environment, not hardcoded
IDs, usernames, container names, file paths, or secrets.

Principles:
- Resolve `spec_name` from the active work item/spec.
- Resolve `test_extension_id` from the installed/published test app for that spec.
- Resolve `container_name` from the active local BC environment.
- Resolve `covers` from the acceptance-criterion indices in the Charter/spec.
- Read secrets from environment variables only.
- Only run API-contract validation when the spec actually declares an API surface.

Item-only empiric validation:

```text
bc_run_tests(
	project_root=<repo-root>,
	container_name=<resolved-local-container>,
	test_extension_id=<resolved-test-extension-id>,
	credential_env=<env-var-name-containing-password>,
	user=<derived-non-secret-test-user>,
	tenant=<resolved-tenant>,
	spec_name=<active-spec-name>,
	covers=<acceptance-criterion-indexes-for-item-slice>,
	validation_mode="item"
)
```

Targeted regression validation:

```text
bc_run_tests(
	project_root=<repo-root>,
	container_name=<resolved-local-container>,
	test_extension_id=<resolved-test-extension-id>,
	credential_env=<env-var-name-containing-password>,
	user=<derived-non-secret-test-user>,
	tenant=<resolved-tenant>,
	spec_name=<active-spec-name>,
	covers=<acceptance-criterion-indexes-covered-by-regression-slice>,
	validation_mode="regression"
)
```

Conditional API-contract validation:

```text
bc_api_contract(
	project_root=<repo-root>,
	base_url=<resolved-live-api-base-url>,
	entity=<resolved-entity-name-from-spec>,
	fields=<data-model-fields-from-spec>,
	operations=<operations-from-spec>,
	user=<derived-non-secret-api-user>,
	password_env=<env-var-name-containing-api-password>,
	spec_name=<active-spec-name>,
	covers=<acceptance-criterion-indexes-for-api-slice>
)
```

Verification:
- `bc_verify` should pass only when required validation classes are present.
- Required classes are deterministic:
	- always: `heuristic`, `empiric-item`, `regression`
	- only for API items: `api-contract`
