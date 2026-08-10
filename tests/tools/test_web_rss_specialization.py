from __future__ import annotations

import json

import pytest

from tools.web_specializations import rss


RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example News</title>
    <description>Useful &amp; current</description>
    <lastBuildDate>Tue, 14 Jul 2026 06:00:00 GMT</lastBuildDate>
    <item>
      <guid>one</guid>
      <title>First item</title>
      <link>https://example.com/one</link>
      <pubDate>Tue, 14 Jul 2026 05:00:00 GMT</pubDate>
      <description><![CDATA[<p>Hello <b>world</b>.</p>]]></description>
    </item>
    <item>
      <guid>one</guid>
      <title>Duplicate</title>
      <link>https://example.com/duplicate</link>
    </item>
  </channel>
</rss>
"""

ATOM_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <updated>2026-07-14T06:00:00Z</updated>
  <entry>
    <id>tag:example.com,2026:1</id>
    <title>Atom item</title>
    <link rel="alternate" href="https://example.com/atom-one" />
    <author><name>Ada</name></author>
    <published>2026-07-14T05:00:00Z</published>
    <summary>Atom summary</summary>
  </entry>
</feed>
"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<rss version="2.0"><channel><title>x</title><item><title>y</title></item></channel></rss>
"""

XXE_PAYLOAD = b"""<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<rss version="2.0"><channel><title>&xxe;</title></channel></rss>
"""

RDF_ONTOLOGY = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="http://example.com/Foo"/>
</rdf:RDF>
"""

EMPTY_RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel>'
    b"<title>Empty</title></channel></rss>"
)


def _use_stdlib_xml(monkeypatch):
    import xml.etree.ElementTree as stdlib_et

    monkeypatch.setattr(rss, "_HAS_DEFUSEDXML", False)
    monkeypatch.setattr(rss, "ET", stdlib_et)
    monkeypatch.setattr(rss, "_PARSE_ERRORS", (stdlib_et.ParseError,))


def _padded_doctype_payload() -> bytes:
    padding = b"x" * 9000
    return padding + BILLION_LAUGHS


def _utf16_doctype_payload() -> bytes:
    text = (
        '<?xml version="1.0"?>\n'
        "<!DOCTYPE foo [\n  <!ENTITY xxe \"test\">\n]>\n"
        '<rss version="2.0"><channel><title>T</title>'
        "<item><title>One</title></item></channel></rss>"
    )
    return b"\xff\xfe" + text.encode("utf-16-le")


def test_parse_rss_normalizes_and_deduplicates():
    result = rss.parse_feed(RSS_XML, "https://example.com/feed")
    assert result is not None
    assert result["title"] == "Example News"
    assert result["metadata"]["entryCount"] == 1
    assert "# Example News" in result["content"]
    assert "[First item](https://example.com/one)" in result["content"]
    assert "Hello world." in result["content"]
    assert "Duplicate" not in result["content"]


def test_parse_atom_normalizes_author_and_link():
    result = rss.parse_feed(ATOM_XML, "https://example.com/atom.xml")
    assert result is not None
    assert result["title"] == "Example Atom"
    assert result["metadata"]["entryCount"] == 1
    assert "[Atom item](https://example.com/atom-one)" in result["content"]
    assert "- Author: Ada" in result["content"]
    assert "Atom summary" in result["content"]


def test_parse_non_feed_xml_returns_none():
    assert (
        rss.parse_feed(b"<document><title>Nope</title></document>", "https://example.com/x.xml")
        is None
    )


def test_parse_malformed_xml_returns_none():
    assert rss.parse_feed(b"<rss><channel>", "https://example.com/feed") is None


def test_billion_laughs_rejected_without_expansion():
    assert rss.parse_feed(BILLION_LAUGHS, "https://example.com/feed") is None


def test_xxe_doctype_rejected():
    assert rss.parse_feed(XXE_PAYLOAD, "https://example.com/feed") is None


def test_billion_laughs_rejected_when_defusedxml_absent(monkeypatch):
    _use_stdlib_xml(monkeypatch)
    assert rss.parse_feed(BILLION_LAUGHS, "https://example.com/feed") is None


def test_xxe_rejected_when_defusedxml_absent(monkeypatch):
    _use_stdlib_xml(monkeypatch)
    assert rss.parse_feed(XXE_PAYLOAD, "https://example.com/feed") is None


def test_padded_doctype_rejected_on_stdlib_path_without_expansion(monkeypatch):
    _use_stdlib_xml(monkeypatch)
    payload = _padded_doctype_payload()
    assert len(payload) > 8192
    assert rss.parse_feed(payload, "https://example.com/feed") is None


def test_utf16_doctype_rejected_on_stdlib_path(monkeypatch):
    _use_stdlib_xml(monkeypatch)
    assert rss.parse_feed(_utf16_doctype_payload(), "https://example.com/feed") is None


def test_malformed_xml_returns_none_on_stdlib_path(monkeypatch):
    _use_stdlib_xml(monkeypatch)
    assert rss.parse_feed(b"<rss><channel>", "https://example.com/feed") is None


def test_rdf_ontology_falls_back_without_overwriting():
    assert rss.parse_feed(RDF_ONTOLOGY, "https://example.com/ontology.rdf") is None


def test_empty_rss_feed_falls_back():
    assert rss.parse_feed(EMPTY_RSS, "https://example.com/empty") is None


def test_empty_rss_feed_apply_leaves_content_unchanged():
    original = {
        "url": "https://example.com/empty",
        "title": "Provider",
        "content": EMPTY_RSS.decode(),
        "raw_content": EMPTY_RSS.decode(),
    }
    before = dict(original)
    rss.apply_rss_specialization(original)
    assert original["content"] == before["content"]


def test_oversized_content_is_never_parsed():
    sniff_prefix = b"<?xml version='1.0'?><rss><channel><title>x</title>"
    oversized = sniff_prefix + (b" " * (rss._MAX_FEED_BYTES + 1))
    assert len(oversized) > rss._MAX_FEED_BYTES
    assert rss.parse_feed(oversized, "https://example.com/huge") is None


def test_oversized_apply_specialization_leaves_content_unchanged(monkeypatch):
    sniff_prefix = b"<?xml version='1.0'?><rss><channel><title>x</title>"
    oversized = sniff_prefix + (b" " * (rss._MAX_FEED_BYTES + 1))
    original = {
        "url": "https://example.com/huge",
        "title": "Provider",
        "content": oversized.decode("latin-1"),
        "raw_content": oversized,
    }
    before = dict(original)
    called = []

    def forbidden_parse(*args, **kwargs):
        called.append(1)
        raise AssertionError("parse_feed must not run for oversized content")

    monkeypatch.setattr(rss, "parse_feed", forbidden_parse)
    rss.apply_rss_specialization(original)
    assert not called
    assert original["content"] == before["content"]


def test_rss_specialization_disabled_when_extract_specializations_not_dict(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {"extract_specializations": "yes"},
    )
    assert web_tools._rss_specialization_enabled() is False


def test_rss_specialization_disabled_when_web_section_null(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"web": None},
    )
    assert web_tools._load_web_config() == {}
    assert web_tools._rss_specialization_enabled() is False

def test_apply_specialization_leaves_plain_html_unchanged():
    original = {
        "url": "https://example.com/page",
        "title": "Page",
        "content": "<html><body>Hello</body></html>",
        "raw_content": "<html><body>Hello</body></html>",
    }
    before = dict(original)
    rss.apply_rss_specialization(original)
    assert original["content"] == before["content"]


def test_many_entries_are_bounded():
    items = []
    for i in range(500):
        items.append(
            f"<item><guid>{i}</guid><title>Item {i}</title>"
            f"<description>summary {i}</description></item>"
        )
    xml = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Big</title>'
        + "".join(items).encode()
        + b"</channel></rss>"
    )
    assert len(xml) <= rss._MAX_FEED_BYTES
    result = rss.parse_feed(xml, "https://example.com/big")
    assert result is not None
    assert result["metadata"]["entryCount"] == rss._MAX_ENTRIES


@pytest.mark.asyncio
async def test_web_extract_malformed_feed_falls_back_to_provider_content(monkeypatch):
    from agent import web_search_registry
    from agent.web_search_provider import WebSearchProvider
    from tools import web_tools

    malformed = b"<rss><channel>"

    class FakeExa(WebSearchProvider):
        @property
        def name(self):
            return "exa"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

        def extract(self, urls, **kwargs):
            return [
                {
                    "url": url,
                    "title": "Provider",
                    "content": malformed.decode(),
                    "raw_content": malformed.decode(),
                }
                for url in urls
            ]

    original = dict(web_search_registry._providers)
    try:
        web_search_registry._providers.clear()
        web_search_registry.register_provider(FakeExa())
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "extract_backend": "exa",
                "extract_specializations": {"rss": True},
            },
        )
        monkeypatch.setattr(web_tools, "async_is_safe_url", lambda url: _true())

        output = json.loads(
            await web_tools.web_extract_tool(["https://example.com/feed"])
        )
        assert output["results"][0]["content"] == malformed.decode()
        assert output["results"][0].get("error") is None
    finally:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(original)


@pytest.mark.asyncio
async def test_web_extract_xxe_payload_falls_back(monkeypatch):
    from agent import web_search_registry
    from agent.web_search_provider import WebSearchProvider
    from tools import web_tools

    class FakeExa(WebSearchProvider):
        @property
        def name(self):
            return "exa"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

        def extract(self, urls, **kwargs):
            return [
                {
                    "url": url,
                    "title": "Provider",
                    "content": XXE_PAYLOAD.decode(),
                    "raw_content": XXE_PAYLOAD.decode(),
                }
                for url in urls
            ]

    original = dict(web_search_registry._providers)
    try:
        web_search_registry._providers.clear()
        web_search_registry.register_provider(FakeExa())
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "extract_backend": "exa",
                "extract_specializations": {"rss": True},
            },
        )
        monkeypatch.setattr(web_tools, "async_is_safe_url", lambda url: _true())

        output = json.loads(
            await web_tools.web_extract_tool(["https://example.com/feed"])
        )
        assert XXE_PAYLOAD.decode() in output["results"][0]["content"]
    finally:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(original)


@pytest.mark.asyncio
async def test_web_extract_many_entries_respects_char_limit(monkeypatch, tmp_path):
    from agent import web_search_registry
    from agent.web_search_provider import WebSearchProvider
    from tools import web_tools

    items = []
    for i in range(500):
        items.append(
            f"<item><guid>{i}</guid><title>Item {i}</title>"
            f"<description>{'word ' * 400}</description></item>"
        )
    big_feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Big</title>'
        + "".join(items).encode()
        + b"</channel></rss>"
    )

    class FakeExa(WebSearchProvider):
        @property
        def name(self):
            return "exa"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

        def extract(self, urls, **kwargs):
            return [
                {
                    "url": url,
                    "title": "Big",
                    "content": big_feed.decode(),
                    "raw_content": big_feed.decode(),
                }
                for url in urls
            ]

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = dict(web_search_registry._providers)
    try:
        web_search_registry._providers.clear()
        web_search_registry.register_provider(FakeExa())
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "extract_backend": "exa",
                "extract_specializations": {"rss": True},
            },
        )
        monkeypatch.setattr(web_tools, "async_is_safe_url", lambda url: _true())

        char_limit = 5000
        output = json.loads(
            await web_tools.web_extract_tool(
                ["https://example.com/big"], char_limit=char_limit
            )
        )
        content = output["results"][0]["content"]
        assert len(content) <= char_limit + 500
        assert "[TRUNCATED]" in content
    finally:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(original)


@pytest.mark.asyncio
async def test_default_off_skips_specialization(monkeypatch):
    from agent import web_search_registry
    from agent.web_search_provider import WebSearchProvider
    from tools import web_tools

    class FakeExa(WebSearchProvider):
        @property
        def name(self):
            return "exa"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

        def extract(self, urls, **kwargs):
            return [
                {
                    "url": url,
                    "title": "Feed",
                    "content": RSS_XML.decode(),
                    "raw_content": RSS_XML.decode(),
                }
                for url in urls
            ]

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("parse_feed must not run when specialization is off")

    original = dict(web_search_registry._providers)
    try:
        web_search_registry._providers.clear()
        web_search_registry.register_provider(FakeExa())
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_backend": "exa"})
        monkeypatch.setattr(web_tools, "async_is_safe_url", lambda url: _true())
        monkeypatch.setattr(rss, "parse_feed", forbidden_parse)

        output = json.loads(
            await web_tools.web_extract_tool(["https://example.com/feed"])
        )
        assert RSS_XML.decode() in output["results"][0]["content"]
        assert "# Example News" not in output["results"][0]["content"]
        assert "<rss" in output["results"][0]["content"]
    finally:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(original)


@pytest.mark.asyncio
async def test_web_extract_opt_in_parses_feed_from_provider(monkeypatch):
    from agent import web_search_registry
    from agent.web_search_provider import WebSearchProvider
    from tools import web_tools

    calls = []

    class FakeExa(WebSearchProvider):
        @property
        def name(self):
            return "exa"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

        def extract(self, urls, **kwargs):
            calls.extend(urls)
            out = []
            for url in urls:
                if url.endswith("/feed"):
                    out.append(
                        {
                            "url": url,
                            "title": "Raw",
                            "content": RSS_XML.decode(),
                            "raw_content": RSS_XML.decode(),
                        }
                    )
                else:
                    out.append(
                        {
                            "url": url,
                            "title": "Generic",
                            "content": "generic body",
                            "raw_content": "generic body",
                        }
                    )
            return out

    original = dict(web_search_registry._providers)
    try:
        web_search_registry._providers.clear()
        web_search_registry.register_provider(FakeExa())
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "extract_backend": "exa",
                "extract_specializations": {"rss": True},
            },
        )
        monkeypatch.setattr(web_tools, "async_is_safe_url", lambda url: _true())

        output = json.loads(
            await web_tools.web_extract_tool(
                ["https://example.com/feed", "https://example.com/article"]
            )
        )
        assert calls == ["https://example.com/feed", "https://example.com/article"]
        assert [item["url"] for item in output["results"]] == [
            "https://example.com/feed",
            "https://example.com/article",
        ]
        assert "# Example News" in output["results"][0]["content"]
        assert "[First item](https://example.com/one)" in output["results"][0]["content"]
        assert output["results"][1]["content"] == "generic body"
    finally:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(original)


@pytest.mark.asyncio
async def test_web_extract_performs_exactly_one_provider_fetch(monkeypatch):
    from agent import web_search_registry
    from agent.web_search_provider import WebSearchProvider
    from tools import web_tools

    extract_calls = 0

    class FakeExa(WebSearchProvider):
        @property
        def name(self):
            return "exa"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

        def extract(self, urls, **kwargs):
            nonlocal extract_calls
            extract_calls += 1
            return [
                {
                    "url": url,
                    "title": "Feed",
                    "content": RSS_XML.decode(),
                    "raw_content": RSS_XML.decode(),
                }
                for url in urls
            ]

    async def forbidden_stream(*args, **kwargs):
        raise AssertionError("web_extract must not open a separate httpx stream")

    original = dict(web_search_registry._providers)
    try:
        web_search_registry._providers.clear()
        web_search_registry.register_provider(FakeExa())
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "extract_backend": "exa",
                "extract_specializations": {"rss": True},
            },
        )
        monkeypatch.setattr(web_tools, "async_is_safe_url", lambda url: _true())
        monkeypatch.setattr(web_tools.httpx, "stream", forbidden_stream)
        monkeypatch.setattr(web_tools.httpx, "AsyncClient", forbidden_stream)

        output = json.loads(
            await web_tools.web_extract_tool(["https://example.com/feed"])
        )
        assert extract_calls == 1
        assert "# Example News" in output["results"][0]["content"]
    finally:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(original)


async def _true():
    return True
