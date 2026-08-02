"""bc_run_tests — execute AL tests in a BC container and capture the result as evidence.

A4: when ``app_project_folder`` is supplied the tool owns the FULL cycle
(sync -> compile -> dev-endpoint publish -> run) so the sanctioned path is the
easiest path. A1: every mode is gated on a fresh, passing environment preflight.

Feature model: install ONCE per iteration (``app_project_folder``), then test by
SLICE (``test_codeunit``). The install records WHICH commit it published; a slice
run against a moved branch is refused — silently testing stale code is the
failure mode this gate kills.
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from bc_agentic_mcp import al_runner, env_preflight, security, verification
from bc_agentic_mcp.validation import validate_covers, validate_validation_mode
from bc_agentic_mcp.workspace import specs_root


def _head_sha(repo_dir: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=15, stdin=subprocess.DEVNULL, check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _install_manifest_path(project_root: Path, container_name: str) -> Path:
    return specs_root(project_root) / ".env" / f"{container_name}-install.json"


async def handle_run_tests(
    project_root: str,
    container_name: str,
    test_extension_id: str,
    credential_env: str = "BC_TEST_PASSWORD",
    user: str = "admin",
    tenant: str = "default",
    spec_name: Optional[str] = None,
    covers: Optional[Union[str, List[int]]] = None,
    validation_mode: str = "item",
    app_project_folder: Optional[str] = None,
    test_codeunit: Optional[str] = None,
    publish_only: bool = False,
) -> Dict[str, Any]:
    """Run a published test extension in ``container_name`` and (optionally) record evidence.

    Nothing is hardcoded: container/app/test-id/credentials are inputs and the password is
    read from ``credential_env`` inside the child process. When ``spec_name`` + ``covers``
    are supplied, the *captured* run result is recorded as runtime evidence. Supplying
    ``app_project_folder`` (host path to the test app sources) makes the tool perform the
    full sync -> compile -> publish -> run cycle before running.
    """
    # Poka-yoke (F1): refuse loose values at the boundary, before any container I/O.
    mode = validate_validation_mode(validation_mode)
    if covers is not None:
        validate_covers(covers)

    # A1 gate: container work requires a fresh, PASSING environment preflight —
    # environment truth is checked in 30 seconds, not discovered by failing at it.
    gate = env_preflight.require_fresh(Path(project_root).resolve(), container_name)
    if not gate["ok"]:
        return {
            "status": "blocked_env_preflight",
            "blocked": True,
            "executed": False,
            "reason": gate["reason"],
            "next_action": gate["next_action"],
        }
    manifest = gate["manifest"]

    # The container USER is environment truth: default from the preflight manifest
    # (probed devadmin/admin) unless the caller explicitly overrides it.
    if user == "admin" and manifest.get("container_user"):
        user = str(manifest["container_user"])

    root = Path(project_root).resolve()
    if app_project_folder:
        # Cross-process container mutex: two publishes against the same tenant abort
        # each other ('another service is currently modifying the state of extensions').
        try:
            with al_runner.container_mutex(container_name):
                result = al_runner.run_full_cycle(
                    container_name=container_name,
                    app_project_folder=app_project_folder,
                    test_extension_id=test_extension_id,
                    fingerprint=str(manifest.get("fingerprint", "unknown")),
                    credential_env=credential_env,
                    user=user,
                    tenant=tenant,
                    test_codeunit=test_codeunit,
                    publish_only=publish_only,
                )
        except TimeoutError as exc:
            return {"status": "blocked_container_busy", "blocked": True,
                    "executed": False, "reason": str(exc)}
        # Record WHAT was installed (commit + apps), so slice runs can detect drift.
        if result.get("executed") and not publish_only:
            sha = _head_sha(app_project_folder)
            try:
                p = _install_manifest_path(root, container_name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({
                    "container": container_name,
                    "installed_sha": sha,
                    "app_project_folder": app_project_folder,
                    "test_extension_id": test_extension_id,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }, indent=2), encoding="utf-8")
                result["installed_sha"] = sha
            except OSError:
                pass
    else:
        # SLICE run — refuse when the install is stale (branch moved since publish).
        if test_codeunit:
            mp = _install_manifest_path(root, container_name)
            if not mp.exists():
                return {
                    "status": "blocked_no_install",
                    "blocked": True, "executed": False,
                    "reason": ("No install manifest for this container — run the install "
                               "cycle first (bc_run_tests with app_project_folder), then "
                               "test by slice."),
                }
            try:
                inst = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                inst = {}
            # FOREIGN-INSTALL wall: the spec store is keyed by REPO, so a sibling
            # worktree's publish writes the same manifest path. Its sha check would
            # then compare the SIBLING's branch to itself and wave the slice through
            # while the container runs someone else's binaries (observed live:
            # wi267598 slice "passed 3/3" against wt-240435's TestApp — proof theater).
            installed_folder = str(inst.get("app_project_folder") or "")
            same_tree = False
            if installed_folder:
                try:
                    Path(installed_folder).resolve().relative_to(root)
                    same_tree = True
                except (OSError, ValueError):
                    same_tree = False
            if not same_tree:
                return {
                    "status": "blocked_foreign_install",
                    "blocked": True, "executed": False,
                    "reason": (f"Container '{container_name}' was installed from "
                               f"'{installed_folder or 'unknown'}' — a DIFFERENT worktree "
                               f"than '{root}'. A slice run would silently test someone "
                               "else's binaries. Run the install cycle from THIS worktree "
                               "first (bc_run_tests with app_project_folder)."),
                    "installed_from": installed_folder,
                }
            installed = inst.get("installed_sha")
            current = _head_sha(str(root))
            if installed and current and installed != current:
                return {
                    "status": "blocked_stale_install",
                    "blocked": True, "executed": False,
                    "reason": (f"Install is STALE: container has {installed[:12]} but the "
                               f"branch is at {current[:12]} — a slice run would silently "
                               "test old code. Re-run the install cycle first."),
                    "installed_sha": installed, "branch_sha": current,
                }
        result = al_runner.run_container_tests(
            container_name=container_name,
            test_extension_id=test_extension_id,
            credential_env=credential_env,
            user=user,
            tenant=tenant,
            test_codeunit=test_codeunit,
        )
    if spec_name and covers is not None and result.get("executed"):
        layer = "al-regression" if mode == "regression" else "al-unit"
        # Path-shape counts from the EXECUTED test names — feeds the path-coverage
        # validation class (happy AND negative AND edge required for every lane).
        executed_names = [t.get("name", "") for cu in result.get("codeunits", [])
                          for t in cu.get("tests", [])]
        shape = verification.classify_test_paths(executed_names)
        evidence = (
            f"container={container_name} ext={test_extension_id} "
            f"passed={result.get('passed')}/{result.get('total')} "
            f"exit={result.get('exit_code')} mode={mode} "
            f"paths=happy:{shape['happy']},negative:{shape['negative']},edge:{shape['edge']}"
        )
        evidence_receipt = security.issue_evidence(
            project_root=Path(project_root).resolve(),
            spec_name=spec_name,
            producer="bc_run_tests",
            name=f"AL run {result.get('passed')}/{result.get('total')} ({container_name})",
            result="pass" if result.get("all_passed") else "fail",
            covers=covers,
            layer=layer,
            evidence=evidence,
        )
        # EXPLICIT per-test visibility (PBI-template shape): the caller — and the
        # human — must see WHICH tests ran and WHAT shape each one proves, not a
        # bare pass count. Also persisted as TEST-REPORT.md in the spec folder.
        declared = _declared_shapes(Path(project_root).resolve(), spec_name)
        executed_tests = [
            {"codeunit": cu.get("name") or cu.get("id"),
             "test": t.get("name", ""),
             "shape": _shape_of(t.get("name", ""), declared),
             "result": t.get("result", "")}
            for cu in result.get("codeunits", [])
            for t in cu.get("tests", [])
        ]
        result["executed_tests"] = executed_tests
        result["path_shapes"] = shape
        verification.record_test(
            Path(project_root).resolve(),
            spec_name,
            name=f"AL run {result.get('passed')}/{result.get('total')} ({container_name})",
            result="pass" if result.get("all_passed") else "fail",
            covers=covers,
            layer=layer,
            evidence=evidence,
            executed_tests=executed_tests,
            failures=result.get("failures") or None,
            evidence_receipt=evidence_receipt,
        )
        result["evidence_recorded"] = True
        result["evidence_receipt"] = evidence_receipt
        result["validation_mode"] = mode
        try:
            result["test_report_path"] = _write_test_report(
                Path(project_root).resolve(), spec_name)
        except OSError:
            pass
    return result


_SHAPE_STOPWORDS = {"a", "an", "the", "is", "are", "and", "with", "for", "then",
                    "when", "given", "to", "of", "in", "on", "at", "by", "check"}


def _stmt_tokens(text: str) -> "set[str]":
    import re as _re
    spaced = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))
    return {t.lower() for t in _re.split(r"[^A-Za-z0-9]+", spaced) if t}


def _declared_shapes(project_root: Path, spec_name: str) -> List[Dict[str, Any]]:
    """AT statements + their DECLARED path_shape from the spec (spec truth)."""
    if not spec_name:
        return []
    try:
        from bc_agentic_mcp.spec_loader import load_spec
        spec = load_spec(specs_root(project_root) / spec_name)
    except Exception:
        return []
    return [{"tokens": _stmt_tokens(at.get("statement", "")), "shape": str(at["path_shape"])}
            for at in (spec.get("acceptance_tests") or []) if at.get("path_shape")]


def _shape_of(name: str, declared: Optional[List[Dict[str, Any]]] = None) -> str:
    """Shape of one executed test: the spec's DECLARED shape wins when the test
    name unambiguously maps to one acceptance test; the name classifier is the
    fallback. (Retro wi267598: name heuristics called 'CreationRefused' tests
    happy and the lowest-Volgnr edge a happy path — the spec knew better all along.)
    Deterministic: an override happens ONLY when every matching AT agrees.
    """
    if declared:
        ntoks = _stmt_tokens(name) - _SHAPE_STOPWORDS
        if ntoks:
            shapes = {d["shape"] for d in declared if ntoks <= d["tokens"]}
            if len(shapes) == 1:
                return shapes.pop()
    counts = verification.classify_test_paths([name])
    for shape in ("edge", "negative", "happy"):
        if counts.get(shape):
            return shape
    return "happy"


def _write_test_report(project_root: Path, spec_name: str) -> str:
    """Render TEST-REPORT.md: every recorded run with its executed tests + shapes,
    cumulative shape coverage, and the validation-class status — the same explicit
    template the PBI lane declares (happy / negative / edge / regression / api)."""
    from bc_agentic_mcp import checkpoints as memory
    tests = [c for c in memory.load_checkpoints(project_root, spec_name)
             if c.get("kind") == "test"]
    digest = verification.build_verification(project_root, spec_name)
    classes = digest.get("validation_classes") or {}
    lines = [f"# Test Report — {spec_name}", "",
             "## Recorded runs (newest last)", ""]
    for t in tests:
        d = t.get("details") or {}
        lines.append(f"### {t.get('summary')} — {str(d.get('result', '')).upper()} "
                     f"(layer: {d.get('layer', '?')})")
        lines.append(f"- evidence: {d.get('evidence', '')}")
        for f in d.get("failures") or []:
            lines.append(f"- FAILED {f.get('test', '?')}: {f.get('error', '')}")
        lines.append("")
    counts = (classes.get("path-coverage") or {}).get("counts") or {}
    lines += ["## Cumulative shape coverage (executed empiric tests)", "",
              f"- happy: {counts.get('happy', 0)}",
              f"- negative: {counts.get('negative', 0)}",
              f"- edge: {counts.get('edge', 0)}", "",
              "## Validation classes", ""]
    for cname, state in classes.items():
        mark = "OK" if state.get("ok") else ("REQUIRED — MISSING" if state.get("required") else "n/a")
        lines.append(f"- {cname}: {mark}" + (f" — {state['reason']}" if state.get("reason") else ""))
    out = specs_root(project_root) / spec_name / "TEST-REPORT.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
