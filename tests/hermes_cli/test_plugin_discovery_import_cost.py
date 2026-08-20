"""Regression: loading a bundled backend plugin must not import an HTTP client.

Why this exists
---------------
Bundled ``kind: backend`` plugins -- the browser, web-search, image-gen,
video-gen, dashboard-auth and spotify backends under ``plugins/`` -- are loaded
EAGERLY by ``PluginManager._discover_and_load_inner``. Bundled *platform*
plugins are deferred (``_register_deferred_platform``, because their SDKs are
unavoidably huge); backends are not, because they register tools at load time.

That is fine as long as loading one is cheap. It was not. Nine of them carried
``import requests`` or ``import httpx`` at module scope, and
``discover_plugins()`` is called by ``gateway.config.load_gateway_config()``,
which every ``hermes send`` to a real platform calls. So a Telegram message
paid for the HTTP stacks of a cloud browser, an OAuth dashboard and a Spotify
client. Measured on PRECISION 2026-08-19:

    discover_plugins() imported 362 modules; `send --to <platform>` cost 590

``import httpx`` is worth 256 modules by itself on this box: httpx 0.28.1's
``__init__`` does ``from ._main import main``, which drags in click, rich,
pygments and attrs.

The fix is per-plugin, not a change to the plugin manager: each of the nine
imports its client inside the methods that call it, and exposes the module
attribute lazily through a PEP 562 ``__getattr__`` so callers -- and the ~91
tests that patch ``"plugins.<x>.requests.post"`` -- resolve exactly the object
they always did. After: 238 modules and 516.

BEWARE THE ATTRIBUTION TRAP when re-measuring. Counting ``sys.modules`` growth
across one plugin's load charges every shared module to whichever plugin loaded
first. It named ``dashboard_auth/nous`` and ``browser/browser_use`` as the two
costs worth 291 of 359 modules; fixing exactly those two moved the identical
cost onto ``dashboard_auth/self_hosted`` and ``browser/browserbase`` and saved
nothing. Import each candidate ALONE in a fresh interpreter instead.

The AST scan below is the durable half: it fails for a NEW backend plugin that
re-introduces a module-scope client import, which no measurement of today's
tree can do.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "plugins"

# Clients heavy enough that a plugin must not import one just to be loadable.
# ``requests`` costs 177 modules on this box and ``httpx`` 256; ``aiohttp`` is
# listed pre-emptively because it is the third client used in this tree.
FORBIDDEN_CLIENTS = frozenset({"requests", "httpx", "aiohttp", "urllib3"})

# The nine that were fixed, as (import path, client). Kept explicit so the
# per-module test names the real thing rather than whatever the scan discovers.
FIXED_PLUGIN_MODULES = [
    ("plugins.browser.browser_use.provider", "requests"),
    ("plugins.browser.browserbase.provider", "requests"),
    ("plugins.browser.firecrawl.provider", "requests"),
    ("plugins.image_gen.krea", "requests"),
    ("plugins.image_gen.xai", "requests"),
    ("plugins.video_gen.xai", "httpx"),
    ("plugins.dashboard_auth.nous", "httpx"),
    ("plugins.dashboard_auth.self_hosted", "httpx"),
    ("plugins.spotify.client", "httpx"),
]

# Plugins whose own imports are clean but that still end up with the client in
# ``sys.modules`` because of a dependency OUTSIDE plugins/. Tracked as a
# TRIPWIRE, not a skip: the test below asserts the client IS still present, so
# when the upstream import is fixed this file goes red and tells the next
# person to move the entry into the clean set.
#
# ``plugins.spotify.client`` imports ``hermes_cli.auth`` for its OAuth token
# handling, and ``hermes_cli/auth.py`` line 44 is a module-scope ``import
# httpx``. Same defect class, different owner: 23 call sites across an
# 8,500-line credential module, and 46 tests patch ``hermes_cli.auth.httpx`` --
# several with ``patch("hermes_cli.auth.httpx")``, which replaces the whole
# module attribute and therefore cannot be satisfied by a function-local
# import the way the plugin fixes were. Deferring it needs a lazy module proxy
# in a credential path, which is a deliberately separate decision.
TRANSITIVE_CLIENT_VIA_HERMES_CLI_AUTH = {
    "plugins.spotify.client": "hermes_cli.auth",
}


def _child_env() -> dict:
    env = dict(os.environ)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + existing if existing else str(REPO_ROOT)
    )
    return env


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=900,
    )


def _bundled_backend_dirs() -> list[Path]:
    """Every bundled plugin directory whose manifest declares ``kind: backend``.

    Parsed with a line scan rather than PyYAML so the test has no dependency of
    its own, and so a malformed sibling manifest cannot make it silently cover
    nothing.
    """
    found = []
    for manifest in PLUGINS_DIR.rglob("plugin.yaml"):
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip().startswith("kind:"):
                if line.split(":", 1)[1].strip().strip("\"'") == "backend":
                    found.append(manifest.parent)
                break
    return sorted(found)


def _module_scope_client_imports(py_file: Path) -> list[str]:
    """Forbidden clients imported at MODULE SCOPE in *py_file*.

    Only the module's top-level body is inspected, so a lazy import inside a
    function -- or inside ``if TYPE_CHECKING:`` -- is correctly ignored. That
    distinction is the whole point of the fix, so the scan must honour it.
    """
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    hits = []
    for node in tree.body:  # top level only -- deliberately not ast.walk
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_CLIENTS:
                    hits.append(f"{py_file.name}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] in FORBIDDEN_CLIENTS:
                hits.append(
                    f"{py_file.name}:{node.lineno} from {node.module} import ..."
                )
    return hits


def test_the_scan_actually_finds_backend_plugins():
    """Guard the guard: an empty sweep must not read as a clean sweep."""
    dirs = _bundled_backend_dirs()
    assert len(dirs) >= 20, (
        f"only {len(dirs)} bundled backend plugin(s) found under {PLUGINS_DIR}. "
        "There were 26 on 2026-08-19. Either the manifest layout changed or "
        "this scan is looking in the wrong place -- in which case the test "
        "below is vacuous."
    )


def test_no_bundled_backend_imports_an_http_client_at_module_scope():
    """A backend plugin must be loadable without paying for an HTTP stack."""
    offenders = {}
    for plugin_dir in _bundled_backend_dirs():
        for py_file in sorted(plugin_dir.rglob("*.py")):
            if "__pycache__" in py_file.parts:
                continue
            hits = _module_scope_client_imports(py_file)
            if hits:
                offenders[str(py_file.relative_to(REPO_ROOT))] = hits

    assert not offenders, (
        "Bundled backend plugins import an HTTP client at module scope:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(offenders.items()))
        + "\n\nThese plugins are loaded EAGERLY by discover_plugins(), which "
        "load_gateway_config() calls, which every `hermes send` calls. Import "
        "the client inside the methods that use it and expose it through a "
        "module-level __getattr__ (see plugins/spotify/client.py) so callers "
        "and tests that patch through the module path keep working."
    )


@pytest.mark.parametrize(
    "module,client",
    FIXED_PLUGIN_MODULES,
    ids=[m for m, _ in FIXED_PLUGIN_MODULES],
)
def test_plugin_module_imports_without_its_http_client(module, client):
    """Importing the plugin must not import the client.

    Subprocess, not in-process: ``sys.modules`` is process-global, so a sibling
    test that legitimately imported httpx would mask the regression entirely.
    """
    proc = _run(
        "import sys\n"
        f"import {module}\n"
        f"print('LOADED' if {client!r} in sys.modules else 'CLEAN')\n"
    )
    assert proc.returncode == 0, (
        f"importing {module} failed.\nstdout:\n{proc.stdout}\n"
        f"stderr tail:\n{proc.stderr[-3000:]}"
    )

    upstream = TRANSITIVE_CLIENT_VIA_HERMES_CLI_AUTH.get(module)
    if upstream:
        assert "LOADED" in proc.stdout, (
            f"{module} no longer pulls {client} transitively -- {upstream} "
            f"must have stopped importing it at module scope. Good news: "
            f"delete this entry from TRANSITIVE_CLIENT_VIA_HERMES_CLI_AUTH so "
            f"the real assertion below starts guarding {module}, and consider "
            "tightening test_plugin_discovery_does_not_import_requests to "
            "forbid httpx too."
        )
        return

    assert "CLEAN" in proc.stdout, (
        f"importing {module} pulled in {client}. Move the import inside the "
        f"methods that call it.\nstdout:\n{proc.stdout}"
    )


@pytest.mark.parametrize(
    "module,client",
    FIXED_PLUGIN_MODULES,
    ids=[m for m, _ in FIXED_PLUGIN_MODULES],
)
def test_lazy_client_attribute_still_resolves(module, client):
    """``<plugin module>.<client>`` must still be the real client module.

    The deferral would otherwise be a breaking change: mock.patch targets like
    ``"plugins.image_gen.krea.requests.post"`` resolve the attribute off the
    module, and ~91 tests in tests/plugins do exactly that. The PEP 562
    ``__getattr__`` must hand back the same object ``import <client>`` gives.
    """
    proc = _run(
        f"import {module} as m\n"
        f"import {client}\n"
        f"assert m.{client} is {client}, 'not the same module object'\n"
        "print('SAME')\n"
    )
    assert proc.returncode == 0 and "SAME" in proc.stdout, (
        f"{module}.{client} no longer resolves to the {client} module.\n"
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )


@pytest.mark.timeout(900)
def test_plugin_discovery_does_not_import_requests():
    """``discover_plugins()`` must not pull the requests stack.

    Only ``requests`` and ``urllib3`` are asserted here, not ``httpx``:
    ``load_gateway_config()`` also imports ``hermes_cli.auth``, which still
    carries a module-scope ``import httpx`` of its own. That is a separate
    (larger) defect on the same path -- 23 call sites in an 8,500-line module --
    and forbidding httpx here would assert something this change did not fix.
    The per-module tests above cover the httpx half of the plugin side.
    """
    proc = _run(
        "import sys\n"
        "from hermes_cli.plugins import discover_plugins\n"
        "discover_plugins()\n"
        "bad = sorted(m for m in ('requests', 'urllib3') if m in sys.modules)\n"
        "print('OFFENDERS=[' + ','.join(bad) + ']')\n"
    )
    assert proc.returncode == 0, (
        f"discover_plugins() failed.\nstdout:\n{proc.stdout}\n"
        f"stderr tail:\n{proc.stderr[-3000:]}"
    )
    assert "OFFENDERS=[]" in proc.stdout, (
        f"discover_plugins() imported the requests stack: {proc.stdout.strip()}\n"
        "A bundled backend plugin is importing it at module scope again."
    )
