"""breaking_change — pre-PR wall for AppSourceCop-class breaking changes.

Born from a live failure (Bug 267600, build 257437): local per-app compile and
container tests were all green while the incremental merge build failed with
AS0083 — removing a RELEASED enum value breaks the baseline contract. The
pipeline compiles against a released-app baseline cache with warnings-as-errors,
a check class that does not exist locally. This module pre-flights the
deterministic subset of that verdict from the git diff alone:

  BC-BREAK-ENUMVAL   removed `value(N; Name)` from an enum/enumextension
  BC-BREAK-FIELD     removed `field(N; ...)` from a table/tableextension
  BC-BREAK-TABLE     deleted table/tableextension/enum/enumextension .al file

Deliberately NOT flagged (evidence: the same failing build ACCEPTED them):
  - deleted codeunits (Access=Internal convention repo-wide)
  - value/field edits that keep the member (obsolete-pending edits pass)
  - removals of members ADDED IN THE SAME BRANCH (diff vs merge-base never
    shows them as removed-from-released)

The scan is a PURE function over `git diff <merge-base>...HEAD` text so it is
unit-testable without a repository.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_DELETED_FILE_RE = re.compile(r"^deleted file mode", re.MULTILINE)
_VALUE_REMOVED_RE = re.compile(r"^-\s*value\(\s*(\d+)\s*;\s*([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\")\s*\)", re.MULTILINE)
_VALUE_ADDED_RE = re.compile(r"^\+\s*value\(\s*(\d+)\s*;\s*([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\")\s*\)", re.MULTILINE)
_FIELD_REMOVED_RE = re.compile(r"^-\s*field\(\s*(\d+)\s*;\s*([^;)]+?)\s*;", re.MULTILINE)
_FIELD_ADDED_RE = re.compile(r"^\+\s*field\(\s*(\d+)\s*;\s*([^;)]+?)\s*;", re.MULTILINE)

_SCHEMA_FILE_RE = re.compile(
    r"\.(Table|TableExt|Enum|EnumExt)\.al$", re.IGNORECASE)
_ENUMISH_FILE_RE = re.compile(r"\.(Enum|EnumExt)\.al$", re.IGNORECASE)
_TABLEISH_FILE_RE = re.compile(r"\.(Table|TableExt)\.al$", re.IGNORECASE)


def merge_base_diff(repo_root: Path, target_branch: str = "master") -> Optional[str]:
    """Return `git diff <merge-base(origin/target, HEAD)>...HEAD -- *.al` or None."""
    def _git(*args: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True, text=True, timeout=120, check=False,
                stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    base = _git("merge-base", f"origin/{target_branch}", "HEAD")
    if not base:
        base = _git("merge-base", target_branch, "HEAD")
    if not base:
        return None
    return _git("diff", base.strip(), "HEAD", "--", "*.al")


def scan_breaking(diff_text: str) -> List[Dict[str, Any]]:
    """Scan a git diff for AppSourceCop-class breaking removals (pure)."""
    findings: List[Dict[str, Any]] = []
    if not diff_text:
        return findings

    # Split into per-file chunks (keep the header line with each chunk).
    chunks = re.split(r"(?m)^(?=diff --git )", diff_text)
    for chunk in chunks:
        m = _DIFF_HEADER_RE.match(chunk.splitlines()[0] if chunk else "")
        if not m:
            continue
        path = m.group(2)
        if not path.lower().endswith(".al"):
            continue

        if _SCHEMA_FILE_RE.search(path) and _DELETED_FILE_RE.search(chunk):
            findings.append({
                "code": "BC-BREAK-TABLE",
                "severity": "error",
                "file": path,
                "message": (f"Deleted schema-bearing file {path}: removing a released "
                            "table/enum (extension) is an AppSourceCop breaking change. "
                            "Use two-phase obsoleting (ObsoleteState=Pending now, removal item later)."),
            })
            continue

        if _ENUMISH_FILE_RE.search(path):
            added = {(v[0], v[1].strip('"')) for v in _VALUE_ADDED_RE.findall(chunk)}
            for num, raw_name in _VALUE_REMOVED_RE.findall(chunk):
                name = raw_name.strip('"')
                if (num, name) in added:
                    continue  # moved/re-declared, not removed
                findings.append({
                    "code": "BC-BREAK-ENUMVAL",
                    "severity": "error",
                    "file": path,
                    "message": (f"Removed enum value ({num}; {name}) from {path}: AS0083 — a released "
                                "enum value cannot be removed. Keep it with ObsoleteState=Pending + "
                                "ObsoleteReason + ObsoleteTag (team precedent: WordLink feature)."),
                })

        if _TABLEISH_FILE_RE.search(path):
            added_fields = {(f[0]) for f in _FIELD_ADDED_RE.findall(chunk)}
            for num, name in _FIELD_REMOVED_RE.findall(chunk):
                if num in added_fields:
                    continue  # re-declared (rename/retype shows as remove+add; AppSourceCop
                              # flags retype separately — out of this wall's precision range)
                findings.append({
                    "code": "BC-BREAK-FIELD",
                    "severity": "error",
                    "file": path,
                    "message": (f"Removed field ({num}; {name.strip()}) from {path}: removing a released "
                                "table field is an AppSourceCop breaking change. Mark it "
                                "ObsoleteState=Pending instead (two-phase obsoleting)."),
                })

    return findings


def scan_missing_namespaces(diff_text: str, read_file=None) -> List[Dict[str, Any]]:
    """Pipeline rule CheckMissingNamespaces=true, mirrored EXACTLY from the team cmdlet
    Test-cdsaMissingNamespacesIncr (ERP.PSModules/cdsa.build.al, fetched 2026-07-04):
      - only MODIFIED (not deleted) *.al files are checked
      - any path containing 'Test' is EXEMPT ($_ -notlike "*Test*") — this is why
        build 257443 passed with a namespace-less test codeunit
      - the FIRST LINE must contain 'namespace Zig.' (-notlike "*namespace Zig.*"),
        not merely any namespace anywhere.
    read_file(path)->str|None is injectable; unreadable files stay fail-open (the
    pipeline remains the authority)."""
    findings: List[Dict[str, Any]] = []
    if not diff_text:
        return findings
    chunks = re.split(r"(?m)^(?=diff --git )", diff_text)
    for chunk in chunks:
        header = chunk.splitlines()[0] if chunk else ""
        m = _DIFF_HEADER_RE.match(header)
        if not m:
            continue
        path = m.group(2)
        if not path.lower().endswith(".al") or _DELETED_FILE_RE.search(chunk):
            continue
        if "test" in path.lower():  # cmdlet: $_ -notlike "*Test*" (case-insensitive)
            continue
        content = read_file(path) if read_file else None
        if content is None:
            continue  # cannot prove — fail-open per file
        lines = content.splitlines()
        first_line = lines[0] if lines else ""
        if "namespace Zig." not in first_line:
            findings.append({
                "code": "BC-NS-MISSING",
                "severity": "error",
                "file": path,
                "message": (f"First line of modified {path} lacks 'namespace Zig.' — the "
                            "incremental build fails it (Test-cdsaMissingNamespacesIncr, "
                            "CheckMissingNamespaces=true; test apps are exempt)."),
            })
    return findings


def gate(repo_root: Path, target_branch: str = "master") -> Dict[str, Any]:
    """Run the pre-PR breaking-change wall against merge-base(origin/<target>)."""
    diff_text = merge_base_diff(repo_root, target_branch)
    if diff_text is None:
        # Fail-open on inability to diff (no remote, detached, etc.) — the PIPELINE
        # remains the authority; this wall only pre-flights what it can prove.
        return {"ok": True, "checked": False, "findings": [],
                "note": "merge-base diff unavailable — breaking-change pre-flight skipped"}
    findings = scan_breaking(diff_text)

    def _read(rel: str) -> Optional[str]:
        p = repo_root / rel
        try:
            return p.read_text(encoding="utf-8-sig", errors="replace") if p.is_file() else None
        except OSError:
            return None

    findings.extend(scan_missing_namespaces(diff_text, read_file=_read))
    return {"ok": not findings, "checked": True, "findings": findings}
