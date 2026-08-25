#!/usr/bin/env python3
"""
Fix cron jobs blocked by the provider/base_url safety gate.

These jobs were created with base_url='https://api.ollama.com' which the
scheduler rejects because it cannot host-match 'api.ollama.com' to the ollama
provider's registered inference endpoint.

On this fleet, Hermes already has a named custom provider ``litellm_proxy``
(Tailscale LiteLLM, stored ``LITELLM_VIRTUAL_KEY``). Pointing jobs at that
named provider + its configured host passes the safety-gate host-match and
actually authenticates. Bare ``custom`` + ``http://localhost:4000/v1`` is the
generic fallback for hosts with no named proxy — and on mfc1 nothing listens
on :4000.

Usage:
    python3 scripts/fix-cron-ollama-base-url.py [--dry-run]

Requires: a live Hermes environment (HERMES_HOME / ~/.hermes must exist and
contain cron/jobs.json).
"""
from __future__ import annotations

import argparse
import sys
import os

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TARGET_JOB_IDS = {
    "122eda6f83b1",  # dla-sanford-confirmation-monitor
    "0060360d35b4",  # Signal Scanner Unified — Advocacy + Cadenazzi (3x daily)
    "3672e891da08",  # LinkedIn Content Scan — Post Opportunity Digest (3x daily)
    "aa9098c66058",  # UST Legal Matter Tracker
    "141615d9bbfb",  # UST Accounting Wind-down Weekly Scan
}

BAD_BASE_URL = "https://api.ollama.com"
FALLBACK_PROVIDER = "custom"
FALLBACK_BASE_URL = "http://localhost:4000/v1"
NAMED_PROXY = "litellm_proxy"

# Distinctive phrase from cron.scheduler._guard_job_credential_exfil:
#   RuntimeError(f"Cron job '{job_id}' blocked for safety: {err}")
# run_job persists that as "RuntimeError: Cron job '... blocked for safety: ..."
SAFETY_GATE_ERROR_PREFIX = "blocked for safety:"


def _iter_compatible_provider_entries(config: dict) -> list:
    """Yield custom-provider entries from legacy list and v12 ``providers:`` dict.

    Prefers ``get_compatible_custom_providers`` so config v12 (which migrates
    ``custom_providers`` into ``providers`` and deletes the list) still
    surfaces ``litellm_proxy``.
    """
    try:
        from hermes_cli.config import get_compatible_custom_providers

        entries = get_compatible_custom_providers(config)
        if isinstance(entries, list):
            return entries
    except Exception:
        pass

    entries: list = []
    legacy = config.get("custom_providers")
    if isinstance(legacy, list):
        entries.extend(legacy)
    providers = config.get("providers")
    if isinstance(providers, dict):
        for key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item.setdefault("provider_key", str(key))
            if not str(item.get("name") or "").strip():
                item["name"] = str(key)
            if not str(item.get("base_url") or "").strip():
                item["base_url"] = item.get("api") or item.get("url") or ""
            entries.append(item)
    return entries


def _provider_entry_url(entry: dict) -> str:
    """Extract a configured endpoint, matching v12 then legacy field names."""
    return str(
        entry.get("api") or entry.get("url") or entry.get("base_url") or ""
    ).strip().rstrip("/")


def _matches_named_proxy(entry: dict, key: str = "") -> bool:
    name = str(entry.get("name") or "").strip()
    provider_key = str(entry.get("provider_key") or key or "").strip()
    return name == NAMED_PROXY or provider_key == NAMED_PROXY


def _resolve_from_v12_providers(config: dict) -> tuple[str, str] | None:
    """Prefer the v12 ``providers`` dict, matching runtime ``_get_named_custom_provider``.

    Checks ``providers[NAMED_PROXY]`` by key first, then any entry whose
    ``name`` is NAMED_PROXY. Returns None when the dict is absent or has no
    usable URL so the caller can fall back to the compatibility list.
    """
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return None

    keyed = providers.get(NAMED_PROXY)
    if isinstance(keyed, dict):
        url = _provider_entry_url(keyed)
        if url:
            return NAMED_PROXY, url

    for key, entry in providers.items():
        if key == NAMED_PROXY or not isinstance(entry, dict):
            continue
        if not _matches_named_proxy(entry, str(key)):
            continue
        url = _provider_entry_url(entry)
        if url:
            return NAMED_PROXY, url
    return None


def resolve_litellm_target(config: dict | None) -> tuple[str, str]:
    """Return (provider, base_url) for this host's LiteLLM proxy.

    Runtime resolution checks the v12 ``providers`` mapping first
    (``_get_named_custom_provider``); ``get_compatible_custom_providers``
    returns legacy ``custom_providers`` entries first, so we must not walk
    that list until the v12 dict has been tried.
    """
    if isinstance(config, dict):
        v12 = _resolve_from_v12_providers(config)
        if v12 is not None:
            return v12
        for entry in _iter_compatible_provider_entries(config):
            if not isinstance(entry, dict):
                continue
            if not _matches_named_proxy(entry):
                continue
            url = _provider_entry_url(entry)
            if url:
                return NAMED_PROXY, url
    return FALLBACK_PROVIDER, FALLBACK_BASE_URL


def _needs_safety_gate_resume(job: dict) -> bool:
    """True when a paused/error job is stuck on the provider/base_url gate."""
    if job.get("state") not in {"paused", "error"}:
        return False
    last_error = job.get("last_error") or ""
    return SAFETY_GATE_ERROR_PREFIX in last_error


def process_target_job(
    job: dict,
    good_provider: str,
    good_base_url: str,
    *,
    dry_run: bool = False,
    compute_next_run=None,
    current_provider: str | None = None,
    current_base_url: str | None = None,
) -> tuple[bool, str | None]:
    """Apply idempotent routing and auto-resume.

    Returns ``(needs_write, resumed_from_state)``. Routing that is already
    correct must not skip the resume block: a job can already point at the
    desired provider/URL while remaining paused/error with a safety-gate
    last_error.
    """
    routing_ok = current_provider == good_provider and current_base_url == good_base_url
    needs_resume = _needs_safety_gate_resume(job)
    if routing_ok and not needs_resume:
        return False, None

    previous_state = job.get("state") if needs_resume else None
    if not dry_run:
        if not routing_ok:
            job["provider"] = good_provider
            job["base_url"] = good_base_url
            # Drop a stale snapshot from the old ollama/openrouter pairing.
            job["provider_snapshot"] = None
        if needs_resume:
            next_run_fn = compute_next_run
            if next_run_fn is None:
                from cron.jobs import compute_next_run as next_run_fn
            job["enabled"] = True
            job["state"] = "scheduled"
            job["paused_at"] = None
            job["paused_reason"] = None
            job["consecutive_failures"] = 0
            job["last_error"] = None
            job["next_run_at"] = next_run_fn(job.get("schedule", {}), job.get("last_run_at"))
    return True, previous_state


def _load_hermes_config() -> dict | None:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
    args = parser.parse_args()

    from cron.jobs import load_jobs, save_jobs, _normalize_job_optional_text

    good_provider, good_base_url = resolve_litellm_target(_load_hermes_config())
    print(f"Target: provider={good_provider!r} base_url={good_base_url!r}")

    jobs = load_jobs()
    changed = []

    for job in jobs:
        job_id = job.get("id", "")
        if job_id not in TARGET_JOB_IDS:
            continue

        current_base_url = _normalize_job_optional_text(job.get("base_url"), strip_trailing_slash=True)
        current_provider = _normalize_job_optional_text(job.get("provider"))
        name = job.get("name", job_id)

        print(f"  Job {job_id} ({name!r})")
        print(f"    provider: {current_provider!r} → {good_provider!r}")
        print(f"    base_url: {current_base_url!r} → {good_base_url!r}")

        routing_ok = current_provider == good_provider and current_base_url == good_base_url
        if routing_ok:
            print("    already at target")

        wrote, resumed_from = process_target_job(
            job,
            good_provider,
            good_base_url,
            dry_run=args.dry_run,
            current_provider=current_provider,
            current_base_url=current_base_url,
        )
        if not wrote:
            print("    skip")
            continue

        if resumed_from is not None:
            print(f"    state: auto-resumed (was {resumed_from})")

        changed.append(job_id)

    if not changed:
        print("No matching jobs found — nothing to fix.")
        return 0

    if args.dry_run:
        print(f"\nDry run: would update {len(changed)} job(s). Re-run without --dry-run to apply.")
        return 0

    save_jobs(jobs)
    print(f"\nUpdated {len(changed)} job(s) and saved jobs.json.")
    print("Run 'hermes cron list' to confirm state=scheduled for each job.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
