"""Tests for SSRF protection in url_safety module."""

import asyncio
import base64
from collections import UserDict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from tools.url_safety import (
    is_safe_url,
    async_is_safe_url,
    is_always_blocked_url,
    normalize_url_for_request,
    redirect_target_from_response,
    create_ssrf_safe_client,
    create_ssrf_safe_async_client,
    SSRFConnectionBlocked,
    _SSRFGuardedAsyncNetworkBackend,
    _MAX_SSRF_CONNECT_IPS,
    _resolved_http_connect_ips,
    _is_blocked_ip,
    _configured_doh_resolver,
    _global_allow_private_urls,
    _reset_allow_private_cache,
)

import ipaddress
import pytest


_TLS_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "url_safety_tls"


@contextmanager
def _running_test_server(handler, *, tls_context=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "test HTTPS server thread did not terminate"


class TestNormalizeUrlForRequest:
    def test_percent_encodes_non_ascii_path(self):
        assert (
            normalize_url_for_request("https://wttr.in/Köln")
            == "https://wttr.in/K%C3%B6ln"
        )

    def test_preserves_existing_percent_escapes(self):
        assert (
            normalize_url_for_request("https://wttr.in/K%C3%B6ln")
            == "https://wttr.in/K%C3%B6ln"
        )

    def test_preserves_reserved_query_syntax(self):
        assert (
            normalize_url_for_request("https://example.com/search?q=Köln&lang=de")
            == "https://example.com/search?q=K%C3%B6ln&lang=de"
        )

    def test_idna_encodes_hostname(self):
        assert (
            normalize_url_for_request("https://münich.example/Köln")
            == "https://xn--mnich-kva.example/K%C3%B6ln"
        )

    def test_repairs_space_between_scheme_and_authority(self):
        assert (
            normalize_url_for_request("https:// docs.openclaw.ai")
            == "https://docs.openclaw.ai"
        )

    def test_repairs_tab_between_scheme_and_authority(self):
        assert (
            normalize_url_for_request("https://	docs.openclaw.ai/path")
            == "https://docs.openclaw.ai/path"
        )

    def test_trims_but_preserves_path_and_query_space_semantics(self):
        assert (
            normalize_url_for_request(" https://example.com/a b?q=c d ")
            == "https://example.com/a%20b?q=c%20d"
        )

    def test_does_not_collapse_embedded_scheme_separator_in_query(self):
        assert (
            normalize_url_for_request("https://example.com/r?next=https:// evil.example")
            == "https://example.com/r?next=https://%20evil.example"
        )


class TestIsSafeUrl:
    def test_public_url_allowed(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert is_safe_url("https://example.com/image.png") is True

    def test_configured_doh_public_answer_overrides_fake_ip_dns(self):
        queries = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, **kwargs):
                assert kwargs["trust_env"] is False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, *, params, headers):
                assert url == "https://1.1.1.1/dns-query"
                assert headers["accept"] == "application/dns-json"
                queries.append(params["type"])
                answers = (
                    [
                        {
                            "name": "example.com",
                            "type": 5,
                            "TTL": 60,
                            "data": "edge.example.net.",
                        },
                        {
                            "name": "edge.example.net",
                            "type": 1,
                            "TTL": 60,
                            "data": "93.184.216.34",
                        },
                    ]
                    if params["type"] == "A"
                    else []
                )
                return FakeResponse({"Status": 0, "Answer": answers})

        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
                "url_safety_doh_timeout": 5,
            }
        }
        fake_ip_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.1.10", 0)),
        ]

        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("socket.getaddrinfo", return_value=fake_ip_answer),
            patch("httpx.Client", FakeClient),
        ):
            assert is_safe_url("https://example.com/") is True

        assert queries == ["A", "AAAA"]

    def test_configured_doh_private_answer_is_blocked(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                return_value=["192.168.1.10"],
            ),
        ):
            assert is_safe_url("https://example.com/") is False

    def test_configured_doh_mixed_public_and_private_ipv6_is_blocked(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                return_value=["93.184.216.34", "fd00::10"],
            ),
        ):
            assert is_safe_url("https://example.com/") is False

    def test_configured_doh_metadata_answer_is_always_blocked(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                return_value=["169.254.169.254"],
            ),
        ):
            assert is_safe_url("https://attacker.example/") is False

    def test_configured_doh_failure_does_not_fallback_to_system_dns(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                side_effect=socket.gaierror("DoH unavailable"),
            ),
            patch("socket.getaddrinfo") as system_resolve,
        ):
            assert is_safe_url("https://example.com/") is False

        system_resolve.assert_not_called()

    @pytest.mark.parametrize("invalid_url", [None, False, 0, [], {}])
    def test_non_string_doh_url_fails_closed_without_system_dns(self, invalid_url):
        config = {"security": {"url_safety_doh_url": invalid_url}}
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("socket.getaddrinfo") as system_resolve,
        ):
            assert is_safe_url("https://example.com/") is False
            system_resolve.assert_not_called()

    def test_configured_doh_failure_stays_blocked_with_proxy_configured(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://127.0.0.1:8080"},
                clear=False,
            ),
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                side_effect=socket.gaierror("DoH unavailable"),
            ),
            patch("socket.getaddrinfo") as system_resolve,
        ):
            assert is_safe_url("https://example.com/") is False

        system_resolve.assert_not_called()

    def test_literal_ip_does_not_use_configured_doh(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("tools.url_safety._resolve_hostname_via_doh") as doh_resolve,
        ):
            assert is_safe_url("https://93.184.216.34/") is True

        doh_resolve.assert_not_called()

    @pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
    def test_configured_doh_non_finite_timeout_uses_safe_default(self, timeout):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
                "url_safety_doh_timeout": timeout,
            }
        }
        with patch("hermes_cli.config.read_raw_config", return_value=config):
            assert _configured_doh_resolver() == (
                "https://1.1.1.1/dns-query",
                5.0,
            )

    @pytest.mark.parametrize(
        ("timeout", "expected"),
        [(-1, 0.5), (0, 0.5), (31, 30.0)],
    )
    def test_configured_doh_timeout_is_clamped(self, timeout, expected):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
                "url_safety_doh_timeout": timeout,
            }
        }
        with patch("hermes_cli.config.read_raw_config", return_value=config):
            assert _configured_doh_resolver() == (
                "https://1.1.1.1/dns-query",
                expected,
            )

    def test_configured_doh_non_https_endpoint_fails_closed(self):
        config = {
            "security": {
                "url_safety_doh_url": "http://127.0.0.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("socket.getaddrinfo") as system_resolve,
        ):
            assert is_safe_url("https://example.com/") is False
            assert is_always_blocked_url("https://attacker.example/") is True

        system_resolve.assert_not_called()

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"Status": False, "Answer": []},
            {"Status": "0", "Answer": []},
            {"Status": 2, "Answer": []},
            {"Status": 0, "Answer": {}},
            {"Status": 0, "Answer": ""},
            {"Status": 0, "Answer": "not-a-list"},
            {"Status": 0, "Answer": [False]},
            {
                "Status": 0,
                "Answer": [{"type": True, "data": "93.184.216.34"}],
            },
            {
                "Status": 0,
                "Answer": [{"type": 1, "data": "2001:4860:4860::8888"}],
            },
            {
                "Status": 0,
                "Answer": [
                    {"type": 1, "data": "93.184.216.34"},
                    {"type": 28, "data": "not-an-ip"},
                ],
            },
            {
                "Status": 0,
                "Answer": [
                    {"type": 28, "data": "2606:4700:4700::1111"},
                    {"type": 1, "data": "169.254.169.254"},
                ],
            },
        ],
    )
    def test_configured_doh_malformed_json_fails_closed(self, payload):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("httpx.Client", FakeClient),
            patch("socket.getaddrinfo") as system_resolve,
        ):
            assert is_safe_url("https://example.com/") is False
            assert is_always_blocked_url("https://attacker.example/") is True

        system_resolve.assert_not_called()

    def test_configured_doh_malformed_a_blocks_even_when_aaaa_is_valid(self):
        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, *_args, params, **_kwargs):
                if params["type"] == "A":
                    return FakeResponse({"Status": 0, "Answer": [False]})
                return FakeResponse({
                    "Status": 0,
                    "Answer": [
                        {
                            "type": 28,
                            "data": "2606:2800:220:1:248:1893:25c8:1946",
                        },
                    ],
                })

        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("httpx.Client", FakeClient),
        ):
            assert is_safe_url("https://example.com/") is False

    def test_configured_doh_aaaa_only_answer_is_allowed(self):
        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, *_args, params, **_kwargs):
                answers = (
                    []
                    if params["type"] == "A"
                    else [
                        {
                            "type": 28,
                            "data": "2606:2800:220:1:248:1893:25c8:1946",
                        },
                    ]
                )
                return FakeResponse({"Status": 0, "Answer": answers})

        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("httpx.Client", FakeClient),
        ):
            assert is_safe_url("https://example.com/") is True

    @pytest.mark.parametrize(
        "config_text",
        [
            "[not, a, mapping]\n",
            "null\n",
            "false\n",
            "0\n",
            '""\n',
            "security: []\n",
            "security: [unterminated\n",
        ],
    )
    def test_malformed_existing_config_fails_closed(
        self, monkeypatch, tmp_path, config_text
    ):
        from hermes_cli.config import read_raw_config

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            config_text,
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        # A prior non-strict caller must not normalize/cache away malformed data.
        read_raw_config()
        with patch("socket.getaddrinfo") as system_resolve:
            assert is_safe_url("https://example.com/") is False
            assert is_always_blocked_url("https://attacker.example/") is True

        system_resolve.assert_not_called()

    def test_strict_config_read_rechecks_readability_after_cache(
        self, monkeypatch, tmp_path
    ):
        from hermes_cli.config import read_raw_config

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "security:\n"
            '  url_safety_doh_url: "https://resolver.example/dns-query"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        assert read_raw_config()["security"]["url_safety_doh_url"]

        with (
            patch("builtins.open", side_effect=PermissionError("config unreadable")),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                return_value=["93.184.216.34"],
            ) as doh_resolve,
            patch("socket.getaddrinfo") as system_resolve,
        ):
            assert is_safe_url("https://example.com/") is False

        doh_resolve.assert_not_called()
        system_resolve.assert_not_called()

    def test_ftp_scheme_blocked(self):
        """Only http/https should be allowed for fetch tools."""
        assert is_safe_url("ftp://example.com/file.txt") is False

    def test_missing_scheme_blocked(self):
        """Bare host/path should be rejected to avoid ambiguous handling."""
        assert is_safe_url("example.com/path") is False

    def test_localhost_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            assert is_safe_url("http://localhost:8080/secret") is False

    def test_loopback_ip_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            assert is_safe_url("http://127.0.0.1/admin") is False

    def test_private_10_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]):
            assert is_safe_url("http://internal-service.local/api") is False

    def test_private_172_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("172.16.0.1", 0)),
        ]):
            assert is_safe_url("http://private.corp/data") is False

    def test_private_192_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.1.1", 0)),
        ]):
            assert is_safe_url("http://router.local") is False

    def test_link_local_169_254_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ]):
            assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False

    def test_metadata_google_internal_blocked(self):
        assert is_safe_url("http://metadata.google.internal/computeMetadata/v1/") is False

    def test_ipv6_loopback_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::1", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[::1]:8080/") is False

    def test_dns_failure_blocked(self, monkeypatch):
        """DNS failures fail closed — block the request (no proxy configured)."""
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(var, raising=False)
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name resolution failed")):
            assert is_safe_url("https://nonexistent.example.com") is False


class TestProxyEnvironmentDnsDelegation:
    """When an HTTP proxy is configured, DNS is delegated to the proxy.

    Sandbox / proxy-only environments (Docker + Squid, NVIDIA OpenShell,
    iron-proxy egress sandboxes) block direct DNS at the network level;
    only HTTP(S) via the proxy works. is_safe_url must not fail closed on
    the pre-flight DNS check there — the proxy is the egress boundary.
    Regression tests for #32217 / PR #68469.
    """

    @pytest.fixture(autouse=True)
    def _clear_proxy_env(self, monkeypatch):
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(var, raising=False)

    def test_dns_failure_allowed_when_proxy_configured(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://host.docker.internal:9090")
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("blocked at network level")):
            assert is_safe_url("https://api.openai.com/v1/models") is True

    def test_lowercase_proxy_var_also_recognized(self, monkeypatch):
        monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("no dns")):
            assert is_safe_url("https://example.com/") is True

    def test_metadata_hostname_still_blocked_with_proxy(self, monkeypatch):
        """The blocked-hostname floor runs BEFORE the DNS skip."""
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("no dns")):
            assert is_safe_url("http://metadata.google.internal/computeMetadata/v1/") is False

    def test_literal_metadata_ip_still_blocked_with_proxy(self, monkeypatch):
        """Literal IPs never take the DNS-failure path — floor intact."""
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False

    def test_literal_private_ip_still_blocked_with_proxy(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        assert is_safe_url("http://192.168.1.1/admin") is False

    def test_dns_success_path_unchanged_with_proxy(self, monkeypatch):
        """When DNS resolves, the normal IP checks still apply under a proxy."""
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.5", 0)),
        ]):
            assert is_safe_url("https://internal.corp/") is False

    def test_empty_proxy_var_does_not_trigger_delegation(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "")
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("fail")):
            assert is_safe_url("https://nonexistent.example.com") is False

    def test_empty_url_blocked(self):
        assert is_safe_url("") is False

    def test_no_hostname_blocked(self):
        assert is_safe_url("http://") is False

    def test_public_ip_allowed(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert is_safe_url("https://example.com") is True

    # ── New tests for hardened SSRF protection ──

    def test_cgnat_100_64_blocked(self):
        """100.64.0.0/10 (CGNAT/Shared Address Space) is NOT covered by
        ipaddress.is_private — must be blocked explicitly."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("100.64.0.1", 0)),
        ]):
            assert is_safe_url("http://some-cgnat-host.example/") is False

    def test_cgnat_100_127_blocked(self):
        """Upper end of CGNAT range (100.127.255.255)."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("100.127.255.254", 0)),
        ]):
            assert is_safe_url("http://tailscale-peer.example/") is False

    def test_multicast_blocked(self):
        """Multicast addresses (224.0.0.0/4) not caught by is_private."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("224.0.0.251", 0)),
        ]):
            assert is_safe_url("http://mdns-host.local/") is False

    def test_multicast_ipv6_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("ff02::1", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[ff02::1]/") is False

    def test_ipv4_mapped_ipv6_loopback_blocked(self):
        """::ffff:127.0.0.1 — IPv4-mapped IPv6 loopback."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[::ffff:127.0.0.1]/") is False

    def test_ipv4_mapped_ipv6_metadata_blocked(self):
        """::ffff:169.254.169.254 — IPv4-mapped IPv6 cloud metadata."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::ffff:169.254.169.254", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[::ffff:169.254.169.254]/") is False

    def test_ipv6_scope_id_link_local_blocked(self):
        """fe80::1%eth0 — a scope-ID-bearing link-local address must not bypass
        the guard. ``ipaddress.ip_address`` rejects the ``%scope`` suffix, so
        the scope must be stripped before the block check rather than skipped.
        """
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("fe80::1%eth0", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[fe80::1%eth0]/") is False

    def test_ipv6_scope_id_loopback_blocked(self):
        """::1%lo — scoped IPv6 loopback must still be blocked."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::1%lo", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[::1%lo]/") is False

    def test_unparseable_ip_after_scope_strip_fails_closed(self):
        """An address that is still unparseable after stripping the scope ID
        must fail closed (block), not be silently skipped."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("not-an-ip%garbage", 0, 0, 0)),
        ]):
            assert is_safe_url("http://example.invalid/") is False

    def test_unspecified_address_blocked(self):
        """0.0.0.0 — unspecified address, can bind to all interfaces."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("0.0.0.0", 0)),
        ]):
            assert is_safe_url("http://0.0.0.0/") is False

    def test_unexpected_error_fails_closed(self):
        """Unexpected exceptions should block, not allow."""
        with patch("tools.url_safety.urlparse", side_effect=ValueError("bad url")):
            assert is_safe_url("http://evil.com/") is False

    def test_metadata_goog_blocked(self):
        assert is_safe_url("http://metadata.goog/computeMetadata/v1/") is False

    def test_ipv6_unique_local_blocked(self):
        """fc00::/7 — IPv6 unique local addresses."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("fd12::1", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[fd12::1]/internal") is False

    def test_non_cgnat_100_allowed(self):
        """100.0.0.1 is NOT in CGNAT range (100.64.0.0/10), should be allowed."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("100.0.0.1", 0)),
        ]):
            # 100.0.0.1 is a global IP, not in CGNAT range
            assert is_safe_url("http://legit-host.example/") is True

    def test_benchmark_ip_blocked_for_non_allowlisted_host(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.18.0.23", 0)),
        ]):
            assert is_safe_url("https://example.com/file.jpg") is False

    def test_qq_multimedia_hostname_allowed_with_benchmark_ip(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.18.0.23", 0)),
        ]):
            assert is_safe_url("https://multimedia.nt.qq.com.cn/download?id=123") is True

    def test_qq_multimedia_hostname_exception_is_exact_match(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.18.0.23", 0)),
        ]):
            assert is_safe_url("https://sub.multimedia.nt.qq.com.cn/download?id=123") is False

    def test_qq_multimedia_hostname_exception_requires_https(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.18.0.23", 0)),
        ]):
            assert is_safe_url("http://multimedia.nt.qq.com.cn/download?id=123") is False

    def test_qq_multimedia_hostname_dns_failure_still_blocked(self, monkeypatch):
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(var, raising=False)
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name resolution failed")):
            assert is_safe_url("https://multimedia.nt.qq.com.cn/download?id=123") is False


class TestAsyncIsSafeUrl:
    """async_is_safe_url must match is_safe_url (runs DNS in a thread pool)."""

    @pytest.mark.asyncio
    async def test_public_url_allowed(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert await async_is_safe_url("https://example.com/x") is True

    @pytest.mark.asyncio
    async def test_localhost_blocked(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            assert await async_is_safe_url("http://localhost:8080/") is False


class TestSSRFGuardedHttpxClient:
    def test_sync_client_forces_environment_proxy_bypass(self):
        client = object()
        with (
            patch("httpx.Client", return_value=client) as constructor,
            patch("tools.url_safety._install_ssrf_guard_on_client") as install_guard,
        ):
            result = create_ssrf_safe_client(trust_env=True)

        assert result is client
        assert constructor.call_args.kwargs["trust_env"] is False
        install_guard.assert_called_once_with(client)

    def test_async_client_forces_environment_proxy_bypass(self):
        client = object()
        with (
            patch("httpx.AsyncClient", return_value=client) as constructor,
            patch(
                "tools.url_safety._install_ssrf_guard_on_async_client"
            ) as install_guard,
        ):
            result = create_ssrf_safe_async_client(trust_env=True)

        assert result is client
        assert constructor.call_args.kwargs["trust_env"] is False
        install_guard.assert_called_once_with(client)

    def test_config_yaml_to_doh_json_to_validated_ip_dial(self, monkeypatch, tmp_path):
        """Exercise config, TLS DoH, preflight, guarded dial, Host, and SNI."""
        cert_path = _TLS_FIXTURE_DIR / "test-cert.pem"
        key_path = tmp_path / "test-key.pem"
        key_path.write_bytes(
            base64.b64decode(
                (_TLS_FIXTURE_DIR / "test-key.pem.b64").read_bytes().strip(),
                validate=True,
            )
        )

        destination_requests = []
        destination_sni = []

        class DestinationHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                destination_requests.append((self.path, self.headers.get("host")))
                body = b"validated destination"
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        doh_queries = []
        doh_sni = []

        class DoHHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                query_type = query.get("type", [""])[0]
                hostname = query.get("name", [""])[0]
                doh_queries.append(
                    (parsed.path, hostname, query_type, self.headers.get("host"))
                )
                answers = (
                    [{"name": hostname, "type": 1, "TTL": 60, "data": "127.0.0.1"}]
                    if query_type == "A"
                    else []
                )
                body = json.dumps({"Status": 0, "Answer": answers}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/dns-json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        doh_tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        doh_tls_context.load_cert_chain(cert_path, key_path)
        doh_tls_context.set_servername_callback(
            lambda _socket, server_name, _context: doh_sni.append(server_name)
        )
        destination_tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        destination_tls_context.load_cert_chain(cert_path, key_path)
        destination_tls_context.set_servername_callback(
            lambda _socket, server_name, _context: destination_sni.append(server_name)
        )
        client_tls_context = ssl.create_default_context(cafile=str(cert_path))
        real_httpx_client = httpx.Client
        client_options = []

        def client_trusting_test_certificate(**kwargs):
            client_options.append(dict(kwargs))
            kwargs.setdefault("verify", client_tls_context)
            return real_httpx_client(**kwargs)

        real_getaddrinfo = socket.getaddrinfo
        system_dns_hosts = []

        def reject_destination_system_dns(host, *args, **kwargs):
            normalized_host = host.decode() if isinstance(host, bytes) else host
            system_dns_hosts.append(normalized_host)
            if normalized_host == "integration.example":
                raise AssertionError("destination hostname reached the system resolver")
            return real_getaddrinfo(host, *args, **kwargs)

        with (
            _running_test_server(
                DestinationHandler, tls_context=destination_tls_context
            ) as destination,
            _running_test_server(DoHHandler, tls_context=doh_tls_context) as doh,
        ):
            hermes_home = tmp_path / "hermes-home"
            hermes_home.mkdir()
            (hermes_home / "config.yaml").write_text(
                "security:\n"
                "  allow_private_urls: true\n"
                f"  url_safety_doh_url: https://localhost:{doh.server_port}/dns-query\n"
                "  url_safety_doh_timeout: 2\n",
                encoding="utf-8",
            )
            monkeypatch.setenv("HERMES_HOME", str(hermes_home))
            monkeypatch.setattr(httpx, "Client", client_trusting_test_certificate)
            monkeypatch.setattr(socket, "getaddrinfo", reject_destination_system_dns)
            _reset_allow_private_cache()
            target_url = (
                f"https://integration.example:{destination.server_port}/payload"
            )
            try:
                assert is_safe_url(target_url) is True
                with create_ssrf_safe_client(
                    trust_env=False, timeout=5, verify=client_tls_context
                ) as client:
                    sync_response = client.get(target_url)

                async def async_roundtrip():
                    assert await async_is_safe_url(target_url) is True
                    async with create_ssrf_safe_async_client(
                        trust_env=False, timeout=5, verify=client_tls_context
                    ) as client:
                        return await client.get(target_url)

                async_response = asyncio.run(async_roundtrip())
            finally:
                _reset_allow_private_cache()

        assert sync_response.status_code == 200
        assert sync_response.text == "validated destination"
        assert async_response.status_code == 200
        assert async_response.text == "validated destination"
        assert doh_queries == [
            (
                "/dns-query",
                "integration.example",
                query_type,
                f"localhost:{doh.server_port}",
            )
            for _resolution_pass in range(4)
            for query_type in ("A", "AAAA")
        ]
        assert doh_sni == ["localhost"] * len(doh_queries)
        assert "integration.example" not in system_dns_hosts
        assert destination_requests == [
            ("/payload", f"integration.example:{destination.server_port}"),
            ("/payload", f"integration.example:{destination.server_port}"),
        ]
        assert destination_sni == ["integration.example", "integration.example"]
        assert any(
            options.get("follow_redirects") is False
            and options.get("trust_env") is False
            for options in client_options
        )

    def test_connect_resolution_uses_configured_doh(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
                "url_safety_doh_timeout": 5,
            }
        }
        fake_ip_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.1.10", 443)),
        ]

        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                return_value=["93.184.216.34"],
            ) as doh_resolve,
            patch("socket.getaddrinfo", return_value=fake_ip_answer),
        ):
            ips = _resolved_http_connect_ips("example.com", 443, "https")

        assert ips == ["93.184.216.34"]
        doh_resolve.assert_called_once_with(
            "example.com", "https://1.1.1.1/dns-query", 5.0
        )

    def test_connect_doh_transport_failure_is_blocked_without_dns_fallback(self):
        import httpx

        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }

        class FailingClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, *, params, headers):
                request = httpx.Request("GET", url)
                raise httpx.ConnectError("DoH unavailable", request=request)

        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch("httpx.Client", FailingClient),
            patch("socket.getaddrinfo") as system_resolve,
        ):
            with pytest.raises(SSRFConnectionBlocked, match="DNS resolution failed"):
                _resolved_http_connect_ips("example.com", 443, "https")

        system_resolve.assert_not_called()

    def test_connect_resolution_caps_safe_ip_candidates(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"93.184.216.{idx}", 80))
            for idx in range(1, _MAX_SSRF_CONNECT_IPS + 4)
        ]

        with patch("socket.getaddrinfo", return_value=answers):
            ips = _resolved_http_connect_ips("example.com", 80, "http")

        assert len(ips) == _MAX_SSRF_CONNECT_IPS
        assert ips[0] == "93.184.216.1"
        assert ips[-1] == f"93.184.216.{_MAX_SSRF_CONNECT_IPS}"

    def test_connect_resolution_checks_private_ip_beyond_candidate_cap(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"93.184.216.{idx}", 80))
            for idx in range(1, _MAX_SSRF_CONNECT_IPS + 1)
        ]
        answers.append(
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))
        )

        with patch("socket.getaddrinfo", return_value=answers):
            with pytest.raises(SSRFConnectionBlocked, match="metadata"):
                _resolved_http_connect_ips("example.com", 80, "http")

    @pytest.mark.asyncio
    async def test_async_client_dials_validated_ip_not_hostname(self, monkeypatch):
        """Direct httpx fetches should connect to the vetted IP, not re-resolve hostnames."""
        import httpcore
        from httpcore._backends.auto import AutoBackend

        for proxy_var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.delenv(proxy_var, raising=False)

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            ],
        )

        connect_attempts = []

        async def fake_connect_tcp(
            self,
            host,
            port,
            timeout=None,
            local_address=None,
            socket_options=None,
        ):
            connect_attempts.append((host, port))
            raise httpcore.ConnectError("stop before network")

        monkeypatch.setattr(AutoBackend, "connect_tcp", fake_connect_tcp)

        async with create_ssrf_safe_async_client(timeout=0.01, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://example.com/image.png")

        assert connect_attempts == [("93.184.216.34", 80)]

    @pytest.mark.asyncio
    async def test_async_backend_blocks_unix_socket_connects(self):
        import contextvars

        backend = _SSRFGuardedAsyncNetworkBackend(contextvars.ContextVar("test_schemes"))

        with pytest.raises(SSRFConnectionBlocked, match="Unix socket"):
            await backend.connect_unix_socket("/tmp/hermes.sock")

    def test_async_client_rejects_unpatchable_custom_transport(self):
        class CustomTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, request=request)

        with pytest.raises(SSRFConnectionBlocked, match="Unsupported async httpx transport"):
            create_ssrf_safe_async_client(transport=CustomTransport())

    def test_sync_client_rejects_custom_http_transport_subclass_mount(self):
        class DelegatingTransport(httpx.HTTPTransport):
            def handle_request(self, request):
                raise AssertionError("custom transport must never execute")

        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(mounts={"all://": DelegatingTransport()})

    def test_async_client_rejects_custom_http_transport_subclass_mount(self):
        class DelegatingTransport(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request):
                raise AssertionError("custom transport must never execute")

        with pytest.raises(SSRFConnectionBlocked, match="Unsupported async httpx transport"):
            create_ssrf_safe_async_client(mounts={"all://": DelegatingTransport()})

    def test_sync_client_rejection_does_not_mutate_primary_transport(self):
        class UnsupportedTransport(httpx.BaseTransport):
            def handle_request(self, request):
                return httpx.Response(200, request=request)

        primary = httpx.HTTPTransport()
        original_backend = primary._pool._network_backend
        original_close = primary.close
        closed = False

        def track_close():
            nonlocal closed
            closed = True
            original_close()

        primary.close = track_close
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(
                transport=primary,
                mounts=UserDict({"all://": UnsupportedTransport()}),
            )

        assert closed is False
        assert primary._pool._network_backend is original_backend
        assert not getattr(primary, "_hermes_ssrf_guarded", False)
        assert "handle_request" not in primary.__dict__

    def test_async_client_rejection_does_not_mutate_primary_transport(self):
        class UnsupportedAsyncTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, request=request)

        primary = httpx.AsyncHTTPTransport()
        original_backend = primary._pool._network_backend
        original_aclose = primary.aclose
        closed = False

        async def track_aclose():
            nonlocal closed
            closed = True
            await original_aclose()

        primary.aclose = track_aclose
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported async httpx transport"):
            create_ssrf_safe_async_client(
                transport=primary,
                mounts=UserDict({"all://": UnsupportedAsyncTransport()}),
            )

        assert closed is False
        assert primary._pool._network_backend is original_backend
        assert not getattr(primary, "_hermes_ssrf_guarded", False)
        assert "handle_async_request" not in primary.__dict__

    def test_client_rejects_spoofed_proxy_pool(self):
        fake_proxy_type = type("HTTPProxy", (), {"__module__": "httpcore"})
        transport = httpx.HTTPTransport()
        transport._pool = fake_proxy_type()

        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(mounts={"all://": transport})

    def test_sync_client_rejects_proxy_transport_instance_override(self):
        transport = httpx.HTTPTransport(proxy="http://proxy.example:8080")

        def bypass(request):
            return httpx.Response(200, request=request)

        transport.handle_request = bypass

        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(mounts={"all://": transport})

    def test_async_client_rejects_proxy_transport_instance_override(self):
        transport = httpx.AsyncHTTPTransport(proxy="http://proxy.example:8080")

        async def bypass(request):
            return httpx.Response(200, request=request)

        transport.handle_async_request = bypass
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported async httpx transport"):
            create_ssrf_safe_async_client(mounts={"all://": transport})

    def test_client_rejects_spoofed_guard_marker(self):
        transport = httpx.HTTPTransport()
        transport._hermes_ssrf_guarded = True

        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(mounts={"all://": transport})

    def test_client_rejects_direct_pool_instance_override(self):
        transport = httpx.HTTPTransport()

        def bypass(request):
            return httpx.Response(200, request=request)

        transport._pool.handle_request = bypass
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(mounts={"all://": transport})

    def test_client_rejects_proxy_pool_instance_override(self):
        transport = httpx.HTTPTransport(proxy="http://proxy.example:8080")

        def bypass(request):
            return httpx.Response(200, request=request)

        transport._pool.handle_request = bypass
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(mounts={"all://": transport})

    def test_async_client_rejects_proxy_pool_instance_override(self):
        transport = httpx.AsyncHTTPTransport(proxy="http://proxy.example:8080")

        async def bypass(request):
            return httpx.Response(200, request=request)

        transport._pool.handle_async_request = bypass
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported async httpx transport"):
            create_ssrf_safe_async_client(mounts={"all://": transport})

    @pytest.mark.asyncio
    async def test_async_client_ignores_environment_proxy_mounts(self, monkeypatch):
        """Ambient proxy variables cannot move resolution outside the guard."""
        for proxy_var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            monkeypatch.delenv(proxy_var, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

        client = create_ssrf_safe_async_client(timeout=0.01)
        try:
            proxy_transports = [
                transport
                for transport in client.__dict__.get("_mounts", {}).values()
                if transport is not None
            ]
            assert proxy_transports == []
            assert type(client._transport._pool._network_backend).__name__ == (
                "_SSRFGuardedAsyncNetworkBackend"
            )
        finally:
            await client.aclose()

    def test_sync_client_guards_direct_mount_but_trusts_explicit_proxy_mount(self):
        direct_transport = httpx.HTTPTransport()
        proxy_transport = httpx.HTTPTransport(proxy="http://proxy.example:8080")
        client = create_ssrf_safe_client(
            mounts={"http://": direct_transport, "https://": proxy_transport}
        )
        try:
            assert direct_transport._hermes_ssrf_guarded is True
            assert type(direct_transport._pool._network_backend).__name__ == (
                "_SSRFGuardedNetworkBackend"
            )
            assert not getattr(proxy_transport, "_hermes_ssrf_guarded", False)
            assert type(proxy_transport._pool).__name__ == "HTTPProxy"
        finally:
            client.close()

    @pytest.mark.parametrize(
        "proxy_url,pool_name",
        [
            ("http://proxy.example:8080", "HTTPProxy"),
            ("socks5://proxy.example:1080", "SOCKSProxy"),
        ],
    )
    def test_sync_client_trusts_explicit_primary_proxy_transport(
        self, proxy_url, pool_name
    ):
        transport = httpx.HTTPTransport(proxy=proxy_url)
        client = create_ssrf_safe_client(transport=transport)
        try:
            assert client._transport is transport
            assert type(transport._pool).__name__ == pool_name
            assert not getattr(transport, "_hermes_ssrf_guarded", False)
        finally:
            client.close()

    @pytest.mark.parametrize("placement", ["primary", "mount"])
    def test_sync_client_rejects_async_proxy_transport(self, placement):
        transport = httpx.AsyncHTTPTransport(proxy="http://proxy.example:8080")
        kwargs = (
            {"transport": transport}
            if placement == "primary"
            else {"mounts": {"all://": transport}}
        )
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported httpx transport"):
            create_ssrf_safe_client(**kwargs)

    @pytest.mark.asyncio
    async def test_async_client_guards_direct_mount_but_trusts_explicit_proxy_mount(
        self,
    ):
        direct_transport = httpx.AsyncHTTPTransport()
        proxy_transport = httpx.AsyncHTTPTransport(
            proxy="http://proxy.example:8080"
        )
        client = create_ssrf_safe_async_client(
            mounts={"http://": direct_transport, "https://": proxy_transport}
        )
        try:
            assert direct_transport._hermes_ssrf_guarded is True
            assert type(direct_transport._pool._network_backend).__name__ == (
                "_SSRFGuardedAsyncNetworkBackend"
            )
            assert not getattr(proxy_transport, "_hermes_ssrf_guarded", False)
            assert type(proxy_transport._pool).__name__ == "AsyncHTTPProxy"
        finally:
            await client.aclose()

    @pytest.mark.parametrize(
        "proxy_url,pool_name",
        [
            ("http://proxy.example:8080", "AsyncHTTPProxy"),
            ("socks5://proxy.example:1080", "AsyncSOCKSProxy"),
        ],
    )
    @pytest.mark.asyncio
    async def test_async_client_trusts_explicit_primary_proxy_transport(
        self, proxy_url, pool_name
    ):
        transport = httpx.AsyncHTTPTransport(proxy=proxy_url)
        client = create_ssrf_safe_async_client(transport=transport)
        try:
            assert client._transport is transport
            assert type(transport._pool).__name__ == pool_name
            assert not getattr(transport, "_hermes_ssrf_guarded", False)
        finally:
            await client.aclose()

    @pytest.mark.parametrize("placement", ["primary", "mount"])
    def test_async_client_rejects_sync_proxy_transport(self, placement):
        transport = httpx.HTTPTransport(proxy="http://proxy.example:8080")
        kwargs = (
            {"transport": transport}
            if placement == "primary"
            else {"mounts": {"all://": transport}}
        )
        with pytest.raises(SSRFConnectionBlocked, match="Unsupported async httpx transport"):
            create_ssrf_safe_async_client(**kwargs)


class TestIsBlockedIp:
    """Direct tests for the _is_blocked_ip helper."""

    @pytest.mark.parametrize("ip_str", [
        "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
        "169.254.169.254", "0.0.0.0", "224.0.0.1", "255.255.255.255",
        "100.64.0.1", "100.100.100.100", "100.127.255.254", "198.18.0.23",
        "::1", "fe80::1", "fc00::1", "fd12::1", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    ])
    def test_blocked_ips(self, ip_str):
        ip = ipaddress.ip_address(ip_str)
        assert _is_blocked_ip(ip) is True, f"{ip_str} should be blocked"

    @pytest.mark.parametrize("ip_str", [
        "8.8.8.8", "93.184.216.34", "1.1.1.1", "100.0.0.1",
        "2606:4700::1", "2001:4860:4860::8888",
    ])
    def test_allowed_ips(self, ip_str):
        ip = ipaddress.ip_address(ip_str)
        assert _is_blocked_ip(ip) is False, f"{ip_str} should be allowed"


class TestGlobalAllowPrivateUrls:
    """Tests for the security.allow_private_urls config toggle."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """Reset the module-level toggle cache before and after each test."""
        _reset_allow_private_cache()
        yield
        _reset_allow_private_cache()

    def test_default_is_false(self, monkeypatch):
        """Toggle defaults to False when no env var or config is set."""
        monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
        with patch("hermes_cli.config.read_raw_config", side_effect=Exception("no config")):
            assert _global_allow_private_urls() is False

    def test_env_var_true(self, monkeypatch):
        """HERMES_ALLOW_PRIVATE_URLS=true enables the toggle."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        assert _global_allow_private_urls() is True

    def test_env_var_1(self, monkeypatch):
        """HERMES_ALLOW_PRIVATE_URLS=1 enables the toggle."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "1")
        assert _global_allow_private_urls() is True

    def test_env_var_yes(self, monkeypatch):
        """HERMES_ALLOW_PRIVATE_URLS=yes enables the toggle."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "yes")
        assert _global_allow_private_urls() is True

    def test_env_var_false(self, monkeypatch):
        """HERMES_ALLOW_PRIVATE_URLS=false keeps it disabled."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "false")
        assert _global_allow_private_urls() is False

    def test_config_security_section(self, monkeypatch):
        """security.allow_private_urls in config enables the toggle."""
        monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
        cfg = {"security": {"allow_private_urls": True}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _global_allow_private_urls() is True

    def test_config_browser_fallback(self, monkeypatch):
        """browser.allow_private_urls works as legacy fallback."""
        monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
        cfg = {"browser": {"allow_private_urls": True}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _global_allow_private_urls() is True

    def test_config_security_string_false_stays_disabled(self, monkeypatch):
        """Quoted false must not opt out of SSRF protection."""
        monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
        cfg = {"security": {"allow_private_urls": "false"}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _global_allow_private_urls() is False

    def test_config_browser_string_false_stays_disabled(self, monkeypatch):
        """Legacy browser.allow_private_urls also normalises quoted false."""
        monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
        cfg = {"browser": {"allow_private_urls": "false"}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _global_allow_private_urls() is False

    def test_config_security_takes_precedence_over_browser(self, monkeypatch):
        """security section is checked before browser section."""
        monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
        cfg = {"security": {"allow_private_urls": True}, "browser": {"allow_private_urls": False}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _global_allow_private_urls() is True

    def test_env_var_overrides_config(self, monkeypatch):
        """Env var takes priority over config."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "false")
        cfg = {"security": {"allow_private_urls": True}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _global_allow_private_urls() is False

    def test_result_is_cached(self, monkeypatch):
        """Second call uses cached result, doesn't re-read config."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        assert _global_allow_private_urls() is True
        # Change env after first call — should still be True (cached)
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "false")
        assert _global_allow_private_urls() is True


class TestAllowPrivateUrlsIntegration:
    """Integration tests: is_safe_url respects the global toggle."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        _reset_allow_private_cache()
        yield
        _reset_allow_private_cache()

    def test_private_ip_allowed_when_toggle_on(self, monkeypatch):
        """Private IPs pass is_safe_url when toggle is enabled."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.1.1", 0)),
        ]):
            assert is_safe_url("http://router.local") is True

    def test_benchmark_ip_allowed_when_toggle_on(self, monkeypatch):
        """198.18.x.x (benchmark/OpenWrt proxy range) passes when toggle is on."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.18.23.183", 0)),
        ]):
            assert is_safe_url("https://nousresearch.com") is True

    def test_cgnat_allowed_when_toggle_on(self, monkeypatch):
        """CGNAT range (100.64.0.0/10) passes when toggle is on."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("100.100.100.100", 0)),
        ]):
            assert is_safe_url("http://tailscale-peer.example/") is True

    def test_localhost_allowed_when_toggle_on(self, monkeypatch):
        """Even localhost passes when toggle is on."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            assert is_safe_url("http://localhost:8080/api") is True

    # --- Cloud metadata always blocked regardless of toggle ---

    def test_metadata_hostname_blocked_even_with_toggle(self, monkeypatch):
        """metadata.google.internal is ALWAYS blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        assert is_safe_url("http://metadata.google.internal/computeMetadata/v1/") is False

    def test_metadata_goog_blocked_even_with_toggle(self, monkeypatch):
        """metadata.goog is ALWAYS blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        assert is_safe_url("http://metadata.goog/computeMetadata/v1/") is False

    def test_metadata_ip_blocked_even_with_toggle(self, monkeypatch):
        """169.254.169.254 (AWS/GCP metadata IP) is ALWAYS blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ]):
            assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False

    def test_metadata_ipv6_blocked_even_with_toggle(self, monkeypatch):
        """fd00:ec2::254 (AWS IPv6 metadata) is ALWAYS blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("fd00:ec2::254", 0, 0, 0)),
        ]):
            assert is_safe_url("http://[fd00:ec2::254]/latest/") is False

    def test_ecs_metadata_blocked_even_with_toggle(self, monkeypatch):
        """169.254.170.2 (AWS ECS task metadata) is ALWAYS blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.170.2", 0)),
        ]):
            assert is_safe_url("http://169.254.170.2/v2/credentials") is False

    def test_alibaba_metadata_blocked_even_with_toggle(self, monkeypatch):
        """100.100.100.200 (Alibaba Cloud metadata) is ALWAYS blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("100.100.100.200", 0)),
        ]):
            assert is_safe_url("http://100.100.100.200/latest/meta-data/") is False

    def test_azure_wire_server_blocked_even_with_toggle(self, monkeypatch):
        """169.254.169.253 (Azure IMDS wire server) is ALWAYS blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.169.253", 0)),
        ]):
            assert is_safe_url("http://169.254.169.253/") is False

    def test_entire_link_local_blocked_even_with_toggle(self, monkeypatch):
        """Any 169.254.x.x address is ALWAYS blocked (entire link-local range)."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.42.99", 0)),
        ]):
            assert is_safe_url("http://169.254.42.99/anything") is False

    def test_dns_failure_still_blocked_with_toggle(self, monkeypatch):
        """DNS failures are still blocked even with toggle on."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(var, raising=False)
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("fail")):
            assert is_safe_url("https://nonexistent.example.com") is False

    def test_empty_url_still_blocked_with_toggle(self, monkeypatch):
        """Empty URLs are still blocked."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        assert is_safe_url("") is False


class TestIsAlwaysBlockedUrl:
    """The always-blocked floor — cloud metadata only, narrower than is_safe_url."""

    # -- The sentinel set that must always block --------------------------------

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",            # AWS / GCP / Azure / DO / Oracle
        "http://169.254.169.253/metadata/instance",              # Azure IMDS wire server
        "http://169.254.170.2/v2/credentials",                   # AWS ECS task metadata
        "http://100.100.100.200/latest/meta-data/",              # Alibaba Cloud
        "http://169.254.42.1/",                                  # Any /16 link-local
    ])
    def test_literal_imds_ips_always_blocked(self, url):
        """Literal IMDS IPs and the /16 link-local range always block."""
        assert is_always_blocked_url(url) is True

    def test_gcp_metadata_hostname_always_blocked_even_without_dns(self):
        """metadata.google.internal blocks by hostname, no DNS needed."""
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            assert is_always_blocked_url("http://metadata.google.internal/") is True

    def test_hostname_resolving_to_imds_always_blocked(self):
        """Attacker-controlled hostname resolving to IMDS still blocks."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ]):
            assert is_always_blocked_url("http://attacker-controlled.example.com/") is True

    def test_hostname_resolving_to_imds_via_configured_doh_always_blocked(self):
        config = {
            "security": {
                "url_safety_doh_url": "https://1.1.1.1/dns-query",
            }
        }
        fake_ip_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.1.10", 0)),
        ]
        with (
            patch("hermes_cli.config.read_raw_config", return_value=config),
            patch(
                "tools.url_safety._resolve_hostname_via_doh",
                return_value=["169.254.169.254"],
            ),
            patch("socket.getaddrinfo", return_value=fake_ip_answer),
        ):
            assert (
                is_always_blocked_url("http://attacker-controlled.example.com/") is True
            )

    def test_scope_id_imds_in_floor_blocked(self):
        """A scope-ID suffix on an IPv4-mapped IMDS address resolving in the
        always-blocked floor must be caught after the scope is stripped, not
        skipped as unparseable."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::ffff:169.254.169.254%eth0", 0, 0, 0)),
        ]):
            assert is_always_blocked_url("http://attacker-controlled.example.com/") is True

    # -- Things the floor must NOT block ----------------------------------------

    def test_public_url_not_blocked(self):
        assert is_always_blocked_url("https://example.com/path") is False

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8080/",
        "http://192.168.1.1/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://100.64.0.1/",  # CGNAT — blocked by is_safe_url but not by the floor
    ])
    def test_ordinary_private_urls_not_in_floor(self, url):
        """Floor is narrower than is_safe_url — ordinary private URLs pass."""
        assert is_always_blocked_url(url) is False

    def test_dns_failure_not_in_floor(self):
        """DNS failure on a non-sentinel hostname = not always-blocked.

        Caller's ordinary fail-closed path (is_safe_url) handles that case.
        """
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("fail")):
            assert is_always_blocked_url("http://nonexistent.example.com/") is False

    def test_empty_url_not_in_floor(self):
        """Empty URL falls through — caller decides what to do with a malformed URL."""
        assert is_always_blocked_url("") is False

    def test_malformed_url_not_in_floor(self):
        """Parse errors don't claim always-blocked status."""
        assert is_always_blocked_url("not a url at all") is False

    def test_floor_ignores_allow_private_urls_toggle(self, monkeypatch):
        """security.allow_private_urls can NOT unblock cloud metadata."""
        monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
        assert is_always_blocked_url("http://169.254.169.254/") is True


class TestIPv4MappedIPv6SSRF:
    """Regression tests for SSRF bypass via IPv4-mapped IPv6 addresses.

    DNS resolvers may return ``::ffff:x.x.x.x`` for IPv4-only hosts.
    Python's ipaddress module treats these as distinct from the plain
    IPv4 address, so ``ip in frozenset({IPv4Address(...)})`` and
    ``ip in IPv4Network(...)`` both return False.  Without explicit
    handling, an attacker could use IPv4-mapped addresses to bypass
    all SSRF protections.
    """

    # ── _is_blocked_ip direct tests ──

    @pytest.mark.parametrize("ip_str", [
        "::ffff:100.64.0.1",       # CGNAT start
        "::ffff:100.100.100.200",  # Alibaba Cloud metadata (in CGNAT range)
        "::ffff:100.127.255.254",  # CGNAT end
        "::ffff:169.254.42.99",    # Link-local (non-metadata)
        "::ffff:0.0.0.0",          # Unspecified
        "::ffff:224.0.0.1",        # Multicast
    ])
    def test_ipv4_mapped_blocked_ips(self, ip_str):
        """IPv4-mapped IPv6 addresses that should be blocked."""
        ip = ipaddress.ip_address(ip_str)
        assert _is_blocked_ip(ip) is True, f"{ip_str} should be blocked"

    @pytest.mark.parametrize("ip_str", [
        "::ffff:8.8.8.8",          # Public DNS
        "::ffff:93.184.216.34",    # example.com
        "::ffff:100.0.0.1",        # Not in CGNAT range
    ])
    def test_ipv4_mapped_allowed_ips(self, ip_str):
        """IPv4-mapped IPv6 addresses that should be allowed."""
        ip = ipaddress.ip_address(ip_str)
        assert _is_blocked_ip(ip) is False, f"{ip_str} should be allowed"

    # ── is_safe_url integration tests: always-blocked metadata IPs ──

    def test_ipv4_mapped_aws_metadata_blocked(self):
        """::ffff:169.254.169.254 (AWS metadata) must always be blocked."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::ffff:169.254.169.254", 0, 0, 0)),
        ]):
            assert is_safe_url("http://aws-metadata.internal/") is False

    def test_ipv4_mapped_ecs_metadata_blocked(self):
        """::ffff:169.254.170.2 (AWS ECS task metadata) must always be blocked."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::ffff:169.254.170.2", 0, 0, 0)),
        ]):
            assert is_safe_url("http://ecs-metadata.internal/") is False

    def test_ipv4_mapped_azure_wire_server_blocked(self):
        """::ffff:169.254.169.253 (Azure IMDS wire server) must always be blocked."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::ffff:169.254.169.253", 0, 0, 0)),
        ]):
            assert is_safe_url("http://azure-metadata.internal/") is False

    def test_ipv4_mapped_alibaba_metadata_blocked(self):
        """::ffff:100.100.100.200 (Alibaba Cloud metadata) must always be blocked."""
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::ffff:100.100.100.200", 0, 0, 0)),
        ]):
            assert is_safe_url("http://aliyun-metadata.internal/") is False


class _FakeResponse:
    """Minimal stand-in for an httpx response as seen inside a response hook."""

    def __init__(self, *, is_redirect, location=None, url="", next_request=None):
        self.is_redirect = is_redirect
        self.headers = {"location": location} if location else {}
        self.url = url
        self.next_request = next_request


class _FakeNextRequest:
    def __init__(self, url):
        self.url = url


class TestRedirectTargetFromResponse:
    """redirect_target_from_response is the SSRF-guard boundary for httpx hooks.

    Inside httpx AsyncClient response hooks, ``response.next_request`` is often
    ``None`` even for a real redirect, so a guard keyed only on it silently
    never fires. Resolving from the ``Location`` header closes that hole.
    """

    def test_absolute_location_without_next_request(self):
        # The exact bypass: redirect present, next_request unset, private target.
        resp = _FakeResponse(
            is_redirect=True,
            location="http://169.254.169.254/latest/meta-data",
            url="https://public.example/image.png",
        )
        assert (
            redirect_target_from_response(resp)
            == "http://169.254.169.254/latest/meta-data"
        )

    def test_relative_location_is_resolved_against_response_url(self):
        resp = _FakeResponse(
            is_redirect=True,
            location="/redir",
            url="https://public.example/image.png",
        )
        assert redirect_target_from_response(resp) == "https://public.example/redir"

    def test_non_redirect_returns_none(self):
        resp = _FakeResponse(is_redirect=False, location="http://169.254.169.254/")
        assert redirect_target_from_response(resp) is None

    def test_falls_back_to_next_request_when_no_location(self):
        resp = _FakeResponse(
            is_redirect=True,
            next_request=_FakeNextRequest("http://10.0.0.1/meta"),
        )
        assert redirect_target_from_response(resp) == "http://10.0.0.1/meta"

    def test_no_location_no_next_request_returns_none(self):
        resp = _FakeResponse(is_redirect=True)
        assert redirect_target_from_response(resp) is None
