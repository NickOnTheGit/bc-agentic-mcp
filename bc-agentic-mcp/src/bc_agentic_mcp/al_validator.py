"""Self-contained AL static validator.

Provides deterministic AL diagnostics with NO external tool dependency, so the
MCP can validate Business Central AL standalone. When the real `alc` compiler or
the AL MCP backend is available, callers may merge its diagnostics on top of these.

Checks are intentionally low-false-positive and grounded in BC rules:
- V0001 (error): unbalanced braces in an object file.
- V0002 (error): object ID outside the app.json idRanges (AppSourceCop AS0013/AS0084).
- V0003 (error): duplicate object ID across files.
- V0061 (warning): API page missing ODataKeyFields = SystemId (ALCops LC0061).
- V0060 (info): ApplicationArea on an API page (ALCops LC0060).
- V0070 (warning): table field name exceeds 30 characters (AL0468 / SQL safety).
- V0100 (warning): a per-company upgrade (UpgradePerCompanySAN) modifies a shared
  DataPerCompany=false table — shared/database-scoped data must be upgraded per-database
  (UpgradePerDatabaseSAN), which runs once with no company guard.
- V0101 (warning): a per-database upgrade (UpgradePerDatabaseSAN) modifies a per-company
  DataPerCompany=true table — per-company data must be upgraded per-company.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

_OBJECT_HEADER = re.compile(
    r"(?im)^\s*(page|pageextension|table|tableextension|codeunit|enum|enumextension|report|query|xmlport|interface)"
    r'\s+(\d+)\s+("[^"]+"|[A-Za-z0-9_]+)'
)


def _load_id_ranges(root: Path) -> List[Dict[str, int]]:
    app_json = root / "app.json"
    if not app_json.exists():
        return []
    try:
        data = json.loads(app_json.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    ranges: List[Dict[str, int]] = []
    for r in data.get("idRanges") or []:
        if isinstance(r, dict) and "from" in r and "to" in r:
            ranges.append({"from": int(r["from"]), "to": int(r["to"])})
    single = data.get("idRange")
    if isinstance(single, dict) and "from" in single and "to" in single:
        ranges.append({"from": int(single["from"]), "to": int(single["to"])})
    return ranges


def _prop(content: str, name: str) -> Optional[str]:
    match = re.search(rf"(?im)^\s*{name}\s*=\s*([^;]+);", content)
    return match.group(1).strip() if match else None


def _diag(code: str, message: str, severity: str, file: str, line: int) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "severity": severity,
        "sourceLocation": {"file": file, "line": line},
    }


def _normalize_rel(path_text: str) -> str:
    return str(path_text).replace("\\", "/").lstrip("./")


def validate_project(
    project_root: Path,
    max_files: int = 2000,
    include_files: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Run self-contained AL static checks over the project's src/ tree."""
    root = Path(project_root)
    src_dir = root / "src"
    scan_dir = src_dir if src_dir.exists() else root

    id_ranges = _load_id_ranges(root)
    diagnostics: List[Dict[str, Any]] = []
    seen_ids: Dict[tuple, str] = {}
    # Cross-file upgrade-scope analysis (V0100/V0101): a shared (DataPerCompany=false)
    # table must be upgraded per-database; a per-company table per-company.
    table_data_per_company: Dict[str, bool] = {}
    upgrade_codeunits: List[Dict[str, Any]] = []

    if include_files:
        selected: Set[Path] = set()
        for rel in include_files:
            rel_norm = _normalize_rel(rel)
            if not rel_norm.lower().endswith(".al"):
                continue
            fp = (root / rel_norm).resolve()
            if fp.exists() and fp.is_file():
                selected.add(fp)
        files = sorted(selected)
    else:
        files = sorted(scan_dir.rglob("*.al"))[:max_files]
    for al_file in files:
        try:
            content = al_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(al_file.relative_to(root)) if root in al_file.parents else al_file.name

        # V0001: brace balance.
        opens = content.count("{")
        closes = content.count("}")
        if opens != closes:
            diagnostics.append(
                _diag(
                    "V0001",
                    f"Unbalanced braces ({opens} '{{' vs {closes} '}}').",
                    "error",
                    rel,
                    1,
                )
            )

        header = _OBJECT_HEADER.search(content)
        if not header:
            continue
        object_id = int(header.group(2))
        object_name = header.group(3).strip('"')
        object_kind = header.group(1).lower()
        line = content[: header.start()].count("\n") + 1

        # V0030: AL caps application object identifiers at 30 characters (AL0305).
        # Caught at write time so the 2-minute container compile never pays for it
        # (observed live: FacilitiesPerSpaceRegressionFDNT, 32 chars).
        if len(object_name) > 30:
            diagnostics.append(
                _diag(
                    "V0030",
                    f"Object identifier '{object_name}' is {len(object_name)} characters — "
                    "AL caps object names at 30 (AL0305).",
                    "error",
                    rel,
                    line,
                )
            )

        # Collect signals for the cross-file upgrade-scope check (V0100/V0101).
        if object_kind == "table":
            # BC default is DataPerCompany = true when the property is absent.
            _dpc = (_prop(content, "DataPerCompany") or "").strip().lower()
            table_data_per_company[object_name.lower()] = (_dpc != "false")
        elif object_kind == "codeunit":
            _impl = re.search(r"(?is)\bimplements\b(.*?)\{", content)
            _scopes = set()
            if _impl:
                if re.search(r"UpgradePerCompanySAN", _impl.group(1)):
                    _scopes.add("per-company")
                if re.search(r"UpgradePerDatabaseSAN", _impl.group(1)):
                    _scopes.add("per-database")
            if _scopes:
                _modified = set()
                for _perm in re.finditer(
                    r'(?im)tabledata\s+(?:"([^"]+)"|([A-Za-z_]\w*))\s*=\s*([A-Za-z]+)',
                    content,
                ):
                    _grant = (_perm.group(3) or "").lower()
                    if any(c in _grant for c in ("i", "m", "d")):
                        _modified.add((_perm.group(1) or _perm.group(2) or "").lower())
                upgrade_codeunits.append(
                    {"file": rel, "name": object_name, "line": line,
                     "scopes": _scopes, "modified": _modified}
                )

        # V0002: id range.
        if id_ranges and not any(r["from"] <= object_id <= r["to"] for r in id_ranges):
            diagnostics.append(
                _diag(
                    "V0002",
                    f"Object {object_name} (id {object_id}) is outside the app idRanges.",
                    "error",
                    rel,
                    line,
                )
            )

        # V0003: duplicate id within the same object type (AL ids are unique per type).
        key = (object_kind, object_id)
        if key in seen_ids:
            diagnostics.append(
                _diag(
                    "V0003",
                    f"Duplicate {object_kind} id {object_id}: {object_name} also in {seen_ids[key]}.",
                    "error",
                    rel,
                    line,
                )
            )
        else:
            seen_ids[key] = rel

        # API page checks.
        is_api = (header.group(1).lower() == "page") and (
            (_prop(content, "PageType") or "").strip().lower() == "api"
            or _prop(content, "EntityName") is not None
        )
        if is_api:
            odata = _prop(content, "ODataKeyFields") or ""
            if "systemid" not in odata.lower():
                diagnostics.append(
                    _diag(
                        "V0061",
                        f"API page {object_name} should set ODataKeyFields = SystemId (ALCops LC0061).",
                        "warning",
                        rel,
                        line,
                    )
                )
            if re.search(r"(?im)ApplicationArea\s*=", content):
                diagnostics.append(
                    _diag(
                        "V0060",
                        f"ApplicationArea is not applicable to API page {object_name} (ALCops LC0060).",
                        "info",
                        rel,
                        line,
                    )
                )

        # V0070: table field names must be <= 30 characters (AL0468 / SQL safety).
        if object_kind in ("table", "tableextension"):
            for match in re.finditer(
                r'(?im)^\s*field\(\s*\d+\s*;\s*(?:"([^"]+)"|([A-Za-z_]\w*))\s*;',
                content,
            ):
                field_name = match.group(1) or match.group(2) or ""
                if len(field_name) > 30:
                    field_line = content[: match.start()].count("\n") + 1
                    diagnostics.append(
                        _diag(
                            "V0070",
                            (
                                f"Table field name '{field_name}' exceeds 30 characters "
                                f"({len(field_name)}); AL0468 SQL-safety limit."
                            ),
                            "warning",
                            rel,
                            field_line,
                        )
                    )

    # Cross-file pass: reconcile upgrade scope against each modified table's DataPerCompany.
    # Only fires when the table is in the scanned set (avoids false positives on dependency tables).
    for cu in upgrade_codeunits:
        for tname in cu["modified"]:
            dpc = table_data_per_company.get(tname)
            if dpc is None:
                continue
            if dpc is False and "per-company" in cu["scopes"] and "per-database" not in cu["scopes"]:
                diagnostics.append(_diag(
                    "V0100",
                    (f"Upgrade codeunit {cu['name']} is per-company (UpgradePerCompanySAN) but modifies "
                     f"shared table {tname} (DataPerCompany = false). Shared/database-scoped data should be "
                     f"upgraded per-database (UpgradePerDatabaseSAN), which runs once with no company guard."),
                    "warning", cu["file"], cu["line"],
                ))
            elif dpc is True and "per-database" in cu["scopes"] and "per-company" not in cu["scopes"]:
                diagnostics.append(_diag(
                    "V0101",
                    (f"Upgrade codeunit {cu['name']} is per-database (UpgradePerDatabaseSAN) but modifies "
                     f"per-company table {tname} (DataPerCompany = true). Per-company data should be upgraded "
                     f"per-company (UpgradePerCompanySAN)."),
                    "warning", cu["file"], cu["line"],
                ))

    return diagnostics
