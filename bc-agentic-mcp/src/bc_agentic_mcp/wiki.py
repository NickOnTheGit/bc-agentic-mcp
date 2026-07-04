"""wiki — fetch authoritative Azure DevOps wiki content FRESH (never a stale workaround).

Root-cause rule this encodes: when a work item references a wiki, the wiki is the source of
truth for WHICH object/API to change and its naming. A local wiki clone may be stale and code
convention is only an unverified fallback. This module fetches the live page via the REST API
using a PAT from the environment (never stored, never logged). The network call is an injectable
seam so the URL/auth logic stays pure and deterministic.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Callable, Dict, Optional
from urllib import request as _urlrequest
from urllib import error as _urlerror

# dev.azure.com/{org}/{project}/_wiki/wikis/{wiki}/{pageId}/{slug}
_AZ_RE = re.compile(
    r"https?://dev\.azure\.com/(?P<org>[^/]+)/(?P<project>[^/]+)/_wiki/wikis/"
    r"(?P<wiki>[^/]+)/(?P<id>\d+)",
    re.IGNORECASE,
)
# {org}.visualstudio.com/{project}/_wiki/wikis/{wiki}/{pageId}/{slug}
_VS_RE = re.compile(
    r"https?://(?P<org>[^.]+)\.visualstudio\.com/(?P<project>[^/]+)/_wiki/wikis/"
    r"(?P<wiki>[^/]+)/(?P<id>\d+)",
    re.IGNORECASE,
)

Fetcher = Callable[[str, Dict[str, str]], "tuple[int, str]"]


def parse_wiki_url(url: str) -> Optional[Dict[str, str]]:
    """Parse an ADO wiki URL into {org_url, project, wiki, page_id}. Deterministic."""
    m = _AZ_RE.search(url or "")
    if m:
        return {
            "org_url": f"https://dev.azure.com/{m.group('org')}",
            "project": m.group("project"),
            "wiki": m.group("wiki"),
            "page_id": m.group("id"),
        }
    m = _VS_RE.search(url or "")
    if m:
        return {
            "org_url": f"https://{m.group('org')}.visualstudio.com",
            "project": m.group("project"),
            "wiki": m.group("wiki"),
            "page_id": m.group("id"),
        }
    return None


def build_rest_url(org_url: str, project: str, wiki: str, page_id: str) -> str:
    """Build the wiki page REST URL (id-based so nested paths resolve)."""
    from urllib.parse import quote
    return (
        f"{org_url.rstrip('/')}/{quote(str(project), safe='')}/_apis/wiki/wikis/{quote(str(wiki), safe='')}/pages/{page_id}"
        f"?includeContent=true&api-version=7.0"
    )


def basic_auth_header(pat: str) -> str:
    """ADO PAT auth: Basic base64(':<pat>'). The PAT is never returned or logged."""
    token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return f"Basic {token}"


def _default_fetcher(url: str, headers: Dict[str, str]):
    req = _urlrequest.Request(url, headers=headers, method="GET")
    try:
        with _urlrequest.urlopen(req, timeout=60) as resp:  # noqa: S310 (caller-provided URL)
            return resp.status, resp.read().decode("utf-8", "replace")
    except _urlerror.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""


def fetch_wiki_page(
    *,
    org_url: str,
    project: str,
    wiki: str,
    page_id: str,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
    fetcher: Optional[Fetcher] = None,
) -> Dict[str, Any]:
    """Fetch the wiki page content FRESH. Fail-closed: no PAT => refuse (no stale fallback)."""
    pat = os.environ.get(pat_env)
    if not pat and fetcher is None:
        return {
            "fetched": False,
            "reason": (
                f"{pat_env} not set — cannot fetch the authoritative wiki fresh. Do NOT fall "
                "back to a local clone or convention inference; set the PAT and retry."
            ),
        }
    url = build_rest_url(org_url, project, wiki, page_id)
    fetch = fetcher or _default_fetcher
    status, body = fetch(url, {"Authorization": basic_auth_header(pat or "")})
    if status < 200 or status >= 300:
        return {"fetched": False, "status": status, "reason": f"HTTP {status}", "body": body[:500]}
    content = ""
    try:
        content = (json.loads(body) or {}).get("content", "") if body else ""
    except json.JSONDecodeError:
        content = body
    return {
        "fetched": True,
        "status": status,
        "content": content,
        "content_length": len(content),
        "source": "rest-api-fresh",
    }


def fetch_from_url(
    url: str,
    *,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
    fetcher: Optional[Fetcher] = None,
) -> Dict[str, Any]:
    """Parse a wiki URL and fetch its content fresh."""
    parsed = parse_wiki_url(url)
    if not parsed:
        return {"fetched": False, "reason": f"not a recognizable ADO wiki URL: {url}"}
    result = fetch_wiki_page(
        org_url=parsed["org_url"], project=parsed["project"], wiki=parsed["wiki"],
        page_id=parsed["page_id"], pat_env=pat_env, fetcher=fetcher,
    )
    result["parsed"] = parsed
    return result
