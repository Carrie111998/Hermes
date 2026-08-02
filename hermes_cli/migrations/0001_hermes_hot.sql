CREATE SCHEMA IF NOT EXISTS hermes_hot;

CREATE TABLE IF NOT EXISTS hermes_hot.migration_runs (
 run_id uuid PRIMARY KEY, state_sha256 text NOT NULL, agent_logs_sha256 text NOT NULL,
 cutoff double precision NOT NULL, status text NOT NULL CONSTRAINT migration_runs_status CHECK (status = 'completed'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), completed_at timestamptz NOT NULL,
 UNIQUE (state_sha256, agent_logs_sha256, cutoff)
);
CREATE TABLE IF NOT EXISTS hermes_hot.domain_manifests (
 run_id uuid NOT NULL REFERENCES hermes_hot.migration_runs(run_id) ON DELETE CASCADE,
 domain text NOT NULL CHECK (domain IN ('sessions','messages','session_model_usage','async_delegations','compression_locks','gateway_routing','state_meta','agent_logs')), row_count bigint NOT NULL, min_id text, max_id text,
 min_timestamp double precision, max_timestamp double precision, nul_values bigint NOT NULL,
 payload_bytes bigint NOT NULL, digest_sha256 text NOT NULL, source_sha256 text NOT NULL,
 state_sha256 text NOT NULL, agent_logs_sha256 text NOT NULL, PRIMARY KEY (run_id, domain)
);
CREATE TABLE IF NOT EXISTS hermes_hot.sessions (
 id text PRIMARY KEY, source text NOT NULL, user_id text, session_key text, chat_id text, chat_type text, thread_id text, model text, model_config bytea, system_prompt bytea, parent_session_id text, started_at double precision NOT NULL, ended_at double precision, end_reason text, message_count bigint, tool_call_count bigint, input_tokens bigint, output_tokens bigint, cache_read_tokens bigint, cache_write_tokens bigint, reasoning_tokens bigint, cwd bytea, git_branch text, git_repo_root bytea, billing_provider text, billing_base_url bytea, billing_mode text, estimated_cost_usd double precision, actual_cost_usd double precision, cost_status text, cost_source text, pricing_version text, title bytea, api_call_count bigint, handoff_state text, handoff_platform text, handoff_error bytea, compression_failure_cooldown_until double precision, compression_failure_error bytea, rewind_count bigint NOT NULL, archived bigint NOT NULL, display_name bytea, origin_json bytea, expiry_finalized bigint, compression_fallback_streak bigint NOT NULL, compression_ineffective_count bigint NOT NULL, profile_name text, pinned bigint NOT NULL, source_active_message_count bigint NOT NULL, hot_active_message_count bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS hermes_hot.messages (
 id bigint PRIMARY KEY, session_id text NOT NULL REFERENCES hermes_hot.sessions(id), role text NOT NULL, content bytea, tool_call_id text, tool_calls bytea, tool_name text, timestamp double precision NOT NULL, token_count bigint, finish_reason text, reasoning bytea, reasoning_content bytea, reasoning_details bytea, codex_reasoning_items bytea, codex_message_items bytea, platform_message_id text, observed bigint, active bigint NOT NULL, compacted bigint NOT NULL, effect_disposition text, api_content bytea, display_kind text, display_metadata bytea
);
CREATE INDEX IF NOT EXISTS messages_session_timestamp ON hermes_hot.messages USING btree(session_id, timestamp);
CREATE TABLE IF NOT EXISTS hermes_hot.session_model_usage (
 session_id text NOT NULL REFERENCES hermes_hot.sessions(id), model text NOT NULL, billing_provider text NOT NULL, billing_base_url bytea NOT NULL, billing_mode text NOT NULL, task text NOT NULL, api_call_count bigint NOT NULL, input_tokens bigint NOT NULL, output_tokens bigint NOT NULL, cache_read_tokens bigint NOT NULL, cache_write_tokens bigint NOT NULL, reasoning_tokens bigint NOT NULL, estimated_cost_usd double precision NOT NULL, actual_cost_usd double precision NOT NULL, cost_status text, cost_source text, first_seen double precision, last_seen double precision, PRIMARY KEY(session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
CREATE TABLE IF NOT EXISTS hermes_hot.async_delegations (delegation_id text PRIMARY KEY, origin_session text NOT NULL, origin_ui_session_id text NOT NULL, parent_session_id text, state text NOT NULL, dispatched_at double precision NOT NULL, completed_at double precision, updated_at double precision NOT NULL, event_json bytea, result_json bytea, delivery_state text NOT NULL, delivery_attempts bigint NOT NULL, delivered_at double precision, owner_pid bigint, owner_started_at bigint, task_json bytea, delivery_claim text, delivery_claimed_at double precision, origin_session_id text);
CREATE TABLE IF NOT EXISTS hermes_hot.compression_locks (session_id text PRIMARY KEY, holder text NOT NULL, acquired_at double precision NOT NULL, expires_at double precision NOT NULL);
CREATE TABLE IF NOT EXISTS hermes_hot.gateway_routing (scope text NOT NULL, session_key text NOT NULL, entry_json bytea NOT NULL, updated_at double precision NOT NULL, PRIMARY KEY(scope, session_key));
CREATE TABLE IF NOT EXISTS hermes_hot.state_meta (key text PRIMARY KEY, value bytea);
CREATE TABLE IF NOT EXISTS hermes_hot.agent_logs (id text PRIMARY KEY, agent_name text NOT NULL, task_description bytea NOT NULL, model_used text, status text NOT NULL, created_at text NOT NULL, trace_id text, span_id text);
