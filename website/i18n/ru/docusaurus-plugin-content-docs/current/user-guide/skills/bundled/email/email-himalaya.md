---
title: 'Himalaya — Himalaya CLI: электронная почта IMAP/SMTP с терминала'
sidebar_label: Himalaya
description: 'Himalaya CLI: электронная почта IMAP/SMTP с терминала'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Гималаи

Himalaya CLI: электронная почта IMAP/SMTP с терминала.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/email/himalaya` |
| Версия | `1.1.0` |
| Автор | сообщество |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Email`, `IMAP`, `SMTP`, `CLI`, `Communication` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Интерфейс командной строки электронной почты Himalaya

Himalaya — это почтовый клиент с интерфейсом командной строки, который позволяет управлять электронной почтой с терминала с помощью серверов IMAP, SMTP, Notmuch или Sendmail.

Этот навык не связан с адаптером шлюза электронной почты Hermes. Шлюз
Адаптер позволяет людям отправлять агенту электронную почту и использует встроенный IMAP/SMTP компании Hermes.
адаптер; этот навык позволяет агенту управлять почтовым ящиком с помощью терминальных инструментов и
требуется внешний интерфейс командной строки `himalaya`.

## Ссылки

- `references/configuration.md` (настройка файла конфигурации + аутентификация IMAP/SMTP)
- `references/message-composition.md` (синтаксис MML для составления электронных писем)

## Предварительные условия

1. Himalaya CLI установлен (`himalaya --version` для проверки)
2. Файл конфигурации по адресу `~/.config/himalaya/config.toml`.
3. Настроены учетные данные IMAP/SMTP (пароль надежно хранится)

### Установка

```bash
# Pre-built binary (Linux/macOS — recommended)
curl -sSL https://raw.githubusercontent.com/pimalaya/himalaya/master/install.sh | PREFIX=~/.local sh

# macOS via Homebrew
brew install himalaya

# Or via cargo (any platform with Rust)
cargo install himalaya --locked
```

## Настройка конфигурации

Запустите интерактивный мастер для настройки учетной записи:

```bash
himalaya account configure
```

Или создайте `~/.config/himalaya/config.toml` вручную:

```toml
[accounts.personal]
email = "you@example.com"
display-name = "Your Name"
default = true

backend.type = "imap"
backend.host = "imap.example.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "you@example.com"
backend.auth.type = "password"
backend.auth.cmd = "pass show email/imap"  # or use keyring

message.send.backend.type = "smtp"
message.send.backend.host = "smtp.example.com"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "you@example.com"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "pass show email/smtp"

# Folder aliases (himalaya v1.2.0+ syntax). Required whenever the
# server's folder names don't match himalaya's canonical names
# (inbox/sent/drafts/trash). Gmail is the common case — see
# `references/configuration.md` for the `[Gmail]/Sent Mail` mapping.
folder.aliases.inbox = "INBOX"
folder.aliases.sent = "Sent"
folder.aliases.drafts = "Drafts"
folder.aliases.trash = "Trash"
```

> **Обратите внимание на синтаксис псевдонимов.** В документации до версии 1.2.0 использовался
> Подраздел `[accounts.NAME.folder.alias]` (единственное число `alias`).
> v1.2.0 молча игнорирует эту форму — TOML анализирует нормально, но
> преобразователь псевдонимов никогда его не читает, поэтому каждый поиск проваливается
> Каноническое имя. В Gmail это означает, что сохранение для отправки не выполняется *после*.
> Доставка SMTP успешна, и `himalaya message send` завершается с ненулевым результатом.
> Любой вызывающий абонент (агент, сценарий, пользователь), который повторяет этот код выхода.
> повторно запустит всю отправку, включая SMTP, создав дубликат
> электронные письма получателям. Всегда используйте `folder.aliases.X` (множественное число, разделенное точками).
> ключи, прямо под `[accounts.NAME]`).

## Примечания по интеграции Hermes

- **Чтение, листинг, поиск, перемещение, удаление** — все работает непосредственно через терминал.
- **Составление/ответ/пересылка** — для надежности рекомендуется использовать конвейерный ввод (`cat << EOF | himalaya template send`). Интерактивный режим `$EDITOR` работает с `pty=true` + фоном + инструментом обработки, но требует знания редактора и его команд.
– Используйте `--output json` для структурированного вывода, который легче анализировать программно.
- Мастер `himalaya account configure` требует интерактивного ввода — используйте режим PTY: `terminal(command="himalaya account configure", pty=true)`.

## Общие операции

### Список папок

```bash
himalaya folder list
```

### Список адресов электронной почты

Список писем в папке «Входящие» (по умолчанию):

```bash
himalaya envelope list
```

Перечислите электронные письма в определенной папке:

```bash
himalaya envelope list --folder "Sent"
```

Список с нумерацией страниц:

```bash
himalaya envelope list --page 1 --page-size 20
```

### Поиск писем

```bash
himalaya envelope list from john@example.com subject meeting
```

### Прочитать электронное письмо

Чтение электронной почты по идентификатору (показывает простой текст):

```bash
himalaya message read 42
```

Экспортировать необработанный MIME:

```bash
himalaya message export 42 --full
```

### Ответ на электронное письмо

Чтобы ответить в неинтерактивном режиме от Hermes, прочитайте исходное сообщение, составьте ответ и передайте его по каналу:

```bash
# Get the reply template, edit it, and send
himalaya template reply 42 | sed 's/^$/\nYour reply text here\n/' | himalaya template send
```

Или создайте ответ вручную:

```bash
cat << 'EOF' | himalaya template send
From: you@example.com
To: sender@example.com
Subject: Re: Original Subject
In-Reply-To: <original-message-id>

Your reply here.
EOF
```

Ответить всем (интерактивно — требуется $EDITOR, вместо этого используйте шаблонный подход, описанный выше):

```bash
himalaya message reply 42 --all
```

### Переслать электронное письмо

```bash
# Get forward template and pipe with modifications
himalaya template forward 42 | sed 's/^To:.*/To: newrecipient@example.com/' | himalaya template send
```

### Напишите новое письмо

**Неинтерактивный (используйте это от Hermes)** — передайте сообщение через стандартный ввод:

```bash
cat << 'EOF' | himalaya template send
From: you@example.com
To: recipient@example.com
Subject: Test Message

Hello from Himalaya!
EOF
```

Или с флагом заголовков:

```bash
himalaya message write -H "To:recipient@example.com" -H "Subject:Test" "Message body here"
```

Примечание. `himalaya message write` без конвейерного входа открывает `$EDITOR`. Это работает в фоновом режиме `pty=true` +, но конвейерная обработка проще и надежнее.

### Переместить/копировать электронные письма

Перейти в папку (сначала указывается целевая папка, затем идентификатор сообщения):

```bash
himalaya message move "Archive" 42
```

Скопировать в папку (сначала указывается целевая папка, затем идентификатор сообщения):

```bash
himalaya message copy "Important" 42
```

### Удалить электронное письмо

```bash
himalaya message delete 42
```

### Управление флагами

Добавить флаг:

```bash
himalaya flag add 42 --flag seen
```

Удалить флаг:

```bash
himalaya flag remove 42 --flag seen
```

## Несколько учетных записей

Список аккаунтов:

```bash
himalaya account list
```

Используйте конкретную учетную запись:

```bash
himalaya --account work envelope list
```

## Вложения

Сохраните вложения из сообщения:

```bash
himalaya attachment download 42
```

Сохранить в конкретный каталог:

```bash
himalaya attachment download 42 --downloads-dir ~/Downloads
```

## Выходные форматы

Большинство команд поддерживают `--output` для структурированного вывода:

```bash
himalaya envelope list --output json
himalaya envelope list --output plain
```

## Отладка

Включите ведение журнала отладки:

```bash
RUST_LOG=debug himalaya envelope list
```

Полная трассировка с обратной трассировкой:

```bash
RUST_LOG=trace RUST_BACKTRACE=1 himalaya envelope list
```

## Советы

– Используйте `himalaya --help` или `himalaya <command> --help` для подробной информации об использовании.
- Идентификаторы сообщений указаны относительно текущей папки; повторный список после изменения папки.
- Для составления расширенных электронных писем с вложениями используйте синтаксис MML (см. `references/message-composition.md`).
- Надежно храните пароли, используя `pass`, системный набор ключей или команду, которая выводит пароль.