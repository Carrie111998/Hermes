"""Device-side executor for opaque Hermes workspace bindings."""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from hermes_cli.runner_protocol import RunnerCommand, RunnerEvent
from hermes_cli.runner_spool import BindingRecord, LeaseRecord, RunnerSpool
from hermes_cli.workspace.domain import normalize_binding_relative_path

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 2 * 1024 * 1024


class WorkspaceRunner:
    def __init__(
        self,
        spool: RunnerSpool,
        *,
        operation_handlers: dict[str, Callable[[RunnerCommand, Path], dict[str, Any]]] | None = None,
        trusted_executables: set[str] | None = None,
    ):
        self.spool = spool
        self.operation_handlers = operation_handlers or {}
        self._event_lock = threading.RLock()
        self._process_lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._canceled_processes: set[str] = set()
        defaults = {sys.executable}
        for name in ("git", "node", "npm", "npx", "pytest", "python3", "uv"):
            resolved = shutil.which(name)
            if resolved:
                defaults.add(resolved)
        self._trusted_executables = {
            str(Path(executable).expanduser().resolve())
            for executable in (trusted_executables or defaults)
        }
        self.terminal_sandbox_available = (
            sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()
        )

    def register_binding(
        self,
        *,
        project_id: str,
        root_path: str | Path,
        label: str,
        binding_id: str | None = None,
    ) -> BindingRecord:
        return self.spool.register_binding(
            binding_id=binding_id,
            label=label,
            project_id=project_id,
            root_path=root_path,
        )

    def acquire_lease(
        self,
        *,
        binding_id: str,
        owner: str,
        ttl_seconds: float,
        expected_head: str | None,
        now: float | None = None,
    ) -> LeaseRecord:
        return self.spool.acquire_lease(
            binding_id=binding_id,
            expected_head=expected_head,
            now=now,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )

    @staticmethod
    def _parts(relative_path: str, *, allow_root: bool = False) -> tuple[str, ...]:
        raw = str(relative_path or "").strip()
        if allow_root and raw in {"", "."}:
            return ()
        normalized = normalize_binding_relative_path(raw)
        return tuple(part for part in normalized.split("/") if part and part != ".")

    @staticmethod
    @contextmanager
    def _directory_fd(root: Path, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            current = os.open(root, flags | nofollow)
            descriptors.append(current)
            for part in parts:
                try:
                    next_fd = os.open(part, flags | nofollow, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o700, dir_fd=current)
                    next_fd = os.open(part, flags | nofollow, dir_fd=current)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise ValueError("binding path crosses a symlink or invalid directory") from exc
                    raise
                descriptors.append(next_fd)
                current = next_fd
            yield current
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _read_file(self, root: Path, relative_path: str) -> dict[str, Any]:
        parts = self._parts(relative_path)
        parent, leaf = parts[:-1], parts[-1]
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            with self._directory_fd(root, parent) as directory_fd:
                descriptor = os.open(
                    leaf,
                    os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("binding path is not a regular file")
                    if info.st_size > MAX_FILE_BYTES:
                        raise ValueError("binding file exceeds the read limit")
                    data = b""
                    while len(data) <= MAX_FILE_BYTES:
                        chunk = os.read(descriptor, min(1024 * 1024, MAX_FILE_BYTES + 1 - len(data)))
                        if not chunk:
                            break
                        data += chunk
                    if len(data) > MAX_FILE_BYTES:
                        raise ValueError("binding file exceeds the read limit")
                finally:
                    os.close(descriptor)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError("binding path crosses a symlink or invalid directory") from exc
            raise

        try:
            return {"encoding": "utf-8", "text": data.decode("utf-8")}
        except UnicodeDecodeError:
            return {"data": base64.b64encode(data).decode("ascii"), "encoding": "base64"}

    def _write_file(self, root: Path, relative_path: str, params: dict[str, Any]) -> dict[str, Any]:
        parts = self._parts(relative_path)
        parent, leaf = parts[:-1], parts[-1]
        if "text" in params:
            data = str(params["text"]).encode("utf-8")
        elif "data" in params:
            try:
                data = base64.b64decode(str(params["data"]), validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("fs.write data must be valid base64") from exc
        else:
            raise ValueError("fs.write requires text or base64 data")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("binding file exceeds the write limit")

        temporary_name = f".hermes-write-{uuid.uuid4().hex}"
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        with self._directory_fd(root, parent, create=True) as directory_fd:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.rename(
                    temporary_name,
                    leaf,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            except Exception:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
                raise

        return {"bytes_written": len(data)}

    def _list_directory(self, root: Path, relative_path: str) -> dict[str, Any]:
        parts = self._parts(relative_path, allow_root=True)
        with self._directory_fd(root, parts) as directory_fd:
            names = sorted(os.listdir(directory_fd))[:2000]
        return {"entries": names, "truncated": len(names) >= 2000}

    def _stat_path(self, root: Path, relative_path: str) -> dict[str, Any]:
        parts = self._parts(relative_path)
        parent, leaf = parts[:-1], parts[-1]
        with self._directory_fd(root, parent) as directory_fd:
            info = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("binding path is a symlink")
        return {
            "is_directory": stat.S_ISDIR(info.st_mode),
            "is_file": stat.S_ISREG(info.st_mode),
            "size": info.st_size,
        }

    @staticmethod
    def _git_head(root: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _git_status(root: Path) -> dict[str, Any]:
        result = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch"],
            cwd=root,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        if result.returncode != 0:
            raise ValueError("binding is not a readable Git repository")
        branch = ""
        head = ""
        changes = 0
        for line in result.stdout.splitlines():
            if line.startswith("# branch.head "):
                branch = line.removeprefix("# branch.head ")
            elif line.startswith("# branch.oid "):
                head = line.removeprefix("# branch.oid ")
            elif line and not line.startswith("#"):
                changes += 1
        return {"branch": branch, "changed_files": changes, "head": head}

    @staticmethod
    def _git(root: Path, args: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        runner_home = root / ".hermes-runner-home"
        runner_home.mkdir(mode=0o700, exist_ok=True)
        environment = {
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(runner_home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "SSH_ASKPASS": "/usr/bin/false",
            "SSH_AUTH_SOCK": "",
        }
        try:
            return subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", *args],
                cwd=root,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                env=environment,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("runner Git command failed") from exc

    @classmethod
    def _git_checked(cls, root: Path, args: list[str], *, timeout: float = 60) -> str:
        result = cls._git(root, args, timeout=timeout)
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "runner Git command failed")
        return result.stdout.strip()

    def _git_worktree_add(self, command: RunnerCommand, root: Path) -> dict[str, Any]:
        existing_branch = str(command.params.get("existingBranch") or "").strip()
        branch = existing_branch or str(command.params.get("branch") or "").strip()
        raw_name = str(command.params.get("name") or branch.rsplit("/", 1)[-1]).strip()
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip(".-")[:80]
        if not branch or not name:
            raise ValueError("worktree branch and name are required")
        self._git_checked(root, ["check-ref-format", "--branch", branch])
        worktrees_root = root / ".hermes-worktrees"
        worktrees_root.mkdir(mode=0o700, exist_ok=True)
        target = worktrees_root / name
        if target.exists():
            raise ValueError("worktree target already exists")

        common_dir_raw = self._git_checked(root, ["rev-parse", "--git-common-dir"])
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = (root / common_dir).resolve()
        exclude = common_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing_excludes = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".hermes-worktrees/" not in existing_excludes.splitlines():
            with exclude.open("a", encoding="utf-8") as handle:
                if existing_excludes and not existing_excludes.endswith("\n"):
                    handle.write("\n")
                handle.write(".hermes-worktrees/\n")

        if existing_branch:
            args = ["worktree", "add", str(target), existing_branch]
        else:
            args = ["worktree", "add", "-b", branch, str(target)]
            base = str(command.params.get("base") or "").strip()
            if base:
                args.append(base)
        self._git_checked(root, args, timeout=120)
        parent = self.spool.binding_record(command.binding_id)
        child = self.register_binding(
            label=branch,
            project_id=parent.project_id,
            root_path=target,
        )
        return {"binding": child.public_dict(), "branch": branch}

    def _git_worktree_remove(self, command: RunnerCommand, root: Path) -> dict[str, Any]:
        child_binding_id = str(command.params.get("binding_id") or "").strip()
        if not child_binding_id:
            raise ValueError("worktree binding_id is required")
        if child_binding_id == command.binding_id:
            raise ValueError("command binding cannot remove itself")
        parent = self.spool.binding_record(command.binding_id)
        child = self.spool.binding_record(child_binding_id)
        if parent.project_id != child.project_id:
            raise ValueError("worktree binding belongs to another project")
        if self.spool.has_live_lease(child_binding_id):
            raise ValueError("worktree binding still has an active lease")
        child_root = self.spool.resolve_binding(child_binding_id)
        parent_common = Path(
            self._git_checked(
                root,
                ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            ).strip()
        ).resolve()
        child_common = Path(
            self._git_checked(
                child_root,
                ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            ).strip()
        ).resolve()
        child_git_dir = Path(
            self._git_checked(
                child_root,
                ["rev-parse", "--path-format=absolute", "--git-dir"],
            ).strip()
        ).resolve()
        if parent_common != child_common or child_git_dir == child_common:
            raise ValueError("binding is not a linked worktree of this project")
        if self._git_checked(
            child_root,
            ["status", "--porcelain=v1", "--untracked-files=all"],
        ).strip():
            raise ValueError("worktree has uncommitted changes")
        self._git_checked(root, ["worktree", "remove", str(child_root)])
        self.spool.revoke_binding(child_binding_id)
        return {"binding_id": child_binding_id, "removed": True}

    def _git_commit(self, command: RunnerCommand, root: Path) -> dict[str, Any]:
        message = str(command.params.get("message") or "").strip()
        checks = command.params.get("checks")
        if not message:
            raise ValueError("commit message is required")
        if not isinstance(checks, list) or not checks:
            raise ValueError("at least one successful check is required before commit")

        check_results: list[dict[str, Any]] = []
        for check in checks:
            if not isinstance(check, dict):
                raise ValueError("commit check is invalid")
            result = self._terminal_run(command, root, params=check)
            check_results.append(result)
            if result["exit_code"] != 0 or result["timed_out"] or result["canceled"]:
                raise ValueError("a required check failed; commit was not created")

        self._git_checked(root, ["add", "-A"])
        staged = self._git(root, ["diff", "--cached", "--quiet"])
        if staged.returncode == 0:
            raise ValueError("there are no staged changes to commit")
        if staged.returncode != 1:
            raise ValueError(staged.stderr.strip() or "could not inspect staged changes")
        self._git_checked(
            root,
            [
                "-c",
                "user.name=Hermes Runner",
                "-c",
                "user.email=hermes-runner@localhost",
                "commit",
                "-m",
                message,
            ],
            timeout=120,
        )
        commit_sha = self._git_checked(root, ["rev-parse", "HEAD"])
        self.spool.update_lease_head(
            binding_id=command.binding_id,
            expected_head=commit_sha,
            fencing_token=command.fencing_token,
            lease_id=command.lease_id,
        )
        return {"checks": check_results, "commit_sha": commit_sha}

    @staticmethod
    def _push_url_identity(root: Path, raw_url: str) -> tuple[str, str, str]:
        effective = raw_url.strip()
        if not effective or any(character in effective for character in "\r\n\0"):
            raise ValueError("Git push URL is invalid")
        parsed = urlsplit(effective)
        if parsed.scheme:
            scheme = parsed.scheme.lower()
            if scheme not in {"file", "git+ssh", "https", "ssh"}:
                raise ValueError("Git push URL scheme is not allowed")
            if scheme == "file":
                effective = str(Path(parsed.path).expanduser().resolve())
                display = f"local:{Path(effective).name}"
            else:
                if not parsed.hostname:
                    raise ValueError("Git push URL hostname is required")
                if scheme == "https" and (parsed.username or parsed.password):
                    raise ValueError("embedded Git HTTPS credentials are not allowed")
                netloc = (
                    f"{parsed.hostname}:{parsed.port}"
                    if parsed.port
                    else str(parsed.hostname)
                )
                display = urlunsplit(
                    (scheme, netloc, parsed.path, parsed.query, parsed.fragment)
                )
        else:
            scp = re.fullmatch(
                r"(?:(?P<user>[^@/\\\s:]+)@)?(?P<host>[^/\\\s:]+):(?P<path>[^\r\n]+)",
                effective,
            )
            if scp is not None:
                display = f"ssh://{scp.group('host')}/{scp.group('path').lstrip('/')}"
            else:
                effective = str(
                    (root / effective).resolve()
                    if not os.path.isabs(effective)
                    else Path(effective).resolve()
                )
                display = f"local:{Path(effective).name}"
        return effective, display, hashlib.sha256(effective.encode()).hexdigest()

    @classmethod
    def _push_snapshot(cls, root: Path) -> dict[str, str]:
        if cls._git_checked(root, ["status", "--porcelain"]):
            raise ValueError("commit or discard changes before requesting push approval")
        branch = cls._git_checked(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        if not branch or branch == "HEAD":
            raise ValueError("a named branch is required before pushing")
        tracking_result = cls._git(
            root,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        )
        tracking = tracking_result.stdout.strip() if tracking_result.returncode == 0 else ""
        remote = tracking.split("/", 1)[0] if "/" in tracking else "origin"
        cls._git_checked(root, ["remote", "get-url", remote])
        push_urls = [
            value.strip()
            for value in cls._git_checked(
                root, ["remote", "get-url", "--push", "--all", remote]
            ).splitlines()
            if value.strip()
        ]
        if len(push_urls) != 1:
            raise ValueError("exactly one Git push destination is required before approval")
        effective_push_url, remote_url, remote_url_digest = cls._push_url_identity(
            root, push_urls[0]
        )
        base_ref = tracking
        if not base_ref:
            symbolic = cls._git(
                root,
                ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"],
            )
            base_ref = symbolic.stdout.strip() if symbolic.returncode == 0 else ""
        if not base_ref:
            for candidate in (f"{remote}/main", f"{remote}/master"):
                if cls._git(root, ["rev-parse", "--verify", candidate]).returncode == 0:
                    base_ref = candidate
                    break
        commit_sha = cls._git_checked(root, ["rev-parse", "HEAD"])
        commit_range = f"{base_ref}..HEAD" if base_ref else "HEAD"
        commits = cls._git_checked(root, ["log", "--format=%H%x00%P%x00%s", commit_range])
        if not commits:
            raise ValueError("there are no local commits to push")
        diff = cls._git_checked(
            root,
            ["diff", "--binary", f"{base_ref}...HEAD"]
            if base_ref
            else ["show", "--binary", "--format=fuller", "--no-ext-diff", "HEAD"],
        )
        digest_payload = {
            "baseRef": base_ref,
            "commitSha": commit_sha,
            "commits": commits,
            "destinationBranch": branch,
            "diff": diff,
            "remote": remote,
            "remoteUrl": remote_url,
            "remoteUrlDigest": remote_url_digest,
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "changeSetDigest": digest,
            "commitSha": commit_sha,
            "destinationBranch": branch,
            "effectivePushUrl": effective_push_url,
            "remote": remote,
            "remoteUrl": remote_url,
            "remoteUrlDigest": remote_url_digest,
        }

    def _git_push_request(self, command: RunnerCommand, root: Path) -> dict[str, Any]:
        now = time.time()
        snapshot = self._push_snapshot(root)
        effective_push_url = snapshot.pop("effectivePushUrl")
        request = {
            **snapshot,
            "createdAt": datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
            "expiresAt": datetime.fromtimestamp(now + 600, UTC).isoformat().replace("+00:00", "Z"),
            "requestId": str(uuid.uuid4()),
        }
        self.spool.store_push_request(
            binding_id=command.binding_id,
            request={**request, "_effectivePushUrl": effective_push_url},
        )
        return request


    def active_process_ids(self) -> list[str]:
        with self._process_lock:
            return sorted(self._processes)

    def shutdown(self) -> None:
        for command_id in self.active_process_ids():
            self.cancel_process(command_id)

    def cancel_process(self, command_id: str) -> bool:
        with self._process_lock:
            process = self._processes.get(command_id)
            if process is None or process.poll() is not None:
                return False
            self._canceled_processes.add(command_id)
            try:
                os.killpg(process.pid, 15)
            except (OSError, ProcessLookupError):
                process.terminate()
            return True

    @staticmethod
    def _sandbox_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _terminal_cwd(self, root: Path, relative_path: str) -> Path:
        current = root
        for part in self._parts(relative_path, allow_root=True):
            current = current / part
            try:
                info = current.lstat()
            except OSError as exc:
                raise ValueError("terminal cwd does not exist inside the binding") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("terminal cwd crosses a symlink or is not a directory")
        resolved = current.resolve(strict=True)
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            raise ValueError("terminal cwd escapes the binding")
        return resolved

    def _resolve_terminal_executable(self, raw: str) -> str:
        candidate = str(raw or "").strip()
        if not candidate or "\x00" in candidate:
            raise ValueError("terminal executable is required")
        resolved = (
            Path(candidate).expanduser().resolve()
            if os.path.isabs(candidate)
            else Path(shutil.which(candidate) or "").resolve()
        )
        if not str(resolved) or str(resolved) not in self._trusted_executables:
            raise ValueError("terminal executable is not trusted")
        return str(resolved)

    def _terminal_run(
        self,
        command: RunnerCommand,
        root: Path,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.terminal_sandbox_available:
            raise ValueError("a supported terminal sandbox is unavailable; execution is disabled")
        operation = params or command.params
        raw_argv = operation.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv or len(raw_argv) > 256:
            raise ValueError("terminal argv must be a non-empty bounded list")
        argv = [str(item) for item in raw_argv]
        if any("\x00" in item or len(item) > 32_768 for item in argv):
            raise ValueError("terminal argv contains an invalid argument")
        executable = self._resolve_terminal_executable(argv[0])
        cwd = self._terminal_cwd(root, str(operation.get("cwd") or "."))
        try:
            timeout_seconds = float(operation.get("timeout_seconds", 300))
        except (TypeError, ValueError) as exc:
            raise ValueError("terminal timeout is invalid") from exc
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("terminal timeout must be between 0 and 3600 seconds")

        runner_home = root / ".hermes-runner-home"
        runner_tmp = root / ".hermes-runner-tmp"
        runner_home.mkdir(mode=0o700, exist_ok=True)
        runner_tmp.mkdir(mode=0o700, exist_ok=True)
        escaped_root = self._sandbox_literal(str(root))
        profile = " ".join(
            (
                "(version 1)",
                "(deny default)",
                "(allow process*)",
                "(allow file-read*)",
                f'(allow file-write* (subpath "{escaped_root}") (literal "/dev/null"))',
                "(allow sysctl-read)",
                "(allow mach-lookup)",
                "(allow signal (target self))",
            )
        )
        process_argv = ["/usr/bin/sandbox-exec", "-p", profile, executable, *argv[1:]]
        environment = {
            "CI": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(runner_home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(runner_tmp),
        }
        process = subprocess.Popen(
            process_argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with self._process_lock:
            self._processes[command.command_id] = process

        output_chunks: list[bytes] = []
        output_size = 0
        output_truncated = False
        output_lock = threading.Lock()

        def consume_output() -> None:
            nonlocal output_size, output_truncated
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", "replace")
                self._emit(command, "run.output", {"chunk": text, "stream": "combined"})
                with output_lock:
                    remaining = MAX_PROCESS_OUTPUT_BYTES - output_size
                    if remaining > 0:
                        kept = chunk[:remaining]
                        output_chunks.append(kept)
                        output_size += len(kept)
                    if len(chunk) > remaining:
                        output_truncated = True

        reader = threading.Thread(target=consume_output, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, 15)
            except (OSError, ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, 9)
                except (OSError, ProcessLookupError):
                    process.kill()
                process.wait(timeout=3)
        finally:
            reader.join(timeout=3)
            with self._process_lock:
                canceled = command.command_id in self._canceled_processes
                self._canceled_processes.discard(command.command_id)
                self._processes.pop(command.command_id, None)

        return {
            "canceled": canceled,
            "exit_code": process.returncode,
            "output": b"".join(output_chunks).decode("utf-8", "replace"),
            "output_truncated": output_truncated,
            "timed_out": timed_out,
        }

    def _emit(self, command: RunnerCommand, event_type: str, payload: dict[str, Any]) -> None:
        with self._event_lock:
            self.spool.append_event(self._new_event(command, event_type, payload))

    def _new_event(
        self,
        command: RunnerCommand,
        event_type: str,
        payload: dict[str, Any],
    ) -> RunnerEvent:
        return RunnerEvent.create(
            attempt_id=command.attempt_id,
            event_type=event_type,
            payload=payload,
            run_id=command.run_id,
            sequence=self.spool.next_event_sequence(command.attempt_id),
        )

    def _dispatch(self, command: RunnerCommand, root: Path) -> dict[str, Any]:
        handler = self.operation_handlers.get(command.method)
        if handler is not None:
            return handler(command, root)
        if command.method == "binding.inspect":
            binding = next(
                (
                    item
                    for item in self.spool.public_bindings()
                    if item["binding_id"] == command.binding_id
                ),
                None,
            )
            if binding is None:
                raise ValueError("binding is unknown")
            return binding
        if command.method == "fs.read":
            return self._read_file(root, str(command.params.get("path") or ""))
        if command.method == "fs.write":
            return self._write_file(root, str(command.params.get("path") or ""), command.params)
        if command.method == "fs.list":
            return self._list_directory(root, str(command.params.get("path") or "."))
        if command.method == "fs.stat":
            return self._stat_path(root, str(command.params.get("path") or ""))
        if command.method == "git.status":
            return self._git_status(root)
        if command.method == "git.worktree.add":
            return self._git_worktree_add(command, root)
        if command.method == "git.worktree.remove":
            return self._git_worktree_remove(command, root)
        if command.method == "git.commit":
            return self._git_commit(command, root)
        if command.method == "git.push.request":
            return self._git_push_request(command, root)

        if command.method == "terminal.run":
            return self._terminal_run(command, root)
        if command.method == "process.cancel":
            target = str(command.params.get("command_id") or "")
            return {"canceled": self.cancel_process(target), "command_id": target}
        raise ValueError(f"runner method is not implemented: {command.method}")

    def accept(self, command: RunnerCommand) -> tuple[bool, dict[str, Any] | None]:
        # Revocation/root replacement is an authority check, not a command
        # result. It must run before idempotent replay so a cached success can
        # never resurrect a revoked binding.
        root = self.spool.resolve_binding(command.binding_id)

        if not self.spool.begin_command(command):
            stored = self.spool.command_result(command.command_id)
            if stored is None or stored["result"] is None:
                return False, {
                    "ok": False,
                    "replayed": True,
                    "state": stored["state"] if stored else "unknown",
                }
            replay = dict(stored["result"])
            replay["replayed"] = True
            return False, replay

        try:
            self.spool.validate_lease(
                binding_id=command.binding_id,
                fencing_token=command.fencing_token,
                lease_id=command.lease_id,
                live_head=self._git_head(root),
            )
            self._emit(command, "run.accepted", {"command_id": command.command_id})
            return True, None
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            response = {"error": error, "ok": False, "replayed": False}
            with self._event_lock:
                self.spool.complete_command_with_event(
                    command.command_id,
                    result=response,
                    state="failed",
                    event=self._new_event(command, "run.failed", {"error": error}),
                )
            raise

    def execute_accepted(self, command: RunnerCommand) -> dict[str, Any]:
        root = self.spool.resolve_binding(command.binding_id)
        try:
            self.spool.validate_lease(
                binding_id=command.binding_id,
                fencing_token=command.fencing_token,
                lease_id=command.lease_id,
                live_head=self._git_head(root),
            )
            self._emit(command, "run.started", {"method": command.method})
            result = self._dispatch(command, root)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            with self._event_lock:
                self.spool.complete_command_with_event(
                    command.command_id,
                    result={"error": error, "ok": False, "replayed": False},
                    state="failed",
                    event=self._new_event(command, "run.failed", {"error": error}),
                )
            if isinstance(exc, ValueError):
                raise
            raise ValueError(error) from exc

        response = {"ok": True, "replayed": False, "result": result}
        with self._event_lock:
            self.spool.complete_command_with_event(
                command.command_id,
                result=response,
                state="completed",
                event=self._new_event(
                    command,
                    "run.completed",
                    {"command_id": command.command_id},
                ),
            )
        return response

    def execute(self, command: RunnerCommand) -> dict[str, Any]:
        accepted, replay = self.accept(command)
        if not accepted:
            if replay is None:
                raise ValueError("runner replay result is missing")
            return replay
        return self.execute_accepted(command)
