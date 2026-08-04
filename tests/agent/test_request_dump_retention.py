"""Request-dump retention contract (issue #77472).

``dump_api_request_debug`` writes ``request_dump_<session>_<timestamp>.json``
into ``agent.logs_dir`` on every non-retryable 4xx and every exhausted-retry
failure.  The microsecond timestamp makes every filename unique, so nothing is
ever reclaimed by rewriting, and each dump embeds the full request body (system
prompt + tool schemas + message history).  The only pre-existing cleanup is
session-scoped (``SessionDB._remove_session_files``, on session delete/prune),
so dumps from live and recent sessions accumulated without bound.

These tests drive the REAL dump path on a real ``AIAgent`` against a temp
``HERMES_HOME`` — the assertions are about which files exist on disk, not about
which functions were called.
"""

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent
from agent import agent_runtime_helpers


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so config reads and dumps stay inside the test."""
    home = tmp_path / ".hermes"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def agent(hermes_home, monkeypatch):
    """A real AIAgent whose logs_dir is the temp sessions directory."""
    monkeypatch.setattr(
        run_agent,
        "get_tool_definitions",
        lambda **kwargs: [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run shell commands.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})

    built = run_agent.AIAgent(
        model="gpt-4o",
        base_url="http://127.0.0.1:9208/v1",
        api_key="test-key",
        quiet_mode=True,
        max_iterations=1,
        skip_context_files=True,
        skip_memory=True,
    )
    built.logs_dir = hermes_home / "sessions"
    built._vprint = lambda *a, **k: None
    return built


def _write_config(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(body, encoding="utf-8")


def _kwargs(marker="hello"):
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": marker}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }


def _dumps(logs_dir: Path):
    return sorted(logs_dir.glob("request_dump_*.json"))


def _dump_n(agent, count, *, reason="non_retryable_client_error"):
    """Drive the real dump path *count* times, returning the paths written."""
    written = []
    for i in range(count):
        path = agent._dump_api_request_debug(
            _kwargs(f"turn {i}"), reason=reason, error=ValueError("HTTP 400"),
        )
        assert path is not None, "dump path must still be returned to the caller"
        written.append(path)
    return written


class TestDirectoryStaysBounded:
    def test_n_plus_k_errors_leave_exactly_n_newest_dumps(self, agent):
        """The core invariant: 30 errors under a cap of 5 leave the newest 5."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 5\n",
        )

        written = _dump_n(agent, 30)

        survivors = {p.name for p in _dumps(agent.logs_dir)}
        assert len(survivors) == 5
        assert survivors == {p.name for p in written[-5:]}, "survivors are the newest"

    def test_growth_is_bounded_at_every_step(self, agent):
        """The cap holds continuously, not just after the last write."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 3\n",
        )

        for i in range(15):
            agent._dump_api_request_debug(_kwargs(f"t{i}"), reason="preflight")
            assert len(_dumps(agent.logs_dir)) <= 3, f"cap exceeded after write {i}"

    def test_dump_just_written_is_never_the_one_pruned(self, agent):
        """A burst of errors must not evict the dump the user is about to read."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 1\n",
        )

        for i in range(10):
            path = agent._dump_api_request_debug(_kwargs(f"t{i}"), reason="preflight")
            assert path.exists(), "the returned path must exist for the caller to read"
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["request"]["body"]["messages"][0]["content"] == f"t{i}"

    def test_retention_spans_sessions_not_just_one(self, agent):
        """Per-session caps would not bound the directory — a new id per run is
        exactly how it grew. The cap is directory-wide."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 4\n",
        )

        written = []
        for session_index in range(6):
            agent.session_id = f"20260804_12000{session_index}_abcdef"
            written.extend(_dump_n(agent, 3))

        remaining = {p.name for p in _dumps(agent.logs_dir)}
        # 18 dumps across 6 session ids, cap 4 — the cap is directory-wide, so
        # the survivors are the globally-newest 4 and straddle the session
        # boundary (3 from the last session + 1 from the one before it).
        assert len(remaining) == 4, "cap applies across sessions, not per session"
        assert remaining == {p.name for p in written[-4:]}
        assert len({name.split("_abcdef_")[0] for name in remaining}) == 2, (
            "survivors straddle sessions, proving retention is not per-session"
        )


class TestConfigKnob:
    def test_default_is_twenty_and_lives_in_config_defaults(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["sessions"]["request_dump_retention"] == 20
        assert (
            agent_runtime_helpers._REQUEST_DUMP_DEFAULT_KEEP
            == DEFAULT_CONFIG["sessions"]["request_dump_retention"]
        ), "code default and config default must not drift"

    def test_default_applies_with_no_config_file(self, agent):
        """A fresh install (no config.yaml) is still bounded."""
        assert not (Path(os.environ["HERMES_HOME"]) / "config.yaml").exists()

        _dump_n(agent, 26)

        assert len(_dumps(agent.logs_dir)) == 20

    def test_custom_value_is_honored(self, agent):
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 7\n",
        )

        _dump_n(agent, 12)

        assert len(_dumps(agent.logs_dir)) == 7

    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_value_opts_out_of_pruning(self, agent, value):
        """Operators who manage cleanup externally must be able to disable it."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            f"sessions:\n  request_dump_retention: {value}\n",
        )

        _dump_n(agent, 9)

        assert len(_dumps(agent.logs_dir)) == 9, "pruning must be fully disabled"

    def test_malformed_value_falls_back_to_default_not_unlimited(self, agent):
        """A bad config must not silently restore unbounded growth."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: not-a-number\n",
        )

        assert agent_runtime_helpers._request_dump_keep() == 20

        _dump_n(agent, 23)
        assert len(_dumps(agent.logs_dir)) == 20

    def test_missing_sessions_block_falls_back_to_default(self, agent):
        _write_config(Path(os.environ["HERMES_HOME"]), "model:\n  name: gpt-4o\n")

        assert agent_runtime_helpers._request_dump_keep() == 20

    def test_shipped_example_config_matches_code_default(self):
        """The template is copied verbatim into ~/.hermes/config.yaml by the
        installers, so a mismatch would become an explicit user override."""
        import yaml

        template = Path(__file__).resolve().parents[2] / "cli-config.yaml.example"
        parsed = yaml.safe_load(template.read_text(encoding="utf-8")) or {}
        assert (
            parsed["sessions"]["request_dump_retention"]
            == agent_runtime_helpers._REQUEST_DUMP_DEFAULT_KEEP
        )


class TestDeletionSafety:
    def test_only_request_dumps_are_deleted(self, agent):
        """Session transcripts, state.db and anything else in the sessions
        directory must be untouched."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 2\n",
        )
        bystanders = [
            "state.db",
            "session_20260804_120000_abcdef.json",
            "20260804_120000_abcdef.jsonl",
            "request_dump_notes.txt",
        ]
        for name in bystanders:
            (agent.logs_dir / name).write_text("keep me", encoding="utf-8")

        _dump_n(agent, 8)

        for name in bystanders:
            assert (agent.logs_dir / name).exists(), f"{name} must survive pruning"
        assert len(_dumps(agent.logs_dir)) == 2

    def test_traversal_shaped_session_id_stays_inside_logs_dir(self, agent, tmp_path):
        """A hostile session id (X-Hermes-Session-Id header) must neither escape
        logs_dir on write nor widen what pruning deletes."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 3\n",
        )
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        precious = outside / "precious.json"
        precious.write_text("do not delete", encoding="utf-8")

        agent.session_id = "../../../../outside_dir/pwned"
        written = _dump_n(agent, 6)

        for path in written[-3:]:
            assert path.parent.resolve() == agent.logs_dir.resolve(), (
                "dump must be written directly inside logs_dir"
            )
        assert precious.exists(), "pruning must not reach outside logs_dir"
        assert len(_dumps(agent.logs_dir)) == 3
        assert not (outside / "pwned").exists()

    def test_subdirectory_of_logs_dir_is_not_swept(self, agent):
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 1\n",
        )
        nested = agent.logs_dir / "archive"
        nested.mkdir()
        archived = nested / "request_dump_old_20260101_000000_000000.json"
        archived.write_text("archived", encoding="utf-8")

        _dump_n(agent, 5)

        assert archived.exists(), "pruning must not recurse into subdirectories"


class TestFailureIsNonFatal:
    def test_prune_failure_still_returns_the_dump_path(self, agent, monkeypatch):
        """Retention is housekeeping — it must never break debuggability."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 2\n",
        )

        def explode(*_a, **_k):
            raise RuntimeError("filesystem on fire")

        monkeypatch.setattr(agent_runtime_helpers, "prune_oldest_files", explode)

        path = agent._dump_api_request_debug(_kwargs(), reason="preflight")

        assert path is not None and path.exists(), (
            "a prune failure must not cost the caller the dump"
        )

    def test_dump_content_is_unchanged_by_retention(self, agent):
        """The feature being bounded must still produce the same payload."""
        _write_config(
            Path(os.environ["HERMES_HOME"]),
            "sessions:\n  request_dump_retention: 5\n",
        )

        path = agent._dump_api_request_debug(
            _kwargs("inspect me"),
            reason="max_retries_exhausted",
            error=ValueError("HTTP 400 invalid_request_error"),
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload["reason"] == "max_retries_exhausted"
        assert payload["request"]["url"].endswith("/chat/completions")
        assert payload["request"]["body"]["messages"][0]["content"] == "inspect me"
        assert payload["request"]["body"]["tools"], "tool schemas still captured"
        assert payload["error"]["type"] == "ValueError"
        assert "invalid_request_error" in payload["error"]["message"]


class TestPruneHelperDirectly:
    def test_prune_request_dumps_reports_deleted_count(self, hermes_home):
        _write_config(hermes_home, "sessions:\n  request_dump_retention: 2\n")
        logs_dir = hermes_home / "sessions"
        for i in range(6):
            path = logs_dir / f"request_dump_sid_2026080{i}_120000_00000{i}.json"
            path.write_text("{}", encoding="utf-8")
            stamp = 1_700_000_000.0 + i * 10
            os.utime(path, (stamp, stamp))

        assert agent_runtime_helpers.prune_request_dumps(logs_dir) == 4
        assert len(list(logs_dir.glob("request_dump_*.json"))) == 2

    def test_prune_request_dumps_on_missing_dir_is_safe(self, hermes_home):
        assert agent_runtime_helpers.prune_request_dumps(hermes_home / "gone") == 0
