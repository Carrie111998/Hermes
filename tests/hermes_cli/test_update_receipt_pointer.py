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
        _write(receipts, "20260101-000000.json", {"outcome": "older"})
        assert update_receipt.read_latest_receipt()["outcome"] == "pointed-at"

    def test_a_missing_pointer_falls_back_to_the_newest_receipt(self, receipts):
        _write(receipts, "20260101-000000.json", {"outcome": "older"})
        _write(receipts, "20260823-120000.json", {"outcome": "newest"})

        assert update_receipt.read_latest_receipt()["outcome"] == "newest", (
            "with no pointer the reader reported nothing, so a completed "
            "update looks like it never ran"
        )

    def test_a_corrupt_pointer_falls_back_rather_than_reporting_nothing(self, receipts):
        (receipts / "latest.json").write_text("{ not json", encoding="utf-8")
        _write(receipts, "20260823-120000.json", {"outcome": "newest"})
        assert update_receipt.read_latest_receipt()["outcome"] == "newest"

    def test_an_empty_directory_is_still_none(self, receipts):
        assert update_receipt.read_latest_receipt() is None

    def test_the_pointer_itself_is_never_scanned_as_a_receipt(self, receipts):
        """`latest.json` sorts after the timestamps; it must not win the scan."""
        _write(receipts, "20260823-120000.json", {"outcome": "newest"})
        assert update_receipt.read_latest_receipt()["outcome"] == "newest"


class TestWriter:
    """Driven through the real finalize path, not a test-only seam."""

    def test_a_failed_pointer_write_removes_the_stale_one(self, receipts, monkeypatch, caplog):
        _write(receipts, "latest.json", {"outcome": "PREVIOUS RUN"})

        real_write = update_receipt.Path.write_text

        def _fail_on_pointer(self, data, **kw):
            if self.name == "latest.json":
                raise OSError("read-only")
            return real_write(self, data, **kw)

        monkeypatch.setattr(update_receipt.Path, "write_text", _fail_on_pointer)
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

        real_write = update_receipt.Path.write_text

        def _fail_on_pointer(self, data, **kw):
            if self.name == "latest.json":
                raise OSError("read-only")
            return real_write(self, data, **kw)

        monkeypatch.setattr(update_receipt.Path, "write_text", _fail_on_pointer)
        update_receipt.begin_update_receipt()
        update_receipt.finalize_update_receipt("success")

        # The reader only reads; the write patch is irrelevant to it, and
        # undoing every patch here would also drop the receipt-dir fixture.
        got = update_receipt.read_latest_receipt()
        assert got is not None and got.get("outcome") == "success", got

    def test_a_healthy_run_still_writes_the_pointer(self, receipts):
        update_receipt.begin_update_receipt()
        assert update_receipt.finalize_update_receipt("success") is not None
        assert (receipts / "latest.json").is_file()
