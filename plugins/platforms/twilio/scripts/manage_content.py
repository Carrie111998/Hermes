#!/usr/bin/env python3
"""Twilio Content API helper for the twilio Hermes plugin.

Creates and inspects Twilio Content API templates so their Content SID can
be sent as rich RCS content through the twilio platform's send path:

    hermes send --to twilio:+15551234567 "CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    hermes send --to twilio:+15551234567 'CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:{"1":"Alice"}'

RCS-supported Content API types (per Twilio docs): twilio/text, twilio/media,
twilio/card, twilio/carousel. 'twilio/card' and 'twilio/carousel' are
implemented here with a 'media' field on both (schema live-verified —
note the field shape differs: an array on twilio/card, a plain string on
twilio/carousel; see create_card/create_carousel docstrings).
'twilio/quick-reply' is also implemented (schema verified) but is NOT in
Twilio's RCS-supported type list — sends over RCS silently fall back to
plain SMS/MMS rather than rendering reply chips; it's real and WhatsApp-
supported, just not a true RCS rich-content type.

This file intentionally uses Python stdlib HTTP clients only (no aiohttp
dependency), mirroring
optional-skills/productivity/telephony/scripts/telephony.py, so it can run
standalone outside the Hermes venv.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONTENT_API_BASE = "https://content.twilio.com/v1/Content"


class ContentApiError(RuntimeError):
    """Domain-specific failure surfaced to the caller."""


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values() -> dict[str, str]:
    env_file = _hermes_home() / ".env"
    if not env_file.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = raw_line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key, "")
    if value:
        return value
    return _load_dotenv_values().get(key, default)


def _request(method: str, url: str, *, body: dict | None = None) -> dict:
    account_sid = _env("TWILIO_ACCOUNT_SID")
    auth_token = _env("TWILIO_AUTH_TOKEN")
    if not (account_sid and auth_token):
        raise ContentApiError(
            "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set (env or ~/.hermes/.env)"
        )

    creds = base64.b64encode(f"{account_sid}:{auth_token}".encode("ascii")).decode("ascii")
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise ContentApiError(f"Twilio Content API {e.code}: {detail}") from e


def create_quick_reply(
    friendly_name: str,
    body: str,
    actions: list[tuple[str, str]],
    *,
    language: str = "en",
    variables: dict[str, str] | None = None,
) -> dict:
    """actions: list of (title, id) pairs — id is the postback payload."""
    payload: dict[str, Any] = {
        "friendly_name": friendly_name,
        "language": language,
        "types": {
            "twilio/quick-reply": {
                "body": body,
                "actions": [{"title": title, "id": id_} for title, id_ in actions],
            }
        },
    }
    if variables:
        payload["variables"] = variables
    return _request("POST", CONTENT_API_BASE, body=payload)


def create_card(
    friendly_name: str,
    title: str,
    *,
    subtitle: str = "",
    media: str = "",
    actions: list[dict] | None = None,
    language: str = "en",
    variables: dict[str, str] | None = None,
) -> dict:
    """actions: dicts like {'type': 'URL'|'PHONE_NUMBER'|'QUICK_REPLY', 'title': ..., plus 'url'/'phone'/'id'}.

    Live-verified against the Content API (2026-08-26): unlike
    twilio/carousel's per-card 'media' (a single URL string — see
    create_carousel), twilio/card's top-level 'media' must be a JSON
    ARRAY of URL strings — a bare string 400s with "Request input may
    be invalid". Confirmed via GET echo that 'media' is a real field
    name on this content type.
    """
    card: dict[str, Any] = {"title": title}
    if subtitle:
        card["subtitle"] = subtitle
    if media:
        card["media"] = [media]
    if actions:
        card["actions"] = actions
    payload: dict[str, Any] = {
        "friendly_name": friendly_name,
        "language": language,
        "types": {"twilio/card": card},
    }
    if variables:
        payload["variables"] = variables
    return _request("POST", CONTENT_API_BASE, body=payload)


def create_carousel(
    friendly_name: str,
    body: str,
    cards: list[dict],
    *,
    language: str = "en",
    variables: dict[str, str] | None = None,
) -> dict:
    """cards: list of {'title': ..., 'body': ..., 'media': <image url string>, 'actions': [...]}.

    Schema verified against a full worked example in Twilio's docs
    (twilio/carousel: top-level 'body' + 'cards' array; each card has
    'title', 'body', 'media' (a single URL STRING — confirmed live; an
    array here 400s, the opposite of twilio/card's top-level 'media').
    """
    if not cards:
        raise ContentApiError("twilio/carousel requires at least one card")
    payload: dict[str, Any] = {
        "friendly_name": friendly_name,
        "language": language,
        "types": {
            "twilio/carousel": {
                "body": body,
                "cards": cards,
            }
        },
    }
    if variables:
        payload["variables"] = variables
    return _request("POST", CONTENT_API_BASE, body=payload)


def list_content(page_size: int = 20) -> dict:
    return _request("GET", f"{CONTENT_API_BASE}?PageSize={page_size}")


def get_content(content_sid: str) -> dict:
    return _request("GET", f"{CONTENT_API_BASE}/{content_sid}")


def _parse_card_action(raw: str) -> dict:
    """'url:Title:https://...' / 'phone:Title:+1555...' / 'quick_reply:Title:id'."""
    parts = raw.split(":", 2)
    if len(parts) != 3:
        raise ContentApiError(
            f"Invalid --action '{raw}' — expected 'url:Title:https://...', "
            "'phone:Title:+1555...', or 'quick_reply:Title:id'"
        )
    kind, title, value = (p.strip() for p in parts)
    kind = kind.lower()
    if kind == "url":
        return {"type": "URL", "title": title, "url": value}
    if kind == "phone":
        return {"type": "PHONE_NUMBER", "title": title, "phone": value}
    if kind in ("quick_reply", "quick-reply"):
        return {"type": "QUICK_REPLY", "title": title, "id": value}
    raise ContentApiError(f"Unknown action kind '{kind}' in --action '{raw}'")


def _parse_quick_reply_action(raw: str) -> tuple[str, str]:
    """'Title:reply_id'."""
    title, _, id_ = raw.partition(":")
    if not id_:
        raise ContentApiError(f"Invalid --action '{raw}' — expected 'Title:reply_id'")
    return title.strip(), id_.strip()


def _parse_var(raw: str) -> tuple[str, str]:
    key, _, value = raw.partition("=")
    if not key:
        raise ContentApiError(f"Invalid --var '{raw}' — expected KEY=VALUE")
    return key.strip(), value


def _print_result(result: dict) -> None:
    print(json.dumps(result, indent=2))
    sid = result.get("sid")
    if sid:
        print(f"\nContent SID: {sid}", file=sys.stderr)
        print(
            f'Use it: hermes send --to twilio:+15551234567 "CONTENT:{sid}"',
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="List existing Content API templates")
    p.add_argument("--page-size", type=int, default=20)

    p = sub.add_parser("get", help="Show one Content API template")
    p.add_argument("content_sid")

    p = sub.add_parser(
        "create-quick-reply",
        help="Create a twilio/quick-reply template (WhatsApp — NOT in Twilio's documented RCS-supported type list; sends over RCS silently fall back to SMS)",
    )
    p.add_argument("--friendly-name", required=True)
    p.add_argument("--body", required=True, help="Message body; use {{1}}, {{2}}, ... for variables")
    p.add_argument(
        "--action", action="append", default=[], metavar="TITLE:ID",
        help="Repeatable. 'Title:reply_id'",
    )
    p.add_argument("--language", default="en")
    p.add_argument(
        "--var", action="append", default=[], metavar="KEY=VALUE",
        help="Sample/default variable value, repeatable",
    )

    p = sub.add_parser(
        "create-card", help="Create a twilio/card rich card template (RCS + WhatsApp)"
    )
    p.add_argument("--friendly-name", required=True)
    p.add_argument("--title", required=True, help="Card headline; use {{1}}, {{2}}, ... for variables")
    p.add_argument("--subtitle", default="")
    p.add_argument(
        "--media", default="",
        help="Image URL for the card header. NOT confirmed against a twilio/card doc example — inferred from twilio/carousel's per-card schema; verify delivery before relying on it.",
    )
    p.add_argument(
        "--action", action="append", default=[], metavar="KIND:TITLE:VALUE",
        help="Repeatable. 'url:Title:https://...' / 'phone:Title:+1555...' / 'quick_reply:Title:id'",
    )
    p.add_argument("--language", default="en")
    p.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")

    p = sub.add_parser(
        "create-carousel",
        help="Create a twilio/carousel template (RCS + WhatsApp) — multiple cards, each with title/body/media/actions",
    )
    p.add_argument("--friendly-name", required=True)
    p.add_argument("--body", required=True, help="Carousel intro text; use {{1}}, {{2}}, ... for variables")
    p.add_argument(
        "--cards-json", required=True,
        help=(
            "JSON array of cards, e.g. "
            '[{"title":"Twilio Hoodie","body":"Warm as owl feathers.",'
            '"media":"https://example.com/hoodie.jpeg",'
            '"actions":[{"type":"QUICK_REPLY","title":"I want it!","id":"want_hoodie"},'
            '{"type":"URL","title":"Shop","url":"https://example.com"}]}]'
        ),
    )
    p.add_argument("--language", default="en")
    p.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")

    args = parser.parse_args()

    try:
        if args.command == "list":
            result = list_content(args.page_size)
        elif args.command == "get":
            result = get_content(args.content_sid)
        elif args.command == "create-quick-reply":
            actions = [_parse_quick_reply_action(raw) for raw in args.action]
            variables = dict(_parse_var(v) for v in args.var) or None
            result = create_quick_reply(
                args.friendly_name, args.body, actions,
                language=args.language, variables=variables,
            )
        elif args.command == "create-card":
            actions = [_parse_card_action(raw) for raw in args.action]
            variables = dict(_parse_var(v) for v in args.var) or None
            result = create_card(
                args.friendly_name, args.title, subtitle=args.subtitle,
                media=args.media, actions=actions, language=args.language,
                variables=variables,
            )
        elif args.command == "create-carousel":
            try:
                cards = json.loads(args.cards_json)
            except json.JSONDecodeError as e:
                raise ContentApiError(f"Invalid --cards-json: {e}")
            if not isinstance(cards, list):
                raise ContentApiError("--cards-json must be a JSON array of card objects")
            variables = dict(_parse_var(v) for v in args.var) or None
            result = create_carousel(
                args.friendly_name, args.body, cards,
                language=args.language, variables=variables,
            )
        else:
            parser.error(f"Unknown command {args.command}")
            return 2
    except ContentApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
