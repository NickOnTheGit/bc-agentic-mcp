# Deterministic Evidence-First Planning and Review Strategy

## 1. Purpose

Define a fully deterministic, enforceable workflow for `prepare_review` so that generated review packets are:

1. Evidence-based.
2. Explicit and implementation-ready.
3. Fail-closed when information is incomplete.
4. Reproducible for the same input and idempotency key.

This plan aligns with the paradigm:

1. Decompose from first principles.
2. Search local ERP AL first.
3. Search linked artifacts and external sources only when required.
4. Ask questions only after exhaustive evidence checks.
5. Produce a final review where each claim is traceable to evidence.

> Status: Draft v2 (for external review). Section 4B (Machine Spec Schema) is the
> normative contract; everything else exists to produce and guarantee it.

## 1A. Industry Standards Alignment (Evidence-Based)

This strategy is deliberately aligned with three established, independently sourced
standards so the output format is not invented in isolation.

### 1A.1 EARS — Easy Approach to Requirements Syntax

Source: `https://alistairmavin.com/ears/` (used by NASA, Airbus, Bosch, Intel,
Siemens, Rolls-Royce). EARS constrains natural-language requirements into a small
set of unambiguous, testable patterns. Every requirement in the machine spec MUST
be expressed in one EARS pattern:

1. Ubiquitous: `The <system> shall <response>.`
2. State-driven: `While <precondition>, the <system> shall <response>.`
3. Event-driven: `When <trigger>, the <system> shall <response>.`
4. Optional: `Where <feature>, the <system> shall <response>.`
5. Unwanted: `If <trigger>, then the <system> shall <response>.`
6. Complex: combination of the above keywords in fixed order.

Rationale: EARS removes ambiguity at the source, which is the single biggest cause
of friction for a downstream implementation agent.

### 1A.2 GitHub Spec Kit — Spec-Driven Development workflow

Source: `https://github.com/github/spec-kit`. Workflow:
`constitution -> specify -> clarify -> plan -> tasks -> analyze -> checklist -> implement`.

Adopted ideas:

1. `clarify` runs before `plan` (never plan on ambiguity).
2. `analyze` performs cross-artifact consistency and coverage checks.
3. `checklist` acts as "unit tests for English" — it validates requirement
   completeness, clarity, and consistency before implementation.

### 1A.3 AWS Kiro — three-file spec model

Source: `https://kiro.dev/docs/specs/`. Kiro emits three artifacts:

1. `requirements.md` — user stories plus EARS acceptance criteria.
2. `design.md` — architecture, sequence/data flow, error handling, testing.
3. `tasks.md` — discrete tasks grouped into dependency waves (already mirrored by
   this repo's `breakdown_tasks.py`).

Kiro also separates Feature specs from Bugfix specs (current/expected/unchanged
behavior). This plan adopts the same `spec_type` distinction.

## 2. Current Pain Points (Observed)

1. Placeholder leakage in generated outputs (for example, flow sections and extension points).
2. Non-specific evidence snippets (examples that are related to file family but not exact intent).
3. Weak coupling between claims and source evidence in final review.
4. Inconsistent failure policy (historically could return `ready_for_review` even with low signal).
5. Missing explicit completeness guarantees in final artifacts.

## 3. Target Operating Model

### 3.1 Deterministic Principles

1. Same input + same workspace snapshot + same idempotency key => same outputs.
2. Stable ordering for decomposition, evidence collection, ranking, and rendering.
3. No implicit assumptions without explicit "assumption" labeling.
4. No unresolved placeholders in final review when status is `ready_for_review`.
5. Fail closed: unresolved critical requirements force `needs_clarification`.

### 3.2 Source Priority (Strict)

1. Local ERP AL codebase (authoritative for implementation behavior).
2. Linked internal artifacts (work item, wiki, existing spec artifacts).
3. External documentation/web (only for unresolved technical semantics).

## 4. End-to-End Pipeline (Required Stages)

### Stage A: Intake and First-Principles Decomposition

Input:

1. Human description.
2. Project root.
3. Idempotency key.

Output artifact:

1. `decomposition.json` under `.specs/<spec_name>/`.

Required decomposition schema:

1. `actors` (who initiates behavior).
2. `intent` (business objective).
3. `api_targets_requested` (endpoint names, versions if implied).
4. `field_requirements` (canonical names and inferred types if possible).
5. `operation_requirements` (`read`, `update`, `create`, `delete`).
6. `validation_requirements` (explicit or unresolved).
7. `state_behavior_requirements` (lifecycle or workflow implications).
8. `compatibility_requirements` (backward compatibility constraints).
9. `test_expectations` (minimum outcomes to verify).
10. `open_points` (items requiring evidence or clarification).
11. `user_stories` (each as `as_a` / `i_want` / `so_that`).
12. `requirements_ears` (each requirement normalized into one EARS pattern with a
    stable `REQ-NNN` id and a `story_ref`).

Deterministic rules:

1. Stable extraction order based on line order and canonical sorting.
2. Unknowns are not dropped; they are listed in `open_points`.
3. Every requirement MUST be emitted in an EARS pattern (Section 1A.1). A
   requirement that cannot be expressed in EARS is treated as ambiguous and routed
   to `open_points`.
4. Each requirement is assigned a stable id (`REQ-001`, `REQ-002`, ...) by
   canonical ordering, so reruns produce identical ids.

### Stage B: Evidence Collection

Output artifact:

1. `evidence.json` under `.specs/<spec_name>/`.

Required evidence schema for each claim:

1. `claim_id`.
2. `claim_text`.
3. `source_type` (`local_code`, `work_item`, `wiki`, `external_doc`).
4. `source_path_or_url`.
5. `source_excerpt`.
6. `confidence` (`high`, `medium`, `low`).
7. `status` (`supported`, `contradicted`, `insufficient`).

Collection policy:

1. Attempt local code evidence first for every claim.
2. If insufficient, attempt linked artifacts.
3. If still insufficient and technically necessary, attempt external docs.
4. Record each attempt outcome even when unsuccessful.

### Stage C: Evidence-to-Spec Reconstruction

Output artifacts:

1. `spec.json`.
2. `TDD.md`.

Reconstruction rules:

1. Only `supported` or explicitly accepted assumptions may populate hard commitments.
2. `insufficient` claims stay unresolved and must not be silently converted into facts.
3. Every business rule in `spec.json` must include a traceable claim reference.
4. Every object target must be evidenced by local code discovery or explicit user instruction.

### Stage D: Deterministic Clarification Generation

Entry condition:

1. One or more critical claims remain `insufficient`.

Output artifact:

1. `clarifications.md`.

Rules:

1. Questions must include:
   - What was searched.
   - What was found.
   - Why ambiguity remains.
   - Concrete option framing and impact.
2. Generic clarification questions are forbidden.
3. If this stage triggers, final status is `needs_clarification`.

### Stage E: Design and Task Generation

Output artifacts:

1. `DESIGN.md`.
2. `TASKS.md`.

Rules:

1. No placeholder markers allowed in `ready_for_review` output.
2. Data Flow sections must be fully populated from machine spec and evidence links.
3. Task list must include object-linked tasks for every `objects_to_modify` and `objects_to_create` item.
4. Tests must be mapped to requirement claims.

### Stage F: Final Review Assembly

Output artifact:

1. `REVIEW.md`.

Mandatory sections:

1. Requirement decomposition summary.
2. Evidence matrix (claim -> source -> confidence -> status).
3. Scope and target files.
4. Business rules with traceability.
5. Data flows with explicit steps.
6. Task plan and acceptance criteria.
7. Assumptions and unresolved items.
8. Deterministic enforcement report.

## 4B. Machine Spec Schema (Bulletproof Implementation Contract)

This is the normative output of the planning MCP. The implementation agent consumes
`spec.json` and nothing else is required for it to proceed. The schema is versioned
and every element is traceable.

### 4B.1 Top-level shape

```json
{
  "schema_version": "2.0",
  "spec_id": "wi-264484",
  "spec_name": "wi-264484-rental-mutation-on-hold-subprocess",
  "spec_type": "feature",
  "idempotency_key": "wi-264484-20260630",
  "module": "EmpireTableAPI",
  "source": {
    "work_item_url": "https://.../_workitems/edit/264484",
    "wiki_url": "https://.../EMP-Rental-Mutation",
    "related_work_items": ["258909"],
    "description_sha256": "<hash of raw description>"
  },
  "summary": {
    "goal": "<one sentence>",
    "in_scope": ["..."],
    "out_of_scope": ["..."]
  },
  "user_stories": [
    { "id": "US-001", "as_a": "external application", "i_want": "read/update on-hold subprocess fields", "so_that": "the inspector knows if a subprocess is on hold" }
  ],
  "requirements": [
    {
      "id": "REQ-001",
      "story_ref": "US-001",
      "ears_type": "event",
      "statement": "When an external app sends a PATCH to rentalMutation, the API shall persist the provided on-hold subprocess field values.",
      "fields": ["SubprocessLeavingTenantOnHoldTill"],
      "evidence_refs": ["EV-003"],
      "acceptance_tests": ["AT-001"],
      "status": "supported"
    }
  ],
  "data_model": [
    {
      "field": "SubprocessLeavingTenantOnHoldTill",
      "al_type": "Date",
      "source_table": "RentalMutationHSG",
      "api_attribute": "subprocessLeavingTenantOnHoldTill",
      "read": true,
      "update": true,
      "evidence_refs": ["EV-010"]
    }
  ],
  "objects_to_create": [],
  "objects_to_modify": [
    {
      "id": "OBJ-001",
      "type": "API Page",
      "target": "src/v21/Housing/RentalMutationHSG21.Page.al",
      "version": "v21",
      "change": "Add API field mappings for the 10 subprocess on-hold fields.",
      "fields_added": ["SubprocessLeavingTenantOnHoldTill"],
      "evidence_refs": ["EV-021"]
    }
  ],
  "business_rules": [
    {
      "id": "BR-001",
      "statement": "The API shall expose on-hold subprocess fields as direct Rec mappings (no added date validation).",
      "ears_type": "ubiquitous",
      "applies_to_fields": ["SubprocessLeavingTenantOnHoldTill"],
      "evidence_refs": ["EV-030"]
    }
  ],
  "acceptance_tests": [
    {
      "id": "AT-001",
      "requirement_ref": "REQ-001",
      "type": "update",
      "given": "A rentalMutation record exists",
      "when": "PATCH sets subprocessLeavingTenantOnHoldTill",
      "then": "GET returns the updated value"
    }
  ],
  "evidence": [
    {
      "id": "EV-003",
      "claim_ref": "REQ-001",
      "source_type": "local_code",
      "source": "src/v21/Housing/RentalMutationHSG21.Page.al",
      "excerpt": "field(\"leavingTenantSubProcCompleted\"; Rec.\"LeavingTenantSubProcCompleted\")",
      "confidence": "high",
      "status": "supported"
    }
  ],
  "assumptions": [
    { "id": "AS-001", "statement": "No extra date validation on OnHoldTill", "rationale": "Existing OnHoldDate APIs are passthrough", "reversible": true, "evidence_refs": ["EV-030"] }
  ],
  "open_questions": [],
  "scope_boundaries": {
    "allowed_extensions": ["EmpireTableAPI"],
    "allowed_files": ["src/v21/Housing/RentalMutationHSG21.Page.al"],
    "forbidden_patterns": ["OnValidate date rejection"]
  },
  "traceability": {
    "requirement_to_test": { "REQ-001": ["AT-001"] },
    "requirement_to_object": { "REQ-001": ["OBJ-001"] },
    "field_to_object": { "SubprocessLeavingTenantOnHoldTill": ["OBJ-001"] }
  },
  "references": ["<urls>"],
  "quality_gate": { "pass": true, "checks": { } }
}
```

### 4B.2 Mandatory schema invariants (enforced, fail-closed)

1. Every `requirements[]` item has `ears_type`, at least one `evidence_refs`, and at
   least one `acceptance_tests`.
2. Every requirement `status` in a `ready_for_review` spec is `supported` or
   `assumption` (never `insufficient`).
3. Every `assumption` has a `rationale` and at least one `evidence_refs`.
4. Every `data_model[]` field maps to at least one object via
   `traceability.field_to_object`.
5. Every `objects_to_modify[]` / `objects_to_create[]` item has at least one
   `evidence_refs` (local code discovery or explicit user instruction).
6. Every requested field from the description appears in `data_model[]` and in at
   least one requirement.
7. Every `acceptance_tests[]` item references an existing `requirement_ref`.
8. `traceability` maps are complete: no dangling ids, no orphan requirements.
9. All ids are stable and canonical (`REQ-001`, `OBJ-001`, `AT-001`, `EV-001`),
   reproducible across idempotent reruns.
10. `description_sha256` pins the exact input the spec was derived from.

### 4B.3 Why this is "frictionless" for the implementation agent

1. No prose interpretation required: every action is an EARS statement plus a
   concrete file target plus an acceptance test.
2. No missing scope: `scope_boundaries` defines exactly which files may change.
3. No silent assumptions: anything inferred is labeled in `assumptions[]`.
4. No unverifiable claims: each requirement is backed by `evidence[]`.
5. No coverage gaps: `traceability` guarantees requirement -> object -> test closure.

## 4C. Business Central-Aware Reviewer (Layer 1, deterministic)

A dedicated analyzer stage runs after tasks and before the final gate. It is pure,
deterministic code (no LLM) and writes `ANALYSIS.md` + `analysis.json`. Error-level
findings fail the gate closed (forces `needs_clarification`). It adopts real BC/AL
tooling facts:

1. `app.json` `idRanges`: every modified object's numeric ID must fall inside the
   declared ranges (mirrors AppSourceCop AS0013/AS0084/AS0099). The spec now carries
   `bc_metadata.id_ranges` and each object's `object_id`, `object_name`, `object_kind`,
   and `id_in_range`.
2. API writability: each target API page is parsed for `ModifyAllowed`,
   `DataAccessIntent`, and `Permissions`. If the spec requires update but every target
   is read-only, the analyzer raises `BC-READONLY` (error). This catches the exact
   real-world case where `rentalMutation` API pages are `DataAccessIntent = ReadOnly`
   / `Permissions = R` yet the request asks to update.
3. Backward compatibility: a requirement preserving existing behavior must exist
   (mirrors AppSourceCop AS0001/AS0002/AS0005 breaking-change rules).
4. Scope: `scope_boundaries.allowed_files` must be non-empty and contain every target.
5. Cross-artifact: every target must appear in `DESIGN.md` and have a task in
   `TASKS.md`.
6. Mandatory affix: `AppSourceCop.json` `mandatoryAffixes` are surfaced (info) for new
   table fields (mirrors AS0011/AS0098).
7. DataClassification reminder for new fields (mirrors AS0016).

The analyzer findings, a requirements checklist ("unit tests for English"), and the
human `RATIONALE.md` are all embedded into `REVIEW.md`.

## 5. Enforcement Hooks (Hard Gates)

Generate and persist `quality_gate.json` with detailed checks.

### 5.1 Minimum Gate Checks

1. `has_any_objects`.
2. `has_api_target`.
3. `field_coverage_ok`.
4. `tasks_ok`.
5. `group_coverage_in_examples`.
6. `version_coverage_in_examples`.
7. `no_placeholders_remaining`.
8. `all_required_claims_evidenced`.
9. `evidence_matrix_present`.
10. `flow_sections_complete`.
11. `every_requirement_is_ears` (each requirement matches an EARS pattern).
12. `every_requirement_has_test` (requirement -> acceptance test closure).
13. `every_field_mapped_to_object` (no field without an implementation target).
14. `every_object_has_evidence` (no target without source justification).
15. `traceability_complete` (no dangling or orphan ids).
16. `cross_artifact_consistent` (spec/design/tasks agree on objects and fields).
17. `requirements_checklist_pass` ("unit tests for English": completeness,
    clarity, consistency — per Spec Kit `checklist`).
18. `no_insufficient_requirements` (no `status: insufficient` in ready output).

### 5.2 Gate Policy

1. If any required check is false, status must be `needs_clarification`.
2. `ready_for_review` is forbidden when gate fails.
3. Gate failures must be listed in `clarifications.md` and `quality_gate.json`.

## 6. Placeholder and Completeness Policy

Forbidden tokens in `ready_for_review` artifacts include:

1. `to be filled by AI model`.
2. `(to be filled)`.
3. `TBD`.
4. `TODO`.

Validation behavior:

1. If found in required sections, fail gate.
2. Return `needs_clarification` with explicit remediation prompts.

## 7. Evidence Matrix Specification

Add a dedicated section and/or artifact with one row per claim:

1. Claim ID.
2. Requirement text fragment.
3. Decision outcome.
4. Primary evidence source.
5. Supporting evidence sources.
6. Confidence.
7. Notes/assumption flag.

Rules:

1. Every field requirement must map to at least one evidence row.
2. Every object target must map to at least one evidence row.
3. Every flow step must reference one or more relevant claims.

## 8. Clarification Policy (After Full Search Only)

Question generation checklist:

1. Local ERP AL search completed and recorded.
2. Work item/wiki parsing completed and recorded.
3. External lookup attempted only if technically needed.
4. Ambiguity is classified as blocking or non-blocking.

Question format:

1. `Context`.
2. `Observed evidence`.
3. `Decision options`.
4. `Recommended default`.
5. `Impact if unanswered`.

## 9. Determinism Controls

1. Stable candidate sorting for files, claims, and tasks.
2. Explicit max limits and deterministic truncation policy.
3. Stable serialization (sorted keys where needed).
4. Idempotency check before regeneration.
5. Evidence ranking formula must be static and test-covered.

## 10. Test Strategy

### 10.1 Unit Tests

1. Decomposition extraction consistency.
2. Evidence collection ordering and source fallback.
3. Gate failure when placeholders exist.
4. Gate failure when claims are unevidenced.
5. Gate failure when version coverage is missing.
6. Clarification generation content quality (contains search context and options).

### 10.2 Golden Tests

1. Known work-item descriptions with expected stable outputs.
2. Repeated run with same idempotency key returns same artifact paths/content hash.

### 10.3 Regression Tests

1. Prevent reintroduction of placeholder text in ready output.
2. Prevent empty object/task plans for modification-only specs.
3. Prevent generic, non-evidence clarification prompts.

## 11. Rollout Plan

### Phase 1: Core Evidence Scaffold

Deliverables:

1. `decomposition.json` generation.
2. `evidence.json` generation.
3. Base evidence matrix rendering in review.

Exit criteria:

1. Decomposition and evidence artifacts exist for every run.
2. No regression in existing tests.

### Phase 2: Gate Expansion

Deliverables:

1. Add `no_placeholders_remaining`, `all_required_claims_evidenced`, `flow_sections_complete` checks.
2. Fail-closed enforcement for all new checks.

Exit criteria:

1. `ready_for_review` impossible with placeholders or missing evidence.

### Phase 3: Clarification Hardening

Deliverables:

1. Context-rich, evidence-aware question templates.
2. Blocking/non-blocking ambiguity classification.

Exit criteria:

1. Clarifications are only emitted after full search chain.
2. Questions include explicit options and impact.

### Phase 4: Deterministic Replay Validation

Deliverables:

1. Golden tests and deterministic replay checks.
2. Hash comparison for idempotent reruns.

Exit criteria:

1. Same input/key reproduces equivalent outputs.

## 12. Governance and Review Process

For each generated review packet, reviewer must verify:

1. Evidence matrix completeness.
2. No placeholder tokens.
3. Flow steps are explicit and actionable.
4. Task plan maps directly to evidenced scope.
5. Quality gate passes with no ignored failures.

Reviewer sign-off block to include:

1. `Evidence completeness: pass/fail`.
2. `Ambiguity risk: low/medium/high`.
3. `Implementation readiness: yes/no`.

## 13. Immediate Implementation Backlog

1. Add decomposition artifact writer in `prepare_review` pipeline.
2. Add evidence artifact writer with source-priority tracing.
3. Extend `quality_gate.json` schema with evidence and placeholder checks.
4. Add explicit evidence matrix section in `REVIEW.md`.
5. Add placeholder scanner for all generated markdown artifacts.
6. Add tests for all new gate checks and clarification quality.

## 14. Success Criteria

The strategy is considered complete when all are true:

1. `ready_for_review` outputs have zero placeholders.
2. Every critical claim in review is evidence-backed.
3. Clarifications are rare, specific, and justified by recorded search gaps.
4. Generated task plans are object-linked and executable.
5. Deterministic reruns are stable and auditable.

## 15. Implementation Agent Contract

The planning MCP and the implementation agent communicate only through `spec.json`
(Section 4B). The contract guarantees:

1. Input stability: the agent reads `spec.json`; `REVIEW.md` is for humans only.
2. Scope safety: the agent must not modify files outside `scope_boundaries`.
3. Action sourcing: every change the agent makes maps to a `REQ-NNN` and an
   `OBJ-NNN`; if a needed change has no requirement, the agent must stop and
   request a spec amendment rather than improvise.
4. Test obligation: the agent must implement/satisfy every `acceptance_tests[]`
   entry; these become the definition of done.
5. Assumption transparency: the agent treats `assumptions[]` as accepted defaults,
   not as facts to re-derive.
6. Failure handling: if the spec fails any gate, the agent receives
   `needs_clarification` and must not start implementation.

Agent-side preconditions before writing code:

1. `quality_gate.pass == true`.
2. `schema_version` is supported.
3. `description_sha256` matches the spec the human approved.

## 16. Gap Analysis vs Existing Implementation

Current code already provides a strong base; the deltas to reach this contract are:

1. `write_spec.py` emits a thin `spec.json` (objects, business rules, references).
   Missing: `user_stories`, EARS `requirements[]`, `data_model[]`, `evidence[]`,
   `assumptions[]`, `acceptance_tests[]`, `traceability`, `spec_type`,
   `schema_version`, `description_sha256`.
2. `prepare_review.py` has good deterministic gates (objects, fields, version and
   group example coverage, placeholder-aware review stamping) but does not yet
   produce `decomposition.json` or `evidence.json`, and gates do not yet assert
   EARS form, requirement-test closure, or traceability completeness.
3. `plan_design.py` now fills data-flow steps deterministically, but `DESIGN.md`
   still contains `(to be filled)` markers in Error Handling and Extension Points —
   these must be eliminated to satisfy `no_placeholders_remaining`.
4. `breakdown_tasks.py` already builds dependency waves (aligned with Kiro/Spec
   Kit) but tasks are not yet linked to `REQ-NNN`/`AT-NNN` ids.
5. `clarify.py` uses regex heuristics; it must be upgraded to the evidence-aware,
   options-plus-impact format (Section 8) and only fire after the search chain.
6. `quality_check.py` covers AL analyzer baselining (post-implementation) and is
   complementary; the new gates are spec-time (pre-implementation) and live in
   `prepare_review.py`.

Concrete first-build order to make the machine spec bulletproof:

1. Introduce `schema_version` + `description_sha256` + `spec_type` in `write_spec`.
2. Add EARS `requirements[]` and `user_stories[]` derivation from decomposition.
3. Add `data_model[]` with read/update flags and `traceability` maps.
4. Add `evidence[]` and `assumptions[]` and wire `evidence_refs` everywhere.
5. Add `acceptance_tests[]` and enforce requirement-test closure.
6. Eliminate all `(to be filled)` markers in `DESIGN.md`.
7. Add the new gates (Section 5.1 items 11-18) and fail closed.
8. Add golden/replay tests proving stable ids and identical reruns.

## 17. References

1. EARS — Easy Approach to Requirements Syntax: `https://alistairmavin.com/ears/`
2. GitHub Spec Kit: `https://github.com/github/spec-kit`
3. AWS Kiro specs: `https://kiro.dev/docs/specs/`
