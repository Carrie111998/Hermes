import pytest

@pytest.fixture
def ov(tmp_path, monkeypatch):
    p = tmp_path / "model_overrides.json"
    monkeypatch.setattr("events.model_override._store_path", lambda: p)
    from events import model_override
    model_override.reset_cache()
    return p


def test_no_override_returns_none(ov):
    from events.model_override import get_override
    assert get_override("deepseek", "deepseek-v4-pro") is None


def test_set_then_get_roundtrip(ov):
    from events.model_override import set_override, get_override
    ok, _ = set_override(provider="deepseek", model="deepseek-v4-pro",
                         replacement_provider="openai-codex",
                         replacement_model="gpt-5.6-sol",
                         ttl_seconds=6 * 3600, set_by="telegram:diego")
    assert ok is True
    rec = get_override("deepseek", "deepseek-v4-pro")
    assert rec["replacement_model"] == "gpt-5.6-sol"


def test_expired_override_is_not_returned(ov):
    from events.model_override import set_override, get_override
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=-1, set_by="test")
    assert get_override("deepseek", "deepseek-v4-pro") is None


def test_ttl_is_capped_at_24h(ov):
    from events.model_override import set_override, get_override, MAX_TTL_SECONDS
    from datetime import datetime, timezone
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=99 * 3600, set_by="test")
    rec = get_override("deepseek", "deepseek-v4-pro")
    exp = datetime.fromisoformat(rec["expires_at"])
    remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    assert remaining <= MAX_TTL_SECONDS + 5


def test_self_target_is_rejected(ov):
    """Overriding a model to itself is a routing loop."""
    from events.model_override import set_override
    ok, reason = set_override(provider="deepseek", model="deepseek-v4-pro",
                              replacement_provider="deepseek",
                              replacement_model="deepseek-v4-pro",
                              ttl_seconds=3600, set_by="test")
    assert ok is False
    assert "itself" in reason.lower() or "loop" in reason.lower()


def test_malformed_store_fails_open(ov):
    ov.write_text("{not json", encoding="utf-8")
    from events import model_override
    model_override.reset_cache()
    assert model_override.get_override("deepseek", "deepseek-v4-pro") is None


def test_get_override_never_raises(ov, monkeypatch):
    from events import model_override
    model_override.reset_cache()
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert model_override.get_override("deepseek", "deepseek-v4-pro") is None


def test_clear_and_list(ov):
    from events.model_override import set_override, clear_override, list_overrides
    set_override(provider="deepseek", model="deepseek-v4-pro",
                 replacement_provider="openai-codex",
                 replacement_model="gpt-5.6-sol",
                 ttl_seconds=3600, set_by="test")
    assert len(list_overrides()) == 1
    assert clear_override(provider="deepseek", model="deepseek-v4-pro") is True
    assert list_overrides() == []
    assert clear_override(provider="deepseek", model="deepseek-v4-pro") is False
