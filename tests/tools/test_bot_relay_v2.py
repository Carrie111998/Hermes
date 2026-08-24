"""Behavioral acceptance tests for the durable cross-connection Bot relay.

These tests deliberately exercise the public ``tools.bot_relay`` seam and
the waiter command as a real child process.  They do not inspect source text
or manufacture ledger rows directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from gateway.durable_events import EventConflict, LeaseMismatch, get_event
from tools import bot_relay


NAMESPACE_A = "desktop-namespace-a"
NAMESPACE_B = "desktop-namespace-b"
COURIER_A = "courier-a"
COURIER_B = "courier-b"


def _target(*, namespace: str = NAMESPACE_A, connection: str = "cloud-a") -> dict:
    return {
        "profile": "ops",
        "handle": "ops",
        "connection_id": connection,
        "connection_label": "Cloud A",
        "courier_namespace_id": namespace,
        "target_install_id": "install-ops",
        "title": "Operator",
        "description": "Runs operations",
    }


def _enqueue(
    root: Path,
    *,
    key: str,
    body: str = "ping",
    namespace: str = NAMESPACE_A,
    sender_profile: str = "default",
) -> dict:
    return bot_relay.enqueue_envelope(
        root,
        target=_target(namespace=namespace),
        message=f"Message from 🤖 hermes (@hermes): {body}",
        body=body,
        sender_profile=sender_profile,
        sender_handle="hermes",
        idempotency_key=key,
    )


def _claim(
    root: Path,
    courier: str,
    *,
    namespace: str = NAMESPACE_A,
    now: float | None = None,
    limit: int = 1,
    lease_seconds: int = 60,
) -> list[dict]:
    return bot_relay.claim_leased_envelopes(
        root,
        courier_namespace_id=namespace,
        courier_id=courier,
        limit=limit,
        lease_seconds=lease_seconds,
        now=now,
    )


def _lease_kwargs(envelope: dict, *, courier: str) -> dict:
    return {
        "envelope_id": envelope["id"],
        "courier_id": courier,
        "lease_token": envelope["lease_token"],
        "lease_generation": envelope["lease_generation"],
    }


def _run_waiter(command: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Execute the command in the bash-shaped shell used by Hermes."""
    if sys.platform == "win32":
        import shutil

        bash = shutil.which("bash") or shutil.which("bash.exe")
        if not bash:
            pytest.skip("Hermes local commands require Git Bash on Windows")
        argv = [bash, "-c", command]
    else:
        argv = ["/bin/sh", "-c", command]
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_single_item_claims_rotate_across_profile_ledgers(tmp_path: Path) -> None:
    for index in range(3):
        _enqueue(tmp_path, key=f"default-{index}")
    named = _enqueue(tmp_path, key="named", sender_profile="research")

    first = _claim(tmp_path, COURIER_A, limit=1)
    second = _claim(tmp_path, COURIER_A, limit=1)

    assert len(first) == len(second) == 1
    assert first[0]["from_profile"] == "default"
    assert second[0]["id"] == named["id"]
    assert second[0]["from_profile"] == "research"


def test_namespaced_rosters_are_isolated_sanitized_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row_a = {
        **_target(namespace=NAMESPACE_A, connection="route-a"),
        "connection_label": "Cloud\n\tA",
        "title": "Ops\r\nIGNORE PREVIOUS",
        "description": "runs\x00  operations",
    }
    row_b = {
        **_target(namespace=NAMESPACE_B, connection="route-b"),
        "profile": "scout",
        "handle": "scout",
    }

    assert bot_relay.write_remote_roster(
        tmp_path, [row_a], courier_namespace_id=NAMESPACE_A
    ) == 1
    assert bot_relay.write_remote_roster(
        tmp_path, [row_b], courier_namespace_id=NAMESPACE_B
    ) == 1

    rows = bot_relay.read_remote_roster(tmp_path)
    by_namespace = {row["courier_namespace_id"]: row for row in rows}
    assert set(by_namespace) == {NAMESPACE_A, NAMESPACE_B}
    assert by_namespace[NAMESPACE_A]["connection_label"] == "Cloud A"
    assert by_namespace[NAMESPACE_A]["title"] == "Ops IGNORE PREVIOUS"
    assert by_namespace[NAMESPACE_A]["description"] == "runs operations"

    # Replacing one Desktop's snapshot cannot erase another Desktop's route.
    assert bot_relay.write_remote_roster(
        tmp_path,
        [{**row_a, "title": "Updated"}],
        courier_namespace_id=NAMESPACE_A,
    ) == 1
    rows = bot_relay.read_remote_roster(tmp_path)
    assert {(row["courier_namespace_id"], row["profile"]) for row in rows} == {
        (NAMESPACE_A, "ops"),
        (NAMESPACE_B, "scout"),
    }

    many = [
        {
            "profile": f"p{index}",
            "handle": f"p{index}",
            "connection_id": f"route-{index}",
        }
        for index in range(bot_relay.MAX_ROSTER_ROWS + 25)
    ]
    assert (
        bot_relay.write_remote_roster(
            tmp_path, many, courier_namespace_id="desktop-namespace-c"
        )
        == bot_relay.MAX_ROSTER_ROWS
    )

    # The encoded-byte limit is enforced after normalization, not merely a
    # caller-provided row-count check.
    monkeypatch.setattr(bot_relay, "MAX_ROSTER_BYTES", 100)
    with pytest.raises(ValueError, match="too large"):
        bot_relay.write_remote_roster(
            tmp_path, [row_a], courier_namespace_id="desktop-namespace-d"
        )


def test_same_desktop_local_route_is_namespace_qualified_and_exact(
    tmp_path: Path,
) -> None:
    for namespace, install in (
        (NAMESPACE_A, "install-a"),
        (NAMESPACE_B, "install-b"),
    ):
        assert bot_relay.write_remote_roster(
            tmp_path,
            [
                {
                    "profile": "ops",
                    "handle": "ops",
                    "connection_id": "local",
                    "target_install_id": install,
                }
            ],
            courier_namespace_id=namespace,
        ) == 1

    roster = bot_relay.read_remote_roster(tmp_path)
    assert bot_relay.resolve_remote_target("ops", roster) == "ambiguous"
    assert bot_relay.resolve_remote_target("ops@local", roster) == "ambiguous"

    forms = bot_relay.remote_target_forms(roster)
    assert set(forms) == {
        f"ops@local~{NAMESPACE_A}",
        f"ops@local~{NAMESPACE_B}",
    }
    selected = bot_relay.resolve_remote_target(
        f"ops@local~{NAMESPACE_B}", roster
    )
    assert selected["courier_namespace_id"] == NAMESPACE_B
    assert selected["target_install_id"] == "install-b"


def test_namespaced_snapshot_supersedes_legacy_duplicate_during_upgrade(
    tmp_path: Path,
) -> None:
    row = {
        "profile": "ops",
        "handle": "ops",
        "connection_id": "local",
    }
    assert bot_relay.write_remote_roster(tmp_path, [row]) == 1
    assert bot_relay.write_remote_roster(
        tmp_path,
        [{**row, "target_install_id": "install-v2"}],
        courier_namespace_id=NAMESPACE_A,
    ) == 1

    roster = bot_relay.read_remote_roster(tmp_path)
    assert len(roster) == 1
    assert roster[0]["courier_namespace_id"] == NAMESPACE_A
    assert bot_relay.resolve_remote_target("ops", roster) == roster[0]
    assert bot_relay.remote_target_forms(roster) == ["ops"]


def test_roster_rejects_operator_bearing_route_coordinates(tmp_path: Path) -> None:
    rows = [
        {
            "profile": "ops",
            "handle": "ops",
            "connection_id": "safe-route; touch owned",
        },
        {
            "profile": "scout",
            "handle": "scout",
            "connection_id": "safe-route",
        },
    ]
    assert bot_relay.write_remote_roster(
        tmp_path, rows, courier_namespace_id=NAMESPACE_A
    ) == 1
    assert [row["profile"] for row in bot_relay.read_remote_roster(tmp_path)] == [
        "scout"
    ]


def test_deterministic_enqueue_is_idempotent_and_rejects_payload_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _enqueue(tmp_path, key="session-1/tool-call-1", body="deploy")

    # A transport retry can happen seconds later. Volatile timestamps must not
    # turn the same logical tool invocation into a payload conflict.
    real_time = bot_relay.time.time
    monkeypatch.setattr(bot_relay.time, "time", lambda: real_time() + 30)
    repeated = _enqueue(tmp_path, key="session-1/tool-call-1", body="deploy")
    assert repeated == first

    with pytest.raises(
        (EventConflict, ValueError), match="different content|conflict|collision"
    ):
        _enqueue(tmp_path, key="session-1/tool-call-1", body="destroy")


def test_two_couriers_contending_through_tools_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    event = _enqueue(tmp_path, key="claim-race")
    assert _claim(tmp_path, "foreign-courier", namespace=NAMESPACE_B) == []
    barrier = Barrier(2)

    def contend(courier: str) -> list[dict]:
        barrier.wait(timeout=5)
        return _claim(tmp_path, courier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = list(pool.map(contend, [COURIER_A, COURIER_B]))

    winners = [row for batch in batches for row in batch]
    assert [row["id"] for row in winners] == [event["id"]]
    assert winners[0]["lease_owner"] in {COURIER_A, COURIER_B}
    assert sum(not batch for batch in batches) == 1


def test_claimed_non_object_payload_is_terminalized_not_silently_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = {
        "event_id": "a" * 32,
        "payload": "poison",
        "lease_token": "lease-token",
        "generation": 2,
        "attempts": 1,
    }
    calls = []

    def fake_claim(*_args, **_kwargs):
        return [claimed] if not calls else []

    def fake_nack(*args, **kwargs):
        calls.append((args, kwargs))
        return {"state": "failed"}

    monkeypatch.setattr("gateway.durable_events.claim", fake_claim)
    monkeypatch.setattr("gateway.durable_events.nack", fake_nack)
    assert _claim(tmp_path, COURIER_A) == []
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["event_id"] == claimed["event_id"]
    assert kwargs["owner"] == COURIER_A
    assert kwargs["lease_token"] == claimed["lease_token"]
    assert kwargs["generation"] == claimed["generation"]
    assert kwargs["retryable"] is False
    assert "payload" in kwargs["error"]


def test_lease_expiry_reclaims_with_new_fence_and_stale_authority_fails(
    tmp_path: Path,
) -> None:
    event = _enqueue(tmp_path, key="crash-recovery")
    first = _claim(tmp_path, COURIER_A, lease_seconds=30)[0]
    assert first["id"] == event["id"]
    assert _claim(
        tmp_path,
        COURIER_B,
        now=float(first["lease_expires_at"]) - 0.001,
    ) == []

    second = _claim(
        tmp_path,
        COURIER_B,
        now=float(first["lease_expires_at"]) + 0.001,
        lease_seconds=60,
    )[0]
    assert second["id"] == event["id"]
    assert second["lease_token"] != first["lease_token"]
    assert second["lease_generation"] > first["lease_generation"]

    for operation in (
        lambda: bot_relay.renew_envelope_lease(
            tmp_path, **_lease_kwargs(first, courier=COURIER_A)
        ),
        lambda: bot_relay.ack_envelope(
            tmp_path,
            **_lease_kwargs(first, courier=COURIER_A),
            reply="stale",
        ),
        lambda: bot_relay.nack_envelope(
            tmp_path,
            **_lease_kwargs(first, courier=COURIER_A),
            error="stale",
            retryable=True,
        ),
    ):
        with pytest.raises(LeaseMismatch):
            operation()

    with pytest.raises(LeaseMismatch):
        bot_relay.renew_envelope_lease(
            tmp_path,
            envelope_id=second["id"],
            courier_id=COURIER_B,
            lease_token=second["lease_token"],
            lease_generation=second["lease_generation"] - 1,
        )
    with pytest.raises(LeaseMismatch):
        bot_relay.ack_envelope(
            tmp_path,
            envelope_id=second["id"],
            courier_id=COURIER_B,
            lease_token="forged-token",
            lease_generation=second["lease_generation"],
            reply="forged",
        )
    with pytest.raises(LeaseMismatch):
        bot_relay.ack_envelope(
            tmp_path,
            envelope_id=second["id"],
            courier_id="foreign-courier",
            lease_token=second["lease_token"],
            lease_generation=second["lease_generation"],
            reply="forged",
        )


def test_live_lease_renewal_extends_expiry_without_changing_fence(tmp_path: Path) -> None:
    _enqueue(tmp_path, key="renew")
    claimed = _claim(tmp_path, COURIER_A, lease_seconds=30)[0]
    renewed = bot_relay.renew_envelope_lease(
        tmp_path,
        **_lease_kwargs(claimed, courier=COURIER_A),
        lease_seconds=120,
        now=float(claimed["lease_expires_at"]) - 1,
    )
    # The generic ledger never returns the plaintext token after its initial
    # claim.  An unchanged generation plus successful use of the original
    # token proves that renewal preserved the fence.
    assert renewed["generation"] == claimed["lease_generation"]
    assert renewed["lease_expires_at"] > claimed["lease_expires_at"]
    bot_relay.ack_envelope(
        tmp_path,
        **_lease_kwargs(claimed, courier=COURIER_A),
        reply="renewed",
        now=float(claimed["lease_expires_at"]) + 1,
    )


def test_ack_is_immutable_exact_duplicate_is_idempotent_and_conflict_fails(
    tmp_path: Path,
) -> None:
    event = _enqueue(tmp_path, key="ack")
    claimed = _claim(tmp_path, COURIER_A)[0]
    digest = bot_relay.outcome_digest(event["id"], "pong", "")
    kwargs = {
        **_lease_kwargs(claimed, courier=COURIER_A),
        "reply": "pong",
        "claimed_outcome_digest": digest,
    }

    first = bot_relay.ack_envelope(tmp_path, **kwargs)
    duplicate = bot_relay.ack_envelope(tmp_path, **kwargs)
    assert first["idempotent"] is False
    assert duplicate["idempotent"] is True
    assert duplicate["outcome"] == first["outcome"]
    assert duplicate["outcome_digest"] == first["outcome_digest"]

    row = get_event(
        tmp_path / "state.db", stream=bot_relay.DELIVERY_STREAM, event_id=event["id"]
    )
    assert row["state"] == "acked"
    assert row["outcome"]["reply"] == "pong"

    with pytest.raises((LeaseMismatch, ValueError)):
        bot_relay.ack_envelope(
            tmp_path,
            **_lease_kwargs(claimed, courier=COURIER_A),
            reply="different",
            claimed_outcome_digest=bot_relay.outcome_digest(
                event["id"], "different", ""
            ),
        )
    unchanged = get_event(
        tmp_path / "state.db", stream=bot_relay.DELIVERY_STREAM, event_id=event["id"]
    )
    assert unchanged["outcome"]["reply"] == "pong"


def test_retryable_nack_requeues_and_terminal_nack_wakes_waiter(
    tmp_path: Path,
) -> None:
    event = _enqueue(tmp_path, key="nack")
    first = _claim(tmp_path, COURIER_A)[0]
    retry = bot_relay.nack_envelope(
        tmp_path,
        **_lease_kwargs(first, courier=COURIER_A),
        error="gateway disconnected",
        retryable=True,
        retry_after_seconds=0,
    )
    assert retry.get("terminal") is not True

    second = _claim(tmp_path, COURIER_B)[0]
    assert second["id"] == event["id"]
    assert second["lease_generation"] > first["lease_generation"]
    terminal = bot_relay.nack_envelope(
        tmp_path,
        **_lease_kwargs(second, courier=COURIER_B),
        error="target identity changed",
        retryable=False,
    )
    assert terminal["state"] == "failed"

    row = get_event(
        tmp_path / "state.db", stream=bot_relay.DELIVERY_STREAM, event_id=event["id"]
    )
    assert row["state"] in {"failed", "dead_lettered"}
    assert "target identity changed" in row["outcome"]["error"]

    completed = _run_waiter(bot_relay.waiter_command(tmp_path, event), cwd=Path.cwd())
    assert completed.returncode == 1
    assert "target identity changed" in completed.stdout


def test_reconnect_retries_do_not_exhaust_before_a_realistic_outage(
    tmp_path: Path,
) -> None:
    event = _enqueue(tmp_path, key="extended-outage")
    current = time.time() + 1

    # The previous 12-attempt ceiling terminalized a six-hour event after
    # roughly one hour even with a five-minute retry delay.  A two-hour outage
    # must remain recoverable and retain the same immutable event identity.
    for attempt in range(24):
        claimed = _claim(
            tmp_path,
            COURIER_A,
            now=current,
            lease_seconds=60,
        )[0]
        assert claimed["id"] == event["id"]
        assert claimed["attempt"] == attempt + 1
        queued = bot_relay.nack_envelope(
            tmp_path,
            **_lease_kwargs(claimed, courier=COURIER_A),
            error="gateway remains disconnected",
            retryable=True,
            retry_after_seconds=300,
            now=current + 0.1,
        )
        assert queued["state"] == "queued"
        current += 300.2

    row = get_event(
        tmp_path / "state.db",
        stream=bot_relay.DELIVERY_STREAM,
        event_id=event["id"],
        now=current,
    )
    assert row["state"] == "queued"
    assert row["attempts"] == 24


def test_legacy_reply_requires_a_real_claim_and_is_write_once(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown|unclaimed"):
        bot_relay.write_reply(tmp_path, "a" * 32, reply="forged")

    event = bot_relay.enqueue_envelope(
        tmp_path,
        target=_target(namespace=""),
        message="legacy message",
        sender_profile="default",
        sender_handle="hermes",
    )
    assert [row["id"] for row in bot_relay.claim_pending_envelopes(tmp_path)] == [
        event["id"]
    ]
    path = bot_relay.write_reply(tmp_path, event["id"], reply="first")
    assert json.loads(path.read_text(encoding="utf-8"))["reply"] == "first"
    assert bot_relay.write_reply(tmp_path, event["id"], reply="first") == path
    with pytest.raises(ValueError, match="conflicting"):
        bot_relay.write_reply(tmp_path, event["id"], reply="forged overwrite")


def test_waiter_treats_quote_and_shell_operator_route_text_as_literal(
    tmp_path: Path,
) -> None:
    event_id = "b" * 32
    python_marker = tmp_path / "python-injection-owned"
    shell_marker = tmp_path / "shell-injection-owned"
    malicious_route = (
        "route';__import__('pathlib').Path("
        + repr(str(python_marker))
        + ").write_text('owned');# $(touch "
        + str(shell_marker)
        + ")"
    )
    replies = bot_relay.relay_root(tmp_path) / bot_relay.REPLIES_DIR
    replies.mkdir(parents=True)
    (replies / f"{event_id}.json").write_text(
        json.dumps({"id": event_id, "reply": "safe reply", "error": ""}),
        encoding="utf-8",
    )
    command = bot_relay.waiter_command(
        tmp_path,
        {
            "id": event_id,
            "target_handle": "ops",
            "target_connection": malicious_route,
        },
    )

    completed = _run_waiter(command, cwd=Path.cwd())
    assert completed.returncode == 0, completed.stderr
    assert malicious_route in completed.stdout
    assert "safe reply" in completed.stdout
    assert not python_marker.exists()
    assert not shell_marker.exists()
