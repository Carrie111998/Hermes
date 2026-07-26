#!/usr/bin/env python3
"""Upscale une image via l'API Magnific (POST + polling + telechargement).

Usage:
    python3 upscale.py SOURCE [-o OUT.png] [--mode creative|precision]
                       [--scale 2x|4x|8x|16x] [--optimized-for STYLE]
                       [--engine automatic|magnific_illusio|magnific_sharpy|magnific_sparkle]
                       [--prompt TXT] [--creativity N] [--hdr N]
                       [--resemblance N] [--fractality N]

SOURCE : chemin local OU URL http(s). L'API n'accepte QUE du base64, donc une
URL est telechargee localement d'abord puis encodee.

Requiert MAGNIFIC_API_KEY dans l'environnement.
"""
import argparse
import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("MAGNIFIC_API_BASE", "https://api.magnific.com/v1")
ENDPOINTS = {
    "creative": "/ai/image-upscaler",
    "precision": "/ai/image-upscaler-precision",
}
MAX_OUTPUT_PIXELS = 25_300_000


def api_key() -> str:
    key = os.environ.get("MAGNIFIC_API_KEY")
    if not key:
        sys.exit("MAGNIFIC_API_KEY absente de l'environnement.")
    return key


def request(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={"x-magnific-api-key": api_key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        sys.exit(f"HTTP {exc.code} sur {method} {path}\n{detail}")


def read_source(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=120) as resp:
            return resp.read()
    with open(source, "rb") as handle:
        return handle.read()


def png_size(raw: bytes) -> tuple[int, int] | None:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", raw[16:24])
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("-o", "--out", default="upscaled.png")
    parser.add_argument("--mode", choices=sorted(ENDPOINTS), default="creative")
    parser.add_argument("--scale", default="2x", choices=["2x", "4x", "8x", "16x"])
    parser.add_argument("--optimized-for", default="standard")
    parser.add_argument("--engine", default="automatic")
    parser.add_argument("--prompt")
    parser.add_argument("--creativity", type=int)
    parser.add_argument("--hdr", type=int)
    parser.add_argument("--resemblance", type=int)
    parser.add_argument("--fractality", type=int)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    raw = read_source(args.source)
    size = png_size(raw)
    if size:
        factor = int(args.scale.rstrip("x"))
        out_pixels = size[0] * factor * size[1] * factor
        print(f"source {size[0]}x{size[1]} -> sortie ~{out_pixels/1e6:.1f} MP")
        if out_pixels > MAX_OUTPUT_PIXELS:
            sys.exit(
                f"Sortie {out_pixels/1e6:.1f} MP > limite 25.3 MP. "
                "Baisse --scale ou recadre la source."
            )

    payload = {
        "image": base64.b64encode(raw).decode(),
        "scale_factor": args.scale,
        "optimized_for": args.optimized_for,
        "engine": args.engine,
    }
    for name in ("prompt", "creativity", "hdr", "resemblance", "fractality"):
        value = getattr(args, name)
        if value is not None:
            payload[name] = value

    task = request("POST", ENDPOINTS[args.mode], payload)["data"]
    task_id = task["task_id"]
    print(f"task {task_id} status={task['status']}")

    deadline = time.time() + args.timeout
    status = task["status"]
    while status in ("CREATED", "IN_PROGRESS"):
        if time.time() > deadline:
            sys.exit(f"Timeout apres {args.timeout}s (task {task_id} encore {status}).")
        time.sleep(args.poll_interval)
        task = request("GET", f"{ENDPOINTS[args.mode]}/{task_id}")["data"]
        status = task["status"]
        print(f"  status={status}")

    if status != "COMPLETED":
        sys.exit(f"Task {status}: {task.get('error')}")

    urls = task.get("generated") or []
    if not urls:
        sys.exit("COMPLETED mais aucune URL dans generated[].")

    # Les URLs CDN sont signees et expirent : telecharger immediatement.
    with urllib.request.urlopen(urls[0], timeout=300) as resp:
        data = resp.read()
    with open(args.out, "wb") as handle:
        handle.write(data)

    out_size = png_size(data)
    dims = f"{out_size[0]}x{out_size[1]}" if out_size else "?"
    print(f"ecrit {args.out} ({dims}, {len(data)} octets)")


if __name__ == "__main__":
    main()
