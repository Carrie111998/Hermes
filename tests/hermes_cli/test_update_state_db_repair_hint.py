"""The post-update state.db corruption message must name a repair (#88252).

`hermes update` verifies state.db afterwards and, when the check fails, tries
to restore a whole-database snapshot.  When there is no usable snapshot it
used to stop there — the reporter saw

    ⚠ state.db is corrupted after update: integrity check failed:
      malformed inverted index for FTS5 table main.messages_fts_trigram
      ⚠ No pre-update snapshot was taken

on every update, concluded their history was damaged, and eventually ran the
FTS5 ``'rebuild'`` by hand.  It was not damaged: that message names a derived
search index, and ``hermes sessions repair`` already rebuilds it in place.
"""
from __future__ import annotations

import inspect

import pytest

from hermes_cli import update_cmd


# The exact string PRAGMA integrity_check produced on the reporter's database
# (Windows 11 26200, Hermes 0.20.2, state.db ~207 MB), as quoted in #88252.
REPORTED_MESSAGE = (
    "integrity check failed: malformed inverted index for FTS5 table "
    "main.messages_fts_trigram"
)


class TestDamageClassification:
    """Only FTS damage may be described as leaving the transcript intact."""

    @pytest.mark.parametrize(
        "message",
        [
            REPORTED_MESSAGE,
            "malformed inverted index for FTS5 table main.messages_fts",
            # The newer-SQLite wording of the same class, already recognised
            # elsewhere by SessionDB._is_fts_write_corruption_error.
            'fts5: corrupt structure record for table "messages_fts"',
            # Callers hand us whatever verify_sqlite_integrity returned, and
            # SQLite's own casing has changed between releases.
            "MALFORMED INVERTED INDEX FOR FTS5 TABLE main.messages_fts_cjk",
        ],
    )
    def test_fts_damage_is_recognised(self, message):
        assert update_cmd._state_db_damage_is_fts_only(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            # Genuine b-tree damage in a canonical table — the transcript
            # really may be gone, so we must not say otherwise.
            "row 12 missing from index sqlite_autoindex_sessions_1",
            "wrong # of entries in index idx_messages_session",
            "integrity check failed: database disk image is malformed",
            "header check failed: not a database",
            "unknown error",
            "",
        ],
    )
    def test_non_fts_damage_is_not_claimed_as_fts(self, message):
        assert update_cmd._state_db_damage_is_fts_only(message) is False

    def test_generic_malformed_image_is_not_treated_as_fts(self):
        """The load-bearing negative case.

        ``database disk image is malformed`` is what a corrupt FTS shadow
        table raises on *older* SQLite builds, which is why
        ``is_malformed_db_error`` accepts it — but it is equally what real
        page damage raises, and integrity_check offers no way to tell them
        apart from the string alone.  Reassuring a user with genuine page
        damage that their messages are fine is a worse failure than saying
        nothing, so this class deliberately falls through to the generic
        hint.
        """
        assert (
            update_cmd._state_db_damage_is_fts_only("database disk image is malformed")
            is False
        )


class TestHintOutput:
    """What the user is actually told once auto-restore is not an option."""

    def test_fts_hint_names_the_command_and_reassures(self, capsys):
        update_cmd._print_state_db_repair_hint(REPORTED_MESSAGE)
        out = capsys.readouterr().out

        assert "hermes sessions repair" in out
        assert "intact" in out
        assert "FTS5" in out

    def test_generic_hint_names_the_command_without_reassuring(self, capsys):
        update_cmd._print_state_db_repair_hint(
            "row 12 missing from index sqlite_autoindex_sessions_1"
        )
        out = capsys.readouterr().out

        assert "hermes sessions repair" in out
        # No promise about the data: this class can genuinely have lost rows.
        assert "intact" not in out

    @pytest.mark.parametrize(
        "message",
        [REPORTED_MESSAGE, "row 12 missing from index sqlite_autoindex_sessions_1"],
    )
    def test_hint_never_claims_to_have_done_anything(self, capsys, message):
        """A hint, never a receipt.

        An update must not acquire a write lock on a database the user has
        not asked it to rewrite, so nothing here repairs anything.  If a
        later change makes it act, this test should fail and force the
        wording — and the reasoning — to be revisited.
        """
        update_cmd._print_state_db_repair_hint(message)
        out = capsys.readouterr().out.lower()

        for claim in ("repaired", "rebuilt", "restored", "fixed"):
            assert claim not in out


class TestBothUpdatePathsAreWired:
    """The two post-update checks must both reach the hint.

    ``_cmd_update_impl`` and ``_update_via_zip`` are the git and zip update
    flows; each verifies state.db afterwards and each could reach the dead
    end.  Neither is callable in a test — they drive a whole update — so
    this asserts on their source, the same technique used across
    ``tests/hermes_cli`` for logic buried inside long command functions.
    """

    @pytest.mark.parametrize(
        "func",
        [update_cmd._cmd_update_impl, update_cmd._update_via_zip],
        ids=["git-path", "zip-path"],
    )
    def test_path_prints_the_hint_when_nothing_was_restored(self, func):
        source = inspect.getsource(func)

        assert "_print_state_db_repair_hint(" in source, (
            f"{func.__name__} reports state.db corruption but never names a repair"
        )
        assert "if not _state_restored:" in source, (
            f"{func.__name__} must gate the hint on the restore having failed"
        )

    @pytest.mark.parametrize(
        "func",
        [update_cmd._cmd_update_impl, update_cmd._update_via_zip],
        ids=["git-path", "zip-path"],
    )
    def test_path_records_a_successful_restore(self, func):
        """Otherwise a repaired-by-restore database still gets the hint."""
        source = inspect.getsource(func)

        assert "_state_restored = False" in source
        assert "_state_restored = True" in source
