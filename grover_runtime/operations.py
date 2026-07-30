"""Fail-closed runtime layout validation and Telegram cutover orchestration.

The module deliberately does not spawn processes.  Every command is a fixed
``tuple[str, ...]`` supplied in :class:`CutoverCommands` and is executed only
through the runner injected by the caller.  Command output is bounded and JSON
is decoded with duplicate-key and non-finite-number rejection.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_MAX_ARGV_ITEMS = 32
_MAX_ARG_BYTES = 4096
_MAX_ARGV_BYTES = 32 * 1024
_MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
_MAX_ENV_VALUE_BYTES = 32 * 1024
_MAX_SHADOW_CREDENTIAL_FILE_BYTES = 64 * 1024

_SAFE_INHERITED_ENVIRONMENT = frozenset({
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TZ",
    "WINDIR",
})


class SafetyViolation(RuntimeError):
    """Raised when a runtime safety invariant cannot be established."""


class CutoverRolledBack(SafetyViolation):
    """Raised after a failed cutover has been verified back on the old runtime."""


@dataclass(frozen=True)
class CommandResult:
    """Bounded, shell-free command result returned by an injected runner."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
    ) -> CommandResult: ...


@dataclass(frozen=True)
class CutoverCommands:
    """Operator-reviewed command vectors used by :func:`execute_cutover`."""

    probe: tuple[str, ...]
    disable_new: tuple[str, ...]
    stop_old: tuple[str, ...]
    enable_new: tuple[str, ...]
    start_new: tuple[str, ...]
    stop_new: tuple[str, ...]
    start_old: tuple[str, ...]
    health_new: tuple[str, ...]
    health_old: tuple[str, ...]
    receipt: tuple[str, ...]


@dataclass(frozen=True)
class CutoverSpec:
    release_id: str
    old_consumer_id: str
    new_consumer_id: str
    commands: CutoverCommands
    prod_home: Path


@dataclass(frozen=True)
class CutoverResult:
    release_id: str
    consumer_id: str
    provider: str
    provider_message_id: str
    already_active: bool


@dataclass(frozen=True)
class RuntimeLayout:
    prod_home: Path
    shadow_home: Path
    release_root: Path


def sanitize_runtime_environment(
    base_environment: Mapping[str, str],
    *,
    profile: str,
    home: Path,
    role: str,
) -> dict[str, str]:
    """Build a minimal runtime environment without inherited credentials.

    An allowlist is used rather than trying to enumerate secret variable names.
    ``HOME`` and ``HERMES_HOME`` are always replaced by the selected clean
    profile home.
    """

    if not isinstance(base_environment, Mapping):
        raise SafetyViolation("base environment must be a mapping")
    _validate_identifier(profile, "profile")
    if role not in {"prod", "shadow"}:
        raise SafetyViolation("runtime role must be 'prod' or 'shadow'")

    home_text = str(Path(home))
    _validate_environment_value(home_text, "runtime home")

    environment: dict[str, str] = {}
    for key in sorted(_SAFE_INHERITED_ENVIRONMENT):
        value = base_environment.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise SafetyViolation(f"environment value for {key} must be text")
        _validate_environment_value(value, key)
        environment[key] = value

    environment.update({
        "GROVER_RUNTIME_ROLE": role,
        "HERMES_HOME": home_text,
        "HERMES_PROFILE": profile,
        "HOME": home_text,
    })
    return environment


def validate_runtime_layout(layout: RuntimeLayout) -> None:
    """Verify that production, shadow, and release storage are disjoint.

    The shadow profile may contain an absent, blank, or comment-only ``.env``;
    any credential-bearing content fails closed.
    """

    if not isinstance(layout, RuntimeLayout):
        raise SafetyViolation("runtime layout has the wrong type")

    labelled_paths = {
        "production profile home": Path(layout.prod_home),
        "shadow profile home": Path(layout.shadow_home),
        "release root": Path(layout.release_root),
    }
    resolved = {
        label: path.resolve(strict=False) for label, path in labelled_paths.items()
    }

    items = list(resolved.items())
    for index, (left_label, left) in enumerate(items):
        for right_label, right in items[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise SafetyViolation(
                    "runtime paths must be disjoint: "
                    f"{left_label} and {right_label} overlap"
                )

    for label, path in labelled_paths.items():
        if not path.exists() or not path.is_dir():
            raise SafetyViolation(f"{label} must be an existing directory")

    shadow_home = labelled_paths["shadow profile home"]
    for credential_name in (".env", "auth.json", "credentials.json"):
        credential_file = shadow_home / credential_name
        if not credential_file.exists():
            continue
        if credential_file.is_symlink() or not credential_file.is_file():
            raise SafetyViolation(
                f"shadow credential file is not empty: {credential_name}"
            )
        try:
            size = credential_file.stat().st_size
        except OSError as exc:
            raise SafetyViolation("cannot inspect shadow credential file") from exc
        if size > _MAX_SHADOW_CREDENTIAL_FILE_BYTES:
            raise SafetyViolation(
                f"shadow credential file is not empty: {credential_name}"
            )
        try:
            text = credential_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SafetyViolation("cannot read shadow credential file safely") from exc

        if credential_name == ".env":
            has_content = any(
                line.strip() and not line.lstrip().startswith("#")
                for line in text.splitlines()
            )
        else:
            has_content = bool(text.strip())
        if has_content:
            raise SafetyViolation(
                f"shadow credential file is not empty: {credential_name}"
            )


def execute_cutover(
    spec: CutoverSpec,
    runner: CommandRunner,
    *,
    base_environment: Mapping[str, str],
) -> CutoverResult:
    """Move one Telegram consumer from the old runtime to the pinned release.

    The normal transition is deliberately explicit: one old consumer, then
    zero consumers, then one new consumer.  Once stopping the old runtime has
    been attempted, every failure invokes the exact rollback sequence encoded
    in :func:`_rollback`.
    """

    _validate_cutover_spec(spec)
    if not callable(runner):
        raise SafetyViolation("command runner must be callable")

    environment = sanitize_runtime_environment(
        base_environment,
        profile="grover-prod",
        home=spec.prod_home,
        role="prod",
    )

    active_consumer = _probe_consumers(
        spec,
        runner,
        environment,
        expected="one-known",
    )
    if active_consumer == spec.new_consumer_id:
        _verify_health(
            spec,
            runner,
            environment,
            command=spec.commands.health_new,
            expected_consumer=spec.new_consumer_id,
            expected_release=spec.release_id,
            label="new runtime",
        )
        receipt = _verify_receipt(spec, runner, environment)
        return _cutover_result(spec, receipt, already_active=True)

    if active_consumer != spec.old_consumer_id:  # defensive; probe already gates
        raise SafetyViolation("probe must report exactly one known Telegram consumer")

    rollback_required = False
    try:
        _run_command(
            "disable new runtime", spec.commands.disable_new, runner, environment
        )
        _probe_consumers(
            spec,
            runner,
            environment,
            expected=(spec.old_consumer_id,),
        )

        # A command can mutate state before reporting a failure, so arm rollback
        # immediately before attempting to stop the old consumer.
        rollback_required = True
        _run_command("stop old runtime", spec.commands.stop_old, runner, environment)
        _probe_consumers(spec, runner, environment, expected=())

        _run_command(
            "enable new runtime", spec.commands.enable_new, runner, environment
        )
        _run_command("start new runtime", spec.commands.start_new, runner, environment)
        _probe_consumers(
            spec,
            runner,
            environment,
            expected=(spec.new_consumer_id,),
        )
        _verify_health(
            spec,
            runner,
            environment,
            command=spec.commands.health_new,
            expected_consumer=spec.new_consumer_id,
            expected_release=spec.release_id,
            label="new runtime",
        )
        receipt = _verify_receipt(spec, runner, environment)
    except SafetyViolation as failure:
        if not rollback_required:
            raise
        try:
            _rollback(spec, runner, environment)
        except SafetyViolation as rollback_failure:
            raise SafetyViolation(
                "cutover failed and rollback could not be verified"
            ) from rollback_failure
        raise CutoverRolledBack(str(failure)) from failure

    return _cutover_result(spec, receipt, already_active=False)


def _cutover_result(
    spec: CutoverSpec,
    receipt: dict[str, Any],
    *,
    already_active: bool,
) -> CutoverResult:
    return CutoverResult(
        release_id=spec.release_id,
        consumer_id=spec.new_consumer_id,
        provider=receipt["provider"],
        provider_message_id=receipt["provider_message_id"],
        already_active=already_active,
    )


def _rollback(
    spec: CutoverSpec,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> None:
    """Restore the old consumer using the non-negotiable rollback order."""

    # Stop/disable commands may mutate state and then report failure. Continue
    # through both attempts and trust only the subsequent independent consumer
    # probe. Never restart the old consumer unless that probe proves zero.
    for label, command in (
        ("stop new runtime", spec.commands.stop_new),
        ("disable new runtime", spec.commands.disable_new),
    ):
        try:
            _run_command(label, command, runner, environment)
        except SafetyViolation:
            pass
    _probe_consumers(spec, runner, environment, expected=())
    _run_command("start old runtime", spec.commands.start_old, runner, environment)
    _probe_consumers(
        spec,
        runner,
        environment,
        expected=(spec.old_consumer_id,),
    )
    _verify_health(
        spec,
        runner,
        environment,
        command=spec.commands.health_old,
        expected_consumer=spec.old_consumer_id,
        expected_release=None,
        label="old runtime",
    )


def _probe_consumers(
    spec: CutoverSpec,
    runner: CommandRunner,
    environment: Mapping[str, str],
    *,
    expected: str | tuple[str, ...],
) -> str | None:
    result = _run_command(
        "Telegram consumer probe", spec.commands.probe, runner, environment
    )
    payload = _strict_json_object(result.stdout, "Telegram consumer probe")
    pollers = payload.get("pollers")
    if not isinstance(pollers, list) or any(
        not isinstance(item, str) or not item for item in pollers
    ):
        raise SafetyViolation("Telegram consumer probe returned an invalid poller list")
    if len(pollers) != len(set(pollers)):
        raise SafetyViolation("probe must report exactly one known Telegram consumer")

    known = {spec.old_consumer_id, spec.new_consumer_id}
    if any(item not in known for item in pollers):
        raise SafetyViolation("probe must report exactly one known Telegram consumer")

    if expected == "one-known":
        if len(pollers) != 1:
            raise SafetyViolation(
                "probe must report exactly one known Telegram consumer"
            )
        return pollers[0]

    if len(pollers) != len(expected) or set(pollers) != set(expected):
        if not expected:
            raise SafetyViolation("Telegram consumer probe must report zero consumers")
        raise SafetyViolation(
            "probe must report exactly one known Telegram consumer: " + expected[0]
        )
    return pollers[0] if pollers else None


def _verify_health(
    spec: CutoverSpec,
    runner: CommandRunner,
    environment: Mapping[str, str],
    *,
    command: tuple[str, ...],
    expected_consumer: str,
    expected_release: str | None,
    label: str,
) -> None:
    result = _run_command(f"{label} health", command, runner, environment)
    try:
        payload = _strict_json_object(result.stdout, f"{label} health")
    except SafetyViolation as exc:
        raise SafetyViolation(f"{label} health gate failed: {exc}") from exc

    healthy = payload.get("healthy") is True
    consumer_matches = payload.get("consumer_id") == expected_consumer
    release_matches = (
        expected_release is None or payload.get("release_id") == expected_release
    )
    if not (healthy and consumer_matches and release_matches):
        raise SafetyViolation(f"{label} health gate failed")


def _verify_receipt(
    spec: CutoverSpec,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    result = _run_command(
        "provider delivery receipt", spec.commands.receipt, runner, environment
    )
    try:
        payload = _strict_json_object(result.stdout, "provider delivery receipt")
    except SafetyViolation as exc:
        raise SafetyViolation(f"provider delivery receipt gate failed: {exc}") from exc

    if payload.get("delivered") is not True:
        raise SafetyViolation("provider delivery receipt gate failed")
    if payload.get("release_id") != spec.release_id:
        raise SafetyViolation("provider delivery receipt release gate failed")

    provider = payload.get("provider")
    identity_source = payload.get("identity_source")
    provider_message_id = payload.get("provider_message_id")
    if (
        provider != "telegram"
        or identity_source != "provider"
        or not isinstance(provider_message_id, str)
        or not provider_message_id.strip()
    ):
        raise SafetyViolation(
            "delivery receipt is missing provider-native message identity"
        )
    return payload


def _run_command(
    label: str,
    argv: tuple[str, ...],
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> CommandResult:
    _validate_argv(argv, label)
    try:
        result = runner(argv, env=dict(environment))
    except Exception as exc:
        raise SafetyViolation(f"{label} command runner failed") from exc

    if not isinstance(result, CommandResult):
        raise SafetyViolation(f"{label} command returned an invalid result")
    if isinstance(result.returncode, bool) or not isinstance(result.returncode, int):
        raise SafetyViolation(f"{label} command returned an invalid exit code")
    for stream_name, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        if not isinstance(stream, str):
            raise SafetyViolation(f"{label} command returned non-text {stream_name}")
        if len(stream.encode("utf-8")) > _MAX_COMMAND_OUTPUT_BYTES:
            raise SafetyViolation(f"{label} command {stream_name} exceeded the limit")
    if result.returncode != 0:
        raise SafetyViolation(
            f"{label} command failed with exit code {result.returncode}"
        )
    return result


def _strict_json_object(text: str, label: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise SafetyViolation(f"{label} did not return text JSON")
    if len(text.encode("utf-8")) > _MAX_COMMAND_OUTPUT_BYTES:
        raise SafetyViolation(f"{label} JSON exceeded the limit")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    try:
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SafetyViolation(f"{label} returned invalid strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SafetyViolation(f"{label} JSON must be an object")
    return payload


def _validate_cutover_spec(spec: CutoverSpec) -> None:
    if not isinstance(spec, CutoverSpec):
        raise SafetyViolation("cutover spec has the wrong type")
    _validate_identifier(spec.release_id, "release id")
    _validate_identifier(spec.old_consumer_id, "old consumer id")
    _validate_identifier(spec.new_consumer_id, "new consumer id")
    if spec.old_consumer_id == spec.new_consumer_id:
        raise SafetyViolation("old and new consumer ids must be different")
    prod_home = Path(spec.prod_home)
    if not prod_home.is_absolute():
        raise SafetyViolation("production runtime home must be absolute")
    _validate_environment_value(str(prod_home), "production runtime home")
    if not isinstance(spec.commands, CutoverCommands):
        raise SafetyViolation("cutover commands have the wrong type")
    for name in (
        "probe",
        "disable_new",
        "stop_old",
        "enable_new",
        "start_new",
        "stop_new",
        "start_old",
        "health_new",
        "health_old",
        "receipt",
    ):
        _validate_argv(getattr(spec.commands, name), name)


def _validate_argv(argv: tuple[str, ...], label: str) -> None:
    if not isinstance(argv, tuple) or not argv:
        raise SafetyViolation(f"{label} must be a non-empty fixed argv tuple")
    if len(argv) > _MAX_ARGV_ITEMS:
        raise SafetyViolation(f"{label} argv has too many items")
    total = 0
    for argument in argv:
        if not isinstance(argument, str) or not argument or "\x00" in argument:
            raise SafetyViolation(f"{label} argv contains an invalid argument")
        size = len(argument.encode("utf-8"))
        if size > _MAX_ARG_BYTES:
            raise SafetyViolation(f"{label} argv argument exceeded the limit")
        total += size + 1
    if total > _MAX_ARGV_BYTES:
        raise SafetyViolation(f"{label} argv exceeded the limit")


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise SafetyViolation(f"{label} is invalid")


def _validate_environment_value(value: str, label: str) -> None:
    if "\x00" in value or len(value.encode("utf-8")) > _MAX_ENV_VALUE_BYTES:
        raise SafetyViolation(f"{label} environment value is invalid")
