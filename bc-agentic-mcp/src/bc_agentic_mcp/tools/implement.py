"""bc_implement — core implementation engine. See spec Section 3.9.

V2 scope: two-phase tool.

Phase 1 (code=None): context preparation — unchanged from V1.
  Returns task context + implementer prompt for the model.

Phase 2 (code is provided): code execution.
  Validates scope, writes AL file, compiles via altool, returns diagnostics.
  Supports compile-and-fix loop (max 3 attempts).
"""
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, List, Optional
import re
import subprocess

from bc_agentic_mcp.errors import MCPError, ErrorCode
from bc_agentic_mcp.scope import ScopeEnforcer
from bc_agentic_mcp.spec_loader import (
    load_spec,
    upgrade_contract_for_file,
    validate_upgrade_code_against_contract,
)
from bc_agentic_mcp import authorization


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def _load_implementer_prompt() -> str:
    """Load the implementer.md prompt file."""
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "implementer.md"
    if not prompt_path.exists():
        return ""  # Graceful degradation — model can still implement without custom prompt
    return prompt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File writer with scope enforcement
# ---------------------------------------------------------------------------

def _write_al_file(
    root: Path,
    scope: ScopeEnforcer,
    file_path: str,
    content: str,
) -> Path:
    """Write an AL file. Validates scope before writing."""
    from bc_agentic_mcp.validation import sanitize_path

    sanitize_path(file_path)
    target = root / file_path

    # Check if file already exists (modify) or is new (create)
    if target.exists():
        if not scope.check_write(file_path):
            reason = scope.block_reason(file_path)
            raise MCPError(ErrorCode.SCOPE_ERROR, reason,
                           hint="Expand scope boundaries in spec.json or choose an alternative approach.")
    else:
        if not scope.check_create(file_path):
            reason = scope.block_reason(file_path)
            raise MCPError(ErrorCode.SCOPE_ERROR, reason,
                           hint="Expand scope boundaries or choose a file in an allowed extension.")

    target.parent.mkdir(parents=True, exist_ok=True)
    # Line-ending discipline: write_text without newline= lets Python translate
    # '\n' -> os.linesep, so CRLF input becomes \r\r\n (observed live: wi267598
    # commit showed a 484-line rewrite for a 20-line change). Normalize to the
    # file's EXISTING convention (LF for new files — the ERP repo standard) and
    # write untranslated.
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if target.exists():
        try:
            existing = target.read_bytes()
            # Any CRLF presence (INCLUDING corrupted \r\r\n — which implies \r\n) means
            # the file's convention is CRLF: rewriting it LF-only would show a whole-
            # file line-ending diff (PR 41670 restore scenario, 2026-07-06).
            if b"\r\n" in existing:
                normalized = normalized.replace("\n", "\r\n")
        except OSError:
            pass
    target.write_text(normalized, encoding="utf-8", newline="")
    return target


# ---------------------------------------------------------------------------
# Implementation instructions builder
# ---------------------------------------------------------------------------

def _build_implementation_instructions(
    tasks_path: Path, task_id: str, spec: Dict[str, Any]
) -> str:
    """Build the instruction block for the AI model."""
    scope = spec.get("scope_boundaries", {})
    allowed = scope.get("allowed_files", [])
    forbidden = scope.get("forbidden_patterns", [])

    lines = [
        f"Read TASKS.md at: {tasks_path}",
        f"Find task {task_id} and implement it.",
        f"Allowed files (do NOT write outside these): {allowed}",
        f"Forbidden patterns (do NOT use these): {forbidden}",
        "",
        "After writing AL code:",
        "1. Compile the project",
        "2. Read diagnostics",
        "3. Fix any errors (max 3 attempts)",
        "4. If 3rd attempt fails, mark task as failed and report diagnostics",
        "",
        "Copy naming conventions and error handling patterns exactly from ",
        "the module analysis. Follow the implementer prompt rules.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TASKS.md updater
# ---------------------------------------------------------------------------


def _update_tasks_md(specs_dir: Path, task_ids: Optional[List[str]], failed: bool = False) -> None:
    """Update TASKS.md: mark tasks as complete or failed."""
    tasks_path = specs_dir / "TASKS.md"
    if not tasks_path.exists():
        return
    content = tasks_path.read_text(encoding="utf-8")
    for task_id in task_ids or []:
        old = f"- [ ] {task_id}"
        new = f"- [x] {task_id}" if not failed else f"- [x] {task_id} (FAILED)"
        content = content.replace(old, new)
    tasks_path.write_text(content, encoding="utf-8")


_IFACE_DECL_RE = re.compile(r"\b(\w+)\s*:\s*Interface\s+(\w+)\s*;", re.IGNORECASE)
_IFACE_PROC_RE = re.compile(r"procedure\s+(\w+)", re.IGNORECASE)


def _interface_call_issues(root: Path, code: str) -> List[str]:
    """AL0132 pre-check: calls through `Interface X` vars must be members of X.

    Uses the persistent object index (interfaces carry their procedure list); a
    24h-old index is fine — interface members change rarely, and a false negative
    just falls through to the real compiler.
    """
    decls = {var: iface for var, iface in _IFACE_DECL_RE.findall(code)}
    if not decls:
        return []
    try:
        from bc_agentic_mcp import object_index
        objects = object_index.refresh(Path(root).resolve(), max_age_seconds=86400)["objects"]
    except Exception:
        return []  # index unavailable: leave it to the compiler
    issues: List[str] = []
    for var, iface in decls.items():
        entry = objects.get(iface.lower())
        if not entry or entry.get("kind") != "interface":
            continue  # unknown interface (dependency symbol) — compiler decides
        members = {m.group(1).lower() for p in (entry.get("detail", {}).get("procedures") or [])
                   for m in [_IFACE_PROC_RE.search(str(p))] if m}
        if not members:
            continue
        for call in re.finditer(rf"\b{re.escape(var)}\.(\w+)\s*\(", code):
            method = call.group(1)
            if method.lower() not in members:
                issues.append(
                    f"'{var}.{method}()' is not a member of interface {iface} "
                    f"(members: {', '.join(sorted(members))})")
    return issues


# ---------------------------------------------------------------------------
# Altool path helper (lazy import to avoid circular dependency)
# ---------------------------------------------------------------------------


def _get_altool_path() -> Optional[Path]:
    """Get altool path from server context. Returns None if not available."""
    try:
        from bc_agentic_mcp.server import _get_ctx
        return _get_ctx().config.al_tool.altool_path
    except (AssertionError, ImportError, AttributeError):
        return None


def _persist_quality(specs_dir: Path, errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]],
                     mode: str, analyzers: List[str]) -> None:
    """Persist a quality snapshot from the REAL compile so the commit gate (F1) can verify it."""
    try:
        import hashlib as _h
        import json as _json
        from datetime import datetime, timezone
        spec_file = specs_dir / "spec.json"
        spec_sha = _h.sha256(spec_file.read_bytes()).hexdigest() if spec_file.exists() else None
        (specs_dir / "quality.json").write_text(_json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode, "errors": len(errors), "warnings": len(warnings),
            "analyzers": analyzers, "spec_sha": spec_sha,
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _split_diagnostics_by_file(
    diagnostics: List[Dict[str, Any]],
    file_path: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Split diagnostics between changed-file and baseline diagnostics."""
    expected = str(file_path).replace("\\", "/").lower()
    changed: List[Dict[str, Any]] = []
    baseline: List[Dict[str, Any]] = []
    for d in diagnostics:
        src = str(((d.get("sourceLocation") or {}).get("file") or "")).replace("\\", "/").lower()
        if src.endswith(expected) or src == expected:
            changed.append(d)
        else:
            baseline.append(d)
    return {"changed": changed, "baseline": baseline}


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle_implement(
    project_root: str,
    spec_name: str,
    task_ids: Optional[List[str]] = None,
    mode: str = "auto",
    dry_run: bool = False,
    # Phase 2 parameters
    code: Optional[str] = None,
    file_path: Optional[str] = None,
    attempt: int = 1,
    previous_diagnostics: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute implementation tasks. Two-phase behavior.

    Phase 1 (code=None): prepare context for model (unchanged from V1).
    Phase 2 (code is provided): write file, compile, return diagnostics.
    """
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name

    spec = load_spec(specs_dir)

    scope_boundaries = spec.get("scope_boundaries", {})
    scope = ScopeEnforcer(
        allowed_files=scope_boundaries.get("allowed_files", []),
        project_root=root,
        allowed_extensions=scope_boundaries.get("allowed_extensions", []),
        scope_mode=scope_boundaries.get("scope_mode", "strict"),
    )

    # ------------------------------------------------------------------
    # Phase 2: code execution
    # ------------------------------------------------------------------
    if code is not None:
        # Validate file_path
        if not file_path:
            return {
                "status": "error",
                "message": "file_path is required when code is provided",
            }

        # Poka-yoke (Layer 2): refuse to write implementation code before human approval.
        if not authorization.implementation_authorized(root, spec_name):
            return {
                "status": "blocked_needs_approval",
                "file": file_path,
                "message": (
                    "Implementation is not authorized: no approved decision for a gating phase "
                    "(tasks/implement/complete). Run bc_request_approval -> human bc_submit_decision "
                    "(approve) before writing code. This is the sanctioned write path; do not edit "
                    "spec-scoped files with other tools."
                ),
            }

        fresh_review, freshness_reason = authorization.review_is_fresh(root, spec_name)
        if not fresh_review:
            return {
                "status": "blocked_needs_fresh_review",
                "file": file_path,
                "message": (
                    "Implementation is blocked until a fresh review packet exists. "
                    "Run bc_prepare_review for this spec, ensure quality_gate.json pass=true, "
                    "and then request/approve the tasks phase again."
                ),
                "reason": freshness_reason,
            }

        governed, upgrade_contract = upgrade_contract_for_file(spec, file_path)
        if governed:
            contract_issues = validate_upgrade_code_against_contract(code, upgrade_contract)
            if contract_issues:
                return {
                    "status": "blocked_upgrade_contract",
                    "file": file_path,
                    "message": "Upgrade safety contract failed",
                    "issues": contract_issues,
                    "required_scope": upgrade_contract.get("required_scope"),
                    "required_tag": upgrade_contract.get("idempotency_tag"),
                }

        # INTERFACE TRUTH (AL0132 class): every `Var.Method()` call through a declared
        # `Interface X` variable must name a member X actually declares — verified
        # against the object index BEFORE paying the container compile (observed live:
        # IsActive() called via FeatureV3SAN, which does not declare it).
        iface_issues = _interface_call_issues(root, code)
        if iface_issues:
            return {
                "status": "blocked_interface_contract",
                "file": file_path,
                "message": "Interface-member check failed (would be AL0132 in the container)",
                "issues": iface_issues,
                "hint": ("Read the interface definition and call only its members; call "
                         "implementation-specific procedures on the concrete codeunit instead."),
            }

        # Write file with scope validation
        try:
            _write_al_file(root, scope, file_path, code)
        except MCPError as e:
            return {
                "status": "scope_violation",
                "message": str(e),
                "hint": e.hint,
            }

        # Attempt compile — prefer the REAL AL compiler (alc.exe) on the written file's extension.
        from bc_agentic_mcp import al_compiler
        comp = al_compiler.compile_project(str(root / file_path))
        if comp.get("available"):
            c_errors = [d for d in comp["diagnostics"] if d.get("severity") == "error"]
            c_warnings = [d for d in comp["diagnostics"] if d.get("severity") == "warning"]
            # The quality snapshot must record the CHANGED-FILE judgment, not the raw
            # project-wide count: persisting 68k baseline errors (package-cache noise,
            # pre-existing issues) made the commit gate refuse a change the tool itself
            # had judged clean (changed_file_error_count == 0). Baseline noise belongs
            # in the response detail, never in the gate's pass/fail number.
            _scoped = _split_diagnostics_by_file(c_errors, file_path)
            _persist_quality(specs_dir, _scoped["changed"], c_warnings, "compiler",
                             comp.get("analyzers", []))
            if comp.get("success"):
                _update_tasks_md(specs_dir, task_ids)
                return {
                    "status": "completed", "file": file_path, "mode": "compiler",
                    "project": comp.get("project"),
                    "compile_result": {"success": True, "error_count": len(c_errors),
                                       "warning_count": len(c_warnings)},
                }
            split = _split_diagnostics_by_file(c_errors, file_path)
            changed_errors = split["changed"]
            baseline_errors = split["baseline"]
            if not changed_errors:
                _update_tasks_md(specs_dir, task_ids)
                return {
                    "status": "completed_with_baseline_noise",
                    "file": file_path,
                    "mode": "compiler",
                    "project": comp.get("project"),
                    "compile_result": {
                        "success": False,
                        "error_count": len(c_errors),
                        "warning_count": len(c_warnings),
                        "changed_file_error_count": 0,
                        "baseline_error_count": len(baseline_errors),
                    },
                    "baseline_diagnostics": baseline_errors[:50],
                    "message": "Changed file has no compile errors; build failed due to baseline project errors.",
                }
            if attempt < 3:
                return {
                    "status": "compile_failed", "file": file_path, "mode": "compiler",
                    "diagnostics": changed_errors,
                    "baseline_error_count": len(baseline_errors),
                    "attempt": attempt, "retry": True,
                    "guidance": "Fix the errors below and call bc_implement again with attempt+1",
                }
            _update_tasks_md(specs_dir, task_ids, failed=True)
            return {
                "status": "failed_after_retries", "file": file_path, "mode": "compiler",
                "diagnostics": changed_errors or c_errors,
                "baseline_error_count": len(baseline_errors),
                "human_action_required": "Review errors manually",
            }

        # Fallback: legacy altool (fictional) or self-contained regex validation.
        altool_path = _get_altool_path()
        if altool_path is None:
            # Self-contained validation — no external toolchain required.
            from bc_agentic_mcp.al_validator import validate_project

            diagnostics = validate_project(root)
            errors = [d for d in diagnostics if d.get("severity") == "error"]
            warnings = [d for d in diagnostics if d.get("severity") == "warning"]
            _persist_quality(specs_dir, errors, warnings, "self-contained", ["regex"])
            if not errors:
                _update_tasks_md(specs_dir, task_ids)
                return {
                    "status": "completed",
                    "file": file_path,
                    "mode": "self-contained",
                    "compile_result": {
                        "success": True,
                        "error_count": 0,
                        "warning_count": len([d for d in diagnostics if d.get("severity") == "warning"]),
                    },
                }
            split = _split_diagnostics_by_file(errors, file_path)
            changed_errors = split["changed"]
            baseline_errors = split["baseline"]
            if not changed_errors:
                _update_tasks_md(specs_dir, task_ids)
                return {
                    "status": "completed_with_baseline_noise",
                    "file": file_path,
                    "mode": "self-contained",
                    "compile_result": {
                        "success": False,
                        "error_count": len(errors),
                        "warning_count": len(warnings),
                        "changed_file_error_count": 0,
                        "baseline_error_count": len(baseline_errors),
                    },
                    "baseline_diagnostics": baseline_errors[:50],
                    "message": "Changed file has no compile errors; validation failed due to baseline project errors.",
                }
            if attempt < 3:
                return {
                    "status": "compile_failed",
                    "file": file_path,
                    "mode": "self-contained",
                    "diagnostics": changed_errors,
                    "baseline_error_count": len(baseline_errors),
                    "attempt": attempt,
                    "retry": True,
                    "guidance": "Fix the errors below and call bc_implement again with attempt+1",
                }
            _update_tasks_md(specs_dir, task_ids, failed=True)
            return {
                "status": "failed_after_retries",
                "file": file_path,
                "mode": "self-contained",
                "diagnostics": changed_errors or errors,
                "baseline_error_count": len(baseline_errors),
                "human_action_required": "Review errors manually",
            }

        from bc_agentic_mcp.al_client import compile_extension
        compile_result = compile_extension(altool_path, root)
        c_errors = [d for d in compile_result.diagnostics if d.get("severity") == "error"]
        c_warnings = [d for d in compile_result.diagnostics if d.get("severity") == "warning"]
        _persist_quality(specs_dir, c_errors, c_warnings, "altool", ["legacy"])

        if compile_result.success:
            _update_tasks_md(specs_dir, task_ids)
            return {
                "status": "completed",
                "file": file_path,
                "compile_result": {
                    "success": True,
                    "error_count": compile_result.error_count,
                    "warning_count": compile_result.warning_count,
                },
            }
        split = _split_diagnostics_by_file(c_errors, file_path)
        changed_errors = split["changed"]
        baseline_errors = split["baseline"]
        if not changed_errors:
            _update_tasks_md(specs_dir, task_ids)
            return {
                "status": "completed_with_baseline_noise",
                "file": file_path,
                "mode": "altool",
                "compile_result": {
                    "success": False,
                    "error_count": len(c_errors),
                    "warning_count": len(c_warnings),
                    "changed_file_error_count": 0,
                    "baseline_error_count": len(baseline_errors),
                },
                "baseline_diagnostics": baseline_errors[:50],
                "message": "Changed file has no compile errors; build failed due to baseline project errors.",
            }
        if attempt < 3:
            return {
                "status": "compile_failed",
                "file": file_path,
                "diagnostics": changed_errors,
                "baseline_error_count": len(baseline_errors),
                "attempt": attempt,
                "retry": True,
                "guidance": "Fix the errors below and call bc_implement again with attempt+1",
            }
        _update_tasks_md(specs_dir, task_ids, failed=True)
        return {
            "status": "failed_after_retries",
            "file": file_path,
            "diagnostics": changed_errors or c_errors,
            "baseline_error_count": len(baseline_errors),
            "human_action_required": "Review errors manually",
        }

    # ------------------------------------------------------------------
    # Phase 1: context preparation (unchanged from V1)
    # ------------------------------------------------------------------
    if not task_ids:
        return {
            "status": "blocked_no_tasks_selected",
            "spec_name": spec_name,
            "tasks_executed": 0,
            "results": [],
            "message": "No task_ids were provided. Select at least one task from TASKS.md.",
        }

    results: List[Dict[str, Any]] = []

    for task_id in task_ids or []:
        result = await _execute_task(root, specs_dir, spec, scope, task_id, dry_run)
        results.append(result)
        if result["status"] == "failed" and mode == "auto":
            break  # Stop on failure in auto mode

    return {
        "spec_name": spec_name,
        "tasks_executed": len(results),
        "results": results,
        "knowledge": _knowledge_worklist_for_context(root, spec_name, task_ids or []),
    }


# ---------------------------------------------------------------------------
# F2: single-behavior tools. bc_implement silently switched between context-prep
# and code-write on a parameter; these two names each do exactly one thing.
# bc_implement stays as a deprecated alias for one release.
# ---------------------------------------------------------------------------

def _knowledge_worklist_for_context(
    root: Path, spec_name: str, task_ids: List[str],
) -> List[Dict[str, Any]]:
    """Surface matched knowledge articles BEFORE code is written (index-aware
    context, BCQuality contract): lean discovery hints ranked against the
    Charter + selected tasks; the implementer reads each listed file in full
    for its ## Best Practice / ## Anti Pattern rules. Fail-open -> []."""
    try:
        from bc_agentic_mcp import checkpoints as memory
        from bc_agentic_mcp import knowledge
        charter = memory.load_charter(root, spec_name) or {}
        query = " ".join(filter(None, [
            str(charter.get("purpose") or ""),
            " ".join(str(c) for c in charter.get("acceptance_criteria") or []),
            " ".join(task_ids),
        ]))
        return [{"path": a.get("path"), "layer": a.get("layer"), "title": a.get("title"),
                 "description": a.get("description"), "file": a.get("file"),
                 "score": a.get("score")}
                for a in knowledge.select_articles(root, query)]
    except Exception:  # noqa: BLE001 — advisory, never blocks context prep
        return []


async def handle_implement_context(
    project_root: str,
    spec_name: str,
    task_ids: Optional[List[str]] = None,
    mode: str = "auto",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Prepare task context for the model. Never writes code (poka-yoke by shape)."""
    return await handle_implement(
        project_root, spec_name, task_ids=task_ids, mode=mode, dry_run=dry_run,
    )


_AL_OBJECT_DECL_RE = re.compile(
    r"^(codeunit|table|page|report|query|xmlport|enum|interface|permissionset|profile"
    r"|pageextension|tableextension|reportextension|enumextension|permissionsetextension)"
    r"\s+(\d+)\s+", re.IGNORECASE | re.MULTILINE)


def _live_worktrees(project_root: Path) -> List[Path]:
    """All live git worktrees of this repo (including project_root itself)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return [project_root]
    trees = [Path(line[9:].strip()) for line in out.splitlines() if line.startswith("worktree ")]
    return trees or [project_root]


def _id_collision_wall(project_root: Path, code: str, file_path: str) -> Optional[Dict[str, Any]]:
    """CROSS-WORKTREE ID WALL: a NEW object id must be free in EVERY live worktree.

    Encoded from wi267598 (2026-07-04): the spec resolver claimed 66190 free (taken
    in-repo by SetRealtyObjSpaceDescrFDNT) and the retry 66189 was taken by a SIBLING
    worktree's unpushed branch — two collisions, one wasted container cycle each.
    ``git grep --untracked`` sees exactly what a merge would see: tracked + new files.
    Fail-open on git errors (no git = no wall), never on findings.
    """
    m = _AL_OBJECT_DECL_RE.search(code or "")
    if not m:
        return None
    kind, obj_id = m.group(1).lower(), m.group(2)
    needle = re.compile(rf"^{kind}\s+{obj_id}\s+", re.IGNORECASE)
    target_rel = str(file_path).replace("\\", "/").lower()
    for tree in _live_worktrees(project_root):
        try:
            out = subprocess.run(
                ["git", "-C", str(tree), "grep", "-l", "-i", "--untracked",
                 "-E", f"^{kind} +{obj_id} ", "--", "*.al"],
                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for rel in (line.strip() for line in out.splitlines() if line.strip()):
            if tree.resolve() == project_root.resolve() and rel.replace("\\", "/").lower() == target_rel:
                continue  # the file being (re)written is not its own collision
            try:
                first = (tree / rel).read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                first = ""
            if not needle.search(first):
                continue  # git-grep line-anchor false positive
            return {
                "status": "blocked_id_collision",
                "blocked": True,
                "reason": (f"{kind} {obj_id} is already declared in '{rel}' "
                           f"(worktree {tree}). Unpushed sibling branches mint ids too — "
                           "pick an id that is free in EVERY live worktree."),
                "colliding_file": str(tree / rel),
                "next_action": "Renumber the object and retry bc_implement_write.",
            }
    return None


def _fixture_archaeology(root: Path, file_path: str, code: str) -> Optional[Dict[str, Any]]:
    """FIXTURE ARCHAEOLOGY (assist, non-blocking): mine sibling test codeunits'
    Initialize() for setup calls the new test file lacks.

    Encoded from wi267598 (2026-07-04): two container failures were fixture gaps the
    SIBLINGS already solved — DeactivateDaebNonDaebIntegration lived one folder over
    in ContactTest, and the auto contract line was documented by which library call
    siblings used. Heuristic ⇒ warning, never a wall (RULES-CONVICT-OWN-CODE: only
    deterministic checks may block).
    """
    if "subtype = test" not in (code or "").lower():
        return None
    target = root / file_path
    folder = target.parent
    if not folder.exists():
        return None
    call_re = re.compile(r"^\s*(?:\w+\.)?(\w+)\(", re.MULTILINE)
    sibling_calls: List[set] = []
    for sib in sorted(folder.glob("*.al")):
        if sib.resolve() == target.resolve():
            continue
        try:
            text = sib.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        im = re.search(r"local procedure Initialize\(\)(.*?)\n    end;", text,
                       re.DOTALL | re.IGNORECASE)
        if not im:
            continue
        sibling_calls.append(set(call_re.findall(im.group(1))))
    if not sibling_calls:
        return None
    common = {c for c in set.union(*sibling_calls)
              if sum(c in s for s in sibling_calls) * 2 >= len(sibling_calls)}
    missing = sorted(c for c in common if c not in code and c not in
                     {"Get", "Modify", "Insert", "Commit", "Evaluate", "Clear", "exit"})
    if not missing:
        return None
    return {
        "siblings_with_initialize": len(sibling_calls),
        "common_setup_missing": missing,
        "note": ("Sibling test codeunits in this folder call these in Initialize() but "
                 "the new file does not — verify each is genuinely unneeded BEFORE the "
                 "first container run (DAEB deactivation cost a full cycle on wi267598)."),
    }


async def handle_implement_write(
    project_root: str,
    spec_name: str,
    code: str,
    file_path: str,
    task_ids: Optional[List[str]] = None,
    attempt: int = 1,
    previous_diagnostics: Optional[str] = None,
) -> Dict[str, Any]:
    """Write one AL file and compile it. ``code`` + ``file_path`` are REQUIRED here —
    the accidental context-prep fallback of the dual-behavior tool cannot happen."""
    if not code or not str(code).strip():
        raise MCPError(ErrorCode.CLIENT_ERROR, "code is required for bc_implement_write",
                       hint="Use bc_implement_context to prepare task context.")
    if not file_path or not str(file_path).strip():
        raise MCPError(ErrorCode.CLIENT_ERROR, "file_path is required for bc_implement_write",
                       hint="Provide the workspace-relative AL file path to write.")
    root = Path(project_root).resolve()
    target = root / file_path
    if not target.exists():
        collision = _id_collision_wall(root, code, file_path)
        if collision:
            return collision
    result = await handle_implement(
        project_root, spec_name, task_ids=task_ids, code=code, file_path=file_path,
        attempt=attempt, previous_diagnostics=previous_diagnostics,
    )
    if isinstance(result, dict):
        archaeology = _fixture_archaeology(root, file_path, code)
        if archaeology:
            result["fixture_archaeology"] = archaeology
    return result


async def handle_implement_alias(**kwargs: Any) -> Dict[str, Any]:
    """Deprecated dual-behavior entry point; stamps a deprecation notice on results."""
    result = await handle_implement(**kwargs)
    if isinstance(result, dict):
        result.setdefault(
            "deprecation",
            "bc_implement is deprecated: use bc_implement_context (prep) or "
            "bc_implement_write (write+compile).",
        )
    return result


async def handle_implement_delete(
    project_root: str,
    spec_name: str,
    file_path: str,
    task_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Delete one spec-scoped AL file — the decommission twin of bc_implement_write.

    Same walls as the write path (approval gate, fresh review, scope), plus one more:
    the CHARTERED SPEC must explicitly order this file's removal (an objects_to_modify
    entry whose change starts with 'Remove' targeting exactly this path) — ad-hoc
    deletion through the fenced path is impossible. The file content is backed up to
    .specs/<spec>/deleted/ before removal (reversible without git), and the containing
    AL project is recompiled afterwards so dangling references surface immediately.
    """
    if not file_path or not str(file_path).strip():
        raise MCPError(ErrorCode.CLIENT_ERROR, "file_path is required for bc_implement_delete",
                       hint="Provide the workspace-relative AL file path to delete.")
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
    spec = load_spec(specs_dir)

    scope_boundaries = spec.get("scope_boundaries", {})
    scope = ScopeEnforcer(
        allowed_files=scope_boundaries.get("allowed_files", []),
        project_root=root,
        allowed_extensions=scope_boundaries.get("allowed_extensions", []),
        scope_mode=scope_boundaries.get("scope_mode", "strict"),
    )

    if not authorization.implementation_authorized(root, spec_name):
        return {
            "status": "blocked_needs_approval",
            "file": file_path,
            "message": (
                "Deletion is not authorized: no approved decision for a gating phase. "
                "Run bc_request_approval -> human bc_submit_decision (approve) first."
            ),
        }
    fresh_review, freshness_reason = authorization.review_is_fresh(root, spec_name)
    if not fresh_review:
        return {
            "status": "blocked_needs_fresh_review",
            "file": file_path,
            "message": "Deletion is blocked until a fresh review packet exists.",
            "reason": freshness_reason,
        }

    from bc_agentic_mcp.validation import sanitize_path
    sanitize_path(file_path)
    norm = file_path.replace("/", "\\")
    ordered = any(
        str(o.get("change", "")).lower().startswith("remove")
        and str(o.get("target", "")).replace("/", "\\").lower() == norm.lower()
        for o in spec.get("objects_to_modify", [])
    )
    if not ordered:
        return {
            "status": "blocked_not_in_removal_plan",
            "file": file_path,
            "message": (
                "The approved spec does not order this file's removal — only files with an "
                "explicit 'Remove …' change in objects_to_modify may be deleted. Fix the spec "
                "(scope change + re-review) instead of forcing the deletion."
            ),
        }
    if not scope.check_write(file_path):
        return {"status": "scope_violation", "file": file_path,
                "message": scope.block_reason(file_path)}
    target = root / file_path
    if not target.exists():
        return {"status": "already_absent", "file": file_path,
                "message": "File does not exist — nothing to delete (idempotent)."}

    backup_dir = specs_dir / "deleted"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / target.name
    backup_path.write_text(target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    project_dir = target.parent

    # Judge by DELTA, not raw project errors (same lesson as the write path's
    # baseline-noise rule): compile BEFORE the deletion for a baseline, delete,
    # compile again — only NEW errors are dangling references to the removed object.
    from bc_agentic_mcp import al_compiler
    baseline = al_compiler.compile_project(str(project_dir))
    baseline_keys = {
        (d.get("code"), (d.get("sourceLocation") or {}).get("file"), d.get("message"))
        for d in baseline.get("diagnostics", []) if d.get("severity") == "error"
    } if baseline.get("available") else set()

    target.unlink()

    comp = al_compiler.compile_project(str(project_dir))
    result: Dict[str, Any] = {
        "status": "deleted",
        "file": file_path,
        "backup": str(backup_path),
    }
    if comp.get("available"):
        c_errors = [d for d in comp["diagnostics"] if d.get("severity") == "error"]
        c_warnings = [d for d in comp["diagnostics"] if d.get("severity") == "warning"]
        new_errors = [
            d for d in c_errors
            if (d.get("code"), (d.get("sourceLocation") or {}).get("file"), d.get("message"))
            not in baseline_keys
        ]
        _persist_quality(specs_dir, new_errors, c_warnings, "compiler", comp.get("analyzers", []))
        result["compile"] = {"success": comp.get("success"), "errors": len(c_errors),
                             "new_errors": len(new_errors), "project": comp.get("project")}
        if new_errors:
            result["status"] = "deleted_with_dangling_references"
            result["message"] = (
                "The deletion introduced new compile errors — remaining code still references "
                "the removed object(s). Remove those references (bc_implement_write) before "
                "proceeding."
            )
            result["diagnostics"] = new_errors[:20]
            return result
    else:
        result["compile"] = {"success": None,
                             "note": comp.get("reason", "compiler unavailable — static-only")}
    _update_tasks_md(specs_dir, task_ids)
    return result


async def _execute_task(
    root: Path,
    specs_dir: Path,
    spec: Dict[str, Any],
    scope: ScopeEnforcer,
    task_id: str,
    dry_run: bool,
) -> Dict[str, Any]:
    """Execute a single implementation task with compile-and-fix loop.

    Flow:
      1. Parse task from TASKS.md
      2. Load module analysis + implementer prompt
      3. Generate AL code (delegated to AI model via hosting agent)
      4. Validate file path against scope
      5. Write file
      6. Compile via al_client.compile_extension
      7. If errors: feed diagnostics back, regenerate (max 3 attempts)
      8. On success: update TASKS.md status
      9. On failure after 3 attempts: mark task failed, report diagnostics
    """
    if dry_run:
        return {
            "task_id": task_id, "status": "dry_run_skipped",
            "files_created": [], "files_modified": [],
            "diagnostics": {"errors": [], "warnings": []},
            "compile_result": {"success": False, "error_count": 0},
            "message": "Dry run — no files written.",
        }

    # 1. Parse task from TASKS.md
    tasks_path = specs_dir / "TASKS.md"
    if not tasks_path.exists():
        raise MCPError(ErrorCode.CLIENT_ERROR, "TASKS.md not found",
                       hint="Run bc_breakdown_tasks first.")

    # 2. Load context files
    analysis_path = specs_dir / "analysis.md"
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "implementer.md"

    context = {
        "task_id": task_id,
        "tasks_file": str(tasks_path),
        "analysis_file": str(analysis_path) if analysis_path.exists() else None,
        "prompt_file": str(prompt_path) if prompt_path.exists() else None,
        "spec_file": str(specs_dir / "spec.json"),
        "scope_boundaries": spec.get("scope_boundaries", {}),
        "instructions": _build_implementation_instructions(tasks_path, task_id, spec),
    }

    return {
        "task_id": task_id,
        "status": "ready_for_model",
        "context": context,
        "files_created": [],
        "files_modified": [],
        "diagnostics": {"errors": [], "warnings": []},
        "compile_result": {"success": False, "error_count": 0},
        "message": "Task context prepared. Generate AL code following the implementer prompt.",
    }
