"""Tests for WS-upgrade and cross-process handoff ticket stores."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.ws_tickets import (
    TTL_SECONDS,
    TicketInvalid,
    _reset_for_tests,
    consume_ticket,
    mint_ticket,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMintAndConsume:
    def test_round_trip(self):
        ticket = mint_ticket(user_id="u1", provider="nous")
        info = consume_ticket(ticket)
        assert info["user_id"] == "u1"
        assert info["provider"] == "nous"
        assert "minted_at" in info

    def test_ticket_has_minimum_length(self):
        # ``secrets.token_urlsafe(32)`` produces ~43 chars; enforce a floor
        # so a future refactor can't accidentally shrink the entropy.
        ticket = mint_ticket(user_id="u1", provider="nous")
        assert len(ticket) >= 32


# ---------------------------------------------------------------------------
# Single-use
# ---------------------------------------------------------------------------


class TestSingleUse:
    def test_second_consume_raises(self):
        ticket = mint_ticket(user_id="u1", provider="stub")
        consume_ticket(ticket)
        with pytest.raises(TicketInvalid, match="unknown"):
            consume_ticket(ticket)

    def test_unknown_ticket_rejected(self):
        with pytest.raises(TicketInvalid, match="unknown"):
            consume_ticket("nope-never-minted")


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestTTL:
    def test_constant_is_30_seconds(self):
        # Pinned so a refactor that doubled the lifetime would surface here.
        assert TTL_SECONDS == 30

    def test_expired_ticket_rejected(self, monkeypatch):
        # Mock time inside the ws_tickets module so mint and consume see
        # different clocks. We have to patch the symbol the module actually
        # binds; ``time`` is module-level there.
        clock = {"now": 1_000_000}

        def fake_time():
            return clock["now"]

        monkeypatch.setattr(ws_tickets.time, "time", fake_time)

        ticket = mint_ticket(user_id="u1", provider="stub")
        clock["now"] += TTL_SECONDS + 1
        with pytest.raises(TicketInvalid, match="expired"):
            consume_ticket(ticket)


# ---------------------------------------------------------------------------
# Truncated value in error message (secret hygiene)
# ---------------------------------------------------------------------------


class TestErrorMessages:
    def test_unknown_ticket_error_truncates_value(self):
        long_value = "a" * 100
        with pytest.raises(TicketInvalid) as exc_info:
            consume_ticket(long_value)
        # Never log more than the first 8 chars of an opaque ticket.
        message = str(exc_info.value)
        assert long_value not in message
        assert long_value[:8] in message


# ---------------------------------------------------------------------------
# Thread safety: mint + consume from many threads doesn't deadlock or
# return duplicates.
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_mint_and_consume_concurrent(self):
        results: list[dict] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(i: int):
            try:
                t = mint_ticket(user_id=f"u{i}", provider="stub")
                info = consume_ticket(t)
                with lock:
                    results.append(info)
            except Exception as exc:  # noqa: BLE001 — collect for assert
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive(), "thread deadlocked"

        assert errors == []
        assert len(results) == 20
        # Every consume returns a distinct user_id (no cross-thread bleed).
        assert {r["user_id"] for r in results} == {f"u{i}" for i in range(20)}


# ---------------------------------------------------------------------------
# Process-lifetime internal credential (server-spawned PTY child auth).
# Direct unit coverage for internal_ws_credential / consume_internal_credential
# — _ws_auth_ok exercises these indirectly, but the mint-once, unminted, and
# empty-value branches are only reachable via direct calls.
# ---------------------------------------------------------------------------


class TestInternalCredential:
    def test_reset_clears_and_remints(self):
        first = ws_tickets.internal_ws_credential()
        _reset_for_tests()
        # The old value no longer validates after reset.
        with pytest.raises(TicketInvalid):
            ws_tickets.consume_internal_credential(first)
        # A fresh mint produces a different value.
        second = ws_tickets.internal_ws_credential()
        assert second != first
        assert ws_tickets.consume_internal_credential(second)["user_id"] == (
            ws_tickets.INTERNAL_USER_ID
        )

    def test_independent_of_ticket_store(self):
        """The internal credential is not a ticket — minting tickets doesn't
        touch it, and consuming the credential doesn't consume tickets."""
        cred = ws_tickets.internal_ws_credential()
        ticket = mint_ticket(user_id="u1", provider="nous")
        # Consuming the internal credential leaves the ticket intact.
        ws_tickets.consume_internal_credential(cred)
        assert consume_ticket(ticket)["user_id"] == "u1"


# ---------------------------------------------------------------------------
# Phone-handoff tickets (QR path) — separate store + prefix from WS tickets
# ---------------------------------------------------------------------------


class TestHandoffTickets:
    def test_round_trip(self):
        ticket = ws_tickets.mint_handoff_ticket(
            session_id="sess-1",
            profile="default",
            user_id="u1",
            provider="stub",
        )
        assert ticket.startswith(ws_tickets.HANDOFF_TICKET_PREFIX)
        info = ws_tickets.consume_handoff_ticket(ticket)
        assert info["kind"] == "handoff"
        assert info["session_id"] == "sess-1"
        assert info["profile"] == "default"
        assert info["user_id"] == "u1"
        assert info["scopes"] == list(ws_tickets.HANDOFF_SCOPES)
        assert "access_token" not in info

    def test_ttl_is_120_seconds(self):
        assert ws_tickets.HANDOFF_TTL_SECONDS == 120
        # WS ticket TTL must remain untouched.
        assert TTL_SECONDS == 30

    def test_single_use(self):
        ticket = ws_tickets.mint_handoff_ticket(
            session_id="s", user_id="u", provider="stub"
        )
        ws_tickets.consume_handoff_ticket(ticket)
        with pytest.raises(TicketInvalid, match="unknown"):
            ws_tickets.consume_handoff_ticket(ticket)

    def test_cross_process_store_is_hash_only_and_single_use(self, tmp_path):
        hermes_root = tmp_path / "hermes-home"
        profile_home = hermes_root / "profiles" / "work"
        profile_home.mkdir(parents=True)
        store = hermes_root / "runtime" / "desktop-handoff.sqlite3"
        mint_env = {**os.environ, "HERMES_HOME": str(profile_home)}
        consume_env = {**os.environ, "HERMES_HOME": str(hermes_root)}
        mint = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from hermes_cli.dashboard_auth.ws_tickets import mint_handoff_ticket; "
                    "print(mint_handoff_ticket(session_id='shared', user_id='u', provider='desktop'))"
                ),
            ],
            capture_output=True,
            check=True,
            env=mint_env,
            text=True,
        )
        ticket = mint.stdout.strip()

        with sqlite3.connect(store) as db:
            row = db.execute(
                "SELECT ticket_hash, payload_json FROM handoff_tickets"
            ).fetchone()
        assert row is not None
        assert ticket not in row[1]
        assert ticket.encode() not in store.read_bytes()

        consume = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys; "
                    "from hermes_cli.dashboard_auth.ws_tickets import consume_handoff_ticket; "
                    "print(json.dumps(consume_handoff_ticket(sys.stdin.read().strip())))"
                ),
            ],
            capture_output=True,
            check=True,
            env=consume_env,
            input=ticket,
            text=True,
        )
        info = json.loads(consume.stdout)
        assert info["session_id"] == "shared"
        replay = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from hermes_cli.dashboard_auth.ws_tickets import consume_handoff_ticket; "
                    "consume_handoff_ticket(sys.stdin.read().strip())"
                ),
            ],
            capture_output=True,
            env=consume_env,
            input=ticket,
            text=True,
        )
        assert replay.returncode != 0
        assert "unknown ticket" in replay.stderr

    def test_expired_rejected(self, monkeypatch):
        clock = {"now": 1_000_000}

        monkeypatch.setattr(ws_tickets.time, "time", lambda: clock["now"])
        ticket = ws_tickets.mint_handoff_ticket(
            session_id="s", user_id="u", provider="stub"
        )
        clock["now"] += ws_tickets.HANDOFF_TTL_SECONDS + 1
        with pytest.raises(TicketInvalid, match="expired"):
            ws_tickets.consume_handoff_ticket(ticket)

    def test_handoff_not_accepted_as_ws_ticket(self):
        ticket = ws_tickets.mint_handoff_ticket(
            session_id="s", user_id="u", provider="stub"
        )
        with pytest.raises(TicketInvalid, match="handoff ticket not valid as ws"):
            consume_ticket(ticket)
        # Still consumable via the handoff path after the WS reject.
        info = ws_tickets.consume_handoff_ticket(ticket)
        assert info["session_id"] == "s"

    def test_ws_ticket_not_accepted_as_handoff(self):
        ticket = mint_ticket(user_id="u1", provider="stub")
        with pytest.raises(TicketInvalid, match="ws ticket not valid as handoff"):
            ws_tickets.consume_handoff_ticket(ticket)
        # WS path still works.
        assert consume_ticket(ticket)["user_id"] == "u1"

    def test_scopes_are_resume_only_never_superuser(self):
        ticket = ws_tickets.mint_handoff_ticket(
            session_id="s", user_id="u", provider="stub"
        )
        info = ws_tickets.consume_handoff_ticket(ticket)
        scopes = set(info["scopes"])
        assert scopes == {"resume"}
        assert not scopes.intersection({"*", "superuser", "API_SERVER_KEY"})

    def test_session_id_required(self):
        with pytest.raises(ValueError, match="session_id"):
            ws_tickets.mint_handoff_ticket(
                session_id="  ", user_id="u", provider="stub"
            )
