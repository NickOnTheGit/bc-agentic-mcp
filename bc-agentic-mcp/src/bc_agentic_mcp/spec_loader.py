"""Shared spec loading with existence guard."""
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple
from bc_agentic_mcp.errors import MCPError, ErrorCode


SUPPORTED_SCHEMA_VERSIONS = {"2.0"}
REQUIRED_TOP_LEVEL_BY_VERSION = {
    "2.0": {
        "schema_version",
        "spec_id",
        "spec_name",
        "summary",
        "requirements",
        "acceptance_tests",
        "objects_to_create",
        "objects_to_modify",
        "scope_boundaries",
        "traceability",
    }
}


def _normalize_rel_path(value: str) -> str:
    p = str(value or "").strip().replace("/", "\\")
    while "\\\\" in p:
        p = p.replace("\\\\", "\\")
    return p.lstrip("\\")


def _validate_schema_gate(spec: Dict[str, Any], strict_schema: bool = False) -> List[str]:
    issues: List[str] = []
    version = str(spec.get("schema_version") or "").strip()
    if not version:
        return ["schema_version is missing"] if strict_schema else []
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        return [f"unsupported schema_version '{version}' (supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"]

    if not strict_schema:
        return issues

    required = REQUIRED_TOP_LEVEL_BY_VERSION.get(version, set())
    missing = sorted(k for k in required if k not in spec)
    if missing:
        issues.append("missing required top-level fields: " + ", ".join(missing))
    return issues


def _validate_traceability_contract(spec: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    reqs = [r for r in (spec.get("requirements") or []) if isinstance(r, dict) and r.get("id")]
    ats = [a for a in (spec.get("acceptance_tests") or []) if isinstance(a, dict) and a.get("id")]
    req_ids = {str(r.get("id")) for r in reqs}
    at_ids = {str(a.get("id")) for a in ats}
    has_declared_objects = bool((spec.get("objects_to_create") or []) or (spec.get("objects_to_modify") or []))

    tr = spec.get("traceability") or {}
    req_to_test = tr.get("requirement_to_test") or {}
    req_to_obj = tr.get("requirement_to_object") or {}

    for rid in sorted(req_ids):
        mapped_tests = req_to_test.get(rid)
        if not isinstance(mapped_tests, list) or not mapped_tests:
            issues.append(f"traceability.requirement_to_test missing or empty for {rid}")
        else:
            bad_tests = [t for t in mapped_tests if str(t) not in at_ids]
            if bad_tests:
                issues.append(f"traceability.requirement_to_test for {rid} references unknown tests: {bad_tests}")

        mapped_objs = req_to_obj.get(rid)
        if has_declared_objects and (not isinstance(mapped_objs, list) or not mapped_objs):
            issues.append(f"traceability.requirement_to_object missing or empty for {rid}")

    return issues


def _validate_create_targets(spec: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    allowed = {_normalize_rel_path(p) for p in (spec.get("scope_boundaries", {}).get("allowed_files", []) or [])}

    for idx, obj in enumerate(spec.get("objects_to_create", []) or [], start=1):
        name = str(obj.get("name") or "").strip()
        target = str(obj.get("target") or "").strip()
        if not name:
            issues.append(f"objects_to_create[{idx}] is missing 'name'")
        if not target:
            issues.append(f"objects_to_create[{idx}] '{name or '?'}' is missing 'target'")
            continue

        normalized = _normalize_rel_path(target)
        if normalized != target.replace("/", "\\").lstrip("\\"):
            issues.append(f"objects_to_create[{idx}] target '{target}' is not canonicalized")
        if ".." in normalized.split("\\"):
            issues.append(f"objects_to_create[{idx}] target '{target}' contains parent traversal")
        if allowed and normalized not in allowed:
            issues.append(f"objects_to_create[{idx}] target '{target}' is not listed in scope_boundaries.allowed_files")

    return issues


def _requires_upgrade_contract(spec: Dict[str, Any]) -> bool:
    work_types = {str(w).lower() for w in (spec.get("work_types") or [])}
    if "upgrade" in work_types:
        return True
    for obj in (spec.get("objects_to_create") or []):
        subtype = str(obj.get("subtype") or "").lower()
        name = str(obj.get("name") or "").lower()
        target = str(obj.get("target") or "").lower()
        if "upgrade" in subtype or "upgrade" in name or "_upgrade" in target:
            return True
    return False


def _validate_upgrade_contract(spec: Dict[str, Any]) -> List[str]:
    if not _requires_upgrade_contract(spec):
        return []

    issues: List[str] = []
    # Multi-target shape: one contract PER upgrade codeunit (removals often need a
    # per-company cleanup AND a per-database cleanup — observed live on Bug 267600).
    contracts = spec.get("upgrade_contracts")
    if isinstance(contracts, list) and contracts:
        for idx, contract in enumerate(contracts):
            # ZERO-based index + codeunit name: the 1-based label sent a live debugging
            # session hunting a phantom second contract ('upgrade_contracts[1]' was the
            # FIRST and only entry — observed on Bug 267600 fix-10).
            who = str(contract.get("codeunit_target") or "").rsplit("\\", 1)[-1] or "?"
            issues.extend(_validate_one_upgrade_contract(
                contract, f"upgrade_contracts[{idx}] ({who})"))
        return issues
    contract = spec.get("upgrade_contract") or {}
    issues.extend(_validate_one_upgrade_contract(contract, "upgrade_contract"))
    return issues


def _validate_one_upgrade_contract(contract: Dict[str, Any], label: str) -> List[str]:
    issues: List[str] = []
    table_target = str(contract.get("table_target") or "").strip()
    data_per_company = contract.get("data_per_company")
    required_scope = str(contract.get("required_scope") or "").strip().lower()
    tag = str(contract.get("idempotency_tag") or "").strip()

    if not table_target:
        issues.append(f"{label}.table_target is required")
    if not isinstance(data_per_company, bool):
        issues.append(f"{label}.data_per_company must be boolean")
    if required_scope not in {"per-company", "per-database"}:
        issues.append(f"{label}.required_scope must be 'per-company' or 'per-database'")
    if not tag:
        issues.append(f"{label}.idempotency_tag is required")

    if isinstance(data_per_company, bool) and required_scope:
        expected = "per-company" if data_per_company else "per-database"
        if required_scope != expected:
            issues.append(
                f"{label}.required_scope does not match data_per_company "
                f"(expected {expected}, got {required_scope})"
            )
    return issues


def _validate_scope_boundaries(spec: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    scope = spec.get("scope_boundaries") or {}
    allowed_files = [
        _normalize_rel_path(p)
        for p in (scope.get("allowed_files") or [])
        if isinstance(p, str) and p.strip()
    ]
    allowed_extensions = [
        str(x).strip()
        for x in (scope.get("allowed_extensions") or [])
        if str(x).strip()
    ]
    scope_mode = str(scope.get("scope_mode") or "strict").strip().lower()

    if scope_mode not in {"strict", "permissive"}:
        issues.append("scope_boundaries.scope_mode must be 'strict' or 'permissive'")

    if allowed_extensions and allowed_files:
        first_parts = {
            f.split("\\", 1)[0]
            for f in allowed_files
            if "\\" in f
        }
        # Guardrail lesson: avoid impossible scope definitions like allowed_extensions=["ERP AL"]
        # while all allowed_files start with "extensions\\...".
        if first_parts and first_parts.isdisjoint(set(allowed_extensions)):
            issues.append(
                "scope_boundaries.allowed_extensions does not match allowed_files roots "
                f"(extensions={allowed_extensions}, roots={sorted(first_parts)})"
            )

    return issues


def validate_spec_contract(spec: Dict[str, Any], strict_schema: bool = False) -> List[str]:
    """Validate grounded-spec invariants required by planning and implementation.

    Current invariant set (fail-closed): when a spec is grounded, any created codeunit
    must have a concrete name and target path, and that target must be in allowed_files.
    """
    issues: List[str] = []
    issues.extend(_validate_schema_gate(spec, strict_schema=strict_schema))
    status = str(spec.get("status") or "grounded").lower()
    if status != "grounded":
        return issues

    if strict_schema:
        issues.extend(_validate_traceability_contract(spec))
        issues.extend(_validate_create_targets(spec))
        issues.extend(_validate_upgrade_contract(spec))
        issues.extend(_validate_scope_boundaries(spec))

    allowed_files = {_normalize_rel_path(p) for p in (spec.get("scope_boundaries", {}).get("allowed_files", []) or [])}
    for idx, obj in enumerate(spec.get("objects_to_create", []) or [], start=1):
        obj_type = str(obj.get("type", "")).lower()
        if obj_type != "codeunit":
            continue
        name = str(obj.get("name") or "").strip()
        target = _normalize_rel_path(str(obj.get("target") or "").strip())
        if not name:
            issues.append(f"objects_to_create[{idx}] codeunit is missing 'name'")
        if not target:
            issues.append(f"objects_to_create[{idx}] codeunit '{name or '?'}' is missing 'target'")
        elif allowed_files and target not in allowed_files:
            issues.append(
                f"objects_to_create[{idx}] codeunit target '{target}' is not listed in scope_boundaries.allowed_files"
            )
    return issues


def load_spec(specs_dir: Path) -> Dict[str, Any]:
    """Load spec.json, raising MCPError with guidance if missing."""
    spec_path = specs_dir / "spec.json"
    if not spec_path.exists():
        raise MCPError(
            ErrorCode.CLIENT_ERROR,
            f"spec.json not found at {spec_path}",
            hint="Run bc_write_spec first to create the specification.",
        )
    spec = json.loads(spec_path.read_text())
    issues = validate_spec_contract(spec)
    if issues:
        raise MCPError(
            ErrorCode.CLIENT_ERROR,
            "Invalid grounded spec contract: " + "; ".join(issues),
            hint=(
                "Regenerate spec so created codeunits include concrete target paths in "
                "scope_boundaries.allowed_files before planning or implementation."
            ),
        )
    return spec


def upgrade_contract_for_file(spec: Dict[str, Any], file_path: str) -> Tuple[bool, Dict[str, Any]]:
    """Return whether `file_path` is governed by the upgrade contract and the contract itself."""
    target = _normalize_rel_path(file_path)
    # Multi-target shape first: the contract that names THIS codeunit file governs it.
    for contract in (spec.get("upgrade_contracts") or []):
        c_target = _normalize_rel_path(str(contract.get("codeunit_target") or ""))
        if c_target and c_target == target:
            return True, contract
    contract = spec.get("upgrade_contract") or {}
    if not contract:
        return False, {}
    for obj in spec.get("objects_to_create", []) or []:
        o_target = _normalize_rel_path(str(obj.get("target") or ""))
        subtype = str(obj.get("subtype") or "").lower()
        if o_target and o_target == target and ("upgrade" in subtype or "upgrade" in o_target.lower()):
            return True, contract
    return False, {}


def validate_upgrade_code_against_contract(code: str, contract: Dict[str, Any]) -> List[str]:
    """Validate upgrade code text against required scope + idempotency tag contract."""
    issues: List[str] = []
    required_scope = str(contract.get("required_scope") or "").lower()
    tag = str(contract.get("idempotency_tag") or "").strip()

    if required_scope == "per-database":
        if not re.search(r"\bUpgradePerDatabase\w*\b", code or "", re.IGNORECASE):
            issues.append("upgrade code must implement UpgradePerDatabase* trigger")
    elif required_scope == "per-company":
        if not re.search(r"\bUpgradePerCompany\w*\b", code or "", re.IGNORECASE):
            issues.append("upgrade code must implement UpgradePerCompany* trigger")

    if tag and tag not in (code or ""):
        issues.append(f"upgrade idempotency tag '{tag}' is missing from code")
    return issues
