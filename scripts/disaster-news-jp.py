#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JMA earthquake alert: report only records with verified hypocenter data."""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

JMA_BASE = "https://www.jma.go.jp/bosai/quake/data/"
JST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}


def fetch_json(url: str):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def parse_coordinate(value: str):
    """Parse JMA +lat+lon-depth_m/ notation."""
    if not isinstance(value, str):
        return None
    match = re.match(r"^([+-]?\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)-([0-9]+)", value)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2)), int(match.group(3))


def detail_fields(detail: dict):
    body = detail.get("Body") or detail.get("body") or {}
    earthquake = body.get("Earthquake") or body.get("earthquake") or {}
    hypocenter = earthquake.get("Hypocenter") or earthquake.get("hypocenter") or {}
    area = hypocenter.get("Area") or hypocenter.get("area") or {}
    magnitude = earthquake.get("Magnitude") or earthquake.get("magnitude")
    if isinstance(magnitude, dict):
        magnitude = magnitude.get("Value") or magnitude.get("value")
    coordinate = area.get("Coordinate") or area.get("coordinate")
    parsed = parse_coordinate(coordinate)
    values = {
        "name": area.get("Name") or area.get("name"),
        "magnitude": magnitude,
        "coordinate": parsed,
    }
    intensity = body.get("Intensity") or body.get("intensity") or {}
    observation = intensity.get("Observation") or intensity.get("observation") or {}
    values["maxi"] = observation.get("MaxInt") or observation.get("maxInt")
    return values


def verified_event(entry: dict):
    """Return a complete event, or None; never manufacture 'unknown' fields."""
    name = entry.get("anm") or None
    magnitude = entry.get("mag") or None
    coordinate = parse_coordinate(str(entry.get("cod") or ""))
    maxi = entry.get("maxi") or None

    filename = entry.get("json") or ""
    # VXSE5k contains the authoritative combined report. VXSE51 is intensity-only.
    if filename and ("VXSE5k" in filename or "VXSE52" in filename):
        try:
            fields = detail_fields(fetch_json(JMA_BASE + filename))
            name = fields["name"] or name
            magnitude = fields["magnitude"] or magnitude
            coordinate = fields["coordinate"] or coordinate
            maxi = fields["maxi"] or maxi
        except Exception:
            pass

    try:
        intensity = int(maxi)
    except (TypeError, ValueError):
        return None
    if not name or magnitude in (None, "") or not coordinate:
        return None

    latitude, longitude, depth_m = coordinate
    # JMA's cod depth is metres; 0 means sea-level/very shallow and is valid.
    depth_km = depth_m // 1000 if depth_m >= 1000 else depth_m
    return {
        "name": str(name),
        "magnitude": str(magnitude),
        "latitude": latitude,
        "longitude": longitude,
        "depth_km": depth_km,
        "intensity": intensity,
    }


def parse_quakes(data, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    candidates = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            timestamp = datetime.fromisoformat((entry.get("at") or "").replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp < cutoff:
                continue
            intensity = int(str(entry.get("maxi") or 0))
        except (TypeError, ValueError):
            continue
        if intensity < 3:
            continue
        event_id = entry.get("eid") or entry.get("json") or entry.get("at")
        # Prefer complete combined reports over intensity-only reports.
        old = candidates.get(event_id)
        if old is None or "VXSE5k" in (entry.get("json") or ""):
            candidates[event_id] = (timestamp, entry)

    results = []
    for timestamp, entry in candidates.values():
        event = verified_event(entry)
        if event is not None:
            event["time"] = timestamp
            results.append(event)
    return sorted(results, key=lambda item: item["time"], reverse=True)


def main():
    try:
        data = fetch_json(JMA_BASE + "list.json")
        quakes = parse_quakes(data)
        now = datetime.now(JST)
    except Exception as exc:
        now = datetime.now(JST)
        quakes = []
        print(f"【災害・安全保障速報｜{now:%Y-%m-%d %H:%M JST}】\n\n■ 判定\n- JMA取得失敗: {exc}\n- 未確認の地震は速報として掲載していません。")
        return

    lines = [f"【災害・安全保障速報｜{now:%Y-%m-%d %H:%M JST}】", "", "■ 判定"]
    if quakes:
        lines.append(f"- 確認済み速報: {len(quakes)}件（過去1時間以内、震度3以上）")
        for index, event in enumerate(quakes[:5], 1):
            lines.append(
                f"  {index}. {event['name']} ({event['latitude']:.2f}°, {event['longitude']:.2f}°) "
                f"M{event['magnitude']} 深さ{event['depth_km']}km 震度{event['intensity']} "
                f"({event['time'].astimezone(JST):%Y-%m-%d %H:%M JST})"
            )
        if len(quakes) > 5:
            lines.append(f"  他 {len(quakes) - 5} 件")
    else:
        lines.append("- 確認済み速報: なし（完全な震源情報を確認できた震度3以上の地震なし）")
    lines += [
        f"- 根拠時刻: {now:%Y-%m-%d %H:%M:%S %z}",
        "",
        "■ 影響",
        "- 地震が発生しています。津波情報にもご注意ください。" if quakes else "- 現在のところ、注目すべき地震はありません。",
        "",
        "■ 次に取る行動",
        "- 最新の情報は気象庁ウェブサイト等でご確認ください。",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
