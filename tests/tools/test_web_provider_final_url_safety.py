"""Fail-closed final-URL regression tests for content extraction providers."""

import asyncio
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


PUBLIC_DNS = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))
]
PRIVATE_DNS = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", 0))
]


def _assert_blocked(document, reported_url=""):
    assert document["url"] == ""
    assert document["title"] == ""
    assert document["content"] == ""
    assert document["raw_content"] == ""
    assert "authoritative final URL" in document["error"]
    assert document.get("metadata") in (None, {})
    if reported_url:
        assert reported_url not in json.dumps(document)


def _assert_allowed(document):
    assert document["url"] == "https://public.example/page"
    assert document["title"] == "Retrieved"
    assert document["content"] == "public content"
    assert document["raw_content"] == "public content"
    assert "error" not in document


class TestProviderFinalUrlValidation:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "not a URL",
            " https://example.com/",
            "https://example.com:invalid/",
            "https://example.com/\x00hidden",
            r"https://127.0.0.1\@public.example/",
            "https://user:password@public.example/",
            "https://public.example/%ZZ",
        ],
    )
    def test_missing_or_malformed_url_is_rejected(self, value):
        from tools.url_safety import validate_provider_final_url

        with patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS):
            assert validate_provider_final_url(value) is None

    def test_private_url_is_rejected_even_when_private_urls_are_enabled(self):
        from tools.url_safety import validate_provider_final_url

        with (
            patch("tools.url_safety._global_allow_private_urls", return_value=True),
            patch("tools.url_safety.socket.getaddrinfo", return_value=PRIVATE_DNS),
        ):
            assert validate_provider_final_url("https://private.example/page") is None

    def test_private_url_is_rejected_even_when_host_exception_matches(self):
        from tools.url_safety import validate_provider_final_url

        with (
            patch("tools.url_safety._allows_private_ip_resolution", return_value=True),
            patch("tools.url_safety.socket.getaddrinfo", return_value=PRIVATE_DNS),
        ):
            assert validate_provider_final_url("https://private.example/page") is None

    def test_dns_failure_is_rejected_even_when_proxy_is_configured(self, monkeypatch):
        from tools.url_safety import validate_provider_final_url

        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
        with patch(
            "tools.url_safety.socket.getaddrinfo",
            side_effect=socket.gaierror("resolution failed"),
        ):
            assert validate_provider_final_url("https://unresolved.example/page") is None

    def test_empty_dns_result_is_rejected(self):
        from tools.url_safety import validate_provider_final_url

        with patch("tools.url_safety.socket.getaddrinfo", return_value=[]):
            assert validate_provider_final_url("https://unresolved.example/page") is None

    def test_public_url_is_returned_unchanged(self):
        from tools.url_safety import validate_provider_final_url

        with patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS):
            assert (
                validate_provider_final_url("https://public.example/page")
                == "https://public.example/page"
            )

    @pytest.mark.asyncio
    async def test_async_validation_keeps_event_loop_responsive(self):
        from tools.url_safety import async_validate_provider_final_url

        def slow_dns(*_args, **_kwargs):
            time.sleep(0.15)
            return PUBLIC_DNS

        ticks = 0

        async def ticker():
            nonlocal ticks
            deadline = asyncio.get_running_loop().time() + 0.1
            while asyncio.get_running_loop().time() < deadline:
                ticks += 1
                await asyncio.sleep(0.005)

        with patch("tools.url_safety.socket.getaddrinfo", side_effect=slow_dns):
            validated, _ = await asyncio.gather(
                async_validate_provider_final_url("https://public.example/page"),
                ticker(),
            )

        assert validated == "https://public.example/page"
        assert ticks >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_url", [None, "http://127.0.0.1/private"])
async def test_firecrawl_suppresses_content_without_safe_final_url(reported_url):
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

    metadata = {"title": "Retrieved"}
    if reported_url is not None:
        metadata["sourceURL"] = reported_url
    client = SimpleNamespace(
        scrape=lambda **_kwargs: {"markdown": "sensitive content", "metadata": metadata}
    )

    with (
        patch(
            "plugins.web.firecrawl.provider._get_firecrawl_client", return_value=client
        ),
        patch("plugins.web.firecrawl.provider.check_website_access", return_value=None),
        patch("tools.interrupt.is_interrupted", return_value=False),
    ):
        documents = await FirecrawlWebSearchProvider().extract(
            ["https://requested.example/page"], format="markdown"
        )

    _assert_blocked(documents[0], reported_url or "")
    assert documents[0]["url"] != "https://requested.example/page"


@pytest.mark.parametrize("reported_url", [None, "http://127.0.0.1/private"])
def test_tavily_suppresses_content_without_safe_final_url(reported_url):
    from plugins.web.tavily.provider import _normalize_tavily_documents

    result = {"raw_content": "sensitive content", "title": "Retrieved"}
    if reported_url is not None:
        result["url"] = reported_url

    documents = _normalize_tavily_documents(
        {"results": [result]}, fallback_url="https://requested.example/page"
    )

    _assert_blocked(documents[0], reported_url or "")
    assert documents[0]["url"] != "https://requested.example/page"


@pytest.mark.parametrize(
    "response",
    [
        {
            "failed_results": [
                {
                    "url": "http://127.0.0.1/private",
                    "error": "provider-controlled diagnostic",
                }
            ]
        },
        {"failed_urls": ["http://127.0.0.1/private"]},
    ],
)
def test_tavily_suppresses_unsafe_failure_record(response):
    from plugins.web.tavily.provider import _normalize_tavily_documents

    documents = _normalize_tavily_documents(response)

    _assert_blocked(documents[0], "http://127.0.0.1/private")
    assert "provider-controlled diagnostic" not in json.dumps(documents[0])


@pytest.mark.parametrize("reported_url", [None, "http://127.0.0.1/private"])
def test_exa_suppresses_content_without_safe_final_url(reported_url):
    from plugins.web.exa.provider import ExaWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                text="sensitive content", url=reported_url, title="Retrieved"
            )
        ]
    )
    client = SimpleNamespace(get_contents=lambda *_args, **_kwargs: response)

    with (
        patch("plugins.web.exa.provider._get_exa_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
    ):
        documents = ExaWebSearchProvider().extract(["https://requested.example/page"])

    _assert_blocked(documents[0], reported_url or "")
    assert documents[0]["url"] != "https://requested.example/page"


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_url", [None, "http://127.0.0.1/private"])
async def test_parallel_suppresses_content_without_safe_final_url(reported_url):
    from plugins.web.parallel.provider import ParallelWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                full_content="sensitive content",
                excerpts=[],
                url=reported_url,
                title="Retrieved",
            )
        ],
        errors=[],
    )
    client = SimpleNamespace(
        beta=SimpleNamespace(extract=AsyncMock(return_value=response))
    )

    with (
        patch("plugins.web.parallel.provider._get_async_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
    ):
        documents = await ParallelWebSearchProvider().extract([
            "https://requested.example/page"
        ])

    _assert_blocked(documents[0], reported_url or "")
    assert documents[0]["url"] != "https://requested.example/page"


@pytest.mark.asyncio
async def test_parallel_suppresses_unsafe_error_record():
    from plugins.web.parallel.provider import ParallelWebSearchProvider

    response = SimpleNamespace(
        results=[],
        errors=[
            SimpleNamespace(
                url="http://127.0.0.1/private",
                content="provider-controlled diagnostic",
                error_type="provider-secret-type",
            )
        ],
    )
    client = SimpleNamespace(
        beta=SimpleNamespace(extract=AsyncMock(return_value=response))
    )

    with (
        patch("plugins.web.parallel.provider._get_async_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
    ):
        documents = await ParallelWebSearchProvider().extract([
            "https://requested.example/page"
        ])

    _assert_blocked(documents[0], "http://127.0.0.1/private")
    serialized = json.dumps(documents[0])
    assert "provider-controlled diagnostic" not in serialized
    assert "provider-secret-type" not in serialized


@pytest.mark.asyncio
async def test_firecrawl_preserves_content_with_safe_final_url():
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

    client = SimpleNamespace(
        scrape=lambda **_kwargs: {
            "markdown": "public content",
            "metadata": {
                "sourceURL": "https://public.example/page",
                "title": "Retrieved",
            },
        }
    )
    with (
        patch(
            "plugins.web.firecrawl.provider._get_firecrawl_client", return_value=client
        ),
        patch("plugins.web.firecrawl.provider.check_website_access", return_value=None),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
    ):
        documents = await FirecrawlWebSearchProvider().extract(
            ["https://requested.example/page"], format="markdown"
        )
    _assert_allowed(documents[0])


def test_tavily_preserves_content_with_safe_final_url():
    from plugins.web.tavily.provider import _normalize_tavily_documents

    with (
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
        patch("plugins.web.tavily.provider.check_website_access", return_value=None),
    ):
        documents = _normalize_tavily_documents({
            "results": [
                {
                    "url": "https://public.example/page",
                    "title": "Retrieved",
                    "raw_content": "public content",
                }
            ]
        })
    _assert_allowed(documents[0])


def test_exa_preserves_content_with_safe_final_url():
    from plugins.web.exa.provider import ExaWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                text="public content",
                url="https://public.example/page",
                title="Retrieved",
            )
        ]
    )
    client = SimpleNamespace(get_contents=lambda *_args, **_kwargs: response)
    with (
        patch("plugins.web.exa.provider._get_exa_client", return_value=client),
        patch("plugins.web.exa.provider.check_website_access", return_value=None),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
    ):
        documents = ExaWebSearchProvider().extract(["https://requested.example/page"])
    _assert_allowed(documents[0])


@pytest.mark.asyncio
async def test_parallel_preserves_content_with_safe_final_url():
    from plugins.web.parallel.provider import ParallelWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                full_content="public content",
                excerpts=[],
                url="https://public.example/page",
                title="Retrieved",
            )
        ],
        errors=[],
    )
    client = SimpleNamespace(
        beta=SimpleNamespace(extract=AsyncMock(return_value=response))
    )
    with (
        patch("plugins.web.parallel.provider._get_async_client", return_value=client),
        patch("plugins.web.parallel.provider.check_website_access", return_value=None),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
    ):
        documents = await ParallelWebSearchProvider().extract([
            "https://requested.example/page"
        ])
    _assert_allowed(documents[0])


def _policy_block():
    return {
        "host": "blocked.example",
        "rule": "blocked.example",
        "source": "config",
        "message": "Blocked by website policy",
    }


def _assert_policy_blocked(document, title="Blocked title"):
    assert document["url"] == "https://blocked.example/page"
    assert document["title"] == title
    assert document["content"] == ""
    assert document["raw_content"] == ""
    assert document["error"] == "Blocked by website policy"
    assert document["blocked_by_policy"] == {
        "host": "blocked.example",
        "rule": "blocked.example",
        "source": "config",
    }
    assert "metadata" not in document


def test_tavily_suppresses_content_for_policy_blocked_final_url():
    from plugins.web.tavily.provider import _normalize_tavily_documents

    with (
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
        patch(
            "plugins.web.tavily.provider.check_website_access",
            return_value=_policy_block(),
            create=True,
        ),
    ):
        documents = _normalize_tavily_documents(
            {
                "results": [
                    {
                        "url": "https://blocked.example/page",
                        "title": "Blocked title",
                        "raw_content": "blocked content",
                    }
                ]
            }
        )

    _assert_policy_blocked(documents[0])


def test_exa_suppresses_content_for_policy_blocked_final_url():
    from plugins.web.exa.provider import ExaWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                text="blocked content",
                url="https://blocked.example/page",
                title="Blocked title",
            )
        ]
    )
    client = SimpleNamespace(get_contents=lambda *_args, **_kwargs: response)
    with (
        patch("plugins.web.exa.provider._get_exa_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
        patch(
            "plugins.web.exa.provider.check_website_access",
            return_value=_policy_block(),
            create=True,
        ),
    ):
        documents = ExaWebSearchProvider().extract(["https://requested.example/page"])

    _assert_policy_blocked(documents[0])


@pytest.mark.asyncio
async def test_parallel_suppresses_content_for_policy_blocked_final_url():
    from plugins.web.parallel.provider import ParallelWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                full_content="blocked content",
                excerpts=[],
                url="https://blocked.example/page",
                title="Blocked title",
            )
        ],
        errors=[],
    )
    client = SimpleNamespace(
        beta=SimpleNamespace(extract=AsyncMock(return_value=response))
    )
    with (
        patch("plugins.web.parallel.provider._get_async_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
        patch(
            "plugins.web.parallel.provider.check_website_access",
            return_value=_policy_block(),
            create=True,
        ),
    ):
        documents = await ParallelWebSearchProvider().extract([
            "https://requested.example/page"
        ])

    _assert_policy_blocked(documents[0])


@pytest.mark.parametrize(
    "response",
    [
        {
            "failed_results": [
                {
                    "url": "https://blocked.example/page",
                    "error": "provider-controlled diagnostic",
                }
            ]
        },
        {"failed_urls": ["https://blocked.example/page"]},
    ],
)
def test_tavily_suppresses_policy_blocked_failure_record(response):
    from plugins.web.tavily.provider import _normalize_tavily_documents

    with (
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
        patch(
            "plugins.web.tavily.provider.check_website_access",
            return_value=_policy_block(),
        ),
    ):
        documents = _normalize_tavily_documents(response)

    _assert_policy_blocked(documents[0], title="")
    assert "provider-controlled diagnostic" not in json.dumps(documents[0])


@pytest.mark.asyncio
async def test_parallel_suppresses_policy_blocked_error_record():
    from plugins.web.parallel.provider import ParallelWebSearchProvider

    response = SimpleNamespace(
        results=[],
        errors=[
            SimpleNamespace(
                url="https://blocked.example/page",
                content="provider-controlled diagnostic",
                error_type="provider-secret-type",
            )
        ],
    )
    client = SimpleNamespace(
        beta=SimpleNamespace(extract=AsyncMock(return_value=response))
    )
    with (
        patch("plugins.web.parallel.provider._get_async_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("tools.url_safety.socket.getaddrinfo", return_value=PUBLIC_DNS),
        patch(
            "plugins.web.parallel.provider.check_website_access",
            return_value=_policy_block(),
        ),
    ):
        documents = await ParallelWebSearchProvider().extract([
            "https://requested.example/page"
        ])

    _assert_policy_blocked(documents[0], title="")
    serialized = json.dumps(documents[0])
    assert "provider-controlled diagnostic" not in serialized
    assert "provider-secret-type" not in serialized


async def _run_shared_sink_registered_result(
    provider_result,
    *,
    policy_block,
    dns_result=PUBLIC_DNS,
    requested_urls=None,
):
    from agent import web_search_registry
    from agent.web_search_provider import WebSearchProvider
    from tools import web_tools

    class RegisteredExtractProvider(WebSearchProvider):
        @property
        def name(self):
            return "registered-final-url-test"

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, _urls, **_kwargs):
            if isinstance(provider_result, list):
                return provider_result
            return [provider_result]

    with web_search_registry._lock:
        previous = dict(web_search_registry._providers)
        web_search_registry._providers.clear()
    provider = RegisteredExtractProvider()
    web_search_registry.register_provider(provider)
    try:
        with (
            patch("tools.web_tools._ensure_web_plugins_loaded"),
            patch("tools.web_tools._get_extract_backend", return_value=provider.name),
            patch("tools.web_tools.async_is_safe_url", new=AsyncMock(return_value=True)),
            patch("tools.url_safety.socket.getaddrinfo", return_value=dns_result),
            patch(
                "tools.web_tools.check_website_access",
                return_value=policy_block,
            ),
        ):
            return json.loads(
                await web_tools.web_extract_tool(
                    requested_urls or ["https://requested.example/page"]
                )
            )
    finally:
        with web_search_registry._lock:
            web_search_registry._providers.clear()
            web_search_registry._providers.update(previous)


@pytest.mark.asyncio
async def test_shared_sink_blocks_policy_denied_registered_provider_result():
    response = await _run_shared_sink_registered_result(
        {
            "url": "https://blocked.example/page",
            "title": "provider-controlled title",
            "content": "provider-controlled content",
            "raw_content": "provider-controlled raw content",
            "metadata": {"provider": "controlled metadata"},
            "error": "provider-controlled diagnostic",
        },
        policy_block=_policy_block(),
    )

    assert response == {
        "results": [
            {
                "url": "",
                "title": "",
                "content": "",
                "error": "Blocked by website policy",
                "blocked_by_policy": {
                    "host": "blocked.example",
                    "rule": "blocked.example",
                    "source": "config",
                },
            }
        ]
    }
    serialized = json.dumps(response)
    for secret in (
        "provider-controlled title",
        "provider-controlled content",
        "provider-controlled raw content",
        "controlled metadata",
        "provider-controlled diagnostic",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_shared_sink_preserves_safe_registered_provider_result():
    response = await _run_shared_sink_registered_result(
        {
            "url": "https://public.example/page",
            "title": "Retrieved",
            "content": "public content",
            "raw_content": "public content",
            "metadata": {"provider": "registered-final-url-test"},
        },
        policy_block=None,
    )

    assert response == {
        "results": [
            {
                "url": "https://public.example/page",
                "title": "Retrieved",
                "content": "public content",
                "error": None,
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_url", "dns_result"),
    [
        (None, PUBLIC_DNS),
        ("not a URL", PUBLIC_DNS),
        ("http://127.0.0.1/private", PRIVATE_DNS),
    ],
)
async def test_shared_sink_blocks_unsafe_registered_provider_result(
    reported_url, dns_result
):
    from tools.url_safety import PROVIDER_FINAL_URL_ERROR

    provider_result = {
        "title": "provider-controlled title",
        "content": "provider-controlled content",
        "raw_content": "provider-controlled raw content",
        "metadata": {"provider": "controlled metadata"},
        "error": "provider-controlled diagnostic",
    }
    if reported_url is not None:
        provider_result["url"] = reported_url

    response = await _run_shared_sink_registered_result(
        provider_result,
        policy_block=None,
        dns_result=dns_result,
    )

    assert response == {
        "results": [
            {
                "url": "",
                "title": "",
                "content": "",
                "error": PROVIDER_FINAL_URL_ERROR,
            }
        ]
    }
    serialized = json.dumps(response)
    for secret in (
        "provider-controlled title",
        "provider-controlled content",
        "provider-controlled raw content",
        "controlled metadata",
        "provider-controlled diagnostic",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_shared_sink_ignores_excess_registered_provider_results():
    provider_results = [
        {
            "url": f"https://public-{index}.example/page",
            "title": f"Result {index}",
            "content": f"content {index}",
        }
        for index in range(3)
    ]
    validated_urls = []

    async def track_validation(url):
        validated_urls.append(url)
        return url

    with patch(
        "tools.web_tools.async_validate_provider_final_url",
        side_effect=track_validation,
    ):
        response = await _run_shared_sink_registered_result(
            provider_results,
            policy_block=None,
        )

    assert validated_urls == ["https://public-0.example/page"]
    assert response == {
        "results": [
            {
                "url": "https://public-0.example/page",
                "title": "Result 0",
                "content": "content 0",
                "error": None,
            }
        ]
    }


@pytest.mark.asyncio
async def test_shared_sink_synthesizes_missing_safe_result():
    response = await _run_shared_sink_registered_result(
        {
            "url": "https://first.example/page",
            "title": "First",
            "content": "first content",
        },
        policy_block=None,
        requested_urls=[
            "https://first.example/page",
            "https://second.example/page",
        ],
    )

    assert len(response["results"]) == 2
    assert response["results"][0]["url"] == "https://first.example/page"
    assert response["results"][1] == {
        "url": "https://second.example/page",
        "title": "",
        "content": "",
        "error": "Extract backend returned no result for this URL",
    }


@pytest.mark.asyncio
async def test_shared_sink_correlates_reordered_success_and_error_by_url():
    response = await _run_shared_sink_registered_result(
        [
            {
                "url": "https://second.example/page",
                "title": "Second",
                "content": "second content",
            },
            {
                "url": "https://first.example/page",
                "title": "",
                "content": "",
                "error": "first failed",
            },
        ],
        policy_block=None,
        requested_urls=[
            "https://first.example/page",
            "https://second.example/page",
        ],
    )

    assert response["results"] == [
        {
            "url": "https://first.example/page",
            "title": "",
            "content": "",
            "error": "first failed",
        },
        {
            "url": "https://second.example/page",
            "title": "Second",
            "content": "second content",
            "error": None,
        },
    ]


@pytest.mark.asyncio
async def test_shared_sink_suppresses_ambiguous_batch_redirects():
    response = await _run_shared_sink_registered_result(
        [
            {
                "url": "https://redirect-one.example/page",
                "title": "Secret one",
                "content": "secret content one",
            },
            {
                "url": "https://redirect-two.example/page",
                "title": "Secret two",
                "content": "secret content two",
            },
        ],
        policy_block=None,
        requested_urls=[
            "https://first.example/page",
            "https://second.example/page",
        ],
    )

    assert [result["url"] for result in response["results"]] == [
        "https://first.example/page",
        "https://second.example/page",
    ]
    assert all(
        result["error"] == "Extract backend returned no result for this URL"
        for result in response["results"]
    )
    serialized = json.dumps(response)
    assert "redirect-one.example" not in serialized
    assert "redirect-two.example" not in serialized
    assert "secret content" not in serialized


@pytest.mark.asyncio
async def test_shared_sink_does_not_correlate_redirect_after_generic_failure():
    response = await _run_shared_sink_registered_result(
        [
            {
                "url": None,
                "title": "Unsafe",
                "content": "unsafe secret",
            },
            {
                "url": "https://redirect.example/page",
                "title": "Redirect",
                "content": "redirect secret",
            },
        ],
        policy_block=None,
        requested_urls=[
            "https://first.example/page",
            "https://second.example/page",
        ],
    )

    serialized = json.dumps(response)
    assert "redirect.example" not in serialized
    assert "redirect secret" not in serialized
    assert "unsafe secret" not in serialized
    assert len(response["results"]) == 2
    assert all(not result["content"] for result in response["results"])


def test_exa_caps_provider_records_before_final_url_validation():
    from plugins.web.exa.provider import ExaWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(text="content", url=f"https://{index}.example", title="")
            for index in range(3)
        ]
    )
    client = SimpleNamespace(get_contents=lambda *_args, **_kwargs: response)
    with (
        patch("plugins.web.exa.provider._get_exa_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch(
            "plugins.web.exa.provider.validate_provider_final_url",
            side_effect=lambda url: url,
        ) as validate,
        patch("plugins.web.exa.provider.check_website_access", return_value=None),
    ):
        documents = ExaWebSearchProvider().extract(["https://requested.example"])

    assert validate.call_count == 1
    assert len(documents) == 1


def test_tavily_caps_combined_records_before_final_url_validation():
    from plugins.web.tavily.provider import _normalize_tavily_documents

    with (
        patch(
            "plugins.web.tavily.provider.validate_provider_final_url",
            side_effect=lambda url: url,
        ) as validate,
        patch("plugins.web.tavily.provider.check_website_access", return_value=None),
    ):
        documents = _normalize_tavily_documents(
            {
                "results": [{"url": "https://success.example", "content": "ok"}],
                "failed_results": [
                    {"url": "https://failed.example", "error": "failed"}
                ],
                "failed_urls": ["https://also-failed.example"],
            },
            max_results=1,
        )

    assert validate.call_count == 1
    assert len(documents) == 1


@pytest.mark.asyncio
async def test_parallel_caps_combined_records_before_final_url_validation():
    from plugins.web.parallel.provider import ParallelWebSearchProvider

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                full_content="content",
                excerpts=[],
                url=f"https://{index}.example",
                title="",
            )
            for index in range(2)
        ],
        errors=[
            SimpleNamespace(
                url="https://error.example",
                content="failed",
                error_type="error",
            )
        ],
    )
    client = SimpleNamespace(beta=SimpleNamespace(extract=AsyncMock(return_value=response)))
    with (
        patch("plugins.web.parallel.provider._get_async_client", return_value=client),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch(
            "plugins.web.parallel.provider.async_validate_provider_final_url",
            AsyncMock(side_effect=lambda url: url),
        ) as validate,
        patch("plugins.web.parallel.provider.check_website_access", return_value=None),
    ):
        documents = await ParallelWebSearchProvider().extract(
            ["https://requested.example"]
        )

    assert validate.await_count == 1
    assert len(documents) == 1
