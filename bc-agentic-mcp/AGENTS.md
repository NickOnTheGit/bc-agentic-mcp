# AGENTS.md — bc-agentic-mcp operating protocol

Rules for any agent using this MCP server. These are **mandatory**, not advisory.

## Path output policy (mandatory)

When reporting files or artifacts in chat responses:
- Always provide the **absolute path** first.
- On Windows, use full drive-rooted paths (example: `C:/Users/NicolaeCatalina/Brain/bc-agentic-mcp/bc-agentic-mcp/docs/diagrams/bc-mcp-lifecycle.png`).
- Never provide only relative paths, shortened paths, or ellipsis forms.
- If the file is inside this repo, also include the workspace-relative path in parentheses after the absolute path.
- Before sending a path, verify the file exists (for example by listing the directory).
- If an absolute path cannot be determined, state that explicitly and do not emit a partial path.

Self-check before final response:
- Confirm: "All reported file paths are absolute and complete."

## Durable memory protocol (bullet-proof against goal drift)

Long tasks cause "context rot" — recall of the original goal degrades over many turns.
The item's intent is therefore **pinned to disk**, not trusted to conversation memory.

Per spec, under `.specs/<spec>/`:
- `charter.json` / `CHARTER.md` — immutable **purpose**, **operations `{read, update}`**, and **acceptance criteria**. Written by `bc_prepare_review`. Create-once.
- `checkpoints.jsonl` — append-only log of decisions/milestones.

### Required actions
1. **Re-anchor before acting.** Before implementing, continuing, or making any scope
   decision on an in-progress spec, call **`bc_recall`** and restate the purpose +
   operations in your own words. Every spec-scoped tool response also carries a
   `reanchor` field — read it each turn.
2. **Never contradict the Charter.** If your intended action conflicts with the charter's
   `operations` or acceptance criteria, STOP and reconcile with the user. Do not silently
   narrow/expand scope (e.g. treating a `read+update` item as read-only).
3. **Checkpoint decisions.** After any non-trivial decision (chosen approach, discovered
   constraint, scope change), call **`bc_checkpoint`** with a one-line summary.
4. **Change the Charter only deliberately.** A genuine scope change requires an explicit
   `overwrite` of the charter — announce it and record a checkpoint.
5. **Reflect automatically (no prompting needed).** Whenever you make a mistake, correct
   course, change scope, override a gate, or redo work, record it with `bc_checkpoint` using
   the matching `kind` (`mistake` / `correction` / `scope_change` / `override` / `rework`).
   The server then injects a **`reflection_due`** nudge into every spec-scoped response until
   you call **`bc_reflect`** with the lesson(s) — `promote: true` for a cross-repo lesson.
   Reflection is part of the workflow; the user should not have to ask for it.

## Mistake detection & approval enforcement (Layers 1–4)
Reflection closes the *record-the-lesson* gap; these layers close the *detect* and *prevent* gaps
so a human is not the only thing catching mistakes.
1. **Deterministic detectors (`bc_detect`, auto-run inside `bc_quality_check`).** Codifiable
   mistakes (upgrade-scope vs `DataPerCompany` = `V0100/V0101`, implementation started without
   approval, mutation lacking evidence) are auto-recorded as `mistake` checkpoints — which
   automatically raise `reflection_due`. No human catch required.
2. **Poka-yoke write path.** `bc_implement` refuses to write (`blocked_needs_approval`) unless a
   gating phase (tasks/implement/complete) is approved. It is the ONLY sanctioned code-write path;
   never edit spec-scoped files with generic editor/terminal tools.
   **Mechanical gate:** `python -m bc_agentic_mcp.gate --project-root <repo> --staged` (installable
   git pre-commit hook via `gate.install_hook`) rejects commits that touch spec-scoped code without
   an approved Charter — the one control that survives hand-editing outside the mcp.
3. **Separate reviewer (`bc_review`).** A *different* instance (the `bc-reviewer` agent) screens the
   diff against the Charter + BC first-principles checklist and records findings, which trigger the
   reflection loop. An actor cannot reliably grade its own work.
4. **Specialized driver agent.** The `bc-implementer` agent encodes this whole contract
   (capture → review → **await human approval** → implement-only-via-`bc_implement` → detect → review
   → reflect → verify). Agent files live under `agents/`.
5. **The learn loop (server-enforced, `attempts.py`).** Every guarded tool call runs
   research → try → fail → retry → succeed → learn:
   - every failure (exception OR blocked/failed structured result) is classified
     (license / dependency-symbol / missing-object / schema-mismatch / publish / auth /
     timeout / container) and appended to `.specs/<item>/failed_approaches.jsonl`;
   - an **identical** call (same tool + param fingerprint) that already failed twice with no
     intervening success is REFUSED with the prior errors — a doom-loop refusal means
     *change the approach* (different params, a preflight fix, a different tool), never
     retry harder;
   - a success that follows failures auto-records a `correction` checkpoint, which raises
     `reflection_due` so the recovery delta becomes a durable lesson;
   - unknown-error failures carry `applicable_lessons` in the error details — read them
     before the next attempt.

## Pre-implementation preflight (enforced by `bc_prepare_review`)
- **Capture the full item context FRESH, first (`bc_capture_item_context`).** The very first
  action on a work item is to materialize a durable bundle in `.specs/<spec>/context/`: the
  description, **every linked wiki page** (live via REST+PAT), and **every related item**. All
  later steps reference this saved bundle — never memory, a stale local clone, or code-convention
  inference. Fetch failures are recorded as `unresolved` and must be resolved, not skipped.
- **Fresh data, no workarounds.** When a blocker occurs (auth/403, encoding crash, missing tool),
  fix the root cause to obtain FRESH authoritative data (e.g. `bc_fetch_wiki` with the PAT), never
  substitute stale/cached/inferred data. Inference is a last resort and must be labelled unverified.
- **Consult the item's own references before choosing a target (`bc_extract_references`).** The
  linked wiki is authoritative for *which* object/API to change and **overrides codebase-convention
  inference**. When several API surfaces expose the same table, the wiki names the correct one.
- **Latest source (`BC-STALE-SOURCE`).** The local checkout must be up to date with its
  upstream before changes. If behind, pull first — a stale checkout leads to recreating
  objects/fields that already exist upstream.
- **Verify existence before creating.** "Extend the API" exposes EXISTING table fields; it
  does not authorize new table fields. Confirm each requested field exists on the source
  table (or upstream via `git show origin/<branch>:<path>`) before adding anything.

## Bugfix lane (Path D — work item type `Bug`)
A Bug is not a small PBI: its lifecycle starts from a SYMPTOM, not a requirement. Capture
detects the lane automatically (identity `type: Bug` ⇒ `lane: bugfix`; ReproSteps/SystemInfo
TCM fields are folded into the captured description so an empty `System.Description` never
yields an empty context).
1. `bc_capture_item_context` — fresh bundle; result prescribes `bc_root_cause` for bugs.
2. `bc_root_cause` — **diagnosis before planning** (the bug-lane twin of `bc_refine_item`):
   the model's symptom/root-cause/fix judgment is CONFRONTED with code reality — every
   evidence reference (AL path or object ref) is verified against the repo/object index,
   fail-closed. Persists `ROOT-CAUSE.md` + `root_cause.json`; phase `root_cause_identified`.
   The `root_cause` enforcement engine blocks bug planning/commits without it (non-bug
   lanes pass trivially).
3. `bc_write_spec` — emits `spec_type: bugfix` with a MANDATORY symptom-regression
   requirement (EARS `unwanted`): the acceptance test must fail on pre-fix code and pass
   after the fix (`layer='al-regression'`), plus the standard targeted regression slice so
   the fix itself introduces no new regressions. The spec bullets MUST declare the full
   test pyramid (`TEST happy:/negative:/edge:` lines — `TEST api:` when an API surface
   touches the artifact); the review quality gate (`test_shapes_declared`) refuses
   under-declared plans, and `verification.api_pages_touching` forces the api-contract
   validation class whenever an existing API page serves a touched object.
4. From here the canonical lifecycle applies UNCHANGED — design → tasks → review →
   **human `plan` gate** → `bc_implement_write` → detect → review → tests (empiric container
   evidence; the `regression` validation class is required) → verify → PR → archive.
5. **Learning loop:** `bc_archive` of a bugfix item auto-records a `BUG-PATTERN` lesson from
   the verified root cause; a recurring pattern is promoted to a confirmed lesson
   (surfaced proactively by analyzers) and the archive response prescribes
   `bc_promote_lesson` for cross-project reach. Reflection (`reflection_due`) and the
   doom-loop learn loop apply exactly as in the PBI lane.

## Ground truth
- Self-contained static validation (`al_validator` / `bc_quality_check`) is **necessary but
  not sufficient**. Real proof = compile against the target container symbols + upgrade
  compatibility + run tests + API. Never report "validated/tested" from static checks alone.
- Live lifecycle requirement: every implemented item must (1) have tests generated when missing,
  (2) execute tests on a local BC container, and (3) record that evidence before approval/archive.
  This is mandatory; skipping requires explicit override rationale.
- Before compiling/publishing any test app, run a **dependency symbol preflight**: each
  `app.json` dependency must resolve from the target tenant's symbol feed (`/dev/packages`).
  If a dependency symbol returns 404 (common after restore on Zig test stacks), publish the
  missing chain first in this order: `Zig365 Test Library` -> `Zig365 Test Framework` ->
  `Zig365 Foundation Test`, then retry the target app compile/publish.
- BC table field names must be ≤ 30 chars (AL0468 / `V0070` / `BC-FIELDLEN`).
- **Upgrade scope is derived from the table's `DataPerCompany`, from first principles — not
  copied from a neighbouring codeunit.** `DataPerCompany = false` = shared/database-scoped data
  (one physical copy per database) ⇒ upgrade it **per-database** (`implements UpgradePerDatabaseSAN`,
  `UpgradePerDatabase`, tag in the per-database upgrade-tags enum): it runs **once, with no company
  guard**. `DataPerCompany = true` ⇒ **per-company** (`UpgradePerCompanySAN`). A per-company upgrade
  with a "first company" guard (`if Company.Name <> CompanyName() then exit`) on shared data is a
  fragile workaround — do NOT mirror it just because older siblings do. `al_validator` flags the
  mismatch (`V0100` per-company→shared table, `V0101` per-database→per-company table). General rule:
  treat semantic properties (`DataPerCompany`, `Editable`, `DataClassification`, `TableType`,
  `ObsoleteState`) as **decision inputs to derive from**, never attributes to copy from a neighbour.
- Multi-version API mirroring is **not** automatic — verify against upstream which versions
  actually expose new fields (a feature may ship on the latest version only).
- **BC permissions are table-level.** Adding fields to an API page needs **no** permission
  change when a set already grants `tabledata <Table> = r/m` (directly or via
  `IncludedPermissionSets`). Verify with `bc_check_permission_coverage` — do not blindly edit a
  permission set, nor assume one is needed. A purpose-built update API already grants `= m`; only
  a generic read-only table-mirror page grants `= r` (the one case where you bump `r`→`Rm`).
- **API versioning for additive fields:** extend the existing current (non-obsolete) version
  page in place; do not cut a new version and do not add to an `ObsoleteState = Pending` page.
  Confirm the team pattern from git history (prior "Extend API" PRs on that page).

## Testing playbook (`bc_generate_tests` + `testing_playbook`)
Happy paths are not enough. `bc_generate_tests` scaffolds five layers; cover each that applies:
- **happy-path** — valid input produces the expected persisted result.
- **negative** — invalid input / forbidden operations are *rejected* (past dates, broken
  table relations, over-length text, wrong types).
- **boundary** — edge values: today, blank `0D`, and the BC `Date` limits (`1753-01-01` ..
  `9999-12-31`; `0D` serializes over OData as `0001-01-01`).
- **business-logic** — behaviour derived from tracing a field's *consumers* (grep the field
  name across the app), e.g. an "until/till" date that auto-expires and releases state. Pull
  the real work item to understand intent; do not test fields in isolation.
- **api-contract** — for API pages: insert/delete gating (update-only → POST 405, DELETE
  blocked), optimistic concurrency (stale ETag), malformed bodies; re-read and assert the
  record is **unchanged** after every rejected request. Date edge cases (invalid calendar,
  negative/zero year, year > 9999) are only testable via raw JSON PATCH — the AL `Date` type
  cannot hold them.

Generated scaffolds **fail until implemented** (`LibraryAssert.Fail('TODO...')`). Never leave
a vacuous `IsTrue(true)` or an `asserterror Fail` stub — both pass trivially and are mistaken
for real coverage. A to-do test must be red until it makes a real assertion.

Mandatory execution order for implemented items:
1. Generate tests (`bc_generate_tests`) when coverage is missing.
2. Implement/fix tests until assertions are real and deterministic.
3. Run tests in local container (`bc_run_tests`) and capture evidence.
4. Verify (`bc_verify`) and only then request/approve completion.

Testing doctrine for Business Central items (mandatory):
1. **Heuristic validation first**
  - Re-read the spec acceptance criteria.
  - Inspect code context and consumers of the changed symbol/field/object.
  - Identify business-rule risks, data dependencies, and likely regressions before writing tests.
2. **Empiric item validation second**
  - Execute item-only tests mapped to acceptance criteria.
  - Cover happy-path, negative, edge/boundary, and business-logic scenarios that apply.
  - For APIs, also cover contract behavior (method restrictions, concurrency, malformed payloads, unchanged-state assertions).
3. **Regression validation third**
  - Run a targeted regression slice for nearby consumers or adjacent business flows impacted by the item.
  - Use broad extension-suite runs only when risk warrants it; broad suites are regression signals, not primary item proof.
4. **Performance/regression depth when warranted**
  - If the change affects high-traffic flows, repeated queries, reports, or integration endpoints, include a performance-regression probe.
  - Prefer BC Performance Toolkit / telemetry-based checks for performance-sensitive changes.

Evidence reporting requirement (mandatory):
- Completion evidence must be item-scoped. Do not present only broad extension/regression
  suite output for item sign-off.
- Produce a readable evidence artifact per item with, at minimum, these fields per test:
  `name`, `description`, `expected`, `actual`, `status`, and execution time.
- Keep broad suite results as secondary signals; item acceptance is judged by item-scoped
  tests mapped to acceptance criteria.
- Distinguish evidence classes in the report when applicable:
  - `heuristic`: code/context/consumer analysis
  - `empiric-item`: item-only live tests
  - `regression`: targeted neighboring flow tests
  - `performance-regression`: BCPT or telemetry-backed signal for risk-sensitive changes

Deterministic usage example (no hardcoded values):
- Resolve all runtime values from the active spec and environment; never bake in a specific
  container name, extension id, user, path, or secret.
- Item slice:
  - `bc_run_tests(project_root=<repo-root>, container_name=<resolved-local-container>, test_extension_id=<resolved-test-extension-id>, credential_env=<password-env>, user=<derived-user>, tenant=<resolved-tenant>, spec_name=<active-spec>, covers=<criterion-indexes>, validation_mode="item")`
- Regression slice:
  - `bc_run_tests(project_root=<repo-root>, container_name=<resolved-local-container>, test_extension_id=<resolved-test-extension-id>, credential_env=<password-env>, user=<derived-user>, tenant=<resolved-tenant>, spec_name=<active-spec>, covers=<criterion-indexes>, validation_mode="regression")`
- API slice (only for API items):
  - `bc_api_contract(project_root=<repo-root>, base_url=<resolved-base-url>, entity=<entity-from-spec>, fields=<data-model-from-spec>, operations=<operations-from-spec>, user=<derived-user>, password_env=<api-password-env>, spec_name=<active-spec>, covers=<criterion-indexes>)`

## Execution, evidence & schema-safety tools
All are deterministic: pure logic at the core with real I/O (subprocess/HTTP/filesystem)
behind injectable seams; nothing is hardcoded (container/endpoint/field/credentials are
inputs; secrets come from env vars, never stored).
- **`bc_run_tests`** — runs a published test extension in a BC container, parses the result,
  and (with `spec_name`+`covers`) records it as *captured* runtime evidence.
- **`bc_api_contract`** — derives an OData contract plan (GET + negatives + boundaries) from
  the spec's own field metadata and executes it against a live endpoint.
- **`bc_reconcile_target`** — diffs requested fields against deployed OData `$metadata` so an
  "extend the API" item never recreates an existing field.
- **`bc_upgrade_preflight`** — flags an upgrade that would REMOVE fields/tables the deployed
  baseline still has (breaking).
- **`bc_find_consumers`** — finds who *consumes* an AL symbol (business-logic discovery).
- **`bc_promote_lesson`** — promotes a lesson to the cross-project store; `applicable_lessons`
  surfaces it in other repos via deterministic token-overlap.

### Evidence bar (`bc_verify`)
Coverage alone is not "done". Each passing test carries an evidence *tier*
(claim < compile < executed-test < live-runtime); an empiric claim with **no captured
evidence is downgraded to a bare claim**. `bc_verify` derives the required tier from the
item's operations (a mutation must reach live-runtime) and reports `fully_validated_strict`
plus `evidence_gaps`. Record evidence via `bc_run_tests`/`bc_api_contract`, not by hand.

### Enforcement (the gate is consulted, not just reported)
`verification.gate()` is a fail-closed check (coverage AND evidence bar). It is wired into
`bc_submit_decision`: an **`approve` on the `implement`/`complete` phase is BLOCKED** unless
the gate passes — the approval is not written. A deliberate bypass requires an explicit
`override_reason`, which is stamped loudly into the audit entry (never silent). This is the
chokepoint that makes the evidence bar real; a tool that must be *remembered* is not
enforcement.

## Tests
`cd` to the repo and run `python -m pytest -q` after any change to the server.
