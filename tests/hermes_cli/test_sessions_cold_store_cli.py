"""CLI contract for storing one explicit archived session lineage locally."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def session_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import hermes_state

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    return home


def _seed_archived_lineage() -> None:
    db = SessionDB()
    try:
        db.create_session("lineage-root", source="cli")
        db.append_message("lineage-root", role="user", content="sensitive first turn")
        db.end_session("lineage-root", "compression")
        db.create_session(
            "lineage-terminal",
            source="cli",
            parent_session_id="lineage-root",
        )
        db.append_message(
            "lineage-terminal", role="assistant", content="sensitive final turn"
        )
        db.end_session("lineage-terminal", "completed")
        assert db.set_session_archived("lineage-terminal", True)
    finally:
        db.close()


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, str]:
    import hermes_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    try:
        main_mod.main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _lineage_rows() -> list[tuple[str, int]]:
    db = SessionDB()
    try:
        assert db._conn is not None
        return [
            (str(row["id"]), int(row["archived"]))
            for row in db._conn.execute(
                "SELECT id, archived FROM sessions ORDER BY id"
            ).fetchall()
        ]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("arguments", "required_argument"),
    [
        (("sessions", "cold-store"), "ROOT"),
        (("sessions", "cold-store", "archive"), "--session-id"),
    ],
)
def test_cold_store_requires_root_and_named_session_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    required_argument: str,
) -> None:
    code, _out, err = _run_cli(monkeypatch, capsys, *arguments)

    assert code == 2
    assert required_argument in err
    assert "required" in err


def test_cold_store_yes_stores_exactly_one_resolved_lineage_and_reports_identity(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_archived_lineage()
    archive_root = session_home.parent / "cold-root"
    before = _lineage_rows()

    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-store",
        str(archive_root),
        "--session-id",
        "lineage-term",
        "--yes",
    )

    assert code == 0
    assert err == ""
    assert "terminal ID: lineage-terminal" in out
    assert "physical IDs: lineage-root, lineage-terminal" in out
    assert re.search(r"fingerprint: [0-9a-f]{64}\b", out)
    snapshot_match = re.search(r"local snapshot: (.+)$", out, re.MULTILINE)
    assert snapshot_match is not None
    snapshot = Path(snapshot_match.group(1))
    assert snapshot.is_relative_to(archive_root)
    assert {path.name for path in snapshot.iterdir()} == {
        "artifacts",
        "metadata.json",
        "session.jsonl",
    }
    assert _lineage_rows() == before == [
        ("lineage-root", 1),
        ("lineage-terminal", 1),
    ]


def test_cold_store_default_confirmation_warns_about_raw_transcript_and_cancels(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_archived_lineage()
    archive_root = session_home.parent / "cold-root"
    prompts: list[str] = []

    def decline(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", decline)
    before = _lineage_rows()

    code, out, _err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-store",
        str(archive_root),
        "--session-id",
        "lineage-terminal",
    )

    assert code == 0
    assert out.strip() == "Cancelled."
    assert len(prompts) == 1
    assert "sensitive raw transcript" in prompts[0]
    assert str(archive_root) in prompts[0]
    assert prompts[0].endswith("[y/N] ")
    assert not archive_root.exists()
    assert _lineage_rows() == before


def test_cold_store_dry_run_creates_nothing_and_does_not_prompt(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_archived_lineage()
    archive_root = session_home.parent / "cold-root"
    before = _lineage_rows()

    def unexpected_prompt(_prompt: str) -> str:
        raise AssertionError("dry-run must not prompt")

    monkeypatch.setattr("builtins.input", unexpected_prompt)
    code, out, _err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-store",
        str(archive_root),
        "--session-id",
        "lineage-term",
        "--dry-run",
    )

    assert code == 0
    assert "Would store archived session lineage 'lineage-terminal'" in out
    assert str(archive_root) in out
    assert "nothing was written" in out
    assert not archive_root.exists()
    assert _lineage_rows() == before


@pytest.mark.parametrize("execution_flag", ["--yes", "--dry-run"])
def test_cold_store_cli_reports_unsupported_blob_without_writing(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    execution_flag: str,
) -> None:
    db = SessionDB()
    try:
        db.create_session("blob-session", source="cli")
        message_id = db.append_message(
            "blob-session", role="user", content="placeholder"
        )
        assert db._conn is not None
        db._conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (b"future-blob", message_id),
        )
        db.end_session("blob-session", "completed")
        assert db.set_session_archived("blob-session", True)
    finally:
        db.close()
    archive_root = session_home.parent / "cold-root"
    before = _lineage_rows()

    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-store",
        str(archive_root),
        "--session-id",
        "blob-session",
        execution_flag,
    )

    assert code == 1
    assert err == ""
    assert (
        "cold store v1 does not support SQLite BLOB/bytes values at message.content"
        in out
    )
    assert not archive_root.exists()
    assert _lineage_rows() == before == [("blob-session", 1)]

    db = SessionDB()
    try:
        assert db._conn is not None
        row = db._conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", ("blob-session",)
        ).fetchone()
        assert row is not None and row["content"] == b"future-blob"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("session_id", "extra_session"),
    [
        ("missing", None),
        ("lineage-", "lineage-another"),
    ],
)
def test_cold_store_rejects_missing_or_ambiguous_ids_before_writing(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session_id: str,
    extra_session: str | None,
) -> None:
    _seed_archived_lineage()
    if extra_session is not None:
        db = SessionDB()
        try:
            db.create_session(extra_session, source="cli")
            db.end_session(extra_session, "completed")
        finally:
            db.close()
    archive_root = session_home.parent / "cold-root"

    code, out, _err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-store",
        str(archive_root),
        "--session-id",
        session_id,
        "--yes",
    )

    assert code == 1
    assert f"Session '{session_id}' was not found or is ambiguous." in out
    assert not archive_root.exists()


@pytest.mark.parametrize("execution_flag", ["--yes", "--dry-run"])
def test_cold_store_surfaces_store_eligibility_rejection_without_db_changes(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    execution_flag: str,
) -> None:
    db = SessionDB()
    try:
        db.create_session("not-archived", source="cli")
        db.end_session("not-archived", "completed")
    finally:
        db.close()
    archive_root = session_home.parent / "cold-root"
    before = _lineage_rows()

    code, out, _err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-store",
        str(archive_root),
        "--session-id",
        "not-archived",
        execution_flag,
    )

    assert code == 1
    assert "all compression lineage rows must be marked archived" in out
    assert not archive_root.exists()
    assert _lineage_rows() == before == [("not-archived", 0)]


@pytest.mark.parametrize("execution_flag", ["--yes", "--dry-run"])
def test_cold_store_surfaces_non_terminal_rejection_without_db_changes(
    session_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    execution_flag: str,
) -> None:
    db = SessionDB()
    try:
        db.create_session("still-open", source="cli")
        assert db.set_session_archived("still-open", True)
    finally:
        db.close()
    archive_root = session_home.parent / "cold-root"
    before = _lineage_rows()

    code, out, _err = _run_cli(
        monkeypatch,
        capsys,
        "sessions",
        "cold-store",
        str(archive_root),
        "--session-id",
        "still-open",
        execution_flag,
    )

    assert code == 1
    assert "terminal session must be ended and non-compression" in out
    assert not archive_root.exists()
    assert _lineage_rows() == before == [("still-open", 1)]
