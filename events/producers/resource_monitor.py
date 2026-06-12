"""ResourcePressureMonitor — emits RESOURCE_PRESSURE on system-resource exhaustion.

Sibling of GatewayHealthMonitor: where that producer watches WhatsApp/Telegram
connectivity, this one watches the *host's* commit charge, pagefile allocation,
and C: free space, and fires a high-priority alert BEFORE the disk gets eaten.

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
  * C: free       == ``shutil.disk_usage(SystemDrive).free``

``psutil`` is a hard dependency too, but its ``virtual_memory()`` reports
physical RAM (not commit) and its ``swap_memory()`` interpretation of the
pagefile is version-dependent; GlobalMemoryStatusEx is the precise, stable
source for the commit numbers this alert is about.

Emission policy
---------------
Edge-triggered with a re-arm cooldown (mirrors the gateway lag-alert pattern):
fire once on the rising edge of "any trigger active", stay quiet while the
episode persists, and re-ping every ``re_alert_cooldown_seconds`` if it drags
on. A falling edge (pressure clears) resets the episode so the next rise fires
immediately. This keeps a sustained 22-minute incident to a handful of alerts
instead of one every poll.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

_GB = 1024 ** 3

# Default thresholds — tuned to the 2026-06-11 incident shape.
DEFAULT_COMMIT_PCT_THRESHOLD = 85.0      # commit charge > this % of the limit
DEFAULT_DISK_FREE_GB_THRESHOLD = 15.0    # C: free below this many GB
DEFAULT_PAGEFILE_GROWTH_GB_THRESHOLD = 2.0   # pagefile grew more than this...
DEFAULT_GROWTH_WINDOW_SECONDS = 600.0    # ...within this trailing window (10 min)
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 900.0    # re-ping a sustained episode every 15 min


@dataclass(frozen=True)
class ResourceSample:
    """A single point-in-time reading of host resource pressure (bytes)."""

    commit_used_bytes: int
    commit_limit_bytes: int
    pagefile_allocated_bytes: int
    disk_free_bytes: int

    @property
    def commit_pct(self) -> float:
        if self.commit_limit_bytes <= 0:
            return 0.0
        return 100.0 * self.commit_used_bytes / self.commit_limit_bytes


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
        pagefile_growth_gb_threshold: float = DEFAULT_PAGEFILE_GROWTH_GB_THRESHOLD,
        growth_window_seconds: float = DEFAULT_GROWTH_WINDOW_SECONDS,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
    ):
        self.bus = bus
        self._sampler = sampler or sample_resources
        self._clock = clock or time.monotonic
        self.commit_pct_threshold = commit_pct_threshold
        self.disk_free_gb_threshold = disk_free_gb_threshold
        self.pagefile_growth_gb_threshold = pagefile_growth_gb_threshold
        self.growth_window_seconds = growth_window_seconds
        self.re_alert_cooldown_seconds = re_alert_cooldown_seconds

        # Rolling (monotonic_ts, pagefile_bytes) window for growth detection.
        self._pagefile_window: List[Tuple[float, int]] = []
        # Edge-trigger state.
        self._in_pressure: bool = False
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
        if sample.disk_free_bytes < self.disk_free_gb_threshold * _GB:
            reasons.append("disk_low")
        if growth_bytes > self.pagefile_growth_gb_threshold * _GB:
            reasons.append("pagefile_growth")

        if not reasons:
            # Pressure cleared (or never present): reset the episode so the
            # next rising edge fires immediately rather than waiting a cooldown.
            self._in_pressure = False
            return None

        # Pressure is active. Decide whether to emit: rising edge always; a
        # sustained episode re-pings only after the cooldown elapses.
        rising_edge = not self._in_pressure
        cooldown_elapsed = (
            self._last_emit is None
            or (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        self._in_pressure = True
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
            "pagefile_allocated_gb": round(sample.pagefile_allocated_bytes / _GB, 2),
            "pagefile_growth_gb_10min": round(growth_bytes / _GB, 2),
            "disk_c_free_gb": round(sample.disk_free_bytes / _GB, 2),
            "thresholds": {
                "commit_pct": self.commit_pct_threshold,
                "disk_free_gb": self.disk_free_gb_threshold,
                "pagefile_growth_gb": self.pagefile_growth_gb_threshold,
                "growth_window_min": round(self.growth_window_seconds / 60.0, 1),
            },
        }
        logger.warning(
            "Resource pressure: %s — commit %.1f%% (%.1f/%.1f GB) · "
            "pagefile %.1f GB (+%.1f GB/%.0fm) · C: %.1f GB free",
            ",".join(reasons),
            payload["commit_pct"], payload["commit_used_gb"], payload["commit_limit_gb"],
            payload["pagefile_allocated_gb"], payload["pagefile_growth_gb_10min"],
            payload["thresholds"]["growth_window_min"], payload["disk_c_free_gb"],
        )
        return self.bus.emit(
            event_type=EventType.RESOURCE_PRESSURE,
            source="system",
            payload=payload,
            tags=["resource", "pressure"] + reasons,
        )
