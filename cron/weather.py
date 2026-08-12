"""Bounded weather retrieval for the built-in morning briefing.

This is deliberately a cron-side data source rather than an agent terminal
instruction.  The scheduled agent receives a small, redacted result and never
needs to request approval for a weather URL.

The endpoint is intentionally fixed to wttr.in.  Only the location is
configurable, which keeps the cron data source from becoming an arbitrary URL
fetcher.  Configure it in ``config.yaml``::

    cron:
      morning_brief:
        weather_location: Taipei

Missing configuration and upstream failures are represented as a successful,
non-blocking ``WEATHER_UNAVAILABLE`` result so the rest of the briefing can
still run.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Mapping

BUILTIN_SCRIPT = "builtin:morning-brief-weather"
_ENDPOINT = "https://wttr.in/"
_REQUEST_TIMEOUT_SECONDS = 5.0
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.2
_MAX_RESPONSE_BYTES = 256_000

__all__ = ["BUILTIN_SCRIPT", "fetch_morning_brief_weather"]


def _weather_config() -> Mapping[str, Any]:
    """Return the morning-brief weather config without exposing secrets."""

    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
    except Exception:
        return {}

    cron_config = config.get("cron", {}) if isinstance(config, dict) else {}
    morning_config = (
        cron_config.get("morning_brief", {})
        if isinstance(cron_config, dict)
        else {}
    )
    return morning_config if isinstance(morning_config, dict) else {}


def _location() -> str:
    value = _weather_config().get("weather_location") or ""
    # Keep user-controlled output bounded and prevent a location value from
    # becoming an accidental second instruction in the injected context.
    return " ".join(str(value).split())[:120]


def _request_json(location: str) -> dict[str, Any]:
    query = urllib.parse.quote(location, safe="")
    url = f"{_ENDPOINT}{query}?format=j1"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hermes-Agent/cron-weather"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response is not an object")
    return payload


def _first(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _format_weather(payload: Mapping[str, Any], location: str) -> str:
    current_items = payload.get("current_condition")
    if not isinstance(current_items, list) or not current_items:
        raise ValueError("missing current conditions")
    current = current_items[0]
    if not isinstance(current, dict):
        raise ValueError("invalid current conditions")

    descriptions = current.get("weatherDesc")
    description = ""
    if isinstance(descriptions, list) and descriptions and isinstance(descriptions[0], dict):
        description = _first(descriptions[0], "value")
    temperature = _first(current, "temp_C")
    feels_like = _first(current, "FeelsLikeC")
    humidity = _first(current, "humidity")
    precipitation = _first(current, "chanceofrain")
    wind = _first(current, "windspeedKmph")

    details = []
    if description:
        details.append(description)
    if temperature:
        details.append(f"{temperature} C")
    if feels_like:
        details.append(f"feels like {feels_like} C")
    if humidity:
        details.append(f"humidity {humidity}%")
    if precipitation:
        details.append(f"rain chance {precipitation}%")
    if wind:
        details.append(f"wind {wind} km/h")
    if not details:
        raise ValueError("current conditions are empty")

    return "WEATHER_OK\nLocation: " + location + "\nCurrent: " + ", ".join(details)


def fetch_morning_brief_weather() -> str:
    """Return bounded weather context; never fail the morning briefing."""

    location = _location()
    if not location:
        return (
            "WEATHER_UNAVAILABLE: no weather location configured; set "
            "cron.morning_brief.weather_location in config.yaml"
        )

    for attempt in range(_MAX_ATTEMPTS):
        try:
            return _format_weather(_request_json(location), location)
        except (OSError, ValueError, TypeError, UnicodeError):
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS)

    return "WEATHER_UNAVAILABLE: weather service did not respond with usable data"
