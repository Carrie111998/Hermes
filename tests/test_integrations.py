"""Tests for External Integrations (v0.26.0).

Covers:
- Credential storage (store, get, delete, list, tenant isolation)
- Webhook management (register, deactivate, list)
- Event recording (idempotent, unprocessed queue, mark processed)
- Full acceptance cycle

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import time
import uuid

import pytest

POSTGRES_URL = os.environ.get("AUTHORITY_POSTGRES_TEST_URL", "")


@pytest.fixture
def pg():
    if not POSTGRES_URL:
        pytest.skip("AUTHORITY_POSTGRES_TEST_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg not installed")

    schema_name = f"integ_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(POSTGRES_URL, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    conn.commit()
    from hermes_cli.postgres_authority import init_schema
    init_schema(conn)
    yield conn
    conn.close()
    cleanup = psycopg.connect(POSTGRES_URL, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema_name} CASCADE")
    cleanup.close()


class TestCredentials:
    def test_store_and_get(self, pg):
        from hermes_cli.postgres_authority import (
            store_credential, get_credential, DEFAULT_TENANT_ID,
        )

        cred = store_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            credential_type="oauth2",
            data="encrypted:refresh_token_abc123",
            scopes=["gmail.send", "gmail.read"],
        )
        assert cred["provider"] == "google"
        assert cred["credential_type"] == "oauth2"

        fetched = get_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
        )
        assert fetched["encrypted_data"] == "encrypted:refresh_token_abc123"
        assert "gmail.send" in fetched["scopes"]

    def test_store_upserts(self, pg):
        from hermes_cli.postgres_authority import (
            store_credential, get_credential, DEFAULT_TENANT_ID,
        )

        store_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="crm", provider="salesforce",
            credential_type="oauth2", data="old_token",
        )
        store_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="crm", provider="salesforce",
            credential_type="oauth2", data="new_token",
        )
        cred = get_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="crm", provider="salesforce",
        )
        assert cred["encrypted_data"] == "new_token"

    def test_delete_credential(self, pg):
        from hermes_cli.postgres_authority import (
            store_credential, delete_credential, get_credential, DEFAULT_TENANT_ID,
        )

        store_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="cal", provider="outlook",
            credential_type="api_key", data="key_xyz",
        )
        result = delete_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="cal", provider="outlook",
        )
        assert result is True
        assert get_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="cal", provider="outlook",
        ) is None

    def test_list_excludes_encrypted_data(self, pg):
        from hermes_cli.postgres_authority import (
            store_credential, list_credentials, DEFAULT_TENANT_ID,
        )

        store_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="sendgrid",
            credential_type="api_key", data="secret_key_123",
        )
        creds = list_credentials(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(creds) >= 1
        for c in creds:
            assert "encrypted_data" not in c

    def test_tenant_isolation(self, pg):
        from hermes_cli.postgres_authority import (
            store_credential, get_credential,
        )

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())

        store_credential(
            pg, tenant_id=tenant_a,
            integration_id="email", provider="google",
            credential_type="oauth2", data="tenant_a_secret",
        )

        assert get_credential(
            pg, tenant_id=tenant_b,
            integration_id="email", provider="google",
        ) is None


class TestWebhooks:
    def test_register_and_list(self, pg):
        from hermes_cli.postgres_authority import (
            register_webhook, list_webhooks, DEFAULT_TENANT_ID,
        )

        wh = register_webhook(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            event_type="email.received",
            webhook_url="https://api.example.com/hooks/email",
            secret="whsec_abc123",
        )
        assert wh["event_type"] == "email.received"
        assert wh["active"] is True

        hooks = list_webhooks(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(hooks) == 1
        assert hooks[0]["webhook_url"] == "https://api.example.com/hooks/email"

    def test_deactivate(self, pg):
        from hermes_cli.postgres_authority import (
            register_webhook, deactivate_webhook, list_webhooks, DEFAULT_TENANT_ID,
        )

        register_webhook(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="cal", provider="google",
            event_type="calendar.event.created",
            webhook_url="https://api.example.com/hooks/cal",
        )
        result = deactivate_webhook(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="cal", event_type="calendar.event.created",
        )
        assert result is True

        hooks = list_webhooks(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(hooks) == 0

    def test_register_upserts(self, pg):
        from hermes_cli.postgres_authority import (
            register_webhook, list_webhooks, DEFAULT_TENANT_ID,
        )

        register_webhook(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="crm", provider="hubspot",
            event_type="contact.created",
            webhook_url="https://old.example.com/hooks",
        )
        register_webhook(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="crm", provider="hubspot",
            event_type="contact.created",
            webhook_url="https://new.example.com/hooks",
        )
        hooks = list_webhooks(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(hooks) == 1
        assert hooks[0]["webhook_url"] == "https://new.example.com/hooks"


class TestEvents:
    def test_record_event(self, pg):
        from hermes_cli.postgres_authority import (
            record_integration_event, get_unprocessed_events, DEFAULT_TENANT_ID,
        )

        result = record_integration_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            event_type="email.received", event_id="msg_001",
            payload={"from": "user@example.com", "subject": "Hello"},
        )
        assert result is True

        events = get_unprocessed_events(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(events) == 1
        assert events[0]["event_type"] == "email.received"

    def test_record_idempotent(self, pg):
        from hermes_cli.postgres_authority import (
            record_integration_event, get_unprocessed_events, DEFAULT_TENANT_ID,
        )

        record_integration_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            event_type="email.received", event_id="dup_001",
            payload={"subject": "First"},
        )
        dup = record_integration_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            event_type="email.received", event_id="dup_001",
            payload={"subject": "Duplicate"},
        )
        assert dup is False

        events = get_unprocessed_events(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(events) == 1

    def test_mark_processed(self, pg):
        from hermes_cli.postgres_authority import (
            record_integration_event, get_unprocessed_events,
            mark_event_processed, DEFAULT_TENANT_ID,
        )

        record_integration_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="crm", provider="salesforce",
            event_type="contact.updated", event_id="evt_002",
            payload={"contact_id": "c123"},
        )

        events = get_unprocessed_events(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(events) == 1

        result = mark_event_processed(pg, event_id_internal=events[0]["id"])
        assert result is True

        remaining = get_unprocessed_events(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(remaining) == 0


class TestIntegrationAcceptance:
    """Full cycle: credential → webhook → event → process."""

    def test_full_integration_cycle(self, pg):
        from hermes_cli.postgres_authority import (
            store_credential, register_webhook,
            record_integration_event, get_unprocessed_events,
            mark_event_processed, get_credential, DEFAULT_TENANT_ID,
        )

        # 1. Store OAuth2 credential for Gmail
        store_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            credential_type="oauth2",
            data="encrypted:ya29.refresh_token_xyz",
            scopes=["gmail.send", "gmail.read", "gmail.labels"],
            expires_at=time.time() + 3600,
        )

        # 2. Register webhook for inbound emails
        register_webhook(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            event_type="email.received",
            webhook_url="https://hermes.example.com/hooks/gmail",
            secret="whsec_gmail_abc",
        )

        # 3. Simulate receiving an email event
        recorded = record_integration_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
            event_type="email.received",
            event_id="msg_google_789",
            payload={
                "from": "client@bigcorp.com",
                "subject": "Q3 Budget Approval",
                "thread_id": "thread_abc",
                "labels": ["INBOX", "IMPORTANT"],
            },
        )
        assert recorded is True

        # 4. Agent retrieves unprocessed events
        events = get_unprocessed_events(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(events) == 1
        event = events[0]
        assert event["payload"]["subject"] == "Q3 Budget Approval"

        # 5. Agent processes the event (e.g., drafts a reply)
        # It needs the credential to send
        cred = get_credential(
            pg, tenant_id=DEFAULT_TENANT_ID,
            integration_id="email", provider="google",
        )
        assert cred is not None
        assert "gmail.send" in cred["scopes"]

        # 6. Mark event as processed
        mark_event_processed(pg, event_id_internal=event["id"])

        # 7. No more pending events
        remaining = get_unprocessed_events(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(remaining) == 0
