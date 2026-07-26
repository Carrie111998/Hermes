"""Strict policy, provider, lease, and drain lifecycle for cron runtimes."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, cast

from cron.scheduler_lease import SchedulerOwnershipLease

logger = logging.getLogger("cron.scheduler_runtime")
RuntimeOwner = Literal["gateway", "desktop"]
OwnershipMode = Literal["auto", "gateway", "desktop"]
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_BUILTIN_NAMES = frozenset({"", "builtin", "in-process", "inprocess"})
_ACTIVE_PROVIDERS: dict[str, "_ProviderAdmission"] = {}
_ACTIVE_PROVIDERS_LOCK = threading.Lock()


@dataclass(frozen=True)
class SchedulerOwnershipPolicy:
    mode: OwnershipMode
    configured_provider: str


def _read_mapping(path: Path) -> dict[str, Any]:
    from utils import fast_safe_load

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    meaningful = [
        line.split("#", 1)[0].strip()
        for line in raw.splitlines()
        if line.split("#", 1)[0].strip() not in {"", "---", "..."}
    ]
    if not meaningful:
        return {}
    parsed = fast_safe_load(io.StringIO(raw))
    if parsed is None:
        raise ValueError("explicit null config root")
    if not isinstance(parsed, dict):
        raise ValueError("config root must be a mapping")
    return parsed


def _read_effective_cron_config_strict(home: Path) -> dict[str, Any] | None:
    from hermes_cli import managed_scope
    from hermes_cli.config import _expand_env_vars

    try:
        user = cast(
            dict[str, Any], _expand_env_vars(_read_mapping(home / "config.yaml"))
        )
        managed_dir = managed_scope.get_managed_dir()
        managed = (
            cast(
                dict[str, Any],
                _expand_env_vars(_read_mapping(managed_dir / "config.yaml")),
            )
            if managed_dir is not None
            else {}
        )
        user_cron = user.get("cron", {})
        managed_cron = managed.get("cron", {})
        if "cron" in user and not isinstance(user_cron, dict):
            raise ValueError("cron section must be a mapping")
        if "cron" in managed and not isinstance(managed_cron, dict):
            raise ValueError("managed cron section must be a mapping")
        effective = dict(user_cron)
        effective.update(managed_cron)
        return {"cron": effective}
    except Exception:
        logger.error(
            "Unable to read valid cron scheduler policy for %s; scheduler startup disabled.",
            home,
        )
        return None


def read_scheduler_ownership_policy_strict(
    config: dict[str, Any] | None = None,
    *,
    hermes_home: Path | None = None,
) -> SchedulerOwnershipPolicy | None:
    """Read owner and provider together, failing closed on malformed input."""
    if config is None:
        if hermes_home is None:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        config = _read_effective_cron_config_strict(
            Path(hermes_home).expanduser().resolve()
        )
        if config is None:
            return None
    if not isinstance(config, dict):
        logger.error("Invalid configuration root; scheduler startup disabled.")
        return None
    cron = config.get("cron", {})
    if "cron" in config and not isinstance(cron, dict):
        logger.error("Invalid cron configuration shape; scheduler startup disabled.")
        return None

    raw_mode = cron.get("scheduler_owner", "auto")
    mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
    if mode not in {"auto", "gateway", "desktop"}:
        logger.error(
            "Invalid cron.scheduler_owner; use auto, gateway, or desktop. "
            "Scheduler startup disabled."
        )
        return None
    raw_provider = cron.get("provider", "")
    if raw_provider is None or not isinstance(raw_provider, str):
        logger.error("Invalid cron.provider; scheduler startup disabled.")
        return None
    provider = raw_provider.strip()
    if provider.lower() in _BUILTIN_NAMES:
        provider = "builtin"
    elif not _PROVIDER_RE.fullmatch(provider):
        logger.error("Invalid cron.provider; scheduler startup disabled.")
        return None
    return SchedulerOwnershipPolicy(cast(OwnershipMode, mode), provider)


def scheduler_runtime_is_eligible(
    policy: SchedulerOwnershipPolicy,
    *,
    runtime: RuntimeOwner,
    same_home_gateway_running: bool,
) -> bool:
    if policy.mode == "gateway":
        return runtime == "gateway"
    if policy.mode == "desktop":
        return runtime == "desktop"
    if runtime == "gateway":
        return True
    return policy.configured_provider == "builtin" and not same_home_gateway_running


def _home_key(home: Path) -> str:
    return str(home.expanduser().resolve())


def _gateway_presence_path(home: Path) -> Path:
    return home / "cron" / ".gateway-present.json"


def exact_home_gateway_is_running(home: Path) -> bool:
    """Check the per-served-profile Gateway presence record.

    A bare live PID is insufficient because the OS may have recycled it after
    the publishing Gateway exited.  Require the persisted Gateway identity,
    exact home, and process start fingerprint to still match.
    """
    try:
        from gateway.status import _pid_exists, get_process_start_time

        record = json.loads(_gateway_presence_path(home).read_text(encoding="utf-8"))
        pid = int(record["pid"])
        start_time = record["start_time"]
        if (
            record.get("kind") != "gateway"
            or Path(record["hermes_home"]).expanduser().resolve() != home.resolve()
            or not isinstance(start_time, int)
            or isinstance(start_time, bool)
        ):
            return False
        return _pid_exists(pid) and get_process_start_time(pid) == start_time
    except (
        FileNotFoundError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ):
        return False


def _publish_gateway_presence(home: Path) -> None:
    from gateway.status import get_process_start_time

    start_time = get_process_start_time(os.getpid())
    if start_time is None:
        logger.warning(
            "Cannot publish PID-reuse-safe Gateway presence for %s; "
            "Desktop auto ownership will remain fail-open",
            home,
        )
        return
    path = _gateway_presence_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "pid": os.getpid(),
            "start_time": start_time,
            "kind": "gateway",
            "hermes_home": str(home.resolve()),
        }),
        encoding="utf-8",
    )


def _clear_gateway_presence(home: Path) -> None:
    from gateway.status import get_process_start_time

    path = _gateway_presence_path(home)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            int(record.get("pid", -1)) == os.getpid()
            and record.get("start_time") == get_process_start_time(os.getpid())
            and record.get("kind") == "gateway"
        ):
            path.unlink()
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


class _ProviderAdmission:
    """Closeable exact-home provider admission with in-flight accounting."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._condition = threading.Condition()
        self._accepting = True
        self._in_flight = 0

    def borrow(self) -> Any | None:
        with self._condition:
            if not self._accepting:
                return None
            self._in_flight += 1
            return self.provider

    def release(self) -> None:
        with self._condition:
            self._in_flight -= 1
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._accepting = False

    def wait_drained(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    @property
    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight


def _active_admission(home: Path) -> _ProviderAdmission | None:
    with _ACTIVE_PROVIDERS_LOCK:
        return _ACTIVE_PROVIDERS.get(_home_key(home))


def get_active_scheduler_provider(*, hermes_home: Path | None = None) -> Any | None:
    """Compatibility snapshot; new callback paths must use borrow_scheduler_provider."""
    if hermes_home is None:
        from hermes_constants import get_hermes_home

        hermes_home = get_hermes_home()
    admission = _active_admission(Path(hermes_home))
    return admission.provider if admission is not None else None


def _set_active_provider(home: Path, admission: _ProviderAdmission | None) -> None:
    with _ACTIVE_PROVIDERS_LOCK:
        if admission is None:
            _ACTIVE_PROVIDERS.pop(_home_key(home), None)
        else:
            _ACTIVE_PROVIDERS[_home_key(home)] = admission


@contextmanager
def borrow_scheduler_provider(*, hermes_home: Path | None = None):
    """Borrow the exact-home owner provider, or yield ``None`` if admission closed."""
    if hermes_home is None:
        from hermes_constants import get_hermes_home

        hermes_home = get_hermes_home()
    admission = _active_admission(Path(hermes_home))
    provider = admission.borrow() if admission is not None else None
    try:
        yield provider
    finally:
        if provider is not None:
            admission.release()


class OwnedSchedulerRuntime:
    """Blocking policy supervisor intended to run in one daemon thread."""

    def __init__(
        self,
        runtime_owner: RuntimeOwner,
        *,
        adapters: Any = None,
        loop: Any = None,
        can_dispatch: Callable[[], bool] | None = None,
        gateway_is_running: Callable[[], bool] | None = None,
        interval: int = 60,
        poll_interval: float = 0.5,
        drain_timeout: float = 65.0,
        hermes_home: Path | None = None,
    ) -> None:
        if runtime_owner not in {"gateway", "desktop"}:
            raise ValueError("Unknown cron scheduler runtime owner")
        self.runtime_owner = runtime_owner
        self.adapters = adapters
        self.loop = loop
        self.can_dispatch = can_dispatch
        self.gateway_is_running = gateway_is_running or (lambda: False)
        self.interval = interval
        self.poll_interval = poll_interval
        self.drain_timeout = drain_timeout
        self.hermes_home = Path(hermes_home).resolve() if hermes_home else None
        self._active_provider: Any | None = None
        self._active_policy: SchedulerOwnershipPolicy | None = None
        self._lease: SchedulerOwnershipLease | None = None
        self._provider_thread: threading.Thread | None = None
        self._provider_stop: threading.Event | None = None
        self._provider_failed = threading.Event()
        self._state_lock = threading.Lock()
        self._admission: _ProviderAdmission | None = None
        self._jobs_signature: tuple[int, int, int] | None = None
        self._reconcile_retry_at = 0.0
        self._reconcile_retry_delay = poll_interval
        self._retry_delay = poll_interval
        self._retry_at = 0.0

    @property
    def active_provider(self) -> Any | None:
        with self._state_lock:
            return self._active_provider

    def _home(self) -> Path:
        if self.hermes_home is not None:
            return self.hermes_home
        from hermes_constants import get_hermes_home

        return get_hermes_home().expanduser().resolve()

    def _eligible(self, policy: SchedulerOwnershipPolicy | None) -> bool:
        if policy is None:
            return False
        gateway_running = False
        if self.runtime_owner == "desktop":
            try:
                gateway_running = exact_home_gateway_is_running(self._home()) or bool(
                    self.gateway_is_running()
                )
            except Exception:
                logger.exception("Gateway presence probe failed; Desktop cron yields")
                gateway_running = True
        return scheduler_runtime_is_eligible(
            policy,
            runtime=self.runtime_owner,
            same_home_gateway_running=gateway_running,
        )

    def _provider_target(
        self, provider: Any, stop: threading.Event, home: Path
    ) -> None:
        from cron.scheduler_provider import InProcessCronScheduler
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(home)
        kwargs = {
            "adapters": self.adapters,
            "loop": self.loop,
            "interval": self.interval,
        }
        if isinstance(provider, InProcessCronScheduler) and self.can_dispatch:
            kwargs["can_dispatch"] = self.can_dispatch
        try:
            provider.start(stop, **kwargs)
        except BaseException:
            self._provider_failed.set()
            logger.exception("Cron scheduler provider failed for %s", home)
        finally:
            reset_hermes_home_override(token)

    def _start_active(self, policy: SchedulerOwnershipPolicy, home: Path) -> bool:
        lease = SchedulerOwnershipLease.try_acquire(
            hermes_home=home,
            owner=self.runtime_owner,
            provider=policy.configured_provider,
        )
        if lease is None:
            return False
        fresh = read_scheduler_ownership_policy_strict(hermes_home=home)
        if fresh != policy or not self._eligible(fresh):
            lease.release()
            return False

        from cron.scheduler_provider import resolve_cron_scheduler_runtime_strict
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(home)
        try:
            provider = resolve_cron_scheduler_runtime_strict(policy.configured_provider)
        finally:
            reset_hermes_home_override(token)
        if provider is None:
            lease.release()
            return False
        provider_stop = threading.Event()
        admission = _ProviderAdmission(provider)
        self._provider_failed.clear()
        thread = threading.Thread(
            target=self._provider_target,
            args=(provider, provider_stop, home),
            daemon=True,
            name=f"{self.runtime_owner}-cron-provider",
        )
        with self._state_lock:
            self._lease = lease
            self._active_provider = provider
            self._active_policy = policy
            self._provider_stop = provider_stop
            self._provider_thread = thread
            self._admission = admission
        try:
            self._jobs_signature = self._read_jobs_signature(home)
        except OSError:
            self._jobs_signature = None
            self._reconcile_retry_at = time.monotonic() + self.poll_interval
            logger.exception("Cannot read initial cron jobs signature for %s", home)
        _set_active_provider(home, admission)
        try:
            thread.start()
        except BaseException:
            # Publication makes the provider borrowable before Thread.start().
            # A concurrent callback may therefore already be executing.  Close
            # admission first, wait for every such borrower, then unpublish and
            # only after that release the single-owner lease.
            admission.close()
            warned = False
            while not admission.wait_drained(self.poll_interval):
                if not warned:
                    logger.warning(
                        "Cron provider startup failed for %s; retaining lease "
                        "until published borrowers drain",
                        home,
                    )
                    warned = True
            _set_active_provider(home, None)
            with self._state_lock:
                self._lease = None
                self._active_provider = None
                self._active_policy = None
                self._provider_stop = None
                self._provider_thread = None
                self._admission = None
            lease.release()
            raise
        logger.info(
            "%s owns cron for %s (provider=%s)",
            self.runtime_owner.capitalize(),
            home,
            provider.name,
        )
        self._reconcile_retry_delay = self.poll_interval
        self._reconcile_retry_at = 0.0
        return True

    @staticmethod
    def _read_jobs_signature(home: Path) -> tuple[int, int, int] | None:
        path = home / "cron" / "jobs.json"
        try:
            stat = path.stat()
            return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except FileNotFoundError:
            return None

    def _reconcile_if_changed(self, home: Path) -> None:
        try:
            signature = self._read_jobs_signature(home)
        except OSError:
            now = time.monotonic()
            if now >= self._reconcile_retry_at:
                logger.exception("Cannot read cron jobs signature for %s", home)
                self._reconcile_retry_delay = min(
                    max(self.poll_interval, self._reconcile_retry_delay * 2),
                    30.0,
                )
                self._reconcile_retry_at = now + self._reconcile_retry_delay
            return
        if signature == self._jobs_signature:
            return
        now = time.monotonic()
        if now < self._reconcile_retry_at:
            return
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        with borrow_scheduler_provider(hermes_home=home) as provider:
            if provider is None:
                return
            token = set_hermes_home_override(home)
            try:
                provider.on_jobs_changed()
            except BaseException:
                logger.exception("Cron provider reconciliation failed for %s", home)
                self._reconcile_retry_delay = min(
                    max(
                        self.poll_interval,
                        self._reconcile_retry_delay * 2,
                    ),
                    30.0,
                )
                self._reconcile_retry_at = now + self._reconcile_retry_delay
                return
            finally:
                reset_hermes_home_override(token)
        self._jobs_signature = signature
        self._reconcile_retry_delay = self.poll_interval
        self._reconcile_retry_at = 0.0

    @staticmethod
    def _jobs_drained(home: Path) -> bool:
        from cron.scheduler import get_running_job_ids

        # Job IDs are process-global today. Conservatively holding every active
        # home lease until all jobs drain prevents cross-profile overlap.
        return not get_running_job_ids()

    def _drain_active(self, supervisor_stop: threading.Event, home: Path) -> None:
        with self._state_lock:
            provider = self._active_provider
            provider_stop = self._provider_stop
            thread = self._provider_thread
            lease = self._lease
            admission = self._admission
        if provider is None or lease is None:
            return
        # Close admission before unpublishing or signaling teardown. Every fire
        # and reconcile that already borrowed the provider remains counted.
        if admission is not None:
            admission.close()
        _set_active_provider(home, None)
        if provider_stop:
            provider_stop.set()
        deadline = time.monotonic() + max(0.0, self.drain_timeout)
        warned = False
        while (
            (thread is not None and thread.is_alive())
            or (admission is not None and admission.in_flight)
            or not self._jobs_drained(home)
        ):
            if not warned and time.monotonic() >= deadline:
                warned = True
                logger.warning(
                    "Cron for %s did not drain within %.0fs; retaining lease",
                    home,
                    self.drain_timeout,
                )
            # The supervisor stop event is already set during shutdown; waiting
            # on it would return immediately and busy-spin while providers/jobs
            # finish draining. Sleep independently so lease retention remains
            # fail-closed without burning a CPU core.
            time.sleep(min(self.poll_interval, 0.1) if warned else self.poll_interval)
        # Provider stop tears down resources that admitted callbacks and the
        # provider thread may still be using. Run it only after every borrower,
        # provider lifecycle, and in-flight job has crossed the drain barrier;
        # the ownership lease remains held throughout.
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(home)
        try:
            provider.stop()
        except BaseException:
            logger.exception("Cron provider stop failed")
        finally:
            reset_hermes_home_override(token)
        with self._state_lock:
            self._active_provider = None
            self._active_policy = None
            self._provider_stop = None
            self._provider_thread = None
            self._lease = None
            self._admission = None
            self._jobs_signature = None
            self._reconcile_retry_delay = self.poll_interval
            self._reconcile_retry_at = 0.0
        lease.release()

    def run(self, stop_event: threading.Event) -> None:
        home = self._home()
        if self.runtime_owner == "gateway":
            _publish_gateway_presence(home)
        try:
            while not stop_event.is_set():
                policy = read_scheduler_ownership_policy_strict(hermes_home=home)
                active = self.active_provider
                provider_failed = self._provider_failed.is_set()
                if active is not None and (
                    not self._eligible(policy)
                    or policy != self._active_policy
                    or provider_failed
                ):
                    self._drain_active(stop_event, home)
                    if provider_failed:
                        self._retry_delay = min(
                            max(self.poll_interval, self._retry_delay * 2), 30.0
                        )
                        self._retry_at = time.monotonic() + self._retry_delay
                    continue
                if active is None and self._eligible(policy):
                    assert policy is not None
                    now = time.monotonic()
                    if now >= self._retry_at:
                        if self._start_active(policy, home):
                            self._retry_delay = self.poll_interval
                        else:
                            self._retry_delay = min(
                                max(self.poll_interval, self._retry_delay * 2), 30.0
                            )
                            self._retry_at = now + self._retry_delay
                elif active is not None:
                    self._reconcile_if_changed(home)
                    self._retry_delay = self.poll_interval
                stop_event.wait(self.poll_interval)
        finally:
            # Any unexpected supervisor failure must fail closed: stop and
            # drain the provider before releasing its exact-home lease.
            self._drain_active(stop_event, home)
            if self.runtime_owner == "gateway":
                _clear_gateway_presence(home)
