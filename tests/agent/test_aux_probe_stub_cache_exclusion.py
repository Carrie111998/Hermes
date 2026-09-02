"""Availability-probe stubs must never enter the auxiliary client cache.

``aux_probe_mode()`` answers "is a client resolvable?" without building a real
SDK client: it returns an ``_AuxProbeClientStub`` that raises on any attribute
access, so a leak into a runtime path fails loudly instead of silently sending
requests nowhere.

``_store_cached_client()`` has always excluded stubs, but ``_get_cached_client``
writes to ``_client_cache`` directly and lacked the same guard. The cached stub
was then served to the NEXT resolution for that key:

* a second probe hit ``_compat_model()`` → ``_is_openrouter_client(stub)`` →
  ``stub._client`` → ``RuntimeError``, which ``check_vision_requirements()``
  swallowed into ``False``. ``vision_analyze`` therefore dropped off the tool
  schema after the first probe and stayed off for the life of the process
  (check_fn results are TTL-cached process-wide, so a long-lived gateway or
  desktop backend never recovered).
* a real caller got the non-functional stub instead of a client.

Only slash-bearing model ids reach the ``_compat_model`` branch, which is why
this surfaced on providers whose model ids are namespaced (NVIDIA's
``meta/llama-3.2-11b-vision-instruct``).
"""

from unittest.mock import MagicMock, patch

import pytest

import agent.auxiliary_client as aux

_PROBE_BASE_URL = "https://integrate.api.nvidia.com/v1"
_SLASH_MODEL = "meta/llama-3.2-11b-vision-instruct"


@pytest.fixture(autouse=True)
def _clean_aux_state():
    aux.shutdown_cached_clients()
    aux.clear_runtime_main()
    yield
    aux.shutdown_cached_clients()
    aux.clear_runtime_main()


def _stub() -> aux._AuxProbeClientStub:
    return aux._AuxProbeClientStub(api_key="probe-key", base_url=_PROBE_BASE_URL)


def _cached_stub_count() -> int:
    return sum(
        1
        for entry in aux._client_cache.values()
        if isinstance(entry[0], aux._AuxProbeClientStub)
    )


def test_probe_stub_is_returned_but_not_cached():
    """The probe still answers "resolvable", yet leaves the cache untouched."""
    stub = _stub()
    with patch.object(aux, "resolve_provider_client", return_value=(stub, _SLASH_MODEL)):
        with aux.aux_probe_mode():
            client, model = aux._get_cached_client("nvidia", model=_SLASH_MODEL)

    assert client is stub
    assert model == _SLASH_MODEL
    assert _cached_stub_count() == 0


def test_repeated_probes_stay_resolvable_for_slash_bearing_models():
    """Every probe resolves; none is served a stub from a previous probe."""
    built = []

    def _resolve(provider, model, async_mode=False, **kwargs):
        built.append(provider)
        return _stub(), _SLASH_MODEL

    with patch.object(aux, "resolve_provider_client", side_effect=_resolve):
        with aux.aux_probe_mode():
            for _ in range(3):
                client, model = aux._get_cached_client("nvidia", model=_SLASH_MODEL)
                assert client is not None
                assert model == _SLASH_MODEL

    # A cache hit would have skipped resolution (and raised on the stub).
    assert len(built) == 3
    assert _cached_stub_count() == 0


def test_runtime_caller_after_a_probe_gets_a_real_client():
    """A probe must not poison the cache for the real call that follows it."""
    real = MagicMock(name="real-client")

    def _resolve(provider, model, async_mode=False, **kwargs):
        if aux._aux_probe_active():
            return _stub(), _SLASH_MODEL
        return real, _SLASH_MODEL

    with patch.object(aux, "resolve_provider_client", side_effect=_resolve):
        with aux.aux_probe_mode():
            aux._get_cached_client("nvidia", model=_SLASH_MODEL)
        client, model = aux._get_cached_client("nvidia", model=_SLASH_MODEL)

    assert client is real
    assert model == _SLASH_MODEL
