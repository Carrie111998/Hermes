"""Probe stubs must never reach a runtime consumer via the client cache.

Regression coverage for the compression abort:

    Compression aborted: 984 messages preserved
    Reason: _AuxProbeClientStub used as a real client (attribute 'close');
            aux_probe_mode is for availability checks only

`aux_probe_mode()` makes client constructors return a lightweight
`_AuxProbeClientStub` so tool-gating check_fns can answer "is this provider
resolvable?" without paying for real SDK construction. The stub is explicitly
documented as never-cached. `_store_cached_client` enforced that, but
`_get_cached_client` writes `_client_cache` directly and did not — so a
check_fn probing under probe mode could seed the cache, and the next real
consumer (context compression) got a non-functional stub on the cache hit.

These are behaviour contracts, not snapshots: they assert the invariant
("a probe never publishes into the cache"; "a stub is inert to teardown
inspection but loud on real use") rather than freezing any particular
cache key, provider list, or call count.
"""

import pytest

import agent.auxiliary_client as ac
from agent.auxiliary_client import _AuxProbeClientStub, aux_probe_mode


@pytest.fixture(autouse=True)
def _clean_client_cache():
    """Each test owns the cache; never leak entries across tests."""
    with ac._client_cache_lock:
        saved = dict(ac._client_cache)
        ac._client_cache.clear()
    try:
        yield
    finally:
        with ac._client_cache_lock:
            ac._client_cache.clear()
            ac._client_cache.update(saved)


@pytest.fixture
def stub_resolver(monkeypatch):
    """resolve_provider_client returns a stub in probe mode, a real object otherwise.

    Mirrors the real constructors (`_create_openai_client`,
    `_try_anthropic_native`, …), which all branch on `_aux_probe_active()`.
    """
    sentinel = object()

    def fake_resolve(provider, model, async_mode, **kwargs):
        if ac._aux_probe_active():
            return _AuxProbeClientStub(api_key="k", base_url="https://example.invalid"), model
        return sentinel, model

    monkeypatch.setattr(ac, "resolve_provider_client", fake_resolve)
    return sentinel


def test_probe_does_not_publish_stub_into_client_cache(stub_resolver):
    """A probe-mode resolution must leave the shared cache stub-free."""
    with aux_probe_mode():
        client, _ = ac._get_cached_client("anthropic", model="m")

    assert isinstance(client, _AuxProbeClientStub), "probe should still get its stub"
    cached = [entry[0] for entry in ac._client_cache.values()]
    assert not any(isinstance(c, _AuxProbeClientStub) for c in cached), (
        "a probe stub was published into the shared client cache; the next "
        "real consumer would receive a non-functional client"
    )


def test_runtime_consumer_after_probe_gets_a_real_client(stub_resolver):
    """The bug: probe first, then a real consumer on the same key."""
    with aux_probe_mode():
        ac._get_cached_client("anthropic", model="m")

    client, _ = ac._get_cached_client("anthropic", model="m")

    assert client is stub_resolver
    assert not isinstance(client, _AuxProbeClientStub), (
        "runtime consumer (e.g. context compression) received a probe stub "
        "from the cache — compression would abort instead of summarizing"
    )


def test_stub_is_inert_to_teardown_inspection():
    """Best-effort cleanup paths must not explode on a stub.

    `getattr(client, "close", None)` relies on AttributeError to fall back to
    the default; the stub raises RuntimeError from __getattr__, so `close`
    must exist as a real no-op member.
    """
    stub = _AuxProbeClientStub()

    assert callable(getattr(stub, "close", None))
    assert stub.close() is None
    ac._close_cached_client(stub)          # must not raise
    ac._evict_cached_client_instance(stub)  # must not raise


def test_stub_still_fails_loudly_when_used_as_a_real_client():
    """The guard rail stays: genuine client USE must raise, not silently no-op."""
    stub = _AuxProbeClientStub()

    with pytest.raises(RuntimeError, match="used as a real client"):
        stub.chat.completions.create()

    with pytest.raises(RuntimeError, match="used as a real client"):
        stub.messages.create()
