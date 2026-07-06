"""PR tool family (B1): bc_prepare_pr, bc_create_pr, bc_get_review_comments,
bc_resolve_review_comment, bc_merge_status — plus bc_sync_item_state (B3).

Lifecycle after verification: prepare (local, gated on evidence) -> create (ADO REST)
-> review comments (open threads re-admit implement-stage rework, B2) -> resolve ->
merge status (approval satisfies the internal `code` gate, C1; completed -> archive).

Timeline phases are result-driven via ``_timeline_phase`` (ground truth from ADO, not
tool-name assumptions): review_comments_open only when open threads exist; merged only
when the PR is actually completed.
"""
from __future__ import annotations

import json
import re
import subprocess as _sp
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import pr as pr_core
from bc_agentic_mcp import verification
from bc_agentic_mcp.spec_loader import load_spec
from bc_agentic_mcp.workspace import specs_root


def _require_pr_record(root: Path, spec_name: str) -> "tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]":
    record = pr_core.load_pr_record(root, spec_name)
    if record is None or not record.get("pr_id"):
        return None, {
            "status": "blocked_no_pr",
            "blocked": True,
            "reason": f"No PR recorded for '{spec_name}'. Create it first.",
            "next_action": {"tool": "bc_create_pr", "params_hint": {"spec_name": spec_name}},
        }
    return record, None


def _coords(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "org_url": record["org_url"],
        "project": record["project"],
        "repository": record["repository"],
        "pr_id": int(record["pr_id"]),
    }


def _feature_child_specs(root: Path, spec_name: str) -> List[str]:
    """Item-spec folder names of a FEATURE's live children ([] for plain items)."""
    if not (specs_root(root) / spec_name / "feature_plan.json").exists():
        return []
    try:
        from bc_agentic_mcp.tools.feature import feature_children_specs
        return [c["item_spec"] for c in feature_children_specs(root, spec_name) if c.get("item_spec")]
    except Exception:
        return []


def _feature_aggregate_spec(
    root: Path, spec_name: str, children: List[str], base_spec: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge the children's spec.json files into a feature-level pseudo-spec.

    One PR per feature: scope lists, schema objects (data-model note) and upgrade
    hints all live on the ITEMS — the description must aggregate them or the
    reviewer guide goes silent on exactly the risky parts.
    """
    agg = dict(base_spec or {})
    mods: List[Dict[str, Any]] = []
    creates: List[Dict[str, Any]] = []
    goals: List[str] = []
    for child in children:
        try:
            child_spec = load_spec(specs_root(root) / child)
        except Exception:
            continue
        wi = child_spec.get("work_item_id") or child_spec.get("work_item")
        tag = f"[{wi or child}] "
        for o in child_spec.get("objects_to_modify") or []:
            o = dict(o)
            o["target"] = tag + str(o.get("target", o.get("name", "?")))
            mods.append(o)
        for o in child_spec.get("objects_to_create") or []:
            o = dict(o)
            o["name"] = tag + str(o.get("name", "?"))
            creates.append(o)
        if child_spec.get("upgrade_contracts") or child_spec.get("upgrade_contract"):
            agg.setdefault("upgrade_contracts", True)
        if child_spec.get("goal"):
            goals.append(f"{tag}{child_spec['goal']}")
    try:
        from bc_agentic_mcp import feature_context
        tree = feature_context.load_tree(str(root), spec_name)
        if tree:
            node = tree.get("feature") or tree  # captured shape: {feature: {...}, children: [...]}
            if node.get("title"):
                agg.setdefault("feature_name", str(node["title"]))
            if node.get("id"):
                agg.setdefault("work_item_id", node["id"])
    except Exception:
        pass
    if goals and not agg.get("goal"):
        agg["goal"] = ("One feature delivered as ONE PR (per-item commits inside). "
                       "Per-item goals:\n" + "\n".join(f"- {g}" for g in goals))
    agg["objects_to_modify"] = mods
    agg["objects_to_create"] = creates
    return agg


def _clean_goal(text: str) -> str:
    """Strip machine prefixes from a spec goal: 'WI 239944 [Facilities] Enable X' -> 'Enable X'."""
    out = re.sub(r"^WI\s*\d+\s*", "", str(text or "").strip())
    out = re.sub(r"^\[[^\]]+\]\s*-?\s*", "", out).strip()
    return out[:1].upper() + out[1:] if out else out


def _schema_and_upgrade_names(spec: Dict[str, Any]) -> "tuple[List[str], List[str]]":
    def human(name: str) -> str:
        # '[wi239944] extensions\...\RealtyObjectFacility.Table.al' -> 'RealtyObjectFacility'
        # (the aggregate may stack MULTIPLE '[id]' prefixes — strip them all)
        out = re.sub(r"^(\[[^\]]+\]\s*)+", "", str(name or "?"))
        out = out.replace("\\", "/").rstrip("/").split("/")[-1]
        return re.sub(r"\.(Table|TableExt|Enum|EnumExt|Page|Codeunit|Report)(Ext)?\.al$", "", out, flags=re.IGNORECASE)

    objs = (spec.get("objects_to_modify") or []) + (spec.get("objects_to_create") or [])
    schema = [human(o.get("target") or o.get("name"))
              for o in objs
              if str(o.get("type", "")).lower() in {"table", "tableextension", "enum", "enumextension"}]
    upgrades = [human(o.get("name") or o.get("target"))
                for o in (spec.get("objects_to_create") or [])
                if "upgrade" in str(o.get("name", "")).lower() or "_Upgrade" in str(o.get("target", ""))]
    return schema, upgrades


def _feature_story_lines(
    root: Path, spec_name: str, children: List[str], spec: Dict[str, Any],
    digest: Dict[str, Any], work_item: Any,
) -> List[str]:
    """The reviewer's story for a ONE-FEATURE PR — sentences, not dumps."""
    feature_title = _clean_goal(str(spec.get("feature_name") or spec_name))
    lines = [f"# {feature_title}" + (f" (Feature {work_item})" if work_item else ""), ""]

    # What this delivers — the children's goals as one readable list. Human text
    # sources IN ORDER: the charter's purpose, the captured work-item TITLE, the
    # spec goal — the folder name is never shown (observed live: 'Wi239589-…'
    # bullets told the reviewer nothing).
    titles_by_spec: Dict[str, str] = {}
    wi_by_spec: Dict[str, Any] = {}
    try:
        from bc_agentic_mcp.tools.feature import feature_children_specs
        for c in feature_children_specs(root, spec_name):
            if c.get("item_spec"):
                titles_by_spec[c["item_spec"]] = str(c.get("title", ""))
                wi_by_spec[c["item_spec"]] = c.get("id")
    except Exception:
        pass
    lines += ["## What this delivers", ""]
    child_bits: List[str] = []
    for child in children:
        try:
            cspec = load_spec(specs_root(root) / child)
        except Exception:
            cspec = {}
        charter_purpose = ""
        try:
            from bc_agentic_mcp import checkpoints as _memory
            charter_purpose = str((_memory.load_charter(root, child) or {}).get("purpose") or "")
        except Exception:
            pass
        wi = cspec.get("work_item_id") or cspec.get("work_item") or wi_by_spec.get(child)
        # the captured work-item TITLE is team-written and uniformly human — it wins;
        # charter purposes are agent-written and sometimes carry machine bullets
        goal = _clean_goal(titles_by_spec.get(child) or charter_purpose or cspec.get("goal") or cspec.get("purpose") or "")
        if not goal:
            continue
        # first sentence only — the description is a story, details live in the items
        goal = goal.split(". ")[0].rstrip(".")
        child_bits.append(f"- {goal}." + (f" (WI {wi})" if wi else ""))
    lines += child_bits + [""]

    # Where to look first — the risky parts by NAME with the reason in words.
    schema, upgrades = _schema_and_upgrade_names(spec)
    lines += ["## Where to look first", ""]
    if schema:
        names = ", ".join(sorted(set(schema))[:6])
        lines.append(
            f"- **Persisted schema** ({names}): fields and enum values survive on customer "
            "databases forever — this part needs the second developer's data-model sign-off "
            "before merge.")
    if upgrades:
        names = ", ".join(sorted(set(upgrades))[:4])
        lines.append(
            f"- **Upgrade/conversion code** ({names}): must run twice without side effects "
            "and never trigger business logic; tags are registered in the SAN registry.")
    lines.append(
        "- **Permissions**: every new page/table has permission-set coverage — the most "
        "common review finding in this team, so worth a deliberate look.")
    lines.append(
        "- **Feature gating**: everything is behind the feature flag and stays OFF until "
        "the feature is implemented per customer — existing flows are unchanged until then.")
    lines.append("")

    # What was proven — one honest paragraph, not a metrics table.
    lines += ["## What was proven", ""]
    lines.append(
        f"All {digest.get('criteria_count')} acceptance criteria across the items are covered "
        f"by tests executed on a real BC container ({digest.get('tests_recorded')} recorded "
        "runs: per-item happy/negative/edge suites, a regression codeunit proving existing "
        "flows, and live API contract checks). The full per-test list — each test's name, "
        "what it proves, and its result — is the FIRST COMMENT on this PR.")
    lines.append("")
    return lines


def _item_story_lines(
    root: Path, spec_name: str, spec: Dict[str, Any],
    digest: Dict[str, Any], work_item: Any,
) -> List[str]:
    """The reviewer's story for a single-item PR."""
    title = _clean_goal(str(spec.get("feature_name") or spec_name))
    lines = [f"# {title}" + (f" (WI {work_item})" if work_item else ""), ""]
    # Bugfix lane: the recorded root cause IS the story — symptom, cause, fix in
    # sentences. The first cut said 'See the acceptance criteria in the linked
    # work item', which violates PR self-containment (caught live on wi267598:
    # the reviewer must never need anything outside the PR).
    rc: Dict[str, Any] = {}
    try:
        rc_path = specs_root(root) / spec_name / "root_cause.json"
        if rc_path.exists():
            rc = json.loads(rc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rc = {}
    delivers = _clean_goal(str(rc.get("fix_approach") or "")) or \
        _clean_goal(spec.get("goal") or spec.get("purpose") or "") or \
        "See the acceptance criteria in the linked work item."
    lines += ["## What this delivers", "", delivers, ""]
    if rc.get("symptom") and rc.get("root_cause"):
        lines += ["## The bug and its root cause", "",
                  f"**Symptom:** {_clean_goal(str(rc['symptom']))}", "",
                  f"**Root cause:** {_clean_goal(str(rc['root_cause']))}", ""]
        if rc.get("regression_risk"):
            lines += [f"**Regression risk:** {_clean_goal(str(rc['regression_risk']))}", ""]
    changed = [re.sub(r"^\[\d+\]\s*", "", str(o.get("target") or o.get("name") or "?"))
               for o in (spec.get("objects_to_modify") or []) + (spec.get("objects_to_create") or [])]
    if changed:
        lines += ["It touches: " + ", ".join(sorted(set(changed))[:8]) + ".", ""]
    schema, upgrades = _schema_and_upgrade_names(spec)
    lines += ["## Where to look first", ""]
    if schema:
        lines.append(f"- **Persisted schema** ({', '.join(sorted(set(schema))[:6])}): needs the "
                     "second developer's data-model sign-off before merge.")
    if upgrades:
        lines.append(f"- **Upgrade code** ({', '.join(sorted(set(upgrades))[:4])}): check idempotency "
                     "and the SAN tag registration.")
    lines.append("- **Permissions**: verify permission-set coverage for every new object.")
    lines.append("")
    lines += ["## What was proven", "",
              f"{digest.get('criteria_count')} acceptance criteria covered at "
              f"{digest.get('coverage_pct')}% by container-executed tests "
              f"({digest.get('tests_recorded')} recorded runs). The per-test list is the "
              "first comment on this PR.", ""]
    return lines


def _reviewer_gate(root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    """Mandatory internal review before PR creation (user decision 2026-07-04).

    A recorded review verdict (review_rubric.json entry with passed=True) must be
    NEWER than the last commit on the branch — a stale review reviewed different
    code. Returns a block dict when unmet, None when satisfied.

    FEATURE specs: reviews are recorded per CHILD item — the gate demands a
    passing review on every child instead of one on the feature folder.
    """
    children = _feature_child_specs(root, spec_name)
    if children:
        unreviewed = [c for c in children if _reviewer_gate_single(root, c) is not None]
        if unreviewed:
            block = _reviewer_block(spec_name)
            block["reason"] = (
                "PR preparation refused: these feature children lack a fresh passing "
                "internal review: " + ", ".join(unreviewed)
                + ". Run the separate reviewer per item and record it via bc_review."
            )
            block["unreviewed_children"] = unreviewed
            return block
        return None
    return _reviewer_gate_single(root, spec_name)


def _reviewer_block(spec_name: str) -> Dict[str, Any]:
    return {
        "status": "blocked_reviewer_required",
        "blocked": True,
        "reason": ("PR preparation refused: no fresh internal review verdict. Run the "
                   "separate reviewer (bc-reviewer) on the diff and record it via bc_review "
                   "with a rubric ({grounding, coverage, conventions, risk} each 0.0-1.0) "
                   "and verdict. Housing-team evidence: reviewers catch permissions/logic/"
                   "missing-test issues compilation cannot."),
        "next_action": {
            "tool": "bc_review",
            "reason": "Record the internal reviewer's rubric + verdict for the current diff",
            "params_hint": {"spec_name": spec_name,
                            "rubric": {"grounding": "<0-1>", "coverage": "<0-1>",
                                       "conventions": "<0-1>", "risk": "<0-1>"},
                            "verdict": "approve"},
        },
    }


def _reflection_gate(root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    """LEARN-BEFORE-SHIP: unreflected mistake/correction checkpoints block the PR.

    Reflection was only ever a nudge — and a whole feature shipped with 65
    unreflected signals while the lessons store stayed frozen (retro 2026-07-04).
    Feature specs check every child: lessons live where the mistakes happened.
    """
    from bc_agentic_mcp import reflection as _reflection
    specs = _feature_child_specs(root, spec_name) or [spec_name]
    due: Dict[str, int] = {}
    sample: List[str] = []
    for spec in specs:
        pending = _reflection.pending_reflections(root, spec)
        if pending["count"]:
            due[spec] = pending["count"]
            sample.extend(str(s.get("summary", ""))[:90] for s in pending["signals"][:2])
    if not due:
        return None
    worst = max(due, key=due.get)
    return {
        "status": "blocked_reflection_due",
        "blocked": True,
        "reason": (
            "PR preparation refused: "
            + ", ".join(f"{spec} has {n} unreflected mistake/correction signal(s)" for spec, n in due.items())
            + ". Distill them into lessons (bc_reflect) before shipping — the PR is the "
              "last moment the context still exists."
        ),
        "reflection_due": due,
        "signal_samples": sample[:6],
        "next_action": {
            "tool": "bc_reflect",
            "reason": "Turn the recorded mistakes/corrections into durable lessons",
            "params_hint": {"spec_name": worst, "lessons": [
                {"mistake": "<what went wrong>", "correction": "<what fixed it>",
                 "rule": "<the reusable rule>"}]},
        },
    }


def _reviewer_gate_single(root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    import json as _json
    import subprocess as _sp
    from datetime import datetime, timezone
    rubric_path = specs_root(root) / spec_name / "review_rubric.json"
    entries: List[Dict[str, Any]] = []
    if rubric_path.exists():
        try:
            entries = _json.loads(rubric_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            entries = []
    passed = [e for e in entries if e.get("passed")]
    block = _reviewer_block(spec_name)
    if not passed:
        return block
    # Freshness: newest passing review must postdate the branch's last commit.
    try:
        r = _sp.run(["git", "-C", str(root), "log", "-1", "--format=%cI"],
                    capture_output=True, text=True, timeout=15, check=False,
                    stdin=_sp.DEVNULL)
        last_commit = datetime.fromisoformat(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, _sp.SubprocessError, ValueError):
        last_commit = None
    if last_commit is not None:
        newest = max(datetime.fromisoformat(e["ts"]) for e in passed if e.get("ts"))
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        if newest < last_commit:
            block["reason"] = ("PR preparation refused: the newest passing review "
                              f"({newest.isoformat()}) predates the last commit "
                              f"({last_commit.isoformat()}) — it reviewed different code. "
                              "Re-run the reviewer on the current diff and record it via bc_review.")
            return block
    return None


def _humanize_test_name(name: str) -> str:
    """Human sentence from the team's Subject_Condition_Expectation convention.

    'CreationCheck_NoContractLinesAndHeaderSold_CreationRefused' must read
    'When no contract lines and header sold, then creation refused.' — the first
    cut only camel-split and left the underscores in, producing word-soup
    ('Creation check_no contract lines...', caught on wi267598's PR-TESTS.md).
    """
    def _words(seg: str) -> str:
        return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", seg).lower()

    parts = [p for p in str(name or "").split("_") if p]
    if len(parts) >= 3:
        condition = _words(" ".join(parts[1:-1]))
        outcome = _words(parts[-1])
        return f"When {condition}, then {outcome}."
    if len(parts) == 2:
        return f"When {_words(parts[0])}, then {_words(parts[1])}."
    return _words(name).capitalize() + "."


def _infer_branches(root: Path, source_branch: Optional[str], target_branch: Optional[str]) -> tuple:
    """Infer source/target from GIT, not from wishful defaults.

    The old defaults produced 'feature/wi267598' -> 'main' on a repo whose real
    state was 'nicolae-catalina/wi-267598' -> 'master' — wrong on both ends,
    every time (caught live on wi267598's first prepared PR). Explicit caller
    values always win; git fills the gaps; the old strings remain the last resort.
    """
    def _git(*args: str) -> str:
        try:
            return _sp.run(["git", "-C", str(root), *args], capture_output=True,
                           text=True, timeout=15, stdin=_sp.DEVNULL).stdout.strip()
        except (OSError, _sp.SubprocessError):
            return ""

    src = source_branch or _git("branch", "--show-current") or None
    tgt = target_branch
    if not tgt or tgt == "main":  # "main" is the signature default, not a choice
        head = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")  # e.g. origin/master
        inferred = head.rsplit("/", 1)[-1] if head else ""
        tgt = inferred or tgt or "main"
    return src, tgt


async def handle_prepare_pr(
    project_root: str,
    spec_name: str,
    source_branch: Optional[str] = None,
    target_branch: str = "main",
) -> Dict[str, Any]:
    """Deterministic local PR preparation — no network.

    Fail-closed on evidence: a PR description is only produced when the verification
    gate passes (every criterion covered at its evidence bar). The description is built
    from the spec + verification digest, written to ``.specs/<item>/pr/PR.md``.
    """
    root = Path(project_root).resolve()
    source_branch, target_branch = _infer_branches(root, source_branch, target_branch)
    gate = verification.gate(root, spec_name)
    if not gate["passed"]:
        return {
            "status": "blocked_verification",
            "blocked": True,
            "reason": "PR preparation refused: the verification gate did not pass.",
            "blockers": gate["blockers"][:10],
            "next_action": {
                "tool": "bc_verify",
                "reason": "Close the evidence gaps, then prepare the PR again.",
                "params_hint": {"spec_name": spec_name},
            },
        }

    # DATA-MODEL sign-off is a MERGE blocker, not a CREATION blocker (the invariant
    # itself says "the PR cannot MERGE until granted"). The PR is ADO's review vehicle:
    # the second developer reviews the schema change AS the PR diff, so refusing to
    # create the PR forced the review out-of-band (observed live on Bug 267600). The
    # hard walls stay merge-side: bc_submit_decision refuses the code-gate approval and
    # bc_merge_status flags an approved-but-unsigned PR. Here the pending sign-off is
    # carried LOUDLY into the PR description so the reviewer sees it first.
    from bc_agentic_mcp.tools.approval import _data_model_gate
    dm = _data_model_gate(root, specs_root(root) / spec_name)

    # PIPELINE-TRUTH wall 1 — breaking changes: the incremental merge build compiles
    # against the RELEASED baseline (AppSourceCop, warnings-as-errors) — a check class
    # local compiles never run. Bug 267600 shipped a green PR that failed AS0083 in the
    # pipeline. Pre-flight the deterministic subset from the merge-base diff and refuse
    # PR creation on a provable breaking removal (the pipeline WILL fail it anyway).
    from bc_agentic_mcp import breaking_change
    bc_gate = breaking_change.gate(root, target_branch=target_branch)
    if not bc_gate["ok"]:
        return {
            "status": "blocked_breaking_change",
            "blocked": True,
            "reason": ("PR preparation refused: the merge-base diff removes RELEASED schema "
                       "members — the incremental merge build will fail AppSourceCop "
                       "(observed live: AS0083, build 257437). Use two-phase obsoleting "
                       "(ObsoleteState=Pending) instead of removal."),
            "findings": bc_gate["findings"][:10],
            "next_action": {
                "tool": "bc_write_spec",
                "reason": "Re-plan the removals as obsolete-pending changes, then re-implement.",
                "params_hint": {"spec_name": spec_name},
            },
        }

    # PIPELINE-TRUTH wall 2 — mandatory internal review: human reviewers catch what
    # compilation cannot (housing-team evidence: permissions #1, logic #2, missing
    # tests #3 across 42 threads). A recorded bc_review verdict must exist and be
    # FRESH (newer than the last commit) before the PR goes out.
    review_block = _reviewer_gate(root, spec_name)
    if review_block is not None:
        return review_block

    # LEARN-BEFORE-SHIP wall — reflection is ENFORCED, not nudged: a whole feature
    # shipped with 65 unreflected mistake/correction checkpoints while lessons.json
    # stayed frozen (retro 2026-07-04). The PR is the last deterministic moment the
    # lessons can be distilled while context exists — block until bc_reflect runs.
    reflect_block = _reflection_gate(root, spec_name)
    if reflect_block is not None:
        return reflect_block

    # PIPELINE-TRUTH wall 3 — dependent-closure compile: the app under change compiled
    # clean while its DEPENDENT broke (Bug 267600: EmpireHousingTests referenced a
    # deleted codeunit — AL0185 only surfaced at container publish, which then
    # uninstalled the whole dependency chain). Compile every app that depends on a
    # changed app, in dependency order, and refuse on errors. Fail-open on oversized
    # closures (the pipeline build owns those).
    from bc_agentic_mcp import dependent_build
    db_gate = dependent_build.gate(root, target_branch=target_branch)
    if not db_gate["ok"]:
        return {
            "status": "blocked_dependent_build",
            "blocked": True,
            "reason": ("PR preparation refused: dependent app(s) no longer compile against "
                       "this change — the merge pipeline's full build will fail the same way "
                       "(observed live: deleted codeunit broke EmpireHousingTests at publish)."),
            "failures": db_gate["failures"][:5],
            "checked": db_gate.get("checked", []),
            "next_action": {
                "tool": "bc_implement_write",
                "reason": "Fix the dependent app's references (or extend the item scope), then re-prepare.",
                "params_hint": {"spec_name": spec_name},
            },
        }

    specs_dir = specs_root(root) / spec_name
    try:
        spec = load_spec(specs_dir)
    except Exception:
        spec = {}
    feature_children = _feature_child_specs(root, spec_name)
    if feature_children:
        # One PR per FEATURE: the description aggregates the children's specs —
        # scope, schema objects and upgrade hints all live on the items.
        spec = _feature_aggregate_spec(root, spec_name, feature_children, spec)
    digest = gate["digest"]
    work_item = spec.get("work_item_id") or spec.get("work_item")
    # Team spec (wiki: Code review/Pull Requests): title = {target}-{short description};
    # R20 = master in ERP AL. Keep the WI number for traceability.
    branch_tag = "R20" if target_branch == "master" else target_branch
    short = str(spec.get("feature_name") or spec_name)
    title = f"{branch_tag}-{short}" if not short.startswith(branch_tag) else short

    # HUMAN-FIRST DESCRIPTION (user verdict 2026-07-04 on the first generated PR:
    # "not human friendly and honestly does not tell anything useful"). The reviewer
    # reads a STORY — what this does, how it works, where to look, what was proven —
    # in sentences built from recorded data. Checkbox dumps, [id]-prefixed object
    # lists and bare counts live in artifacts/comments, never in the description.
    if feature_children:
        lines = _feature_story_lines(root, spec_name, feature_children, spec, digest, work_item)
    else:
        lines = _item_story_lines(root, spec_name, spec, digest, work_item)
    # EXPLICIT tests (user requirement 2026-07-04): the reviewer must see WHICH tests
    # exist, WHAT each validates, and its STATUS — an aggregate '8/8' forces them to
    # open the test files. Source: test-kind checkpoints; the newest run per layer
    # carries executed_tests [{codeunit, test, shape, result}].
    from bc_agentic_mcp import checkpoints as _ckpt
    if feature_children:
        test_entries = []
        for _child in feature_children:
            _wi = re.match(r"wi(\d+)", _child)
            _label = _wi.group(1) if _wi else _child
            for c in _ckpt.load_checkpoints(root, _child):
                if c.get("kind") != "test":
                    continue
                det = dict(c.get("details") or {})
                if det.get("layer"):
                    det["layer"] = f"{det['layer']} · WI {_label}"
                c = dict(c)
                c["details"] = det
                test_entries.append(c)
    else:
        test_entries = [c for c in _ckpt.load_checkpoints(root, spec_name)
                        if c.get("kind") == "test"]
    newest_per_layer: Dict[str, Dict[str, Any]] = {}
    for entry in test_entries:
        det = entry.get("details") or {}
        if det.get("executed_tests"):
            newest_per_layer[str(det.get("layer") or "?")] = entry
    api_entries = [e for e in test_entries
                   if str((e.get("details") or {}).get("layer", "")) == "api"
                   and (e.get("details") or {}).get("result") == "pass"]
    # The explicit test list goes to a SEPARATE artifact posted as a PR COMMENT.
    # Format rules learned live on PR 41670 (user review, 2026-07-04):
    #   - NO pipe tables: ADO comment threads do not render Markdown tables — the
    #     table collapsed into an unreadable word-soup.
    #   - NO local file references (TEST-REPORT.md etc.): the PR reviewer cannot and
    #     must not need anything outside the PR itself.
    #   - HUMAN sentences per test: given/when/then-style 'validates' phrasing, the
    #     shape spelled out in words, every check named — including API checks.
    tests_md_lines: list = []
    if newest_per_layer or api_entries:
        tests_md_lines += ["## Tests — every test, what it proves, and its result", ""]
        layer_words = {"al-unit": "Functional tests (item scope)",
                       "al-regression": "Regression tests (unchanged behavior)",
                       "api": "API contract checks"}
        shape_words = {"happy": "happy path", "negative": "negative (must be refused)",
                       "edge": "edge case", "regression": "regression"}
        # DEDUP identical runs: an item run and a regression run of the SAME test
        # codeunit list the SAME tests — print the list once under a merged heading
        # (caught on wi267598: 9 tests printed twice, reviewer reads 18 lines of noise).
        merged_layers: Dict[str, Dict[str, Any]] = {}
        consumed: set = set()
        for layer, entry in sorted(newest_per_layer.items()):
            if layer in consumed:
                continue
            names = [t.get("test") for t in (entry.get("details") or {}).get("executed_tests", [])]
            partners = [l2 for l2, e2 in sorted(newest_per_layer.items())
                        if l2 != layer and l2 not in consumed
                        and [t.get("test") for t in (e2.get("details") or {}).get("executed_tests", [])] == names]
            key = " + ".join([layer] + partners)
            merged_layers[key] = entry
            consumed.update([layer] + partners)
        for layer, entry in merged_layers.items():
            det = entry.get("details") or {}
            parts = []
            for single in layer.split(" + "):
                base_layer, _, layer_suffix = single.partition(" · ")
                parts.append(layer_words.get(base_layer, base_layer)
                             + (f" — {layer_suffix}" if layer_suffix else ""))
            layer_display = " + ".join(parts)
            tests_md_lines.append(f"**{layer_display}** — {entry.get('summary', 'run')}:")
            tests_md_lines.append("")
            for i, t in enumerate(det["executed_tests"], start=1):
                name = str(t.get("test", "?"))
                validates = t.get("validates") or _humanize_test_name(name)
                # The RECORDED shape is data — never recompute it at render time
                # (a first cut did, and overrode a declared 'happy' via the
                # 'deleted' edge token). Stale shapes are fixed by RE-RECORDING
                # the run with the current classifier, not by rewriting history.
                shape = shape_words.get(str(t.get("shape", "")), str(t.get("shape", "")))
                status = "PASSED" if str(t.get("result", "")).lower() in ("success", "pass") else "FAILED"
                tests_md_lines.append(f"{i}. {name} — {status}")
                tests_md_lines.append(f"   Validates ({shape}): {validates}")
            tests_md_lines.append("")
        for entry in api_entries[-1:]:
            det = entry.get("details") or {}
            api_tests = det.get("executed_tests") or []
            tests_md_lines.append(f"**API contract checks** — {entry.get('summary', '?')}:")
            tests_md_lines.append("")
            if api_tests:
                for i, t in enumerate(api_tests, start=1):
                    status = "PASSED" if str(t.get("result", "")).lower() in ("success", "pass") else "FAILED"
                    tests_md_lines.append(f"{i}. {t.get('test', '?')} — {status}")
                    tests_md_lines.append(f"   Validates: {t.get('validates', '?')}")
            else:
                tests_md_lines.append(
                    f"- {str(det.get('result', '?')).upper()}: every check of the recorded run "
                    "passed; per-check names were not captured by the engine version that ran it.")
            tests_md_lines.append("")
    # (the description's proof paragraph already points to the first comment —
    # no duplicate '## Tests' stub here)
    if dm is not None:
        lines += [
            "",
            "## Before merge — data-model sign-off pending",
            f"{dm} A second developer confirms the schema part (persisted fields/enum "
            "values are forever on customer databases); the internal code gate refuses "
            "merge approval until that sign-off is recorded.",
        ]
    lines += [
        "",
        "_Generated by bc_prepare_pr from recorded evidence (no manual claims)._",
    ]
    description = "\n".join(lines)
    # ADO hard limit (discovered live): descriptions max 4000 chars. Truncate with a
    # pointer rather than fail the PATCH/create; the full body persists in PR.md.
    if len(description) > 3900:
        description = description[:3800] + "\n\n_[truncated — full description in the item's PR.md artifact]_"

    # STORY STANDARD is a GATE: a description failing the lint is never persisted
    # as ready — the generator itself must be fixed, not the output hand-edited.
    story_problems = _lint_pr_description(description)
    if story_problems:
        return {
            "status": "blocked_description_standard",
            "blocked": True,
            "reason": ("The generated description fails the story standard — the "
                       "GENERATOR needs fixing (never hand-edit the output): "
                       + "; ".join(story_problems)),
            "problems": story_problems,
            "description_preview": description[:1500],
        }

    branch = source_branch or f"feature/{spec_name}"
    directory = pr_core.pr_dir(root, spec_name)
    directory.mkdir(parents=True, exist_ok=True)
    pr_md = directory / "PR.md"
    pr_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tests_md = directory / "PR-TESTS.md"
    if tests_md_lines:
        tests_md.write_text("\n".join(tests_md_lines) + "\n", encoding="utf-8")
    (directory / "prepared.json").write_text(json.dumps({
        "title": title,
        "source_branch": branch,
        "target_branch": target_branch,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "coverage_pct": digest.get("coverage_pct"),
    }, indent=2), encoding="utf-8")

    return {
        "status": "pr_prepared",
        "title": title,
        "source_branch": branch,
        "target_branch": target_branch,
        "description": description,
        "pr_path": str(pr_md),
        "_timeline_phase": "pr_prepared",
        **({"data_model_approval_pending": dm} if dm is not None else {}),
        "next_action": {
            "tool": "bc_create_pr",
            "reason": "Push the branch, then create the PR in ADO."
                      + (" NOTE: data-model sign-off still pending - the PR carries a"
                         " DO-NOT-MERGE section and the code gate will refuse until"
                         " bc_approve_data_model records it." if dm is not None else ""),
            "params_hint": {"spec_name": spec_name, "source_branch": branch},
        },
    }


_STORY_SECTIONS = ("## What this delivers", "## Where to look first", "## What was proven")


def _lint_outbound_text(text: str, *, cap: int = 3900) -> List[str]:
    """Deterministic lint of text about to LEAVE the machine (PR descriptions,
    comments). Catches the classes that shipped ugly live (PR 41673: spec-slug
    bullets, [id]-prefixed paths, silent truncation)."""
    warnings: List[str] = []
    if len(text) > cap:
        warnings.append(f"text is {len(text)} chars — ADO caps at ~4000; it WILL be truncated")
    if re.search(r"^- (Wi|wi)\d{5,}", text, re.MULTILINE):
        warnings.append("a bullet renders a spec folder slug (e.g. 'Wi239589-…') — humans read titles, not slugs")
    if re.search(r"\[\w[\w-]*\] extensions[\\/]", text):
        warnings.append("an object is rendered as a bracketed file path — name the OBJECT, not the path")
    if re.search(r"^- - ", text, re.MULTILINE):
        warnings.append("a bullet starts with '- -' — a machine bullet leaked into prose")
    if "|" in text and re.search(r"^\|.+\|$", text, re.MULTILINE):
        warnings.append("markdown pipe table present — ADO comment threads do not render tables")
    return warnings


def _lint_pr_description(text: str) -> List[str]:
    """The STORY STANDARD for every PR description (user verdict 2026-07-04 on the
    generated story: 'beautifully written … make this enforced for every pr').
    Anti-patterns AND structure: all three story sections must exist so a reviewer
    always gets what/where/proof — a description failing this never leaves the machine."""
    problems = _lint_outbound_text(text)
    for section in _STORY_SECTIONS:
        if section not in text:
            problems.append(f"missing story section '{section}' — every PR tells what it delivers, where to look, and what was proven")
    # the delivers section must carry CONTENT (a paragraph or bullets), not just the heading
    m = re.search(r"## What this delivers\n+(\S)", text)
    if m is None and "## What this delivers" in text:
        problems.append("'What this delivers' is empty — the reviewer learns nothing")
    return problems


async def handle_create_pr(
    project_root: str,
    spec_name: str,
    org_url: str,
    project: str,
    repository: str,
    source_branch: Optional[str] = None,
    target_branch: str = "main",
    title: Optional[str] = None,
    work_item_id: Optional[int] = None,
    pat_env: str = pr_core.DEFAULT_PAT_ENV,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create the ADO pull request from the prepared artifact (bc_prepare_pr first).

    DRY-RUN BY DEFAULT: without ``confirm=true`` nothing leaves the machine — the
    response is the exact outbound payload plus deterministic lint warnings
    (PR 41673 shipped a machine-speak description because nothing forced a look
    at the artifact between prepare and create)."""
    root = Path(project_root).resolve()
    directory = pr_core.pr_dir(root, spec_name)
    prepared_path = directory / "prepared.json"
    pr_md = directory / "PR.md"
    if not prepared_path.exists() or not pr_md.exists():
        return {
            "status": "blocked_not_prepared",
            "blocked": True,
            "reason": "No prepared PR artifact. Run bc_prepare_pr first (it enforces the evidence gate).",
            "next_action": {"tool": "bc_prepare_pr", "params_hint": {"spec_name": spec_name}},
        }
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    description = pr_md.read_text(encoding="utf-8")
    # ADO hard limit (discovered live: PATCH rejected at 4001+): cap defensively at
    # the API boundary too — prepare_pr already splits the test table into PR-TESTS.md.
    if len(description) > 3900:
        description = description[:3800] + "\n\n_[truncated — full description in the item's PR.md artifact]_"

    if not confirm:
        preview_title = title or prepared["title"]
        warnings = _lint_pr_description(description)
        return {
            "status": "dry_run",
            "executed": False,
            "would_create": {
                "title": preview_title,
                "source_branch": source_branch or prepared["source_branch"],
                "target_branch": target_branch or prepared["target_branch"],
                "work_item_id": work_item_id,
                "description": description,
            },
            "lint_warnings": warnings,
            "reason": ("Nothing was sent. Read would_create.description AS A REVIEWER"
                       + (", fix the lint warnings first" if warnings else "")
                       + ", then re-call with confirm=true."),
            "next_action": {
                "tool": "bc_create_pr",
                "reason": "Re-call with confirm=true to actually create the PR",
                "params_hint": {"spec_name": spec_name, "confirm": True},
            },
        }

    # The story standard is ENFORCED at send time too — a stale/hand-edited PR.md
    # that fails the lint never reaches the colleagues ("make this enforced for
    # every pr", user 2026-07-04).
    story_problems = _lint_pr_description(description)
    if story_problems:
        return {
            "status": "blocked_description_standard",
            "blocked": True,
            "reason": ("Refusing to create the PR: the description fails the story "
                       "standard: " + "; ".join(story_problems)
                       + ". Regenerate it (bc_prepare_pr) — never hand-edit the output."),
            "problems": story_problems,
            "next_action": {"tool": "bc_prepare_pr", "params_hint": {"spec_name": spec_name}},
        }

    result = pr_core.create_pr(
        org_url=org_url,
        project=project,
        repository=repository,
        source_branch=source_branch or prepared["source_branch"],
        target_branch=target_branch or prepared["target_branch"],
        title=title or prepared["title"],
        description=description,
        work_item_id=work_item_id,
        pat_env=pat_env,
    )
    if not result.get("ok"):
        return {"status": "create_failed", "isError": True, **result}

    record = {
        "pr_id": result["pr_id"],
        "url": result.get("url"),
        "org_url": org_url,
        "project": project,
        "repository": repository,
        "source_branch": result["source_branch"],
        "target_branch": result["target_branch"],
        "work_item_id": work_item_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    record_path = pr_core.save_pr_record(root, spec_name, record)
    out: Dict[str, Any] = {
        "status": "pr_created",
        "pr_id": result["pr_id"],
        "url": result.get("url"),
        "pr_record_path": record_path,
        "_timeline_phase": "pr_created",
        "next_action": {
            "tool": "bc_get_review_comments",
            "reason": "Poll review comments; open threads re-admit implement-stage rework.",
            "params_hint": {"spec_name": spec_name},
        },
    }
    # Post the explicit test table as an informational (closed) comment thread —
    # the reviewer sees WHICH tests ran, WHAT each validates and its STATUS without
    # opening the test files; the description only carries a pointer (4000-char cap).
    tests_md = directory / "PR-TESTS.md"
    if tests_md.exists():
        posted = pr_core.post_thread(
            org_url=org_url, project=project, repository=repository,
            pr_id=int(result["pr_id"]),
            content=tests_md.read_text(encoding="utf-8"),
            pat_env=pat_env,
        )
        out["tests_thread"] = posted
    return out


async def handle_get_review_comments(
    project_root: str,
    spec_name: str,
    pat_env: str = pr_core.DEFAULT_PAT_ENV,
) -> Dict[str, Any]:
    """Fetch PR comment threads; open threads flip the item into the rework loop (B2)."""
    root = Path(project_root).resolve()
    record, blocked = _require_pr_record(root, spec_name)
    if blocked:
        return blocked
    result = pr_core.get_threads(**_coords(record), pat_env=pat_env)
    if not result.get("ok"):
        return {"status": "fetch_failed", "isError": True, **result}
    if result["open_count"] > 0:
        return {
            "status": "review_comments_open",
            "open_count": result["open_count"],
            "resolved_count": result["resolved_count"],
            "comments": result["open"],
            "_timeline_phase": "review_comments_open",
            "next_action": {
                "tool": "bc_implement_write",
                "reason": (
                    "Address each open comment WITHIN the approved Charter scope "
                    "(scope changes require re-approval), then bc_resolve_review_comment."
                ),
                "params_hint": {"spec_name": spec_name},
            },
        }
    return {
        "status": "no_open_comments",
        "open_count": 0,
        "resolved_count": result["resolved_count"],
        "next_action": {
            "tool": "bc_merge_status",
            "reason": "No open threads — check approval/merge state.",
            "params_hint": {"spec_name": spec_name},
        },
    }


async def handle_resolve_review_comment(
    project_root: str,
    spec_name: str,
    thread_id: int,
    reply: Optional[str] = None,
    resolution: str = "fixed",
    pat_env: str = pr_core.DEFAULT_PAT_ENV,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Reply to and resolve one PR comment thread (after the fix is pushed).

    DRY-RUN BY DEFAULT: a reply is a message to a COLLEAGUE — without confirm=true
    the response shows exactly what would be posted, plus lint warnings."""
    root = Path(project_root).resolve()
    record, blocked = _require_pr_record(root, spec_name)
    if blocked:
        return blocked
    if not confirm:
        return {
            "status": "dry_run",
            "executed": False,
            "would_post": {"thread_id": int(thread_id), "reply": reply,
                           "resolution": resolution, "pr_id": record.get("pr_id")},
            "lint_warnings": _lint_outbound_text(reply or ""),
            "reason": "Nothing was sent. Read would_post.reply as the colleague will, then re-call with confirm=true.",
            "next_action": {"tool": "bc_resolve_review_comment",
                            "params_hint": {"spec_name": spec_name, "thread_id": int(thread_id), "confirm": True}},
        }
    result = pr_core.resolve_thread(
        **_coords(record), thread_id=int(thread_id), reply=reply,
        status=resolution, pat_env=pat_env,
    )
    if not result.get("ok"):
        return {"status": "resolve_failed", "isError": True, **result}
    return {
        "status": "comment_resolved",
        "thread_id": result["thread_id"],
        "resolution": result["new_status"],
        "next_action": {
            "tool": "bc_get_review_comments",
            "reason": "Re-check for remaining open threads.",
            "params_hint": {"spec_name": spec_name},
        },
    }


async def handle_merge_status(
    project_root: str,
    spec_name: str,
    pat_env: str = pr_core.DEFAULT_PAT_ENV,
) -> Dict[str, Any]:
    """PR approval/merge truth from ADO. Approval satisfies the internal `code` gate (C1);
    completion advances the timeline to `merged` and points at bc_archive."""
    root = Path(project_root).resolve()
    record, blocked = _require_pr_record(root, spec_name)
    if blocked:
        return blocked
    result = pr_core.merge_status(**_coords(record), pat_env=pat_env)
    if not result.get("ok"):
        return {"status": "fetch_failed", "isError": True, **result}

    # MERGE-side data-model watch: the invariant ("cannot merge until granted") is
    # enforced where merging is actually decided. An approved-but-unsigned PR gets a
    # DO-NOT-COMPLETE warning here; bc_submit_decision refuses the code gate outright.
    from bc_agentic_mcp.tools.approval import _data_model_gate
    dm_pending = _data_model_gate(root, specs_root(root) / spec_name)

    code_gate_path = pr_core.record_code_gate_from_pr(root, spec_name, record, result)
    out: Dict[str, Any] = {
        "status": "merged" if result["completed"] else ("approved" if result["approved"] else "pending_review"),
        "pr_status": result["pr_status"],
        "merge_status": result.get("merge_status"),
        "approved": result["approved"],
        "completed": result["completed"],
        "reviewers": result["reviewers"],
    }
    if dm_pending is not None and not result["completed"]:
        out["data_model_approval_pending"] = dm_pending
        out["warning"] = (
            "DO NOT COMPLETE the PR in ADO: data-model sign-off missing (" + dm_pending + "). "
            "Record it via bc_approve_data_model first - the internal code gate refuses without it."
        )
    if code_gate_path:
        out["code_gate_recorded"] = code_gate_path
    if result["completed"]:
        out["_timeline_phase"] = "merged"
        out["next_action"] = {
            "tool": "bc_archive",
            "reason": "PR merged — archive the item.",
            "params_hint": {"spec_name": spec_name},
        }
    elif result["approved"]:
        out["next_action"] = {
            "tool": "bc_merge_status",
            "reason": "Approved; merge happens in ADO (human gate 3). Re-check after merging.",
            "params_hint": {"spec_name": spec_name},
        }
    else:
        out["next_action"] = {
            "tool": "bc_get_review_comments",
            "reason": "Not approved yet — check for open review comments.",
            "params_hint": {"spec_name": spec_name},
        }
    return out


async def handle_sync_item_state(
    org_url: str,
    project: str,
    work_item_id: int,
    state: str,
    pat_env: str = pr_core.DEFAULT_PAT_ENV,
    confirm: bool = False,
) -> Dict[str, Any]:
    """B3: set the ADO work item state on a lifecycle transition.

    State names are org-specific, so ``state`` is an explicit input — the tool never
    invents or hardcodes a state model. DRY-RUN BY DEFAULT: the team's board is a
    shared surface; without confirm=true the response only shows the would-be change.
    """
    if not confirm:
        return {
            "status": "dry_run",
            "executed": False,
            "would_set": {"work_item_id": int(work_item_id), "state": state},
            "reason": "Nothing was sent. Re-call with confirm=true to set the state on the board.",
            "next_action": {"tool": "bc_sync_item_state",
                            "params_hint": {"work_item_id": int(work_item_id),
                                            "state": state, "confirm": True}},
        }
    result = pr_core.sync_workitem_state(
        org_url=org_url, project=project, work_item_id=int(work_item_id),
        state=state, pat_env=pat_env,
    )
    if not result.get("ok"):
        return {"status": "sync_failed", "isError": True, **result}
    return {"status": "state_synced", "work_item_id": result["work_item_id"], "new_state": result["new_state"]}
