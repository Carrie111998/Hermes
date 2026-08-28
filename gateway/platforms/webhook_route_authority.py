"""Route responsibilities for the webhook adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shutil
import weakref
from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType
from typing import Any, List, Mapping, Optional

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore[assignment]

from gateway.config import Platform
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookRouteConfig,
)
from gateway.platforms.webhook_filters import (
    BoundedFileSnapshotTooLarge,
    MAX_FILTER_FILE_SNAPSHOT_BYTES,
    WebhookPreparedScript,
    _resolve_profile_path,
    read_bounded_regular_file_snapshot,
)
from gateway.platforms.webhook_ledger import (
    MAXIMUM_RECOVERY_PROFILES,
    OperationAuthority,
    WebhookLedgerError,
)

from gateway.platforms.webhook_common import (
    AuthenticatedRouteAuthority,
    PreparedSkillInvocation,
    PreparedTargetTemplate,
    _INSECURE_NO_AUTH,
    _MAX_DYNAMIC_ROUTES_FILE_BYTES,
    _PROMPT_TOKEN_RE,
    _authentication_key_fingerprints,
    _is_loopback_host,
    _plain_json_snapshot,
    _profile_incarnation_token,
    _reject_nonfinite_json,
    _route_policy_sha256,
    _snapshot_route_config,
)

logger = logging.getLogger(__name__)


class WebhookRouteAuthorityMixin:
    def _intake_is_authoritative(self, profile: str) -> bool:
        """Return whether this exact shared listener may admit requests."""

        if not self._accepting_webhooks:
            return False
        runner = self.gateway_runner
        if runner is None:
            # Standalone adapter use and direct handler tests have no registry.
            return True
        if (
            getattr(runner, "_startup_restore_in_progress", False) is True
            or getattr(runner, "_draining", False) is True
            or getattr(runner, "_external_drain_active", False) is True
            or getattr(runner, "_running", True) is False
        ):
            return False
        shutdown_event = getattr(runner, "_shutdown_event", None)
        shutdown_is_set = getattr(shutdown_event, "is_set", None)
        if callable(shutdown_is_set) and shutdown_is_set() is True:
            return False
        # Webhook is a port-binding platform. Multiplexing deliberately starts
        # one process-level listener in ``runner.adapters`` and routes named
        # profiles through its /p/<profile>/ prefix; secondary profile maps may
        # never contain another webhook adapter. Profile-specific authorization
        # is enforced by the bound route and runtime scope after this transport
        # ownership check.
        del profile
        return (getattr(runner, "adapters", None) or {}).get(Platform.WEBHOOK) is self

    def prepare_ledger_owned_final_content(
        self,
        content: str,
        *,
        session_key: str,
    ) -> str:
        """Reduce one agent response to a replayable text-only carrier.

        Local attachments cannot be snapshotted as stable outbound objects:
        the referenced files can change or disappear before crash recovery.
        Strip those directives before staging and include one non-sensitive
        notice in the same exact final text. Public image links remain ordinary
        text and are delivered without invoking a second media API.
        """

        del session_key
        media_files, cleaned = self.extract_media(content)
        local_files, cleaned = self.extract_local_files(cleaned)
        if media_files or local_files:
            notice = "⚠️ Local attachments were omitted from webhook delivery."
            cleaned = f"{cleaned.rstrip()}\n\n{notice}" if cleaned.strip() else notice
        return cleaned or "[SILENT]"

    def resolved_toolsets_for_source(self, source) -> List[str]:
        """Return the already-validated durable grant, or deny all."""

        chat_id = str(getattr(source, "chat_id", "") or "")
        if not chat_id:
            return []
        try:
            authority = self._operation_ledger.lookup_session(chat_id)
        except Exception:
            logger.exception(
                "[webhook] Failed to resolve durable toolset authority for %s",
                chat_id,
            )
            return []
        grant = authority.grant_snapshot if authority is not None else None
        if not isinstance(grant, Mapping) or grant.get("v") != 1:
            return []
        toolsets = grant.get("toolsets")
        if not isinstance(toolsets, (list, tuple)):
            return []
        if any(not isinstance(value, str) or not value for value in toolsets):
            return []
        return list(toolsets)

    def toolsets_for_source(self, source) -> Optional[List[str]]:
        """Compatibility facade for callers predating resolved grant carriers."""

        return self.resolved_toolsets_for_source(source)

    @staticmethod
    def _source_for_route_authority(
        bound_route: WebhookRouteConfig,
        *,
        authority_profile: Optional[str] = None,
    ):
        """Build the profile-stamped source used for config publication."""

        from gateway.session import SessionSource

        return SessionSource(
            platform=Platform.WEBHOOK,
            chat_id=(f"webhook-policy/{bound_route.profile}/{bound_route.name}"),
            chat_name=f"webhook/{bound_route.name}",
            chat_type="webhook",
            user_id=f"webhook:{bound_route.name}",
            user_name=bound_route.name,
            profile=authority_profile or bound_route.profile,
        )

    def _credential_authority_profile(
        self,
        bound_route: WebhookRouteConfig,
    ) -> str:
        """Return the real profile domain behind a canonical default route."""

        if bound_route.profile != "default":
            return bound_route.profile
        runner_config = getattr(self.gateway_runner, "config", None)
        if getattr(runner_config, "multiplex_profiles", False) is True:
            return "default"
        try:
            from hermes_cli.profiles import get_active_profile_name

            active_profile = get_active_profile_name()
        except Exception:
            active_profile = "default"
        if active_profile == "custom":
            # A custom HERMES_HOME is the installation root, not a named
            # profile below ``profiles/``.  Its physical root already scopes
            # the shared ledger, and runner profile resolution recognizes the
            # base authority as ``default``.
            return "default"
        return active_profile or "default"

    def _validate_route_profile_reachable(
        self,
        route: Mapping[str, Any],
        bound_route: WebhookRouteConfig,
    ) -> None:
        """Reject unreachable explicit profile authority before key binding."""

        if "profile" not in route:
            return
        runner_config = getattr(self.gateway_runner, "config", None)
        if getattr(runner_config, "multiplex_profiles", False) is True:
            try:
                from hermes_cli.profiles import profiles_to_serve

                served = {
                    name
                    for name, _home in profiles_to_serve(
                        multiplex=True,
                        profile_allowlist=getattr(
                            runner_config,
                            "multiplex_profile_allowlist",
                            None,
                        ),
                    )
                }
            except Exception as exc:
                raise WebhookContractError(
                    "served webhook profile authority cannot be resolved"
                ) from exc
            if bound_route.profile not in served:
                raise WebhookContractError(
                    f"route {bound_route.name!r} profile "
                    f"{bound_route.profile!r} is not served"
                )
            return
        if bound_route.profile == "default":
            # Canonical default is also the omitted-profile spelling. In a
            # named single-profile process its credential owner and physical
            # generation are deliberately mapped to that active profile.
            return
        try:
            from hermes_cli.profiles import profile_matches_home

            matches = profile_matches_home(bound_route.profile)
        except Exception as exc:
            raise WebhookContractError(
                "active webhook profile authority cannot be resolved"
            ) from exc
        if not matches:
            raise WebhookContractError(
                f"route {bound_route.name!r} profile "
                f"{bound_route.profile!r} is not this gateway's profile"
            )

    def _profile_authority_generation(
        self,
        bound_route: WebhookRouteConfig,
        *,
        authority_profile: str,
    ) -> str:
        """Bind keys to one physical profile incarnation, not only its name."""

        return self._current_profile_authority_generation(
            authority_profile,
            route_name=bound_route.name,
        )

    def _current_profile_authority_generation(
        self,
        authority_profile: str,
        *,
        route_name: str,
    ) -> str:
        """Return the current incarnation for one physical profile domain."""

        from gateway.session import SessionSource

        source = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id=f"webhook-policy/{authority_profile}/{route_name}",
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
            profile=authority_profile,
        )

        runner_config = getattr(self.gateway_runner, "config", None)
        multiplexed = getattr(runner_config, "multiplex_profiles", False) is True
        resolver = getattr(
            self.gateway_runner,
            "_resolve_profile_home_for_source",
            None,
        )
        if multiplexed:
            if callable(resolver):
                profile_home = Path(resolver(source))
            else:
                # Narrow compatibility for publication-focused test doubles;
                # a real multiplex GatewayRunner always owns the resolver.
                from hermes_constants import get_hermes_home

                profile_home = get_hermes_home()
        else:
            # In a named single-profile process, an omitted route profile is
            # canonically stamped "default" for URL routing, but its physical
            # authority is the process launch HERMES_HOME. Asking the runner to
            # resolve that synthetic stamp can incorrectly select the root
            # default profile instead of the named profile actually executing.
            from hermes_constants import get_hermes_home

            profile_home = get_hermes_home()
        try:
            resolved = profile_home.resolve(strict=True)
        except OSError as exc:
            raise WebhookContractError(
                f"profile home for route {route_name!r} is unavailable"
            ) from exc
        incarnation = _profile_incarnation_token(resolved)
        material = f"{resolved}\0{incarnation}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _prepared_target_authority(
        prepared: PreparedTargetTemplate,
    ) -> dict[str, Any]:
        """Return canonical JSON authority for one frozen target template."""

        return {
            "kind": prepared.kind,
            "profile": prepared.profile,
            "platform": prepared.platform,
            "home_chat_id": prepared.home_chat_id,
            "home_thread_id": prepared.home_thread_id,
            "home_scope_id": prepared.home_scope_id,
            "slack_static_chat_id": prepared.slack_static_chat_id,
            "slack_static_scope_id": prepared.slack_static_scope_id,
            "slack_scope_locked": prepared.slack_scope_locked,
        }

    def _prepare_route_filter_authority(
        self,
        route: dict[str, Any],
        bound_route: WebhookRouteConfig,
        *,
        authority_profile: str,
    ) -> tuple[dict[str, Any], tuple[tuple[str, str], ...]]:
        """Replace every mutable in_file lookup with exact captured values."""

        if "filters" not in route:
            return _snapshot_route_config(route), ()

        source = self._source_for_route_authority(
            bound_route,
            authority_profile=authority_profile,
        )
        captured: dict[str, tuple[list[Any], str, str]] = {}

        def capture(path_value: Any) -> tuple[list[Any], str, str]:
            path = _resolve_profile_path(path_value)
            if path is None:
                raise WebhookContractError("webhook filter in_file path is invalid")
            try:
                resolved = path.resolve(strict=True)
                raw = read_bounded_regular_file_snapshot(
                    resolved,
                    max_bytes=MAX_FILTER_FILE_SNAPSHOT_BYTES,
                ).content
            except BoundedFileSnapshotTooLarge as exc:
                raise WebhookContractError(
                    "webhook filter in_file exceeds the 1 MiB authority limit"
                ) from exc
            except (OSError, ValueError) as exc:
                raise WebhookContractError(
                    f"webhook filter in_file is unavailable: {path}"
                ) from exc
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WebhookContractError(
                    "webhook filter in_file must be UTF-8"
                ) from exc
            try:
                data = json.loads(text)
            except RecursionError as exc:
                raise WebhookContractError(
                    "webhook filter in_file JSON nesting is too deep"
                ) from exc
            except json.JSONDecodeError:
                values: list[Any] = [
                    line.strip() for line in text.splitlines() if line.strip()
                ]
            except ValueError as exc:
                raise WebhookContractError(
                    "webhook filter in_file contains an invalid JSON value"
                ) from exc
            else:
                if isinstance(data, list):
                    values = data
                elif isinstance(data, dict):
                    values = list(data.keys())
                else:
                    values = [data]
            # The frozen evaluator and the route-policy digest both require
            # finite, detached JSON. This also rejects NaN/Infinity accepted
            # by Python's permissive json.loads default.
            try:
                values = json.loads(
                    json.dumps(
                        values,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ),
                    parse_constant=_reject_nonfinite_json,
                )
            except (
                TypeError,
                ValueError,
                json.JSONDecodeError,
                RecursionError,
            ) as exc:
                raise WebhookContractError(
                    "webhook filter in_file must contain finite JSON values"
                ) from exc
            digest = hashlib.sha256(raw).hexdigest()
            return values, str(resolved), digest

        def freeze_filter_node(value: Any, *, root_list: bool = False) -> Any:
            if root_list and isinstance(value, list):
                return [freeze_filter_node(item) for item in value]
            if not isinstance(value, dict):
                return _plain_json_snapshot(value)
            # Only recurse through filter grammar edges. Operand JSON under
            # equals/contains/in may legitimately contain a literal key named
            # "in_file" and must never be interpreted as another filter node.
            frozen = _plain_json_snapshot(value)
            for operator in ("all", "any"):
                children = value.get(operator)
                if isinstance(children, list):
                    frozen[operator] = [freeze_filter_node(item) for item in children]
            nested = value.get("not")
            if isinstance(nested, dict):
                frozen["not"] = freeze_filter_node(nested)
            if "in_file" not in value:
                return frozen
            if "in" in value:
                raise WebhookContractError(
                    "webhook filter cannot combine in and in_file"
                )
            path_key = json.dumps(value.get("in_file"), ensure_ascii=False)
            cached = captured.get(path_key)
            if cached is None:
                values, resolved_path, digest = capture(value.get("in_file"))
                captured[path_key] = (values, resolved_path, digest)
            else:
                values, resolved_path, digest = cached
            frozen.pop("in_file", None)
            frozen["in"] = _plain_json_snapshot(values)
            return frozen

        try:
            with self._profile_runtime_context(source):
                filters = route.get("filters")
                frozen_filters = freeze_filter_node(
                    filters,
                    root_list=isinstance(filters, list),
                )
            frozen_route = _snapshot_route_config(route)
        except RecursionError as exc:
            raise WebhookContractError(
                "webhook filter authority nesting is too deep"
            ) from exc
        frozen_route["filters"] = frozen_filters
        authority_items: list[tuple[str, str]] = []
        for values, resolved_path, digest in captured.values():
            del values
            authority_items.append((resolved_path, digest))
        return frozen_route, tuple(sorted(authority_items))

    def _prepare_route_skill_authority(
        self,
        route: Mapping[str, Any],
        bound_route: WebhookRouteConfig,
        *,
        authority_profile: str,
    ) -> Optional[PreparedSkillInvocation]:
        """Capture the exact expanded scaffold for the first available skill."""

        skills = route.get("skills", [])
        if skills is None:
            skills = []
        if not isinstance(skills, list) or any(
            not isinstance(name, str) or not name.strip() for name in skills
        ):
            raise WebhookContractError(
                "webhook route skills must be a list of non-empty names"
            )
        if not skills:
            return None

        source = self._source_for_route_authority(
            bound_route,
            authority_profile=authority_profile,
        )
        marker_seed = hashlib.sha256(
            f"{authority_profile}\0{bound_route.name}".encode("utf-8")
        ).hexdigest()
        marker = f"__HERMES_WEBHOOK_PROMPT_{marker_seed}__"
        with self._profile_runtime_context(source):
            from agent.skill_commands import (
                build_skill_invocation_message,
                get_skill_commands,
            )

            commands = get_skill_commands()
            for skill_name in skills:
                command = f"/{skill_name.strip()}"
                if command not in commands:
                    continue
                rendered = build_skill_invocation_message(
                    command,
                    user_instruction=marker,
                )
                if not rendered:
                    continue
                if not isinstance(rendered, str):
                    raise WebhookContractError(
                        f"webhook skill {command!r} cannot be frozen safely"
                    )
                if marker in rendered:
                    prefix, suffix = rendered.rsplit(marker, 1)
                    inject_prompt = True
                else:
                    # Compatibility with a fixed-output/custom builder: its
                    # exact output is still immutable authority, it simply
                    # elects not to include the caller's rendered prompt.
                    prefix, suffix = rendered, ""
                    inject_prompt = False
                return PreparedSkillInvocation(
                    command=command,
                    prefix=prefix,
                    suffix=suffix,
                    source_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                    inject_prompt=inject_prompt,
                )
        raise WebhookContractError(
            f"route {bound_route.name!r} has no available configured skill"
        )

    def _effective_toolsets_for_route_authority(
        self,
        route: Mapping[str, Any],
        bound_route: WebhookRouteConfig,
        *,
        authority_profile: str,
    ) -> tuple[str, ...]:
        """Resolve the immutable grant joined to one credential policy."""

        deliver_only = route.get("deliver_only", False)
        if not isinstance(deliver_only, bool):
            raise WebhookContractError(
                f"route {bound_route.name!r} has invalid deliver_only"
            )
        if deliver_only:
            return ()
        if self.gateway_runner is not None and (
            not hasattr(self.gateway_runner, "config")
            or not callable(
                getattr(
                    self.gateway_runner,
                    "_resolve_profile_home_for_source",
                    None,
                )
            )
        ):
            # Legacy lifecycle doubles have no configuration authority at all;
            # bind them to an explicit deny-all grant rather than broadening
            # through process-global defaults.
            return ()

        source = self._source_for_route_authority(
            bound_route,
            authority_profile=authority_profile,
        )
        resolver = self._resolve_admitted_toolsets
        if (
            getattr(resolver, "__func__", None)
            is WebhookRouteAuthorityMixin._resolve_admitted_toolsets
        ):
            return tuple(resolver(route, source, strict_config=True))
        # Compatibility for narrow test/embedding overrides that provide an
        # already-strict grant resolver with the historical two-argument API.
        return tuple(resolver(route, source))

    def _profile_runtime_context(self, source: Any):
        runner = self.gateway_runner
        config = getattr(runner, "config", None)
        if getattr(config, "multiplex_profiles", False) is not True:
            return nullcontext()
        resolver = getattr(runner, "_resolve_profile_home_for_source", None)
        if not callable(resolver):
            raise WebhookContractError(
                "multiplexed webhook profile resolver is unavailable"
            )
        from gateway.run import _profile_runtime_scope

        return _profile_runtime_scope(resolver(source))

    def _route_authentication_authority_snapshot(
        self,
        routes: Mapping[str, Any],
        *,
        prepared_skill_overrides: Optional[
            Mapping[str, PreparedSkillInvocation]
        ] = None,
    ) -> tuple[
        tuple[Any, ...],
        list[tuple[str, str, str, str, str, str]],
        Mapping[str, tuple[Any, ...]],
        Mapping[str, tuple[str, ...]],
        Mapping[str, Optional[WebhookPreparedScript]],
        Mapping[str, str],
        Mapping[str, AuthenticatedRouteAuthority],
    ]:
        """Validate a complete route set and build its durable key bindings."""

        material_owners: dict[str, tuple[str, str, str, str]] = {}
        bindings: list[tuple[str, str, str, str, str, str]] = []
        snapshot: list[tuple[Any, ...]] = []
        authorities: dict[str, tuple[Any, ...]] = {}
        effective_toolsets_by_route: dict[str, tuple[str, ...]] = {}
        prepared_scripts: dict[str, Optional[WebhookPreparedScript]] = {}
        profile_generations: dict[str, str] = {}
        observed_profile_generations: dict[str, str] = {}
        bundles: dict[str, AuthenticatedRouteAuthority] = {}
        authority_profiles: set[str] = set()
        for route_name in sorted(routes):
            route = routes[route_name]
            if not isinstance(route, Mapping):
                raise WebhookContractError(
                    f"route {route_name!r} configuration must be an object"
                )
            route_snapshot = _snapshot_route_config(dict(route))
            bound_route = WebhookRouteConfig.bind(
                route_name,
                route_snapshot,
                headers={},
                request_profile=route_snapshot.get("profile", "default"),
            )
            self._validate_route_profile_reachable(route_snapshot, bound_route)
            secret = route_snapshot.get("secret", self._global_secret)
            if not isinstance(secret, str) or not secret:
                raise WebhookContractError(f"route {route_name!r} has no HMAC secret")
            if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
                raise WebhookContractError(
                    f"route {route_name!r} uses INSECURE_NO_AUTH on a non-loopback host"
                )
            deliver_only = route_snapshot.get("deliver_only", False)
            if not isinstance(deliver_only, bool):
                raise WebhookContractError(
                    f"route {route_name!r} has invalid deliver_only"
                )
            if deliver_only:
                deliver = route_snapshot.get("deliver", "log")
                if not isinstance(deliver, str) or not deliver or deliver == "log":
                    raise WebhookContractError(
                        f"route {route_name!r} has deliver_only=true without "
                        "a real delivery target"
                    )
            owner = (
                self._credential_authority_profile(bound_route),
                bound_route.name,
                bound_route.provider,
                bound_route.signature_mode,
            )
            authority_profiles.add(owner[0])
            if len(authority_profiles) > MAXIMUM_RECOVERY_PROFILES:
                raise WebhookContractError(
                    "webhook routes exceed the supported physical profile limit"
                )
            profile_generation_before = self._profile_authority_generation(
                bound_route,
                authority_profile=owner[0],
            )
            effective_toolsets = self._effective_toolsets_for_route_authority(
                route_snapshot,
                bound_route,
                authority_profile=owner[0],
            )
            effective_toolsets_by_route[route_name] = effective_toolsets
            prepared_script: Optional[WebhookPreparedScript] = None
            if route_snapshot.get("script"):
                source = self._source_for_route_authority(
                    bound_route,
                    authority_profile=owner[0],
                )
                with self._profile_runtime_context(source):
                    prepared_script, script_error = (
                        self._route_processor.prepare_route_script(
                            route_snapshot.get("script")
                        )
                    )
                if prepared_script is None:
                    raise WebhookContractError(
                        f"route {route_name!r} script is unavailable: "
                        f"{script_error or 'unknown error'}"
                    )
            prepared_scripts[route_name] = prepared_script
            script_sha256 = (
                prepared_script.execution_sha256
                if prepared_script is not None
                else None
            )
            filter_route, filter_authority = self._prepare_route_filter_authority(
                route_snapshot,
                bound_route,
                authority_profile=owner[0],
            )
            prepared_skill = (
                prepared_skill_overrides.get(route_name)
                if prepared_skill_overrides is not None
                and route_name in prepared_skill_overrides
                else self._prepare_route_skill_authority(
                    route_snapshot,
                    bound_route,
                    authority_profile=owner[0],
                )
            )
            source = self._source_for_route_authority(
                bound_route,
                authority_profile=owner[0],
            )
            with self._profile_runtime_context(source):
                prepared_target = self._preflight_target_template(
                    profile=owner[0],
                    deliver=route_snapshot.get("deliver", "log"),
                    deliver_extra=route_snapshot.get("deliver_extra", {}),
                )
            profile_generation = self._profile_authority_generation(
                bound_route,
                authority_profile=owner[0],
            )
            if not secrets.compare_digest(
                profile_generation_before,
                profile_generation,
            ):
                raise WebhookContractError(
                    f"route {route_name!r} profile authority changed while "
                    "execution dependencies were snapshotted"
                )
            observed_profile_generation = observed_profile_generations.get(owner[0])
            if observed_profile_generation is not None and not secrets.compare_digest(
                observed_profile_generation,
                profile_generation,
            ):
                raise WebhookContractError(
                    f"profile {owner[0]!r} authority changed while the route set "
                    "was snapshotted"
                )
            observed_profile_generations[owner[0]] = profile_generation
            profile_generations[route_name] = profile_generation
            policy_sha256 = _route_policy_sha256(
                route_snapshot,
                owner[0],
                effective_toolsets,
                script_sha256,
                profile_generation,
                filter_authority,
                (prepared_skill.source_sha256 if prepared_skill is not None else None),
                self._prepared_target_authority(prepared_target),
            )
            if secret == _INSECURE_NO_AUTH:
                authority = (*owner, policy_sha256, ("local-bypass",))
                snapshot.append(authority)
                authorities[route_name] = authority
                bundles[route_name] = AuthenticatedRouteAuthority(
                    authority=authority,
                    secret=secret,
                    route_config=route_snapshot,
                    filter_route_config=filter_route,
                    effective_toolsets=effective_toolsets,
                    prepared_script=prepared_script,
                    prepared_skill=prepared_skill,
                    prepared_target=prepared_target,
                    profile_generation=profile_generation,
                )
                continue

            fingerprints = tuple(
                sorted(
                    fingerprint.hex()
                    for fingerprint in _authentication_key_fingerprints(
                        secret,
                        bound_route.signature_mode,
                    )
                )
            )
            for fingerprint in fingerprints:
                prior_owner = material_owners.get(fingerprint)
                if prior_owner is not None and prior_owner != owner:
                    prior_scope = f"{prior_owner[0]}/{prior_owner[1]}"
                    current_scope = f"{owner[0]}/{owner[1]}"
                    raise WebhookContractError(
                        "authenticated webhook routes must not reuse secret "
                        f"material across authority scopes {prior_scope!r} "
                        f"and {current_scope!r}"
                    )
                material_owners[fingerprint] = owner
                bindings.append((fingerprint, *owner, policy_sha256))
            authority = (*owner, policy_sha256, fingerprints)
            snapshot.append(authority)
            authorities[route_name] = authority
            bundles[route_name] = AuthenticatedRouteAuthority(
                authority=authority,
                secret=secret,
                route_config=route_snapshot,
                filter_route_config=filter_route,
                effective_toolsets=effective_toolsets,
                prepared_script=prepared_script,
                prepared_skill=prepared_skill,
                prepared_target=prepared_target,
                profile_generation=profile_generation,
            )
        return (
            tuple(snapshot),
            bindings,
            MappingProxyType(authorities),
            MappingProxyType(effective_toolsets_by_route),
            MappingProxyType(prepared_scripts),
            MappingProxyType(profile_generations),
            MappingProxyType(bundles),
        )

    def _bind_route_authentication_authorities(
        self,
        routes: Mapping[str, Any],
    ) -> tuple[Any, ...]:
        """Publish one complete route-key snapshot to durable authority."""

        (
            snapshot,
            bindings,
            authorities,
            effective_toolsets,
            prepared_scripts,
            profile_generations,
            bundles,
        ) = self._route_authentication_authority_snapshot(routes)
        if snapshot == self._authenticated_route_snapshot:
            self._prune_rate_limit_buckets(self._authenticated_route_bundles)
            return snapshot
        self._authentication_authority_ledger.bind_authentication_keys(bindings)
        self._authenticated_route_snapshot = snapshot
        self._authenticated_route_authorities = authorities
        self._authenticated_route_effective_toolsets = effective_toolsets
        self._authenticated_route_scripts = prepared_scripts
        self._authenticated_route_profile_generations = profile_generations
        # Publish last: this single reference swap is the request-facing
        # generation token and makes the complete bundle registry atomic.
        self._authenticated_route_bundles = bundles
        self._prune_rate_limit_buckets(bundles)
        return snapshot

    def _route_owns_unique_authenticated_secret(
        self,
        route_name: str,
        secret: str,
        signature_mode: str,
        bundle: Optional[AuthenticatedRouteAuthority] = None,
    ) -> bool:
        """Defensively reject unbound or reassigned keys after config mutation."""

        try:
            if self._authenticated_route_snapshot is None:
                # Compatibility for tests/embedders that exercise the handler
                # without connect(). Production listeners bind before opening.
                self._bind_route_authentication_authorities(self._routes)
            bundle = bundle or self._authenticated_route_bundles.get(route_name)
            expected = bundle.authority if bundle is not None else None
            route = self._routes.get(route_name)
            if bundle is None or expected is None or not isinstance(route, Mapping):
                return False
            if bundle.secret != secret:
                return False
            live_snapshot = _snapshot_route_config(dict(route))
            if live_snapshot != bundle.route_config:
                return False
            bound_route = WebhookRouteConfig.bind(
                route_name,
                bundle.route_config,
                headers={},
                request_profile=bundle.route_config.get("profile", "default"),
            )
            if bound_route.signature_mode != signature_mode:
                return False
            fingerprints: tuple[str, ...]
            if secret == _INSECURE_NO_AUTH:
                fingerprints = ("local-bypass",)
            else:
                fingerprints = tuple(
                    sorted(
                        fingerprint.hex()
                        for fingerprint in _authentication_key_fingerprints(
                            secret,
                            signature_mode,
                        )
                    )
                )
            observed = (
                self._credential_authority_profile(bound_route),
                bound_route.name,
                bound_route.provider,
                bound_route.signature_mode,
                expected[4],
                fingerprints,
            )
        except (WebhookContractError, WebhookLedgerError):
            return False
        return observed == expected

    def _live_route_authority_matches(
        self,
        route_name: str,
        bundle: AuthenticatedRouteAuthority,
    ) -> bool:
        """Revalidate mutable profile grants/code against the bound snapshot."""

        try:
            _, _, authorities, _, _, _, _ = (
                self._route_authentication_authority_snapshot(
                    {
                        route_name: bundle.route_config,
                    },
                    prepared_skill_overrides=(
                        {route_name: bundle.prepared_skill}
                        if bundle.prepared_skill is not None
                        and not bundle.prepared_skill.inject_prompt
                        else None
                    ),
                )
            )
        except Exception:
            return False
        return authorities.get(route_name) == bundle.authority

    def _route_bundle_is_current(
        self,
        route_name: str,
        bundle: AuthenticatedRouteAuthority,
    ) -> bool:
        """Check the request's exact registry object, not merely its name."""

        return (
            self._authenticated_route_bundles.get(route_name) is bundle
            and route_name in self._routes
        )

    @staticmethod
    def _route_bundle_authentication_bindings(
        bundle: AuthenticatedRouteAuthority,
    ) -> tuple[tuple[str, str, str, str, str, str], ...]:
        """Project one immutable bundle into its exact durable key proofs."""

        profile, route, provider, signature_mode, policy_sha256, fingerprints = (
            bundle.authority
        )
        if fingerprints == ("local-bypass",):
            return ()
        return tuple(
            (
                fingerprint,
                profile,
                route,
                provider,
                signature_mode,
                policy_sha256,
            )
            for fingerprint in fingerprints
        )

    def _withdraw_live_route(
        self,
        route_name: str,
        expected: Optional[AuthenticatedRouteAuthority] = None,
    ) -> bool:
        """Fence one route in memory until a valid rotated snapshot publishes."""

        if expected is not None and (
            self._authenticated_route_bundles.get(route_name) is not expected
        ):
            return False
        self._dynamic_routes.pop(route_name, None)
        self._routes.pop(route_name, None)
        self._prune_rate_limit_buckets()
        return True

    def _resolve_admitted_toolsets(
        self,
        route_config: Mapping[str, Any],
        source: Any,
        *,
        strict_config: bool = False,
    ) -> list[str]:
        """Resolve and validate the exact effective grants once at admission."""

        if self.gateway_runner is None:
            return []
        with self._profile_runtime_context(source):
            from gateway.run import _load_gateway_config
            from hermes_cli.tools_config import _get_platform_tools

            config = (
                self._load_gateway_config_for_authority()
                if strict_config
                else _load_gateway_config()
            )
            if "toolsets" in route_config:
                raw = route_config.get("toolsets")
                if not isinstance(raw, list):
                    raise WebhookContractError("webhook route toolsets must be a list")
                normalized: list[str] = []
                for value in raw:
                    if not isinstance(value, str) or not value.strip():
                        raise WebhookContractError(
                            "webhook route toolset names must be non-empty strings"
                        )
                    name = value.strip()
                    if name not in normalized:
                        normalized.append(name)
                if not normalized:
                    return []
                config = dict(config)
                platform_toolsets = dict(config.get("platform_toolsets") or {})
                platform_toolsets[Platform.WEBHOOK.value] = normalized
                config["platform_toolsets"] = platform_toolsets
            return sorted(_get_platform_tools(config, Platform.WEBHOOK.value))

    @staticmethod
    def _load_gateway_config_for_authority() -> dict[str, Any]:
        """Strictly load the profile config that defines webhook grants."""

        from hermes_constants import get_hermes_home

        config_path = get_hermes_home() / "config.yaml"
        try:
            raw_bytes = read_bounded_regular_file_snapshot(
                config_path,
                max_bytes=_MAX_DYNAMIC_ROUTES_FILE_BYTES,
            ).content
        except BoundedFileSnapshotTooLarge as exc:
            raise WebhookContractError(
                "webhook toolset authority config exceeds 4 MiB"
            ) from exc
        except OSError as exc:
            raise WebhookContractError(
                "webhook toolset authority config is unavailable"
            ) from exc
        try:
            import yaml

            loaded = yaml.safe_load(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise WebhookContractError(
                "webhook toolset authority config is malformed"
            ) from exc
        if not isinstance(loaded, dict):
            raise WebhookContractError(
                "webhook toolset authority config must be an object"
            )
        try:
            from hermes_cli import managed_scope
            from hermes_cli.config import _normalize_root_model_keys

            loaded = managed_scope.apply_managed_overlay(loaded)
            if not isinstance(loaded, dict):
                raise TypeError("managed config overlay is not an object")
            loaded = _normalize_root_model_keys(loaded)
        except Exception as exc:
            raise WebhookContractError(
                "webhook managed toolset authority cannot be resolved"
            ) from exc
        if not isinstance(loaded, dict):
            raise WebhookContractError(
                "webhook toolset authority config must remain an object"
            )
        return loaded

    def _resolve_target_adapter(self, platform_name: str, profile: str):
        runner = self.gateway_runner
        if runner is None:
            return None
        try:
            platform = Platform(platform_name)
        except ValueError:
            return None
        resolver = getattr(runner, "_authorization_adapter", None)
        if callable(resolver):
            return resolver(platform, profile)

        # Narrow exact fallback for legacy test doubles. It preserves the same
        # profile ownership rules and never borrows a sibling profile adapter.
        normalized_profile = str(profile or "default").strip() or "default"
        active_name = None
        active_name_fn = getattr(runner, "_active_profile_name", None)
        if callable(active_name_fn):
            active_name = active_name_fn()
        if active_name == normalized_profile:
            return (getattr(runner, "adapters", None) or {}).get(platform)
        profile_map = (getattr(runner, "_profile_adapters", None) or {}).get(
            normalized_profile
        )
        if isinstance(profile_map, dict):
            return profile_map.get(platform)
        if (
            normalized_profile == "default"
            and getattr(
                getattr(runner, "config", None),
                "multiplex_profiles",
                False,
            )
            is not True
        ):
            return (getattr(runner, "adapters", None) or {}).get(platform)
        return None

    def _preflight_target_template(
        self,
        *,
        profile: str,
        deliver: Any,
        deliver_extra: Any,
    ) -> PreparedTargetTemplate:
        """Validate target structure and freeze any home-channel fallback."""

        if not isinstance(deliver, str) or not deliver.strip():
            raise WebhookContractError("webhook deliver target must be a string")
        kind = deliver.strip().lower()
        if not isinstance(deliver_extra, Mapping):
            raise WebhookContractError("webhook deliver_extra must be an object")
        extra_keys = set(deliver_extra)
        if kind == "log":
            if extra_keys:
                raise WebhookContractError("log delivery does not accept deliver_extra")
            return PreparedTargetTemplate(kind="log", profile=profile)
        if kind == "github_comment":
            unknown = extra_keys - {"repo", "pr_number"}
            if unknown:
                raise WebhookContractError(
                    f"github delivery has unsupported fields: {sorted(unknown)}"
                )
            if not shutil.which("gh"):
                raise WebhookContractError("gh CLI is unavailable")
            return PreparedTargetTemplate(kind=kind, profile=profile)

        unknown = extra_keys - {
            "chat_id",
            "thread_id",
            "message_thread_id",
            "scope_id",
        }
        if unknown:
            raise WebhookContractError(
                f"platform delivery has unsupported fields: {sorted(unknown)}"
            )
        try:
            platform = Platform(kind)
        except ValueError as exc:
            raise WebhookContractError(f"unknown delivery platform {kind!r}") from exc
        if platform in {
            Platform.LOCAL,
            Platform.API_SERVER,
            Platform.WEBHOOK,
            Platform.MSGRAPH_WEBHOOK,
            Platform.RELAY,
        }:
            raise WebhookContractError(
                f"platform {kind!r} is not an outbound webhook target"
            )
        explicit_chat = "chat_id" in deliver_extra
        configured_chat = deliver_extra.get("chat_id")
        if explicit_chat:
            configured_chat = self._optional_nonempty_string(
                configured_chat,
                label="chat_id",
            )
        configured_scope_value = deliver_extra.get("scope_id")
        if configured_scope_value is not None:
            configured_scope_value = self._optional_nonempty_string(
                configured_scope_value,
                label="scope_id",
            )

        # An explicit non-Slack destination is fully route-bound and needs no
        # startup-time adapter registry join. Gateway adapters are published
        # concurrently; requiring one here makes otherwise valid webhook
        # startup depend on connection order.
        if platform is not Platform.SLACK and explicit_chat:
            return PreparedTargetTemplate(
                kind="platform",
                profile=profile,
                platform=kind,
            )

        chat_is_static = (
            isinstance(configured_chat, str)
            and _PROMPT_TOKEN_RE.search(configured_chat) is None
        )
        scope_is_static = (
            isinstance(configured_scope_value, str)
            and _PROMPT_TOKEN_RE.search(configured_scope_value) is None
        )
        if (
            platform is Platform.SLACK
            and explicit_chat
            and configured_scope_value is not None
        ):
            return PreparedTargetTemplate(
                kind="platform",
                profile=profile,
                platform=kind,
                slack_static_chat_id=(configured_chat if chat_is_static else None),
                slack_static_scope_id=(
                    configured_scope_value
                    if chat_is_static and scope_is_static
                    else None
                ),
                slack_scope_locked=chat_is_static and scope_is_static,
            )

        adapter = self._resolve_target_adapter(kind, profile)
        home = (
            getattr(getattr(adapter, "config", None), "home_channel", None)
            if adapter is not None
            else None
        )
        runner = self.gateway_runner
        runner_config = getattr(runner, "config", None)
        multiplex = getattr(runner_config, "multiplex_profiles", False) is True
        if home is None and multiplex:
            # Secondary adapters are published after the shared webhook
            # listener.  While already inside this route's exact profile
            # runtime scope, strictly load that physical profile's config and
            # freeze its home target; never borrow the default/sibling config.
            from gateway.config import GatewayConfig
            from hermes_constants import get_hermes_home

            profile_config_path = get_hermes_home() / "config.yaml"
            try:
                profile_config = GatewayConfig.from_dict(
                    self._load_gateway_config_for_authority()
                    if profile_config_path.exists()
                    else {}
                )
            except Exception as exc:
                raise WebhookContractError(
                    "webhook target authority config is invalid"
                ) from exc
            home = profile_config.get_home_channel(platform)
        elif home is None:
            get_home = getattr(runner_config, "get_home_channel", None)
            if callable(get_home):
                home = get_home(platform)
        home_chat_id = None
        home_thread_id = None
        home_scope_id = None
        if home is not None:
            if getattr(home, "platform", None) != platform:
                raise WebhookContractError("home channel belongs to another platform")
            home_chat_id = self._optional_nonempty_string(
                getattr(home, "chat_id", None), label="home chat_id"
            )
            home_thread_id = self._optional_nonempty_string(
                getattr(home, "thread_id", None), label="home thread_id"
            )
            home_scope_id = self._optional_nonempty_string(
                getattr(home, "scope_id", None), label="home scope_id"
            )
        if "chat_id" not in deliver_extra and home_chat_id is None:
            if self.gateway_runner is None:
                # Standalone compatibility: capture the absence itself. A
                # later request cannot acquire a newly configured home through
                # this frozen template and therefore still fails closed during
                # materialization.
                return PreparedTargetTemplate(
                    kind="platform",
                    profile=profile,
                    platform=kind,
                )
            raise WebhookContractError(
                f"platform {kind!r} has no explicit chat_id or home channel"
            )
        slack_static_chat_id = None
        slack_static_scope_id = None
        slack_scope_locked = False
        if platform is Platform.SLACK:
            if "chat_id" not in deliver_extra:
                slack_static_chat_id = home_chat_id
            elif chat_is_static:
                slack_static_chat_id = configured_chat
            configured_static_scope = (
                configured_scope_value if scope_is_static else None
            )
            if slack_static_chat_id is not None:
                resolver = (
                    getattr(adapter, "scope_id_for_chat", None)
                    if adapter is not None
                    else None
                )
                observed_scope = (
                    resolver(slack_static_chat_id) if callable(resolver) else None
                )
                observed_scope = self._optional_nonempty_string(
                    observed_scope,
                    label="resolved scope_id",
                )
                home_owns_target = home_chat_id == slack_static_chat_id
                asserted_scope = configured_static_scope or (
                    home_scope_id if home_owns_target else None
                )
                if observed_scope is not None and (
                    asserted_scope is not None and asserted_scope != observed_scope
                ):
                    raise WebhookContractError(
                        "configured Slack scope does not own the target channel"
                    )
                slack_static_scope_id = observed_scope or asserted_scope
                if slack_static_scope_id is None:
                    raise WebhookContractError(
                        "Slack target workspace scope cannot be established"
                    )
                slack_scope_locked = True
            elif "scope_id" not in deliver_extra:
                # A payload-templated channel cannot be joined to a mutable
                # live channel-to-workspace map after authentication. Require
                # the workspace selector itself to be part of the signed,
                # route-bound target template.
                raise WebhookContractError(
                    "templated Slack chat_id requires an explicit scope_id"
                )
        return PreparedTargetTemplate(
            kind="platform",
            profile=profile,
            platform=kind,
            home_chat_id=home_chat_id,
            home_thread_id=home_thread_id,
            home_scope_id=home_scope_id,
            slack_static_chat_id=slack_static_chat_id,
            slack_static_scope_id=slack_static_scope_id,
            slack_scope_locked=slack_scope_locked,
        )

    @staticmethod
    def _optional_nonempty_string(value: Any, *, label: str) -> Optional[str]:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not value.strip():
            raise WebhookContractError(f"{label} must be a non-empty string")
        return value.strip()

    def _materialize_target(
        self,
        prepared: PreparedTargetTemplate,
        rendered_extra: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build the strict canonical target snapshot from captured authority."""

        if prepared.kind == "log":
            return {"v": 1, "kind": "log", "profile": prepared.profile}
        if prepared.kind == "github_comment":
            repo = rendered_extra.get("repo")
            pr_number = rendered_extra.get("pr_number")
            if not isinstance(repo, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo
            ):
                raise WebhookContractError("github repo must use owner/name syntax")
            if isinstance(pr_number, bool):
                raise WebhookContractError(
                    "github pr_number must be a positive integer"
                )
            if isinstance(pr_number, str):
                if not pr_number.isdigit():
                    raise WebhookContractError(
                        "github pr_number must be a positive integer"
                    )
                pr_number = int(pr_number)
            if not isinstance(pr_number, int) or pr_number <= 0:
                raise WebhookContractError(
                    "github pr_number must be a positive integer"
                )
            return {
                "v": 1,
                "kind": "github_comment",
                "profile": prepared.profile,
                "repo": repo,
                "pr_number": pr_number,
            }

        if prepared.kind != "platform" or not prepared.platform:
            raise WebhookContractError("prepared webhook target is invalid")
        explicit_chat = self._optional_nonempty_string(
            rendered_extra.get("chat_id"), label="chat_id"
        )
        chat_id = explicit_chat or prepared.home_chat_id
        if chat_id is None:
            raise WebhookContractError("platform delivery has no chat_id")

        thread_id = self._optional_nonempty_string(
            rendered_extra.get("thread_id"), label="thread_id"
        )
        message_thread_id = self._optional_nonempty_string(
            rendered_extra.get("message_thread_id"), label="message_thread_id"
        )
        if thread_id and message_thread_id and thread_id != message_thread_id:
            raise WebhookContractError(
                "thread_id and message_thread_id identify different targets"
            )
        selected_thread = thread_id or message_thread_id
        if selected_thread is None and explicit_chat is None:
            selected_thread = prepared.home_thread_id

        configured_scope = self._optional_nonempty_string(
            rendered_extra.get("scope_id"), label="scope_id"
        )
        scope_id = configured_scope
        if prepared.platform == Platform.SLACK.value:
            if prepared.slack_scope_locked:
                if chat_id != prepared.slack_static_chat_id:
                    raise WebhookContractError(
                        "rendered Slack target conflicts with frozen authority"
                    )
                if configured_scope and (
                    configured_scope != prepared.slack_static_scope_id
                ):
                    raise WebhookContractError(
                        "configured Slack scope does not own the target channel"
                    )
                scope_id = prepared.slack_static_scope_id
            else:
                scope_id = configured_scope
            if scope_id is None:
                raise WebhookContractError(
                    "Slack target workspace scope cannot be established"
                )
        elif scope_id is not None:
            raise WebhookContractError("scope_id is supported only for Slack delivery")

        target: dict[str, Any] = {
            "v": 1,
            "kind": "platform",
            "profile": prepared.profile,
            "platform": prepared.platform,
            "chat_id": chat_id,
        }
        if selected_thread is not None:
            target["thread_id"] = selected_thread
        if scope_id is not None:
            target["scope_id"] = scope_id
        return target

    @staticmethod
    def _live_slack_scope_matches(
        target: Mapping[str, Any],
        adapter: Any,
    ) -> bool:
        """Rejoin a durable Slack target to one live workspace authority."""

        chat_id = target.get("chat_id")
        scope_id = target.get("scope_id")
        resolver = getattr(adapter, "scope_id_for_chat", None)
        try:
            observed_scope = resolver(chat_id) if callable(resolver) else None
        except Exception:
            return False
        if observed_scope:
            return str(observed_scope) == scope_id

        home = getattr(getattr(adapter, "config", None), "home_channel", None)
        return bool(
            home is not None
            and getattr(home, "platform", None) is Platform.SLACK
            and str(getattr(home, "chat_id", "") or "") == chat_id
            and str(getattr(home, "scope_id", "") or "") == scope_id
        )

    @staticmethod
    def _validate_target_snapshot(snapshot: Any) -> dict[str, Any]:
        """Validate durable target semantics before every external attempt."""

        if not isinstance(snapshot, Mapping):
            raise WebhookContractError("durable target snapshot is missing")
        target = _plain_json_snapshot(snapshot)
        if type(target.get("v")) is not int or target.get("v") != 1:
            raise WebhookContractError("durable target version is invalid")
        kind = target.get("kind")
        profile = target.get("profile")
        if not isinstance(profile, str) or not profile:
            raise WebhookContractError("durable target profile is invalid")
        if kind == "log":
            if set(target) != {"v", "kind", "profile"}:
                raise WebhookContractError("durable log target has unknown fields")
            return target
        if kind == "github_comment":
            if set(target) != {
                "v",
                "kind",
                "profile",
                "repo",
                "pr_number",
            }:
                raise WebhookContractError("durable GitHub target has unknown fields")
            repo = target.get("repo")
            number = target.get("pr_number")
            if not isinstance(repo, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo
            ):
                raise WebhookContractError("durable GitHub repo is invalid")
            if type(number) is not int or number <= 0:
                raise WebhookContractError("durable GitHub PR number is invalid")
            return target
        if kind != "platform":
            raise WebhookContractError("durable target kind is invalid")
        allowed = {
            "v",
            "kind",
            "profile",
            "platform",
            "chat_id",
            "thread_id",
            "scope_id",
        }
        if not set(target).issubset(allowed):
            raise WebhookContractError("durable platform target has unknown fields")
        if not {"v", "kind", "profile", "platform", "chat_id"}.issubset(target):
            raise WebhookContractError("durable platform target is incomplete")
        platform_name = target.get("platform")
        chat_id = target.get("chat_id")
        if not isinstance(platform_name, str) or not platform_name:
            raise WebhookContractError("durable target platform is invalid")
        try:
            Platform(platform_name)
        except ValueError as exc:
            raise WebhookContractError("durable target platform is unknown") from exc
        if not isinstance(chat_id, str) or not chat_id:
            raise WebhookContractError("durable target chat_id is invalid")
        for key in ("thread_id", "scope_id"):
            value = target.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                raise WebhookContractError(f"durable target {key} is invalid")
        if platform_name == Platform.SLACK.value and not target.get("scope_id"):
            raise WebhookContractError("durable Slack target lacks workspace scope")
        if platform_name != Platform.SLACK.value and "scope_id" in target:
            raise WebhookContractError("durable non-Slack target has scope_id")
        return target

    def _source_from_authority(self, authority: OperationAuthority):
        """Rebuild and cross-check only the source stored by durable prepare."""

        snapshot = authority.event_snapshot
        if not isinstance(snapshot, Mapping):
            raise WebhookContractError("durable event snapshot is missing")
        source_blob = snapshot.get("source")
        if not isinstance(source_blob, Mapping):
            raise WebhookContractError("durable event source is missing")
        from gateway.session import SessionSource

        try:
            source = SessionSource.from_dict(_plain_json_snapshot(source_blob))
        except Exception as exc:
            raise WebhookContractError("durable event source is invalid") from exc
        if (
            source.platform is not Platform.WEBHOOK
            or source.chat_id != authority.session_key
            or (source.profile or "default") != authority.profile
            or source.chat_type != "webhook"
            or source.user_id != f"webhook:{authority.route}"
            or source.user_name != authority.route
        ):
            raise WebhookContractError(
                "durable event source conflicts with operation authority"
            )
        source.profile = authority.profile
        source._transport_adapter_ref = weakref.ref(self)
        return source
