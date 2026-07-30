-- SkyAI-only durable Discord mirror outbox.
--
-- Apply this migration with a mirror-only database owner.  The runtime role
-- must not receive privileges on Canonical Brain, skyai_ci, or any other
-- application schema.

BEGIN;

CREATE SCHEMA IF NOT EXISTS skyai_discord_mirror;
REVOKE ALL ON SCHEMA skyai_discord_mirror FROM PUBLIC;

CREATE TABLE IF NOT EXISTS skyai_discord_mirror.threads (
    surface text NOT NULL
        CHECK (surface IN ('chat', 'voice')),
    configured_channel_id text NOT NULL
        CHECK (configured_channel_id <> ''),
    conversation_hash text NOT NULL
        CHECK (conversation_hash ~ '^[0-9a-f]{64}$'),
    conversation_id text NOT NULL
        CHECK (
            octet_length(conversation_id) BETWEEN 1 AND 256
        ),
    recovery_name text NOT NULL
        CHECK (
            recovery_name <> ''
            AND char_length(recovery_name) <= 100
        ),
    discord_thread_id text NOT NULL
        CHECK (discord_thread_id <> ''),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (
        surface,
        configured_channel_id,
        conversation_hash
    ),
    UNIQUE (
        surface,
        configured_channel_id,
        conversation_id
    )
);

CREATE TABLE IF NOT EXISTS skyai_discord_mirror.deliveries (
    delivery_id text PRIMARY KEY
        CHECK (
            octet_length(delivery_id) BETWEEN 1 AND 256
        ),
    surface text NOT NULL
        CHECK (surface IN ('chat', 'voice')),
    configured_channel_id text NOT NULL
        CHECK (configured_channel_id <> ''),
    conversation_hash text NOT NULL
        CHECK (conversation_hash ~ '^[0-9a-f]{64}$'),
    conversation_id text NOT NULL
        CHECK (
            octet_length(conversation_id) BETWEEN 1 AND 256
        ),
    -- Restricted transient outbox payload.  It is retained while pending and
    -- retrying, then redacted only after successful delivery and the bounded
    -- operational retention period documented in the plugin README.
    content text,
    chunks jsonb,
    state text NOT NULL
        CHECK (state IN ('pending', 'leased', 'retry', 'delivered')),
    attempt_count integer NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL,
    lease_token text,
    lease_expires_at timestamptz,
    thread_id text,
    next_chunk_index integer NOT NULL DEFAULT 0
        CHECK (next_chunk_index >= 0),
    message_ids jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(message_ids) = 'array'),
    last_error text,
    delivered_at timestamptz,
    payload_redacted_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (
        (content IS NULL AND chunks IS NULL AND payload_redacted_at IS NOT NULL)
        OR
        (
            content IS NOT NULL
            AND content <> ''
            AND chunks IS NOT NULL
            AND jsonb_typeof(chunks) = 'array'
            AND jsonb_array_length(chunks) > 0
            AND payload_redacted_at IS NULL
        )
    ),
    CHECK (
        chunks IS NULL
        OR next_chunk_index <= jsonb_array_length(chunks)
    ),
    CHECK (
        jsonb_array_length(message_ids) = next_chunk_index
    ),
    CHECK (
        (state = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (state <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (state = 'delivered' AND delivered_at IS NOT NULL)
        OR
        (state <> 'delivered' AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS skyai_discord_mirror_deliveries_due_idx
    ON skyai_discord_mirror.deliveries (
        available_at,
        created_at,
        delivery_id
    )
    WHERE state IN ('pending', 'retry');

CREATE INDEX IF NOT EXISTS skyai_discord_mirror_deliveries_expired_lease_idx
    ON skyai_discord_mirror.deliveries (
        lease_expires_at,
        delivery_id
    )
    WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS skyai_discord_mirror_deliveries_retention_idx
    ON skyai_discord_mirror.deliveries (
        delivered_at,
        delivery_id
    )
    WHERE state = 'delivered' AND payload_redacted_at IS NULL;

REVOKE ALL ON ALL TABLES IN SCHEMA skyai_discord_mirror FROM PUBLIC;

-- Provision the login separately as a role with no membership in Canonical
-- Brain, skyai_ci, or application roles. Recommended exact runtime role:
-- skyai_discord_mirror_runtime
--
-- GRANT USAGE ON SCHEMA skyai_discord_mirror
--     TO skyai_discord_mirror_runtime;
-- GRANT SELECT, INSERT, UPDATE
--     ON skyai_discord_mirror.threads,
--        skyai_discord_mirror.deliveries
--     TO skyai_discord_mirror_runtime;
--
-- The migration intentionally does not create a login or embed a password.

COMMIT;
