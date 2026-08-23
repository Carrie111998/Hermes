from pathlib import Path


def test_windows_native_install_path_docs_match_installer() -> None:
    doc = Path("website/docs/user-guide/windows-native.md").read_text()
    install = Path("scripts/install.ps1").read_text()

    # The launchers live in the managed binary dir OUTSIDE the git checkout
    # (ORION_HOME\bin, next to the managed uv) — NOT the whole venv\Scripts
    # (which would shadow the user's python, #83797) and NOT a dir inside
    # the checkout (which `orion update`'s autostash swept off disk).
    assert "%LOCALAPPDATA%\\orion\\bin" in doc
    assert (
        "Get-Command orion        # should print "
        "C:\\Users\\<you>\\AppData\\Local\\orion\\bin\\orion.exe"
    ) in doc
    # Installer exposes $OrionHome\bin, and must copy the launchers into it.
    assert '$orionBin = "$OrionHome\\bin"' in install
    assert "orion.exe" in install and "orion-acp.exe" in install
    # Guard against regressions to either legacy layout.
    assert '$orionBin = "$InstallDir\\venv\\Scripts"' not in install
    assert '$orionBin = "$InstallDir\\bin"' not in install
