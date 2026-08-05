"""Linear event/readback boundary tests for Kanban orchestration."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_linear as linear


NOW = 2_000_000_000
ISSUE_ID = "linear-issue-uuid-1"
IDENTIFIER = "ECH-999"
REPO = "echlon-bank/echlon-bank"
SECRET = "test-only-linear-inbox-secret"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


class FakeLinearProvider:
    def __init__(self, snapshot: linear.LinearIssueSnapshot):
        self.snapshot = snapshot
        self.calls: list[str] = []

    def read_issue(self, issue_id: str) -> linear.LinearIssueSnapshot:
        self.calls.append(issue_id)
        return self.snapshot


class FakePullRequestProvider:
    def __init__(self, *snapshots: linear.PullRequestSnapshot):
        self.snapshots = {snapshot.ref: snapshot for snapshot in snapshots}
        self.calls: list[tuple[linear.PullRequestRef, ...]] = []

    def read_pull_requests(
        self,
        refs: tuple[linear.PullRequestRef, ...],
    ) -> tuple[linear.PullRequestSnapshot, ...]:
        self.calls.append(refs)
        return tuple(self.snapshots[ref] for ref in refs if ref in self.snapshots)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = home / "kanban" / "boards" / "echlon-linear-fixes" / "kanban.db"
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(path)
    return path


def _ref(number: int) -> linear.PullRequestRef:
    return linear.PullRequestRef(REPO, number)


def _issue(
    revision: int,
    *attachments: linear.PullRequestRef,
    title: str = "Implement the exact-head gate",
    attachments_complete: bool = True,
) -> linear.LinearIssueSnapshot:
    return linear.LinearIssueSnapshot(
        issue_id=ISSUE_ID,
        identifier=IDENTIFIER,
        title=title,
        issue_url="https://linear.app/echlon/issue/ECH-999/test",
        source_revision=revision,
        attachments=(tuple(attachments) if attachments_complete else None),
    )


def _pr(
    number: int,
    *,
    state: Literal["open", "closed", "merged"] = "open",
    head_sha: str = HEAD_A,
    revision: int = 1,
    observed_at: int = NOW,
) -> linear.PullRequestSnapshot:
    return linear.PullRequestSnapshot(
        ref=_ref(number),
        pr_url=f"https://github.com/Echlon-Bank/Echlon-Bank/pull/{number}",
        state=state,
        is_draft=False,
        base_branch="main",
        head_branch=f"ech-{number}-implementation",
        head_sha=head_sha,
        provider_revision=revision,
        observed_at=observed_at,
    )


def _body(
    event_id: str,
    revision: int,
    *,
    event_kind: str = "issue",
    source_key: str = f"issue:{ISSUE_ID}",
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "provider": "linear",
            "event_id": event_id,
            "event_kind": event_kind,
            "issue_id": ISSUE_ID,
            "source_key": source_key,
            "source_revision": revision,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _ingest(
    conn,
    event_id: str,
    revision: int,
    *,
    event_kind: str = "issue",
    source_key: str = f"issue:{ISSUE_ID}",
):
    body = _body(
        event_id,
        revision,
        event_kind=event_kind,
        source_key=source_key,
    )
    return linear.ingest_signed_event(
        conn,
        body=body,
        timestamp=NOW,
        signature=linear.sign_event_body(SECRET, NOW, body),
        secret=SECRET,
        now=NOW,
    )


def _apply(
    conn,
    event_id: str,
    issue_provider: FakeLinearProvider,
    pr_provider: FakePullRequestProvider,
    *,
    now: int = NOW,
):
    plan = linear.build_reconciliation_plan(
        conn,
        event_id=event_id,
        issue_provider=issue_provider,
        pr_provider=pr_provider,
        now=now,
    )
    return linear.apply_reconciliation_plan(conn, plan, now=now)


def test_schema_and_one_issue_one_pr_flow_keep_wakeup_readback_and_apply_separate(
    db_path: Path,
):
    with kb.connect(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "linear_issue_coordinators",
            "linear_event_inbox",
            "linear_issue_pr_links",
            "linear_pr_aggregates",
            "linear_pr_head_generations",
        } <= tables
        assert {
            "uq_linear_event_provider_id",
            "uq_linear_event_source_revision",
        } <= indexes

        receipt = _ingest(conn, "evt-1", 1)
        assert receipt.created is True
        assert linear.get_issue_coordinator(conn, ISSUE_ID) is None

        issue_provider = FakeLinearProvider(_issue(1, _ref(101)))
        pr_provider = FakePullRequestProvider(_pr(101))
        plan = linear.build_reconciliation_plan(
            conn,
            event_id="evt-1",
            issue_provider=issue_provider,
            pr_provider=pr_provider,
            now=NOW,
        )
        assert linear.get_issue_coordinator(conn, ISSUE_ID) is None
        assert conn.execute("SELECT COUNT(*) FROM linear_pr_aggregates").fetchone()[0] == 0

        result = linear.apply_reconciliation_plan(conn, plan, now=NOW)
        assert result.outcome == "created"
        coordinator = linear.get_issue_coordinator(conn, ISSUE_ID)
        assert coordinator is not None
        assert coordinator.issue_id == ISSUE_ID
        assert coordinator.identifier == IDENTIFIER
        assert coordinator.source_revision == 1
        assert linear.list_issue_pr_refs(conn, ISSUE_ID) == (_ref(101),)

        current = linear.resolve_current_pr_aggregates(conn, ISSUE_ID)
        assert [aggregate.ref for aggregate in current] == [_ref(101)]
        assert current[0].current_head_sha == HEAD_A
        assert linear.list_head_generations(conn, _ref(101)) == (HEAD_A,)

        replay = linear.apply_reconciliation_plan(conn, plan, now=NOW + 1)
        assert replay.outcome == "processed"
        assert replay.changed is False
        assert conn.execute(
            "SELECT COUNT(*) FROM linear_issue_coordinators"
        ).fetchone()[0] == 1


def test_one_issue_many_prs_preserves_merged_history_and_resolves_only_open_readback(
    db_path: Path,
):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-many", 4, event_kind="attachment")
        issue_provider = FakeLinearProvider(_issue(4, _ref(201), _ref(202)))
        pr_provider = FakePullRequestProvider(
            _pr(201, state="merged", revision=9),
            _pr(202, state="open", head_sha=HEAD_B, revision=3),
        )
        result = _apply(conn, "evt-many", issue_provider, pr_provider)

        assert result.outcome == "created"
        assert linear.list_issue_pr_refs(conn, ISSUE_ID) == (_ref(201), _ref(202))
        current = linear.resolve_current_pr_aggregates(conn, ISSUE_ID)
        assert [(aggregate.ref, aggregate.state) for aggregate in current] == [
            (_ref(202), "open")
        ]
        merged = conn.execute(
            "SELECT state, current_head_sha FROM linear_pr_aggregates "
            "WHERE repository=? AND pr_number=201",
            (REPO,),
        ).fetchone()
        assert dict(merged) == {"state": "merged", "current_head_sha": HEAD_A}


def test_duplicate_delivery_and_source_revision_replay_are_idempotent(
    db_path: Path,
):
    with kb.connect(db_path) as conn:
        first = _ingest(conn, "evt-dup", 7, event_kind="comment", source_key="comment:c1")
        same_id = _ingest(
            conn,
            "evt-dup",
            7,
            event_kind="comment",
            source_key="comment:c1",
        )
        new_id_same_revision = _ingest(
            conn,
            "evt-replayed",
            7,
            event_kind="comment",
            source_key="comment:c1",
        )

        assert first.created is True
        assert same_id.created is False
        assert same_id.duplicate_reason == "event_id"
        assert new_id_same_revision.created is False
        assert new_id_same_revision.duplicate_reason == "source_revision"
        assert new_id_same_revision.inbox_id == first.inbox_id
        assert conn.execute("SELECT COUNT(*) FROM linear_event_inbox").fetchone()[0] == 1

        _apply(
            conn,
            "evt-dup",
            FakeLinearProvider(_issue(7)),
            FakePullRequestProvider(),
        )
        replay_after_processing = _ingest(
            conn,
            "evt-dup",
            7,
            event_kind="comment",
            source_key="comment:c1",
        )
        assert replay_after_processing.status == "processed"
        assert conn.execute(
            "SELECT COUNT(*) FROM linear_issue_coordinators"
        ).fetchone()[0] == 1


def test_out_of_order_delivery_converges_on_one_coordinator(db_path: Path):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-newer", 2)
        _ingest(conn, "evt-older", 1)
        issue_provider = FakeLinearProvider(_issue(2, _ref(301)))
        pr_provider = FakePullRequestProvider(_pr(301, revision=2))

        newer = _apply(conn, "evt-newer", issue_provider, pr_provider)
        older = _apply(conn, "evt-older", issue_provider, pr_provider, now=NOW + 1)

        assert newer.coordinator_revision == 2
        assert older.coordinator_revision == 2
        assert older.outcome == "refreshed"
        assert older.changed is False
        assert conn.execute(
            "SELECT COUNT(*) FROM linear_issue_coordinators"
        ).fetchone()[0] == 1
        statuses = {
            row["event_id"]: row["status"]
            for row in conn.execute("SELECT event_id, status FROM linear_event_inbox")
        }
        assert statuses == {"evt-newer": "processed", "evt-older": "processed"}


def test_stale_source_revision_cannot_roll_back_coordinator_or_pr(db_path: Path):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-current", 5)
        _apply(
            conn,
            "evt-current",
            FakeLinearProvider(_issue(5, _ref(401), title="current title")),
            FakePullRequestProvider(_pr(401, head_sha=HEAD_B, revision=5)),
        )
        _ingest(conn, "evt-stale", 4)
        stale = _apply(
            conn,
            "evt-stale",
            FakeLinearProvider(_issue(4, _ref(401), title="stale title")),
            FakePullRequestProvider(_pr(401, head_sha=HEAD_A, revision=4)),
            now=NOW + 1,
        )

        assert stale.outcome == "stale"
        coordinator = linear.get_issue_coordinator(conn, ISSUE_ID)
        assert coordinator is not None
        assert coordinator.title == "current title"
        assert coordinator.source_revision == 5
        current = linear.resolve_current_pr_aggregates(conn, ISSUE_ID)
        assert current[0].current_head_sha == HEAD_B
        assert conn.execute(
            "SELECT status FROM linear_event_inbox WHERE event_id='evt-stale'"
        ).fetchone()[0] == "stale"


def test_source_revision_ahead_of_live_readback_defers_without_transition(
    db_path: Path,
):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-ahead", 3)
        lagging_issue = FakeLinearProvider(_issue(2, _ref(501)))
        pr_provider = FakePullRequestProvider(_pr(501, revision=2))
        result = _apply(conn, "evt-ahead", lagging_issue, pr_provider)

        assert result.outcome == "source_not_visible"
        assert linear.get_issue_coordinator(conn, ISSUE_ID) is None
        inbox = conn.execute(
            "SELECT status, read_attempt_count, last_error "
            "FROM linear_event_inbox WHERE event_id='evt-ahead'"
        ).fetchone()
        assert inbox["status"] == "pending"
        assert inbox["read_attempt_count"] == 1
        assert "behind inbox revision" in inbox["last_error"]

        lagging_issue.snapshot = _issue(3, _ref(501))
        pr_provider.snapshots[_ref(501)] = _pr(501, revision=3)
        retried = _apply(
            conn,
            "evt-ahead",
            lagging_issue,
            pr_provider,
            now=NOW + 1,
        )
        assert retried.outcome == "created"
        assert retried.coordinator_revision == 3


def test_missing_attachment_data_is_non_destructive_and_skips_pr_readback(
    db_path: Path,
):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-with-attachment", 1)
        initial_pr_provider = FakePullRequestProvider(_pr(601))
        _apply(
            conn,
            "evt-with-attachment",
            FakeLinearProvider(_issue(1, _ref(601))),
            initial_pr_provider,
        )

        _ingest(conn, "evt-missing-attachment-data", 2, event_kind="attachment")
        missing_pr_provider = FakePullRequestProvider()
        result = _apply(
            conn,
            "evt-missing-attachment-data",
            FakeLinearProvider(
                _issue(
                    2,
                    attachments_complete=False,
                    title="updated without attachment payload",
                )
            ),
            missing_pr_provider,
            now=NOW + 1,
        )

        assert result.outcome == "updated"
        assert missing_pr_provider.calls == []
        assert linear.list_issue_pr_refs(conn, ISSUE_ID) == (_ref(601),)
        assert [aggregate.ref for aggregate in linear.resolve_current_pr_aggregates(
            conn,
            ISSUE_ID,
        )] == [_ref(601)]


def test_missing_trusted_pr_snapshot_records_association_but_not_current_state(
    db_path: Path,
):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-pr-unavailable", 1, event_kind="attachment")
        _apply(
            conn,
            "evt-pr-unavailable",
            FakeLinearProvider(_issue(1, _ref(701))),
            FakePullRequestProvider(),
        )

        assert linear.list_issue_pr_refs(conn, ISSUE_ID) == (_ref(701),)
        assert linear.resolve_current_pr_aggregates(conn, ISSUE_ID) == ()
        assert conn.execute("SELECT COUNT(*) FROM linear_pr_aggregates").fetchone()[0] == 0


def test_head_generations_are_full_sha_keys_from_trusted_provider(db_path: Path):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-head-a", 1)
        _apply(
            conn,
            "evt-head-a",
            FakeLinearProvider(_issue(1, _ref(801))),
            FakePullRequestProvider(_pr(801, head_sha=HEAD_A, revision=1)),
        )
        _ingest(conn, "evt-head-b", 2)
        _apply(
            conn,
            "evt-head-b",
            FakeLinearProvider(_issue(2, _ref(801))),
            FakePullRequestProvider(
                _pr(
                    801,
                    head_sha=HEAD_B,
                    revision=2,
                    observed_at=NOW + 1,
                )
            ),
            now=NOW + 1,
        )

        assert linear.list_head_generations(conn, _ref(801)) == (HEAD_A, HEAD_B)
        assert linear.resolve_current_pr_aggregates(
            conn,
            ISSUE_ID,
        )[0].current_head_sha == HEAD_B
        with pytest.raises(linear.LinearBoundaryError, match="full 40-character"):
            _pr(801, head_sha="branch-name-is-not-a-sha", revision=3)


def test_linear_prose_cannot_override_trusted_pr_state_or_head(db_path: Path):
    with kb.connect(db_path) as conn:
        _ingest(conn, "evt-prose", 1, event_kind="comment", source_key="comment:c-prose")
        issue_provider = FakeLinearProvider(
            _issue(
                1,
                _ref(901),
                title=(
                    "PR 901 merged at deadbeef; branch says obsolete-head "
                    "(untrusted prose)"
                ),
            )
        )
        pr_provider = FakePullRequestProvider(
            _pr(901, state="open", head_sha=HEAD_B, revision=11)
        )
        _apply(conn, "evt-prose", issue_provider, pr_provider)

        current = linear.resolve_current_pr_aggregates(conn, ISSUE_ID)
        assert len(current) == 1
        assert current[0].state == "open"
        assert current[0].current_head_sha == HEAD_B


def test_signed_boundary_rejects_tampering_stale_signatures_and_unknown_payloads(
    db_path: Path,
):
    with kb.connect(db_path) as conn:
        body = _body("evt-signed", 1)
        with pytest.raises(linear.LinearSignatureError, match="invalid"):
            linear.ingest_signed_event(
                conn,
                body=body + b" ",
                timestamp=NOW,
                signature=linear.sign_event_body(SECRET, NOW, body),
                secret=SECRET,
                now=NOW,
            )
        with pytest.raises(linear.LinearSignatureError, match="stale"):
            linear.ingest_signed_event(
                conn,
                body=body,
                timestamp=NOW - 301,
                signature=linear.sign_event_body(SECRET, NOW - 301, body),
                secret=SECRET,
                now=NOW,
            )

        unsupported = json.dumps(
            {
                **json.loads(body),
                "linear_comment_prose": "trust me, the PR merged",
            },
            sort_keys=True,
        ).encode()
        with pytest.raises(linear.LinearBoundaryError, match="unsupported fields"):
            linear.ingest_signed_event(
                conn,
                body=unsupported,
                timestamp=NOW,
                signature=linear.sign_event_body(SECRET, NOW, unsupported),
                secret=SECRET,
                now=NOW,
            )
        assert conn.execute("SELECT COUNT(*) FROM linear_event_inbox").fetchone()[0] == 0
