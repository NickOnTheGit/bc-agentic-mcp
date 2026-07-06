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
* vendor     — a BCQuality-style clone: ``<root>/{microsoft,community,custom}/
               knowledge/<domain>/**/*.md``. The root path comes from the
               committed policy (``vendor.root``) or the ``BC_MCP_KNOWLEDGE_ROOT``
               env var (machine-local override).

Policy (``.specs/policy/knowledge.json``, committed and reviewed like code —
same precedent as guidelines_policy): enabled kill-switch, enabled_layers,
allow/deny globs ENFORCED at walk time (we are the consumer BCQuality expects
to prune), and the vendor ``pinned_commit`` the curation applies to — the index
header records the clone's actual HEAD and flags drift.

Selection uses our existing Okapi BM25 (:func:`bc_agentic_mcp.lessons.bm25_scores`)
over title + domain + keywords + dimensions + description — a strict upgrade
over plain keyword overlap, still deterministic and dependency-free — with a
relevance floor so a single shared token never drags an article onto the worklist.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bc_agentic_mcp.lessons import bm25_scores
from bc_agentic_mcp.workspace import external_base, specs_root

SCHEMA_VERSION = 1  # BCQuality knowledge-index schema version (compatibility contract)

ENV_VENDOR_ROOT = "BC_MCP_KNOWLEDGE_ROOT"
VENDOR_LAYERS = ("microsoft", "community", "custom")  # BCQuality clone layout
REPO_LAYER = "repo"

LEAN_DESCRIPTION_MAX = 120
DEFAULT_TOP_K = 6
# Worklist noise floor: an article must score at least this fraction of the top
# hit to stay on the worklist (a single shared common token is not relevance).
RELEVANCE_FLOOR_RATIO = 0.25

# Committed, team-shared policy (mirrors guidelines_policy's .specs/policy
# precedent). The vendor root PATH may be machine-local (env override); the
# POLICY — which layers, what is allowed/denied, which commit is pinned — is
# repo-scoped and reviewed like code.
DEFAULT_POLICY: Dict[str, Any] = {
    "enabled": True,
    "vendor": {"root": "", "pinned_commit": ""},
    "enabled_layers": [],  # empty = every layer present on disk
    "allow": [],           # fnmatch globs against '<layer>/<rel>'; empty = allow all
    "deny": [],            # fnmatch globs against '<layer>/<rel>'; deny wins
}

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


def _policy_dir(project_root: Path) -> Path:
    candidates: List[Path] = [
        Path(project_root) / ".specs" / "policy",
        specs_root(project_root) / "policy",
    ]
    base = external_base()
    if base is not None:
        candidates.append(base.parent / ".specs" / "policy")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(project_root) / ".specs" / "policy"


def policy_path(project_root: Path) -> Path:
    return _policy_dir(Path(project_root).resolve()) / "knowledge.json"


def load_policy(project_root: Path) -> Dict[str, Any]:
    """Committed knowledge policy; fail-open to safe defaults on any problem."""
    default = json.loads(json.dumps(DEFAULT_POLICY))
    path = policy_path(project_root)
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(data, dict):
        return default
    default.update({k: v for k, v in data.items() if k in DEFAULT_POLICY})
    if not isinstance(default.get("vendor"), dict):
        default["vendor"] = dict(DEFAULT_POLICY["vendor"])
    return default


def vendor_root(project_root: Optional[Path] = None) -> Optional[Path]:
    """Vendor clone root: env override (machine-local path) > committed policy."""
    env = os.environ.get(ENV_VENDOR_ROOT)
    if env:
        return Path(env).expanduser()
    if project_root is not None:
        raw = str((load_policy(project_root).get("vendor") or {}).get("root") or "").strip()
        if raw:
            return Path(raw).expanduser()
    return None


def knowledge_roots(project_root: Path) -> List[Tuple[str, Path]]:
    """(layer, directory) pairs to walk, in deterministic order. Missing dirs skipped."""
    roots: List[Tuple[str, Path]] = []
    repo_root = repo_knowledge_root(project_root)
    if repo_root.is_dir():
        roots.append((REPO_LAYER, repo_root))
    vendor = vendor_root(project_root)
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


def _matches_any(scoped_rel: str, globs: List[str]) -> bool:
    """Case-insensitive fnmatch of '<layer>/<rel>' against policy globs."""
    target = scoped_rel.lower()
    return any(fnmatch.fnmatchcase(target, str(g).replace("\\", "/").lower()) for g in globs)


def _walk_articles(
    project_root: Path,
    policy: Optional[Dict[str, Any]] = None,
) -> List[Tuple[str, Path, Path, str]]:
    """Deterministic (layer, kb_root, file, rel) tuples across all configured layers.

    The committed policy is ENFORCED here (unlike the BCQuality generator, which
    delegates pruning to the consumer — we are the consumer): allow globs, then
    deny globs, matched against '<layer>/<rel>'. ``enabled: false`` empties the
    corpus without deleting any files (the curation kill-switch).
    """
    pol = policy if policy is not None else load_policy(project_root)
    if not pol.get("enabled", True):
        return []
    allow = [str(g) for g in pol.get("allow") or []]
    deny = [str(g) for g in pol.get("deny") or []]
    out: List[Tuple[str, Path, Path, str]] = []
    for layer, kb_root in knowledge_roots(project_root):
        for f in sorted(kb_root.rglob("*.md")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(kb_root)).replace("\\", "/")
            scoped = f"{layer}/{rel}"
            if allow and not _matches_any(scoped, allow):
                continue
            if deny and _matches_any(scoped, deny):
                continue
            out.append((layer, kb_root, f, rel))
    return out


def _fingerprint(project_root: Path, files: List[Tuple[str, Path, Path, str]]) -> str:
    """Stat-only staleness key over the source corpus AND the policy file
    (a policy edit must invalidate the cache even when no article changed)."""
    h = hashlib.sha1()
    ppath = policy_path(project_root)
    try:
        pst = ppath.stat()
        h.update(f"policy|{ppath}|{pst.st_mtime_ns}|{pst.st_size}\n".encode("utf-8"))
    except OSError:
        h.update(b"policy|absent\n")
    for layer, _kb_root, f, rel in files:
        try:
            st = f.stat()
            h.update(f"{layer}|{rel}|{f}|{st.st_mtime_ns}|{st.st_size}\n".encode("utf-8"))
        except OSError:
            h.update(f"{layer}|{rel}|{f}|gone\n".encode("utf-8"))
    return h.hexdigest()


def _vendor_commit(root: Optional[Path]) -> str:
    """HEAD SHA of the vendor clone for provenance; '' when unknown (fail-open)."""
    if root is None or not root.exists():
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def build_knowledge_index(
    project_root: Path,
    *,
    enabled_layers: Optional[List[str]] = None,
    full: bool = False,
) -> Dict[str, Any]:
    """Walk the corpus and write the discovery artifact. Returns the index dict.

    The committed policy governs the walk (allow/deny globs enforced, layers
    from ``enabled_layers`` policy key unless the parameter overrides it) and
    is recorded in the header. ``full=True`` keeps verbatim descriptions and
    pretty-prints; the default is the lean variant.
    """
    root = Path(project_root).resolve()
    policy = load_policy(root)
    files = _walk_articles(root, policy)
    effective_layers = (
        [str(l) for l in enabled_layers]
        if enabled_layers
        else [str(l) for l in policy.get("enabled_layers") or []]
    )
    if effective_layers:
        allowed = {l.lower() for l in effective_layers}
        files = [t for t in files if t[0].lower() in allowed]

    articles: List[Dict[str, Any]] = []
    for layer, _kb_root, f, rel in files:
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
        "enabledLayers": effective_layers,
        "knowledgeAllow": [str(g) for g in policy.get("allow") or []],
        "knowledgeDeny": [str(g) for g in policy.get("deny") or []],
        "articleCount": len(articles),
        "articles": articles,
        "fingerprint": _fingerprint(root, files),  # local extension: stat-only staleness key
    }
    # Vendor provenance: which content the index was built from, and whether it
    # drifted from the commit the team reviewed (curation is per-commit).
    vendor_commit = _vendor_commit(vendor_root(root))
    pinned = str((policy.get("vendor") or {}).get("pinned_commit") or "").strip()
    if vendor_commit:
        index["vendorCommit"] = vendor_commit
    if pinned:
        index["pinnedCommit"] = pinned
        index["vendorDrift"] = bool(vendor_commit) and vendor_commit != pinned
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if full:
        path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    else:
        path.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    return index


def load_knowledge_index(project_root: Path) -> Dict[str, Any]:
    """Load the cached index; rebuild only when the corpus OR policy changed."""
    root = Path(project_root).resolve()
    path = index_path(root)
    current = _fingerprint(root, _walk_articles(root))
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
    if not ranked:
        return []
    # Relevance floor: one shared common token is not relevance. Keep only
    # articles scoring a meaningful fraction of the best hit.
    floor = ranked[0]["score"] * RELEVANCE_FLOOR_RATIO
    return [a for a in ranked if a["score"] >= floor][:max(0, top_k)]


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
