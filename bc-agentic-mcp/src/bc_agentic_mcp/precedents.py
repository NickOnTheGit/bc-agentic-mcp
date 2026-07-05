"""precedents — mine ADO delivery history: how did we ship items LIKE this one before?

The team's conventions live in years of ADO history that predates this machine:
which objects a "contract PBI" really touches, whether items like this one carry an
upgrade codeunit, where their bugs clustered. This module makes that history a
first-class, DETERMINISTIC context source.

Chain (all REST+PAT, fetcher seams for tests, fail-closed without a PAT):
  WIQL: recent closed items of the same type
    -> local BM25 rank on titles (rare terms weigh more; ties -> lower id)
      -> linked PRs per item (ArtifactLink vstfs:///Git/PullRequestId/...)
        -> changed paths of the last PR iteration
          -> distilled delivery shape (object kinds, upgrade/permission/test/xlf rates)

Determinism contract: same ADO responses -> byte-identical precedents.json.
Every collection is explicitly sorted; scores are rounded; no wall-clock inside
the payload except the explicit `as_of` stamp.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import quote

from bc_agentic_mcp.lessons import bm25_scores

GetFetcher = Callable[[str, Dict[str, str]], Tuple[int, str]]
PostFetcher = Callable[[str, Dict[str, str], str], Tuple[int, str]]

_CANDIDATE_POOL = 200   # recent closed same-type items considered
_DEFAULT_TOP_K = 5      # precedents actually mined in depth
_MAX_PRS_PER_ITEM = 2   # a delivery rarely needs more; keeps the call budget flat
_DONE_STATES = ("Done", "Closed", "Resolved")

# AL file-name suffix -> object kind. Order matters: longest suffix first so
# .TableExt.al never classifies as .Table.al.
_KIND_SUFFIXES = [
    ("permissionsetext.al", "PermissionSet"),
    ("permissionset.al", "PermissionSet"),
    ("tableext.al", "TableExtension"),
    ("pageext.al", "PageExtension"),
    ("enumext.al", "EnumExtension"),
    ("reportext.al", "ReportExtension"),
    ("codeunit.al", "Codeunit"),
    ("interface.al", "Interface"),
    ("xmlport.al", "XmlPort"),
    ("report.al", "Report"),
    ("table.al", "Table"),
    ("query.al", "Query"),
    ("page.al", "Page"),
    ("enum.al", "Enum"),
]


def _default_get(url: str, headers: Dict[str, str]) -> Tuple[int, str]:
    req = _urlrequest.Request(url, headers=headers, method="GET")
    try:
        with _urlrequest.urlopen(req, timeout=60) as resp:  # noqa: S310 (built URL)
            return resp.status, resp.read().decode("utf-8", "replace")
    except _urlerror.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""
    except Exception as exc:
        return 0, str(exc)


def _default_post(url: str, headers: Dict[str, str], body: str) -> Tuple[int, str]:
    req = _urlrequest.Request(
        url, headers={**headers, "Content-Type": "application/json"},
        data=body.encode("utf-8"), method="POST",
    )
    try:
        with _urlrequest.urlopen(req, timeout=60) as resp:  # noqa: S310 (built URL)
            return resp.status, resp.read().decode("utf-8", "replace")
    except _urlerror.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""
    except Exception as exc:
        return 0, str(exc)


def _auth(pat: str) -> Dict[str, str]:
    token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _api(org_url: str, project: str, path: str) -> str:
    return f"{org_url.rstrip('/')}/{quote(str(project), safe='')}/_apis/{path}"


def _json_or_none(status: int, body: str) -> Optional[Dict[str, Any]]:
    if status != 200:
        return None
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def classify_path(path: str) -> Optional[str]:
    """AL object kind from a changed file path; None for non-AL files."""
    low = path.replace("\\", "/").lower()
    name = low.rsplit("/", 1)[-1]
    if not name.endswith(".al"):
        return None
    for suffix, kind in _KIND_SUFFIXES:
        if name.endswith("." + suffix):
            return kind
    return "OtherAL"


def find_candidates(
    *, org_url: str, project: str, item_type: str, exclude_id: str,
    headers: Dict[str, str], post: PostFetcher,
) -> List[int]:
    """Recent closed items of the same type (WIQL, newest first). Deterministic:
    ADO orders by ChangedDate DESC; we keep that order as the recency pool."""
    states = ", ".join(f"'{s}'" for s in _DONE_STATES)
    wiql = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = @project AND [System.WorkItemType] = '{item_type}' "
        f"AND [System.State] IN ({states}) AND [System.Id] <> {int(exclude_id or 0)} "
        "ORDER BY [System.ChangedDate] DESC"
    )
    url = _api(org_url, project, f"wit/wiql?$top={_CANDIDATE_POOL}&api-version=7.0")
    data = _json_or_none(*post(url, headers, json.dumps({"query": wiql})))
    if not data:
        return []
    return [int(w["id"]) for w in data.get("workItems", []) if "id" in w]


def fetch_titles(
    *, org_url: str, project: str, ids: List[int],
    headers: Dict[str, str], post: PostFetcher,
) -> Dict[int, str]:
    """Batch-fetch titles (workitemsbatch caps at 200 — exactly our pool size)."""
    if not ids:
        return {}
    url = _api(org_url, project, "wit/workitemsbatch?api-version=7.0")
    body = json.dumps({"ids": ids[:200], "fields": ["System.Id", "System.Title"]})
    data = _json_or_none(*post(url, headers, body))
    if not data:
        return {}
    out: Dict[int, str] = {}
    for item in data.get("value", []):
        fields = item.get("fields", {})
        wid = item.get("id")
        if wid is not None:
            out[int(wid)] = str(fields.get("System.Title", ""))
    return out


def rank_similar(title: str, titles: Dict[int, str], top_k: int) -> List[Tuple[int, float]]:
    """BM25 over candidate titles. Deterministic order: score DESC, id ASC.
    Zero-score candidates never surface — no title overlap means no precedent claim."""
    ids = sorted(titles.keys())
    scores = bm25_scores(title, [titles[i] for i in ids])
    ranked = sorted(zip(ids, scores), key=lambda p: (-p[1], p[0]))
    return [(i, s) for i, s in ranked if s > 0][:top_k]


_PR_ARTIFACT = re.compile(r"vstfs:///Git/PullRequestId/[^/]+%2F([0-9a-fA-F-]+)%2F(\d+)")


def linked_prs(
    *, org_url: str, project: str, item_id: int,
    headers: Dict[str, str], get: GetFetcher,
) -> List[Dict[str, str]]:
    """PR links from the item's relations: [{repository_id, pr_id}], pr_id ASC."""
    url = _api(org_url, project, f"wit/workitems/{item_id}?$expand=relations&api-version=7.0")
    data = _json_or_none(*get(url, headers))
    if not data:
        return []
    found: List[Dict[str, str]] = []
    for rel in data.get("relations", []) or []:
        m = _PR_ARTIFACT.search(str(rel.get("url", "")))
        if m:
            found.append({"repository_id": m.group(1), "pr_id": m.group(2)})
    found.sort(key=lambda p: int(p["pr_id"]))
    return found[:_MAX_PRS_PER_ITEM]


def pr_changed_paths(
    *, org_url: str, project: str, repository_id: str, pr_id: str,
    headers: Dict[str, str], get: GetFetcher,
) -> List[str]:
    """Changed paths of the PR's LAST iteration (the merged truth), sorted."""
    base = f"git/repositories/{repository_id}/pullRequests/{pr_id}/iterations"
    data = _json_or_none(*get(_api(org_url, project, base + "?api-version=7.0"), headers))
    if not data or not data.get("value"):
        return []
    last = max(int(i.get("id", 0)) for i in data["value"])
    changes = _json_or_none(*get(
        _api(org_url, project, f"{base}/{last}/changes?$top=1000&api-version=7.0"), headers))
    if not changes:
        return []
    paths = {
        str((c.get("item") or {}).get("path", "")).strip()
        for c in changes.get("changeEntries", []) or []
    }
    return sorted(p for p in paths if p)


def distill_item(paths: List[str]) -> Dict[str, Any]:
    """One precedent's delivery shape from its changed paths. Pure + deterministic."""
    kinds: Dict[str, int] = {}
    dirs: Dict[str, int] = {}
    has_upgrade = touched_tests = touched_xlf = touched_permissions = False
    for path in paths:
        low = path.replace("\\", "/").lower()
        kind = classify_path(path)
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "PermissionSet":
            touched_permissions = True
        if "upgrade" in low.rsplit("/", 1)[-1] and low.endswith("codeunit.al"):
            has_upgrade = True
        if "/test" in low or low.startswith("test"):
            touched_tests = True
        if low.endswith(".xlf"):
            touched_xlf = True
        parts = [p for p in low.split("/") if p]
        if len(parts) > 1:
            top = "/".join(parts[:2])
            dirs[top] = dirs.get(top, 0) + 1
    return {
        "files_changed": len(paths),
        "object_kinds": sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])),
        "top_dirs": sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
        "has_upgrade_codeunit": has_upgrade,
        "touched_permissions": touched_permissions,
        "touched_tests": touched_tests,
        "touched_xlf": touched_xlf,
    }


def aggregate_shape(precedents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The historical delivery shape: what items LIKE this one usually touch."""
    mined = [p for p in precedents if p.get("files_changed", 0) > 0]
    if not mined:
        return {"based_on": 0}
    n = len(mined)
    kind_totals: Dict[str, int] = {}
    dir_totals: Dict[str, int] = {}
    for p in mined:
        for kind, count in p.get("object_kinds", []):
            kind_totals[kind] = kind_totals.get(kind, 0) + count
        for d, count in p.get("top_dirs", []):
            dir_totals[d] = dir_totals.get(d, 0) + count

    def pct(flag: str) -> float:
        return round(100.0 * sum(1 for p in mined if p.get(flag)) / n, 1)

    return {
        "based_on": n,
        "object_kind_totals": sorted(kind_totals.items(), key=lambda kv: (-kv[1], kv[0])),
        "top_dirs": sorted(dir_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
        "pct_with_upgrade_codeunit": pct("has_upgrade_codeunit"),
        "pct_touching_permissions": pct("touched_permissions"),
        "pct_with_tests": pct("touched_tests"),
        "pct_touching_xlf": pct("touched_xlf"),
    }


def mine(
    *,
    org_url: str,
    project: str,
    item_id: str,
    item_type: str,
    title: str,
    pat: str,
    top_k: int = _DEFAULT_TOP_K,
    get: Optional[GetFetcher] = None,
    post: Optional[PostFetcher] = None,
) -> Dict[str, Any]:
    """Full mining chain. Returns the persistable payload (no I/O here)."""
    get = get or _default_get
    post = post or _default_post
    headers = _auth(pat)
    candidates = find_candidates(
        org_url=org_url, project=project, item_type=item_type,
        exclude_id=item_id, headers=headers, post=post)
    titles = fetch_titles(org_url=org_url, project=project, ids=candidates,
                          headers=headers, post=post)
    ranked = rank_similar(title, titles, top_k)
    precedents: List[Dict[str, Any]] = []
    for wid, score in ranked:
        prs = linked_prs(org_url=org_url, project=project, item_id=wid,
                         headers=headers, get=get)
        paths: List[str] = []
        for pr in prs:
            paths.extend(pr_changed_paths(
                org_url=org_url, project=project,
                repository_id=pr["repository_id"], pr_id=pr["pr_id"],
                headers=headers, get=get))
        paths = sorted(set(paths))
        entry = {
            "id": wid,
            "title": titles.get(wid, ""),
            "score": round(score, 4),
            "pr_ids": [int(p["pr_id"]) for p in prs],
            **distill_item(paths),
        }
        precedents.append(entry)
    return {
        "mined": True,
        "query": {"item_id": str(item_id), "item_type": item_type, "title": title},
        "candidate_pool": len(candidates),
        "precedents": precedents,
        "delivery_shape": aggregate_shape(precedents),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Persistence + the gate contract (bc_plan_design enforces this file's existence)
# ---------------------------------------------------------------------------

def precedents_path(root: Path, spec_name: str) -> Path:
    from bc_agentic_mcp.item_context import context_dir
    return context_dir(Path(root).resolve(), spec_name) / "precedents.json"


def save(root: Path, spec_name: str, payload: Dict[str, Any]) -> Path:
    path = precedents_path(root, spec_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load(root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    path = precedents_path(root, spec_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def evidence_status(root: Path, spec_name: str) -> Dict[str, Any]:
    """What the gate reads: mined evidence, an explicit skip, or nothing.

    Deterministic contract — the gate opens ONLY on a persisted record:
    - {mined: true, ...}   -> open (even zero precedents: 'no similar items' IS evidence)
    - {skipped: true, reason} -> open (explicit, auditable waiver)
    - no file / malformed  -> closed
    """
    data = load(root, spec_name)
    if data is None:
        return {"present": False}
    if data.get("mined"):
        return {"present": True, "kind": "mined",
                "precedents": len(data.get("precedents", [])),
                "based_on": (data.get("delivery_shape") or {}).get("based_on", 0)}
    if data.get("skipped") and str(data.get("reason", "")).strip():
        return {"present": True, "kind": "skipped", "reason": data["reason"]}
    return {"present": False}
