"""
Smoke tests for the nocodb optional skill.

Validates:
  - SKILL.md frontmatter conforms to the authoring standards
  - Both platform scripts ship and expose an identical command surface
  - Every command named in SKILL.md prose is one the scripts implement
    (catches doc drift when the vendored scripts are refreshed upstream)
  - The scripts only ever talk to the configured NocoDB origin

No network. Everything here is static analysis of the shipped files.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "nocodb"
)
SH = SKILL_DIR / "scripts" / "nocodb.sh"
PS1 = SKILL_DIR / "scripts" / "nocodb.ps1"

# `case` labels in the Bash dispatcher, at column 0. Some labels alternate:
#   record:update-many)
#   where:help|filter:help)
_SH_COMMAND = re.compile(r"^([a-z][a-z:|-]*)\)", re.MULTILINE)
# `switch` labels in the PowerShell dispatcher, either a plain literal
#   "record:update-many" {
# or a script-block condition covering several aliases
#   { $_ -eq "where:help" -or $_ -eq "filter:help" } {
_PS1_COMMAND = re.compile(r'^\s+"([a-z][a-z:-]*)"\s*\{', re.MULTILINE)
_PS1_ALIAS = re.compile(r'\$_ -eq "([a-z][a-z:-]*)"')
# Commands as written in SKILL.md prose/tables: `record:update-many`.
_DOC_COMMAND = re.compile(r"`(?:scripts/nocodb\.sh )?([a-z][a-z-]+:[a-z:-]+)`")


def _sh_commands_from(src: str) -> set[str]:
    """Flatten `a|b)` alternation labels into individual command names."""
    return {c for label in _SH_COMMAND.findall(src) for c in label.split("|")}


def _ps1_commands_from(src: str) -> set[str]:
    return set(_PS1_COMMAND.findall(src)) | set(_PS1_ALIAS.findall(src))


@pytest.fixture(scope="module")
def skill_src() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_src: str) -> dict:
    m = re.search(r"^---\n(.*?)\n---", skill_src, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(m.group(1))


@pytest.fixture(scope="module")
def sh_commands() -> set[str]:
    return _sh_commands_from(SH.read_text(encoding="utf-8"))


def test_skill_dir_and_scripts_exist() -> None:
    assert SKILL_DIR.is_dir(), f"missing skill dir: {SKILL_DIR}"
    assert SH.is_file(), "missing scripts/nocodb.sh"
    assert PS1.is_file(), "missing scripts/nocodb.ps1"


def test_bash_script_is_executable() -> None:
    # The skill documents `scripts/nocodb.sh <command>` as the entry point,
    # so the mode bit has to survive the commit.
    assert SH.stat().st_mode & 0o111, "scripts/nocodb.sh is not executable"


def test_required_frontmatter_fields(frontmatter: dict) -> None:
    for field in ("name", "description", "version", "author", "license", "platforms"):
        assert field in frontmatter, f"missing frontmatter field: {field}"
    assert frontmatter["name"] == "nocodb"
    assert frontmatter["license"] == "MIT"


def test_description_hardline(frontmatter: dict) -> None:
    desc = frontmatter["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars (hardline 60)"
    assert desc.endswith("."), "description must end with a period"


def test_declares_token_env_var(frontmatter: dict) -> None:
    names = {e["name"] for e in frontmatter["required_environment_variables"]}
    assert "NOCODB_TOKEN" in names
    assert frontmatter["prerequisites"]["commands"] == ["curl", "jq"]


def test_platforms_match_shipped_scripts(frontmatter: dict) -> None:
    # Windows is only claimable because the PowerShell port ships alongside
    # the Bash one. If nocodb.ps1 is ever dropped, this fails loudly.
    assert set(frontmatter["platforms"]) == {"macos", "linux", "windows"}
    assert PS1.is_file()


def test_tags_present(frontmatter: dict) -> None:
    assert frontmatter["metadata"]["hermes"]["tags"], "no metadata.hermes.tags"


def test_both_scripts_expose_the_same_commands(sh_commands: set[str]) -> None:
    ps1_commands = _ps1_commands_from(PS1.read_text(encoding="utf-8"))
    assert sh_commands, "no commands parsed out of nocodb.sh"
    assert sh_commands == ps1_commands, (
        "Bash/PowerShell command surfaces diverged — "
        f"sh-only={sorted(sh_commands - ps1_commands)} "
        f"ps1-only={sorted(ps1_commands - sh_commands)}"
    )


def test_documented_commands_all_exist(skill_src: str, sh_commands: set[str]) -> None:
    documented = {
        c for c in _DOC_COMMAND.findall(skill_src) if not c.startswith("app.nocodb")
    }
    assert documented, "no commands found in SKILL.md — regex drift?"
    unknown = documented - sh_commands
    assert not unknown, f"SKILL.md documents commands the scripts lack: {sorted(unknown)}"


def test_documented_command_count_matches(skill_src: str, sh_commands: set[str]) -> None:
    m = re.search(r"identical (\d+)-command surface", skill_src)
    assert m, "SKILL.md no longer states the command-surface size"
    assert int(m.group(1)) == len(sh_commands), (
        f"SKILL.md claims {m.group(1)} commands, scripts implement {len(sh_commands)}"
    )


@pytest.mark.parametrize("script", [SH, PS1], ids=["sh", "ps1"])
def test_scripts_contact_only_nocodb_default_origin(script: Path) -> None:
    # Every request is built off $NOCODB_URL, whose only default is the NocoDB
    # cloud origin. A second literal host on an executable line would mean
    # exfiltration or an unreviewed upstream change. Comment lines are skipped
    # so the provenance header can cite the upstream repo.
    code = "\n".join(
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    hosts = set(re.findall(r"https?://[\w.-]+", code))
    assert hosts == {"https://app.nocodb.com"}, f"unexpected hosts: {sorted(hosts)}"


def test_bash_script_parses() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    proc = subprocess.run(
        [bash, "-n", str(SH)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"
