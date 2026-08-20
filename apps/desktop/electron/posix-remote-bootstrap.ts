/**
 * posix-remote-bootstrap.ts
 *
 * ONE remote round-trip bootstrap of a POSIX Hermes backend over SSH.
 *
 * Why this exists
 * ---------------
 * remote-lifecycle.ts walks the remote lifecycle as ~15 separate `ssh.exec()`
 * calls plus a readiness poll that adds two more every 750ms. With
 * ControlMaster (Linux/macOS clients) those all ride one authenticated
 * connection, so the cost is a round-trip each. Windows OpenSSH has no
 * ControlMaster — mux sockets were never implemented on Win32 — so on a Windows
 * client every single one of those calls is a full TCP connect + key
 * authentication against the agent. With a hardware/vault-backed agent
 * (1Password, YubiKey) that is 1-3s and a user-visible approval each time; a
 * cold start measured 43 authentications and minutes of wall clock.
 *
 * The fix is not to make the calls faster, it is to stop making them. This
 * module ships the ENTIRE remote lifecycle as one Python program executed by a
 * single `ssh` invocation:
 *
 *   1. one ssh exec  -> platform, hermes location/version, HERMES_HOME, lock
 *                       read + ownership proof, reuse-or-spawn, local wait for
 *                       HERMES_BACKEND_READY, lockfile write. One JSON line back.
 *   2. one ssh -N -L -> the tunnel.
 *   3. HTTP + WebSocket checks run THROUGH that tunnel, with no further ssh.
 *
 * Cold start: 2 authentications. Warm restart: 2. A served token that drifts
 * from the spawn token costs one extra exec to re-stamp the lockfile.
 *
 * Compatibility
 * -------------
 * The lockfile schema, ownership proof, token-file handling and spawn argv are
 * byte-compatible with remote-lifecycle.ts, so a backend started by either path
 * is reusable and reapable by the other. Whatever this module cannot do (a
 * Windows remote, a host without python3) reports itself as unavailable and the
 * caller falls back to the legacy per-call path.
 *
 * Security
 * --------
 *   - The session token travels on stdin, never in argv, never in a log. Only
 *     its SHA256 fingerprint is passed as a parameter, exactly as the lockfile
 *     already stores it on the remote.
 *   - The remote script writes the token to a 0600 file in a 0700 directory it
 *     verifies it owns, with O_EXCL|O_NOFOLLOW.
 *   - A backend is only ever reused or terminated after its argv proves our
 *     ownership nonce + token path. A pid we cannot prove is ours is left
 *     alone.
 *   - The backend binds 127.0.0.1 on the remote and the tunnel binds 127.0.0.1
 *     locally.
 *
 * No `import 'electron'`: main.ts injects the SshConnection, the HTTP probes
 * and the token adoption.
 */

import crypto from 'node:crypto'

import { assertBootstrapNotSuperseded } from './ssh-connection'

const LOCKFILE_SCHEMA_VERSION = 2
const PROTOCOL_VERSION = 1
const DEFAULT_READY_TIMEOUT_MS = 45_000
// The remote script owns the readiness wait, so the ssh exec has to outlive it.
// This margin covers login shell + `hermes --version` + `serve --help` on a
// cold page cache.
const EXEC_MARGIN_MS = 60_000
const REMOTE_NOFILE_SOFT_LIMIT = 65_536
const RESPONSE_MARKER = 'HERMES_DESKTOP_BOOTSTRAP'

// Error kinds that mean "the transport is broken", not "the bootstrap failed".
// Falling back to the legacy path for these would just repeat the failure at
// 20x the authentication cost.
const TRANSPORT_KINDS = new Set(['auth-failed', 'host-key-changed', 'superseded', 'timeout', 'unreachable'])

// Kinds the remote script is allowed to report. Anything else is treated as a
// malformed response rather than silently trusted.
const REMOTE_ERROR_KINDS = new Set([
  'hermes-not-found',
  'ready-timeout',
  'spawn-failed',
  'transient-transport-error',
  'unknown',
  'unsafe-path',
  'unsupported-platform',
  'update-required'
])

// The remote program. Kept as a raw string so backslashes survive into Python.
const REMOTE_BOOTSTRAP_PY = String.raw`
import base64
import errno
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time

MARKER = "HERMES_DESKTOP_BOOTSTRAP"
READY_RE = re.compile(r"^HERMES_(?:BACKEND|DASHBOARD)_READY port=(\d+)", re.M)
SUPPORTED_OS = ("Darwin", "Linux")
LOCK_ROOT = "~/.hermes/desktop-ssh"
POLL_SECONDS = 0.2
LOG_TAIL_BYTES = 65536
HOME = os.path.expanduser("~")


class Failure(Exception):
    def __init__(self, kind, message):
        Exception.__init__(self, message)
        self.kind = kind


def emit(payload):
    sys.stdout.write(MARKER + " " + json.dumps(payload) + "\n")
    sys.stdout.flush()


def real(p):
    return os.path.expanduser(p)


def owner_dir(oid):
    return LOCK_ROOT + "/" + oid


def lock_path(oid):
    return owner_dir(oid) + "/backend.lock.json"


def token_path(oid, nonce):
    return owner_dir(oid) + "/" + nonce + ".token"


def log_path(oid, nonce):
    return owner_dir(oid) + "/" + nonce + ".log"


def capture(argv, timeout):
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        out = proc.communicate(timeout=timeout)[0]
    except Exception:
        try:
            proc.kill()
            proc.communicate()
        except Exception:
            pass
        raise
    return out.decode("utf-8", "replace")


def executable(candidate):
    try:
        target = real(candidate)
        return os.path.isfile(target) and os.access(target, os.X_OK)
    except OSError:
        return False


def locate_hermes(explicit):
    if explicit:
        if executable(explicit):
            return explicit
        raise Failure(
            "hermes-not-found",
            'The Hermes path you set is not an executable on the remote host: "%s". '
            "Check the path (it must be the full path to the hermes binary on the remote, e.g. "
            "~/hermes-agent/.venv/bin/hermes), or clear it to auto-detect." % explicit,
        )
    candidates = []
    try:
        found = capture(["bash", "-lc", "command -v hermes"], 30).strip().split("\n")
        if found and found[-1].strip():
            candidates.append(found[-1].strip())
    except Exception:
        pass
    candidates.append("~/.local/bin/hermes")
    candidates.append("/usr/local/bin/hermes")
    candidates.append("~/.hermes/hermes-agent/venv/bin/hermes")
    for candidate in candidates:
        if candidate and executable(candidate):
            return candidate
    raise Failure(
        "hermes-not-found",
        "Hermes is not installed on the remote host (could not find a hermes executable). "
        "Install it on the remote with:  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh  "
        "- or set the Hermes path explicitly in the SSH connection settings.",
    )


def hermes_version(hermes):
    try:
        return capture([real(hermes), "--version"], 120).strip().split("\n")[0].strip()
    except Exception:
        return ""


def supports_ssh_ownership(hermes):
    try:
        text = capture([real(hermes), "serve", "--help"], 120)
    except Exception:
        return False
    return "ssh-session-token-file" in text and "ssh-owner-nonce" in text


def read_lock(oid, protocol_version, schema_version):
    try:
        with open(real(lock_path(oid)), "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("schemaVersion") != schema_version:
        return None
    if parsed.get("protocolVersion") != protocol_version:
        return None
    pid = parsed.get("pid")
    port = parsed.get("port")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid > 4194304:
        return None
    if not isinstance(port, int) or isinstance(port, bool) or port < 0 or port > 65535:
        return None
    if parsed.get("ownershipId") != oid:
        return None
    if not re.match(r"^[0-9a-f]{16}$", str(parsed.get("spawnNonce") or "")):
        return None
    if not re.match(r"^[0-9a-f]{32}$", str(parsed.get("tokenFingerprint") or "")):
        return None
    if parsed.get("logPath") != log_path(oid, parsed["spawnNonce"]):
        return None
    for field in ("profile", "hermesPath", "hermesHome", "logPath", "startedAt"):
        value = parsed.get(field)
        if not isinstance(value, str) or len(value) > 1024:
            return None
    return parsed


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError as error:
        # EPERM: the pid exists but belongs to someone else. Report it alive so
        # the ownership proof (which will say FOREIGN) decides, never a kill.
        return error.errno == errno.EPERM


def process_args(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            raw = handle.read()
        return [x.decode("utf-8", "surrogateescape") for x in raw.split(b"\0") if x]
    except OSError:
        pass
    try:
        line = capture(["ps", "-ww", "-o", "command=", "-p", str(pid)], 30).strip()
    except Exception:
        return None
    if not line:
        return None
    try:
        return shlex.split(line)
    except ValueError:
        return None


def is_our_backend(pid, nonce, hermes_path, hermes_home, oid, profile):
    if not pid or not re.match(r"^[0-9a-f]{16}$", str(nonce or "")) or not hermes_path:
        return False
    args = process_args(pid)
    if not args:
        return False
    expected = real(hermes_path)
    entries = set([expected])
    if hermes_home:
        entries.add(os.path.join(real(hermes_home), "hermes-agent", "venv", "bin", "hermes"))
    expected_token = real(token_path(oid, nonce)) if oid else ""
    try:
        serve = args.index("serve")
        owner = args.index("--ssh-owner-nonce", serve + 1)
        token = args.index("--ssh-session-token-file", serve + 1) if expected_token else -1
        isolated = args.index("--isolated", serve + 1)
        profile_arg = args.index("--profile") if profile else -1
        serve_count = args.count("serve")
        owner_count = args.count("--ssh-owner-nonce")
        token_count = args.count("--ssh-session-token-file")
        isolated_count = args.count("--isolated")
        profile_count = args.count("--profile")
        direct = args[0] in entries
        python_entry = len(args) > 1 and args[1] in entries and os.path.basename(args[0]).startswith("python")
        token_ok = (not expected_token) or args[token + 1] == expected_token
        isolated_ok = isolated_count == 1 and isolated > serve
        if profile:
            profile_ok = profile_count == 1 and profile_arg < serve and args[profile_arg + 1] == profile
        else:
            profile_ok = profile_count == 0
        spawn_proof = bool(expected_token) and owner_count == 1 and token_count == 1 and token_ok and profile_ok
        return bool(
            (direct or python_entry or spawn_proof)
            and serve_count == 1
            and isolated_ok
            and owner_count == 1
            and args[owner + 1] == nonce
            and token_ok
            and profile_ok
        )
    except (IndexError, ValueError):
        return False


def unlink(path):
    try:
        os.unlink(real(path))
    except OSError:
        pass


def terminate(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(50):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    raise Failure("transient-transport-error", "Could not terminate the stale SSH backend.")


def cleanup_stale(oid, lock, alive, owned):
    if lock and alive and owned:
        terminate(lock["pid"])
    if lock and lock.get("logPath") == log_path(oid, lock.get("spawnNonce") or ""):
        unlink(lock["logPath"])
    if lock and lock.get("spawnNonce"):
        unlink(token_path(oid, lock["spawnNonce"]))
    unlink(lock_path(oid))


def write_lock(oid, lock):
    directory = real(owner_dir(oid))
    previous = os.umask(0o077)
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        temporary = os.path.join(directory, "." + binhex(os.urandom(8)) + ".lock.tmp")
        with open(temporary, "w") as handle:
            handle.write(json.dumps(lock))
        os.replace(temporary, real(lock_path(oid)))
    finally:
        os.umask(previous)


def binhex(raw):
    return "".join("%02x" % byte for byte in bytearray(raw))


def write_token(oid, nonce, data):
    path = real(token_path(oid, nonce))
    directory = os.path.dirname(path)
    name = os.path.basename(path)
    if not os.path.isdir(directory):
        os.makedirs(directory, 0o700)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(directory, flags)
    try:
        info = os.fstat(handle)
        if not stat.S_ISDIR(info.st_mode):
            raise Failure("unsafe-path", "unsafe token directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise Failure("unsafe-path", "token directory owner mismatch")
        if (info.st_mode & 0o777) != 0o700:
            os.fchmod(handle, 0o700)
        now = time.time()
        for stale in os.listdir(handle):
            if stale.endswith(".token") and len(stale) == 22:
                try:
                    info2 = os.stat(stale, dir_fd=handle, follow_symlinks=False)
                    if stat.S_ISREG(info2.st_mode) and now - info2.st_mtime > 3600:
                        os.unlink(stale, dir_fd=handle)
                except OSError:
                    pass
        create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, create, 0o600, dir_fd=handle)
        try:
            os.write(fd, data)
        except BaseException:
            try:
                os.unlink(name, dir_fd=handle)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
    finally:
        os.close(handle)


def raise_nofile(limit):
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass


def spawn_backend(hermes, profile, oid, nonce, nofile):
    argv = [real(hermes)]
    if profile:
        argv += ["--profile", profile]
    argv += [
        "serve",
        "--isolated",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--ssh-session-token-file",
        real(token_path(oid, nonce)),
        "--ssh-owner-nonce",
        nonce,
    ]
    log = real(log_path(oid, nonce))
    directory = os.path.dirname(log)
    if not os.path.isdir(directory):
        os.makedirs(directory, 0o700)
    env = dict(os.environ)
    env["HERMES_DESKTOP"] = "1"
    raise_nofile(nofile)
    sink = open(log, "ab", 0)
    devnull = open(os.devnull, "rb")
    try:
        return subprocess.Popen(
            argv,
            close_fds=True,
            cwd=HOME,
            env=env,
            start_new_session=True,
            stderr=subprocess.STDOUT,
            stdin=devnull,
            stdout=sink,
        )
    finally:
        sink.close()
        devnull.close()


def read_log_tail(path):
    try:
        with open(real(path), "rb") as handle:
            try:
                handle.seek(-LOG_TAIL_BYTES, os.SEEK_END)
            except OSError:
                handle.seek(0)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def wait_ready(proc, path, timeout_ms):
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        tail = read_log_tail(path)
        port = None
        for match in READY_RE.finditer(tail):
            port = int(match.group(1))
        if port:
            return port
        if proc.poll() is not None:
            raise Failure(
                "spawn-failed",
                "Remote dashboard process exited before announcing its port (exit=%s). %s"
                % (proc.returncode, tail[-2000:]),
            )
        time.sleep(POLL_SECONDS)
    raise Failure("ready-timeout", "Timed out waiting for the remote dashboard to announce its port (%dms)." % timeout_ms)


def reuse_blockers(lock, alive, owned, profile, hermes, hermes_home, reuse_fingerprint, params):
    """Why an existing lock could not be adopted, in stable machine-readable
    tokens. Never carries a secret: the token fingerprints are compared, not
    reported."""
    blockers = []
    if not params.get("allowReuse"):
        blockers.append("reuse-not-allowed")
    if not alive:
        blockers.append("pid-dead")
    elif not owned:
        blockers.append("pid-not-ours")
    if lock["port"] <= 0:
        blockers.append("spawn-in-progress")
    if lock["profile"] != profile:
        blockers.append("profile-changed")
    if not reuse_fingerprint:
        blockers.append("no-saved-token")
    elif lock["tokenFingerprint"] != reuse_fingerprint:
        blockers.append("saved-token-mismatch")
    if lock["hermesPath"] != hermes:
        blockers.append("hermes-path-changed")
    if lock["hermesHome"] != hermes_home:
        blockers.append("hermes-home-changed")
    return blockers or ["none"]


def bootstrap(params, token):
    started = time.time()
    oid = str(params.get("ownershipId") or "")
    if not re.match(r"^[0-9a-f]{32}$", oid):
        raise Failure("unknown", "SSH ownership ID is invalid.")
    nonce = str(params.get("spawnNonce") or "")
    if not re.match(r"^[0-9a-f]{16}$", nonce):
        raise Failure("unknown", "SSH spawn nonce is invalid.")
    fingerprint = str(params.get("tokenFingerprint") or "")
    if not re.match(r"^[0-9a-f]{32}$", fingerprint):
        raise Failure("unknown", "SSH token fingerprint is invalid.")
    profile = str(params.get("profile") or "")
    protocol_version = params.get("protocolVersion")
    schema_version = params.get("schemaVersion")
    uname = os.uname()
    if uname.sysname not in SUPPORTED_OS:
        raise Failure(
            "unsupported-platform",
            'Unsupported remote platform "%s". Hermes Desktop SSH mode supports Linux, macOS, '
            "and Windows remote hosts." % (uname.sysname or "unknown"),
        )
    hermes = locate_hermes(str(params.get("remoteHermesPath") or ""))
    hermes_home = os.environ.get("HERMES_HOME") or os.path.join(HOME, ".hermes")
    located = time.time()
    lock = read_lock(oid, protocol_version, schema_version)
    reuse_fingerprint = str(params.get("reuseFingerprint") or "")
    alive = False
    owned = False
    respawn_reason = "no-lock"
    if lock:
        alive = pid_alive(lock["pid"])
        owned = alive and is_our_backend(
            lock["pid"], lock["spawnNonce"], lock["hermesPath"], lock["hermesHome"], oid, lock["profile"]
        )
        respawn_reason = ",".join(
            reuse_blockers(lock, alive, owned, profile, hermes, hermes_home, reuse_fingerprint, params)
        )
        reusable = (
            bool(params.get("allowReuse"))
            and alive
            and owned
            and lock["port"] > 0
            and lock["profile"] == profile
            and bool(reuse_fingerprint)
            and lock["tokenFingerprint"] == reuse_fingerprint
            and lock["hermesPath"] == hermes
            and lock["hermesHome"] == hermes_home
        )
        if reusable:
            version = str(lock.get("hermesVersion") or "") or hermes_version(hermes)
            return {
                "arch": uname.machine,
                "hermesHome": hermes_home,
                "hermesPath": hermes,
                "hermesVersion": version,
                "logPath": lock["logPath"],
                "os": uname.sysname,
                "ownershipId": oid,
                "pid": lock["pid"],
                "port": lock["port"],
                "profile": profile,
                "protocolVersion": protocol_version,
                "respawnReason": "",
                "reused": True,
                "schemaVersion": schema_version,
                "spawnNonce": lock["spawnNonce"],
                "timings": {
                    "locateMs": int((located - started) * 1000),
                    "readyMs": 0,
                    "spawnMs": 0,
                    "totalMs": int((time.time() - started) * 1000),
                },
                "tokenFingerprint": lock["tokenFingerprint"],
            }
        cleanup_stale(oid, lock, alive, owned)
    if not supports_ssh_ownership(hermes):
        raise Failure(
            "update-required",
            "The remote Hermes install does not support --ssh-session-token-file and --ssh-owner-nonce. "
            "Update Hermes on the remote host to continue using Desktop SSH mode.",
        )
    version = hermes_version(hermes)
    probed = time.time()
    write_token(oid, nonce, token)
    record = {
        "hermesHome": hermes_home,
        "hermesPath": hermes,
        "hermesVersion": version,
        "logPath": log_path(oid, nonce),
        "ownershipId": oid,
        "pid": 0,
        "port": 0,
        "profile": profile,
        "protocolVersion": protocol_version,
        "schemaVersion": schema_version,
        "spawnNonce": nonce,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "tokenFingerprint": fingerprint,
    }
    proc = None
    try:
        proc = spawn_backend(hermes, profile, oid, nonce, params.get("nofileSoftLimit") or 65536)
        record["pid"] = proc.pid
        # Written with port=0 BEFORE readiness: if this attempt is superseded
        # and cleanup cannot reach the box, the next connect still finds the
        # record and reaps the process by exact ownership.
        write_lock(oid, record)
        spawned = time.time()
        port = wait_ready(proc, record["logPath"], params.get("readyTimeoutMs") or 45000)
    except BaseException:
        if proc is not None and proc.poll() is None:
            try:
                terminate(proc.pid)
            except Exception:
                pass
        unlink(record["logPath"])
        unlink(token_path(oid, nonce))
        unlink(lock_path(oid))
        raise
    record["port"] = port
    write_lock(oid, record)
    finished = time.time()
    return {
        "arch": uname.machine,
        "hermesHome": hermes_home,
        "hermesPath": hermes,
        "hermesVersion": version,
        "logPath": record["logPath"],
        "os": uname.sysname,
        "ownershipId": oid,
        "pid": record["pid"],
        "port": port,
        "profile": profile,
        "protocolVersion": protocol_version,
        "respawnReason": respawn_reason,
        "reused": False,
        "schemaVersion": schema_version,
        "spawnNonce": nonce,
        "timings": {
            "locateMs": int((located - started) * 1000),
            "probeMs": int((probed - located) * 1000),
            "readyMs": int((finished - spawned) * 1000),
            "spawnMs": int((spawned - probed) * 1000),
            "totalMs": int((finished - started) * 1000),
        },
        "tokenFingerprint": fingerprint,
    }


def main():
    try:
        # argv[1] is this program (the loader already exec'd it); argv[2] is
        # the parameter block. The session token is on stdin, never in argv.
        params = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
    except Exception as error:
        emit({"error": "invalid bootstrap parameters: %s" % error, "kind": "unknown", "ok": False})
        return
    try:
        token = sys.stdin.buffer.read()
    except Exception as error:
        emit({"error": "could not read the session token: %s" % error, "kind": "unknown", "ok": False})
        return
    try:
        result = bootstrap(params, token)
        result["ok"] = True
        emit(result)
    except Failure as error:
        emit({"error": str(error), "kind": error.kind, "ok": False})
    except Exception as error:
        emit({"error": "%s: %s" % (type(error).__name__, error), "kind": "unknown", "ok": False})


main()
`

// shell-single-quote a value for safe interpolation into a remote command.
function shq(value: string): string {
  return `'${String(value).replace(/'/g, `'\\''`)}'`
}

function fingerprintToken(token: string): string {
  return crypto
    .createHash('sha256')
    .update(String(token || ''))
    .digest('hex')
    .slice(0, 32)
}

export interface BootstrapParams {
  allowReuse: boolean
  nofileSoftLimit?: number
  ownershipId: string
  profile: string
  protocolVersion: number
  readyTimeoutMs: number
  remoteHermesPath: string
  reuseFingerprint: string
  schemaVersion: number
  spawnNonce: string
  tokenFingerprint: string
}

/**
 * The one remote command. The program and its parameters travel base64-encoded
 * in argv so no quoting rule of the remote login shell can corrupt them; the
 * SESSION TOKEN is deliberately absent — it goes on stdin.
 */
export function buildBootstrapCommand(params: BootstrapParams): string {
  const source = Buffer.from(REMOTE_BOOTSTRAP_PY, 'utf8').toString('base64')
  const encoded = Buffer.from(JSON.stringify(params), 'utf8').toString('base64')

  return (
    'python3 -c ' +
    shq('import base64,sys;exec(compile(base64.b64decode(sys.argv[1]),"<hermes-desktop-bootstrap>","exec"))') +
    ` ${shq(source)} ${shq(encoded)}`
  )
}

export interface BootstrapResponse {
  arch: string
  error?: string
  hermesHome: string
  hermesPath: string
  hermesVersion: string
  kind?: string
  logPath: string
  ok: boolean
  os: string
  ownershipId: string
  pid: number
  port: number
  profile: string
  respawnReason?: string
  reused: boolean
  spawnNonce: string
  timings?: Record<string, number>
  tokenFingerprint: string
}

function malformed(detail: string): Error {
  const error: any = new Error(`The remote bootstrap returned an unusable response (${detail}).`)
  error.kind = 'batched-unavailable'

  return error
}

/**
 * Strictly validate the single JSON line the remote program prints. Everything
 * here is remote-controlled, so nothing is trusted by shape alone: ids, paths,
 * ports and pids are re-checked against the same rules the lockfile reader
 * uses. A response that fails validation is reported as `batched-unavailable`
 * so the caller can fall back instead of connecting to something unverified.
 */
export function parseBootstrapResponse(stdout: string, expected: BootstrapParams): BootstrapResponse {
  const line = String(stdout || '')
    .split(/\r?\n/)
    .filter(text => text.startsWith(`${RESPONSE_MARKER} `))
    .pop()

  if (!line) {
    throw malformed('no response line')
  }

  let parsed: any

  try {
    parsed = JSON.parse(line.slice(RESPONSE_MARKER.length + 1))
  } catch {
    throw malformed('not JSON')
  }

  if (!parsed || typeof parsed !== 'object') {
    throw malformed('not an object')
  }

  if (parsed.ok !== true) {
    const kind = REMOTE_ERROR_KINDS.has(parsed.kind) ? parsed.kind : 'unknown'
    const detail = String(parsed.error || 'the remote bootstrap failed')
    // eslint-disable-next-line no-control-regex -- remote output is headed for the UI
    const error: any = new Error(detail.replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 2000))
    error.kind = kind

    throw error
  }

  const port = Number(parsed.port)
  const pid = Number(parsed.pid)

  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw malformed('invalid port')
  }

  if (!Number.isInteger(pid) || pid < 1 || pid > 4194304) {
    throw malformed('invalid pid')
  }

  if (parsed.ownershipId !== expected.ownershipId) {
    throw malformed('ownership id mismatch')
  }

  if (parsed.profile !== expected.profile) {
    throw malformed('profile mismatch')
  }

  if (!/^[0-9a-f]{16}$/.test(String(parsed.spawnNonce || ''))) {
    throw malformed('invalid spawn nonce')
  }

  if (!/^[0-9a-f]{32}$/.test(String(parsed.tokenFingerprint || ''))) {
    throw malformed('invalid token fingerprint')
  }

  if (typeof parsed.reused !== 'boolean') {
    throw malformed('missing reuse flag')
  }

  // A fresh spawn MUST report the nonce and token we just minted; anything else
  // means the response does not describe the process we own.
  if (!parsed.reused) {
    if (parsed.spawnNonce !== expected.spawnNonce || parsed.tokenFingerprint !== expected.tokenFingerprint) {
      throw malformed('spawn identity mismatch')
    }
  }

  if (parsed.reused && parsed.tokenFingerprint !== expected.reuseFingerprint) {
    throw malformed('reuse fingerprint mismatch')
  }

  for (const field of ['hermesPath', 'hermesHome', 'logPath', 'os', 'arch'] as const) {
    if (typeof parsed[field] !== 'string' || parsed[field].length > 1024) {
      throw malformed(`invalid ${field}`)
    }
  }

  if (parsed.logPath !== `~/.hermes/desktop-ssh/${expected.ownershipId}/${parsed.spawnNonce}.log`) {
    throw malformed('log path outside the ownership directory')
  }

  return {
    arch: parsed.arch,
    hermesHome: parsed.hermesHome,
    hermesPath: parsed.hermesPath,
    hermesVersion: typeof parsed.hermesVersion === 'string' ? parsed.hermesVersion.slice(0, 300) : '',
    logPath: parsed.logPath,
    ok: true,
    os: parsed.os,
    ownershipId: parsed.ownershipId,
    pid,
    port,
    profile: parsed.profile,
    respawnReason: typeof parsed.respawnReason === 'string' ? parsed.respawnReason.slice(0, 200) : '',
    reused: parsed.reused,
    spawnNonce: parsed.spawnNonce,
    timings: parsed.timings && typeof parsed.timings === 'object' ? parsed.timings : undefined,
    tokenFingerprint: parsed.tokenFingerprint
  }
}

export function isTransportError(error: any): boolean {
  return TRANSPORT_KINDS.has(error?.kind)
}

/** True when the caller should fall back to the legacy per-call lifecycle. */
export function shouldFallBackToLegacy(error: any): boolean {
  if (isTransportError(error)) {
    return false
  }

  // A kind the remote program itself reported is a real verdict about the
  // remote, not a limitation of this transport — do not repeat it 20x slower.
  if (REMOTE_ERROR_KINDS.has(error?.kind)) {
    return false
  }

  return true
}

export interface BatchedConnectDeps {
  adoptServedToken: (baseUrl: string, token: string, options: any) => Promise<string>
  cancelForward: (localPort: number, remotePort: number) => Promise<void>
  forward: (localPort: number, remotePort: number, options?: any) => Promise<void>
  onPhase?: (phase: string, ms: number) => void
  ownershipId: string
  pickLocalPort: () => Promise<number>
  probeReuseProof: (baseUrl: string, token: string, spawnNonce: string) => Promise<string>
  probeWebSocket?: (wsUrl: string) => Promise<{ ok: boolean; reason?: string }>
  profile?: string
  readyTimeoutMs?: number
  rememberLog?: (message: string) => void
  remoteHermesPath?: string
  reuseToken?: string
  signal?: AbortSignal
  ssh: any
  waitForHermes?: (baseUrl: string, token: string) => Promise<unknown>
}

/**
 * Mint the spawn credential and the parameter block. The token stays OUT of
 * the block on purpose: the block is base64'd into argv (visible in the remote
 * process list), the token is written to stdin.
 */
export function bootstrapParams(
  deps: Pick<BatchedConnectDeps, 'ownershipId' | 'profile' | 'readyTimeoutMs' | 'remoteHermesPath' | 'reuseToken'>,
  allowReuse: boolean,
  mint: () => string = () => crypto.randomBytes(32).toString('hex'),
  nonce: () => string = () => crypto.randomBytes(8).toString('hex')
): { params: BootstrapParams; token: string } {
  const token = mint()

  return {
    params: {
      allowReuse,
      nofileSoftLimit: REMOTE_NOFILE_SOFT_LIMIT,
      ownershipId: deps.ownershipId,
      profile: String(deps.profile || ''),
      protocolVersion: PROTOCOL_VERSION,
      readyTimeoutMs: deps.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
      remoteHermesPath: String(deps.remoteHermesPath || ''),
      reuseFingerprint: allowReuse && deps.reuseToken ? fingerprintToken(deps.reuseToken) : '',
      schemaVersion: LOCKFILE_SCHEMA_VERSION,
      spawnNonce: nonce(),
      tokenFingerprint: fingerprintToken(token)
    },
    token
  }
}

async function runRemoteBootstrap(deps: BatchedConnectDeps, allowReuse: boolean) {
  const { params, token } = bootstrapParams(deps, allowReuse)

  assertBootstrapNotSuperseded(deps.signal)

  const stdout = await deps.ssh.exec(buildBootstrapCommand(params), {
    signal: deps.signal,
    stdinData: token,
    timeoutMs: params.readyTimeoutMs + EXEC_MARGIN_MS
  })

  return { response: parseBootstrapResponse(stdout, params), token }
}

/**
 * Bring up (or adopt) the remote backend and its tunnel with a bounded number
 * of ssh invocations. Returns the same descriptor shape as
 * remote-lifecycle.connect so main.ts does not care which path ran.
 */
export async function connectBatched(deps: BatchedConnectDeps) {
  const log = (message: string) => deps.rememberLog?.(`[ssh-lifecycle] ${message}`)
  const phase = (name: string, startedAt: number) => deps.onPhase?.(name, Date.now() - startedAt)

  let allowReuse = Boolean(deps.reuseToken)
  let attempt = 0

  for (;;) {
    attempt += 1
    const bootstrapStartedAt = Date.now()
    const { response, token: spawnToken } = await runRemoteBootstrap(deps, allowReuse)
    phase('bootstrap', bootstrapStartedAt)
    log(
      `remote platform ${response.os}/${response.arch}; hermes ${response.hermesPath}` +
        `${response.hermesVersion ? ` (${response.hermesVersion})` : ''}`
    )
    log(
      `${response.reused ? 'backend reused' : 'backend spawned'} pid=${response.pid} port=${response.port} ` +
        `remoteMs=${response.timings?.totalMs ?? '?'}` +
        `${response.reused ? '' : ` respawnReason=${response.respawnReason || 'unknown'}`}`
    )

    const tunnelStartedAt = Date.now()
    const localPort = await deps.pickLocalPort()
    await deps.forward(localPort, response.port, { signal: deps.signal })
    phase('tunnel', tunnelStartedAt)
    const baseUrl = `http://127.0.0.1:${localPort}`

    try {
      assertBootstrapNotSuperseded(deps.signal)
      const httpStartedAt = Date.now()
      const reuseToken = String(deps.reuseToken || '')
      let token = response.reused ? reuseToken : spawnToken

      if (!response.reused && deps.waitForHermes) {
        await deps.waitForHermes(baseUrl, token)
        assertBootstrapNotSuperseded(deps.signal)
      }

      if (response.reused) {
        const classification = await deps.probeReuseProof(baseUrl, reuseToken, response.spawnNonce)

        if (classification === 'authenticated-stale') {
          // The lock says ours, the backend says otherwise. Drop the tunnel and
          // make ONE more attempt that is forbidden from reusing anything.
          log('reuse proof stale; respawning the remote backend')
          await deps.cancelForward(localPort, response.port)

          if (attempt >= 2) {
            const error: any = new Error('The remote SSH backend could not be verified after a respawn.')
            error.kind = 'transient-transport-error'
            throw error
          }

          allowReuse = false

          continue
        }

        if (classification !== 'authenticated-ok') {
          const error: any = new Error('SSH reuse proof returned an invalid classification.')
          error.kind = 'transient-transport-error'
          throw error
        }
      }

      token = await deps.adoptServedToken(baseUrl, token, {
        childAlive: () => true,
        label: response.reused ? 'reused remote dashboard' : 'remote dashboard'
      })

      // Ownership proof over the ADOPTED token: it authenticates AND returns
      // the owner nonce, so a port squatter or a foreign backend fails here
      // without costing another ssh round-trip.
      assertBootstrapNotSuperseded(deps.signal)
      const proof = await deps.probeReuseProof(baseUrl, token, response.spawnNonce)

      if (proof !== 'authenticated-ok') {
        const error: any = new Error(
          `${baseUrl} is served by a process that does not carry our SSH ownership nonce; refusing it.`
        )

        error.kind = 'foreign-backend'
        throw error
      }

      phase('http', httpStartedAt)

      if (deps.probeWebSocket) {
        const wsStartedAt = Date.now()
        const wsUrl = `ws://127.0.0.1:${localPort}/api/ws?token=${encodeURIComponent(token)}`
        const probe = await deps.probeWebSocket(wsUrl)
        phase('ws', wsStartedAt)

        if (!probe.ok) {
          const error: any = new Error(
            `The remote Hermes backend is HTTP-reachable through the tunnel but the WebSocket (/api/ws) ` +
              `rejected the session token: ${probe.reason || 'unknown reason'}`
          )

          error.kind = 'spawn-failed'
          throw error
        }
      }

      const tokenFingerprint = fingerprintToken(token)

      // Served-token drift (the dashboard regenerated its own) is the ONLY case
      // that needs a third ssh call: the lockfile has to record the credential
      // that actually authenticates, or the next start cannot prove reuse.
      if (tokenFingerprint !== response.tokenFingerprint) {
        assertBootstrapNotSuperseded(deps.signal)
        log('served token differs from the spawn token; re-stamping the remote lockfile')
        await restampLockFingerprint(deps, response, tokenFingerprint)
      }

      assertBootstrapNotSuperseded(deps.signal)

      return {
        baseUrl,
        hermesHome: response.hermesHome,
        hermesPath: response.hermesPath,
        hermesVersion: response.hermesVersion,
        localPort,
        logPath: response.logPath,
        ownershipId: response.ownershipId,
        pid: response.pid,
        platform: { arch: response.arch, os: response.os },
        remotePort: response.port,
        reused: response.reused,
        spawnNonce: response.spawnNonce,
        token,
        tokenFingerprint
      }
    } catch (error) {
      // Any failure past the forward leaves the tunnel behind unless we say so.
      try {
        await deps.cancelForward(localPort, response.port)
      } catch {
        void 0
      }

      throw error
    }
  }
}

// Rewrite ONLY the token fingerprint of a lockfile we already proved is ours.
// Python again (not sed/jq) so the rewrite is atomic and the ownership id is
// re-checked on the remote side before anything is written.
function buildRestampCommand(ownershipId: string, spawnNonce: string, fingerprint: string): string {
  const script = [
    'import json,os,sys',
    `oid=${JSON.stringify(ownershipId)}`,
    `nonce=${JSON.stringify(spawnNonce)}`,
    `fp=${JSON.stringify(fingerprint)}`,
    'p=os.path.expanduser("~/.hermes/desktop-ssh/"+oid+"/backend.lock.json")',
    'raw=open(p,"rb").read()',
    'lock=json.loads(raw.decode("utf-8"))',
    'assert lock.get("ownershipId")==oid and lock.get("spawnNonce")==nonce',
    'lock["tokenFingerprint"]=fp',
    'd=os.path.dirname(p)',
    'prev=os.umask(0o077)',
    't=os.path.join(d,"."+nonce+".restamp.tmp")',
    'open(t,"w").write(json.dumps(lock))',
    'os.replace(t,p)',
    'os.umask(prev)',
    'sys.stdout.write("OK")'
  ].join('\n')

  return `python3 -c ${shq(script)}`
}

async function restampLockFingerprint(deps: BatchedConnectDeps, response: BootstrapResponse, fingerprint: string) {
  const out = await deps.ssh.exec(buildRestampCommand(response.ownershipId, response.spawnNonce, fingerprint), {
    signal: deps.signal
  })

  if (String(out || '').trim() !== 'OK') {
    throw Object.assign(new Error('Could not update the remote SSH backend ownership record.'), {
      kind: 'transient-transport-error'
    })
  }
}

export {
  buildRestampCommand,
  DEFAULT_READY_TIMEOUT_MS,
  fingerprintToken,
  LOCKFILE_SCHEMA_VERSION,
  PROTOCOL_VERSION,
  REMOTE_BOOTSTRAP_PY,
  RESPONSE_MARKER
}
