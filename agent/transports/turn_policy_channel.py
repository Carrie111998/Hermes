"""Cross-process request-policy channel for the Codex MCP bridge.

``codex app-server`` launches ``hermes-tools`` as a separate MCP subprocess.
ContextVars from the parent Hermes turn therefore do not cross that process
boundary.  This module carries only the small safety state the MCP callback
needs: request phase/text, workspace, selected root skills, and cumulative
skill payload usage.

The channel is an ephemeral, bearer-keyed SQLite row.  Each MCP dispatch holds
an immediate transaction while it validates, applies, and writes back the
policy, so concurrent skill calls cannot race the cumulative payload ceiling.
The row is HMAC-authenticated to fail closed on accidental corruption or an
untrusted/stale path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

POLICY_DB_ENV = "HERMES_CODEX_TURN_POLICY_DB"
POLICY_ID_ENV = "HERMES_CODEX_TURN_POLICY_ID"
POLICY_KEY_ENV = "HERMES_CODEX_TURN_POLICY_KEY"
POLICY_REQUIRED_ENV = "HERMES_CODEX_TURN_POLICY_REQUIRED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_policy (
    policy_id TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    state_json TEXT NOT NULL,
    signature TEXT NOT NULL
)
"""


class TurnPolicyChannelError(RuntimeError):
    """The cross-process safety contract was unavailable or invalid."""


def _signature(key: str, generation: str, state_json: str) -> str:
    message = f"{generation}\n{state_json}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _policy_state(
    policy: Any,
    *,
    fallback_phase: str,
    fallback_request_text: str,
    fallback_workspace: str,
) -> dict[str, Any]:
    phase = getattr(getattr(policy, "phase", None), "value", None)
    if phase is None:
        phase = fallback_phase
    request_text = getattr(policy, "request_text", None)
    if request_text is None:
        request_text = fallback_request_text
    workspace = getattr(policy, "workspace", None)
    if workspace is None:
        workspace = fallback_workspace
    loaded = getattr(policy, "loaded_root_skills", None)
    payload_chars = getattr(policy, "skill_payload_chars", 0)
    return {
        "phase": str(phase or "operation"),
        "request_text": str(request_text or ""),
        "workspace": str(workspace or fallback_workspace),
        "loaded_root_skills": [
            str(name) for name in (loaded or []) if str(name).strip()
        ],
        "skill_payload_chars": max(0, int(payload_chars or 0)),
    }


class CodexTurnPolicyChannel:
    """Parent-side owner for one Codex session's ephemeral policy row."""

    def __init__(self, *, db_path: Optional[Path | str] = None) -> None:
        self.policy_id = uuid.uuid4().hex
        self.key = secrets.token_urlsafe(32)
        if db_path is None:
            handle, raw_path = tempfile.mkstemp(
                prefix="hermes-codex-turn-policy-",
                suffix=".sqlite3",
            )
            os.close(handle)
            self.db_path = Path(raw_path)
        else:
            self.db_path = Path(db_path).expanduser().resolve(strict=False)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._published = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    @property
    def environment(self) -> dict[str, str]:
        return {
            POLICY_DB_ENV: str(self.db_path),
            POLICY_ID_ENV: self.policy_id,
            POLICY_KEY_ENV: self.key,
        }

    @property
    def published(self) -> bool:
        return self._published

    def publish(
        self,
        policy: Any,
        *,
        fallback_phase: str,
        fallback_request_text: str,
        fallback_workspace: str,
    ) -> str:
        """Reset the shared row to the current parent-turn state."""

        if self._closed:
            raise TurnPolicyChannelError("turn-policy channel is closed")
        generation = uuid.uuid4().hex
        state = _policy_state(
            policy,
            fallback_phase=fallback_phase,
            fallback_request_text=fallback_request_text,
            fallback_workspace=fallback_workspace,
        )
        state_json = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        signature = _signature(self.key, generation, state_json)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO turn_policy(
                    policy_id, generation, state_json, signature
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    generation = excluded.generation,
                    state_json = excluded.state_json,
                    signature = excluded.signature
                """,
                (self.policy_id, generation, state_json, signature),
            )
            connection.commit()
        self._published = True
        return generation

    def read_state(self) -> dict[str, Any]:
        """Read and authenticate state for diagnostics/tests."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT generation, state_json, signature
                FROM turn_policy WHERE policy_id = ?
                """,
                (self.policy_id,),
            ).fetchone()
        if row is None:
            raise TurnPolicyChannelError("turn-policy row is missing")
        generation, state_json, signature = row
        expected = _signature(self.key, generation, state_json)
        if not hmac.compare_digest(signature, expected):
            raise TurnPolicyChannelError("turn-policy signature is invalid")
        state = json.loads(state_json)
        if not isinstance(state, dict):
            raise TurnPolicyChannelError("turn-policy state is invalid")
        return state

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for path in (
            self.db_path,
            Path(f"{self.db_path}-journal"),
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # The client process has already been stopped before close.
                # Leave an unusable random-keyed temp file rather than making
                # session shutdown fail.
                pass


def _failure(tool_name: str, detail: str) -> str:
    return json.dumps(
        {
            "success": False,
            "tool": tool_name,
            "error": (
                "Request safety block: the Codex subprocess could not verify "
                f"this turn's policy ({detail}). No tool effect was executed. "
                "Continue without the blocked effect or end with one exact "
                "blocker."
            ),
        },
        ensure_ascii=False,
    )


def _channel_contract_from_env() -> tuple[Path, str, str]:
    raw_path = os.environ.get(POLICY_DB_ENV, "").strip()
    policy_id = os.environ.get(POLICY_ID_ENV, "").strip()
    key = os.environ.get(POLICY_KEY_ENV, "").strip()
    if not raw_path or not policy_id or not key:
        raise TurnPolicyChannelError("policy channel environment is missing")
    return Path(raw_path), policy_id, key


def dispatch_with_turn_policy(
    tool_name: str,
    args: dict[str, Any],
    dispatcher: Callable[[str, dict[str, Any]], str],
) -> str:
    """Dispatch an MCP tool under the authenticated parent-turn policy.

    A configured Codex child must authenticate the channel for every tool:
    otherwise its MCP ``terminal``/file tools could escape the app-server
    sandbox and bypass the parent's phase boundary. Standalone MCP consumers
    that have no policy environment retain native non-skill behavior.
    ``skill_view`` still fails closed without a channel because its cumulative
    content budget cannot be enforced safely.
    """

    channel_expected = any(
        os.environ.get(name, "").strip()
        for name in (
            POLICY_DB_ENV,
            POLICY_ID_ENV,
            POLICY_KEY_ENV,
            POLICY_REQUIRED_ENV,
        )
    )
    try:
        db_path, policy_id, key = _channel_contract_from_env()
    except TurnPolicyChannelError as exc:
        if channel_expected or tool_name == "skill_view":
            return _failure(tool_name, str(exc))
        return dispatcher(tool_name, args)

    connection: Optional[sqlite3.Connection] = None
    token = None
    try:
        connection = sqlite3.connect(str(db_path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT generation, state_json, signature
            FROM turn_policy WHERE policy_id = ?
            """,
            (policy_id,),
        ).fetchone()
        if row is None:
            raise TurnPolicyChannelError("policy row is missing")
        generation, state_json, signature = row
        expected = _signature(key, generation, state_json)
        if not hmac.compare_digest(signature, expected):
            raise TurnPolicyChannelError("policy signature is invalid")
        state = json.loads(state_json)
        if not isinstance(state, dict):
            raise TurnPolicyChannelError("policy state is invalid")

        from agent import request_phase as request_phase_module
        from agent.request_phase import RequestPhase, TurnPolicy

        policy = TurnPolicy(
            phase=RequestPhase(str(state["phase"])),
            request_text=str(state.get("request_text") or ""),
            workspace=Path(str(state.get("workspace") or os.getcwd())),
            loaded_root_skills=[
                str(name)
                for name in (state.get("loaded_root_skills") or [])
                if str(name).strip()
            ],
            skill_payload_chars=max(
                0,
                int(state.get("skill_payload_chars") or 0),
            ),
        )
        token = request_phase_module._ACTIVE_TURN_POLICY.set(policy)
        result = dispatcher(tool_name, args)

        state["loaded_root_skills"] = list(policy.loaded_root_skills)
        state["skill_payload_chars"] = policy.skill_payload_chars
        updated_json = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        updated_signature = _signature(key, generation, updated_json)
        connection.execute(
            """
            UPDATE turn_policy
            SET state_json = ?, signature = ?
            WHERE policy_id = ? AND generation = ?
            """,
            (updated_json, updated_signature, policy_id, generation),
        )
        connection.commit()
        return result
    except Exception as exc:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        return _failure(tool_name, str(exc))
    finally:
        if token is not None:
            request_phase_module._ACTIVE_TURN_POLICY.reset(token)
        if connection is not None:
            connection.close()


__all__ = [
    "CodexTurnPolicyChannel",
    "POLICY_DB_ENV",
    "POLICY_ID_ENV",
    "POLICY_KEY_ENV",
    "POLICY_REQUIRED_ENV",
    "TurnPolicyChannelError",
    "dispatch_with_turn_policy",
]
