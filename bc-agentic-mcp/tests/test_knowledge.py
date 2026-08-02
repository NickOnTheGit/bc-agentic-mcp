"""Tests for the knowledge corpus + lean discovery index (BCQuality contract)."""
import json
import time

import pytest

from bc_agentic_mcp import knowledge, review
from bc_agentic_mcp import checkpoints as memory


@pytest.fixture
def _use_real_vendor():
    """Opt-in marker: test needs the real bundled BCQuality vendor snapshot."""


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, request):
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    monkeypatch.delenv(knowledge.ENV_VENDOR_ROOT, raising=False)
    # Isolate corpus from the bundled BCQuality vendor snapshot so tests that
    # build tiny corpora get deterministic article counts.
    # Tests that need the real bundled vendor request the _use_real_vendor fixture.
    if "_use_real_vendor" not in request.fixturenames:
        monkeypatch.setattr(knowledge, "_bundled_vendor_root", lambda: None)


ARTICLE = """---
domain: upgrade
bc-version: [26, 27]
technologies: [AL]
countries: []
application-area: [Foundation]
keywords: [DataPerCompany, upgrade codeunit, company guard]
---

# Upgrade scope must match DataPerCompany

## Description

A data-upgrade codeunit for a shared table (DataPerCompany=false) must run
per-database without a company guard. Per-company scope silently skips rows.

## Best Practice

Match the upgrade codeunit's scope to the target table's DataPerCompany.

## Anti Pattern

A per-company upgrade over a shared table.
"""


def _write_article(root, rel="upgrade/datapercompany.md", text=ARTICLE):
    path = knowledge.repo_knowledge_root(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- parsing ----------------------------------------------------------------

def test_parse_article_extracts_frontmatter_title_description(tmp_path):
    path = _write_article(tmp_path)
    parsed = knowledge.parse_article(path)
    assert parsed["domain"] == "upgrade"
    assert parsed["bc-version"] == ["26", "27"]
    assert parsed["keywords"] == ["DataPerCompany", "upgrade codeunit", "company guard"]
    assert parsed["title"] == "Upgrade scope must match DataPerCompany"
    assert parsed["description"].startswith("A data-upgrade codeunit")
    # rule bodies are deliberately NOT captured
    assert "Best Practice" not in parsed["description"]


def test_parse_article_without_frontmatter_returns_none(tmp_path):
    path = knowledge.repo_knowledge_root(tmp_path) / "d" / "plain.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Just a heading\n\nNo frontmatter.\n", encoding="utf-8")
    assert knowledge.parse_article(path) is None


def test_lean_description_first_sentence_and_truncation():
    assert knowledge.lean_description("Short one. Second sentence.") == "Short one."
    long = "word " * 60  # no sentence terminator, > 120 chars
    lean = knowledge.lean_description(long)
    assert len(lean) <= knowledge.LEAN_DESCRIPTION_MAX + 1
    assert lean.endswith("…")
    assert knowledge.lean_description("") == ""


# --- index build + fail-open ------------------------------------------------

def test_build_index_schema_and_lean_default(tmp_path):
    _write_article(tmp_path)
    index = knowledge.build_knowledge_index(tmp_path)
    assert index["version"] == knowledge.SCHEMA_VERSION
    assert index["articleCount"] == 1
    art = index["articles"][0]
    assert art["layer"] == "repo"
    assert art["path"] == "upgrade/datapercompany.md"
    assert art["parsed"] is True
    # lean: first sentence only
    assert art["description"].endswith("without a company guard.")
    assert knowledge.index_path(tmp_path).exists()


def test_build_index_full_keeps_verbatim_description(tmp_path):
    _write_article(tmp_path)
    index = knowledge.build_knowledge_index(tmp_path, full=True)
    assert "silently skips rows" in index["articles"][0]["description"]


def test_unparseable_article_listed_fail_open(tmp_path):
    _write_article(tmp_path)
    bad = knowledge.repo_knowledge_root(tmp_path) / "permissions" / "raw-notes.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("just notes, no frontmatter\n", encoding="utf-8")
    index = knowledge.build_knowledge_index(tmp_path)
    assert index["articleCount"] == 2
    unparsed = [a for a in index["articles"] if not a["parsed"]]
    assert len(unparsed) == 1
    assert unparsed[0]["path"] == "permissions/raw-notes.md"
    assert unparsed[0]["domain"] == "permissions"  # derived from path, never dropped


def test_load_index_uses_cache_until_corpus_changes(tmp_path):
    path = _write_article(tmp_path)
    first = knowledge.load_knowledge_index(tmp_path)
    again = knowledge.load_knowledge_index(tmp_path)
    assert again["generatedAt"] == first["generatedAt"]  # cache hit, no rebuild
    time.sleep(0.01)
    path.write_text(ARTICLE.replace("silently skips", "loudly skips"), encoding="utf-8")
    rebuilt = knowledge.load_knowledge_index(tmp_path)
    assert rebuilt["fingerprint"] != first["fingerprint"]


def test_vendor_layer_walked_via_env(tmp_path, monkeypatch):
    vendor = tmp_path / "bcquality-clone"
    art = vendor / "microsoft" / "knowledge" / "al" / "commit.md"
    art.parent.mkdir(parents=True)
    art.write_text(ARTICLE, encoding="utf-8")
    monkeypatch.setenv(knowledge.ENV_VENDOR_ROOT, str(vendor))
    project = tmp_path / "proj"
    project.mkdir()
    index = knowledge.build_knowledge_index(project)
    assert index["articleCount"] == 1
    assert index["articles"][0]["layer"] == "microsoft"
    assert index["articles"][0]["path"] == "al/commit.md"


def test_enabled_layers_filter_recorded_in_header(tmp_path, monkeypatch):
    _write_article(tmp_path)
    vendor = tmp_path / "clone"
    v_art = vendor / "community" / "knowledge" / "x" / "a.md"
    v_art.parent.mkdir(parents=True)
    v_art.write_text(ARTICLE, encoding="utf-8")
    monkeypatch.setenv(knowledge.ENV_VENDOR_ROOT, str(vendor))
    index = knowledge.build_knowledge_index(tmp_path, enabled_layers=["repo"])
    assert index["enabledLayers"] == ["repo"]
    assert {a["layer"] for a in index["articles"]} == {"repo"}


# --- selection (worklist) ----------------------------------------------------

def test_select_articles_ranks_by_bm25(tmp_path):
    _write_article(tmp_path)
    _write_article(tmp_path, rel="api/versioning.md", text=ARTICLE.replace(
        "domain: upgrade", "domain: api").replace(
        "DataPerCompany, upgrade codeunit, company guard", "API, versioning").replace(
        "# Upgrade scope must match DataPerCompany", "# Extend the current API version in place"))
    hits = knowledge.select_articles(tmp_path, "upgrade codeunit DataPerCompany shared table")
    assert hits
    assert hits[0]["path"] == "upgrade/datapercompany.md"
    assert hits[0]["score"] > 0
    assert hits[0]["file"]  # absolute path for phase-2 full read


def test_select_articles_empty_query_or_corpus(tmp_path):
    assert knowledge.select_articles(tmp_path, "") == []
    assert knowledge.select_articles(tmp_path, "anything") == []


# --- graduation (lesson -> article) ------------------------------------------

def test_graduate_lesson_creates_idempotent_article(tmp_path):
    lesson = {"id": "L-0007", "message": "Always seed the fixture company before install",
              "severity": "error", "recurrence": 3}
    out = knowledge.graduate_lesson_to_article(tmp_path, lesson, domain="containers")
    assert out["created"] is True
    art_path = knowledge.repo_knowledge_root(tmp_path) / "containers"
    files = list(art_path.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "## Best Practice" in text and "## Anti Pattern" in text
    assert "Graduated from lesson L-0007" in text
    # index kept in lockstep
    index = json.loads(knowledge.index_path(tmp_path).read_text(encoding="utf-8"))
    assert index["articleCount"] == 1
    # idempotent: second call does not overwrite
    again = knowledge.graduate_lesson_to_article(tmp_path, lesson, domain="containers")
    assert again["created"] is False


def test_graduated_article_is_selectable(tmp_path):
    knowledge.graduate_lesson_to_article(
        tmp_path,
        {"id": "L-1", "message": "Remove poisoned symbol cache copies before republish"},
        domain="containers",
    )
    hits = knowledge.select_articles(tmp_path, "poisoned symbol cache republish")
    assert hits and hits[0]["layer"] == "repo"


# --- review packet integration ------------------------------------------------

def test_review_packet_carries_knowledge_worklist(tmp_path):
    _write_article(tmp_path)
    memory.write_charter(tmp_path, "s1", purpose="add upgrade codeunit for shared DataPerCompany table",
                         operations={"read": True})
    packet = review.build_review_packet(tmp_path, "s1", changed_files=["UpgradeShared.Codeunit.al"])
    assert packet["knowledge"]
    assert packet["knowledge"][0]["path"] == "upgrade/datapercompany.md"
    assert "bc_get_knowledge_article" in packet["instructions"]


def test_review_packet_knowledge_empty_without_corpus(tmp_path):
    memory.write_charter(tmp_path, "s1", purpose="test", operations={})
    packet = review.build_review_packet(tmp_path, "s1", changed_files=["a.al"])
    assert packet["knowledge"] == []
    assert "bc_get_knowledge_article" not in packet["instructions"]


# --- policy (committed .specs/policy/knowledge.json) --------------------------

def _write_policy(root, **overrides):
    pol = {"enabled": True, "vendor": {"root": "", "pinned_commit": ""},
           "enabled_layers": [], "allow": [], "deny": []}
    pol.update(overrides)
    path = root / ".specs" / "policy" / "knowledge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pol), encoding="utf-8")
    return path


def _vendor_clone(base, layer="microsoft", rel="al/commit.md", text=ARTICLE):
    art = base / layer / "knowledge" / rel
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(text, encoding="utf-8")
    return base


def test_policy_deny_glob_enforced_at_walk(tmp_path):
    _write_article(tmp_path)  # upgrade/datapercompany.md
    _write_article(tmp_path, rel="appsource/marketplace.md")
    _write_policy(tmp_path, deny=["repo/appsource/**"])
    index = knowledge.build_knowledge_index(tmp_path)
    assert index["articleCount"] == 1
    assert index["articles"][0]["path"] == "upgrade/datapercompany.md"
    assert index["knowledgeDeny"] == ["repo/appsource/**"]  # recorded AND applied


def test_policy_allow_globs_restrict_walk(tmp_path):
    _write_article(tmp_path)
    _write_article(tmp_path, rel="permissions/grants.md")
    _write_policy(tmp_path, allow=["repo/upgrade/*"])
    index = knowledge.build_knowledge_index(tmp_path)
    assert [a["path"] for a in index["articles"]] == ["upgrade/datapercompany.md"]


def test_policy_enabled_false_empties_corpus(tmp_path):
    _write_article(tmp_path)
    _write_policy(tmp_path, enabled=False)
    index = knowledge.build_knowledge_index(tmp_path)
    assert index["articleCount"] == 0


def test_policy_enabled_layers_apply_without_param(tmp_path, monkeypatch):
    _write_article(tmp_path)
    clone = _vendor_clone(tmp_path / "clone")
    monkeypatch.setenv(knowledge.ENV_VENDOR_ROOT, str(clone))
    _write_policy(tmp_path, enabled_layers=["repo"])
    index = knowledge.build_knowledge_index(tmp_path)
    assert {a["layer"] for a in index["articles"]} == {"repo"}
    assert index["enabledLayers"] == ["repo"]


def test_vendor_root_from_policy_env_overrides(tmp_path, monkeypatch):
    policy_clone = _vendor_clone(tmp_path / "policy-clone")
    _write_policy(tmp_path, vendor={"root": str(policy_clone), "pinned_commit": ""})
    project = tmp_path  # policy lives in tmp_path/.specs
    index = knowledge.build_knowledge_index(project)
    assert index["articleCount"] == 1
    assert index["articles"][0]["layer"] == "microsoft"
    # env var (machine-local path) wins over the committed root
    env_clone = _vendor_clone(tmp_path / "env-clone", rel="al/other.md")
    monkeypatch.setenv(knowledge.ENV_VENDOR_ROOT, str(env_clone))
    index2 = knowledge.build_knowledge_index(project)
    assert [a["path"] for a in index2["articles"]] == ["al/other.md"]


def test_pinned_commit_drift_recorded(tmp_path, monkeypatch):
    clone = _vendor_clone(tmp_path / "clone")
    _write_policy(tmp_path, vendor={"root": str(clone), "pinned_commit": "def456"})
    monkeypatch.setattr(knowledge, "_vendor_commit", lambda root: "abc123")
    index = knowledge.build_knowledge_index(tmp_path)
    assert index["vendorCommit"] == "abc123"
    assert index["pinnedCommit"] == "def456"
    assert index["vendorDrift"] is True
    monkeypatch.setattr(knowledge, "_vendor_commit", lambda root: "def456")
    index2 = knowledge.build_knowledge_index(tmp_path)
    assert index2["vendorDrift"] is False


def test_policy_change_invalidates_cache(tmp_path):
    _write_article(tmp_path)
    _write_article(tmp_path, rel="appsource/marketplace.md")
    first = knowledge.load_knowledge_index(tmp_path)
    assert first["articleCount"] == 2
    _write_policy(tmp_path, deny=["repo/appsource/**"])  # no article file touched
    rebuilt = knowledge.load_knowledge_index(tmp_path)
    assert rebuilt["articleCount"] == 1


# --- bundled vendor root (Plan B gap 3 verification) --------------------------

def test_bundled_vendor_root_present(_use_real_vendor):
    """The package-data BCQuality snapshot must be present after vendor population."""
    root = knowledge._bundled_vendor_root()
    assert root is not None, "_bundled_vendor_root() returned None — vendor not populated"
    assert root.is_dir(), f"Bundled vendor dir does not exist: {root}"
    manifest = root / knowledge.MANIFEST_FILENAME
    assert manifest.exists(), "MANIFEST.json missing from bundled vendor"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data.get("article_count", 0) > 0


def test_vendor_root_falls_back_to_bundled(tmp_path, _use_real_vendor):
    """vendor_root() must return the bundled path when no env/policy override is set."""
    vroot = knowledge.vendor_root(tmp_path)
    bundled = knowledge._bundled_vendor_root()
    assert vroot is not None
    assert vroot == bundled


def test_vendor_commit_reads_manifest_for_bundled(_use_real_vendor):
    """_vendor_commit() must read MANIFEST.json when vendor has no .git directory."""
    bundled = knowledge._bundled_vendor_root()
    if bundled is None:
        pytest.skip("bundled vendor not populated")
    commit = knowledge._vendor_commit(bundled)
    assert commit, "_vendor_commit returned empty for bundled vendor"
    assert len(commit) == 40, f"Expected 40-char SHA, got: {commit!r}"


# --- companions (golden templates) -------------------------------------------

def test_parse_article_includes_companions(tmp_path):
    path = _write_article(tmp_path, rel="perf/setloadfields.md")
    # Write companion AL files alongside the article
    good_al = path.parent / "setloadfields.good.al"
    bad_al = path.parent / "setloadfields.bad.al"
    good_al.write_text("// good pattern\n", encoding="utf-8")
    bad_al.write_text("// bad pattern\n", encoding="utf-8")
    parsed = knowledge.parse_article(path)
    assert parsed is not None
    companions = parsed.get("companions", [])
    kinds = {c["kind"] for c in companions}
    assert kinds == {"good", "bad"}


def test_build_index_propagates_companions(tmp_path):
    path = _write_article(tmp_path, rel="perf/setloadfields.md")
    (path.parent / "setloadfields.good.al").write_text("// good\n", encoding="utf-8")
    index = knowledge.build_knowledge_index(tmp_path)
    art = index["articles"][0]
    assert len(art.get("companions", [])) == 1
    assert art["companions"][0]["kind"] == "good"


def test_select_articles_includes_companions(tmp_path):
    path = _write_article(tmp_path, rel="upgrade/datapercompany.md")
    (path.parent / "datapercompany.good.al").write_text("// good\n", encoding="utf-8")
    hits = knowledge.select_articles(tmp_path, "upgrade DataPerCompany shared table")
    assert hits
    assert any(c["kind"] == "good" for c in hits[0].get("companions", []))


# --- check_vendor_health -------------------------------------------------------

def test_check_vendor_health_ok_when_bundled_present(_use_real_vendor):
    health = knowledge.check_vendor_health()
    bundled = knowledge._bundled_vendor_root()
    if bundled is None:
        pytest.skip("bundled vendor not populated")
    assert health["ok"] is True
    assert health["bundled"] is True
    assert health["present"] is True
    assert health["errors"] == []


def test_check_vendor_health_error_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(knowledge.ENV_VENDOR_ROOT, str(tmp_path / "nonexistent"))
    health = knowledge.check_vendor_health(tmp_path)
    assert health["ok"] is False
    assert health["present"] is False
    assert health["errors"]


def test_check_vendor_health_drift(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    (clone / "microsoft" / "knowledge" / "x").mkdir(parents=True)
    _write_article(tmp_path, rel="x/a.md")  # repo layer
    _write_policy(tmp_path, vendor={"root": str(clone), "pinned_commit": "def456abc" * 4})
    monkeypatch.setattr(knowledge, "_vendor_commit", lambda root: "abc123def" * 4)
    health = knowledge.check_vendor_health(tmp_path)
    assert health["drift"] is True
    assert health["ok"] is False
    assert health["errors"]


# --- review packet: packet_article_count + vendor_health ----------------------

def test_review_packet_has_article_count_and_vendor_health(tmp_path):
    _write_article(tmp_path)
    memory.write_charter(tmp_path, "s1", purpose="upgrade DataPerCompany shared table",
                         operations={"update": True})
    packet = review.build_review_packet(tmp_path, "s1", changed_files=["Upgrade.al"])
    assert "packet_article_count" in packet
    assert "vendor_health" in packet
    assert isinstance(packet["packet_article_count"], int)


def test_review_packet_meta_written_to_disk(tmp_path):
    _write_article(tmp_path)
    memory.write_charter(tmp_path, "s1", purpose="upgrade DataPerCompany shared table",
                         operations={"update": True})
    review.build_review_packet(tmp_path, "s1", changed_files=["Upgrade.al"])
    from bc_agentic_mcp.workspace import specs_root as _sr
    meta = _sr(tmp_path) / "s1" / "review_packet_meta.json"
    assert meta.exists()
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert "packet_article_count" in data
    assert "vendor_health" in data



def test_relevance_floor_drops_weak_matches(tmp_path):
    strong = ARTICLE.replace(
        "domain: upgrade", "domain: containers").replace(
        "DataPerCompany, upgrade codeunit, company guard",
        "poisoned, symbolcache, republish, container").replace(
        "# Upgrade scope must match DataPerCompany",
        "# Remove poisoned symbol cache copies before republish")
    weak = ARTICLE.replace(
        "domain: upgrade", "domain: api").replace(
        "DataPerCompany, upgrade codeunit, company guard", "container").replace(
        "# Upgrade scope must match DataPerCompany",
        "# Extend the current version in place").replace(
        "A data-upgrade codeunit for a shared table (DataPerCompany=false) must run\nper-database without a company guard. Per-company scope silently skips rows.",
        "Versioning rules for pages exposed externally.")
    _write_article(tmp_path, rel="containers/poison.md", text=strong)
    _write_article(tmp_path, rel="api/versioning.md", text=weak)
    hits = knowledge.select_articles(tmp_path, "poisoned symbolcache republish container")
    assert [h["path"] for h in hits] == ["containers/poison.md"]  # weak match floored out


# --- promote-to-article tool path ----------------------------------------------

@pytest.mark.asyncio
async def test_promote_lesson_to_article(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_MCP_GLOBAL_LESSONS", str(tmp_path / "global-lessons.json"))
    from bc_agentic_mcp.tools.lessons_tool import handle_promote_lesson
    out = await handle_promote_lesson(
        str(tmp_path), message="Never call Commit inside a posting routine",
        severity="error", to_article=True, domain="posting",
    )
    assert out["promoted"] is True
    assert out["article"]["created"] is True
    hits = knowledge.select_articles(tmp_path, "Commit posting routine")
    assert hits and hits[0]["domain"] == "posting"
