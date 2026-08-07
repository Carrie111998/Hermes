#!/usr/bin/env python3
"""Organize generated FW26 images into PRODUCT IMG with SKU-based progressive renaming.

Reads the authoritative SKU map from HELMUR-master-prodotti spreadsheet (tab VARIANTI),
renames local generated outputs (ghost_<C>.png / indossato_<posa>_<C>.png) to
<SKU-base>_<modello>-<colore>_<NN>_<posa>.png, and uploads them to:

    PRODUCT IMG (root_id) > DONNA|UOMO > <MODELLO> > <CodColore>-<COLORE>/

Idempotent: skip uploads whose exact target filename already exists in the target folder
(and trash-all-upload-one is NOT used here — filenames are unique per model+color+pose).

Usage:
  python organize_output.py                              # all garments
  python organize_output.py ALASKA MONTANA               # only these models
  python organize_output.py --local-only ALASKA          # just rename locally, no upload
  python organize_output.py --start 1                     # progressive index base (default 0)

Requires google_api.py on PATH via $GAPI, and the xlsx via --xlsx (default downloaded fresh).
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile
import openpyxl

BASE = os.environ.get("FW_OUTPUT_BASE", os.path.dirname(os.path.abspath(__file__)))
# Drive target root: PRODUCT IMG. Subfolders DONNA/UOMO already exist; model/color created on demand.
PRODUCT_IMG_ROOT = os.environ.get("PRODUCT_IMG_ROOT", "1vy41E81IYScJOVJYYz076sCc_eBCCblN")
SEX_FOLDERS = {"Donna": "1YqQ27arr_CvWIFPZe3XK9B-UpuhDw6l8", "Uomo": "1ZhlmWVWFEnmN6VJpTMEog9zWTQMg4Vdi"}  # may re-query by name
XLSX = os.environ.get("HELMUR_XLSX", "/root/analysis/master.xlsx")
GAPI = os.environ.get("GAPI", "python /root/.hermes/skills/productivity/google-workspace/scripts/google_api.py")

# working color token -> Cod. colore (number). Fill from the manifest / vision mapping.
# token like "NOCCIOLA-302" -> 302 ; "MASTICE-202" -> 202 ; "CENERE-201" -> 201
def parse_code(token):
    m = re.search(r"-(\d+)$", token)
    return m.group(1) if m else token

def load_sku_map(xlsx):
    """Return {modello: {codcol(str): {sku, colore, foto, reparto}}}."""
    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    ws = wb["VARIANTI"]
    rows = ws.iter_rows(values_only=True)
    next(rows)
    m = {}
    for r in rows:
        sku, mod, rep, cat, codcol, col, *_rest = (list(r) + [None] * 10)[:10]
        foto = _rest[3] if len(_rest) > 3 else None
        if not mod:
            continue
        m.setdefault(mod, {})[str(codcol)] = {"sku": sku, "colore": col, "foto": foto, "reparto": rep}
    return m

def popen_gapi(args):
    proc = subprocess.run(f"{GAPI} {args}".split(), capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None

def child_id(parent, name):
    d = popen_gapi(f"drive search \"'{parent}' in parents and name='{name}' and trashed=false\" --raw-query --max 50")
    if d:
        for x in d:
            if x.get("name") == name and x.get("mimeType") == "application/vnd.google-apps.folder":
                return x["id"]
    return None

def ensure_folder(parent, name):
    e = child_id(parent, name)
    if e:
        return e
    d = popen_gapi(f"drive create-folder \"{name}\" --parent {parent}")
    return d.get("id") if d else None

def target_name(sku_map, modulo, colore_token, posa_enum, idx, start):
    code = parse_code(colore_token)
    info = sku_map.get(modulo, {}).get(code, {}) if sku_map else {}
    nome_col = (info.get("colore") or colore_token.split("-")[0]).lower()
    sku = info.get("sku") or f"{modulo[:4].upper()}-{code}"
    nn = str(start + idx).zfill(2)
    posa = "ghost" if posa_enum == "ghost" else posa_enum  # front / bust34 / editorial
    return f"{sku}_{modulo.lower()}-{nome_col}_{nn}_{posa}.png", info

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("modelli", nargs="*")
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--xlsx", default=XLSX)
    a = ap.parse_args()

    sku_map = load_sku_map(a.xlsx) if os.path.exists(a.xlsx) else {}
    want = set(a.modelli) or None

    # local generated outputs live under BASE/<MODELLO>/
    for capo in os.listdir(BASE):
        capodir = os.path.join(BASE, capo)
        if not os.path.isdir(capodir) or capo.startswith("."):
            continue
        if want and capo not in want:
            continue
        # group generated files by color token
        files = sorted(f for f in os.listdir(capodir) if re.match(r"(ghost|indossato)_", f))
        bycolour = {}
        for f in files:
            # ghost_NOCCIOLA-302.png | indossato_front_NOCCIOLA-302.png | indossato_editorial_...
            m = re.match(r"(ghost|indossato)_(?:([a-z0-9]+)_)?(.+)\.png$", f)
            if not m:
                continue
            kind, posa, colore = m.group(1), m.group(2) or "front", m.group(3)
            posa_enum = "ghost" if kind == "ghost" else posa
            bycolour.setdefault(colore, []).append(f)
        for colore, flist in sorted(bycolour.items()):
            # sorted poses: ghost, front, bust34, editorial
            order = {"ghost": 0, "front": 1, "bust34": 2, "editorial": 3}
            flist.sort(key=lambda f: order.get(("ghost" if f.startswith("ghost") else
                     ("bust34" if "bust34" in f else ("editorial" if "editorial" in f else "front"))), 9))
            info, _it = {}, {}
            for idx, f in enumerate(flist):
                posa_enum = "ghost" if f.startswith("ghost") else \
                    ("bust34" if "bust34" in f else ("editorial" if "editorial" in f else "front"))
                newname, info = target_name(sku_map, capo, colore, posa_enum, idx, a.start)
                src = os.path.join(capodir, f)
                dst = os.path.join(capodir, newname)
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.copy2(src, dst)
                if a.local_only:
                    print(f"  [local] {capo}/{colore}: {f} -> {newname}", flush=True)
                    continue
                # upload to PRODUCT IMG / DOC | UOMO / modello / colore
                reparto = info.get("reparto") or ("Uomo" if capo in ("OXFORD","PORTLAND","LEMANS") else "Donna")
                sex = sex_folder_name(reparto)
                sexid = SEX_FOLDERS.get(sex)
                if not sexid:
                    sexid = ensure_folder(PRODUCT_IMG_ROOT, sex) or ensure_folder(PRODUCT_IMG_ROOT, "Uomo")
                model_id = ensure_folder(sexid, capo)
                col_name = f"{parse_code(colore)}-{info.get('colore') or colore}"
                col_id = ensure_folder(model_id, col_name)
                if child_id(col_id, newname):
                    print(f"  [skip] {capo}/{colore}: {newname} già presente", flush=True)
                    continue
                popen_gapi(f"drive upload {dst} --name \"{newname}\" --parent {col_id}")
                print(f"  [ok] {capo}/{colore}: {newname} -> PRODUCT IMG/{sex}/{capo}/{col_name}/", flush=True)

def sex_folder_name(reparto):
    return "Donna" if str(reparto).strip().lower() in ("donna", "femmina", "f") else "Uomo"

if __name__ == "__main__":
    main()
