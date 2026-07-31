#!/usr/bin/env python3
"""Audit read-only des owners AI Factory qui ne protègent plus une lane active.

La vérité du bail vit dans `factory_lane.py` : ce checker ne réinvente aucune
sémantique PID/TTL/worktree, il appelle `determine_process_state`,
`_get_worktree_last_active` et `evaluate_reclaim` de la production. Un owner
n'est signalé que si la production elle-même le jugerait réclamable.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

# Le checker vit à côté de factory_lane.py mais peut être invoqué depuis
# n'importe quel cwd : on épingle le répertoire des scripts avant l'import.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import factory_lane


class CloseoutCheckError(RuntimeError):
    """Registre illisible ou incomplet — l'audit doit échouer, pas dire « rien »."""


def _lease_fields(owner: dict) -> dict | None:
    """Les champs exacts que `evaluate_reclaim` consomme, ou None si absents.

    La production écrit toujours `heartbeat_at` et `ttl_hours` ; un owner qui
    ne les porte pas n'est pas un bail qu'on peut juger, on le signale invalide
    plutôt que de deviner un TTL.
    """
    try:
        return {
            "heartbeat_at": float(owner["heartbeat_at"]),
            "ttl_hours": float(owner["ttl_hours"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def find_inactive_owners(
    registry_root: Path, active_lanes: Iterable[str], *, now: float | None = None,
    process_state: Callable[[dict], str] | None = None,
    worktree_last_active: Callable[[str | None], float | None] | None = None,
) -> list[dict]:
    """Liste les owners que la production jugerait réclamables, sans écriture.

    `process_state` et `worktree_last_active` sont des points d'injection de
    test ; par défaut on appelle les helpers durcis de `factory_lane`
    (PermissionError = vivant, réutilisation de PID via l'heure de départ,
    activité récente du worktree = non réclamable).
    """
    locks_root = Path(registry_root) / "locks"
    if not locks_root.is_dir():
        raise CloseoutCheckError(f"répertoire locks introuvable: {locks_root}")
    active = set(active_lanes)
    now = time.time() if now is None else now
    findings = []
    for owner_path in sorted(locks_root.glob("*/owner.json")):
        key = owner_path.parent.name
        if key in active:
            continue
        try:
            owner = json.loads(owner_path.read_text())
        except (OSError, ValueError):
            owner = None
        lease = _lease_fields(owner) if isinstance(owner, dict) else None
        if lease is None:
            findings.append({"lane": key, "reason": "invalid owner.json", "path": str(owner_path)})
            continue
        state = (
            process_state(owner)
            if process_state is not None
            else factory_lane.determine_process_state(owner)
        )
        last_active = (
            worktree_last_active(owner.get("worktree"))
            if worktree_last_active is not None
            else factory_lane._get_worktree_last_active(owner.get("worktree"))
        )
        verdict = factory_lane.evaluate_reclaim(
            now=now, owner=lease, process_state=state,
            worktree_last_active=last_active,
        )
        if not verdict["reclaimable"]:
            continue
        findings.append({
            "lane": key,
            "reason": f"reclaimable (process {state}, ttl expired, worktree inactive)",
            "pid": owner.get("pid"),
            "session_id": owner.get("session_id"),
            "worktree": owner.get("worktree"),
            "path": str(owner_path),
        })
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--active-lane", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        findings = find_inactive_owners(args.registry, args.active_lane)
    except CloseoutCheckError as exc:
        print(f"⛔ closeout check: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(findings, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
