"""Tests for agent.ssl_verify.resolve_httpx_verify and _is_local_host."""

import ipaddress
import ssl

import certifi
import pytest

from agent.ssl_verify import _is_local_host, resolve_httpx_verify

_CA_ENV_VARS = ("HERMES_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


@pytest.fixture
def clean_ca_env(monkeypatch):
    for var in _CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)




def test_hermes_ca_bundle_returns_ssl_context(clean_ca_env, monkeypatch):
    monkeypatch.setenv("HERMES_CA_BUNDLE", certifi.where())
    result = resolve_httpx_verify()
    assert isinstance(result, ssl.SSLContext)






def test_default_without_env_is_true(clean_ca_env):
    assert resolve_httpx_verify() is True


def test_ssl_verify_false_allowed_for_local_host(clean_ca_env):
    assert resolve_httpx_verify(ssl_verify=False, base_url="http://localhost:11434/v1") is False
    assert resolve_httpx_verify(ssl_verify=False, base_url="https://127.0.0.1:8443/v1") is False
    assert resolve_httpx_verify(ssl_verify=False, base_url="http://ollama:11434/v1") is False


def test_ssl_verify_false_keeps_legacy_behavior_without_base_url(clean_ca_env):
    # Callers that don't know the base_url keep the historical warn-and-proceed
    # behavior so genuinely local setups whose URL is resolved later still work.
    assert resolve_httpx_verify(ssl_verify=False) is False


def test_ssl_verify_false_refused_for_public_host(clean_ca_env):
    with pytest.raises(ValueError, match="public endpoint"):
        resolve_httpx_verify(ssl_verify=False, base_url="https://api.openai.com/v1")
    with pytest.raises(ValueError, match="public endpoint"):
        resolve_httpx_verify(ssl_verify=False, base_url="https://ollama.example.com/v1")


def test_ssl_verify_false_refused_for_ip_lookalike_public_dns(clean_ca_env):
    # The historical string-prefix bug classified these as local; the
    # ``ssl_verify: false`` escape hatch must not apply to them.
    for host in (
        "127.attacker.example",
        "10.attacker.example",
        "172.16.attacker.example",
        "172.31.attacker.example",
        "100.64.attacker.example",
    ):
        with pytest.raises(ValueError, match="public endpoint"):
            resolve_httpx_verify(ssl_verify=False, base_url=f"https://{host}:8443/v1")


# ---------------------------------------------------------------------------
# _is_local_host — exact literal-address classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        # loopback
        "127.0.0.1",
        "127.0.0.0",
        "127.255.255.255",
        # RFC1918 10/8
        "10.0.0.0",
        "10.0.0.1",
        "10.255.255.255",
        # RFC1918 172.16/12
        "172.16.0.0",
        "172.16.0.1",
        "172.31.255.255",
        # RFC1918 192.168/16
        "192.168.0.0",
        "192.168.0.1",
        "192.168.255.255",
        # link-local 169.254/16
        "169.254.0.0",
        "169.254.0.1",
        "169.254.255.255",
        # RFC6598 shared 100.64/10
        "100.64.0.0",
        "100.64.0.1",
        "100.127.255.255",
        # unspecified / this-host
        "0.0.0.0",
        # IPv6 loopback + unspecified
        "::1",
        "::",
        # IPv6 unique-local fc00::/7
        "fc00::",
        "fd00::1",
        "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        # IPv6 link-local fe80::/10
        "fe80::",
        "fe80::1",
        "febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        # IPv6 link-local with zone id
        "fe80::1%eth0",
        # IPv4-mapped IPv6 of a local IPv4
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:172.16.0.1",
        "::ffff:192.168.0.1",
        "::ffff:100.64.0.1",
        # exact localhost name + single-label LAN/container names
        "localhost",
        "localhost.",
        "ollama",
        "vllm",
        "llama-cpp",
    ],
)
def test_is_local_host_true(host):
    assert _is_local_host(host) is True, f"expected {host!r} to be local"


@pytest.mark.parametrize(
    "host",
    [
        # just outside every private/link-local/shared range
        "128.0.0.1",  # past loopback 127/8
        "11.0.0.0",  # past 10/8
        "172.15.255.255",  # below 172.16/12
        "172.32.0.0",  # past 172.16/12
        "192.167.255.255",  # below 192.168/16
        "192.169.0.0",  # past 192.168/16
        "169.253.255.255",  # below link-local
        "169.255.0.0",  # past link-local
        "100.63.255.255",  # below RFC6598
        "100.128.0.0",  # past RFC6598
        # public IPv4
        "8.8.8.8",
        "1.1.1.1",
        "93.184.215.14",
        # IPv6 just outside local ranges
        "fe00::",  # below ULA fc00::/7 and link-local fe80::/10
        "fec0::",  # deprecated site-local — NOT treated as operator-local here
        "ff00::1",  # multicast
        "2001:db8::1",  # documentation range
        "2606:4700:4700::1111",  # public (Cloudflare)
        "2001:4860:4860::8888",  # public (Google)
        # IPv4-mapped IPv6 of a PUBLIC IPv4
        "::ffff:8.8.8.8",
        "::ffff:93.184.215.14",
        # DNS names that merely LOOK like a private address — the historical
        # string-prefix bug classified these as local; they must not be.
        "127.attacker.example",
        "127.0.0.1.attacker.example",
        "10.attacker.example",
        "172.16.attacker.example",
        "172.31.attacker.example",
        "172.16.0.1.attacker.example",
        "192.168.attacker.example",
        "169.254.attacker.example",
        "100.64.attacker.example",
        "0.0.0.0.attacker.example",
        # IPv6-prefix-lookalike DNS names
        "fc00.attacker.example",
        "fd00.attacker.example",
        "fe80.example.com",
        "fe8.example.com",
        # other dotted public hostnames
        "localhost.example.com",
        "ollama.example.com",
        "api.openai.com",
        "example.com",
        # empty / absent
        "",
        None,
    ],
)
def test_is_local_host_false(host):
    assert _is_local_host(host) is False, f"expected {host!r} NOT to be local"


def test_is_local_host_consistent_with_ipaddress_properties():
    # Literals classify identically to ipaddress' own network properties where
    # those exist on this interpreter (is_shared is absent pre-3.11, so the
    # RFC6598 range is pinned explicitly) — the classification tracks the
    # standard, not an ad-hoc prefix list.
    rfc6598 = ipaddress.ip_network("100.64.0.0/10")

    for host in (
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.1.1",
        "100.64.1.1",
        "0.0.0.0",
        "::1",
        "::",
        "fc00::1",
        "fd00::1",
        "fe80::1",
    ):
        addr = ipaddress.ip_address(host)
        local = (
            addr.is_loopback
            or addr.is_unspecified
            or addr.is_link_local
            or (isinstance(addr, ipaddress.IPv4Address) and addr.is_private)
            or addr in rfc6598
            or (isinstance(addr, ipaddress.IPv6Address) and addr in ipaddress.ip_network("fc00::/7"))
        )
        assert _is_local_host(host) is local, f"mismatch for {host!r}"
