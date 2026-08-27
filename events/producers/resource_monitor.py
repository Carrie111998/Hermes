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

The spawn-latency axis (2026-08-27) exists for the same reason one generation
later: on 2026-08-26 a fleet of 84 concurrent agent sessions drove ~576
process creations+deaths per MINUTE and the box starved on PROCESS CHURN — a
trivial ``date`` took 11.8s, a CreateProcess round trip medianed 2.3s, and
the session reaper NO-OPed five times in a row — while commit read 74.7%
(ten points below its gate) and phys 89% (three below its trigger). Every
axis above measures memory or disk; nothing measured the resource that
actually saturated. This axis measures it directly: a rate-limited
CreateProcess round-trip probe (``SpawnLatencyProbe``), classified on
SUSTAINED readings so one slow spawn (a Defender cold scan, a disk hiccup)
never fires. Complementary to laptop-monitor's "Claude session fleet size"
tray row, which watches the fleet COUNT; this axis watches the churn COST.
Loops claim ``host-spawn-churn-20260826`` holds the full forensics.
DETECTION ONLY, like every axis here: it emits ``resource_pressure`` and
stops — no auto-kill, no gating; escalation rides the normal event routing.

``psutil`` is a hard dependency too, but its ``virtual_memory()`` reports
physical RAM (not commit) and its ``swap_memory()`` interpretation of the
pagefile is version-dependent; GlobalMemoryStatusEx is the precise, stable
source for the commit numbers this alert is about.

Emission policy
---------------
Edge-triggered with hysteresis and a re-alert cooldown (mirrors the gateway
lag-alert pattern): fire once on the rising edge of each axis, stay quiet
while that axis persists, and re-ping every ``re_alert_cooldown_seconds`` if
the episode drags on. The edge is PER AXIS — an axis that breaches while
another is already latched emits immediately rather than waiting out the
cooldown, because a new axis is new information and axes no longer share a
severity class (disk_critical pages; disk_low does not). A breached axis stays latched
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
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Set, Tuple

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

_GB = 1024 ** 3

# Default thresholds — tuned to the 2026-06-11 incident shape.
DEFAULT_COMMIT_PCT_THRESHOLD = 85.0      # commit charge > this % of the limit
# Raised 15.0 -> 60.0 on 2026-08-14, then corrected 60.0 -> 45.0 the same day.
# 15 GB sat BELOW the amplitude of normal daily churn on this box: the Docker data
# VHDX routinely allocates 40-50 GB of transient blocks between nightly fstrim runs
# (audit history: 46 -> 95 GB in one night on 08-13/14). A 15 GB trigger therefore
# fired only once the disk was already hours from zero -- it tripped on 11 separate
# days since 07-17, five of them reaching 0.0 GB free.
#
# But 60 GB overshot: this machine's BEST case, immediately after a full VHDX
# reclaim, is ~56.6 GB free. The axis was breaching at the instant it deployed
# (16:20:28Z) and could never clear, because clearing needed >75 GB and the churn
# cycle tops out ~20 GB below that. It re-emitted every cooldown for as long as it
# was live -- ~96 events/day into a topic the two-stage redesign existed to
# de-noise. THE INVARIANT: this trigger must sit BELOW the post-reclaim ceiling and
# its disarm must sit below it too, or the axis is latched from birth. 45 GB still
# gives most of a churn cycle of warning while leaving the disarm reachable.
DEFAULT_DISK_FREE_GB_THRESHOLD = 45.0    # C: free below this many GB
# Second, lower disk axis added 2026-08-14. ``disk_low`` at 45 GB is an EARLY
# WARNING and must stay cheap to receive (routing keeps it a WARN, in the
# security_and_system topic since 19a8dd9abd), or it pages every cooldown for
# a whole day while the disk sits at 55 GB and everyone learns to ignore it.
# ``disk_critical`` is the one that pages: below it the box is close enough to
# wedging that a human must act now. It sits BELOW disk_low, so disk_low always
# latches first on a filling disk — see the per-axis rising edge in evaluate().
DEFAULT_DISK_FREE_GB_CRITICAL = 25.0     # C: free below this many GB -> ACT/page
DEFAULT_PAGEFILE_GROWTH_GB_THRESHOLD = 2.0   # pagefile grew more than this...
DEFAULT_GROWTH_WINDOW_SECONDS = 600.0    # ...within this trailing window (10 min)
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 900.0    # re-ping a sustained episode every 15 min
# Physical-RAM axis — added 2026-07-16 after a paging storm (phys 96.4%,
# services dying, laptop-monitor healthy-count collapsed twice) emitted zero
# events because commit charge stayed at 50-73% the whole day.
DEFAULT_PHYS_PCT_THRESHOLD = 92.0        # physical RAM used > this %
# Spawn-latency axis (2026-08-27) — the churn storm's direct measurement.
# Derived from the 2026-08-26 storm forensics (loops claim
# host-spawn-churn-20260826): ten CreateProcess round trips of ``cmd /c
# exit`` during the storm measured min 471 / median 2272 / mean 2353 /
# max 6225 ms, against tens of ms on a healthy box. 1500 sits ~3x above
# worst-case healthy jitter (a Defender cold scan can cost a few hundred ms)
# yet below the storm's MEDIAN — and even its MIN reads only ~3x under it —
# so the storm shape trips the axis while a merely busy box does not. The
# axis is judged on the sustained FLOOR (the BEST of the last N readings),
# so a breach certifies that EVERY recent spawn was at least this slow.
DEFAULT_SPAWN_LATENCY_WARN_MS = 1500.0   # sustained floor > this many ms
# Sustained = this many consecutive measured readings, ~3 minutes at the 60s
# check cadence. One slow spawn is an anecdote; the 08-26 episode ran hours.
DEFAULT_SPAWN_SUSTAINED_SAMPLES = 3
# Readings older than this age out of the sustained window. Must sit well
# above the probe cadence (~60s) so rate-limited polls cannot starve the
# window, and generous enough that only a genuinely dead probe empties it —
# an empty window reads as comfortably clear (see evaluate()), because a
# probe that died mid-episode must not wedge the episode open forever (the
# phys-axis no-disarm bug in a third disguise).
DEFAULT_SPAWN_WINDOW_MAX_AGE_SECONDS = 300.0

# Hysteresis disarm levels — a breached axis re-arms only once comfortably
# clear of its trigger. Re-arming at the trigger itself let threshold hover
# storm (2026-06-11 22:52-23:21Z: six alerts in 29 min at commit 84.x<->85.x).
DEFAULT_COMMIT_PCT_DISARM = 80.0             # commit back below this % clears
# 52, not 75: the disarm must be REACHABLE. Post-reclaim steady state on this box
# is ~55-56.6 GB, so 52 leaves ~4.6 GB of headroom above the trim-cycle ceiling --
# enough that a genuinely recovered disk actually unlatches the episode. A disarm
# set above the ceiling (75 on 2026-08-14) is indistinguishable from having no
# disarm at all: the axis latches on first sample and re-pings forever.
DEFAULT_DISK_FREE_GB_DISARM = 52.0           # C: free back above this clears
# Every latching axis MUST have a disarm level. Without one it enters
# ``_latched`` via ``reasons`` but can never leave through ``comfortably_clear``,
# so a single breach latches the episode FOREVER -- ``was_in_episode`` stays
# True, no later rising edge ever fires, and the monitor silently degrades to
# cooldown-only re-pings. That is the phys-axis bug documented above; do not
# repeat it. Gap mirrors the low axis (45 -> 52).
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
# Healthy spawns read tens of ms, so a recovered box clears 1000 trivially —
# the disarm-reachability invariant from the 08-14 disk tuning holds by a
# wide margin. The 500 ms gap below the 1500 trigger mirrors commit's
# 85 -> 80: hovering at the trigger cannot re-arm the edge. Clearing
# additionally requires the WHOLE sustained window below this level (the
# mirror of the sustained breach test), so one lucky spawn mid-storm cannot
# end the episode; see evaluate().
DEFAULT_SPAWN_LATENCY_DISARM_MS = 1000.0     # window max back below this clears

# Disk severity bands (2026-08-14). ``normalize_for_fingerprint`` collapses digit
# runs to "N", so "C: free: 0.0 GB" and "C: free: 56.63 GB" fingerprint
# IDENTICALLY and the repeat guard suppressed a dying disk exactly as it
# suppressed a healthy one -- measured over 479 disk_low events, ONE fingerprint
# covered the whole range including 101 below 5 GiB and 13 at exactly 0.0 GiB.
# A band label is LETTERS, which the fingerprint can see, so crossing an edge
# mints a genuinely new message without touching the guard at all.
# Geometric on purpose: coarse where the slide is slow and undramatic, tight
# near zero where minutes matter. Descending -- the DEEPEST edge crossed wins,
# so a 45 -> 2 GiB drop is one "imminent" message, not five.
DISK_BANDS: Tuple[Tuple[float, str], ...] = (
    (45.0, "low"),
    (25.0, "critical"),
    (12.0, "severe"),
    (6.0, "emergency"),
    (3.0, "imminent"),
    (1.0, "full"),
)
# An announced edge re-arms only once free space recovers to this multiple of it
# (45->54, 25->30, 12->14.4, 6->7.2, 3->3.6, 1->1.2). Hovering at a boundary
# therefore cannot flap, while a genuine 11 -> 30 -> 11 GiB round trip does
# re-announce. Same shape as the per-axis ``comfortably_clear`` disarm levels
# above. The 45 edge's 54 GiB re-arm is unreachable in practice because
# disk_low disarms at 52 GiB, which ends the episode and resets every edge --
# that is the intended outcome, not a gap.
DISK_BAND_REARM_FACTOR = 1.2

# Commit / phys severity bands (2026-08-20). The 08-14 band work above cured the
# disk axis and stopped there, so commit and phys kept the identical pathology:
# ``band_changed`` was computed from ``disk_band_for`` alone, so once commit
# latched at its 85% trigger every later sample stamped ``sustained_repeat`` --
# which subscribers drop bus-only -- and an escalation from 85% to 99% could not
# reach chat at all. Measured 2026-08-20: commit hit 99.1% (126.09/127.20 GB),
# 24 ``commit_high`` events were emitted that day and 8 delivered; the 96.0%
# sample was among the silent ones. Same cure as disk: a LETTERS label the
# fingerprint can see, so crossing an edge mints a genuinely new message.
#
# ASCENDING, unlike DISK_BANDS -- for these axes higher is worse, so the DEEPEST
# edge crossed is the HIGHEST one. The shallowest edge is deliberately the axis's
# own trigger (85.0 == DEFAULT_COMMIT_PCT_THRESHOLD, 92.0 ==
# DEFAULT_PHYS_PCT_THRESHOLD), mirroring DISK_BANDS[0] == 45.0 == the disk
# trigger, so a first breach always carries a band.
#
# Every edge sits ABOVE its axis's disarm (commit 80, phys 87) so a genuinely
# recovered axis ends the episode and re-arms the whole set. Note the reachability
# invariant from the 08-14 disk tuning applies to DISARM levels, not to band
# edges: an unreachable disarm latches an axis forever, whereas an unreachable
# top band simply never announces (as disk's 1.0 GB "full" band mostly never does).
COMMIT_BANDS: Tuple[Tuple[float, str], ...] = (
    (85.0, "high"),
    (92.0, "severe"),
    (96.0, "critical"),
    (99.0, "exhausted"),
)
PHYS_BANDS: Tuple[Tuple[float, str], ...] = (
    (92.0, "high"),
    (96.0, "severe"),
    (98.0, "critical"),
    (99.5, "exhausted"),
)
# An announced percentage edge re-arms only once the axis falls this many points
# back below it. A MULTIPLICATIVE factor like DISK_BAND_REARM_FACTOR is wrong for
# a 0-100 bounded axis -- 96/1.2 is 80, which collides with commit's disarm and
# would make the ratchet meaningless. An absolute gap mirrors the per-axis disarm
# gaps instead (commit 85->80, phys 92->87), so hovering at an edge cannot flap
# while a real 96 -> 88 -> 96 round trip does re-announce.
BAND_REARM_GAP_PCT = 5.0

# Spawn-latency severity bands, in ms of sustained floor. Ascending like
# COMMIT/PHYS_BANDS — higher is worse — and the shallowest edge is
# deliberately the axis's own trigger, so a first breach always carries a
# band. "critical" (5000 sustained) marks the tool-timeout amplification
# regime: at multi-second spawns, harness tool calls start timing out, every
# timeout fires a ``taskkill /T /F`` that itself needs spawns plus a
# tree-walk, and the storm feeds itself (69-188 concurrent taskkills
# observed on 08-26). Because the band reads the sustained FLOOR, "critical"
# certifies every recent spawn exceeded 5s — the storm's shape, not one
# outlier. A probe that hits its wait timeout reports elapsed time as a
# FLOOR (>= the ~10s timeout), which lands here.
SPAWN_BANDS: Tuple[Tuple[float, str], ...] = (
    (1500.0, "high"),
    (5000.0, "critical"),
)
# An announced spawn edge re-arms only once the WORST reading in the window
# is comfortably below it (edge / 1.5): 1500 -> 1000 (== the axis disarm, so
# the shallow edge re-arms exactly when the axis itself clears — the same
# intended coincidence as disk's 45-edge vs its 52 disarm), 5000 -> 3333.
# Multiplicative like DISK_BAND_REARM_FACTOR because an absolute gap cannot
# fit both a 1500 and a 5000 edge; BAND_REARM_GAP_PCT is for 0-100 axes.
SPAWN_BAND_REARM_FACTOR = 1.5


def pct_band_for(
    pct: float, bands: Tuple[Tuple[float, str], ...]
) -> Tuple[Optional[str], Optional[float]]:
    """Highest ascending band edge ``pct`` has risen above.

    The percentage-axis twin of ``disk_band_for``: same contract (``(label,
    edge)``, or ``(None, None)`` below every edge), opposite direction, because
    for commit and phys a HIGHER reading is the worse one.
    """
    label: Optional[str] = None
    edge: Optional[float] = None
    for band_edge, band_label in bands:
        if pct > band_edge:
            label, edge = band_label, band_edge
    return label, edge


def disk_band_for(free_bytes: int) -> Tuple[Optional[str], Optional[float]]:
    """Deepest band whose edge ``free_bytes`` has fallen below.

    Returns ``(label, edge_gb)``, or ``(None, None)`` when free space is above
    every edge. Pure -- the ratchet that decides whether an edge may ANNOUNCE
    lives in the monitor, because only it knows the episode.
    """
    label: Optional[str] = None
    edge: Optional[float] = None
    for band_edge, band_label in DISK_BANDS:
        if free_bytes < band_edge * _GB:
            label, edge = band_label, band_edge
    return label, edge


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
    # Spawn-latency axis (2026-08-27): one CreateProcess round trip, in ms.
    # -1.0 means "not measured on this sample" (probe rate-limited, failed,
    # or absent) — the axis then neither breaches nor clears. Defaulted so
    # pre-2026-08-27 constructor shapes keep working, mirroring ``phys_*``.
    spawn_latency_ms: float = -1.0

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


# Probe pacing. 55s sits just under the ~60s check cadence so every poll can
# carry a fresh reading, while a caller polling faster than the cadence gets
# rate-limited to ~1 spawn/min — the probe must not contribute to the storm
# it measures (1/min against the ~576/min measured on 08-26 is ~0.2%).
DEFAULT_SPAWN_PROBE_MIN_INTERVAL_SECONDS = 55.0
# Hard ceiling on the wait for the child to exit. Sits ABOVE the deepest
# SPAWN_BANDS edge (5000), so a timed-out probe still classifies correctly:
# its elapsed reading (>= this) is a floor that lands in "critical".
DEFAULT_SPAWN_PROBE_TIMEOUT_MS = 10000.0


class SpawnLatencyProbe:
    """Measures one CreateProcess round trip (spawn -> child exit), in ms.

    The direct measurement of the resource the 2026-08-26 churn storm
    actually saturated. Spawns ``cmd /c exit`` — deliberately the same
    trivial child the storm forensics baselined (min 471 / median 2272 /
    max 6225 ms mid-storm; tens of ms healthy) — via ctypes CreateProcessW,
    not ``subprocess``: no pipe handles, no Python-side plumbing, just the
    syscall path whose latency is the phenomenon. The child is real churn
    (image load, a conhost, a Defender scan), which is the point: the probe
    pays exactly one unit of the cost it measures.

    Contract: callable returning latency in ms, or ``None`` for "no reading"
    (non-Windows, rate-limited, or the spawn itself failed). Rate limiting
    lives HERE, not in the monitor, so any caller — however hot its loop —
    costs at most ~1 spawn per ``min_interval_seconds``.

    Two measurement caveats, both deliberate:

    * ``CreateProcessW`` itself is a blocking call and cannot be timed out
      from within the calling thread — on a catastrophically wedged box the
      probe can stall for as long as the create takes (2.4s was measured
      mid-storm). The wait for the child's EXIT is what ``timeout_ms``
      bounds; on expiry the child is terminated and the elapsed time is
      returned as a FLOOR ("at least this slow"), which is precisely the
      reading a storm should produce.
    * The probe measures the round trip a real tool call pays (create +
      child run + exit), not the bare CreateProcess return — that is the
      quantity whose degradation times out harness tools and feeds the
      taskkill amplifier.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = DEFAULT_SPAWN_PROBE_MIN_INTERVAL_SECONDS,
        timeout_ms: float = DEFAULT_SPAWN_PROBE_TIMEOUT_MS,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.min_interval_seconds = min_interval_seconds
        self.timeout_ms = timeout_ms
        self._clock = clock or time.monotonic
        self._last_probe_at: Optional[float] = None

    def __call__(self) -> Optional[float]:
        if sys.platform != "win32":
            return None
        now = self._clock()
        if (
            self._last_probe_at is not None
            and (now - self._last_probe_at) < self.min_interval_seconds
        ):
            return None
        # Stamped BEFORE the measurement so a slow probe cannot compress the
        # interval to its successor.
        self._last_probe_at = now
        try:
            return self._measure()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("SpawnLatencyProbe failed: %s", e)
            return None

    def _measure(self) -> Optional[float]:
        """One timed CreateProcessW of ``cmd /c exit``; ms, or None on failure."""
        import ctypes
        import ctypes.wintypes as wt

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("lpReserved", wt.LPWSTR),
                ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD),
                ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD),
                ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD),
                ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD),
                ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wt.HANDLE),
                ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wt.HANDLE),
                ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        comspec = os.environ.get("ComSpec") or os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"
        )
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        pi = PROCESS_INFORMATION()
        # lpCommandLine must be a WRITABLE buffer — CreateProcessW may modify
        # it in place; passing a Python str here is undefined behaviour.
        cmdline = ctypes.create_unicode_buffer("cmd /c exit")
        CREATE_NO_WINDOW = 0x08000000
        WAIT_OBJECT_0 = 0x00000000
        WAIT_TIMEOUT = 0x00000102

        start = time.perf_counter()
        ok = kernel32.CreateProcessW(
            comspec, cmdline, None, None, False, CREATE_NO_WINDOW,
            None, None, ctypes.byref(si), ctypes.byref(pi),
        )
        if not ok:
            logger.debug(
                "SpawnLatencyProbe: CreateProcessW failed (winerror %s)",
                ctypes.get_last_error(),
            )
            return None
        try:
            rc = kernel32.WaitForSingleObject(pi.hProcess, int(self.timeout_ms))
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if rc == WAIT_TIMEOUT:
                # The child outlived the deadline: kill it (strict bound on
                # the probe's own churn) and report elapsed as a FLOOR.
                kernel32.TerminateProcess(pi.hProcess, 1)
                return elapsed_ms
            if rc != WAIT_OBJECT_0:  # pragma: no cover - WAIT_FAILED
                return None
            return elapsed_ms
        finally:
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)


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
        spawn_probe: Optional[Callable[[], Optional[float]]] = None,
        spawn_latency_warn_ms: float = DEFAULT_SPAWN_LATENCY_WARN_MS,
        spawn_latency_disarm_ms: float = DEFAULT_SPAWN_LATENCY_DISARM_MS,
        spawn_sustained_samples: int = DEFAULT_SPAWN_SUSTAINED_SAMPLES,
        spawn_window_max_age_seconds: float = DEFAULT_SPAWN_WINDOW_MAX_AGE_SECONDS,
    ):
        self.bus = bus
        self._sampler = sampler or sample_resources
        self._clock = clock or time.monotonic
        # The probe rate-limits itself, so check() may call it every poll.
        # Injectable like the sampler; the default no-ops off Windows.
        self._spawn_probe = (
            spawn_probe if spawn_probe is not None
            else SpawnLatencyProbe(clock=self._clock)
        )
        self.spawn_latency_warn_ms = spawn_latency_warn_ms
        self.spawn_latency_disarm_ms = spawn_latency_disarm_ms
        self.spawn_sustained_samples = spawn_sustained_samples
        self.spawn_window_max_age_seconds = spawn_window_max_age_seconds
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
        # Rolling (monotonic_ts, latency_ms) window for the spawn axis. The
        # axis is judged on this window, never on a single reading: breach =
        # window full AND its MIN above the warn level; comfortable clear =
        # window full AND its MAX below the disarm (or the window empty —
        # a dead probe must not wedge the episode open forever).
        self._spawn_window: List[Tuple[float, float]] = []
        # Edge-trigger state: per-axis hysteresis latches (reason strings). An
        # axis latches when its trigger breaches and unlatches only once
        # comfortably clear (its disarm level); episode = any axis latched.
        self._latched: Set[str] = set()
        self._last_emit: Optional[float] = None
        # Band edges already ANNOUNCED this episode, keyed by AXIS so numerically
        # equal edges on different axes (and there will be more as bands are
        # tuned) can never alias each other. Each axis ratchets toward its own
        # worse direction; an edge re-arms only once its axis recovers
        # comfortably back past it, and the whole set clears when the episode
        # ends. Was disk-only until 2026-08-20.
        self._announced_band_edges: Set[Tuple[str, float]] = set()
        # ``reasons`` of the last EMITTED event, for reasons_change detection.
        self._last_reasons: Optional[Set[str]] = None

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
        # Overlay the spawn-latency probe, but only when the sampler did not
        # already provide a reading (an injected test sampler may). A probe
        # failure or rate-limit leaves the sentinel -1.0 = "not measured".
        if sample.spawn_latency_ms < 0 and self._spawn_probe is not None:
            try:
                latency_ms = self._spawn_probe()
            except Exception:
                logger.exception("ResourcePressureMonitor: spawn probe raised")
                latency_ms = None
            if latency_ms is not None:
                sample = replace(sample, spawn_latency_ms=float(latency_ms))
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

        # Spawn-latency window: age out stale readings, record a fresh one
        # (the -1.0 sentinel means "not measured" and records nothing), and
        # keep only the newest ``spawn_sustained_samples``. The axis judges
        # the WINDOW, never one reading: its floor (min) must breach for the
        # reason to fire, so "sustained" means every recent spawn was slow.
        self._spawn_window = [
            (ts, ms) for ts, ms in self._spawn_window
            if now - ts <= self.spawn_window_max_age_seconds
        ]
        if sample.spawn_latency_ms >= 0:
            self._spawn_window.append((now, sample.spawn_latency_ms))
            self._spawn_window = self._spawn_window[-self.spawn_sustained_samples:]
        spawn_readings = [ms for _, ms in self._spawn_window]
        spawn_window_full = len(spawn_readings) >= self.spawn_sustained_samples
        spawn_floor = min(spawn_readings) if spawn_window_full else None

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
        if spawn_floor is not None and spawn_floor > self.spawn_latency_warn_ms:
            reasons.append("spawn_latency")

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
        # Sustained in BOTH directions: clearing needs the whole window
        # comfortably below the disarm, so one lucky spawn mid-storm cannot
        # end the episode (the mirror of the min-based breach). An EMPTY
        # window — probe dead or gone stale — also clears: an axis that can
        # latch but not unlatch wedges the episode forever (the phys-axis
        # no-disarm bug), and this monitor already fails toward silence on
        # measurement loss (sample_resources -> None). A partial window
        # holds: too little data to certify either direction.
        if not spawn_readings or (
            spawn_window_full
            and max(spawn_readings) < self.spawn_latency_disarm_ms
        ):
            comfortably_clear.add("spawn_latency")

        was_latched = set(self._latched)
        self._latched = (self._latched - comfortably_clear) | set(reasons)

        if not reasons:
            # Nothing breaching right now. If every latched axis also cleared
            # comfortably, the episode is over and the next rising edge fires
            # immediately rather than waiting a cooldown.
            if not self._latched:
                # Episode over: every band re-arms, so the NEXT episode
                # announces its severity from scratch.
                self._announced_band_edges.clear()
            return None

        # Severity band for the disk axis. Gated on ``reasons`` rather than on
        # free space alone, so a lowered ``disk_free_gb_threshold`` cannot make
        # a band appear on an episode the disk axis is not part of.
        disk_axis = "disk_low" in reasons or "disk_critical" in reasons
        band, band_edge = (
            disk_band_for(sample.disk_free_bytes) if disk_axis else (None, None)
        )
        # Commit and phys band the same way (2026-08-20). Each is gated on
        # ``reasons`` exactly as disk is, so a lowered threshold cannot make a
        # band appear on an episode that axis is not part of.
        commit_band, commit_band_edge = (
            pct_band_for(sample.commit_pct, COMMIT_BANDS)
            if "commit_high" in reasons else (None, None)
        )
        phys_band, phys_band_edge = (
            pct_band_for(sample.phys_pct, PHYS_BANDS)
            if "phys_high" in reasons else (None, None)
        )
        # The spawn band reads the sustained FLOOR, not the instantaneous
        # sample, so its label certifies every recent spawn crossed the edge.
        # ``pct_band_for`` is direction-generic (value > ascending edges)
        # despite its name; ms fit it exactly as percent does.
        spawn_band, spawn_band_edge = (
            pct_band_for(spawn_floor, SPAWN_BANDS)
            if "spawn_latency" in reasons else (None, None)
        )

        # Re-arm first: an edge an axis has recovered comfortably back past may
        # announce again. Unconditional -- an episode kept alive by another axis
        # must still re-arm its edges. Each axis re-arms in its own direction:
        # disk multiplicatively upward (free space recovering), the percentage
        # axes by an absolute gap downward, spawn multiplicatively downward
        # judged on the window's WORST reading (one lucky spawn cannot re-arm).
        def _edge_still_hot(axis: str, edge: float) -> bool:
            if axis == "disk":
                return sample.disk_free_bytes <= edge * DISK_BAND_REARM_FACTOR * _GB
            if axis == "spawn":
                return (
                    not spawn_readings
                    or max(spawn_readings) * SPAWN_BAND_REARM_FACTOR >= edge
                )
            pct = sample.commit_pct if axis == "commit" else sample.phys_pct
            return pct > edge - BAND_REARM_GAP_PCT

        self._announced_band_edges = {
            (axis, edge) for axis, edge in self._announced_band_edges
            if _edge_still_hot(axis, edge)
        }

        # One ``change`` stamp covers the whole event, so ANY axis crossing a
        # fresh edge makes this a band_change -- that is what carries an
        # escalation into chat.
        crossed = [
            ("disk", band_edge, DISK_BANDS,
             lambda e: sample.disk_free_bytes < e * _GB),
            ("commit", commit_band_edge, COMMIT_BANDS,
             lambda e: sample.commit_pct > e),
            ("phys", phys_band_edge, PHYS_BANDS,
             lambda e: sample.phys_pct > e),
            ("spawn", spawn_band_edge, SPAWN_BANDS,
             lambda e: spawn_floor is not None and spawn_floor > e),
        ]
        band_changed = any(
            edge is not None and (axis, edge) not in self._announced_band_edges
            for axis, edge, _bands, _is_past in crossed
        )
        for axis, edge, axis_bands, is_past in crossed:
            if edge is None or (axis, edge) in self._announced_band_edges:
                continue
            # Mark EVERY crossed edge announced, not just the deepest, so a
            # single steep move is one message instead of one per edge along
            # the way.
            self._announced_band_edges.update(
                (axis, band_edge_gb) for band_edge_gb, _label in axis_bands
                if is_past(band_edge_gb)
            )
        reasons_changed = (
            self._last_reasons is not None and set(reasons) != self._last_reasons
        )

        # Pressure is active. Decide whether to emit: rising edge always; a
        # sustained episode re-pings only after the cooldown elapses.
        #
        # A rising edge is PER AXIS, not per episode. This used to be
        # ``not was_in_episode`` -- one global boolean -- which folded an axis
        # that breached later into the running episode and made it wait out the
        # cooldown. Harmless while every axis routed identically; a paging bug
        # once the disk axis split in two (2026-08-14). disk_critical (25 GB)
        # sits BELOW disk_low (45 GB), so on the exact failure mode this axis
        # exists for -- a disk filling monotonically until a human frees space
        # -- disk_low always latches first and the ACT/action_required page was
        # never prompt. A breach that recovered inside the cooldown was never
        # sent at all. Only a gateway restarting while already below 25 GB
        # could page on the edge.
        #
        # Note this cannot resurrect the 2026-06-11 hover storm: re-latching an
        # axis requires first going comfortably clear of its disarm level, and
        # an axis that merely hovers in its band never leaves ``_latched``, so
        # it is never NEWLY breached. Only genuinely new pressure escalates.
        newly_breached = set(reasons) - was_latched
        rising_edge = bool(newly_breached)
        cooldown_elapsed = (
            self._last_emit is None
            or (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        if not (rising_edge or band_changed or reasons_changed or cooldown_elapsed):
            return None

        # Why this emission exists. Subscribers deliver everything EXCEPT
        # ``sustained_repeat``; the bus keeps them all, so the 900s sampling
        # that makes an episode reconstructable after the fact is preserved.
        if rising_edge:
            change = "rising_edge"
        elif band_changed:
            change = "band_change"
        elif reasons_changed:
            change = "reasons_change"
        else:
            change = "sustained_repeat"

        self._last_emit = now
        self._last_reasons = set(reasons)
        return self._emit(
            sample, reasons, growth_bytes, band, band_edge, change,
            commit_band, commit_band_edge, phys_band, phys_band_edge,
            spawn_band, spawn_band_edge, spawn_floor,
        )

    def _emit(
        self, sample: ResourceSample, reasons: List[str], growth_bytes: int,
        band: Optional[str] = None, band_edge: Optional[float] = None,
        change: str = "rising_edge",
        commit_band: Optional[str] = None,
        commit_band_edge: Optional[float] = None,
        phys_band: Optional[str] = None,
        phys_band_edge: Optional[float] = None,
        spawn_band: Optional[str] = None,
        spawn_band_edge: Optional[float] = None,
        spawn_floor_ms: Optional[float] = None,
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
            "disk_band": band,
            "disk_band_edge_gb": band_edge,
            # Per-axis severity labels (2026-08-20). Kept as separate keys rather
            # than folded into ``disk_band`` so existing consumers, replayed
            # events and the 08-14 disk tests all keep their exact shape.
            "commit_band": commit_band,
            "commit_band_edge_pct": commit_band_edge,
            "phys_band": phys_band,
            "phys_band_edge_pct": phys_band_edge,
            # Spawn-latency axis (2026-08-27). The instantaneous reading may
            # be None ("not measured this sample"); the sustained floor is
            # what the axis and its band were judged on.
            "spawn_latency_ms": (
                round(sample.spawn_latency_ms, 1)
                if sample.spawn_latency_ms >= 0 else None
            ),
            "spawn_latency_sustained_ms": (
                round(spawn_floor_ms, 1) if spawn_floor_ms is not None else None
            ),
            "spawn_band": spawn_band,
            "spawn_band_edge_ms": spawn_band_edge,
            "change": change,
            "thresholds": {
                "commit_pct": self.commit_pct_threshold,
                "phys_pct": self.phys_pct_threshold,
                "disk_free_gb": self.disk_free_gb_threshold,
                "disk_free_gb_critical": self.disk_free_gb_critical,
                "pagefile_growth_gb": self.pagefile_growth_gb_threshold,
                "growth_window_min": round(self.growth_window_seconds / 60.0, 1),
                "spawn_latency_warn_ms": self.spawn_latency_warn_ms,
                "spawn_sustained_samples": self.spawn_sustained_samples,
            },
        }
        spawn_txt = (
            "%.0f ms" % sample.spawn_latency_ms
            if sample.spawn_latency_ms >= 0 else "n/a"
        )
        logger.warning(
            "Resource pressure: %s — commit %.1f%% (%.1f/%.1f GB) · "
            "phys %.1f%% (%.1f GB avail) · "
            "pagefile %.1f GB (+%.1f GB/%.0fm) · C: %.1f GB free · spawn %s",
            ",".join(reasons),
            payload["commit_pct"], payload["commit_used_gb"], payload["commit_limit_gb"],
            payload["phys_used_pct"], payload["phys_available_gb"],
            payload["pagefile_allocated_gb"], payload["pagefile_growth_gb_10min"],
            payload["thresholds"]["growth_window_min"], payload["disk_c_free_gb"],
            spawn_txt,
        )
        return self.bus.emit(
            event_type=EventType.RESOURCE_PRESSURE,
            source="system",
            payload=payload,
            tags=["resource", "pressure"] + reasons,
        )
