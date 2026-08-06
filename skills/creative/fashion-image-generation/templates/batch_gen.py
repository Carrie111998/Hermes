#!/usr/bin/env python3
"""Manifest-driven fashion batch generator (STEP A recolor + STEP B worn).
Provider: OpenRouter gpt-image family. Idempotent: skips any step whose OUTPUT
already exists, so re-running never re-bills. Copy & adapt the MANIFEST/prompts.
Usage:  python batch_gen.py                # all garments
        python batch_gen.py --stepa        # recolor only
        python batch_gen.py --stepb        # worn only
        python batch_gen.py GARMENT_NAME   # single garment (exact dict key)
"""
import base64, json, os, sys, time, urllib.request

ORKEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
MODEL = os.environ.get("IMG_MODEL", "openai/gpt-5.4-image-2")
BASE = os.path.dirname(os.path.abspath(__file__))
IMPOSSIBLE_KEY = "$$$never-matches$$$"  # sentinel => unset => run all

# manifest: garment -> {ghost: rel_path, model: rel_path, swatch: {COLOR: rel_path}}
MANIFEST = {
    # "ALASKA": {
    #   "ghost":  "ALASKA/IMG_5967.jpg",
    #   "model":  "MODELLE/m1.jpg",           # REQUIRED for STEP B (user-provided photo)
    #   "swatch": {"NOCCIOLA-302": "ALASKA/Bazaart_663DFE11.jpeg", "...": "..."},
    # },
}

STEPA_PROMPT = ("Recolor this ghost-mannequin garment to the exact solid color shown in the "
    "reference swatch (reference 2). Keep the garment shape, fabric texture, knit/fur structure, "
    "stitching, zippers, buttons, pockets and labels exactly identical — change only the color. "
    "Same lighting, same neutral grey background, invisible/ghost mannequin product photography. "
    "Do not add or alter any text or logos.")

STEPB_PROMPT = ("The model from the reference image wearing the garment from the reference image, "
    "standing front pose, arms relaxed at sides, neutral light grey background, full body shot, "
    "professional ecommerce fashion photography, clean studio lighting. "
    "Preserve all original garment details exactly as shown: stitching, hardware, zippers, buttons "
    "and labels must remain identical to the reference, do not generate any text, logos or writing "
    "on buttons, zippers or labels, keep all branding elements blank and anonymous.")

def data_url(path):
    b = open(path, "rb").read()
    ext = "png" if path.lower().endswith(".png") else "jpeg"
    return f"data:image/{ext};base64," + base64.b64encode(b).decode()

def gen(prompt, refs, out, aspect="2:3", quality="high", retries=3):
    payload = {"model": MODEL, "prompt": prompt, "n": 1, "quality": quality,
               "aspect_ratio": aspect,
               "input_references": [{"type": "image_url", "image_url": {"url": r}} for r in refs]}
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/images",
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {ORKEY}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.load(resp)
            img = base64.b64decode(data["data"][0]["b64_json"])
            with open(out, "wb") as f:
                f.write(img)
            return len(img), data.get("usage", {}).get("cost")
        except Exception as e:
            last = e
            print(f"    [retry {attempt}] {e}", flush=True)
            time.sleep(5 * attempt)
    raise RuntimeError(f"FAILED {out}: {last}")

# selector: exact garment names, else run all; flags for step A/B only
_want = [a for a in sys.argv[1:] if not a.startswith("--")]
ovr = None if not _want else {x: x for x in _want}
only_stepa, only_stepb = "--stepa" in sys.argv, "--stepb" in sys.argv

summary = []
for capo, cfg in MANIFEST.items():
    if ovr is not None and capo not in ovr:
        continue
    ghost = f"{BASE}/{cfg['ghost']}"
    modela = f"{BASE}/{cfg.get('model', IMPOSSIBLE_KEY)}"
    print(f"\n========== {capo} ==========", flush=True)
    for colore, swatchf in cfg["swatch"].items():
        g_out = f"{BASE}/{capo}/ghost_{colore}.png"
        w_out = f"{BASE}/{capo}/indossato_front_{colore}.png"
        print(f"\n--- {capo} / {colore} ---", flush=True)
        # STEP A: skip if ghost output already exists
        if not only_stepb and not os.path.exists(g_out):
            try:
                n, c = gen(STEPA_PROMPT, [data_url(ghost), data_url(f"{BASE}/{swatchf}")], g_out)
                print(f"  STEP A ok ${c}", flush=True); summary.append(("A", capo, colore, c))
            except Exception as e:
                print(f"  STEP A FAIL {colore}: {e}", flush=True)
        else:
            print("  STEP A: skip", flush=True)
        # STEP B: skip if worn output already exists (idempotent!) or model missing
        if not only_stepa and os.path.exists(g_out) and not os.path.exists(w_out):
            if not os.path.exists(modela):
                print(f"  STEP B skip: model photo missing ({modela})", flush=True)
                continue
            try:
                n, c = gen(STEPB_PROMPT, [data_url(modela), data_url(g_out)], w_out)
                print(f"  STEP B ok ${c}", flush=True); summary.append(("B", capo, colore, c))
            except Exception as e:
                print(f"  STEP B FAIL {colore}: {e}", flush=True)
        else:
            print("  STEP B: skip", flush=True)

tot = sum((c or 0) for *_, c in summary)
print("\n===== RIEPILOGO =====")
for s, capo, colore, c in summary:
    print(f"  STEP {s} {capo} {colore}: ${c}")
print(f"COSTO TOTALE: ${tot:.4f}")
print("DONE")