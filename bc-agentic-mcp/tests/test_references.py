"""Tests for work-item reference extraction (the wiki/related-item hook)."""
from bc_agentic_mcp import item_references as refs


def test_extracts_wiki_related_and_urls():
    text = (
        'As external app... See related workitem: '
        '<a href="https://dev.azure.com/cegekadsa/DynamicsEmpire/_workitems/edit/258909/">#258909</a> '
        'See wiki page: '
        'https://dev.azure.com/cegekadsa/DynamicsEmpire/_wiki/wikis/DynamicsEmpire.wiki/10107/EMP-Rental-Mutation '
        'and https://example.com/spec.pdf'
    )
    r = refs.extract_references(text)
    assert r["has_references"] is True
    assert r["wiki_links"] == [
        {"wiki": "DynamicsEmpire.wiki", "page_id": "10107", "slug": "EMP-Rental-Mutation"}
    ]
    assert "258909" in r["related_work_items"]
    assert "https://example.com/spec.pdf" in r["urls"]
    # The wiki URL must NOT be double-counted in 'urls'.
    assert all("/_wiki/wikis/" not in u for u in r["urls"])


def test_bare_hash_related_items():
    r = refs.extract_references("depends on #264484 and #258909")
    assert r["related_work_items"] == ["258909", "264484"]  # sorted numerically


def test_no_references():
    r = refs.extract_references("Just a plain description with no links.")
    assert r["has_references"] is False
    assert r["reference_count"] == 0
    assert "No wiki" in refs.render_reference_checklist(r)


def test_checklist_lists_each_reference():
    text = ("wiki https://dev.azure.com/o/p/_wiki/wikis/W/55/Some-Page and #100")
    md = refs.render_reference_checklist(refs.extract_references(text))
    assert "WIKI: Some-Page" in md and "#100" in md
    assert "BEFORE choosing" in md


def test_deterministic():
    text = "#300 #100 #200 https://a.com https://b.com"
    a = refs.extract_references(text)
    b = refs.extract_references(text)
    assert a == b
    assert a["related_work_items"] == ["100", "200", "300"]
