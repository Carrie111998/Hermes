"""Tests for the Bing-via-agent-browser web search/extract provider.

Covers:
- ``BingBrowserWebSearchProvider`` basics: name ("bing-browser"), ABC
  membership, search+extract capabilities, setup schema with no env vars
- ``is_available()`` gating: explicit web config naming ``bing-browser``
  AND the agent-browser CLI being resolvable — no network, no subprocess
- Accessibility-snapshot result parsing: ``link "Title" [ref=@e1]
  [url=https://...]`` lines, Bing organic-result ``listitem`` blocks
  (URL StaticText captions with breadcrumb/ellipsis markers, level=2
  heading title, paragraph description), bare-URL fallback for
  navigate-embedded snapshots that omit annotations, bing.com-internal
  filtering, scheme filtering, dedupe, cap at limit (hard cap 20)
- ``search()``: Bing URL building, navigation, full-snapshot fallback
  (including when the navigate snapshot is nonempty but header-only),
  error handling, cleanup-in-finally, actionable missing-browser error,
  unique per-call task ids
- ``extract()``: order preservation, per-URL error entries that don't
  abort the batch, max-5 URL cap, per-task cleanup, metadata, empty input
- Registry wiring: explicit-backend resolution, ``web_tools._get_backend``
  / ``_is_backend_available``, and the plugin ``register(ctx)`` entry point

Browser functions are monkeypatched at the provider module level — no live
browser, no network.
"""
from __future__ import annotations

import json
import urllib.parse
from unittest.mock import MagicMock

import pytest

from agent.web_search_provider import WebSearchProvider

# ---------------------------------------------------------------------------
# Sample agent-browser accessibility snapshots
# ---------------------------------------------------------------------------

# Link lines carry [url=...] annotations; includes bing.com internal links
# and non-http schemes that must be filtered out.
_SNAPSHOT_WITH_URLS = """\
- heading "Search results" [level=2]
- link "OpenAI" [ref=@e1] [url=https://www.bing.com/ck/a?x=1]
- staticText "OpenAI is an AI research and deployment company."
- link "https://openai.com" [url=https://www.bing.com/ck/a?x=2]
- link "Example Domain" [ref=@e3] [url=https://example.com]
- link "Docs" [url=https://docs.example.com]
- link "NoAnnotation" [ref=@e5]
- link "Mail" [url=mailto:test@example.com]
- link "JS" [url=javascript:void(0)]
- link "Data" [url=data:text/html,hi]
- link "About" [url=about:blank]
"""

# Link lines with NO url annotations — real Bing result captions commonly
# omit them while the visible caption URL appears as plain text nearby.
_SNAPSHOT_BARE_URLS = """\
- heading "OpenAI" [level=2]
- link "OpenAI" [ref=@e1]
- staticText "OpenAI is an AI research and deployment company."
- staticText "https://openai.com"
- link "Example" [ref=@e2]
- staticText "https://example.com"
- staticText "See also https://www.bing.com/search?q=openai"
"""

# Full-snapshot style: link lines WITH annotations (browser_snapshot path
# must NOT fall back to bare-URL scanning).
_SNAPSHOT_FULL = """\
- link "Example Domain" [ref=@e1] [url=https://example.com]
- staticText "A plain https://bare.example.com URL in body text."
"""

# Real Bing organic results: one ``listitem`` accessibility block per
# result, shaped like the live SERP (attr2-snapshot.txt). The URL is a
# child StaticText of the result link and carries breadcrumb (``›``) /
# ellipsis (``…`` / ``...``) markers; the title is the block's level=2
# heading; the description is the paragraph's StaticText. Blocks missing
# a URL caption (hydrotank.jp), a title (innovatopia.jp), or both (the
# "一部の検索結果が削除されました" removed-results filler) must be skipped.
# The banner noise URL must never surface: listitem parsing wins and the
# full-snapshot path never bare-scans.
_SNAPSHOT_LISTITEMS = """\
- generic
  - banner
    - button "コンテンツに移動" [ref=e1]
      - StaticText "コンテンツに移動"
    - StaticText "https://noise.example.com"
  - main "検索結果" [ref=e4]
    - StaticText "約 94,300 件の結果"
    - list
      - listitem [level=1]
        - link "cloudflare.app" [ref=e28]
          - StaticText "cloudflare.app"
          - StaticText "https://kitesurf.cloudflare.app"
        - heading "Kitesurf - stateless browser running entirely on Workers" [level=2, ref=e17]
          - link "Kitesurf - stateless browser running entirely on Workers" [ref=e29]
        - paragraph
          - StaticText "5 日前· Kitesurf the browser for the Agentic Cloud Kitesurf is Cloudflare's new stateless browser."
      - listitem [level=1]
        - link "cloudflare.com" [ref=e40]
          - StaticText "cloudflare.com"
          - StaticText "https://blog.cloudflare.com › kitesurf"
        - heading "Introducing Kitesurf: The agent-first browser that runs in V8 ..." [level=2, ref=e23]
          - link "Introducing Kitesurf: The agent-first browser that runs in V8 ..." [ref=e41]
            - StaticText ": The agent-first browser that runs in V8 ..."
        - paragraph
      - listitem [level=1]
        - link "note.com" [ref=e30]
          - StaticText "note.com"
          - StaticText "https://note.com › masa_cloud"
        - heading "Kitesurf 解説：エージェンティッククラウドのためのブラウザ ..." [level=2, ref=e18]
          - link "Kitesurf 解説：エージェンティッククラウドのためのブラウザ ..." [ref=e31]
        - paragraph
          - StaticText "2 日前· Cloudflare が発表した Kitesurf は、 エージェンティッククラウド（Agentic Cloud） 向けに設計された、まったく新しいス …"
      - listitem [level=1]
        - link "ai-revolution.co.jp" [ref=e36]
          - StaticText "ai-revolution.co.jp"
          - StaticText "https://ai-revolution.co.jp › ... › what-is-cloudflare-kitesurf"
        - heading "Cloudflare Kitesurfとは？AIエージェント専用ブラウザの仕組み ..." [level=2, ref=e21]
          - link "Cloudflare Kitesurfとは？AIエージェント専用ブラウザの仕組み ..." [ref=e37]
        - paragraph
          - StaticText "4 日前· Cloudflare Kitesurfは2026年8月6日発表のAIエージェント向けヘッドレスブラウザ。Chromium非依存の仕組み、CPU3〜4 …"
      - listitem [level=1]
        - link "hydrotank.jp" [ref=e42]
          - heading "CloudflareがAIエージェント専用ブラウザ「Kitesurf」を発表！特徴 ..." [level=2, ref=e24]
          - link "CloudflareがAIエージェント専用ブラウザ「Kitesurf」を発表！特徴 ..." [ref=e43]
      - listitem [level=1]
        - link "innovatopia.jp" [ref=e44]
      - listitem [level=1]
        - link "一部の検索結果が削除されました" [ref=e46]
"""

# Real navigate-embedded snapshot: NONEMPTY but header-only (banner +
# filter-navigation listitems, no organic results). This is the shape
# (raw-nav.json / direct-snapshot.txt) that previously returned
# {"web": []} without ever taking browser_snapshot(full=True).
_SNAPSHOT_HEADER_ONLY = """\
- generic
  - banner
    - button "コンテンツに移動" [ref=e1]
      - StaticText "コンテンツに移動"
    - generic
      - link "Bing 検索に戻る" [ref=e4]
        - heading "Bing 検索に戻る" [level=1, ref=e7]
      - search
        - searchbox "ここに検索を入力 - 入力するごとに検索候補が表示されます" [ref=e8]: test query
        - button "検索" [ref=e17]
    - navigation "検索フィルター" [ref=e3]
      - list
        - listitem [level=1]
          - link "すべて" [ref=e9]
        - listitem [level=1]
          - link "検索" [ref=e10]
"""


def _nav_json(snapshot: str = "", title: str = "Bing", success: bool = True) -> str:
    return json.dumps(
        {"success": success, "url": "https://www.bing.com/search?q=test", "title": title,
         "snapshot": snapshot} if success
        else {"success": False, "error": "Navigation failed"}
    )


def _snap_json(snapshot: str = "", success: bool = True) -> str:
    return json.dumps(
        {"success": True, "snapshot": snapshot} if success
        else {"success": False, "error": "Snapshot failed"}
    )


def _make_provider():
    from plugins.web.bing_browser.provider import BingBrowserWebSearchProvider

    return BingBrowserWebSearchProvider()


# ---------------------------------------------------------------------------
# Provider basics
# ---------------------------------------------------------------------------


class TestProviderBasics:
    def test_name_is_bing_browser(self):
        assert _make_provider().name == "bing-browser"

    def test_implements_web_search_provider(self):
        from plugins.web.bing_browser.provider import BingBrowserWebSearchProvider

        assert issubclass(BingBrowserWebSearchProvider, WebSearchProvider)

    def test_supports_search_and_extract(self):
        p = _make_provider()
        assert p.supports_search() is True
        assert p.supports_extract() is True

    def test_setup_schema_has_no_env_vars(self):
        schema = _make_provider().get_setup_schema()
        assert isinstance(schema, dict)
        assert schema["name"]
        assert schema["env_vars"] == []


# ---------------------------------------------------------------------------
# is_available(): explicit config AND agent-browser present, no I/O
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_available_when_configured_and_browser_present(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        monkeypatch.setattr(pmod, "_is_explicitly_configured", lambda: True)
        monkeypatch.setattr(pmod, "_agent_browser_present", lambda: True)
        assert _make_provider().is_available() is True

    def test_not_available_without_explicit_config(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        monkeypatch.setattr(pmod, "_is_explicitly_configured", lambda: False)
        monkeypatch.setattr(pmod, "_agent_browser_present", lambda: True)
        assert _make_provider().is_available() is False

    def test_not_available_without_agent_browser(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        monkeypatch.setattr(pmod, "_is_explicitly_configured", lambda: True)
        monkeypatch.setattr(pmod, "_agent_browser_present", lambda: False)
        assert _make_provider().is_available() is False

    def test_is_explicitly_configured_reads_web_config(self, monkeypatch):
        """Any of web.search_backend / web.extract_backend / web.backend
        naming ``bing-browser`` counts as explicit opt-in."""
        from plugins.web.bing_browser import provider as pmod

        def fake_load_config():
            return {"web": {"backend": "bing-browser"}}

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)
        assert pmod._is_explicitly_configured() is True

        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"web": {"search_backend": "BING-BROWSER"}}
        )
        assert pmod._is_explicitly_configured() is True

        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"web": {"extract_backend": "firecrawl"}}
        )
        assert pmod._is_explicitly_configured() is False

        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"web": {}})
        assert pmod._is_explicitly_configured() is False

    def test_agent_browser_present_probes_without_validation(self, monkeypatch):
        """Uses the cheap existence probe (validate=False) — no subprocess."""
        from plugins.web.bing_browser import provider as pmod

        probe = MagicMock(return_value="C:\\agent-browser.exe")
        monkeypatch.setattr(pmod, "_find_agent_browser", probe)
        assert pmod._agent_browser_present() is True
        probe.assert_called_once_with(validate=False)

    def test_agent_browser_present_false_when_probe_raises(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        def raise_not_found(*args, **kwargs):
            raise FileNotFoundError("agent-browser CLI not found")

        monkeypatch.setattr(pmod, "_find_agent_browser", raise_not_found)
        assert pmod._agent_browser_present() is False


# ---------------------------------------------------------------------------
# Snapshot result parsing
# ---------------------------------------------------------------------------


class TestSnapshotParsing:
    def _parse(self, snapshot, limit=10, allow_bare_urls=False):
        from plugins.web.bing_browser.provider import _parse_snapshot_results

        return _parse_snapshot_results(snapshot, limit=limit, allow_bare_urls=allow_bare_urls)

    def test_link_lines_with_annotations_parsed(self):
        results = self._parse(_SNAPSHOT_WITH_URLS, allow_bare_urls=True)
        urls = [r["url"] for r in results]
        assert urls == ["https://example.com", "https://docs.example.com"]
        assert results[0]["title"] == "Example Domain"
        assert results[0]["position"] == 1
        assert results[1]["position"] == 2
        # Legacy shape fields all present.
        for r in results:
            assert set(r) == {"title", "url", "description", "position"}

    def test_bing_internal_links_filtered(self):
        snapshot = (
            '- link "A" [url=https://www.bing.com/ck/a?u=1]\n'
            '- link "B" [url=https://bing.com/search?q=x]\n'
            '- link "C" [url=https://sub.bing.com/x]\n'
            '- link "D" [url=https://example.com]\n'
        )
        results = self._parse(snapshot)
        assert [r["url"] for r in results] == ["https://example.com"]

    def test_non_http_schemes_filtered(self):
        snapshot = (
            '- link "M" [url=mailto:a@b.com]\n'
            '- link "J" [url=javascript:void(0)]\n'
            '- link "D" [url=data:text/html,hi]\n'
            '- link "A" [url=about:blank]\n'
            '- link "OK" [url=https://example.com]\n'
        )
        results = self._parse(snapshot)
        assert [r["url"] for r in results] == ["https://example.com"]

    def test_bare_url_fallback_when_annotations_omitted(self):
        results = self._parse(_SNAPSHOT_BARE_URLS, allow_bare_urls=True)
        urls = [r["url"] for r in results]
        # https://openai.com + https://example.com kept; bing.com internal dropped.
        assert urls == ["https://openai.com", "https://example.com"]
        # Bare-URL entries use the URL as the title.
        assert results[0]["title"] == "https://openai.com"

    def test_no_bare_url_fallback_without_flag(self):
        results = self._parse(_SNAPSHOT_BARE_URLS, allow_bare_urls=False)
        assert results == []

    def test_full_snapshot_path_never_scans_bare_urls(self):
        """browser_snapshot(full=True) output only uses link annotations."""
        results = self._parse(_SNAPSHOT_FULL, allow_bare_urls=False)
        assert [r["url"] for r in results] == ["https://example.com"]

    def test_dedupe_preserves_first_occurrence(self):
        snapshot = (
            '- link "A" [url=https://example.com]\n'
            '- link "B" [url=https://example.com]\n'
            '- link "C" [url=https://other.example.com]\n'
        )
        results = self._parse(snapshot)
        assert [r["url"] for r in results] == [
            "https://example.com",
            "https://other.example.com",
        ]
        assert results[0]["title"] == "A"

    def test_limit_is_respected(self):
        snapshot = "\n".join(
            f'- link "R{i}" [url=https://r{i}.example.com]' for i in range(20)
        )
        results = self._parse(snapshot, limit=5)
        assert len(results) == 5

    def test_hard_cap_at_20(self):
        """Approved contract: results cap at 20 (min(limit, 20)), mirroring
        the brave-free provider's API cap — even if the tool layer allows
        limits up to 100."""
        from plugins.web.bing_browser.provider import _MAX_RESULTS

        snapshot = "\n".join(
            f'- link "R{i}" [url=https://r{i}.example.com]' for i in range(50)
        )
        results = self._parse(snapshot, limit=50)
        assert len(results) == _MAX_RESULTS == 20

    def test_empty_snapshot(self):
        assert self._parse("", allow_bare_urls=True) == []


class TestListitemParsing:
    """Parsing of real Bing organic-result ``listitem`` accessibility
    blocks (URL StaticText captions, level=2 heading title, paragraph
    description)."""

    def _parse(self, snapshot, limit=10, allow_bare_urls=False):
        from plugins.web.bing_browser.provider import _parse_snapshot_results

        return _parse_snapshot_results(snapshot, limit=limit, allow_bare_urls=allow_bare_urls)

    def test_listitem_blocks_parsed_with_url_title_description(self):
        results = self._parse(_SNAPSHOT_LISTITEMS)
        assert [r["url"] for r in results] == [
            "https://kitesurf.cloudflare.app",
            "https://blog.cloudflare.com",
            "https://note.com",
            "https://ai-revolution.co.jp",
        ]
        assert results[0]["title"] == (
            "Kitesurf - stateless browser running entirely on Workers"
        )
        assert results[0]["description"] == (
            "5 日前· Kitesurf the browser for the Agentic Cloud Kitesurf is Cloudflare's new stateless browser."
        )
        assert results[0]["position"] == 1
        assert results[1]["title"].startswith("Introducing Kitesurf")
        # Empty paragraph → empty description, not a missing field.
        assert results[1]["description"] == ""
        assert results[2]["description"].startswith("2 日前·")
        assert results[3]["description"].startswith("4 日前·")
        for r in results:
            assert set(r) == {"title", "url", "description", "position"}

    def test_listitem_url_truncated_at_breadcrumb(self):
        results = self._parse(_SNAPSHOT_LISTITEMS)
        urls = [r["url"] for r in results]
        assert "https://blog.cloudflare.com" in urls
        assert "https://note.com" in urls
        assert not any("›" in u for u in urls)

    def test_listitem_url_truncated_at_ellipsis_in_breadcrumb(self):
        results = self._parse(_SNAPSHOT_LISTITEMS)
        urls = [r["url"] for r in results]
        assert "https://ai-revolution.co.jp" in urls
        assert not any("..." in u for u in urls)

    def test_listitem_missing_url_caption_skipped(self):
        results = self._parse(_SNAPSHOT_LISTITEMS)
        # hydrotank.jp has a level=2 heading but no URL caption.
        assert len(results) == 4
        assert all(r["title"] != "hydrotank.jp" for r in results)

    def test_listitem_missing_title_skipped(self):
        results = self._parse(_SNAPSHOT_LISTITEMS)
        # innovatopia.jp (link only) and the "一部の検索結果が削除されました"
        # removed-results filler have no level=2 heading → skipped.
        assert len(results) == 4
        assert all(r["title"] != "innovatopia.jp" for r in results)

    def test_listitem_nested_links_do_not_duplicate_results(self):
        results = self._parse(_SNAPSHOT_LISTITEMS)
        # Nested links inside headings/paragraphs (e.g. the heading link
        # and its child StaticText) must not create extra results.
        urls = [r["url"] for r in results]
        assert len(urls) == len(set(urls))
        assert len(results) == 4

    def test_listitem_parsing_wins_over_bare_url_fallback(self):
        # Even with bare URLs allowed (navigate-embedded path), the noise
        # URL outside any listitem must not surface once listitems parse.
        results = self._parse(_SNAPSHOT_LISTITEMS, allow_bare_urls=True)
        urls = [r["url"] for r in results]
        assert "https://noise.example.com" not in urls
        assert urls == [
            "https://kitesurf.cloudflare.app",
            "https://blog.cloudflare.com",
            "https://note.com",
            "https://ai-revolution.co.jp",
        ]

    def test_listitem_results_respect_limit_and_cap(self):
        from plugins.web.bing_browser.provider import _MAX_RESULTS

        limited = self._parse(_SNAPSHOT_LISTITEMS, limit=2)
        assert len(limited) == 2
        assert limited[0]["url"] == "https://kitesurf.cloudflare.app"
        assert _MAX_RESULTS == 20

    def test_listitem_same_url_different_titles_both_retained(self):
        """Regression: a Bing SERP repeats a host across distinct organic
        results (real artifacts show same-host/multiple-title rows, e.g.
        festo.com.cn). Listitem candidates must dedupe on (url, title) —
        NOT on url alone — so same-URL/different-title results are both
        retained, while an exact duplicate (url + title) is dropped.
        (Annotated-link candidates keep their URL-only dedupe; that side
        is pinned by TestSnapshotParsing.test_dedupe_preserves_first_occurrence.)
        """
        snapshot = (
            "- listitem [level=1]\n"
            "  - link \"same.example\" [ref=e1]\n"
            "    - StaticText \"same.example\"\n"
            "    - StaticText \"https://same.example\"\n"
            "  - heading \"First result\" [level=2, ref=e2]\n"
            "  - paragraph\n"
            "    - StaticText \"First description.\"\n"
            "- listitem [level=1]\n"
            "  - link \"same.example\" [ref=e3]\n"
            "    - StaticText \"same.example\"\n"
            "    - StaticText \"https://same.example\"\n"
            "  - heading \"Second result\" [level=2, ref=e4]\n"
            "  - paragraph\n"
            "    - StaticText \"Second description.\"\n"
            "- listitem [level=1]\n"
            "  - link \"same.example\" [ref=e5]\n"
            "    - StaticText \"same.example\"\n"
            "    - StaticText \"https://same.example\"\n"
            "  - heading \"First result\" [level=2, ref=e6]\n"
            "  - paragraph\n"
            "    - StaticText \"Exact duplicate of the first.\"\n"
            "- listitem [level=1]\n"
            "  - link \"other.example\" [ref=e7]\n"
            "    - StaticText \"other.example\"\n"
            "    - StaticText \"https://other.example\"\n"
            "  - heading \"Other result\" [level=2, ref=e8]\n"
            "  - paragraph\n"
            "    - StaticText \"Other description.\"\n"
        )
        results = self._parse(snapshot)
        # Same URL with different titles: both kept, tree order preserved.
        assert [r["url"] for r in results] == [
            "https://same.example",
            "https://same.example",
            "https://other.example",
        ]
        assert [r["title"] for r in results] == [
            "First result",
            "Second result",
            "Other result",
        ]
        # Exact duplicate (url + title) is deduped; positions stay 1..N.
        assert [r["position"] for r in results] == [1, 2, 3]


class TestCleanSnapshotText:
    def test_strips_ref_and_url_annotations_only(self):
        from plugins.web.bing_browser.provider import _clean_snapshot_text

        snapshot = (
            '- link "Example" [ref=@e1] [url=https://example.com]\n'
            '- heading "H1" [level=2]\n'
            '- staticText "Some body text."\n'
        )
        cleaned = _clean_snapshot_text(snapshot)
        assert "[ref=" not in cleaned
        assert "[url=" not in cleaned
        # Non-ref/url annotations and content survive.
        assert "[level=2]" in cleaned
        assert "Some body text." in cleaned
        assert "Example" in cleaned


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


class TestSearch:
    def _search(self, monkeypatch, navigate, snapshot_call=None, cleanup=None,
                browser_present=True):
        from plugins.web.bing_browser import provider as pmod

        monkeypatch.setattr(pmod, "_agent_browser_present", lambda: browser_present)
        monkeypatch.setattr(pmod, "browser_navigate", navigate)
        if snapshot_call is not None:
            monkeypatch.setattr(pmod, "browser_snapshot", snapshot_call)
        if cleanup is not None:
            monkeypatch.setattr(pmod, "cleanup_browser", cleanup)
        return _make_provider().search("test query", limit=5)

    def test_happy_path_returns_legacy_shape(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        calls = {}

        def fake_navigate(url, task_id=None):
            calls["url"] = url
            calls["task_id"] = task_id
            return _nav_json(snapshot=_SNAPSHOT_WITH_URLS)

        cleanup = MagicMock()
        result = self._search(monkeypatch, fake_navigate, cleanup=cleanup)

        assert result["success"] is True
        web = result["data"]["web"]
        assert [r["url"] for r in web] == ["https://example.com", "https://docs.example.com"]
        assert web[0]["title"] == "Example Domain"
        assert web[0]["position"] == 1

    def test_builds_bing_search_url_with_encoded_query(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        calls = {}

        def fake_navigate(url, task_id=None):
            calls["url"] = url
            return _nav_json(snapshot=_SNAPSHOT_WITH_URLS)

        self._search(monkeypatch, fake_navigate)
        parsed = urllib.parse.urlparse(calls["url"])
        assert parsed.scheme == "https"
        assert parsed.netloc == "www.bing.com"
        assert parsed.path == "/search"
        assert urllib.parse.parse_qs(parsed.query)["q"] == ["test query"]

    def test_uses_unique_task_ids_per_call(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        task_ids = []

        def fake_navigate(url, task_id=None):
            task_ids.append(task_id)
            return _nav_json(snapshot=_SNAPSHOT_WITH_URLS)

        self._search(monkeypatch, fake_navigate)
        self._search(monkeypatch, fake_navigate)
        assert len(task_ids) == 2
        assert task_ids[0] != task_ids[1]
        assert all(isinstance(t, str) and t for t in task_ids)

    def test_cleanup_called_with_task_id(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        nav_task = {}
        cleanup_task = {}

        def fake_navigate(url, task_id=None):
            nav_task["id"] = task_id
            return _nav_json(snapshot=_SNAPSHOT_WITH_URLS)

        def fake_cleanup(task_id=None):
            cleanup_task["id"] = task_id

        self._search(monkeypatch, fake_navigate, cleanup=fake_cleanup)
        assert cleanup_task["id"] == nav_task["id"]

    def test_navigation_failure_returns_error_and_cleans_up(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        cleanup = MagicMock()
        result = self._search(
            monkeypatch,
            lambda url, task_id=None: json.dumps(
                {"success": False, "error": "Navigation failed"}
            ),
            cleanup=cleanup,
        )
        assert result["success"] is False
        assert "Navigation failed" in result["error"]
        cleanup.assert_called_once()

    def test_invalid_json_from_navigate_returns_error_and_cleans_up(self, monkeypatch):
        cleanup = MagicMock()
        result = self._search(
            monkeypatch, lambda url, task_id=None: "not json at all", cleanup=cleanup
        )
        assert result["success"] is False
        assert "error" in result
        cleanup.assert_called_once()

    def test_navigate_raising_returns_error_and_cleans_up(self, monkeypatch):
        cleanup = MagicMock()

        def boom(url, task_id=None):
            raise RuntimeError("browser exploded")

        result = self._search(monkeypatch, boom, cleanup=cleanup)
        assert result["success"] is False
        assert "browser exploded" in result["error"]
        cleanup.assert_called_once()

    def test_snapshot_fallback_when_navigate_omits_snapshot(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        calls = {}

        def fake_snapshot(full=False, task_id=None, user_task=None):
            calls["full"] = full
            calls["task_id"] = task_id
            return _snap_json(snapshot=_SNAPSHOT_WITH_URLS)

        result = self._search(
            monkeypatch,
            lambda url, task_id=None: _nav_json(snapshot=""),
            snapshot_call=fake_snapshot,
        )
        assert result["success"] is True
        assert calls["full"] is True
        assert [r["url"] for r in result["data"]["web"]] == [
            "https://example.com",
            "https://docs.example.com",
        ]

    def test_full_snapshot_path_does_not_scan_bare_urls(self, monkeypatch):
        """browser_snapshot fallback output must not be bare-URL scanned —
        a full accessibility tree is full of plain-text URL noise."""
        from plugins.web.bing_browser import provider as pmod

        def fake_snapshot(full=False, task_id=None, user_task=None):
            return _snap_json(snapshot=_SNAPSHOT_BARE_URLS)

        result = self._search(
            monkeypatch,
            lambda url, task_id=None: _nav_json(snapshot=""),
            snapshot_call=fake_snapshot,
        )
        assert result["success"] is True
        assert result["data"]["web"] == []

    def test_snapshot_failure_returns_error_and_cleans_up(self, monkeypatch):
        cleanup = MagicMock()
        result = self._search(
            monkeypatch,
            lambda url, task_id=None: _nav_json(snapshot=""),
            snapshot_call=lambda full=False, task_id=None, user_task=None: _snap_json(
                success=False
            ),
            cleanup=cleanup,
        )
        assert result["success"] is False
        assert "Snapshot failed" in result["error"]
        cleanup.assert_called_once()

    def test_missing_browser_returns_actionable_error(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        navigate = MagicMock()
        result = self._search(monkeypatch, navigate, browser_present=False)
        assert result["success"] is False
        assert "agent-browser" in result["error"].lower()
        navigate.assert_not_called()

    def test_limit_caps_results(self, monkeypatch):
        snapshot = "\n".join(
            f'- link "R{i}" [url=https://r{i}.example.com]' for i in range(20)
        )
        result = self._search(monkeypatch, lambda url, task_id=None: _nav_json(snapshot=snapshot))
        assert result["success"] is True
        assert len(result["data"]["web"]) == 5

    def test_full_snapshot_fallback_when_embedded_has_no_results(self, monkeypatch):
        """Regression: a NONEMPTY header-only navigate snapshot (banner +
        filter nav, no organic results) must fall back to
        browser_snapshot(full=True) and parse the full snapshot's
        listitem blocks — not return {"web": []}."""
        snapshot_call = MagicMock(return_value=_snap_json(snapshot=_SNAPSHOT_LISTITEMS))
        result = self._search(
            monkeypatch,
            lambda url, task_id=None: _nav_json(snapshot=_SNAPSHOT_HEADER_ONLY),
            snapshot_call=snapshot_call,
        )
        assert result["success"] is True
        snapshot_call.assert_called_once()
        assert snapshot_call.call_args.kwargs["full"] is True
        assert snapshot_call.call_args.kwargs["task_id"]
        assert [r["url"] for r in result["data"]["web"]] == [
            "https://kitesurf.cloudflare.app",
            "https://blog.cloudflare.com",
            "https://note.com",
            "https://ai-revolution.co.jp",
        ]
        assert result["data"]["web"][0]["title"] == (
            "Kitesurf - stateless browser running entirely on Workers"
        )
        assert result["data"]["web"][0]["position"] == 1

    def test_full_snapshot_fallback_when_embedded_unparseable(self, monkeypatch):
        """Nonempty embedded snapshot with no parseable results at all
        (no annotated links, no listitems) also triggers the fallback."""
        snapshot_call = MagicMock(return_value=_snap_json(snapshot=_SNAPSHOT_LISTITEMS))
        result = self._search(
            monkeypatch,
            lambda url, task_id=None: _nav_json(
                snapshot='- generic\n  - banner\n    - button "B" [ref=e1]\n'
            ),
            snapshot_call=snapshot_call,
        )
        assert result["success"] is True
        snapshot_call.assert_called_once()
        assert snapshot_call.call_args.kwargs["full"] is True
        assert len(result["data"]["web"]) == 4

    def test_no_full_snapshot_when_embedded_has_results(self, monkeypatch):
        """When the embedded snapshot already yields organic results, no
        extra browser_snapshot round-trip happens."""
        snapshot_call = MagicMock(return_value=_snap_json(snapshot=_SNAPSHOT_LISTITEMS))
        result = self._search(
            monkeypatch,
            lambda url, task_id=None: _nav_json(snapshot=_SNAPSHOT_WITH_URLS),
            snapshot_call=snapshot_call,
        )
        assert result["success"] is True
        assert [r["url"] for r in result["data"]["web"]] == [
            "https://example.com",
            "https://docs.example.com",
        ]
        snapshot_call.assert_not_called()


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


class TestExtract:
    def _extract(self, monkeypatch, navigate, snapshot_call=None, cleanup=None, urls=None):
        from plugins.web.bing_browser import provider as pmod

        if snapshot_call is not None:
            monkeypatch.setattr(pmod, "browser_snapshot", snapshot_call)
        if cleanup is not None:
            monkeypatch.setattr(pmod, "cleanup_browser", cleanup)
        monkeypatch.setattr(pmod, "browser_navigate", navigate)
        return _make_provider().extract(urls or ["https://example.com"])

    def test_happy_path_preserves_order_and_metadata(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        urls = ["https://a.example.com", "https://b.example.com"]
        nav_task_ids = []

        def fake_navigate(url, task_id=None):
            nav_task_ids.append(task_id)
            return json.dumps(
                {"success": True, "url": url, "title": f"Title {url}",
                 "snapshot": f'- heading "H" [level=2]\n- link "X" [ref=@e1] [url={url}]\n- staticText "Body."'}
            )

        cleanup = MagicMock()
        results = self._extract(monkeypatch, fake_navigate, cleanup=cleanup, urls=urls)

        assert [r["url"] for r in results] == urls
        assert results[0]["title"] == "Title https://a.example.com"
        assert "[ref=" not in results[0]["content"]
        assert "Body." in results[0]["content"]
        assert results[0]["raw_content"] == results[0]["content"]
        assert results[0]["metadata"]["source"] == "bing-browser"
        assert results[0]["metadata"]["task_id"] == nav_task_ids[0]
        assert nav_task_ids[0] != nav_task_ids[1]
        assert cleanup.call_count == 2

    def test_per_url_failure_continues_batch(self, monkeypatch):
        results = self._extract(
            monkeypatch,
            lambda url, task_id=None: json.dumps(
                {"success": False, "error": "Blocked page"} if "a." in url
                else {"success": True, "url": url, "title": "B", "snapshot": "content b"}
            ),
            urls=["https://a.example.com", "https://b.example.com"],
        )
        assert len(results) == 2
        assert results[0] == {"url": "https://a.example.com", "error": "Blocked page"}
        assert results[1]["url"] == "https://b.example.com"
        assert results[1]["content"] == "content b"

    def test_cleanup_runs_for_failed_url_too(self, monkeypatch):
        cleanup = MagicMock()

        def boom(url, task_id=None):
            raise RuntimeError("boom")

        self._extract(monkeypatch, boom, cleanup=cleanup, urls=["https://a.example.com"])
        cleanup.assert_called_once()
        assert cleanup.call_args.args and cleanup.call_args.args[0]

    def test_caps_at_five_urls(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        urls = [f"https://u{i}.example.com" for i in range(8)]
        seen = []

        def fake_navigate(url, task_id=None):
            seen.append(url)
            return json.dumps({"success": True, "url": url, "title": "", "snapshot": "c"})

        self._extract(monkeypatch, fake_navigate, urls=urls)
        assert seen == urls[: pmod._MAX_EXTRACT_URLS]
        assert pmod._MAX_EXTRACT_URLS == 5

    def test_snapshot_fallback_used_when_navigate_omits_snapshot(self, monkeypatch):
        calls = {}

        def fake_snapshot(full=False, task_id=None, user_task=None):
            calls["full"] = full
            return _snap_json(snapshot='- staticText "fallback body"')

        results = self._extract(
            monkeypatch,
            lambda url, task_id=None: _nav_json(snapshot=""),
            snapshot_call=fake_snapshot,
        )
        assert calls["full"] is True
        assert results[0]["content"] == '- staticText "fallback body"'

    def test_empty_input_returns_empty_list(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        navigate = MagicMock()
        monkeypatch.setattr(pmod, "browser_navigate", navigate)
        assert _make_provider().extract([]) == []
        navigate.assert_not_called()

    def test_non_string_entries_skipped(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod

        seen = []

        def fake_navigate(url, task_id=None):
            seen.append(url)
            return json.dumps({"success": True, "url": url, "title": "", "snapshot": "c"})

        monkeypatch.setattr(pmod, "browser_navigate", fake_navigate)
        results = _make_provider().extract(["https://ok.example.com", None, 42])
        assert [r["url"] for r in results] == ["https://ok.example.com"]
        assert seen == ["https://ok.example.com"]


# ---------------------------------------------------------------------------
# Registry wiring: explicit opt-in backend
# ---------------------------------------------------------------------------


class TestRegistryWiring:
    @pytest.fixture(autouse=True)
    def _register(self):
        from agent.web_search_registry import register_provider, _reset_for_tests

        _reset_for_tests()
        register_provider(_make_provider())
        yield
        _reset_for_tests()

    def test_registered_under_bing_browser_name(self):
        from agent.web_search_registry import get_provider

        assert get_provider("bing-browser") is not None

    def test_explicit_config_resolves_regardless_of_availability(self):
        from agent.web_search_registry import _resolve

        provider = _resolve("bing-browser", capability="search")
        assert provider is not None
        assert provider.name == "bing-browser"
        provider = _resolve("bing-browser", capability="extract")
        assert provider is not None
        assert provider.name == "bing-browser"

    def test_web_tools_get_backend_accepts_bing_browser(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "bing-browser"})
        assert web_tools._get_backend() == "bing-browser"

    def test_is_backend_available_delegates_to_provider(self, monkeypatch):
        from plugins.web.bing_browser import provider as pmod
        from tools.web_tools import _is_backend_available

        monkeypatch.setattr(pmod, "_is_explicitly_configured", lambda: True)
        monkeypatch.setattr(pmod, "_agent_browser_present", lambda: True)
        assert _is_backend_available("bing-browser") is True

        monkeypatch.setattr(pmod, "_agent_browser_present", lambda: False)
        assert _is_backend_available("bing-browser") is False

    def test_plugin_register_entry_point(self):
        from plugins.web.bing_browser import register

        class FakeCtx:
            def __init__(self):
                self.registered = []

            def register_web_search_provider(self, provider):
                self.registered.append(provider)

        ctx = FakeCtx()
        register(ctx)
        assert len(ctx.registered) == 1
        assert ctx.registered[0].name == "bing-browser"
