"""Contract tests for the Fly Sprites terminal backend."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class FakeNotFoundError(Exception):
    pass


@dataclass
class FakeSpriteConfig:
    ram_mb: int | None = None
    cpus: int | None = None
    storage_gb: int | None = None


class FakePath:
    def __init__(self, store: dict[str, bytes], path: str):
        self.store = store
        self.path = path

    def write_bytes(self, data: bytes, mode: int = 0o644, mkdir_parents: bool = True):
        self.store[self.path] = data

    def read_bytes(self):
        return self.store[self.path]

    def unlink(self, missing_ok: bool = False):
        if missing_ok:
            self.store.pop(self.path, None)
        else:
            del self.store[self.path]


class FakeFilesystem:
    def __init__(self, store: dict[str, bytes]):
        self.store = store

    def path(self, path: str):
        return FakePath(self.store, path)


class FakeSprite:
    def __init__(self, name: str):
        self.name = name
        self.files: dict[str, bytes] = {}
        self.run = MagicMock(side_effect=self._run)

    @staticmethod
    def _run(*args, **kwargs):
        if args == ("bash", "-c", 'printf %s "$HOME"'):
            return SimpleNamespace(stdout=b"/home/sprite", stderr=b"", returncode=0)
        # Match sprites-py 0.5's CompletedProcess contract exactly.
        return SimpleNamespace(
            stdout=b"hello\n", stderr=b"warning\n", returncode=7
        )

    def filesystem(self, working_dir: str = "/"):
        return FakeFilesystem(self.files)


class FakeClient:
    existing: dict[str, FakeSprite] = {}
    instances: list["FakeClient"] = []

    def __init__(self, token: str, base_url: str = "https://api.sprites.dev", **kwargs):
        self.token = token
        self.base_url = base_url
        self.create_sprite = MagicMock(side_effect=self._create)
        self.destroy_sprite = MagicMock(side_effect=self.existing.pop)
        self.close = MagicMock()
        self.instances.append(self)

    def get_sprite(self, name: str):
        try:
            return self.existing[name]
        except KeyError as exc:
            raise FakeNotFoundError(name) from exc

    def _create(self, name: str, **kwargs):
        sprite = FakeSprite(name)
        self.existing[name] = sprite
        return sprite


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    FakeClient.existing = {}
    FakeClient.instances = []
    module = types.ModuleType("sprites")
    module.SpritesClient = FakeClient
    module.SpriteConfig = FakeSpriteConfig
    module.NotFoundError = FakeNotFoundError
    monkeypatch.setitem(sys.modules, "sprites", module)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.environments.base.BaseEnvironment.init_session", lambda self: None)
    monkeypatch.setattr("tools.environments.file_sync.FileSyncManager.sync", lambda self, **kw: None)
    monkeypatch.setattr("tools.environments.file_sync.FileSyncManager.sync_back", lambda self, **kw: None)


def test_task_name_is_deterministic_sanitized_and_bounded():
    from tools.environments.sprites import sprite_name_for_task

    first = sprite_name_for_task("Task / With ! Unsafe Ünicode")
    second = sprite_name_for_task("Task / With ! Unsafe Ünicode")

    assert first == second
    assert first.startswith("hermes-task-with-unsafe-nicode-")
    assert len(first) <= 63
    assert first.replace("-", "").isalnum()
    assert first == first.lower()
    assert sprite_name_for_task("same", namespace="profile-a") != sprite_name_for_task(
        "same", namespace="profile-b"
    )


def test_constructor_uses_host_credentials_and_resumes_existing_sprite(monkeypatch):
    from tools.environments.sprites import SpritesEnvironment, sprite_name_for_task

    task_id = "Session 123"
    name = sprite_name_for_task(task_id)
    existing = FakeSprite(name)
    FakeClient.existing[name] = existing
    monkeypatch.setenv("SPRITE_TOKEN", "host-secret")
    monkeypatch.setenv("SPRITES_API_URL", "https://sprites.internal.example/v1")

    env = SpritesEnvironment(
        cwd="/workspace", timeout=30, cpu=2, memory=4096, disk=20480,
        persistent_filesystem=True, task_id=task_id,
    )

    client = FakeClient.instances[-1]
    assert client.token == "host-secret"
    assert client.base_url == "https://sprites.internal.example/v1"
    assert env._sprite is existing
    client.create_sprite.assert_not_called()
    assert "SPRITE_TOKEN" not in existing.run.call_args_list


def test_constructor_creates_sprite_with_resource_config(monkeypatch):
    from tools.environments.sprites import SpritesEnvironment

    monkeypatch.setenv("SPRITE_TOKEN", "token")
    env = SpritesEnvironment(
        cwd="/workspace", timeout=30, cpu=2, memory=4096, disk=20480,
        persistent_filesystem=False, task_id="new-task",
    )

    kwargs = FakeClient.instances[-1].create_sprite.call_args.kwargs
    assert kwargs["config"] == FakeSpriteConfig(ram_mb=4096, cpus=2, storage_gb=20)
    assert env.cwd == "/workspace"


def test_constructor_detects_sprite_home_and_maps_root_default(monkeypatch):
    from tools.environments.sprites import SpritesEnvironment

    monkeypatch.setenv("SPRITE_TOKEN", "token")
    env = SpritesEnvironment(cwd="/root", task_id="home")

    assert env._remote_home == "/home/sprite"
    assert env.cwd == "/home/sprite"


def test_missing_token_fails_before_sdk_client_creation(monkeypatch):
    from tools.environments.sprites import SpritesEnvironment

    monkeypatch.delenv("SPRITE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SPRITE_TOKEN"):
        SpritesEnvironment(task_id="x")
    assert FakeClient.instances == []


def test_execute_returns_combined_command_output_and_exit_code(monkeypatch):
    from tools.environments.sprites import SpritesEnvironment

    monkeypatch.setenv("SPRITE_TOKEN", "token")
    env = SpritesEnvironment(cwd="/workspace", task_id="execute")

    result = env.execute("printf hello")

    assert "hello" in result["output"]
    assert "warning" in result["output"]
    assert result["returncode"] == 7
    args, kwargs = env._sprite.run.call_args
    assert args[0] == "bash"
    assert "-c" in args
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] > 0
    assert "SPRITE_TOKEN" not in (kwargs.get("env") or {})


def test_sdk_filesystem_callbacks_upload_and_delete(monkeypatch, tmp_path):
    from tools.environments.sprites import SpritesEnvironment

    monkeypatch.setenv("SPRITE_TOKEN", "token")
    env = SpritesEnvironment(task_id="files")
    source = tmp_path / "skill.md"
    source.write_bytes(b"content")

    env._sprite_upload(str(source), "/home/sprite/.hermes/skills/demo/SKILL.md")
    assert env._sprite.files["/home/sprite/.hermes/skills/demo/SKILL.md"] == b"content"

    env._sprite_delete(["/home/sprite/.hermes/skills/demo/SKILL.md"])
    assert "/home/sprite/.hermes/skills/demo/SKILL.md" not in env._sprite.files


def test_sync_sources_exclude_credential_mounts(monkeypatch):
    from tools.environments.sprites import iter_sprite_sync_files

    credential_mounts = MagicMock(side_effect=AssertionError("credentials must not be enumerated"))
    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", credential_mounts)
    monkeypatch.setattr(
        "tools.credential_files.iter_skills_files",
        lambda **kw: [{"host_path": "/host/skill", "container_path": "/home/sprite/.hermes/skills/s"}],
    )
    monkeypatch.setattr(
        "tools.credential_files.iter_cache_files",
        lambda **kw: [{"host_path": "/host/cache", "container_path": "/home/sprite/.hermes/cache/c"}],
    )

    assert iter_sprite_sync_files() == [
        ("/host/skill", "/home/sprite/.hermes/skills/s"),
        ("/host/cache", "/home/sprite/.hermes/cache/c"),
    ]
    credential_mounts.assert_not_called()


def test_cleanup_keeps_persistent_sprite_and_deletes_ephemeral(monkeypatch):
    from tools.environments.sprites import SpritesEnvironment

    monkeypatch.setenv("SPRITE_TOKEN", "token")
    persistent = SpritesEnvironment(task_id="keep", persistent_filesystem=True)
    keep_name = persistent._sprite.name
    persistent.cleanup()
    assert keep_name in FakeClient.existing
    assert FakeClient.instances[-1].close.called

    ephemeral = SpritesEnvironment(task_id="delete", persistent_filesystem=False)
    delete_name = ephemeral._sprite.name
    client = FakeClient.instances[-1]
    ephemeral.cleanup()
    client.destroy_sprite.assert_called_once_with(delete_name)
    assert delete_name not in FakeClient.existing


def test_terminal_factory_selects_sprites_backend(monkeypatch):
    from tools import terminal_tool

    sentinel = object()
    module = types.ModuleType("tools.environments.sprites")
    ctor = MagicMock(return_value=sentinel)
    module.SpritesEnvironment = ctor
    monkeypatch.setitem(sys.modules, "tools.environments.sprites", module)

    result = terminal_tool._create_environment(
        "sprites", "ignored", "/workspace", 45,
        container_config={
            "container_cpu": 2,
            "container_memory": 4096,
            "container_disk": 20480,
            "container_persistent": False,
        },
        task_id="abc",
    )

    assert result is sentinel
    ctor.assert_called_once_with(
        cwd="/workspace", timeout=45, cpu=2, memory=4096, disk=20480,
        persistent_filesystem=False, task_id="abc",
    )
