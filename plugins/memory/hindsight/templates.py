"""Starter bank templates for the Hindsight memory-provider setup wizard.

Fetches the Hindsight Bank Templates catalog, filters to templates tagged for
the ``hermes`` integration, and applies a chosen manifest to the user's bank
via the import API (``POST /v1/default/banks/{bank}/import``, which creates the
bank if it doesn't exist).

Kept out of ``__init__`` so the wizard logic stays small and testable. The
catalog source is overridable with ``HINDSIGHT_TEMPLATES_URL`` (e.g. to pin a
version or point at a mirror).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# The Bank Templates catalog lives in the Hindsight docs repo and is the same
# file that powers hindsight.vectorize.io/templates.
_DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/vectorize-io/hindsight/main/"
    "hindsight-docs/src/data/templates.json"
)
_HTTP_TIMEOUT = 15


def catalog_url() -> str:
    return os.environ.get("HINDSIGHT_TEMPLATES_URL", _DEFAULT_CATALOG_URL)


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 - fixed https catalog
        return json.loads(resp.read().decode("utf-8"))


def fetch_hermes_templates(url: str | None = None) -> list[dict]:
    """Return catalog entries tagged for the ``hermes`` integration."""
    catalog = _get_json(url or catalog_url())
    entries = catalog.get("templates", []) if isinstance(catalog, dict) else []
    return [e for e in entries if "hermes" in (e.get("integrations") or [])]


def fetch_manifest(entry: dict, url: str | None = None) -> dict:
    """Fetch the BankTemplateManifest JSON for a catalog entry."""
    # manifest_file is relative to the catalog (e.g. "templates/foo.json").
    manifest_url = urljoin(url or catalog_url(), entry["manifest_file"])
    return _get_json(manifest_url)


def apply_template(api_url: str, bank_id: str, api_key: str | None, manifest: dict) -> None:
    """Apply a manifest to a bank via the import endpoint. Raises on failure."""
    endpoint = f"{api_url.rstrip('/')}/v1/default/banks/{bank_id}/import"
    data = json.dumps(manifest).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")  # noqa: S310
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
        resp.read()  # drain; urlopen raises HTTPError on non-2xx


def run_template_step(
    *,
    api_url: str,
    bank_id: str,
    api_key: str | None,
    select,
    cancelled,
    log=print,
) -> str | None:
    """Drive the wizard's starter-template step.

    ``select(title, items, default, cancel_returns)`` is the picker (injected so
    this is testable without curses). Returns the applied template id, or None
    if skipped/blank/failed. Never raises — the template is a nice-to-have.
    """
    try:
        entries = fetch_hermes_templates()
    except Exception as e:  # network/parse — non-fatal
        logger.debug("Hindsight: could not fetch templates: %s", e)
        return None
    if not entries:
        return None

    items = [(e.get("name", e["id"]), (e.get("description") or "")[:72]) for e in entries]
    items.append(("Blank", "Start with an empty memory bank"))
    idx = select("  Starter memory template", items, default=0, cancel_returns=cancelled)
    if idx == cancelled or idx >= len(entries):
        return None  # blank or cancelled

    entry = entries[idx]
    try:
        manifest = fetch_manifest(entry)
        apply_template(api_url, bank_id, api_key, manifest)
        log(f"  ✓ Applied '{entry.get('name', entry['id'])}' template to bank '{bank_id}'")
        return entry["id"]
    except Exception as e:
        log(f"  ⚠ Could not apply template ({e}). You can apply one later from "
            f"hindsight.vectorize.io/templates.")
        return None
