# Image to ASCII

Convert images (PNG, JPEG, GIF, WEBP) to ASCII art.

## Option A: ascii-image-converter (recommended, modern)

```bash
# Install
sudo snap install ascii-image-converter
# OR: go install github.com/TheZoraiz/ascii-image-converter@latest
```

```bash
ascii-image-converter image.png                  # Basic
ascii-image-converter image.png -C               # Color output
ascii-image-converter image.png -d 60,30         # Set dimensions
ascii-image-converter image.png -b               # Braille characters
ascii-image-converter image.png -n               # Negative/inverted
ascii-image-converter https://url/image.jpg      # Direct URL
ascii-image-converter image.png --save-txt out   # Save as text
```

## Option B: jp2a (lightweight, JPEG only)

```bash
sudo apt install jp2a -y
jp2a --width=80 image.jpg
jp2a --colors image.jpg              # Colorized
```

## Notes

- Keep `-d` / `--width` within the terminal-safe budget (60 columns by default)
  unless the user explicitly wants a wide render.
- Braille mode (`-b`) packs roughly 2x4 pixels per glyph — much more detail, but
  it needs a font with full Braille coverage to render correctly.
