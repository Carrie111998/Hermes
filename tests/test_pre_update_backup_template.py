"""Regression: the shipped config template must not disable the pre-update
backup safety net (#94944).

Installers (scripts/install.sh, hermes doctor --fix, docker/stage2-hook.sh)
copy cli-config.yaml.example verbatim to ~/.hermes/config.yaml, so whatever
``updates.pre_update_backup`` the template ships becomes an EXPLICIT user
setting that overrides the code default. The runtime default is ``quick``
(#65754); the legacy boolean ``false`` maps to ``off``. A template still
carrying ``false`` therefore silently disables the #48200 wipe safety net on
every ``hermes update``, with no output, for users who never opted out.
"""

from pathlib import Path
from types import SimpleNamespace

from hermes_cli.update_cmd import _resolve_pre_update_backup_mode

TEMPLATE = Path(__file__).resolve().parents[1] / "cli-config.yaml.example"


def _seed_from_template(hermes_home: Path) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )


def test_shipped_template_keeps_pre_update_backup_safety_net(tmp_path, monkeypatch):
    """A fresh install seeded from the template must keep a backup snapshot
    active — the resolved mode must not be ``off``."""
    hermes_home = tmp_path / ".hermes"
    _seed_from_template(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # No CLI flags — the config value alone decides.
    args = SimpleNamespace(no_backup=False, backup=False)
    mode = _resolve_pre_update_backup_mode(args)

    assert mode != "off", "shipped template disables the pre-update backup safety net"
    assert mode == "quick"


def test_no_backup_flag_still_wins_over_template(tmp_path, monkeypatch):
    """The fix must not take the choice away: an explicit --no-backup still
    disables the snapshot even though the template now enables it."""
    hermes_home = tmp_path / ".hermes"
    _seed_from_template(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    args = SimpleNamespace(no_backup=True, backup=False)
    assert _resolve_pre_update_backup_mode(args) == "off"
