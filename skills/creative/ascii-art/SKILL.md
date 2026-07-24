---
name: ascii-art
description: "ASCII art: pyfiglet, cowsay, boxes, image-to-ascii."
version: 4.0.0
author: 0xbyt4, Hermes Agent
license: MIT
dependencies: []
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ASCII, Art, Banners, Creative, Unicode, Text-Art, pyfiglet, figlet, cowsay, boxes]
    related_skills: [excalidraw]

---

# ASCII Art Skill

## When to use

Use when the user wants text or pictures rendered as characters: an ASCII banner
or figlet title, a project/repo header, a terminal splash screen, a cowsay
message, a boxed/framed block of text, art of a specific subject (cat, rocket,
dragon), an image converted to ASCII, an ASCII QR code, or ASCII weather. Also
use when they ask for "text art", "figlet", "terminal art", or hand-drawn
Unicode box diagrams.

Do not use for real vector/raster diagrams (`excalidraw`) or for video-to-ASCII
playback (`ascii-video`).

Multiple tools cover different needs. All are local CLI programs or free REST
APIs — **no API keys required, ever**. If a request seems to need a credential,
something is wrong: re-read the routing table below.

## Red lines

- **Monospace-safe output only.** Everything must render correctly in a
  fixed-width font. Never rely on proportional spacing.
- **Max width 60 characters per line**, unless the user explicitly asks for wider.
  Overflowing lines wrap and destroy the whole picture.
- **Max height 15 lines for banners, 25 for scenes.**
- **Preserve artist signatures/initials** in art fetched from the web — this is
  community etiquette, not decoration. Do not strip them.
- **No API keys, no paid services, no auth.** Every route here is free.
- **Never invent a font, character, or box design name.** List first
  (`--list_fonts`, `cowsay -l`, `boxes -l`, the `/fonts` endpoint), then use.
- **ANSI color output (toilet, `-C` flags) is terminal-only.** Do not put it in
  files or chat where escape codes leak as garbage.
- **If a tool is not installed:** install it, or fall back to the next option —
  do not fake the output.

## Decision flow

| Request | Route | Reference |
|---------|-------|-----------|
| Text as a banner | pyfiglet if installed, else asciified API via curl | `references/text-banners.md` |
| Colored / filtered banner (terminal only) | toilet | `references/text-banners.md` |
| Wrap a message in fun character art | cowsay | `references/cowsay-and-boxes.md` |
| Add a decorative border/frame | boxes (pipe pyfiglet/asciified into it) | `references/cowsay-and-boxes.md` |
| Art of a specific thing (cat, rocket, dragon) | ascii.co.uk via curl + parsing | `references/art-sources.md` |
| Convert an image to ASCII | ascii-image-converter, or jp2a | `references/image-to-ascii.md` |
| QR code, weather, moon phase, octocat | free curl services | `references/art-sources.md` |
| Something custom/creative with no tool for it | draw it yourself with the Unicode palette | `references/unicode-palette.md` |

## Minimal end-to-end

The common case — a banner, framed, in one line:

```bash
pip install pyfiglet --break-system-packages -q      # once
python3 -m pyfiglet "HERMES" -f slant | boxes -d stone
```

If pyfiglet is unavailable, swap in the zero-install route:

```bash
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=HERMES&font=Slant" | boxes -d stone
```

Then show the result to the user, and offer 2-3 alternate fonts or box designs
before settling.

## References

Load on demand with `skill_view(name="ascii-art", file_path="references/...")`.

| To do this | Read |
|------------|------|
| Render large lettering — pyfiglet fonts and flags, the asciified REST API, toilet color filters | `references/text-banners.md` |
| Put text in a speech bubble or a decorative frame — cowsay characters/modifiers, boxes designs | `references/cowsay-and-boxes.md` |
| Turn an image file or URL into ASCII — ascii-image-converter, jp2a | `references/image-to-ascii.md` |
| Fetch existing art or ASCII web services — ascii.co.uk subjects and scraping, octocat, qrenco.de, wttr.in | `references/art-sources.md` |
| Draw art by hand when no tool fits — box-drawing/block/geometric character sets and composition rules | `references/unicode-palette.md` |
