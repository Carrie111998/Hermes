#!/usr/bin/env python3
"""Build the TEFA vendor landing page and the storefront mockups.

Inlines the subset webfonts from fonts/ into each source page and writes
self-contained HTML documents. No build dependencies, no network, no CDN —
these pages have to work when dropped onto a static host or opened from
a USB stick.

    python3 build.py

Produces:
    index.html                 main landing page, from src/page.html
    store/shop.html             ESA store mockup: category grid
    store/product.html          ESA store mockup: product detail
    store/order.html             ESA store mockup: order/quote review
    general-store/shop.html      General Store mockup: category grid
    general-store/product.html   General Store mockup: product detail
    general-store/checkout.html  General Store mockup: checkout

Both mockup sets share store/shared_style.css (font-inlined once), spliced
in at each page's __STORE_SHARED_CSS__ marker, so the design tokens stay in
one place instead of being copy-pasted six times. Cross-links between the
two mockup sets use MAIN_SITE_URL / ESA_SHOP_URL placeholders, resolved
here to relative paths for the repo build — see resolve_cross_links().
Each set also loads its own cart.js directly (a relative <script src>, not
inlined) since the mockups aren't meant to be single-file-portable the way
index.html is.

The fonts in fonts/ were subset from their upstream Google Fonts releases to
Latin text plus the punctuation this page actually uses, and the variable
faces were instanced down to a single optical size. See fonts/README.md.
"""
import base64
import pathlib
import re

HERE = pathlib.Path(__file__).parent
FONTS = {
    "__BRIC__": "bricolage.woff2",
    "__NEWS__": "newsreader.woff2",
    "__MONO4__": "dmmono400.woff2",
    "__MONO5__": "dmmono500.woff2",
}


def inline_fonts(text: str, label: str) -> str:
    for token, filename in FONTS.items():
        if token not in text:
            raise SystemExit(f"{token} missing from {label}")
        data = (HERE / "fonts" / filename).read_bytes()
        text = text.replace(token, base64.b64encode(data).decode())
    return text


def standalone_document(body_html: str, description: str) -> str:
    m = re.search(r"<title>(.*?)</title>\s*", body_html, re.S)
    title, inner = m.group(1), body_html[: m.start()] + body_html[m.end() :]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<meta name="color-scheme" content="light dark">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<style>
  *,*::before,*::after{{box-sizing:border-box}}
  html,body{{margin:0; padding:0}}
  img,svg,video{{max-width:100%; height:auto; display:block}}
</style>
</head>
<body>
{inner}
</body>
</html>
"""


def build_main_page():
    page = inline_fonts((HERE / "src" / "page.html").read_text(), "src/page.html")
    desc = (
        "Hands-on life skills, AI literacy, and focus coaching for students aged 8-18, "
        "funded through your family's TEFA account."
    )
    out = HERE / "index.html"
    out.write_text(standalone_document(page, desc))
    print(f"index.html  {out.stat().st_size / 1024:.0f} KB")


def build_store_pages(shared_css: str):
    store = HERE / "store"
    desc = (
        "Storefront mockup: kits, tools, and homeschool resources invoiced against "
        "TEFA and other state education funds."
    )
    for name in ("shop", "product", "order"):
        src = (store / "src" / f"{name}.html").read_text()
        if "__STORE_SHARED_CSS__" not in src:
            raise SystemExit(f"__STORE_SHARED_CSS__ missing from store/src/{name}.html")
        page = src.replace("__STORE_SHARED_CSS__", shared_css)
        page = resolve_cross_links(
            page, main="../index.html", esa_shop="shop.html", general_store="../general-store/shop.html"
        )
        out = store / f"{name}.html"
        out.write_text(standalone_document(page, desc))
        print(f"store/{name}.html  {out.stat().st_size / 1024:.0f} KB")


def build_general_store_pages(shared_css: str):
    gs = HERE / "general-store"
    desc = (
        "General Store mockup: family titles and activity books sold at retail, "
        "kept separate from the TEFA/ESA-funded storefront."
    )
    for name in ("shop", "product", "checkout"):
        src = (gs / "src" / f"{name}.html").read_text()
        if "__STORE_SHARED_CSS__" not in src:
            raise SystemExit(f"__STORE_SHARED_CSS__ missing from general-store/src/{name}.html")
        page = src.replace("__STORE_SHARED_CSS__", shared_css)
        page = resolve_cross_links(
            page, main="../index.html", esa_shop="../store/shop.html", general_store="shop.html"
        )
        out = gs / f"{name}.html"
        out.write_text(standalone_document(page, desc))
        print(f"general-store/{name}.html  {out.stat().st_size / 1024:.0f} KB")


def resolve_cross_links(page: str, main: str, esa_shop: str, general_store: str) -> str:
    return (
        page.replace("MAIN_SITE_URL", main)
        .replace("ESA_SHOP_URL", esa_shop)
        .replace("GENERAL_STORE_URL", general_store)
    )


if __name__ == "__main__":
    build_main_page()
    _shared_css = inline_fonts(
        (HERE / "store" / "shared_style.css").read_text(), "store/shared_style.css"
    )
    build_store_pages(_shared_css)
    build_general_store_pages(_shared_css)
