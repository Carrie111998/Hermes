"""Seam tests for the server.py -> methods_projects.py extraction (slice R5-S1).

Verifies the identity-preserving seam contract (method_ctx.py): every name
the moved handlers/helpers resolve through either namespace must be the SAME
object in server.py and methods_projects.py, and the moved block must be
byte-identical to the consensus golden window (server.py:11171-11322, sha
5f62871aee30af48...).

The #1 trap of this extraction is a name resolved through the wrong
namespace at runtime (NameError inside a handler); the aggressive cases at
the bottom drive the real registered handlers and helpers end-to-end.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import tui_gateway.methods_projects as mp
import tui_gateway.server as server

# Names moved into methods_projects.py that server.py's trailer re-exports
# (same objects) — consumed either by sibling methods_* handlers
# (methods_config.py) or by the moved handlers themselves after install()
# rebinds them onto server globals.
REEXPORTED = [
    "_E_NO_PROJECT",
    "_E_PROJECT_ARG",
    "_E_PROJECTS",
    "_NoProject",
    "_build_project_tree",
    "_discover_repos_payload",
    "_projects_payload",
    "_repo_discovery_policy",
    "_repo_discovery_policy_is_default",
    "_repo_discovery_policy_key",
    "_require_project",
]

# Names the moved code resolves through methods_projects.py's module
# namespace (inner handler bodies and plain helpers keep their home-module
# globals; install() only rebinds the outer @method wrappers). `logger` is
# the same named-logger singleton server.py binds (identity holds); the
# four function names (`_load_cfg`, `_ok`, `_completion_cwd`,
# `_git_branch_for_cwd`) are call-time delegates into server.py's live
# module state, so monkeypatches of server.<name> stay visible (no frozen
# copy at import, the stale-copy trap).
LOGGER_IDENTITY = "logger"

# Aliases methods_projects.py binds from git_probe — must be the same objects
# as server.py's bindings of the same attributes.
GIT_PROBE_ALIASED = ["_git_common_repo_root_for_cwd", "_resolve_cwd_git"]

_C1_HANDLERS = {
    "projects.list",
    "projects.get",
    "projects.create",
    "projects.update",
    "projects.add_folder",
    "projects.remove_folder",
    "projects.set_primary",
    "projects.archive",
    "projects.delete",
    "projects.set_active",
    "projects.for_cwd",
}

# projects.* handlers hosted in methods_config.py — their resolution of the
# re-exported C2 names is exactly what the seam must keep working.
_CFG_HANDLERS = {
    "projects.discover_repos",
    "projects.project_sessions",
    "projects.record_repos",
    "projects.tree",
}


def test_reexports_are_identical_objects():
    for name in REEXPORTED:
        assert getattr(server, name) is getattr(mp, name), name


def test_logger_is_the_same_singleton():
    assert getattr(mp, LOGGER_IDENTITY) is getattr(server, LOGGER_IDENTITY)


def test_delegated_names_forward_to_server_bindings():
    """Wrapper delegates resolve to the live server bindings at call time."""
    assert mp._ok(7, {"ping": True}) == server._ok(7, {"ping": True})
    assert mp._completion_cwd({}) == server._completion_cwd({})
    assert mp._git_branch_for_cwd("") == server._git_branch_for_cwd("")


def test_server_monkeypatches_are_visible_through_the_module(monkeypatch):
    """The stale-copy trap: moved helpers must not freeze server bindings.

    test_projects_rpc.py monkeypatches ``server._load_cfg`` to steer the
    discovery policy; the moved ``_repo_discovery_policy`` resolves it
    through methods_projects.py, which must see the patched binding.
    """
    sentinel = {
        "desktop": {
            "repo_scan_enabled": False,
            "repo_scan_roots": [],
            "repo_scan_exclude_paths": [],
        }
    }
    monkeypatch.setattr(server, "_load_cfg", lambda: sentinel)
    assert mp._load_cfg() is sentinel
    assert mp._repo_discovery_policy()["enabled"] is False


def test_git_probe_aliases_are_identical_objects():
    for name in GIT_PROBE_ALIASED:
        assert getattr(mp, name) is getattr(server, name), name


def test_moved_block_is_byte_identical_to_golden_window():
    """server.py:11171-11322 must reproduce the consensus sha byte-for-byte."""
    src = Path(mp.__file__).read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(src) if line.startswith("_E_PROJECTS = 5061"))
    block = "".join(src[start : start + 152])
    digest = hashlib.sha256(block.encode()).hexdigest()
    assert digest == "5f62871aee30af486ba7f209768c0b916f8240bcc4442382f8cdcad78d398e01"


def test_all_projects_methods_registered():
    keys = set(server._methods)
    assert _C1_HANDLERS <= keys
    assert _CFG_HANDLERS <= keys


def test_for_cwd_still_name_routed_to_long_pool():
    # Pool membership is by method name, not module — unchanged by the move.
    assert "projects.for_cwd" in server._LONG_HANDLERS


@pytest.fixture
def _fast_git_probe(monkeypatch):
    """Cheap .git-directory probe so tree builds don't spawn real git."""
    from tui_gateway import git_probe

    git_probe.invalidate()
    monkeypatch.setattr(git_probe, "run_git", lambda cwd, *_a: "")
    yield
    git_probe.invalidate()


def test_crud_validation_error_path_resolves_via_seam(_fast_git_probe):
    """projects.get with an unresolvable id must surface 5062 (not NameError).

    Exercises the whole rebind chain: registered handler -> server-global
    ``_ok``/``_err`` -> re-exported ``_NoProject`` raise (module namespace) ->
    re-exported ``_E_NO_PROJECT`` catch (server namespace).
    """
    resp = server._methods["projects.get"](1, {"id": "no-such-project-r5s1"})
    assert "error" in resp
    assert resp["error"]["code"] == 5062
    resp2 = server._methods["projects.set_primary"](1, {"id": "no-such-project-r5s1"})
    assert resp2["error"]["code"] == 5062


def test_for_cwd_uses_bridged_completion_and_git_branch_names():
    """projects.for_cwd resolves _completion_cwd/_git_branch_for_cwd bridges."""
    resp = server._methods["projects.for_cwd"](1, {})
    assert "error" not in resp
    result = resp["result"]
    assert "project" in result and "cwd" in result and "branch" in result


def test_build_project_tree_traverses_via_module_namespace(_fast_git_probe):
    """The full C2 helper chain runs with bridges and re-exports in place.

    Drives the moved ``_build_project_tree`` directly: module-local
    ``_project_tree_inputs`` -> bridged ``_load_cfg`` policy path ->
    ``project_tree.build_tree`` (function-local import).
    """

    class _FakeDB:
        def list_sessions_rich(self, **kwargs):
            return []

    tree, active_id = mp._build_project_tree(
        _FakeDB(),
        preview_limit=10,
        hydrate=False,
        session_limit=5,
        include_discovered=False,
    )
    assert isinstance(tree, dict)
    assert "projects" in tree
    assert active_id is None or isinstance(active_id, str)
