"""Deterministic recovery for activations the event path missed.

Events are primary but not exclusive: a dropped event, a dispatcher restart,
or a message written while the subscriber was down would otherwise sit in an
inbox forever. This scan is the safety net, and it must reach the same verdict
as the dispatcher — same routing table, same claim ledger.

It routes from the FILENAME only. Real names look like
``20260810T200409_TAILOR_REQUEST_tracker_3af5f853.json`` and sometimes carry a
sequence number (``..._01_TAILOR_REQUEST_main.json``), so positional parsing is
fragile; matching the closed set of known types is not.
"""

from __future__ import annotations

import json

import pytest

from jobflow_dispatch.reconcile import scan_actionable
from jobflow_dispatch.store import ActivationStore


@pytest.fixture
def root(tmp_path):
    for dest in ("matcher", "tailor", "researcher", "main"):
        (tmp_path / dest / "inbox").mkdir(parents=True)
        (tmp_path / dest / "processed").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return ActivationStore(tmp_path / "dispatch.db", lease_seconds=900)


def _msg(root, dest, name, body=None, where="inbox"):
    p = root / dest / where / name
    p.write_text(json.dumps(body if body is not None else {"job_id": "j1"}), encoding="utf-8")
    return p


class TestFindsRealWork:
    def test_unclaimed_request_becomes_an_activation(self, root, store):
        _msg(root, "tailor", "20260810T200409_TAILOR_REQUEST_tracker_3af5f853.json")

        found = scan_actionable(root, store, now=1000)

        assert len(found) == 1
        assert found[0].activity_id == "jobflow.tailor.generate"
        assert found[0].message_key.endswith("20260810T200409_TAILOR_REQUEST_tracker_3af5f853.json")
        assert found[0].reason == "reconcile"

    def test_sequence_numbered_filename_still_routes(self, root, store):
        _msg(root, "tailor", "20260412T162256_01_TAILOR_REQUEST_main.json")
        assert len(scan_actionable(root, store, now=1000)) == 1

    def test_longest_type_match_wins(self, root, store):
        """TAILOR_MODULE_REQUEST must not be read as TAILOR_REQUEST."""
        _msg(root, "tailor", "20260810T2004_TAILOR_MODULE_REQUEST_main_a1.json")
        found = scan_actionable(root, store, now=1000)
        assert [a.activity_id for a in found] == ["jobflow.tailor.generate"]


class TestRespectsTheLedger:
    def test_freshly_claimed_work_is_skipped(self, root, store):
        p = _msg(root, "matcher", "20260810T01_SCORE_REQUEST_tracker_aa.json")
        key = scan_actionable(root, store, now=1000)[0].message_key
        store.claim(key, "cron.jobflow.matcher", now=1000)

        assert scan_actionable(root, store, now=1010) == []
        assert p.exists()

    def test_completed_work_is_never_resurfaced(self, root, store):
        _msg(root, "matcher", "20260810T01_SCORE_REQUEST_tracker_aa.json")
        key = scan_actionable(root, store, now=1000)[0].message_key
        store.claim(key, "cron.jobflow.matcher", now=1000)
        store.complete(key, "cron.jobflow.matcher", outcome="succeeded", now=1005)

        assert scan_actionable(root, store, now=10_000_000) == []

    def test_expired_claim_is_resurfaced(self, root, store):
        _msg(root, "matcher", "20260810T01_SCORE_REQUEST_tracker_aa.json")
        key = scan_actionable(root, store, now=1000)[0].message_key
        store.claim(key, "cron.jobflow.matcher", now=1000)

        assert len(scan_actionable(root, store, now=1000 + 901)) == 1


class TestIgnoresNonWork:
    def test_results_and_notifications_are_not_work(self, root, store):
        _msg(root, "main", "20260810T01_NOTIFICATION_sentinel_aa.json")
        _msg(root, "main", "20260810T01_SCORE_RESULT_matcher_bb.json")
        _msg(root, "main", "20260810T01_ERROR_applier_cc.json")

        assert scan_actionable(root, store, now=1000) == []

    def test_processed_directory_is_ignored(self, root, store):
        _msg(root, "tailor", "20260810T01_TAILOR_REQUEST_main_aa.json", where="processed")
        assert scan_actionable(root, store, now=1000) == []

    def test_misrouted_request_is_not_work(self, root, store):
        """A SCORE_REQUEST sitting in tailor/inbox is a bug, not a job."""
        _msg(root, "tailor", "20260810T01_SCORE_REQUEST_tracker_aa.json")
        assert scan_actionable(root, store, now=1000) == []

    def test_unknown_destination_is_ignored(self, root, store):
        (root / "curator" / "inbox").mkdir(parents=True)
        _msg(root, "curator", "20260810T01_SCORE_REQUEST_tracker_aa.json")
        assert scan_actionable(root, store, now=1000) == []

    def test_non_json_files_are_ignored(self, root, store):
        (root / "tailor" / "inbox" / "20260810T01_TAILOR_REQUEST_main.txt").write_text("x")
        assert scan_actionable(root, store, now=1000) == []


class TestRobustness:
    def test_malformed_json_is_still_actionable(self, root, store):
        """Routing is filename-based; a bad body must not hide real work."""
        p = root / "tailor" / "inbox" / "20260810T01_TAILOR_REQUEST_main_aa.json"
        p.write_text("{not json", encoding="utf-8")

        found = scan_actionable(root, store, now=1000)
        assert len(found) == 1
        assert found[0].correlation_id is None

    def test_correlation_id_is_read_when_present(self, root, store):
        _msg(root, "tailor", "20260810T01_TAILOR_REQUEST_main_aa.json",
             body={"correlation_id": "corr-9"})
        assert scan_actionable(root, store, now=1000)[0].correlation_id == "corr-9"

    def test_missing_mailbox_root_is_empty_not_an_error(self, tmp_path, store):
        assert scan_actionable(tmp_path / "nope", store, now=1000) == []

    def test_no_message_body_reaches_the_activation(self, root, store):
        """Never let packet contents leak into an activation record."""
        secret = "sk-live-must-not-appear"
        _msg(root, "tailor", "20260810T01_TAILOR_REQUEST_main_aa.json",
             body={"token": secret, "correlation_id": "c1"})

        blob = repr(scan_actionable(root, store, now=1000))
        assert secret not in blob
