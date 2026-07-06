"""Weak-agent simulations — deterministic personas fuzz the machine's guidance.

PARADIGM (2026-07-06): a strong model silently COMPENSATES for gaps in guidance —
a missing params_hint, a blocked response without a next_action, a validation
error that doesn't name the offending field. A weak model exposes every one of
them by getting stuck. Real weak LLMs are non-deterministic, so these personas
are SCRIPTED simulations of specific cognitive deficits, each driving the REAL
server closures (policy, stages, doom-guard, timeline, recovery all live):

- LITERALIST: only executes next_action.tool + params_hint; cannot improvise.
  Every gate must therefore carry an executable prescription.
- SKIPPER: calls lifecycle tools wildly out of order. Every out-of-order call
  must be refused WITH guidance and WITHOUT state corruption.
- FUZZER: garbage params (traversal, empty, wrong enums). Refusals must name
  the offending field; never a traceback; server keeps serving.
- REPEATER: repeats an identical failing call. The doom leash must fire with a
  human-readable, machine-followable refusal.
- VAGUE ANSWERER: answers clarifications with "yes ok". The machine must reject
  vagueness EXPLICITLY (say what a valid answer looks like) and stay bounded
  (questions must not multiply).
- AMNESIAC: forgets everything between calls; bc_status alone must carry the
  full resume surface (stage, timeline, on_disk, blockers in plain words).

THE RESPONSE CONTRACT (two-audience rule) is checked on EVERY response of every
persona: AI-friendly (deterministic status, registered next_action.tool,
params_hint keys that are REAL parameters of the target closure) and
human-friendly (reasons in words, no stack traces, no raw phase ids).
"""
import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "fixtures" / "golden"

# ---------------------------------------------------------------------------
# Shared simulation infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    for var in ("AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_EXT_PAT",
                "BC_MCP_TEAM_LESSONS_URL", "BC_MCP_TEAM_LESSONS_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BC_MCP_NO_WARMUP", "1")
    src = tmp_path / "extensions" / "BaseApp" / "src"
    src.mkdir(parents=True)
    for f in GOLDEN.glob("*.al"):
        (src / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    from bc_agentic_mcp.server import create_server
    server = create_server()
    tools = {t.name: t.fn for t in server._tool_manager._tools.values()}
    return SimHarness(tmp_path, tools)


class SimHarness:
    def __init__(self, root: Path, tools):
        self.root = root
        self.tools = tools
        self.transcript = []
        self.violations = []

    def call(self, _tool, **kw):
        result = asyncio.run(self.tools[_tool](**kw))
        self.transcript.append((_tool, result if isinstance(result, dict) else {"raw": str(result)}))
        if isinstance(result, dict):
            self.check_contract(_tool, result)
        return result

    # --- THE RESPONSE CONTRACT ---------------------------------------------
    def check_contract(self, tool, result):
        def violation(msg):
            self.violations.append(f"{tool}: {msg} :: {json.dumps(result, default=str)[:220]}")

        text_blob = json.dumps(result, default=str)
        # human-friendliness: never leak stack traces or repr internals
        if "Traceback (most recent call last)" in text_blob:
            violation("stack trace leaked to caller")
        status = str(result.get("status", ""))
        is_refusal = (result.get("isError") is True or result.get("blocked") is True
                      or status.startswith(("blocked", "error", "needs")))
        if not is_refusal:
            return
        # every refusal must explain itself in words
        words = str(result.get("reason") or result.get("message")
                    or (result.get("_meta") or {}).get("hint") or "")
        if len(words.strip()) < 15:
            violation("refusal without a human-readable reason")
        # machine-followability: a next_action that exists must be executable
        na = result.get("next_action") or ((result.get("_meta") or {}).get("details") or {}).get("next_action")
        if isinstance(na, dict) and na.get("tool"):
            target = na["tool"]
            if target not in self.tools:
                violation(f"next_action names unregistered tool '{target}'")
            else:
                sig_params = set(inspect.signature(self.tools[target]).parameters)
                hint = na.get("params_hint") or {}
                if isinstance(hint, dict):
                    bogus = set(hint) - sig_params
                    if bogus:
                        violation(f"params_hint keys {sorted(bogus)} are not parameters of {target}")

    def all_findings(self):
        return self.violations


DESC = ("Add a read-only indicator to the facilities overview. Modify page "
        "FacilitiesOfRealtyObjectFDN to surface the existing NoOfAddresses field so "
        "housing coordinators can see address counts. No new tables. No data upgrade.")

ANSWER = ("Read-only display of the existing NoOfAddresses field on page "
          "FacilitiesOfRealtyObjectFDN; evidence extensions/BaseApp/src/FacilitiesOfRealtyObject.Page.al; "
          "no permission changes, no upgrade. "
          "TEST negative: a facility without linked addresses shows 0 and no error. "
          "TEST edge: a facility with the maximum number of linked addresses renders unclipped.")


def _bootstrap_item(h: SimHarness, spec: str):
    """Common opening moves shared by personas (capture with hermetic identity)."""
    root = str(h.root)
    h.call("bc_init", project_root=root, module_name="BaseApp")
    h.call("bc_capture_item_context", project_root=root, spec_name=spec,
           work_item_id="990001", description=DESC)
    mpath = h.root / ".specs" / spec / "context" / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    manifest["identity"] = {"id": "990001", "type": "Product Backlog Item",
                            "title": "Surface NoOfAddresses on facilities overview"}
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# PERSONA: the Literalist — can ONLY do what next_action says
# ---------------------------------------------------------------------------

def test_literalist_every_gate_carries_an_executable_prescription(sim):
    """Walk plan->design purely by prescriptions. Wherever the machine blocks, the
    literalist executes next_action verbatim (only filling project_root/spec_name
    and its fixed answer text). A block without a followable prescription = the
    exact place a weak agent dies in production."""
    h, spec, root = sim, "lit-1", str(sim.root)
    _bootstrap_item(h, spec)
    out = h.call("bc_write_spec", project_root=root, spec_name=spec,
                 human_bullets=DESC, idempotency_key="lit-spec-1")
    assert out.get("machine_spec_path"), f"write_spec gave no path: {out.get('status')}"

    out = h.call("bc_plan_design", project_root=root, spec_name=spec)
    steps = 0
    while str(out.get("status", "")).startswith(("blocked", "needs")) and steps < 6:
        steps += 1
        na = out.get("next_action") or {}
        tool = na.get("tool")
        assert tool, f"literalist STUCK: blocked '{out.get('status')}' with no next_action"
        params = {k: v for k, v in (na.get("params_hint") or {}).items() if v not in (None, "", [])}
        params.setdefault("project_root", root)
        params.setdefault("spec_name", spec)
        if tool == "bc_mine_precedents":  # the hint tells it to skip+reason when no ADO
            params.setdefault("skip", True)
            params.setdefault("reason", "literalist walk: no ADO access in simulation")
        if tool == "bc_answer_clarification":
            cpath = h.root / ".specs" / spec / "clarifications.md"
            ids = re.findall(r"\b(Q-\d+)\b", cpath.read_text(encoding="utf-8")) if cpath.exists() else []
            params.setdefault("answers", {q: ANSWER for q in dict.fromkeys(ids)})
        if tool == "bc_write_spec":
            params.setdefault("human_bullets", DESC)
            params.setdefault("idempotency_key", f"lit-spec-{steps + 1}")
        h.call(tool, **params)
        out = h.call("bc_plan_design", project_root=root, spec_name=spec)
    assert not str(out.get("status", "")).startswith(("blocked", "needs")), \
        f"literalist never reached design in {steps} prescribed steps: {out.get('status')}"
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# PERSONA: the Skipper — everything out of order
# ---------------------------------------------------------------------------

def test_skipper_out_of_order_is_refused_with_guidance_not_corruption(sim):
    h, spec, root = sim, "skip-1", str(sim.root)
    _bootstrap_item(h, spec)
    # Straight to the end game, no spec, no approval, no evidence:
    jumps = [
        ("bc_prepare_pr", {}),
        ("bc_implement_write", {"code": "codeunit 50999 X {}", "file_path": "extensions/BaseApp/src/X.Codeunit.al"}),
        ("bc_verify", {}),
        ("bc_submit_decision", {"phase": "plan", "decision": "approve"}),
        ("bc_archive", {}),
    ]
    for tool, extra in jumps:
        out = h.call(tool, project_root=root, spec_name=spec, **extra)
        status = str(out.get("status", ""))
        refused = (out.get("isError") is True or out.get("blocked") is True
                   or status.startswith(("blocked", "error", "needs")) or out.get("ok") is False)
        assert refused, f"{tool} out of order was NOT refused (status={status!r})"
    # No corruption: nothing pretending progress happened.
    sdir = h.root / ".specs" / spec
    assert not (sdir / "pr" / "PR.md").exists(), "skipper created a PR artifact without a lifecycle"
    assert not (h.root / "extensions" / "BaseApp" / "src" / "X.Codeunit.al").exists(), \
        "skipper wrote AL code without approval"
    # The machine still serves normally afterwards.
    out = h.call("bc_status", project_root=root, spec_name=spec)
    assert out.get("specs"), "bc_status broken after out-of-order storm"
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# PERSONA: the Fuzzer — garbage in, explicit refusal out
# ---------------------------------------------------------------------------

def test_fuzzer_garbage_params_get_named_field_errors_not_tracebacks(sim):
    h, root = sim, str(sim.root)
    h.call("bc_init", project_root=root, module_name="BaseApp")
    # Cases are STAGE-LEGAL on purpose: the policy wall fires first for out-of-stage
    # calls (correct layering, proven by the skipper persona) — these must reach the
    # VALIDATORS, whose refusals must name the offending field.
    garbage = [
        ("bc_submit_decision", {"spec_name": "ghost", "phase": "yolo", "decision": "approve"}, "phase"),
        ("bc_submit_decision", {"spec_name": "ghost", "phase": "plan", "decision": "maybe"}, "decision"),
        # first failing input wins the refusal: nonexistent artifact is named explicitly
        ("bc_request_approval", {"spec_name": "ghost", "phase": "plan", "artifact_path": "x",
                                 "summary": "s", "idempotency_key": "!!"}, "artifact"),
    ]
    for tool, params, field in garbage:
        out = h.call(tool, project_root=root, **params)
        blob = json.dumps(out, default=str)
        refused = (out.get("isError") is True or str(out.get("status", "")).startswith(("blocked", "error"))
                   or out.get("ok") is False)
        assert refused, f"{tool} accepted garbage {field}: {blob[:200]}"
        assert "Traceback" not in blob, f"{tool} leaked a traceback for bad {field}"
        assert field.lower() in blob.lower(), \
            f"{tool} refusal does not NAME the offending field '{field}': {blob[:220]}"
    # Path traversal in spec_name must be refused or neutralized — never escape .specs/.
    out = h.call("bc_capture_item_context", project_root=root, spec_name="../../evil",
                 work_item_id="1", description="x")
    escaped = (h.root.parent.parent / "evil").exists() or (h.root / "evil").exists()
    assert not escaped, "path traversal in spec_name escaped the .specs sandbox!"
    # Server unharmed.
    assert h.call("bc_status", project_root=root).get("summary") is not None
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# PERSONA: the Repeater — identical failing call, again and again
# ---------------------------------------------------------------------------

def test_repeater_hits_the_leash_with_followable_refusal(sim):
    """A weak agent hammers the same POLICY-refused call forever — gate blocks are
    ledger-neutral by design, so before the escalation fix NOTHING ever told it to
    stop. By MAX_IDENTICAL_REFUSALS the refusal must escalate to an explicit STOP
    with a followable redirect (bc_status)."""
    h, spec, root = sim, "rep-1", str(sim.root)
    _bootstrap_item(h, spec)
    from bc_agentic_mcp import attempts as _attempts
    results = []
    for _ in range(_attempts.MAX_IDENTICAL_REFUSALS + 1):
        # bc_record_test is not callable in stage 'plan' — an eternal policy refusal.
        results.append(h.call("bc_record_test", project_root=root, spec_name=spec,
                              name="T", result="pass", covers="all", layer="al-unit"))
    last = results[-1]
    blob = json.dumps(last, default=str)
    assert "STOP" in blob, f"no STOP escalation after identical refusals: {blob[:260]}"
    details = (last.get("_meta") or {}).get("details") or {}
    assert details.get("identical_refusals", 0) >= _attempts.MAX_IDENTICAL_REFUSALS
    na = details.get("next_action") or {}
    assert na.get("tool") == "bc_status", "escalated refusal must redirect to bc_status"
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# PERSONA: the Vague Answerer — "yes ok" is not an answer
# ---------------------------------------------------------------------------

def test_vague_answers_rejected_explicitly_and_questions_stay_bounded(sim):
    h, spec, root = sim, "vague-1", str(sim.root)
    _bootstrap_item(h, spec)
    h.call("bc_write_spec", project_root=root, spec_name=spec,
           human_bullets=DESC, idempotency_key="vague-spec-1")
    from bc_agentic_mcp import precedents
    precedents.save(h.root, spec, {"skipped": True, "reason": "sim", "as_of": "t"})
    question_counts = []
    for round_no in range(3):
        out = h.call("bc_prepare_review", project_root=root, spec_name=spec,
                     human_bullets=DESC, idempotency_key=f"vague-rev-{round_no}")
        if not str(out.get("status", "")).startswith("needs"):
            break
        qids = [q.get("id") for q in out.get("questions", [])]
        question_counts.append(len(qids))
        ans = h.call("bc_answer_clarification", project_root=root, spec_name=spec,
                     answers={qid: "yes ok" for qid in qids})
        issues = ans.get("issues") or []
        rejected = ans.get("ok") is False or issues
        if rejected:
            blob = json.dumps(ans, default=str).lower()
            assert "evidence" in blob or ".al" in blob or "reference" in blob, \
                f"vague-answer rejection does not say WHAT a valid answer needs: {blob[:220]}"
    # Bounded: the question list must not multiply across rounds.
    assert len(set(question_counts)) <= 1, f"questions multiplied across rounds: {question_counts}"
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# PERSONA: the Amnesiac — bc_status alone must be a complete resume packet
# ---------------------------------------------------------------------------

def test_amnesiac_resumes_from_status_alone(sim):
    h, spec, root = sim, "amn-1", str(sim.root)
    _bootstrap_item(h, spec)
    h.call("bc_write_spec", project_root=root, spec_name=spec,
           human_bullets=DESC, idempotency_key="amn-spec-1")
    # Total amnesia strikes. The ONLY call the persona remembers is bc_status.
    out = h.call("bc_status", project_root=root, spec_name=spec)
    assert out.get("stage"), "resume packet lost the stage"
    tl = out.get("timeline") or {}
    assert tl.get("current_phase"), "resume packet lost the timeline story"
    on_disk = out.get("on_disk") or {}
    key_paths = [e["path"] for e in on_disk.get("key_files", [])]
    assert any(p.endswith("spec.json") for p in key_paths), "resume packet lost the disk map"
    assert "hint" in on_disk and "re-run" in on_disk["hint"], \
        "disk map does not TELL the agent to read instead of re-running"
    # And the very next spec-scoped call re-anchors identity without being asked.
    nxt = h.call("bc_timeline", project_root=root, spec_name=spec)
    assert nxt.get("ok") is not False
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# PERSONA: the Natural Writer — multi-line answers must not lose their evidence
# ---------------------------------------------------------------------------

def test_multiline_answer_evidence_is_seen(sim):
    """LIVE Gemini-Flash finding (2026-07-06): a natural multi-line answer carried
    .al evidence on line 3, but both answer parsers read ONLY the first line —
    'answer lacks AL file evidence' for an answer that had it, a prescription loop
    (follow the instruction -> same refusal)."""
    h, spec, root = sim, "nat-1", str(sim.root)
    _bootstrap_item(h, spec)
    multiline = ("TEST negative: a facility without addresses shows 0.\n"
                 "TEST edge: max addresses renders unclipped.\n"
                 "Evidence: extensions/BaseApp/src/FacilitiesOfRealtyObject.Page.al")
    out = h.call("bc_answer_clarification", project_root=root, spec_name=spec,
                 answers={"Q-901": multiline})
    # Whether or not Q-901 pre-exists, the write path must validate the FULL text.
    issues = [i for i in (out.get("issues") or []) if "lacks AL file evidence" in str(i)]
    assert not issues, f"multi-line evidence was not seen: {issues}"
    # And the engine that gates the lifecycle must agree with the write-path.
    from bc_agentic_mcp import enforcement
    clar = h.root / ".specs" / spec / "clarifications.md"
    clar.write_text(
        "# Clarifications for: nat-1\n\n## Q-901: test shapes?\n_Answer:_ TEST negative: zero case.\n"
        "TEST edge: max case.\nEvidence: extensions/BaseApp/src/FacilitiesOfRealtyObject.Page.al\n"
        "\n## Deterministic Quality Gate Failures\n\n- some dump\n",
        encoding="utf-8")
    status = enforcement.engine_status(h.root, spec)["engines"]["clarifications"]
    assert status["ok"] is True, f"engine still blind to multi-line evidence: {status}"
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# PERSONA: the Self-Approver — confusion + escape hatch must NOT bypass humans
# ---------------------------------------------------------------------------

def test_agent_cannot_override_human_gate_alone(sim):
    """LIVE weak-model finding (Haiku run, 2026-07-06): confused by a stale-packet
    blocker, the agent invented 'sandbox testing' as override_reason and approved
    ITS OWN plan. Override is a human act: without confirm_human it must be refused
    — and the refusal must say to ask the human, not suggest another workaround."""
    h, spec, root = sim, "selfapp-1", str(sim.root)
    _bootstrap_item(h, spec)
    out = h.call("bc_submit_decision", project_root=root, spec_name=spec,
                 phase="plan", decision="approve",
                 override_reason="sandbox testing, just moving forward")
    assert out.get("status") == "blocked_override_needs_human", \
        f"agent-only override was NOT refused: {out.get('status')}"
    blob = json.dumps(out, default=str).lower()
    assert "human" in blob and "confirm_human" in blob
    assert h.all_findings() == [], "\n".join(h.all_findings())


# ---------------------------------------------------------------------------
# REPO-BOUNDARY GUARD: the machine must never mutate an ENCLOSING repository
# ---------------------------------------------------------------------------

def test_non_repo_project_root_never_reaches_enclosing_repo(tmp_path):
    """LIVE collateral (Haiku run, 2026-07-06): the sandbox was not a git repo, git
    resolved the ENCLOSING Brain repo, and the machine's 'commit or stash' advice
    made the agent stash an unrelated repo's work-in-progress. Now: a non-repo root
    passes clean-latest with an explicit note, and stash/pull REFUSE outright."""
    import subprocess
    from bc_agentic_mcp import repo_state
    enclosing = tmp_path / "outer"
    project = enclosing / "inner-project"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(enclosing)], check=True)
    (enclosing / "wip.txt").write_text("precious work-in-progress", encoding="utf-8")

    st = repo_state.status(str(project))
    assert st["is_repo"] is False
    assert st["enclosing_repo"] and st["enclosing_repo"].endswith("outer")

    check = repo_state.is_clean_latest(str(project))
    assert check["ok"] is True  # nothing of OURS can be stale
    assert "never be committed/stashed" in check["note"]

    stashed = repo_state.stash(str(project))
    assert stashed["stashed"] is False and "refused" in stashed["output"]
    pulled = repo_state.make_latest(str(project))
    assert pulled["pulled"] is False and "refused" in pulled["output"]
    # The enclosing repo's WIP is untouched.
    assert (enclosing / "wip.txt").read_text(encoding="utf-8") == "precious work-in-progress"


# ---------------------------------------------------------------------------
# VARIATION SWEEP: edge-case inputs — never crash, always explicit + followable
# ---------------------------------------------------------------------------

VARIATIONS = {
    "dutch": ("Voeg een alleen-lezen indicator toe aan het facilities overzicht. "
              "Wijzig page FacilitiesOfRealtyObjectFDN zodat het bestaande veld "
              "NoOfAddresses zichtbaar wordt. Geen nieuwe tabellen. Geen data upgrade."),
    "negation_without": ("Show the existing NoOfAddresses field on page "
                         "FacilitiesOfRealtyObjectFDN, without any data upgrade and "
                         "without new tables."),
    "unicode_noise": ("Add ✨ a read-only indicator — modify page "
                      "FacilitiesOfRealtyObjectFDN to surface NoOfAddresses (naïve café ①). "
                      "No data upgrade."),
    "minimal_vague": "make facilities better",
    "huge_rambling": ("The facilities overview should show address counts. " * 180
                      + " Modify page FacilitiesOfRealtyObjectFDN. No data upgrade."),
}


@pytest.mark.parametrize("variant", sorted(VARIATIONS))
def test_variation_inputs_never_crash_and_stay_followable(sim, variant):
    h, root = sim, str(sim.root)
    spec = f"var-{variant}"
    text = VARIATIONS[variant]
    h.call("bc_init", project_root=root, module_name="BaseApp")
    h.call("bc_capture_item_context", project_root=root, spec_name=spec,
           work_item_id="990002", description=text)
    out = h.call("bc_write_spec", project_root=root, spec_name=spec,
                 human_bullets=text, idempotency_key=f"var-{variant}-1")
    status = str(out.get("status", ""))
    if out.get("machine_spec_path") and not status.startswith(("blocked", "needs")):
        # Grounded straight away — negation variants must NOT drag in phantom upgrades.
        if variant in ("dutch", "negation_without"):
            spec_json = json.loads(Path(out["machine_spec_path"]).read_text(encoding="utf-8"))
            phantom = [o for o in spec_json.get("objects_to_create", [])
                       if o.get("subtype") == "upgrade"]
            assert not phantom, f"'{variant}' negation spawned a phantom upgrade codeunit"
            assert "upgrade" not in spec_json.get("work_types", []), \
                f"'{variant}' negation tagged upgrade work_types"
    else:
        # Refusal path: must be explicit (reason in words) and followable (next_action
        # or open_questions present) — the contract checker enforces the rest.
        has_followup = bool(out.get("next_action") or out.get("open_questions")
                            or out.get("questions"))
        assert has_followup, \
            f"'{variant}' refused with status={status!r} but no followable next step"
    # The machine survived and still answers.
    assert h.call("bc_status", project_root=root).get("summary") is not None
    assert h.all_findings() == [], "\n".join(h.all_findings())
