#!/usr/bin/env python3
"""Build the TEFA vendor landing page.

Inlines the subset webfonts from fonts/ into src/page.html and writes a single
self-contained index.html. No build dependencies, no network, no CDN — the page
has to work when it is dropped onto a static host or opened from a USB stick.

    python3 build.py

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
DESC = ("Hands-on life skills, AI literacy, and focus coaching for students aged 8-18, "
        "funded through your family's TEFA account.")

page = (HERE / "src" / "page.html").read_text()

for token, filename in FONTS.items():
    if token not in page:
        raise SystemExit(f"{token} missing from src/page.html")
    data = (HERE / "fonts" / filename).read_bytes()
    page = page.replace(token, base64.b64encode(data).decode())

# <title> belongs in the head of the standalone document, not the body.
m = re.search(r"<title>(.*?)</title>\s*", page, re.S)
title, inner = m.group(1), page[:m.start()] + page[m.end():]

(HERE / "index.html").write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{DESC}">
<meta name="color-scheme" content="light dark">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{DESC}">
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
""")

print(f"index.html  {(HERE / 'index.html').stat().st_size / 1024:.0f} KB")
