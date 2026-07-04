"""bc_fetch_wiki — read an authoritative ADO wiki page FRESH (no stale-clone workaround)."""
from typing import Any, Dict, Optional

from bc_agentic_mcp import quarantine, wiki


def _quarantined(res: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Wiki text is UNTRUSTED input — fence it and surface injection findings."""
    if res.get("fetched") and isinstance(res.get("content"), str):
        q = quarantine.apply(res["content"], source)
        res["content"] = q["text"]
        if q["flags"]:
            res["quarantine"] = {"risk": q["risk"], "flags": q["flags"]}
    return res


async def handle_fetch_wiki(
    url: Optional[str] = None,
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    wiki_name: Optional[str] = None,
    page_id: Optional[str] = None,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
) -> Dict[str, Any]:
    """Fetch a wiki page's content live via the REST API using a PAT from the environment.

    Provide either a wiki ``url`` or the discrete ``org_url``/``project``/``wiki_name``/``page_id``.
    Fail-closed when the PAT is missing — it will NOT fall back to a possibly-stale local clone.
    """
    if url:
        return _quarantined(wiki.fetch_from_url(url, pat_env=pat_env), f"ado-wiki {url}")
    if org_url and project and wiki_name and page_id:
        return _quarantined(
            wiki.fetch_wiki_page(
                org_url=org_url, project=project, wiki=wiki_name, page_id=page_id, pat_env=pat_env
            ),
            f"ado-wiki-{wiki_name}#{page_id}",
        )
    return {"fetched": False, "reason": "provide 'url' or all of org_url/project/wiki_name/page_id"}
