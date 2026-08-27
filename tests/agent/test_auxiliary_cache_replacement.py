"""Regression coverage for auxiliary cache replacement ownership."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import agent.auxiliary_client as aux


class _BlockingClient:
    def __init__(self) -> None:
        self.closed = Event()
        self.started = Event()
        self.release = Event()

    def request(self) -> str:
        self.started.set()
        assert self.release.wait(timeout=2)
        if self.closed.is_set():
            raise ConnectionError("client closed during request")
        return "complete"

    def close(self) -> None:
        self.closed.set()


def test_same_key_replacement_does_not_close_inflight_client():
    key = ("replacement-test", False)
    inflight = _BlockingClient()
    replacement = _BlockingClient()

    with aux._client_cache_lock:
        saved = aux._client_cache.pop(key, None)
        aux._client_cache[key] = (inflight, "old-model", None)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(inflight.request)
            assert inflight.started.wait(timeout=2)

            aux._store_cached_client(key, replacement, "new-model")
            inflight.release.set()

            assert future.result(timeout=2) == "complete"

        with aux._client_cache_lock:
            assert aux._client_cache[key] == (replacement, "new-model", None)
        assert not inflight.closed.is_set()
    finally:
        with aux._client_cache_lock:
            aux._client_cache.pop(key, None)
            if saved is not None:
                aux._client_cache[key] = saved
