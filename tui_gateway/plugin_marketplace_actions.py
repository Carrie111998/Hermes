"""Gateway helpers for Git-backed plugin marketplace requests."""

from __future__ import annotations

MARKETPLACE_ACTIONS = {
    "marketplaces",
    "marketplace_add",
    "marketplace_remove",
    "marketplace_refresh",
}


class MarketplaceRequestError(ValueError):
    def __init__(self, message: str, code: int = 4019):
        super().__init__(message)
        self.code = code


def catalog_pins() -> dict[tuple[str, str], dict[str, str]]:
    """Return current commit pins from official and private catalogues."""
    try:
        from hermes_cli.plugin_catalog import load_catalog_live

        pins = {
            ("official", entry.name): {"sha": entry.sha, "tree_sha": ""}
            for entry in load_catalog_live()
        }
    except Exception:
        pins = {}

    try:
        from hermes_cli.plugin_marketplaces import list_marketplaces

        for marketplace in list_marketplaces():
            if not marketplace.get("available") or marketplace.get("stale"):
                continue
            for entry in marketplace.get("entries", []):
                if isinstance(entry, dict) and entry.get("name"):
                    pins[(marketplace["id"], str(entry["name"]))] = {
                        "sha": str(entry.get("sha") or ""),
                        "tree_sha": str(entry.get("tree_sha") or ""),
                    }
    except Exception:
        pass
    return pins


def manage_marketplace(action: str, params: dict) -> dict:
    """Run one profile-scoped marketplace mutation or listing action."""
    from hermes_cli.plugin_marketplaces import (
        add_marketplace,
        list_marketplaces,
        public_marketplace,
        remove_marketplace,
    )

    if action == "marketplace_add":
        marketplace = add_marketplace(str(params.get("url") or "").strip())
        return {"ok": True, "marketplace": public_marketplace(marketplace)}
    if action == "marketplace_remove":
        source_id = str(params.get("source_id") or "").strip()
        if not source_id:
            raise MarketplaceRequestError("marketplace_remove requires 'source_id'")
        return {"ok": True, "removed": remove_marketplace(source_id)}
    return {
        "marketplaces": [
            public_marketplace(marketplace)
            for marketplace in list_marketplaces(force=action == "marketplace_refresh")
        ]
    }


def catalog_install_args(params: dict) -> tuple[str, str, str]:
    """Resolve the identifier and catalogue source for one install request."""
    identifier = str(params.get("identifier") or params.get("repo") or "").strip()
    catalog_name = str(params.get("catalog_name") or "").strip()
    catalog_source = "official"
    marketplace_id = str(params.get("marketplace_id") or "").strip()
    if marketplace_id:
        catalog_name = str(params.get("marketplace_plugin_name") or "").strip()
        if not catalog_name:
            raise MarketplaceRequestError(
                "marketplace install requires 'marketplace_plugin_name'"
            )
        catalog_source = marketplace_id
    if not identifier and not catalog_name:
        raise MarketplaceRequestError(
            "plugins.install requires 'identifier', 'repo', or 'catalog_name'"
        )
    return identifier, catalog_name, catalog_source


def update_plugin(params: dict) -> dict:
    """Update one installed catalogue or marketplace plugin to its current pin."""
    from hermes_cli.plugins_cmd import (
        PluginOperationError,
        _catalog_install_identifier,
        _get_live_catalog_entry,
        _install_plugin_core,
        _marketplace_install,
        _marketplace_metadata,
        _plugins_dir,
        _read_catalog_sidecar,
        _sanitize_plugin_name,
    )

    key = str(params.get("key") or "").strip()
    if not key:
        raise MarketplaceRequestError("plugins.update requires a canonical 'key'")
    try:
        target = _sanitize_plugin_name(key, _plugins_dir(), allow_subdir=True)
    except ValueError as exc:
        raise MarketplaceRequestError(str(exc)) from exc
    if not target.is_dir():
        raise MarketplaceRequestError(f"Plugin '{key}' is not installed", 4020)

    marketplace = _marketplace_install(key) if key == target.name else None
    sidecar = None if marketplace else _read_catalog_sidecar(target)
    if marketplace:
        marketplace_id = str(marketplace["marketplace_id"])
        marketplace_plugin_name = str(
            marketplace.get("marketplace_plugin_name") or target.name
        )
        entry = _get_live_catalog_entry(
            marketplace_plugin_name,
            marketplace_id,
            force=True,
        )
        if entry is None:
            raise MarketplaceRequestError(
                f"Marketplace source for '{marketplace_plugin_name}' is unavailable",
                4021,
            )
        installed_tree = str(marketplace.get("installed_tree_sha") or "").lower()
        current_tree = str(getattr(entry, "tree_sha", "")).lower()
        if installed_tree and installed_tree == current_tree:
            return {"ok": True, "unchanged": True, "sha": entry.sha}
        metadata_extra = _marketplace_metadata(entry)
    elif sidecar and sidecar.get("catalog_name"):
        entry = _get_live_catalog_entry(str(sidecar["catalog_name"]))
        if entry is None:
            raise MarketplaceRequestError(
                f"'{sidecar['catalog_name']}' is no longer in the catalog",
                4021,
            )
        installed_sha = str(sidecar.get("sha") or "").lower()
        if installed_sha == entry.sha:
            return {"ok": True, "unchanged": True, "sha": entry.sha}
        metadata_extra = None
    else:
        raise MarketplaceRequestError(
            f"'{key}' is not a catalog or marketplace install", 4020
        )

    try:
        _install_plugin_core(
            _catalog_install_identifier(entry),
            force=True,
            ref=entry.sha,
            metadata_extra=metadata_extra,
            catalog_entry=entry if marketplace is None else None,
        )
    except PluginOperationError as exc:
        raise MarketplaceRequestError(str(exc), 5026) from exc
    return {"ok": True, "unchanged": False, "sha": entry.sha}
