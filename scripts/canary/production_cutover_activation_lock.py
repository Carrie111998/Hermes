#!/usr/bin/env python3
"""Shared activation-lock boundary for production authority writers."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import sys
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from gateway import canonical_writer_activation as activation


INHERITED_LOCK_FD_ENV = "MUNCHO_WRITER_ACTIVATION_LOCK_FD"
ACTIVATION_LOCK_PATH = Path("/run/muncho-writer-activation.lock")
_CANONICAL_FD = re.compile(r"^(?:[3-9]|[1-9][0-9]+)$")
_PROCESS_ACTIVATION_MUTEX = threading.RLock()
_THREAD_DELEGATION = threading.local()


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
def _bind_thread_delegation(descriptor: int) -> Iterator[None]:
    if getattr(_THREAD_DELEGATION, "descriptor", None) is not None:
        raise AuthorityActivationLockError(
            "production_authority_activation_lock_invalid"
        )
    _THREAD_DELEGATION.descriptor = descriptor
    try:
        yield
    finally:
        del _THREAD_DELEGATION.descriptor


@contextmanager
def _reuse_same_thread_delegation(descriptor: int) -> Iterator[None]:
    raw_descriptor = os.environ.get(INHERITED_LOCK_FD_ENV)
    if (
        not sys.platform.startswith("linux")
        or raw_descriptor != str(descriptor)
    ):
        raise AuthorityActivationLockError(
            "production_authority_activation_lock_invalid"
        )
    _validate_lock_identity(descriptor)
    try:
        if not os.get_inheritable(descriptor):
            raise OSError("delegated activation descriptor is not inheritable")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError) as exc:
        raise AuthorityActivationLockError(
            "production_authority_activation_lock_unavailable"
        ) from exc
    _validate_lock_identity(descriptor)
    yield


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
    # ``flock`` ownership follows an open file description rather than a
    # Python thread.  The delegated descriptor and its environment variable
    # are process-global, so a second thread could otherwise reuse the outer
    # thread's exact open file description and enter concurrently.  Hold a
    # process-local reentrant mutex before reading that environment and record
    # delegation in thread-local state.  Same-thread composition remains
    # reentrant; unrelated threads wait until the outer lifecycle has restored
    # the environment and closed (or released) its host lock.
    with _PROCESS_ACTIVATION_MUTEX:
        nested_descriptor = getattr(
            _THREAD_DELEGATION,
            "descriptor",
            None,
        )
        if nested_descriptor is not None:
            if type(nested_descriptor) is not int or nested_descriptor < 3:
                raise AuthorityActivationLockError(
                    "production_authority_activation_lock_invalid"
                )
            with _reuse_same_thread_delegation(nested_descriptor):
                yield
            return

        raw_descriptor = os.environ.get(INHERITED_LOCK_FD_ENV)
        if raw_descriptor is None:
            with ExitStack() as stack:
                try:
                    descriptor = stack.enter_context(
                        activation._host_activation_lock()
                    )
                except (OSError, PermissionError, RuntimeError) as exc:
                    raise AuthorityActivationLockError(
                        "production_authority_activation_lock_unavailable"
                    ) from exc
                # The canonical lock owns one open file description.  Publish
                # that exact descriptor only while the protected body runs so
                # a nested activation primitive or exec'd child can prove and
                # reuse it without opening a conflicting second flock.  The
                # environment is restored exactly on every exit path.
                if (
                    descriptor is None
                    and not sys.platform.startswith("linux")
                ):
                    # Non-Linux tests may replace the production context with
                    # a value-less stand-in.  Production never takes this
                    # branch.
                    yield
                    return
                if (
                    type(descriptor) is not int
                    or descriptor < 3
                    or not sys.platform.startswith("linux")
                ):
                    raise AuthorityActivationLockError(
                        "production_authority_activation_lock_invalid"
                    )
                _validate_lock_identity(descriptor)
                prior_raw = os.environ.get(INHERITED_LOCK_FD_ENV)
                prior_inheritable: bool | None = None
                try:
                    prior_inheritable = os.get_inheritable(descriptor)
                    os.set_inheritable(descriptor, True)
                    os.environ[INHERITED_LOCK_FD_ENV] = str(descriptor)
                    _validate_lock_identity(descriptor)
                    with _bind_thread_delegation(descriptor):
                        yield
                except AuthorityActivationLockError:
                    raise
                except (OSError, ValueError) as exc:
                    raise AuthorityActivationLockError(
                        "production_authority_activation_lock_unavailable"
                    ) from exc
                finally:
                    if prior_raw is None:
                        os.environ.pop(INHERITED_LOCK_FD_ENV, None)
                    else:
                        os.environ[INHERITED_LOCK_FD_ENV] = prior_raw
                    if prior_inheritable is not None:
                        try:
                            os.set_inheritable(
                                descriptor,
                                prior_inheritable,
                            )
                        except (OSError, ValueError):
                            pass
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
            prior_inheritable = os.get_inheritable(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(descriptor, True)
        except (OSError, ValueError) as exc:
            raise AuthorityActivationLockError(
                "production_authority_activation_lock_unavailable"
            ) from exc
        _validate_lock_identity(descriptor)
        try:
            with _bind_thread_delegation(descriptor):
                yield
        finally:
            try:
                os.set_inheritable(descriptor, prior_inheritable)
            except (OSError, ValueError):
                pass
