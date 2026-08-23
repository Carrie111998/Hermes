"""The latest-receipt pointer must never speak for a run it did not record.

`read_latest_receipt` is what the Desktop reads — `web_server.py` calls it
"the durable success signal the Desktop ... rely on". It consulted only
`latest.json`, and the writer swallowed a failed pointer write with a bare
`pass`. So a run whose timestamped receipt wrote fine but whose pointer did
not would report the PREVIOUS run's outcome as this one's — #81193's exact
shape ("desktop shows failure for a successful update"), one of the issues
this module was written to end.
"""

import json
import logging
import time

import pytest

from hermes_cli import update_receipt


@pytest.fixture()
def receipts(tmp_path, monkeypatch):
    d = tmp_path / "logs" / "update_receipts"
    d.mkdir(parents=True)
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: d)
    return d


def _write(directory, name, payload):
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


class TestReader:
    def test_the_pointer_is_used_when_present(self, receipts):
        _write(receipts, "latest.json", {"outcome": "pointed-at"})
        _write(receipts, "update_20260101_000000_1.json", {"outcome": "older"})
        assert update_receipt.read_latest_receipt()["outcome"] == "pointed-at"

    def test_a_missing_pointer_falls_back_to_the_newest_receipt(self, receipts):
        _write(receipts, "update_20260101_000000_1.json", {"outcome": "older"})
        time.sleep(0.01)
        _write(receipts, "update_20260823_120000_2.json", {"outcome": "newest"})

        assert update_receipt.read_latest_receipt()["outcome"] == "newest", (
            "with no pointer the reader reported nothing, so a completed "
            "update looks like it never ran"
        )

    def test_a_corrupt_pointer_falls_back_rather_than_reporting_nothing(self, receipts):
        (receipts / "latest.json").write_text("{ not json", encoding="utf-8")
        _write(receipts, "update_20260823_120000_2.json", {"outcome": "newest"})
        assert update_receipt.read_latest_receipt()["outcome"] == "newest"

    def test_an_empty_directory_is_still_none(self, receipts):
        assert update_receipt.read_latest_receipt() is None

    @pytest.mark.parametrize(
        "bad", ["{ truncated", '["not", "a", "dict"]', '"a string"', "null"]
    )
    def test_an_unreadable_newest_is_unknown_not_a_predecessor(self, receipts, bad):
        """Reporting an older run as the current one is the staleness this
        module exists to prevent — it reads identically to a caller whether
        it came from the pointer or from the scan."""
        _write(receipts, "update_20260101_000000_1.json", {"outcome": "OLD RUN"})
        time.sleep(0.01)
        (receipts / "update_20260823_120000_2.json").write_text(bad, encoding="utf-8")

        assert update_receipt.read_latest_receipt() is None, (
            "an unreadable newest receipt surfaced a predecessor as the "
            "current run"
        )

    def test_ordering_is_by_mtime_not_filename(self, receipts):
        """Names carry a pid suffix, so lexicographic order is not run order."""
        _write(receipts, "update_20260823_120000_99.json", {"outcome": "written first"})
        time.sleep(0.01)
        _write(receipts, "update_20260823_120000_10.json", {"outcome": "written second"})

        assert update_receipt.read_latest_receipt()["outcome"] == "written second"

    def test_a_broken_receipt_dir_never_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError("broken profile")

        monkeypatch.setattr(update_receipt, "_receipt_dir", _boom)
        assert update_receipt.read_latest_receipt() is None


class TestWriter:
    """Driven through the real begin/finalize path, not a test-only seam."""

    @staticmethod
    def _break_pointer_write(monkeypatch):
        """Fail only the atomic rename onto `latest.json`."""
        real_replace = update_receipt.os.replace

        def _fail(src, dst, *a, **kw):
            if str(dst).endswith("latest.json"):
                raise OSError("read-only")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(update_receipt.os, "replace", _fail)

    def test_a_failed_pointer_write_removes_the_stale_one(self, receipts, monkeypatch, caplog):
        _write(receipts, "latest.json", {"outcome": "PREVIOUS RUN"})
        self._break_pointer_write(monkeypatch)
        update_receipt.begin_update_receipt()

        with caplog.at_level(logging.WARNING, logger="hermes_cli.update_receipt"):
            written = update_receipt.finalize_update_receipt("success")

        assert written is not None, "the timestamped receipt should still be written"
        assert not (receipts / "latest.json").exists(), (
            "the previous run's pointer survived, so the Desktop would report "
            "its outcome for this update"
        )
        assert any("stale" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_the_reader_then_finds_this_run_not_the_previous_one(self, receipts, monkeypatch):
        _write(receipts, "latest.json", {"outcome": "PREVIOUS RUN"})
        self._break_pointer_write(monkeypatch)
        update_receipt.begin_update_receipt()
        update_receipt.finalize_update_receipt("success")

        # The reader only reads; the rename patch is irrelevant to it, and
        # undoing every patch here would also drop the receipt-dir fixture.
        got = update_receipt.read_latest_receipt()
        assert got is not None and got.get("outcome") == "success", got

    def test_no_temp_file_is_left_behind_when_the_rename_fails(self, receipts, monkeypatch):
        self._break_pointer_write(monkeypatch)
        update_receipt.begin_update_receipt()
        update_receipt.finalize_update_receipt("success")

        leftovers = list(receipts.glob(".latest-*.tmp"))
        assert leftovers == [], f"atomic-write scratch file leaked: {leftovers}"

    def test_a_healthy_run_still_writes_the_pointer(self, receipts):
        update_receipt.begin_update_receipt()
        assert update_receipt.finalize_update_receipt("success") is not None
        pointer = receipts / "latest.json"
        assert pointer.is_file()
        assert json.loads(pointer.read_text(encoding="utf-8"))["outcome"] == "success"
