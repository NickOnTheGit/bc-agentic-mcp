"""al_compiler — deterministic AL compile + analyzer diagnostics via the REAL alc.exe.

Replaces the fictional ``altool`` abstraction. The AL compiler (``alc.exe``, shipped with the
``ms-dynamics-smb.al`` VS Code extension) is the authoritative, container-free way to compile an
AL project and obtain semantic + analyzer diagnostics. We invoke it with ``/errorlog:<file>``
which emits **SARIF 2.1.0**, then parse that deterministically — no screen-scraping.

Analyzers (Microsoft CodeCop / AppSourceCop / UICop / PerTenantExtensionCop) ship in the
extension's ``bin/Analyzers``; extra analyzer DLLs (LinterCop / ALCops) can be supplied via the
``AL_EXTRA_ANALYZERS`` env var (``;``-separated). Symbols come from the project's ``.alpackages``.

Everything is behind a ``runner`` seam so the wiring is unit-testable without invoking alc.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Microsoft analyzer DLLs (in <extension>/bin/Analyzers).
_MS_ANALYZERS = (
    "Microsoft.Dynamics.Nav.CodeCop.dll",
    "Microsoft.Dynamics.Nav.AppSourceCop.dll",
    "Microsoft.Dynamics.Nav.UICop.dll",
    "Microsoft.Dynamics.Nav.PerTenantExtensionCop.dll",
)

# PerTenantExtensionCop enforces the PTE object-id range. Running it against an
# app with LICENSED id ranges is a category error: every object becomes a false
# 'ID must be within [50000..99999]' (observed live: 525 phantom errors on
# EmpireHousing). The app's own manifest decides which cop applies.
_PTE_RANGE = (50000, 99999)


def _project_id_ranges(project_dir: Optional[Path]) -> List[tuple]:
    """The app.json idRanges as (from, to) tuples; [] when unknown."""
    if project_dir is None:
        return []
    try:
        data = json.loads((project_dir / "app.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    ranges = data.get("idRanges") or ([data["idRange"]] if "idRange" in data else [])
    out: List[tuple] = []
    for r in ranges:
        try:
            out.append((int(r["from"]), int(r["to"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _is_per_tenant_app(project_dir: Optional[Path]) -> bool:
    """True only when EVERY declared id range fits the PTE window (or nothing is known)."""
    ranges = _project_id_ranges(project_dir)
    if not ranges:
        return True  # unknown -> keep the historic default (all cops)
    return all(_PTE_RANGE[0] <= lo and hi <= _PTE_RANGE[1] for lo, hi in ranges)


def find_ruleset(project_dir: Optional[Path]) -> Optional[Path]:
    """The repo's own ruleset (<ancestor>/.codeAnalysis/*.ruleset.json), if any."""
    if project_dir is None:
        return None
    for base in [project_dir, *list(project_dir.parents)[:3]]:
        ca = base / ".codeAnalysis"
        if ca.is_dir():
            hits = sorted(ca.glob("*.ruleset.json"))
            if hits:
                return hits[0]
    return None

# runner(cmd, errorlog_path) -> return code. Default runs alc; alc writes the SARIF to errorlog.
Runner = Callable[[List[str], Path], int]


@dataclass
class CompilerInfo:
    command_prefix: Optional[List[str]] = None  # [alc.exe] or [dotnet, alc.dll]
    analyzers_dir: Optional[Path] = None
    version: str = ""

    @property
    def available(self) -> bool:
        return bool(self.command_prefix)


def _al_extension_dirs() -> List[Path]:
    base = Path.home() / ".vscode" / "extensions"
    if not base.is_dir():
        return []
    def _ver_key(p: Path):
        try:
            return tuple(int(x) for x in p.name.split("al-")[-1].split("."))
        except ValueError:
            return (0,)
    return sorted(base.glob("ms-dynamics-smb.al-*"), key=_ver_key, reverse=True)


def discover_compiler() -> CompilerInfo:
    """Locate the newest installed alc compiler + its analyzer folder. Honors env overrides."""
    if os.environ.get("BC_AGENTIC_DISABLE_COMPILER"):
        return CompilerInfo()  # hermetic tests / opt-out: never invoke the real compiler
    override = os.environ.get("AL_COMPILER_PATH")
    if override and Path(override).exists():
        p = Path(override)
        prefix = [str(p)] if p.suffix.lower() == ".exe" else ["dotnet", str(p)]
        adir = Path(os.environ.get("AL_ANALYZERS_DIR", str(p.parent.parent / "Analyzers")))
        return CompilerInfo(prefix, adir if adir.is_dir() else None)

    plat = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
    for ext in _al_extension_dirs():
        bin_dir = ext / "bin"
        exe = bin_dir / plat / "alc.exe"
        dll = bin_dir / plat / "alc.dll"
        analyzers = bin_dir / "Analyzers"
        if sys.platform == "win32" and exe.exists():
            return CompilerInfo([str(exe)], analyzers if analyzers.is_dir() else None, ext.name)
        if dll.exists():
            return CompilerInfo(["dotnet", str(dll)], analyzers if analyzers.is_dir() else None, ext.name)
    return CompilerInfo()


def find_project(path: Path) -> Optional[Path]:
    """Walk up from a file or dir to the nearest AL project (folder containing app.json)."""
    cur = Path(path).resolve()
    if cur.is_file():
        cur = cur.parent
    for _ in range(12):
        if (cur / "app.json").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def analyzer_dlls(info: CompilerInfo, extra: Optional[List[str]] = None,
                  project_dir: Optional[Path] = None) -> List[str]:
    """The analyzer DLLs to apply: Microsoft built-ins that exist + any extras (LinterCop/ALCops).

    The set is derived from the app's OWN manifest: PerTenantExtensionCop only for true
    per-tenant apps (licensed-range apps get phantom id-range errors from it).
    """
    out: List[str] = []
    if info.analyzers_dir and info.analyzers_dir.is_dir():
        names = list(_MS_ANALYZERS)
        if not _is_per_tenant_app(project_dir):
            names = [n for n in names if "PerTenantExtensionCop" not in n]
        for name in names:
            dll = info.analyzers_dir / name
            if dll.exists():
                out.append(str(dll))
    for e in (extra or []):
        if e and Path(e).exists():
            out.append(str(e))
    for e in filter(None, os.environ.get("AL_EXTRA_ANALYZERS", "").split(os.pathsep)):
        if Path(e).exists():
            out.append(e)
    return out


def build_command(
    info: CompilerInfo,
    project_dir: Path,
    out_file: Path,
    errorlog: Path,
    *,
    package_cache_paths: Optional[List[Path]] = None,
    analyzers: Optional[List[str]] = None,
    ruleset: Optional[Path] = None,
) -> List[str]:
    """Assemble the alc.exe command line (SARIF via /errorlog). Deterministic."""
    cmd = list(info.command_prefix or [])
    cmd += [f"/project:{project_dir}", f"/out:{out_file}", f"/errorlog:{errorlog}"]
    for pc in (package_cache_paths or []):
        cmd.append(f"/packagecachepath:{pc}")
    for dll in (analyzers or []):
        cmd.append(f"/analyzer:{dll}")
    if ruleset:
        cmd.append(f"/ruleset:{ruleset}")
    return cmd


_SEVERITY = {"error": "error", "warning": "warning", "note": "info", "info": "info",
             "information": "info", "hidden": "info", "none": "info"}


def _uri_to_path(uri: str, project_dir: Path) -> str:
    if not uri:
        return ""
    p = uri
    if p.startswith("file:///"):
        p = p[8:]
    elif p.startswith("file://"):
        p = p[7:]
    p = p.replace("%20", " ")
    try:
        return str(Path(p).resolve().relative_to(Path(project_dir).resolve()))
    except (ValueError, OSError):
        return p


def _parse_al_issues(data: Dict[str, Any], project_dir: Path) -> List[Dict[str, Any]]:
    """AL compiler native error-log format (``{"version":"0.2","issues":[...]}``)."""
    out: List[Dict[str, Any]] = []
    for it in data.get("issues", []) or []:
        props = it.get("properties", {}) or {}
        sev = props.get("severity") or props.get("defaultSeverity") or "Warning"
        loc0 = (it.get("locations", []) or [{}])[0]
        target = (loc0.get("analysisTarget", []) or [{}])[0]
        uri = target.get("uri", "")
        line = (target.get("region", {}) or {}).get("startLine", 0) or 0
        out.append({
            "code": it.get("ruleId", ""),
            "message": it.get("shortMessage") or it.get("fullMessage") or "",
            "severity": _SEVERITY.get(str(sev).lower(), "warning"),
            "sourceLocation": {"file": _uri_to_path(uri, project_dir), "line": line},
            "source": "compiler",
        })
    return out


def _parse_sarif_results(data: Dict[str, Any], project_dir: Path) -> List[Dict[str, Any]]:
    """Standard SARIF 2.1.0 (``runs[].results[]``) — fallback for newer alc versions."""
    out: List[Dict[str, Any]] = []
    for run in data.get("runs", []) or []:
        rule_default: Dict[str, str] = {}
        for rule in (run.get("tool", {}).get("driver", {}).get("rules", []) or []):
            lvl = (rule.get("defaultConfiguration", {}) or {}).get("level")
            if rule.get("id") and lvl:
                rule_default[rule["id"]] = lvl
        for res in run.get("results", []) or []:
            rule_id = res.get("ruleId", "")
            level = res.get("level") or rule_default.get(rule_id) or "warning"
            locs = res.get("locations", []) or []
            file_uri, line = "", 0
            if locs:
                phys = locs[0].get("physicalLocation", {}) or {}
                file_uri = (phys.get("artifactLocation", {}) or {}).get("uri", "")
                line = (phys.get("region", {}) or {}).get("startLine", 0) or 0
            out.append({
                "code": rule_id,
                "message": (res.get("message", {}) or {}).get("text", ""),
                "severity": _SEVERITY.get(str(level).lower(), "warning"),
                "sourceLocation": {"file": _uri_to_path(file_uri, project_dir), "line": line},
                "source": "compiler",
            })
    return out


def parse_sarif(sarif_path: Path, project_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Parse an alc error log into normalized diagnostics. Handles the AL compiler's native
    ``issues`` format and standard SARIF ``runs/results``. Empty list on any failure."""
    try:
        data = json.loads(Path(sarif_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    project_dir = Path(project_dir) if project_dir else Path(".")
    if isinstance(data.get("issues"), list):
        return _parse_al_issues(data, project_dir)
    return _parse_sarif_results(data, project_dir)


def _default_runner(cmd: List[str], errorlog: Path) -> int:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        return proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def compile_project(
    project_dir: str,
    *,
    package_cache_paths: Optional[List[str]] = None,
    extra_analyzers: Optional[List[str]] = None,
    ruleset: Optional[str] = None,
    runner: Optional[Runner] = None,
) -> Dict[str, Any]:
    """Compile an AL project with analyzers and return normalized SARIF diagnostics.

    Returns ``{available, success, returncode, diagnostics, errors, warnings, sarif, project}``.
    ``available=False`` (no compile) when alc cannot be found or no app.json is discoverable —
    callers then fall back to the regex validator.
    """
    info = discover_compiler()
    pdir = find_project(Path(project_dir)) or (Path(project_dir) if (Path(project_dir) / "app.json").exists() else None)
    if not info.available or pdir is None:
        return {"available": False, "diagnostics": [],
                "reason": "alc not found" if not info.available else "no app.json (not an AL project)"}

    if package_cache_paths is None:
        # Monorepos keep ONE shared symbol cache above the per-app folders
        # (extensions/.alpackages serving extensions/<App>/) — an app-local-only
        # lookup failed with 'package ... could not be found' on a cache that DID
        # exist two levels up (observed live on EmpireHousing in a fresh worktree).
        pkgs = [pdir / ".alpackages"]
        for ancestor in list(pdir.parents)[:3]:
            pkgs.append(ancestor / ".alpackages")
        package_cache_paths = [str(p) for p in pkgs if p.is_dir()]

    analyzers = analyzer_dlls(info, extra_analyzers, project_dir=pdir)
    effective_ruleset = Path(ruleset) if ruleset else find_ruleset(pdir)
    tmp = Path(tempfile.mkdtemp(prefix="bc_alc_"))
    out_file = tmp / "out.app"
    errorlog = tmp / "diagnostics.sarif"
    cmd = build_command(
        info, pdir, out_file, errorlog,
        package_cache_paths=[Path(p) for p in package_cache_paths],
        analyzers=analyzers,
        ruleset=effective_ruleset,
    )
    rc = (runner or _default_runner)(cmd, errorlog)
    diagnostics = parse_sarif(errorlog, pdir) if errorlog.exists() else []
    errors = [d for d in diagnostics if d["severity"] == "error"]
    warnings = [d for d in diagnostics if d["severity"] == "warning"]
    return {
        "available": True,
        "success": rc == 0 and not errors,
        "returncode": rc,
        "diagnostics": diagnostics,
        "errors": len(errors),
        "warnings": len(warnings),
        "analyzers": [Path(a).name for a in analyzers],
        "sarif": str(errorlog),
        "project": str(pdir),
    }
