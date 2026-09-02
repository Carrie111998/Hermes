"""Tests for ``list_picker_providers`` — the /model picker filter.

``list_picker_providers`` wraps ``list_authenticated_providers`` and
post-processes the result for interactive pickers (Telegram, Discord):

- OpenRouter's ``models`` are replaced with the live-filtered output of
  ``fetch_openrouter_models``, so IDs the live catalog no longer carries
  drop out.
- Provider rows with an empty ``models`` list are dropped, except custom
  endpoints (``is_user_defined=True`` with an ``api_url``) where the user
  may supply their own model set through config.

These tests exercise the filter in isolation by mocking
``list_authenticated_providers`` and ``fetch_openrouter_models`` so no
network or auth state is required.
"""

import pytest
from hermes_cli import model_switch


@pytest.fixture(autouse=True)
def _disable_live_custom_provider_model_probe(monkeypatch):
    """Keep custom-provider picker fixtures independent of local model servers."""
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *_a, **_kw: None)


def _make_provider(slug, name=None, models=None, *, is_current=False,
                   is_user_defined=False, source="built-in", api_url=None):
    """Build a dict shaped like ``list_authenticated_providers`` output."""
    entry = {
        "slug": slug,
        "name": name or slug.title(),
        "is_current": is_current,
        "is_user_defined": is_user_defined,
        "models": list(models or []),
        "total_models": len(models or []),
        "source": source,
    }
    if api_url is not None:
        entry["api_url"] = api_url
    return entry


















def test_passthrough_kwargs_to_base(monkeypatch):
    """All kwargs must be forwarded to ``list_authenticated_providers`` unchanged.

    The gateway /model picker passes ``current_base_url`` and ``current_model``
    so custom endpoint grouping can mark the current row. Dropping those kwargs
    regressed Telegram/Discord into the text-list fallback.
    """
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(model_switch, "list_authenticated_providers", _capture)
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: [])

    model_switch.list_picker_providers(
        current_provider="openrouter",
        current_base_url="http://x",
        current_model="openai/gpt-5.4",
        user_providers={"foo": {"api": "http://x"}},
        custom_providers=[{"name": "bar", "base_url": "http://y"}],
        max_models=12,
    )

    assert captured["current_provider"] == "openrouter"
    assert captured["current_base_url"] == "http://x"
    assert captured["current_model"] == "openai/gpt-5.4"
    assert captured["user_providers"] == {"foo": {"api": "http://x"}}
    assert captured["custom_providers"] == [{"name": "bar", "base_url": "http://y"}]
    assert captured["max_models"] == 12



def test_current_custom_endpoint_passthrough_marks_current_row(monkeypatch):
    """Interactive picker should preserve current custom endpoint semantics."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("agent.models_dev.PROVIDER_TO_MODELS_DEV", {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: [])

    result = model_switch.list_picker_providers(
        current_provider="custom:ollama",
        current_base_url="http://localhost:11434/v1",
        current_model="glm-5.1",
        user_providers={},
        custom_providers=[
            {
                "name": "Ollama — GLM 5.1",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "glm-5.1",
                "discover_models": False,
            },
            {
                "name": "Ollama — Qwen3",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "qwen3",
                "discover_models": False,
            },
        ],
        max_models=50,
    )

    custom_rows = [p for p in result if p.get("is_user_defined")]
    assert len(custom_rows) == 1
    row = custom_rows[0]
    assert row["slug"] == "custom:ollama"
    assert row["is_current"] is True
    assert row["models"] == ["glm-5.1", "qwen3"]



# ---------------------------------------------------------------------------
# list_authenticated_providers: alias/canonical de-dup for Kimi (#49439)
# ---------------------------------------------------------------------------
#
# A single Kimi credential used to surface TWO picker rows: the alias slug
# "kimi" (emitted by the PROVIDER_TO_MODELS_DEV pass) plus its canonical
# "kimi-coding" (re-emitted by the CANONICAL_PROVIDERS cross-check pass),
# both backed by the same kimi-for-coding models.dev provider. The picker
# must list each authenticated credential exactly once, under the CANONICAL
# slug ("kimi-coding") — matching list_authenticated_providers' other alias
# rows and the overlay slug-resolution contract (see
# test_overlay_slug_resolution.py).


def _stub_kimi_discovery(monkeypatch, *, canonical):
    """Isolate list_authenticated_providers to the Kimi alias family.

    Restricts the models.dev map / catalog / overlays / canonical list to
    just the Kimi entries and stubs the model-id fetch so discovery stays
    offline and deterministic. ``canonical`` is the CANONICAL_PROVIDERS list
    the 2b cross-check pass should iterate.
    """
    import agent.models_dev as md
    import hermes_cli.models as hm

    kimi_map = {
        "kimi": "kimi-for-coding",
        "kimi-coding": "kimi-for-coding",
        "moonshot": "kimi-for-coding",
        "kimi-coding-cn": "kimi-for-coding",
    }
    monkeypatch.setattr(md, "PROVIDER_TO_MODELS_DEV", kimi_map)
    monkeypatch.setattr(
        md, "fetch_models_dev",
        lambda *a, **k: {
            "kimi-for-coding": {"name": "Kimi For Coding", "env": ["KIMI_API_KEY"]},
        },
    )

    class _PInfo:
        name = "Kimi For Coding"

    monkeypatch.setattr(md, "get_provider_info", lambda _pid: _PInfo())
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setattr(hm, "CANONICAL_PROVIDERS", canonical)
    monkeypatch.setattr(hm, "cached_provider_model_ids",
                        lambda *a, **k: ["kimi-k2.6", "kimi-k2.5"])
    monkeypatch.setattr(hm, "clear_provider_models_cache", lambda *a, **k: None)


def test_single_kimi_credential_yields_one_canonical_row(monkeypatch):
    """One Kimi key yields a single row under the canonical 'kimi-coding' slug."""
    import hermes_cli.models as hm

    _stub_kimi_discovery(
        monkeypatch,
        canonical=[hm.ProviderEntry("kimi-coding", "Kimi / Kimi Coding Plan", "desc")],
    )
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi")

    rows = model_switch.list_authenticated_providers(max_models=10)
    slugs = [r["slug"] for r in rows]

    # Exactly one Kimi / kimi-for-coding-backed row, under the canonical slug —
    # not both the alias ("kimi") and its canonical ("kimi-coding").
    kimi_rows = [s for s in slugs if s in {"kimi", "kimi-coding"}]
    assert kimi_rows == ["kimi-coding"], (
        f"expected a single canonical Kimi row, got: {slugs}"
    )
    assert slugs.count("kimi-coding") == 1
    assert "kimi" not in slugs


def test_distinct_kimi_china_credential_still_listed(monkeypatch):
    """A separate China (kimi-coding-cn) credential remains its own row.

    Negative-control guard: the de-dup must collapse only the alias/canonical
    pair that share a credential, not legitimately distinct providers.
    """
    import hermes_cli.models as hm

    _stub_kimi_discovery(
        monkeypatch,
        canonical=[
            hm.ProviderEntry("kimi-coding", "Kimi / Kimi Coding Plan", "desc"),
            hm.ProviderEntry("kimi-coding-cn", "Kimi / Moonshot (China)", "desc"),
        ],
    )
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi")
    monkeypatch.setenv("KIMI_CN_API_KEY", "sk-test-kimi-cn")

    rows = model_switch.list_authenticated_providers(max_models=10)
    slugs = [r["slug"] for r in rows]

    assert "kimi-coding" in slugs       # canonical global row
    assert slugs.count("kimi-coding") == 1
    assert "kimi" not in slugs          # alias collapsed into the canonical row
    assert "kimi-coding-cn" in slugs    # distinct China endpoint preserved


# ── Virtual MoA row vs model_catalog.excluded_providers (#94068) ──


def _patch_moa_prepend(monkeypatch):
    """Isolate the picker from the inventory: fixed virtual MoA row."""
    moa_row = _make_provider("moa", name="Mixture of Agents", models=["balanced", "deep"])

    def _prepend(providers, current_provider=""):
        return [moa_row] + [p for p in providers if str(p.get("slug", "")).lower() != "moa"]

    def _base(**kw):
        excluded = {str(p).strip().lower() for p in (kw.get("excluded_providers") or []) if p}
        rows = [_make_provider("openai", models=["gpt-x"])]
        return [r for r in rows if r["slug"] not in excluded]

    monkeypatch.setattr(model_switch, "_prepend_moa_picker_provider", _prepend)
    monkeypatch.setattr(model_switch, "list_authenticated_providers", _base)


def test_moa_row_hidden_when_excluded(monkeypatch):
    """The gateway picker passes include_moa=True unconditionally; the virtual
    row must still honor model_catalog.excluded_providers (#94068)."""
    _patch_moa_prepend(monkeypatch)

    rows = model_switch.list_picker_providers(
        include_moa=True, excluded_providers=["moa"]
    )

    assert "moa" not in [r["slug"] for r in rows]
    assert "openai" in [r["slug"] for r in rows]


def test_moa_row_hidden_with_case_and_whitespace_variant(monkeypatch):
    """Exclusion matching normalizes like list_authenticated_providers."""
    _patch_moa_prepend(monkeypatch)

    rows = model_switch.list_picker_providers(
        include_moa=True, excluded_providers=["  MOA "]
    )

    assert "moa" not in [r["slug"] for r in rows]


def test_moa_row_present_without_exclusion(monkeypatch):
    """Default: the virtual row is still prepended first for gateway pickers."""
    _patch_moa_prepend(monkeypatch)

    rows = model_switch.list_picker_providers(include_moa=True)

    assert rows[0]["slug"] == "moa"


def test_moa_row_survives_other_provider_exclusion(monkeypatch):
    """Excluding an unrelated provider must not hide the MoA row."""
    _patch_moa_prepend(monkeypatch)

    rows = model_switch.list_picker_providers(
        include_moa=True, excluded_providers=["openai"]
    )

    slugs = [r["slug"] for r in rows]
    assert "moa" in slugs
    assert "openai" not in slugs
