#!/usr/bin/env python3
"""Genere une image via l'API Magnific Mystic (POST + polling + telechargement).

Usage:
    python3 generate.py "PROMPT" [-o OUT] [--resolution 1k|2k|4k]
                        [--aspect square_1_1|widescreen_16_9|...]
                        [--model realism|fluid|zen|flexible|super_real|editorial_portraits]
                        [--creative-detailing 0-100]
                        [--engine automatic|illusio|sharpy|sparkle]
                        [--structure-reference FILE] [--structure-strength 0-100]
                        [--style-reference FILE] [--adherence 0-100] [--hdr 0-100]

Requiert MAGNIFIC_API_KEY. La sortie est du JPEG (extension ajustee auto).
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
PATH = "/ai/mystic"

MODELS = ["realism", "fluid", "zen", "flexible", "super_real", "editorial_portraits"]
RESOLUTIONS = ["1k", "2k", "4k"]
ASPECTS = [
    "square_1_1", "classic_4_3", "traditional_3_4", "widescreen_16_9",
    "social_story_9_16", "smartphone_horizontal_20_9", "smartphone_vertical_9_20",
    "standard_3_2", "portrait_2_3", "horizontal_2_1", "vertical_1_2",
    "social_5_4", "social_post_4_5",
]
# Le modele 'fluid' (Google Imagen 3) n'accepte qu'un sous-ensemble de ratios.
FLUID_ASPECTS = {
    "square_1_1", "social_story_9_16", "widescreen_16_9",
    "traditional_3_4", "classic_4_3",
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


def b64_file(path: str) -> str:
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode()


def image_dims(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", raw[16:24])
        return f"{w}x{h} PNG"
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return f"{w}x{h} JPEG"
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
        return "JPEG"
    return "inconnu"


def ext_for(raw: bytes) -> str:
    if raw[:2] == b"\xff\xd8":
        return ".jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ".bin"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("-o", "--out", default="generated")
    parser.add_argument("--resolution", default="1k", choices=RESOLUTIONS)
    parser.add_argument("--aspect", default="square_1_1", choices=ASPECTS)
    parser.add_argument("--model", default="realism", choices=MODELS)
    parser.add_argument("--creative-detailing", type=int)
    parser.add_argument("--engine", default="automatic",
                        choices=["automatic", "illusio", "sharpy", "sparkle"])
    parser.add_argument("--structure-reference")
    parser.add_argument("--structure-strength", type=int)
    parser.add_argument("--style-reference")
    parser.add_argument("--adherence", type=int)
    parser.add_argument("--hdr", type=int)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    if args.model == "fluid" and args.aspect not in FLUID_ASPECTS:
        sys.exit(
            f"Le modele 'fluid' n'accepte que {sorted(FLUID_ASPECTS)}; "
            f"'{args.aspect}' sera ignore silencieusement par l'API."
        )

    payload = {
        "prompt": args.prompt,
        "resolution": args.resolution,
        "aspect_ratio": args.aspect,
        "model": args.model,
        "engine": args.engine,
    }
    if args.creative_detailing is not None:
        payload["creative_detailing"] = args.creative_detailing
    if args.adherence is not None:
        payload["adherence"] = args.adherence
    if args.hdr is not None:
        payload["hdr"] = args.hdr
    if args.structure_reference:
        payload["structure_reference"] = b64_file(args.structure_reference)
        if args.structure_strength is not None:
            payload["structure_strength"] = args.structure_strength
    if args.style_reference:
        payload["style_reference"] = b64_file(args.style_reference)

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
        base, existing_ext = os.path.splitext(args.out)
        ext = existing_ext or ext_for(raw)
        suffix = "" if len(urls) == 1 else f"_{index}"
        out = f"{base}{suffix}{ext}"
        with open(out, "wb") as handle:
            handle.write(raw)
        print(f"ecrit {out} ({image_dims(raw)}, {len(raw)} octets)")


if __name__ == "__main__":
    main()
