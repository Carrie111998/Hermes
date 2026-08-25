"""Managed-context fingerprint for durable-session prompt restore (#68563).

Gateway / OpenAI-compatible API sessions persist ``system_prompt`` and reuse
it on the next agent start. After HTTP 409 ``session_exists``, a client
continues that durable row instead of creating a new session. Restore must
not resurrect a stale SOUL / managed-context snapshot just because Model
and Provider still match.

Dual-store boundary (#1081, cross-repo): this engine patch rebuilds the
prompt pair on SOUL/managed-context drift and keeps conversation history
on the same session id (the #68563 contract). The BFF/UI thread is a
separate store. ``sessions.system_prompt_fingerprint`` is the observable
version a BFF can watch to mint a new thread; clearing BFF history is
not done here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.conversation_loop import (
    _restore_or_build_system_prompt,
    build_prompt_with_fingerprint,
    compute_current_context_fingerprint,
)
from agent.prompt_builder import compute_context_fingerprint, load_soul_md
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


STALE_SOUL = "STALE SOUL TOKEN unique-old-soul-abc"
CURRENT_SOUL = "CURRENT SOUL TOKEN unique-new-soul-xyz"
HISTORY = [{"role": "user", "content": "hello from the durable API session"}]


def _agent(session_db, session_id: str, *, home):
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = session_id
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent.platform = "api"
    agent._session_db = session_db
    agent._use_prompt_caching = False
    # SOUL-only fingerprint so the worktree's AGENTS.md cannot shadow the test.
    agent.skip_context_files = True
    agent.load_soul_identity = True
    agent._bot_mode_protocol = False

    def _build(_system_message=None):
        soul = load_soul_md(home_override=home) or ""
        return (
            "You are Hermes Agent.\n"
            f"{soul}\n"
            "Model: test-model\n"
            "Provider: openrouter"
        )

    agent._build_system_prompt = _build
    return agent


def _open_home(tmp_path, soul: str):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "SOUL.md").write_text(soul + "\n", encoding="utf-8")
    token = set_hermes_home_override(home)
    db = SessionDB(db_path=tmp_path / "state.db")
    return home, token, db


class TestApi409StaleSoulResurrection:
    def test_409_continuation_rebuilds_stale_soul_before_reuse(
        self, tmp_path
    ):
        """Existing durable API session + 409 session_exists must not resurrect
        a stale SOUL snapshot on the next agent start.

        Reproduction of the 409 continuation class:

        1. A durable API session row already exists with conversation history
           and a stored system prompt assembled from an older SOUL.md.
        2. The client POSTs the same session id, receives HTTP 409
           ``session_exists``, and continues that row rather than minting a
           new session.
        3. The next AIAgent start restores via ``_restore_or_build_system_prompt``.

        The helper (not the HTTP handler) is the restore seam. It must rebuild
        from the current SOUL and atomically replace the stored prompt (and
        its managed-context fingerprint, once persisted) before any reuse.

        Engine history stays on the same session id. The new fingerprint is
        the version contract a BFF can observe (#1081); this test does not
        delete transcript rows or call website code.
        """
        home, token, db = _open_home(tmp_path, CURRENT_SOUL)
        try:
            session_id = "api-durable-409-session"
            stale_prompt = (
                "You are Hermes Agent.\n"
                f"{STALE_SOUL}\n"
                "Model: test-model\n"
                "Provider: openrouter"
            )
            db.create_session(
                session_id,
                source="api",
                model="test-model",
                system_prompt=stale_prompt,
            )
            db.append_message(
                session_id, "user", "hello from the durable API session"
            )
            conn = db._conn
            assert conn is not None
            conn.execute(
                "UPDATE sessions SET system_prompt_fingerprint = ? "
                "WHERE id = ?",
                ("stale-managed-context-fingerprint", session_id),
            )
            conn.commit()

            agent = _agent(db, session_id, home=home)
            _restore_or_build_system_prompt(agent, None, HISTORY)

            cached = agent._cached_system_prompt or ""
            assert CURRENT_SOUL in cached
            assert STALE_SOUL not in cached

            row = db.get_session(session_id) or {}
            persisted = row.get("system_prompt") or ""
            assert CURRENT_SOUL in persisted
            assert STALE_SOUL not in persisted
            expected_fp = compute_current_context_fingerprint(agent)
            assert row.get("system_prompt_fingerprint") == expected_fp
            assert expected_fp != "stale-managed-context-fingerprint"
            # Same UPDATE wrote hash + fingerprint; raw blob is content-addressed.
            raw = conn.execute(
                "SELECT system_prompt, system_prompt_hash, "
                "system_prompt_fingerprint FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert raw["system_prompt"] is None
            assert raw["system_prompt_hash"]
            assert raw["system_prompt_fingerprint"] == expected_fp
            messages = db.get_messages(session_id)
            assert any(
                (m.get("content") or "") == "hello from the durable API session"
                for m in messages
            )
        finally:
            db.close()
            reset_hermes_home_override(token)


class TestHermesHomeFingerprintLifecycle:
    def test_persist_reuse_soul_edit_rebuild_then_reuse(self, tmp_path):
        """Real temp HERMES_HOME: persist → byte-stable reuse → SOUL edit
        rebuilds a new pair → subsequent turn reuses the new pair."""
        home, token, db = _open_home(tmp_path, CURRENT_SOUL)
        try:
            session_id = "e2e-soul-lifecycle"
            db.create_session(session_id, source="api", model="test-model")
            agent = _agent(db, session_id, home=home)

            _restore_or_build_system_prompt(agent, None, [])
            first = agent._cached_system_prompt
            assert CURRENT_SOUL in first
            first_fp = (db.get_session(session_id) or {}).get(
                "system_prompt_fingerprint"
            )
            assert first_fp

            agent2 = _agent(db, session_id, home=home)
            _restore_or_build_system_prompt(agent2, None, HISTORY)
            assert agent2._cached_system_prompt == first
            assert agent2._cached_system_prompt.encode("utf-8") == first.encode("utf-8")
            assert (db.get_session(session_id) or {}).get(
                "system_prompt_fingerprint"
            ) == first_fp

            (home / "SOUL.md").write_text("EDITED SOUL TOKEN unique-edit-soul\n")
            agent3 = _agent(db, session_id, home=home)
            _restore_or_build_system_prompt(agent3, None, HISTORY)
            rebuilt = agent3._cached_system_prompt or ""
            assert "unique-edit-soul" in rebuilt
            assert CURRENT_SOUL not in rebuilt
            rebuilt_fp = (db.get_session(session_id) or {}).get(
                "system_prompt_fingerprint"
            )
            assert rebuilt_fp
            assert rebuilt_fp != first_fp

            agent4 = _agent(db, session_id, home=home)
            _restore_or_build_system_prompt(agent4, None, HISTORY)
            assert agent4._cached_system_prompt == rebuilt
            assert (db.get_session(session_id) or {}).get(
                "system_prompt_fingerprint"
            ) == rebuilt_fp
        finally:
            db.close()
            reset_hermes_home_override(token)

    def test_legacy_null_fingerprint_self_heals_once(self, tmp_path):
        home, token, db = _open_home(tmp_path, CURRENT_SOUL)
        try:
            session_id = "legacy-null-fp"
            prompt = (
                "You are Hermes Agent.\n"
                f"{CURRENT_SOUL}\n"
                "Model: test-model\n"
                "Provider: openrouter"
            )
            db.create_session(
                session_id, source="api", model="test-model", system_prompt=prompt
            )
            conn = db._conn
            assert conn is not None
            conn.execute(
                "UPDATE sessions SET system_prompt_fingerprint = NULL WHERE id = ?",
                (session_id,),
            )
            conn.commit()
            agent = _agent(db, session_id, home=home)
            _restore_or_build_system_prompt(agent, None, HISTORY)
            row = db.get_session(session_id) or {}
            assert row.get("system_prompt_fingerprint")
            assert CURRENT_SOUL in (row.get("system_prompt") or "")
        finally:
            db.close()
            reset_hermes_home_override(token)


class TestFingerprintFailOpenAndToctou:
    def test_fingerprint_compute_failure_fails_open_to_runtime_reuse(self):
        stored = (
            "You are Hermes Agent.\n"
            "Model: test-model\n"
            "Provider: openrouter"
        )
        db = MagicMock()
        db.get_session.return_value = {
            "system_prompt": stored,
            "system_prompt_fingerprint": "anything",
        }
        agent = MagicMock()
        agent._cached_system_prompt = None
        agent.session_id = "fail-open"
        agent.model = "test-model"
        agent.provider = "openrouter"
        agent.platform = "api"
        agent._session_db = db
        agent._use_prompt_caching = False
        agent.skip_context_files = True
        agent.load_soul_identity = False
        agent._bot_mode_protocol = False
        agent._build_system_prompt = MagicMock(return_value="SHOULD_NOT_BUILD")

        with patch(
            "agent.conversation_loop.compute_current_context_fingerprint",
            return_value=None,
        ):
            _restore_or_build_system_prompt(
                agent, None, [{"role": "user", "content": "hi"}]
            )

        assert agent._cached_system_prompt == stored
        agent._build_system_prompt.assert_not_called()
        db.update_system_prompt.assert_not_called()

    def test_toctou_disagreement_persists_null_fingerprint(self):
        agent = MagicMock()
        agent._build_system_prompt = MagicMock(return_value="built")
        with patch(
            "agent.conversation_loop.compute_current_context_fingerprint",
            side_effect=["pre-fp", "post-fp"],
        ):
            prompt, fingerprint = build_prompt_with_fingerprint(agent, None)
        assert prompt == "built"
        assert fingerprint is None


class TestContextFingerprintSemantics:
    def test_soul_edit_changes_digest(self, tmp_path):
        home = tmp_path / "h"
        home.mkdir()
        (home / "SOUL.md").write_text("alpha\n")
        token = set_hermes_home_override(home)
        try:
            first = compute_context_fingerprint(
                include_soul=True,
                include_project_context=False,
                home_override=home,
            )
            (home / "SOUL.md").write_text("beta\n")
            second = compute_context_fingerprint(
                include_soul=True,
                include_project_context=False,
                home_override=home,
            )
            assert first != second
        finally:
            reset_hermes_home_override(token)

    def test_shadowed_project_file_edit_does_not_change_digest(self, tmp_path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        (cwd / ".hermes.md").write_text("winner\n")
        (cwd / "AGENTS.md").write_text("shadowed-v1\n")
        first = compute_context_fingerprint(
            cwd=str(cwd),
            include_soul=False,
            include_project_context=True,
        )
        (cwd / "AGENTS.md").write_text("shadowed-v2\n")
        second = compute_context_fingerprint(
            cwd=str(cwd),
            include_soul=False,
            include_project_context=True,
        )
        assert first == second

    def test_adding_shadowed_candidate_changes_digest(self, tmp_path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        (cwd / ".hermes.md").write_text("winner\n")
        first = compute_context_fingerprint(
            cwd=str(cwd),
            include_soul=False,
            include_project_context=True,
        )
        (cwd / "AGENTS.md").write_text("now exists\n")
        second = compute_context_fingerprint(
            cwd=str(cwd),
            include_soul=False,
            include_project_context=True,
        )
        assert first != second
