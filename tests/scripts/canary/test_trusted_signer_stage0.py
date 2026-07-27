from __future__ import annotations

import stat
from types import SimpleNamespace

import pytest

from scripts.canary import trusted_signer_stage0 as stage0


def _directory_state(
    mode: int,
    *,
    uid: int = 0,
    gid: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=uid,
        st_gid=gid,
    )


@pytest.mark.parametrize("mode", (0o755, 0o775, 0o1777))
def test_host_runtime_lock_accepts_pinned_root_owned_parent_modes(
    mode: int,
) -> None:
    assert stage0._lock_parent_is_trusted(_directory_state(mode))


@pytest.mark.parametrize(
    "state",
    (
        _directory_state(0o777),
        _directory_state(0o1775),
        _directory_state(0o1777, uid=1),
        _directory_state(0o1777, gid=1),
        SimpleNamespace(
            st_mode=stat.S_IFREG | 0o1777,
            st_uid=0,
            st_gid=0,
        ),
        SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o1777,
            st_uid=0,
            st_gid=0,
        ),
    ),
)
def test_host_runtime_lock_rejects_untrusted_parent_contract(
    state: SimpleNamespace,
) -> None:
    assert not stage0._lock_parent_is_trusted(state)
