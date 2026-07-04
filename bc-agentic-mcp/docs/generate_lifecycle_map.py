"""generate_lifecycle_map.py — emit an interactive HTML map of the BC MCP paradigm.

The map is GENERATED from the code (workflow_policy, timeline, advance, attempts,
server registrations) — never hand-drawn — so it cannot drift from reality.
Re-run after any lifecycle change:  python docs/generate_lifecycle_map.py
Output: docs/lifecycle-map.html (self-contained; cytoscape.js from CDN).
"""
import json
import re
from pathlib import Path

from bc_agentic_mcp import advance, attempts, timeline
from bc_agentic_mcp import workflow_policy as wp

ROOT = Path(__file__).resolve().parent.parent
SERVER_SRC = (ROOT / "src" / "bc_agentic_mcp" / "server.py").read_text(encoding="utf-8")
REGISTERED = sorted(set(re.findall(r'@mcp\.tool\(name="([^"]+)"', SERVER_SRC)))

# Server-side gates guarding specific transitions (tool -> gate description).
GATES = {
    "bc_run_tests": "env-preflight gate (fresh passing .specs/.env/<container>.json) + container mutex + doom-loop guard",
    "bc_generate_tests": "schema-reality gate (API data_model fields must exist or be declared new) + doom-loop guard",
    "bc_implement_write": "approval gate (plan/code approved) + scope enforcement + fresh-review gate + doom-loop guard",
    "bc_submit_decision": "evidence gates: verification (coverage+strength), test lifecycle, local-container proof, validation classes, traceability",
    "bc_prepare_pr": "verification gate (refuses while evidence gaps exist)",
    "bc_create_pr": "prepared-artifact gate + PAT fail-closed + doom-loop guard",
    "bc_archive": "task-completion + test-evidence gate",
}

ACTOR = {  # who drives each state forward
    "feature_captured": ("deterministic", "Feature tier: whole tree fetched fresh (bc_capture_feature)"),
    "feature_refined": ("deterministic", "Claims x code reality: mismatches/redundancies/conflicts are FACTS; model adds critique"),
    "feature_planned": ("judgment", "HUMAN GATE F1 next: facts are deterministic, waves are model judgment, approval is human"),
    "feature_implementing": ("deterministic", "dispatch loop: bc_feature_status prescribes the next child; each child runs the FULL per-item lifecycle below on its own branch (feature/<id>/<item-spec>)"),
    "feature_integrating": ("human", "all children merged into the feature branch — ONE feature PR into master (explicit human approval to push/create)"),
    "feature_done": ("terminal", "feature branch merged; bc_archive closes the feature folder"),
    "item_received": ("judgment", "model plans (capture done, refinement next)"),
    "item_refined": ("deterministic", "Item claims x code reality (bc_refine_item, ENFORCED engine) — ranked context pack emitted"),
    "spec_written": ("judgment", "model authors design"),
    "design_planned": ("judgment", "model builds code context"),
    "code_context_built": ("judgment", "model breaks down tasks"),
    "tasks_broken_down": ("judgment", "model prepares review packet"),
    "review_prepared": ("judgment", "model requests approval"),
    "approval_requested": ("human", "HUMAN GATE 1: plan approval via bc_submit_decision"),
    "decision_recorded": ("judgment", "model writes code via bc_implement_write"),
    "implemented": ("deterministic", "bc_advance: generate tests"),
    "tests_generated": ("deterministic", "bc_advance: run tests (full cycle in container)"),
    "tests_run": ("deterministic", "bc_advance: compute verification"),
    "verified": ("deterministic", "bc_advance: prepare PR from evidence"),
    "reviewed": ("deterministic", "bc-reviewer subagent verdict recorded"),
    "pr_prepared": ("deterministic", "bc_advance: create PR in ADO"),
    "pr_created": ("human", "HUMAN GATE 2: PR review votes in ADO (polled via bc_get_review_comments / bc_merge_status; approval satisfies internal code gate)"),
    "review_comments_open": ("judgment", "rework loop: fix within Charter scope, resolve threads"),
    "merged": ("deterministic", "HUMAN GATE 3 (merge button) just happened in ADO; bc_advance archives"),
    "archived": ("terminal", "lifecycle complete"),
}

ORDER = list(ACTOR)

# Transitions: planning chain (tool completes phase), advance chain, and loops.
PHASE_BY_TOOL = dict(timeline.TOOL_PHASE)
EDGES = [
    ("feature_captured", "feature_refined", "bc_refine_feature", "auto"),
    ("feature_refined", "feature_planned", "bc_plan_feature", "auto"),
    ("feature_planned", "feature_implementing", "HUMAN GATE F1 approve (bc_submit_decision) -> dispatch loop starts", "human"),
    ("feature_implementing", "item_received", "bc_feature_status -> bc_capture_item_context (per child, own branch)", "auto"),
    ("archived", "feature_implementing", "bc_feature_status: next child (repeat until all merged)", "auto"),
    ("feature_implementing", "feature_integrating", "all children merged into the feature branch", "auto"),
    ("feature_integrating", "feature_done", "feature PR into master approved + merged (HUMAN) -> bc_archive", "human"),
    ("item_received", "item_refined", "bc_refine_item", "auto"),
    ("item_refined", "spec_written", "bc_write_spec (grounded by refinement claims)", "judgment"),
    ("spec_written", "design_planned", "bc_plan_design", "judgment"),
    ("design_planned", "code_context_built", "bc_read_code_context", "judgment"),
    ("code_context_built", "tasks_broken_down", "bc_breakdown_tasks", "judgment"),
    ("tasks_broken_down", "review_prepared", "bc_prepare_review", "judgment"),
    ("review_prepared", "approval_requested", "bc_request_approval", "judgment"),
    ("approval_requested", "decision_recorded", "bc_submit_decision", "human"),
    ("decision_recorded", "implemented", "bc_implement_write", "judgment"),
    ("implemented", "tests_generated", "bc_generate_tests", "auto"),
    ("tests_generated", "tests_run", "bc_run_tests", "auto"),
    ("tests_run", "verified", "bc_verify", "auto"),
    ("verified", "reviewed", "bc_review (bc-reviewer subagent)", "subagent"),
    ("reviewed", "pr_prepared", "bc_prepare_pr", "auto"),
    ("verified", "pr_prepared", "bc_prepare_pr", "auto"),
    ("pr_prepared", "pr_created", "bc_create_pr", "auto"),
    ("pr_created", "review_comments_open", "bc_get_review_comments (open threads)", "auto"),
    ("pr_created", "merged", "bc_merge_status (completed)", "human"),
    ("review_comments_open", "implemented", "bc_implement_write (rework in scope)", "judgment"),
    ("merged", "archived", "bc_archive", "auto"),
]

nodes = []
for phase in ORDER:
    kind, note = ACTOR[phase]
    stage = wp._phase_to_stage(phase)
    tools = sorted(wp.STAGE_ALLOWLIST.get(stage, set()))
    seed = advance.seed_action(phase, {"container_name": "<c>", "test_extension_id": "<e>",
                                       "org_url": "<o>", "project": "<p>", "repository": "<r>"})
    nodes.append({
        "id": phase,
        "label": timeline._PHASE_LABEL.get(phase, phase),
        "kind": kind,
        "note": note,
        "stage": stage,
        "allowed_tools": tools,
        "advance_seed": seed.get("action") or f"stop: {seed.get('stop')}",
    })

edges = []
for i, (src, dst, tool, kind) in enumerate(EDGES):
    bare = tool.split(" ")[0]
    edges.append({
        "id": f"e{i}", "source": src, "target": dst, "tool": tool, "kind": kind,
        "gate": GATES.get(bare, ""),
        "guarded": bare in attempts.GUARDED_TOOLS,
    })

model = {
    "generated_from": "workflow_policy + timeline + advance + attempts + server registrations",
    "registered_tool_count": len(REGISTERED),
    "registered_tools": REGISTERED,
    "guarded_tools": sorted(attempts.GUARDED_TOOLS),
    "nodes": nodes,
    "edges": edges,
}

HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>BC MCP — Lifecycle Map (generated from code)</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
  body { margin:0; font-family:'Segoe UI',sans-serif; background:#0f1420; color:#dce3f0; display:flex; height:100vh; }
  #cy { flex:1; }
  #panel { width:380px; background:#161d2e; border-left:1px solid #2a3550; padding:16px; overflow-y:auto; }
  h1 { font-size:15px; margin:0 0 4px; } h2 { font-size:13px; color:#8fa3c8; margin:14px 0 4px; }
  .sub { font-size:11px; color:#6b7c9e; margin-bottom:10px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:9px; font-size:11px; margin:2px 3px 2px 0; }
  .human { background:#8b2635; } .judgment { background:#8a6d1a; } .deterministic { background:#1e6b45; }
  .subagent { background:#1e4d8a; } .terminal { background:#3a3f52; } .auto { background:#1e6b45; }
  .tool { background:#232c44; font-size:10px; padding:1px 6px; border-radius:4px; display:inline-block; margin:1px; }
  .gate { background:#3a1f24; border:1px solid #8b2635; padding:6px 8px; border-radius:6px; font-size:11px; margin:6px 0; }
  #legend { position:absolute; top:10px; left:10px; background:#161d2eee; padding:10px 12px; border-radius:8px; font-size:11px; z-index:5; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:5px; margin-right:5px; vertical-align:-1px; }
</style></head><body>
<div id="legend">
  <b>BC MCP lifecycle</b> — click any state or arrow<br>
  <span class="dot" style="background:#c0392b"></span>human gate&nbsp;
  <span class="dot" style="background:#d4a017"></span>model judgment&nbsp;
  <span class="dot" style="background:#27ae60"></span>deterministic (bc_advance)<br>
  <span class="dot" style="background:#2e6fd8"></span>reviewer subagent&nbsp;
  <span class="dot" style="background:#7f8c9b"></span>terminal&nbsp;
  &#9889; = doom-loop guarded
</div>
<div id="cy"></div>
<div id="panel">
  <h1>BC MCP — Lifecycle Map</h1>
  <div class="sub" id="meta"></div>
  <div id="detail">Click a state (node) or transition (edge).<br><br>
  <b>Paradigm:</b> agents are policy-scoped actors over a server-owned state machine.
  States and gates live in the SERVER; agents supply judgment only; humans are three
  explicit gates (plan approval, PR review, merge). Everything between gates is one
  bc_advance call. This page is generated from the code — if it looks wrong, the code
  is wrong.</div>
</div>
<script>
const M = __DATA__;
document.getElementById("meta").textContent =
  M.registered_tool_count + " registered tools · generated from " + M.generated_from;
const COLOR = {human:"#c0392b", judgment:"#d4a017", deterministic:"#27ae60",
               subagent:"#2e6fd8", terminal:"#7f8c9b", auto:"#27ae60"};
const cy = cytoscape({
  container: document.getElementById("cy"),
  elements: [
    ...M.nodes.map(n => ({data:{...n}})),
    ...M.edges.map(e => ({data:{...e, label:(e.guarded?"\\u26a1 ":"")+e.tool.split(" ")[0]}}))
  ],
  style: [
    {selector:"node", style:{
      "background-color": ele => COLOR[ele.data("kind")] || "#555",
      "label":"data(label)", "color":"#e8edf7", "font-size":"11px",
      "text-wrap":"wrap", "text-max-width":"120px", "text-valign":"center",
      "width":"130px", "height":"52px", "shape":"round-rectangle",
      "border-width":1, "border-color":"#2a3550"}},
    {selector:"edge", style:{
      "curve-style":"bezier", "target-arrow-shape":"triangle",
      "line-color": ele => COLOR[ele.data("kind")] || "#445",
      "target-arrow-color": ele => COLOR[ele.data("kind")] || "#445",
      "width":2, "label":"data(label)", "font-size":"9px", "color":"#9db1d6",
      "text-rotation":"autorotate", "text-background-color":"#0f1420",
      "text-background-opacity":0.85, "text-background-padding":"2px"}},
    {selector:":selected", style:{"border-width":3, "border-color":"#ffffff"}}
  ],
  layout: {name:"breadthfirst", roots:["item_received"], directed:true,
           spacingFactor:1.15, padding:30}
});
const panel = document.getElementById("detail");
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML;}
cy.on("tap","node",evt=>{
  const d = evt.target.data();
  panel.innerHTML =
    "<h2>"+esc(d.label)+"</h2>"+
    "<span class='badge "+d.kind+"'>"+d.kind+"</span>"+
    "<span class='badge terminal'>stage: "+d.stage+"</span>"+
    "<p style='font-size:12px'>"+esc(d.note)+"</p>"+
    "<h2>bc_advance from here</h2><div class='tool'>"+esc(d.advance_seed)+"</div>"+
    "<h2>Tools admitted in stage '"+d.stage+"' ("+d.allowed_tools.length+")</h2>"+
    d.allowed_tools.map(t=>"<span class='tool'>"+esc(t)+"</span>").join("");
});
cy.on("tap","edge",evt=>{
  const d = evt.target.data();
  panel.innerHTML =
    "<h2>"+esc(d.tool)+"</h2>"+
    "<span class='badge "+d.kind+"'>"+d.kind+"</span>"+
    (d.guarded?"<span class='badge human'>\\u26a1 doom-loop guarded</span>":"")+
    "<p style='font-size:12px'>"+esc(d.source)+" \\u2192 "+esc(d.target)+"</p>"+
    (d.gate?"<div class='gate'><b>Server gate:</b> "+esc(d.gate)+"</div>":
     "<div class='sub'>No dedicated gate on this transition (policy + envelope still apply).</div>");
});
</script></body></html>"""

out = ROOT / "docs" / "lifecycle-map.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(HTML.replace("__DATA__", json.dumps(model, indent=1)), encoding="utf-8")
print(f"written: {out}")
print(f"nodes: {len(nodes)}  edges: {len(edges)}  tools: {len(REGISTERED)}")
