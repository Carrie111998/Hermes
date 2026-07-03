---
name: ocr-and-documents
description: "Extract text from PDFs/scans (pymupdf, tesseract, marker-pdf)."
version: 2.4.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [PDF, Documents, Research, Arxiv, Text-Extraction, OCR]
    related_skills: [powerpoint]
---

# PDF & Document Extraction

For DOCX: use `python-docx` (parses actual document structure, far better than OCR).
For PPTX: see the `powerpoint` skill (uses `python-pptx` with full slide/notes support).
This skill covers **PDFs and scanned documents**.

## Step 1: Remote URL Available?

If the document has a URL, **always try `web_extract` first**:

```
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])
web_extract(urls=["https://example.com/report.pdf"])
```

This handles PDF-to-markdown conversion via Firecrawl with no local dependencies.

Only use local extraction when: the file is local, web_extract fails, or you need batch processing.

## Step 2: Choose Local Extractor

| Feature | pymupdf (~25MB) | tesseract (~150MB) | marker-pdf (~3-5GB) |
|---------|-----------------|--------------------|---------------------|
| **Text-based PDF** | ✅ | ✅ (overkill) | ✅ |
| **Scanned PDF (OCR)** | ❌ | ✅ | ✅ (90+ languages) |
| **Images → text (OCR)** | ❌ | ✅ | ✅ |
| **Tables** | ✅ (basic) | ❌ | ✅ (high accuracy) |
| **Equations / LaTeX** | ❌ | ❌ | ✅ |
| **Forms** | ❌ | ❌ | ✅ |
| **Reading order / layout** | ❌ | ❌ (line-by-line) | ✅ |
| **Images extraction** | ✅ (embedded) | ❌ | ✅ (with context) |
| **EPUB** | ✅ | ❌ | ✅ |
| **Markdown output** | ✅ (via pymupdf4llm) | ❌ (plain text) | ✅ (native, higher quality) |
| **Install size** | ~25MB | ~150MB | ~3-5GB (PyTorch + models) |
| **Speed** | Instant | ~0.5-2s/page | ~1-14s/page (CPU), ~0.2s/page (GPU) |

**Decision ladder**:
1. **Text-based PDF** (text is selectable) → **pymupdf**. Instant, no OCR needed.
2. **Scanned PDF or image** (text not selectable) → **tesseract**. Lightweight OCR, no GPU.
3. **Complex layout, equations, tables, forms, or reading-order matters** → **marker-pdf**. Only when 1–2 fall short.

In the Docker/Railway image, pymupdf and tesseract (+ eng/fra language data) are
**pre-baked** — they work immediately, no install, even as the non-root runtime
user. marker-pdf is **not** baked (its ~5GB footprint is impractical there).

If the user needs marker capabilities but the system lacks ~5GB free disk:
> "This document needs ML-grade extraction (marker-pdf), which requires ~5GB for PyTorch and models. Your system has [X]GB free. Options: free up space, provide a URL so I can use web_extract, or I can try tesseract (lightweight OCR — handles scans/images but not equations or complex tables)."

**Never** `apt-get install tesseract` at runtime — the agent runs as a non-root
user, so it fails. tesseract is baked into the image; if it is genuinely missing
in some other environment, add it to the Dockerfile apt layer and rebuild.

---

## pymupdf (lightweight)

```bash
pip install pymupdf pymupdf4llm
```

**Via helper script**:
```bash
python scripts/extract_pymupdf.py document.pdf              # Plain text
python scripts/extract_pymupdf.py document.pdf --markdown    # Markdown
python scripts/extract_pymupdf.py document.pdf --tables      # Tables
python scripts/extract_pymupdf.py document.pdf --images out/ # Extract images
python scripts/extract_pymupdf.py document.pdf --metadata    # Title, author, pages
python scripts/extract_pymupdf.py document.pdf --pages 0-4   # Specific pages
```

**Inline**:
```bash
python3 -c "
import pymupdf
doc = pymupdf.open('document.pdf')
for page in doc:
    print(page.get_text())
"
```

---

## tesseract (lightweight OCR)

For scanned PDFs and images. Pre-baked in the Docker image (engine + `eng`/`fra`
language data + poppler); the Python wrappers (`pytesseract`, `pillow`,
`pdf2image`) are baked too. On other systems install with:

```bash
# system engine (needs root) + python wrappers
apt-get install -y tesseract-ocr tesseract-ocr-fra poppler-utils   # Debian/Ubuntu
pip install pytesseract pillow pdf2image
```

**Via helper script**:
```bash
python scripts/extract_tesseract.py scan.png                 # OCR an image
python scripts/extract_tesseract.py scan.pdf                 # OCR a scanned PDF
python scripts/extract_tesseract.py scan.pdf --lang fra      # French (default: eng)
python scripts/extract_tesseract.py scan.pdf --lang eng+fra  # multiple languages
python scripts/extract_tesseract.py scan.pdf --pages 0-4     # specific pages
python scripts/extract_tesseract.py scan.png --dpi 300       # PDF raster DPI
```

Run `tesseract --list-langs` to see installed languages. To add one, append the
matching `tesseract-ocr-<lang>` package to the Dockerfile apt layer and rebuild.

---

## marker-pdf (high-quality OCR)

```bash
# Check disk space first
python scripts/extract_marker.py --check

pip install marker-pdf
```

**Via helper script**:
```bash
python scripts/extract_marker.py document.pdf                # Markdown
python scripts/extract_marker.py document.pdf --json         # JSON with metadata
python scripts/extract_marker.py document.pdf --output_dir out/  # Save images
python scripts/extract_marker.py scanned.pdf                 # Scanned PDF (OCR)
python scripts/extract_marker.py document.pdf --use_llm      # LLM-boosted accuracy
```

**CLI** (installed with marker-pdf):
```bash
marker_single document.pdf --output_dir ./output
marker /path/to/folder --workers 4    # Batch
```

---

## Arxiv Papers

```
# Abstract only (fast)
web_extract(urls=["https://arxiv.org/abs/2402.03300"])

# Full paper
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])

# Search
web_search(query="arxiv GRPO reinforcement learning 2026")
```

## Split, Merge & Search

pymupdf handles these natively — use `execute_code` or inline Python:

```python
# Split: extract pages 1-5 to a new PDF
import pymupdf
doc = pymupdf.open("report.pdf")
new = pymupdf.open()
for i in range(5):
    new.insert_pdf(doc, from_page=i, to_page=i)
new.save("pages_1-5.pdf")
```

```python
# Merge multiple PDFs
import pymupdf
result = pymupdf.open()
for path in ["a.pdf", "b.pdf", "c.pdf"]:
    result.insert_pdf(pymupdf.open(path))
result.save("merged.pdf")
```

```python
# Search for text across all pages
import pymupdf
doc = pymupdf.open("report.pdf")
for i, page in enumerate(doc):
    results = page.search_for("revenue")
    if results:
        print(f"Page {i+1}: {len(results)} match(es)")
        print(page.get_text("text"))
```

No extra dependencies needed — pymupdf covers split, merge, search, and text extraction in one package.

---

## Notes

- `web_extract` is always first choice for URLs
- pymupdf is the safe default — instant, no models, works everywhere
- tesseract is the lightweight OCR for scans/images — baked into the Docker image, no runtime install
- marker-pdf is for equations, complex layouts, high-accuracy tables — install only when needed
- Both helper scripts accept `--help` for full usage
- marker-pdf downloads ~2.5GB of models to `~/.cache/huggingface/` on first use
- For Word docs: `pip install python-docx` (better than OCR — parses actual structure)
- For PowerPoint: see the `powerpoint` skill (uses python-pptx)
