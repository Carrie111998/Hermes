"""Atomic migration of plaintext webhook secrets to profile references.

All secret values are persisted and read back before any source document is
switched.  Legacy backups are scrubbed before the live source, and receipts and
exceptions contain identifiers only—never secret values.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping

from hermes_cli.webhook_secrets import (
    resolve_webhook_secret,
    store_webhook_secret,
    store_webhook_secret_unlocked,
    validate_webhook_secret_ref,
    webhook_route_secret_ref,
    webhook_secret_write_lock,
)
from utils import atomic_replace


_SECRET_KEYS = ("secret", "secret_value")


class WebhookSecretMigrationError(RuntimeError):
    """A value-safe migration failure with a machine-readable receipt."""

    def __init__(
        self,
        message: str,
        *,
        receipt: dict[str, Any] | None = None,
        source: str = "",
    ) -> None:
        super().__init__(message)
        self.receipt = receipt or {}
        self.rollback_receipt = {
            "source": source,
            "source_preserved_before_switch": True,
        }


def _plaintext_secret(container: Mapping[str, Any], *, label: str) -> str | None:
    values: list[str] = []
    for key in _SECRET_KEYS:
        if key not in container or container.get(key) in (None, ""):
            continue
        value = container.get(key)
        if not isinstance(value, str):
            raise WebhookSecretMigrationError(
                f"Webhook secret {label!r} must be a non-empty string"
            )
        values.append(value)
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise WebhookSecretMigrationError(
            f"Webhook secret {label!r} has conflicting plaintext fields"
        )
    return values[0]


def _backup_suffix(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10].upper()


def _route_reference(
    route_name: str,
    route: Mapping[str, Any],
    *,
    backup_path: Path | None = None,
    namespace: str = "",
) -> str:
    existing = route.get("secret_ref")
    if existing not in (None, ""):
        return validate_webhook_secret_ref(existing)
    ref = webhook_route_secret_ref(route_name, namespace=namespace)
    if backup_path is not None:
        ref = f"{ref}_BACKUP_{_backup_suffix(backup_path)}"
    return ref


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, 0o600)
        real_path = Path(atomic_replace(tmp_path, path))
        os.chmod(real_path, 0o600)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _writer_context(store: Callable[[str, str], None] | None):
    return webhook_secret_write_lock() if store is None else nullcontext()


def _register_candidate(
    candidates: dict[str, tuple[str, list[dict[str, Any]]]],
    *,
    ref: str,
    value: str,
    receipt: dict[str, Any],
    source: str,
) -> None:
    prior = candidates.get(ref)
    if prior is not None and prior[0] != value:
        raise WebhookSecretMigrationError(
            f"Secret reference {ref!r} maps to conflicting webhook values",
            receipt={"reference": ref, "conflict": True},
            source=source,
        )
    if prior is None:
        candidates[ref] = (value, [receipt])
    else:
        prior[1].append(receipt)


def _stage_route_document(
    routes: Mapping[str, Any],
    *,
    document: Path,
    source: Path,
    candidates: dict[str, tuple[str, list[dict[str, Any]]]],
) -> tuple[dict[str, Any], list[str]]:
    staged = copy.deepcopy(dict(routes))
    migrated: list[str] = []
    for raw_name, route in routes.items():
        if not isinstance(route, Mapping):
            continue
        name = str(raw_name)
        value = _plaintext_secret(route, label=name)
        if value is None:
            if any(key in route for key in _SECRET_KEYS):
                staged_route = staged[raw_name]
                staged_route.pop("secret", None)
                staged_route.pop("secret_value", None)
                migrated.append(name)
            continue
        ref = _route_reference(
            name,
            route,
            backup_path=document if document != source else None,
        )
        receipt = {
            "route": name,
            "reference": ref,
            "document": "source" if document == source else "backup",
            "stored": False,
            "verified": False,
        }
        _register_candidate(
            candidates,
            ref=ref,
            value=value,
            receipt=receipt,
            source=str(source),
        )
        staged_route = staged[raw_name]
        staged_route.pop("secret", None)
        staged_route.pop("secret_value", None)
        staged_route["secret_ref"] = ref
        migrated.append(name)
    return staged, migrated


def _persist_and_verify(
    candidates: dict[str, tuple[str, list[dict[str, Any]]]],
    *,
    put: Callable[[str, str], None],
    lookup: Callable[[str], str | None],
    source: Path,
) -> None:
    for ref, (value, receipts) in candidates.items():
        try:
            put(ref, value)
            for receipt in receipts:
                receipt["stored"] = True
            if lookup(ref) != value:
                raise WebhookSecretMigrationError(
                    "Secret backend verification failed",
                    receipt={"reference": ref, "verified": False},
                    source=str(source),
                )
            for receipt in receipts:
                receipt["verified"] = True
        except WebhookSecretMigrationError:
            raise
        except Exception:
            raise WebhookSecretMigrationError(
                f"Secure persistence failed for reference {ref!r}; source left untouched",
                receipt={"reference": ref, "stored": False},
                source=str(source),
            ) from None


def _read_json_mapping(path: Path, *, source: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise WebhookSecretMigrationError(
            "Unable to read webhook routes safely", source=str(source)
        ) from None
    if not isinstance(value, dict):
        raise WebhookSecretMigrationError(
            "Webhook route store must be a JSON object", source=str(source)
        )
    return value


def migrate_webhook_routes(
    source_path: str | Path,
    *,
    store: Callable[[str, str], None] | None = None,
    resolve: Callable[[str], str | None] | None = None,
    backup_paths: tuple[str | Path, ...] = (),
) -> dict[str, Any]:
    """Migrate route JSON via preflight → persist → verify → scrub → switch."""
    source = Path(source_path)
    backups = tuple(
        path
        for path in (Path(raw) for raw in backup_paths)
        if path.exists() and path != source
    )
    with _writer_context(store):
        documents = {source: _read_json_mapping(source, source=source)}
        for backup in backups:
            documents[backup] = _read_json_mapping(backup, source=source)

        candidates: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        staged: dict[Path, dict[str, Any]] = {}
        migrated_by_path: dict[Path, list[str]] = {}
        for path, routes in documents.items():
            staged[path], migrated_by_path[path] = _stage_route_document(
                routes,
                document=path,
                source=source,
                candidates=candidates,
            )

        put = store or store_webhook_secret_unlocked
        lookup = resolve or resolve_webhook_secret
        _persist_and_verify(candidates, put=put, lookup=lookup, source=source)

        scrubbed: list[str] = []
        for backup in backups:
            if not migrated_by_path[backup]:
                continue
            try:
                _write_json_atomic(backup, staged[backup])
                scrubbed.append(str(backup))
            except Exception:
                raise WebhookSecretMigrationError(
                    "Backup scrub failed; live source left untouched",
                    receipt={"scrubbed_backups": scrubbed},
                    source=str(source),
                ) from None

        migrated = migrated_by_path[source]
        if migrated:
            try:
                _write_json_atomic(source, staged[source])
            except Exception:
                raise WebhookSecretMigrationError(
                    "Atomic route switch failed; source remains available for retry",
                    receipt={
                        "migrated_routes": migrated,
                        "scrubbed_backups": scrubbed,
                    },
                    source=str(source),
                ) from None

        receipts = [
            receipt
            for _value, grouped in candidates.values()
            for receipt in grouped
        ]
        return {
            "migrated_routes": migrated,
            "receipts": receipts,
            "scrubbed_backups": scrubbed,
            "rollback": {
                "source": str(source),
                "source_preserved_on_pre_switch_failure": True,
            },
        }


def _read_config_mapping(path: Path, *, source: Path) -> dict[str, Any]:
    try:
        from hermes_cli.config import require_readable_config_before_write

        value = require_readable_config_before_write(path)
    except Exception:
        raise WebhookSecretMigrationError(
            "Unable to parse webhook config safely", source=str(source)
        ) from None
    if not isinstance(value, dict):
        raise WebhookSecretMigrationError(
            "Webhook config must be a YAML mapping", source=str(source)
        )
    return value


def _global_plaintext(webhook: Mapping[str, Any]) -> str | None:
    values: list[str] = []
    direct = _plaintext_secret(webhook, label="global")
    if direct is not None:
        values.append(direct)
    extra = webhook.get("extra")
    if isinstance(extra, Mapping):
        nested = _plaintext_secret(extra, label="global")
        if nested is not None:
            values.append(nested)
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise WebhookSecretMigrationError(
            "Webhook global secret has conflicting plaintext fields"
        )
    return values[0]


def _global_reference(
    webhook: Mapping[str, Any], *, backup_path: Path | None = None
) -> str:
    refs: list[str] = []
    direct = webhook.get("secret_ref")
    if direct not in (None, ""):
        refs.append(validate_webhook_secret_ref(direct))
    extra = webhook.get("extra")
    if isinstance(extra, Mapping) and extra.get("secret_ref") not in (None, ""):
        refs.append(validate_webhook_secret_ref(extra.get("secret_ref")))
    if refs and any(ref != refs[0] for ref in refs[1:]):
        raise WebhookSecretMigrationError(
            "Webhook global secret has conflicting references"
        )
    if refs:
        return refs[0]
    if backup_path is None:
        return "WEBHOOK_SECRET"
    return f"WEBHOOK_SECRET_BACKUP_{_backup_suffix(backup_path)}"


def _stage_config_document(
    config: Mapping[str, Any],
    *,
    document: Path,
    source: Path,
    candidates: dict[str, tuple[str, list[dict[str, Any]]]],
) -> tuple[dict[str, Any], bool]:
    staged = copy.deepcopy(dict(config))
    platforms = staged.get("platforms")
    if not isinstance(platforms, dict):
        return staged, False
    webhook = platforms.get("webhook")
    if not isinstance(webhook, dict):
        return staged, False
    extra = webhook.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        webhook["extra"] = extra

    changed = False
    global_value = _global_plaintext(webhook)
    if global_value is not None:
        ref = _global_reference(
            webhook,
            backup_path=document if document != source else None,
        )
        receipt = {
            "route": "global",
            "reference": ref,
            "document": "source" if document == source else "backup",
            "stored": False,
            "verified": False,
        }
        _register_candidate(
            candidates,
            ref=ref,
            value=global_value,
            receipt=receipt,
            source=str(source),
        )
        for container in (webhook, extra):
            container.pop("secret", None)
            container.pop("secret_value", None)
        webhook.pop("secret_ref", None)
        extra["secret_ref"] = ref
        changed = True
    else:
        # Empty legacy fields are not credentials, but retaining them keeps
        # the deprecated plaintext shape writable and makes operator intent
        # ambiguous. Normalize those fields away as part of the same pass.
        for container in (webhook, extra):
            for key in _SECRET_KEYS:
                if key in container:
                    container.pop(key, None)
                    changed = True

    # Accept both the canonical ``extra.routes`` location and the historical
    # top-level ``webhook.routes`` shape. The egress guard covers both, so the
    # migration must be able to evacuate both without dead-ending an update.
    for routes in (extra.get("routes"), webhook.get("routes")):
        if not isinstance(routes, dict):
            continue
        for raw_name, route in list(routes.items()):
            if not isinstance(route, Mapping):
                continue
            name = str(raw_name)
            value = _plaintext_secret(route, label=name)
            if value is None:
                for key in _SECRET_KEYS:
                    if key in route:
                        route.pop(key, None)
                        changed = True
                continue
            ref = _route_reference(
                name,
                route,
                backup_path=document if document != source else None,
                namespace="CONFIG",
            )
            receipt = {
                "route": name,
                "reference": ref,
                "document": "source" if document == source else "backup",
                "stored": False,
                "verified": False,
            }
            _register_candidate(
                candidates,
                ref=ref,
                value=value,
                receipt=receipt,
                source=str(source),
            )
            route.pop("secret", None)
            route.pop("secret_value", None)
            route["secret_ref"] = ref
            changed = True
    return staged, changed


def migrate_webhook_config(
    config_path: str | Path,
    *,
    store: Callable[[str, str], None] | None = None,
    resolve: Callable[[str], str | None] | None = None,
    backup_paths: tuple[str | Path, ...] = (),
) -> dict[str, Any]:
    """Migrate global/static-route YAML secrets and scrub supplied backups."""
    source = Path(config_path)
    backups = tuple(
        path
        for path in (Path(raw) for raw in backup_paths)
        if path.exists() and path != source
    )
    with _writer_context(store):
        documents = {source: _read_config_mapping(source, source=source)}
        for backup in backups:
            documents[backup] = _read_config_mapping(backup, source=source)

        candidates: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        staged: dict[Path, dict[str, Any]] = {}
        changed: dict[Path, bool] = {}
        for path, config in documents.items():
            staged[path], changed[path] = _stage_config_document(
                config,
                document=path,
                source=source,
                candidates=candidates,
            )

        put = store or store_webhook_secret_unlocked
        lookup = resolve or resolve_webhook_secret
        _persist_and_verify(candidates, put=put, lookup=lookup, source=source)

        from hermes_cli.config import atomic_config_write

        scrubbed: list[str] = []
        for backup in backups:
            if not changed[backup]:
                continue
            try:
                atomic_config_write(backup, staged[backup], sort_keys=False)
                scrubbed.append(str(backup))
            except Exception:
                raise WebhookSecretMigrationError(
                    "Config backup scrub failed; live source left untouched",
                    receipt={"scrubbed_backups": scrubbed},
                    source=str(source),
                ) from None

        if changed[source]:
            try:
                atomic_config_write(source, staged[source], sort_keys=False)
            except Exception:
                raise WebhookSecretMigrationError(
                    "Atomic config switch failed; source remains available for retry",
                    receipt={"scrubbed_backups": scrubbed},
                    source=str(source),
                ) from None

        receipts = [
            receipt
            for _value, grouped in candidates.values()
            for receipt in grouped
        ]
        return {
            "migrated": changed[source],
            "receipts": receipts,
            "scrubbed_backups": scrubbed,
            "rollback": {
                "source": str(source),
                "source_preserved_on_pre_switch_failure": True,
            },
        }


migrate = migrate_webhook_routes
migrate_webhook_secret_refs = migrate_webhook_routes

__all__ = [
    "WebhookSecretMigrationError",
    "migrate_webhook_config",
    "migrate_webhook_routes",
    "migrate_webhook_secret_refs",
    "resolve_webhook_secret",
    "store_webhook_secret",
]
