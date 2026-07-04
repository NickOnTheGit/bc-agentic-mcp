"""Tests for item_context — fresh capture of the full item bundle to disk."""
import json

from bc_agentic_mcp import item_context


def _desc():
    return (
        "As external app... See related workitem: #258909 . "
        "See wiki page: https://dev.azure.com/cegekadsa/DynamicsEmpire/_wiki/wikis/"
        "DynamicsEmpire.wiki/10107/EMP-Rental-Mutation"
    )


def test_capture_materializes_full_bundle(tmp_path):
    def wiki_fetcher(url, headers):
        return 200, json.dumps({"content": "# rentalMutation API\nAPI Group: contract"})

    def related_fetcher(url, headers):
        return 200, json.dumps({"fields": {"System.Title": "Add fields",
                                            "System.Description": "<div>Adds on-hold fields</div>"},
                                "relations": []})

    manifest = item_context.capture(
        str(tmp_path), "wi-264484", item_id="264484", description=_desc(),
        org_url="https://dev.azure.com/cegekadsa", project="DynamicsEmpire",
        wiki_fetcher=wiki_fetcher, related_fetcher=related_fetcher,
    )
    cdir = tmp_path / ".specs" / "wi-264484" / "context"
    # Item description saved.
    assert (cdir / "item-264484.md").exists()
    # Wiki page fetched fresh + saved.
    wiki_files = list((cdir / "wiki").glob("10107_*.md"))
    assert wiki_files and "contract" in wiki_files[0].read_text()
    # Related item fetched fresh + saved (HTML stripped to text).
    rel = (cdir / "related" / "258909.md").read_text()
    assert "on-hold fields" in rel and "<div>" not in rel
    # Manifest is complete and marks sources as fresh.
    assert manifest["complete"] is True
    assert any(f["source"] == "rest-api-fresh" and f["kind"] == "wiki" for f in manifest["files"])
    assert manifest["references"]["wiki_links"][0]["page_id"] == "10107"


def test_capture_records_unresolved_when_wiki_fetch_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)

    def wiki_fetcher(url, headers):
        return 403, "forbidden"

    manifest = item_context.capture(
        str(tmp_path), "s", item_id="1", description=_desc(),
        org_url="https://dev.azure.com/o", project="P",
        wiki_fetcher=wiki_fetcher, related_fetcher=lambda u, h: (200, json.dumps({"fields": {}}))
    )
    # A failed wiki fetch must be recorded as unresolved, never silently skipped.
    assert manifest["complete"] is False
    assert any(u["kind"] == "wiki" for u in manifest["unresolved"])


def test_load_context_roundtrip(tmp_path):
    item_context.capture(str(tmp_path), "s", item_id="1", description="no refs here",
                         org_url="https://dev.azure.com/o", project="P")
    loaded = item_context.load_context(str(tmp_path), "s")
    assert loaded is not None and loaded["item_id"] == "1"
    assert item_context.load_context(str(tmp_path), "missing") is None


def test_html_to_text():
    assert item_context._html_to_text("<div>A<br/>B</div>") == "A\nB"


def test_fetch_work_item_parses_relations():
    def fetcher(url, headers):
        return 200, json.dumps({
            "fields": {"System.Title": "T", "System.Description": "<p>hi</p>"},
            "relations": [{"url": "https://x/_apis/wit/workItems/258909"}],
        })
    wi = item_context.fetch_work_item(org_url="https://dev.azure.com/o", project="P",
                                      item_id="264484", fetcher=fetcher)
    assert wi["fetched"] and wi["title"] == "T" and wi["description"] == "hi"
    assert wi["related_ids"] == ["258909"]
