---
title: 'Github Auth — настройка аутентификации GitHub: токены HTTPS, ключи SSH, вход
  в интерфейс командной строки gh.'
sidebar_label: Github Auth
description: 'Настройка аутентификации GitHub: токены HTTPS, ключи SSH, вход в интерфейс
  командной строки gh'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Авторизация на Гитхабе

Настройка аутентификации GitHub: токены HTTPS, ключи SSH, вход в CLI.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/github/github-auth` |
| Версия | `1.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `GitHub`, `Authentication`, `Git`, `gh-cli`, `SSH`, `Setup` |
| Сопутствующие навыки | [`github-pr-workflow`](/docs/user-guide/skills/bundled/github/github-github-pr-workflow), [`github-code-review`](/docs/user-guide/skills/bundled/github/github-github-code-review), [`github-issues`](/docs/user-guide/skills/bundled/github/github-github-issues), [`github-repo-management`](/docs/user-guide/skills/bundled/github/github-github-repo-management) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Настройка аутентификации GitHub

Этот навык настраивает аутентификацию, чтобы агент мог работать с репозиториями GitHub, запросами на запросы, проблемами и CI. Он охватывает два пути:

- **`git` (всегда доступен)** — использует токены личного доступа HTTPS или ключи SSH.
- **`gh` CLI (если установлен)** — расширенный доступ к API GitHub с более простым процессом аутентификации.

## Порядок обнаружения

Когда пользователь просит вас поработать с GitHub, сначала запустите эту проверку:

```bash
# Check what's available
git --version
gh --version 2>/dev/null || echo "gh not installed"

# Check if already authenticated
gh auth status 2>/dev/null || echo "gh not authenticated"
git config --global credential.helper 2>/dev/null || echo "no git credential helper"
```

**Схема принятия решений:**
1. Если `gh auth status` показывает проверку подлинности → всё в порядке, используйте `gh` для всего.
2. Если `gh` установлен, но не прошел проверку подлинности → используйте метод «gh auth», указанный ниже.
3. Если `gh` не установлен → используйте метод «только git» ниже (sudo не требуется)

---

## Метод 1: аутентификация только с помощью Git (без gh, без sudo)

Это работает на любой машине с установленным `git`. Никакого root-доступа не требуется.

### Вариант A: HTTPS с личным токеном доступа (рекомендуется)

Это самый переносимый метод — работает везде, настройка SSH не требуется.

**Шаг 1. Создайте токен личного доступа**

Попросите пользователя перейти по адресу: **https://github.com/settings/tokens**.

- Нажмите «Создать новый токен (классический)».
- Дайте ему имя типа "гермес-агент"
- Выберите области действия:
  - `repo` (полный доступ к репозиторию — чтение, запись, отправка, PR)
  - `workflow` (запуск и управление действиями GitHub)
  - `read:org` (при работе с репозиториями организаций)
- Установить срок действия (90 дней — хороший вариант по умолчанию)
- Скопируйте токен — он больше не будет отображаться

**Шаг 2. Настройте git для хранения токена**

```bash
# Set up the credential helper to cache credentials
# "store" saves to ~/.git-credentials in plaintext (simple, persistent)
git config --global credential.helper store

# Now do a test operation that triggers auth — git will prompt for credentials
# Username: <their-github-username>
# Password: <paste the personal access token, NOT their GitHub password>
git ls-remote https://github.com/<their-username>/<any-repo>.git
```

После однократного ввода учетных данных они сохраняются и повторно используются для всех будущих операций.

**Альтернатива: помощник кэша (срок действия учетных данных истекает из памяти)**

```bash
# Cache in memory for 8 hours (28800 seconds) instead of saving to disk
git config --global credential.helper 'cache --timeout=28800'
```

**Альтернатива: установите токен непосредственно в удаленном URL-адресе (для каждого репозитория)**

```bash
# Embed token in the remote URL (avoids credential prompts entirely)
git remote set-url origin https://<username>:<token>@github.com/<owner>/<repo>.git
```

**Шаг 3. Настройте идентификатор git**

```bash
# Required for commits — set name and email
git config --global user.name "Their Name"
git config --global user.email "their-email@example.com"
```

**Шаг 4. Проверка**

```bash
# Test push access (this should work without any prompts now)
git ls-remote https://github.com/<their-username>/<any-repo>.git

# Verify identity
git config --global user.name
git config --global user.email
```

### Вариант Б: Аутентификация по ключу SSH

Подходит для пользователей, которые предпочитают SSH или уже имеют настроенные ключи.

**Шаг 1. Проверьте наличие существующих ключей SSH**

```bash
ls -la ~/.ssh/id_*.pub 2>/dev/null || echo "No SSH keys found"
```

**Шаг 2. При необходимости сгенерируйте ключ**

```bash
# Generate an ed25519 key (modern, secure, fast)
ssh-keygen -t ed25519 -C "their-email@example.com" -f ~/.ssh/id_ed25519 -N ""

# Display the public key for them to add to GitHub
cat ~/.ssh/id_ed25519.pub
```

Попросите пользователя добавить открытый ключ по адресу: **https://github.com/settings/keys**.
- Нажмите «Новый ключ SSH».
- Вставьте содержимое открытого ключа.
- Дайте ему название, например "hermes-agent-&lt;machine-name>"

**Шаг 3. Проверьте соединение**

```bash
ssh -T git@github.com
# Expected: "Hi <username>! You've successfully authenticated..."
```

**Шаг 4. Настройте git для использования SSH для GitHub**

```bash
# Rewrite HTTPS GitHub URLs to SSH automatically
git config --global url."git@github.com:".insteadOf "https://github.com/"
```

**Шаг 5. Настройте идентификатор git**

```bash
git config --global user.name "Their Name"
git config --global user.email "their-email@example.com"
```

---

## Способ 2: аутентификация через CLI

Если установлен `gh`, он обрабатывает как доступ к API, так и учетные данные git за один шаг.

### Интерактивный вход в браузер (на рабочем столе)

```bash
gh auth login
# Select: GitHub.com
# Select: HTTPS
# Authenticate via browser
```

### Вход на основе токенов (безголовые / SSH-серверы)

```bash
echo "<THEIR_TOKEN>" | gh auth login --with-token

# Set up git credentials through gh
gh auth setup-git
```

### Проверить

```bash
gh auth status
```

---

## Использование API GitHub без gh

Если `gh` недоступен, вы все равно можете получить доступ к полному API GitHub, используя `curl` с личным токеном доступа. Вот как другие навыки GitHub реализуют свои резервные варианты.

### Установка токена для вызовов API

```bash
# Option 1: Export as env var (preferred — keeps it out of commands)
export GITHUB_TOKEN="<token>"

# Then use in curl calls:
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/user
```

### Извлечение токена из учетных данных Git

Если учетные данные git уже настроены (через хранилище credential.helper), токен можно извлечь:

```bash
# Read from git credential store
uv run python3 "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py"
```

### Помощник: определение метода аутентификации

Используйте этот шаблон в начале любого рабочего процесса GitHub:

```bash
# Try gh first, fall back to git + curl
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  echo "AUTH_METHOD=gh"
elif [ -n "$GITHUB_TOKEN" ]; then
  echo "AUTH_METHOD=curl"
elif _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
  export GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
  echo "AUTH_METHOD=curl"
elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
  export GITHUB_TOKEN=$(uv run python3 "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py")
  echo "AUTH_METHOD=curl"
else
  echo "AUTH_METHOD=none"
  echo "Need to set up authentication first"
fi
```

---

## Устранение неполадок

| Проблема | Решение |
|---------|----------|
| `git push` запрашивает пароль | GitHub отключил аутентификацию по паролю. Используйте токен личного доступа в качестве пароля или переключитесь на SSH |
| `remote: Permission to X denied` | Токену может не хватать области действия `repo` — выполните повторную генерацию с использованием правильных областей |
| `fatal: Authentication failed` | Кэшированные учетные данные могут быть устаревшими — запустите `git credential reject` и повторите аутентификацию |
| `ssh: connect to host github.com port 22: Connection refused` | Попробуйте SSH через порт HTTPS: добавьте `Host github.com` с `Port 443` и `Hostname ssh.github.com` к `~/.ssh/config` |
| Учетные данные не сохраняются | Отметьте `git config --global credential.helper` — должно быть `store` или `cache` |
| Несколько учетных записей GitHub | Используйте SSH с разными ключами для каждого псевдонима хоста в `~/.ssh/config` или URL-адресами учетных данных для каждого репозитория |
| `gh: command not found` + без sudo | Используйте только git-метод 1, описанный выше — установка не требуется |