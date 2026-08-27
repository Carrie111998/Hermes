---
title: Nano Pdf — редактируйте текст в существующих PDF-файлах с помощью подсказок
  на естественном языке.
sidebar_label: Nano Pdf
description: Редактируйте текст в существующих PDF-файлах с помощью подсказок на естественном
  языке.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# НаноPDF

Редактируйте текст в существующих PDF-файлах с помощью подсказок на естественном языке.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/productivity/nano-pdf` |
| Версия | `1.0.0` |
| Автор | сообщество |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `PDF`, `Documents`, `Editing`, `NLP`, `Productivity` |
| Сопутствующие навыки | [`pdf`](/docs/user-guide/skills/bundled/productivity/productivity-pdf), [`ocr-and-documents`](/docs/user-guide/skills/bundled/productivity/productivity-ocr-and-documents) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# нано-pdf

Редактируйте PDF-файлы, используя инструкции на естественном языке. Наведите его на страницу и опишите, что нужно изменить. Для работы со структурой PDF (слияние, разделение, формы, водяные знаки, создание) см. навык `pdf`; информацию об извлечении текста из сканов см. в `ocr-and-documents`.

## Предварительные условия

```bash
# Install with uv (recommended — already available in Hermes)
uv pip install nano-pdf

# Or with pip
pip install nano-pdf
```

## Использование

```bash
nano-pdf edit <file.pdf> <page_number> "<instruction>"
```

## Примеры

```bash
# Change a title on page 1
nano-pdf edit deck.pdf 1 "Change the title to 'Q3 Results' and fix the typo in the subtitle"

# Update a date on a specific page
nano-pdf edit report.pdf 3 "Update the date from January to February 2026"

# Fix content
nano-pdf edit contract.pdf 2 "Change the client name from 'Acme Corp' to 'Acme Industries'"
```

## Примечания

- Номера страниц могут начинаться с 0 или с 1 в зависимости от версии. Если редактирование касается не той страницы, повторите попытку с ±1.
– Всегда проверяйте выходной PDF-файл после редактирования (используйте `read_file`, чтобы проверить размер файла или открыть его).
- Инструмент использует LLM под капотом — требуется ключ API (проверьте `nano-pdf --help` для конфигурации)
- Хорошо работает для изменения текста; сложные изменения макета могут потребовать другого подхода