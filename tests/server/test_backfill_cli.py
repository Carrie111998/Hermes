"""`backfill-candidate-search` has to actually reach a real database.

The backfill itself is covered in test_candidate_search_text.py. What is only
testable here is the wiring: that the subcommand parses, opens the configured
database rather than a test fixture's, writes, and reports counts without ever
echoing a candidate row — the same rule `import-candidates` follows, and the
reason both commands print counts instead of records.
"""
from __future__ import annotations

import json

from server.__main__ import main
from server.db import Database
from server.lead_research.candidates import CandidateRepository


def _corpus() -> bytes:
    return b"\n".join(json.dumps({
        "source_record_id": f"atlas-{index}",
        "company_name": f"Atlas Kitchens {index} GmbH",
        "country": "DE",
        "domain": f"https://atlas-{index}.example.test",
        "categories": ["Built-in ovens"],
    }).encode() for index in range(5))


def _seed_without_search_text(path):
    db = Database(path)
    CandidateRepository(db).import_file("kitchen-appliances", "1", "c.jsonl", _corpus())
    db.execute("UPDATE candidate_records SET search_text=NULL")
    return db


def test_the_command_fills_the_configured_database(tmp_path, monkeypatch, capsys):
    path = tmp_path / "interfaze.db"
    db = _seed_without_search_text(path)
    monkeypatch.setenv("INTERFAZE_DATABASE_PATH", str(path))

    main(["backfill-candidate-search"])

    assert json.loads(capsys.readouterr().out) == {"filled": 5, "remaining": 0}
    assert db.one(
        "SELECT COUNT(*) AS n FROM candidate_records WHERE search_text IS NULL"
    )["n"] == 0


def test_a_second_run_reports_nothing_to_do_rather_than_failing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "interfaze.db"
    _seed_without_search_text(path)
    monkeypatch.setenv("INTERFAZE_DATABASE_PATH", str(path))
    main(["backfill-candidate-search"])
    capsys.readouterr()

    main(["backfill-candidate-search"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"filled": 0, "remaining": 0}
    # `{"filled": 0}` on its own reads like a failure, so the reason is stated.
    assert "nothing to backfill" in captured.err


def test_batching_still_fills_every_row(tmp_path, monkeypatch, capsys):
    path = tmp_path / "interfaze.db"
    db = _seed_without_search_text(path)
    monkeypatch.setenv("INTERFAZE_DATABASE_PATH", str(path))

    main(["backfill-candidate-search", "--batch", "2"])

    assert json.loads(capsys.readouterr().out) == {"filled": 5, "remaining": 0}
    assert db.one(
        "SELECT COUNT(*) AS n FROM candidate_records WHERE search_text IS NULL"
    )["n"] == 0


def test_no_candidate_row_is_echoed(tmp_path, monkeypatch, capsys):
    """The corpus is private service data; this CLI prints counts, never rows."""
    path = tmp_path / "interfaze.db"
    _seed_without_search_text(path)
    monkeypatch.setenv("INTERFAZE_DATABASE_PATH", str(path))

    main(["backfill-candidate-search"])

    captured = capsys.readouterr()
    assert "Atlas" not in captured.out + captured.err
    assert "example.test" not in captured.out + captured.err
