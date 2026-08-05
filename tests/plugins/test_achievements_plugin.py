"""Tests for the bundled hermes-achievements dashboard plugin.

These target the two behaviors that matter for official integration:

* The 200-session scan cap is removed — the plugin now walks the entire
  session history by default. Lifetime badges (tens of thousands of
  tool calls) were unreachable before this fix on long-running installs.
* First-ever scans run in a background thread so the dashboard request
  path never blocks, even on 8000+ session databases where a cold scan
  takes minutes.

The upstream repo ships its own unittest suite under
``plugins/hermes-achievements/tests/`` covering the achievement engine
internals (tier math, secret-state handling, catalog invariants). These
tests live at the hermes-agent level and focus on the integration
contract: the plugin scans ALL of your sessions, not the first 200.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PLUGIN_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "hermes-achievements"
    / "dashboard"
    / "plugin_api.py"
)


@pytest.fixture
def plugin_api(tmp_path, monkeypatch):
    """Load plugin_api with isolated ~/.hermes so state/snapshot files don't collide.

    We load the module fresh per test because the plugin keeps module-level
    caches (``_SNAPSHOT_CACHE``, ``_SCAN_STATUS``, background thread handle).
    Reloading gives each test a clean world.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    spec = importlib.util.spec_from_file_location(
        f"plugin_api_test_{id(tmp_path)}", PLUGIN_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Stash monkeypatch so ``_install_fake_session_db`` can use it to
    # swap ``sys.modules['hermes_state']`` with auto-restoration. Without
    # this, a raw ``sys.modules[...] = fake`` assignment would leak the
    # fake into later tests in the same xdist worker — breaking every
    # test that does ``from hermes_state import SessionDB``.
    module._test_monkeypatch = monkeypatch
    yield module


class _FakeSessionDB:
    """Stand-in for hermes_state.SessionDB that records scan calls."""

    def __init__(self, session_count: int, scan_delay: float = 0):
        self.session_count = session_count
        self.scan_delay = scan_delay
        self.last_limit: Optional[int] = None
        self.last_include_children: Optional[bool] = None
        self.list_calls = 0
        self.messages_calls = 0

    def list_sessions_rich(
        self,
        source: Optional[str] = None,
        exclude_sources: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
    ) -> List[Dict[str, Any]]:
        if self.scan_delay:
            time.sleep(self.scan_delay)
        self.last_limit = limit
        self.last_include_children = include_children
        self.list_calls += 1
        # SQLite semantics: LIMIT -1 = unlimited. Honor that here.
        effective = self.session_count if limit == -1 else min(self.session_count, limit)
        now = int(time.time())
        return [
            {
                "id": f"sess-{i}",
                "title": f"Session {i}",
                "preview": f"preview {i}",
                "started_at": now - (self.session_count - i) * 60,
                "last_active": now - (self.session_count - i) * 60 + 30,
                "source": "cli",
                "model": "test-model",
            }
            for i in range(effective)
        ]

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        self.messages_calls += 1
        return [
            {"role": "user", "content": f"ask {session_id}"},
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "terminal"}}],
            },
            {"role": "tool", "tool_name": "terminal", "content": "ok"},
        ]

    def close(self) -> None:
        pass


def _install_fake_session_db(plugin_api, fake_db):
    """Inject a fake SessionDB so ``scan_sessions`` finds it via its local import.

    Uses the monkeypatch stashed on ``plugin_api`` by the fixture, so the
    ``sys.modules['hermes_state']`` swap is auto-restored at test teardown
    and cannot leak into unrelated tests in the same xdist worker.
    """
    fake_module = type(sys)("hermes_state")
    fake_module.SessionDB = lambda: fake_db
    plugin_api._test_monkeypatch.setitem(sys.modules, "hermes_state", fake_module)


class _Request:
    def __init__(self, query=None, header=""):
        self.query_params = query or {}
        self.headers = {"accept-language": header}


def _fixed_snapshot(plugin_api):
    achievements = []
    unlocked_count = 0
    secret_count = 0
    for index, definition in enumerate(plugin_api.ACHIEVEMENTS):
        is_secret = bool(definition.get("secret"))
        state = "secret" if is_secret else "unlocked"
        if is_secret:
            secret_count += 1
        else:
            unlocked_count += 1
        achievements.append(
            plugin_api.display_achievement(
                {
                    **definition,
                    "unlocked": not is_secret,
                    "discovered": not is_secret,
                    "state": state,
                    "progress": 1 if not is_secret else 0,
                    "progress_pct": 100 if not is_secret else 0,
                    "tier": "Bronze" if not is_secret else None,
                    "unlocked_at": len(plugin_api.ACHIEVEMENTS) - index,
                }
            )
        )
    return {
        "achievements": achievements,
        "sessions": [
            {
                "session_id": "session-1",
                "tool_call_count": 100_000,
                "distinct_tool_count": 100,
                "message_count": 100_000,
                "terminal_calls": 100_000,
                "file_tool_calls": 100_000,
                "web_calls": 100_000,
                "web_browser_calls": 100_000,
                "files_touched_count": 100_000,
            }
        ],
        "aggregate": {},
        "scan_meta": {"mode": "full", "sessions_total": 1},
        "error": None,
        "unlocked_count": unlocked_count,
        "discovered_count": 0,
        "secret_count": secret_count,
        "total_count": len(achievements),
        "generated_at": 1,
    }


def _stub_evaluate_all(plugin_api, snapshot):
    plugin_api._test_monkeypatch.setattr(
        plugin_api, "evaluate_all", lambda force=False: snapshot
    )


def _contains_cjk(value):
    return any("\u3400" <= character <= "\u9fff" for character in value)


def _string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _string_values(item)


def test_scan_sessions_default_scans_all_history_not_first_200(plugin_api):
    """Bug regression: ``scan_sessions()`` used to cap at limit=200.

    A user with 8000+ sessions would only see ~2% of their history in
    achievement totals, making lifetime badges unreachable. The default
    now passes ``LIMIT -1`` (SQLite "unlimited") to ``list_sessions_rich``.
    """
    fake_db = _FakeSessionDB(session_count=500)  # > old 200 cap
    _install_fake_session_db(plugin_api, fake_db)

    result = plugin_api.scan_sessions()

    assert fake_db.last_limit == -1, (
        "scan_sessions() must pass LIMIT=-1 (unlimited) to list_sessions_rich "
        f"by default, got {fake_db.last_limit}"
    )
    assert fake_db.last_include_children is True, (
        "scan_sessions() must include subagent/compression child sessions so "
        "tool calls made in delegated agents still count toward achievements"
    )
    assert len(result["sessions"]) == 500
    assert result["scan_meta"]["sessions_total"] == 500


def test_evaluate_all_first_run_returns_pending_and_starts_background_scan(plugin_api):
    """First-ever evaluate_all with no cache returns a pending placeholder
    immediately and kicks off a background scan thread. Cold scans on
    large DBs take minutes — blocking the dashboard request path is not
    acceptable.
    """
    fake_db = _FakeSessionDB(session_count=50)
    _install_fake_session_db(plugin_api, fake_db)

    # Wrap _run_scan_and_update_cache so we can release it on demand,
    # simulating a slow cold scan without actually waiting.
    scan_started = threading.Event()
    allow_scan_finish = threading.Event()
    original_run = plugin_api._run_scan_and_update_cache

    def gated_run(*args, **kwargs):
        scan_started.set()
        allow_scan_finish.wait(timeout=5)
        original_run(*args, **kwargs)

    plugin_api._run_scan_and_update_cache = gated_run

    t0 = time.time()
    result = plugin_api.evaluate_all()
    elapsed = time.time() - t0

    # Immediate return — should not block waiting for the scan.
    assert elapsed < 1.0, f"evaluate_all blocked for {elapsed:.2f}s on first run"
    assert result["scan_meta"]["mode"] == "pending"
    assert result["unlocked_count"] == 0
    # Catalog still rendered so UI has something to draw.
    assert result["total_count"] >= 60

    # Background scan is running.
    assert scan_started.wait(timeout=2), "background scan did not start"

    # Let the scan complete, then a second call returns real data.
    allow_scan_finish.set()
    # Wait for thread to finish.
    thread = plugin_api._BACKGROUND_SCAN_THREAD
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    second = plugin_api.evaluate_all()
    assert second["scan_meta"]["mode"] != "pending"
    assert second["scan_meta"].get("sessions_total") == 50


def test_start_background_scan_is_idempotent_while_running(plugin_api):
    """Multiple concurrent dashboard requests must not spawn duplicate scans."""
    fake_db = _FakeSessionDB(session_count=5)
    _install_fake_session_db(plugin_api, fake_db)

    release = threading.Event()
    original_run = plugin_api._run_scan_and_update_cache

    def gated_run(*args, **kwargs):
        release.wait(timeout=5)
        original_run(*args, **kwargs)

    plugin_api._run_scan_and_update_cache = gated_run

    plugin_api._start_background_scan()
    first_thread = plugin_api._BACKGROUND_SCAN_THREAD
    assert first_thread is not None and first_thread.is_alive()

    plugin_api._start_background_scan()
    plugin_api._start_background_scan()

    assert plugin_api._BACKGROUND_SCAN_THREAD is first_thread

    release.set()
    first_thread.join(timeout=5)


def test_background_scan_publishes_partial_snapshots(plugin_api):
    """The background scanner publishes intermediate snapshots to the cache
    every ~N sessions. Each dashboard refresh during a long cold scan sees
    more badges unlocked instead of staring at zeros for minutes and then
    having everything pop at the end.
    """
    fake_db = _FakeSessionDB(session_count=750)
    _install_fake_session_db(plugin_api, fake_db)

    # Record every partial snapshot the scanner publishes.
    partial_snapshots: List[Dict[str, Any]] = []
    original_compute_from_scan = plugin_api._compute_from_scan

    def recording_compute(scan, *, is_partial=False):
        result = original_compute_from_scan(scan, is_partial=is_partial)
        if is_partial:
            partial_snapshots.append(result)
        return result

    plugin_api._compute_from_scan = recording_compute

    # scan 750 sessions with progress_every=250 → expect 2 intermediate
    # publications (at 250 and 500; the final 750 call goes through the
    # finished, non-partial path).
    plugin_api._run_scan_and_update_cache(publish_partial_snapshots=True)

    assert len(partial_snapshots) >= 2, (
        f"expected at least 2 partial publications on a 750-session scan with "
        f"progress_every=250, got {len(partial_snapshots)}"
    )
    # Partial snapshots should report growing session counts.
    counts = [p["scan_meta"].get("sessions_scanned_so_far") for p in partial_snapshots]
    assert counts == sorted(counts), f"partial session counts not monotonic: {counts}"
    assert counts[0] < 750 and counts[-1] < 750, (
        f"partial counts should be less than the final total; got {counts}"
    )
    # Every partial reports the expected end-state total so the UI can
    # show an accurate progress bar.
    for p in partial_snapshots:
        assert p["scan_meta"].get("sessions_expected_total") == 750

    # Final snapshot in cache is the real (non-partial) one.
    final = plugin_api._SNAPSHOT_CACHE
    assert final is not None
    assert final["scan_meta"].get("mode") != "in_progress"
    assert final["scan_meta"].get("sessions_total") == 750


def test_partial_snapshots_do_not_persist_unlock_timestamps(plugin_api):
    """Intermediate snapshots must not write to state.json — an unlock
    that appears at 30% scan progress could disappear when a later session
    rebalances the aggregate. Only the final snapshot records ``unlocked_at``.
    """
    fake_db = _FakeSessionDB(session_count=10)
    _install_fake_session_db(plugin_api, fake_db)

    # Seed empty state, then invoke partial compute directly.
    plugin_api.save_state({"unlocks": {}})
    partial_scan = {
        "sessions": [{"session_id": "x", "tool_call_count": 99999, "tool_names": set()}],
        "aggregate": {"max_tool_calls_in_session": 99999, "total_tool_calls": 99999},
        "scan_meta": {"mode": "in_progress"},
    }
    result = plugin_api._compute_from_scan(partial_scan, is_partial=True)

    # Some achievements should evaluate as unlocked in this aggregate...
    assert any(a["unlocked"] for a in result["achievements"])

    # ...but state.json on disk stays empty (no timestamps were recorded).
    persisted = plugin_api.load_state()
    assert persisted.get("unlocks", {}) == {}, (
        "partial scans must not record unlock timestamps — a later session "
        "could change whether the badge deserves to be unlocked yet"
    )


def test_achievements_locale_applies_names_descriptions_and_criteria(plugin_api):
    """zh-CN locale should translate achievement data, not just dashboard chrome."""
    definition = next(a for a in plugin_api.ACHIEVEMENTS if a["id"] == "let_him_cook")
    item = plugin_api.display_achievement({**definition, "state": "discovered", "unlocked": False}, "zh-CN")

    assert item["name"] == "放手一搏"
    assert item["description"] == "让 Hermes 在一次会话中自主执行一套完整的工具链。"
    assert item["category"] == "自主代理"
    assert "要求" in item["criteria"]
    assert "单次会话工具调用次数" in item["criteria"]
    assert "青铜 200" in item["criteria"]


def test_zh_cn_locale_translates_every_catalog_metric(plugin_api):
    """Every metric used to render criteria has a non-empty zh-CN label."""
    used_metrics = {
        metric
        for achievement in plugin_api.ACHIEVEMENTS
        for metric in (
            [achievement["threshold_metric"]]
            if "threshold_metric" in achievement
            else [requirement["metric"] for requirement in achievement.get("requirements", [])]
        )
    }
    translated_metrics = plugin_api._load_locale("zh-CN")["._metrics"]

    missing_metrics = {
        metric
        for metric in used_metrics
        if not isinstance(translated_metrics.get(metric), str) or not translated_metrics[metric].strip()
    }
    assert missing_metrics == set()


def test_achievements_locale_keeps_secret_cards_hidden(plugin_api):
    """Localized secret achievements still hide trigger/name until discovered."""
    definition = next(a for a in plugin_api.ACHIEVEMENTS if a.get("secret"))
    item = plugin_api.display_achievement({**definition, "state": "secret", "unlocked": False}, "zh-CN")

    assert item["name"] == "???"
    assert item["icon"] == "secret"
    assert "秘密成就" in item["description"]
    assert "秘密成就" in item["criteria"]


def test_achievements_locale_resolution_accepts_query_and_header(plugin_api):
    assert plugin_api._resolve_locale_from_request(_Request({"locale": "zh"})) == "zh-CN"
    assert plugin_api._resolve_locale_from_request(_Request(header="zh-CN,zh;q=0.9,en;q=0.8")) == "zh-CN"
    assert plugin_api._resolve_locale_from_request(_Request({"locale": "en"}, "zh-CN")) == "en"
    assert plugin_api._resolve_locale_from_request(_Request(header="fr,en;q=0.8")) == "en"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("en-US,en;q=0.9,zh-CN;q=0.8", "en"),
        ("zh-CN;q=0,en;q=1", "en"),
        ("en;q=1,zh-CN;q=0", "en"),
    ],
)
def test_accept_language_honors_quality_and_exclusions(plugin_api, header, expected):
    assert plugin_api._resolve_locale_from_request(_Request(header=header)) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("zh-Hans-CN;q=0.7,en-GB;q=0.6", "zh-CN"),
        ("zh-CN;q=0.8,en;q=0.8", "zh-CN"),
        ("en;q=0,zh-SG;q=0.001", "zh-CN"),
        ("*;q=1,zh-CN;q=0.5", "en"),
        ("zh-Hant;q=1,zh-CN;q=0.5", "zh-CN"),
    ],
)
def test_accept_language_handles_supported_ranges_ties_and_wildcards(
    plugin_api, header, expected
):
    assert plugin_api._resolve_locale_from_request(_Request(header=header)) == expected


@pytest.mark.parametrize(
    "malformed_quality",
    ["", ".8", "bogus", "-0.1", "1.001", "0.1234"],
)
def test_accept_language_ignores_malformed_quality_values(plugin_api, malformed_quality):
    header = f"zh-CN;q={malformed_quality},en;q=0.5"

    assert plugin_api._resolve_locale_from_request(_Request(header=header)) == "en"


def test_explicit_locale_param_wins_over_accept_language(plugin_api):
    request = _Request({"locale": "ja"}, "zh-CN")

    assert plugin_api._resolve_locale_from_request(request) == "en"


def test_explicit_locale_wins_over_lang_and_lang_wins_over_header(plugin_api):
    assert (
        plugin_api._resolve_locale_from_request(
            _Request({"locale": "ja", "lang": "zh-CN"}, "zh-CN")
        )
        == "en"
    )
    assert (
        plugin_api._resolve_locale_from_request(_Request({"lang": "en-US"}, "zh-CN"))
        == "en"
    )


def test_zh_hant_does_not_fall_back_to_simplified(plugin_api):
    request = _Request({"locale": "zh-hant"}, "zh-CN")

    assert plugin_api._resolve_locale_from_request(request) == "en"


def test_achievements_route_returns_localized_payload(plugin_api):
    snapshot = _fixed_snapshot(plugin_api)
    _stub_evaluate_all(plugin_api, snapshot)

    chinese = asyncio.run(plugin_api.achievements(_Request({"locale": "zh-CN"})))
    english = asyncio.run(plugin_api.achievements(_Request({"locale": "en"})))

    assert all("criteria" in item for item in chinese["achievements"])
    assert all(
        _contains_cjk(item["name"])
        for item in chinese["achievements"]
        if item["state"] != "secret"
    )
    assert english["achievements"][0]["name"] == "Let Him Cook"
    assert not any(_contains_cjk(item["name"]) for item in english["achievements"])


def test_achievements_route_honors_accept_language_header(plugin_api):
    _stub_evaluate_all(plugin_api, _fixed_snapshot(plugin_api))

    payload = asyncio.run(plugin_api.achievements(_Request(header="zh-CN,zh;q=0.9")))

    assert payload["achievements"][0]["name"] == "放手一搏"
    assert "要求" in payload["achievements"][0]["criteria"]


def test_recent_unlocks_route_localized(plugin_api):
    _stub_evaluate_all(plugin_api, _fixed_snapshot(plugin_api))

    payload = asyncio.run(plugin_api.recent_unlocks(_Request({"locale": "zh-CN"})))

    assert payload[0]["name"] == "放手一搏"
    assert "要求" in payload[0]["criteria"]


def test_session_badges_route_localized(plugin_api):
    _stub_evaluate_all(plugin_api, _fixed_snapshot(plugin_api))

    payload = asyncio.run(
        plugin_api.session_badges("session-1", _Request({"locale": "zh-CN"}))
    )

    assert set(payload) == {"session_id", "badges"}
    badge = next(item for item in payload["badges"] if item["id"] == "let_him_cook")
    assert badge["name"] == "放手一搏"
    assert "要求" in badge["criteria"]


def test_rescan_route_localized(plugin_api):
    snapshot = _fixed_snapshot(plugin_api)
    force_values = []

    def evaluate_all(force=False):
        force_values.append(force)
        return snapshot

    plugin_api._test_monkeypatch.setattr(plugin_api, "evaluate_all", evaluate_all)

    payload = asyncio.run(plugin_api.rescan(_Request({"locale": "zh-CN"})))

    assert payload["ok"] is True
    assert payload["achievements"][0]["name"] == "放手一搏"
    assert force_values == [True]


def test_secret_cards_stay_hidden_through_route(plugin_api):
    _stub_evaluate_all(plugin_api, _fixed_snapshot(plugin_api))
    definition = next(item for item in plugin_api.ACHIEVEMENTS if item.get("secret"))

    payload = asyncio.run(plugin_api.achievements(_Request({"locale": "zh-CN"})))
    secret_card = next(
        item for item in payload["achievements"] if item["id"] == definition["id"]
    )

    assert secret_card["name"] == "???"
    assert secret_card["icon"] == "secret"
    assert "秘密成就" in secret_card["description"]
    assert "秘密成就" in secret_card["criteria"]
    assert all(definition["name"] not in value for value in _string_values(secret_card))
