"""Shared gateway restart constants and supervisor detection helpers."""

import os
from collections.abc import Mapping

from hermes_cli.config import DEFAULT_CONFIG

# EX_TEMPFAIL from sysexits.h — used to ask the service manager to restart
# the gateway after a graceful drain/reload path completes.
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75

# EX_CONFIG from sysexits.h — fatal configuration error (e.g. token
# collision, no messaging platforms).  The s6 finish script translates
# this into exit 125 (permanent failure) so the supervisor stops
# restarting the gateway.  See #51228.
GATEWAY_FATAL_CONFIG_EXIT_CODE = 78

# Set by ``hermes gateway run --external-supervisor``. Unlike systemd's
# INVOCATION_ID and launchd's XPC_SERVICE_NAME, this survives wrappers that
# intentionally replace the child environment (for example ``sudo env -i``).
EXTERNAL_GATEWAY_SUPERVISOR_ENV = "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"

DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_drain_timeout"]
)

# In-band restart (``/restart``, SIGUSR1, self-restart from a child CLI)
# waits for active turns to finish *before* ``stop()`` begins. Distinct
# from ``restart_drain_timeout``, which is the force-interrupt budget
# once ``stop()`` is running (and must stay short under systemd
# TimeoutStopSec). See #77184.
DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_after_turn_timeout"]
)


def _innermost_systemd_unit_kinds(cgroup_path: str = "/proc/self/cgroup") -> set:
    """Return the innermost systemd unit suffixes owning this process.

    Each ``/proc/self/cgroup`` line is scanned from the leaf toward the
    root; the first component ending in ``.service`` or ``.scope`` is that
    hierarchy's innermost unit (the one that directly contains the
    process).  Scanning inward-first matters: a desktop-terminal process
    lives at ``.../user@N.service/app.slice/ptyxis-spawn-X.scope``, so a
    naive "any .service in the path" check would misread the ``user@``
    manager unit as service ownership, while a gateway with ``Delegate=yes``
    lives at ``.../hermes-gateway.service/<sub-cgroup>`` and a naive
    leaf-only check would miss the service.  Returns a subset of
    ``{"service", "scope"}``; empty when unreadable (macOS/Windows/etc.).

    Kept injectable (``cgroup_path``) for the same reason
    :func:`is_container_restart_context` was extracted — tests must be able
    to pin cgroup detection hermetically, or a CI runner that itself runs
    inside a systemd unit flips the result under the test.
    """
    kinds: set = set()
    try:
        with open(cgroup_path, encoding="utf-8") as fh:
            for line in fh:
                for component in reversed(line.strip().split("/")):
                    if component.endswith(".service"):
                        kinds.add("service")
                        break
                    if component.endswith(".scope"):
                        kinds.add("scope")
                        break
    except OSError:
        return set()
    return kinds


def is_gateway_supervisor_process(
    environ: Mapping[str, str] | None = None,
    cgroup_path: str = "/proc/self/cgroup",
) -> bool:
    """Return whether this gateway process is owned by a supervisor."""
    env = os.environ if environ is None else environ
    if env.get("INVOCATION_ID"):
        # systemd sets INVOCATION_ID for every unit it launches — including
        # desktop sessions: on GNOME every process of a graphical login
        # inherits it because gnome-session and the terminal's transient
        # ``*-spawn-*.scope`` are themselves systemd units. A process whose
        # innermost cgroup unit is a transient *scope* (and no enclosing
        # *service* below the user@ manager) is such a desktop/terminal
        # process, not a supervised service: treating it as supervised
        # routes /restart to the exit-75 service path where nothing
        # restarts the gateway. Veto only that shape; any ``.service``
        # ownership — hermes-gateway.service, legacy hermes.service, or a
        # user-custom unit — keeps supervisor status, and an unreadable
        # cgroup file (macOS, containers without cgroupfs) preserves the
        # historical INVOCATION_ID meaning.
        unit_kinds = _innermost_systemd_unit_kinds(cgroup_path)
        if "service" in unit_kinds or not unit_kinds:
            return True
    if env.get("HERMES_S6_SUPERVISED_CHILD"):
        return True
    xpc_service = env.get("XPC_SERVICE_NAME", "")
    if xpc_service and xpc_service != "0":
        return True
    return str(env.get(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_container_restart_context() -> bool:
    """Return whether the gateway is running inside a container for restart
    routing purposes (Docker/Podman ⇒ the detached setsid path dies with the
    cgroup; exit-75 service restart is the only viable path).

    Extracted from the inline probe in the /restart handler so tests can mock
    container detection hermetically — a real ``/.dockerenv`` on a
    containerized CI runner otherwise flips the routing under the test.
    """
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def parse_restart_drain_timeout(raw: object) -> float:
    """Parse a configured drain timeout, falling back to the shared default."""
    try:
        value = float(raw) if str(raw or "").strip() else DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    return max(0.0, value)


def parse_restart_after_turn_timeout(raw: object) -> float:
    """Parse the after-turn wait cap for in-band restart, falling back to default.

    ``0`` is a deliberate disable (legacy immediate drain) and must not fall
    through to the default — unlike empty/missing input.
    """
    if raw is None:
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    if isinstance(raw, str) and not raw.strip():
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    return max(0.0, value)


def resolve_restart_exit_wait_budget(
    drain_timeout: float,
    after_turn_timeout: float,
    *,
    headroom: float = 15.0,
) -> float:
    """Seconds a CLI should wait for the gateway PID to exit after SIGUSR1.

    In-band restart may defer ``stop()`` until active turns finish
    (``after_turn_timeout``) and then spend up to ``drain_timeout`` inside
    ``stop()``. Callers that fall back to a hard kill on wait expiry must
    cover both phases or they reintroduce #77184.
    """
    try:
        drain = max(float(drain_timeout), 0.0)
    except (TypeError, ValueError):
        drain = 0.0
    try:
        after_turn = max(float(after_turn_timeout), 0.0)
    except (TypeError, ValueError):
        after_turn = 0.0
    try:
        margin = max(float(headroom), 0.0)
    except (TypeError, ValueError):
        margin = 0.0
    return drain + after_turn + margin
