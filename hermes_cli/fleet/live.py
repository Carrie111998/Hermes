"""Read-only, fail-closed qualification of live subscription lanes."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .types import AdapterKind, LaneProfile, OverageState, Qualification


_FORBIDDEN_ENV = {
    "chatgpt_codex": ("OPENAI_API_KEY",),
    "claude_code": ("ANTHROPIC_API_KEY",),
    "grok": ("XAI_API_KEY",),
    "antigravity": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _command(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", type(exc).__name__


def _claude_code_oauth_status() -> Mapping[str, object]:
    """Return secret-free evidence for the live Claude Code OAuth record."""

    from agent.anthropic_adapter import (
        _is_oauth_token,
        is_claude_code_token_valid,
        read_claude_code_credentials,
    )

    credentials = read_claude_code_credentials()
    if not isinstance(credentials, dict):
        return {"logged_in": False}
    token = str(credentials.get("accessToken") or "").strip()
    source = str(credentials.get("source") or "").strip()
    return {
        "logged_in": bool(
            source
            and token
            and _is_oauth_token(token)
            and is_claude_code_token_valid(credentials)
        ),
        "auth_mode": "claude_code_oauth",
        "source": source,
    }


class FleetQualificationDoctor:
    """Inspect route identity without returning or persisting credentials."""

    def __init__(
        self,
        *,
        auth_status: Callable[[str], Mapping[str, object]] | None = None,
        claude_oauth_status: Callable[[], Mapping[str, object]] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        command: Callable[[Sequence[str]], tuple[int, str, str]] = _command,
        environment: Mapping[str, str] | None = None,
        billing_status: Callable[[str], Mapping[str, object]] | None = None,
        now: Callable[[], datetime] | None = None,
        platform_name: str | None = None,
    ) -> None:
        if auth_status is None:
            from hermes_cli.auth import get_auth_status

            auth_status = get_auth_status
        self.auth_status = auth_status
        self.claude_oauth_status = (
            claude_oauth_status or _claude_code_oauth_status
        )
        self.which = which
        self.command = command
        self.environment = dict(os.environ if environment is None else environment)
        self.billing_status = billing_status
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.platform_name = os.name if platform_name is None else platform_name

    def _failed(
        self, profile: LaneProfile, detail: str, *, executable: str | None = None
    ) -> Qualification:
        at = self.now().astimezone(timezone.utc)
        return Qualification(
            qualified=False,
            captured_at=at,
            expires_at=at + timedelta(minutes=5),
            auth_kind=None,
            auth_source=None,
            overage_disabled=None,
            provider_id=profile.provider_id,
            models=(),
            efforts=(),
            fast_off_supported=False,
            capabilities=frozenset(),
            executable=executable,
            evidence_id=f"live-doctor:{profile.lane_id}:not-qualified",
            detail=detail,
        )

    def _external_receipt(
        self, profile: LaneProfile, executable: str
    ) -> tuple[str | None, tuple[str, ...], str | None]:
        command_name = executable
        version_code, version_out, _ = self.command((command_name, "--version"))
        if version_code != 0 or not version_out.strip():
            return None, (), "version command failed"
        code, stdout, _ = self.command((command_name, "models"))
        if code != 0:
            return None, (), "agy models command failed"
        listed_tokens = frozenset(
            re.findall(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", stdout)
        )
        qualified_models = tuple(
            model for model in profile.ordered_models if model in listed_tokens
        )
        if not qualified_models:
            return None, (), "required exact model absent from agy models"
        return version_out.strip().splitlines()[0], qualified_models, None

    def _external_executable(self, profile: LaneProfile) -> str | None:
        executable = self.which(profile.executable or "")
        if executable:
            return str(Path(executable).resolve())
        if (
            self.platform_name == "nt"
            and profile.lane_id == "antigravity"
            and self.environment.get("LOCALAPPDATA")
        ):
            candidate = (
                Path(self.environment["LOCALAPPDATA"])
                / "agy"
                / "bin"
                / "agy.exe"
            ).resolve()
            if candidate.is_file():
                return str(candidate)
        return None

    def qualify(self, profiles: Iterable[LaneProfile]) -> dict[str, Qualification]:
        result: dict[str, Qualification] = {}
        for profile in profiles:
            if not profile.implemented:
                result[profile.lane_id] = self._failed(
                    profile, "adapter is not implemented"
                )
                continue
            forbidden = next(
                (name for name in _FORBIDDEN_ENV.get(profile.lane_id, ()) if self.environment.get(name)),
                None,
            )
            if forbidden:
                result[profile.lane_id] = self._failed(
                    profile, f"forbidden API-key environment variable present: {forbidden}"
                )
                continue
            at = self.now().astimezone(timezone.utc)
            executable = None
            version = None
            models = profile.ordered_models
            auth_source = None
            auth_kind = "oauth_subscription"
            if profile.adapter_kind is AdapterKind.NATIVE_PROVIDER:
                if profile.provider_id == "anthropic":
                    status = self.claude_oauth_status()
                    expected_mode = "claude_code_oauth"
                else:
                    status = self.auth_status(profile.provider_id)
                    expected_mode = (
                        "chatgpt"
                        if profile.provider_id == "openai-codex"
                        else "oauth_device_code"
                    )
                if status.get("logged_in") is not True or status.get("auth_mode") != expected_mode:
                    result[profile.lane_id] = self._failed(
                        profile, f"{profile.provider_id} subscription OAuth is not proven"
                    )
                    continue
                source = status.get("source")
                if not isinstance(source, str) or not source:
                    result[profile.lane_id] = self._failed(
                        profile, f"{profile.provider_id} auth source is not attributable"
                    )
                    continue
                # Credential-pool aliases (for example ``pool:...``) and the
                # runtime resolver's source label (for example
                # ``manual:device_code``) describe the same OAuth credential
                # through different layers.  Bind execution to the stable,
                # provider-scoped subscription identity after both layers have
                # independently proven their attributable source.
                auth_source = (
                    "anthropic:claude_code_oauth"
                    if profile.provider_id == "anthropic"
                    else f"{profile.provider_id}:oauth_subscription"
                )
            else:
                executable = self._external_executable(profile)
                if not executable:
                    result[profile.lane_id] = self._failed(
                        profile, f"executable not found: {profile.executable}"
                    )
                    continue
                version, models, error = self._external_receipt(
                    profile, executable
                )
                if error:
                    result[profile.lane_id] = self._failed(
                        profile, error, executable=executable
                    )
                    continue
                auth_kind = "cli_subscription"
                auth_source = "antigravity:agy-models-policy"
            if profile.lane_id == "claude_code":
                policy_detail = (
                    "observed evidence: live Claude Code OAuth credential, exact "
                    "native Anthropic route, and forbidden billable API-key env absent; "
                    "provider overage state requires separate billing telemetry"
                )
            elif profile.lane_id == "antigravity":
                policy_detail = (
                    "policy evidence: agy executable/version plus exact model "
                    "qualification from `agy models` and forbidden billable API-key "
                    "env absent; parent session remains unproven until a strict two-turn "
                    "canary verifies stable identity through --conversation and --continue, "
                    "--remote-control execution, and exact provider-reported served-model "
                    "evidence; provider overage state requires separate billing telemetry"
                )
            else:
                policy_detail = (
                    f"policy evidence: {profile.provider_id} subscription OAuth "
                    "route and forbidden billable API-key env absent; "
                    "provider overage state requires separate billing telemetry"
                )
            billing = (
                dict(self.billing_status(profile.lane_id))
                if self.billing_status is not None
                else {}
            )
            try:
                overage_state = OverageState(
                    str(billing.get("overage_state", OverageState.UNKNOWN.value))
                )
            except ValueError:
                overage_state = OverageState.UNKNOWN
            result[profile.lane_id] = Qualification(
                qualified=True,
                captured_at=at,
                expires_at=at + timedelta(minutes=5),
                auth_kind=auth_kind,
                auth_source=auth_source,
                overage_disabled=overage_state is OverageState.OFF,
                provider_id=profile.provider_id,
                models=models,
                efforts=profile.supported_efforts,
                fast_off_supported=True,
                capabilities=profile.capabilities,
                executable=str(Path(executable).resolve()) if executable else None,
                version=version,
                evidence_id=f"live-doctor:{profile.lane_id}:{at.isoformat()}",
                detail=policy_detail,
                subscription_only_proven=True,
                paid_fallback_absent=True,
                overage_state=overage_state,
            )
        return result
