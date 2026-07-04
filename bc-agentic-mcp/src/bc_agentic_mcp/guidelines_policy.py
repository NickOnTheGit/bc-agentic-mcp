"""guidelines_policy — enforce local coding guidelines as executable checks.

The Azure Wiki page is private/authenticated in many environments, so enforcement
is local-first:
  1) Built-in baseline rules (safe defaults)
  2) Optional override file at .specs/policy/coding_guidelines.json

Rule schema in override file:
{
  "enabled": true,
  "source": {"url": "..."},
  "rules": [
    {
      "id": "GL-9001",
      "description": "No TODO markers",
      "severity": "warning",
      "pattern": "(?i)\\bTODO\\b",
      "include": ["**/*.al"],
      "exclude": ["**/test/**"]
    }
  ]
}
"""
from __future__ import annotations

import bisect
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from bc_agentic_mcp.workspace import external_base, specs_root


DEFAULT_POLICY: Dict[str, Any] = {
    "enabled": True,
    "source": {
        "url": "https://dev.azure.com/cegekadsa/DynamicsEmpire/_wiki/wikis/DynamicsEmpire.wiki/4167/Guidelines",
        "note": "Export this private wiki to .specs/policy/coding_guidelines.json to enforce exact team rules.",
    },
    "rules": [
        {
            "id": "GL-0001",
            "description": "Do not leave TODO/FIXME/HACK markers in production AL code.",
            "severity": "warning",
            "pattern": r"(?i)\\b(TODO|FIXME|HACK|XXX)\\b",
            "include": ["**/*.al"],
            "exclude": [],
        },
        {
            "id": "GL-0002",
            "description": "Do not commit merge conflict markers.",
            "severity": "error",
            "pattern": r"^(<<<<<<<|=======|>>>>>>>).*$",
            "include": ["**/*"],
            "exclude": [],
        },
    ],
}

DEFAULT_MAX_SCAN_BYTES = 50 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
MAX_TOTAL_FINDINGS = 50000


def _policy_dir(project_root: Path) -> Path:
    candidates: List[Path] = [
        project_root / ".specs" / "policy",
        specs_root(project_root) / "policy",
    ]
    base = external_base()
    if base is not None:
        candidates.append(base.parent / ".specs" / "policy")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return project_root / ".specs" / "policy"


def _policy_path(project_root: Path) -> Path:
    return _policy_dir(project_root) / "coding_guidelines.json"


def _manifest_path(project_root: Path) -> Path:
    return _policy_dir(project_root) / "guideline_rule_manifest.json"


def load_policy(project_root: Path) -> Dict[str, Any]:
    path = _policy_path(project_root)
    if not path.exists():
        return dict(DEFAULT_POLICY)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_POLICY)
    if not isinstance(data, dict) or "rules" not in data:
        return dict(DEFAULT_POLICY)
    return data


def _iter_candidate_files(project_root: Path, includes: Iterable[str]) -> Iterable[Path]:
    seen = set()
    for pattern in includes:
        patterns = [pattern]
        if "/**/" in pattern:
            patterns.append(pattern.replace("/**/", "/"))
        for pat in patterns:
            for p in project_root.glob(pat):
                if p.is_file():
                    r = p.resolve()
                    if r not in seen:
                        seen.add(r)
                        yield p


def _is_excluded(path: Path, project_root: Path, excludes: Iterable[str]) -> bool:
    rel = str(path.relative_to(project_root)).replace("\\", "/")
    for pat in excludes:
        if Path(rel).match(pat):
            return True
    return False


def _finding(code: str, message: str, severity: str, file: str, line: int) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "severity": severity,
        "sourceLocation": {"file": file, "line": line},
    }


def _load_json_if_exists(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_read_text(path: Path, max_bytes: int, read_stats: Optional[Dict[str, int]] = None) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            if read_stats is not None:
                read_stats["skipped_too_large"] = read_stats.get("skipped_too_large", 0) + 1
            return None
    except OSError:
        if read_stats is not None:
            read_stats["read_errors"] = read_stats.get("read_errors", 0) + 1
        return None

    chunks: List[str] = []
    total_bytes = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            while True:
                chunk = handle.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk.encode("utf-8", errors="ignore"))
                if total_bytes > max_bytes:
                    return None
    except OSError:
        if read_stats is not None:
            read_stats["read_errors"] = read_stats.get("read_errors", 0) + 1
        return None
    except MemoryError:
        if read_stats is not None:
            read_stats["memory_errors"] = read_stats.get("memory_errors", 0) + 1
        return None

    return "".join(chunks)


def _newline_offsets(content: str) -> List[int]:
    return [m.start() for m in re.finditer(r"\n", content)]


def _line_from_offset(newline_offsets: List[int], index: int) -> int:
    return bisect.bisect_right(newline_offsets, index) + 1


def _normalize_analyzer_token(token: str) -> str:
    return token.strip().strip("${}").lower()


def _load_rule_manifest(project_root: Path) -> Dict[str, Any]:
    path = _manifest_path(project_root)
    return _load_json_if_exists(path)


def _manifest_defined_codes(manifest: Dict[str, Any]) -> Set[str]:
    codes: Set[str] = set()
    for entry in manifest.get("rules", []) or []:
        if isinstance(entry, dict):
            code = str(entry.get("code") or "").strip()
            if code:
                codes.add(code)
    return codes


def _normalize_rel_path(path_text: str) -> str:
    return str(path_text).replace("\\", "/").lstrip("./")


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _load_spec_for_scan(project_root: Path, spec_name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not spec_name:
        return None
    spec_path = specs_root(project_root) / spec_name / "spec.json"
    if not spec_path.exists():
        return None
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _allowed_files_from_spec(spec: Optional[Dict[str, Any]]) -> Set[str]:
    if not spec:
        return set()
    allowed = spec.get("scope_boundaries", {}).get("allowed_files", [])
    out: Set[str] = set()
    for p in allowed or []:
        if isinstance(p, str) and p.strip():
            out.add(_normalize_rel_path(p))
    return out


def _is_relevant_path(rel: str, allowed_files: Set[str]) -> bool:
    if not allowed_files:
        return True
    r = _normalize_rel_path(rel)
    if r in allowed_files:
        return True
    return any(r.endswith(a) or a.endswith(r) for a in allowed_files)


def _requires_unit_tests(spec: Optional[Dict[str, Any]]) -> bool:
    if not spec:
        return True
    work_types = [str(w).lower() for w in (spec.get("work_types") or [])]
    if any(w in {"table", "table-field", "tableextension", "page", "codeunit", "api"} for w in work_types):
        return True
    for bucket in ("objects_to_create", "objects_to_modify"):
        for obj in spec.get(bucket, []) or []:
            kind = str((obj or {}).get("type", "")).lower()
            if kind in {"table", "tableextension", "page", "codeunit", "api"}:
                return True
    return False


def _scan_builtins(
    project_root: Path,
    spec: Optional[Dict[str, Any]] = None,
    read_stats: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    allowed_files = _allowed_files_from_spec(spec)
    policy_dir = _policy_dir(project_root)

    # Coverage contract: every synced guideline page must be explicitly enforced
    # either by machine checks or by approved manual review.
    sync_report_path = policy_dir / "guidelines_sync_report.json"
    coverage_contract_path = policy_dir / "guidelines_coverage_contract.json"
    if sync_report_path.exists():
        sync = _load_json_if_exists(sync_report_path)
        sync_pages = [str(p.get("path")) for p in (sync.get("pages") or []) if p.get("path")]
        if not coverage_contract_path.exists():
            findings.append(
                _finding(
                    "GL-COV001",
                    "Missing guidelines coverage contract (.specs/policy/guidelines_coverage_contract.json).",
                    "error",
                    ".specs/policy/guidelines_coverage_contract.json",
                    1,
                )
            )
        else:
            contract = _load_json_if_exists(coverage_contract_path)
            machine = set(str(x) for x in (contract.get("machine_enforced_pages") or []))
            manual_entries = contract.get("manual_review_pages") or []
            manual = set()
            unapproved_manual = []
            for entry in manual_entries:
                if isinstance(entry, dict):
                    p = str(entry.get("path") or "")
                    if p:
                        manual.add(p)
                        if not bool(entry.get("approved", False)):
                            unapproved_manual.append(p)
                elif isinstance(entry, str):
                    manual.add(entry)

            covered = machine | manual
            missing = sorted(p for p in sync_pages if p not in covered)
            if missing:
                findings.append(
                    _finding(
                        "GL-COV001",
                        f"Coverage contract missing {len(missing)} guideline page(s).",
                        "error",
                        ".specs/policy/guidelines_coverage_contract.json",
                        1,
                    )
                )
            if unapproved_manual:
                findings.append(
                    _finding(
                        "GL-COV002",
                        f"Manual review pages not approved: {len(unapproved_manual)}.",
                        "error",
                        ".specs/policy/guidelines_coverage_contract.json",
                        1,
                    )
                )

    # ERP Guidelines/Code Analyzers: CodeCop + UICop are mandatory.
    settings_rel = ".vscode/settings.json"
    if _is_relevant_path(settings_rel, allowed_files):
        settings_path = project_root / ".vscode" / "settings.json"
        settings = _load_json_if_exists(settings_path)
        analyzers = settings.get("al.codeAnalyzers", []) if isinstance(settings, dict) else []
        normalized = {_normalize_analyzer_token(str(a)) for a in analyzers if str(a).strip()}
        if not ({"codecop", "uicop"} <= normalized):
            findings.append(
                _finding(
                    "GL-AN001",
                    "Mandatory AL analyzers missing: configure both CodeCop and UICop in .vscode/settings.json (al.codeAnalyzers).",
                    "error",
                    settings_rel,
                    1,
                )
            )

    # ERP Guidelines/Affixes: when AppSourceCop is used, mandatoryAffixes must be present.
    appsource_path = project_root / "AppSourceCop.json"
    appsource = _load_json_if_exists(appsource_path)
    if appsource:
        mandatory_affixes = appsource.get("mandatoryAffixes", [])
        ok_affixes = (
            isinstance(mandatory_affixes, list)
            and any(isinstance(a, str) and len(a.strip()) >= 3 for a in mandatory_affixes)
        )
        if not ok_affixes:
            findings.append(
                _finding(
                    "GL-AF001",
                    "AppSourceCop.json must define mandatoryAffixes with at least one affix of length >= 3.",
                    "error",
                    "AppSourceCop.json",
                    1,
                )
            )

    # ERP Guidelines/Object subfolders: extension objects should live in underscore folders.
    path_rules = [
        ("*.TableExt.al", "/_TableExtensions/", "GL-SF001"),
        ("*.PageExt.al", "/_PageExtensions/", "GL-SF002"),
        ("*.EnumExt.al", "/_EnumExtensions/", "GL-SF003"),
        ("*.ReportExt.al", "/_ReportExtensions/", "GL-SF004"),
        ("*.PermissionSetExt.al", "/_PermissionsSetExtensions/", "GL-SF005"),
    ]
    for glob_pat, required_segment, code in path_rules:
        for fp in project_root.glob(f"**/{glob_pat}"):
            rel = str(fp.relative_to(project_root)).replace("\\", "/")
            if not _is_relevant_path(rel, allowed_files):
                continue
            if required_segment.lower() not in f"/{rel.lower()}/":
                findings.append(
                    _finding(
                        code,
                        f"{glob_pat} files should be placed under {required_segment.strip('/')} according to Object subfolders guideline.",
                        "warning",
                        rel,
                        1,
                    )
                )

    # ERP Guidelines/Access modifiers: new codeunits should explicitly set Access and procedures should be local by default.
    for fp in project_root.glob("**/*.Codeunit.al"):
        rel = str(fp.relative_to(project_root)).replace("\\", "/")
        if not _is_relevant_path(rel, allowed_files):
            continue
        content = _safe_read_text(fp, DEFAULT_MAX_SCAN_BYTES, read_stats)
        if content is None:
            continue
        line_offsets = _newline_offsets(content)
        if not re.search(r"(?im)^\s*access\s*=\s*(internal|public)\s*;", content):
            findings.append(
                _finding(
                    "GL-AC001",
                    "Codeunit should define explicit Access (prefer internal by default).",
                    "warning",
                    rel,
                    1,
                )
            )
        for m in re.finditer(r"(?im)^\s*procedure\s+[A-Za-z0-9_]+\s*\(", content):
            if len(findings) >= MAX_TOTAL_FINDINGS:
                break
            line = _line_from_offset(line_offsets, m.start())
            findings.append(
                _finding(
                    "GL-AC002",
                    "Use local/internal/protected procedures unless wider visibility is explicitly required.",
                    "warning",
                    rel,
                    line,
                )
            )

    return findings


def _requires_data_model_approval(spec: Dict[str, Any]) -> bool:
    work_types = [str(w).lower() for w in (spec.get("work_types") or [])]
    dm_markers = {
        "table",
        "table-field",
        "tableextension",
        "enum",
        "enumextension",
    }
    if any(w in dm_markers for w in work_types):
        return True

    if spec.get("data_model"):
        return True

    for bucket in ("objects_to_create", "objects_to_modify"):
        for obj in spec.get(bucket, []) or []:
            kind = str((obj or {}).get("type", "")).lower()
            if kind in {"table", "tableextension", "enum", "enumextension"}:
                return True
    return False


def _scan_data_model_approval(project_root: Path, spec_name: str | None) -> List[Dict[str, Any]]:
    if not spec_name:
        return []

    sdir = specs_root(project_root) / spec_name
    spec_path = sdir / "spec.json"
    if not spec_path.exists():
        return []

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []

    if not _requires_data_model_approval(spec):
        return []

    approval_path = sdir / "data_model_approval.json"
    if not approval_path.exists():
        return [
            _finding(
                "GL-DM001",
                "Data model approval required for schema-affecting changes (missing "
                "data_model_approval.json). Team process: create a Task under the PBI and "
                "have ANOTHER developer sign off the schema change (record via "
                "bc_approve_data_model) — the PR cannot merge until granted.",
                "warning",
                _display_path(approval_path, project_root),
                1,
            )
        ]

    approval = _load_json_if_exists(approval_path)
    if not bool(approval.get("approved", False)):
        return [
            _finding(
                "GL-DM001",
                "Data model approval required: data_model_approval.json exists but approved=false.",
                "warning",
                _display_path(approval_path, project_root),
                1,
            )
        ]

    return []


def scan(project_root: Path, spec_name: str | None = None) -> List[Dict[str, Any]]:
    policy = load_policy(project_root)
    if not policy.get("enabled", True):
        return []

    spec = _load_spec_for_scan(project_root, spec_name)
    allowed_files = _allowed_files_from_spec(spec)
    max_scan_bytes = DEFAULT_MAX_SCAN_BYTES
    findings: List[Dict[str, Any]] = []
    read_stats: Dict[str, int] = {"skipped_too_large": 0, "read_errors": 0, "memory_errors": 0}
    scan_capped = False
    findings.extend(_scan_builtins(project_root, spec=spec, read_stats=read_stats))
    findings.extend(_scan_data_model_approval(project_root, spec_name))
    for rule in policy.get("rules", []):
        rid = str(rule.get("id") or "GL-UNKNOWN")
        sev = str(rule.get("severity") or "warning").lower()
        description = str(rule.get("description") or "Guideline violation")
        pattern = str(rule.get("pattern") or "")
        includes = rule.get("include") or ["**/*.al"]
        excludes = rule.get("exclude") or []
        if not pattern:
            continue
        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error:
            continue

        for fp in _iter_candidate_files(project_root, includes):
            # SCOPE FIRST: when the spec fences the change to specific files, never
            # read outside that fence — this loop was reading 27k+ files per rule.
            # (string-only check, before _is_excluded's realpath syscalls)
            rel_for_scope = str(fp.relative_to(project_root)).replace("\\", "/")
            if not _is_relevant_path(rel_for_scope, allowed_files):
                continue
            if _is_excluded(fp, project_root, excludes):
                continue
            content = _safe_read_text(fp, max_scan_bytes, read_stats)
            if content is None:
                continue

            line_offsets = _newline_offsets(content)

            for m in regex.finditer(content):
                if len(findings) >= MAX_TOTAL_FINDINGS:
                    break
                line = _line_from_offset(line_offsets, m.start())
                findings.append(
                    {
                        "code": rid,
                        "message": description,
                        "severity": sev,
                        "sourceLocation": {
                            "file": str(fp.relative_to(project_root)).replace("\\", "/"),
                            "line": line,
                        },
                    }
                )

            if len(findings) >= MAX_TOTAL_FINDINGS:
                findings.append(
                    _finding(
                        "GL-SCAN001",
                        "Guideline findings were capped to protect memory; narrow scope or fix high-volume violations.",
                        "warning",
                        ".specs/policy/coding_guidelines.json",
                        1,
                    )
                )
                scan_capped = True
                break

        if scan_capped:
            break

    # Rule manifest contract: every emitted rule should be declared with an applicability predicate.
    manifest = _load_rule_manifest(project_root)
    manifest_path = _manifest_path(project_root)
    policy_path = _policy_path(project_root)
    rules = manifest.get("rules", []) if isinstance(manifest, dict) else []
    manifest_ready = bool(isinstance(rules, list) and rules)
    enforce_manifest = policy_path.exists() or manifest_path.exists()
    if enforce_manifest and not manifest_ready:
        findings.append(
            _finding(
                "GL-RM001",
                "Missing/invalid guideline rule manifest (.specs/policy/guideline_rule_manifest.json).",
                "error",
                _display_path(manifest_path, project_root),
                1,
            )
        )
    elif enforce_manifest:
        bad_entries = []
        for entry in rules:
            if not isinstance(entry, dict):
                bad_entries.append("<non-object-entry>")
                continue
            code = str(entry.get("code") or "").strip()
            appl = str(entry.get("applicability") or "").strip()
            if not code or not appl:
                bad_entries.append(code or "<missing-code>")
        if bad_entries:
            findings.append(
                _finding(
                    "GL-RM001",
                    f"Rule manifest has invalid entries (missing code/applicability): {', '.join(sorted(set(bad_entries)))}.",
                    "error",
                    _display_path(manifest_path, project_root),
                    1,
                )
            )

        defined_codes = _manifest_defined_codes(manifest)
        emitted_codes = {str(f.get("code", "")) for f in findings if f.get("code") != "GL-RM001"}
        undeclared = sorted(c for c in emitted_codes if c and c not in defined_codes)
        if undeclared:
            findings = [f for f in findings if f.get("code") in defined_codes or f.get("code") == "GL-RM001"]
            findings.append(
                _finding(
                    "GL-RM001",
                    f"Undeclared rule(s) emitted and blocked: {', '.join(undeclared)}.",
                    "error",
                    _display_path(manifest_path, project_root),
                    1,
                )
            )

    # ERP Guidelines/API Guidelines: API pages must declare required API properties.
    for fp in project_root.glob("**/*.al"):
        rel = str(fp.relative_to(project_root)).replace("\\", "/")
        if not _is_relevant_path(rel, allowed_files):
            continue
        content = _safe_read_text(fp, max_scan_bytes, read_stats)
        if content is None:
            continue

        if not re.search(r"(?im)^\s*pagetype\s*=\s*api\s*;", content):
            continue

        required_props = {
            "apipublisher": "GL-API001",
            "apigroup": "GL-API002",
            "apiversion": "GL-API003",
            "entityname": "GL-API004",
            "entitysetname": "GL-API005",
            "odatakeyfields": "GL-API006",
        }
        for prop, code in required_props.items():
            if not re.search(rf"(?im)^\s*{prop}\s*=\s*.+;", content):
                findings.append(
                    _finding(
                        code,
                        f"API page must define property '{prop}'.",
                        "error",
                        rel,
                        1,
                    )
                )

        m_pub = re.search(r"(?im)^\s*apipublisher\s*=\s*'([^']+)'\s*;", content)
        if m_pub:
            publisher = m_pub.group(1).strip().lower()
            if publisher not in {"cegeka", "vera"}:
                findings.append(
                    _finding(
                        "GL-API007",
                        "APIPublisher should be 'cegeka' (Empire APIs) or 'vera' (VERA APIs).",
                        "warning",
                        rel,
                        1,
                    )
                )

        m_ver = re.search(r"(?im)^\s*apiversion\s*=\s*(.+);", content)
        if m_ver:
            version_text = m_ver.group(1)
            if not re.search(r"'beta'|'v\d+\.\d+'", version_text, re.IGNORECASE):
                findings.append(
                    _finding(
                        "GL-API008",
                        "APIVersion should contain 'beta' and/or semantic version values like 'v1.0'.",
                        "warning",
                        rel,
                        1,
                    )
                )

        for prop, code in (("entityname", "GL-API009"), ("entitysetname", "GL-API010")):
            m_name = re.search(rf"(?im)^\s*{prop}\s*=\s*'([^']+)'\s*;", content)
            if not m_name:
                continue
            value = m_name.group(1).strip()
            if not re.fullmatch(r"[a-z][A-Za-z0-9]*", value):
                findings.append(
                    _finding(
                        code,
                        f"{prop} should follow lower camelCase naming.",
                        "warning",
                        rel,
                        1,
                    )
                )

        # VERA APIs are expected to be placed under a VERA folder structure.
        if m_pub and m_pub.group(1).strip().lower() == "vera":
            if "/vera/" not in rel.lower():
                findings.append(
                    _finding(
                        "GL-API011",
                        "VERA API page should be located under a VERA folder path.",
                        "warning",
                        rel,
                        1,
                    )
                )

        # API page convention: DelayedInsert and SourceTable should be explicitly set.
        if not re.search(r"(?im)^\s*delayedinsert\s*=\s*true\s*;", content):
            findings.append(
                _finding(
                    "GL-API012",
                    "API page should set DelayedInsert = true.",
                    "warning",
                    rel,
                    1,
                )
            )
        if not re.search(r"(?im)^\s*sourcetable\s*=\s*.+;", content):
            findings.append(
                _finding(
                    "GL-API013",
                    "API page must define SourceTable (driving table).",
                    "error",
                    rel,
                    1,
                )
            )

    # API permission-set coverage: when API pages exist, expect at least one API permission set.
    api_pages = []
    for fp in project_root.glob("**/*.al"):
        rel = str(fp.relative_to(project_root)).replace("\\", "/")
        if not _is_relevant_path(rel, allowed_files):
            continue
        content = _safe_read_text(fp, max_scan_bytes, read_stats)
        if content is None:
            continue
        if re.search(r"(?im)^\s*pagetype\s*=\s*api\s*;", content):
            api_pages.append(fp)

    if api_pages:
        perm_files = list(project_root.glob("**/*.PermissionSet.al")) + list(project_root.glob("**/*.permissionset.al"))
        has_api_permset = False
        for pf in perm_files:
            ptxt = _safe_read_text(pf, max_scan_bytes, read_stats)
            if ptxt is None:
                continue
            if re.search(r"(?i)\bAPI[-_A-Za-z0-9]*\b", ptxt):
                has_api_permset = True
                break
        if not has_api_permset:
            rel0 = str(api_pages[0].relative_to(project_root)).replace("\\", "/")
            findings.append(
                _finding(
                    "GL-API014",
                    "API guideline: expected API permission set entries (e.g. API-* permission set).",
                    "warning",
                    rel0,
                    1,
                )
            )

    # ERP Guidelines/DataClassifications: table/tableextension field blocks should define DataClassification.
    for fp in project_root.glob("**/*.al"):
        rel = str(fp.relative_to(project_root)).replace("\\", "/")
        if not _is_relevant_path(rel, allowed_files):
            continue
        lower_rel = rel.lower()
        if not (lower_rel.endswith(".table.al") or lower_rel.endswith(".tableext.al")):
            continue
        content = _safe_read_text(fp, max_scan_bytes, read_stats)
        if content is None:
            continue
        line_offsets = _newline_offsets(content)

        for m in re.finditer(r"(?is)field\([^)]*\)\s*\{(.*?)\}", content):
            block = m.group(1)
            if not re.search(r"(?im)^\s*dataclassification\s*=\s*.+;", block):
                if len(findings) >= MAX_TOTAL_FINDINGS:
                    break
                line = _line_from_offset(line_offsets, m.start())
                findings.append(
                    _finding(
                        "GL-DC001",
                        "Table/tableextension fields should define DataClassification.",
                        "warning",
                        rel,
                        line,
                    )
                )

    # ERP Guidelines/Unit testing: repository with source AL should include at least one test AL file.
    src_al = [
        fp for fp in project_root.glob("src/**/*.al")
        if _is_relevant_path(str(fp.relative_to(project_root)).replace("\\", "/"), allowed_files)
    ]
    test_al = list(project_root.glob("tests/**/*.al")) + list(project_root.glob("test/**/*.al"))
    test_al += list(project_root.glob("src/**/test/**/*.al")) + list(project_root.glob("src/**/tests/**/*.al"))
    if _requires_unit_tests(spec) and src_al and not test_al:
        findings.append(
            _finding(
                "GL-UT001",
                "Unit testing guideline: no AL test files found (expected tests/**/*.al or src/**/test/**/*.al).",
                "warning",
                "src",
                1,
            )
        )

    # Bad habits + performance + secure coding + namespace + cleanup heuristics.
    for fp in project_root.glob("src/**/*.al"):
        rel = str(fp.relative_to(project_root)).replace("\\", "/")
        if not _is_relevant_path(rel, allowed_files):
            continue
        content = _safe_read_text(fp, max_scan_bytes, read_stats)
        if content is None:
            continue
        line_offsets = _newline_offsets(content)

        # Namespaces guideline.
        if not re.search(r"(?im)^\s*namespace\s+[A-Za-z0-9_.]+\s*;", content):
            findings.append(
                _finding(
                    "GL-NS001",
                    "Namespace guideline: AL source files should declare a namespace.",
                    "warning",
                    rel,
                    1,
                )
            )

        # Code cleanup guideline: trailing whitespace should not be committed.
        for m in re.finditer(r"(?m)^.*[ \t]+$", content):
            if len(findings) >= MAX_TOTAL_FINDINGS:
                break
            line = _line_from_offset(line_offsets, m.start())
            findings.append(
                _finding(
                    "GL-CR002",
                    "Code cleanup: remove trailing whitespace.",
                    "warning",
                    rel,
                    line,
                )
            )

        # Bad habits: avoid table/field triggers in tableextension when possible.
        if rel.lower().endswith(".tableext.al"):
            for m in re.finditer(r"(?im)^\s*trigger\s+On[A-Za-z0-9_]+\s*\(", content):
                if len(findings) >= MAX_TOTAL_FINDINGS:
                    break
                line = _line_from_offset(line_offsets, m.start())
                findings.append(
                    _finding(
                        "GL-BH001",
                        "Bad habits: avoid heavy logic in tableextension triggers; prefer dedicated codeunits/events.",
                        "warning",
                        rel,
                        line,
                    )
                )

        # Bad habits: prefer safer existence checks over broad FIND('-')/FIND('+').
        for m in re.finditer(r"(?im)\.find\s*\(\s*'[-+]'\s*\)", content):
            if len(findings) >= MAX_TOTAL_FINDINGS:
                break
            line = _line_from_offset(line_offsets, m.start())
            findings.append(
                _finding(
                    "GL-BH002",
                    "Bad habits: avoid FIND('-'/'+' ) patterns when clearer retrieval methods apply.",
                    "warning",
                    rel,
                    line,
                )
            )

        # Bad habits: avoid overusing TestField as control flow.
        for m in re.finditer(r"(?im)\btestfield\s*\(", content):
            if len(findings) >= MAX_TOTAL_FINDINGS:
                break
            line = _line_from_offset(line_offsets, m.start())
            findings.append(
                _finding(
                    "GL-BH003",
                    "Bad habits: review TestField usage and prefer explicit validation/error handling where clearer.",
                    "warning",
                    rel,
                    line,
                )
            )

        # Bad habits: Today vs WorkDate guideline.
        for m in re.finditer(r"(?im)\btoday\s*\(", content):
            if len(findings) >= MAX_TOTAL_FINDINGS:
                break
            line = _line_from_offset(line_offsets, m.start())
            findings.append(
                _finding(
                    "GL-BH004",
                    "Bad habits: review Today() usage; prefer WorkDate where business date is required.",
                    "warning",
                    rel,
                    line,
                )
            )

        # Secure coding: hardcoded secrets/tokens/password-like assignments.
        for m in re.finditer(r"(?im)\b(password|passwd|apikey|api_key|token|secret)\b\s*(?::=|=|:)\s*'[^']+'", content):
            if len(findings) >= MAX_TOTAL_FINDINGS:
                break
            line = _line_from_offset(line_offsets, m.start())
            findings.append(
                _finding(
                    "GL-SC001",
                    "Secure coding: avoid hardcoded secrets/tokens/password values.",
                    "error",
                    rel,
                    line,
                )
            )

    # ERP Guidelines/Commit message + Code review/Pull Requests:
    # validate latest git commit subject shape when repository is git-backed.
    git_dir = project_root / ".git"
    if git_dir.exists():
        try:
            subject = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,  # a stalled git (hooks/credential helpers) must NEVER hang the scan
                stdin=subprocess.DEVNULL,  # never inherit the MCP stdio pipe
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            subject = ""

        if subject:
            if len(subject) > 50:
                findings.append(
                    _finding(
                        "GL-CM001",
                        "Commit subject should be <= 50 characters.",
                        "warning",
                        ".git/COMMIT_EDITMSG",
                        1,
                    )
                )
            if subject.endswith("."):
                findings.append(
                    _finding(
                        "GL-CM002",
                        "Commit subject should not end with a period.",
                        "warning",
                        ".git/COMMIT_EDITMSG",
                        1,
                    )
                )
            if subject and subject[0].isalpha() and subject[0] != subject[0].upper():
                findings.append(
                    _finding(
                        "GL-CM003",
                        "Commit subject should start with a capital letter.",
                        "warning",
                        ".git/COMMIT_EDITMSG",
                        1,
                    )
                )

            if not re.match(r"^[A-Za-z0-9._/-]+-.+", subject):
                findings.append(
                    _finding(
                        "GL-PR001",
                        "Title should follow '{target branch}-{short description}' format (Pull Request guideline).",
                        "warning",
                        ".git/COMMIT_EDITMSG",
                        1,
                    )
                )

    # Wave 4 (manual governance): require explicit acknowledgement for human-only guideline pages.
    human_gate = _policy_dir(project_root) / "human_review_gate.json"
    if human_gate.exists():
        gate_data = _load_json_if_exists(human_gate)
        if not gate_data.get("approved", False):
            findings.append(
                _finding(
                    "GL-MN001",
                    "Human-only guideline gate is not approved. Update .specs/policy/human_review_gate.json after review.",
                    "error",
                    _display_path(human_gate, project_root),
                    1,
                )
            )
    else:
        findings.append(
            _finding(
                "GL-MN001",
                "Missing human-only guideline gate file (.specs/policy/human_review_gate.json).",
                "warning",
                _display_path(human_gate, project_root),
                1,
            )
        )

    skipped_too_large = read_stats.get("skipped_too_large", 0)
    read_errors = read_stats.get("read_errors", 0)
    memory_errors = read_stats.get("memory_errors", 0)
    if skipped_too_large or read_errors or memory_errors:
        findings.append(
            _finding(
                "GL-SCAN002",
                (
                    "Scan safety guard skipped some files "
                    f"(too_large={skipped_too_large}, read_errors={read_errors}, memory_errors={memory_errors}). "
                    "Increase scan limit or narrow scope if full-file coverage is required."
                ),
                "warning",
                ".specs/policy/coding_guidelines.json",
                1,
            )
        )

    return findings
