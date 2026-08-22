"""Fly Sprites cloud execution environment.

The backend uses the official ``sprites-py`` distribution (imported as
``sprites``). Authentication remains host-side: Sprites receives only shell
commands and the narrow skills/cache file set managed by FileSyncManager.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shlex
import threading
import uuid
from pathlib import Path

from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle
from tools.environments.file_sync import FileSyncManager

logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "https://api.sprites.dev"
_DEFAULT_SPRITE_HOME = "/home/sprite"
_MAX_SPRITE_NAME_LENGTH = 63


def sprite_name_for_task(task_id: str, *, namespace: str | None = None) -> str:
    """Return a deterministic, profile-scoped Sprites-safe task name."""
    if namespace is None:
        from hermes_constants import get_hermes_home

        namespace = str(get_hermes_home().resolve())
    raw = str(task_id or "default")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "task"
    digest = hashlib.sha256(f"{namespace}\0{raw}".encode("utf-8")).hexdigest()[:10]
    suffix = f"-{digest}"
    prefix = "hermes-"
    slug_budget = _MAX_SPRITE_NAME_LENGTH - len(prefix) - len(suffix)
    slug = slug[:slug_budget].rstrip("-") or "task"
    return f"{prefix}{slug}{suffix}"


def iter_sprite_sync_files(
    container_base: str = f"{_DEFAULT_SPRITE_HOME}/.hermes",
) -> list[tuple[str, str]]:
    """Enumerate the narrow host file set allowed into a Sprite.

    Deliberately excludes ``get_credential_file_mounts``. The SDK token and all
    other host credentials remain on the host; only skills and non-secret cache
    files use the shared remote-filesystem synchronization contract.
    """
    from tools.credential_files import iter_cache_files, iter_skills_files

    files: list[tuple[str, str]] = []
    for entry in iter_skills_files(container_base=container_base):
        files.append((entry["host_path"], entry["container_path"]))
    for entry in iter_cache_files(container_base=container_base):
        files.append((entry["host_path"], entry["container_path"]))
    return files


class SpritesEnvironment(BaseEnvironment):
    """Execute commands in a persistent Fly Sprite via ``sprites-py`` 0.5.0."""

    _stdin_mode = "heredoc"

    def __init__(
        self,
        cwd: str = _DEFAULT_SPRITE_HOME,
        timeout: int = 60,
        cpu: int | float = 1,
        memory: int = 5120,
        disk: int = 51200,
        persistent_filesystem: bool = True,
        task_id: str = "default",
    ):
        from agent.secret_scope import get_secret

        token = (get_secret("SPRITE_TOKEN") or "").strip()
        if not token:
            raise ValueError(
                "SPRITE_TOKEN is required for the Sprites terminal backend. "
                "Store it in ~/.hermes/.env or the host process environment."
            )

        requested_cwd = cwd
        super().__init__(cwd=cwd, timeout=timeout)

        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("terminal.sprites", prompt=False)
        from sprites import NotFoundError, SpriteConfig, SpritesClient  # ty: ignore[unresolved-import]

        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._name = sprite_name_for_task(task_id)
        self._lock = threading.Lock()
        self._client = SpritesClient(
            token=token,
            base_url=(os.getenv("SPRITES_API_URL") or _DEFAULT_API_URL).strip().rstrip("/"),
        )
        self._sprite = None

        try:
            try:
                self._sprite = self._client.get_sprite(self._name)
                logger.info("Sprites: resumed %s for task %s", self._name, task_id)
            except NotFoundError:
                config = SpriteConfig(
                    cpus=max(1, int(math.ceil(float(cpu)))),
                    ram_mb=max(256, int(memory)),
                    storage_gb=max(1, int(math.ceil(int(disk) / 1024))),
                )
                self._sprite = self._client.create_sprite(
                    self._name,
                    config=config,
                    labels=["hermes-agent"],
                )
                logger.info("Sprites: created %s for task %s", self._name, task_id)
        except Exception:
            self._client.close()
            raise

        self._remote_home = self._detect_remote_home()
        if requested_cwd in {"~", "/root", _DEFAULT_SPRITE_HOME}:
            self.cwd = self._remote_home
        logger.info("Sprites: resolved home to %s, cwd to %s", self._remote_home, self.cwd)

        self._filesystem = self._sprite.filesystem("/")
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sprite_sync_files(f"{self._remote_home}/.hermes"),
            upload_fn=self._sprite_upload,
            delete_fn=self._sprite_delete,
            bulk_download_fn=self._sprite_bulk_download,
        )
        self._sync_manager.sync(force=True)
        self.init_session()

    @staticmethod
    def _decode_output(value: bytes | str | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    def _detect_remote_home(self) -> str:
        """Resolve HOME from the Sprite rather than assuming a root user."""
        try:
            sprite = self._sprite
            if sprite is None:
                return _DEFAULT_SPRITE_HOME
            result = sprite.run(
                "bash", "-c", 'printf %s "$HOME"',
                capture_output=True,
                timeout=min(self.timeout, 30),
            )
            home = self._decode_output(result.stdout).strip()
            if result.returncode == 0 and home.startswith("/"):
                return home.rstrip("/") or "/"
        except Exception as exc:
            logger.warning("Sprites: could not detect remote HOME: %s", exc)
        return _DEFAULT_SPRITE_HOME

    def _sprite_upload(self, host_path: str, remote_path: str) -> None:
        data = Path(host_path).read_bytes()
        self._filesystem.path(remote_path).write_bytes(
            data, mode=0o600, mkdir_parents=True
        )

    def _sprite_delete(self, remote_paths: list[str]) -> None:
        for remote_path in remote_paths:
            self._filesystem.path(remote_path).unlink(missing_ok=True)

    def _sprite_bulk_download(self, dest: Path) -> None:
        remote_tar = f"/tmp/.hermes-sync-{uuid.uuid4().hex}.tar"
        hermes_dir = f"{self._remote_home}/.hermes"
        archive_path = hermes_dir.lstrip("/")
        command = (
            f"if [ -d {shlex.quote(hermes_dir)} ]; then "
            f"tar cf {shlex.quote(remote_tar)} -C / {shlex.quote(archive_path)}; "
            "else tar cf "
            f"{shlex.quote(remote_tar)} --files-from /dev/null; fi"
        )
        sprite = self._sprite
        if sprite is None:
            raise RuntimeError("Sprite is unavailable (environment already cleaned up)")
        result = sprite.run(
            "bash", "-c", command, capture_output=True, timeout=self.timeout
        )
        if result.returncode != 0:
            error = self._decode_output(result.stderr or result.stdout)
            raise RuntimeError(f"Sprites filesystem archive failed: {error.strip()}")
        try:
            dest.write_bytes(self._filesystem.path(remote_tar).read_bytes())
        finally:
            self._filesystem.path(remote_tar).unlink(missing_ok=True)

    def _before_execute(self) -> None:
        self._sync_manager.sync()

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        sprite = self._sprite
        if sprite is None:
            raise RuntimeError("Sprite is unavailable (environment already cleaned up)")
        shell_args = ("bash", "-l", "-c", cmd_string) if login else (
            "bash", "-c", cmd_string
        )

        def exec_fn() -> tuple[str, int]:
            result = sprite.run(
                *shell_args,
                capture_output=True,
                timeout=timeout,
            )
            stdout = self._decode_output(result.stdout)
            stderr = self._decode_output(result.stderr)
            return stdout + stderr, int(result.returncode)

        return _ThreadedProcessHandle(exec_fn)

    def cleanup(self) -> None:
        with self._lock:
            if self._sprite is None:
                return
            try:
                self._sync_manager.sync_back()
            except Exception as exc:
                logger.warning("Sprites: sync_back failed for %s: %s", self._name, exc)
            try:
                if not self._persistent:
                    self._client.destroy_sprite(self._name)
                    logger.info("Sprites: destroyed ephemeral sprite %s", self._name)
            except Exception as exc:
                logger.warning("Sprites: cleanup failed for %s: %s", self._name, exc)
            finally:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._sprite = None
