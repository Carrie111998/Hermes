"""Discovery parity for out-of-tree memory providers.

Upstream policy closed ``plugins/memory/`` to new providers, so every new
memory backend now lives outside this tree. These tests cover the two sources
that reach it — project-local directories and pip entry points — and the
integration points a directory install gets for free but a pip install
historically did not: the dashboard config panel, the provider's CLI
subcommands, and the ``memory.provider`` dropdown.
"""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap
from pathlib import Path

import pytest

import plugins.memory as memory_plugins

PROVIDER_SOURCE = """\
from agent.memory_provider import MemoryProvider


class Provider(MemoryProvider):
    @property
    def name(self):
        return "{name}"

    def is_available(self):
        return True

    def initialize(self, *a, **kw):
        pass

    def get_tool_schemas(self):
        return []


def register(ctx):
    ctx.register_memory_provider(Provider())
"""


class FakeEntryPoint:
    """Mirrors the importlib.metadata EntryPoint surface discovery uses."""

    group = "hermes_agent.memory_providers"

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def load(self):
        import importlib

        module_name, _, attr = self.value.partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, attr) if attr else module


class FakeEntryPoints(list):
    def select(self, *, group):
        return [ep for ep in self if ep.group == group]


@pytest.fixture
def entry_points(monkeypatch):
    """Install a replaceable entry-point set for the memory group."""
    registry = FakeEntryPoints()
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda: registry)
    return registry


def _write_provider_dir(root: Path, name: str) -> Path:
    provider = root / name
    provider.mkdir(parents=True)
    (provider / "__init__.py").write_text(PROVIDER_SOURCE.format(name=name), encoding="utf-8")
    return provider


# ---------------------------------------------------------------------------
# Project-local providers
# ---------------------------------------------------------------------------


def test_project_dir_is_ignored_without_opt_in(tmp_path, monkeypatch):
    """A repo you merely cd into must not be able to offer a memory backend."""
    _write_provider_dir(tmp_path / ".hermes" / "plugins", "projectmem")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)

    assert "projectmem" not in memory_plugins.list_memory_provider_names()
    assert memory_plugins.find_provider_dir("projectmem") is None


def test_project_dir_is_discovered_when_opted_in(tmp_path, monkeypatch):
    provider = _write_provider_dir(tmp_path / ".hermes" / "plugins", "projectmem")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")

    assert "projectmem" in memory_plugins.list_memory_provider_names()
    assert memory_plugins.find_provider_dir("projectmem") == provider


def test_bundled_still_wins_over_project(tmp_path, monkeypatch):
    """Precedence here is bundled-first, the reverse of the general
    PluginManager's later-wins order. A provider is activated by name, so a
    directory dropped into the working tree must not be able to shadow a
    shipped one and silently redirect the agent's memory."""
    _write_provider_dir(tmp_path / ".hermes" / "plugins", "honcho")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")

    resolved = memory_plugins.find_provider_dir("honcho")
    assert resolved == Path(memory_plugins.__file__).parent / "honcho"


# ---------------------------------------------------------------------------
# Pip entry-point providers
# ---------------------------------------------------------------------------


def test_entry_point_provider_is_listed(entry_points, tmp_path, monkeypatch):
    """list_memory_provider_names() fills the dashboard's memory.provider
    dropdown. Enumerating entry points reads distribution metadata without
    executing any of it, so this stays safe to call at import time."""
    entry_points.append(FakeEntryPoint("pipmem", "pipmem_pkg"))
    assert "pipmem" in memory_plugins.list_memory_provider_names()


def test_find_provider_dir_resolves_a_package_entry_point(entry_points, tmp_path, monkeypatch):
    """Without a directory, a pip-installed provider silently loses its
    dashboard config panel and its `hermes <provider>` subcommands — both are
    read from disk rather than imported."""
    package = tmp_path / "pipmem_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(PROVIDER_SOURCE.format(name="pipmem"), encoding="utf-8")
    (package / "config_schema.py").write_text("CONFIG_SCHEMA = None\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    entry_points.append(FakeEntryPoint("pipmem", "pipmem_pkg:register"))

    assert memory_plugins.find_provider_dir("pipmem") == package


def test_resolving_an_entry_point_does_not_import_it(entry_points, tmp_path, monkeypatch):
    """Discovery runs before the operator has chosen a provider. Importing
    every installed candidate would execute third-party code on the strength of
    a package merely being present."""
    package = tmp_path / "sideeffect_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(
        textwrap.dedent(
            """\
            import pathlib
            pathlib.Path(__file__).with_name("IMPORTED").write_text("x")
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    entry_points.append(FakeEntryPoint("sideeffect", "sideeffect_pkg"))

    assert memory_plugins.find_provider_dir("sideeffect") == package
    assert not (package / "IMPORTED").exists()
    assert "sideeffect_pkg" not in sys.modules


def test_bare_module_entry_point_has_no_directory(entry_points, tmp_path, monkeypatch):
    """A single-file provider has nowhere to put a sibling config_schema.py, so
    it resolves to None rather than handing back the whole site-packages root."""
    (tmp_path / "flatmem.py").write_text(PROVIDER_SOURCE.format(name="flatmem"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    entry_points.append(FakeEntryPoint("flatmem", "flatmem"))

    assert memory_plugins.find_provider_dir("flatmem") is None


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def test_a_secondary_registration_cannot_cost_the_provider(tmp_path, monkeypatch):
    """register_auxiliary_task used to raise AttributeError on the collector —
    which the loader caught, discarded the registered provider, and replaced
    with a bare second instance built by the subclass scan. A silent downgrade
    that looked like success."""
    plugins_root = tmp_path / "plugins"
    provider = _write_provider_dir(plugins_root, "auxmem")
    (provider / "__init__.py").write_text(
        PROVIDER_SOURCE.format(name="auxmem").replace(
            "    ctx.register_memory_provider(Provider())\n",
            "    instance = Provider()\n"
            "    instance.marked = True\n"
            "    ctx.register_memory_provider(instance)\n"
            "    ctx.register_auxiliary_task(\n"
            "        'auxmem_filter', display_name='Aux', description='d'\n"
            "    )\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    loaded = memory_plugins.load_memory_provider("auxmem")
    assert loaded is not None
    assert loaded.name == "auxmem"
    # The instance register() handed over, not a replacement.
    assert getattr(loaded, "marked", False)


def test_activation_is_not_gated_on_plugins_enabled(tmp_path, monkeypatch):
    """Memory providers are activated by naming them in memory.provider. Using
    a real PluginContext for secondary registrations must not start also
    requiring the plugin in plugins.enabled — that would break every existing
    user-installed provider."""
    _write_provider_dir(tmp_path / "plugins", "gatedmem")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert memory_plugins.load_memory_provider("gatedmem") is not None


def test_profile_clone_resolves_provider_through_canonical_config_loader(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "source"
    profile_dir = tmp_path / "profile"
    source_dir.mkdir()
    profile_dir.mkdir()
    (source_dir / "config.yaml").write_text(
        "memory:\n  provider: ${CLONE_MEMORY_PROVIDER}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLONE_MEMORY_PROVIDER", "custommem")
    monkeypatch.setattr(memory_plugins, "_iter_provider_dirs", lambda: [])

    calls = []

    class Provider:
        def clone_profile(self, profile_name, **kwargs):
            calls.append((profile_name, kwargs))
            return "cloned"

    monkeypatch.setattr(
        memory_plugins,
        "load_memory_provider",
        lambda name, **kwargs: Provider() if name == "custommem" else None,
    )

    from hermes_constants import get_hermes_home

    ambient_home = get_hermes_home()
    result = memory_plugins.clone_memory_provider_profile(
        "coder",
        source_dir=source_dir,
        profile_dir=profile_dir,
    )

    assert result == ["cloned"]
    assert calls == [(
        "coder",
        {
            "source_dir": source_dir,
            "profile_dir": profile_dir,
            "clone_all": False,
        },
    )]
    assert get_hermes_home() == ambient_home


def test_profile_clone_runs_provider_declared_by_manifest(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    profile_dir = tmp_path / "profile"
    provider_dir = tmp_path / "legacy-memory"
    source_dir.mkdir()
    profile_dir.mkdir()
    provider_dir.mkdir()
    (provider_dir / "plugin.yaml").write_text(
        "name: legacy-memory\nprofile_clone: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        memory_plugins,
        "_iter_provider_dirs",
        lambda: [("legacy-memory", provider_dir)],
    )

    calls = []

    class Provider:
        def clone_profile(self, profile_name, **kwargs):
            calls.append((profile_name, kwargs))
            return "legacy-cloned"

    monkeypatch.setattr(
        memory_plugins,
        "load_memory_provider",
        lambda name, **kwargs: Provider() if name == "legacy-memory" else None,
    )

    result = memory_plugins.clone_memory_provider_profile(
        "coder",
        source_dir=source_dir,
        profile_dir=profile_dir,
        clone_all=True,
    )

    assert result == ["legacy-cloned"]
    assert calls == [(
        "coder",
        {
            "source_dir": source_dir,
            "profile_dir": profile_dir,
            "clone_all": True,
        },
    )]


def test_profile_clone_loads_provider_from_explicit_source_profile(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "source"
    profile_dir = tmp_path / "profile"
    source_dir.mkdir()
    profile_dir.mkdir()
    (source_dir / "config.yaml").write_text(
        "memory:\n  provider: sourceclone\n",
        encoding="utf-8",
    )
    provider_dir = source_dir / "plugins" / "sourceclone"
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text(
        PROVIDER_SOURCE.format(name="sourceclone").replace(
            "    def get_tool_schemas(self):\n        return []\n",
            "    def get_tool_schemas(self):\n"
            "        return []\n\n"
            "    def clone_profile(self, profile_name, **kwargs):\n"
            "        (kwargs['profile_dir'] / 'sourceclone.txt').write_text(profile_name)\n"
            "        return 'source-cloned'\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_plugins, "_MEMORY_PLUGINS_DIR", tmp_path / "bundled")

    from hermes_constants import get_hermes_home

    ambient_home = get_hermes_home()
    result = memory_plugins.clone_memory_provider_profile(
        "coder",
        source_dir=source_dir,
        profile_dir=profile_dir,
    )

    assert result == ["source-cloned"]
    assert (profile_dir / "sourceclone.txt").read_text() == "coder"
    assert get_hermes_home() == ambient_home


def test_profile_clone_isolates_bad_manifests_and_failing_hooks(
    tmp_path, monkeypatch, caplog
):
    source_dir = tmp_path / "source"
    profile_dir = tmp_path / "profile"
    source_dir.mkdir()
    profile_dir.mkdir()

    provider_dirs = []
    for name, manifest in (
        ("malformed", "- not-a-mapping\n"),
        ("failing", "profile_clone: true\n"),
        ("working", "profile_clone: true\n"),
    ):
        provider_dir = tmp_path / name
        provider_dir.mkdir()
        (provider_dir / "plugin.yaml").write_text(manifest, encoding="utf-8")
        provider_dirs.append((name, provider_dir))
    monkeypatch.setattr(memory_plugins, "_iter_provider_dirs", lambda: provider_dirs)

    class FailingProvider:
        def clone_profile(self, profile_name, **kwargs):
            raise RuntimeError("broken clone hook")

    class WorkingProvider:
        def clone_profile(self, profile_name, **kwargs):
            return "working-cloned"

    def load_provider(name, **kwargs):
        if name == "failing":
            return FailingProvider()
        if name == "working":
            return WorkingProvider()
        return None

    monkeypatch.setattr(memory_plugins, "load_memory_provider", load_provider)

    with caplog.at_level("DEBUG", logger="plugins.memory"):
        result = memory_plugins.clone_memory_provider_profile(
            "coder",
            source_dir=source_dir,
            profile_dir=profile_dir,
        )

    assert result == ["working-cloned"]
    assert "profile-clone manifest for memory provider 'malformed'" in caplog.text
    assert "Memory provider 'failing' failed to clone profile 'coder'" in caplog.text


def test_profile_clone_reloads_same_named_provider_from_source_profile(
    tmp_path, monkeypatch
):
    provider_name = "sameprofilemem"
    marker_source = """\
from agent.memory_provider import MemoryProvider


class Provider(MemoryProvider):
    @property
    def name(self):
        return "sameprofilemem"

    def is_available(self):
        return True

    def initialize(self, *args, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def clone_profile(self, profile_name, **kwargs):
        (kwargs["profile_dir"] / "provider-origin.txt").write_text(ORIGIN)


def register(ctx):
    ctx.register_memory_provider(Provider())
"""
    ambient_dir = tmp_path / "ambient" / "plugins" / provider_name
    source_dir = tmp_path / "source"
    source_provider_dir = source_dir / "plugins" / provider_name
    profile_dir = tmp_path / "profile"
    ambient_dir.mkdir(parents=True)
    source_provider_dir.mkdir(parents=True)
    profile_dir.mkdir()
    (ambient_dir / "__init__.py").write_text(
        f'ORIGIN = "ambient"\n{marker_source}',
        encoding="utf-8",
    )
    (source_provider_dir / "__init__.py").write_text(
        f'ORIGIN = "source"\n{marker_source}',
        encoding="utf-8",
    )
    (source_dir / "config.yaml").write_text(
        f"memory:\n  provider: {provider_name}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_plugins, "_MEMORY_PLUGINS_DIR", tmp_path / "bundled")

    ambient_provider = memory_plugins._load_provider_from_dir(ambient_dir)
    assert ambient_provider is not None

    memory_plugins.clone_memory_provider_profile(
        "coder",
        source_dir=source_dir,
        profile_dir=profile_dir,
    )

    assert (profile_dir / "provider-origin.txt").read_text() == "source"
