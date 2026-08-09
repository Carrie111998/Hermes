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
) -> None:
    plugin_dir = home / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {plugin_name}\n"
        "kind: standalone\n"
        "version: 1.0.0\n"
        "description: External secret-source startup test\n",
        encoding="utf-8",
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
        "def register(ctx):\n"
        "    ctx.register_secret_source(StartupTestSource())\n",
        encoding="utf-8",
    )


def _write_config(
    home: Path,
    *,
    source_name: str = "startup_test_source",
    plugin_name: str = "startup-test-plugin",
) -> None:
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        f"    - {plugin_name}\n"
        "secrets:\n"
        "  sources:\n"
        f"    - {source_name}\n"
        f"  {source_name}:\n"
        "    enabled: true\n"
        "    env:\n"
        "      STARTUP_TEST_API_KEY: test-ref\n",
        encoding="utf-8",
    )


def _run_python(home: Path, code: str) -> subprocess.CompletedProcess[str]:
    bundled = home / "empty-bundled-plugins"
    bundled.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(home),
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
        cwd=home,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_first_dotenv_load_discovers_plugin_before_validation_and_apply(tmp_path):
    secret_value = "startup-value-that-must-not-be-logged"
    _write_secret_source_plugin(
        tmp_path,
        fetch_body=(
            "        marker = home_path / 'fetch-count'\n"
            "        prior = marker.read_text() if marker.exists() else ''\n"
            "        marker.write_text(prior + 'fetch\\n')\n"
            f"        return FetchResult(secrets={{'STARTUP_TEST_API_KEY': {secret_value!r}}})"
        ),
    )
    _write_config(tmp_path)

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "from hermes_cli.env_loader import (\n"
        "    get_secret_source, load_hermes_dotenv,\n"
        ")\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "load_hermes_dotenv(hermes_home=home)\n"
        "load_hermes_dotenv(hermes_home=home)\n"
        f"assert os.environ['STARTUP_TEST_API_KEY'] == {secret_value!r}\n"
        "assert get_secret_source('STARTUP_TEST_API_KEY') == 'startup_test_source'\n"
        "assert (home / 'fetch-count').read_text() == 'fetch\\n'\n"
        "print('registered-before-apply')\n",
    )

    assert result.returncode == 0, result.stderr
    assert "registered-before-apply" in result.stdout
    assert "unknown source" not in (result.stdout + result.stderr).lower()
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr


def test_plugin_discovery_failure_is_fail_open_and_does_not_log_value(
    monkeypatch, caplog, capsys
):
    from agent.secret_sources import registry
    from hermes_cli import env_loader, plugins

    secret_value = "discovery-exception-secret-that-must-not-be-logged"
    monkeypatch.setattr(registry, "get_source", lambda _name: None)

    def _fail_discovery():
        raise RuntimeError(secret_value)

    monkeypatch.setattr(plugins, "discover_plugins", _fail_discovery)

    env_loader._discover_configured_secret_source_plugins(
        {"sources": ["startup_test_source"]}
    )

    captured = capsys.readouterr()
    assert secret_value not in caplog.text
    assert secret_value not in captured.out
    assert secret_value not in captured.err


def test_benign_secret_config_read_does_not_discover_or_fetch_plugin(tmp_path):
    _write_secret_source_plugin(
        tmp_path,
        fetch_body=(
            "        (home_path / 'fetch-count').write_text('fetched')\n"
            "        return FetchResult(secrets={'STARTUP_TEST_API_KEY': 'unused-value'})"
        ),
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
        "assert not (home / 'fetch-count').exists()\n"
        "assert 'STARTUP_TEST_API_KEY' not in os.environ\n"
        "print('config-read-only')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "config-read-only"
    assert "unused-value" not in result.stderr


def test_plugin_fetch_failure_is_fail_open_and_does_not_log_secret_value(tmp_path):
    secret_value = "failure-value-that-must-not-be-logged"
    _write_secret_source_plugin(
        tmp_path,
        fetch_body=f"        raise RuntimeError({secret_value!r})",
    )
    _write_config(tmp_path)

    result = _run_python(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "from hermes_cli.env_loader import load_hermes_dotenv\n"
        "load_hermes_dotenv(hermes_home=Path(os.environ['HERMES_HOME']))\n"
        "assert 'STARTUP_TEST_API_KEY' not in os.environ\n"
        "print('startup-survived')\n",
    )

    assert result.returncode == 0, result.stderr
    assert "startup-survived" in result.stdout
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr
