"""Operator-owned lifecycle for a locally installed Ares runtime.

This is deliberately separate from the candidate-custody activation path.  It
owns one local, source-tracked runtime for an operator who wants a stable
agent while their checkout remains a development worktree.  It never imports
from that worktree after ``ares setup`` has completed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_CONFIG_SCHEMA = 1


class AresLocalRuntimeError(RuntimeError):
    """Raised when the explicit local-runtime contract is not satisfied."""


@dataclass(frozen=True)
class AresLocalPaths:
    """All state owned by the local Ares runtime controller."""

    state_root: Path
    data_root: Path
    agent_home: Path
    launcher_path: Path
    unit_path: Path

    @property
    def config_path(self) -> Path:
        return self.state_root / "config.json"

    @property
    def lock_path(self) -> Path:
        return self.state_root / "control.lock"

    @property
    def releases_dir(self) -> Path:
        return self.data_root / "releases"

    @property
    def staging_dir(self) -> Path:
        return self.data_root / "staging"

    @property
    def current_link(self) -> Path:
        return self.data_root / "current"

    @property
    def previous_link(self) -> Path:
        return self.data_root / "previous"


def _default_paths() -> AresLocalPaths:
    home = Path.home()
    agent_home = home / ".ares"
    return AresLocalPaths(
        state_root=agent_home / "runtime-state",
        data_root=agent_home / "runtime",
        agent_home=agent_home,
        launcher_path=home / ".local" / "bin" / "ares",
        unit_path=home / ".config" / "systemd" / "user" / "ares-gateway.service",
    )


class AresLocalRuntime:
    """Build, select, and launch the one stable local Ares runtime.

    ``current`` is the only active-runtime pointer.  ``previous`` is solely a
    rollback target.  Repository metadata is held separately in ``config`` so
    there is no duplicate active-runtime truth and no development-worktree
    fallback after setup.
    """

    def __init__(self, paths: AresLocalPaths | None = None) -> None:
        self.paths = paths or _default_paths()

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.paths.state_root.mkdir(parents=True, exist_ok=True)
        with self.paths.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_layout(self) -> None:
        self.paths.state_root.mkdir(parents=True, exist_ok=True)
        self.paths.releases_dir.mkdir(parents=True, exist_ok=True)
        self.paths.staging_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _run(
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        capture: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(arg) for arg in args]
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=False,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise AresLocalRuntimeError(
                f"command failed ({completed.returncode}): {' '.join(command)}"
                + (f"\n{detail}" if detail else "")
            )
        return completed

    @staticmethod
    def _git_output(source: Path, *args: str) -> str:
        return AresLocalRuntime._run(
            ["git", "-C", source, *args], capture=True
        ).stdout.strip()

    @staticmethod
    def _require_revision(value: object) -> str:
        revision = str(value)
        if not _REVISION_RE.fullmatch(revision):
            raise AresLocalRuntimeError("invalid Ares release revision")
        return revision

    def _release_dir(self, revision: str) -> Path:
        return self.paths.releases_dir / self._require_revision(revision)

    def _release_source(self, revision: str) -> Path:
        source = self._release_dir(revision) / "source"
        if not source.is_dir():
            raise AresLocalRuntimeError(f"release {revision} is not installed")
        return source

    @staticmethod
    def _python_for(source: Path) -> Path:
        return source / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _release_from_link(self, link: Path, label: str) -> tuple[str, Path] | None:
        if not link.is_symlink():
            return None
        source = link.resolve(strict=True)
        try:
            relative = source.relative_to(self.paths.releases_dir.resolve())
        except ValueError as exc:
            raise AresLocalRuntimeError(f"{label} pointer escapes the Ares release directory") from exc
        if len(relative.parts) != 2 or relative.parts[1] != "source":
            raise AresLocalRuntimeError(f"{label} pointer has an invalid release layout")
        revision = self._require_revision(relative.parts[0])
        if source != self._release_source(revision).resolve():
            raise AresLocalRuntimeError(f"{label} pointer does not match its release")
        return revision, source

    def active_release(self) -> tuple[str, Path]:
        value = self._release_from_link(self.paths.current_link, "current")
        if value is None:
            raise AresLocalRuntimeError("Ares is not set up; run `ares setup --source <checkout>`")
        return value

    def previous_release(self) -> tuple[str, Path] | None:
        return self._release_from_link(self.paths.previous_link, "previous")

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _atomic_link(path: Path, target: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        try:
            os.symlink(str(target), temporary)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()

    def _read_config(self) -> dict[str, object]:
        try:
            raw = json.loads(self.paths.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AresLocalRuntimeError("Ares source configuration is missing; run `ares setup`") from exc
        except json.JSONDecodeError as exc:
            raise AresLocalRuntimeError("Ares source configuration is invalid") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != _CONFIG_SCHEMA:
            raise AresLocalRuntimeError("Ares source configuration has an unsupported schema")
        remote = raw.get("remote")
        branch = raw.get("branch")
        if not isinstance(remote, str) or not remote.strip() or "\n" in remote:
            raise AresLocalRuntimeError("Ares source configuration has an invalid remote")
        if not isinstance(branch, str) or not branch.strip() or "\n" in branch:
            raise AresLocalRuntimeError("Ares source configuration has an invalid branch")
        return raw

    def _write_config(self, *, remote: str, branch: str) -> None:
        self._atomic_json(
            self.paths.config_path,
            {
                "schema_version": _CONFIG_SCHEMA,
                "remote": remote,
                "branch": branch,
            },
        )

    def _activate(self, revision: str) -> None:
        target = self._release_source(revision).resolve()
        current = self._release_from_link(self.paths.current_link, "current")
        if current is not None and current[1] == target:
            return
        if current is not None:
            self._atomic_link(self.paths.previous_link, current[1])
        self._atomic_link(self.paths.current_link, target)

    @staticmethod
    def _build_environment(source: Path) -> dict[str, str]:
        environment = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
            environment.pop(name, None)
        environment["UV_PROJECT_ENVIRONMENT"] = str(source / ".venv")
        return environment

    def _agent_environment(self) -> dict[str, str]:
        """Return the process environment for the isolated Ares agent home."""

        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(self.paths.agent_home)
        return environment

    def _seed_agent_home(self, source_home: Path) -> bool:
        """Create an independent Ares home from the useful Hermes settings once.

        This is an explicit migration, not a runtime fallback.  Secrets and
        settings are copied so the already working provider configuration is
        available immediately; later changes are independent in each home.
        """

        source_home = source_home.expanduser().resolve()
        marker = self.paths.agent_home / "ares-migration.json"
        if marker.exists():
            return False
        if self.paths.agent_home.exists():
            managed_entries = {"runtime", "runtime-state"}
            if any(entry.name not in managed_entries for entry in self.paths.agent_home.iterdir()):
                return False
        else:
            self.paths.agent_home.mkdir(parents=True, exist_ok=False)
        copied: list[str] = []
        for name in ("config.yaml", ".env", "active_profile"):
            candidate = source_home / name
            if candidate.is_file():
                shutil.copy2(candidate, self.paths.agent_home / name)
                copied.append(name)
        for name in ("profiles", "skills", "plugins"):
            candidate = source_home / name
            if candidate.is_dir():
                shutil.copytree(candidate, self.paths.agent_home / name, symlinks=True)
                copied.append(name)
        self._atomic_json(
            self.paths.agent_home / "ares-migration.json",
            {
                "schema_version": 1,
                "source_home": str(source_home),
                "copied": copied,
                "migrated_at": int(time.time()),
            },
        )
        return True

    def _build_runtime(self, source: Path, *, desktop: bool) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise AresLocalRuntimeError("`uv` is required to build the stable Ares runtime")
        environment = self._build_environment(source)
        self._run(
            [uv, "sync", "--locked", "--extra", "all", "--no-dev", "--no-editable"],
            cwd=source,
            env=environment,
        )
        python = self._python_for(source)
        if not python.is_file():
            raise AresLocalRuntimeError("Ares runtime build did not create its Python interpreter")
        self._run(
            [python, "-c", "import ares_runtime.local_runtime; import hermes_cli.main"],
            cwd=source,
            env=self._build_environment(source),
        )
        if desktop:
            npm = shutil.which("npm")
            if npm is None:
                raise AresLocalRuntimeError("`npm` is required to build the Ares Desktop application")
            self._run([npm, "ci"], cwd=source, env=self._build_environment(source))
            self._run([npm, "run", "pack"], cwd=source / "apps" / "desktop", env=self._build_environment(source))
            if self._desktop_binary(source) is None:
                raise AresLocalRuntimeError("Ares Desktop build completed without an executable")

    @staticmethod
    def _desktop_binary(source: Path) -> Path | None:
        if sys.platform == "darwin":
            candidate = source / "apps" / "desktop" / "release" / "mac" / "Hermes.app" / "Contents" / "MacOS" / "Hermes"
        elif os.name == "nt":
            candidate = source / "apps" / "desktop" / "release" / "win-unpacked" / "Hermes.exe"
        else:
            candidate = source / "apps" / "desktop" / "release" / "linux-unpacked" / "Hermes"
        return candidate if candidate.is_file() else None

    def _materialize(self, source_spec: str, revision: str, *, desktop: bool) -> None:
        self._ensure_layout()
        final_dir = self._release_dir(revision)
        if final_dir.exists():
            source = self._release_source(revision)
            self._build_runtime(source, desktop=desktop and self._desktop_binary(source) is None)
            return
        staging = self.paths.staging_dir / f"{revision}.{uuid.uuid4().hex}"
        source = staging / "source"
        try:
            self._run(["git", "clone", "--no-local", source_spec, source])
            self._run(["git", "-C", source, "checkout", "--detach", revision])
            self._build_runtime(source, desktop=desktop)
            self._atomic_json(
                staging / "release.json",
                {
                    "revision": revision,
                    "source": source_spec,
                    "installed_at": int(time.time()),
                },
            )
            os.replace(staging, final_dir)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def _install_launcher(self) -> None:
        self.paths.launcher_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"runtime_root={str(self.paths.current_link)!r}\n"
            "python=\"$runtime_root/.venv/bin/python\"\n"
            "if [[ ! -x \"$python\" ]]; then\n"
            "  printf '%s\\n' 'Ares runtime is not installed; run ares setup from the Ares checkout.' >&2\n"
            "  exit 1\n"
            "fi\n"
            "exec \"$python\" -m ares_runtime.local_runtime \"$@\"\n"
        )
        temporary = self.paths.launcher_path.with_name(f".{self.paths.launcher_path.name}.{uuid.uuid4().hex}")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o755)
        os.replace(temporary, self.paths.launcher_path)

    def _install_gateway_unit(self) -> None:
        self.paths.unit_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[Unit]\n"
            "Description=Ares stable Hermes gateway\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"Environment=HERMES_HOME={self.paths.agent_home}\n"
            f"ExecStart={self.paths.launcher_path} gateway --foreground\n"
            "Restart=on-failure\n"
            "RestartSec=3\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        temporary = self.paths.unit_path.with_name(f".{self.paths.unit_path.name}.{uuid.uuid4().hex}")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, self.paths.unit_path)

    @staticmethod
    def _systemd_environment() -> dict[str, str]:
        """Resolve the normal user bus when Ares starts outside a login shell."""

        environment = os.environ.copy()
        runtime_dir = environment.get("XDG_RUNTIME_DIR")
        if not runtime_dir:
            candidate = Path("/run/user") / str(os.getuid())
            if candidate.is_dir():
                runtime_dir = str(candidate)
                environment["XDG_RUNTIME_DIR"] = runtime_dir
        if runtime_dir and not environment.get("DBUS_SESSION_BUS_ADDRESS"):
            bus = Path(runtime_dir) / "bus"
            if bus.exists():
                environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        return environment

    def _systemctl(self, *args: str, required: bool = True) -> bool:
        if shutil.which("systemctl") is None:
            if required:
                raise AresLocalRuntimeError("systemd user services are unavailable on this host")
            return False
        completed = subprocess.run(
            ["systemctl", "--user", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._systemd_environment(),
            check=False,
        )
        if completed.returncode and required:
            detail = (completed.stderr or completed.stdout).strip()
            raise AresLocalRuntimeError(
                f"systemctl --user {' '.join(args)} failed"
                + (f": {detail}" if detail else "")
            )
        return completed.returncode == 0

    def _handoff_gateway(self, *, legacy_active: bool) -> None:
        self._systemctl("daemon-reload")
        if legacy_active:
            self._systemctl("disable", "--now", "hermes-gateway.service")
        try:
            self._systemctl("enable", "--now", "ares-gateway.service")
            time.sleep(1)
            if not self._systemctl("is-active", "--quiet", "ares-gateway.service", required=False):
                raise AresLocalRuntimeError("Ares gateway did not remain active after startup")
        except Exception:
            self._systemctl("disable", "--now", "ares-gateway.service", required=False)
            if legacy_active:
                self._systemctl("enable", "--now", "hermes-gateway.service", required=False)
            raise

    def setup(
        self,
        source: Path,
        *,
        desktop: bool,
        gateway: bool,
        seed_from: Path,
    ) -> tuple[str, bool]:
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise AresLocalRuntimeError(f"Ares source checkout does not exist: {source}")
        if self._git_output(source, "rev-parse", "--is-inside-work-tree") != "true":
            raise AresLocalRuntimeError(f"not a Git checkout: {source}")
        revision = self._require_revision(self._git_output(source, "rev-parse", "HEAD"))
        try:
            remote = self._git_output(source, "remote", "get-url", "origin")
        except AresLocalRuntimeError:
            remote = str(source)
        try:
            branch = self._git_output(source, "symbolic-ref", "--quiet", "--short", "HEAD")
        except AresLocalRuntimeError:
            branch = "main"
        with self.locked():
            old_active = self._release_from_link(self.paths.current_link, "current")
            legacy_active = self._systemctl("is-active", "--quiet", "hermes-gateway.service", required=False)
            self._materialize(str(source), revision, desktop=desktop)
            seeded = self._seed_agent_home(seed_from)
            self._activate(revision)
            self._write_config(remote=remote, branch=branch)
            self._install_launcher()
            if gateway:
                self._install_gateway_unit()
                try:
                    self._handoff_gateway(legacy_active=legacy_active)
                except Exception:
                    if old_active is not None:
                        self._atomic_link(self.paths.current_link, old_active[1])
                    else:
                        self.paths.current_link.unlink(missing_ok=True)
                    raise
        return revision, seeded

    def update(self, *, desktop: bool) -> tuple[str, bool]:
        with self.locked():
            config = self._read_config()
            remote = str(config["remote"])
            branch = str(config["branch"])
            revision = self._require_revision(
                self._run(["git", "ls-remote", remote, f"refs/heads/{branch}"], capture=True)
                .stdout.split()[0]
            )
            current = self._release_from_link(self.paths.current_link, "current")
            if current is not None and current[0] == revision:
                return revision, False
            old_active = current
            self._materialize(remote, revision, desktop=desktop)
            self._activate(revision)
            if self.paths.unit_path.exists():
                try:
                    self._systemctl("restart", "ares-gateway.service")
                    time.sleep(1)
                    if not self._systemctl("is-active", "--quiet", "ares-gateway.service", required=False):
                        raise AresLocalRuntimeError("Ares gateway did not remain active after update")
                except Exception:
                    if old_active is not None:
                        self._atomic_link(self.paths.current_link, old_active[1])
                        self._systemctl("restart", "ares-gateway.service", required=False)
                    raise
            return revision, True

    def rollback(self) -> str:
        with self.locked():
            current = self.active_release()
            previous = self.previous_release()
            if previous is None:
                raise AresLocalRuntimeError("no previous Ares runtime is available for rollback")
            self._atomic_link(self.paths.current_link, previous[1])
            self._atomic_link(self.paths.previous_link, current[1])
            if self.paths.unit_path.exists():
                self._systemctl("restart", "ares-gateway.service")
                time.sleep(1)
                if not self._systemctl("is-active", "--quiet", "ares-gateway.service", required=False):
                    self._atomic_link(self.paths.current_link, current[1])
                    self._atomic_link(self.paths.previous_link, previous[1])
                    self._systemctl("restart", "ares-gateway.service", required=False)
                    raise AresLocalRuntimeError("Ares gateway did not remain active after rollback")
            return previous[0]

    def doctor(self) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []
        try:
            revision, source = self.active_release()
            checks.append(("active runtime", True, revision))
        except AresLocalRuntimeError as exc:
            checks.append(("active runtime", False, str(exc)))
            return checks
        python = self._python_for(source)
        checks.append(("stable Python", python.is_file(), str(python)))
        if python.is_file():
            probe = subprocess.run(
                [python, "-c", "import ares_runtime.local_runtime; import hermes_cli.main"],
                cwd=source,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            checks.append(
                ("Ares and Hermes imports", probe.returncode == 0, (probe.stderr or "ok").strip())
            )
        context_governor = shutil.which("context-governor")
        checks.append(
            ("Context Governor binary", context_governor is not None, context_governor or "not found"))
        gateway_active = self._systemctl("is-active", "--quiet", "ares-gateway.service", required=False)
        checks.append(("Ares gateway", gateway_active, "active" if gateway_active else "inactive"))
        return checks

    def status(self) -> list[str]:
        current = self._release_from_link(self.paths.current_link, "current")
        previous = self.previous_release()
        try:
            config = self._read_config()
        except AresLocalRuntimeError as exc:
            return [f"Ares status: {exc}"]
        return [
            f"active: {current[0] if current else 'none'}",
            f"previous: {previous[0] if previous else 'none'}",
            f"remote: {config['remote']}",
            f"branch: {config['branch']}",
            f"gateway: {'active' if self._systemctl('is-active', '--quiet', 'ares-gateway.service', required=False) else 'inactive'}",
        ]

    def _exec_hermes(self, arguments: Sequence[str]) -> None:
        _, source = self.active_release()
        python = self._python_for(source)
        if not python.is_file():
            raise AresLocalRuntimeError("stable Ares Python is missing; run `ares update`")
        os.chdir(source)
        os.execve(
            str(python),
            [str(python), "-m", "hermes_cli.main", *arguments],
            self._agent_environment(),
        )

    def tui(self, arguments: Sequence[str]) -> None:
        self._exec_hermes(["--tui", *arguments])

    def chat(self, arguments: Sequence[str]) -> None:
        self._exec_hermes(arguments)

    def gateway(self, action: str) -> None:
        if action == "foreground":
            self._exec_hermes(["gateway"])
        if action == "start":
            self._systemctl("enable", "--now", "ares-gateway.service")
        elif action == "stop":
            self._systemctl("disable", "--now", "ares-gateway.service")
        elif action == "restart":
            self._systemctl("restart", "ares-gateway.service")
        elif action == "status":
            active = self._systemctl("is-active", "--quiet", "ares-gateway.service", required=False)
            print("Ares gateway is " + ("active" if active else "inactive"))
            if not active:
                raise AresLocalRuntimeError("Ares gateway is inactive")
        else:
            raise AresLocalRuntimeError(f"unsupported gateway action: {action}")

    def desktop(self, *, rebuild: bool) -> None:
        revision, source = self.active_release()
        if rebuild:
            with self.locked():
                self._build_runtime(source, desktop=True)
        executable = self._desktop_binary(source)
        if executable is None:
            raise AresLocalRuntimeError(
                "Ares Desktop is not built; run `ares update` or `ares desktop --rebuild`"
            )
        environment = self._agent_environment()
        environment["HERMES_DESKTOP_HERMES_ROOT"] = str(source)
        environment["HERMES_DESKTOP_PYTHON"] = str(self._python_for(source))
        environment["HERMES_DESKTOP_APP_NAME"] = "Ares"
        subprocess.Popen([str(executable)], cwd=source, env=environment, start_new_session=True)
        print(f"Ares Desktop started from stable release {revision}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ares",
        description="Manage the stable local Ares runtime independently from its development checkout.",
    )
    subparsers = parser.add_subparsers(dest="command")
    setup = subparsers.add_parser("setup", help="Build and select a stable runtime from a Git checkout")
    setup.add_argument("--source", type=Path, default=Path.cwd(), help="Ares checkout to install (default: current directory)")
    setup.add_argument(
        "--seed-from",
        type=Path,
        default=Path.home() / ".hermes",
        help="copy settings and credentials from this Hermes home only when ~/.ares does not yet exist",
    )
    setup.add_argument("--no-desktop", action="store_true", help="Do not build or install Desktop")
    setup.add_argument("--no-gateway", action="store_true", help="Do not install or start the Ares gateway service")
    update = subparsers.add_parser("update", help="Build and atomically select the configured remote branch")
    update.add_argument("--no-desktop", action="store_true", help="Do not build Desktop for this release")
    subparsers.add_parser("rollback", help="Return to the previous stable runtime")
    subparsers.add_parser("doctor", help="Check the selected runtime and gateway")
    subparsers.add_parser("status", help="Show the selected runtime, remote, and gateway")
    desktop = subparsers.add_parser("desktop", help="Launch the selected Ares Desktop application")
    desktop.add_argument("--rebuild", action="store_true", help="Build Desktop in the selected stable runtime first")
    tui = subparsers.add_parser("tui", help="Launch the selected TUI")
    tui.add_argument("arguments", nargs=argparse.REMAINDER)
    chat = subparsers.add_parser("chat", help="Launch the selected Hermes-compatible CLI")
    chat.add_argument("arguments", nargs=argparse.REMAINDER)
    gateway = subparsers.add_parser("gateway", help="Manage the selected Ares gateway service")
    gateway.add_argument("action", choices=("start", "stop", "restart", "status", "foreground"))
    parser.add_argument("--version", action="store_true", help="Print the selected stable runtime revision")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    runtime = AresLocalRuntime()
    try:
        if args.version:
            revision, _ = runtime.active_release()
            print(f"Ares {revision}")
        elif args.command == "setup":
            revision, seeded = runtime.setup(
                args.source,
                desktop=not args.no_desktop,
                gateway=not args.no_gateway,
                seed_from=args.seed_from,
            )
            print(f"Ares stable runtime selected: {revision}")
            if seeded:
                print(f"Ares home seeded once from: {args.seed_from}")
        elif args.command == "update":
            revision, changed = runtime.update(desktop=not args.no_desktop)
            print(("Updated" if changed else "Already current") + f" Ares runtime: {revision}")
        elif args.command == "rollback":
            print(f"Rolled back Ares runtime to: {runtime.rollback()}")
        elif args.command == "doctor":
            checks = runtime.doctor()
            for label, passed, detail in checks:
                print(f"{'PASS' if passed else 'FAIL'} {label}: {detail}")
            if not all(passed for _, passed, _ in checks):
                raise AresLocalRuntimeError("Ares doctor found failed checks")
        elif args.command == "status":
            print("\n".join(runtime.status()))
        elif args.command == "desktop":
            runtime.desktop(rebuild=args.rebuild)
        elif args.command == "tui":
            runtime.tui(args.arguments)
        elif args.command == "chat":
            runtime.chat(args.arguments)
        elif args.command == "gateway":
            runtime.gateway(args.action)
        else:
            runtime.tui(())
    except AresLocalRuntimeError as exc:
        print(f"ares: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
