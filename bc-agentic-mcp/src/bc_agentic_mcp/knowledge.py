"""knowledge — layered markdown knowledge corpus + lean discovery index.

Adopts the BCQuality knowledge-index contract (microsoft/BCQuality
``tools/Build-KnowledgeIndex.ps1``) as a Python module:

* Articles are markdown files with YAML-ish frontmatter (domain, bc-version,
  technologies, countries, application-area, keywords), an H1 title, a
  ``## Description`` section (the retrieval target), and normative
  ``## Best Practice`` / ``## Anti Pattern`` rule bodies.
* ONE compact JSON discovery artifact (``.specs/.index/knowledge.json``,
  schema ``version: 1`` — BCQuality-compatible) lets a consumer enumerate
  candidate articles and compute worklist overlap without opening every file.
* Two-phase retrieval: the index substitutes ONLY for frontmatter +
  Description. A consumer opens each worklisted article IN FULL for its
  normative rule bodies — those are deliberately NOT in the index.
* LEAN by default: descriptions trimmed to a one-line hint (first sentence,
  <= 120 chars) and compact JSON, so the index prefix an agent replays across
  passes stays small.
* Fail-open: an unparseable file is still listed (``parsed: false`` with a
  domain derived from its path) so it is never silently dropped — consumers
  fall back to reading it in full.

Layers (walked in order; later layers do not shadow earlier ones — every
article is indexed with its layer recorded):

* ``repo``   — ``<specs_root>/knowledge/<domain>/**/*.md`` (repo-local articles,
               e.g. confirmed lessons graduated to prose).
* vendor     — a BCQuality-style clone pointed at by ``BC_MCP_KNOWLEDGE_ROOT``:
               ``<root>/{microsoft,community,custom}/knowledge/<domain>/**/*.md``.

Selection uses our existing Okapi BM25 (:func:`bc_agentic_mcp.lessons.bm25_scores`)
over title + domain + keywords + dimensions + description — a strict upgrade
over plain keyword overlap, still deterministic and dependency-free.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bc_agentic_mcp.lessons import bm25_scores
from bc_agentic_mcp.workspace import specs_root

SCHEMA_VERSION = 1  # BCQuality knowledge-index schema version (compatibility contract)

ENV_VENDOR_ROOT = "BC_MCP_KNOWLEDGE_ROOT"
VENDOR_LAYERS = ("microsoft", "community", "custom")  # BCQuality clone layout
REPO_LAYER = "repo"

LEAN_DESCRIPTION_MAX = 120
DEFAULT_TOP_K = 6

_FRONTMATTER_KEY_RE = re.compile(r"^\s*([a-zA-Z][\w-]*)\s*:\s*(.*)$")
_LIST_VALUE_RE = re.compile(r"^\[(.*)\]$")
_H1_RE = re.compile(r"^\#\s+(.+?)\s*$")
_DESCRIPTION_HEADER_RE = re.compile(r"^\#\#\s+Description\s*$")
_SECTION_HEADER_RE = re.compile(r"^\#\#\s")
_FIRST_SENTENCE_RE = re.compile(r"^(.*?[\.!?])(\s|$)")

# Frontmatter list dimensions carried verbatim into the index (BCQuality parity).
_DIMENSION_KEYS = ("bc-version", "technologies", "countries", "application-area", "keywords")


def index_path(project_root: Path) -> Path:
    return specs_root(Path(project_root).resolve()) / ".index" / "knowledge.json"


def repo_knowledge_root(project_root: Path) -> Path:
    return specs_root(Path(project_root).resolve()) / "knowledge"


def vendor_root() -> Optional[Path]:
    env = os.environ.get(ENV_VENDOR_ROOT)
    return Path(env).expanduser() if env else None


def knowledge_roots(project_root: Path) -> List[Tuple[str, Path]]:
    """(layer, directory) pairs to walk, in deterministic order. Missing dirs skipped."""
    roots: List[Tuple[str, Path]] = []
    repo_root = repo_knowledge_root(project_root)
    if repo_root.is_dir():
        roots.append((REPO_LAYER, repo_root))
    vendor = vendor_root()
    if vendor is not None:
        for layer in VENDOR_LAYERS:
            kb = vendor / layer / "knowledge"
            if kb.is_dir():
                roots.append((layer, kb))
    return roots


def lean_description(text: str, max_len: int = LEAN_DESCRIPTION_MAX) -> str:
    """First sentence, truncated on a word boundary (port of Get-LeanDescription)."""
    if not text or not text.strip():
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    m = _FIRST_SENTENCE_RE.match(t)
    if m:
        t = m.group(1).strip()
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    sp = cut.rfind(" ")
    if sp > 40:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _as_list(value: Any) -> List[str]:
    """PowerShell ``@($x)`` semantics: missing -> [], scalar -> [scalar], list -> list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def parse_article(path: Path) -> Optional[Dict[str, Any]]:
    """Parse frontmatter dimensions + H1 title + verbatim ``## Description``.

    Returns ``None`` for files without a leading ``---`` frontmatter block
    (the fail-open caller lists them as ``parsed: false``). Normative rule
    bodies (## Best Practice / ## Anti Pattern) are deliberately NOT read —
    the index substitutes only for frontmatter + Description.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    fm_end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break
    if fm_end < 0:
        return None

    fm: Dict[str, Any] = {}
    for i in range(1, fm_end):
        m = _FRONTMATTER_KEY_RE.match(lines[i])
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        list_match = _LIST_VALUE_RE.match(val)
        if list_match:
            inner = list_match.group(1).strip()
            fm[key] = [p.strip() for p in inner.split(",") if p.strip()] if inner else []
        elif val:
            fm[key] = val

    title = ""
    in_description = False
    desc_buffer: List[str] = []
    for i in range(fm_end + 1, len(lines)):
        line = lines[i]
        if not title:
            h1 = _H1_RE.match(line)
            if h1:
                title = h1.group(1).strip()
                continue
        if _DESCRIPTION_HEADER_RE.match(line):
            in_description = True
            continue
        if in_description:
            if _SECTION_HEADER_RE.match(line):
                break  # next section ends the Description
            if line.strip():
                desc_buffer.append(line.strip())
    description = " ".join(desc_buffer).strip()

    parsed: Dict[str, Any] = {
        "domain": str(fm.get("domain", "")),
        "title": title,
        "description": description,
    }
    for key in _DIMENSION_KEYS:
        parsed[key] = _as_list(fm.get(key))
    return parsed


def _domain_from_path(rel: str) -> str:
    """First path segment under the layer's knowledge root (fail-open fallback)."""
    return rel.split("/", 1)[0] if "/" in rel else ""


def _walk_articles(project_root: Path) -> List[Tuple[str, Path, Path]]:
    """Deterministic (layer, kb_root, file) triples across all configured layers."""
    out: List[Tuple[str, Path, Path]] = []
    for layer, kb_root in knowledge_roots(project_root):
        for f in sorted(kb_root.rglob("*.md")):
            if f.is_file():
                out.append((layer, kb_root, f))
    return out


def _fingerprint(files: List[Tuple[str, Path, Path]]) -> str:
    """Stat-only staleness key over the source corpus (no file reads)."""
    h = hashlib.sha1()
    for layer, _kb_root, f in files:
        try:
            st = f.stat()
            h.update(f"{layer}|{f}|{st.st_mtime_ns}|{st.st_size}\n".encode("utf-8"))
        except OSError:
            h.update(f"{layer}|{f}|gone\n".encode("utf-8"))
    return h.hexdigest()


def build_knowledge_index(
    project_root: Path,
    *,
    enabled_layers: Optional[List[str]] = None,
    full: bool = False,
) -> Dict[str, Any]:
    """Walk the corpus and write the discovery artifact. Returns the index dict.

    ``enabled_layers`` restricts the walk (recorded in the header for
    provenance, per the BCQuality contract). ``full=True`` keeps verbatim
    descriptions and pretty-prints; the default is the lean variant.
    """
    root = Path(project_root).resolve()
    files = _walk_articles(root)
    if enabled_layers:
        allowed = {l.lower() for l in enabled_layers}
        files = [t for t in files if t[0].lower() in allowed]

    articles: List[Dict[str, Any]] = []
    for layer, kb_root, f in files:
        rel = str(f.relative_to(kb_root)).replace("\\", "/")
        try:
            parsed = parse_article(f)
        except Exception:  # noqa: BLE001 — fail-open by contract
            parsed = None
        if parsed is None:
            # Never silently dropped: listed with parsed=false so consumers
            # know it exists and fall back to reading it in full.
            articles.append({
                "path": rel, "layer": layer, "domain": _domain_from_path(rel),
                "bc-version": [], "technologies": [], "countries": [],
                "application-area": [], "keywords": [], "title": "",
                "description": "", "parsed": False, "file": str(f),
            })
            continue
        articles.append({
            "path": rel,
            "layer": layer,
            "domain": parsed["domain"] or _domain_from_path(rel),
            "bc-version": parsed["bc-version"],
            "technologies": parsed["technologies"],
            "countries": parsed["countries"],
            "application-area": parsed["application-area"],
            "keywords": parsed["keywords"],
            "title": parsed["title"],
            "description": parsed["description"] if full else lean_description(parsed["description"]),
            "parsed": True,
            "file": str(f),
        })

    index: Dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "enabledLayers": list(enabled_layers or []),
        "knowledgeAllow": [],
        "knowledgeDeny": [],
        "articleCount": len(articles),
        "articles": articles,
        "fingerprint": _fingerprint(files),  # local extension: stat-only staleness key
    }
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if full:
        path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    else:
        path.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    return index


def load_knowledge_index(project_root: Path) -> Dict[str, Any]:
    """Load the cached index; rebuild only when the source corpus changed."""
    root = Path(project_root).resolve()
    path = index_path(root)
    current = _fingerprint(_walk_articles(root))
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(cached, dict)
                    and cached.get("version") == SCHEMA_VERSION
                    and cached.get("fingerprint") == current):
                return cached
        except (OSError, json.JSONDecodeError):
            pass
    return build_knowledge_index(root)


def _article_doc(article: Dict[str, Any]) -> str:
    """The BM25 document for one article: selection inputs only (lossless set)."""
    parts = [
        article.get("title", ""),
        article.get("domain", ""),
        article.get("path", ""),
        " ".join(article.get("keywords") or []),
        " ".join(article.get("technologies") or []),
        " ".join(article.get("application-area") or []),
        article.get("description", ""),
    ]
    return " ".join(p for p in parts if p)


def select_articles(
    project_root: Path,
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> List[Dict[str, Any]]:
    """Rank the corpus against ``query`` and return the worklist (score > 0).

    Each entry carries the discovery fields + ``file`` (absolute) so a
    consumer can open the article in full for its normative rule bodies.
    Empty corpus or empty query -> [] (fail-open, never an error).
    """
    if not query or not query.strip():
        return []
    index = load_knowledge_index(project_root)
    articles = index.get("articles") or []
    if not articles:
        return []
    scores = bm25_scores(query, [_article_doc(a) for a in articles])
    ranked = sorted(
        (dict(a, score=s) for a, s in zip(articles, scores) if s > 0),
        key=lambda a: (-a["score"], a.get("path", "")),
    )
    return ranked[:max(0, top_k)]


def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "article"


def graduate_lesson_to_article(
    project_root: Path,
    lesson: Dict[str, Any],
    *,
    domain: str = "lessons",
    anti_pattern: str = "",
) -> Dict[str, Any]:
    """Second promotion tier: a confirmed JSON lesson graduates into a repo-layer
    knowledge article (markdown with Best Practice / Anti Pattern bodies), making
    the corpus self-growing. Returns {path, created} — existing articles are not
    overwritten (idempotent by slug)."""
    root = Path(project_root).resolve()
    message = str(lesson.get("message") or "").strip()
    if not message:
        return {"created": False, "reason": "lesson has no message"}
    lesson_id = str(lesson.get("id") or "lesson")
    title = lean_description(message, max_len=80).rstrip("…").rstrip(".") or lesson_id
    keywords = sorted(set(re.findall(r"[a-z0-9]{4,}", message.lower())))[:8]
    target_dir = repo_knowledge_root(root) / _slugify(domain)
    target = target_dir / f"{_slugify(f'{lesson_id}-{title}')}.md"
    if target.exists():
        return {"created": False, "path": str(target), "reason": "article already exists"}
    body = "\n".join([
        "---",
        f"domain: {_slugify(domain)}",
        "bc-version: []",
        "technologies: []",
        "countries: []",
        "application-area: []",
        f"keywords: [{', '.join(keywords)}]",
        "---",
        "",
        f"# {title}",
        "",
        "## Description",
        "",
        f"{message} (Graduated from lesson {lesson_id}; "
        f"severity {lesson.get('severity', 'warning')}, "
        f"seen {lesson.get('recurrence', 1)}x.)",
        "",
        "## Best Practice",
        "",
        message,
        "",
        "## Anti Pattern",
        "",
        anti_pattern or "The inverse of the Best Practice above (fill in from the recorded mistake).",
        "",
    ])
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    build_knowledge_index(root)  # keep the discovery artifact in lockstep
    return {"created": True, "path": str(target)}
