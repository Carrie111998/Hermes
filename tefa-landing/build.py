#!/usr/bin/env python3
"""Build the TEFA vendor landing page and the storefront mockups.

Inlines the subset webfonts from fonts/ into each source page and writes
self-contained HTML documents. No build dependencies, no network, no CDN —
these pages have to work when dropped onto a static host or opened from
a USB stick.

    python3 build.py

Produces:
    index.html          the main landing page, from src/page.html
    store/shop.html      storefront mockup: category grid, from store/src/shop.html
    store/product.html   storefront mockup: product detail, from store/src/product.html
    store/order.html      storefront mockup: order/quote review, from store/src/order.html

The store pages share store/shared_style.css (also font-inlined), spliced in
at each page's __STORE_SHARED_CSS__ marker, so the design tokens stay in one
place instead of being copy-pasted three times. They also load store/cart.js
directly (a relative <script src>, not inlined) since the mockups aren't
meant to be single-file-portable the way index.html is.

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


def build_store_pages():
    store = HERE / "store"
    shared_css = inline_fonts(
        (store / "shared_style.css").read_text(), "store/shared_style.css"
    )
    desc = (
        "Storefront mockup: kits, tools, and homeschool resources invoiced against "
        "TEFA and other state education funds."
    )
    for name in ("shop", "product", "order"):
        src = (store / "src" / f"{name}.html").read_text()
        if "__STORE_SHARED_CSS__" not in src:
            raise SystemExit(f"__STORE_SHARED_CSS__ missing from store/src/{name}.html")
        page = src.replace("__STORE_SHARED_CSS__", shared_css)
        out = store / f"{name}.html"
        out.write_text(standalone_document(page, desc))
        print(f"store/{name}.html  {out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    build_main_page()
    build_store_pages()
