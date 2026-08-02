"""BC Agentic MCP Server — main entry point with infrastructure wiring.

Target protocol: MCP 2026-07-28. Current FastMCP version may lag.

Migration checklist for full 2026-07-28 compliance:
  1. server/discover RPC handler (list all tools in discover format)
  2. Mcp-Method / Mcp-Name headers on every response
  3. Error code migration from -32002 to -32602
  4. Streamable HTTP transport support (SSE-based)
  5. ttlMs and cacheScope metadata in tools/list response

Infrastructure lifecycle:
  1. Config loaded (ServerConfig)
  2. Rate limiter instantiated per-session
  3. Audit logger instantiated (writes to .specs/.audit/)
  4. AL tool discovered (graceful degradation)
  5. All registered tools use rate-limit + audit wrappers
  6. Tool integrity verification (hash-pinning, GAP 7)
"""
import argparse
import asyncio
import inspect
import math
import os
import sys
import time
from pathlib import Path
from bc_agentic_mcp.workspace import ENV_VAR as specs_env, specs_root
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP

from bc_agentic_mcp import __version__
from bc_agentic_mcp.config import ServerConfig, discover_al_tool, discover_app_json
from bc_agentic_mcp.rate_limiter import RateLimiter
from bc_agentic_mcp.audit import AuditLogger
from bc_agentic_mcp.errors import MCPError, ErrorCode, error_response
from bc_agentic_mcp import attempts
from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import security
from bc_agentic_mcp import timeline
from bc_agentic_mcp.path_enforcement import enforce_response_paths
from bc_agentic_mcp.workflow_policy import check_tool_call, infer_stage

# --- Tool handlers ---
from bc_agentic_mcp.tools.init import handle_init
from bc_agentic_mcp.tools.analyze import handle_analyze_module
from bc_agentic_mcp.tools.clarify import handle_clarify
from bc_agentic_mcp.tools.answer_clarification import handle_answer_clarification
from bc_agentic_mcp.tools.write_spec import handle_write_spec
from bc_agentic_mcp.tools.plan_design import handle_plan_design
from bc_agentic_mcp.tools.breakdown_tasks import handle_breakdown_tasks
from bc_agentic_mcp.tools.prepare_review import handle_prepare_review
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision
from bc_agentic_mcp.tools.status import handle_status
from bc_agentic_mcp.tools.recall import handle_recall, handle_checkpoint
from bc_agentic_mcp.tools.verify import handle_verify, handle_record_test
from bc_agentic_mcp.tools.implement import (
    handle_implement_alias,
    handle_implement_context,
    handle_implement_delete,
    handle_implement_write,
)
from bc_agentic_mcp.tools.pr import (
    handle_create_pr,
    handle_get_review_comments,
    handle_merge_status,
    handle_prepare_pr,
    handle_resolve_review_comment,
    handle_sync_item_state,
)
from bc_agentic_mcp.tools.advance import handle_advance
from bc_agentic_mcp.tools.auto_clarify import handle_auto_clarify
from bc_agentic_mcp.tools.feature import (
    handle_capture_feature,
    handle_feature_status,
    handle_plan_feature,
    handle_prepare_feature_review,
    handle_refine_feature,
    handle_refine_item,
    handle_repo_map,
)
from bc_agentic_mcp.metrics import handle_metrics
from bc_agentic_mcp.tools.generate_tests import handle_generate_tests
from bc_agentic_mcp.tools.upgrade_codeunit import handle_upgrade_codeunit
from bc_agentic_mcp.tools.converge import handle_converge
from bc_agentic_mcp.tools.quality_check import handle_quality_check
from bc_agentic_mcp.tools.feedback import handle_feedback
from bc_agentic_mcp.tools.archive import handle_archive
from bc_agentic_mcp.tools.lessons_tool import handle_lessons, handle_promote_lesson
from bc_agentic_mcp.tools.run_tests import handle_run_tests
from bc_agentic_mcp.tools.env_preflight import handle_env_preflight
from bc_agentic_mcp.tools.api_contract_tool import handle_api_contract
from bc_agentic_mcp.tools.schema_tools import handle_reconcile_target, handle_upgrade_preflight
from bc_agentic_mcp.tools.consumers_tool import handle_find_consumers
from bc_agentic_mcp.tools.references_tool import handle_extract_references
from bc_agentic_mcp.tools.wiki_tool import handle_fetch_wiki
from bc_agentic_mcp.tools.worktree import handle_worktree
from bc_agentic_mcp.intake import (
    handle_intake_add,
    handle_intake_analyze,
    handle_intake_graduate,
    handle_intake_start,
)
from bc_agentic_mcp.consistency import handle_analyze_consistency
from bc_agentic_mcp.tool_health import handle_tool_health
from bc_agentic_mcp.ado_items import handle_push_items
from bc_agentic_mcp.tools.item_context_tool import handle_capture_item_context
from bc_agentic_mcp.tools.root_cause import handle_root_cause
from bc_agentic_mcp.tools.permissions_tool import handle_check_permission_coverage
from bc_agentic_mcp.tools.reflect_tool import handle_reflect
from bc_agentic_mcp.tools.pr_thread_guard import handle_guard_pr_thread_resolution
from bc_agentic_mcp.detectors import handle_detect
from bc_agentic_mcp.review import handle_review
from bc_agentic_mcp.code_context import handle_read_code_context
from bc_agentic_mcp.tools.knowledge_tool import handle_get_knowledge_article


# ---------------------------------------------------------------------------
# Tool wrapper: injects rate limiting + audit logging around every call
# ---------------------------------------------------------------------------


class ToolContext:
    """Infrastructure injected into every tool call."""

    __slots__ = ("config", "rate_limiter", "audit", "agent_role")

    def __init__(self, config: ServerConfig, rate_limiter: RateLimiter, audit: AuditLogger, agent_role: str = "orchestrator"):
        self.config = config
        self.rate_limiter = rate_limiter
        self.audit = audit
        self.agent_role = agent_role


_ctx: Optional[ToolContext] = None  # module-level singleton, set at boot

# A2: one operation at a time per BC container — overlapping runs cause server-side
# transaction rollbacks and corrupt result files (observed live on WI 239597).
_container_locks: Dict[str, asyncio.Lock] = {}

# C2: inline tool output cap. Anything larger is written to .specs/<item>/logs/ and
# summarized, so one verbose compile can never flood the context window again.
_MAX_INLINE_VALUE_CHARS = 4000
_LOG_KEYS = {"stdout", "stderr", "output", "raw_output", "log", "compile_output"}

# Per-tool timeout overrides: container cycles (sync+compile+publish+run) legitimately
# take minutes — the default 60s would kill them mid-compile.
_TOOL_TIMEOUT_OVERRIDES = {
    "bc_run_tests": 2400,
    "bc_env_preflight": 300,
    "bc_api_contract": 300,
    # First index build walks the whole repo (schema-3 full parse); refreshes are
    # stat-only and fast. The one-time cost is the point: pay it once, never again.
    "bc_refine_feature": 600,
    "bc_refine_item": 600,
    "bc_root_cause": 600,
    "bc_repo_map": 600,
    # bc_verify walks the object index (api_pages_touching reverse lookup) — after an
    # implement_write invalidates the index, the rebuild alone exceeds the 60s default
    # (observed live: two timeouts at exactly 60s on a 12k-file repo).
    "bc_verify": 600,
    # bc_detect scans the full diff surface for mistake patterns (observed live: two
    # timeouts at exactly 60s on wi267598, a 3-file change on the ERP AL repo — the
    # scan cost scales with repo size, not diff size).
    "bc_detect": 600,
    # bc_prepare_pr runs the pipeline-truth walls: verification gate + breaking-change
    # diff scan + dependent-closure compile (up to 8 local alc runs at ~60-120s each).
    # The 60s default timed out the FIRST run of the new wall (same lesson class as
    # bc_verify: a tool that gains expensive work needs its budget moved with it).
    "bc_prepare_pr": 1200,
    # One-prompt planner runs module analysis on large modules.
    "bc_prepare_review": 600,
    # Precedent scan walks module sources (pre-index design; rewire to the index later).
    "bc_read_code_context": 600,
    # Scope-enforced write compiles the file with alc.exe against ~140 symbol apps —
    # the first compile in a session loads the full symbol set (observed 60s+).
    "bc_implement_write": 900,
    "bc_implement_delete": 900,
    "bc_implement": 900,
    # Spec generation refreshes the object index; an expired TTL means a stat-refresh
    # of ~12k files inside the handler (observed 63s — timed out at the 60s default
    # and the orphaned thread then poisoned the idempotency key).
    "bc_write_spec": 300,
    "bc_prepare_feature_review": 300,
}


def _get_container_lock(container_name: str) -> asyncio.Lock:
    lock = _container_locks.get(container_name)
    if lock is None:
        lock = asyncio.Lock()
        _container_locks[container_name] = lock
    return lock


def _cap_large_outputs(result: Any, project_root: Optional[str], spec_name: Optional[str], tool: str) -> Any:
    """Cap oversized string values inline; write the full text to a log file on disk.

    Only known output-carrying keys are capped so structured payloads stay intact.
    Fail-open: if the log cannot be written, the value is truncated with a notice.
    """
    if not isinstance(result, dict):
        return result
    for key in list(result.keys()):
        value = result.get(key)
        if key not in _LOG_KEYS or not isinstance(value, str) or len(value) <= _MAX_INLINE_VALUE_CHARS:
            continue
        log_path = ""
        if project_root and spec_name:
            try:
                logs_dir = specs_root(Path(project_root).resolve()) / spec_name / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                log_file = logs_dir / f"{tool}-{key}-{stamp}.log"
                log_file.write_text(value, encoding="utf-8")
                log_path = str(log_file)
            except OSError:
                log_path = ""
        head = value[:1500]
        tail = value[-1500:]
        result[key] = (
            f"{head}\n... [{len(value)} chars total — inline output capped"
            + (f"; full output: {log_path}" if log_path else "")
            + f"] ...\n{tail}"
        )
        if log_path:
            result.setdefault("log_paths", []).append(log_path)
    return result


def _get_ctx() -> ToolContext:
    """Get the module-level ToolContext singleton."""
    assert _ctx is not None, "ToolContext not initialized — server must call create_server()"
    return _ctx


def _attach_reanchor(result: Any, kwargs: Dict[str, Any]) -> Any:
    """Bullet-proof memory: echo the pinned Charter purpose/operations on every
    spec-scoped tool response, so the item's intent is re-injected into context on
    each call and cannot silently drift. No-op when there is no charter for the spec.
    """
    if not isinstance(result, dict):
        return result
    spec_name = kwargs.get("spec_name")
    project_root = kwargs.get("project_root")
    if not spec_name or not project_root:
        return result
    try:
        charter = memory.load_charter(Path(project_root).resolve(), spec_name)
    except Exception:
        return result
    if not charter:
        return result
    ops = charter.get("operations", {})
    ops_line = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in ops.items()) or "(unspecified)"
    # Explicit identity first: WHICH work item is this (PBI vs Feature vs Bug), its
    # title, and its parent — so no reader ever has to guess what "the feature" means.
    identity_line = ""
    try:
        from bc_agentic_mcp import item_context as _ic
        ctx = _ic.load_context(str(project_root), spec_name) or {}
        ident = ctx.get("identity") or {}
        if ident.get("id"):
            identity_line = f"ITEM: {ident.get('type', 'work item')} {ident['id']} — '{ident.get('title', '')}'"
            if ident.get("parent_id"):
                identity_line += (f" (child of {ident.get('parent_type', 'parent')} "
                                  f"{ident['parent_id']} '{ident.get('parent_title', '')}')")
            identity_line += " | "
    except Exception:
        identity_line = ""
    result.setdefault(
        "reanchor",
        f"{identity_line}PURPOSE: {charter.get('purpose', '(none)')} | OPERATIONS IN SCOPE: {ops_line}",
    )
    result.setdefault("charter", {"purpose": charter.get("purpose"), "operations": ops})
    # Automatic self-reflection: if mistakes/corrections/overrides were checkpointed since the
    # last reflection, surface a nudge on EVERY spec-scoped response so the agent records the
    # lesson (bc_reflect) without the user having to ask.
    try:
        from bc_agentic_mcp import reflection as _reflection
        pending = _reflection.pending_reflections(Path(project_root).resolve(), spec_name)
        if pending["count"] > 0:
            result.setdefault(
                "reflection_due",
                {
                    "count": pending["count"],
                    "signals": pending["signals"],
                    "action": "Call bc_reflect to record the lesson(s) before continuing.",
                },
            )
    except Exception:
        pass
    # Lifecycle timeline: surface the compact, always-in-context story of this item so far.
    try:
        tl = timeline.digest(Path(project_root).resolve(), spec_name)
        if tl:
            result.setdefault("timeline", tl)
    except Exception:
        pass
    return result


def _apply_envelope(result: Any, kwargs: Dict[str, Any]) -> Any:
    """F4: uniform result envelope — every dict result carries {ok, stage, artifacts}.

    Additive only (existing keys always win) so no caller breaks. ``ok`` is derived
    from the same deterministic markers the learn loop reads; ``artifacts`` collects
    the *_path values tools already return.
    """
    if not isinstance(result, dict):
        return result
    if "ok" not in result:
        status = str(result.get("status", ""))
        result["ok"] = not (
            result.get("isError") is True
            or result.get("blocked") is True
            or "error" in result  # error-shaped results are never ok
            or status.startswith(("blocked", "error", "failed"))
            # Runner truth (live finding 2026-07-06): a run with a failed step or a
            # red verdict summarized as ok=true misled the whole evidence sequence.
            or bool(result.get("failed_step"))
            or result.get("all_passed") is False
        )
    # TWO-AUDIENCE RULE for asking states (literalist persona finding 2026-07-06):
    # a needs_* response must carry BOTH a human reason and an executable
    # next_action, or a weak agent dies one tool call away from the fix.
    status_now = str(result.get("status", ""))
    if status_now.startswith("needs"):
        questions = result.get("questions") or []
        if not (result.get("reason") or result.get("message")):
            result["reason"] = (
                f"{len(questions)} clarification question(s) must be answered before this "
                "step can proceed — see 'questions' for the exact asks."
                if questions else
                "This step needs more input before it can proceed — see the response fields."
            )
        if "next_action" not in result and questions:
            result["next_action"] = {
                "tool": "bc_answer_clarification",
                "params_hint": {
                    "spec_name": kwargs.get("spec_name"),
                    "answers": {str(q.get("id")): "<your answer with .al evidence>"
                                for q in questions if isinstance(q, dict) and q.get("id")},
                },
            }
    if "stage" not in result:
        try:
            result["stage"] = infer_stage(kwargs.get("project_root"), kwargs.get("spec_name"))
        except Exception:
            result["stage"] = "plan"
    if "artifacts" not in result:
        result["artifacts"] = [
            v for k, v in result.items()
            if isinstance(v, str) and k.endswith("_path")
        ][:8]
    # Plain-language narrative: WHERE the item stands / WHAT happens next / WHO acts —
    # a human must never need the phase-name glossary to follow a response.
    if "human" not in result and kwargs.get("spec_name") and kwargs.get("project_root"):
        try:
            from bc_agentic_mcp import item_context as _ic
            from bc_agentic_mcp import narrator as _narrator
            phase = timeline.current_phase(
                Path(kwargs["project_root"]).resolve(), kwargs["spec_name"])
            story = _narrator.explain_phase(
                phase, lane=_ic.lane(kwargs["project_root"], kwargs["spec_name"]))
            if story:
                result["human"] = story
        except Exception:
            pass
    return result


async def _run_tool(tool_name: str, handler, session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Rate-limit, execute, audit-log, error-wrap a tool handler.

    Async handlers run directly; sync handlers are offloaded to a worker thread
    so one slow analysis cannot block the MCP event loop.
    """
    ctx = _get_ctx()

    # 1. Rate limit check. Consume tokens at the live boundary; a non-consuming
    # check only reports capacity and leaves the server unbounded.
    allowed, retry_after = ctx.rate_limiter.consume(tool_name)
    if not allowed:
        retry_after_int = max(1, math.ceil(retry_after))
        return error_response(
            ErrorCode.CLIENT_ERROR,
            f"Rate limit exceeded for {tool_name}",
            hint=f"Wait {retry_after:.1f}s before retrying",
            retry_after=retry_after_int,
        )

    # 1b. MCP orchestration policy check (role + deterministic stage routing)
    if tool_name.startswith("bc_") or tool_name == "_health":
        allowed, meta = check_tool_call(
            tool_name=tool_name,
            agent_role=ctx.agent_role,
            project_root=kwargs.get("project_root"),
            spec_name=kwargs.get("spec_name"),
        )
        if not allowed:
            # Repeater-persona finding (2026-07-06): a weak agent hammers the same
            # policy-refused call forever — gate blocks are ledger-neutral by design,
            # so nothing ever told it to STOP. Escalate the hint on a streak.
            _fp = attempts.param_fingerprint(tool_name, kwargs)
            _streak = attempts.note_refusal(_fp)
            _hint = meta.get("hint") or ""
            if _streak >= attempts.MAX_IDENTICAL_REFUSALS:
                _hint = (
                    f"STOP: this identical call has now been refused {_streak} times in a row — "
                    "repeating it cannot succeed. Call bc_status with the spec_name and execute "
                    "its FIRST next_actions entry instead. " + _hint
                )
            return error_response(
                ErrorCode.CLIENT_ERROR,
                f"Policy blocked tool call '{tool_name}'",
                hint=_hint,
                details={
                    "policy": meta,
                    "tool": tool_name,
                    "role": ctx.agent_role,
                    "identical_refusals": _streak,
                    "next_action": {"tool": "bc_status",
                                    "params_hint": {"spec_name": kwargs.get("spec_name")}},
                },
            )

    # 1c. Learn-loop TRY/RETRY guard: refuse an identical approach that already failed
    # MAX_IDENTICAL_FAILURES times for this item (doom-loop killer). Deterministic.
    attempt = attempts.check_attempt(
        kwargs.get("project_root"), kwargs.get("spec_name"), tool_name, kwargs
    )
    if not attempt["allowed"]:
        refusal = attempt["refusal"]
        return error_response(
            ErrorCode.CLIENT_ERROR,
            f"Doom-loop guard: {refusal['reason']}",
            hint=f"Last error ({'/'.join(refusal['error_classes'])}): {refusal['last_error'][:200]}",
            details={"prior_failures": attempt["prior_failures"], "fingerprint": attempt["fingerprint"]},
        )

    # A2: serialize operations against the same BC container (prevents transaction
    # rollbacks from overlapping runs).
    container_name = kwargs.get("container_name") if isinstance(kwargs.get("container_name"), str) else None
    container_lock = _get_container_lock(container_name) if container_name else None

    # 2. Execute handler (supports both sync and async handlers)
    started = time.monotonic()
    try:
        timeout_seconds = _TOOL_TIMEOUT_OVERRIDES.get(
            tool_name, _get_ctx().config.tool_timeout_seconds
        )
        if container_lock is not None:
            await container_lock.acquire()
        try:
            if inspect.iscoroutinefunction(handler):
                # Run the coroutine on a PRIVATE loop in a worker thread. Most handlers
                # are async-signature but sync-bodied (file scans, subprocesses): run
                # directly on the server loop they BLOCK it, which (a) freezes the MCP
                # protocol pump and (b) makes this asyncio.wait_for decorative — the
                # timeout can never fire while the loop is blocked. Observed live as
                # 494s wire-hangs of a 7s tool.
                def _run_in_private_loop():
                    return asyncio.run(handler(**kwargs))

                result = await asyncio.wait_for(
                    asyncio.to_thread(_run_in_private_loop), timeout=timeout_seconds
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(handler, **kwargs), timeout=timeout_seconds
                )
        finally:
            if container_lock is not None:
                container_lock.release()
        ctx.audit.log(tool_name, session_id, success=True, duration_ms=int((time.monotonic() - started) * 1000),
                      spec_name=kwargs.get("spec_name"))
        # Learn-loop SUCCEED/FAIL on structured results: some failures come back as
        # dicts (failed test runs, isError) rather than exceptions — they must count
        # as failed attempts or the doom-loop guard never sees them. Gate blocks are
        # NEUTRAL: not failures (the same call is the correct retry after the
        # prerequisite is satisfied) and not successes (they must not clear streaks).
        failure_message = attempts.result_failure_signal(result)
        if failure_message is not None:
            attempts.record_failure(
                kwargs.get("project_root"), kwargs.get("spec_name"), tool_name,
                attempt["fingerprint"], failure_message,
            )
        elif attempts.result_gate_blocked(result):
            # Gate blocks stay ledger-neutral, but an IDENTICAL gate-blocked call
            # repeated past the leash gets escalating guidance (repeater persona).
            streak = attempts.note_refusal(attempt["fingerprint"])
            if streak >= attempts.MAX_IDENTICAL_REFUSALS and isinstance(result, dict):
                result["identical_refusals"] = streak
                result["reason"] = (
                    f"STOP: this identical call has been refused {streak} times in a row — "
                    "satisfy the prerequisite it names (see next_action) before retrying. "
                    + str(result.get("reason", ""))
                )
        else:
            attempts.clear_refusals(attempt["fingerprint"])
            recovery = attempts.record_success(
                kwargs.get("project_root"), kwargs.get("spec_name"), tool_name, attempt["fingerprint"]
            )
            if recovery["recovered"] and kwargs.get("project_root") and kwargs.get("spec_name"):
                try:
                    classes = sorted({e["error_class"] for e in recovery["recovered_from"] if e.get("error_class")})
                    memory.append_checkpoint(
                        Path(kwargs["project_root"]).resolve(), kwargs["spec_name"],
                        kind="correction",
                        summary=f"{tool_name} succeeded after {len(recovery['recovered_from'])} failed attempt(s) ({', '.join(classes)})",
                        details={"recovered_from": recovery["recovered_from"], "tool": tool_name},
                    )
                except Exception:
                    pass
        # Auto-record this tool's lifecycle phase into the item timeline (single store: checkpoints).
        timeline.record_tool_phase(kwargs.get("project_root"), kwargs.get("spec_name"), tool_name, result)
        base_dir = Path(kwargs.get("project_root") or str(_get_ctx().config.project_root)).resolve()
        result = _cap_large_outputs(result, kwargs.get("project_root"), kwargs.get("spec_name"), tool_name)
        # Context-loss armor: oversized results are persisted verbatim to the item's
        # artifacts/ (+ timeline checkpoint), so a compacted agent re-READS instead of
        # re-RUNNING expensive tools or fabricating from half-memory.
        from bc_agentic_mcp import context_recovery
        result = context_recovery.persist_result(
            result, kwargs.get("project_root"), kwargs.get("spec_name"), tool_name)
        result = _apply_envelope(result, kwargs)
        # Timer on every tool: the response itself reports what it cost, so slow steps
        # are visible at the call site instead of being discovered by wall-clock pain.
        if isinstance(result, dict):
            result.setdefault("duration_s", round(time.monotonic() - started, 2))
        normalized_result = enforce_response_paths(result, base_dir)
        return _attach_reanchor(normalized_result, kwargs)
    except MCPError as e:
        ctx.audit.log(tool_name, session_id, success=False, duration_ms=int((time.monotonic() - started) * 1000),
                      spec_name=kwargs.get("spec_name"))
        attempts.record_failure(
            kwargs.get("project_root"), kwargs.get("spec_name"), tool_name, attempt["fingerprint"], e.message
        )
        return error_response(e.code, e.message, hint=e.hint, details=e.details)
    except asyncio.TimeoutError:
        ctx.audit.log(tool_name, session_id, success=False, duration_ms=int((time.monotonic() - started) * 1000),
                      spec_name=kwargs.get("spec_name"))
        attempts.record_failure(
            kwargs.get("project_root"), kwargs.get("spec_name"), tool_name, attempt["fingerprint"],
            f"timeout after {timeout_seconds}s",
        )
        return error_response(
            ErrorCode.SERVER_ERROR,
            f"Tool {tool_name} exceeded the configured timeout of {timeout_seconds}s",
            hint="Retry with a narrower scope, fewer files, or a smaller depth setting.",
            retry_after=timeout_seconds,
            details={"timeout_seconds": timeout_seconds},
        )
    except Exception as e:
        ctx.audit.log(tool_name, session_id, success=False, duration_ms=int((time.monotonic() - started) * 1000),
                      spec_name=kwargs.get("spec_name"))
        failure = attempts.record_failure(
            kwargs.get("project_root"), kwargs.get("spec_name"), tool_name, attempt["fingerprint"], str(e)
        )
        # Learn-loop RESEARCH-on-failure: attach applicable lessons so the retry is
        # informed by prior cross-item experience instead of blind.
        lesson_hints: List[Dict[str, Any]] = []
        try:
            from bc_agentic_mcp import lessons as _lessons
            root = Path(kwargs.get("project_root") or str(_get_ctx().config.project_root)).resolve()
            hits = _lessons.applicable_lessons(
                root, api="", keywords_text=f"{tool_name} {failure['error_class']} {str(e)[:200]}"
            )
            lesson_hints = [{"message": h.get("message", "")[:240]} for h in hits[:3]]
        except Exception:
            lesson_hints = []
        return error_response(
            ErrorCode.SERVER_ERROR,
            f"Internal error in {tool_name}: {e}",
            hint="Retry the operation. If the error persists, report it.",
            retry_after=10,
            details={
                "error_class": failure["error_class"],
                "applicable_lessons": lesson_hints,
                "fingerprint": attempt["fingerprint"],
            },
        )


# ---------------------------------------------------------------------------
# Tool integrity verification (GAP 7)
# ---------------------------------------------------------------------------


def _verify_tool_integrity(mcp: FastMCP, specs_dir: Path) -> None:
    """Verify tool definitions haven't changed and save updated manifest."""
    try:
        from bc_agentic_mcp.tool_defense import verify_manifest, save_manifest

        all_tools = [t for t in mcp._tool_manager._tools.values()]
        tool_defs = [
            {"name": t.name, "description": t.description, "inputSchema": t.parameters}
            for t in all_tools
        ]
        integrity_dir = specs_dir / ".integrity"
        results = verify_manifest(integrity_dir, tool_defs)
        changed = {k: v for k, v in results.items() if v == "changed"}
        if changed:
            print(
                f"WARNING: {len(changed)} tool definitions changed since last approval: "
                f"{list(changed.keys())}",
                file=sys.stderr,
            )
        save_manifest(integrity_dir, tool_defs)
    except Exception as e:
        print(
            f"WARNING: Tool integrity verification failed (non-blocking): {e}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Server creation
# ---------------------------------------------------------------------------


def create_server(project_root: Optional[str] = None) -> FastMCP:
    """Create and configure the MCP server with all registered tools + infrastructure."""
    global _ctx
    root = Path(project_root or os.getcwd()).resolve()

    # Bootstrap config
    config = ServerConfig(
        project_root=root,
        al_tool=discover_al_tool(),
        app_json_path=discover_app_json(root),
        agent_role=os.environ.get("BC_MCP_AGENT_ROLE", "orchestrator").strip().lower(),
    )

    # Bootstrap infrastructure
    specs_dir = specs_root(root)
    # Least-privilege exec guard: every subprocess this process spawns is checked
    # against an explicit allowlist and audited (capabilities.py). Installed BEFORE
    # any tool can run. Disable only via BC_AGENTIC_EXEC_GUARD=off (audited).
    from bc_agentic_mcp import capabilities
    capabilities.install(audit_dir=specs_dir / ".audit")
    rate_limiter = RateLimiter(
        per_tool_rate=config.per_tool_rate,
        per_session_rate=config.per_session_rate,
    )
    audit = AuditLogger(specs_dir)
    _ctx = ToolContext(config=config, rate_limiter=rate_limiter, audit=audit, agent_role=config.agent_role)

    # P0: warm the object index at boot (background, best-effort) so the FIRST tool
    # call never pays the cold repo walk inside its own budget. Disable via env for tests.
    if not os.environ.get("BC_MCP_NO_WARMUP"):
        import threading as _threading

        def _warm_index() -> None:
            try:
                from bc_agentic_mcp import object_index
                object_index.refresh(root)
            except Exception:
                pass  # warm-up must never break the server

        _threading.Thread(target=_warm_index, name="bc-index-warmup", daemon=True).start()

        def _warm_team_lessons() -> None:
            try:
                from bc_agentic_mcp import team_lessons
                team_lessons.sync_pull()  # clone-or-pull + push stranded commits; offline-safe
            except Exception:
                pass

        _threading.Thread(target=_warm_team_lessons, name="bc-team-lessons-sync", daemon=True).start()

    mcp = FastMCP("bc-agentic-mcp")

    # -----------------------------------------------------------------------
    # Register all tools with rate-limiting + audit wrapping
    # -----------------------------------------------------------------------

    @mcp.tool(name="bc_init")
    async def bc_init(
        project_root: Optional[str] = None,
        module_name: Optional[str] = None,
        constitution: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Initialize .specs/ directory structure for BC agentic development."""
        return await _run_tool(
            "bc_init", handle_init,
            project_root=project_root or str(_get_ctx().config.project_root),
            module_name=module_name,
            constitution=constitution,
        )

    @mcp.tool(name="bc_analyze_module")
    async def bc_analyze_module(
        project_root: Optional[str] = None,
        spec_name: Optional[str] = None,
        depth: str = "basic",
    ) -> Dict[str, Any]:
        """Read an AL module's structure and extract naming/patterns/dependencies."""
        return await _run_tool(
            "bc_analyze_module", handle_analyze_module,
            module_path=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            depth=depth,
            max_files=_get_ctx().config.analysis_max_files,
            max_sibling_modules=_get_ctx().config.analysis_max_sibling_modules,
        )

    @mcp.tool(name="bc_clarify")
    async def bc_clarify(
        spec_name: str,
        context: str,
        project_root: Optional[str] = None,
        analysis: Optional[str] = None,
        specific_concern: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate structured clarification questions (Q-NNN IDs) from requirement bullets.

        Use when: requirements contain ambiguous terms (notify, validate, background, date, etc.)
        or a specific concern needs structured resolution before writing the spec.

        Returns: file_path to clarifications.md + questions list with Q-NNN IDs.
        NEXT STEP after this call: bc_answer_clarification — pass the Q-NNN IDs and your
        answers there. Do NOT open clarifications.md with a generic file editor.

        Boundary with bc_answer_clarification: bc_clarify WRITES the questions;
        bc_answer_clarification WRITES the answers. They are separate tools for a reason.
        Boundary with bc_write_spec: do not call bc_write_spec until all clarifications are
        answered (bc_status enforcement.clarifications.ok == true)."""
        return await _run_tool(
            "bc_clarify", handle_clarify,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            context=context,
            analysis=analysis,
            specific_concern=specific_concern,
        )

    @mcp.tool(name="bc_answer_clarification")
    async def bc_answer_clarification(
        spec_name: str,
        answers: Dict[str, str],
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write answers into clarifications.md through the MCP-fenced path.

        This is the ONLY sanctioned way to answer clarification questions.
        Never edit clarifications.md with read_file/replace_string_in_file or terminal tools.
        Doing so bypasses enforcement and will cause gate.py to block the commit.

        answers format: {"Q-NNN": "answer text including src/Path/To/File.al evidence"}
        Example: {"Q-901": "No API change needed. Scope: src/Tables/VeraSpaceDetailType.al only."}

        Each answer MUST contain at least one .al file path — this proves code-search grounding.
        Answers containing tbd/unknown/unsure/maybe/n/a are rejected.

        Returns: {ok, written, enforcement_status, next_action}
        When ok=true: follow next_action (usually bc_quality_check).
        When ok=false: fix the issues listed and call again.

        Boundary: call bc_clarify first (generates Q-NNN IDs). This tool requires those IDs."""
        return await _run_tool(
            "bc_answer_clarification", handle_answer_clarification,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            answers=answers,
        )

    @mcp.tool(name="bc_write_spec")
    async def bc_write_spec(
        spec_name: str,
        human_bullets: str,
        idempotency_key: str,
        project_root: Optional[str] = None,
        analysis: Optional[str] = None,
        clarifications: Optional[str] = None,
        template: str = "tdd",
    ) -> Dict[str, Any]:
        """Generate a TDD and machine-consumable spec from human bullets."""
        return await _run_tool(
            "bc_write_spec", handle_write_spec,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            human_bullets=human_bullets,
            analysis=analysis,
            clarifications=clarifications,
            idempotency_key=idempotency_key,
            template=template,
        )

    @mcp.tool(name="bc_plan_design")
    async def bc_plan_design(
        spec_name: str,
        project_root: Optional[str] = None,
        machine_spec_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate the technical design (DESIGN.md + ADRs) from the machine spec."""
        return await _run_tool(
            "bc_plan_design", handle_plan_design,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            machine_spec_path=machine_spec_path,
        )

    @mcp.tool(name="bc_breakdown_tasks")
    async def bc_breakdown_tasks(
        spec_name: str,
        project_root: Optional[str] = None,
        design_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Decompose the design into dependency-ordered implementation tasks."""
        return await _run_tool(
            "bc_breakdown_tasks", handle_breakdown_tasks,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            design_path=design_path,
        )

    @mcp.tool(name="bc_prepare_review")
    async def bc_prepare_review(
        spec_name: str,
        human_bullets: str,
        idempotency_key: str,
        project_root: Optional[str] = None,
        analysis: Optional[str] = None,
        clarifications: Optional[str] = None,
        template: str = "tdd",
    ) -> Dict[str, Any]:
        """One-prompt planner: returns clarifying questions or a bulletproof, gated review packet."""
        return await _run_tool(
            "bc_prepare_review", handle_prepare_review,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            human_bullets=human_bullets,
            idempotency_key=idempotency_key,
            analysis=analysis,
            clarifications=clarifications,
            template=template,
        )

    @mcp.tool(name="bc_request_approval")
    async def bc_request_approval(
        spec_name: str,
        phase: str,
        artifact_path: str,
        summary: str,
        idempotency_key: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit a phase artifact for human review."""
        return await _run_tool(
            "bc_request_approval", handle_request_approval,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            phase=phase,
            artifact_path=artifact_path,
            summary=summary,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(name="bc_submit_decision")
    async def bc_submit_decision(
        spec_name: str,
        phase: str,
        decision: str,
        project_root: Optional[str] = None,
        feedback: str = "",
        override_reason: str = "",
        confirm_human: bool = False,
    ) -> Dict[str, Any]:
        """Record the human's decision on a pending approval. override_reason requires confirm_human=true — ONLY set it when a human literally authorized the override; an agent must never set it on its own."""
        return await _run_tool(
            "bc_submit_decision", handle_submit_decision,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            phase=phase,
            decision=decision,
            feedback=feedback,
            override_reason=override_reason,
            confirm_human=confirm_human,
        )

    @mcp.tool(name="bc_approve_data_model")
    async def bc_approve_data_model(
        spec_name: str,
        approved: bool,
        approver: str,
        schema_changes: str,
        project_root: Optional[str] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Record the HUMAN data-model approval for schema-affecting changes (clears GL-DM001)."""
        from bc_agentic_mcp.tools.approval import handle_approve_data_model
        return await _run_tool(
            "bc_approve_data_model", handle_approve_data_model,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            approved=approved,
            approver=approver,
            schema_changes=schema_changes,
            notes=notes,
        )

    @mcp.tool(name="bc_status")
    async def bc_status(
        project_root: Optional[str] = None,
        spec_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Show enforcement status, phase, and next_actions for all specs (or one spec).

        Always call with spec_name when working on a single item — the response then includes
        enforcement.next_actions: a prioritized list of {engine, tool, params_hint} entries.
        Follow the FIRST next_action entry — call that tool with those params.
        Do NOT reason about what to do next or use generic tools to resolve blockers;
        the next_actions array is the authoritative prescription.

        Example: if next_actions[0] == {tool: 'bc_answer_clarification', ...}
        → call bc_answer_clarification immediately, not read_file on clarifications.md."""
        return await _run_tool(
            "bc_status", handle_status,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_recall")
    async def bc_recall(
        spec_name: str,
        project_root: Optional[str] = None,
        checkpoint_limit: int = 8,
    ) -> Dict[str, Any]:
        """Re-anchor on a spec's durable Charter (purpose/operations/criteria) + recent checkpoints."""
        return await _run_tool(
            "bc_recall", handle_recall,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            checkpoint_limit=checkpoint_limit,
        )

    @mcp.tool(name="bc_checkpoint")
    async def bc_checkpoint(
        spec_name: str,
        summary: str,
        kind: str = "decision",
        project_root: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append a durable checkpoint (decision / milestone) to a spec's memory log."""
        return await _run_tool(
            "bc_checkpoint", handle_checkpoint,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            summary=summary,
            kind=kind,
            details=details,
        )

    @mcp.tool(name="bc_timeline")
    async def bc_timeline(
        spec_name: str,
        project_root: Optional[str] = None,
        write_file: bool = True,
    ) -> Dict[str, Any]:
        """Return the item's lifecycle timeline (item received -> context -> spec -> ... -> verify) and (re)write TIMELINE.md."""
        return await _run_tool(
            "bc_timeline", timeline.handle_timeline,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            write_file=write_file,
        )

    @mcp.tool(name="bc_record_test")
    async def bc_record_test(
        spec_name: str,
        name: str,
        result: str,
        covers: Any,
        layer: str = "",
        evidence: str = "",
        evidence_receipt: str = "",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record evidence only when paired with a receipt from an execution tool.

        ``bc_run_tests`` and ``bc_api_contract`` issue receipts after their own
        execution. Caller-supplied evidence text without one is rejected.
        """
        return await _run_tool(
            "bc_record_test", handle_record_test,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            name=name,
            result=result,
            covers=covers,
            layer=layer,
            evidence=evidence,
            evidence_receipt=evidence_receipt,
        )

    @mcp.tool(name="bc_verify")
    async def bc_verify(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the verification/coverage report proving every acceptance criterion has a passing test."""
        return await _run_tool(
            "bc_verify", handle_verify,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_implement")
    async def bc_implement(
        spec_name: str,
        project_root: Optional[str] = None,
        task_ids: Optional[List[str]] = None,
        mode: str = "auto",
        dry_run: bool = False,
        # Phase 2: code execution
        code: Optional[str] = None,
        file_path: Optional[str] = None,
        attempt: int = 1,
        previous_diagnostics: Optional[str] = None,
    ) -> Dict[str, Any]:
        """DEPRECATED dual-behavior alias — use bc_implement_context (prep) or
        bc_implement_write (write+compile). Kept one release for compatibility."""
        return await _run_tool(
            "bc_implement", handle_implement_alias,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            task_ids=task_ids,
            mode=mode,
            dry_run=dry_run,
            code=code,
            file_path=file_path,
            attempt=attempt,
            previous_diagnostics=previous_diagnostics,
        )

    @mcp.tool(name="bc_implement_context")
    async def bc_implement_context(
        spec_name: str,
        project_root: Optional[str] = None,
        task_ids: Optional[List[str]] = None,
        mode: str = "auto",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Prepare implementation task context for the model (never writes code)."""
        return await _run_tool(
            "bc_implement_context", handle_implement_context,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            task_ids=task_ids,
            mode=mode,
            dry_run=dry_run,
        )

    @mcp.tool(name="bc_implement_write")
    async def bc_implement_write(
        spec_name: str,
        code: str,
        file_path: str,
        project_root: Optional[str] = None,
        task_ids: Optional[List[str]] = None,
        attempt: int = 1,
        previous_diagnostics: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write ONE AL file (scope-enforced, approval-gated) and compile it."""
        return await _run_tool(
            "bc_implement_write", handle_implement_write,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            code=code,
            file_path=file_path,
            task_ids=task_ids,
            attempt=attempt,
            previous_diagnostics=previous_diagnostics,
        )

    @mcp.tool(name="bc_implement_delete")
    async def bc_implement_delete(
        spec_name: str,
        file_path: str,
        project_root: Optional[str] = None,
        task_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Delete ONE spec-scoped AL file (decommission twin of bc_implement_write):
        approval-gated, scope-enforced, allowed ONLY for files whose removal the
        chartered spec explicitly orders ('Remove …' in objects_to_modify). Backs the
        file up to .specs/<spec>/deleted/ and recompiles the project so dangling
        references fail closed."""
        return await _run_tool(
            "bc_implement_delete", handle_implement_delete,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            file_path=file_path,
            task_ids=task_ids,
        )

    @mcp.tool(name="bc_generate_tests")
    async def bc_generate_tests(
        spec_name: str,
        project_root: Optional[str] = None,
        test_app_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate an AL test codeunit scaffold. Resolves the REAL test app (idRanges,
        free object id); pass test_app_hint when several test apps exist."""
        return await _run_tool(
            "bc_generate_tests", handle_generate_tests,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            test_app_hint=test_app_hint,
        )

    @mcp.tool(name="bc_upgrade_codeunit")
    async def bc_upgrade_codeunit(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate an AL upgrade codeunit scaffold for the spec."""
        return await _run_tool(
            "bc_upgrade_codeunit", handle_upgrade_codeunit,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_converge")
    async def bc_converge(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare the implementation on disk against the declared spec."""
        return await _run_tool(
            "bc_converge", handle_converge,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_quality_check")
    async def bc_quality_check(
        project_root: Optional[str] = None,
        spec_name: str = "",
        run_compiler: bool = False,
    ) -> Dict[str, Any]:
        """Run AL analyzers (CodeCop/AppSourceCop/UICop). Set run_compiler=true for the real alc compiler."""
        return await _run_tool(
            "bc_quality_check", handle_quality_check,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            run_compiler=run_compiler,
        )

    @mcp.tool(name="bc_feedback")
    async def bc_feedback(
        spec_name: str,
        feedback: str,
        project_root: Optional[str] = None,
        rating: int = 0,
        lesson_message: Optional[str] = None,
        lesson_match: Optional[Dict[str, str]] = None,
        lesson_severity: str = "warning",
    ) -> Dict[str, Any]:
        """Record human feedback for a spec and optionally teach a durable lesson."""
        return await _run_tool(
            "bc_feedback", handle_feedback,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            feedback=feedback,
            rating=rating,
            lesson_message=lesson_message,
            lesson_match=lesson_match,
            lesson_severity=lesson_severity,
        )

    @mcp.tool(name="bc_archive")
    async def bc_archive(
        spec_name: str,
        project_root: Optional[str] = None,
        outcome: str = "merged",
        force: bool = False,
    ) -> Dict[str, Any]:
        """Close out a spec with an outcome.

        Blocked (archived=false) when TASKS.md has unchecked tasks or no test evidence exists.
        Follow next_action (bc_generate_tests) to resolve before archiving.

        force=true bypasses the test gate — only use with explicit human confirmation that
        tests were run outside the MCP. Do NOT set force=true autonomously.
        """
        return await _run_tool(
            "bc_archive", handle_archive,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            outcome=outcome,
            force=force,
        )

    @mcp.tool(name="bc_lessons")
    async def bc_lessons(
        project_root: Optional[str] = None,
        write_file: bool = False,
    ) -> Dict[str, Any]:
        """Summarize the auto-improver lessons learned across past specs."""
        return await _run_tool(
            "bc_lessons", handle_lessons,
            project_root=project_root or str(_get_ctx().config.project_root),
            write_file=write_file,
        )

    # -------------------------------------------------------------------
    # Execution + evidence + schema-safety tools (deterministic; I/O behind seams)
    # -------------------------------------------------------------------
    @mcp.tool(name="bc_env_preflight")
    async def bc_env_preflight(
        container_name: str,
        tenant: str = "default",
        user: str = "admin",
        credential_env: str = "BC_TEST_PASSWORD",
        app_json_path: Optional[str] = None,
        dev_port: int = 7049,
        server_instance: str = "BC",
        ttl_seconds: int = 1800,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ONE deterministic pass over container-environment truth (health, license,
        shared folder, dependency symbols). Caches a manifest that gates bc_run_tests —
        run this FIRST, before any container work."""
        return await _run_tool(
            "bc_env_preflight", handle_env_preflight,
            project_root=project_root or str(_get_ctx().config.project_root),
            container_name=container_name,
            tenant=tenant,
            user=user,
            credential_env=credential_env,
            app_json_path=app_json_path,
            dev_port=dev_port,
            server_instance=server_instance,
            ttl_seconds=ttl_seconds,
        )

    @mcp.tool(name="bc_run_tests")
    async def bc_run_tests(
        container_name: str,
        test_extension_id: str,
        credential_env: str = "BC_TEST_PASSWORD",
        user: str = "admin",
        tenant: str = "default",
        spec_name: Optional[str] = None,
        covers: Any = None,
        validation_mode: str = "item",
        app_project_folder: Optional[str] = None,
        test_codeunit: Optional[str] = None,
        publish_only: bool = False,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a published test extension in a BC container and capture the result as evidence.
        Feature model: supply app_project_folder ONCE per iteration (full sync->compile->publish->run),
        then re-run per SLICE with test_codeunit=<id|name> (no republish).
        publish_only=true publishes a dependency app (e.g. BaseApp) without running tests.
        Requires a fresh bc_env_preflight pass for the container."""
        return await _run_tool(
            "bc_run_tests", handle_run_tests,
            project_root=project_root or str(_get_ctx().config.project_root),
            container_name=container_name,
            test_extension_id=test_extension_id,
            credential_env=credential_env,
            user=user,
            tenant=tenant,
            spec_name=spec_name,
            covers=covers,
            validation_mode=validation_mode,
            app_project_folder=app_project_folder,
            test_codeunit=test_codeunit,
            publish_only=publish_only,
        )

    # -------------------------------------------------------------------
    # B1 PR family + B3 item-state sync: the lifecycle ends at a merged PR,
    # not at bc_archive. All network I/O via pr.py's requester seam (PAT from env).
    # -------------------------------------------------------------------
    @mcp.tool(name="bc_prepare_pr")
    async def bc_prepare_pr(
        spec_name: str,
        source_branch: Optional[str] = None,
        target_branch: str = "main",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the PR title/description from recorded evidence (local, no network).
        Fail-closed: refuses while the verification gate is failing."""
        return await _run_tool(
            "bc_prepare_pr", handle_prepare_pr,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            source_branch=source_branch,
            target_branch=target_branch,
        )

    @mcp.tool(name="bc_create_pr")
    async def bc_create_pr(
        spec_name: str,
        org_url: str,
        project: str,
        repository: str,
        source_branch: Optional[str] = None,
        target_branch: str = "main",
        title: Optional[str] = None,
        work_item_id: Optional[int] = None,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Create the ADO pull request from the prepared artifact (bc_prepare_pr first).
        DRY-RUN by default: without confirm=true nothing is sent — the response shows the
        exact outbound payload + lint warnings for a self-review pass."""
        return await _run_tool(
            "bc_create_pr", handle_create_pr,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            org_url=org_url,
            project=project,
            repository=repository,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            work_item_id=work_item_id,
            pat_env=pat_env,
            confirm=confirm,
        )

    @mcp.tool(name="bc_get_review_comments")
    async def bc_get_review_comments(
        spec_name: str,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch PR comment threads. Open threads flip the item into the rework loop
        (implement-stage tools re-admitted within Charter scope)."""
        return await _run_tool(
            "bc_get_review_comments", handle_get_review_comments,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            pat_env=pat_env,
        )

    @mcp.tool(name="bc_resolve_review_comment")
    async def bc_resolve_review_comment(
        spec_name: str,
        thread_id: int,
        reply: Optional[str] = None,
        resolution: str = "fixed",
        judgment: Optional[str] = None,
        analysis: Optional[str] = None,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Reply to and resolve one PR comment thread (after the fix is pushed).
        TRIAGE WALL: requires judgment ('correct'|'partially-correct'|'incorrect') +
        analysis grounded in code reality — a remark is a claim to verify, not a
        command to obey. 'incorrect' never auto-closes the thread. DRY-RUN by default."""
        return await _run_tool(
            "bc_resolve_review_comment", handle_resolve_review_comment,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            thread_id=thread_id,
            reply=reply,
            resolution=resolution,
            judgment=judgment,
            analysis=analysis,
            pat_env=pat_env,
            confirm=confirm,
        )

    @mcp.tool(name="bc_merge_status")
    async def bc_merge_status(
        spec_name: str,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PR approval/merge truth from ADO. PR approval satisfies the internal `code`
        gate; a completed PR advances the timeline to `merged` -> bc_archive."""
        return await _run_tool(
            "bc_merge_status", handle_merge_status,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            pat_env=pat_env,
        )

    @mcp.tool(name="bc_sync_item_state")
    async def bc_sync_item_state(
        org_url: str,
        project: str,
        work_item_id: int,
        state: str,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
        spec_name: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Set the ADO work item state on a lifecycle transition (state names are
        org-specific and always explicit — never invented). DRY-RUN by default."""
        return await _run_tool(
            "bc_sync_item_state", handle_sync_item_state,
            org_url=org_url,
            project=project,
            work_item_id=work_item_id,
            state=state,
            pat_env=pat_env,
            confirm=confirm,
        )

    # -------------------------------------------------------------------
    # C3 composite driver + C4 auto-clarify + E2 metrics
    # -------------------------------------------------------------------
    _ADVANCE_HANDLERS = {
        "bc_generate_tests": handle_generate_tests,
        "bc_run_tests": handle_run_tests,
        "bc_verify": handle_verify,
        "bc_prepare_pr": handle_prepare_pr,
        "bc_create_pr": handle_create_pr,
        "bc_get_review_comments": handle_get_review_comments,
        "bc_merge_status": handle_merge_status,
        "bc_archive": handle_archive,
    }

    async def _advance_run_tool(tool_name: str, **params: Any) -> Dict[str, Any]:
        """Inner dispatcher for bc_advance: every chained step goes through the SAME
        _run_tool pipeline (policy, doom-loop guard, timeline, audit, output caps)."""
        return await _run_tool(tool_name, _ADVANCE_HANDLERS[tool_name], **params)

    @mcp.tool(name="bc_advance")
    async def bc_advance(
        spec_name: str,
        project_root: Optional[str] = None,
        max_steps: int = 6,
        test_container_name: Optional[str] = None,
        test_extension_id: Optional[str] = None,
        credential_env: str = "BC_TEST_PASSWORD",
        app_project_folder: Optional[str] = None,
        org_url: Optional[str] = None,
        project: Optional[str] = None,
        repository: Optional[str] = None,
        work_item_id: Optional[int] = None,
        target_branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Chain every DETERMINISTIC lifecycle step server-side until a human gate,
        a judgment step, a blocked gate, or completion. Idempotent — safe to re-run."""
        return await _run_tool(
            "bc_advance", handle_advance,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            run_tool=_advance_run_tool,
            max_steps=max_steps,
            test_container_name=test_container_name,
            test_extension_id=test_extension_id,
            credential_env=credential_env,
            app_project_folder=app_project_folder,
            org_url=org_url,
            project=project,
            repository=repository,
            work_item_id=work_item_id,
            target_branch=target_branch,
        )

    @mcp.tool(name="bc_auto_clarify")
    async def bc_auto_clarify(
        spec_name: str,
        auto_submit: bool = False,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Propose evidence-grounded answers to open clarifications from the captured
        context bundle; only genuinely unanswerable questions reach the human."""
        return await _run_tool(
            "bc_auto_clarify", handle_auto_clarify,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            auto_submit=auto_submit,
        )

    @mcp.tool(name="bc_metrics")
    async def bc_metrics(
        spec_name: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cycle-time + reliability metrics from the audit log (per tool, per item)."""
        return await _run_tool(
            "bc_metrics", handle_metrics,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    # -------------------------------------------------------------------
    # Workstream H: feature tier (plan at feature altitude, deliver per item)
    # -------------------------------------------------------------------
    @mcp.tool(name="bc_capture_feature")
    async def bc_capture_feature(
        spec_name: str,
        work_item_id: str,
        org_url: str,
        project: str,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Capture an ENTIRE feature tree fresh (feature + every child PBI). The id may
        be the feature itself or any child — the parent is resolved automatically."""
        return await _run_tool(
            "bc_capture_feature", handle_capture_feature,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            work_item_id=work_item_id,
            org_url=org_url,
            project=project,
            pat_env=pat_env,
        )

    @mcp.tool(name="bc_refine_feature")
    async def bc_refine_feature(
        spec_name: str,
        critique: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """H4: confront every PBI claim with CODE REALITY (parse the actual objects):
        mismatches (wrong/missing field ids), redundancies, cross-item conflicts,
        guideline flags, empiric-verification requirements. Facts deterministic;
        `critique` carries the model's first-principles judgment."""
        return await _run_tool(
            "bc_refine_feature", handle_refine_feature,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            critique=critique,
        )

    @mcp.tool(name="bc_repo_map")
    async def bc_repo_map(
        query: str = "",
        kind: Optional[str] = None,
        limit: int = 20,
        object_key: Optional[str] = None,
        free_id_range: Optional[str] = None,
        max_age_seconds: int = 60,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The repo's TABLE OF CONTENTS from the persistent index: search objects by
        name/caption (query, optional kind) or get one object's full signature
        (object_key='table 11024121'). free_id_range='lo-hi' (+kind) lists CONTENT-
        VERIFIED free object ids (filenames lie — observed live: misspelled
        .Codeuit.al hid a taken id). TTL fast-path by default; max_age_seconds=0
        forces filesystem reconciliation."""
        return await _run_tool(
            "bc_repo_map", handle_repo_map,
            project_root=project_root or str(_get_ctx().config.project_root),
            query=query,
            kind=kind,
            limit=limit,
            free_id_range=free_id_range,
            object_key=object_key,
            max_age_seconds=max_age_seconds,
        )

    @mcp.tool(name="bc_refine_item")
    async def bc_refine_item(
        spec_name: str,
        critique: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PBI-level refinement: THIS item's claims confronted with code reality
        (cited field ids verified, proposed ids checked free, redundancies flagged).
        Uses the persistent object index; findings land in ITEM-REFINEMENT.md."""
        return await _run_tool(
            "bc_refine_item", handle_refine_item,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            critique=critique,
        )

    @mcp.tool(name="bc_root_cause")
    async def bc_root_cause(
        spec_name: str,
        symptom: str,
        root_cause: str,
        evidence: List[str],
        fix_approach: str,
        regression_risk: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bugfix lane diagnosis (MANDATORY before the fix spec): record symptom +
        root cause + fix approach with EVERY evidence reference verified against the
        repo (AL file paths / object index) — fail-closed on unverifiable evidence.
        Persists ROOT-CAUSE.md + root_cause.json; the `root_cause` enforcement engine
        blocks bug planning without it."""
        return await _run_tool(
            "bc_root_cause", handle_root_cause,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            symptom=symptom,
            root_cause=root_cause,
            evidence=evidence,
            fix_approach=fix_approach,
            regression_risk=regression_risk,
        )

    @mcp.tool(name="bc_plan_feature")
    async def bc_plan_feature(
        spec_name: str,
        notes: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Deterministic cross-item analysis (object refs, mention graph, collisions,
        foundation-first order) + the model's wave narrative -> FEATURE-PLAN.md.
        Approval of that plan is human gate F1 (the C1 `plan` gate on this folder)."""
        return await _run_tool(
            "bc_plan_feature", handle_plan_feature,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            notes=notes,
        )

    @mcp.tool(name="bc_prepare_feature_review")
    async def bc_prepare_feature_review(
        spec_name: str,
        decisions: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ONE mega review packet for the WHOLE feature (FEATURE-REVIEW.md): every
        child item's spec/design/tasks + full test matrix + feature schema + a
        'Decisions, in plain language' section (caller narrative via `decisions`
        + machine-recorded checkpoint/refinement log). The human approves ONCE —
        the feature-level plan approval then cascades to every item. Blocks while
        any live child is unauthored or misses a test bucket."""
        return await _run_tool(
            "bc_prepare_feature_review", handle_prepare_feature_review,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            decisions=decisions or "",
        )

    @mcp.tool(name="bc_feature_status")
    async def bc_feature_status(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Roll-up of every child's per-item lifecycle phase + the prescribed next item."""
        return await _run_tool(
            "bc_feature_status", handle_feature_status,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_api_contract")
    async def bc_api_contract(
        base_url: str,
        entity: str,
        fields: Optional[List[Dict[str, Any]]] = None,
        operations: Any = None,
        user: str = "",
        password_env: str = "BC_API_PASSWORD",
        spec_name: Optional[str] = None,
        covers: Any = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run OData/API contract checks (GET + negatives + boundaries) against a live endpoint."""
        return await _run_tool(
            "bc_api_contract", handle_api_contract,
            project_root=project_root or str(_get_ctx().config.project_root),
            base_url=base_url,
            entity=entity,
            fields=fields,
            operations=operations,
            user=user,
            password_env=password_env,
            spec_name=spec_name,
            covers=covers,
        )

    @mcp.tool(name="bc_reconcile_target")
    async def bc_reconcile_target(
        requested: List[str],
        deployed: Optional[List[str]] = None,
        metadata_url: Optional[str] = None,
        entity: str = "",
        spec_name: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reconcile requested fields against what is already deployed (avoid recreating fields)."""
        return await _run_tool(
            "bc_reconcile_target", handle_reconcile_target,
            project_root=project_root or str(_get_ctx().config.project_root),
            requested=requested,
            deployed=deployed,
            metadata_url=metadata_url,
            entity=entity,
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_upgrade_preflight")
    async def bc_upgrade_preflight(
        current_fields: List[str],
        baseline_fields: List[str],
        current_tables: Optional[List[str]] = None,
        baseline_tables: Optional[List[str]] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Detect whether an upgrade would REMOVE fields/tables the deployed baseline has."""
        return await _run_tool(
            "bc_upgrade_preflight", handle_upgrade_preflight,
            project_root=project_root or str(_get_ctx().config.project_root),
            current_fields=current_fields,
            baseline_fields=baseline_fields,
            current_tables=current_tables,
            baseline_tables=baseline_tables,
        )

    @mcp.tool(name="bc_find_consumers")
    async def bc_find_consumers(
        symbol: str,
        source_root: Optional[str] = None,
        exclude_definition: bool = True,
        max_files: Optional[int] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Find who CONSUMES an AL symbol (fields/tables) to target business-logic tests."""
        return await _run_tool(
            "bc_find_consumers", handle_find_consumers,
            project_root=project_root or str(_get_ctx().config.project_root),
            symbol=symbol,
            source_root=source_root,
            exclude_definition=exclude_definition,
            max_files=max_files,
        )

    @mcp.tool(name="bc_promote_lesson")
    async def bc_promote_lesson(
        lesson_id: Optional[str] = None,
        message: Optional[str] = None,
        match: Optional[Dict[str, str]] = None,
        severity: str = "warning",
        to_article: bool = False,
        domain: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Promote a lesson to the cross-project store so it applies to every repo.
        to_article=True also graduates it into a repo-layer knowledge article whose
        Best Practice / Anti Pattern rules the review worklist surfaces."""
        return await _run_tool(
            "bc_promote_lesson", handle_promote_lesson,
            project_root=project_root or str(_get_ctx().config.project_root),
            lesson_id=lesson_id,
            message=message,
            match=match,
            severity=severity,
            to_article=to_article,
            domain=domain,
        )

    @mcp.tool(name="bc_extract_references")
    async def bc_extract_references(text: str) -> Dict[str, Any]:
        """Surface the wiki/related-item references in a work item — consult them BEFORE choosing the target."""
        return await _run_tool(
            "bc_extract_references", handle_extract_references, text=text,
        )

    @mcp.tool(name="bc_fetch_wiki")
    async def bc_fetch_wiki(
        url: Optional[str] = None,
        org_url: Optional[str] = None,
        project: Optional[str] = None,
        wiki_name: Optional[str] = None,
        page_id: Optional[str] = None,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
    ) -> Dict[str, Any]:
        """Fetch an authoritative ADO wiki page FRESH via REST+PAT (never a stale local-clone workaround)."""
        return await _run_tool(
            "bc_fetch_wiki", handle_fetch_wiki,
            url=url, org_url=org_url, project=project, wiki_name=wiki_name,
            page_id=page_id, pat_env=pat_env,
        )

    @mcp.tool(name="bc_mine_precedents")
    async def bc_mine_precedents(
        spec_name: str,
        work_item_id: Optional[str] = None,
        item_type: Optional[str] = None,
        title: Optional[str] = None,
        top_k: int = 5,
        skip: bool = False,
        reason: Optional[str] = None,
        org_url: Optional[str] = None,
        project: Optional[str] = None,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mine ADO history for how items LIKE this one were delivered (similar items -> PRs -> changed paths -> delivery shape). REQUIRED before bc_plan_design; skip=true+reason records an audited waiver."""
        from bc_agentic_mcp.tools.mine_precedents import handle_mine_precedents
        return await _run_tool(
            "bc_mine_precedents", handle_mine_precedents,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name, work_item_id=work_item_id, item_type=item_type,
            title=title, top_k=top_k, skip=skip, reason=reason,
            org_url=org_url, project=project, pat_env=pat_env,
        )

    @mcp.tool(name="bc_capture_item_context")
    async def bc_capture_item_context(
        spec_name: str,
        work_item_id: Union[str, int],
        description: Optional[str] = None,
        org_url: Optional[str] = None,
        project: Optional[str] = None,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """FIRST action on an item: save its description + linked wikis + related items FRESH to disk."""
        return await _run_tool(
            "bc_capture_item_context", handle_capture_item_context,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            # AI-friendly coercion (GPT-5-mini run, 2026-07-06): a numeric id IS a
            # number to a weak model — rejecting int with a pydantic error is hostile.
            work_item_id=str(work_item_id),
            description=description,
            org_url=org_url,
            project=project,
            pat_env=pat_env,
        )

    @mcp.tool(name="bc_check_permission_coverage")
    async def bc_check_permission_coverage(
        table: str,
        required_access: str = "R",
        source_root: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Check whether a permission set already grants table-level access (so new API fields need no permission change)."""
        return await _run_tool(
            "bc_check_permission_coverage", handle_check_permission_coverage,
            project_root=project_root or str(_get_ctx().config.project_root),
            table=table,
            required_access=required_access,
            source_root=source_root,
        )

    @mcp.tool(name="bc_reflect")
    async def bc_reflect(
        spec_name: str,
        note: str = "",
        lessons: Optional[List[Dict[str, Any]]] = None,
        promote: bool = False,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record lessons learned (project + optionally global) and clear the automatic reflection nudge."""
        return await _run_tool(
            "bc_reflect", handle_reflect,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            note=note,
            lessons=lessons,
            promote=promote,
        )

    @mcp.tool(name="bc_guard_pr_thread_resolution")
    async def bc_guard_pr_thread_resolution(
        touched_files: List[str],
        require_branch_hygiene: bool = True,
        require_tracking_upstream: bool = True,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Gate PR thread closure until local unpushed commits cover touched file(s)."""
        return await _run_tool(
            "bc_guard_pr_thread_resolution", handle_guard_pr_thread_resolution,
            project_root=project_root or str(_get_ctx().config.project_root),
            touched_files=touched_files,
            require_branch_hygiene=require_branch_hygiene,
            require_tracking_upstream=require_tracking_upstream,
        )

    @mcp.tool(name="bc_detect")
    async def bc_detect(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Layer 1: run deterministic mistake detectors; auto-record findings + trigger reflection."""
        return await _run_tool(
            "bc_detect", handle_detect,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_review")
    async def bc_review(
        spec_name: str,
        findings: Optional[List[Dict[str, Any]]] = None,
        changed_files: Optional[List[str]] = None,
        rubric: Optional[Dict[str, Any]] = None,
        verdict: str = "",
        knowledge_applied: Optional[List[str]] = None,
        knowledge_receipts: Optional[List[str]] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Layer 3: separate-reviewer packet (no findings) or record reviewer findings (triggers reflection).

        WITHOUT findings: returns the review packet (charter + checklist + BCQuality knowledge worklist).
        WITH findings: records findings as checkpoints AND returns them inline alongside a
        ``human_gate_required: true`` flag.  The agent MUST present the findings to the human
        immediately and ask for an explicit approve/reject/change-request decision before advancing
        the lifecycle. Never declare the review complete without surfacing findings to the human.

        Pass rubric={grounding, coverage, conventions, risk} (each 0.0-1.0, judge-style) to make
        review quality MEASURABLE across items and prompt versions.
        Pass knowledge_applied=[list of BCQuality article paths read via bc_get_knowledge_article]
        alongside findings. The machine verifies signed reads for the current packet_id;
        caller-supplied paths alone are not evidence."""
        return await _run_tool(
            "bc_review", handle_review,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            findings=findings,
            changed_files=changed_files,
            rubric=rubric,
            verdict=verdict,
            knowledge_applied=knowledge_applied,
            knowledge_receipts=knowledge_receipts,
        )

    @mcp.tool(name="bc_read_code_context")
    async def bc_read_code_context(
        spec_name: str,
        objects: Optional[List[Dict[str, Any]]] = None,
        work_types: Optional[List[str]] = None,
        require_clean_latest: bool = True,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Code read-context: find sibling/same-kind precedents + conventions on LATEST+CLEAN source."""
        return await _run_tool(
            "bc_read_code_context", handle_read_code_context,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            objects=objects,
            work_types=work_types,
            require_clean_latest=require_clean_latest,
        )

    @mcp.tool(name="bc_get_knowledge_article")
    async def bc_get_knowledge_article(
        path: str,
        spec_name: Optional[str] = None,
        packet_id: str = "",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read a BCQuality knowledge article in full (## Best Practice, ## Anti Pattern)
        plus companion golden-template files (.good.al / .bad.al).

        ``path`` is the relative path from the knowledge worklist. Pass the current
        ``spec_name`` and review ``packet_id`` so the successful read is signed and
        bound to that packet.

        REQUIRED: call this for each article in the 'knowledge' worklist BEFORE
        submitting findings to bc_review. The worklist entry is a discovery hint only;
        the normative rule bodies and AL examples are in the full content returned here.
        """
        return await _run_tool(
            "bc_get_knowledge_article", handle_get_knowledge_article,
            project_root=project_root or str(_get_ctx().config.project_root),
            path=path,
            spec_name=spec_name,
            packet_id=packet_id,
        )

    # -------------------------------------------------------------------
    # GAP 8: Health check (for Docker/K8s liveness probes)
    # -------------------------------------------------------------------
    @mcp.tool(name="_health")
    async def _health() -> Dict[str, Any]:
        return {"status": "ok", "version": __version__, "security_mode": security.security_mode()}

    @mcp.tool(name="bc_health")
    async def bc_health() -> Dict[str, Any]:
        """Canonical name for the health probe (F3); `_health` kept one release."""
        return {"status": "ok", "version": __version__, "security_mode": security.security_mode()}

    @mcp.tool(name="bc_worktree")
    async def bc_worktree(
        action: str,
        spec_name: Optional[str] = None,
        branch: Optional[str] = None,
        base_ref: Optional[str] = None,
        worktrees_base: Optional[str] = None,
        force: bool = False,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Manage per-item git worktrees (create|status|list|remove) so missions can
        build/test in parallel isolated checkouts while governance state stays shared."""
        return await _run_tool(
            "bc_worktree", handle_worktree,
            project_root=project_root or str(_get_ctx().config.project_root),
            action=action,
            spec_name=spec_name,
            branch=branch,
            base_ref=base_ref,
            worktrees_base=worktrees_base,
            force=force,
        )

    @mcp.tool(name="bc_analyze_consistency")
    async def bc_analyze_consistency(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cross-artifact story check: every requirement addressed by DESIGN and carried by a
        TASK, scope files vs plan agree (drift = critical), charter criteria represented —
        plus a per-requirement quality checklist. Run before requesting the plan gate."""
        return await _run_tool(
            "bc_analyze_consistency", handle_analyze_consistency,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_tool_health")
    async def bc_tool_health(
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Per-tool reliability from the audit log + ranked docstring-improvement candidates
        (the deterministic half of the tool self-improvement loop; prompt CI gates changes)."""
        return await _run_tool(
            "bc_tool_health", handle_tool_health,
            project_root=project_root or str(_get_ctx().config.project_root),
        )

    @mcp.tool(name="bc_push_items")
    async def bc_push_items(
        spec_name: str,
        org_url: str,
        project: str,
        items: List[Dict[str, Any]],
        parent_work_item_id: Optional[str] = None,
        item_type: str = "Product Backlog Item",
        confirm: bool = False,
        pat_env: str = "AZURE_DEVOPS_EXT_PAT",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """CREATE the proposed child work items in Azure DevOps (deliberate shared-system
        write: requires confirm=true after explicit human approval; idempotent by title)."""
        return await _run_tool(
            "bc_push_items", handle_push_items,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            org_url=org_url,
            project=project,
            items=items,
            parent_work_item_id=parent_work_item_id,
            item_type=item_type,
            confirm=confirm,
            pat_env=pat_env,
        )

    # -------------------------------------------------------------------
    # Refinement lab: free-form material -> evidence dossier -> delivery lane.
    # -------------------------------------------------------------------
    @mcp.tool(name="bc_intake_start")
    async def bc_intake_start(
        name: str,
        text: Optional[str] = None,
        source: str = "pasted",
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a refinement-lab intake for raw material (email/notes/requirements);
        every document is quarantined untrusted input."""
        return await _run_tool(
            "bc_intake_start", handle_intake_start,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=name if name.startswith("intake-") else f"intake-{name}",
            intake_name_arg=name, text=text, source=source,
        )

    @mcp.tool(name="bc_intake_add")
    async def bc_intake_add(
        name: str,
        filename: str,
        content: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add one more pasted/uploaded document to an intake."""
        return await _run_tool(
            "bc_intake_add", handle_intake_add,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=name if name.startswith("intake-") else f"intake-{name}",
            intake_name_arg=name, filename=filename, content=content,
        )

    @mcp.tool(name="bc_intake_analyze")
    async def bc_intake_analyze(
        name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the evidence dossier: BM25 precedents from past specs, object-index
        code reality, ambiguity questions and a deterministic lane hint."""
        return await _run_tool(
            "bc_intake_analyze", handle_intake_analyze,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=name if name.startswith("intake-") else f"intake-{name}",
            intake_name_arg=name,
        )

    @mcp.tool(name="bc_intake_graduate")
    async def bc_intake_graduate(
        name: str,
        lane: str,
        spec_name: str,
        work_item_id: Optional[str] = None,
        children: Optional[List[str]] = None,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Materialize a refined intake as a real lifecycle item: lane = bug | pbi |
        feature (epics stay a roll-up: split them into features first)."""
        return await _run_tool(
            "bc_intake_graduate", handle_intake_graduate,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=name if name.startswith("intake-") else f"intake-{name}",
            intake_name_arg=name, lane=lane, spec_name_target=spec_name,
            work_item_id=work_item_id, children=children,
        )

    # -------------------------------------------------------------------
    # GAP 7: Tool integrity verification (hash-pinning)
    # -------------------------------------------------------------------
    _verify_tool_integrity(mcp, specs_dir)

    return mcp


def main():
    """Console-script entry point referenced by [project.scripts]."""
    parser = argparse.ArgumentParser(description="BC Agentic MCP Server")
    parser.add_argument(
        "--project-root", default=os.getcwd(), help="AL project root directory"
    )
    parser.add_argument(
        "--specs-root",
        default=None,
        help=("External base directory for governance artifacts (.specs). When set, "
              "artifacts live outside the code repo, keyed per-repo, keeping the code "
              "tree pristine. Defaults to colocated <project-root>/.specs."),
    )
    args = parser.parse_args()

    if args.specs_root:
        os.environ[specs_env] = args.specs_root

    root = Path(args.project_root).resolve()
    if not root.is_dir():
        print(f"FATAL: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    os.chdir(str(root))
    server = create_server(str(root))
    server.run()


if __name__ == "__main__":
    main()
