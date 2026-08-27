---
title: 'Проверка кодовой базы — проверка кодовых баз с помощью pygount: LOC, языки,
  соотношения.'
sidebar_label: Codebase Inspection
description: 'Проверьте кодовые базы с помощью pygount: LOC, языки, соотношения.'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Проверка кодовой базы

Проверьте кодовые базы с помощью pygount: LOC, языки, соотношения.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/github/codebase-inspection` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `LOC`, `Code Analysis`, `pygount`, `Codebase`, `Metrics`, `Repository` |
| Сопутствующие навыки | [`github-repo-management`](/docs/user-guide/skills/bundled/github/github-github-repo-management) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Проверка кодовой базы с помощью pygount

Анализируйте репозитории на предмет строк кода, языковой разбивки, количества файлов и соотношения кода и комментариев с помощью `pygount`.

## Когда использовать

- Пользователь запрашивает количество LOC (строк кода).
- Пользователь хочет получить языковую разбивку репозитория.
- Пользователь спрашивает о размере или составе кодовой базы.
- Пользователь хочет соотношение кода и комментариев.
- Общие вопросы «насколько велико это репо»

## Предварительные условия

```bash
pip install --break-system-packages pygount 2>/dev/null || pip install pygount
```

## 1. Основное резюме (наиболее распространенное)

Получите полную разбивку по языку с указанием количества файлов, строк кода и строк комментариев:

```bash
cd /path/to/repo
pygount --format=summary \
  --folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,.eggs,*.egg-info" \
  .
```

**ВАЖНО:** Всегда используйте `--folders-to-skip` для исключения каталогов зависимостей/сборки, иначе pygount просканирует их, что займет очень много времени или зависнет.

## 2. Общие исключения папок

Настройте в зависимости от типа проекта:

```bash
# Python projects
--folders-to-skip=".git,venv,.venv,__pycache__,.cache,dist,build,.tox,.eggs,.mypy_cache"

# JavaScript/TypeScript projects
--folders-to-skip=".git,node_modules,dist,build,.next,.cache,.turbo,coverage"

# General catch-all
--folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,vendor,third_party"
```

## 3. Фильтровать по определенному языку

```bash
# Only count Python files
pygount --suffix=py --format=summary .

# Only count Python and YAML
pygount --suffix=py,yaml,yml --format=summary .
```

## 4. Подробный пофайловый вывод

```bash
# Default format shows per-file breakdown
pygount --folders-to-skip=".git,node_modules,venv" .

# Sort by code lines (pipe through sort)
pygount --folders-to-skip=".git,node_modules,venv" . | sort -t$'\t' -k1 -nr | head -20
```

## 5. Форматы вывода

```bash
# Summary table (default recommendation)
pygount --format=summary .

# JSON output for programmatic use
pygount --format=json .

# Pipe-friendly: Language, file count, code, docs, empty, string
pygount --format=summary . 2>/dev/null
```

## 6. Интерпретация результатов

Столбцы сводной таблицы:
- **Язык** — обнаруженный язык программирования.
- **Файлы** — количество файлов на этом языке.
- **Код** — строки реального кода (исполняемого/декларативного).
- **Комментарий** — строки, являющиеся комментариями или документацией.
- **%** — процент от суммы

Специальные псевдоязыки:
- `__empty__` — пустые файлы
- `__binary__` — бинарные файлы (изображения, скомпилированные и т. д.)
- `__generated__` — автоматически создаваемые файлы (определяются эвристически)
- `__duplicate__` — файлы с идентичным содержимым.
- `__unknown__` — нераспознанные типы файлов.

## Подводные камни

1. **Всегда исключайте .git, node_modules, venv** — без `--folders-to-skip` pygount просканирует всё, что может занять несколько минут или зависнуть на больших деревьях зависимостей.
2. **Markdown показывает 0 строк кода** — pygount классифицирует весь контент Markdown как комментарии, а не как код. Это ожидаемое поведение.
3. **Файлы JSON показывают малое количество кода** — pygount может консервативно подсчитывать строки JSON. Для точного подсчета строк JSON используйте напрямую `wc -l`.
4. **Большие монорепозитории**. Для очень больших репозиториев рассмотрите возможность использования `--suffix` для таргетинга на определенные языки, а не сканирования всего.