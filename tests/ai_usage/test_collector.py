import itertools
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import ai_usage.collector as collector_module
from ai_usage.collector import collect, write_atomic

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


@dataclass
class FakeWin:
    label: str
    used_percent: Optional[float]
    reset_at: Optional[datetime]


@dataclass
class FakeSnap:
    available: bool
    windows: tuple
    fetched_at: datetime = NOW
    unavailable_reason: Optional[str] = None
    balance_usd: Optional[float] = None


def _seed_db(path):
    conn = sqlite3.connect(path)
    # `model` column is required by spend_provider (gemini's mode); tokensum
    # ignores it but the shared table must carry it.
    conn.execute(
        "CREATE TABLE session_model_usage ("
        "billing_provider TEXT, model TEXT, input_tokens INT, output_tokens INT, "
        "estimated_cost_usd REAL, last_seen REAL)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)",
        ("kimi-coding", "kimi-k3", 100, 50, 0.01, NOW.timestamp() - 60),
    )
    conn.commit()
    conn.close()


def test_collect_builds_all_providers(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))

    def fetch(provider):
        if provider == "anthropic":
            return FakeSnap(True, (FakeWin("Current session", 62.0, None),))
        if provider == "deepseek":
            return FakeSnap(True, (), balance_usd=9.74)
        # codex + kimi are both budget-mode; unavailable fetch → unconfigured
        return FakeSnap(False, (), unavailable_reason="no token")

    data = collect(db_path=str(db), prev=None, fetch_usage=fetch, now=NOW)
    assert data["generated_at"] == "2026-08-04T15:00:00Z"
    by = {p["key"]: p for p in data["providers"]}
    assert list(by.keys()) == [
        "anthropic", "openai-codex", "kimi", "deepseek", "gemini", "xai",
        "opencode-go",
    ]
    # opencode-go is tokens-mode (flat subscription, no usage API); no rows seeded
    assert by["opencode-go"]["mode"] == "tokens"
    assert by["opencode-go"]["state"] == "unconfigured"
    assert by["anthropic"]["state"] == "ok"
    assert by["openai-codex"]["state"] == "unconfigured"
    # kimi is budget-mode now: routed through fetch, not the state.db token-sum
    assert by["kimi"]["mode"] == "budget"
    assert by["kimi"]["state"] == "unconfigured"
    # deepseek is balance-mode: outstanding-$ from the fetch snapshot
    assert by["deepseek"]["mode"] == "balance"
    assert by["deepseek"]["state"] == "ok"
    assert by["deepseek"]["balance_usd"] == 9.74
    assert by["deepseek"]["detail"] == "$9.74 left"
    # gemini is spend-mode: month-to-date $ from state.db; no gemini rows here
    assert by["gemini"]["mode"] == "spend"
    assert by["gemini"]["state"] == "unconfigured"


def test_collect_carries_forward_balance_on_fetch_failure(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    prev = {
        "generated_at": "2026-08-04T14:00:00Z",
        "providers": [
            {"key": "deepseek", "label": "DeepSeek", "mode": "balance",
             "state": "ok", "fetched_at": "2026-08-04T14:00:00Z",
             "balance_usd": 8.10, "windows": [], "detail": "$8.10 left"},
        ],
    }

    def fetch(provider):
        return None  # total fetch failure

    data = collect(db_path=str(db), prev=prev, fetch_usage=fetch, now=NOW)
    by = {p["key"]: p for p in data["providers"]}
    assert by["deepseek"]["state"] == "stale"
    assert by["deepseek"]["balance_usd"] == 8.10  # last-known preserved


def test_collect_carries_forward_last_known_on_fetch_failure(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    prev = {
        "generated_at": "2026-08-04T14:00:00Z",
        "providers": [
            {"key": "anthropic", "label": "Claude", "mode": "budget",
             "state": "ok", "fetched_at": "2026-08-04T14:00:00Z",
             "windows": [{"id": "5h", "label": "5h", "used_pct": 55.0}],
             "detail": "5h 55%"},
        ],
    }

    def fetch(provider):
        return None  # total fetch failure

    data = collect(db_path=str(db), prev=prev, fetch_usage=fetch, now=NOW)
    by = {p["key"]: p for p in data["providers"]}
    assert by["anthropic"]["state"] == "stale"
    assert by["anthropic"]["windows"][0]["used_pct"] == 55.0  # last-known preserved


def test_unconfigured_prev_does_not_carry_forward_as_stale(tmp_path):
    # Prior run had this budget provider as unconfigured; the live fetch is
    # still unavailable this run. It must stay "unconfigured", NOT flip to
    # "stale" (which would imply stale DATA that never existed).
    db = tmp_path / "state.db"
    _seed_db(str(db))
    prev = {
        "generated_at": "2026-08-04T14:00:00Z",
        "providers": [
            {"key": "openai-codex", "label": "Codex", "mode": "budget",
             "state": "unconfigured", "windows": [], "detail": "no data"},
        ],
    }

    def fetch(provider):
        # still unconfigured this run (unavailable with a reason)
        return FakeSnap(False, (), unavailable_reason="no token")

    data = collect(db_path=str(db), prev=prev, fetch_usage=fetch, now=NOW)
    by = {p["key"]: p for p in data["providers"]}
    assert by["openai-codex"]["state"] == "unconfigured"
    assert by["openai-codex"]["state"] != "stale"


def test_write_atomic_roundtrip(tmp_path):
    path = tmp_path / "sub" / "ai-tokens.json"
    write_atomic(path, {"generated_at": "x", "providers": []})
    assert json.loads(path.read_text(encoding="utf-8"))["generated_at"] == "x"


def test_manual_snapshots_override_all_hermes_derived_providers(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    manual_path = tmp_path / "ai-usage-manual.json"
    manual_path.write_text(json.dumps({
        "schema_version": 1,
        "providers": {
            "gemini": {"used_pct": 65.0, "saved_at": "2026-08-08T19:30:00Z"},
            "xai": {"used_pct": 35.0, "saved_at": "2026-08-08T19:30:00Z"},
            "opencode-go": {"used_pct": 10.0, "saved_at": "2026-08-08T19:30:00Z"},
        },
    }), encoding="utf-8")

    data = collect(
        db_path=str(db),
        prev=None,
        fetch_usage=lambda _: FakeSnap(False, (), unavailable_reason="no token"),
        now=datetime(2026, 8, 8, 19, 35, tzinfo=timezone.utc),
        manual_store_path=str(manual_path),
    )

    by = {p["key"]: p for p in data["providers"]}
    for key, expected_pct in (("gemini", 65.0), ("xai", 35.0), ("opencode-go", 10.0)):
        row = by[key]
        assert row["mode"] == "budget"
        assert row["source"] == "manual"
        assert row["state"] == "ok"
        assert row["windows"][0]["used_pct"] == expected_pct


def test_no_manual_record_preserves_hermes_fallback(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    manual_path = tmp_path / "ai-usage-manual.json"
    manual_path.write_text(json.dumps({
        "schema_version": 1,
        "providers": {
            "xai": {"used_pct": 10.0, "saved_at": "2026-08-08T19:30:00Z"},
        },
    }), encoding="utf-8")

    data = collect(
        db_path=str(db),
        prev=None,
        fetch_usage=lambda _: FakeSnap(False, (), unavailable_reason="no token"),
        now=datetime(2026, 8, 8, 19, 35, tzinfo=timezone.utc),
        manual_store_path=str(manual_path),
    )

    by = {p["key"]: p for p in data["providers"]}
    assert by["gemini"]["mode"] == "spend"
    assert by["gemini"]["source"] == "hermes"
    assert by["xai"]["mode"] == "budget"
    assert by["xai"]["source"] == "manual"


def test_all_rows_receive_a_source_field(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))

    def fetch(provider):
        if provider == "anthropic":
            return FakeSnap(True, (FakeWin("Current session", 62.0, None),))
        if provider == "deepseek":
            return FakeSnap(True, (), balance_usd=5.00)
        return FakeSnap(False, (), unavailable_reason="no token")

    data = collect(db_path=str(db), prev=None, fetch_usage=fetch, now=NOW)
    by = {p["key"]: p for p in data["providers"]}

    assert all("source" in row for row in data["providers"])
    assert by["anthropic"]["source"] == "official"
    assert by["deepseek"]["source"] == "official"
    assert by["gemini"]["source"] == "hermes"
    assert by["xai"]["source"] == "hermes"
    assert by["opencode-go"]["source"] == "hermes"


def test_stale_manual_snapshot_still_wins_over_hermes(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    manual_path = tmp_path / "ai-usage-manual.json"
    manual_path.write_text(json.dumps({
        "schema_version": 1,
        "providers": {
            "gemini": {"used_pct": 99.0, "saved_at": "2026-08-07T19:00:00Z"},
        },
    }), encoding="utf-8")

    data = collect(
        db_path=str(db),
        prev=None,
        fetch_usage=lambda _: FakeSnap(False, (), unavailable_reason="no token"),
        now=datetime(2026, 8, 9, 19, 1, tzinfo=timezone.utc),
        manual_store_path=str(manual_path),
    )

    gemini = {p["key"]: p for p in data["providers"]}["gemini"]
    assert gemini["source"] == "manual"
    assert gemini["state"] == "stale"
    assert gemini["mode"] == "budget"


def test_manual_snapshot_survives_missing_state_db(tmp_path):
    manual_path = tmp_path / "ai-usage-manual.json"
    manual_path.write_text(json.dumps({
        "schema_version": 1,
        "providers": {
            "gemini": {"used_pct": 42.0, "saved_at": "2026-08-08T19:30:00Z"},
        },
    }), encoding="utf-8")

    data = collect(
        db_path=str(tmp_path / "missing-state.db"),
        prev=None,
        fetch_usage=lambda _: FakeSnap(False, (), unavailable_reason="no token"),
        now=datetime(2026, 8, 8, 19, 35, tzinfo=timezone.utc),
        manual_store_path=str(manual_path),
    )

    by = {p["key"]: p for p in data["providers"]}
    assert by["gemini"]["source"] == "manual"
    assert by["xai"]["source"] == "hermes"
    assert by["xai"]["state"] == "error"


def test_carried_forward_row_preserves_existing_source(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    prev = {
        "providers": [{
            "key": "anthropic",
            "label": "Claude",
            "mode": "budget",
            "source": "manual",
            "state": "ok",
            "windows": [{"id": "5h", "label": "5h", "used_pct": 55.0}],
            "detail": "last known",
        }],
    }

    data = collect(
        db_path=str(db),
        prev=prev,
        fetch_usage=lambda _: None,
        now=NOW,
    )

    anthropic = {p["key"]: p for p in data["providers"]}["anthropic"]
    assert anthropic["state"] == "stale"
    assert anthropic["source"] == "manual"


def test_collect_propagates_remaining_budget_and_reports_sanitized_attempts(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    calls = []
    ticks = itertools.chain((10.0, 10.0, 10.2, 10.2, 10.5, 10.5, 10.7), itertools.repeat(10.8))

    def fetch(provider, *, budget_seconds):
        calls.append((provider, budget_seconds))
        if provider == "anthropic":
            return FakeSnap(True, (FakeWin("Current session", 62.0, None),))
        if provider == "openai-codex":
            return FakeSnap(False, (), unavailable_reason="secret URL https://x?token=abc")
        if provider == "kimi":
            raise RuntimeError("Bearer secret-token at https://secret.invalid")
        return FakeSnap(True, (), balance_usd=9.74)

    data = collect(
        db_path=str(db), prev=None, fetch_usage=fetch, now=NOW,
        deadline_seconds=5.0, _monotonic=lambda: next(ticks),
    )

    assert [provider for provider, _ in calls] == [
        "anthropic", "openai-codex", "kimi", "deepseek",
    ]
    assert calls[0][1] == 5.0
    assert calls[1][1] < calls[0][1]
    diagnostics = data["diagnostics"]
    assert diagnostics["deadline_seconds"] == 5.0
    assert diagnostics["elapsed_ms"] >= 0
    assert [item["outcome"] for item in diagnostics["providers"][:4]] == [
        "ok", "unavailable", "exception", "ok",
    ]
    assert all(set(item) == {"key", "outcome", "elapsed_ms", "budget_seconds"}
               for item in diagnostics["providers"])
    serialized = json.dumps(diagnostics)
    assert "secret-token" not in serialized
    assert "https://" not in serialized
    assert "Bearer" not in serialized


def test_collect_stops_starting_providers_after_deadline_and_carries_stale(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    calls = []
    ticks = itertools.chain((100.0, 100.0), itertools.repeat(102.0))
    prev = {
        "providers": [{
            "key": "openai-codex", "label": "Codex", "mode": "budget",
            "state": "ok", "windows": [{"used_pct": 50.0}], "detail": "50%",
        }],
    }

    def fetch(provider, *, budget_seconds):
        calls.append(provider)
        return FakeSnap(True, (FakeWin("Current session", 62.0, None),))

    data = collect(
        db_path=str(db), prev=prev, fetch_usage=fetch, now=NOW,
        deadline_seconds=1.0, _monotonic=lambda: next(ticks),
    )

    assert calls == ["anthropic"]
    by = {row["key"]: row for row in data["providers"]}
    assert by["openai-codex"]["state"] == "stale"
    outcomes = {item["key"]: item["outcome"] for item in data["diagnostics"]["providers"]}
    assert outcomes["anthropic"] == "deadline_exhausted"
    assert outcomes["openai-codex"] == "deadline_exhausted"
    assert outcomes["deepseek"] == "deadline_exhausted"
    assert outcomes["gemini"] == "deadline_exhausted"
    assert outcomes["xai"] == "deadline_exhausted"
    assert outcomes["opencode-go"] == "deadline_exhausted"


def test_manual_snapshot_read_consumes_collection_deadline(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    calls = []
    ticks = itertools.chain((10.0, 12.0), itertools.repeat(12.0))

    def slow_manual(path, now):
        del path, now
        return {}

    monkeypatch.setattr(collector_module, "read_manual_snapshot", slow_manual)

    data = collect(
        db_path=str(db), prev=None,
        fetch_usage=lambda provider, **kwargs: calls.append((provider, kwargs)),
        now=NOW, manual_store_path=str(tmp_path / "manual.json"),
        deadline_seconds=1.0, _monotonic=lambda: next(ticks),
    )

    assert calls == []
    assert all(
        item["outcome"] == "deadline_exhausted"
        for item in data["diagnostics"]["providers"]
    )


def test_state_db_diagnostics_report_error_and_stale_outcomes(tmp_path):
    prev = {
        "providers": [{
            "key": "gemini", "label": "Gemini", "mode": "spend",
            "state": "ok", "windows": [], "detail": "last known",
        }],
    }

    data = collect(
        db_path=str(tmp_path / "missing-state.db"), prev=prev,
        fetch_usage=lambda _: None, now=NOW,
    )

    outcomes = {item["key"]: item["outcome"] for item in data["diagnostics"]["providers"]}
    assert outcomes["gemini"] == "stale"
    assert outcomes["xai"] == "exception"
    assert outcomes["opencode-go"] == "exception"
    assert "missing-state.db" not in json.dumps(data["diagnostics"])


def test_pre_provenance_carry_forward_infers_provider_source(tmp_path):
    prev = {
        "providers": [
            {"key": "anthropic", "label": "Claude", "mode": "budget", "state": "ok", "windows": []},
            {"key": "openai-codex", "label": "Codex", "mode": "budget", "state": "ok", "windows": []},
            {"key": "gemini", "label": "Gemini", "mode": "spend", "state": "ok", "windows": []},
        ],
    }

    data = collect(
        db_path=str(tmp_path / "missing-state.db"), prev=prev,
        fetch_usage=lambda _: None, now=NOW,
    )

    by = {row["key"]: row for row in data["providers"]}
    assert by["anthropic"]["source"] == "official"
    assert by["openai-codex"]["source"] == "official"
    assert by["gemini"]["source"] == "hermes"


def test_collect_keeps_one_argument_fetcher_compatibility(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    calls = []

    def legacy_fetch(provider):
        calls.append(provider)
        return FakeSnap(False, (), unavailable_reason="no token")

    data = collect(db_path=str(db), prev=None, fetch_usage=legacy_fetch, now=NOW)

    assert calls == ["anthropic", "openai-codex", "kimi", "deepseek"]
    assert len(data["providers"]) == 7


def test_carried_forward_pre_provenance_row_gets_hermes_source(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    prev = {
        "providers": [{
            "key": "gemini",
            "label": "Gemini",
            "mode": "spend",
            "state": "ok",
            "windows": [],
            "detail": "last known",
        }],
    }

    data = collect(
        db_path=str(tmp_path / "missing-state.db"),
        prev=prev,
        fetch_usage=lambda _: None,
        now=NOW,
    )

    gemini = {p["key"]: p for p in data["providers"]}["gemini"]
    assert gemini["state"] == "stale"
    assert gemini["source"] == "hermes"


def test_production_fetcher_accepts_the_cooperative_budget():
    """collect()'s deadline is only real if the REAL fetcher takes the budget.

    Every other budget test in this file drives a fake declared as
    ``def fetch(provider, *, budget_seconds)``. That makes them blind to the
    thing most likely to break: production passing a callable that does NOT
    accept ``budget_seconds``. When that happens ``_supports_budget`` returns
    False, collect() silently falls back to ``fetch_usage(key)``, and the
    documented 90s bound degrades to a check performed only BETWEEN providers
    -- while this file stays green. It shipped that way until 2026-08-20.

    So pin the real callable, not a stand-in.
    """
    from agent.account_usage import fetch_account_usage

    assert collector_module._supports_budget(fetch_account_usage), (
        "agent.account_usage.fetch_account_usage must accept budget_seconds, "
        "otherwise collect()'s deadline_seconds never bounds a provider call"
    )
