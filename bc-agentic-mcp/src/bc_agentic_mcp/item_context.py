"""item_context — capture ALL of a work item's referenced data FRESH to disk, once.

On first contact with a work item, materialize a single durable context bundle: the item
description, every linked wiki page (fetched live), and every related work item. Planning and
implementation then reference THIS saved bundle — never re-inferring, never re-fetching from a
possibly-stale source mid-task. This is the antidote to the two failures we hit: choosing a
target from code convention instead of the wiki, and working around blockers with stale data.

Pure orchestration + deterministic on-disk layout; all network I/O is injected (seams).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Callable, Dict, List, Optional

from bc_agentic_mcp import item_references, quarantine, wiki

Fetcher = Callable[[str, Dict[str, str]], "tuple[int, str]"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-")
    return s[:60] or "item"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _html_to_text(html: str) -> str:
    """Deterministic, dependency-free HTML -> text for saving item descriptions.

    Two lossy-capture bugs fixed 2026-07-06 (user report: items with pictures):
    - <img> tags were SILENTLY DELETED — a screenshot carrying requirements (column
      layouts, field lists) vanished without a trace. Each image now leaves an
      explicit inline marker so nothing embedded is ever invisible.
    - table cells lost their boundaries (</td>/</th> unhandled): 'Col A|Col B'
      became 'Col ACol B' — columns not where they should be, literally.
    """
    t = html or ""
    # Images FIRST (before the generic tag strip destroys them): inline marker with
    # the filename/alt when available — the download step saves the bytes next door.
    idx = 0
    def _img_marker(m: "re.Match") -> str:
        nonlocal idx
        idx += 1
        tag = m.group(0)
        alt = re.search(r'alt="([^"]*)"', tag, re.IGNORECASE)
        src = re.search(r'src="([^"]*)"', tag, re.IGNORECASE)
        label = (alt.group(1).strip() if alt else "") or _attachment_name(src.group(1) if src else "")
        return f"\n[IMAGE {idx}{': ' + label if label else ''} — saved to context/images/, MUST be viewed and transcribed]\n"
    t = re.sub(r"<img\b[^>]*>", _img_marker, t, flags=re.IGNORECASE)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.IGNORECASE)
    # Cell boundaries become explicit separators; row/blocks become newlines.
    t = re.sub(r"</(td|th)>", " | ", t, flags=re.IGNORECASE)
    t = re.sub(r"</(p|div|li|ul|ol|tr|table|h\d)>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"[ \t]*\|[ \t]*\n", "\n", t)  # trailing cell separator at row end
    return _html.unescape(t).strip()


_ATTACHMENT_URL_RE = re.compile(
    r'src="(https?://[^"]*/_apis/wit/attachments/[0-9a-fA-F-]+[^"]*)"', re.IGNORECASE)


def _attachment_name(src: str) -> str:
    """Human file name from an ADO attachment URL's fileName= query arg."""
    m = re.search(r"[?&]fileName=([^&]+)", src or "", re.IGNORECASE)
    if not m:
        return ""
    from urllib.parse import unquote
    return unquote(m.group(1))


def extract_image_urls(html: str) -> List[Dict[str, str]]:
    """Ordered, de-duplicated ADO attachment image URLs embedded in HTML."""
    out: List[Dict[str, str]] = []
    seen: set = set()
    for m in _ATTACHMENT_URL_RE.finditer(html or ""):
        url = _html.unescape(m.group(1))
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "name": _attachment_name(url)})
    return out


BinaryFetcher = Callable[[str, Dict[str, str]], "tuple[int, bytes]"]


def _default_binary_fetcher(url: str, headers: Dict[str, str]) -> "tuple[int, bytes]":
    from urllib import error as _urlerror
    from urllib import request as _urlrequest
    req = _urlrequest.Request(url, headers=headers, method="GET")
    try:
        with _urlrequest.urlopen(req, timeout=60) as resp:  # noqa: S310 (ADO attachment URL)
            return resp.status, resp.read()
    except _urlerror.HTTPError as exc:
        return exc.code, b""
    except Exception:
        return 0, b""


def download_images(
    cdir: Path,
    images: List[Dict[str, str]],
    *,
    source: str,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
    binary_fetcher: Optional[BinaryFetcher] = None,
) -> "tuple[List[Dict[str, Any]], List[Dict[str, Any]]]":
    """Materialize embedded images into context/images/ (disk is truth).

    Returns (saved, unresolved). Never raises; a failed download is recorded as
    unresolved so it can never be silently skipped.
    """
    saved: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    if not images:
        return saved, unresolved
    pat = os.environ.get(pat_env)
    fetch = binary_fetcher or (_default_binary_fetcher if pat else None)
    (cdir / "images").mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(images, start=1):
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", img.get("name") or "") or "attachment.png"
        rel = f"images/{source}-{i:02d}-{name}"
        if fetch is None:
            unresolved.append({"kind": "image", "url": img["url"], "source": source,
                               "reason": f"{pat_env} not set — image bytes not downloadable"})
            continue
        status, blob = fetch(img["url"], {"Authorization": wiki.basic_auth_header(pat or "")})
        if status < 200 or status >= 300 or not blob:
            unresolved.append({"kind": "image", "url": img["url"], "source": source,
                               "reason": f"download failed (HTTP {status})"})
            continue
        (cdir / rel).write_bytes(blob)
        saved.append({"kind": "image", "path": rel, "index": i, "source": source,
                      "name": img.get("name") or name, "bytes": len(blob),
                      "sha": hashlib.sha256(blob).hexdigest()[:16]})
    return saved, unresolved


def context_dir(root: Path, spec_name: str) -> Path:
    return specs_root(root) / spec_name / "context"


IMAGE_QUESTION_PREFIX = "Q-95"  # reserved band: image-transcription questions


def _write_image_questions(sdir: Path, spec_name: str, image_files: List[Dict[str, Any]]) -> None:
    """Append one blocking clarification question per captured image (idempotent)."""
    clar = sdir / "clarifications.md"
    existing = clar.read_text(encoding="utf-8", errors="replace") if clar.exists() else ""
    blocks: List[str] = []
    for i, img in enumerate(image_files, start=1):
        qid = f"Q-{950 + i:03d}"
        if qid in existing:
            continue  # already asked (re-capture must not duplicate)
        blocks.append(
            f"## {qid}: [IMAGE] {img.get('name', img['path'])} — view and transcribe before the spec\n"
            f"Embedded image saved to `context/{img['path']}` (source: {img.get('source', 'item')}).\n"
            "Screenshots carry REAL requirements — column layouts, field lists, captions, values.\n"
            "VIEW the saved file and answer with: (1) exactly what it demands — column names in\n"
            "order, field captions, layout — and (2) the target .al object/file it maps to.\n"
            "Only a seeing agent or a human may answer this; it is never auto-answerable.\n"
            "_Answer:_ \n"
        )
    if not blocks:
        return
    if not existing:
        existing = f"# Clarifications for: {spec_name}\n\nReview these questions before implementation.\n\n"
    if not existing.endswith("\n\n"):
        existing = existing.rstrip("\n") + "\n\n"
    clar.write_text(existing + "\n".join(blocks), encoding="utf-8")


def build_work_item_rest_url(org_url: str, project: str, item_id: str) -> str:
    # Path segments URL-encoded: ADO project names may contain spaces — the exact
    # class that broke PR creation live ('ERP AL', Bug 267600).
    from urllib.parse import quote
    return (
        f"{org_url.rstrip('/')}/{quote(str(project), safe='')}/_apis/wit/workitems/{item_id}"
        f"?$expand=all&api-version=7.0"
    )


def build_comments_rest_url(org_url: str, project: str, item_id: str) -> str:
    from urllib.parse import quote
    return (
        f"{org_url.rstrip('/')}/{quote(str(project), safe='')}/_apis/wit/workItems/{item_id}/comments"
        f"?api-version=7.0-preview.4"
    )


def fetch_comments(
    *, org_url: str, project: str, item_id: str,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT", fetcher: Optional[Fetcher] = None,
) -> Dict[str, Any]:
    """Fetch a work item's comments FRESH via REST+PAT (they often hold decisive decisions)."""
    pat = os.environ.get(pat_env)
    if not pat and fetcher is None:
        return {"fetched": False, "reason": f"{pat_env} not set"}
    url = build_comments_rest_url(org_url, project, item_id)
    fetch = fetcher or wiki._default_fetcher
    status, body = fetch(url, {"Authorization": wiki.basic_auth_header(pat or "")})
    if status < 200 or status >= 300:
        return {"fetched": False, "status": status, "reason": f"HTTP {status}"}
    data = json.loads(body) if body else {}
    comments = [
        {
            "author": (c.get("createdBy") or {}).get("displayName", ""),
            "date": c.get("createdDate", ""),
            "text": _html_to_text(c.get("text", "")),
            "images": extract_image_urls(c.get("text", "")),
        }
        for c in (data.get("comments", []) or [])
    ]
    return {"fetched": True, "comments": comments}


def fetch_work_item(
    *, org_url: str, project: str, item_id: str,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT", fetcher: Optional[Fetcher] = None,
) -> Dict[str, Any]:
    """Fetch a work item FRESH via REST+PAT. Fail-closed with no PAT (no stale fallback)."""
    pat = os.environ.get(pat_env)
    if not pat and fetcher is None:
        return {"fetched": False, "reason": f"{pat_env} not set — cannot fetch item fresh"}
    url = build_work_item_rest_url(org_url, project, item_id)
    fetch = fetcher or wiki._default_fetcher
    status, body = fetch(url, {"Authorization": wiki.basic_auth_header(pat or "")})
    if status < 200 or status >= 300:
        return {"fetched": False, "status": status, "reason": f"HTTP {status}"}
    data = json.loads(body) if body else {}
    fields = data.get("fields", {}) or {}
    desc_html = fields.get("System.Description", "") or ""
    # Bugs carry their substance in the TCM fields — System.Description is often empty.
    # Compose the full narrative so capture never saves an empty description for a bug.
    repro_html = fields.get("Microsoft.VSTS.TCM.ReproSteps", "") or ""
    sysinfo_html = fields.get("Microsoft.VSTS.TCM.SystemInfo", "") or ""
    description = _html_to_text(desc_html)
    if repro_html:
        description = (description + "\n\n## Repro Steps\n" + _html_to_text(repro_html)).strip()
    if sysinfo_html:
        description = (description + "\n\n## System Info\n" + _html_to_text(sysinfo_html)).strip()
    related_ids = []
    for rel in data.get("relations", []) or []:
        m = re.search(r"/workItems/(\d+)$", str(rel.get("url", "")))
        if m:
            related_ids.append(m.group(1))
    return {
        "fetched": True,
        "item_id": str(item_id),
        "title": fields.get("System.Title", ""),
        "description": description,
        "description_html": desc_html,
        # Embedded pictures carry REQUIREMENTS (column layouts, field lists) —
        # collected from every narrative field so capture can materialize them.
        "images": extract_image_urls(desc_html + "\n" + repro_html + "\n" + sysinfo_html),
        "related_ids": sorted(set(related_ids), key=lambda x: int(x)),
        "fields": fields,
    }


def fetch_ancestry(
    *, org_url: str, project: str, item_id: str,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT", fetcher: Optional[Fetcher] = None,
    max_depth: int = 6,
) -> List[str]:
    """Walk the parent chain (item -> parent -> feature -> epic) and return the ancestor ids."""
    chain: List[str] = []
    seen = {str(item_id)}
    current = str(item_id)
    for _ in range(max_depth):
        wi = fetch_work_item(org_url=org_url, project=project, item_id=current,
                             pat_env=pat_env, fetcher=fetcher)
        if not wi.get("fetched"):
            break
        parent = wi.get("fields", {}).get("System.Parent")
        if not parent:
            break
        parent = str(parent)
        if parent in seen:
            break
        seen.add(parent)
        chain.append(parent)
        current = parent
    return chain


def capture(
    root: str,
    spec_name: str,
    *,
    item_id: str,
    description: str,
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
    wiki_fetcher: Optional[Fetcher] = None,
    related_fetcher: Optional[Fetcher] = None,
    extra_related_ids: Optional[List[str]] = None,
    comments: Optional[List[Dict[str, Any]]] = None,
    identity: Optional[Dict[str, Any]] = None,
    images: Optional[List[Dict[str, str]]] = None,
    binary_fetcher: Optional["BinaryFetcher"] = None,
) -> Dict[str, Any]:
    """Materialize the item's full fresh context to ``.specs/<spec>/context/``.

    Writes the description, each linked wiki page (fetched live) and each related work item,
    plus a manifest listing every artifact with its source + content hash. ``unresolved``
    records anything that could not be fetched fresh (so it is never silently skipped).
    """
    cdir = context_dir(Path(root).resolve(), spec_name)
    (cdir / "wiki").mkdir(parents=True, exist_ok=True)
    (cdir / "related").mkdir(parents=True, exist_ok=True)

    refs = item_references.extract_references(description)
    files: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    findings_by_file: Dict[str, List[Dict[str, Any]]] = {}

    # 1) The item description itself — UNTRUSTED: fenced + scanned (indirect
    # prompt injection via ticket text is this system's main untrusted input).
    desc_path = cdir / f"item-{item_id}.md"
    desc_q = quarantine.apply(description or "", f"ado-workitem-{item_id}")
    desc_path.write_text(desc_q["text"], encoding="utf-8")
    findings_by_file[str(desc_path.relative_to(cdir))] = desc_q["flags"]
    files.append({"kind": "item", "path": str(desc_path.relative_to(cdir)),
                  "sha": _sha(description), "source": "provided"})

    # 1b) Embedded PICTURES — requirements often live ONLY in screenshots (column
    # layouts, field lists). Materialize the bytes into the bundle (disk is truth);
    # a failed download lands in `unresolved`, never silently skipped. The saved
    # files MUST then be viewed + transcribed — the spec gate enforces that.
    image_files: List[Dict[str, Any]] = []
    desc_images = list(images or [])
    saved_imgs, missed_imgs = download_images(
        cdir, desc_images, source="item", pat_env=pat_env, binary_fetcher=binary_fetcher)
    image_files.extend(saved_imgs)
    unresolved.extend(missed_imgs)
    comment_images: List[Dict[str, str]] = []
    for c in (comments or []):
        comment_images.extend(c.get("images") or [])
    saved_cimgs, missed_cimgs = download_images(
        cdir, comment_images, source="comment", pat_env=pat_env, binary_fetcher=binary_fetcher)
    image_files.extend(saved_cimgs)
    unresolved.extend(missed_cimgs)
    files.extend(image_files)

    # IMAGE OBLIGATION (user report 2026-07-06): a screenshot's requirements
    # (columns, fields, layout) must never be silently skipped. Each saved image
    # becomes a BLOCKING clarification question (Q-95x) in the standard grammar —
    # the existing clarifications engine then refuses to spec the item until a
    # SEEING agent (or human) transcribes every picture. Q-95x is excluded from
    # auto-answering: text matching cannot read pixels.
    if image_files:
        _write_image_questions(cdir.parent, spec_name, image_files)

    # 1c) The item's comments — often hold the decisive decision (fetched FRESH).
    if comments:
        cbody = "\n\n".join(
            f"**{c.get('author', '')}** ({c.get('date', '')}):\n{c.get('text', '')}"
            for c in comments
        )
        cpath = cdir / f"comments-{item_id}.md"
        comments_q = quarantine.apply(cbody, f"ado-comments-{item_id}")
        cpath.write_text(comments_q["text"], encoding="utf-8")
        findings_by_file[str(cpath.relative_to(cdir))] = comments_q["flags"]
        files.append({"kind": "comments", "path": str(cpath.relative_to(cdir)),
                      "sha": _sha(cbody), "source": "rest-api-fresh", "count": len(comments)})

    # 2) Every linked wiki page — fetched FRESH.
    for w in refs["wiki_links"]:
        org = org_url or (f"https://dev.azure.com/{w['wiki'].split('.')[0]}" if not org_url else org_url)
        res = wiki.fetch_wiki_page(
            org_url=org_url or org, project=project or "", wiki=w["wiki"],
            page_id=w["page_id"], pat_env=pat_env, fetcher=wiki_fetcher,
        )
        if res.get("fetched"):
            wpath = cdir / "wiki" / f"{w['page_id']}_{_slug(w['slug'])}.md"
            wiki_q = quarantine.apply(res["content"], f"ado-wiki-{w['wiki']}#{w['page_id']}")
            wpath.write_text(wiki_q["text"], encoding="utf-8")
            findings_by_file[str(wpath.relative_to(cdir))] = wiki_q["flags"]
            files.append({"kind": "wiki", "path": str(wpath.relative_to(cdir)),
                          "page_id": w["page_id"], "sha": _sha(res["content"]),
                          "source": "rest-api-fresh"})
        else:
            unresolved.append({"kind": "wiki", "page_id": w["page_id"],
                               "reason": res.get("reason", "fetch failed")})

    # 3) Every related work item — fetched FRESH.
    related_ids = sorted(set(list(refs["related_work_items"]) + list(extra_related_ids or [])),
                         key=lambda x: int(x))
    for rid in related_ids:
        if related_fetcher is None and not os.environ.get(pat_env):
            unresolved.append({"kind": "related", "item_id": rid, "reason": "no fetcher/PAT"})
            continue
        wi = fetch_work_item(org_url=org_url or "", project=project or "", item_id=rid,
                             pat_env=pat_env, fetcher=related_fetcher)
        if wi.get("fetched"):
            rpath = cdir / "related" / f"{rid}.md"
            body = f"# {wi.get('title', '')} (#{rid})\n\n{wi.get('description', '')}"
            rel_q = quarantine.apply(body, f"ado-workitem-{rid}")
            rpath.write_text(rel_q["text"], encoding="utf-8")
            findings_by_file[str(rpath.relative_to(cdir))] = rel_q["flags"]
            files.append({"kind": "related", "path": str(rpath.relative_to(cdir)),
                          "item_id": rid, "sha": _sha(body), "source": "rest-api-fresh"})
        else:
            unresolved.append({"kind": "related", "item_id": rid,
                               "reason": wi.get("reason", "fetch failed")})

    manifest = {
        "spec_name": spec_name,
        "item_id": str(item_id),
        # Explicit identity: WHAT KIND of work item this is and where it hangs in the
        # tree — a human reading any downstream artifact must never have to guess
        # whether "the feature" means the ADO Feature, this PBI, or a BC Empire Feature.
        "identity": identity or {},
        "captured_at": _now(),
        "references": refs,
        "files": files,
        # First-class image ledger: every embedded picture with its saved path.
        # Downstream gates key off this — an item with images may not be specced
        # until each one is viewed and transcribed (they often carry the columns).
        "images": image_files,
        "unresolved": unresolved,
        "complete": not unresolved,
        # Injection defense verdict: captured text is DATA, never instructions.
        "quarantine": quarantine.summarize(findings_by_file),
    }
    (cdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_context(root: str, spec_name: str) -> Optional[Dict[str, Any]]:
    """Read back the saved context manifest (the single source of truth for the item)."""
    mpath = context_dir(Path(root).resolve(), spec_name) / "manifest.json"
    if not mpath.exists():
        return None
    try:
        return json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def lane(root: str, spec_name: str) -> str:
    """The item's delivery lane, derived from CAPTURED identity (never guessed).

    'bugfix' when the captured work item type is Bug (or identity.lane says so);
    'pbi' otherwise — including when no context was captured yet (default lane).
    """
    ctx = load_context(root, spec_name)
    identity = (ctx or {}).get("identity") or {}
    explicit = str(identity.get("lane", "")).strip().lower()
    if explicit:
        return explicit
    return "bugfix" if str(identity.get("type", "")).strip().lower() == "bug" else "pbi"


def context_source(root: str, spec_name: str) -> Optional[Dict[str, Any]]:
    """Assemble the authoritative requirement text (+ hash) from the saved bundle.

    This is what the PLANNER reads as its source of truth — description + comments + related,
    exactly as validated and saved to disk, never a paraphrase or a re-fetch.
    """
    root_p = Path(root).resolve()
    cdir = context_dir(root_p, spec_name)
    manifest = load_context(root, spec_name)
    if not manifest:
        return None
    parts: List[str] = []
    for f in manifest.get("files", []):
        p = cdir / f.get("path", "")
        if p.exists():
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    text = "\n\n".join(parts).strip()
    if not text:
        return None
    return {"text": text, "sha": _sha(text)}
