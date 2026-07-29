#!/usr/bin/env python3
"""Shared activation-lock boundary for production authority writers."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from gateway import canonical_writer_activation as activation


INHERITED_LOCK_FD_ENV = "MUNCHO_WRITER_ACTIVATION_LOCK_FD"
ACTIVATION_LOCK_PATH = Path("/run/muncho-writer-activation.lock")
_CANONICAL_FD = re.compile(r"^(?:[3-9]|[1-9][0-9]+)$")


class AuthorityActivationLockError(RuntimeError):
    """The fixed production activation lock could not be proven."""


def _validate_lock_identity(descriptor: int) -> None:
    try:
        activation._validate_root_parent_chain(
            ACTIVATION_LOCK_PATH.parent
        )
        opened = os.fstat(descriptor)
        reached = ACTIVATION_LOCK_PATH.lstat()
        if (
            activation.ACTIVATION_LOCK_PATH != ACTIVATION_LOCK_PATH
            or ACTIVATION_LOCK_PATH.resolve(strict=True)
            != ACTIVATION_LOCK_PATH
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != 0
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino)
            != (reached.st_dev, reached.st_ino)
            or activation._list_xattrs(ACTIVATION_LOCK_PATH)
        ):
            raise AuthorityActivationLockError(
                "production_authority_activation_lock_invalid"
            )
    except AuthorityActivationLockError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthorityActivationLockError(
            "production_authority_activation_lock_invalid"
        ) from exc


@contextmanager
def authority_activation_lock(
    *,
    require_root: bool,
    lock_factory: Callable[[], Any] | None = None,
) -> Iterator[None]:
    """Acquire the lock or validate one inherited from the owning deploy."""

    if lock_factory is not None:
        with ExitStack() as stack:
            try:
                stack.enter_context(lock_factory())
            except (OSError, PermissionError, RuntimeError) as exc:
                raise AuthorityActivationLockError(
                    "production_authority_activation_lock_unavailable"
                ) from exc
            yield
        return
    if not require_root:
        yield
        return
    raw_descriptor = os.environ.get(INHERITED_LOCK_FD_ENV)
    if raw_descriptor is None:
        with ExitStack() as stack:
            try:
                stack.enter_context(activation._host_activation_lock())
            except (OSError, PermissionError, RuntimeError) as exc:
                raise AuthorityActivationLockError(
                    "production_authority_activation_lock_unavailable"
                ) from exc
            yield
        return
    if (
        not sys.platform.startswith("linux")
        or _CANONICAL_FD.fullmatch(raw_descriptor) is None
    ):
        raise AuthorityActivationLockError(
            "production_authority_activation_lock_invalid"
        )
    descriptor = int(raw_descriptor)
    _validate_lock_identity(descriptor)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(descriptor, True)
    except (OSError, ValueError) as exc:
        raise AuthorityActivationLockError(
            "production_authority_activation_lock_unavailable"
        ) from exc
    _validate_lock_identity(descriptor)
    yield
