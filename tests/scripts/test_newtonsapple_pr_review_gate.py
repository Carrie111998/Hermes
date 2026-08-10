"""Contract tests for the downtime-tolerant NewtonsApple review gate."""

import json
import base64
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import newtonsapple_pr_review_gate as gate
from scripts.newtonsapple_pr_review_gate import (
    ReviewStateStore,
    ReviewTuple,
    _buzz_find,
    _gate_webhook,
    select_authorized_tuple,
    drain_summary_outbox,
)


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
REVIEW_REQUEST_ID = 123456


def _execution_request(operation):
    return {
        "operation": operation,
        "contract_version": "v2",
        "repository": "NewtonsAppleAI/newtonsapple-web",
        "pr_number": "185",
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "review_request_id": REVIEW_REQUEST_ID,
    }


def _install_attestation_key(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setenv(
        "NEWTONSAPPLE_REVIEW_ATTESTATION_PRIVATE_KEY",
        base64.b64encode(raw).decode(),
    )
    return private_key


def test_gate_resolution_is_signed_and_local_only(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)

    result = gate.resolve_execution_gates(
        _execution_request("resolve_execution_gates")
    )

    payload = base64.b64decode(result["gate_resolution_payload"])
    private_key.public_key().verify(
        base64.b64decode(result["gate_resolution_signature"]), payload
    )
    manifest = json.loads(payload)
    assert manifest["resolved_gates"] == ["quality", "integration", "e2e"]
    assert manifest["policy_sha256"] == gate.EXECUTION_GATE_POLICY_SHA256
    assert manifest["gate_contracts"]["quality"] == {
        "kind": "command",
        "command": ["npm", "run", "check"],
        "executor": "review_worker",
        "runner": {"kind": "review_worker", "name": "docker-node22"},
        "statuses": ["pass", "pr-fail", "unavailable"],
        "exit_codes": list(range(0, 256)),
    }


def test_execution_evidence_is_signed_from_the_local_worker(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)
    monkeypatch.setattr(
        gate,
        "_run_local_execution_worker",
        lambda review_tuple: {
            "base_tree_sha": "c" * 40,
            "head_tree_sha": "d" * 40,
            "worker": {"required": True, "isolation": "docker"},
            "gates": [
                gate._unavailable_local_gate(name, "Docker unavailable")
                for name in gate.BASELINE_EXECUTION_GATES
            ],
        },
    )

    result = gate.execution_evidence(_execution_request("execution_evidence"))

    payload = base64.b64decode(result["attestation_payload"])
    private_key.public_key().verify(
        base64.b64decode(result["attestation_signature"]), payload
    )
    report = json.loads(payload)
    assert report["worker"] == {"required": True, "isolation": "docker"}
    assert [item["id"] for item in report["gates"]] == [
        "quality",
        "integration",
        "e2e",
    ]
    assert {item["status"] for item in report["gates"]} == {"unavailable"}


def test_review_state_store_uses_wal_and_reclaims_only_expired_leases(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    assert store.journal_mode() == "wal"
    first_token = store.reserve(review_tuple, now=100, lease_seconds=30)
    assert isinstance(first_token, str)
    assert store.reserve(review_tuple, now=129, lease_seconds=30) is None
    second_token = store.reserve(review_tuple, now=130, lease_seconds=30)
    assert isinstance(second_token, str)
    assert second_token != first_token

    with pytest.raises(ValueError, match="lease not found"):
        store.release(review_tuple, lease_token=first_token)
    store.release(review_tuple, lease_token=second_token)
    third_token = store.reserve(review_tuple, now=131, lease_seconds=30)
    assert isinstance(third_token, str)
    store.complete(review_tuple, lease_token=third_token, now=132)
    assert store.reserve(review_tuple, now=10_000, lease_seconds=30) is None


def test_new_request_generation_bypasses_prior_completion_and_dead_letter(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    completed = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        request_id=1,
    )
    dead_lettered = ReviewTuple(
        repository=completed.repository,
        pr_number=completed.pr_number,
        base_sha=completed.base_sha,
        head_sha=completed.head_sha,
        request_id=2,
    )
    current = ReviewTuple(
        repository=completed.repository,
        pr_number=completed.pr_number,
        base_sha=completed.base_sha,
        head_sha=completed.head_sha,
        request_id=3,
    )

    completed_lease = store.reserve(completed, now=100, lease_seconds=30)
    assert isinstance(completed_lease, str)
    store.complete(completed, lease_token=completed_lease, now=101)

    dead_lettered_lease = store.reserve(dead_lettered, now=102, lease_seconds=30)
    assert isinstance(dead_lettered_lease, str)
    failure = store.record_failure(
        dead_lettered,
        lease_token=dead_lettered_lease,
        now=103,
        retry_delay=60,
        max_attempts=1,
        dead_letter_marker="dead-letter-marker",
        dead_letter_content="dead-letter-content",
    )
    assert failure["dead_lettered"] is True

    assert store.reserve(completed, now=104, lease_seconds=30) is None
    assert store.reserve(dead_lettered, now=104, lease_seconds=30) is None
    assert isinstance(store.reserve(current, now=104, lease_seconds=30), str)


def test_publication_claim_is_single_use_token_fenced_and_extends_lease(tmp_path):
    first_store = ReviewStateStore(tmp_path / "review.sqlite3")
    second_store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    lease_token = first_store.reserve(review_tuple, now=100, lease_seconds=30)
    assert isinstance(lease_token, str)

    first_store.claim_publication(
        review_tuple,
        lease_token=lease_token,
        now=120,
        lease_seconds=300,
    )

    assert second_store.reserve(review_tuple, now=131, lease_seconds=30) is None
    with pytest.raises(ValueError, match="review publication claim not found"):
        first_store.claim_publication(
            review_tuple,
            lease_token=lease_token,
            now=121,
            lease_seconds=300,
        )
    replacement = second_store.reserve(review_tuple, now=420, lease_seconds=30)
    assert isinstance(replacement, str)


def test_settlement_control_plane_claims_publication_before_side_effect(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    lease_token = store.reserve(review_tuple, now=100, lease_seconds=60)
    assert isinstance(lease_token, str)
    monkeypatch.setattr(gate.time, "time", lambda: 120)
    monkeypatch.setattr(
        gate,
        "_recoverable_live_tuple",
        lambda *args: (_live_pr(), review_tuple, []),
    )
    payload = {
        "operation": "claim_publish",
        "contract_version": "v2",
        "repository": review_tuple.repository,
        "pr_number": review_tuple.pr_number,
        "base_sha": review_tuple.base_sha,
        "head_sha": review_tuple.head_sha,
        "review_request_id": review_tuple.request_id,
        "lease_token": lease_token,
    }

    assert gate._settle(payload, "newtonsapple-bot", store) == {
        "settled": "claim_publish"
    }
    with pytest.raises(ValueError, match="review publication claim not found"):
        gate._settle(payload, "newtonsapple-bot", store)


def test_publication_claim_rejects_a_superseded_request_generation(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    stale = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        request_id=1,
    )
    current = ReviewTuple(
        repository=stale.repository,
        pr_number=stale.pr_number,
        base_sha=stale.base_sha,
        head_sha=stale.head_sha,
        request_id=2,
    )
    lease_token = store.reserve(stale, now=100, lease_seconds=60)
    assert isinstance(lease_token, str)
    monkeypatch.setattr(gate.time, "time", lambda: 120)
    monkeypatch.setattr(
        gate,
        "_recoverable_live_tuple",
        lambda *args: (_live_pr(), current, []),
    )

    with pytest.raises(RuntimeError, match="generation changed before publication"):
        gate._settle(
            {
                "operation": "claim_publish",
                "contract_version": "v2",
                "repository": stale.repository,
                "pr_number": stale.pr_number,
                "base_sha": stale.base_sha,
                "head_sha": stale.head_sha,
                "review_request_id": stale.request_id,
                "lease_token": lease_token,
            },
            "newtonsapple-bot",
            store,
        )

    assert store.active_lease(stale, lease_token=lease_token, now=120) is True


def test_complete_settlement_records_review_without_commit_status(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    lease_token = store.reserve(review_tuple, now=100, lease_seconds=60)
    assert isinstance(lease_token, str)
    live_pr = _live_pr()
    marker_body = (
        "## Findings\n\n"
        "### P2 — Example finding\n\n"
        "Detailed impact that belongs only on GitHub.\n\n"
        "## Verification\n\n"
        "| Gate / command | Status | SHA | Executor | Evidence |\n"
        "|---|---|---|---|---|\n"
        f"| `npm run check` | **unavailable** | `{HEAD_SHA}` | worker | offline |\n\n"
        f"{gate.review_marker(review_tuple)}"
    )
    monkeypatch.setattr(
        gate,
        "_recoverable_live_tuple",
        lambda *args: (live_pr, review_tuple, [marker_body]),
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    delivered = []

    def buzz_send(content, reply_to=None):
        delivered.append((content, reply_to))
        return f"buzz-{len(delivered)}"

    monkeypatch.setattr(
        gate, "_buzz_send", buzz_send
    )
    monkeypatch.setattr(gate.time, "time", lambda: 120)

    result = gate._settle(
        {
            "operation": "complete",
            "contract_version": "v2",
            "repository": review_tuple.repository,
            "pr_number": review_tuple.pr_number,
            "base_sha": review_tuple.base_sha,
            "head_sha": review_tuple.head_sha,
            "review_request_id": review_tuple.request_id,
            "lease_token": lease_token,
        },
        "newtonsapple-bot",
        store,
    )

    assert result == {"settled": "complete", "outbox_delivered": 1}
    assert store.reserve(review_tuple, now=200, lease_seconds=30) is None
    assert len(delivered) == 3
    requested, started, completed = delivered
    assert requested[1] is None
    assert "PR review requested" in requested[0]
    assert started[1] == "buzz-1"
    assert "PR review started" in started[0]
    assert completed[1] == "buzz-1"
    assert "1 actionable finding; highest is P2: Example finding" in completed[0]
    assert "Gates: 0 PASS, 0 FAIL, 1 UNAVAILABLE" in completed[0]
    assert "Detailed impact" not in completed[0]


def test_review_failures_back_off_and_eventually_dead_letter(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    first_token = store.reserve(review_tuple, now=100, lease_seconds=30)
    assert isinstance(first_token, str)
    first = store.record_failure(
        review_tuple,
        lease_token=first_token,
        now=110,
        retry_delay=60,
        max_attempts=2,
        dead_letter_marker="dead-letter-marker",
        dead_letter_content="review failed permanently",
    )
    assert first == {"attempts": 1, "dead_lettered": False, "retry_after": 170}
    assert store.pending_summaries() == [
        {
            "id": 1,
            "key": f"retry:1:{gate.tuple_key(review_tuple)}",
            "marker": gate._retry_marker(review_tuple, 1),
            "content": gate._retry_content(
                review_tuple,
                attempt=1,
                max_attempts=2,
                retry_after=170,
                failure_reason="the review run ended before formal publication",
            ),
        }
    ]
    assert store.reserve(review_tuple, now=169, lease_seconds=30) is None
    second_token = store.reserve(review_tuple, now=170, lease_seconds=30)
    assert isinstance(second_token, str)

    second = store.record_failure(
        review_tuple,
        lease_token=second_token,
        now=180,
        retry_delay=60,
        max_attempts=2,
        dead_letter_marker="dead-letter-marker",
        dead_letter_content="review failed permanently",
    )
    assert second == {"attempts": 2, "dead_lettered": True, "retry_after": None}
    assert store.pending_summaries()[-1] == {
        "id": 2,
        "key": f"blocker:dead-letter:{gate.tuple_key(review_tuple)}",
        "marker": "dead-letter-marker",
        "content": "review failed permanently",
    }
    assert store.reserve(review_tuple, now=10_000, lease_seconds=30) is None


def test_release_settlement_never_publishes_a_github_status(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    monkeypatch.setattr(
        gate,
        "gh_json",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("release must not publish a GitHub status")
        ),
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    delivered = []
    monkeypatch.setattr(
        gate,
        "_buzz_send",
        lambda content, reply_to=None: delivered.append((content, reply_to))
        or "buzz-blocker",
    )

    result = None
    for attempt in range(gate.MAX_REVIEW_ATTEMPTS):
        now = 100 + attempt * (gate.RETRY_DELAY_SECONDS + 10)
        lease_token = store.reserve(review_tuple, now=now, lease_seconds=30)
        assert isinstance(lease_token, str)
        monkeypatch.setattr(gate.time, "time", lambda current=now: current)
        result = gate._settle(
            {
                "operation": "release",
                "contract_version": "v2",
                "repository": review_tuple.repository,
                "pr_number": review_tuple.pr_number,
                "base_sha": review_tuple.base_sha,
                "head_sha": review_tuple.head_sha,
                "review_request_id": review_tuple.request_id,
                "lease_token": lease_token,
                "failure_code": "review_evidence_incomplete",
            },
            "newtonsapple-bot",
            store,
        )

    assert result == {
        "settled": "release",
        "attempts": gate.MAX_REVIEW_ATTEMPTS,
        "dead_lettered": True,
        "retry_after": None,
    }
    assert len(delivered) == gate.MAX_REVIEW_ATTEMPTS
    assert "No GitHub review was posted" in delivered[0][0]
    assert "immutable review evidence was incomplete" in delivered[0][0]
    assert "No GitHub review was published" in delivered[-1][0]


def test_webhook_gate_returns_the_opaque_lease_token(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    monkeypatch.setattr(
        "scripts.newtonsapple_pr_review_gate._live_review_state",
        lambda number, login: (live_pr, []),
    )
    monkeypatch.setattr(
        gate,
        "_load_timeline",
        lambda number: [
            {
                "id": REVIEW_REQUEST_ID,
                "event": "review_requested",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            }
        ],
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    monkeypatch.setattr(
        gate, "_buzz_send", lambda content, reply_to=None: "buzz-request"
    )

    result = _gate_webhook(
        {
            "action": "review_requested",
            "number": 185,
            "repository": {"full_name": "NewtonsAppleAI/newtonsapple-web"},
            "requested_reviewer": {"login": "newtonsapple-bot"},
            "pull_request": live_pr,
        },
        "newtonsapple-bot",
        store,
    )

    assert result["contract_version"] == "v2"
    assert result["expected_base_ref"] == "dev"
    assert result["review_request_id"] == REVIEW_REQUEST_ID
    assert isinstance(result["lease_token"], str)
    assert len(result["lease_token"]) >= 32
    assert store.requested_event_id(
        ReviewTuple(
            repository=gate.REPOSITORY,
            pr_number=185,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            request_id=REVIEW_REQUEST_ID,
        )
    ) == "buzz-request"


def test_started_control_plane_replies_once_under_requested_thread(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    lease_token = store.reserve(review_tuple, now=100, lease_seconds=300)
    assert isinstance(lease_token, str)
    store.record_requested_event(review_tuple, "buzz-request")
    monkeypatch.setattr(gate.time, "time", lambda: 120)
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    delivered = []
    monkeypatch.setattr(
        gate,
        "_buzz_send",
        lambda content, reply_to=None: delivered.append((content, reply_to))
        or "buzz-started",
    )
    payload = {
        "operation": "started",
        "contract_version": "v2",
        "repository": review_tuple.repository,
        "pr_number": review_tuple.pr_number,
        "base_sha": review_tuple.base_sha,
        "head_sha": review_tuple.head_sha,
        "review_request_id": review_tuple.request_id,
        "lease_token": lease_token,
        "pr_url": "https://github.com/NewtonsAppleAI/newtonsapple-web/pull/185",
    }

    assert gate._settle(payload, "newtonsapple-bot", store) == {
        "settled": "started"
    }
    assert gate._settle(payload, "newtonsapple-bot", store) == {
        "settled": "started"
    }
    assert delivered == [
        (
            gate._started_content(
                review_tuple,
                "https://github.com/NewtonsAppleAI/newtonsapple-web/pull/185",
            )
            + "\n\n"
            + gate._started_marker(review_tuple),
            "buzz-request",
        )
    ]


def test_summary_outbox_is_tuple_unique_and_replay_checks_buzz_before_sending(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    marker = (
        "<!-- newtonsapple-pr-review-summary:v2 "
        f"repo=NewtonsAppleAI/newtonsapple-web pr=185 base={BASE_SHA} "
        f"head={HEAD_SHA} request={review_tuple.request_id} -->"
    )

    first_id = store.enqueue_summary(review_tuple, marker=marker, content="review summary")
    second_id = store.enqueue_summary(review_tuple, marker=marker, content="replacement ignored")

    assert first_id == second_id
    assert store.pending_summaries() == [
        {
            "id": first_id,
            "key": (
                f"v2:NewtonsAppleAI/newtonsapple-web:185:{BASE_SHA}:{HEAD_SHA}:"
                f"{review_tuple.request_id}"
            ),
            "marker": marker,
            "content": "review summary",
        }
    ]

    sent = []
    processed = drain_summary_outbox(
        store,
        find_existing=lambda candidate, reply_to: "buzz-event-existing"
        if candidate == marker
        else None,
        send=lambda content, reply_to: sent.append((content, reply_to))
        or "buzz-event-new",
    )

    assert processed == 1
    assert sent == []
    assert store.pending_summaries() == []
    assert store.sent_event_id(first_id) == "buzz-event-existing"


def test_summary_outbox_replies_to_the_persisted_request_root(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    store.enqueue_summary(
        review_tuple,
        marker="summary-marker",
        content="one paragraph",
        reply_to="request-root",
    )
    delivered = []

    processed = drain_summary_outbox(
        store,
        find_existing=lambda marker, reply_to: None,
        send=lambda content, reply_to: delivered.append((content, reply_to))
        or "summary-event",
    )

    assert processed == 1
    assert delivered == [
        ("one paragraph\n\nsummary-marker", "request-root")
    ]


def test_buzz_marker_reconciliation_requires_configured_channel_and_own_author(monkeypatch):
    marker = "<!-- tuple-marker -->"
    own_pubkey = "b" * 64
    responses = iter(
        [
            [{"display_name": "Hermany", "pubkey": own_pubkey}],
            [
                {
                    "id": "wrong-channel",
                    "pubkey": own_pubkey,
                    "content": marker,
                    "tags": [["h", "different-channel"]],
                },
                {
                    "id": "forged-same-channel",
                    "pubkey": "a" * 64,
                    "content": marker,
                    "tags": [["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"]],
                },
                {
                    "id": "quoted-by-own-author",
                    "pubkey": own_pubkey,
                    "content": f"quoted {marker} but not a settled outbox message",
                    "tags": [["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"]],
                },
                {
                    "id": "expected-channel-and-author",
                    "pubkey": own_pubkey,
                    "content": marker,
                    "tags": [["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"]],
                },
            ],
        ]
    )
    monkeypatch.setattr(
        "scripts.newtonsapple_pr_review_gate.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(next(responses)),
        ),
    )

    assert _buzz_find(marker) == "expected-channel-and-author"


def test_buzz_marker_reconciliation_requires_the_expected_thread_parent(monkeypatch):
    marker = "<!-- tuple-marker -->"
    own_pubkey = "b" * 64
    responses = iter(
        [
            [{"display_name": "Hermany", "pubkey": own_pubkey}],
            [
                {
                    "id": "wrong-parent",
                    "pubkey": own_pubkey,
                    "content": marker,
                    "tags": [
                        ["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"],
                        ["e", "other-root", "", "reply"],
                    ],
                },
                {
                    "id": "expected-parent",
                    "pubkey": own_pubkey,
                    "content": marker,
                    "tags": [
                        ["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"],
                        ["e", "request-root", "", "reply"],
                    ],
                },
            ],
        ]
    )
    monkeypatch.setattr(
        "scripts.newtonsapple_pr_review_gate.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(next(responses)),
        ),
    )

    assert _buzz_find(marker, "request-root") == "expected-parent"


def test_summary_outbox_remains_pending_when_buzz_delivery_fails(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    store.enqueue_summary(review_tuple, marker="tuple-marker", content="summary")

    processed = drain_summary_outbox(
        store,
        find_existing=lambda marker, reply_to: None,
        send=lambda content, reply_to: (_ for _ in ()).throw(
            RuntimeError("Buzz offline")
        ),
    )

    assert processed == 0
    assert len(store.pending_summaries()) == 1


def test_summary_outbox_claims_are_exclusive_and_stale_tokens_cannot_ack(tmp_path):
    path = tmp_path / "review.sqlite3"
    first_store = ReviewStateStore(path)
    second_store = ReviewStateStore(path)
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    outbox_id = first_store.enqueue_summary(
        review_tuple, marker="tuple-marker", content="summary"
    )

    first_claim = first_store.claim_summary(now=100, lease_seconds=30)
    assert first_claim is not None
    assert second_store.claim_summary(now=129, lease_seconds=30) is None
    second_claim = second_store.claim_summary(now=130, lease_seconds=30)
    assert second_claim is not None
    assert second_claim["claim_token"] != first_claim["claim_token"]

    with pytest.raises(ValueError, match="outbox claim not found"):
        first_store.mark_summary_sent(
            outbox_id, "stale-event", claim_token=first_claim["claim_token"]
        )
    second_store.mark_summary_sent(
        outbox_id, "accepted-event", claim_token=second_claim["claim_token"]
    )
    assert first_store.sent_event_id(outbox_id) == "accepted-event"


def test_buzz_outbox_claim_can_be_token_fenced_and_renewed(tmp_path):
    first_store = ReviewStateStore(tmp_path / "review.sqlite3")
    second_store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    outbox_id = first_store.enqueue_summary(
        review_tuple, marker="tuple-marker", content="summary"
    )
    claim = first_store.claim_summary(now=100, lease_seconds=60)
    assert claim is not None

    first_store.renew_summary_claim(
        outbox_id,
        claim_token=claim["claim_token"],
        now=150,
        lease_seconds=300,
    )

    assert second_store.claim_summary(now=200, lease_seconds=60) is None
    with pytest.raises(ValueError, match="outbox claim not found"):
        first_store.renew_summary_claim(
            outbox_id,
            claim_token="stale-token",
            now=200,
            lease_seconds=300,
        )


def test_operational_blocker_has_distinct_outbox_identity_from_later_summary(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    blocker_id = store.enqueue_blocker(
        review_tuple, marker="blocker-marker", content="request provenance unavailable"
    )
    summary_id = store.enqueue_summary(
        review_tuple, marker="summary-marker", content="review completed"
    )

    assert blocker_id != summary_id
    assert [item["content"] for item in store.pending_summaries()] == [
        "request provenance unavailable",
        "review completed",
    ]


def _live_pr(**overrides):
    pull = {
        "number": 185,
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/NewtonsAppleAI/newtonsapple-web/pull/185",
        "base": {"ref": "dev", "sha": BASE_SHA},
        "head": {"ref": "chore--review", "sha": HEAD_SHA},
        "requested_reviewers": [{"login": "newtonsapple-bot"}],
    }
    pull.update(overrides)
    return pull


def test_reconciliation_selects_only_live_exact_tuple_with_no_bot_marker():
    selected = select_authorized_tuple(
        _live_pr(),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: [
            {
                "id": 1,
                "event": "review_requested",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            }
        ],
    )

    assert selected == ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )


def test_reconcile_does_not_settle_an_untrusted_legacy_bot_marker(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    review_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    marker_body = f"legacy review\n\n{gate.review_marker(review_tuple)}"
    state = (live_pr, None, [marker_body])
    monkeypatch.setattr(gate, "_collection", lambda endpoint: [live_pr])
    monkeypatch.setattr(
        gate, "_recoverable_live_tuple", lambda *args: state
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    monkeypatch.setattr(
        gate, "_buzz_send", lambda content, reply_to=None: "buzz-blocker"
    )

    result = gate._reconcile("newtonsapple-bot", store)

    assert result["events"] == []


def test_reconcile_settles_existing_marker_for_verified_timeline_request(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    review_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    marker_body = f"verified review\n\n{gate.review_marker(review_tuple)}"
    monkeypatch.setattr(gate, "_collection", lambda endpoint: [live_pr])
    monkeypatch.setattr(
        gate,
        "_recoverable_live_tuple",
        lambda *args: (live_pr, review_tuple, [marker_body]),
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    monkeypatch.setattr(
        gate, "_buzz_send", lambda content, reply_to=None: "buzz-summary"
    )

    result = gate._reconcile("newtonsapple-bot", store)

    assert result == {"events": [], "outbox_delivered": 1}
    assert store.reserve(review_tuple, now=200, lease_seconds=30) is None


def test_reconcile_isolates_a_malformed_candidate_and_continues(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    first = _live_pr()
    second_head = "c" * 40
    second = _live_pr(
        number=186,
        html_url="https://github.com/NewtonsAppleAI/newtonsapple-web/pull/186",
        head={"ref": "feature", "sha": second_head},
    )
    second_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=186,
        base_sha=BASE_SHA,
        head_sha=second_head,
    )
    monkeypatch.setattr(gate, "_collection", lambda endpoint: [first, second])

    def recoverable(number, login):
        if number == 185:
            raise RuntimeError("malformed request provenance")
        return second, second_tuple, []

    monkeypatch.setattr(gate, "_recoverable_live_tuple", recoverable)
    delivered = []
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    monkeypatch.setattr(
        gate,
        "_buzz_send",
        lambda content, reply_to=None: delivered.append(content) or "buzz-blocker",
    )

    result = gate._reconcile("newtonsapple-bot", store)

    assert len(result["events"]) == 1
    assert result["events"][0]["payload"]["number"] == 186
    assert result["outbox_delivered"] == 1
    assert "PR review requested" in delivered[0]
    assert "could not safely verify current request provenance" in delivered[-1]


def test_reconciliation_accepts_latest_current_timeline_request():
    timeline = [
        {"id": 1, "event": "committed"},
        {
            "id": 2,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: timeline,
    )

    assert selected == ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        request_id=2,
    )


@pytest.mark.parametrize(
    "later_event",
    [
        {"id": 3, "event": "committed"},
        {"id": 3, "event": "head_ref_force_pushed"},
        {"id": 3, "event": "base_ref_changed"},
        {
            "id": 3,
            "event": "review_request_removed",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        {"id": 3, "event": "converted_to_draft"},
        {"id": 3, "event": "closed"},
        {"id": 3, "event": "merged"},
    ],
)
def test_reconciliation_rejects_events_after_latest_request(later_event):
    timeline = [
        {
            "id": 2,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        later_event,
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: timeline,
    )

    assert selected is None


def test_reconciliation_accepts_the_latest_rerequest():
    timeline = [
        {
            "id": 2,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        {
            "id": 3,
            "event": "review_request_removed",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        {
            "id": 4,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: timeline,
    )

    assert selected == ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        request_id=4,
    )


@pytest.mark.parametrize("request_id", [None, True, "4", 0, -1])
def test_reconciliation_rejects_a_request_without_a_positive_timeline_id(request_id):
    selected = select_authorized_tuple(
        _live_pr(),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: [
            {
                "id": request_id,
                "event": "review_requested",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            }
        ],
    )

    assert selected is None


def test_prior_generation_marker_does_not_block_the_latest_rerequest():
    prior = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        request_id=2,
    )

    selected = select_authorized_tuple(
        _live_pr(),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[f"prior review\n\n{gate.review_marker(prior)}"],
        load_timeline=lambda pr_number: [
            {
                "id": 2,
                "event": "review_requested",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            },
            {
                "id": 3,
                "event": "review_request_removed",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            },
            {
                "id": 4,
                "event": "review_requested",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            },
        ],
    )

    assert selected == ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        request_id=4,
    )


def test_webhook_accepts_verified_payload_tuple(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    monkeypatch.setattr(
        gate,
        "_live_review_state",
        lambda number, login: (live_pr, []),
    )
    monkeypatch.setattr(
        gate,
        "_load_timeline",
        lambda number: [
            {
                "id": REVIEW_REQUEST_ID,
                "event": "review_requested",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            }
        ],
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker, reply_to=None: None)
    monkeypatch.setattr(
        gate, "_buzz_send", lambda content, reply_to=None: "buzz-request"
    )

    result = _gate_webhook(
        {
            "action": "review_requested",
            "number": 185,
            "repository": {"full_name": "NewtonsAppleAI/newtonsapple-web"},
            "requested_reviewer": {"login": "newtonsapple-bot"},
            "pull_request": live_pr,
        },
        "newtonsapple-bot",
        store,
    )

    assert result["expected_base_sha"] == BASE_SHA
    assert result["expected_head_sha"] == HEAD_SHA
    assert result["review_request_id"] == REVIEW_REQUEST_ID


def test_webhook_rejects_head_mutation_between_payload_and_live_state(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    payload_pr = _live_pr()
    live_pr = _live_pr(head={"ref": "chore--review", "sha": "c" * 40})
    monkeypatch.setattr(
        gate,
        "_live_review_state",
        lambda number, login: (live_pr, []),
    )

    with pytest.raises(RuntimeError, match="tuple changed"):
        _gate_webhook(
            {
                "action": "review_requested",
                "number": 185,
                "repository": {"full_name": "NewtonsAppleAI/newtonsapple-web"},
                "requested_reviewer": {"login": "newtonsapple-bot"},
                "pull_request": payload_pr,
            },
            "newtonsapple-bot",
            store,
        )


def test_local_execution_worker_attempts_every_gate_without_credentials(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_commit_tree_sha",
        lambda sha: "c" * 40 if sha == BASE_SHA else "d" * 40,
    )
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-propagate")
    monkeypatch.setenv("GH_TOKEN", "must-not-propagate")
    monkeypatch.setattr(gate, "_local_docker_host", lambda: "unix:///tmp/docker.sock")
    fetched = []
    monkeypatch.setattr(
        gate,
        "_fetch_exact_commit",
        lambda workspace, sha, *, home: fetched.append((sha, home)),
    )
    monkeypatch.setattr(
        gate,
        "_git_output",
        lambda workspace, *args: (
            HEAD_SHA
            if args == ("rev-parse", "HEAD")
            else "d" * 40
            if args == ("rev-parse", "HEAD^{tree}")
            else "commit"
            if args == ("cat-file", "-t", BASE_SHA)
            else ""
        ),
    )
    calls = []

    def fake_run(command, *, cwd, env, timeout):
        calls.append((command, env))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(gate, "_run_command", fake_run)

    result = gate._run_local_execution_worker(
        ReviewTuple(
            repository=gate.REPOSITORY,
            pr_number=185,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )
    )

    assert result["worker"]["required"] is True
    assert result["worker"]["preflight"]["host_mounts_absent"] is True
    assert [item["status"] for item in result["gates"]] == ["pass", "pass", "pass"]
    assert all(item["attempted"] is True for item in result["gates"])
    assert [sha for sha, _ in fetched] == [HEAD_SHA, BASE_SHA]
    docker_calls = [(command, env) for command, env in calls if command[0] == "docker"]
    assert len(docker_calls) == 6
    assert [command[command.index("--network") + 1] for command, _ in docker_calls] == [
        "bridge",
        "none",
        "bridge",
        "none",
        "bridge",
        "none",
    ]
    assert all(env["DOCKER_HOST"] == "unix:///tmp/docker.sock" for _, env in docker_calls)
    assert all("GITHUB_TOKEN" not in env and "GH_TOKEN" not in env for _, env in docker_calls)


def test_local_execution_worker_reports_each_gate_when_docker_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_commit_tree_sha",
        lambda sha: "c" * 40 if sha == BASE_SHA else "d" * 40,
    )
    monkeypatch.setattr(gate, "_local_docker_host", lambda: "unix:///tmp/docker.sock")
    monkeypatch.setattr(gate, "_fetch_exact_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gate,
        "_git_output",
        lambda workspace, *args: (
            HEAD_SHA
            if args == ("rev-parse", "HEAD")
            else "d" * 40
            if args == ("rev-parse", "HEAD^{tree}")
            else "commit"
            if args == ("cat-file", "-t", BASE_SHA)
            else ""
        ),
    )
    docker_calls = []

    def fake_run(command, *, cwd, env, timeout):
        if command[0] == "docker":
            docker_calls.append(command)
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="failed to connect to the docker API",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "_run_command", fake_run)

    result = gate._run_local_execution_worker(
        ReviewTuple(
            repository=gate.REPOSITORY,
            pr_number=185,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )
    )

    assert len(docker_calls) == 6
    assert [item["status"] for item in result["gates"]] == [
        "unavailable",
        "unavailable",
        "unavailable",
    ]
    assert all(item["attempted"] is False for item in result["gates"])


def test_local_execution_reports_unavailable_gates_in_signed_evidence(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)
    monkeypatch.setattr(
        gate,
        "_run_local_execution_worker",
        lambda review_tuple: {
            "base_tree_sha": "c" * 40,
            "head_tree_sha": "d" * 40,
            "worker": {"required": True, "isolation": "docker"},
            "gates": [
                gate._unavailable_local_gate(name, "local worker unavailable")
                for name in gate.BASELINE_EXECUTION_GATES
            ],
        },
    )
    result = gate.execution_evidence(_execution_request("execution_evidence"))

    payload = base64.b64decode(result["attestation_payload"])
    private_key.public_key().verify(
        base64.b64decode(result["attestation_signature"]), payload
    )
    report = json.loads(payload)
    assert [item["status"] for item in report["gates"]] == [
        "unavailable",
        "unavailable",
        "unavailable",
    ]


def test_exact_commit_fetch_uses_only_the_pinned_gh_credential_helper(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", "/trusted/gh-config")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-propagate")
    calls = []

    def fake_run(command, *, cwd, env, timeout):
        calls.append((command, cwd, env, timeout))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "_run_command", fake_run)

    gate._fetch_exact_commit(workspace, HEAD_SHA, home=home)

    assert calls == [
        (
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "fetch",
                "-q",
                "--depth=1",
                "origin",
                HEAD_SHA,
            ],
            workspace,
            {
                "PATH": gate.os.environ.get(
                    "PATH", "/usr/bin:/bin:/usr/sbin:/sbin"
                ),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "HOME": str(home),
                "GH_CONFIG_DIR": "/trusted/gh-config",
            },
            180,
        )
    ]


def test_local_docker_host_resolves_only_a_unix_socket(monkeypatch):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="unix:///Users/reviewer/.docker/run/docker.sock\n",
            stderr="",
        ),
    )

    assert gate._local_docker_host() == (
        "unix:///Users/reviewer/.docker/run/docker.sock"
    )


def test_local_docker_host_rejects_remote_daemon(monkeypatch):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="tcp://docker.example:2375\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="local Docker socket"):
        gate._local_docker_host()


def test_gate_resolution_exposes_all_local_gate_outcomes(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)

    result = gate.resolve_execution_gates(_execution_request("resolve_execution_gates"))

    payload = base64.b64decode(result["gate_resolution_payload"])
    signature = base64.b64decode(result["gate_resolution_signature"])
    private_key.public_key().verify(signature, payload)
    signed = json.loads(payload)
    assert {
        name: contract["statuses"]
        for name, contract in signed["gate_contracts"].items()
    } == {
        "quality": ["pass", "pr-fail", "unavailable"],
        "integration": ["pass", "pr-fail", "unavailable"],
        "e2e": ["pass", "pr-fail", "unavailable"],
    }


def test_reconciliation_rechecks_request_event_and_later_invalidation():
    timeline = [
        {
            "id": 29064129383,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        }
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        load_timeline=lambda pr_number: timeline,
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
    )
    assert selected is not None

    timeline.append({"id": 29064129384, "event": "committed"})
    rejected = select_authorized_tuple(
        _live_pr(),
        load_timeline=lambda pr_number: timeline,
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
    )
    assert rejected is None


@pytest.mark.parametrize(
    ("live_overrides", "bot_bodies"),
    [
        ({"state": "closed"}, []),
        ({"draft": True}, []),
        ({"base": {"ref": "feature", "sha": BASE_SHA}}, []),
        ({"requested_reviewers": []}, []),
        ({}, ["prefix <!-- newtonsapple-pr-review:v2 repo=NewtonsAppleAI/newtonsapple-web "
              f"pr=185 base={BASE_SHA} head={HEAD_SHA} request=1 --> suffix"]),
    ],
)
def test_reconciliation_rejects_stale_ineligible_or_completed_tuple(
    live_overrides, bot_bodies
):
    selected = select_authorized_tuple(
        _live_pr(**live_overrides),
        reviewer_login="newtonsapple-bot",
        bot_bodies=bot_bodies,
        load_timeline=lambda pr_number: [
            {
                "id": 1,
                "event": "review_requested",
                "requested_reviewer": {"login": "newtonsapple-bot"},
            }
        ],
    )

    assert selected is None
