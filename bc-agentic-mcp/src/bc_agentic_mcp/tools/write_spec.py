"""bc_write_spec — generate TDD and machine spec from human bullets.
See spec Section 3.4.
"""
import json
from pathlib import Path
from typing import Dict, Any, Optional

from bc_agentic_mcp.validation import validate_spec_name, validate_idempotency_key


async def handle_write_spec(
    project_root: str,
    spec_name: str,
    human_bullets: str,
    analysis: Optional[str] = None,
    clarifications: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    template: str = "tdd",
) -> Dict[str, Any]:
    """Generate TDD and machine spec from human bullets.

    For V1, generates a scaffold that the model fills in; the structure and
    file locations are server-enforced.
    """
    validate_spec_name(spec_name)
    validate_idempotency_key(idempotency_key)

    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name
    specs_dir.mkdir(parents=True, exist_ok=True)

    # Idempotency
    idem_dir = root / ".specs" / ".idempotency"
    idem_dir.mkdir(parents=True, exist_ok=True)
    idem_key_path = idem_dir / f"{spec_name}.key"
    if idem_key_path.exists():
        stored = idem_key_path.read_text().strip()
        if stored == idempotency_key:
            tdd_path = specs_dir / "TDD.md"
            spec_path = specs_dir / "spec.json"
            if tdd_path.exists() and spec_path.exists():
                return {
                    "tdd_path": str(tdd_path),
                    "machine_spec_path": str(spec_path),
                    "summary": {"title": spec_name, "status": "already exists (idempotent)"},
                    "references": [],
                }

    idem_key_path.write_text(idempotency_key)

    if not analysis:
        analysis_path = specs_dir / "analysis.md"
        if analysis_path.exists():
            analysis = analysis_path.read_text(encoding="utf-8")

    tdd_content = f"""# Technical Design Document: {spec_name}

## 1. Overview
**Goal:** (to be filled by AI model from human bullets)
**Scope:** (to be filled by AI model)

### Human Requirements
{human_bullets}

### Clarifications
{clarifications or 'None'}

## 2. Key Decisions
(to be filled by AI model — architecture choices and rationale)

## 3. Objects
(to be filled by AI model — tables, pages, codeunits, extensions)

## 4. Data Model
(to be filled by AI model — table structures, field purposes, relationships)

## 5. Business Logic
(to be filled by AI model — process flow, rules, edge cases)

## 6. Integration Points
(to be filled by AI model — event subscribers, API endpoints, job queues)

## 7. Upgrade Considerations
(to be filled by AI model — data migration, breaking changes)

## 8. Testing Strategy
(to be filled by AI model — what to test, test scenarios)
"""
    tdd_path = specs_dir / "TDD.md"
    tdd_path.write_text(tdd_content, encoding="utf-8")

    spec_json = {
        "spec_name": spec_name,
        "version": 1,
        "module": root.name,
        "objects_to_create": [],
        "objects_to_modify": [],
        "business_rules": [],
        "event_subscribers": [],
        "scope_boundaries": {
            "allowed_extensions": [root.name],
            "allowed_files": [],
            "forbidden_patterns": [],
        },
        "references": [],
    }
    spec_path = specs_dir / "spec.json"
    spec_path.write_text(json.dumps(spec_json, indent=2))

    # Register with state manager so bc_status can track it
    from bc_agentic_mcp.state import StateManager
    sm = StateManager(root / ".specs")
    sm.init()  # ensures state.json exists
    try:
        sm.get_spec(spec_name)
    except KeyError:
        sm.add_spec(spec_name, template or "tdd")

    return {
        "tdd_path": str(tdd_path),
        "machine_spec_path": str(spec_path),
        "summary": {"title": spec_name},
        "references": [],
    }
