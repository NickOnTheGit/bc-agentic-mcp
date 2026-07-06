"""Pictures ARE requirements — the image-capture wall (user report 2026-07-06).

The failure: ADO items embed screenshots (column layouts, field lists) as <img>
tags; capture stripped them silently and mangled HTML table columns, so specs
were built blind to requirements that only exist in pictures ("not adding
columns where they should be").

The wall, end to end:
1. NEVER SILENT — every <img> leaves an inline [IMAGE n] marker; table cells
   keep explicit | boundaries.
2. ALWAYS MATERIALIZED — attachment bytes are downloaded into context/images/;
   a failed download lands in `unresolved`, never skipped.
3. ENFORCED LOOK — each image writes a blocking Q-95x clarification; the
   existing clarifications engine refuses to spec the item until a SEEING agent
   transcribes every picture; auto_clarify refuses the Q-95x band (text matching
   cannot read pixels — auto-answering would be fabrication).
"""
import json
from pathlib import Path

from bc_agentic_mcp import auto_clarify, enforcement, item_context


ADO_IMG = ('https://dev.azure.com/org/proj/_apis/wit/attachments/'
           'aaaaaaaa-1111-2222-3333-444444444444?fileName=columns%20layout.png')


# ---------------------------------------------------------------------------
# 1. Never silent
# ---------------------------------------------------------------------------

def test_html_to_text_marks_images_instead_of_deleting_them():
    html = f'<p>Add these columns:</p><img src="{ADO_IMG}" alt="">'
    text = item_context._html_to_text(html)
    assert "[IMAGE 1: columns layout.png" in text
    assert "MUST be viewed" in text


def test_html_to_text_preserves_table_columns():
    html = ("<table><tr><th>Field</th><th>Caption</th></tr>"
            "<tr><td>NoOfAddresses</td><td>Addresses</td></tr></table>")
    text = item_context._html_to_text(html)
    assert "Field | Caption" in text
    assert "NoOfAddresses | Addresses" in text


def test_html_to_text_plain_unchanged():
    assert item_context._html_to_text("<div>A<br/>B</div>") == "A\nB"


def test_extract_image_urls_dedupes_and_names():
    html = f'<img src="{ADO_IMG}"><img src="{ADO_IMG}"><img src="https://other/x.png">'
    urls = item_context.extract_image_urls(html)
    assert len(urls) == 1  # non-attachment URLs are not ADO attachments; dupes collapse
    assert urls[0]["name"] == "columns layout.png"


# ---------------------------------------------------------------------------
# 2. Always materialized
# ---------------------------------------------------------------------------

def _binary_ok(url, headers):
    return 200, b"\x89PNG fake-bytes"


def _binary_404(url, headers):
    return 404, b""


def test_download_images_saves_bytes_and_records_failures(tmp_path):
    cdir = tmp_path / "context"
    cdir.mkdir()
    saved, unresolved = item_context.download_images(
        cdir, [{"url": ADO_IMG, "name": "columns layout.png"}],
        source="item", binary_fetcher=_binary_ok)
    assert len(saved) == 1 and unresolved == []
    p = cdir / saved[0]["path"]
    assert p.exists() and p.read_bytes().startswith(b"\x89PNG")
    assert saved[0]["source"] == "item" and saved[0]["bytes"] > 0

    saved2, unresolved2 = item_context.download_images(
        cdir, [{"url": ADO_IMG, "name": "x.png"}], source="comment",
        binary_fetcher=_binary_404)
    assert saved2 == [] and len(unresolved2) == 1
    assert "download failed" in unresolved2[0]["reason"]


def test_download_images_without_pat_is_loud(tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    cdir = tmp_path / "context"
    cdir.mkdir()
    saved, unresolved = item_context.download_images(
        cdir, [{"url": ADO_IMG, "name": "x.png"}], source="item")
    assert saved == [] and len(unresolved) == 1
    assert "not downloadable" in unresolved[0]["reason"]


# ---------------------------------------------------------------------------
# 3. Enforced look — the full chain through capture
# ---------------------------------------------------------------------------

def test_capture_materializes_images_and_blocks_spec_until_transcribed(tmp_path):
    manifest = item_context.capture(
        str(tmp_path), "img-spec",
        item_id="990400",
        description="Add columns per the attached screenshot.\n[IMAGE 1: columns layout.png]",
        images=[{"url": ADO_IMG, "name": "columns layout.png"}],
        binary_fetcher=_binary_ok,
    )
    # Ledger: manifest carries the image with its saved path.
    assert len(manifest["images"]) == 1
    rel = manifest["images"][0]["path"]
    assert (tmp_path / ".specs" / "img-spec" / "context" / rel).exists()

    # Blocking question exists in the standard grammar with an EMPTY answer.
    clar = tmp_path / ".specs" / "img-spec" / "clarifications.md"
    text = clar.read_text(encoding="utf-8")
    assert "## Q-951: [IMAGE] columns layout.png" in text
    assert "context/images/" in text

    # The clarifications engine BLOCKS (unanswered image question).
    status = enforcement.engine_status(tmp_path, "img-spec")["engines"]["clarifications"]
    assert status["ok"] is False
    assert "Q-951" in str(status)

    # Re-capture must not duplicate the question.
    item_context.capture(
        str(tmp_path), "img-spec", item_id="990400",
        description="recapture", images=[{"url": ADO_IMG, "name": "columns layout.png"}],
        binary_fetcher=_binary_ok)
    assert clar.read_text(encoding="utf-8").count("## Q-951") == 1

    # A seeing agent transcribes the image (multi-line, with .al evidence) -> unblocked.
    answered = text.replace(
        "_Answer:_ \n",
        "_Answer:_ The screenshot demands columns: No., Description, NoOfAddresses (in this order).\n"
        "Maps to page FacilitiesOfRealtyObjectFDN in extensions/BaseApp/src/FacilitiesOfRealtyObject.Page.al\n",
        1)
    clar.write_text(answered, encoding="utf-8")
    status = enforcement.engine_status(tmp_path, "img-spec")["engines"]["clarifications"]
    assert status["ok"] is True, f"transcribed image still blocks: {status}"


def test_auto_clarify_refuses_to_answer_image_questions():
    clar_text = (
        "# Clarifications for: x\n\n"
        "## Q-001: Which page?\n_Answer:_ \n\n"
        "## Q-951: [IMAGE] shot.png — view and transcribe before the spec\n_Answer:_ \n"
    )
    open_qs = auto_clarify.parse_open_questions(clar_text)
    ids = [q["id"] for q in open_qs]
    assert "Q-001" in ids
    assert "Q-951" not in ids, "auto-clarify must never fabricate an answer to pixels"


def test_comments_images_are_collected(tmp_path):
    comment_html = f'<div>see attached <img src="{ADO_IMG}"></div>'
    def fetcher(url, headers):
        return 200, json.dumps({"comments": [
            {"createdBy": {"displayName": "PO"}, "createdDate": "2026-07-06",
             "text": comment_html}]})
    out = item_context.fetch_comments(org_url="https://dev.azure.com/o", project="p",
                                      item_id="1", fetcher=fetcher)
    assert out["fetched"] is True
    c = out["comments"][0]
    assert "[IMAGE 1: columns layout.png" in c["text"]
    assert c["images"][0]["name"] == "columns layout.png"
