-- SkyAI customer intelligence schema contract.
-- Apply only to a dedicated SkyAI database/schema. Do not reuse Muncho brain DB.
-- Production roles should grant customer-facing SkyAI INSERT on events only.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS skyai_ci;

CREATE TABLE IF NOT EXISTS skyai_ci.identities (
    identity_id uuid PRIMARY KEY,
    anonymous_id_hash text,
    customer_id_hash text,
    consent_personalization boolean NOT NULL DEFAULT false,
    consent_marketing boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT identities_no_raw_email CHECK ((metadata ? 'email') = false),
    CONSTRAINT identities_no_raw_phone CHECK ((metadata ? 'phone') = false)
);

CREATE UNIQUE INDEX IF NOT EXISTS identities_anonymous_hash_idx
    ON skyai_ci.identities (anonymous_id_hash)
    WHERE anonymous_id_hash IS NOT NULL AND anonymous_id_hash <> '';

CREATE UNIQUE INDEX IF NOT EXISTS identities_customer_hash_idx
    ON skyai_ci.identities (customer_id_hash)
    WHERE customer_id_hash IS NOT NULL AND customer_id_hash <> '';

CREATE TABLE IF NOT EXISTS skyai_ci.events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    received_at timestamptz NOT NULL DEFAULT now(),
    event_type text NOT NULL,
    identity_id uuid REFERENCES skyai_ci.identities(identity_id),
    anonymous_id_hash text,
    conversation_id_hash text,
    surface text NOT NULL DEFAULT 'unknown',
    source text NOT NULL DEFAULT 'skyai_customer_hermes_v2',
    idempotency_key text,
    pii_redaction_status text NOT NULL DEFAULT 'metadata_only_no_raw_text',
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT events_no_raw_text CHECK ((properties ? 'raw_text') = false),
    CONSTRAINT events_no_message CHECK ((properties ? 'message') = false),
    CONSTRAINT events_no_email CHECK ((properties ? 'email') = false),
    CONSTRAINT events_no_phone CHECK ((properties ? 'phone') = false),
    CONSTRAINT events_no_voucher_code CHECK ((properties ? 'voucher_code') = false),
    CONSTRAINT events_no_payment CHECK ((properties ? 'payment') = false)
);

CREATE INDEX IF NOT EXISTS events_occurred_type_idx
    ON skyai_ci.events (occurred_at DESC, event_type);

CREATE INDEX IF NOT EXISTS events_identity_occurred_idx
    ON skyai_ci.events (identity_id, occurred_at DESC)
    WHERE identity_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency_key_idx
    ON skyai_ci.events (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';

CREATE TABLE IF NOT EXISTS skyai_ci.conversation_summaries (
    conversation_id_hash text PRIMARY KEY,
    identity_id uuid REFERENCES skyai_ci.identities(identity_id),
    started_at timestamptz,
    last_seen_at timestamptz,
    public_safe_summary text NOT NULL,
    interests jsonb NOT NULL DEFAULT '[]'::jsonb,
    support_topics jsonb NOT NULL DEFAULT '[]'::jsonb,
    pii_redaction_status text NOT NULL DEFAULT 'summary_only_no_raw_conversation',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS skyai_ci.suppressions (
    suppression_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id uuid REFERENCES skyai_ci.identities(identity_id),
    channel text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT suppressions_no_raw_email CHECK ((metadata ? 'email') = false),
    CONSTRAINT suppressions_no_raw_phone CHECK ((metadata ? 'phone') = false)
);

CREATE INDEX IF NOT EXISTS suppressions_identity_channel_idx
    ON skyai_ci.suppressions (identity_id, channel, created_at DESC);

CREATE TABLE IF NOT EXISTS skyai_ci.journey_state (
    identity_id uuid NOT NULL REFERENCES skyai_ci.identities(identity_id),
    journey text NOT NULL,
    state text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    cooldown_until timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (identity_id, journey)
);

CREATE TABLE IF NOT EXISTS skyai_ci.marketing_outbox (
    outbox_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id uuid REFERENCES skyai_ci.identities(identity_id),
    channel text NOT NULL,
    status text NOT NULL DEFAULT 'draft',
    scheduled_at timestamptz,
    human_approved_at timestamptz,
    subject text,
    body_preview text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT marketing_outbox_no_auto_send CHECK (status <> 'sent' OR human_approved_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS marketing_outbox_status_scheduled_idx
    ON skyai_ci.marketing_outbox (status, scheduled_at);

-- Role contract, expressed as documentation because actual role names are
-- environment-specific:
--
-- skyai_customer_ingest:
--   GRANT USAGE ON SCHEMA skyai_ci;
--   GRANT INSERT ON skyai_ci.events;
--   No SELECT/UPDATE/DELETE on any table.
--
-- skyai_internal_reporting:
--   Read-only SELECT on events, summaries, journey_state, outbox, suppressions.
--
-- skyai_marketing_dry_run:
--   INSERT/UPDATE only on marketing_outbox draft/dry-run rows via controlled
--   internal jobs, never from the public customer-facing runtime.
