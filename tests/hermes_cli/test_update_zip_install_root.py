"""Regression: the ZIP update path must target the managed install (#59850).

``PROJECT_ROOT`` is derived from the running ``hermes_cli`` package. When the
``hermes`` entry point on PATH belongs to a different environment than the
managed install — a conda/miniforge shim in front of an agent installed under
``%LOCALAPPDATA%\\hermes\\hermes-agent`` — the ZIP update copied the new tree
into the shim's site-packages and then ran ``uv pip install -e .`` there, in a
directory with no ``pyproject.toml``. uv exits 2 and the install is left half
updated on every run.
"""

import zipfile
from pathlib import Path
from unittest.mock import patch

from hermes_cli import main as hermes_main


def _make_checkout(root: Path) -> Path:
    """Create the minimal file set that marks a hermes-agent checkout."""
    (root / "hermes_cli").mkdir(parents=True, exist_ok=True)
    (root / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\n', encoding="utf-8"
    )
    return root


def _build_update_zip(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("hermes-agent-main/README.md", "ok\n")


def test_looks_like_hermes_checkout_needs_both_markers(tmp_path):
    checkout = _make_checkout(tmp_path / "install")
    assert hermes_main._looks_like_hermes_checkout(checkout)

    # A site-packages directory holding an installed hermes_cli has the package
    # but no pyproject.toml — exactly the directory `pip install -e .` chokes on.
    site_packages = tmp_path / "site-packages"
    (site_packages / "hermes_cli").mkdir(parents=True)
    (site_packages / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    assert not hermes_main._looks_like_hermes_checkout(site_packages)
    assert not hermes_main._looks_like_hermes_checkout(tmp_path / "missing")


def test_resolve_install_root_prefers_the_running_checkout(tmp_path, monkeypatch):
    """When both roots agree, resolution is a no-op — no behavior change."""
    running = _make_checkout(tmp_path / "running")
    managed = _make_checkout(tmp_path / "managed")

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", running)
    monkeypatch.setenv("HERMES_INSTALL_DIR", str(managed))

    assert hermes_main._resolve_managed_install_root() == running.resolve()


def test_resolve_install_root_recovers_when_roots_diverge(tmp_path, monkeypatch):
    """Module root that isn't a checkout must not be treated as the install."""
    site_packages = tmp_path / "conda" / "Lib" / "site-packages"
    (site_packages / "hermes_cli").mkdir(parents=True)
    (site_packages / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    managed = _make_checkout(tmp_path / "managed")

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", site_packages)
    monkeypatch.setenv("HERMES_INSTALL_DIR", str(managed))

    assert hermes_main._resolve_managed_install_root() == managed.resolve()


def test_resolve_install_root_falls_back_to_project_root(tmp_path, monkeypatch):
    """No candidate validates → keep today's behavior, don't guess."""
    module_root = tmp_path / "module_root"
    module_root.mkdir()

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", module_root)
    monkeypatch.setattr(hermes_main, "_looks_like_hermes_checkout", lambda _p: False)

    assert hermes_main._resolve_managed_install_root() == module_root


def test_run_install_with_heartbeat_honors_cwd(tmp_path, monkeypatch):
    """The install cwd is what makes ``pip install -e .`` resolve correctly."""
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path / "module_root")
    target = tmp_path / "managed"

    with patch("subprocess.run") as fake_run:
        hermes_main._run_install_with_heartbeat(["uv", "pip", "install", "-e", "."])
        assert fake_run.call_args.kwargs["cwd"] == tmp_path / "module_root"

        hermes_main._run_install_with_heartbeat(
            ["uv", "pip", "install", "-e", "."], cwd=target
        )
        assert fake_run.call_args.kwargs["cwd"] == target


def test_update_via_zip_writes_and_installs_into_managed_root(tmp_path, monkeypatch):
    """End to end: divergent roots must not send the update to the shim's tree.

    Both halves of the update are asserted — the file replacement lands in the
    managed checkout, and the dependency install runs with that same directory
    as its cwd (and its venv as ``VIRTUAL_ENV``).
    """
    zip_path = tmp_path / "update.zip"
    _build_update_zip(zip_path)

    # Running hermes_cli lives in a site-packages dir that is NOT a checkout.
    site_packages = tmp_path / "conda" / "Lib" / "site-packages"
    (site_packages / "hermes_cli").mkdir(parents=True)
    (site_packages / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    managed = _make_checkout(tmp_path / "managed")

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", site_packages)
    monkeypatch.setenv("HERMES_INSTALL_DIR", str(managed))
    monkeypatch.setattr(hermes_main, "get_hermes_home", lambda: tmp_path / "home")

    captured: dict = {}

    def fake_urlretrieve(url, dest):
        Path(dest).write_bytes(zip_path.read_bytes())
        return dest, None

    def fake_install(install_cmd_prefix, *, env=None, group="all", cwd=None):
        captured["cwd"] = cwd
        captured["env"] = env

    monkeypatch.setattr(
        hermes_main, "_install_python_dependencies_with_optional_fallback", fake_install
    )
    monkeypatch.setattr(hermes_main, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(
        hermes_main, "_build_web_ui", lambda web_dir, **kw: captured.setdefault(
            "web_dir", web_dir
        )
    )
    monkeypatch.setattr(
        hermes_main, "_kill_stale_dashboard_processes", lambda **kw: None
    )
    # Post-install steps that would otherwise touch the real Hermes home.
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda **kw: {"copied": []})
    monkeypatch.setattr(
        "hermes_cli.model_catalog.seed_cache_from_checkout", lambda root: False
    )
    monkeypatch.setattr(hermes_main, "_print_curator_first_run_notice", lambda: None)
    monkeypatch.setattr(hermes_main, "_print_curator_recent_run_notice", lambda: None)

    args = type("Args", (), {})()

    with patch("urllib.request.urlretrieve", side_effect=fake_urlretrieve), patch(
        "hermes_cli.managed_uv.ensure_uv", return_value="/fake/bin/uv"
    ), patch("hermes_cli.managed_uv.update_managed_uv"):
        hermes_main._update_via_zip(args)

    assert (managed / "README.md").read_text(encoding="utf-8") == "ok\n"
    assert not (site_packages / "README.md").exists()
    assert captured["cwd"] == managed.resolve()
    assert captured["env"]["VIRTUAL_ENV"] == str(managed.resolve() / "venv")
    assert captured["web_dir"] == managed.resolve() / "web"
