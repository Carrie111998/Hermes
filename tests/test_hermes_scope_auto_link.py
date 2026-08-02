"""Tests for the auto-registration hooks that link freshly-created artifacts
(tmux/terminal sessions, delegations, cron jobs) to the current turn's scope,
without ever implicitly creating a scope. See docs/design/thread-scope-isolation.md.
"""
import time

import pytest

import hermes_scope as scope
from gateway.session_context import clear_session_vars, set_session_vars


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def live_session(hermes_home):
    """Set HERMES_SESSION_* context vars so identity_from_session_env() resolves."""
    tokens = set_session_vars(
        platform="discord",
        chat_id="channel-1",
        thread_id="thread-A",
        profile="main",
        session_key="agent:main:discord:thread:channel-1:thread-A",
    )
    yield
    clear_session_vars(tokens)


class TestIdentityFromSessionEnv:
    def test_resolves_from_live_context(self, live_session):
        identity = scope.identity_from_session_env()
        assert identity["platform"] == "discord"
        assert identity["chat_id"] == "channel-1"
        assert identity["thread_id"] == "thread-A"

    def test_no_context_returns_none(self, hermes_home):
        assert scope.identity_from_session_env() is None


class TestResolveCurrentScopeId:
    def test_returns_none_when_no_scope_created_yet(self, live_session, hermes_home):
        assert scope.resolve_current_scope_id(hermes_home=hermes_home) is None

    def test_resolves_existing_scope(self, live_session, hermes_home):
        identity = scope.identity_from_session_env()
        created = scope.create_scope(identity, goal="do the thing", hermes_home=hermes_home)
        assert scope.resolve_current_scope_id(hermes_home=hermes_home) == created["scope_id"]

    def test_never_creates_a_scope(self, live_session, hermes_home):
        scope.resolve_current_scope_id(hermes_home=hermes_home)
        # index must still be empty -- resolution is read-only
        index = scope._load_index(hermes_home)
        assert index == {}


class TestProcessRegistryAutoLink:
    def test_spawn_local_links_owning_scope(self, live_session, hermes_home):
        from tools.process_registry import ProcessRegistry

        identity = scope.identity_from_session_env()
        created = scope.create_scope(identity, goal="do the thing", hermes_home=hermes_home)

        registry = ProcessRegistry()
        session_key = "agent:main:discord:thread:channel-1:thread-A"
        session = registry.spawn_local("echo hello", session_key=session_key)
        registry.wait(session.id, timeout=5)

        assert scope.owns(created["scope_id"], "tmux_session_keys", session_key, hermes_home=hermes_home)

    def test_spawn_without_scope_created_is_a_noop(self, live_session, hermes_home):
        from tools.process_registry import ProcessRegistry

        registry = ProcessRegistry()
        session_key = "agent:main:discord:thread:channel-1:thread-A"
        session = registry.spawn_local("echo hello", session_key=session_key)
        registry.wait(session.id, timeout=5)
        # No exception, and nothing to check ownership against -- the point
        # is this must not create a scope implicitly.
        assert scope._load_index(hermes_home) == {}


class TestAsyncDelegationAutoLink:
    def test_persist_dispatch_links_owning_scope(self, live_session, hermes_home):
        from tools import async_delegation as ad

        identity = scope.identity_from_session_env()
        created = scope.create_scope(identity, goal="do the thing", hermes_home=hermes_home)

        record = {
            "delegation_id": "deleg-test-1",
            "session_key": "agent:main:discord:thread:channel-1:thread-A",
            "dispatched_at": time.time(),
        }
        ad._persist_dispatch(record)

        assert scope.owns(created["scope_id"], "delegation_ids", "deleg-test-1", hermes_home=hermes_home)


class TestCronJobAutoLink:
    def test_create_job_links_owning_scope(self, live_session, hermes_home):
        from cron import jobs as cron_jobs

        identity = scope.identity_from_session_env()
        created = scope.create_scope(identity, goal="do the thing", hermes_home=hermes_home)

        job = cron_jobs.create_job(prompt="do a thing", schedule="0 9 * * *")

        assert scope.owns(created["scope_id"], "cron_job_ids", job["id"], hermes_home=hermes_home)
