"""Review-loop walls from PR 41670/41674 findings (2026-07-06).

- CRLF-DOUBLE: \r\r\n corruption in scope files — phantom blank lines in ADO,
  untouched files rendered as full rewrites (3 of 7 review comments in one PR).
- VAR-PARTIAL-REC: a `var Record` out-parameter that gets SetLoadFields inside
  the procedure — a partial record (with live filters) escaping the scope that
  knows what was loaded (reviewer lesson: expose the DECISION, not the record).
- classify_threads: repo-relative file + line anchor surfaced (the old
  passthrough got mangled into 'C:/extensions/…' with no line).
"""
import json
from pathlib import Path

from bc_agentic_mcp import detectors
from bc_agentic_mcp.pr import classify_threads


def _spec_with_files(tmp_path: Path, rel_files):
    sdir = tmp_path / ".specs" / "s1"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "spec.json").write_text(json.dumps({
        "scope_boundaries": {"allowed_files": rel_files}}), encoding="utf-8")


def test_crlf_corruption_detector_fires_only_on_double_cr(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Corrupt.Codeunit.al").write_bytes(b"codeunit 1 X\r\r\n{\r\r\n}\r\r\n")
    (src / "Clean.Codeunit.al").write_bytes(b"codeunit 2 Y\r\n{\r\n}\r\n")
    _spec_with_files(tmp_path, ["src/Corrupt.Codeunit.al", "src/Clean.Codeunit.al"])
    findings = detectors._detect_crlf_corruption(tmp_path, "s1")
    assert len(findings) == 1
    f = findings[0]
    assert f["code"] == "CRLF-DOUBLE" and f["severity"] == "error"
    assert "Corrupt.Codeunit.al" in f["summary"]
    assert "restore it" in f["summary"] or "restore" in f["summary"]


def test_escaping_partial_record_detector(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    # Offender: var Record out-param + SetLoadFields on it in the body.
    (src / "Leaky.Codeunit.al").write_text(
        "codeunit 3 Leaky\n{\n"
        "    internal procedure TryGetNewest(var NewestContractLine: Record Contract; No: Code[20]): Boolean\n"
        "    begin\n"
        "        NewestContractLine.SetLoadFields(\"State\");\n"
        "        NewestContractLine.SetRange(\"No.\", No);\n"
        "        exit(NewestContractLine.FindFirst());\n"
        "    end;\n}\n", encoding="utf-8")
    # Fine: SetLoadFields on a LOCAL record (never escapes).
    (src / "Encapsulated.Codeunit.al").write_text(
        "codeunit 4 Encapsulated\n{\n"
        "    local procedure Blocks(No: Code[20]): Boolean\n"
        "    var\n        Line: Record Contract;\n"
        "    begin\n"
        "        Line.SetLoadFields(\"State\");\n"
        "        Line.SetRange(\"No.\", No);\n"
        "        exit(Line.FindFirst());\n"
        "    end;\n}\n", encoding="utf-8")
    # Fine: var Record out-param but no SetLoadFields (complete record handed out).
    (src / "Complete.Codeunit.al").write_text(
        "codeunit 5 Complete\n{\n"
        "    procedure GetIt(var Rec: Record Contract)\n"
        "    begin\n        Rec.FindFirst();\n    end;\n}\n", encoding="utf-8")
    _spec_with_files(tmp_path, ["src/Leaky.Codeunit.al", "src/Encapsulated.Codeunit.al",
                                "src/Complete.Codeunit.al"])
    findings = detectors._detect_escaping_partial_record(tmp_path, "s1")
    assert len(findings) == 1
    f = findings[0]
    assert f["code"] == "VAR-PARTIAL-REC"
    assert "NewestContractLine" in f["summary"] and "Leaky" in f["summary"]
    assert "DECISION" in f["summary"]


def test_classify_threads_surfaces_relative_path_and_line():
    threads = [{
        "id": 316419, "status": "active",
        "threadContext": {
            "filePath": "/extensions/BaseApp/src/Housing/RentalProposal/RentalProposalHelper.Codeunit.al",
            "rightFileStart": {"line": 42, "offset": 1},
        },
        "comments": [{"author": {"displayName": "Reviewer"}, "content": "Extra space added"}],
    }, {
        "id": 1, "status": "fixed", "comments": [],
    }]
    out = classify_threads(threads)
    assert out["open_count"] == 1 and out["resolved_count"] == 1
    t = out["open"][0]
    assert t["file"] == "extensions/BaseApp/src/Housing/RentalProposal/RentalProposalHelper.Codeunit.al"
    assert t["line"] == 42


# ---------------------------------------------------------------------------
# TRIAGE WALL: no resolution without a recorded critical judgment
# ---------------------------------------------------------------------------

def _pr_record(tmp_path):
    import asyncio
    pdir = tmp_path / ".specs" / "item-1" / "pr"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "pr.json").write_text(json.dumps({
        "pr_id": 41674, "org_url": "https://dev.azure.com/org", "project": "p",
        "repository": "r"}), encoding="utf-8")
    return pdir


def test_resolve_requires_triage_judgment_and_analysis(tmp_path):
    import asyncio
    from bc_agentic_mcp.tools.pr import handle_resolve_review_comment
    _pr_record(tmp_path)
    # No judgment at all -> wall.
    out = asyncio.run(handle_resolve_review_comment(
        str(tmp_path), "item-1", thread_id=316419, reply="done"))
    assert out["status"] == "blocked_triage_required"
    assert "claim to verify" in out["reason"]
    # Judgment without substantive analysis -> wall.
    out = asyncio.run(handle_resolve_review_comment(
        str(tmp_path), "item-1", thread_id=316419, reply="done",
        judgment="correct", analysis="ok"))
    assert out["status"] == "blocked_triage_required"


def test_triage_dry_run_carries_judgment_and_incorrect_never_closes(tmp_path):
    import asyncio
    from bc_agentic_mcp.tools.pr import handle_resolve_review_comment
    _pr_record(tmp_path)
    analysis = ("Reviewer asks why the enum extension is modified; verified via git diff -w "
                "that the file carries no semantic change - it was a line-ending rewrite.")
    out = asyncio.run(handle_resolve_review_comment(
        str(tmp_path), "item-1", thread_id=316416, reply="Restored from master.",
        judgment="correct", analysis=analysis))
    assert out["status"] == "dry_run"
    assert out["triage"]["judgment"] == "correct"
    # A thread judged incorrect must never be auto-closed: resolution flips to active.
    out = asyncio.run(handle_resolve_review_comment(
        str(tmp_path), "item-1", thread_id=316421, reply="Respectful pushback with evidence.",
        judgment="incorrect", analysis=analysis, resolution="fixed"))
    assert out["status"] == "dry_run"
    assert out["would_post"]["resolution"] == "active"
