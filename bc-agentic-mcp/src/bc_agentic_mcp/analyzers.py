"""analyzers — integrate the real, compiler-integrated AL code analyzers.

The self-contained ``al_validator`` uses regex, which is fragile (comments, strings,
multiline) and re-implements rules that already exist as *semantic* analyzer rules. When
the AL toolchain (``altool``) is available we run the authoritative analyzers — Microsoft
**CodeCop / AppSourceCop / UICop / PerTenantExtensionCop** plus community **LinterCop /
ALCops** — and merge their diagnostics on top of the regex ones, with two deterministic
guarantees:

1. **Provenance**: every diagnostic is tagged ``source = "compiler" | "self"`` so callers
   know whether a rule came from the semantic model or the regex fallback.
2. **De-duplication**: when the compiler reports an authoritative rule that our regex rule
   only approximates (e.g. our ``V0061`` ≈ ``LC0061`` API ODataKeyFields), the regex copy is
   dropped to avoid double-reporting. The compiler is authoritative.

When ``altool`` is absent (spec-only mode) behaviour is unchanged: the regex diagnostics are
returned as-is. A ``runner`` seam makes the merge fully unit-testable without a toolchain.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Regex rule (ours) -> authoritative analyzer rule id(s) that supersede it when present.
# Sourced from ALCops/LinterCop (LC****) and Microsoft analyzers (AA****/AS****/AL****).
EQUIVALENT: Dict[str, set] = {
    "V0061": {"LC0061"},                 # API page ODataKeyFields = SystemId
    "V0060": {"LC0060"},                 # ApplicationArea not applicable to API page
    "V0070": {"LC0051", "LC0076", "AL0468"},  # field name length / text truncation
    "V0019": {"LC0019"},                 # DataClassification inheritance
    "V0001": {"AL0000", "AL0603"},       # brace/syntax — compiler syntax errors supersede
}

# The analyzer set we ask the toolchain to run (Microsoft + community).
ANALYZER_SET = ("CodeCop", "AppSourceCop", "UICop", "PerTenantExtensionCop", "LinterCop")

DiagRunner = Callable[[Path], List[Dict[str, Any]]]


def _file_of(d: Dict[str, Any]) -> str:
    return (d.get("sourceLocation") or {}).get("file", "") or ""


def normalize(raw: List[Dict[str, Any]], source: str = "compiler") -> List[Dict[str, Any]]:
    """Normalize toolchain diagnostics to our shape and stamp provenance."""
    out: List[Dict[str, Any]] = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        loc = d.get("sourceLocation") or {}
        out.append({
            "code": d.get("code") or d.get("ruleId") or d.get("id") or "",
            "message": d.get("message") or d.get("shortMessage") or "",
            "severity": (d.get("severity") or "warning"),
            "sourceLocation": {
                "file": loc.get("file") or d.get("file") or "",
                "line": loc.get("line") or d.get("line") or 0,
            },
            "source": source,
        })
    return out


def dedup(self_diags: List[Dict[str, Any]], analyzer_diags: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge regex + analyzer diagnostics, dropping regex rules the compiler supersedes.

    A regex diagnostic is dropped when an analyzer diagnostic in the SAME file carries an
    equivalent authoritative code (line numbers may legitimately differ between a regex
    match and the semantic model, so file-level equivalence is the deterministic rule).
    """
    analyzer_codes_by_file: Dict[str, set] = {}
    for a in analyzer_diags:
        analyzer_codes_by_file.setdefault(_file_of(a), set()).add(a.get("code", ""))

    merged: List[Dict[str, Any]] = list(analyzer_diags)
    for s in self_diags:
        superseded_by = EQUIVALENT.get(s.get("code", ""))
        if superseded_by:
            present = analyzer_codes_by_file.get(_file_of(s), set())
            if present & superseded_by:
                continue  # compiler already reported the authoritative rule for this file
        merged.append({**s, "source": s.get("source", "self")})
    return merged


def collect(
    project_root: Path,
    altool_status: Any = None,
    self_diags: Optional[List[Dict[str, Any]]] = None,
    runner: Optional[DiagRunner] = None,
    use_compiler: bool = False,
) -> Dict[str, Any]:
    """Produce the merged diagnostic set + provenance summary.

    - ``self_diags``: the regex diagnostics (from ``al_validator.validate_project``). If not
      supplied, they are computed.
    - ``runner``: injectable ``(project_root) -> [raw diagnostics]`` for the analyzers.
    - ``use_compiler``: when True (and no ``runner``), invoke the REAL alc compiler
      (``al_compiler.compile_project``) for authoritative semantic + analyzer diagnostics.
    - else the legacy ``altool`` is used iff ``altool_status`` reports it available.
    """
    root = Path(project_root).resolve()
    if self_diags is None:
        from bc_agentic_mcp.al_validator import validate_project
        self_diags = validate_project(root)
    self_diags = [{**d, "source": d.get("source", "self")} for d in self_diags]

    analyzer_diags: List[Dict[str, Any]] = []
    used_compiler = False
    error: Optional[str] = None

    if runner is not None:
        try:
            analyzer_diags = normalize(runner(root), source="compiler")
            used_compiler = True
        except Exception as exc:  # a runner failure must never break self-contained mode
            error = str(exc)
    elif use_compiler:
        from bc_agentic_mcp import al_compiler
        comp = al_compiler.compile_project(str(root))
        if comp.get("available"):
            analyzer_diags = comp.get("diagnostics", [])  # already normalized (source=compiler)
            used_compiler = True
        else:
            error = comp.get("reason")
    elif altool_status is not None and getattr(altool_status, "available", False) \
            and getattr(altool_status, "altool_path", None) is not None:
        try:
            from bc_agentic_mcp.al_client import get_diagnostics
            analyzer_diags = normalize(get_diagnostics(altool_status.altool_path, root), source="compiler")
            used_compiler = True
        except Exception as exc:
            error = str(exc)

    merged = dedup(self_diags, analyzer_diags)

    result: Dict[str, Any] = {
        "diagnostics": merged,
        "mode": "compiler+self" if used_compiler else "self",
        "analyzers": list(ANALYZER_SET) if used_compiler else [],
        "sources": {
            "compiler": sum(1 for d in merged if d.get("source") == "compiler"),
            "self": sum(1 for d in merged if d.get("source") == "self"),
        },
    }
    if error:
        result["analyzer_error"] = error
    return result
