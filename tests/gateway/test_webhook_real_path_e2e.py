"""Real local-network integration proof for webhook execution authority."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from aiohttp import ClientSession

from gateway.config import PlatformConfig
from gateway.platforms.base import ProcessingOutcome
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_ledger import OperationState, TargetState


@pytest.mark.asyncio
async def test_signed_loopback_request_runs_exact_route_and_deduplicates(
    tmp_path, monkeypatch
):
    """Exercise the real listener, verifier, filter, script, and durable ledger."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "normalize.py").write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "payload['normalized_total'] = payload['amount'] * 2\n"
        "payload['script_marker'] = 'executed'\n"
        "print(json.dumps(payload, sort_keys=True))\n",
        encoding="utf-8",
    )

    secret = "local-e2e-signing-secret"
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "script_timeout_seconds": 5,
                "routes": {
                    "invoice": {
                        "secret": secret,
                        "provider": "generic",
                        "signature_mode": "generic_v2",
                        "events": ["invoice.created"],
                        "filters": [
                            {"field": "kind", "equals": "invoice"},
                            {"field": "amount", "in": [21]},
                        ],
                        "script": "normalize.py",
                        "prompt": (
                            "Ready {customer} total={normalized_total} "
                            "marker={script_marker}"
                        ),
                        "deliver": "log",
                    }
                },
            },
        )
    )

    completed = asyncio.Event()
    events = []
    send_results = []
    consumer_errors: list[BaseException] = []

    async def consume(event):
        events.append(event)
        try:
            await adapter.on_processing_start(event)
            send_results.append(
                await adapter.send(
                    event.source.chat_id,
                    "ACK::" + event.text,
                    metadata={"notify": True},
                )
            )
            await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        except BaseException as exc:
            consumer_errors.append(exc)
            raise
        finally:
            completed.set()

    adapter.handle_message = consume
    assert await adapter.connect() is True
    try:
        assert adapter._runner is not None
        sockets = [
            sock for site in adapter._runner.sites for sock in site._server.sockets
        ]
        assert sockets
        port = sockets[0].getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}/webhooks/invoice"
        timestamp = str(int(time.time()))

        def signed(payload):
            body = json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            signature = hmac.new(
                secret.encode(),
                timestamp.encode() + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            return body, {
                "Content-Type": "application/json",
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature-V2": signature,
            }

        matching_body, matching_headers = signed({
            "event_type": "invoice.created",
            "kind": "invoice",
            "customer": "ACME",
            "amount": 21,
        })
        filtered_body, filtered_headers = signed({
            "event_type": "invoice.created",
            "kind": "other",
            "customer": "NO-DISPATCH",
            "amount": 21,
        })

        async with ClientSession() as client:
            invalid = await client.post(
                endpoint,
                data=matching_body,
                headers={
                    **matching_headers,
                    "X-Webhook-Signature-V2": "0" * 64,
                },
            )
            invalid_payload = await invalid.json()
            assert invalid.status == 401
            assert invalid_payload == {"error": "Invalid signature"}
            assert adapter._operation_ledger.count() == 0

            filtered = await client.post(
                endpoint,
                data=filtered_body,
                headers=filtered_headers,
            )
            filtered_payload = await filtered.json()
            assert filtered.status == 200
            assert filtered_payload == {
                "status": "ignored",
                "reason": "filter",
                "route": "invoice",
            }
            assert events == []

            accepted = await client.post(
                endpoint,
                data=matching_body,
                headers=matching_headers,
            )
            accepted_payload = await accepted.json()
            assert accepted.status == 202
            assert accepted_payload["status"] == "accepted"
            assert (
                accepted_payload["deduplication"]
                == "authenticated_timestamp_body_sha256"
            )
            await asyncio.wait_for(completed.wait(), timeout=10)

            duplicate = await client.post(
                endpoint,
                data=matching_body,
                headers=matching_headers,
            )
            duplicate_payload = await duplicate.json()
            assert duplicate.status == 200
            assert duplicate_payload["status"] == "duplicate"

        assert len(events) == 1
        assert consumer_errors == []
        assert events[0].text == "Ready ACME total=42 marker=executed"
        assert events[0].raw_message["normalized_total"] == 42
        assert events[0].raw_message["script_marker"] == "executed"
        assert len(send_results) == 1
        assert send_results[0].success is True
        assert send_results[0].raw_response == {"webhook_settlement": "suppressed"}

        authority = adapter._operation_ledger.lookup_session(events[0].source.chat_id)
        assert authority is not None
        assert authority.state is OperationState.SETTLED
        assert authority.target_state is TargetState.SUPPRESSED
        assert authority.delivery is not None
        assert authority.delivery.content == "ACK::Ready ACME total=42 marker=executed"
        assert authority.event_snapshot is not None
        assert authority.event_snapshot["payload"]["script_marker"] == "executed"
        assert authority.grant_snapshot is not None
        assert authority.grant_snapshot["profile_generation"]
        assert adapter._operation_ledger.db_path == tmp_path / "state.db"

        with sqlite3.connect(tmp_path / "state.db") as connection:
            operations = connection.execute(
                "SELECT profile, route, state FROM webhook_operations"
            ).fetchall()
            bindings = connection.execute(
                """SELECT profile, route, provider, signature_mode
                   FROM webhook_auth_key_bindings"""
            ).fetchall()
        assert len(operations) == 2
        assert set(operations) == {("default", "invoice", "settled")}
        assert bindings
        assert set(bindings) == {("default", "invoice", "generic", "generic_v2")}
    finally:
        await adapter.disconnect()
