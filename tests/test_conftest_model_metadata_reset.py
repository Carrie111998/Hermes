"""The autouse reset must actually clear agent.model_metadata's caches.

`agent/model_metadata.py` holds nine module-level caches behind TTLs of 3600s /
300s / 30s. Nothing cleared them between tests, so one file's metadata or
capability lookup decided the answer for every later file in the same process
— the mechanism behind the ~128 order- and timing-dependent failures in
`tests/agent`, which pass individually and fail in the hour-long run.

Verifying this needs an ordered pair, because a single test cannot observe a
fixture that runs between tests: the first poisons every cache, the second
asserts it came up clean. Written as a positive control — if the conftest block
is removed, the second test fails immediately and deterministically, rather
than the suite going quietly flaky again.
"""

from __future__ import annotations

import pytest

pytest.importorskip("agent.model_metadata")

DICT_CACHES = (
    "_model_metadata_cache",
    "_novita_metadata_cache",
    "_endpoint_model_metadata_cache",
    "_endpoint_model_metadata_cache_time",
    "_endpoint_probe_path_cache",
    "_codex_oauth_context_cache",
)
SCALAR_CACHES = (
    "_model_metadata_cache_time",
    "_novita_metadata_cache_time",
    "_codex_oauth_context_cache_time",
)


def test_a_poisons_every_model_metadata_cache():
    """Runs first (name ordering). Leaves state the next test must not see."""
    import agent.model_metadata as mm

    for name in DICT_CACHES:
        cache = getattr(mm, name, None)
        assert cache is not None, f"{name} vanished — update the conftest block"
        cache["poison/model"] = {"context_length": 1}

    for name in SCALAR_CACHES:
        assert hasattr(mm, name), f"{name} vanished — update the conftest block"
        setattr(mm, name, 1_000_000_000.0)

    # Sanity: the poison really is in place, so test B is a real assertion.
    assert mm._endpoint_probe_path_cache


def test_b_sees_every_cache_cleared_by_the_autouse_fixture():
    """Runs second. Any surviving entry is cross-test pollution."""
    import agent.model_metadata as mm

    leaked = [n for n in DICT_CACHES if getattr(mm, n, None)]
    assert leaked == [], f"caches leaked across tests: {leaked}"

    stale = [n for n in SCALAR_CACHES if getattr(mm, n, 0)]
    assert stale == [], f"cache timestamps leaked across tests: {stale}"
