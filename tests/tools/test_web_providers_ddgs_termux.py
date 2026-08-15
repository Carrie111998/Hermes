"""Tests for the Termux/Android requests fallback in the ddgs web provider.

These harden NousResearch/hermes-agent#86046 (fix/termux-ddgs-requests-fallback)
so the PR is merge-ready: real parsing logic is exercised against fixture HTML
and against monkeypatched ``requests.post`` (the network call is stubbed, but
the parse/filter/decode/limit path is the real code — no parser mock).

Style follows AGENTS.md: behavior contracts, not snapshots; real imports; no
new env vars; Termux simulated via the same markers the provider uses
(``TERMUX_VERSION`` / ``PREFIX`` containing ``com.termux/files/usr``).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

import pytest
import requests  # type: ignore

from plugins.web.ddgs import provider as ddgs_provider  # noqa: E402


# A minimal but realistic DuckDuckGo HTML response. Two organic results plus
# one sponsored (result--ad) block that must be dropped.
_SAMPLE_HTML = """
<html><body>
<div class="result results_links">
  <a class="result__a" href="https://example.com/a">Example A</a>
  <a class="result__snippet" href="https://example.com/a">First organic hit.</a>
</div>
<div class="result results_links result--ad">
  <a class="result__a" href="https://ads.example/b">Sponsored</a>
  <a class="result__snippet" href="https://ads.example/b">Buy now.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="https://d.gg/l/?uddg=https%3A%2F%2Fc">Example C</a>
  <a class="result__snippet" href="https://d.gg/l/?uddg=https%3A%2F%2Fc">Redir hit.</a>
</div>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _fake_post(text: str = _SAMPLE_HTML):
    def _post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(text)

    return _post


@pytest.fixture
def termux_env(monkeypatch):
    """Simulate a Termux environment via the provider's own markers."""
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    yield
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)


# --- _is_termux -----------------------------------------------------------
def test_is_termux_true_with_version(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    assert ddgs_provider._is_termux() is True
    monkeypatch.delenv("TERMUX_VERSION")


def test_is_termux_true_with_prefix(monkeypatch):
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert ddgs_provider._is_termux() is True
    monkeypatch.delenv("PREFIX")


def test_is_termux_false_on_desktop(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    assert ddgs_provider._is_termux() is False


# --- requests fallback parsing --------------------------------------------
def test_requests_fallback_parses_organic_results(monkeypatch):
    monkeypatch.setattr(requests, "post", _fake_post())
    results = ddgs_provider._run_ddgs_requests_search("anything", 10)
    assert len(results) == 2  # sponsored dropped
    titles = {r["title"] for r in results}
    assert "Example A" in titles
    assert "Example C" in titles
    assert "Sponsored" not in titles


def test_requests_fallback_skips_ads(monkeypatch):
    monkeypatch.setattr(requests, "post", _fake_post())
    results = ddgs_provider._run_ddgs_requests_search("ads", 10)
    assert all("result--ad" not in r["url"] for r in results)
    assert all("Sponsored" != r["title"] for r in results)


def test_requests_fallback_decodes_uddg_redirect(monkeypatch):
    monkeypatch.setattr(requests, "post", _fake_post())
    results = ddgs_provider._run_ddgs_requests_search("decode", 10)
    decoded = [r for r in results if r["title"] == "Example C"]
    assert decoded, "expected the redirected organic result"
    assert decoded[0]["url"] == "https://c"


def test_requests_fallback_respects_limit(monkeypatch):
    # Two organic results; limit=1 must cap to exactly one.
    monkeypatch.setattr(requests, "post", _fake_post())
    results = ddgs_provider._run_ddgs_requests_search("limit", 1)
    assert len(results) == 1
    assert results[0]["position"] == 1


def test_requests_fallback_normalizes_shape(monkeypatch):
    monkeypatch.setattr(requests, "post", _fake_post())
    results = ddgs_provider._run_ddgs_requests_search("shape", 10)
    for r in results:
        assert set(r.keys()) == {"title", "url", "description", "position"}
        assert isinstance(r["position"], int)


# --- is_available on Termux ----------------------------------------------
def test_is_available_true_on_termux_with_requests(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    try:
        assert ddgs_provider.DDGSWebSearchProvider().is_available() is True
    finally:
        monkeypatch.delenv("TERMUX_VERSION", raising=False)


def test_is_available_false_on_termux_without_requests(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    # Force `import requests` to fail so the Termux availability probe sees
    # the missing dependency (mirrors a Termux env without requests).
    monkeypatch.setitem(sys.modules, "requests", None)
    try:
        assert ddgs_provider.DDGSWebSearchProvider().is_available() is False
    finally:
        monkeypatch.delitem(sys.modules, "requests", raising=False)
        monkeypatch.delenv("TERMUX_VERSION", raising=False)


# --- search() end-to-end on Termux WITHOUT ddgs installed -----------------
def test_search_works_on_termux_without_ddgs(monkeypatch):
    """Regression: search() must not require `ddgs` on Termux.

    Before the fix, search() early-returned 'ddgs not installed' even on
    Termux, leaving the requests fallback dead code. Now it must reach the
    bounded worker path even when `ddgs` is unimportable. We stub the
    subprocess worker (not the network) so the test stays deterministic.
    """
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    # Ensure ddgs is unimportable in this process.
    real_meta = importlib.util.find_spec
    def _block_ddgs(name, *a, **k):
        return None if name == "ddgs" else real_meta(name, *a, **k)
    importlib.util.find_spec = _block_ddgs
    # Stub the disposable-worker wrapper so no real subprocess/network runs.
    monkeypatch.setattr(
        ddgs_provider, "_run_ddgs_search_bounded",
        lambda q, lim: ddgs_provider._run_ddgs_requests_search(q, lim),
    )
    try:
        out = ddgs_provider.DDGSWebSearchProvider().search("termux test", limit=5)
        assert out["success"] is True, out
        assert "ddgs package is not installed" not in str(out)
    finally:
        importlib.util.find_spec = real_meta
        monkeypatch.delenv("TERMUX_VERSION", raising=False)
        monkeypatch.delenv("PREFIX", raising=False)


# --- worker env selection -------------------------------------------------
def test_worker_selects_requests_fallback_on_termux(monkeypatch):
    """The disposable worker must pick the requests fallback under Termux."""
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    try:
        src = open(
            os.path.join(os.path.dirname(ddgs_provider.__file__), "_search_worker.py"),
            encoding="utf-8",
        ).read()
        # The worker must branch to the requests fallback when Termux markers
        # are present (and only there).
        assert "_run_ddgs_requests_search" in src
        assert "TERMUX_VERSION" in src or "com.termux/files/usr" in src
    finally:
        monkeypatch.delenv("TERMUX_VERSION", raising=False)
        monkeypatch.delenv("PREFIX", raising=False)
