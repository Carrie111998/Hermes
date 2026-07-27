"""Contract checks for the independent local Charterforge installer."""

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "install-charterforge.sh"


def test_installer_is_executable_and_independent() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert SCRIPT.stat().st_mode & 0o111
    assert "CHARTERFORGE_SOURCE" in text
    assert "CHARTERFORGE_INSTALL_DIR" in text
    assert "uv pip install" in text
    assert "upstream Hermes installer" in text
    assert "hermes-agent.nousresearch.com" not in text


def test_installer_refuses_non_venv_directory() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "refusing to reuse non-venv install directory" in text
