#!/usr/bin/env python3
"""
extract_web_design.py — Forensic design system extractor for client websites.

Principle: theme != brand.
Declared CSS custom properties (:root tokens, framework defaults) are often
generic. The real brand lives in de-facto usage: the literal color/font
values actually applied across real CSS rules, weighted by frequency.
Reconcile declared vs de-facto and flag divergence — same logic as the
pptx-design-extractor skill, applied to CSS instead of OOXML.

Usage:
    python3 extract_web_design.py <url> [<url2> ...] --out <stem>-charte

Limitations (flagged in CHARTE.md):
    - JS-rendered SPAs with runtime CSS-in-JS (styled-components with dynamic
      props) under-represent de-facto usage — static fetch only sees the
      initial stylesheet, not client-computed styles. For those, a
      Playwright-based computed-style pass is needed as a second stage
      (not implemented here — flag and recommend it in CHARTE.md instead
      of silently producing an incomplete charter).
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import tinycss2
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WMH-DesignExtractor/1.0; +internal-tool)"
}
TIMEOUT = 15

HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}){1,2}\b")
RGB_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+))?\s*\)")
IGNORE_COLOR_VALUES = {
    "transparent", "currentcolor", "inherit", "initial", "unset", "none",
    "white", "black",  # too generic to be brand signal on their own; kept in raw counts but flagged
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def rgb_to_hex(r, g, b):
    return "#{:02x}{:02x}{:02x}".format(int(float(r)), int(float(g)), int(float(b)))


def normalize_color(value: str):
    value = value.strip().lower()
    if value in IGNORE_COLOR_VALUES:
        return None
    m = HEX_RE.search(value)
    if m:
        h = m.group(0).lower()
        if len(h) == 4:  # #rgb -> #rrggbb
            h = "#" + "".join(c * 2 for c in h[1:])
        return h[:7]
    m = RGB_RE.search(value)
    if m:
        return rgb_to_hex(m.group(1), m.group(2), m.group(3))
    return None


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def collect_css_sources(page_url: str, html: str):
    """Return list of (source_label, css_text) for every stylesheet linked or inline."""
    soup = BeautifulSoup(html, "html.parser")
    sources = []

    for style_tag in soup.find_all("style"):
        if style_tag.string:
            sources.append((f"{page_url}#inline-style", style_tag.string))

    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href")
        if not href:
            continue
        css_url = urljoin(page_url, href)
        try:
            css_text = fetch(css_url)
            sources.append((css_url, css_text))
        except requests.RequestException as e:
            print(f"  ! skip {css_url}: {e}", file=sys.stderr)

    return sources, soup


def parse_declared_tokens(css_text: str):
    """Extract :root { --token: value; } custom properties — the 'declared theme'."""
    tokens = {}
    rules = tinycss2.parse_stylesheet(css_text, skip_comments=True, skip_whitespace=True)
    for rule in rules:
        if rule.type != "qualified-rule":
            continue
        prelude = tinycss2.serialize(rule.prelude).strip()
        if ":root" not in prelude and "html" not in prelude:
            continue
        decls = tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
        for d in decls:
            if d.type == "declaration" and d.lower_name.startswith("--"):
                value = tinycss2.serialize(d.value).strip()
                tokens[d.lower_name] = value
    return tokens


def parse_defacto_usage(css_text: str, color_counter: Counter, font_counter: Counter,
                         heading_sizes: Counter, radius_counter: Counter, var_ref_counter: Counter):
    """Walk every declaration in every rule (not just :root) and count literal usage."""
    rules = tinycss2.parse_stylesheet(css_text, skip_comments=True, skip_whitespace=True)

    def walk(rule_list, selector=""):
        for rule in rule_list:
            if rule.type == "qualified-rule":
                sel = tinycss2.serialize(rule.prelude).strip()
                decls = tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
                for d in decls:
                    if d.type != "declaration":
                        continue
                    raw_value = tinycss2.serialize(d.value).strip()
                    prop = d.lower_name

                    if "var(" in raw_value:
                        m = re.search(r"var\((--[\w-]+)", raw_value)
                        if m:
                            var_ref_counter[m.group(1)] += 1

                    if prop in ("color", "background-color", "background", "border-color", "fill", "stroke") or "color" in prop:
                        hexval = normalize_color(raw_value)
                        if hexval:
                            color_counter[hexval] += 1

                    if prop == "font-family":
                        cleaned = raw_value.split(",")[0].strip().strip("'\"")
                        if cleaned:
                            font_counter[cleaned] += 1

                    if prop == "font-size" and any(h in sel for h in HEADING_TAGS):
                        heading_sizes[(sel.strip(), raw_value)] += 1

                    if prop == "border-radius":
                        radius_counter[raw_value] += 1

            elif rule.type == "at-rule" and rule.content:
                # descend into @media etc.
                inner = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
                walk(inner, selector)

    walk(rules)


def build_swatch_svg(top_colors, out_path: Path):
    n = len(top_colors)
    if n == 0:
        return
    w, h = 120, 120
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w*n}" height="{h+30}">']
    for i, (hexval, count) in enumerate(top_colors):
        x = i * w
        svg.append(f'<rect x="{x}" y="0" width="{w}" height="{h}" fill="{hexval}" stroke="#ccc"/>')
        svg.append(f'<text x="{x+8}" y="{h+18}" font-size="12" font-family="monospace">{hexval} ({count})</text>')
    svg.append("</svg>")
    out_path.write_text("\n".join(svg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=8, help="nombre de couleurs de-facto à retenir")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    declared_tokens = {}
    color_counter, font_counter = Counter(), Counter()
    heading_sizes, radius_counter, var_ref_counter = Counter(), Counter(), Counter()
    analyzed_pages, css_sources_seen = [], set()

    for url in args.urls:
        print(f"→ fetching {url}")
        try:
            html = fetch(url)
        except requests.RequestException as e:
            print(f"  ! could not fetch {url}: {e}", file=sys.stderr)
            continue
        analyzed_pages.append(url)
        css_sources, soup = collect_css_sources(url, html)

        for src, css_text in css_sources:
            if src in css_sources_seen:
                continue
            css_sources_seen.add(src)
            declared_tokens.update(parse_declared_tokens(css_text))
            parse_defacto_usage(css_text, color_counter, font_counter, heading_sizes, radius_counter, var_ref_counter)

    top_colors = color_counter.most_common(args.top)
    top_fonts = font_counter.most_common(5)

    GENERIC_HEX = {"#000000", "#ffffff", "#fff", "#000"}

    def is_generic(h):
        return h in GENERIC_HEX

    # Reconciliation: does the declared theme's literal values match the de-facto top colors,
    # EXCLUDING black/white (too generic to be a meaningful brand signal on their own).
    declared_color_values = {normalize_color(v) for v in declared_tokens.values() if normalize_color(v)}
    declared_non_generic = {c for c in declared_color_values if not is_generic(c)}
    top_non_generic = [(c, n) for c, n in top_colors if not is_generic(c)]
    top_non_generic_hexes = {c for c, _ in top_non_generic}
    matched = declared_non_generic & top_non_generic_hexes
    diverges = len(top_non_generic) > 0 and len(matched) < min(2, len(top_non_generic_hexes))

    design_tokens = {
        "analyzed_pages": analyzed_pages,
        "css_sources_count": len(css_sources_seen),
        "declared_theme": declared_tokens,
        "defacto": {
            "colors": [{"hex": h, "occurrences": c} for h, c in top_colors],
            "fonts": [{"family": f, "occurrences": c} for f, c in top_fonts],
            "heading_sizes": [{"selector": s[0], "value": s[1], "occurrences": c}
                               for s, c in heading_sizes.most_common(10)],
            "border_radius": [{"value": v, "occurrences": c} for v, c in radius_counter.most_common(5)],
        },
        "var_reference_counts": dict(var_ref_counter.most_common(20)),
        "reconciliation": {
            "palette_diverges": diverges,
            "brand_colors_missing_from_declared_theme": [c for c, _ in top_non_generic if c not in declared_non_generic],
            "note": (
                "Le theme declare (custom properties :root) ne recoupe pas la palette de-facto "
                "la plus utilisee (hors noir/blanc, trop generiques pour etre un signal de marque). "
                "La marque reelle vit dans l'usage, pas dans les tokens declares."
                if diverges else
                "Le theme declare recoupe correctement la palette de-facto observee (hors noir/blanc)."
            ),
        },
    }

    (out_dir / "design-tokens.json").write_text(json.dumps(design_tokens, indent=2, ensure_ascii=False))
    build_swatch_svg(top_colors, out_dir / "palette.svg")

    charte_lines = [
        "# CHARTE — Design system extrait (forensique)",
        "",
        f"Pages analysees : {', '.join(analyzed_pages)}",
        f"Feuilles de style analysees : {len(css_sources_seen)}",
        "",
        "## Reconciliation theme declare vs usage de-facto",
        "",
        f"**{'⚠ DIVERGENCE' if diverges else '✓ Coherent'}** — {design_tokens['reconciliation']['note']}",
    ]
    if design_tokens["reconciliation"]["brand_colors_missing_from_declared_theme"]:
        charte_lines.append("")
        charte_lines.append("Couleurs de-facto frequentes absentes des tokens declares : "
                             + ", ".join(f"`{c}`" for c in design_tokens["reconciliation"]["brand_colors_missing_from_declared_theme"]))
    charte_lines += [
        "## Palette de-facto (par frequence d'usage reelle)",
        "",
        "| Hex | Occurrences |",
        "|---|---|",
    ]
    for h, c in top_colors:
        charte_lines.append(f"| `{h}` | {c} |")

    charte_lines += [
        "",
        "## Theme declare (custom properties :root)",
        "",
        "| Token | Valeur |",
        "|---|---|",
    ]
    for k, v in declared_tokens.items():
        charte_lines.append(f"| `{k}` | `{v}` |")

    charte_lines += [
        "",
        "## Typographie de-facto",
        "",
        "| Police | Occurrences |",
        "|---|---|",
    ]
    for f, c in top_fonts:
        charte_lines.append(f"| {f} | {c} |")

    if heading_sizes:
        charte_lines += ["", "## Tailles de titres observees", "", "| Sélecteur | Taille | Occurrences |", "|---|---|---|"]
        for (sel, val), c in heading_sizes.most_common(10):
            charte_lines.append(f"| `{sel}` | `{val}` | {c} |")

    charte_lines += [
        "",
        "## Limites methodologiques",
        "",
        "- Extraction statique (fetch + parsing CSS) : ne capture pas les styles injectes a "
        "l'execution (CSS-in-JS avec valeurs dynamiques, styled-components). Sur un site JS-heavy "
        "avec peu de resultats de-facto, prevoir une passe complementaire via Playwright "
        "(computed styles sur DOM rendu) avant de considerer cette charte comme definitive.",
        "- Les valeurs `white`/`black`/`transparent` sont comptees mais exclues du signal de marque "
        "(trop generiques pour etre distinctives).",
        "",
        "## Strategie de generation recommandee",
        "",
        "Utiliser la palette et la typographie **de-facto**, pas le theme declare, comme source de "
        "verite pour toute generation \"a la charte\" de ce client.",
    ]

    (out_dir / "CHARTE.md").write_text("\n".join(charte_lines))
    print(f"\n✓ Ecrit dans {out_dir}/ : CHARTE.md, design-tokens.json, palette.svg")


if __name__ == "__main__":
    main()
