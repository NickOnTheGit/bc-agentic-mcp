"""app — Mission Control web server (Starlette + uvicorn, both shipped with `mcp`).

Read endpoints are pure file projections (views.py). Action endpoints call the
real MCP server through one persistent stdio session (bridge.py), so every
policy/gate/audit rule applies exactly as it does for an agent.

Local cockpit by design: binds 127.0.0.1 unless explicitly overridden.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shlex
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from bc_agentic_mcp.mission_control import views
from bc_agentic_mcp.mission_control import routines as routines_mod
from bc_agentic_mcp.mission_control.bridge import McpBridge
from bc_agentic_mcp.workspace import ENV_VAR as SPECS_ENV_VAR

_STATIC = Path(__file__).parent / "static"


class _NoCacheMiddleware(BaseHTTPMiddleware):
    """The cockpit is a live tool — stale JS/CSS must never linger in the browser."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(project_root: str, specs_root: str | None = None) -> Starlette:
    root = Path(project_root).resolve()
    if specs_root:
        # Make the cockpit's own file projections resolve the SAME external
        # workspace base as the spawned MCP server (workspace.specs_root reads this).
        os.environ[SPECS_ENV_VAR] = specs_root
    # Exec guard for the cockpit process too — plus the agent CLIs the headless
    # dispatcher may launch.
    from bc_agentic_mcp import capabilities
    from bc_agentic_mcp.workspace import specs_root as _specs_root_fn
    capabilities.install(audit_dir=_specs_root_fn(root) / ".audit",
                         extra=("opencode", "copilot", "gh"))
    bridge = McpBridge(str(root), specs_root)

    # ---- helpers ---------------------------------------------------------
    def _spec(request: Request) -> str:
        return request.path_params["spec"]

    def _bad_spec(spec: str) -> JSONResponse | None:
        if not views.valid_spec_name(spec):
            return JSONResponse({"error": "invalid spec name"}, status_code=400)
        return None

    async def _json_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    # ---- read endpoints (file projections, safe to poll) ------------------
    async def home(_: Request) -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    async def overview(_: Request) -> JSONResponse:
        return JSONResponse(views.overview(root))

    async def pulse(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or JSONResponse(views.item_pulse(root, spec))

    async def artifact(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        rel = request.query_params.get("name", "")
        return JSONResponse(views.read_artifact(root, spec, rel))

    # ---- action endpoints (through the MCP server) ------------------------
    def _friendly_error(tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Unwrap the server's error envelope into user-facing fields."""
        meta = result.get("_meta") or {}
        details = meta.get("details") or {}
        policy = details.get("policy") or {}
        message = ""
        for chunk in result.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("text"):
                message = str(chunk["text"])
                break
        message = message or str(result.get("message") or result.get("error") or "Tool call failed")
        if message.startswith("CLIENT_ERROR:"):
            message = message[len("CLIENT_ERROR:"):].strip()
        return {
            "tool": tool,
            "error": message,
            "reason": str(policy.get("reason") or details.get("reason") or ""),
            "hint": str(meta.get("hint") or result.get("hint") or ""),
            "stage": str(policy.get("stage") or ""),
            "retryable": bool(meta.get("retryable", False)),
        }

    async def _tool(tool: str, args: Dict[str, Any]) -> JSONResponse:
        try:
            result = await bridge.call(tool, args)
        except Exception as exc:  # bridge restart also failed
            return JSONResponse(
                {"tool": tool, "error": f"MCP bridge failed: {exc}",
                 "hint": "The MCP server subprocess may have crashed — check the cockpit console."},
                status_code=502)
        if result.get("isError"):
            friendly = _friendly_error(tool, result)
            is_policy_block = bool(
                ((result.get("_meta") or {}).get("details") or {}).get("policy"))
            return JSONResponse(friendly, status_code=409 if is_policy_block else 502)
        return JSONResponse({"tool": tool, "result": result}, status_code=200)

    async def capture(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        args: Dict[str, Any] = {"spec_name": spec}
        for key in ("work_item_id", "description", "org_url", "project", "pat_env"):
            if body.get(key):
                args[key] = str(body[key])
        if "work_item_id" not in args:
            return JSONResponse({"error": "work_item_id is required"}, status_code=400)
        tool = "bc_capture_feature" if body.get("tier") == "feature" else "bc_capture_item_context"
        if tool == "bc_capture_feature":
            for required in ("org_url", "project"):
                if required not in args:
                    return JSONResponse(
                        {"error": f"{required} is required for feature capture"}, status_code=400)
            args.pop("description", None)
        return await _tool(tool, args)

    async def advance(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        args: Dict[str, Any] = {"spec_name": spec}
        passthrough = (
            "max_steps", "test_container_name", "test_extension_id", "credential_env",
            "app_project_folder", "org_url", "project", "repository",
            "work_item_id", "target_branch", "project_root",
        )
        for key in passthrough:
            if body.get(key) not in (None, ""):
                args[key] = body[key]
        return await _tool("bc_advance", args)

    async def auto_clarify(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        return await _tool("bc_auto_clarify", {
            "spec_name": spec,
            "auto_submit": bool(body.get("auto_submit", False)),
        })

    async def answer(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        answers = body.get("answers")
        if not isinstance(answers, dict) or not answers:
            return JSONResponse({"error": "answers {Q-NNN: text} required"}, status_code=400)
        return await _tool("bc_answer_clarification", {"spec_name": spec, "answers": answers})

    async def decision(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        phase, verdict = body.get("phase"), body.get("decision")
        if not phase or verdict not in ("approve", "reject", "request_changes"):
            return JSONResponse(
                {"error": "phase and decision (approve|reject|request_changes) required"},
                status_code=400)
        return await _tool("bc_submit_decision", {
            "spec_name": spec,
            "phase": str(phase),
            "decision": str(verdict),
            "feedback": str(body.get("feedback", "")),
            "override_reason": str(body.get("override_reason", "")),
        })

    async def guidance(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or await _tool("bc_status", {"spec_name": spec})

    async def feature_status(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or await _tool("bc_feature_status", {"spec_name": spec})

    async def pr_status(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or await _tool("bc_merge_status", {"spec_name": spec})

    async def pr_comments(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or await _tool("bc_get_review_comments", {"spec_name": spec})

    async def prepare_review(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        bullets = str(body.get("human_bullets") or "").strip()
        if not bullets:
            return JSONResponse(
                {"error": "human_bullets is required",
                 "hint": "Write the item's requirement bullets in the text box first."},
                status_code=400)
        import time as _time
        args: Dict[str, Any] = {
            "spec_name": spec,
            "human_bullets": bullets,
            "idempotency_key": str(body.get("idempotency_key") or f"{spec}-review-{int(_time.time())}"),
        }
        if body.get("template"):
            args["template"] = str(body["template"])
        return await _tool("bc_prepare_review", args)

    async def request_approval(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        summary = str(body.get("summary") or "").strip()
        if not summary:
            return JSONResponse(
                {"error": "summary is required",
                 "hint": "One or two sentences describing what the reviewer is approving."},
                status_code=400)
        import time as _time
        item_dir = views.specs_root(root) / spec
        return await _tool("bc_request_approval", {
            "spec_name": spec,
            "phase": str(body.get("phase") or "plan"),
            "artifact_path": str(body.get("artifact_path") or (item_dir / "TDD.md")),
            "summary": summary,
            "idempotency_key": str(body.get("idempotency_key") or f"{spec}-approval-{int(_time.time())}"),
        })

    async def run_tests(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        if not body.get("test_container_name") or not body.get("test_extension_id"):
            return JSONResponse(
                {"error": "container and test extension id are required",
                 "hint": "Fill 'BC test container name' and 'Test extension id' in ⚙ presets first."},
                status_code=400)
        args: Dict[str, Any] = {
            "spec_name": spec,
            "container_name": str(body["test_container_name"]),
            "test_extension_id": str(body["test_extension_id"]),
            "credential_env": str(body.get("credential_env") or "BC_TEST_PASSWORD"),
            "validation_mode": str(body.get("validation_mode") or "item"),
        }
        for key in ("user", "tenant", "app_project_folder", "test_codeunit", "project_root"):
            if body.get(key):
                args[key] = str(body[key])
        if body.get("covers"):
            args["covers"] = body["covers"]
        return await _tool("bc_run_tests", args)

    async def env_preflight(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if not body.get("test_container_name"):
            return JSONResponse(
                {"error": "container name is required",
                 "hint": "Fill 'BC test container name' in ⚙ presets first."},
                status_code=400)
        args: Dict[str, Any] = {
            "container_name": str(body["test_container_name"]),
            "credential_env": str(body.get("credential_env") or "BC_TEST_PASSWORD"),
        }
        for key in ("user", "tenant"):
            if body.get(key):
                args[key] = str(body[key])
        return await _tool("bc_env_preflight", args)

    async def metrics(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or await _tool("bc_metrics", {"spec_name": spec})

    async def root_cause(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        evidence = body.get("evidence")
        if isinstance(evidence, str):
            evidence = [line.strip() for line in evidence.splitlines() if line.strip()]
        missing = [k for k in ("symptom", "root_cause", "fix_approach")
                   if not str(body.get(k) or "").strip()]
        if missing or not evidence:
            return JSONResponse(
                {"error": f"missing: {', '.join(missing + ([] if evidence else ['evidence']))}",
                 "hint": "A diagnosis needs symptom, root cause, fix approach AND at least one "
                         "evidence reference (an .al path or 'table 11024121') — the server "
                         "verifies every reference against the repo."},
                status_code=400)
        args: Dict[str, Any] = {
            "spec_name": spec,
            "symptom": str(body["symptom"]).strip(),
            "root_cause": str(body["root_cause"]).strip(),
            "fix_approach": str(body["fix_approach"]).strip(),
            "evidence": evidence,
        }
        if str(body.get("regression_risk") or "").strip():
            args["regression_risk"] = str(body["regression_risk"]).strip()
        return await _tool("bc_root_cause", args)

    async def diff(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or JSONResponse(views.git_diff(root, spec))

    # ---- refinement lab ----------------------------------------------------
    async def intake_start(request: Request) -> JSONResponse:
        body = await _json_body(request)
        name = str(body.get("name") or "").strip()
        if not views.valid_spec_name(name):
            return JSONResponse({"error": "a short alphanumeric intake name is required"},
                                status_code=400)
        text = str(body.get("text") or "")
        docs = body.get("docs") or []
        if not text.strip() and not docs:
            return JSONResponse(
                {"error": "paste some material or attach at least one file",
                 "hint": "The refinement lab needs raw input: an email, requirement "
                         "notes, a spec draft — anything."}, status_code=400)
        started = await bridge.call("bc_intake_start", {"name": name, "text": text or None})
        if started.get("isError"):
            return JSONResponse(_friendly_error("bc_intake_start", started), status_code=502)
        for doc in docs[:10]:
            content = str(doc.get("content") or "")[:1_000_000]
            if content.strip():
                await bridge.call("bc_intake_add", {
                    "name": name, "filename": str(doc.get("filename") or "upload.md"),
                    "content": content})
        analyzed = await bridge.call("bc_intake_analyze", {"name": name})
        if analyzed.get("isError"):
            return JSONResponse(_friendly_error("bc_intake_analyze", analyzed), status_code=502)
        return JSONResponse({"tool": "bc_intake_start", "intake": f"intake-{name}",
                             "result": analyzed})

    async def intake_analyze(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or await _tool("bc_intake_analyze", {"name": spec})

    async def intake_graduate(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        lane, new_spec = str(body.get("lane") or ""), str(body.get("spec_name") or "").strip()
        if lane not in ("bug", "pbi", "feature") or not views.valid_spec_name(new_spec):
            return JSONResponse(
                {"error": "lane (bug|pbi|feature) and a valid spec_name are required",
                 "hint": "Epic-sized work: split into features first — epics are a "
                         "roll-up, not a lifecycle."}, status_code=400)
        args: Dict[str, Any] = {"name": spec, "lane": lane, "spec_name": new_spec}
        if body.get("work_item_id"):
            args["work_item_id"] = str(body["work_item_id"])
        children = [c.strip() for c in str(body.get("children") or "").splitlines() if c.strip()]
        if children:
            args["children"] = children
        return await _tool("bc_intake_graduate", args)

    # ---- frontier-wave wiring ------------------------------------------------
    async def consistency_check(request: Request) -> JSONResponse:
        spec = _spec(request)
        return _bad_spec(spec) or await _tool("bc_analyze_consistency", {"spec_name": spec})

    async def tool_health_ep(_: Request) -> JSONResponse:
        return await _tool("bc_tool_health", {})

    async def push_items(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        server_presets = routines_mod.load_server_presets(views.specs_root(root))
        org = str(body.get("org_url") or server_presets.get("org_url") or "")
        proj = str(body.get("project") or server_presets.get("project") or "")
        if not org or not proj:
            return JSONResponse(
                {"error": "ADO org URL and project are required",
                 "hint": "Set them in ⚙ presets (they are saved server-side too) or pass "
                         "them explicitly."}, status_code=400)
        items = body.get("items")
        if not items:
            # Default source: the intake's recorded graduation children.
            grad = views._read_json(views.specs_root(root) / spec / "graduation.json")
            items = [{"title": t} for t in (grad.get("children") or [])] if grad else []
        if not items:
            return JSONResponse(
                {"error": "no child items to push",
                 "hint": "Graduate the intake as a feature with child lines first, or pass "
                         "items explicitly."}, status_code=400)
        args: Dict[str, Any] = {
            "spec_name": spec, "org_url": org, "project": proj, "items": items,
            "confirm": bool(body.get("confirm")),
        }
        if body.get("parent_work_item_id"):
            args["parent_work_item_id"] = str(body["parent_work_item_id"])
        if body.get("item_type"):
            args["item_type"] = str(body["item_type"])
        return await _tool("bc_push_items", args)

    # ---- routines (user-defined schedules) ------------------------------------
    specs_base = views.specs_root(root)

    async def _routine_executor(action: str, presets: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic action menu — every step goes through the same MCP bridge."""
        overview = views.overview(root)
        items = [i for i in overview["items"] if not i.get("is_intake")]
        if action == "pr_sweep":
            details = []
            for item in items:
                if item["phase"] in ("pr_prepared", "pr_created"):
                    status = await bridge.call("bc_merge_status", {"spec_name": item["name"]})
                    comments = await bridge.call("bc_get_review_comments", {"spec_name": item["name"]})
                    details.append({"spec": item["name"],
                                    "merge": str(status.get("status", ""))[:60],
                                    "open_threads": len(comments.get("threads") or [])})
            return {"summary": f"PR sweep: {len(details)} mission(s) checked", "details": details}
        if action == "autopilot_sweep":
            details = []
            for item in items:
                if item["phase"] in ("archived", "merged") or item.get("is_feature"):
                    continue
                args = {"spec_name": item["name"], **{
                    k: v for k, v in presets.items()
                    if k in ("test_container_name", "test_extension_id", "credential_env",
                             "app_project_folder", "org_url", "project", "repository",
                             "target_branch")}}
                result = await bridge.call("bc_advance", args)
                details.append({"spec": item["name"],
                                "stop": str(result.get("stop") or result.get("status") or "")[:60]})
            return {"summary": f"autopilot sweep: advanced {len(details)} mission(s)",
                    "details": details}
        if action == "env_check":
            if not presets.get("test_container_name"):
                return {"error": "no container in server presets — save ⚙ presets first"}
            result = await bridge.call("bc_env_preflight", {
                "container_name": presets["test_container_name"],
                "credential_env": presets.get("credential_env", "BC_TEST_PASSWORD")})
            return {"summary": f"env check: {str(result.get('status', 'unknown'))[:80]}",
                    "details": result.get("checks")}
        if action == "tool_health":
            result = await bridge.call("bc_tool_health", {})
            candidates = result.get("improvement_candidates") or []
            return {"summary": f"tool health: {len(candidates)} improvement candidate(s)",
                    "details": [c.get("tool") for c in candidates]}
        if action == "consistency_sweep":
            details = []
            for item in items:
                pulse = views.item_pulse(root, item["name"])
                if pulse.get("stage") == "plan" and not pulse.get("is_feature"):
                    result = await bridge.call("bc_analyze_consistency", {"spec_name": item["name"]})
                    details.append({"spec": item["name"],
                                    "status": str(result.get("status", ""))[:40]})
            return {"summary": f"consistency sweep: {len(details)} plan-stage mission(s)",
                    "details": details}
        return {"error": f"unknown action '{action}'"}

    scheduler = routines_mod.RoutineScheduler(specs_base, _routine_executor)

    async def routines_list(_: Request) -> JSONResponse:
        return JSONResponse({"routines": routines_mod.load_routines(specs_base),
                             "actions": routines_mod.ACTIONS,
                             "runs": routines_mod.recent_runs(specs_base)})

    async def routines_save(request: Request) -> JSONResponse:
        body = await _json_body(request)
        raw_list = body.get("routines")
        if not isinstance(raw_list, list):
            return JSONResponse({"error": "routines must be a list"}, status_code=400)
        try:
            normalized = [routines_mod.validate_routine(r) for r in raw_list if isinstance(r, dict)]
        except ValueError as exc:
            return JSONResponse({"error": str(exc),
                                 "hint": "Each routine needs action, time HH:MM and days "
                                         "(daily | weekdays | weekend)."}, status_code=400)
        routines_mod.save_routines(specs_base, normalized)
        return JSONResponse({"saved": len(normalized), "routines": normalized})

    async def routine_run_now(request: Request) -> JSONResponse:
        rid = request.path_params["rid"]
        routine = next((r for r in routines_mod.load_routines(specs_base)
                        if r.get("id") == rid), None)
        if not routine:
            return JSONResponse({"error": "routine not found"}, status_code=404)
        record = await scheduler.run_routine(routine)
        return JSONResponse({"ran": True, "record": record})

    async def presets_get(_: Request) -> JSONResponse:
        return JSONResponse(routines_mod.load_server_presets(specs_base))

    async def presets_save(request: Request) -> JSONResponse:
        body = await _json_body(request)
        routines_mod.save_server_presets(specs_base, body)
        return JSONResponse({"saved": True})

    async def worktree(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        body = await _json_body(request)
        args: Dict[str, Any] = {"spec_name": spec,
                                "action": str(body.get("action") or "create")}
        for key in ("branch", "base_ref", "worktrees_base"):
            if body.get(key):
                args[key] = str(body[key])
        if body.get("force"):
            args["force"] = True
        return await _tool("bc_worktree", args)

    # ---- headless agent dispatch ------------------------------------------
    # The cockpit can hand judgment steps to the coding agent itself instead of
    # making the human ferry prompts. Guarded: only known agent CLIs may run.
    _dispatches: Dict[str, Dict[str, Any]] = {}
    _AGENT_BINARIES = {"opencode", "copilot", "gh"}
    _DEFAULT_TEMPLATE = "copilot -p {prompt} --allow-all-tools"

    def _default_prompt(spec: str, work_root: str) -> str:
        if spec.startswith("intake-"):
            return (
                f'Use the bc-refiner protocol on intake "{spec}" (project root: {work_root}). '
                f'Call bc_intake_analyze(name="{spec}"), study the dossier (precedents, code '
                "reality, open questions), then report: EVIDENCE, max-3 highest-ROI QUESTIONS "
                "with proposed defaults, and a lane PROPOSAL. Do NOT graduate without explicit "
                "human confirmation."
            )
        return (
            f'Continue item "{spec}" (project root: {work_root}). '
            f'Call bc_status(spec_name="{spec}") and follow enforcement.next_actions '
            "strictly in order until the next human gate; produce any model-authored "
            "artifact through its bc_* tool only. Stop and report when a human gate "
            "or missing input blocks you."
        )

    async def dispatch(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        active = _dispatches.get(spec)
        if active and active["proc"].returncode is None:
            return JSONResponse(
                {"error": "an agent run is already active for this mission",
                 "hint": "Wait for it to finish or stop it first."}, status_code=409)
        body = await _json_body(request)
        item_dir = views.specs_root(root) / spec
        wt = views._read_json(item_dir / "worktree.json")
        work_root = str(wt.get("path")) if wt and Path(str(wt.get("path", ""))).is_dir() else str(root)
        prompt = str(body.get("prompt") or _default_prompt(spec, work_root))
        template = str(body.get("template") or _DEFAULT_TEMPLATE)
        try:
            tokens = shlex.split(template, posix=True)
        except ValueError as exc:
            return JSONResponse({"error": f"bad template: {exc}"}, status_code=400)
        if not tokens or Path(tokens[0]).stem.lower() not in _AGENT_BINARIES:
            return JSONResponse(
                {"error": f"template must start with one of: {', '.join(sorted(_AGENT_BINARIES))}",
                 "hint": "Set 'Agent command template' in ⚙ presets, e.g. "
                         "'copilot -p {prompt} --allow-all-tools'."}, status_code=400)
        argv = [prompt if t == "{prompt}" else
                t.replace("{spec}", spec).replace("{project_root}", work_root)
                for t in tokens]
        runs_dir = item_dir / "agent-runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = runs_dir / f"{stamp}.log"
        log_fh = open(log_path, "ab")
        log_fh.write((" ".join(argv) + "\n\n").encode("utf-8", errors="replace"))
        log_fh.flush()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=work_root, stdout=log_fh, stderr=asyncio.subprocess.STDOUT)
        except (OSError, PermissionError) as exc:
            log_fh.close()
            return JSONResponse({"error": f"could not start agent: {exc}"}, status_code=502)
        _dispatches[spec] = {"proc": proc, "log": str(log_path), "fh": log_fh,
                             "argv0": argv[0], "started": time.time(), "cwd": work_root}
        return JSONResponse({"started": True, "pid": proc.pid, "log": str(log_path),
                             "cwd": work_root, "agent": argv[0]})

    async def dispatch_status(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        entry = _dispatches.get(spec)
        if not entry:
            return JSONResponse({"active": False})
        proc = entry["proc"]
        running = proc.returncode is None
        if not running and not entry["fh"].closed:
            entry["fh"].close()
        tail = ""
        try:
            data = Path(entry["log"]).read_bytes()
            tail = data[-4000:].decode("utf-8", errors="replace")
        except OSError:
            pass
        return JSONResponse({
            "active": True, "running": running, "returncode": proc.returncode,
            "agent": entry["argv0"], "cwd": entry["cwd"],
            "elapsed_s": round(time.time() - entry["started"]),
            "log": entry["log"], "tail": tail,
        })

    async def dispatch_stop(request: Request) -> JSONResponse:
        spec = _spec(request)
        bad = _bad_spec(spec)
        if bad:
            return bad
        entry = _dispatches.get(spec)
        if not entry or entry["proc"].returncode is not None:
            return JSONResponse({"stopped": False, "reason": "no active run"})
        entry["proc"].terminate()
        return JSONResponse({"stopped": True})

    async def health(_: Request) -> JSONResponse:
        try:
            result = await bridge.call("bc_health", {})
            return JSONResponse({"ok": True, "server": result})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        await bridge.start()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            await bridge.stop()

    return Starlette(
        routes=[
            Route("/", home),
            Route("/api/health", health),
            Route("/api/overview", overview),
            Route("/api/item/{spec}/pulse", pulse),
            Route("/api/item/{spec}/artifact", artifact),
            Route("/api/item/{spec}/capture", capture, methods=["POST"]),
            Route("/api/item/{spec}/advance", advance, methods=["POST"]),
            Route("/api/item/{spec}/auto-clarify", auto_clarify, methods=["POST"]),
            Route("/api/item/{spec}/answer", answer, methods=["POST"]),
            Route("/api/item/{spec}/decision", decision, methods=["POST"]),
            Route("/api/item/{spec}/guidance", guidance, methods=["POST"]),
            Route("/api/item/{spec}/feature-status", feature_status, methods=["POST"]),
            Route("/api/item/{spec}/pr-status", pr_status, methods=["POST"]),
            Route("/api/item/{spec}/pr-comments", pr_comments, methods=["POST"]),
            Route("/api/item/{spec}/prepare-review", prepare_review, methods=["POST"]),
            Route("/api/item/{spec}/request-approval", request_approval, methods=["POST"]),
            Route("/api/item/{spec}/run-tests", run_tests, methods=["POST"]),
            Route("/api/item/{spec}/metrics", metrics, methods=["POST"]),
            Route("/api/item/{spec}/root-cause", root_cause, methods=["POST"]),
            Route("/api/item/{spec}/diff", diff),
            Route("/api/intake/start", intake_start, methods=["POST"]),
            Route("/api/item/{spec}/intake-analyze", intake_analyze, methods=["POST"]),
            Route("/api/item/{spec}/intake-graduate", intake_graduate, methods=["POST"]),
            Route("/api/item/{spec}/worktree", worktree, methods=["POST"]),
            Route("/api/item/{spec}/dispatch", dispatch, methods=["POST"]),
            Route("/api/item/{spec}/dispatch-status", dispatch_status),
            Route("/api/item/{spec}/dispatch-stop", dispatch_stop, methods=["POST"]),
            Route("/api/item/{spec}/consistency", consistency_check, methods=["POST"]),
            Route("/api/item/{spec}/push-items", push_items, methods=["POST"]),
            Route("/api/tool-health", tool_health_ep, methods=["POST"]),
            Route("/api/routines", routines_list),
            Route("/api/routines", routines_save, methods=["POST"]),
            Route("/api/routines/{rid}/run", routine_run_now, methods=["POST"]),
            Route("/api/presets", presets_get),
            Route("/api/presets", presets_save, methods=["POST"]),
            Route("/api/env-preflight", env_preflight, methods=["POST"]),
            Mount("/static", StaticFiles(directory=str(_STATIC)), name="static"),
        ],
        middleware=[Middleware(_NoCacheMiddleware)],
        lifespan=lifespan,
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="BC Agentic Mission Control")
    parser.add_argument("--project-root", required=True, help="AL project root directory")
    parser.add_argument("--specs-root", default=None,
                        help="External governance artifacts base (same as server --specs-root)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="Open the cockpit in a browser")
    args = parser.parse_args()

    app = create_app(args.project_root, args.specs_root)
    url = f"http://{args.host}:{args.port}"
    print(f"Mission Control -> {url}  (project: {args.project_root})")
    if args.open:
        webbrowser.open(url)
    # h11 + ws="none": stick to uvicorn's guaranteed pure-Python protocols; the
    # auto-detected optional ones (httptools/websockets) break on version drift.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                http="h11", ws="none")


if __name__ == "__main__":
    main()
