#!/usr/bin/env python3
"""OCR scans and images with tesseract (via pytesseract). Lightweight (~150MB).

Handles image files directly and rasterizes PDFs page-by-page (pdf2image +
poppler). For text-based PDFs prefer extract_pymupdf.py (no OCR needed); for
complex layouts/equations/tables prefer extract_marker.py.

Usage:
    python extract_tesseract.py scan.png
    python extract_tesseract.py scan.pdf
    python extract_tesseract.py scan.pdf --lang fra        # default: eng
    python extract_tesseract.py scan.pdf --lang eng+fra    # multiple langs
    python extract_tesseract.py scan.pdf --pages 0-4
    python extract_tesseract.py scan.png --dpi 300         # PDF raster DPI

Languages must be installed in the image (tesseract-ocr-<lang>); run
`tesseract --list-langs` to see what's available.
"""
import sys

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"}


def ocr_image(path, lang):
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.open(path), lang=lang)


def ocr_pdf(path, lang, pages=None, dpi=300):
    import pytesseract
    from pdf2image import convert_from_path

    images = convert_from_path(path, dpi=dpi)
    page_range = range(len(images)) if pages is None else pages
    for i in page_range:
        if i < len(images):
            print(f"\n--- Page {i+1}/{len(images)} ---\n")
            print(pytesseract.image_to_string(images[i], lang=lang))


def main():
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        sys.exit(0)

    path = args[0]
    lang = "eng"
    dpi = 300
    pages = None

    if "--lang" in args:
        lang = args[args.index("--lang") + 1]
    if "--dpi" in args:
        dpi = int(args[args.index("--dpi") + 1])
    if "--pages" in args:
        p = args[args.index("--pages") + 1]
        if "-" in p:
            start, end = p.split("-")
            pages = list(range(int(start), int(end) + 1))
        else:
            pages = [int(p)]

    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == ".pdf":
        ocr_pdf(path, lang, pages=pages, dpi=dpi)
    elif ext in IMAGE_EXTS:
        print(ocr_image(path, lang))
    else:
        print(f"Unsupported file type '{ext}'. Use an image or PDF.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
