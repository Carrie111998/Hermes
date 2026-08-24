//! Update orchestration.
//!
//! Driven when the installer is launched as `Hermes-Setup.exe --update` (see
//! `AppMode` in lib.rs). The desktop app hands off to us — it exits, then we:
//!
//!   1. wait for the old Hermes desktop process to fully exit (so both the
//!      venv shim and packaged app.asar are free; otherwise `hermes update`
//!      or repair bootstrap can race locked files),
//!   2. run `hermes update --yes --gateway` (Python/repo update; this does NOT
//!      rebuild apps/desktop by design — see cmd_update in hermes_cli/main.py),
//!   3. run `hermes desktop --build-only` (the rebuild step update skips),
//!   4. launch the freshly-built desktop (reuses bootstrap::launch logic).
//!
//! We reuse the `BootstrapEvent` channel + the existing progress UI by
//! emitting a synthetic multi-stage manifest (handoff → update → rebuild, plus
//! an install stage on macOS). To the frontend an update looks like a short
//! bootstrap, broken into the real operations run_update performs so the user
//! sees discrete steps (with the live log underneath) instead of one bar.
//!
//! Cross-platform note: `hermes update` already handles macOS/Linux (git/pip).
//! The only OS-specific bits here are the venv shim path (resolve_hermes) and
//! the no-window creation flag — both already cfg-gated. Keep new logic
//! OS-agnostic so the mac/linux port stays "fill in the paths".

use std::env;
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use tauri::{AppHandle, Emitter};
use tokio::process::Command;

use crate::events::{BootstrapEvent, LogStream, StageInfo, StageState};
use crate::powershell::{pump_child, DRAIN_GRACE};

/// `hermes update` exit code meaning "another hermes process is holding the
/// venv shim open / dirty precondition" — see _cmd_update_impl in
/// hermes_cli/main.py (sys.exit(2)). We surface a targeted message for this.
const UPDATE_EXIT_CONCURRENT: i32 = 2;

/// Python exited only after an isolated rollout coordinator took the shared
/// marker over and acknowledged its exact PID. This is not a terminal update
/// result; the updater must await the fresh gateway outcome before rebuilding.
const UPDATE_EXIT_INDEPENDENT_HANDOFF: i32 = 75;
const INDEPENDENT_UPDATE_WAIT: Duration = Duration::from_secs(24 * 60 * 60);
const INDEPENDENT_UPDATE_POLL: Duration = Duration::from_millis(200);

/// How long to wait for the old desktop process to release files under the
/// install tree before giving up and letting `hermes update`'s own guard decide.
const DESKTOP_EXIT_WAIT: Duration = Duration::from_secs(20);
const DESKTOP_EXIT_POLL: Duration = Duration::from_millis(500);

/// Guards against concurrent update runs. The frontend kicks `startUpdate()`
/// from a mount effect, which can fire more than once (React strict-mode
/// double-invokes effects in dev; a window reload or stray re-init can do it
/// in prod). Two `run_update` tasks racing on `git stash` corrupt the working
/// tree — one stashes the changes the other then can't find. Exactly one task
/// may hold this flag at a time.
static UPDATE_RUNNING: AtomicBool = AtomicBool::new(false);

/// Frontend → Rust: kick off the update flow. Mirrors `start_bootstrap`'s
/// fire-and-forget shape; progress arrives on the `bootstrap` event channel.
#[tauri::command]
pub async fn start_update(app: AppHandle) -> Result<(), String> {
    // Re-entrancy guard (see UPDATE_RUNNING). compare_exchange lets exactly one
    // caller flip false→true; any concurrent caller no-ops instead of spawning
    // a second racing update.
    if UPDATE_RUNNING
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        // Already running: re-emit the manifest so a duplicate startUpdate()
        // call (which resets the frontend store) can recover its stage list.
        let target_app = if cfg!(target_os = "macos") {
            target_app_from_args(std::env::args().skip(1))
        } else {
            None
        };
        emit(
            &app,
            BootstrapEvent::Manifest {
                stages: update_stages(target_app.is_some()),
                protocol_version: None,
            },
        );
        return Ok(());
    }
    tokio::spawn(async move {
        if let Err(err) = run_update(app.clone()).await {
            // run_update already emits a Failed event on the paths that matter;
            // this catches anything that escaped. Emit defensively.
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: None,
                    error: format!("{err:#}"),
                },
            );
        }
        UPDATE_RUNNING.store(false, Ordering::SeqCst);
    });
    Ok(())
}

/// RAII guard that owns the "update in progress" marker (see
/// `paths::update_in_progress_marker`). Created at the top of `run_update`;
/// its `Drop` removes the marker on EVERY exit path — success, early
/// `return Err`, or a panic that unwinds through `run_update` — so a crashed
/// or aborted updater can never permanently strand the marker and block
/// future desktop launches. The marker payload is `{pid}\n{lease_at_unix}`;
/// dead owners self-heal, while a confirmed-live owner remains authoritative.
///
/// The marker is also the cross-process update lock: `hermes update` claims
/// the same file (see `hermes_cli/update_lock.py`) so a dashboard-spawned
/// update and this updater can't mutate one checkout at the same time.
/// Python and Rust mutation paths validate every existing path component with
/// no-follow/reparse-safe opens, then publish complete staged payloads through
/// atomic no-clobber claims or replacement. Electron is a read-only observer
/// after its initial no-clobber hard-link handoff and never cleans a marker;
/// `acquire` therefore REFUSES when a live foreign owner holds it rather than
/// overwriting — the pre-fix clobber is what let a dashboard `hermes update`
/// keep running while install-mode bootstrap rewrote the tree underneath it.
struct UpdateMarkerGuard {
    path: PathBuf,
    /// False when a live foreign updater already owns the marker: we hold no
    /// claim, so `Drop` must not delete their marker.
    owned: bool,
    heartbeat: Option<MarkerHeartbeat>,
}

#[derive(Debug)]
enum MarkerAcquireError {
    Owned(MarkerOwner),
    Unavailable(String),
}

/// Nominal lease age used by compatibility tests and diagnostics. Mirrors
/// UPDATE_MARKER_MAX_AGE_MS in apps/desktop/electron/update-marker.ts and
/// UPDATE_MARKER_MAX_AGE_SECONDS in hermes_cli/update_lock.py. A live PID still
/// wins after this age; heartbeat failures must never admit a second mutator.
#[cfg(test)]
const UPDATE_MARKER_MAX_AGE_SECS: u64 = 20 * 60;
const MAX_SAFE_MARKER_INTEGER: u64 = (1_u64 << 53) - 1;

/// Reject links and Windows reparse points anywhere in an existing marker
/// topology. Missing leaves/parents are allowed because the first claim may
/// create them, but every existing component is inspected with lstat semantics
/// before marker I/O. This mirrors Python's validate_no_reparse_topology.
fn validate_no_reparse_topology(path: &Path) -> Result<(), String> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|err| format!("cannot inspect marker path {path:?}: {err}"))?
            .join(path)
    };
    let mut existing = Vec::new();
    let mut current = absolute;
    loop {
        match std::fs::symlink_metadata(&current) {
            Ok(_) => existing.push(current.clone()),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => {
                return Err(format!("cannot inspect marker path {current:?}: {err}"));
            }
        }
        let parent = current.parent().unwrap_or(&current);
        if parent == current {
            break;
        }
        current = parent.to_path_buf();
    }

    for component in existing {
        let metadata = std::fs::symlink_metadata(&component).map_err(|err| {
            format!("cannot inspect marker path {component:?}: {err}")
        })?;
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
            if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(format!(
                    "marker path contains a link or reparse point: {component:?}"
                ));
            }
        }
        if metadata.file_type().is_symlink() {
            return Err(format!(
                "marker path contains a link or reparse point: {component:?}"
            ));
        }
    }
    Ok(())
}

/// Validate a marker and its mutex path before either can be opened.
fn validate_marker_io_topology(marker: &Path, lock_path: &Path) -> Result<(), String> {
    validate_no_reparse_topology(marker)?;
    validate_no_reparse_topology(lock_path)?;
    Ok(())
}

/// The pid + age of a confirmed-live update holding the marker.
#[derive(Debug)]
struct MarkerOwner {
    pid: u32,
    age_secs: u64,
}

/// Exact marker observation for mutation and handoff decisions. Liveness
/// probe uncertainty is represented as `Live` by `pid_is_alive`; only a
/// confirmed dead PID may be reclaimed. Unreadable state fails closed.
#[derive(Debug)]
enum StrictMarkerState {
    Absent,
    Live(MarkerOwner),
    Dead(u32),
    Malformed(String),
    Unavailable(String),
}

fn strict_marker_state(path: &Path) -> StrictMarkerState {
    if let Err(error) = validate_no_reparse_topology(path) {
        return StrictMarkerState::Unavailable(error);
    }
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return StrictMarkerState::Absent;
        }
        Err(err) if err.kind() == std::io::ErrorKind::InvalidData => {
            return StrictMarkerState::Malformed(format!(
                "update marker {path:?} is not UTF-8: {err}"
            ));
        }
        Err(err) => {
            return StrictMarkerState::Unavailable(format!(
                "could not read update marker {path:?}: {err}"
            ));
        }
    };
    let normalized = raw.replace("\r\n", "\n");
    let body = normalized.strip_suffix('\n').unwrap_or(&normalized);
    let fields: Vec<_> = body.split('\n').collect();
    if fields.len() != 2 {
        return StrictMarkerState::Malformed(format!(
            "update marker {path:?} must contain exactly two fields"
        ));
    }
    let pid_text = fields[0];
    let pid_is_decimal = pid_text
        .as_bytes()
        .first()
        .is_some_and(|first| matches!(first, b'1'..=b'9'))
        && pid_text.as_bytes().iter().all(u8::is_ascii_digit);
    let pid = match pid_is_decimal
        .then(|| pid_text.parse::<u32>().ok())
        .flatten()
    {
        Some(pid) => pid,
        None => {
            return StrictMarkerState::Malformed(format!(
                "update marker {path:?} has an invalid pid"
            ));
        }
    };
    let lease_text = fields[1];
    let lease_is_decimal = !lease_text.is_empty()
        && lease_text.as_bytes().iter().all(u8::is_ascii_digit);
    let lease_at = match lease_is_decimal
        .then(|| lease_text.parse::<u64>().ok())
        .flatten()
        .filter(|lease| *lease <= MAX_SAFE_MARKER_INTEGER)
    {
        Some(lease_at) => lease_at,
        None => {
            return StrictMarkerState::Malformed(format!(
                "update marker {path:?} has an invalid lease timestamp"
            ));
        }
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    if pid_is_alive(pid) {
        StrictMarkerState::Live(MarkerOwner {
            pid,
            age_secs: now.saturating_sub(lease_at),
        })
    } else {
        StrictMarkerState::Dead(pid)
    }
}

/// Compatibility observation returning a live owner, if any. Mutation paths
/// use `strict_marker_state` so unreadable/malformed state cannot look absent.
///
/// Self-PID is returned so `acquire` can adopt the desktop's pre-written claim
/// without refreshing its acquisition time (#74761). A foreign live pid (e.g.
/// a dashboard-spawned `hermes update`) still blocks.
fn live_marker_owner(path: &Path) -> Option<MarkerOwner> {
    // The owner refreshes this lease every 30s. A confirmed-live PID remains
    // authoritative after suspend, clock jumps, or heartbeat trouble: safety
    // beats the rare liveness cost of a recycled PID retaining the marker.
    match strict_marker_state(path) {
        StrictMarkerState::Live(owner) => Some(owner),
        _ => None,
    }
}

/// True when a process with `pid` currently exists.
#[cfg(windows)]
fn pid_is_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            // ERROR_INVALID_PARAMETER is the no-such-process result. Access
            // denied and unknown probe failures stay conservatively alive:
            // uncertainty must never prune another updater's claim.
            return GetLastError() != 87;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        CloseHandle(handle);
        ok == 0 || code == STILL_ACTIVE as u32
    }
}

#[cfg(not(windows))]
fn pid_is_alive(pid: u32) -> bool {
    // signal 0 delivers nothing; it only probes existence/permission.
    // Only ESRCH proves death. EPERM and unknown errors fail closed as alive.
    let Ok(native_pid) = libc::pid_t::try_from(pid) else {
        return false;
    };
    let rc = unsafe { libc::kill(native_pid, 0) };
    if rc == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
}

const UPDATE_MARKER_HEARTBEAT_SECS: u64 = 30;

fn marker_mutex_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or(".hermes-update-in-progress");
    path.with_file_name(format!("{name}.mutex"))
}

struct MarkerMutex {
    file: File,
}

impl MarkerMutex {
    fn acquire(marker: &Path) -> Result<Self, String> {
        let lock_path = marker_mutex_path(marker);
        validate_marker_io_topology(marker, &lock_path)?;
        if let Some(parent) = lock_path.parent() {
            std::fs::create_dir_all(parent).map_err(|err| {
                format!("could not create update-lock directory {parent:?}: {err}")
            })?;
        }
        // Recheck after creating missing parents and immediately before the
        // mutex open; a newly inserted junction must not redirect marker I/O.
        validate_marker_io_topology(marker, &lock_path)?;
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(&lock_path)
            .map_err(|err| format!("could not open update mutex {lock_path:?}: {err}"))?;
        if file
            .metadata()
            .map_err(|err| format!("could not stat update mutex {lock_path:?}: {err}"))?
            .len()
            == 0
        {
            file.write_all(&[0]).map_err(|err| {
                format!("could not initialize update mutex {lock_path:?}: {err}")
            })?;
            file.sync_data().map_err(|err| {
                format!("could not sync update mutex {lock_path:?}: {err}")
            })?;
        }

        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) };
            if rc != 0 {
                return Err(format!(
                    "could not lock update mutex {lock_path:?}: {}",
                    std::io::Error::last_os_error()
                ));
            }
        }

        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::{
                LockFileEx, LOCKFILE_EXCLUSIVE_LOCK,
            };
            use windows_sys::Win32::System::IO::OVERLAPPED;

            let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
            let ok = unsafe {
                LockFileEx(
                    file.as_raw_handle() as _,
                    LOCKFILE_EXCLUSIVE_LOCK,
                    0,
                    1,
                    0,
                    &mut overlapped,
                )
            };
            if ok == 0 {
                return Err(format!(
                    "could not lock update mutex {lock_path:?}: {}",
                    std::io::Error::last_os_error()
                ));
            }
        }

        Ok(Self { file })
    }
}

impl Drop for MarkerMutex {
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let _ = unsafe { libc::flock(self.file.as_raw_fd(), libc::LOCK_UN) };
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::UnlockFileEx;
            use windows_sys::Win32::System::IO::OVERLAPPED;

            let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
            let _ = unsafe {
                UnlockFileEx(
                    self.file.as_raw_handle() as _,
                    0,
                    1,
                    0,
                    &mut overlapped,
                )
            };
        }
    }
}

fn atomic_write_marker(path: &Path, pid: u32, lease_at: u64) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("update marker has no parent: {path:?}"))?;
    let temp = parent.join(format!(
        ".hermes-update-marker-{}.tmp",
        uuid::Uuid::new_v4()
    ));
    let result = (|| {
        validate_no_reparse_topology(path)?;
        validate_no_reparse_topology(&temp)?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .map_err(|err| format!("could not create marker temp {temp:?}: {err}"))?;
        let payload = format!("{pid}\n{lease_at}");
        file.write_all(payload.as_bytes())
            .map_err(|err| format!("could not write marker temp {temp:?}: {err}"))?;
        file.sync_all()
            .map_err(|err| format!("could not sync marker temp {temp:?}: {err}"))?;
        drop(file);
        validate_no_reparse_topology(path)?;
        validate_no_reparse_topology(&temp)?;

        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStrExt;
            use windows_sys::Win32::Storage::FileSystem::{
                MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
            };

            let from: Vec<u16> = temp.as_os_str().encode_wide().chain(Some(0)).collect();
            let to: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
            let ok = unsafe {
                MoveFileExW(
                    from.as_ptr(),
                    to.as_ptr(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                )
            };
            if ok == 0 {
                return Err(format!(
                    "could not replace update marker {path:?}: {}",
                    std::io::Error::last_os_error()
                ));
            }
        }

        #[cfg(not(windows))]
        {
            std::fs::rename(&temp, path)
                .map_err(|err| format!("could not replace update marker {path:?}: {err}"))?;
        }
        Ok(())
    })();
    if result.is_err() && validate_no_reparse_topology(&temp).is_ok() {
        let _ = std::fs::remove_file(&temp);
    }
    result
}

/// Publish an initial claim without exposing an empty/partial marker and
/// without replacing a claim another language won concurrently.
fn create_marker_no_clobber(path: &Path, pid: u32, lease_at: u64) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("update marker has no parent: {path:?}"))?;
    let temp = parent.join(format!(
        ".hermes-update-marker-{}.claim",
        uuid::Uuid::new_v4()
    ));
    let result = (|| {
        validate_no_reparse_topology(path)?;
        validate_no_reparse_topology(&temp)?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .map_err(|err| format!("could not create marker claim {temp:?}: {err}"))?;
        let payload = format!("{pid}\n{lease_at}\n");
        file.write_all(payload.as_bytes())
            .map_err(|err| format!("could not write marker claim {temp:?}: {err}"))?;
        file.sync_all()
            .map_err(|err| format!("could not sync marker claim {temp:?}: {err}"))?;
        drop(file);
        validate_no_reparse_topology(path)?;
        validate_no_reparse_topology(&temp)?;

        // A hard link publishes the already-complete inode atomically and
        // fails if Python/Electron/Rust created the destination first.
        std::fs::hard_link(&temp, path)
            .map_err(|err| format!("could not publish update marker {path:?}: {err}"))?;
        Ok(())
    })();
    if validate_no_reparse_topology(&temp).is_ok() {
        let _ = std::fs::remove_file(&temp);
    }
    result
}

fn refresh_marker_lease(path: &Path, expected_pid: u32) -> Result<bool, String> {
    let _mutex = MarkerMutex::acquire(path)?;
    match strict_marker_state(path) {
        StrictMarkerState::Live(owner) if owner.pid == expected_pid => {}
        StrictMarkerState::Live(_) => return Ok(false),
        StrictMarkerState::Dead(pid) => {
            return Err(format!("update marker changed to confirmed-dead PID {pid}"));
        }
        StrictMarkerState::Absent => return Err("update marker disappeared".to_string()),
        StrictMarkerState::Malformed(error) | StrictMarkerState::Unavailable(error) => {
            return Err(error);
        }
    }
    let lease_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    atomic_write_marker(path, expected_pid, lease_at)?;
    Ok(true)
}

struct MarkerHeartbeat {
    control: Arc<(Mutex<MarkerHeartbeatControl>, Condvar)>,
    handle: Option<JoinHandle<()>>,
}

#[derive(Default)]
struct MarkerHeartbeatControl {
    stopped: bool,
    failure_observed: bool,
}

impl MarkerHeartbeat {
    fn start(path: PathBuf, pid: u32) -> Result<Self, String> {
        Self::start_with_interval(
            path,
            pid,
            Duration::from_secs(UPDATE_MARKER_HEARTBEAT_SECS),
        )
    }

    fn start_with_interval(
        path: PathBuf,
        pid: u32,
        interval: Duration,
    ) -> Result<Self, String> {
        let control = Arc::new((
            Mutex::new(MarkerHeartbeatControl::default()),
            Condvar::new(),
        ));
        let worker_control = Arc::clone(&control);
        let handle = std::thread::Builder::new()
            .name("hermes-update-lease".to_string())
            .spawn(move || {
                loop {
                    let (mutex, wake) = &*worker_control;
                    let control = mutex
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    if control.stopped {
                        return;
                    }
                    let (control, _) = wake
                        .wait_timeout(control, interval)
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    if control.stopped {
                        return;
                    }
                    // Never hold the control mutex while performing marker
                    // I/O. The failure path below must reacquire this mutex to
                    // publish its state and wait for shutdown; retaining the
                    // guard here would self-deadlock on the first refresh
                    // error and make UpdateMarkerGuard::complete hang forever.
                    drop(control);
                    match refresh_marker_lease(&path, pid) {
                        Ok(true) => {}
                        Ok(false) => return,
                        Err(err) => {
                            tracing::error!(?path, %err, "update marker heartbeat failed; fencing install");
                            {
                                let (mutex, wake) = &*worker_control;
                                let mut control = mutex
                                    .lock()
                                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                                control.failure_observed = true;
                                wake.notify_all();
                            }
                            // If marker refresh becomes impossible, hold the
                            // shared mutation mutex until this guard stops.
                            // Python/Rust contenders then fail or wait even if
                            // the public marker becomes unreadable or missing.
                            // Completion wakes us before it reacquires.
                            loop {
                                match MarkerMutex::acquire(&path) {
                                    Ok(_fence) => {
                                        let (mutex, wake) = &*worker_control;
                                        let mut control = mutex
                                            .lock()
                                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                                        while !control.stopped {
                                            control = wake
                                                .wait(control)
                                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                                        }
                                        return;
                                    }
                                    Err(lock_err) => {
                                        tracing::error!(
                                            ?path,
                                            %lock_err,
                                            "could not establish heartbeat failure fence; retrying"
                                        );
                                        let (mutex, wake) = &*worker_control;
                                        let control = mutex
                                            .lock()
                                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                                        if control.stopped {
                                            return;
                                        }
                                        let (control, _) = wake
                                            .wait_timeout(control, Duration::from_secs(1))
                                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                                        if control.stopped {
                                            return;
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            })
            .map_err(|err| format!("could not start update marker heartbeat: {err}"))?;
        Ok(Self {
            control,
            handle: Some(handle),
        })
    }

    fn stop(&mut self) {
        let (mutex, wake) = &*self.control;
        let mut control = mutex
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        control.stopped = true;
        wake.notify_all();
        drop(control);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }

    #[cfg(test)]
    fn wait_for_failure(&self, timeout: Duration) -> bool {
        let (mutex, wake) = &*self.control;
        let control = mutex
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let (control, _) = wake
            .wait_timeout_while(control, timeout, |state| !state.failure_observed)
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        control.failure_observed
    }
}

impl UpdateMarkerGuard {
    /// Claim the marker, or report the live updater that already owns it.
    ///
    /// Marker/mutex creation is fail-closed: without a provable exclusive
    /// claim we must not mutate the shared checkout at all.
    fn acquire(path: PathBuf) -> Result<Self, MarkerAcquireError> {
        let pid = std::process::id();
        let _mutex =
            MarkerMutex::acquire(&path).map_err(MarkerAcquireError::Unavailable)?;
        match strict_marker_state(&path) {
            StrictMarkerState::Live(owner) if owner.pid == pid => {
                let heartbeat = MarkerHeartbeat::start(path.clone(), pid)
                    .map_err(MarkerAcquireError::Unavailable)?;
                return Ok(Self {
                    path,
                    owned: true,
                    heartbeat: Some(heartbeat),
                });
            }
            StrictMarkerState::Live(owner) => {
                return Err(MarkerAcquireError::Owned(owner));
            }
            StrictMarkerState::Dead(dead_pid) => {
                tracing::debug!(?path, dead_pid, "reclaiming dead update marker owner");
                validate_no_reparse_topology(&path).map_err(|err| {
                    MarkerAcquireError::Unavailable(err)
                })?;
                std::fs::remove_file(&path).map_err(|err| {
                    MarkerAcquireError::Unavailable(format!(
                        "could not remove dead update marker {path:?}: {err}"
                    ))
                })?;
            }
            StrictMarkerState::Malformed(reason) => {
                return Err(MarkerAcquireError::Unavailable(reason));
            }
            StrictMarkerState::Unavailable(err) => {
                return Err(MarkerAcquireError::Unavailable(err));
            }
            StrictMarkerState::Absent => {}
        }
        let lease_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|err| {
                MarkerAcquireError::Unavailable(format!(
                    "could not create update marker directory {parent:?}: {err}"
                ))
            })?;
        }
        if let Err(err) = create_marker_no_clobber(&path, pid, lease_at) {
            match strict_marker_state(&path) {
                StrictMarkerState::Live(owner) if owner.pid == pid => {
                    let heartbeat = MarkerHeartbeat::start(path.clone(), pid)
                        .map_err(MarkerAcquireError::Unavailable)?;
                    return Ok(Self {
                        path,
                        owned: true,
                        heartbeat: Some(heartbeat),
                    });
                }
                StrictMarkerState::Live(owner) => {
                    return Err(MarkerAcquireError::Owned(owner));
                }
                StrictMarkerState::Unavailable(state_err) => {
                    return Err(MarkerAcquireError::Unavailable(format!(
                        "{err}; marker state is unavailable: {state_err}"
                    )));
                }
                StrictMarkerState::Absent
                | StrictMarkerState::Dead(_)
                | StrictMarkerState::Malformed(_) => {}
            }
            return Err(MarkerAcquireError::Unavailable(err));
        }
        let heartbeat = match MarkerHeartbeat::start(path.clone(), pid) {
            Ok(heartbeat) => heartbeat,
            Err(err) => {
                let _ = std::fs::remove_file(&path);
                return Err(MarkerAcquireError::Unavailable(err));
            }
        };
        Ok(Self {
            path,
            owned: true,
            heartbeat: Some(heartbeat),
        })
    }

    /// A verified independent Python coordinator has atomically taken the
    /// marker over. Stop our parent heartbeat and disarm Drop without touching
    /// the child's claim; the caller may reacquire after that child releases.
    fn relinquish_after_verified_handoff(&mut self) {
        if let Some(mut heartbeat) = self.heartbeat.take() {
            heartbeat.stop();
        }
        self.owned = false;
    }

    /// Release the marker as soon as every mutating stage has completed.
    ///
    /// The updater still owns a Tauri/Cocoa event loop while it relaunches the
    /// desktop, and that loop can outlive `app.exit(0)`. Relying on `Drop`
    /// alone therefore leaves a *successful* update looking active — a live
    /// pid holding a fresh marker — which blocks desktop startup and every
    /// other updater until this live process finally exits. Idempotent: `Drop`
    /// still runs and tolerates an already-removed marker.
    fn complete(&mut self) {
        if let Some(mut heartbeat) = self.heartbeat.take() {
            heartbeat.stop();
        }
        if !self.owned {
            return;
        }
        let _mutex = match MarkerMutex::acquire(&self.path) {
            Ok(mutex) => mutex,
            Err(err) => {
                tracing::error!(path = ?self.path, %err, "could not lock completed update marker");
                return;
            }
        };
        match strict_marker_state(&self.path) {
            StrictMarkerState::Live(owner) if owner.pid == std::process::id() => {}
            StrictMarkerState::Absent
            | StrictMarkerState::Live(_)
            | StrictMarkerState::Dead(_)
            | StrictMarkerState::Malformed(_)
            | StrictMarkerState::Unavailable(_) => {
                // Ownership is absent, foreign, or no longer provable. Never
                // turn parse/read uncertainty into permission to unlink.
                self.owned = false;
                return;
            }
        }
        if let Err(err) = validate_no_reparse_topology(&self.path) {
            tracing::error!(path = ?self.path, %err, "could not validate completed update marker");
            self.owned = false;
            return;
        }
        if let Err(err) = std::fs::remove_file(&self.path) {
            if err.kind() != std::io::ErrorKind::NotFound {
                tracing::warn!(path = ?self.path, %err, "could not remove completed update marker");
                return;
            }
        }
        self.owned = false;
    }
}

impl Drop for UpdateMarkerGuard {
    fn drop(&mut self) {
        self.complete();
    }
}

async fn run_update(app: AppHandle) -> Result<()> {
    let hermes_home = crate::paths::hermes_home();
    let install_root = hermes_home.join("hermes-agent");

    // Mutual exclusion (#50238): publish an "update in progress" marker for the
    // entire duration of this update. A desktop instance the user relaunches
    // mid-update consults this before spawning its own local backend — without
    // it, that backend re-locks the venv shim, our `force_kill_other_hermes`
    // straggler-cleanup kills it, and the relaunch/kill cycle loops. The guard
    // removes the marker on every exit path (incl. early returns / panics).
    //
    // The same marker is the cross-process update lock (hermes_cli/
    // update_lock.py claims it too), so a live foreign owner means another
    // updater — most often a dashboard-spawned `hermes update` — is already
    // mutating this checkout. Refuse instead of running a second one over it.
    let mut _update_marker = match UpdateMarkerGuard::acquire(
        crate::paths::update_in_progress_marker(),
    ) {
        Ok(guard) => guard,
        Err(MarkerAcquireError::Owned(owner)) => {
            let mins = owner.age_secs / 60;
            let secs = owner.age_secs % 60;
            let elapsed = if mins > 0 {
                format!("{mins}m {secs}s")
            } else {
                format!("{secs}s")
            };
            let msg = format!(
                "Another Hermes update is already running (PID {}, last active {} ago). \
                 Wait for it to finish, or close the window or dashboard tab that \
                 started it, then try again.",
                owner.pid, elapsed
            );
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: None,
                    error: msg.clone(),
                },
            );
            return Err(anyhow!(msg));
        }
        Err(MarkerAcquireError::Unavailable(error)) => {
            let msg = format!(
                "Could not establish exclusive ownership of the Hermes install: {error}. \
                 No update files were changed. Check HERMES_HOME permissions, then retry."
            );
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: None,
                    error: msg.clone(),
                },
            );
            return Err(anyhow!(msg));
        }
    };

    let update_branch = update_branch_from_args(std::env::args().skip(1))
        .or_else(|| option_env_string("BUILD_PIN_BRANCH"))
        .unwrap_or_else(|| "main".to_string());
    let target_app = if cfg!(target_os = "macos") {
        target_app_from_args(std::env::args().skip(1))
    } else {
        None
    };

    let hermes = resolve_hermes(&install_root).ok_or_else(|| {
        let msg = format!(
            "Could not find the hermes CLI under {}. Is Hermes installed? \
             Re-run the installer to repair the install.",
            install_root.display()
        );
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: None,
                error: msg.clone(),
            },
        );
        anyhow!(msg)
    })?;

    // Synthetic manifest so the existing progress UI renders our stages.
    emit(
        &app,
        BootstrapEvent::Manifest {
            stages: update_stages(target_app.is_some()),
            protocol_version: None,
        },
    );

    // ---- stage 1: wait for the old desktop to die ------------------------
    // The desktop exec'd us then called app.exit(), but process teardown is
    // async on Windows. If it still holds the venv shim, `hermes update`
    // aborts with exit 2. If it still holds the packaged app.asar,
    // install.ps1's repair/re-clone path cannot move/remove the install tree.
    // Give both handles a bounded window to clear. Surfaced as its own stage
    // (rather than a silent pre-step) so a slow close / force-kill reads as
    // real progress instead of a frozen first bar.
    let started = Instant::now();
    emit_stage(&app, "handoff", StageState::Running, None, None);
    wait_for_install_locks_free(&install_root, &app, "handoff").await;
    emit_stage(
        &app,
        "handoff",
        StageState::Succeeded,
        Some(started.elapsed().as_millis() as u64),
        None,
    );

    // ---- stage 2: hermes update -----------------------------------------
    // Pass --branch so `hermes update` targets the branch this installer was
    // built/pinned against (BUILD_PIN_BRANCH), NOT its built-in default of
    // `main`. The install was a detached-HEAD checkout of a specific commit;
    // without --branch, `hermes update` switches the checkout to `main` (a
    // divergent branch that may not even have the desktop CLI command), then
    // reports "already up to date" against the wrong branch. The desktop
    // detected the update against this same branch, so we must update against
    // it too.
    emit_log(
        &app,
        Some("update"),
        LogStream::Stdout,
        &format!("[update] updating against branch {update_branch}"),
    );
    let update_correlation = uuid::Uuid::new_v4().to_string();
    let independent_status = hermes_home.join(format!(
        ".update_exit_code.{update_correlation}"
    ));
    let independent_ready = hermes_home.join(format!(
        ".update_coordinator_ready.{update_correlation}"
    ));
    let mut child_env = update_child_env(&install_root);
    child_env.push((
        "HERMES_UPDATE_CORRELATION_ID".to_string(),
        OsString::from(&update_correlation),
    ));
    child_env.push((
        "HERMES_UPDATE_TAURI_OUTCOME_PATH".to_string(),
        independent_status.as_os_str().to_os_string(),
    ));
    child_env.push((
        "HERMES_UPDATE_TAURI_READY_PATH".to_string(),
        independent_ready.as_os_str().to_os_string(),
    ));
    let mut update_args: Vec<String> =
        vec!["update".into(), "--yes".into(), "--gateway".into()];
    // --force skips `hermes update`'s Windows running-exe guard (which would
    // `sys.exit(2)` and dead-end the handoff). By contract the desktop has
    // already exited and waited for the install locks to clear before launching
    // us, and wait_for_install_locks_free below force-kills any straggler — so by the
    // time `hermes update` runs there is no legitimate hermes.exe to protect,
    // and the guard would only produce a false "Hermes is still running" stop.
    //
    // NOTE: --force does NOT bypass the venv-python holder guard (that needs
    // an explicit `--force-venv`, which we deliberately do not pass). Our lock
    // probe only checks the hermes.exe shim and app.asar, so an external venv
    // python holding a native .pyd (a user terminal, an unmanaged gateway)
    // could still be alive here — mutating the venv under it would strand the
    // install half-updated. If that guard fires, it exits 2 and the match arm
    // below surfaces the correct "close all Hermes windows" message.
    update_args.push("--force".into());
    update_args.push("--branch".into());
    update_args.push(update_branch);

    clear_independent_update_status(&independent_status)?;
    clear_independent_update_status(&independent_ready)?;
    emit_stage(&app, "update", StageState::Running, None, None);
    let started = Instant::now();
    let mut update = run_streamed(
        &app,
        &hermes,
        &update_args,
        &install_root,
        &child_env,
        Some("update"),
    )
    .await?;
    update = resolve_independent_update_attempt(
        update,
        &mut _update_marker,
        &crate::paths::update_in_progress_marker(),
        &independent_status,
        &independent_ready,
        &update_correlation,
    )
    .await?;

    // Retry-once for the update-boundary crash. `hermes update` lazily imports
    // the FRESHLY PULLED modules, but the dependency-install step still runs the
    // already-in-memory pre-pull code for one invocation. A release that changed
    // an updater-path contract across that boundary (e.g. #39780's `_UvResult`,
    // whose `__iter__` injected a bool into the argv and crashed Windows
    // `list2cmdline` with `TypeError: sequence item 1: expected str instance,
    // bool found`, fixed in #39820) therefore kills the FIRST update on the
    // parked population — even though the fix is already on disk by then. A
    // second `hermes update` runs clean because the now-current module is loaded
    // from the start. Rather than make the parked user click Update twice (and
    // stare at a scary crash first), retry once automatically. Skip the retry
    // for the concurrent-instance guard (exit 2) — that's a "close Hermes" state
    // a retry can't fix.
    if update_attempt_needs_retry(update.exit_code) {
        emit_log(
            &app,
            Some("update"),
            LogStream::Stdout,
            "[update] first update attempt failed; retrying once (the fix it just \
             pulled loads on the second run)…",
        );
        clear_independent_update_status(&independent_status)?;
        clear_independent_update_status(&independent_ready)?;
        update = run_streamed(
            &app,
            &hermes,
            &update_args,
            &install_root,
            &child_env,
            Some("update"),
        )
        .await?;
        update = resolve_independent_update_attempt(
            update,
            &mut _update_marker,
            &crate::paths::update_in_progress_marker(),
            &independent_status,
            &independent_ready,
            &update_correlation,
        )
        .await?;
    }
    let update_ms = started.elapsed().as_millis() as u64;

    match update.exit_code {
        Some(0) => {
            emit_stage(&app, "update", StageState::Succeeded, Some(update_ms), None);
        }
        Some(code) if code == UPDATE_EXIT_CONCURRENT => {
            let msg = "Hermes is still running. Close all Hermes windows and try \
                       the update again."
                .to_string();
            emit_stage(
                &app,
                "update",
                StageState::Failed,
                Some(update_ms),
                Some(msg.clone()),
            );
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: Some("update".into()),
                    error: msg.clone(),
                },
            );
            return Err(anyhow!(msg));
        }
        other => {
            let msg = format!(
                "hermes update failed (exit {:?}). See {} for details.",
                other,
                crate::paths::hermes_home()
                    .join("logs")
                    .join("update.log")
                    .display()
            );
            emit_stage(
                &app,
                "update",
                StageState::Failed,
                Some(update_ms),
                Some(msg.clone()),
            );
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: Some("update".into()),
                    error: msg.clone(),
                },
            );
            return Err(anyhow!(msg));
        }
    }

    // ---- stage 3: hermes desktop --build-only ----------------------------
    // `hermes update` deliberately does NOT build apps/desktop (it installs
    // repo-root deps with --workspaces=false). This is the rebuild it skips.
    emit_stage(&app, "rebuild", StageState::Running, None, None);
    let started = Instant::now();
    let rebuild_args: Vec<String> = vec!["desktop".into(), "--build-only".into()];
    let mut rebuild = run_streamed(
        &app,
        &hermes,
        &rebuild_args,
        &install_root,
        &child_env,
        Some("rebuild"),
    )
    .await?;

    // Retry-once: the first `--build-only` can return nonzero on a still-settling
    // post-update tree or a network-blocked Electron fetch that our self-heal
    // repaired mid-run. A second attempt then builds clean off the healed dist
    // (the content-hash stamp makes it a near-no-op when the first actually
    // succeeded). Without this the updater bails here and never reaches the
    // relaunch below — the app updates but doesn't restart. Matches the
    // retry-once `hermes update` already does above, and `hermes update`'s own
    // desktop rebuild in cmd_update.
    if rebuild_needs_retry(rebuild.exit_code) {
        emit_log(
            &app,
            Some("rebuild"),
            LogStream::Stdout,
            "[rebuild] first desktop rebuild failed; retrying once (a self-healed \
             Electron download builds clean on the second run)…",
        );
        rebuild = run_streamed(
            &app,
            &hermes,
            &rebuild_args,
            &install_root,
            &child_env,
            Some("rebuild"),
        )
        .await?;
    }
    let rebuild_ms = started.elapsed().as_millis() as u64;

    if rebuild.exit_code != Some(0) {
        let msg = format!(
            "Rebuilding the desktop app failed (exit {:?}). The update was \
             applied but the app could not be rebuilt; run `hermes desktop` \
             from a terminal to see the error.",
            rebuild.exit_code
        );
        emit_stage(
            &app,
            "rebuild",
            StageState::Failed,
            Some(rebuild_ms),
            Some(msg.clone()),
        );
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: Some("rebuild".into()),
                error: msg.clone(),
            },
        );
        return Err(anyhow!(msg));
    }
    emit_stage(&app, "rebuild", StageState::Succeeded, Some(rebuild_ms), None);

    let launch_target = if let Some(target_app) = target_app {
        let started = Instant::now();
        emit_stage(&app, "install", StageState::Running, None, None);
        match install_macos_app_update(&app, &install_root, &target_app).await {
            Ok(installed_app) => {
                emit_stage(
                    &app,
                    "install",
                    StageState::Succeeded,
                    Some(started.elapsed().as_millis() as u64),
                    None,
                );
                Some(installed_app)
            }
            Err(err) => {
                let msg = format!("{err:#}");
                emit_stage(
                    &app,
                    "install",
                    StageState::Failed,
                    Some(started.elapsed().as_millis() as u64),
                    Some(msg.clone()),
                );
                emit(
                    &app,
                    BootstrapEvent::Failed {
                        stage: Some("install".into()),
                        error: msg.clone(),
                    },
                );
                return Err(anyhow!(msg));
            }
        }
    } else {
        None
    };

    // ---- done: signal complete, then launch the fresh desktop ------------
    emit(
        &app,
        BootstrapEvent::Complete {
            install_root: install_root.to_string_lossy().into_owned(),
            marker: None,
        },
    );
    // Every install-tree mutation is finished. Release the lock BEFORE the
    // relaunch: this process can stay wedged in its native event loop even
    // after a successful app.exit(), and a live pid on a fresh marker would
    // make a completed update look active — blocking desktop startup and
    // every other updater while this still-live process remains.
    _update_marker.complete();

    if let Some(target_app) = launch_target {
        if let Err(err) = launch_macos_app_and_exit(&app, &target_app).await {
            emit_log(
                &app,
                None,
                LogStream::Stderr,
                &format!("[update] could not auto-launch desktop: {err}. Launch Hermes manually."),
            );
        }
    } else if let Err(err) =
        crate::bootstrap::launch_hermes_desktop(app.clone(), install_root.to_string_lossy().into_owned()).await
    {
        // Launch failed: don't hard-fail the update (it succeeded); surface a
        // log line so the success screen can still tell the user to launch
        // manually.
        emit_log(
            &app,
            None,
            LogStream::Stdout,
            &format!("[update] could not auto-launch desktop: {err}. Launch Hermes manually."),
        );
    }

    // The launch helpers normally request exit themselves, but their failure
    // paths must still close a successful updater. A native event loop can
    // ignore that graceful request, so arm a process-exit fallback now that
    // all update state and the marker have been settled.
    exit_after_success(&app);
    Ok(())
}

/// Ask the app to exit, with a hard `process::exit` fallback for a native
/// event loop that ignores the graceful request. Without it a finished updater
/// can linger as a live pid forever.
fn exit_after_success(app: &AppHandle) {
    std::thread::spawn(|| {
        std::thread::sleep(std::time::Duration::from_secs(3));
        tracing::warn!("graceful updater exit timed out; forcing process exit");
        std::process::exit(0);
    });
    app.exit(0);
}

/// Poll until the venv shim AND packaged desktop app bundle are no longer locked
/// (Windows) or a bounded timeout elapses. On non-Windows this is a short fixed
/// grace since file locking isn't the failure mode there.
pub(crate) async fn wait_for_install_locks_free(install_root: &Path, app: &AppHandle, stage: &str) {
    let lock_targets = install_lock_probe_paths(install_root);
    let deadline = Instant::now() + DESKTOP_EXIT_WAIT;

    emit_log(app, Some(stage), LogStream::Stdout, "[handoff] waiting for Hermes to exit…");

    loop {
        let locked = locked_paths(&lock_targets);
        if locked.is_empty() {
            return;
        }
        if Instant::now() >= deadline {
            // Last resort: a backend hermes.exe (or the desktop Hermes.exe
            // itself) is still holding one of the update-sensitive files. The
            // desktop should have reaped its tree before handing off, but
            // SIGTERM races / detached grandchildren / AV handles can leave a
            // straggler. Rather than "proceed anyway" straight into uv's
            // "Access is denied" or install.ps1's locked app.asar failure,
            // force-kill every Hermes.exe except ourselves, then give the OS a
            // beat to unload the image.
            emit_log(
                app,
                Some(stage),
                LogStream::Stdout,
                &format!(
                    "[handoff] Hermes still holding install files ({}); force-killing stragglers…",
                    format_locked_paths(&locked)
                ),
            );
            force_kill_other_hermes();
            tokio::time::sleep(Duration::from_millis(800)).await;
            let locked_after_kill = locked_paths(&lock_targets);
            if locked_after_kill.is_empty() {
                emit_log(
                    app,
                    Some(stage),
                    LogStream::Stdout,
                    "[handoff] install files freed after force-kill",
                );
            } else {
                emit_log(
                    app,
                    Some(stage),
                    LogStream::Stdout,
                    &format!(
                        "[handoff] install files still locked ({}); proceeding (--force + quarantine will handle it)",
                        format_locked_paths(&locked_after_kill)
                    ),
                );
            }
            return;
        }
        tokio::time::sleep(DESKTOP_EXIT_POLL).await;
    }
}

fn install_lock_probe_paths(install_root: &Path) -> Vec<PathBuf> {
    let mut paths = vec![venv_hermes(install_root)];
    paths.extend(desktop_app_payload_paths(install_root));
    paths
}

fn desktop_app_payload_paths(install_root: &Path) -> Vec<PathBuf> {
    let release = install_root.join("apps").join("desktop").join("release");
    if cfg!(target_os = "windows") {
        vec![
            release.join("win-unpacked").join("resources").join("app.asar"),
            release.join("win-arm64-unpacked").join("resources").join("app.asar"),
        ]
    } else if cfg!(target_os = "macos") {
        vec![
            release.join("mac").join("Hermes.app").join("Contents").join("Resources").join("app.asar"),
            release.join("mac-arm64").join("Hermes.app").join("Contents").join("Resources").join("app.asar"),
        ]
    } else {
        vec![release.join("linux-unpacked").join("resources").join("app.asar")]
    }
}

fn locked_paths(paths: &[PathBuf]) -> Vec<PathBuf> {
    paths.iter().filter(|p| is_locked(p)).cloned().collect()
}

fn format_locked_paths(paths: &[PathBuf]) -> String {
    paths.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(", ")
}

/// Force-kill any `hermes.exe` other than this process. Windows-only; a no-op
/// elsewhere (POSIX has no mandatory-lock contention). We can't selectively
/// target "the backend" by PID here — the desktop already exited and we never
/// knew its children — so we kill the whole `hermes.exe` image tree via
/// taskkill, excluding our own PID.
///
/// Safe w.r.t. our own update child: this runs inside the install-lock wait,
/// which completes BEFORE we spawn `venv\Scripts\hermes.exe update`. And a
/// desktop the user relaunches mid-update will NOT have spawned a backend —
/// `startHermes()` in the desktop gates local-backend startup on our
/// update-in-progress marker and parks until we finish (#50238). So the only
/// hermes.exe images here are stragglers from the old desktop — exactly what
/// we want gone. (`/FI PID ne <self>` also spares this Tauri process, though it
/// isn't named hermes.exe.)
fn force_kill_other_hermes() {
    if !cfg!(target_os = "windows") {
        return;
    }
    #[cfg(target_os = "windows")]
    {
        let my_pid = std::process::id();
        // /FI excludes our own PID; /T kills the tree; /F forces.
        let _ = std::process::Command::new("taskkill")
            .args([
                "/F",
                "/T",
                "/IM",
                "hermes.exe",
                "/FI",
                &format!("PID ne {my_pid}"),
            ])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
}

/// Best-effort lock probe: try to open the file for read+write. On Windows an
/// exclusively-held running .exe refuses the open with a sharing violation.
/// On Unix this almost always succeeds (no mandatory locking), which is fine —
/// the venv-shim contention is a Windows-only problem.
fn is_locked(path: &Path) -> bool {
    if !path.exists() {
        return false;
    }
    match std::fs::OpenOptions::new().read(true).write(true).open(path) {
        Ok(_) => false,
        Err(_) => true,
    }
}

/// Whether the `desktop --build-only` rebuild should be retried once. Any
/// non-success exit qualifies: the common cause is a transient first-attempt
/// failure (still-settling tree / self-healed Electron download) that a clean
/// second run resolves.
fn rebuild_needs_retry(exit_code: Option<i32>) -> bool {
    exit_code != Some(0)
}

fn update_attempt_needs_retry(exit_code: Option<i32>) -> bool {
    !matches!(
        exit_code,
        Some(0) | Some(UPDATE_EXIT_CONCURRENT) | Some(UPDATE_EXIT_INDEPENDENT_HANDOFF)
    )
}

fn clear_independent_update_status(path: &Path) -> Result<()> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(anyhow!(
            "cannot clear stale independent update status {}: {err}",
            path.display()
        )),
    }
}

#[derive(Debug, PartialEq, Eq)]
enum IndependentUpdatePoll {
    Pending,
    Complete(i32),
    Missing,
}

fn expected_independent_pid(ready_path: &Path, correlation_id: &str) -> Result<u32> {
    let raw = std::fs::read_to_string(ready_path).map_err(|err| {
        anyhow!(
            "cannot read coordinator readiness {}: {err}",
            ready_path.display()
        )
    })?;
    let payload: serde_json::Value = serde_json::from_str(&raw).map_err(|err| {
        anyhow!(
            "coordinator readiness {} is malformed: {err}",
            ready_path.display()
        )
    })?;
    if payload.get("correlation_id").and_then(|value| value.as_str())
        != Some(correlation_id)
    {
        return Err(anyhow!("coordinator readiness correlation does not match"));
    }
    let pid = payload
        .get("pid")
        .and_then(|value| value.as_u64())
        .and_then(|value| u32::try_from(value).ok())
        .filter(|pid| *pid > 0)
        .ok_or_else(|| anyhow!("coordinator readiness pid is invalid"))?;
    if pid == std::process::id() {
        return Err(anyhow!("coordinator readiness pid names the Rust parent"));
    }
    Ok(pid)
}

fn poll_independent_update(
    status_path: &Path,
    marker_path: &Path,
    expected_pid: u32,
) -> Result<IndependentUpdatePoll> {
    match strict_marker_state(marker_path) {
        StrictMarkerState::Live(owner) if owner.pid == expected_pid => {
            return Ok(IndependentUpdatePoll::Pending);
        }
        StrictMarkerState::Live(owner) => {
            return Err(anyhow!(
                "update marker moved from coordinator PID {expected_pid} to live PID {}",
                owner.pid
            ));
        }
        StrictMarkerState::Dead(pid) if pid != expected_pid => {
            return Err(anyhow!(
                "update marker unexpectedly names dead PID {pid}, not coordinator \
                 PID {expected_pid}"
            ));
        }
        StrictMarkerState::Malformed(error) | StrictMarkerState::Unavailable(error) => {
            return Err(anyhow!("{error}"));
        }
        StrictMarkerState::Absent | StrictMarkerState::Dead(_) => {}
    }

    match std::fs::read_to_string(status_path) {
        Ok(raw) => match raw.trim().parse::<i32>() {
            Ok(code) => Ok(IndependentUpdatePoll::Complete(code)),
            Err(_) => Err(anyhow!(
                "independent update wrote a malformed terminal status to {}",
                status_path.display()
            )),
        },
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            Ok(IndependentUpdatePoll::Missing)
        }
        Err(err) => Err(anyhow!(
            "cannot read independent update status {}: {err}",
            status_path.display()
        )),
    }
}

async fn wait_for_independent_update(
    status_path: &Path,
    marker_path: &Path,
    expected_pid: u32,
) -> Result<i32> {
    let deadline = Instant::now() + INDEPENDENT_UPDATE_WAIT;
    let mut missing_since: Option<Instant> = None;
    loop {
        match poll_independent_update(status_path, marker_path, expected_pid)? {
            IndependentUpdatePoll::Complete(code) => return Ok(code),
            IndependentUpdatePoll::Pending => missing_since = None,
            IndependentUpdatePoll::Missing => {
                let since = missing_since.get_or_insert_with(Instant::now);
                if since.elapsed() >= Duration::from_secs(5) {
                    return Err(anyhow!(
                        "independent update exited without publishing a terminal status"
                    ));
                }
            }
        }
        if Instant::now() >= deadline {
            return Err(anyhow!(
                "independent update did not finish within {} seconds",
                INDEPENDENT_UPDATE_WAIT.as_secs()
            ));
        }
        tokio::time::sleep(INDEPENDENT_UPDATE_POLL).await;
    }
}

async fn resolve_independent_update_attempt(
    result: CmdResult,
    marker_guard: &mut UpdateMarkerGuard,
    marker_path: &Path,
    status_path: &Path,
    ready_path: &Path,
    correlation_id: &str,
) -> Result<CmdResult> {
    if result.exit_code != Some(UPDATE_EXIT_INDEPENDENT_HANDOFF) {
        return Ok(result);
    }

    let expected_pid = expected_independent_pid(ready_path, correlation_id)?;

    // Exit 75 is emitted only after the copied Python child atomically owns
    // the marker. Stop the Rust heartbeat and disarm Drop without touching
    // that claim, then wait for the child's truthful command-boundary result.
    marker_guard.relinquish_after_verified_handoff();
    let exit_code =
        wait_for_independent_update(status_path, marker_path, expected_pid).await?;

    // The Python child releases its marker before we return. Reclaim the
    // install-wide lock before retrying or entering the desktop rebuild.
    let replacement = match UpdateMarkerGuard::acquire(marker_path.to_path_buf()) {
        Ok(guard) => guard,
        Err(MarkerAcquireError::Owned(owner)) => {
            return Err(anyhow!(
                "another updater (PID {}) claimed the install after the independent \
                 coordinator finished",
                owner.pid
            ));
        }
        Err(MarkerAcquireError::Unavailable(error)) => {
            return Err(anyhow!(
                "could not reclaim the update marker after the independent \
                 coordinator finished: {error}"
            ));
        }
    };
    *marker_guard = replacement;
    clear_independent_update_status(status_path)?;
    clear_independent_update_status(ready_path)?;
    Ok(CmdResult {
        exit_code: Some(exit_code),
    })
}

/// Spawn `hermes <args>` from `cwd`, stream stdout/stderr as Log events on the
/// bootstrap channel, and return the exit code. Mirrors powershell::run_script
/// but for an arbitrary command (no install.ps1 -File wrapping).
async fn run_streamed(
    app: &AppHandle,
    program: &Path,
    args: &[String],
    cwd: &Path,
    envs: &[(String, OsString)],
    stage: Option<&str>,
) -> Result<CmdResult> {
    let mut cmd = Command::new(program);
    cmd.args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (key, value) in envs {
        cmd.env(key, value);
    }

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW = 0x08000000 — no flashing console behind the GUI.
        cmd.creation_flags(0x0800_0000);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| anyhow!("spawning {} {:?}: {e}", program.display(), args))?;

    // Same non-UTF-8-safe decode path as powershell::run_script (#67193), and
    // the same rule about pipe EOF: `hermes update` is precisely the shape that
    // leaves resident descendants holding an inherited stdout handle, and every
    // stage this drives sits downstream of the read.
    let stage_owned = stage.map(|s| s.to_string());
    let outcome = pump_child(
        &mut child,
        |l| emit_log(app, stage_owned.as_deref(), LogStream::Stdout, l),
        |l| emit_log(app, stage_owned.as_deref(), LogStream::Stderr, l),
        &mut None,
        DRAIN_GRACE,
    )
    .await
    .map_err(|e| anyhow!("streaming {} {:?}: {e}", program.display(), args))?;

    if outcome.abandoned {
        let note = format!(
            "{} exited but a surviving descendant still holds its stdout/stderr; \
             gave up on the last {}s of output (#90455)",
            program.display(),
            DRAIN_GRACE.as_secs()
        );
        tracing::warn!("{note}");
        emit_log(app, stage_owned.as_deref(), LogStream::Stderr, &note);
    }

    Ok(CmdResult {
        exit_code: outcome.exit_code,
    })
}

struct CmdResult {
    exit_code: Option<i32>,
}

/// Path to the venv hermes shim under an install root, regardless of existence.
fn venv_hermes(install_root: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        install_root.join("venv").join("Scripts").join("hermes.exe")
    } else {
        install_root.join("venv").join("bin").join("hermes")
    }
}

/// Resolve the hermes CLI to drive. Prefer the venv shim in the install we
/// just updated; fall back to `hermes` on PATH.
fn resolve_hermes(install_root: &Path) -> Option<PathBuf> {
    let shim = venv_hermes(install_root);
    if shim.exists() {
        return Some(shim);
    }
    // PATH fallback. which-style probe via env, kept dependency-free.
    let exe = if cfg!(target_os = "windows") { "hermes.exe" } else { "hermes" };
    if let Ok(path) = std::env::var("PATH") {
        let sep = if cfg!(target_os = "windows") { ';' } else { ':' };
        for dir in path.split(sep) {
            let cand = Path::new(dir).join(exe);
            if cand.exists() {
                return Some(cand);
            }
        }
    }
    None
}

fn update_child_env(install_root: &Path) -> Vec<(String, OsString)> {
    let hermes_home = crate::paths::hermes_home();
    let mut envs = vec![(
        "HERMES_HOME".to_string(),
        hermes_home.as_os_str().to_os_string(),
    )];
    // `hermes update` is a Python CLI writing to a pipe here, so CPython
    // block-buffers its stdout: nothing reaches run_streamed (and the live
    // log UI) until 8 KB accumulate or the process exits. Long quiet steps —
    // the pre-update backup can zip multi-GB archives for minutes — render as
    // a frozen stage, and users cancel a healthy update. Force line-by-line
    // output instead.
    envs.push(("PYTHONUNBUFFERED".to_string(), OsString::from("1")));
    // We hold the update-in-progress marker for this whole run, and the
    // `hermes update` child claims that SAME lock (hermes_cli/update_lock.py).
    // Name our pid so the child recognizes the live holder as its own
    // orchestrator and runs under our claim — without this every GUI update
    // refuses its parent's marker with exit 2 ("Hermes is still running")
    // and no number of retries can ever succeed. Keep the variable name in
    // sync with HANDOFF_PID_ENV in hermes_cli/update_lock.py.
    envs.push((
        "HERMES_UPDATE_HANDOFF_PID".to_string(),
        OsString::from(std::process::id().to_string()),
    ));
    if let Some(path) = path_with_prepended_entries(&[
        hermes_home.join("node").join("bin"),
        venv_bin_dir(install_root),
    ]) {
        envs.push(("PATH".to_string(), path));
    }
    envs
}

fn venv_bin_dir(install_root: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        install_root.join("venv").join("Scripts")
    } else {
        install_root.join("venv").join("bin")
    }
}

fn path_with_prepended_entries(entries: &[PathBuf]) -> Option<OsString> {
    let mut parts: Vec<PathBuf> = entries.to_vec();
    if let Some(existing) = env::var_os("PATH") {
        parts.extend(env::split_paths(&existing));
    }
    env::join_paths(parts).ok()
}

fn update_branch_from_args<I, S>(args: I) -> Option<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    arg_value_from_args(args, "--branch")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn target_app_from_args<I, S>(args: I) -> Option<PathBuf>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    arg_value_from_args(args, "--target-app")
        .map(PathBuf::from)
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("app"))
}

fn arg_value_from_args<I, S>(args: I, name: &str) -> Option<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut iter = args.into_iter().map(|s| s.as_ref().to_string()).peekable();
    while let Some(arg) = iter.next() {
        if arg == name {
            return iter.next();
        }
        if let Some(value) = arg.strip_prefix(&format!("{name}=")) {
            return Some(value.to_string());
        }
    }
    None
}

#[cfg(target_os = "macos")]
async fn install_macos_app_update(
    app: &AppHandle,
    install_root: &Path,
    target_app: &Path,
) -> Result<PathBuf> {
    if target_app.extension().and_then(|e| e.to_str()) != Some("app") {
        return Err(anyhow!(
            "refusing to install update into non-app path: {}",
            target_app.display()
        ));
    }

    let rebuilt_app = crate::bootstrap::resolve_hermes_desktop_app(install_root).ok_or_else(|| {
        anyhow!(
            "desktop rebuild succeeded but no Hermes.app was found under {}",
            install_root.join("apps").join("desktop").join("release").display()
        )
    })?;

    let same = match (rebuilt_app.canonicalize(), target_app.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => rebuilt_app == target_app,
    };
    if same {
        emit_log(
            app,
            Some("install"),
            LogStream::Stdout,
            &format!(
                "[update] rebuilt app is already the launch target: {}",
                target_app.display()
            ),
        );
        return Ok(target_app.to_path_buf());
    }

    emit_log(
        app,
        Some("install"),
        LogStream::Stdout,
        &format!(
            "[update] installing rebuilt app {} -> {}",
            rebuilt_app.display(),
            target_app.display()
        ),
    );

    if let Some(parent) = target_app.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let tmp = PathBuf::from(format!("{}.hermes-update-new", target_app.display()));
    let old = PathBuf::from(format!("{}.hermes-update-old", target_app.display()));
    remove_dir_if_exists(&tmp).await;
    remove_dir_if_exists(&old).await;

    let ditto = Command::new("/usr/bin/ditto")
        .arg(&rebuilt_app)
        .arg(&tmp)
        .current_dir(crate::paths::hermes_home())
        .status()
        .await
        .map_err(|e| anyhow!("running ditto: {e}"))?;
    if !ditto.success() {
        return Err(anyhow!(
            "ditto failed while copying updated app into {}",
            tmp.display()
        ));
    }

    // Atomic-as-possible swap with rollback. Extracted so the invariant
    // (target is never left deleted-with-no-replacement) can be unit-tested
    // without ditto / a real .app bundle.
    swap_in_new_bundle(&tmp, target_app, &old).await?;

    let _ = Command::new("/usr/bin/xattr")
        .arg("-dr")
        .arg("com.apple.quarantine")
        .arg(target_app)
        .current_dir(crate::paths::hermes_home())
        .status()
        .await;

    Ok(target_app.to_path_buf())
}

/// Move a freshly-staged bundle (`tmp`) into place at `target`, parking any
/// existing bundle at `old` so the move can succeed (macOS `rename` won't
/// overwrite a non-empty directory).
///
/// Invariant: on ANY failure path, `target` is left pointing at a working
/// bundle — either the original (rolled back from `old`) or untouched — and we
/// never delete the running app with no replacement in place. The staged `tmp`
/// copy is cleaned up on failure.
async fn swap_in_new_bundle(tmp: &Path, target: &Path, old: &Path) -> Result<()> {
    let moved_old = if target.exists() {
        if let Err(err) = tokio::fs::rename(target, old).await {
            // Could not move the existing app aside. Leave it untouched and
            // bail — a failed update must not brick the install.
            remove_dir_if_exists(tmp).await;
            return Err(anyhow!(
                "could not move existing app aside at {} (leaving it in place): {err}",
                target.display()
            ));
        }
        true
    } else {
        false
    };
    if let Err(err) = tokio::fs::rename(tmp, target).await {
        // Restore the original app from the backup so the user keeps a working
        // install, and clean up the staged copy.
        if moved_old {
            let _ = tokio::fs::rename(old, target).await;
        }
        remove_dir_if_exists(tmp).await;
        return Err(anyhow!("installing updated app at {}: {err}", target.display()));
    }
    remove_dir_if_exists(old).await;
    Ok(())
}

#[cfg(not(target_os = "macos"))]
async fn install_macos_app_update(
    _app: &AppHandle,
    _install_root: &Path,
    target_app: &Path,
) -> Result<PathBuf> {
    Ok(target_app.to_path_buf())
}

async fn remove_dir_if_exists(path: &Path) {
    if path.exists() {
        let _ = tokio::fs::remove_dir_all(path).await;
    }
}

#[cfg(target_os = "macos")]
async fn launch_macos_app_and_exit(app: &AppHandle, target_app: &Path) -> Result<()> {
    crate::bootstrap::open_macos_app_detached(target_app)
        .map_err(|e| anyhow!("launching {}: {e}", target_app.display()))?;
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;
    app.exit(0);
    Ok(())
}

#[cfg(not(target_os = "macos"))]
async fn launch_macos_app_and_exit(_app: &AppHandle, _target_app: &Path) -> Result<()> {
    Ok(())
}

// ---------------------------------------------------------------------------
// Event helpers — keep emit shape identical to bootstrap.rs so the UI is reused
// ---------------------------------------------------------------------------

fn stage_info(name: &str, title: &str) -> StageInfo {
    StageInfo {
        name: name.to_string(),
        title: title.to_string(),
        category: "update".to_string(),
        needs_user_input: false,
    }
}

/// The synthetic update manifest. Mirrors the real operations `run_update`
/// performs so the progress UI shows them as discrete steps (with the live log
/// underneath) instead of one monolithic bar. `include_install` adds the macOS
/// app-swap stage. Both the happy path and the re-entrancy guard build the
/// manifest here so the two can never drift apart.
fn update_stages(include_install: bool) -> Vec<StageInfo> {
    let mut stages = vec![
        stage_info("handoff", "Preparing to update"),
        stage_info("update", "Downloading the latest version"),
        stage_info("rebuild", "Rebuilding the desktop app"),
    ];
    if include_install {
        stages.push(stage_info("install", "Installing the update"));
    }
    stages
}

// option_env! only accepts string literals, so the build-time pins are read
// by their literal names here. Mirrors bootstrap.rs's helper of the same name
// (kept local rather than shared because option_env! can't be parameterized).
fn option_env_string(key: &str) -> Option<String> {
    let val = match key {
        "BUILD_PIN_COMMIT" => option_env!("BUILD_PIN_COMMIT"),
        "BUILD_PIN_BRANCH" => option_env!("BUILD_PIN_BRANCH"),
        _ => None,
    };
    val.map(|s| s.to_string())
}

fn emit(app: &AppHandle, event: BootstrapEvent) {
    if let Err(e) = app.emit(BootstrapEvent::CHANNEL, &event) {
        tracing::warn!(?e, "failed to emit update event");
    }
}

fn emit_stage(
    app: &AppHandle,
    name: &str,
    state: StageState,
    duration_ms: Option<u64>,
    error: Option<String>,
) {
    tracing::info!(stage = %name, ?state, ?duration_ms, ?error, "update stage");
    emit(
        app,
        BootstrapEvent::Stage {
            name: name.to_string(),
            state,
            duration_ms,
            result: None,
            error,
        },
    );
}

fn emit_log(app: &AppHandle, stage: Option<&str>, stream: LogStream, line: &str) {
    match stage {
        Some(s) => tracing::info!(target: "bootstrap.log", stage = %s, "{line}"),
        None => tracing::info!(target: "bootstrap.log", "{line}"),
    }
    emit(
        app,
        BootstrapEvent::Log {
            stage: stage.map(|s| s.to_string()),
            line: line.to_string(),
            stream,
        },
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn venv_hermes_is_under_install_root() {
        let root = Path::new("/x/hermes-agent");
        let shim = venv_hermes(root);
        assert!(shim.starts_with(root));
        assert!(shim.to_string_lossy().contains("venv"));
    }

    #[test]
    fn missing_file_is_not_locked() {
        assert!(!is_locked(Path::new("/nonexistent/does/not/exist/xyz")));
    }

    #[test]
    fn update_child_env_forces_unbuffered_python() {
        let envs = update_child_env(Path::new("/x/hermes-agent"));
        assert!(
            envs.iter()
                .any(|(k, v)| k == "PYTHONUNBUFFERED" && v.to_str() == Some("1")),
            "update children must run unbuffered so long steps stream to the live log"
        );
    }

    #[test]
    fn update_child_env_names_our_pid_for_the_lock_handoff() {
        let envs = update_child_env(Path::new("/x/hermes-agent"));
        assert!(
            envs.iter().any(|(k, v)| k == "HERMES_UPDATE_HANDOFF_PID"
                && v.to_str() == Some(std::process::id().to_string().as_str())),
            "the hermes update child claims the same marker we hold; without our pid \
             it refuses its own parent's lock and every GUI update dead-ends on exit 2"
        );
    }

    #[test]
    fn independent_handoff_is_never_retried_as_a_terminal_failure() {
        assert!(!update_attempt_needs_retry(Some(
            UPDATE_EXIT_INDEPENDENT_HANDOFF
        )));
        assert!(update_attempt_needs_retry(Some(1)));
        assert!(!update_attempt_needs_retry(Some(UPDATE_EXIT_CONCURRENT)));
    }

    #[test]
    fn independent_outcome_waits_for_child_marker_release() {
        let dir = unique_tmp_dir("independent-update-outcome");
        std::fs::create_dir_all(&dir).unwrap();
        let status = dir.join(".update_exit_code");
        let marker = dir.join(".hermes-update-in-progress");
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        std::fs::write(&status, "0").unwrap();
        std::fs::write(&marker, format!("{}\n{now}\n", std::process::id())).unwrap();

        assert_eq!(
            poll_independent_update(&status, &marker, std::process::id()).unwrap(),
            IndependentUpdatePoll::Pending,
            "a terminal code is not consumable while the child still owns the lock"
        );
        std::fs::remove_file(&marker).unwrap();
        assert_eq!(
            poll_independent_update(&status, &marker, std::process::id()).unwrap(),
            IndependentUpdatePoll::Complete(0)
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn independent_outcome_rejects_a_new_live_marker_owner() {
        let dir = unique_tmp_dir("independent-update-owner-race");
        std::fs::create_dir_all(&dir).unwrap();
        let status = dir.join(".update_exit_code");
        let marker = dir.join(".hermes-update-in-progress");
        let ready = dir.join(".update_coordinator_ready");
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let expected_pid = std::process::id() + 1;
        std::fs::write(&status, "0").unwrap();
        std::fs::write(&marker, format!("{}\n{now}\n", std::process::id())).unwrap();
        std::fs::write(
            &ready,
            format!(
                r#"{{"correlation_id":"corr-1","pid":{expected_pid}}}"#
            ),
        )
        .unwrap();

        let error = poll_independent_update(&status, &marker, expected_pid)
            .expect_err("a replacement owner must fence the stale child outcome");
        assert!(error.to_string().contains("moved from coordinator PID"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn independent_outcome_rejects_malformed_marker_before_status() {
        let dir = unique_tmp_dir("independent-update-malformed-marker");
        std::fs::create_dir_all(&dir).unwrap();
        let status = dir.join(".update_exit_code");
        let marker = dir.join(".hermes-update-in-progress");
        std::fs::write(&status, "0").unwrap();
        std::fs::write(&marker, "not-a-pid\nnot-a-lease\n").unwrap();

        let error = poll_independent_update(&status, &marker, std::process::id())
            .expect_err("malformed marker state must fence a terminal outcome");
        assert!(error.to_string().contains("invalid pid"));
        assert_eq!(std::fs::read_to_string(&status).unwrap(), "0");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn independent_ready_file_is_bound_to_the_attempt_correlation() {
        let dir = unique_tmp_dir("independent-update-ready-correlation");
        std::fs::create_dir_all(&dir).unwrap();
        let ready = dir.join(".update_coordinator_ready");
        let child_pid = std::process::id().saturating_add(1);
        std::fs::write(
            &ready,
            format!(r#"{{"correlation_id":"old-attempt","pid":{child_pid}}}"#),
        )
        .unwrap();

        let error = expected_independent_pid(&ready, "new-attempt")
            .expect_err("a stale readiness file must not authorize handoff");
        assert!(error.to_string().contains("correlation does not match"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn each_update_attempt_clears_the_previous_terminal_status() {
        let dir = unique_tmp_dir("independent-update-status");
        std::fs::create_dir_all(&dir).unwrap();
        let status = dir.join(".update_exit_code");
        std::fs::write(&status, "1").unwrap();

        clear_independent_update_status(&status).unwrap();

        assert!(!status.exists());
        clear_independent_update_status(&status).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn lock_probe_paths_include_desktop_app_payload() {
        let root = Path::new("/x/hermes-agent");
        let probes = install_lock_probe_paths(root);

        assert!(
            probes.iter().any(|p| p == &venv_hermes(root)),
            "venv shim remains part of the update lock probe"
        );
        assert!(
            // Windows/Linux payloads live under `resources/`, the macOS bundle
            // under `Contents/Resources/` — Path::ends_with is case-sensitive.
            probes.iter().any(|p| {
                p.ends_with(Path::new("resources/app.asar"))
                    || p.ends_with(Path::new("Resources/app.asar"))
            }),
            "packaged app.asar must be probed so repair/re-clone waits for the old desktop to exit"
        );
    }

    #[test]
    fn locked_paths_ignores_missing_payloads() {
        let root = Path::new("/nonexistent/hermes-agent");
        let probes = install_lock_probe_paths(root);

        assert!(locked_paths(&probes).is_empty());
    }

    #[test]
    fn update_marker_guard_writes_then_removes_on_drop() {
        let dir = unique_tmp_dir("marker-guard");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        {
            let _g = UpdateMarkerGuard::acquire(marker.clone())
                .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
            assert!(marker.exists(), "marker must exist while the guard is held");
            let body = std::fs::read_to_string(&marker).unwrap();
            let pid_line = body.lines().next().unwrap();
            assert_eq!(
                pid_line.trim().parse::<u32>().unwrap(),
                std::process::id(),
                "marker records our pid so the desktop can probe liveness"
            );
            assert_eq!(body.lines().count(), 2, "marker is pid + started_at lines");
        }

        assert!(
            !marker.exists(),
            "Drop must remove the marker on every exit path (incl. early return / panic unwind)"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_marker_guard_drop_is_quiet_when_already_gone() {
        let dir = unique_tmp_dir("marker-guard-gone");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
        // Simulate an external cleanup before our guard drops — Drop must not
        // panic or manufacture a replacement marker.
        std::fs::remove_file(&marker).unwrap();
        drop(guard);

        assert!(!marker.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_marker_guard_never_deletes_malformed_self_pid_state() {
        let dir = unique_tmp_dir("marker-guard-malformed-self");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
        let malformed = format!("{}\nnot-a-lease\n", std::process::id());
        std::fs::write(&marker, &malformed).unwrap();
        drop(guard);

        assert_eq!(
            std::fs::read_to_string(&marker).unwrap(),
            malformed,
            "parse uncertainty must remain a fail-closed blocker on guard cleanup"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn initial_marker_publish_is_complete_and_never_clobbers_a_winner() {
        let dir = unique_tmp_dir("marker-no-clobber");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_secs())
            .unwrap_or(0);

        create_marker_no_clobber(&marker, std::process::id(), now)
            .expect("the staged claim should publish into an absent path");
        let payload = std::fs::read_to_string(&marker).unwrap();
        assert_eq!(payload, format!("{}\n{now}\n", std::process::id()));
        assert_eq!(
            payload.lines().count(),
            2,
            "the wire stays exactly two lines"
        );

        let winner = format!("{}\n{}\n", std::process::id().saturating_add(1), now);
        std::fs::write(&marker, &winner).unwrap();
        assert!(
            create_marker_no_clobber(&marker, std::process::id(), now + 1).is_err(),
            "an existing cross-language winner must make publication fail"
        );
        assert_eq!(std::fs::read_to_string(&marker).unwrap(), winner);
        assert!(
            std::fs::read_dir(&dir)
                .unwrap()
                .filter_map(Result::ok)
                .all(|entry| !entry.file_name().to_string_lossy().ends_with(".claim")),
            "the staged claim is cleaned after either outcome"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn marker_heartbeat_refreshes_exact_two_line_lease() {
        let dir = unique_tmp_dir("marker-heartbeat");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let old = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_secs())
            .unwrap_or(0)
            .saturating_sub(120);
        std::fs::write(&marker, format!("{}\n{old}", std::process::id())).unwrap();

        assert!(refresh_marker_lease(&marker, std::process::id()).unwrap());
        let raw = std::fs::read_to_string(&marker).unwrap();
        let lines: Vec<_> = raw.lines().collect();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].parse::<u32>().unwrap(), std::process::id());
        assert!(lines[1].parse::<u64>().unwrap() > old);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn marker_heartbeat_never_overwrites_a_changed_owner() {
        let dir = unique_tmp_dir("marker-heartbeat-owner");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let mut foreign = spawn_foreign_holder();
        let foreign_pid = foreign.id();
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_secs())
            .unwrap_or(0);
        let payload = format!("{foreign_pid}\n{now}");
        std::fs::write(&marker, &payload).unwrap();

        assert!(!refresh_marker_lease(&marker, std::process::id()).unwrap());
        assert_eq!(std::fs::read_to_string(&marker).unwrap(), payload);

        let _ = foreign.kill();
        let _ = foreign.wait();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn marker_heartbeat_fails_closed_on_missing_or_malformed_state() {
        let dir = unique_tmp_dir("marker-heartbeat-uncertain");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        assert!(refresh_marker_lease(&marker, std::process::id()).is_err());
        let malformed = format!("{}\nnot-a-lease\n", std::process::id());
        std::fs::write(&marker, &malformed).unwrap();
        assert!(refresh_marker_lease(&marker, std::process::id()).is_err());
        assert_eq!(std::fs::read_to_string(&marker).unwrap(), malformed);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn marker_heartbeat_failure_fence_stops_without_deadlock() {
        let dir = unique_tmp_dir("marker-heartbeat-stop-after-failure");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let mut heartbeat = MarkerHeartbeat::start_with_interval(
            marker,
            std::process::id(),
            Duration::from_millis(1),
        )
        .unwrap();

        assert!(
            heartbeat.wait_for_failure(Duration::from_secs(2)),
            "the missing marker must drive the heartbeat into its failure fence"
        );

        let (stopped_tx, stopped_rx) = std::sync::mpsc::channel();
        let stop_handle = std::thread::spawn(move || {
            heartbeat.stop();
            let _ = stopped_tx.send(());
        });
        assert!(
            stopped_rx.recv_timeout(Duration::from_secs(2)).is_ok(),
            "failure-fenced heartbeat shutdown must join instead of self-deadlocking"
        );
        stop_handle.join().unwrap();

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn marker_acquire_fails_closed_when_mutex_path_is_unwritable() {
        let dir = unique_tmp_dir("marker-unwritable");
        std::fs::create_dir_all(&dir).unwrap();
        let not_a_dir = dir.join("not-a-directory");
        std::fs::write(&not_a_dir, "file").unwrap();
        let marker = not_a_dir.join(".hermes-update-in-progress");

        let error = UpdateMarkerGuard::acquire(marker)
            .err()
            .expect("unprovable exclusivity must refuse the update");
        assert!(matches!(error, MarkerAcquireError::Unavailable(_)));

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn marker_acquire_fails_closed_when_existing_marker_is_unreadable() {
        let dir = unique_tmp_dir("marker-unreadable");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        std::fs::create_dir(&marker).unwrap();

        let error = UpdateMarkerGuard::acquire(marker.clone())
            .err()
            .expect("unreadable ownership must refuse the update");
        assert!(matches!(error, MarkerAcquireError::Unavailable(_)));
        assert!(marker.is_dir(), "an uncertain marker is never reclaimed");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn marker_guard_rejects_windows_junction_marker_and_parent_topology() {
        fn junction(link: &Path, target: &Path) {
            let output = std::process::Command::new("cmd.exe")
                .args(["/d", "/c", "mklink", "/J"])
                .arg(link)
                .arg(target)
                .output()
                .expect("spawn mklink");
            assert!(
                output.status.success(),
                "mklink failed: {} {}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
        }

        fn remove_junction(link: &Path) {
            let output = std::process::Command::new("cmd.exe")
                .args(["/d", "/c", "rmdir"])
                .arg(link)
                .output()
                .expect("spawn rmdir");
            assert!(output.status.success(), "rmdir junction failed");
        }

        let root = unique_tmp_dir("marker-junction");
        let target = root.join("marker-target");
        std::fs::create_dir_all(&target).unwrap();
        let marker_link = root.join(".hermes-update-in-progress");
        junction(&marker_link, &target);
        let marker_error = UpdateMarkerGuard::acquire(marker_link.clone())
            .err()
            .expect("a junction marker must be rejected before marker I/O");
        assert!(matches!(
            marker_error,
            MarkerAcquireError::Unavailable(reason) if reason.contains("reparse point")
        ));
        remove_junction(&marker_link);

        let parent_target = root.join("parent-target");
        std::fs::create_dir_all(&parent_target).unwrap();
        let parent_link = root.join("linked-parent");
        junction(&parent_link, &parent_target);
        let nested_marker = parent_link.join(".hermes-update-in-progress");
        let parent_error = UpdateMarkerGuard::acquire(nested_marker)
            .err()
            .expect("a junction marker parent must be rejected before marker I/O");
        assert!(matches!(
            parent_error,
            MarkerAcquireError::Unavailable(reason) if reason.contains("reparse point")
        ));
        remove_junction(&parent_link);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn marker_acquire_fails_closed_when_existing_marker_is_malformed() {
        let dir = unique_tmp_dir("marker-malformed");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let bodies = [
            format!("{}\nnot-a-lease\n", std::process::id()),
            "1e3\n123\n".to_string(),
            "+42\n123\n".to_string(),
            "0x10\n123\n".to_string(),
            "4294967296\n123\n".to_string(),
            "42\n1e3\n".to_string(),
            "42\n+123\n".to_string(),
            "42\n0x10\n".to_string(),
            "42\n9007199254740992\n".to_string(),
            "42\n123\nextra\n".to_string(),
            "42\r123\r".to_string(),
        ];
        for body in bodies {
            std::fs::write(&marker, &body).unwrap();
            let error = UpdateMarkerGuard::acquire(marker.clone())
                .err()
                .expect("malformed ownership must refuse the update");
            assert!(matches!(error, MarkerAcquireError::Unavailable(_)));
            assert_eq!(
                std::fs::read_to_string(&marker).unwrap(),
                body,
                "parse uncertainty is never reclaimed"
            );
        }

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        std::fs::write(
            &marker,
            format!("{}\r\n{now}\r\n", std::process::id()),
        )
        .unwrap();
        assert!(matches!(
            strict_marker_state(&marker),
            StrictMarkerState::Live(owner) if owner.pid == std::process::id()
        ));

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Spawn a short-lived sibling process whose pid stands in for a foreign
    /// updater. Same-process double-acquire no longer models contention: since
    /// #74761 `acquire` treats our own pid as adoptable (desktop pre-writes it),
    /// so a second acquire in *this* process would succeed.
    fn spawn_foreign_holder() -> std::process::Child {
        #[cfg(windows)]
        {
            // `timeout.exe` refuses redirected stdin on Windows and can exit
            // immediately in CI. PowerShell's Start-Sleep is independent of a
            // console, so this process remains live long enough to prove that
            // a foreign marker owner is never reclaimed.
            std::process::Command::new("powershell.exe")
                .args([
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Start-Sleep -Seconds 30",
                ])
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn()
                .expect("spawn foreign marker holder")
        }
        #[cfg(not(windows))]
        {
            std::process::Command::new("sleep")
                .arg("30")
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn()
                .expect("spawn foreign marker holder")
        }
    }

    #[test]
    fn acquire_refuses_while_a_live_updater_owns_the_marker() {
        let dir = unique_tmp_dir("marker-contended");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        // A live *foreign* updater holds it. We must NOT clobber the marker and
        // run concurrently over the same checkout — that race is what let a
        // dashboard `hermes update` and install-mode bootstrap mutate one tree
        // at once. Own-pid markers are adoptable (#74761), so the foreign pid
        // must be a real sibling process.
        let mut foreign = spawn_foreign_holder();
        let foreign_pid = foreign.id();
        let started_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        std::fs::write(&marker, format!("{foreign_pid}\n{started_at}")).unwrap();

        let owner = UpdateMarkerGuard::acquire(marker.clone())
            .err()
            .expect("acquire must be refused while a foreign updater is live");
        match owner {
            MarkerAcquireError::Owned(owner) => assert_eq!(owner.pid, foreign_pid),
            MarkerAcquireError::Unavailable(error) => {
                panic!("expected live-owner conflict, got lock error: {error}")
            }
        }

        // The refused guard must not delete the live owner's marker.
        assert!(marker.exists(), "refused acquire must leave the marker intact");
        let _ = foreign.kill();
        let _ = foreign.wait();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn acquire_adopts_a_marker_prewritten_with_our_own_pid() {
        // #74761: desktop writeUpdateMarker(hermesHome, child.pid) races ahead
        // of UpdateMarkerGuard::acquire. The marker names US; refusing it made
        // every in-app desktop update loop forever. Adopt it without resetting
        // the holder age; the heartbeat will refresh it once running.
        let dir = unique_tmp_dir("marker-own-pid");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        let started_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
            .saturating_sub(2);
        std::fs::write(&marker, format!("{}\n{started_at}", std::process::id())).unwrap();

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|error| panic!("own-pid pre-write must be adoptable: {error:?}"));
        assert!(marker.exists(), "adopted guard must own the marker");
        let body = std::fs::read_to_string(&marker).unwrap();
        assert_eq!(
            body.lines().next().unwrap().trim().parse::<u32>().unwrap(),
            std::process::id(),
            "acquire keeps the adopted marker owner"
        );
        assert_eq!(
            body.lines().nth(1).unwrap().trim().parse::<u64>().unwrap(),
            started_at,
            "adopting an own-pid marker must preserve its original holder age"
        );
        drop(guard);
        assert!(
            !marker.exists(),
            "Drop must still clear the marker we adopted"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn acquire_reclaims_a_marker_owned_by_a_dead_pid() {
        let dir = unique_tmp_dir("marker-dead-pid");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        // pid 1 exists everywhere, so fabricate a dead one: a very large pid
        // that no live process owns. A crashed updater must never wedge every
        // future update.
        let started_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        std::fs::write(&marker, format!("4294967294\n{started_at}")).unwrap();

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("a dead owner must not block acquisition"));
        let body = std::fs::read_to_string(&marker).unwrap();
        assert_eq!(
            body.lines().next().unwrap().trim().parse::<u32>().unwrap(),
            std::process::id(),
            "reclaiming rewrites the marker with our pid"
        );
        drop(guard);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn live_owner_past_nominal_age_is_kept_then_refreshed() {
        let dir = unique_tmp_dir("marker-stale-age");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        // Suspend/clock jumps can age the lease before the heartbeat wakes.
        // A confirmed-live owner remains authoritative in that gap.
        let long_ago = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
            .saturating_sub(UPDATE_MARKER_MAX_AGE_SECS + 60);
        std::fs::write(&marker, format!("{}\n{long_ago}", std::process::id())).unwrap();

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("a live owner must remain authoritative"));
        assert!(refresh_marker_lease(&marker, std::process::id()).unwrap());
        let refreshed_at = std::fs::read_to_string(&marker)
            .unwrap()
            .lines()
            .nth(1)
            .unwrap()
            .parse::<u64>()
            .unwrap();
        assert!(refreshed_at > long_ago);
        drop(guard);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn completed_update_releases_marker_before_guard_drop() {
        let dir = unique_tmp_dir("marker-complete");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        let mut guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
        guard.complete();

        assert!(
            !marker.exists(),
            "a successful update must unblock desktop startup before relaunch/exit"
        );
        drop(guard);
        assert!(!marker.exists(), "Drop stays idempotent after completion");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn parses_update_branch_from_space_or_equals_args() {
        assert_eq!(
            update_branch_from_args(["--update", "--branch", "bb/test"]),
            Some("bb/test".to_string())
        );
        assert_eq!(
            update_branch_from_args(["--update", "--branch=main"]),
            Some("main".to_string())
        );
        assert_eq!(update_branch_from_args(["--update"]), None);
    }

    #[test]
    fn update_manifest_leads_with_handoff_and_gates_install() {
        let base = update_stages(false);
        assert_eq!(
            base.first().map(|s| s.name.as_str()),
            Some("handoff"),
            "the lock-wait must surface as the first visible step"
        );
        assert!(
            base.iter().any(|s| s.name == "update") && base.iter().any(|s| s.name == "rebuild"),
            "update + rebuild remain distinct stages"
        );
        assert!(
            base.iter().all(|s| s.name != "install"),
            "no app-swap stage unless an install target was passed"
        );

        let with_install = update_stages(true);
        assert_eq!(
            with_install.last().map(|s| s.name.as_str()),
            Some("install"),
            "the macOS app-swap is the final stage when present"
        );
        assert_eq!(
            with_install.len(),
            base.len() + 1,
            "include_install adds exactly one stage"
        );
    }

    #[test]
    fn rebuild_retries_only_on_failure() {
        assert!(!rebuild_needs_retry(Some(0)), "a clean rebuild must not retry");
        assert!(rebuild_needs_retry(Some(1)), "a failed rebuild retries once");
        assert!(
            rebuild_needs_retry(None),
            "a killed/signalled rebuild (no exit code) retries once"
        );
    }

    #[test]
    fn parses_only_app_targets() {
        assert_eq!(
            target_app_from_args(["--update", "--target-app", "/Applications/Hermes.app"]),
            Some(PathBuf::from("/Applications/Hermes.app"))
        );
        assert_eq!(target_app_from_args(["--target-app", "/tmp/not-an-app"]), None);
    }

    // Helpers for the swap tests: make a throwaway dir tree we can rename.
    fn unique_tmp_dir(tag: &str) -> PathBuf {
        let base = std::env::temp_dir().join(format!(
            "hermes-swap-test-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        base
    }

    fn write_marker(dir: &Path, contents: &str) {
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(dir.join("marker.txt"), contents).unwrap();
    }

    #[tokio::test]
    async fn swap_installs_new_bundle_and_cleans_up() {
        let base = unique_tmp_dir("ok");
        let target = base.join("Hermes.app");
        let tmp = base.join("Hermes.app.hermes-update-new");
        let old = base.join("Hermes.app.hermes-update-old");
        write_marker(&target, "OLD");
        write_marker(&tmp, "NEW");

        swap_in_new_bundle(&tmp, &target, &old).await.unwrap();

        // New bundle is now at target; staging + backup dirs are gone.
        assert_eq!(
            std::fs::read_to_string(target.join("marker.txt")).unwrap(),
            "NEW"
        );
        assert!(!tmp.exists(), "staged copy should be cleaned up");
        assert!(!old.exists(), "backup should be cleaned up on success");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[tokio::test]
    async fn swap_failure_never_leaves_target_missing() {
        // Regression guard for the catastrophic path: the move-aside of the
        // existing app fails AND the staged bundle can't be installed. The
        // buggy version deleted `target` when move-aside failed and then
        // skipped rollback, bricking the install. The fixed version must leave
        // the original app intact on disk.
        //
        // Trigger both failures deterministically:
        //  - `old` is a NON-EMPTY dir  -> rename(target, old) fails
        //  - `tmp` does not exist       -> rename(tmp, target) fails
        let base = unique_tmp_dir("fail");
        let target = base.join("Hermes.app");
        let tmp = base.join("Hermes.app.hermes-update-new"); // intentionally absent
        let old = base.join("Hermes.app.hermes-update-old");
        write_marker(&target, "OLD");
        write_marker(&old, "OCCUPIED"); // non-empty => rename(target,old) fails

        let result = swap_in_new_bundle(&tmp, &target, &old).await;

        assert!(result.is_err(), "swap should fail when neither move can complete");
        assert!(target.exists(), "original app must NOT be deleted on failure");
        assert_eq!(
            std::fs::read_to_string(target.join("marker.txt")).unwrap(),
            "OLD",
            "original app contents must be intact after a failed swap"
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[tokio::test]
    async fn swap_rolls_back_when_install_step_fails() {
        // Move-aside succeeds but installing the staged bundle fails (tmp
        // absent). The original must be rolled back from `old` to `target`.
        let base = unique_tmp_dir("rollback");
        let target = base.join("Hermes.app");
        let tmp = base.join("Hermes.app.hermes-update-new"); // absent
        let old = base.join("Hermes.app.hermes-update-old");
        write_marker(&target, "OLD");

        let result = swap_in_new_bundle(&tmp, &target, &old).await;

        assert!(result.is_err());
        assert!(target.exists(), "original must be restored after failed install");
        assert_eq!(
            std::fs::read_to_string(target.join("marker.txt")).unwrap(),
            "OLD"
        );
        assert!(!old.exists(), "backup should be rolled back, not left behind");
        let _ = std::fs::remove_dir_all(&base);
    }
}
