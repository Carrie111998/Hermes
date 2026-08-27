---
title: Ocr и документы — Извлечение текста из PDF-файлов/сканов (pymupdf, маркер-pdf)
sidebar_label: Ocr And Documents
description: Извлечение текста из PDF-файлов/сканов (pymupdf, маркер-pdf)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# OCR и документы

Извлечение текста из PDF-файлов/сканов (pymupdf, маркер-pdf).

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/productivity/ocr-and-documents` |
| Версия | `2.3.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `PDF`, `Documents`, `Research`, `Arxiv`, `Text-Extraction`, `OCR` |
| Сопутствующие навыки | [`pdf`](/docs/user-guide/skills/bundled/productivity/productivity-pdf), [`docx`](/docs/user-guide/skills/bundled/productivity/productivity-docx), [`powerpoint`](/docs/user-guide/skills/bundled/productivity/productivity-powerpoint) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Извлечение PDF и документов

Для DOCX: см. навык `docx` (создание/редактирование) или используйте `python-docx` для структурированного чтения.
Для PPTX: см. навык `powerpoint` (полная поддержка создания/чтения/редактирования).
Для манипуляций с PDF (слияние, разделение, формы, водяные знаки, создание): см. навык `pdf`.
Этот навык охватывает **извлечение текста из PDF-файлов и отсканированных документов**.

> **Из `read_file` ПРЕДУПРЕЖДЕНИЯ О ПОКРЫТИИ ИЗВЛЕЧЕНИЯ?** `read_file` автоматически конвертирует локальные PDF-файлы, но читает только текстовый слой; в нижнем колонтитуле с предупреждением перечислены страницы, на которых не было текста (отсканированные изображения). Для нескольких страниц рендеринг + просмотр происходит быстрее всего: `pdftoppm -jpeg -r 150 -f N -l N file.pdf /tmp/page`, затем `vision_analyze` каждого изображения. Для массового распознавания многих страниц используйте маркер-pdf ниже (шаг 2).

## Шаг 1. Доступен удаленный URL-адрес?

Если у документа есть URL, **всегда сначала пробуйте `web_extract`**:

```
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])
web_extract(urls=["https://example.com/report.pdf"])
```

Это обеспечивает преобразование PDF в уценку через Firecrawl без каких-либо локальных зависимостей.

Используйте локальное извлечение только в следующих случаях: файл локальный, произошел сбой web_extract или вам нужна пакетная обработка.

## Шаг 2: выберите локальный экстрактор

| Особенность | pymupdf (~25 МБ) | маркер-pdf (~3-5ГБ) |
|---------|-----------------|---------------------|
| **Текстовый PDF** | ✅ | ✅ |
| **Отсканированный PDF-файл (OCR)** | ❌ | ✅ (90+ языков) |
| **Таблицы** | ✅ (базовый) | ✅ (высокая точность) |
| **Уравнения/LaTeX** | ❌ | ✅ |
| **Блоки кода** | ❌ | ✅ |
| **Формы** | ❌ | ✅ |
| **Удаление верхних и нижних колонтитулов** | ❌ | ✅ |
| **Определение порядка чтения** | ❌ | ✅ |
| **Извлечение изображений** | ✅ (встроенный) | ✅ (с контекстом) |
| **Изображения → текст (OCR)** | ❌ | ✅ |
| **EPUB** | ✅ | ✅ |
| **Вывод уценки** | ✅ (через pymupdf4llm) | ✅ (родной, более высокого качества) |
| **Установочный размер** | ~25 МБ | ~3–5 ГБ (модели PyTorch +) |
| **Скорость** | Мгновенный | ~1–14 с/страницу (ЦП), ~0,2 с/страницу (ГП) |

**Решение**: используйте pymupdf, если вам не требуется распознавание текста, уравнения, формы или сложный анализ макета.

Если пользователю нужны возможности маркера, но в системе не хватает свободного диска примерно на 5 ГБ:
> «Этот документ требует OCR/расширенного извлечения (marker-pdf), для чего требуется ~5 ГБ для PyTorch и моделей. В вашей системе свободно [X] ГБ. Варианты: освободить место, предоставить URL-адрес, чтобы я мог использовать web_extract, или я могу попробовать pymupdf, который работает для текстовых PDF-файлов, но не для отсканированных документов или уравнений».

---

## pymupdf (облегченный)

```bash
pip install pymupdf pymupdf4llm
```

**С помощью вспомогательного сценария**:
```bash
python scripts/extract_pymupdf.py document.pdf              # Plain text
python scripts/extract_pymupdf.py document.pdf --markdown    # Markdown
python scripts/extract_pymupdf.py document.pdf --tables      # Tables
python scripts/extract_pymupdf.py document.pdf --images out/ # Extract images
python scripts/extract_pymupdf.py document.pdf --metadata    # Title, author, pages
python scripts/extract_pymupdf.py document.pdf --pages 0-4   # Specific pages
```

**Встроенный**:
```bash
python3 -c "
import pymupdf
doc = pymupdf.open('document.pdf')
for page in doc:
    print(page.get_text())
"
```

---

##marker-pdf (высококачественное распознавание текста)

```bash
# Check disk space first
python scripts/extract_marker.py --check

pip install marker-pdf
```

**С помощью вспомогательного сценария**:
```bash
python scripts/extract_marker.py document.pdf                # Markdown
python scripts/extract_marker.py document.pdf --json         # JSON with metadata
python scripts/extract_marker.py document.pdf --output_dir out/  # Save images
python scripts/extract_marker.py scanned.pdf                 # Scanned PDF (OCR)
python scripts/extract_marker.py document.pdf --use_llm      # LLM-boosted accuracy
```

**CLI** (устанавливается вместе с маркером-pdf):
```bash
marker_single document.pdf --output_dir ./output
marker /path/to/folder --workers 4    # Batch
```

---

## Архив-документы

```
# Abstract only (fast)
web_extract(urls=["https://arxiv.org/abs/2402.03300"])

# Full paper
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])

# Search
web_search(query="arxiv GRPO reinforcement learning 2026")
```

## Разделение, объединение и поиск

pymupdf обрабатывает их изначально — используйте `execute_code` или встроенный Python:

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
- marker-pdf is for OCR, scanned docs, equations, complex layouts — install only when needed
- Both helper scripts accept `--help` for full usage
- marker-pdf downloads ~2.5GB of models to `~/.cache/huggingface/` on first use
- For Word docs: `pip install python-docx` (better than OCR — parses actual structure)
- For PowerPoint: see the `powerpoint` skill (uses python-pptx)
