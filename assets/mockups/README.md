# Vanta Black mockups

These are static, clearly labeled **mockups**, not screenshots of a real herdr integration. They illustrate how a Hermes Agent window could be presented inside a herdr-style host shell in dark and light Vanta Black variants.

## Files

- `herdr-vanta-black-dark.svg` — 1440×900 dark host/window mockup.
- `herdr-vanta-black-light.svg` — 1440×900 light host/window mockup.
- `vanta-black-palette.svg` — palette swatches and hex samples.

SVG is the reproducible source and the exported artifact; it can be opened directly in a browser or imported into design tools. The SVGs use only inline shapes and system fonts, so no build step or network access is required.

## Usage

Open any SVG directly, or convert for a raster-only PR description with an installed renderer such as ImageMagick:

```sh
magick herdr-vanta-black-dark.svg herdr-vanta-black-dark.png
magick herdr-vanta-black-light.svg herdr-vanta-black-light.png
magick vanta-black-palette.svg vanta-black-palette.png
```

The `MOCKUP — not a real integration` chrome label is intentional and should remain in any derivative exports.
