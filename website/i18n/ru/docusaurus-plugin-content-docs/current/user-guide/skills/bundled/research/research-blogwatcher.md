---
title: Blogwatcher — отслеживайте блоги и RSS/Atom-каналы с помощью инструмента blogwatcher-cli.
sidebar_label: Blogwatcher
description: Мониторинг блогов и каналов RSS/Atom с помощью инструмента blogwatcher-cli.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Блогвотчер

Мониторинг блогов и каналов RSS/Atom с помощью инструмента blogwatcher-cli.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/research/blogwatcher` |
| Версия | `2.0.0` |
| Автор | JulienTant (вилка Hyaxia/blogwatcher) |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `RSS`, `Blogs`, `Feed-Reader`, `Monitoring` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Блогвотчер

Отслеживайте обновления блога и каналов RSS/Atom с помощью инструмента `blogwatcher-cli`. Поддерживает автоматическое обнаружение каналов, резервное копирование HTML, импорт OPML и управление прочитанными/непрочитанными статьями.

## Установка

Выберите один метод:

- **Иди:** `go install github.com/JulienTant/blogwatcher-cli/cmd/blogwatcher-cli@latest`
- **Докер:** `docker run --rm -v blogwatcher-cli:/data ghcr.io/julientant/blogwatcher-cli`
- **Двоичный (Linux amd64):** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_linux_amd64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`
- **Двоичный (Linux Arm64):** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_linux_arm64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`
- **Двоичный (macOS Apple Silicon):** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_darwin_arm64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`
- **Двоичный (macOS Intel):** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_darwin_amd64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`

Все выпуски: https://github.com/JulienTant/blogwatcher-cli/releases.

### Docker с постоянным хранилищем

По умолчанию база данных находится по адресу `~/.blogwatcher-cli/blogwatcher-cli.db`. В Docker это теряется при перезапуске контейнера. Используйте `BLOGWATCHER_DB` или монтирование тома, чтобы сохранить его:

```bash
# Named volume (simplest)
docker run --rm -v blogwatcher-cli:/data -e BLOGWATCHER_DB=/data/blogwatcher-cli.db ghcr.io/julientant/blogwatcher-cli scan

# Host bind mount
docker run --rm -v /path/on/host:/data -e BLOGWATCHER_DB=/data/blogwatcher-cli.db ghcr.io/julientant/blogwatcher-cli scan
```

### Миграция с оригинального blogwatcher

При обновлении с `Hyaxia/blogwatcher` переместите базу данных:

```bash
mv ~/.blogwatcher/blogwatcher.db ~/.blogwatcher-cli/blogwatcher-cli.db
```

Двоичное имя изменилось с `blogwatcher` на `blogwatcher-cli`.

## Общие команды

### Управление блогами

- Добавить блог: `blogwatcher-cli add "My Blog" https://example.com`
– Добавить с явным фидом: `blogwatcher-cli add "My Blog" https://example.com --feed-url https://example.com/feed.xml`
– Добавить с помощью очистки HTML: `blogwatcher-cli add "My Blog" https://example.com --scrape-selector "article h2 a"`.
– Список отслеживаемых блогов: `blogwatcher-cli blogs`.
– Удалить блог: `blogwatcher-cli remove "My Blog" --yes`.
- Импорт из OPML: `blogwatcher-cli import subscriptions.opml`

### Сканирование и чтение

- Сканировать все блоги: `blogwatcher-cli scan`
– Отсканируйте один блог: `blogwatcher-cli scan "My Blog"`.
– Список непрочитанных статей: `blogwatcher-cli articles`.
- Список всех статей: `blogwatcher-cli articles --all`
– Фильтровать по блогу: `blogwatcher-cli articles --blog "My Blog"`.
- Фильтровать по категории: `blogwatcher-cli articles --category "Engineering"`
– Отметить статью как прочитанную: `blogwatcher-cli read 1`.
- Пометить статью как непрочитанную: `blogwatcher-cli unread 1`
– Отметить все прочитанными: `blogwatcher-cli read-all`
– Отметить все прочитанные в блоге: `blogwatcher-cli read-all --blog "My Blog" --yes`.

## Переменные среды

Все флаги можно установить через переменные среды с префиксом `BLOGWATCHER_`:

| Переменная | Описание |
|---|---|
| `BLOGWATCHER_DB` | Путь к файлу базы данных SQLite |
| `BLOGWATCHER_WORKERS` | Количество рабочих одновременного сканирования (по умолчанию: 8) |
| `BLOGWATCHER_SILENT` | При сканировании выводится только сообщение «сканирование выполнено» |
| `BLOGWATCHER_YES` | Пропустить запросы подтверждения |
| `BLOGWATCHER_CATEGORY` | Фильтр по умолчанию для статей по категориям |

## Пример вывода

```
$ blogwatcher-cli blogs
Tracked blogs (1):

  xkcd
    URL: https://xkcd.com
    Feed: https://xkcd.com/atom.xml
    Last scanned: 2026-04-03 10:30
```

```
$ blogwatcher-cli scan
Scanning 1 blog(s)...

  xkcd
    Source: RSS | Found: 4 | New: 4

Found 4 new article(s) total!
```

```
$ blogwatcher-cli articles
Unread articles (2):

  [1] [new] Barrel - Part 13
       Blog: xkcd
       URL: https://xkcd.com/3095/
       Published: 2026-04-02
       Categories: Comics, Science

  [2] [new] Volcano Fact
       Blog: xkcd
       URL: https://xkcd.com/3094/
       Published: 2026-04-01
       Categories: Comics
```

## Примечания

- Автоматически обнаруживает каналы RSS/Atom на домашних страницах блогов, если не указан `--feed-url`.
- Возвращается к очистке HTML, если RSS не работает и настроен `--scrape-selector`.
- Категории из каналов RSS/Atom сохраняются и могут использоваться для фильтрации статей.
- Массовый импорт блогов из файлов OPML, экспортированных Feedly, Inoreader, NewsBlur и т. д.
– База данных по умолчанию хранится по адресу `~/.blogwatcher-cli/blogwatcher-cli.db` (переопределить с помощью `--db` или `BLOGWATCHER_DB`).
- Используйте `blogwatcher-cli <command> --help`, чтобы узнать все флаги и параметры.