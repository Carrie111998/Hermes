"""hermes providers — see which providers Hermes can actually use, and
compare models by value-for-money.

Subcommands:
  hermes providers [list]      Show every supported provider + auth status
  hermes providers compare     Rank OpenRouter models by value (live pricing)
  hermes providers search Q    Find models matching a query
  hermes providers best        Top-value models for a task + apply commands
  hermes providers endpoints M List the providers serving a model (per-provider
                                pricing/latency/uptime — mirrors the OpenRouter
                                model-page table)
  hermes providers route       Show or set provider_routing (sort/order/only/ignore)

Data sources:
  - Provider availability: the canonical provider list
    (``hermes_cli.models.CANONICAL_PROVIDERS``) merged with the picker's
    authenticated-provider detection (``hermes_cli.model_switch``) and the
    provider transport overlays (``hermes_cli.providers.HERMES_OVERLAYS``).
  - Value ranking: live OpenRouter catalog or the bundled models.dev
    registry with ``--offline`` (see ``hermes_cli/provider_pricing.py``).
  - Per-model endpoints: OpenRouter ``/api/v1/models/{id}/endpoints``.
  - Routing: writes the documented top-level ``provider_routing`` config
    section, which Hermes forwards to OpenRouter as the ``provider`` body
    object (``sort``/``order``/``only``/``ignore``/``require_parameters``/
    ``data_collection``). See
    https://hermes-agent.nousresearch.com/docs/user-guide/features/provider-routing

Heavy hermes imports are deliberately lazy (inside handlers) so importing
this module stays cheap for the CLI fast-path and for tests.
"""
from __future__ import annotations

from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# list — provider availability
# ---------------------------------------------------------------------------


def _transport_for(slug: str, overlays: Dict[str, Any]) -> str:
    ov = overlays.get(slug)
    return getattr(ov, "transport", "") if ov is not None else ""


def _auth_type_for(slug: str, registry: Dict[str, Any]) -> str:
    cfg = registry.get(slug)
    return getattr(cfg, "auth_type", "") if cfg is not None else ""


def _key_env_for(slug: str, registry: Dict[str, Any]) -> str:
    cfg = registry.get(slug)
    if cfg is None:
        return ""
    envs = getattr(cfg, "api_key_env_vars", ()) or ()
    return str(envs[0]) if envs else ""


def build_list_rows(
    auth_rows: List[dict],
    canonical_providers: List[Any],
    labels: Dict[str, str],
    registry: Dict[str, Any],
    overlays: Dict[str, Any],
) -> List[dict]:
    """Merge authenticated provider rows with canonical skeleton rows.

    Pure — takes already-resolved inputs so tests never touch credentials,
    the network, or the config store. Authenticated rows keep their model
    counts; canonical providers the user has never configured appear as
    ``authenticated=False`` skeletons.
    """
    rows: List[dict] = []
    seen = set()
    for row in auth_rows:
        slug = str(row.get("slug") or "").strip().lower()
        if not slug:
            continue
        seen.add(slug)
        merged = dict(row)
        merged.setdefault("authenticated", True)
        merged["transport"] = _transport_for(slug, overlays)
        merged["auth_type"] = str(row.get("auth_type") or _auth_type_for(slug, registry))
        merged["key_env"] = str(row.get("key_env") or _key_env_for(slug, registry))
        rows.append(merged)
    for entry in canonical_providers:
        slug = str(getattr(entry, "slug", entry.get("slug") if isinstance(entry, dict) else "")).lower()
        if not slug or slug in seen:
            continue
        rows.append(
            {
                "slug": slug,
                "name": labels.get(slug, getattr(entry, "label", slug)),
                "is_current": False,
                "is_user_defined": False,
                "models": [],
                "total_models": 0,
                "source": "canonical",
                "authenticated": False,
                "transport": _transport_for(slug, overlays),
                "auth_type": _auth_type_for(slug, registry),
                "key_env": _key_env_for(slug, registry),
            }
        )
    return rows


def format_list_rows(rows: List[dict]) -> List[str]:
    """Render provider rows as an aligned table (2-space indented lines)."""
    headers = ["PROVIDER", "SLUG", "AUTH", "TRANSPORT", "MODELS"]
    widths = [len(h) for h in headers]
    cells: List[List[str]] = []
    for row in rows:
        slug = str(row.get("slug") or "")
        name = str(row.get("name") or slug)
        if row.get("is_current"):
            name = "* " + name
        line = [
            name,
            slug,
            "yes" if row.get("authenticated") else "no",
            str(row.get("transport") or "-"),
            str(int(row.get("total_models") or 0)),
        ]
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
        cells.append(line)
    out = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  " + "  ".join("-" * w for w in widths))
    for line in cells:
        out.append("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(line)))
    out.append("")
    out.append("  AUTH yes = credentials present.  MODELS = curated model count.")
    out.append("  '* ' marks the provider currently configured in config.yaml.")
    return out


def _cmd_list(args) -> None:  # noqa: ARG001
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.inventory import load_picker_context
    from hermes_cli.model_switch import list_authenticated_providers
    from hermes_cli.models import CANONICAL_PROVIDERS, _PROVIDER_LABELS
    from hermes_cli.providers import HERMES_OVERLAYS

    ctx = load_picker_context()
    try:
        auth_rows = list_authenticated_providers(
            current_provider=ctx.current_provider,
            current_model=ctx.current_model,
            current_base_url=ctx.current_base_url,
            user_providers=ctx.user_providers,
            custom_providers=ctx.custom_providers,
            excluded_providers=ctx.excluded_providers,
            probe_custom_providers=False,
        )
    except Exception:
        # Credential detection must never take the command down.
        auth_rows = []
    rows = build_list_rows(
        auth_rows,
        CANONICAL_PROVIDERS,
        _PROVIDER_LABELS,
        PROVIDER_REGISTRY,
        HERMES_OVERLAYS,
    )
    print()
    for line in format_list_rows(rows):
        print(line)
    print()


# ---------------------------------------------------------------------------
# compare / search / best — value ranking
# ---------------------------------------------------------------------------


def _gather_rows(args) -> List[dict]:
    """Live OpenRouter rows by default; bundled models.dev rows with --offline."""
    from hermes_cli.provider_pricing import fetch_openrouter_models, offline_rows

    if getattr(args, "offline", False):
        return offline_rows()
    return fetch_openrouter_models()


def _source_label(args) -> str:
    return "bundled models.dev catalog" if getattr(args, "offline", False) else "live OpenRouter API"


def _no_data_hint(args) -> None:
    print()
    print("  Could not load the OpenRouter model catalog.")
    print("  Check your network, or use --offline for the bundled models.dev data.")
    print()


def _cmd_compare(args) -> None:
    from hermes_cli.provider_pricing import rank_by_value, format_rows

    rows = _gather_rows(args)
    if not rows:
        _no_data_hint(args)
        return
    ranked = rank_by_value(
        rows,
        min_context=getattr(args, "min_context", 0) or 0,
        task=getattr(args, "task", None),
        include_all=bool(getattr(args, "include_all", False)),
    )
    top = getattr(args, "top", 10) or 10
    shown = ranked[:top]
    print()
    print(
        f"  Top {len(shown)} value picks (agentic-capable, cheapest first) — "
        f"source: {_source_label(args)}"
    )
    print()
    for line in format_rows(shown):
        print(line)
    if ranked and len(ranked) > len(shown):
        print()
        print(f"  {len(ranked)} models matched the filters; showing the {len(shown)} cheapest.")
    print()
    print("  Apply one with:  hermes providers best   (prints the exact config commands)")
    print()


def _cmd_search(args) -> None:
    from hermes_cli.provider_pricing import format_rows, search_models

    rows = _gather_rows(args)
    if not rows:
        _no_data_hint(args)
        return
    hits = search_models(rows, args.query)
    top = getattr(args, "top", 10) or 10
    shown = hits[:top]
    print()
    print(
        f"  {len(hits)} match(es) for '{args.query}' — source: {_source_label(args)}"
    )
    print()
    for line in format_rows(shown):
        print(line)
    if hits and len(hits) > len(shown):
        print()
        print(f"  Showing the first {len(shown)}; refine the query to narrow.")
    print()


def _cmd_best(args) -> None:
    from hermes_cli.provider_pricing import format_rows, rank_by_value

    rows = _gather_rows(args)
    if not rows:
        _no_data_hint(args)
        return
    task = getattr(args, "task", "chat") or "chat"
    ranked = rank_by_value(
        rows,
        min_context=getattr(args, "min_context", 0) or 0,
        task=task,
        include_all=bool(getattr(args, "include_all", False)),
        top=getattr(args, "top", 5) or 5,
    )
    print()
    print(
        f"  Best-value models for task='{task}' (agentic-capable, cheapest first) — "
        f"source: {_source_label(args)}"
    )
    print()
    for line in format_rows(ranked):
        print(line)
    print()
    if ranked:
        print("  To apply the top pick:")
        print("    hermes config set model.provider openrouter")
        print(f"    hermes config set model.default {ranked[0]['id']}")
    else:
        print("  No models matched the current filters (try --min-context 0 / removing --task).")
    print()


# ---------------------------------------------------------------------------
# endpoints / route — per-model providers + provider_routing
# ---------------------------------------------------------------------------


def _cmd_endpoints(args) -> None:
    """Show which providers serve a model, with per-provider pricing."""
    from hermes_cli.provider_pricing import fetch_model_endpoints, format_endpoint_rows

    model_id = args.model
    rows = fetch_model_endpoints(model_id)
    print()
    if not rows:
        print(f"  Could not fetch endpoint data for '{model_id}'.")
        print("  Check the model id (e.g. deepseek/deepseek-v4-flash) and your network.")
        print()
        print("  Tip: set `hermes providers route --sort price` to auto-route to the")
        print("  cheapest available provider, or `--order DeepInfra,Decart` to force one.")
        print()
        return
    print(f"  Providers serving {model_id} — live OpenRouter API")
    print()
    for line in format_endpoint_rows(rows):
        print(line)
    print()
    print("  Route a provider with:  hermes providers route --sort price   (auto-cheapest)")
    print("                    or:  hermes providers route --order <Provider>")
    print()


def _parse_csv(raw: str) -> list:
    """'A, B ,C' -> ['A', 'B', 'C'] (empty entries dropped)."""
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _set_routing_value(pr: dict, key: str, value) -> None:
    if value is None:
        return
    pr[key] = list(value) if isinstance(value, (list, tuple)) else value


def _cmd_route(args) -> None:
    """Show or update the ``provider_routing`` config section."""
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    raw = cfg.get("provider_routing") or {}
    if not isinstance(raw, dict):
        raw = {}

    if getattr(args, "clear", False):
        cfg.pop("provider_routing", None)
        save_config(cfg)
        print()
        print("  provider_routing cleared — Hermes will use OpenRouter's default routing.")
        print()
        return

    changes: list[str] = []
    pr = dict(raw)
    if args.sort is not None:
        pr["sort"] = args.sort
        changes.append(f"sort={args.sort}")
    if args.order is not None:
        pr["order"] = _parse_csv(args.order)
        changes.append(f"order={pr['order']}")
    if args.only is not None:
        pr["only"] = _parse_csv(args.only)
        changes.append(f"only={pr['only']}")
    if args.ignore is not None:
        pr["ignore"] = _parse_csv(args.ignore)
        changes.append(f"ignore={pr['ignore']}")
    if getattr(args, "require_parameters", False):
        pr["require_parameters"] = True
        changes.append("require_parameters=true")
    if args.data_collection is not None:
        pr["data_collection"] = args.data_collection
        changes.append(f"data_collection={args.data_collection}")

    print()
    if not changes:
        if pr:
            print("  Current provider_routing:")
            for key, value in pr.items():
                print(f"    {key}: {value}")
        else:
            print("  provider_routing is not set — Hermes uses OpenRouter's default routing.")
            print()
            print("  Cheapest-first:   hermes providers route --sort price")
            print("  Force providers:  hermes providers route --order DeepInfra,Decart")
            print("  Whitelist:        hermes providers route --only DeepInfra")
            print("  Blacklist:        hermes providers route --ignore SomeProvider")
            print("  Clear all:        hermes providers route --clear")
        print()
        return

    if pr:
        cfg["provider_routing"] = pr
    else:
        cfg.pop("provider_routing", None)
    save_config(cfg)

    print("  provider_routing updated:")
    for key, value in pr.items():
        print(f"    {key}: {value}")
    print()
    print("  Takes effect on the next session (restart the CLI or gateway).")
    print("  Only applies to OpenRouter / Nous Portal routes.")
    print()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def cmd_providers(args) -> None:
    """Top-level dispatcher for ``hermes providers [subcommand]``."""
    sub = getattr(args, "providers_command", None)
    if sub in {None, "", "list", "ls"}:
        _cmd_list(args)
    elif sub in {"compare", "cmp"}:
        _cmd_compare(args)
    elif sub == "search":
        _cmd_search(args)
    elif sub == "best":
        _cmd_best(args)
    elif sub in {"endpoints", "endpoint"}:
        _cmd_endpoints(args)
    elif sub == "route":
        _cmd_route(args)
    else:
        print(f"Unknown providers subcommand: {sub}")
        print("Use one of: list, compare, search, best, endpoints, route")
