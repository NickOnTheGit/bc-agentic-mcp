"""Feature-tier tool handlers (H): bc_capture_feature, bc_refine_feature,
bc_plan_feature, bc_feature_status.

Cycle: capture (ADO claims, fresh) -> refine (claims CONFRONTED with code reality:
mismatches, redundancies, conflicts, guideline flags) -> plan (facts + wave judgment)
-> human gate F1 -> per-item dispatch. The feature folder is an ordinary spec folder —
checkpoints, TIMELINE.md, ITEM.md and the C1 `plan` approval gate work on it unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from bc_agentic_mcp import code_pack, feature_context, feature_plan, feature_refine, object_index, timeline
from bc_agentic_mcp.workspace import specs_root


def _seed_keys(claims: list) -> list:
    """Canonical graph keys of every table the claims touch (the verified targets)."""
    seeds = []
    for c in claims:
        for table in c["tables"]:
            key = f"table {table['number']}"
            if key not in seeds:
                seeds.append(key)
    return seeds


async def handle_capture_feature(
    project_root: str,
    spec_name: str,
    work_item_id: str,
    org_url: str,
    project: str,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
) -> Dict[str, Any]:
    """Capture the WHOLE feature tree fresh (the id may be the feature or any child)."""
    result = feature_context.capture_feature(
        project_root, spec_name,
        work_item_id=str(work_item_id), org_url=org_url, project=project, pat_env=pat_env,
    )
    if not result.get("captured"):
        return {"status": "capture_failed", "isError": True, **result}
    return {
        "status": "feature_captured",
        **result,
        "next_action": {
            "tool": "bc_refine_feature",
            "reason": "Confront every PBI claim with code reality BEFORE planning — "
                      "mismatches, redundancies, conflicts, guideline flags.",
            "params_hint": {"spec_name": spec_name},
        },
    }


def handle_refine_feature(
    project_root: str,
    spec_name: str,
    critique: Optional[str] = None,
) -> Dict[str, Any]:
    """H4: heuristic+empiric refinement — claims x code reality, deterministically.

    SYNC on purpose: the repo walk is blocking I/O, so _run_tool offloads it to a
    worker thread (timeout enforceable). Uses the persistent incremental object index
    (.specs/.index/objects.json) — first build walks once, refreshes are stat-only.
    """
    tree = feature_context.load_tree(project_root, spec_name)
    if tree is None:
        return {
            "status": "blocked_no_capture",
            "blocked": True,
            "reason": "No captured feature tree — run bc_capture_feature first.",
            "next_action": {"tool": "bc_capture_feature", "params_hint": {"spec_name": spec_name}},
        }
    refreshed = object_index.refresh(Path(project_root).resolve())
    index = refreshed["objects"]
    claims = [
        feature_refine.extract_claims(str(c["id"]), f"{c.get('title', '')}\n{c.get('description', '')}")
        for c in tree.get("children", [])
        if str(c.get("state", "")).lower() != "removed"
    ]
    findings = feature_refine.cross_check(claims, index)
    pack = code_pack.ranked_pack(refreshed, _seed_keys(claims))
    pack_path = code_pack.persist_pack(specs_root(Path(project_root).resolve()) / spec_name, pack)
    paths = feature_refine.persist(project_root, spec_name, tree["feature"], claims, findings, critique)
    blocking = findings["counts"]["mismatches"] + findings["counts"]["conflicts"]
    return {
        "status": "feature_refined",
        "index_stats": refreshed["stats"],
        **findings["counts"],
        "mismatches_detail": findings["mismatches"],
        "redundancies_detail": findings["redundancies"],
        "conflicts_detail": findings["conflicts"],
        "guideline_flags_detail": findings["guideline_flags"],
        "empiric_required": findings["empiric_required"],
        "context_pack": {"path": pack_path,
                         "objects": [{"object": s["object"], "role": s["role"], "score": s["score"]}
                                     for s in pack["sections"]],
                         "chars": pack["chars"], "graph_edges": pack["graph_edges"]},
        "refinement_path": paths["refinement_md"],
        "next_action": {
            "tool": "bc_plan_feature",
            "reason": (
                "Findings demand resolution in the plan narrative before gate F1."
                if blocking else "Claims verified — produce the plan (facts + waves)."
            ),
            "params_hint": {"spec_name": spec_name},
        },
    }


async def handle_plan_feature(
    project_root: str,
    spec_name: str,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Deterministic cross-item analysis + optional model narrative -> FEATURE-PLAN.md."""
    tree = feature_context.load_tree(project_root, spec_name)
    if tree is None:
        return {
            "status": "blocked_no_capture",
            "blocked": True,
            "reason": "No captured feature tree — run bc_capture_feature first.",
            "next_action": {"tool": "bc_capture_feature", "params_hint": {"spec_name": spec_name}},
        }
    analysis = feature_plan.analyze(tree)
    paths = feature_plan.persist(project_root, spec_name, tree["feature"], analysis, notes)
    result: Dict[str, Any] = {
        "status": "feature_planned",
        "item_count": analysis["item_count"],
        "excluded": analysis["excluded"],
        "shared_objects": analysis["shared_objects"],
        "collision_warnings": analysis["collision_warnings"],
        "suggested_order": analysis["suggested_order"],
        "plan_path": paths["plan_md"],
        "plan_json_path": paths["plan_json"],
        "next_action": {
            "tool": "bc_request_approval",
            "reason": "Human gate F1: approve the feature plan (C1 `plan` gate on this folder).",
            "params_hint": {"spec_name": spec_name, "phase": "plan",
                            "artifact_path": paths["plan_md"]},
        },
    }
    # Refinement findings (H4) are part of what the human approves: surface unresolved
    # mismatches/conflicts loudly; a plan over unrefined claims says so explicitly.
    refinement_path = specs_root(Path(project_root).resolve()) / spec_name / "feature_refinement.json"
    if refinement_path.exists():
        try:
            counts = json.loads(refinement_path.read_text(encoding="utf-8"))["findings"]["counts"]
            result["refinement"] = counts
            if counts.get("mismatches") or counts.get("conflicts"):
                result["warning"] = (
                    f"Refinement found {counts.get('mismatches', 0)} mismatch(es) and "
                    f"{counts.get('conflicts', 0)} conflict(s) — resolve or explicitly "
                    "accept them in the plan narrative before approval."
                )
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    else:
        result["warning"] = (
            "No refinement recorded — this plan is built on UNVERIFIED claims. "
            "Run bc_refine_feature for the code-reality confrontation."
        )
    return result


def handle_refine_item(
    project_root: str,
    spec_name: str,
    critique: Optional[str] = None,
) -> Dict[str, Any]:
    """PBI-level twin of bc_refine_feature: THIS item's claims x code reality.

    Reads the item's captured context (description + comments), confronts it with the
    cached object index, persists ITEM-REFINEMENT.md into the item's spec folder.
    SYNC on purpose (blocking I/O -> worker thread, timeout enforceable).
    """
    root = Path(project_root).resolve()
    cdir = specs_root(root) / spec_name / "context"
    manifest_path = cdir / "manifest.json"
    if not manifest_path.exists():
        return {
            "status": "blocked_no_capture",
            "blocked": True,
            "reason": "No captured item context — run bc_capture_item_context first.",
            "next_action": {"tool": "bc_capture_item_context",
                            "params_hint": {"spec_name": spec_name}},
        }
    try:
        item_id = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("item_id", ""))
    except (OSError, json.JSONDecodeError):
        item_id = ""
    text_parts = []
    for name in (f"item-{item_id}.md", f"comments-{item_id}.md"):
        path = cdir / name
        if path.exists():
            text_parts.append(path.read_text(encoding="utf-8", errors="replace"))
    if not text_parts:
        return {"status": "blocked_no_capture", "blocked": True,
                "reason": f"Captured context for item {item_id or spec_name} has no description file.",
                "next_action": {"tool": "bc_capture_item_context",
                                "params_hint": {"spec_name": spec_name}}}

    refreshed = object_index.refresh(root)
    claims = [feature_refine.extract_claims(item_id or spec_name, "\n".join(text_parts))]
    findings = feature_refine.cross_check(claims, refreshed["objects"])
    pack = code_pack.ranked_pack(refreshed, _seed_keys(claims))
    pack_path = code_pack.persist_pack(specs_root(root) / spec_name, pack)
    paths = feature_refine.persist(
        project_root, spec_name,
        {"id": item_id or spec_name, "title": spec_name},
        claims, findings, critique,
        md_name="ITEM-REFINEMENT.md", json_name="item_refinement.json",
        title_prefix="Item Refinement",
    )
    blocking = findings["counts"]["mismatches"] + findings["counts"]["conflicts"]
    return {
        "status": "item_refined",
        "index_stats": refreshed["stats"],
        **findings["counts"],
        "mismatches_detail": findings["mismatches"],
        "redundancies_detail": findings["redundancies"],
        "guideline_flags_detail": findings["guideline_flags"],
        "empiric_required": findings["empiric_required"],
        "refinement_path": paths["refinement_md"],
        # Ranked, signature-rendered code context (personalized PageRank from the
        # verified targets) — persisted where code-context grounding picks it up.
        "context_pack": {"path": pack_path,
                         "objects": [{"object": s["object"], "role": s["role"], "score": s["score"]}
                                     for s in pack["sections"]],
                         "chars": pack["chars"], "graph_edges": pack["graph_edges"]},
        "next_action": {
            "tool": "bc_read_code_context" if not blocking else "bc_checkpoint",
            "reason": (
                "Claims verified — ground the code context on the ranked pack (context_pack.md)."
                if not blocking else
                f"{blocking} mismatch(es)/conflict(s): record the correction decision "
                "(bc_checkpoint kind=correction), fix the spec inputs, then re-refine."
            ),
            "params_hint": (
                {"spec_name": spec_name,
                 "objects": [{"name": objects_entry["object"].split(" ", 2)[-1],
                              "kind": objects_entry["object"].split(" ")[0]}
                             for objects_entry in
                             ({"object": s["object"]} for s in pack["sections"] if s["role"] == "target")]}
                if not blocking else {"spec_name": spec_name}
            ),
        },
    }


def handle_repo_map(
    project_root: str,
    query: str = "",
    kind: Optional[str] = None,
    limit: int = 20,
    object_key: Optional[str] = None,
    max_age_seconds: int = 60,
    free_id_range: Optional[str] = None,
) -> Dict[str, Any]:
    """bc_repo_map — the repo's table of contents, answered from the persistent index.

    Modes: search (query/kind -> matching objects with captions + sizes), detail
    (object_key like 'table 11024121' -> full signature), or FREE-ID allocation
    (free_id_range='66000-69000' + kind -> first free ids, CONTENT-parsed via the
    index so misspelled filenames can never hide a taken id — observed live:
    ProjectCalcBudgetService.Codeuit.al hid codeunit 66071 from a filename scan).
    Read-only browsing rides the TTL fast-path (default 60s); pass
    max_age_seconds=0 to force filesystem reconciliation.
    """
    refreshed = object_index.refresh(Path(project_root).resolve(),
                                     max_age_seconds=max_age_seconds)
    objects = refreshed["objects"]
    if free_id_range:
        try:
            lo, hi = (int(x) for x in str(free_id_range).replace(" ", "").split("-", 1))
        except ValueError:
            return {"status": "error", "reason": "free_id_range must look like '66000-69000'"}
        want_kind = (kind or "codeunit").lower()
        used = {int(o["number"]) for o in objects.values()
                if o.get("kind") == want_kind and str(o.get("number", "")).isdigit()
                and lo <= int(o["number"]) <= hi}
        free = [n for n in range(lo, hi + 1) if n not in used][:max(1, limit)]
        return {"status": "free_ids", "kind": want_kind, "range": f"{lo}-{hi}",
                "used_count": len(used), "free": free}
    if object_key:
        entry = objects.get(object_key.strip().lower()) or objects.get(object_key.strip())
        if entry is None:
            return {"status": "not_found", "reason": f"no object '{object_key}' in the index",
                    "index_stats": refreshed["stats"]}
        return {"status": "repo_map", "index_stats": refreshed["stats"],
                "object": f"{entry['kind']} {entry['number']} {entry['name']}",
                "file": entry.get("rel", entry.get("file", "")),
                "signature": code_pack.render_signature(entry)}
    rows = object_index.toc_search(objects, query, kind=kind, limit=limit)
    return {"status": "repo_map", "index_stats": refreshed["stats"],
            "matches": rows, "match_count": len(rows)}


def _find_item_spec(root: Path, child_id: str) -> Optional[str]:
    """Locate the per-item spec folder for a child by its captured manifest item_id."""
    base = specs_root(root)
    if not base.is_dir():
        return None
    for spec_dir in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        manifest = spec_dir / "context" / "manifest.json"
        if manifest.exists():
            try:
                if str(json.loads(manifest.read_text(encoding="utf-8")).get("item_id")) == str(child_id):
                    return spec_dir.name
            except (OSError, json.JSONDecodeError):
                continue
    return None


def feature_children_specs(root: Path, spec_name: str) -> List[Dict[str, Any]]:
    """(child_id, title, state, item_spec) for every live child of the feature."""
    tree = feature_context.load_tree(str(root), spec_name)
    if tree is None:
        return []
    out = []
    for child in tree.get("children", []):
        if str(child.get("state", "")).lower() == "removed":
            continue
        cid = str(child["id"])
        out.append({"id": cid, "title": child.get("title", ""),
                    "state": child.get("state", ""),
                    "item_spec": _find_item_spec(root, cid)})
    return out


def _bucket_of(test: Dict[str, Any]) -> str:
    """Happy/negative/edge bucket of ONE planned acceptance test.

    Structured path_shape wins; statement-token classification is the fallback."""
    shape = str(test.get("path_shape", "")).lower()
    if shape in ("happy", "negative", "edge", "regression"):
        return shape
    from bc_agentic_mcp import verification as _verif
    counts = _verif.classify_test_paths([str(test.get("statement", ""))])
    for bucket in ("edge", "negative", "happy"):
        if counts.get(bucket):
            return bucket
    return "happy"


def _decision_log(root: Path, spec_name: str, children: List[Dict[str, Any]]) -> str:
    """Machine-recorded decisions, assembled for humans.

    Two deterministic sources:
    - feature-folder checkpoints of kinds decision / scope_change / override / correction
      (each was recorded the moment the decision was made);
    - per-item refinement corrections (ticket claims that LOST against code reality —
      concrete examples of why the spec deviates from the ticket text).
    """
    from bc_agentic_mcp import checkpoints as _ckpt
    lines: List[str] = []
    try:
        cps = _ckpt.load_checkpoints(root, spec_name)
    except Exception:
        cps = []
    picked = [c for c in cps if c.get("kind") in
              ("decision", "scope_change", "override", "correction")]
    if picked:
        lines.append("**Recorded while working** (checkpoint log):")
        for c in picked:
            ts = str(c.get("ts", ""))[:10]
            lines.append(f"- [{c.get('kind')}] {ts}: {str(c.get('summary', '')).strip()}")
        lines.append("")
    corrections: List[str] = []
    for child in children:
        item_spec = child.get("item_spec")
        if not item_spec:
            continue
        ref_path = specs_root(root) / item_spec / "item_refinement.json"
        try:
            findings = json.loads(ref_path.read_text(encoding="utf-8")).get("findings", {})
        except (OSError, json.JSONDecodeError):
            continue
        for m in findings.get("mismatches", []) or []:
            corrections.append(f"- {m} — the SPEC follows the code, not the ticket.")
    if corrections:
        lines.append("**Ticket claims that lost against code reality** "
                     "(each was verified in source before the spec was written):")
        lines.extend(corrections)
    return "\n".join(lines) if lines else "(no recorded decisions)"


def handle_prepare_feature_review(
    project_root: str,
    spec_name: str,
    decisions: str = "",
) -> Dict[str, Any]:
    """ONE mega review packet for the WHOLE feature — the single human plan gate.

    Model (per human decision 2026-07-04): items are authored back-to-back with NO
    per-item human gate; this packet aggregates every child's spec/design/tasks so the
    human reviews the feature ONCE, approves ONCE (feature-level bc_submit_decision
    cascades plan approval to every child), then all items are implemented on the
    feature branch, installed on the container ONCE, tested together, and merged via
    ONE feature->master PR (item commits preserved for navigable review).

    Children in a terminal ADO state with no repo work (e.g. Done design items) and
    children delivered OUTSIDE this pipeline are listed, never blocking.
    """
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
    plan_path = specs_dir / "feature_plan.json"
    if not plan_path.exists():
        return {"status": "blocked_no_plan", "blocked": True,
                "reason": "No feature plan — run bc_plan_feature first.",
                "next_action": {"tool": "bc_plan_feature", "params_hint": {"spec_name": spec_name}}}
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "message": f"feature_plan.json unreadable: {exc}"}

    children = feature_children_specs(root, spec_name)
    sections: List[str] = []
    incomplete: List[str] = []
    test_matrix: List[Dict[str, Any]] = []
    test_gaps: List[str] = []
    covered = 0
    for child in children:
        cid, title, item_spec = child["id"], child["title"], child["item_spec"]
        if item_spec is None:
            if str(child.get("state", "")).lower() in ("done", "closed"):
                sections.append(f"## {cid} — {title}\n\n- Already **{child['state']}** in ADO; no repo work in this pipeline.\n")
            else:
                incomplete.append(f"{cid} ({title[:60]}): no item spec authored yet")
            continue
        item_dir = specs_root(root) / item_spec
        spec_file = item_dir / "spec.json"
        design_file = item_dir / "DESIGN.md"
        tasks_file = item_dir / "TASKS.md"
        missing = [n for n, p in (("spec.json", spec_file), ("DESIGN.md", design_file),
                                  ("TASKS.md", tasks_file)) if not p.exists()]
        if missing:
            incomplete.append(f"{cid} ({item_spec}): missing {', '.join(missing)}")
            continue
        covered += 1
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            spec = {}
        reqs = spec.get("requirements") or []
        files = (spec.get("scope_boundaries") or {}).get("allowed_files") or []
        req_lines = "\n".join(
            f"  - {r.get('id', '?')}: {str(r.get('statement', ''))[:160]}" for r in reqs)
        file_lines = "\n".join(f"  - {f}" for f in files)
        design_head = "\n".join(design_file.read_text(encoding="utf-8", errors="replace").splitlines()[:12])
        task_count = sum(1 for line in tasks_file.read_text(encoding="utf-8", errors="replace").splitlines()
                         if line.lstrip().startswith("- ["))

        # PLANNED TEST MATRIX (human demand 2026-07-04: the packet must show ALL tests
        # for ALL items — happy / negative / edge / regression — before the ONE approval).
        # Structured path_shape (extract_declared_tests) is authoritative; statement-token
        # classification is the fallback for specs authored before the declaration template.
        from bc_agentic_mcp import verification as _verif
        planned = spec.get("acceptance_tests") or []
        shaped = [t for t in planned if t.get("path_shape")]
        regression_tests = [t for t in planned
                            if str(t.get("path_shape", "")).lower() == "regression"
                            or (not t.get("path_shape") and (
                                "regression" in (str(t.get("id", "")) + str(t.get("statement", ""))).lower()
                                or "pre-fix" in str(t.get("statement", "")).lower()))]
        path_tests = [t for t in planned if t not in regression_tests]
        buckets = {"happy": 0, "negative": 0, "edge": 0}
        unshaped_statements = []
        for t in path_tests:
            shape = str(t.get("path_shape", "")).lower()
            if shape in buckets:
                buckets[shape] += 1
            else:
                unshaped_statements.append(str(t.get("statement", "")))
        for bucket, count in _verif.classify_test_paths(unshaped_statements).items():
            buckets[bucket] += count
        row = {"id": cid, "item_spec": item_spec, **buckets,
               "regression": len(regression_tests), "total": len(planned)}
        # Items already PROVEN by executed container evidence (validation classes green)
        # owe no planned-statement quota — the machine already holds stronger proof.
        item_gaps = [b for b in ("happy", "negative", "edge") if buckets.get(b, 0) == 0]
        if not regression_tests:
            item_gaps.append("regression")
        if item_gaps:
            # Items already PROVEN by executed container evidence owe no planned quota —
            # read the PERSISTED verdict (bc_verify writes verification.json). NEVER
            # recompute verification.gate here: a cold gate rebuilt the object index
            # (12k files, ~305s) and blew the tool timeout (observed live 2026-07-04).
            try:
                verdict = json.loads((specs_root(root) / item_spec / "verification.json")
                                     .read_text(encoding="utf-8"))
                classes = verdict.get("validation_classes") or {}
                proven = (classes.get("path-coverage", {}).get("ok")
                          and classes.get("regression", {}).get("ok"))
            except (OSError, json.JSONDecodeError):
                proven = False
            if proven:
                row["proven_by_execution"] = True
                item_gaps = []
        for gap in item_gaps:
            test_gaps.append(f"{cid} ({item_spec}): no planned {gap} test")
        test_matrix.append(row)

        test_lines = "\n".join(
            f"  - [{'regression' if t in regression_tests else _bucket_of(t)}] "
            f"{t.get('id', '?')}: {str(t.get('statement', ''))[:180]}"
            for t in planned) or "  - (none planned yet)"
        sections.append(
            f"## {cid} — {title}\n\n"
            f"- Item spec: `{item_spec}` | requirements: {len(reqs)} | tasks: {task_count}\n"
            f"- Requirements:\n{req_lines}\n"
            f"- Files in scope:\n{file_lines}\n"
            f"- Planned tests (happy {buckets['happy']} / negative {buckets['negative']} / "
            f"edge {buckets['edge']} / regression {len(regression_tests)}"
            + (" — PROVEN by executed container evidence" if row.get("proven_by_execution") else "")
            + f"):\n{test_lines}\n"
            f"- Design (head):\n\n```\n{design_head}\n```\n"
        )

    analysis = plan.get("analysis") or {}
    warnings = analysis.get("collision_warnings") or []
    order = [str(e.get("id")) for e in (analysis.get("suggested_order") or [])]
    notes = plan.get("notes", "")
    feature = plan.get("feature") or {}

    # FEATURE SCHEMA (human ask 2026-07-04): one rendered picture of the whole feature —
    # delivery order chain, cross-item mentions, and which items share which objects.
    live_ids = {c["id"] for c in children}
    titles = {c["id"]: str(c.get("title", "")).replace('"', "'")[:46] for c in children}
    schema: List[str] = ["```mermaid", "graph TD"]
    chain = [cid for cid in order if cid in live_ids]
    for cid in chain:
        schema.append(f'    i{cid}["{cid}<br/>{titles.get(cid, "")}"]')
    for a, b in zip(chain, chain[1:]):
        schema.append(f"    i{a} ==>|then| i{b}")
    for edge in analysis.get("mention_edges") or []:
        f_id, t_id = str(edge.get("from")), str(edge.get("to"))
        if f_id in live_ids and t_id in live_ids:
            schema.append(f"    i{f_id} -.->|mentions| i{t_id}")
    shared = analysis.get("shared_objects") or {}
    shared_items = (sorted(shared.items(), key=lambda kv: -len(kv[1]))[:6]
                    if isinstance(shared, dict) else [])
    for idx, (obj_name, item_ids) in enumerate(shared_items):
        safe = str(obj_name).replace('"', "'")
        schema.append(f'    s{idx}{{{{"{safe}"}}}}')
        for cid in item_ids:
            if str(cid) in live_ids:
                schema.append(f"    i{cid} ---|touches| s{idx}")
    schema.append("```")
    feature_schema = "\n".join(schema)

    matrix_lines = "\n".join(
        f"| {r['id']} | {r['item_spec']} | {r['happy']} | {r['negative']} | {r['edge']} "
        f"| {r['regression']} |"
        + (" PROVEN by executed evidence" if r.get("proven_by_execution") else "")
        for r in test_matrix) or "| (no authored items) | | | | | |"
    gaps_block = ("\n".join(f"- MISSING: {g}" for g in test_gaps)
                  if test_gaps else "(every item plans all four buckets)")
    decisions_block = (
        "## Decisions, in plain language\n\n"
        + (decisions.strip() + "\n\n" if decisions.strip() else "")
        + _decision_log(root, spec_name, children) + "\n\n"
    )
    review_md = (
        f"# FEATURE REVIEW — {feature.get('id')} {feature.get('title', spec_name)}\n\n"
        f"ONE approval on this packet approves the plan of EVERY item below "
        f"(no per-item plan gates). After approval: implement all items on the feature "
        f"branch, ONE container install, ALL test codeunits together, ONE feature PR.\n\n"
        f"## Delivery order\n\n{' -> '.join(order) or '(plan order)'}\n\n"
        f"## Feature schema (items, order, shared objects)\n\n{feature_schema}\n\n"
        + decisions_block +
        f"## Test matrix (planned, per item)\n\n"
        f"| Item | Spec | Happy | Negative | Edge | Regression |\n"
        f"|---|---|---|---|---|---|\n{matrix_lines}\n\n"
        f"### Test gaps\n\n{gaps_block}\n\n"
        f"### Execution contract (enforced at the code gate, per item)\n\n"
        f"- heuristic — bc_quality_check clean on changed files\n"
        f"- empiric-item — the item's test codeunit executed IN the container "
        f"(install-once/test-by-slice on the integrated feature branch)\n"
        f"- regression — a regression validation run recorded\n"
        f"- path-coverage — executed tests must count happy>0 AND negative>0 AND edge>0\n"
        f"- api-contract — only when the item exposes an API\n\n"
        f"## Cross-item decisions / wave narrative\n\n{notes or '(none recorded)'}\n\n"
        f"## Shared-object collision warnings\n\n"
        + ("\n".join(f"- {w}" for w in warnings) if warnings else "(none)")
        + "\n\n" + "\n".join(sections)
    )
    review_path = specs_dir / "FEATURE-REVIEW.md"
    review_path.write_text(review_md, encoding="utf-8")

    ready = not incomplete and not test_gaps
    result: Dict[str, Any] = {
        "status": "feature_review_ready" if ready else "blocked_items_incomplete",
        "review_path": str(review_path),
        "items_covered": covered,
        "items_incomplete": incomplete,
        "test_matrix": test_matrix,
        "test_gaps": test_gaps,
        "human": {
            "where": ("The whole-feature review packet is written — every item's spec, design, "
                      "tasks AND full test plan (happy/negative/edge/regression) in one file."
                      if ready else
                      "The packet is written but INCOMPLETE — items are missing artifacts or "
                      "planned tests for required buckets."),
            "next": ("YOU review FEATURE-REVIEW.md and approve ONCE — that single decision "
                     "approves the plan of every item." if ready else
                     "I complete the missing items/test plans first, then regenerate this packet."),
            "who_acts": "YOU approve" if ready else "I act next",
        },
    }
    if not ready:
        result["blocked"] = True
        first_gap = (incomplete or test_gaps)[0]
        result["next_action"] = {"tool": "bc_write_spec",
                                 "reason": "Complete the missing item specs/test plans, then re-run bc_prepare_feature_review.",
                                 "params_hint": {"spec_name": first_gap.split(" ")[0]}}
    else:
        result["next_action"] = {"tool": "bc_request_approval",
                                 "reason": "Submit the ONE feature-wide plan gate.",
                                 "params_hint": {"spec_name": spec_name, "phase": "plan",
                                                  "artifact_path": str(review_path)}}
    return result


async def handle_feature_status(
    project_root: str,
    spec_name: str,
) -> Dict[str, Any]:
    """Roll-up: every child's per-item lifecycle phase + the prescribed next item."""
    root = Path(project_root).resolve()
    tree = feature_context.load_tree(project_root, spec_name)
    plan_path = specs_root(root) / spec_name / "feature_plan.json"
    if tree is None:
        return {
            "status": "blocked_no_capture", "blocked": True,
            "reason": "No captured feature tree — run bc_capture_feature first.",
            "next_action": {"tool": "bc_capture_feature", "params_hint": {"spec_name": spec_name}},
        }
    order = None
    if plan_path.exists():
        try:
            order = [e["id"] for e in json.loads(plan_path.read_text(encoding="utf-8"))
                     ["analysis"]["suggested_order"]]
        except (OSError, json.JSONDecodeError, KeyError):
            order = None
    children = tree.get("children", [])
    by_id = {str(c["id"]): c for c in children}
    ordered_ids = order or [str(c["id"]) for c in children]

    rows = []
    next_item = None
    for cid in ordered_ids:
        child = by_id.get(cid)
        if child is None or str(child.get("state", "")).lower() == "removed":
            continue
        item_spec = _find_item_spec(root, cid)
        phase = timeline.current_phase(root, item_spec) if item_spec else None
        rows.append({"id": cid, "title": child.get("title", "")[:80],
                     "ado_state": child.get("state", ""),
                     "item_spec": item_spec, "phase": phase})
        if next_item is None and phase != "archived":
            next_item = {"id": cid, "item_spec": item_spec, "phase": phase}

    out: Dict[str, Any] = {"status": "feature_status", "feature_id": tree["feature"].get("id"),
                           "items": rows, "all_archived": next_item is None}

    # FEATURE-LEVEL lifecycle: the feature does not stop at dispatch. Its phase is
    # DERIVED from the children (planning -> implementing -> integrating -> done) and
    # its delivery model is explicit: one feature branch; one item branch per child
    # (branch name must end with the item's spec name for the commit gate); each item
    # PR merges INTO the feature branch; the feature branch PR into master closes the
    # feature. Pushes/PRs happen only on explicit human approval.
    phases = [r["phase"] for r in rows]
    started = [p for p in phases if p]
    if not started:
        feature_phase = "feature_planned"
    elif next_item is None:
        feature_phase = "feature_done"
    elif all(p in ("merged", "archived") for p in started) and len(started) == len(phases):
        feature_phase = "feature_integrating"  # children merged; feature branch PR remains
    else:
        feature_phase = "feature_implementing"
    out["feature_phase"] = feature_phase
    out["delivery_model"] = {
        "plan_gate": "ONE feature-wide review (bc_prepare_feature_review -> FEATURE-REVIEW.md); "
                     "a single human approval cascades plan approval to EVERY item",
        "feature_branch": f"feature/{tree['feature'].get('id')}-<slug>",
        "item_commits": f"one commit per item on the feature branch "
                        f"(branch per item feature/{tree['feature'].get('id')}/<item-spec-name> "
                        "merged back, or sequential commits — history stays navigable per item)",
        "testing": "install the integrated feature branch on the container ONCE, then run "
                   "ALL item test codeunits + regression together (install-once/test-by-slice)",
        "feature_pr_target": "master — ONE PR for the whole feature (item commits preserved)",
        "push_policy": "NO push / PR creation without explicit human approval",
    }
    # Mega-review state: the ONE plan gate for the whole feature.
    review_path = specs_root(root) / spec_name / "FEATURE-REVIEW.md"
    from bc_agentic_mcp import authorization as _auth
    out["feature_review"] = {
        "exists": review_path.exists(),
        "approved": _auth.read_decision(root, spec_name, "plan") == "approve",
    }
    if next_item is None:
        out["next_action"] = {"tool": "bc_archive",
                              "reason": "Every child is archived — close the feature folder.",
                              "params_hint": {"spec_name": spec_name, "force": True}}
    elif next_item["item_spec"] is None:
        out["next_action"] = {
            "tool": "bc_capture_item_context",
            "reason": f"Next item in feature order: #{next_item['id']} — start its per-item lifecycle.",
            "params_hint": {"spec_name": f"wi{next_item['id']}", "work_item_id": next_item["id"]},
        }
    else:
        out["next_action"] = {
            "tool": "bc_advance",
            "reason": f"Item #{next_item['id']} is at phase '{next_item['phase']}' — advance it.",
            "params_hint": {"spec_name": next_item["item_spec"]},
        }
    return out
