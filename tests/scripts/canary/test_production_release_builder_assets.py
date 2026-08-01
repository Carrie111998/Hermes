from __future__ import annotations

import runpy
import shlex
import sys
from pathlib import Path

import pytest

from scripts.canary import production_release_builder_phase as phase


ROOT = Path(__file__).resolve().parents[3]
ASSET_ROOT = ROOT / "ops/muncho/release-updater"
WRAPPER = ASSET_ROOT / "muncho-release-builder-phase"
UNIT = ASSET_ROOT / "muncho-release-builder@.service"
SYSUSERS = ASSET_ROOT / "muncho-release-builder.sysusers"
TMPFILES = ASSET_ROOT / "muncho-release-builder.tmpfiles"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="ascii").splitlines()


def test_builder_wrapper_preserves_label_as_module_argv0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = WRAPPER.read_text(encoding="ascii")
    command = wrapper.split("exec ", maxsplit=1)[1].replace("\\\n", "")
    argv = shlex.split(command)
    assert argv[:8] == [
        "/usr/bin/flock",
        "--exclusive",
        "--no-fork",
        "/run/lock/muncho-release-builder-promotion.lock",
        "/usr/bin/python3",
        "-B",
        "-I",
        "-c",
    ]
    assert argv[9:] == [
        "muncho-release-builder-phase",
        "$@",
    ]
    bootstrap = argv[8]
    assert bootstrap == (
        'import runpy,sys; sys.path.insert(0,'
        '"/usr/lib/muncho-release-updater"); '
        "sys.argv=sys.argv[1:]; "
        'runpy.run_module("scripts.canary.'
        'production_release_builder_phase",run_name="__main__")'
    )
    request = Path("/var/lib/muncho-release-updates/tx/input/request.json")
    observed: list[Path] = []
    module_calls: list[tuple[str, str | None, tuple[str, ...]]] = []

    def fake_run_module(
        name: str,
        *,
        run_name: str | None = None,
    ) -> dict[str, object]:
        module_calls.append((name, run_name, tuple(sys.argv)))
        monkeypatch.setattr(
            phase,
            "run_builder_phase",
            lambda path: observed.append(path),
        )
        assert phase.main() == 0
        return {}

    monkeypatch.setattr(runpy, "run_module", fake_run_module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "-c",
            "muncho-release-builder-phase",
            "--request",
            str(request),
        ],
    )

    exec(compile(bootstrap, "<builder-wrapper-bootstrap>", "exec"), {})

    assert module_calls == [
        (
            "scripts.canary.production_release_builder_phase",
            "__main__",
            (
                "muncho-release-builder-phase",
                "--request",
                str(request),
            ),
        )
    ]
    assert observed == [request]


def test_builder_assets_preserve_debian_12_boundary_contract() -> None:
    unit = _lines(UNIT)
    assert "User=muncho-release-builder" in unit
    assert "Group=muncho-release-builder" in unit
    assert "SupplementaryGroups=" in unit
    assert (
        "ExecStart=/usr/libexec/muncho-release-builder-phase "
        "--request /var/lib/muncho-release-updates/%i/input/request.json"
    ) in unit
    assert (
        "WorkingDirectory=/var/lib/muncho-release-updates/%i/output"
    ) in unit
    assert "ProtectSystem=strict" in unit
    assert "PrivateNetwork=yes" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "CapabilityBoundingSet=" in unit
    assert "AmbientCapabilities=" in unit
    assert "SystemCallFilter=@system-service" in unit
    assert (
        "SystemCallFilter=~@mount @privileged @resources @reboot @swap"
    ) in unit
    assert (
        "ReadOnlyPaths=/run/lock/"
        "muncho-release-builder-promotion.lock"
    ) in unit
    assert (
        "ReadOnlyPaths=/var/lib/muncho-release-updates/%i/input"
    ) in unit
    assert (
        "ReadWritePaths=/var/lib/muncho-release-updates/%i/output"
    ) in unit

    assert _lines(SYSUSERS) == [
        "g muncho-release-builder 29104",
        (
            'u muncho-release-builder 29104:29104 '
            '"Muncho pinned release builder" /nonexistent '
            "/usr/sbin/nologin"
        ),
    ]
    assert _lines(TMPFILES) == [
        "d /var/lib/muncho-release-updates 0755 root root -",
        (
            "f /run/lock/muncho-release-builder-promotion.lock "
            "0440 root muncho-release-builder -"
        )
    ]

    wrapper = _lines(WRAPPER)
    assert not any(line.startswith("exec 9<") for line in wrapper)
    assert "exec /usr/bin/flock --exclusive --no-fork \\" in wrapper
    assert (
        "  /run/lock/muncho-release-builder-promotion.lock \\"
    ) in wrapper
    assert "  /usr/bin/python3 -B -I -c \\" in wrapper
