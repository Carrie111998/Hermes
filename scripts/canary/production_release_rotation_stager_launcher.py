#!/usr/bin/env python3
"""Launch the exact root-owned rotation stager from a promoted release.

The launcher is installed outside every release.  It verifies the complete
published release before executing its pinned interpreter and exact rotation
module.  The selected action is either the exact legacy one-call protocol enum
or one of the four split-phase enums; there is no natural-language
classification or routing here.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable, Mapping, Never, Sequence

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_builder_runtime as builder


PRODUCTION_RELEASE_PARENT = Path("/opt/adventico-ai-platform/hermes-agent-releases")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MODULE = "scripts.canary.production_cutover_unit_input_rotation"
_PHASE_ACTIONS = frozenset({
    "prepare-release-unit-inputs",
    "preauthorize-release-unit-inputs",
    "finalize-release-unit-inputs",
    "abort-release-unit-inputs",
})
_LEGACY_ACTION = "rotate-unit-input-authority"
_ACTIONS = _PHASE_ACTIONS | {_LEGACY_ACTION}


class RotationStagerLauncherError(RuntimeError):
    """Stable, secret-free launch failure."""


def _fail(code: str, cause: BaseException | None = None) -> Never:
    del cause
    raise RotationStagerLauncherError(code) from None


def _validate_stager_purpose(
    release: Path,
    revision: str,
    expected_uid: int,
    expected_gid: int,
) -> None:
    selected = release / phase.TERMINAL_RECEIPT_NAME
    try:
        state = os.lstat(selected)
        raw = selected.read_bytes()
    except OSError as exc:
        _fail("rotation_stager_launcher_purpose_invalid", exc)
    if (
        not stat.S_ISREG(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != expected_uid
        or state.st_gid != expected_gid
        or state.st_nlink != 1
        or stat.S_IMODE(state.st_mode) != 0o444
        or not 1 < len(raw) <= phase.MAX_JSON_BYTES
    ):
        _fail("rotation_stager_launcher_purpose_invalid")
    try:
        receipt = phase.validate_terminal_receipt(phase._decode_canonical_document(raw))
    except phase.ProductionReleaseBuilderPhaseError as exc:
        _fail("rotation_stager_launcher_purpose_invalid", exc)
    if (
        receipt.get("schema")
        != phase.UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA
        or receipt.get("purpose") != phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE
        or receipt.get("release_revision") != revision
        or receipt.get("entrypoint_relative_path")
        != phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
    ):
        _fail("rotation_stager_launcher_purpose_invalid")


def _launch_for_test(
    *,
    revision: str,
    action: str,
    release_parent: Path = PRODUCTION_RELEASE_PARENT,
    production: bool = True,
    effective_uid: int | None = None,
    expected_release_uid: int = 0,
    expected_release_gid: int = 0,
    verifier: Callable[..., Mapping[str, object]] | None = None,
    purpose_validator: Callable[[Path, str, int, int], None] = (
        _validate_stager_purpose
    ),
    execve: Callable[[str, Sequence[str], Mapping[str, str]], object] = os.execve,
) -> Never:
    uid = os.geteuid() if effective_uid is None else effective_uid
    if (
        _REVISION.fullmatch(revision or "") is None
        or action not in _ACTIONS
        or not isinstance(release_parent, Path)
        or not release_parent.is_absolute()
        or ".." in release_parent.parts
        or type(production) is not bool
        or type(uid) is not int
        or uid != 0
        or type(expected_release_uid) is not int
        or expected_release_uid < 0
        or type(expected_release_gid) is not int
        or expected_release_gid < 0
        or (production and (expected_release_uid != 0 or expected_release_gid != 0))
        or (production and release_parent != PRODUCTION_RELEASE_PARENT)
        or (production and not sys.platform.startswith("linux"))
    ):
        _fail("rotation_stager_launcher_contract_invalid")
    release = release_parent / f"hermes-agent-{revision[:12]}"
    try:
        root_state = os.lstat(release)
    except OSError as exc:
        _fail("rotation_stager_launcher_release_invalid", exc)
    if (
        not stat.S_ISDIR(root_state.st_mode)
        or stat.S_ISLNK(root_state.st_mode)
        or root_state.st_uid != expected_release_uid
        or root_state.st_gid != expected_release_gid
        or stat.S_IMODE(root_state.st_mode) != 0o555
    ):
        _fail("rotation_stager_launcher_release_invalid")
    selected_verifier = (
        builder._verify_published_release_filesystem if verifier is None else verifier
    )
    try:
        selected_verifier(
            release,
            revision=revision,
            expected_uid=expected_release_uid,
            expected_gid=expected_release_gid,
            require_logical_owner=True,
        )
    except (OSError, builder.ProductionReleaseBuilderError) as exc:
        _fail("rotation_stager_launcher_release_invalid", exc)
    purpose_validator(
        release,
        revision,
        expected_release_uid,
        expected_release_gid,
    )
    interpreter = release / ".venv/bin/python"
    try:
        interpreter_state = os.lstat(interpreter)
    except OSError as exc:
        _fail("rotation_stager_launcher_interpreter_invalid", exc)
    if (
        not stat.S_ISREG(interpreter_state.st_mode)
        or stat.S_ISLNK(interpreter_state.st_mode)
        or interpreter_state.st_uid != expected_release_uid
        or interpreter_state.st_gid != expected_release_gid
        or interpreter_state.st_nlink != 1
        or stat.S_IMODE(interpreter_state.st_mode) != 0o555
    ):
        _fail("rotation_stager_launcher_interpreter_invalid")
    code = (
        "import runpy,sys;"
        f"sys.path.insert(0,{str(release)!r});"
        "sys.argv=sys.argv[1:];"
        f"runpy.run_module({_MODULE!r},run_name='__main__')"
    )
    argv = (
        str(interpreter),
        "-I",
        "-c",
        code,
        "muncho-release-unit-input-rotation-stager",
    )
    if action != _LEGACY_ACTION:
        argv = (*argv, action)
    environment = {
        "HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    try:
        execve(str(interpreter), argv, environment)
    except OSError as exc:
        _fail("rotation_stager_launcher_exec_failed", exc)
    _fail("rotation_stager_launcher_exec_returned")


def launch_rotation_stager(*, revision: str, action: str) -> Never:
    return _launch_for_test(revision=revision, action=action)


def main(argv: Sequence[str] | None = None) -> int:
    selected = list(sys.argv[1:] if argv is None else argv)
    if len(selected) != 2:
        print(
            '{"error_code":"rotation_stager_launcher_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    try:
        launch_rotation_stager(revision=selected[0], action=selected[1])
    except RotationStagerLauncherError:
        print(
            '{"error_code":"rotation_stager_launcher_failed","ok":false}',
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PRODUCTION_RELEASE_PARENT",
    "RotationStagerLauncherError",
    "launch_rotation_stager",
]
