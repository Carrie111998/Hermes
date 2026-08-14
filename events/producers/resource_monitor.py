"""ResourcePressureMonitor — emits RESOURCE_PRESSURE on system-resource exhaustion.

Sibling of GatewayHealthMonitor: where that producer watches WhatsApp/Telegram
connectivity, this one watches the *host's* commit charge, physical RAM,
pagefile allocation, and C: free space, and fires a high-priority alert BEFORE
the disk gets eaten or paging takes services down.

Why this exists
---------------
On 2026-06-11 the machine's commit charge climbed to 84.2/85.6 GB (98.4%) and
Windows expanded ``pagefile.sys`` from 36 GB to 54.4 GB in ~22 minutes,
consuming ~18 GB of C: — with ZERO alerting. Windows' own
Resource-Exhaustion-Detector logged nothing, and the gateway's only health
signal (GatewayHealthMonitor) was busy chattering ``watchdog_burst`` events
about platform probes while the disk bled out. This monitor closes that gap.

How metrics are sampled (and why NOT via WMI)
---------------------------------------------
The failure mode is commit-charge exhaustion, so the sampler must NOT itself
commit much memory — spawning ``wmic`` / ``Get-CimInstance`` to read WMI costs
~50-100 MB per sample and can fail with ERROR_COMMITMENT_LIMIT exactly when we
most need the alert (``wmic`` is also being removed on Windows 11 26200). So we
read the same numbers WMI would report, but in-process via
``kernel32.GlobalMemoryStatusEx`` and ``shutil.disk_usage`` — no subprocess:

  * commit limit  == ``MEMORYSTATUSEX.ullTotalPageFile``
                     (byte-identical to Win32_OperatingSystem.TotalVirtualMemorySize KB * 1024)
  * commit avail  == ``MEMORYSTATUSEX.ullAvailPageFile``
                     (== Win32_OperatingSystem.FreeVirtualMemory KB * 1024)
  * commit used   == limit - avail
  * pagefile alloc ~= ``ullTotalPageFile - ullTotalPhys`` (the pagefile's
                     contribution to the commit limit; tracks
                     Win32_PageFileUsage.AllocatedBaseSize — on the incident
                     machine 85.6 - 31 ~= 54.6 GB, matching the recorded 54.4)
  * phys total    == ``ullTotalPhys``   (physical RAM installed)
  * phys avail    == ``ullAvailPhys``   (== psutil.virtual_memory().available)
  * C: free       == ``shutil.disk_usage(SystemDrive).free``

The physical-RAM axis was added 2026-07-16: a paging storm (phys 96.4%,
Docker/PG down, laptop-monitor healthy-count collapsing to 2-9/75 twice in one
day) produced ZERO events here because commit charge sat at 50-73% throughout —
the monitor was blind to the axis that actually killed services.

``psutil`` is a hard dependency too, but its ``virtual_memory()`` reports
physical RAM (not commit) and its ``swap_memory()`` interpretation of the
pagefile is version-dependent; GlobalMemoryStatusEx is the precise, stable
source for the commit numbers this alert is about.

Emission policy
---------------
Edge-triggered with hysteresis and a re-alert cooldown (mirrors the gateway
lag-alert pattern): fire once on the rising edge of "any trigger active",
stay quiet while the episode persists, and re-ping every
``re_alert_cooldown_seconds`` if it drags on. A breached axis stays latched
until it is *comfortably* clear of its trigger — its disarm level, e.g.
commit back below 80% against the 85% trigger — and the episode only ends
once every latched axis has cleared; then the next rising edge fires
immediately. The disarm gap exists because re-arming at the trigger itself
let threshold hover storm: on 2026-06-11 22:52-23:21Z commit oscillated
84.x<->85.x and fired six alerts in 29 minutes, each dip re-arming the edge
and each re-cross sidestepping the cooldown. Genuine recovery keeps the
immediate-fire property; hovering at the threshold does not.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Set, Tuple

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

_GB = 1024 ** 3

# Default thresholds — tuned to the 2026-06-11 incident shape.
DEFAULT_COMMIT_PCT_THRESHOLD = 85.0      # commit charge > this % of the limit
# Raised 15.0 -> 60.0 on 2026-08-14. 15 GB sat BELOW the amplitude of normal daily
# churn on this box: the Docker data VHDX routinely allocates 40-50 GB of transient
# blocks between nightly fstrim runs (audit history: 46 -> 95 GB in one night on
# 08-13/14). A 15 GB trigger therefore fired only once the disk was already hours
# from zero -- it tripped on 11 separate days since 07-17, five of them reaching
# 0.0 GB free. 60 GB gives roughly a full churn cycle of warning instead.
DEFAULT_DISK_FREE_GB_THRESHOLD = 60.0    # C: free below this many GB
# Second, lower disk axis added 2026-08-14. ``disk_low`` at 60 GB is an EARLY
# WARNING and must stay cheap to receive (routing keeps it a WARN in the alerts
# topic), or it pages every cooldown for a whole day while the disk sits at
# 55 GB and everyone learns to ignore it. ``disk_critical`` is the one that
# pages: below it the box is close enough to wedging that a human must act now.
DEFAULT_DISK_FREE_GB_CRITICAL = 25.0     # C: free below this many GB -> ACT/page
DEFAULT_PAGEFILE_GROWTH_GB_THRESHOLD = 2.0   # pagefile grew more than this...
DEFAULT_GROWTH_WINDOW_SECONDS = 600.0    # ...within this trailing window (10 min)
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 900.0    # re-ping a sustained episode every 15 min
# Physical-RAM axis — added 2026-07-16 after a paging storm (phys 96.4%,
# services dying, laptop-monitor healthy-count collapsed twice) emitted zero
# events because commit charge stayed at 50-73% the whole day.
DEFAULT_PHYS_PCT_THRESHOLD = 92.0        # physical RAM used > this %

# Hysteresis disarm levels — a breached axis re-arms only once comfortably
# clear of its trigger. Re-arming at the trigger itself let threshold hover
# storm (2026-06-11 22:52-23:21Z: six alerts in 29 min at commit 84.x<->85.x).
DEFAULT_COMMIT_PCT_DISARM = 80.0             # commit back below this % clears
DEFAULT_DISK_FREE_GB_DISARM = 75.0           # C: free back above this clears
# Every latching axis MUST have a disarm level. Without one it enters
# ``_latched`` via ``reasons`` but can never leave through ``comfortably_clear``,
# so a single breach latches the episode FOREVER -- ``was_in_episode`` stays
# True, no later rising edge ever fires, and the monitor silently degrades to
# cooldown-only re-pings. That is the phys-axis bug documented above; do not
# repeat it. Gap mirrors the low axis (60 -> 75).
DEFAULT_DISK_FREE_GB_CRITICAL_DISARM = 40.0  # C: free back above this clears
DEFAULT_PAGEFILE_GROWTH_GB_DISARM = 1.0      # in-window growth below this clears
# The phys axis (2026-07-16) postdates the original disarm set (2026-06-12), so
# it had no disarm level until these two landed together. Without one it can
# enter ``_latched`` via ``reasons`` but never leave through
# ``comfortably_clear``, so a single phys breach would latch the episode
# FOREVER — ``was_in_episode`` stays True, no later rising edge ever fires, and
# the monitor silently degrades to cooldown-only re-pings. Gap mirrors commit's
# 5 points (92 -> 87).
DEFAULT_PHYS_PCT_DISARM = 87.0               # phys back below this % clears


@dataclass(frozen=True)
class ResourceSample:
    """A single point-in-time reading of host resource pressure (bytes).

    ``phys_*`` fields default to 0 so pre-2026-07-16 constructor shapes keep
    working; phys_pct then reads 0.0 and the phys axis never triggers.
    """

    commit_used_bytes: int
    commit_limit_bytes: int
    pagefile_allocated_bytes: int
    disk_free_bytes: int
    phys_total_bytes: int = 0
    phys_avail_bytes: int = 0

    @property
    def commit_pct(self) -> float:
        if self.commit_limit_bytes <= 0:
            return 0.0
        return 100.0 * self.commit_used_bytes / self.commit_limit_bytes

    @property
    def phys_pct(self) -> float:
        if self.phys_total_bytes <= 0:
            return 0.0
        used = max(0, self.phys_total_bytes - self.phys_avail_bytes)
        return 100.0 * used / self.phys_total_bytes


def _system_drive_root() -> str:
    """Return the system drive root for disk-free checks, e.g. ``C:\\``."""
    drive = os.environ.get("SystemDrive", "C:")
    return drive + os.sep


def _global_memory_status():
    """Call kernel32.GlobalMemoryStatusEx and return the populated struct.

    Imported and constructed lazily so this module imports cleanly on any
    platform; only called from sample_resources() under sys.platform check.
    """
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise ctypes.WinError()
    return stat


def sample_resources() -> Optional[ResourceSample]:
    """Read host commit charge / pagefile / C: free in-process (no subprocess).

    Returns None on non-Windows hosts or if any read fails — the caller treats
    None as "nothing to evaluate" so the gateway poll loop never crashes.
    """
    if sys.platform != "win32":
        return None
    try:
        stat = _global_memory_status()
        commit_limit = int(stat.ullTotalPageFile)
        commit_avail = int(stat.ullAvailPageFile)
        commit_used = max(0, commit_limit - commit_avail)
        # Pagefile contribution to the commit limit ~= allocated pagefile size.
        pagefile_alloc = max(0, int(stat.ullTotalPageFile) - int(stat.ullTotalPhys))
        disk_free = shutil.disk_usage(_system_drive_root()).free
        return ResourceSample(
            commit_used_bytes=commit_used,
            commit_limit_bytes=commit_limit,
            pagefile_allocated_bytes=pagefile_alloc,
            disk_free_bytes=int(disk_free),
            phys_total_bytes=int(stat.ullTotalPhys),
            phys_avail_bytes=int(stat.ullAvailPhys),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("sample_resources failed: %s", e)
        return None


class ResourcePressureMonitor:
    """Samples host resource pressure and emits RESOURCE_PRESSURE on the edge.

    Call check() every ~60 seconds from the subscriber poll loop. Sampling and
    the clock are injectable so the evaluation core is fully testable without
    real hardware or sleeps.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        sampler: Optional[Callable[[], Optional[ResourceSample]]] = None,
        clock: Optional[Callable[[], float]] = None,
        commit_pct_threshold: float = DEFAULT_COMMIT_PCT_THRESHOLD,
        disk_free_gb_threshold: float = DEFAULT_DISK_FREE_GB_THRESHOLD,
        disk_free_gb_critical: float = DEFAULT_DISK_FREE_GB_CRITICAL,
        pagefile_growth_gb_threshold: float = DEFAULT_PAGEFILE_GROWTH_GB_THRESHOLD,
        growth_window_seconds: float = DEFAULT_GROWTH_WINDOW_SECONDS,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
        phys_pct_threshold: float = DEFAULT_PHYS_PCT_THRESHOLD,
        commit_pct_disarm: float = DEFAULT_COMMIT_PCT_DISARM,
        disk_free_gb_disarm: float = DEFAULT_DISK_FREE_GB_DISARM,
        disk_free_gb_critical_disarm: float = DEFAULT_DISK_FREE_GB_CRITICAL_DISARM,
        pagefile_growth_gb_disarm: float = DEFAULT_PAGEFILE_GROWTH_GB_DISARM,
        phys_pct_disarm: float = DEFAULT_PHYS_PCT_DISARM,
    ):
        self.bus = bus
        self._sampler = sampler or sample_resources
        self._clock = clock or time.monotonic
        self.commit_pct_threshold = commit_pct_threshold
        self.disk_free_gb_threshold = disk_free_gb_threshold
        self.disk_free_gb_critical = disk_free_gb_critical
        self.pagefile_growth_gb_threshold = pagefile_growth_gb_threshold
        self.growth_window_seconds = growth_window_seconds
        self.re_alert_cooldown_seconds = re_alert_cooldown_seconds
        self.phys_pct_threshold = phys_pct_threshold
        self.commit_pct_disarm = commit_pct_disarm
        self.disk_free_gb_disarm = disk_free_gb_disarm
        self.disk_free_gb_critical_disarm = disk_free_gb_critical_disarm
        self.pagefile_growth_gb_disarm = pagefile_growth_gb_disarm
        self.phys_pct_disarm = phys_pct_disarm

        # Rolling (monotonic_ts, pagefile_bytes) window for growth detection.
        self._pagefile_window: List[Tuple[float, int]] = []
        # Edge-trigger state: per-axis hysteresis latches (reason strings). An
        # axis latches when its trigger breaches and unlatches only once
        # comfortably clear (its disarm level); episode = any axis latched.
        self._latched: Set[str] = set()
        self._last_emit: Optional[float] = None

    def check(self) -> Optional[str]:
        """Sample, evaluate, emit if a pressure edge fired. Returns event_id or None.

        Swallows sampler failures: a metric read blowing up must never crash
        the gateway poll loop.
        """
        try:
            sample = self._sampler()
        except Exception:
            logger.exception("ResourcePressureMonitor: sampler raised")
            return None
        if sample is None:
            return None
        return self.evaluate(sample, self._clock())

    def evaluate(self, sample: ResourceSample, now: float) -> Optional[str]:
        """Evaluate one sample at monotonic time ``now``; emit on rising edge.

        Pure given (sample, now) + internal state — the testable core. Records
        the pagefile reading into the growth window on EVERY call (even healthy
        ones) so a later sample has a baseline to compare against.
        """
        # Record + prune the pagefile growth window first, so the baseline is
        # captured even when this sample is otherwise healthy.
        self._pagefile_window.append((now, sample.pagefile_allocated_bytes))
        self._pagefile_window = [
            (ts, pf) for ts, pf in self._pagefile_window
            if now - ts <= self.growth_window_seconds
        ]
        window_min = min(pf for _, pf in self._pagefile_window)
        growth_bytes = sample.pagefile_allocated_bytes - window_min

        reasons: List[str] = []
        if sample.commit_pct > self.commit_pct_threshold:
            reasons.append("commit_high")
        if sample.phys_pct > self.phys_pct_threshold:
            reasons.append("phys_high")
        if sample.disk_free_bytes < self.disk_free_gb_threshold * _GB:
            reasons.append("disk_low")
        if sample.disk_free_bytes < self.disk_free_gb_critical * _GB:
            reasons.append("disk_critical")
        if growth_bytes > self.pagefile_growth_gb_threshold * _GB:
            reasons.append("pagefile_growth")

        # Hysteresis: an axis only unlatches once comfortably clear of its
        # trigger (the disarm level). A dip into the band between disarm and
        # trigger keeps the episode latched, so threshold hover cannot re-arm
        # the rising edge and storm past the cooldown.
        comfortably_clear: Set[str] = set()
        if sample.commit_pct < self.commit_pct_disarm:
            comfortably_clear.add("commit_high")
        if sample.phys_pct < self.phys_pct_disarm:
            comfortably_clear.add("phys_high")
        if sample.disk_free_bytes > self.disk_free_gb_disarm * _GB:
            comfortably_clear.add("disk_low")
        if sample.disk_free_bytes > self.disk_free_gb_critical_disarm * _GB:
            comfortably_clear.add("disk_critical")
        if growth_bytes < self.pagefile_growth_gb_disarm * _GB:
            comfortably_clear.add("pagefile_growth")

        was_in_episode = bool(self._latched)
        self._latched = (self._latched - comfortably_clear) | set(reasons)

        if not reasons:
            # Nothing breaching right now. If every latched axis also cleared
            # comfortably, the episode is over and the next rising edge fires
            # immediately rather than waiting a cooldown.
            return None

        # Pressure is active. Decide whether to emit: rising edge always; a
        # sustained episode re-pings only after the cooldown elapses.
        rising_edge = not was_in_episode
        cooldown_elapsed = (
            self._last_emit is None
            or (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        if not (rising_edge or cooldown_elapsed):
            return None

        self._last_emit = now
        return self._emit(sample, reasons, growth_bytes)

    def _emit(
        self, sample: ResourceSample, reasons: List[str], growth_bytes: int,
    ) -> str:
        payload = {
            "reasons": reasons,
            "commit_used_gb": round(sample.commit_used_bytes / _GB, 2),
            "commit_limit_gb": round(sample.commit_limit_bytes / _GB, 2),
            "commit_pct": round(sample.commit_pct, 1),
            "phys_used_pct": round(sample.phys_pct, 1),
            "phys_available_gb": round(sample.phys_avail_bytes / _GB, 2),
            "pagefile_allocated_gb": round(sample.pagefile_allocated_bytes / _GB, 2),
            "pagefile_growth_gb_10min": round(growth_bytes / _GB, 2),
            "disk_c_free_gb": round(sample.disk_free_bytes / _GB, 2),
            "thresholds": {
                "commit_pct": self.commit_pct_threshold,
                "phys_pct": self.phys_pct_threshold,
                "disk_free_gb": self.disk_free_gb_threshold,
                "disk_free_gb_critical": self.disk_free_gb_critical,
                "pagefile_growth_gb": self.pagefile_growth_gb_threshold,
                "growth_window_min": round(self.growth_window_seconds / 60.0, 1),
            },
        }
        logger.warning(
            "Resource pressure: %s — commit %.1f%% (%.1f/%.1f GB) · "
            "phys %.1f%% (%.1f GB avail) · "
            "pagefile %.1f GB (+%.1f GB/%.0fm) · C: %.1f GB free",
            ",".join(reasons),
            payload["commit_pct"], payload["commit_used_gb"], payload["commit_limit_gb"],
            payload["phys_used_pct"], payload["phys_available_gb"],
            payload["pagefile_allocated_gb"], payload["pagefile_growth_gb_10min"],
            payload["thresholds"]["growth_window_min"], payload["disk_c_free_gb"],
        )
        return self.bus.emit(
            event_type=EventType.RESOURCE_PRESSURE,
            source="system",
            payload=payload,
            tags=["resource", "pressure"] + reasons,
        )
