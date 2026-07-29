#!/usr/bin/env python3
"""Dormant fixed-root recovery coordinator for one active release update.

This module composes existing production primitives but exposes no CLI,
entrypoint, service, timer, installer, or fresh-execution path.  Recovery owns
the global activation lock for the complete lifecycle: normalize the existing
active marker, open only its exact existing journal, recover and revalidate a
terminal host state, then retire only that exact marker.
"""

from __future__ import annotations

from typing import Any, Mapping, NoReturn

from scripts.canary import production_cutover_activation_lock as authority_lock
from scripts.canary import production_release_active_transaction as active
from scripts.canary import production_release_host_actions as host_actions
from scripts.canary import production_release_update_journal as journal_module
from scripts.canary import production_release_update_runtime as runtime


class ProductionReleaseUpdateRecoveryError(RuntimeError):
    """Stable, secret-free failure at the recovery coordinator boundary."""


def _fail(code: str, *, cause: BaseException | None = None) -> NoReturn:
    # Causes can contain credentials, paths, request payloads, or provider
    # responses.  Preserve only the stable boundary code: even a fully
    # formatted traceback must not disclose the nested exception.
    del cause
    error = ProductionReleaseUpdateRecoveryError(code)
    raise error from None


def _validated_marker_authority(
    marker: Any,
) -> Mapping[str, Any]:
    if not isinstance(marker, Mapping):
        _fail("release_update_recovery_marker_invalid")
    raw_authority = marker.get("authority_record")
    if not isinstance(raw_authority, Mapping):
        _fail("release_update_recovery_marker_invalid")
    try:
        authority = runtime.validate_authority_record(raw_authority)
    except (runtime.ProductionReleaseUpdateRuntimeError, TypeError, ValueError) as exc:
        _fail("release_update_recovery_marker_invalid", cause=exc)
    if (
        marker.get("intent_sha256")
        != authority["intent"]["intent_sha256"]
        or marker.get("authority_record_sha256")
        != authority["authority_record_sha256"]
        or marker.get("authority_record") != authority
    ):
        _fail("release_update_recovery_marker_invalid")
    return authority


def _recover_locked() -> runtime.TransactionState | None:
    try:
        marker = active.recover_existing_active_transaction()
    except active.ProductionReleaseActiveTransactionError as exc:
        _fail("release_update_recovery_registry_failed", cause=exc)
    except Exception as exc:
        _fail("release_update_recovery_registry_failed", cause=exc)
    if marker is None:
        return None
    authority = _validated_marker_authority(marker)

    try:
        journal = journal_module.ReleaseUpdateJournal.open_existing(
            authority_record=authority,
        )
        # ``open_existing`` is intentionally lazy.  Load once here so a
        # missing transaction, missing/pending authority header, or invalid
        # marker-selected journal is classified before any host action object
        # is constructed.  The runtime reloads it under the same outer lock.
        journal.load()
    except journal_module.ProductionReleaseUpdateJournalError as exc:
        _fail("release_update_recovery_journal_failed", cause=exc)
    except Exception as exc:
        _fail("release_update_recovery_journal_failed", cause=exc)
    try:
        actions = host_actions.ProductionReleaseHostActions()
    except host_actions.ProductionReleaseHostActionsError as exc:
        _fail("release_update_recovery_host_actions_failed", cause=exc)
    except Exception as exc:
        _fail("release_update_recovery_host_actions_failed", cause=exc)
    try:
        state = runtime.recover_update(
            authority_record=authority,
            actions=actions,
            journal=journal,
        )
    except runtime.ProductionReleaseUpdateRuntimeError as exc:
        _fail("release_update_recovery_runtime_failed", cause=exc)
    except Exception as exc:
        _fail("release_update_recovery_runtime_failed", cause=exc)
    if (
        not isinstance(state, runtime.TransactionState)
        or state.terminal_phase not in runtime.TERMINAL_PHASES
        or state.intent != authority["intent"]
    ):
        _fail("release_update_recovery_terminal_state_invalid")

    try:
        active.retire_active_transaction(
            authority_record=authority,
        )
    except active.ProductionReleaseActiveTransactionError as exc:
        _fail("release_update_recovery_retirement_failed", cause=exc)
    except Exception as exc:
        _fail("release_update_recovery_retirement_failed", cause=exc)
    return state


def recover_active_release_transaction() -> runtime.TransactionState | None:
    """Recover the sole existing production transaction or return idle.

    The public boundary deliberately exposes no path, authority, action, clock,
    journal, or lock override.  The recovery coordinator may append only to the
    exact existing journal selected by the active marker and can never start a
    fresh transaction.
    """

    try:
        context = authority_lock.authority_activation_lock(
            require_root=True,
        )
        with context:
            return _recover_locked()
    except ProductionReleaseUpdateRecoveryError:
        raise
    except authority_lock.AuthorityActivationLockError as exc:
        _fail("release_update_recovery_lock_unavailable", cause=exc)
    except Exception as exc:
        _fail("release_update_recovery_lock_unavailable", cause=exc)


__all__ = [
    "ProductionReleaseUpdateRecoveryError",
    "recover_active_release_transaction",
]
