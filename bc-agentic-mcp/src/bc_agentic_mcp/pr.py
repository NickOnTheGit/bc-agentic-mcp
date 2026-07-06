"""pr — Azure DevOps pull-request lifecycle via REST+PAT (B1) + work-item state sync (B3).

Extends the lifecycle past `bc_archive`: prepare -> create PR -> review comments ->
resolve -> merge. Reuses the proven ``wiki.py`` seam pattern: pure URL/payload builders,
deterministic response classification, one injectable ``requester`` for ALL network I/O,
PAT read from the environment (never stored, never logged, never echoed).

Deterministic classification rules (documented, encoded, tested):
- PR approved  = no reviewer vote < 0 AND at least one vote >= 5
  (ADO votes: 10 approved, 5 approved-with-suggestions, 0 none, -5 waiting, -10 rejected)
- Open thread  = thread status in {"active", "pending"}
- PR merged    = PR status == "completed"

C1 link: a PR that reaches "approved" satisfies the internal ``code`` gate — the approval
artifact is written by :func:`record_code_gate_from_pr` in the exact format
``authorization.read_decision`` parses, stamped with its ADO provenance.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import error as _urlerror
from urllib import request as _urlrequest

from bc_agentic_mcp.workspace import specs_root

DEFAULT_PAT_ENV = "AZURE_DEVOPS_EXT_PAT"
API_VERSION = "7.0"

# (method, url, headers, body) -> (status, response_body)
Requester = Callable[[str, str, Dict[str, str], Optional[bytes]], "tuple[int, str]"]

_OPEN_THREAD_STATUSES = {"active", "pending"}


def _default_requester(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]):
    req = _urlrequest.Request(url, headers=headers, data=body, method=method)
    try:
        with _urlrequest.urlopen(req, timeout=60) as resp:  # noqa: S310 (built URL)
            return resp.status, resp.read().decode("utf-8", "replace")
    except _urlerror.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""
    except Exception as exc:  # URLError, timeout, ...
        return 0, str(exc)


def basic_auth_header(pat: str) -> str:
    """ADO PAT auth: Basic base64(':<pat>'). The PAT is never returned or logged."""
    token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return f"Basic {token}"


def _auth(pat_env: str, requester: Optional[Requester]) -> "tuple[Optional[str], Optional[Dict[str, Any]]]":
    """Resolve the PAT fail-closed: no PAT and no injected requester => refusal."""
    pat = os.environ.get(pat_env)
    if not pat and requester is None:
        return None, {
            "ok": False,
            "reason": f"{pat_env} not set — cannot call Azure DevOps. Set the PAT and retry.",
        }
    return pat or "", None


# ---------------------------------------------------------------------------
# URL / payload builders (pure)
# ---------------------------------------------------------------------------

def repo_api_url(org_url: str, project: str, repository: str, suffix: str = "") -> str:
    # Path segments MUST be URL-encoded: ADO project/repo names may contain spaces
    # ('ERP AL' produced "URL can't contain control characters" — observed live on
    # Bug 267600 PR creation). quote() with safe='' encodes spaces and slashes alike.
    from urllib.parse import quote
    base = (f"{org_url.rstrip('/')}/{quote(project, safe='')}"
            f"/_apis/git/repositories/{quote(repository, safe='')}/pullrequests")
    return f"{base}{suffix}?api-version={API_VERSION}"


def workitem_api_url(org_url: str, project: str, work_item_id: int) -> str:
    from urllib.parse import quote
    return (
        f"{org_url.rstrip('/')}/{quote(project, safe='')}/_apis/wit/workitems/{int(work_item_id)}"
        f"?api-version={API_VERSION}"
    )


def build_create_payload(
    *, source_branch: str, target_branch: str, title: str, description: str,
    work_item_id: Optional[int] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "sourceRefName": f"refs/heads/{source_branch.removeprefix('refs/heads/')}",
        "targetRefName": f"refs/heads/{target_branch.removeprefix('refs/heads/')}",
        "title": title,
        "description": description,
    }
    if work_item_id:
        payload["workItemRefs"] = [{"id": str(int(work_item_id))}]
    return payload


def classify_votes(reviewers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic approval verdict from reviewer votes (rule in module docstring)."""
    votes = [int(r.get("vote", 0)) for r in (reviewers or [])]
    rejected = [v for v in votes if v < 0]
    approving = [v for v in votes if v >= 5]
    return {
        "approved": not rejected and bool(approving),
        "votes": votes,
        "rejections": len(rejected),
        "approvals": len(approving),
    }


def classify_threads(threads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Split PR comment threads into open vs resolved; deleted threads are ignored.

    ``file`` is the REPO-RELATIVE path exactly as ADO reports it (leading slash
    stripped) and ``line`` is the right-file anchor — the old passthrough let the
    server's path absolutizer mangle '/extensions/…' into 'C:/extensions/…' and
    dropped the line, so the rework loop could not point at the flagged spot
    (observed live on PR 41674 thread 316419, 2026-07-06).
    """
    open_threads: List[Dict[str, Any]] = []
    resolved = 0
    for t in threads or []:
        if t.get("isDeleted"):
            continue
        status = str(t.get("status", "")).strip().lower()
        if status in _OPEN_THREAD_STATUSES:
            comments = t.get("comments") or []
            first = comments[0] if comments else {}
            ctx = t.get("threadContext") or {}
            file_path = str(ctx.get("filePath") or "").lstrip("/")
            line = ((ctx.get("rightFileStart") or {}).get("line")
                    or (ctx.get("leftFileStart") or {}).get("line"))
            open_threads.append({
                "thread_id": t.get("id"),
                "status": status,
                "file": file_path,
                "line": line,
                "author": ((first.get("author") or {}).get("displayName")),
                "comment": str(first.get("content", ""))[:500],
                "comment_count": len(comments),
            })
        elif status:  # fixed/closed/wontFix/byDesign
            resolved += 1
    return {"open": open_threads, "open_count": len(open_threads), "resolved_count": resolved}


# ---------------------------------------------------------------------------
# PR record persistence (.specs/<item>/pr/pr.json) — how later calls find the PR
# ---------------------------------------------------------------------------

def pr_dir(project_root: Path, spec_name: str) -> Path:
    return specs_root(Path(project_root).resolve()) / spec_name / "pr"


def save_pr_record(project_root: Path, spec_name: str, record: Dict[str, Any]) -> str:
    directory = pr_dir(project_root, spec_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pr.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=True), encoding="utf-8")
    return str(path)


def load_pr_record(project_root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    path = pr_dir(project_root, spec_name) / "pr.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# REST operations (network via the requester seam only)
# ---------------------------------------------------------------------------

def create_pr(
    *,
    org_url: str,
    project: str,
    repository: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    work_item_id: Optional[int] = None,
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    url = repo_api_url(org_url, project, repository)
    payload = build_create_payload(
        source_branch=source_branch, target_branch=target_branch,
        title=title, description=description, work_item_id=work_item_id,
    )
    request = requester or _default_requester
    status, body = request(
        "POST", url,
        {"Authorization": basic_auth_header(pat), "Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )
    if status not in (200, 201):
        return {"ok": False, "status": status, "reason": f"HTTP {status}", "body": body[:500]}
    data = json.loads(body) if body else {}
    return {
        "ok": True,
        "pr_id": data.get("pullRequestId"),
        "url": ((data.get("_links") or {}).get("web") or {}).get("href") or data.get("url"),
        "status": data.get("status"),
        "source_branch": source_branch,
        "target_branch": target_branch,
    }


def update_pr_description(
    *,
    org_url: str,
    project: str,
    repository: str,
    pr_id: int,
    description: str,
    title: Optional[str] = None,
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    """PATCH an existing PR's description (and optionally title) in place.

    The description is a living artifact: when the generator improves, the OPEN
    PR must receive the better text — reviewers read the PR, not the repo."""
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    url = repo_api_url(org_url, project, repository, f"/{int(pr_id)}")
    payload: Dict[str, Any] = {"description": description[:3990]}
    if title:
        payload["title"] = title
    request = requester or _default_requester
    status, body = request(
        "PATCH", url,
        {"Authorization": basic_auth_header(pat), "Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )
    if status != 200:
        return {"ok": False, "status": status, "reason": f"HTTP {status}", "body": body[:500]}
    return {"ok": True, "pr_id": pr_id, "updated": True}


def get_threads(
    *,
    org_url: str,
    project: str,
    repository: str,
    pr_id: int,
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    url = repo_api_url(org_url, project, repository, f"/{int(pr_id)}/threads")
    request = requester or _default_requester
    status, body = request("GET", url, {"Authorization": basic_auth_header(pat)}, None)
    if status != 200:
        return {"ok": False, "status": status, "reason": f"HTTP {status}", "body": body[:500]}
    threads = (json.loads(body) if body else {}).get("value", [])
    out = classify_threads(threads)
    out["ok"] = True
    return out


def post_thread(
    *,
    org_url: str,
    project: str,
    repository: str,
    pr_id: int,
    content: str,
    status: str = "closed",
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    """Create a new comment thread on the PR (default status closed — informational,
    so it never counts as an open blocking thread). Born from the ADO 4000-char
    description cap: the explicit test table rides as a comment instead."""
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    url = repo_api_url(org_url, project, repository, f"/{int(pr_id)}/threads")
    request = requester or _default_requester
    headers = {"Authorization": basic_auth_header(pat), "Content-Type": "application/json"}
    payload = {"comments": [{"parentCommentId": 0, "content": content, "commentType": 1}],
               "status": 2 if status == "closed" else 1}
    sc, body = request("POST", url, headers, json.dumps(payload).encode("utf-8"))
    if sc not in (200, 201):
        return {"ok": False, "status": sc, "reason": f"HTTP {sc}", "body": body[:300]}
    return {"ok": True, "thread_id": (json.loads(body) if body else {}).get("id")}


def resolve_thread(
    *,
    org_url: str,
    project: str,
    repository: str,
    pr_id: int,
    thread_id: int,
    reply: Optional[str] = None,
    status: str = "fixed",
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    """Optionally reply to the thread, then PATCH its status (default 'fixed')."""
    if status not in {"fixed", "closed", "wontFix", "byDesign"}:
        return {"ok": False, "reason": f"invalid thread status '{status}'; use fixed|closed|wontFix|byDesign"}
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    request = requester or _default_requester
    headers = {"Authorization": basic_auth_header(pat), "Content-Type": "application/json"}
    if reply:
        reply_url = repo_api_url(
            org_url, project, repository, f"/{int(pr_id)}/threads/{int(thread_id)}/comments"
        )
        rc, rbody = request(
            "POST", reply_url, headers,
            json.dumps({"content": reply, "commentType": "text"}).encode("utf-8"),
        )
        if rc not in (200, 201):
            return {"ok": False, "status": rc, "reason": f"reply failed: HTTP {rc}", "body": rbody[:500]}
    patch_url = repo_api_url(org_url, project, repository, f"/{int(pr_id)}/threads/{int(thread_id)}")
    pc, pbody = request("PATCH", patch_url, headers, json.dumps({"status": status}).encode("utf-8"))
    if pc != 200:
        return {"ok": False, "status": pc, "reason": f"HTTP {pc}", "body": pbody[:500]}
    return {"ok": True, "thread_id": thread_id, "new_status": status, "replied": bool(reply)}


def merge_status(
    *,
    org_url: str,
    project: str,
    repository: str,
    pr_id: int,
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    url = repo_api_url(org_url, project, repository, f"/{int(pr_id)}")
    request = requester or _default_requester
    status, body = request("GET", url, {"Authorization": basic_auth_header(pat)}, None)
    if status != 200:
        return {"ok": False, "status": status, "reason": f"HTTP {status}", "body": body[:500]}
    data = json.loads(body) if body else {}
    verdict = classify_votes(data.get("reviewers") or [])
    pr_status = str(data.get("status", "")).lower()
    return {
        "ok": True,
        "pr_status": pr_status,                      # active | completed | abandoned
        "merge_status": data.get("mergeStatus"),     # succeeded | conflicts | ...
        "completed": pr_status == "completed",
        "approved": verdict["approved"],
        "votes": verdict["votes"],
        "rejections": verdict["rejections"],
        "reviewers": [
            {"name": (r.get("displayName") or ""), "vote": r.get("vote", 0)}
            for r in (data.get("reviewers") or [])
        ],
    }


def sync_workitem_state(
    *,
    org_url: str,
    project: str,
    work_item_id: int,
    state: str,
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    """B3: PATCH the ADO work item state (JSON Patch). State names are org-specific
    and therefore an explicit input — never hardcoded here."""
    if not state or not str(state).strip():
        return {"ok": False, "reason": "state is required (org-specific, e.g. 'Active', 'Resolved')"}
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    url = workitem_api_url(org_url, project, work_item_id)
    patch = [{"op": "add", "path": "/fields/System.State", "value": str(state)}]
    request = requester or _default_requester
    status, body = request(
        "PATCH", url,
        {"Authorization": basic_auth_header(pat), "Content-Type": "application/json-patch+json"},
        json.dumps(patch).encode("utf-8"),
    )
    if status != 200:
        return {"ok": False, "status": status, "reason": f"HTTP {status}", "body": body[:500]}
    data = json.loads(body) if body else {}
    return {
        "ok": True,
        "work_item_id": work_item_id,
        "new_state": ((data.get("fields") or {}).get("System.State")) or state,
    }


# ---------------------------------------------------------------------------
# C1 bridge: PR approval satisfies the internal `code` gate
# ---------------------------------------------------------------------------

def record_code_gate_from_pr(
    project_root: Path, spec_name: str, pr_record: Dict[str, Any], verdict: Dict[str, Any]
) -> Optional[str]:
    """Write approvals/code.md (Status: approve) when the PR is approved/completed.

    Idempotent: an existing approved artifact is left untouched. The artifact uses the
    exact ``**Status:**`` format ``authorization.read_decision`` parses, and names its
    provenance (ADO PR review), so the audit trail shows WHO approved: the PR reviewers.
    """
    if not (verdict.get("approved") or verdict.get("completed")):
        return None
    approvals = specs_root(Path(project_root).resolve()) / spec_name / "approvals"
    path = approvals / "code.md"
    if path.exists() and "**Status:** approve" in path.read_text(encoding="utf-8", errors="replace"):
        return str(path)
    approvals.mkdir(parents=True, exist_ok=True)
    reviewers = ", ".join(
        f"{r['name']} ({r['vote']})" for r in verdict.get("reviewers", []) if r.get("name")
    ) or "unknown"
    path.write_text(
        f"""# Approval: {spec_name} — Code Gate (satisfied by ADO PR review)

**Status:** approve
**Recorded:** {datetime.now(timezone.utc).isoformat()}
**Source:** azure-devops-pr
**PR:** {pr_record.get('pr_id')} ({pr_record.get('url', 'n/a')})
**Reviewers:** {reviewers}

The internal `code` gate is satisfied by the pull-request approval in Azure DevOps
(C1: PR approval IS the code review — no duplicate internal sign-off).
""",
        encoding="utf-8",
    )
    return str(path)
