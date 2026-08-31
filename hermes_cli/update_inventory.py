"""Runtime inventory + update plan for the fleet-update pipeline (#91277 Phase 2).

One read-only pass that answers, BEFORE any mutation: what Hermes runtimes
are running on this machine, how is each one deployed, which of them will
this update touch, and how will each be restarted?

This is the "plan" phase of the transactional deployment model (#88683):

    plan → snapshot → apply → restart-per-kind → verify → report

The module is deliberately side-effect free — every collector is a probe
over primitives that already exist (`find_profile_gateway_processes`,
`_get_service_pids`, `gateway_state.json` code stamps from #91283,
`detect_install_method`) — so `hermes update --plan` can run on a live
fleet with zero risk, and the update receipt can embed the inventory
without changing update behavior.

Deployment kinds (the concept most fleet-update bugs were missing):

    git      — source checkout; updatable in place via `hermes update`
    docker   — published image; NOT updatable in place (pull + recreate)
    nix/apt  — package-manager owned; updatable via the manager only
    unknown  — no marker; treated as in-place updatable (legacy default)

Supervisors (how a runtime is restarted after code changes):

    systemd / launchd — restart via the service manager (fleet-wide)
    desktop           — Desktop app supervises `hermes serve`; it respawns
    manual            — plain process; SIGTERM + watcher/manual relaunch
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class RuntimeRecord:
    """One running (or expected) Hermes runtime on this machine."""

    kind: str                     # gateway | dashboard | serve
    profile: str                  # profile name ("default", ...)
    pid: Optional[int] = None     # live PID when known
    supervisor: str = "manual"    # systemd | launchd | desktop | manual
    code_sha: Optional[str] = None       # stamped running-code sha (#91283)
    code_version: Optional[str] = None
    restart_via: str = ""         # human-readable restart mechanism
    # Exact supervisor identity, captured pre-mutation. A custom unit name
    # (``my-dashboard.service``), a launchd label (``ai.hermes.gateway-zeus``)
    # or a Windows SCM service name is unreadable once the process and its
    # cgroup are gone, and it does NOT follow from the profile name — the
    # restart phase must relaunch by THIS string, never by a profile
    # substring match against a discovery glob.
    unit: str = ""
    unit_scope: str = ""          # systemd: user | system; launchd: gui/<uid>
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UpdatePlan:
    """The full pre-update picture: install shape + runtimes + actions."""

    install_method: str = "unknown"       # git | docker | nix | apt | ...
    updatable_in_place: bool = True
    update_mechanism: str = "hermes update"
    expected_sha: Optional[str] = None    # current checkout HEAD (pre-pull)
    expected_version: Optional[str] = None
    profiles: list = field(default_factory=list)
    runtimes: list = field(default_factory=list)  # list[RuntimeRecord]
    # Discovery probes that did NOT answer. Non-empty means the runtime
    # list is a lower bound, not the fleet: a swallowed collector failure
    # is indistinguishable from "no runtimes here", so the quiesce phase
    # refuses to mutate on it (see ``verify_inventory_complete``).
    discovery_errors: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["runtimes"] = [
            r.to_dict() if isinstance(r, RuntimeRecord) else r
            for r in self.runtimes
        ]
        return payload


@dataclass(frozen=True)
class SupervisorIdentity:
    """Exact, relaunch-capable identity of a runtime's supervisor.

    ``unit`` is the string the restart phase must hand back to the
    supervisor verbatim: a systemd unit (``acme-dash.service``), a launchd
    label (``ai.hermes.gateway-zeus``) or a Windows SCM service name. It
    is NOT derivable from the profile — operators name custom units freely
    — and it is unreadable once the process and its cgroup are gone, which
    is why it must be captured in the pre-mutation inventory rather than
    during late cleanup.
    """

    unit: str = ""
    scope: str = ""
    cgroup: str = ""


def _default_pid_cgroup(pid: int) -> Optional[str]:
    """Unified-hierarchy cgroup path for *pid*; ``None`` off Linux."""
    try:
        from hermes_cli.main import _get_pid_cgroup_path

        return _get_pid_cgroup_path(int(pid))
    except Exception as exc:
        logger.debug("cgroup reader unavailable: %s", exc)
        return None


def capture_supervisor_identity(
    pid: int,
    *,
    cgroup_of=None,
    launchd_labels: Optional[dict] = None,
    windows_services: Optional[dict] = None,
) -> SupervisorIdentity:
    """Resolve the exact supervisor identity of *pid*. Never raises."""
    if windows_services:
        name = windows_services.get(int(pid)) if pid is not None else None
        if name:
            return SupervisorIdentity(unit=str(name), scope="scm")
    if launchd_labels:
        label = launchd_labels.get(int(pid)) if pid is not None else None
        if label:
            return SupervisorIdentity(unit=str(label), scope="launchd")
    if cgroup_of is None:
        cgroup_of = _default_pid_cgroup
    try:
        cgroup = cgroup_of(int(pid)) or ""
    except Exception as exc:
        logger.debug("cgroup probe failed for %s: %s", pid, exc)
        return SupervisorIdentity()
    cgroup = str(cgroup)
    unit = ""
    trimmed = cgroup.rstrip("/")
    if trimmed.endswith(".service"):
        unit = trimmed.rsplit("/", 1)[-1]
    scope = ""
    if "/system.slice/" in cgroup:
        scope = "system"
    elif "/user.slice/" in cgroup:
        scope = "user"
    return SupervisorIdentity(unit=unit, scope=scope if unit else "", cgroup=cgroup)


def parse_launchctl_list_labels(text: str) -> dict:
    """Map live PIDs to launchd labels from ``launchctl list`` output.

    Rows whose PID column is ``-`` are loaded-but-not-running jobs; they
    have no PID to reconcile against and are skipped. Never raises.
    """
    labels: dict[int, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        label = parts[-1].strip()
        if label:
            labels[pid] = label
    return labels


#: Where a user/system LaunchAgent plist normally lives. Searched in
#: order; the first hit wins.
_LAUNCHD_PLIST_DIRS = (
    "~/Library/LaunchAgents",
    "/Library/LaunchAgents",
    "/Library/LaunchDaemons",
)


def launchd_domain_for_plist(plist: str) -> str:
    """The launchd domain a job in *plist* belongs to.

    A ``LaunchDaemons`` plist is a system job; everything else is a per-user
    agent in the caller's GUI session. The domain is part of the job's
    identity — ``launchctl bootout gui/501/<daemon-label>`` addresses a job
    that is not there, reports success, and leaves the daemon running — so
    it has to be captured here, next to the plist path, while the runtime is
    still alive.
    """
    if not plist:
        return ""
    parts = {part.lower() for part in Path(plist).parts}
    if "launchdaemons" in parts:
        return "system"
    getuid = getattr(os, "getuid", None)
    return f"gui/{int(getuid()) if getuid else 0}"


def launchd_plist_for_label(label: str, *, search_dirs=None) -> str:
    """Path of *label*'s plist, or ``""``.

    Recorded pre-mutation because ``launchctl bootout`` (the only stop a
    KeepAlive agent respects) unloads the job — bringing it back requires
    re-bootstrapping the plist by path. A label is a filename component,
    never a path: anything with a separator is rejected outright.
    """
    label = str(label or "").strip()
    if not label or "/" in label or "\\" in label or label in (".", ".."):
        return ""
    if search_dirs is None:
        search_dirs = [Path(d).expanduser() for d in _LAUNCHD_PLIST_DIRS]
    for directory in search_dirs:
        try:
            candidate = Path(directory) / f"{label}.plist"
            if candidate.is_file():
                return str(candidate)
        except (OSError, ValueError) as exc:
            logger.debug("launchd plist probe failed in %s: %s", directory, exc)
    return ""


def _live_launchd_labels() -> dict:
    """``launchctl list`` PIDs → labels on macOS; ``{}`` elsewhere."""
    try:
        from hermes_cli.gateway import is_macos

        if not is_macos():
            return {}
    except Exception:
        return {}
    try:
        import subprocess

        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception as exc:
        logger.debug("launchctl list probe failed: %s", exc)
        return {}
    if result.returncode != 0:
        return {}
    return parse_launchctl_list_labels(result.stdout or "")


def _windows_service_names_by_pid() -> dict:
    """Gateway PIDs → SCM service names on Windows; ``{}`` elsewhere."""
    try:
        from hermes_cli.gateway import find_windows_gateway_services

        return {
            int(service.gateway_pid): str(service.name)
            for service in find_windows_gateway_services()
        }
    except Exception as exc:
        logger.debug("Windows SCM identity probe failed: %s", exc)
        return {}


def _detect_supervisor_for_pid(
    pid: int, service_pids: set, windows_service_pids: set | None = None
) -> str:
    """Classify how a live gateway PID is supervised."""
    if windows_service_pids and pid in windows_service_pids:
        # SCM-supervised Windows gateway (WinSW/NSSM/sc.exe create): the
        # update pause machinery stops the SERVICE via sc.exe instead of
        # killing the child, so #91277 Phase 2 reconciliation must plan it
        # under its own mechanism id, not "manual".
        return "windows-service"
    if pid in service_pids:
        try:
            from hermes_cli.gateway import is_macos, supports_systemd_services

            if supports_systemd_services():
                return "systemd"
            if is_macos():
                return "launchd"
        except Exception:
            pass
        return "service"
    return "manual"


def _restart_mechanism(supervisor: str, profile: str) -> str:
    """Machine-readable restart mechanism id for a runtime.

    THE policy table (#91277 Phase 2): restart execution consumes these ids
    via :func:`match_runtime_outcomes` / the update's restart phase, and the
    receipt records per-runtime outcomes against them. Display strings are
    derived by :func:`describe_restart_mechanism` — never the other way
    around.
    """
    if supervisor == "systemd":
        return "systemd"
    if supervisor == "launchd":
        return "launchd"
    if supervisor == "desktop":
        return "desktop"
    if supervisor == "windows-service":
        return "windows-service"
    if supervisor == "manual-serve":
        return "respawn-argv"
    return "manual"


def describe_restart_mechanism(mechanism: str, profile: str) -> str:
    """Human-readable description of a restart mechanism id."""
    if mechanism == "systemd":
        return "systemctl restart (drain-first SIGUSR1 when supported)"
    if mechanism == "launchd":
        return "launchctl kickstart -k (drain-first, per-label domain)"
    if mechanism == "desktop":
        return "Desktop app respawns its serve backend"
    if mechanism == "windows-service":
        return "sc.exe stop before venv mutation, sc.exe start after update"
    if mechanism == "respawn-argv":
        return "stop before code swap, relaunch with recorded launch args"
    if profile != "default":
        return f"hermes -p {profile} gateway restart"
    return "hermes gateway restart"


def _process_start_time(pid: int) -> Optional[float]:
    """Process start time for *pid*, or ``None`` when unreadable.

    A PID is a name the kernel reuses; ``(pid, start_time)`` is identity.
    Capturing it pre-mutation is what lets the stop phase prove the PID
    it is about to signal is still the runtime the plan inventoried, and
    not a bystander that inherited the number.
    """
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception as exc:
        logger.debug("Start-time probe failed for %s: %s", pid, exc)
        return None


def _record_discovery_failure(plan: UpdatePlan, probe: str, exc: BaseException) -> None:
    """Note that a runtime-discovery probe did not answer.

    The rows a broken collector would have produced are invisible — they
    look exactly like an empty fleet. Recording the failure is what lets
    :func:`update_quiesce.verify_inventory_complete` tell "nothing is
    running" apart from "we could not look", and refuse to mutate the
    checkout in the second case.
    """
    logger.debug("%s failed: %s", probe, exc)
    plan.discovery_errors.append(f"{probe}: {exc}")


def collect_runtime_inventory() -> UpdatePlan:
    """Build the pre-update plan. Read-only; never raises.

    Every collector degrades independently — a probe failure yields fewer
    rows, not an exception — but the failure itself is recorded in
    ``plan.discovery_errors`` so the fleet is never silently understated.
    The result is embeddable in the update receipt and printable via
    :func:`print_update_plan`.
    """
    plan = UpdatePlan()

    # --- install shape / deployment kind ---------------------------------
    try:
        from hermes_cli.config import (
            detect_install_method,
            get_managed_system,
            recommended_update_command_for_method,
        )

        method = detect_install_method()
        plan.install_method = method
        managed = get_managed_system()
        if managed:
            plan.install_method = managed
        plan.updatable_in_place = method in ("git", "unknown") and not managed
        # Baked image provenance (#91277 Phase 3): when the image marker is
        # present it is authoritative — a bind-mounted checkout inside a
        # container can look like `git` to the heuristics while the running
        # filesystem is actually an immutable image. Fail-closed: an invalid
        # marker still flips the plan to not-updatable.
        try:
            from hermes_cli.image_provenance import read_image_provenance

            provenance = read_image_provenance()
            if provenance is not None:
                plan.updatable_in_place = False
                if provenance.valid and provenance.manager:
                    plan.install_method = provenance.manager
        except Exception as exc:
            logger.debug("Image provenance probe failed: %s", exc)
        plan.update_mechanism = recommended_update_command_for_method(method)
    except Exception as exc:
        logger.debug("Install-method probe failed: %s", exc)

    # --- expected code identity (pre-pull) --------------------------------
    try:
        from hermes_cli.build_info import get_code_identity

        identity = get_code_identity(refresh=True)
        plan.expected_sha = identity.get("sha")
        plan.expected_version = identity.get("version")
    except Exception as exc:
        logger.debug("Code-identity probe failed: %s", exc)

    # --- profiles ----------------------------------------------------------
    profile_homes: list[tuple[str, Path]] = []
    try:
        from hermes_cli.profiles import (
            _get_default_hermes_home,
            _get_profiles_root,
            _PROFILE_ID_RE,
        )

        default_home = _get_default_hermes_home()
        if default_home.is_dir():
            profile_homes.append(("default", default_home))
        root = _get_profiles_root()
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if (
                    entry.is_dir()
                    and entry.name != "default"
                    and _PROFILE_ID_RE.match(entry.name)
                ):
                    profile_homes.append((entry.name, entry))
        plan.profiles = [name for name, _ in profile_homes]
    except Exception as exc:
        _record_discovery_failure(plan, "profile enumeration", exc)

    # --- service-managed PIDs (fleet-wide) ---------------------------------
    service_pids: set = set()
    try:
        from hermes_cli.gateway import _get_service_pids

        service_pids = _get_service_pids(all_profiles=True) or set()
    except Exception as exc:
        _record_discovery_failure(plan, "service-PID probe", exc)

    # --- SCM-supervised gateway PIDs (Windows) ------------------------------
    # find_windows_gateway_services() maps validated gateway PIDs through
    # process ancestry to running SCM service PIDs (no-op off Windows). The
    # update's pause phase stops these via `sc.exe stop` / restarts via
    # `sc.exe start`, so the plan must carry the matching mechanism id for
    # the #91277 Phase 2 reconciliation and the fleet check.
    windows_service_pids: set = set()
    try:
        from hermes_cli.gateway import find_windows_gateway_services

        windows_service_pids = {
            int(service.gateway_pid)
            for service in find_windows_gateway_services()
        }
    except Exception as exc:
        _record_discovery_failure(
            plan, "Windows SCM service-ownership probe", exc
        )

    # --- per-profile gateways (PID files + runtime status stamps) ----------
    seen_pids: set[int] = set()
    try:
        from gateway.status import _pid_exists, read_runtime_status

        for profile, home in profile_homes:
            # Prefer the gateway-owned control socket (#92091): identity
            # declared by the process itself, including its own supervisor
            # provenance — no argv/PID inference. Scan fallback below.
            identity = None
            try:
                from gateway.control_socket import identify_gateway

                identity = identify_gateway(home)
            except Exception:
                identity = None
            if identity:
                try:
                    sock_pid = int(identity.get("pid"))
                except (TypeError, ValueError):
                    sock_pid = None
                if sock_pid is not None:
                    if sock_pid in seen_pids:
                        # One multiplex gateway can answer identify for
                        # several profile homes — one runtime record per
                        # process, not per home.
                        continue
                    seen_pids.add(sock_pid)
                    declared = identity.get("supervisor")
                    supervisor = (
                        str(declared)
                        if declared
                        else _detect_supervisor_for_pid(
                            sock_pid, service_pids, windows_service_pids
                        )
                    )
                    sock_sha = identity.get("code_sha")
                    plan.runtimes.append(
                        RuntimeRecord(
                            kind="gateway",
                            profile=profile,
                            pid=sock_pid,
                            supervisor=supervisor,
                            code_sha=str(sock_sha) if sock_sha else None,
                            code_version=identity.get("code_version"),
                            restart_via=_restart_mechanism(supervisor, profile),
                        )
                    )
                    continue
            record = read_runtime_status(home / "gateway_state.json")
            pid: Optional[int] = None
            code_sha = code_version = None
            if record:
                try:
                    pid = int(record.get("pid"))
                except (TypeError, ValueError):
                    pid = None
                code_sha = record.get("code_sha")
                code_version = record.get("code_version")
            if pid is None or not _pid_exists(pid):
                continue
            seen_pids.add(pid)
            supervisor = _detect_supervisor_for_pid(
                pid, service_pids, windows_service_pids
            )
            plan.runtimes.append(
                RuntimeRecord(
                    kind="gateway",
                    profile=profile,
                    pid=pid,
                    supervisor=supervisor,
                    code_sha=str(code_sha) if code_sha else None,
                    code_version=code_version,
                    restart_via=_restart_mechanism(supervisor, profile),
                )
            )
    except Exception as exc:
        _record_discovery_failure(plan, "gateway-state inventory", exc)

    # PID-file mapped gateways not covered by a runtime-status record
    try:
        from hermes_cli.gateway import find_profile_gateway_processes

        for proc in find_profile_gateway_processes():
            if proc.pid in seen_pids:
                continue
            seen_pids.add(proc.pid)
            supervisor = _detect_supervisor_for_pid(
                proc.pid, service_pids, windows_service_pids
            )
            plan.runtimes.append(
                RuntimeRecord(
                    kind="gateway",
                    profile=proc.profile,
                    pid=proc.pid,
                    supervisor=supervisor,
                    restart_via=_restart_mechanism(supervisor, proc.profile),
                )
            )
    except Exception as exc:
        _record_discovery_failure(plan, "PID-file gateway inventory", exc)

    # Serve/dashboard backends from the spawn ledger (#63206). These are the
    # runtimes the gateway collectors above can never see: a manually
    # launched `hermes serve --host <ip>` for a remote Desktop, or a
    # long-lived `hermes dashboard`. Every serve/dashboard registers itself
    # (with structured host/port/profile since #63206) at startup, and
    # ledger_entries() live-verifies (pid, create_time) so PID reuse never
    # fabricates a row. A backend is treated as ours to stop and respawn
    # ONLY when its recorded spawner is provably dead; anything else is
    # Desktop-supervised, and those restart via the Desktop's own respawn,
    # not ours.
    try:
        from hermes_cli.process_identity import ledger_entries, spawner_is_dead

        # strict=True: a corrupt/unreadable ledger raises instead of reading
        # as an empty roster, and the raise lands in the ``except`` below as
        # a recorded discovery error. Without it, a damaged ledger and a box
        # with no serve/dashboard backends produce the identical plan — and
        # the second one authorizes the mutation.
        for entry in ledger_entries(strict=True):
            purpose = entry.get("purpose")
            if purpose not in ("serve", "dashboard"):
                continue
            pid = entry.get("pid")
            if not isinstance(pid, int) or pid in seen_pids:
                continue
            seen_pids.add(pid)
            # Only a PROVABLY DEAD spawner licenses the manual-serve
            # treatment (stop by PID, relaunch by respawn). ``None`` — no
            # spawner recorded, or one whose (pid, create_time) cannot be
            # probed — is not evidence that nobody is supervising this
            # backend: a Desktop app whose spawn tag never reached the child,
            # or whose process we may not query, reads exactly the same. So
            # anything short of proof is handled as Desktop-supervised, which
            # makes the stop refuse and ask for manual intervention instead
            # of killing a PID something else will immediately respawn onto
            # pre-update code.
            spawner_dead = spawner_is_dead(entry)
            supervisor = "manual-serve" if spawner_dead is True else "desktop"
            profile = str(entry.get("profile") or "default")
            argv_list = entry.get("argv_list")
            detail = {
                # ``argv`` is the lossy legacy rendering; ``argv_list`` is the
                # command itself. The relaunch prefers the list, then the
                # structured host/port/profile, and refuses a legacy string it
                # cannot prove round-trips (see ``_respawn_recorded_runtime``).
                "argv": entry.get("argv") or "",
                "argv_list": (
                    [str(part) for part in argv_list]
                    if isinstance(argv_list, (list, tuple)) and argv_list
                    else None
                ),
                "host": entry.get("host") or "",
                "port": entry.get("port"),
                # Set in the replacement's environment. Nothing produced this
                # before, so the respawn silently inherited the updater's home.
                "hermes_home": str(entry.get("hermes_home") or ""),
                # Self-stamped by the backend; the only SHA proof for a runtime
                # kind that never writes gateway_state.json.
                "code_sha": str(entry.get("code_sha") or ""),
            }
            if spawner_dead is None:
                # Presumed-supervised, not proven so. The stop refuses on it
                # exactly like a live Desktop, but a POSITIVE supervisor
                # identity (a systemd unit, a launchd label, an SCM service)
                # found by ``_attach_supervisor_identities`` still outranks
                # the presumption: that is hard evidence of who owns the
                # runtime, and stopping it through its unit is both safe and
                # respawn-proof. A provably-alive Desktop spawner carries no
                # such flag and is never downgraded.
                detail["supervisor_unproven"] = True
            create_time = entry.get("create_time")
            if isinstance(create_time, (int, float)):
                detail["start_time"] = float(create_time)
            # The supervisor's own forge-proof identity. The stop phase
            # re-checks it before signalling: a Desktop app that is still
            # alive answers the kill with a fresh backend on pre-update
            # code, and that respawn lands INSIDE the mutation window
            # where the old-PID exit check cannot see it (the replacement
            # has a different PID).
            spawner_pid = entry.get("spawner_pid")
            if isinstance(spawner_pid, int) and spawner_pid > 0:
                detail["spawner_pid"] = spawner_pid
                detail["spawner_create"] = entry.get("spawner_create")
            plan.runtimes.append(
                RuntimeRecord(
                    kind=str(purpose),
                    profile=profile,
                    pid=pid,
                    supervisor=supervisor,
                    restart_via=_restart_mechanism(supervisor, profile),
                    detail=detail,
                )
            )
    except Exception as exc:
        _record_discovery_failure(
            plan, "serve/dashboard ledger inventory", exc
        )

    _attach_supervisor_identities(plan)
    return plan


# Supervisors whose classification is an inference the identity probe may
# legitimately correct. ``desktop`` is excluded on purpose: the Desktop app
# respawns its own backend, and that ownership outranks any unit the
# backend happens to sit in.
_IDENTITY_UPGRADABLE_SUPERVISORS = frozenset({"manual", "manual-serve", "service"})


def _attach_supervisor_identities(plan: UpdatePlan) -> None:
    """Fill each runtime's EXACT supervisor identity, in place.

    Runs while every runtime is still alive — the whole point. A custom
    unit name lives in the process's cgroup, which disappears with the
    process, so a post-stop probe (the old late-cleanup discovery) can
    only ever guess. Also records the cgroup itself, which the updater's
    isolation check compares against.

    Never raises: a runtime whose identity cannot be read keeps whatever
    the collectors inferred.
    """
    try:
        runtimes = [r for r in plan.runtimes if isinstance(r, RuntimeRecord)]
        if not runtimes:
            return
        launchd_labels = _live_launchd_labels()
        windows_services = _windows_service_names_by_pid()
        for runtime in runtimes:
            pid = runtime.pid
            if not isinstance(pid, int) or pid <= 0:
                continue
            # Forge-proof process identity, captured while the runtime is
            # still alive — the stop phase revalidates against it before
            # signalling. Collectors that already know it (the spawn
            # ledger live-verifies its own) keep their value.
            if runtime.detail.get("start_time") is None:
                start_time = _process_start_time(pid)
                if start_time is not None:
                    runtime.detail["start_time"] = start_time
            identity = capture_supervisor_identity(
                pid,
                launchd_labels=launchd_labels,
                windows_services=windows_services,
            )
            if identity.cgroup:
                runtime.detail["cgroup"] = identity.cgroup
            if not identity.unit:
                continue
            runtime.unit = identity.unit
            runtime.unit_scope = identity.scope
            if identity.scope == "launchd":
                plist = launchd_plist_for_label(identity.unit)
                if plist:
                    runtime.detail["plist"] = plist
                    domain = launchd_domain_for_plist(plist)
                    if domain:
                        runtime.detail["launchd_domain"] = domain
            upgradable = (
                runtime.supervisor in _IDENTITY_UPGRADABLE_SUPERVISORS
                or bool(runtime.detail.get("supervisor_unproven"))
            )
            if upgradable:
                runtime.detail.pop("supervisor_unproven", None)
                if identity.scope == "launchd":
                    runtime.supervisor = "launchd"
                elif identity.scope == "scm":
                    runtime.supervisor = "windows-service"
                else:
                    runtime.supervisor = "systemd"
                runtime.restart_via = _restart_mechanism(
                    runtime.supervisor, runtime.profile
                )
    except Exception as exc:
        _record_discovery_failure(plan, "supervisor-identity capture", exc)


def print_update_plan(plan: UpdatePlan) -> None:
    """Human-readable plan — what the update will touch and how."""
    print("Update plan:")
    print(f"  Install: {plan.install_method}", end="")
    if plan.expected_version:
        print(f" (v{plan.expected_version}", end="")
        if plan.expected_sha:
            print(f" @ {plan.expected_sha[:8]}", end="")
        print(")", end="")
    print()
    if not plan.updatable_in_place:
        print("  ⚠ This install is NOT updatable in place.")
        print(f"    Update via: {plan.update_mechanism}")
    profiles = ", ".join(plan.profiles) if plan.profiles else "(none found)"
    print(f"  Profiles: {profiles}")
    if not plan.runtimes:
        print("  Running Hermes services: none detected — code swap only.")
        return
    print(f"  Running services to restart ({len(plan.runtimes)}):")
    for runtime in plan.runtimes:
        sha = f" @ {runtime.code_sha[:8]}" if runtime.code_sha else ""
        print(
            f"    • {runtime.kind} [{runtime.profile}] pid {runtime.pid}"
            f" — {runtime.supervisor}{sha}"
        )
        print(
            "      restart: "
            f"{describe_restart_mechanism(runtime.restart_via, runtime.profile)}"
        )


def match_runtime_outcomes(
    plan: "UpdatePlan",
    *,
    restarted_services: list,
    relaunched_profiles: list,
    externally_supervised_profiles: list,
    killed_pids: set,
    failed_units: list,
) -> list[dict[str, Any]]:
    """Reconcile the plan's runtimes against what the restart phase DID.

    #91277 Phase 2 (restart via declared mechanism): the platform restart
    branches each re-discover their own targets, so a runtime the plan saw
    can be missed entirely with no signal. This cross-checks every planned
    runtime against the phase's bookkeeping and returns one outcome row per
    runtime::

        {"kind", "profile", "pid", "mechanism", "outcome"}

    outcome: ``restarted`` (service restarted / profile relaunched /
    handed to external supervisor), ``stopped`` (pid killed, watcher or
    operator relaunches), ``failed`` (in the phase's failed/stale list) or
    ``unaccounted`` — the plan saw it and NO bookkeeping mentions it: the
    blind-spot tripwire (same philosophy as the fleet matrix's DOWN row).
    Never raises; on any probe error returns what it has.
    """
    outcomes: list[dict[str, Any]] = []
    try:
        failed_set = {str(u) for u in (failed_units or [])}
        restarted_set = {str(s) for s in (restarted_services or [])}
        relaunched = set(relaunched_profiles or [])
        external = set(externally_supervised_profiles or [])
        killed = {int(p) for p in (killed_pids or set())}

        def _identity_matches(unit: str, candidates: set) -> bool:
            """Exact supervisor-identity match against a bookkeeping list.

            ``.service`` is optional on both sides because systemctl
            accepts (and echoes) either spelling.
            """
            unit = (unit or "").strip()
            if not unit:
                return False
            aliases = {unit, unit.removesuffix(".service")}
            for candidate in candidates:
                name = str(candidate).strip()
                if name in aliases or name.removesuffix(".service") in aliases:
                    return True
            return False

        for runtime in plan.runtimes:
            r = runtime if isinstance(runtime, RuntimeRecord) else None
            if r is None:
                continue
            outcome = "unaccounted"
            # Exact identity FIRST. A runtime carrying a recorded unit is
            # reconciled by that unit only: profile-substring matching
            # both invents coverage (a restarted `hermes-gateway.service`
            # "accounting for" an untouched `acme-dash.service` on the
            # same profile) and misses custom units entirely.
            if _identity_matches(r.unit, failed_set):
                outcome = "failed"
            elif _identity_matches(r.unit, restarted_set):
                outcome = "restarted"
            elif r.profile in external:
                # Someone else owns this runtime's lifecycle, and they
                # report per profile, not per unit — the only signal there
                # is.
                outcome = "restarted"
            elif r.unit:
                if r.pid is not None and r.pid in killed:
                    outcome = "stopped"
            elif r.profile in relaunched:
                outcome = "restarted"
            elif r.pid is not None and r.pid in killed:
                outcome = "stopped"
            elif any(
                r.profile in unit or (r.profile == "default" and "hermes-gateway" in unit)
                for unit in failed_set
            ):
                outcome = "failed"
            elif any(
                r.profile in svc or (r.profile == "default" and "hermes-gateway" in svc)
                for svc in restarted_set
            ):
                outcome = "restarted"
            outcomes.append(
                {
                    "kind": r.kind,
                    "profile": r.profile,
                    "pid": r.pid,
                    "mechanism": r.restart_via,
                    "outcome": outcome,
                }
            )
    except Exception as exc:
        logger.debug("Runtime-outcome reconciliation failed: %s", exc)
    return outcomes


def report_unaccounted_runtimes(outcomes: list[dict[str, Any]]) -> bool:
    """Print a loud warning for runtimes the restart phase never touched.

    Returns True when at least one planned runtime is unaccounted — the
    caller escalates exactly like a STALE/DOWN fleet row (exit 1): a runtime
    the plan promised to restart, silently missed, is the class this phase
    exists to kill.
    """
    missed = [o for o in outcomes if o.get("outcome") == "unaccounted"]
    if not missed:
        return False
    print()
    print("  ⚠ Planned runtimes the restart phase never touched:")
    for o in missed:
        print(
            f"    ✗ {o['kind']} [{o['profile']}] pid {o['pid']}"
            f" — planned mechanism: {o['mechanism']}"
        )
    print("    Restart them manually, then verify:")
    print("      hermes gateway restart                # active profile")
    print("      hermes -p <profile> gateway restart   # named profile")
    return True


def record_plan_in_receipt(plan: UpdatePlan) -> None:
    """Attach the inventory to the active update receipt. Never raises."""
    try:
        import hermes_cli.update_receipt as ur

        if ur._current is not None:
            ur._current.data["plan"] = plan.to_dict()
    except Exception as exc:
        logger.debug("Could not record plan in receipt: %s", exc)
