"""Tests for the fresh wiki-fetch capability (no stale-clone workaround)."""
import base64
import json

from bc_agentic_mcp import wiki


def test_parse_dev_azure_url():
    p = wiki.parse_wiki_url(
        "https://dev.azure.com/cegekadsa/DynamicsEmpire/_wiki/wikis/DynamicsEmpire.wiki/10107/EMP-Rental-Mutation"
    )
    assert p == {
        "org_url": "https://dev.azure.com/cegekadsa",
        "project": "DynamicsEmpire",
        "wiki": "DynamicsEmpire.wiki",
        "page_id": "10107",
    }


def test_parse_visualstudio_url():
    p = wiki.parse_wiki_url(
        "https://cegekadsa.visualstudio.com/DynamicsEmpire/_wiki/wikis/W/55/Page"
    )
    assert p["org_url"] == "https://cegekadsa.visualstudio.com"
    assert p["page_id"] == "55"


def test_parse_non_wiki_url():
    assert wiki.parse_wiki_url("https://example.com/not-a-wiki") is None


def test_build_rest_url_is_id_based():
    url = wiki.build_rest_url("https://dev.azure.com/o", "P", "W", "10107")
    assert "/_apis/wiki/wikis/W/pages/10107" in url
    assert "includeContent=true" in url


def test_basic_auth_header_uses_pat_without_leaking():
    h = wiki.basic_auth_header("SECRETPAT")
    assert h.startswith("Basic ")
    decoded = base64.b64decode(h.split(" ", 1)[1]).decode()
    assert decoded == ":SECRETPAT"  # ADO form :<pat>


def test_fetch_fails_closed_without_pat(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    r = wiki.fetch_wiki_page(org_url="https://dev.azure.com/o", project="P", wiki="W", page_id="1")
    assert r["fetched"] is False
    assert "cannot fetch" in r["reason"].lower()


def test_fetch_with_injected_fetcher():
    def fetcher(url, headers):
        assert "Authorization" in headers
        return 200, json.dumps({"content": "# Wiki\nThe rentalMutation API..."})
    r = wiki.fetch_wiki_page(org_url="https://dev.azure.com/o", project="P", wiki="W",
                             page_id="1", fetcher=fetcher)
    assert r["fetched"] is True
    assert "rentalMutation" in r["content"]
    assert r["source"] == "rest-api-fresh"


def test_fetch_from_url_end_to_end():
    def fetcher(url, headers):
        return 200, json.dumps({"content": "hello"})
    r = wiki.fetch_from_url(
        "https://dev.azure.com/o/P/_wiki/wikis/W/9/Slug", fetcher=fetcher
    )
    assert r["fetched"] is True and r["content"] == "hello"
    assert r["parsed"]["page_id"] == "9"


def test_fetch_reports_http_error():
    def fetcher(url, headers):
        return 403, "forbidden"
    r = wiki.fetch_wiki_page(org_url="https://dev.azure.com/o", project="P", wiki="W",
                             page_id="1", fetcher=fetcher)
    assert r["fetched"] is False and r["status"] == 403
