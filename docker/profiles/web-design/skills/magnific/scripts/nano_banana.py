#!/usr/bin/env python3
"""Genere une image via Nano Banana Pro (Google) sur l'API REST Magnific.

Route NON documentee dans llms.txt mais bien active :
    POST /v1/ai/text-to-image/nano-banana-pro
    GET  /v1/ai/text-to-image/nano-banana-pro/{task_id}

A privilegier des qu'il y a du TEXTE a composer dans l'image (titre, affiche,
UI, infographie) : les modeles de diffusion (Mystic, Flux, Seedream) produisent
du pseudo-texte. Sortie PNG.

Usage:
    python3 nano_banana.py "PROMPT" [-o OUT] [--resolution 1K|2K|4K]
                           [--aspect 1:1|16:9|9:16|3:4|4:3|2:3|3:2|5:4|4:5|21:9]
                           [--reference URL [--reference URL ...]]
                           [--reference-text TXT]

Requiert MAGNIFIC_API_KEY.
"""
import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("MAGNIFIC_API_BASE", "https://api.magnific.com/v1")
PATH = "/ai/text-to-image/nano-banana-pro"

# Notation Nano Banana : "16:9". Ne pas confondre avec Mystic ("widescreen_16_9").
ASPECTS = ["1:1", "2:3", "3:2", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16", "21:9"]
RESOLUTIONS = ["1K", "2K", "4K"]
MAX_REFERENCES = 14
PROMPT_MAX = 3000

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


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
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code} sur {method} {path}\n{exc.read().decode(errors='replace')[:500]}")


def mime_for(url: str) -> str:
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    return MIME_BY_EXT.get(ext, "image/jpeg")


def png_dims(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", raw[16:24])
        return f"{w}x{h} PNG"
    if raw[:2] == b"\xff\xd8":
        return "JPEG"
    return "inconnu"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("-o", "--out", default="nano_banana.png")
    parser.add_argument("--resolution", default="1K", choices=RESOLUTIONS)
    parser.add_argument("--aspect", default="1:1", choices=ASPECTS)
    parser.add_argument("--reference", action="append", default=[],
                        help="URL publique d'une image de reference (repetable, max 14). "
                             "PAS de base64 ici, contrairement a Mystic.")
    parser.add_argument("--reference-text", help="Texte associe aux references.")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    if not 2 <= len(args.prompt) <= PROMPT_MAX:
        sys.exit(f"prompt: longueur doit etre entre 2 et {PROMPT_MAX} caracteres.")
    if len(args.reference) > MAX_REFERENCES:
        sys.exit(f"maximum {MAX_REFERENCES} images de reference ({len(args.reference)} fournies).")
    for url in args.reference:
        if not url.startswith(("http://", "https://", "gs://")):
            sys.exit(
                f"reference '{url[:60]}' : l'API exige une URL publique (ou GCS), "
                "pas un chemin local ni du base64."
            )

    payload = {
        "prompt": args.prompt,
        "aspect_ratio": args.aspect,
        "resolution": args.resolution,
    }
    if args.reference:
        payload["reference_images"] = [
            {"image": url, "mime_type": mime_for(url),
             **({"text": args.reference_text} if args.reference_text else {})}
            for url in args.reference
        ]

    task = request("POST", PATH, payload)["data"]
    task_id = task["task_id"]
    print(f"task {task_id} status={task['status']}")

    deadline = time.time() + args.timeout
    status = task["status"]
    while status in ("CREATED", "IN_PROGRESS"):
        if time.time() > deadline:
            sys.exit(f"Timeout apres {args.timeout}s (task {task_id} encore {status}).")
        time.sleep(args.poll_interval)
        task = request("GET", f"{PATH}/{task_id}")["data"]
        status = task["status"]
        print(f"  status={status}")

    if status != "COMPLETED":
        sys.exit(f"Task {status}: {task.get('error')}")

    urls = task.get("generated") or []
    if not urls:
        sys.exit("COMPLETED mais aucune URL dans generated[].")

    # URLs CDN signees et expirantes : telecharger immediatement.
    for index, url in enumerate(urls):
        with urllib.request.urlopen(url, timeout=300) as resp:
            raw = resp.read()
        base, ext = os.path.splitext(args.out)
        suffix = "" if len(urls) == 1 else f"_{index}"
        out = f"{base}{suffix}{ext or '.png'}"
        with open(out, "wb") as handle:
            handle.write(raw)
        print(f"ecrit {out} ({png_dims(raw)}, {len(raw)} octets)")


if __name__ == "__main__":
    main()
