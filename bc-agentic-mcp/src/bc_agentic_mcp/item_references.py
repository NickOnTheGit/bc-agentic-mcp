"""references — deterministically extract the references a work item points to.

A work item's description often links the authoritative spec (a wiki page) and related
items. Those references decide WHICH surface to change; codebase-convention inference must
never override them. This module surfaces them so they cannot be silently skipped.

Pure and reproducible: same text in, same references out.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# Azure DevOps wiki page URL: /_wiki/wikis/<wiki>/<pageId>/<slug>
_WIKI_RE = re.compile(
    r"/_wiki/wikis/(?P<wiki>[^/\s)\"']+)/(?P<id>\d+)/(?P<slug>[^\s)\"'<]+)",
    re.IGNORECASE,
)
# Related work items: explicit ADO edit URLs and bare #123 mentions.
_WI_URL_RE = re.compile(r"/(?:_workitems/edit|workItems)/(?P<id>\d+)", re.IGNORECASE)
_WI_HASH_RE = re.compile(r"(?<![\w/])#(?P<id>\d{3,})\b")
# Any other absolute link (docs, attachments, external specs).
_URL_RE = re.compile(r"https?://[^\s)\"'<>]+", re.IGNORECASE)


def extract_references(text: str) -> Dict[str, Any]:
    """Return the wiki pages, related work items and URLs referenced in ``text``."""
    text = text or ""
    wiki_links: List[Dict[str, str]] = []
    seen_wiki = set()
    for m in _WIKI_RE.finditer(text):
        key = (m.group("wiki"), m.group("id"))
        if key in seen_wiki:
            continue
        seen_wiki.add(key)
        wiki_links.append({
            "wiki": m.group("wiki"),
            "page_id": m.group("id"),
            "slug": m.group("slug"),
        })

    related: set = set()
    for m in _WI_URL_RE.finditer(text):
        related.add(m.group("id"))
    for m in _WI_HASH_RE.finditer(text):
        related.add(m.group("id"))

    urls = sorted({u.rstrip(".,);") for u in _URL_RE.findall(text)})
    # Wiki links are already counted separately; keep 'urls' for the non-wiki remainder.
    non_wiki_urls = [u for u in urls if "/_wiki/wikis/" not in u.lower()]

    wiki_sorted = sorted(wiki_links, key=lambda w: (w["wiki"], w["page_id"]))
    related_sorted = sorted(related, key=lambda x: int(x))
    return {
        "wiki_links": wiki_sorted,
        "related_work_items": related_sorted,
        "urls": non_wiki_urls,
        "reference_count": len(wiki_sorted) + len(related_sorted) + len(non_wiki_urls),
        "has_references": bool(wiki_sorted or related_sorted or non_wiki_urls),
    }


def render_reference_checklist(refs: Dict[str, Any]) -> str:
    """A must-read checklist so the item's own references drive target selection."""
    if not refs.get("has_references"):
        return "No wiki/related-item references found in the work item text."
    lines = ["The work item references the following — consult them BEFORE choosing the",
             "target object/API; they are authoritative over codebase-convention inference:", ""]
    for w in refs["wiki_links"]:
        lines.append(f"- [ ] WIKI: {w['slug']} (page {w['page_id']}, wiki {w['wiki']})")
    for wi in refs["related_work_items"]:
        lines.append(f"- [ ] RELATED WORK ITEM: #{wi}")
    for u in refs["urls"]:
        lines.append(f"- [ ] LINK: {u}")
    return "\n".join(lines)
