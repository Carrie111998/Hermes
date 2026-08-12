"""First-load regressions for plugin-provided external secret sources."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_secret_source_plugin(
    home: Path,
    *,
    fetch_body: str,
    source_name: str = "startup_test_source",
    plugin_name: str = "startup-test-plugin",
    declares_source: bool = True,
    register_body: str | None = None,
) -> None:
    plugin_dir = home / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    declaration = (
        f"provides_secret_sources:\n  - {source_name}\n" if declares_source else ""
    )
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {plugin_name}\n"
        "kind: standalone\n"
        "version: 1.0.0\n"
        "description: External secret-source startup test\n"
        f"{declaration}",
        encoding="utf-8",
    )
    register = register_body or (
        "def register(ctx):\n"
        "    ctx.register_secret_source(StartupTestSource())\n"
    )
    (plugin_dir / "__init__.py").write_text(
        "from agent.secret_sources.base import FetchResult, SecretSource\n"
        "\n"
        "class StartupTestSource(SecretSource):\n"
        f"    name = {source_name!r}\n"
        "    label = 'Startup test source'\n"
        "    shape = 'mapped'\n"
        "\n"
        "    def fetch(self, cfg, home_path):\n"
        f"{fetch_body}\n"
        "\n"
        f"{register}",
        encoding="utf-8",
    )


def _write_package_secret_source_plugin(home: Path, import_marker: Path) -> None:
    plugin_dir = home / "plugins" / "startup-package-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: startup-package-plugin\n"
        "kind: standalone\n"
        "version: 1.0.0\n"
        "description: Package secret-source startup test\n"
        "provides_secret_sources:\n"
        "  - startup_package_source\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from .registration import register\n",
        encoding="utf-8",
    )
    (plugin_dir / "source.py").write_text(
        "from agent.secret_sources.base import FetchResult, SecretSource\n"
        "class PackageSource(SecretSource):\n"
        "    name = 'startup_package_source'\n"
        "    label = 'Package startup source'\n"
        "    shape = 'mapped'\n"
        "    def fetch(self, cfg, home_path):\n"
        "        return FetchResult(secrets={'STARTUP_TEST_API_KEY': 'package-value'})\n",
        encoding="utf-8",
    )
    (plugin_dir / "registration.py").write_text(
        "from pathlib import Path\n"
        "from .source import PackageSource\n"
        f"_marker = Path({str(import_marker)!r})\n"
        "_prior = _marker.read_text() if _marker.exists() else ''\n"
        "_marker.write_text(_prior + 'import\\n')\n"
        "_registered = False\n"
        "def _command(_args):\n"
        "    return 'package-command-ok'\n"
        "def register(ctx):\n"
        "    global _registered\n"
        "    if _registered:\n"
        "        return\n"
        "    _registered = True\n"
        "    ctx.register_secret_source(PackageSource())\n"
        "    ctx.register_command('startup-package-command', _command, description='package command')\n",
        encoding="utf-8",
    )


def _write_unrelated_plugin(home: Path, marker: Path) -> None:
    plugin_dir = home / "plugins" / "unrelated-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: unrelated-plugin\n"
        "kind: standalone\n"
        "version: 1.0.0\n"
        "description: Must not import during secret bootstrap\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "def register(ctx):\n"
        "    return None\n",
        encoding="utf-8",
    )


def _write_config(
    home: Path,
    *,
    source_name: str = "startup_test_source",
    plugin_name: str = "startup-test-plugin",
    include_sources: bool = True,
    include_unrelated: bool = False,
) -> None:
    enabled = [plugin_name]
    if include_unrelated:
        enabled.append("unrelated-plugin")
    sources = f"  sources:\n    - {source_name}\n" if include_sources else ""
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        + "".join(f"    - {name}\n" for name in enabled)
        + "secrets:\n"
        + sources
        + f"  {source_name}:\n"
        "    enabled: true\n"
        "    env:\n"
        "      STARTUP_TEST_API_KEY: test-ref\n",
        encoding="utf-8",
    )


def _run_python(
    cwd: Path,
    code: str,
    *,
    process_home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    process_home = process_home or cwd
    bundled = process_home / "empty-bundled-plugins"
    bundled.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(process_home),
            "HERMES_BUNDLED_PLUGINS": str(bundled),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(ROOT), env.get("PYTHONPATH", "")) if part
            ),
        }
    )
    env.pop("HERMES_SAFE_MODE", None)
    env.pop("STARTUP_TEST_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_first_load_imports_only_enabled_secret_source_plugins(tmp_path):
    secret_value = "startup-value-that-must-not-be-logged"
    fetch_marker = tmp_path / "fetch-count"
    unrelated_marker = tmp_path / "unrelated-imported"
    _write_secret_source_plugin(
        tmp_path,
        fetch_body=(
            "        marker = home_path / 'fetch-count'\n"
            "        prior = marker.read_text() if marker.exists() else ''\n"
            "        marker.write_text(prior + 'fetch\\n')\n"
            f"        return FetchResult(secrets={{'STARTUP_TEST_API_KEY': {secret_value!r}}})"
        ),
    )
    _write_unrelated_plugin(tmp_path, unrelated_marker)
    _write_config(tmp_path, include_unrelated=True)

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "from hermes_cli.env_loader import get_secret_source, load_hermes_dotenv\n"
        "from hermes_cli.plugins import get_plugin_manager\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "load_hermes_dotenv(hermes_home=home)\n"
        "load_hermes_dotenv(hermes_home=home)\n"
        f"assert os.environ['STARTUP_TEST_API_KEY'] == {secret_value!r}\n"
        "assert get_secret_source('STARTUP_TEST_API_KEY') == 'startup_test_source'\n"
        "assert not get_plugin_manager()._discovered\n"
        "print('targeted-bootstrap-ok')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "targeted-bootstrap-ok"
    assert fetch_marker.read_text() == "fetch\n"
    assert not unrelated_marker.exists()
    assert "unknown source" not in (result.stdout + result.stderr).lower()
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr


def test_package_plugin_registers_non_secret_capabilities_after_bootstrap(tmp_path):
    import_marker = tmp_path / "package-import-count"
    _write_package_secret_source_plugin(tmp_path, import_marker)
    _write_config(
        tmp_path,
        source_name="startup_package_source",
        plugin_name="startup-package-plugin",
    )

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os, sys\n"
        "from hermes_cli.env_loader import load_hermes_dotenv\n"
        "from hermes_cli.plugins import get_plugin_commands, get_plugin_manager\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "load_hermes_dotenv(hermes_home=home)\n"
        "manager = get_plugin_manager()\n"
        "assert not manager._discovered\n"
        "assert not any('__secret_bootstrap_' in name for name in sys.modules)\n"
        "commands = get_plugin_commands()\n"
        "assert commands['startup-package-command']['handler']('') == 'package-command-ok'\n"
        "loaded = manager._plugins['startup-package-plugin']\n"
        "assert loaded.enabled and loaded.error is None\n"
        "assert 'startup-package-command' in loaded.commands_registered\n"
        "assert not any('__secret_bootstrap_' in name for name in sys.modules)\n"
        "print('package-full-discovery-ok')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "package-full-discovery-ok"
    assert import_marker.read_text() == "import\nimport\n"
    assert "already registered" not in result.stderr


def test_explicit_non_process_home_is_honored(tmp_path):
    process_home = tmp_path / "process-home"
    target_home = tmp_path / "target-home"
    process_home.mkdir()
    target_home.mkdir()
    _write_secret_source_plugin(
        target_home,
        fetch_body=(
            "        (home_path / 'target-fetch').write_text(str(home_path))\n"
            "        return FetchResult(secrets={'STARTUP_TEST_API_KEY': 'target-value'})"
        ),
    )
    _write_config(target_home)

    result = _run_python(
        target_home,
        "from pathlib import Path\n"
        "import os\n"
        "from hermes_cli.env_loader import load_hermes_dotenv\n"
        f"target = Path({str(target_home)!r})\n"
        "load_hermes_dotenv(hermes_home=target)\n"
        "assert os.environ['STARTUP_TEST_API_KEY'] == 'target-value'\n"
        "assert (target / 'target-fetch').read_text() == str(target)\n"
        "print('explicit-home-ok')\n",
        process_home=process_home,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "explicit-home-ok"
    assert not (process_home / "target-fetch").exists()


def test_omitted_sources_list_uses_enabled_source_section(tmp_path):
    _write_secret_source_plugin(
        tmp_path,
        declares_source=False,
        fetch_body=(
            "        return FetchResult(secrets={'STARTUP_TEST_API_KEY': 'legacy-value'})"
        ),
    )
    _write_config(tmp_path, include_sources=False)

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "from hermes_cli.env_loader import load_hermes_dotenv\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "load_hermes_dotenv(hermes_home=home)\n"
        "assert os.environ['STARTUP_TEST_API_KEY'] == 'legacy-value'\n"
        "print('omitted-list-ok')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "omitted-list-ok"


def test_bootstrap_failure_is_fail_open_and_redacted(tmp_path):
    sentinel = "bootstrap-exception-secret-that-must-not-be-logged"
    _write_secret_source_plugin(
        tmp_path,
        fetch_body="        return FetchResult(secrets={})",
        register_body=(
            "def register(ctx):\n"
            f"    raise RuntimeError({sentinel!r})\n"
        ),
    )
    _write_config(tmp_path)

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "from hermes_cli.env_loader import load_hermes_dotenv\n"
        "load_hermes_dotenv(hermes_home=Path(os.environ['HERMES_HOME']))\n"
        "assert 'STARTUP_TEST_API_KEY' not in os.environ\n"
        "print('bootstrap-survived')\n",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "bootstrap-survived"
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr


def test_fetch_failure_is_fail_open_and_redacted(tmp_path):
    sentinel = "fetch-exception-secret-that-must-not-be-logged"
    _write_secret_source_plugin(
        tmp_path,
        fetch_body=f"        raise RuntimeError({sentinel!r})",
    )
    _write_config(tmp_path)

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "from hermes_cli.env_loader import load_hermes_dotenv\n"
        "load_hermes_dotenv(hermes_home=Path(os.environ['HERMES_HOME']))\n"
        "assert 'STARTUP_TEST_API_KEY' not in os.environ\n"
        "print('fetch-survived')\n",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "fetch-survived"
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr


def test_benign_secret_config_read_does_not_import_or_fetch_plugin(tmp_path):
    import_marker = tmp_path / "plugin-imported"
    plugin_dir = tmp_path / "plugins" / "startup-test-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: startup-test-plugin\n"
        "kind: standalone\n"
        "provides_secret_sources:\n"
        "  - startup_test_source\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )
    _write_config(tmp_path)

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "from agent.secret_sources.registry import get_source\n"
        "from hermes_cli.env_loader import _load_secrets_config\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "cfg = _load_secrets_config(home)\n"
        "assert cfg['sources'] == ['startup_test_source']\n"
        "assert get_source('startup_test_source') is None\n"
        "print('config-read-only')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "config-read-only"
    assert not import_marker.exists()
