"""SSH remote execution environment using PowerShell on Windows hosts."""

import base64
import hashlib
import logging
import ntpath
import os
import subprocess
import tempfile
from pathlib import Path

from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
)
from tools.environments.ssh import (
    SSHEnvironment,
    _ensure_ssh_available,
)

logger = logging.getLogger(__name__)


def _decode_ssh_output(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        pass
    return data.decode("latin-1")


def _quote_pwsh_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _snapshot_mutex_name(snapshot_path: str) -> str:
    digest = hashlib.sha256(snapshot_path.encode("utf-8")).hexdigest()[:16]
    return f"HermesSnapshot_{digest}"


def _with_snapshot_mutex(mutex_name: str, body: str, *,
                         on_timeout: str) -> str:
    return (
        "$script:__hm=[System.Threading.Mutex]::new("
        f"$false,{_quote_pwsh_string(mutex_name)});"
        "$script:__hl=$false;try{try{"
        "$script:__hl=$script:__hm.WaitOne(5000)"
        "}catch [System.Threading.AbandonedMutexException]{"
        "$script:__hl=$true};if($script:__hl){"
        f"{body}"
        "}else{"
        f"{on_timeout}"
        "}}finally{if($script:__hl){$script:__hm.ReleaseMutex()};"
        "$script:__hm.Dispose()}"
    )


def _atomic_snapshot_publish(temp_path: str, snapshot_path: str,
                             mutex_name: str, *,
                             raise_on_failure: bool) -> str:
    cleanup = (
        "Remove-Item -Force -ErrorAction SilentlyContinue "
        "-LiteralPath $script:__ht"
    )
    failure = cleanup
    timeout_failure = cleanup
    if raise_on_failure:
        failure += (
            "; throw ('failed to publish Hermes environment snapshot: ' + "
            "$script:__he.Exception.Message)"
        )
        timeout_failure += "; throw 'timed out waiting for snapshot lock'"

    publish = (
        "$script:__ho=$false;for($script:__ha=0;"
        "$script:__ha -lt 20 -and -not $script:__ho;"
        "$script:__ha++){try{if([System.IO.File]::Exists($script:__hs)){"
        "[System.IO.File]::Replace($script:__ht,$script:__hs,"
        "[System.Management.Automation.Language.NullString]::Value)"
        "}else{[System.IO.File]::Move($script:__ht,$script:__hs)};"
        "$script:__ho=$true}catch{$script:__he=$_;"
        "Start-Sleep -Milliseconds 10}};"
        f"if(-not $script:__ho){{{failure}}}"
    )
    return (
        f"$script:__ht={temp_path};$script:__hs={snapshot_path};"
        "$script:__he=$null;" + _with_snapshot_mutex(
            mutex_name,
            publish,
            on_timeout=timeout_failure,
        )
    )


def _snapshot_write_command(temp_path: str, publish: str, *,
                            raise_on_failure: bool) -> str:
    cleanup = (
        "Remove-Item -Force -ErrorAction SilentlyContinue "
        f"-LiteralPath {temp_path}"
    )
    if raise_on_failure:
        cleanup += ";throw"
    return (
        "try{Get-ChildItem Env:|ForEach-Object{"
        "$val=$_.Value -replace \"'\",\"''\";"
        "\"`$env:$($_.Name) = '$val'\"}|"
        "Set-Content -Encoding UTF8 -ErrorAction Stop "
        f"-LiteralPath {temp_path};{publish}"
        f"}}catch{{{cleanup}}}"
    )


class SSHPwshEnvironment(SSHEnvironment):
    """Run commands on a Windows remote over SSH using PowerShell.

    Extends SSHEnvironment — reuses SSH transport (ControlMaster, scp,
    encoding). Overrides shell-related methods to use ``pwsh`` /
    ``powershell`` instead of ``bash``.

    Uses ``-EncodedCommand`` (base64 UTF-16LE) to pass scripts through
    cmd.exe (the typical SSH server default shell on Windows) without
    quoting issues.
    """

    def __init__(self, host: str, user: str, cwd: str = "~",
                 timeout: int = 60, port: int = 22, key_path: str = ""):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path

        self.control_dir = Path(tempfile.gettempdir()) / "hermes-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)

        _socket_id = hashlib.sha256(
            f"{user}@{host}:{port}".encode()
        ).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"

        _ensure_ssh_available()
        self._detect_shell()
        self._remote_home = self._detect_remote_home()
        self._remote_temp = self._detect_remote_temp()

        # Translate Linux-style cwd to Windows path
        if cwd == "~" or cwd == "/root" or cwd.startswith("/home/"):
            cwd = self._remote_home

        BaseEnvironment.__init__(self, cwd=cwd, timeout=timeout)

        self._ensure_remote_dirs()

        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(
                f"{self._remote_home}\\.hermes"
            ),
            upload_fn=self._scp_upload,
            delete_fn=self._ssh_delete,
            bulk_upload_fn=self._ssh_bulk_upload,
        )
        # Skip forced sync on init - too slow for Windows remotes with many files
        # File sync will happen on-demand during execute() via _before_execute()
        self.init_session()

    def get_temp_dir(self) -> str:
        return getattr(self, "_remote_temp", "/tmp")

    def _encode_pwsh_command(self, pwsh_script: str) -> str:
        """Encode PowerShell script as base64 UTF-16LE for EncodedCommand."""
        return base64.b64encode(pwsh_script.encode("utf-16-le")).decode("ascii")

    def _run_pwsh(self, pwsh_script: str, timeout: int = 10, shell: str | None = None) -> subprocess.CompletedProcess:
        """Run PowerShell script on remote via EncodedCommand."""
        encoded = self._encode_pwsh_command(pwsh_script)
        cmd = self._build_ssh_command()
        shell_cmd = shell or self._pwsh_cmd
        cmd.extend([shell_cmd, "-NoProfile", "-EncodedCommand", encoded])
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )

    def _detect_shell(self) -> None:
        for shell in ("pwsh", "powershell"):
            try:
                result = self._run_pwsh("Write-Output 'ok'", timeout=15, shell=shell)
                if result.returncode == 0:
                    self._pwsh_cmd = shell
                    logger.debug("SSH pwsh: using %s on %s", shell, self.host)
                    return
            except subprocess.TimeoutExpired:
                continue
        raise RuntimeError(
            f"pwsh/PowerShell not found on remote {self.host}. "
            "Install PowerShell 7 (pwsh) or use ssh backend with bash."
        )

    def _detect_remote_home(self) -> str:
        try:
            result = self._run_pwsh("Write-Output $env:USERPROFILE")
            home = _decode_ssh_output(result.stdout).strip().rstrip("\r\n")
            if home and result.returncode == 0:
                logger.debug("SSH pwsh: remote home = %s", home)
                return home
        except Exception:
            pass
        return f"C:\\Users\\{self.user}"

    def _detect_remote_temp(self) -> str:
        try:
            result = self._run_pwsh("Write-Output $env:TEMP")
            temp = _decode_ssh_output(result.stdout).strip().rstrip("\r\n")
            if temp and result.returncode == 0:
                return temp.replace("\\", "/")
        except Exception:
            pass
        return f"C:/Users/{self.user}/AppData/Local/Temp"

    def _ensure_remote_dirs(self) -> None:
        base = f"{self._remote_home}\\.hermes"
        dirs = [base, f"{base}\\skills", f"{base}\\credentials", f"{base}\\cache"]
        dirs_str = ", ".join(f"'{d}'" for d in dirs)
        script = f"foreach ($d in @({dirs_str})) {{ New-Item -ItemType Directory -Force -Path $d | Out-Null }}"
        try:
            self._run_pwsh(script, timeout=30)
        except Exception as e:
            logger.warning("SSH pwsh: failed to create remote dirs: %s", e)

    def _scp_upload(self, host_path: str, remote_path: str) -> None:
        """Upload a single file via scp over ControlMaster (Windows-aware)."""
        parent = ntpath.dirname(remote_path)
        # Use PowerShell to create parent directory (not bash mkdir -p)
        try:
            self._run_pwsh(
                f"New-Item -ItemType Directory -Force -Path '{parent}' | Out-Null",
                timeout=30,
            )
        except Exception as e:
            logger.warning("SSH pwsh: failed to create parent dir %s: %s", parent, e)

        scp_cmd = ["scp", "-o", f"ControlPath={self.control_socket}"]
        if self.port != 22:
            scp_cmd.extend(["-P", str(self.port)])
        if self.key_path:
            scp_cmd.extend(["-i", self.key_path])
        scp_cmd.extend([host_path, f"{self.user}@{self.host}:{remote_path}"])
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"scp failed: {_decode_ssh_output(result.stderr).strip()}")

    def _ssh_delete(self, remote_paths: list[str]) -> None:
        paths_str = ", ".join(f"'{p}'" for p in remote_paths)
        script = f"Remove-Item -Force -Path @({paths_str}) -ErrorAction SilentlyContinue"
        try:
            result = self._run_pwsh(script)
            if result.returncode != 0:
                raise RuntimeError(
                    f"remote rm failed: {_decode_ssh_output(result.stderr).strip()}"
                )
        except subprocess.TimeoutExpired:
            raise RuntimeError("remote rm timed out")

    def _ssh_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Upload many files via a single zip archive.

        Creates a zip locally, scp's it to the remote, and extracts via
        PowerShell ``Expand-Archive``.  This avoids the per-file scp/mkdir
        overhead that makes sequential uploads impractically slow on
        Windows remotes (issue #7467 — tar-based bulk transfer is not
        available because Windows lacks ``tar``).
        """
        if not files:
            return

        import zipfile

        base = f"{self._remote_home}\\.hermes"

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zf:
            zip_path = zf.name
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for host_path, remote_path in files:
                    rel = ntpath.relpath(remote_path, base).replace("\\", "/")
                    archive.write(host_path, rel)

            remote_zip = f"{self._remote_home}\\hermes_sync_{os.getpid()}.zip"
            scp_cmd = ["scp", "-o", f"ControlPath={self.control_socket}"]
            if self.port != 22:
                scp_cmd.extend(["-P", str(self.port)])
            if self.key_path:
                scp_cmd.extend(["-i", self.key_path])
            scp_cmd.extend([zip_path, f"{self.user}@{self.host}:{remote_zip}"])
            result = subprocess.run(
                scp_cmd, capture_output=True, timeout=120,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"scp zip failed: {_decode_ssh_output(result.stderr).strip()}"
                )

            extract_script = (
                "$ErrorActionPreference = 'Stop'; "
                "try { "
                f"Expand-Archive -Path '{remote_zip}' "
                f"-DestinationPath '{base}' -Force "
                "} finally { "
                f"Remove-Item '{remote_zip}' -Force -ErrorAction SilentlyContinue "
                "}"
            )
            extract_result = self._run_pwsh(extract_script, timeout=60)
            if extract_result.returncode != 0:
                detail = _decode_ssh_output(extract_result.stderr).strip()
                raise RuntimeError(f"remote zip extract failed: {detail}")
        finally:
            try:
                os.unlink(zip_path)
            except OSError:
                pass

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        encoded = self._encode_pwsh_command(cmd_string)
        cmd = self._build_ssh_command()
        cmd.extend([self._pwsh_cmd, "-NoProfile", "-EncodedCommand", encoded])
        return _popen_bash(cmd, stdin_data)

    def _before_execute(self) -> None:
        """Sync files to remote via FileSyncManager (rate-limited internally)."""
        self._sync_manager.sync()

    def _wrap_command(self, command: str, cwd: str) -> str:
        escaped = command.replace("'", "''")
        _quoted_snap = _quote_pwsh_string(self._snapshot_path)
        _snap_tmp = f"({_quote_pwsh_string(self._snapshot_path + '.tmp.')} + $PID)"
        _snapshot_mutex = _snapshot_mutex_name(self._snapshot_path)

        parts = []

        if self._snapshot_ready:
            parts.append(
                _with_snapshot_mutex(
                    _snapshot_mutex,
                    f". {_quoted_snap} 2>$null",
                    on_timeout="",
                )
            )

        parts.append(f"Set-Location -LiteralPath {_quote_pwsh_string(cwd)}")
        parts.append("if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit 126 }")

        parts.append(f"Invoke-Expression '{escaped}'")
        parts.append("$script:__hermes_ec = $LASTEXITCODE")

        if self._snapshot_ready:
            # Atomic snapshot replacement (issue #38249): write to a
            # per-writer temp file, then atomically replace the live file so
            # concurrent source() calls see the old or new complete snapshot.
            parts.append(
                _snapshot_write_command(
                    _snap_tmp,
                    _atomic_snapshot_publish(
                        _snap_tmp,
                        _quoted_snap,
                        _snapshot_mutex,
                        raise_on_failure=False,
                    ),
                    raise_on_failure=False,
                )
            )

        parts.append(
            f'Write-Output "`n{self._cwd_marker}$((Get-Location).Path){self._cwd_marker}"'
        )
        parts.append("exit $script:__hermes_ec")

        return "\n".join(parts)

    def init_session(self):
        _quoted_cwd = _quote_pwsh_string(self.cwd)
        _quoted_snap = _quote_pwsh_string(self._snapshot_path)
        _snap_tmp = f"({_quote_pwsh_string(self._snapshot_path + '.tmp.')} + $PID)"
        _snapshot_mutex = _snapshot_mutex_name(self._snapshot_path)

        bootstrap_parts = [
            _snapshot_write_command(
                _snap_tmp,
                _atomic_snapshot_publish(
                    _snap_tmp,
                    _quoted_snap,
                    _snapshot_mutex,
                    raise_on_failure=True,
                ),
                raise_on_failure=True,
            ),
            f"Set-Location -LiteralPath {_quoted_cwd}",
            f'Write-Output "`n{self._cwd_marker}$((Get-Location).Path){self._cwd_marker}"',
        ]
        bootstrap = "\n".join(bootstrap_parts)

        try:
            proc = self._run_bash(bootstrap, login=True,
                                  timeout=self._snapshot_timeout)
            result = self._wait_for_process(proc,
                                            timeout=self._snapshot_timeout)
            if result.get("returncode") != 0:
                raise RuntimeError(
                    "snapshot bootstrap failed with exit code "
                    f"{result.get('returncode')}"
                )
            self._snapshot_ready = True
            self._update_cwd(result)
            logger.info(
                "SSH pwsh: session snapshot created (session=%s, cwd=%s)",
                self._session_id, self.cwd,
            )
        except Exception as exc:
            logger.warning(
                "SSH pwsh: init_session failed (session=%s): %s — "
                "falling back to direct pwsh per command",
                self._session_id, exc,
            )
            self._snapshot_ready = False
