"""gate — mechanical commit/CI gate (Layer 2).

The only control that survives an agent hand-editing files *outside* the mcp: it acts where
code LANDS (commit/CI), not where it is typed. It rejects changes to spec-scoped code files
unless an approved Charter authorizes implementation. Pure core (`check`) takes an explicit
file list; the git/CLI wrapper is a thin seam.

Usage (git pre-commit hook, installed by ``install_hook``):
    python -m bc_agentic_mcp.gate --project-root <repo> --staged
Exit code 0 = allowed, 1 = blocked.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from bc_agentic_mcp import authorization, workspace

# File suffixes considered spec-scoped implementation code that the gate protects.
CODE_SUFFIXES = {".al"}

# Branches where direct implementation commits are disallowed by default.
PROTECTED_BRANCHES = {"main", "master", "trunk"}

# Spec-artifact files that must satisfy enforcement even when no .al files are staged.
# Format: relative path fragment that identifies files inside .specs/<spec>/.
SPEC_ARTIFACT_NAMES = {
    "clarifications.md",
    "spec.json",
    "REVIEW.md",
    "TASKS.md",
    "DESIGN.md",
}


def _spec_name_from_artifact_path(path: str) -> Optional[str]:
    """Extract the spec name from a path like .specs/<spec>/clarifications.md or
    similar. Returns None when the path does not match the pattern."""
    normalized = path.replace("\\", "/")
    # Match: .specs/<spec>/<artifact> (any nesting depth inside .specs/<spec>/)
    import re as _re
    m = _re.match(r"(?:.*[/\\])?\.specs/([^/]+)/", normalized)
    if m:
        candidate = m.group(1)
        # Ignore hidden/internal folders like .audit, .integrity
        if candidate.startswith("."):
            return None
        return candidate
    return None


def _normalize_repo_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("./")


def _current_branch(repo_root: Path) -> Optional[str]:
    for cmd in (
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "symbolic-ref", "--short", "HEAD"],
    ):
        try:
            out = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        branch = (out.stdout or "").strip()
        if branch and branch != "HEAD":
            return branch
    # Fallback for unborn branches where some git commands can be inconsistent.
    head_path = repo_root / ".git" / "HEAD"
    try:
        head_text = head_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if head_text.startswith(prefix):
        branch = head_text[len(prefix):].strip()
        return branch or None
    return None


def _normalize_branchish(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return re.sub(r"-+", "-", text)


def _branch_matches_spec(branch: str, spec_name: str) -> bool:
    branch_norm = _normalize_branchish(branch)
    spec_norm = _normalize_branchish(spec_name)
    if not branch_norm or not spec_norm:
        return False
    if branch_norm == spec_norm:
        return True
    if branch_norm.endswith(spec_norm):
        return True
    if f"-{spec_norm}-" in f"-{branch_norm}-":
        return True
    # Work-item NUMBER is the identity join key: branch 'user/bug-267600-remove-x'
    # and spec 'bug267600-x' are the SAME item even though normalization keeps them
    # textually distinct ('bug-267600' vs 'bug267600' — observed live, false negative
    # blocked a fully approved commit). A >=5-digit run unique to work items must
    # match as a whole token on both sides.
    spec_ids = set(re.findall(r"\d{5,}", spec_norm))
    if spec_ids:
        branch_tokens = set(re.findall(r"\d{5,}", branch_norm))
        if spec_ids & branch_tokens:
            return True
    return False


def _spec_allowed_code_files(repo_root: Path, spec: str) -> List[str]:
    sdir = workspace.specs_root(repo_root) / spec
    spec_path = sdir / "spec.json"
    if not spec_path.exists():
        return []
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    allowed = ((data.get("scope_boundaries") or {}).get("allowed_files") or [])
    normalized = []
    for item in allowed:
        p = _normalize_repo_path(str(item))
        if p and Path(p).suffix.lower() in CODE_SUFFIXES:
            normalized.append(p)
    return normalized


def _approved_scoped_spec_match(
    repo_root: Path,
    approved_specs: Sequence[str],
    code_files: Sequence[str],
) -> Dict[str, Any]:
    """Resolve changed code files against approved specs that declare allowed_files scopes.

    Returns:
      {
        "scoped_specs": [spec...],
        "matching_specs": [spec...],
      }
    """
    normalized_code = [_normalize_repo_path(f) for f in code_files]
    scoped_specs: List[str] = []
    matching_specs: List[str] = []

    for spec in approved_specs:
        allowed = _spec_allowed_code_files(repo_root, spec)
        if not allowed:
            continue
        scoped_specs.append(spec)
        if all(f in allowed for f in normalized_code):
            matching_specs.append(spec)

    return {
        "scoped_specs": scoped_specs,
        "matching_specs": matching_specs,
    }


def _feature_union_match(
    repo_root: Path,
    approved_specs: Sequence[str],
    code_files: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """One-feature model: integration fixes on a feature branch may legitimately span
    several child items (e.g. a cross-namespace `using` wave after the first integrated
    compile). A commit is feature-scoped when EVERY changed code file falls inside the
    union of allowed_files across the feature's APPROVED child specs.

    Returns {feature_spec, member_specs} for the first feature whose approved-children
    union covers all changed code files, else None.
    """
    sroot = workspace.specs_root(repo_root)
    if not sroot.exists():
        return None
    normalized = [_normalize_repo_path(f) for f in code_files]
    for spec_dir in sorted(p for p in sroot.iterdir() if p.is_dir()):
        if not (spec_dir / "feature_plan.json").exists():
            continue
        feature_spec = spec_dir.name
        try:
            from bc_agentic_mcp.tools.feature import feature_children_specs
            children = feature_children_specs(repo_root, feature_spec)
        except Exception:
            continue
        member_of: Dict[str, List[str]] = {}
        union: set = set()
        for child in children:
            child_spec = child.get("item_spec")
            if not child_spec or child_spec not in approved_specs:
                continue
            for f in _spec_allowed_code_files(repo_root, child_spec):
                union.add(f)
                member_of.setdefault(f, []).append(child_spec)
        if union and all(f in union for f in normalized):
            matched = sorted({s for f in normalized for s in member_of.get(f, [])})
            return {"feature_spec": feature_spec, "member_specs": matched}
    return None


def check(
    project_root: str,
    changed_files: Sequence[str],
    spec_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide whether the changed files may be committed. Pure/deterministic.

    Two mechanical layers, both must pass for spec-scoped AL code:
      1. an approved Charter authorizes implementation (human gate); and
      2. every required engine ran and passed for that spec (enforcement.engine_status):
         timeline + traceability + code-context + a green quality/analyzer run.

    Spec-artifact commits (only .specs/ files, no .al changes) are also gated:
    the relevant spec's enforcement engines must all pass, preventing commits of
    hand-edited spec artifacts (clarifications.md, spec.json, etc.) that bypass
    the MCP tool chain.
    """
    root = Path(project_root).resolve()
    code = [f for f in changed_files if Path(f).suffix.lower() in CODE_SUFFIXES]

    if not code:
        # No .al files — check if this is a spec-artifact-only commit that needs gating.
        artifact_specs: Dict[str, List[str]] = {}
        for f in changed_files:
            fname = Path(f).name
            if fname in SPEC_ARTIFACT_NAMES:
                sn = spec_name or _spec_name_from_artifact_path(f)
                if sn:
                    artifact_specs.setdefault(sn, []).append(f)

        if not artifact_specs:
            return {"allowed": True, "blocked": [], "reason": "no spec-scoped code files changed"}

        # For each spec whose artifacts are being committed, run enforcement.
        violations: List[str] = []
        all_engines: Dict[str, Any] = {}
        from bc_agentic_mcp import enforcement
        for sn, files in artifact_specs.items():
            status = enforcement.engine_status(root, sn)
            all_engines[sn] = status["engines"]
            if not status["all_ok"]:
                violations.append(
                    f"spec '{sn}': " + "; ".join(status["blocking"])
                )
        if violations:
            return {
                "allowed": False,
                "blocked": list({f for files in artifact_specs.values() for f in files}),
                "reason": (
                    "Spec artifact enforcement failed — spec-managed files were modified outside the "
                    "MCP tool chain. Resolve via the tool named in each engine's next_action, "
                    "then re-stage: " + " | ".join(violations)
                ),
                "engines": all_engines,
                "hint": (
                    "Direct edits to clarifications.md, spec.json, REVIEW.md etc. are blocked. "
                    "Use bc_answer_clarification / bc_write_spec / bc_prepare_review instead."
                ),
            }
        return {
            "allowed": True,
            "blocked": [],
            "reason": "spec artifact commit — all engines green for "
                      + ", ".join(sorted(artifact_specs)),
            "engines": all_engines,
        }

    if spec_name:
        authorized = authorization.implementation_authorized(root, spec_name)
        target_spec = spec_name if authorized else None
        approved_specs: List[str] = [spec_name] if authorized else []
    else:
        approved_specs = authorization.authorized_specs(root)
        authorized = bool(approved_specs)
        target_spec = approved_specs[0] if len(approved_specs) == 1 else None

    if not authorized:
        return {
            "allowed": False,
            "blocked": code,
            "reason": ("No approved charter authorizes changes to these spec-scoped files. "
                       "Get human approval (bc_request_approval -> bc_submit_decision) before committing."),
        }

    # Deterministic scope attribution: when approved specs declare explicit allowed_files,
    # changed code must map to exactly one such spec. This prevents mixed/bleeding commits
    # across specs and forces one-branch/one-scope hygiene.
    scope_match = _approved_scoped_spec_match(root, approved_specs, code)
    scoped_specs = scope_match["scoped_specs"]
    matching_specs = scope_match["matching_specs"]
    feature_match: Optional[Dict[str, Any]] = None
    if scoped_specs and len(matching_specs) != 1:
        # One-feature model: before blocking, check whether the files span approved
        # CHILD specs of one feature — integration fixes legitimately cross items.
        feature_match = _feature_union_match(root, approved_specs, code)
    if scoped_specs and feature_match is None:
        if len(matching_specs) == 1:
            target_spec = matching_specs[0]
        elif len(matching_specs) == 0:
            return {
                "allowed": False,
                "blocked": code,
                "reason": (
                    "Scope hygiene violation: changed code files do not match any approved spec scope "
                    "(scope_boundaries.allowed_files)."
                ),
                "approved_scoped_specs": scoped_specs,
            }
        else:
            return {
                "allowed": False,
                "blocked": code,
                "reason": (
                    "Scope hygiene violation: changed code files match multiple approved spec scopes. "
                    "Use a dedicated branch/commit per spec."
                ),
                "matching_specs": matching_specs,
            }

    # Branch hygiene: implementation commits should land on dedicated change branches,
    # never directly on protected branches.
    branch = _current_branch(root)
    if branch and branch.lower() in PROTECTED_BRANCHES:
        return {
            "allowed": False,
            "blocked": code,
            "reason": (
                f"Branch hygiene violation: current branch '{branch}' is protected. "
                "Create/use a dedicated feature branch per PR."
            ),
        }

    if feature_match is not None:
        feature_spec = feature_match["feature_spec"]
        if branch and not _branch_matches_spec(branch, feature_spec):
            return {
                "allowed": False,
                "blocked": code,
                "reason": (
                    f"Branch/feature mismatch: files span child specs of '{feature_spec}' "
                    f"but current branch '{branch}' does not map to that feature."
                ),
            }
        return {
            "allowed": True,
            "blocked": [],
            "reason": (
                f"feature-scope commit (one-feature model): files fall inside the approved "
                f"child-spec union of '{feature_spec}' "
                f"({', '.join(feature_match['member_specs'])})"
            ),
            "feature_spec": feature_spec,
            "member_specs": feature_match["member_specs"],
        }

    if branch and target_spec and not _branch_matches_spec(branch, target_spec):
        # One-feature model: a single-item fix commit from the FEATURE branch is fine
        # when the item is an approved child of the feature the branch belongs to.
        late_feature = _feature_union_match(root, approved_specs, code)
        if late_feature is not None and _branch_matches_spec(branch, late_feature["feature_spec"]):
            return {
                "allowed": True,
                "blocked": [],
                "reason": (
                    f"feature-scope commit (one-feature model): files fall inside the approved "
                    f"child-spec union of '{late_feature['feature_spec']}' "
                    f"({', '.join(late_feature['member_specs'])})"
                ),
                "feature_spec": late_feature["feature_spec"],
                "member_specs": late_feature["member_specs"],
            }
        return {
            "allowed": False,
            "blocked": code,
            "reason": (
                f"Branch/spec mismatch: current branch '{branch}' does not clearly map to spec '{target_spec}'. "
                "Use one dedicated implementation branch per spec/work item."
            ),
        }

    # Scope hygiene: when the resolved spec declares allowed files,
    # block out-of-scope AL changes in the same commit.
    if target_spec:
        allowed_code = _spec_allowed_code_files(root, target_spec)
        if allowed_code:
            normalized_code = [_normalize_repo_path(f) for f in code]
            out_of_scope = [f for f in normalized_code if f not in allowed_code]
            if out_of_scope:
                return {
                    "allowed": False,
                    "blocked": out_of_scope,
                    "reason": (
                        f"Scope hygiene violation for spec '{target_spec}': staged code files are outside "
                        "scope_boundaries.allowed_files."
                    ),
                }

    # Engine enforcement: when the commit is attributable to exactly one approved spec, that
    # spec's engines (timeline/traceability/code-context/quality) must all have run and passed.
    if target_spec:
        from bc_agentic_mcp import enforcement
        status = enforcement.engine_status(root, target_spec)
        if not status["all_ok"]:
            return {
                "allowed": False,
                "blocked": code,
                "reason": (f"Engine enforcement failed for spec '{target_spec}': "
                           + "; ".join(status["blocking"])),
                "engines": status["engines"],
            }
        return {
            "allowed": True, "blocked": [],
            "reason": f"approved charter + all engines green ({target_spec})",
            "engines": status["engines"],
        }

    return {"allowed": True, "blocked": [],
            "reason": "approved charter authorizes implementation (spec not uniquely attributable — engine checks skipped)"}



def _staged_files(repo_root: Path) -> List[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
            timeout=20, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


PRE_COMMIT_HOOK = """#!/bin/sh
# bc-agentic-mcp mechanical approval gate — do not commit spec-scoped code without approval.
python -m bc_agentic_mcp.gate --project-root "$(git rev-parse --show-toplevel)" --staged
exit $?
"""


def _is_zero_sha(value: str) -> bool:
    return bool(value) and set(value) == {"0"}


def _diff_name_only(repo_root: Path, base_ref: str, head_ref: str) -> List[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}..{head_ref}"],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
            timeout=20, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _diff_single_commit(repo_root: Path, commit_sha: str) -> List[str]:
    try:
        out = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
            timeout=20, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _pushed_files(repo_root: Path, updates: str) -> List[str]:
    """Resolve changed files for pre-push updates read from stdin.

    Input lines are: <local_ref> <local_sha> <remote_ref> <remote_sha>
    """
    files: List[str] = []
    seen: set[str] = set()
    for raw in (updates or "").splitlines():
        parts = raw.strip().split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if not local_sha or _is_zero_sha(local_sha):
            continue
        if not remote_sha or _is_zero_sha(remote_sha):
            candidates = _diff_single_commit(repo_root, local_sha)
        else:
            candidates = _diff_name_only(repo_root, remote_sha, local_sha)
        for f in candidates:
            if f not in seen:
                files.append(f)
                seen.add(f)
    return files


def _render_hook(specs_root: Optional[str] = None) -> str:
    """Build the pre-commit hook body, baking in the external specs base if given.

    The hook runs in a separate git process that does not inherit the server's
    environment, so the location must be embedded when artifacts are external.
    """
    extra = f' --specs-root "{specs_root}"' if specs_root else ""
    return (
        "#!/bin/sh\n"
        "# bc-agentic-mcp mechanical approval gate — do not commit spec-scoped code without approval.\n"
        f'python -m bc_agentic_mcp.gate --project-root "$(git rev-parse --show-toplevel)"{extra} --staged\n'
        "exit $?\n"
    )


def _render_pre_push_hook(specs_root: Optional[str] = None) -> str:
    extra = f' --specs-root "{specs_root}"' if specs_root else ""
    return (
        "#!/bin/sh\n"
        "# bc-agentic-mcp mechanical push gate — block pushing unauthorized spec-scoped code.\n"
        f'python -m bc_agentic_mcp.gate --project-root "$(git rev-parse --show-toplevel)"{extra} --pre-push\n'
        "exit $?\n"
    )


def install_hook(repo_root: str, specs_root: Optional[str] = None) -> Dict[str, Any]:
    """Install the pre-commit hook into a git repo. Reversible (local .git/hooks only).

    ``specs_root`` (the external base) is baked into the hook so the standalone
    git process resolves governance artifacts at the same external location the
    server uses.
    """
    hooks_dir = Path(repo_root).resolve() / ".git" / "hooks"
    if not hooks_dir.parent.is_dir():
        return {"installed": False, "reason": f"{repo_root} is not a git repository"}
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text(_render_hook(specs_root), encoding="utf-8")
    push_hook_path = hooks_dir / "pre-push"
    push_hook_path.write_text(_render_pre_push_hook(specs_root), encoding="utf-8")
    try:
        hook_path.chmod(0o755)
        push_hook_path.chmod(0o755)
    except OSError:
        pass
    return {"installed": True, "path": str(hook_path), "pre_push_path": str(push_hook_path)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="bc-agentic-mcp approval gate")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--specs-root", default=None,
                        help="External base dir for governance artifacts (.specs).")
    parser.add_argument("--spec", default=None)
    parser.add_argument("--staged", action="store_true", help="Read staged files via git")
    parser.add_argument("--pre-push", action="store_true", help="Read push updates from stdin (pre-push hook mode)")
    parser.add_argument("--from-ref", default=None, help="Base git ref for CI diff mode")
    parser.add_argument("--to-ref", default=None, help="Head git ref for CI diff mode")
    parser.add_argument("--files", nargs="*", default=None)
    args = parser.parse_args(argv)

    if args.specs_root:
        os.environ[workspace.ENV_VAR] = args.specs_root

    if args.pre_push:
        files = _pushed_files(Path(args.project_root), sys.stdin.read())
    elif args.from_ref and args.to_ref:
        files = _diff_name_only(Path(args.project_root), args.from_ref, args.to_ref)
    elif args.staged:
        files = _staged_files(Path(args.project_root))
    else:
        files = list(args.files or [])

    result = check(args.project_root, files, spec_name=args.spec)
    if result["allowed"]:
        print(f"gate: OK — {result['reason']}")
        return 0
    print("gate: BLOCKED — " + result["reason"], file=sys.stderr)
    for f in result["blocked"]:
        print(f"  - {f}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
