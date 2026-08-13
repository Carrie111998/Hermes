"""Regression tests for shared entry-point provider classification.

The ``hermes_agent.plugins`` entry-point group has two consumers that must
agree on ownership: the general PluginManager (hermes_cli.plugins) and the
model-provider registry (providers). Both use the SAME classifier
(hermes_cli.entrypoint_kind) so they can never drift:

* a model provider (register_provider + ProviderProfile) → model-provider,
  owned by the provider registry;
* a memory provider (register_memory_provider / MemoryProvider) → exclusive,
  owned by plugins/memory discovery;
* a general plugin → standalone, owned by the PluginManager.

Without classification, an enabled pip provider is double-loaded: the registry
registers it correctly but the general manager also imports it as a standalone
plugin and flags it errored (pr-85504 defect). Symmetrically, a general plugin
with a zero-arg register() would be wrongly invoked by the provider registry
as a model provider (the inverse bug this module also fixes).
"""

import types

from hermes_cli import entrypoint_kind as ek
from hermes_cli.plugins import PluginManager


class _FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name, value):
        self.name = name
        self.value = value


def _write_module(tmp_path, name, source):
    mod = tmp_path / f"{name}.py"
    mod.write_text(source)
    return mod


def _stub_find_spec(monkeypatch, mapping, packages=None):
    """Point importlib.util.find_spec at fixture files for given module names.

    ``mapping`` maps top-level names to a module file. ``packages`` maps a
    name to a package directory (for the submodule-scan / re-export tests).
    """
    real_find_spec = ek.importlib.util.find_spec

    def fake_find_spec(name):
        if name in mapping:
            return types.SimpleNamespace(origin=str(mapping[name]), name=name)
        if packages and name in packages:
            pkgdir = packages[name]
            return types.SimpleNamespace(
                origin=str(pkgdir / "__init__.py"),
                submodule_search_locations=[str(pkgdir)],
            )
        return real_find_spec(name)

    monkeypatch.setattr(ek.importlib.util, "find_spec", fake_find_spec)


MODEL_SOURCE = (
    "from providers import register_provider, ProviderProfile\n"
    "def _register():\n"
    "    register_provider(ProviderProfile(name='pip', type='model'))\n"
    "_register()\n"
)
MEMORY_SOURCE = (
    "def register_memory_provider(name, cls):\n"
    "    pass\n"
)
GENERAL_SOURCE = (
    "def register(ctx):\n"
    "    ctx.add_tool(name='x', fn=lambda: 1)\n"
)


# ── shared classifier unit tests ─────────────────────────────────────────

def test_classifier_model_provider(monkeypatch, tmp_path):
    mod = _write_module(tmp_path, "pip_provider", MODEL_SOURCE)
    _stub_find_spec(monkeypatch, {"pip_provider": mod})
    assert ek.classify_entrypoint(_FakeEntryPoint("p", "pip_provider:register")) == "model-provider"
    assert ek.kind_is_provider("model-provider") is True


def test_classifier_memory_provider(monkeypatch, tmp_path):
    mod = _write_module(tmp_path, "pip_memory", MEMORY_SOURCE)
    _stub_find_spec(monkeypatch, {"pip_memory": mod})
    assert ek.classify_entrypoint(_FakeEntryPoint("m", "pip_memory:register")) == "exclusive"
    assert ek.kind_is_memory_provider("exclusive") is True


def test_classifier_general_plugin(monkeypatch, tmp_path):
    mod = _write_module(tmp_path, "pip_general", GENERAL_SOURCE)
    _stub_find_spec(monkeypatch, {"pip_general": mod})
    assert ek.classify_entrypoint(_FakeEntryPoint("g", "pip_general:register")) == "standalone"


def test_classifier_unresolvable_is_unknown(monkeypatch):
    def boom(name):
        raise ImportError("nope")
    monkeypatch.setattr(ek.importlib.util, "find_spec", boom)
    # A module:func target whose source can't be read → unknown (not standalone)
    assert ek.classify_entrypoint(_FakeEntryPoint("x", "missing_mod:register")) == "unknown"


def test_classifier_non_string_value_is_unknown():
    # An entry point whose value is a bare callable (not module:func) → unknown
    assert ek.classify_entrypoint(_FakeEntryPoint("c", object())) == "unknown"


def test_classifier_thin_package_reexport_is_model_provider(monkeypatch, tmp_path):
    """A package whose __init__ re-exports register from a submodule is a provider.

    Regression for the triage finding: a thin ``__init__.py`` containing only
    ``from .core import register`` has no provider markers itself, but the
    ``register_provider(ProviderProfile(...))`` call lives in ``core.py``. It
    must classify as model-provider (not standalone, which would silently
    deregister a provider of the documented shape on main).
    """
    pkgdir = tmp_path / "thinpkg"
    pkgdir.mkdir()
    (pkgdir / "__init__.py").write_text("from .core import register\n")
    (pkgdir / "core.py").write_text(
        "from providers import register_provider, ProviderProfile\n"
        "def register():\n"
        "    register_provider(ProviderProfile(name='x'))\n"
    )
    _stub_find_spec(monkeypatch, {}, packages={"thinpkg": pkgdir})

    assert ek.classify_entrypoint(_FakeEntryPoint("t", "thinpkg:register")) == "model-provider"


# ── general manager side ────────────────────────────────────────────────

def test_manager_entrypoint_model_provider_carried_into_manifest(monkeypatch, tmp_path):
    """The manager tags a pip model-provider entry point as model-provider."""
    mod = _write_module(tmp_path, "pip_provider", MODEL_SOURCE)
    _stub_find_spec(monkeypatch, {"pip_provider": mod})

    def fake_entry_points():
        return types.SimpleNamespace(
            select=lambda group: [_FakeEntryPoint("pip_provider", "pip_provider:register")]
        )
    monkeypatch.setattr(ek.importlib.metadata, "entry_points", fake_entry_points)
    monkeypatch.setattr(PluginManager, "_collect_directory_manifests",
                        lambda self: [])

    mgr = PluginManager()
    manifests = mgr._scan_entry_points()
    assert len(manifests) == 1
    assert manifests[0].kind == "model-provider"


# ── provider registry side (the inverse bug) ────────────────────────────

def test_provider_registry_skips_general_zero_arg_plugin(monkeypatch, tmp_path):
    """A general plugin with a zero-arg register is NOT invoked as a provider."""
    mod = _write_module(tmp_path, "pip_general", GENERAL_SOURCE)
    _stub_find_spec(monkeypatch, {"pip_general": mod})

    invoked = []

    class FakeEP(_FakeEntryPoint):
        def load(self):
            invoked.append(self.name)
            return lambda: None  # zero-arg callable would have been invoked pre-fix

    def fake_entry_points():
        return types.SimpleNamespace(
            select=lambda group: [FakeEP("pip_general", "pip_general:register")]
        )

    import importlib.metadata as _md
    import hermes_cli.plugins as plugins_mod
    import providers
    monkeypatch.setattr(_md, "entry_points", fake_entry_points)
    monkeypatch.setattr(plugins_mod, "_get_enabled_plugins",
                        lambda: {"pip_general"})

    providers._discover_entry_point_providers()
    assert invoked == []  # general plugin must not be invoked by the registry


def test_provider_registry_loads_real_model_provider(monkeypatch, tmp_path):
    """A real model-provider entry point IS still invoked (no regression)."""
    mod = _write_module(tmp_path, "pip_provider", MODEL_SOURCE)
    _stub_find_spec(monkeypatch, {"pip_provider": mod})

    invoked = []

    class FakeEP(_FakeEntryPoint):
        def load(self):
            invoked.append(self.name)
            return lambda: None

    def fake_entry_points():
        return types.SimpleNamespace(
            select=lambda group: [FakeEP("pip_provider", "pip_provider:register")]
        )

    import importlib.metadata as _md
    import hermes_cli.plugins as plugins_mod
    import providers
    monkeypatch.setattr(_md, "entry_points", fake_entry_points)
    monkeypatch.setattr(plugins_mod, "_get_enabled_plugins",
                        lambda: {"pip_provider"})

    providers._discover_entry_point_providers()
    assert invoked == ["pip_provider"]  # provider still loaded
