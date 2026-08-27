---
title: 'Обзор кода Github — обзор PR: различия, встроенные комментарии через gh или
  REST'
sidebar_label: Github Code Review
description: 'Обзор PR: различия, встроенные комментарии через gh или REST.'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Обзор кода Github

Обзор PR: различия, встроенные комментарии через gh или REST.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/github/github-code-review` |
| Версия | `1.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `GitHub`, `Code-Review`, `Pull-Requests`, `Git`, `Quality` |
| Сопутствующие навыки | [`github-auth`](/docs/user-guide/skills/bundled/github/github-github-auth), [`github-pr-workflow`](/docs/user-guide/skills/bundled/github/github-github-pr-workflow) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Обзор кода GitHub

Выполняйте проверку кода локальных изменений перед публикацией или просматривайте открытые запросы на отправку изменений на GitHub. Большая часть этого навыка использует простой `git` — разделение `gh`/`curl` имеет значение только для взаимодействий на уровне PR.

## Предварительные условия

- Аутентифицирован с помощью GitHub (см. навык `github-auth`)
- Внутри репозитория git

### Настройка (для PR-взаимодействий)

```bash
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  if [ -z "$GITHUB_TOKEN" ]; then
    if _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(uv run python3 "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py")
    fi
  fi
fi

REMOTE_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
OWNER=$(echo "$OWNER_REPO" | cut -d/ -f1)
REPO=$(echo "$OWNER_REPO" | cut -d/ -f2)
```

---

## 1. Проверка локальных изменений (предварительная публикация)

Это чистый `git` — работает везде, API не требуется.

### Получите разницу

```bash
# Staged changes (what would be committed)
git diff --staged

# All changes vs main (what a PR would contain)
git diff main...HEAD

# File names only
git diff main...HEAD --name-only

# Stat summary (insertions/deletions per file)
git diff main...HEAD --stat
```

### Стратегия обзора

1. **Сначала получите общую картину:**

```bash
git diff main...HEAD --stat
git log main..HEAD --oneline
```

2. **Просмотр файла за файлом** — используйте `read_file` для измененных файлов для полного контекста и разницу, чтобы увидеть, что изменилось:

```bash
git diff main...HEAD -- src/auth/login.py
```

3. **Проверьте распространенные проблемы:**

```bash
# Debug statements, TODOs, console.logs left behind
git diff main...HEAD | grep -n "print(\|console\.log\|TODO\|FIXME\|HACK\|XXX\|debugger"

# Large files accidentally staged
git diff main...HEAD --stat | sort -t'|' -k2 -rn | head -10

# Secrets or credential patterns
git diff main...HEAD | grep -in "password\|secret\|api_key\|token.*=\|private_key"

# Merge conflict markers
git diff main...HEAD | grep -n "<<<<<<\|>>>>>>\|======="
```

4. **Предлагайте пользователю структурированный отзыв**.

### Просмотр формата вывода

Анализируя местные изменения, представьте результаты в следующей структуре:

```
## Code Review Summary

### Critical
- **src/auth.py:45** — SQL injection: user input passed directly to query.
  Suggestion: Use parameterized queries.

### Warnings
- **src/models/user.py:23** — Password stored in plaintext. Use bcrypt or argon2.
- **src/api/routes.py:112** — No rate limiting on login endpoint.

### Suggestions
- **src/utils/helpers.py:8** — Duplicates logic in `src/core/utils.py:34`. Consolidate.
- **tests/test_auth.py** — Missing edge case: expired token test.

### Looks Good
- Clean separation of concerns in the middleware layer
- Good test coverage for the happy path
```

---

## 2. Проверка запроса на извлечение на GitHub

### Просмотр сведений о PR

**С гх:**

```bash
gh pr view 123
gh pr diff 123
gh pr diff 123 --name-only
```

**С помощью git + Curl:**

```bash
PR_NUMBER=123

# Get PR details
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "
import sys, json
pr = json.load(sys.stdin)
print(f\"Title: {pr['title']}\")
print(f\"Author: {pr['user']['login']}\")
print(f\"Branch: {pr['head']['ref']} -> {pr['base']['ref']}\")
print(f\"State: {pr['state']}\")
print(f\"Body:\n{pr['body']}\")"

# List changed files
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/files \
  | python3 -c "
import sys, json
for f in json.load(sys.stdin):
    print(f\"{f['status']:10} +{f['additions']:-4} -{f['deletions']:-4}  {f['filename']}\")"
```

### Проверьте локальный PR для полного обзора

Это работает с простым `git` — `gh` не требуется:

```bash
# Fetch the PR branch and check it out
git fetch origin pull/123/head:pr-123
git checkout pr-123

# Now you can use read_file, search_files, run tests, etc.

# View diff against the base branch
git diff main...pr-123
```

**С помощью gh (ярлыка):**

```bash
gh pr checkout 123
```

### Оставлять комментарии к пиару

**Общий комментарий по связям с общественностью — с gh:**

```bash
gh pr comment 123 --body "Overall looks good, a few suggestions below."
```

**Общий комментарий по связям с общественностью — с завитком:**

```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/$PR_NUMBER/comments \
  -d '{"body": "Overall looks good, a few suggestions below."}'
```

### Оставлять комментарии к отзывам

**Один встроенный комментарий — с gh (через API):**

```bash
HEAD_SHA=$(gh pr view 123 --json headRefOid --jq '.headRefOid')

gh api repos/$OWNER/$REPO/pulls/123/comments \
  --method POST \
  -f body="This could be simplified with a list comprehension." \
  -f path="src/auth/login.py" \
  -f commit_id="$HEAD_SHA" \
  -f line=45 \
  -f side="RIGHT"
```

**Один встроенный комментарий — с завитком:**

```bash
# Get the head commit SHA
HEAD_SHA=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments \
  -d "{
    \"body\": \"This could be simplified with a list comprehension.\",
    \"path\": \"src/auth/login.py\",
    \"commit_id\": \"$HEAD_SHA\",
    \"line\": 45,
    \"side\": \"RIGHT\"
  }"
```

### Отправьте официальную проверку (одобрите/запросите изменения)

**С гх:**

```bash
gh pr review 123 --approve --body "LGTM!"
gh pr review 123 --request-changes --body "See inline comments."
gh pr review 123 --comment --body "Some suggestions, nothing blocking."
```

**С помощью Curl — обзор с несколькими комментариями отправляется атомарно:**

```bash
HEAD_SHA=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews \
  -d "{
    \"commit_id\": \"$HEAD_SHA\",
    \"event\": \"COMMENT\",
    \"body\": \"Code review from Hermes Agent\",
    \"comments\": [
      {\"path\": \"src/auth.py\", \"line\": 45, \"body\": \"Use parameterized queries to prevent SQL injection.\"},
      {\"path\": \"src/models/user.py\", \"line\": 23, \"body\": \"Hash passwords with bcrypt before storing.\"},
      {\"path\": \"tests/test_auth.py\", \"line\": 1, \"body\": \"Add test for expired token edge case.\"}
    ]
  }"
```

Значения событий: `"APPROVE"`, `"REQUEST_CHANGES"`, `"COMMENT"`.

Поле `line` относится к номеру строки в *новой* версии файла. Для удаленных строк используйте `"side": "LEFT"`.

---

## 3. Контрольный список проверки

При проведении проверки кода (локальной или PR) систематически проверяйте:

### Правильность
- Делает ли код то, что утверждает?
- Обработаны пограничные случаи (пустые входные данные, нули, большие данные, одновременный доступ)?
- Пути ошибок обрабатываются корректно?

### Безопасность
- Никаких жестко запрограммированных секретов, учетных данных или ключей API.
- Проверка входных данных, вводимых пользователем.
- Никакой SQL-инъекции, XSS или обхода пути.
- Проверка авторизации/авторизации при необходимости

### Качество кода
- Четкое именование (переменные, функции, классы)
- Никаких ненужных сложностей и преждевременных абстракций.
- DRY — нет дублированной логики, которую необходимо извлечь
- Функции сфокусированы (единая ответственность)

### Тестирование
- Протестированы новые пути кода?
- Учтены счастливые пути и случаи ошибок?
- Тесты читаемы и удобны в сопровождении?

### Производительность
- Никаких запросов N+1 и ненужных циклов.
- Соответствующее кэширование там, где это выгодно.
- Никаких операций блокировки в путях асинхронного кода.

### Документация
- Документированы общедоступные API.
- Неочевидная логика имеет комментарии, объясняющие «почему»
- README обновляется, если поведение изменилось.

---

## 4. Рабочий процесс предварительной проверки

Когда пользователь просит вас «проверить код» или «проверить перед отправкой»:

1. `git diff main...HEAD --stat` — посмотреть объём изменений
2. `git diff main...HEAD` — прочитать полный дифференциал
3. Для каждого измененного файла используйте `read_file`, если вам нужен дополнительный контекст.
4. Примените контрольный список выше
5. Представить выводы в структурированном формате (Критика/Предупреждения/Предложения/Выглядит хорошо)
6. Если обнаружены критические проблемы, предложите исправить их до того, как пользователь нажмет

---

## 5. Рабочий процесс PR-анализа (сквозной)

Когда пользователь просит вас «пересмотреть PR #N», «посмотреть этот PR» или дает вам URL-адрес PR, следуйте этому рецепту:

### Шаг 1. Настройка среды

```bash
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"
# Or run the inline setup block from the top of this skill
```

### Шаг 2. Соберите PR-контекст

Получите метаданные PR, описание и список измененных файлов, чтобы понять масштаб, прежде чем углубляться в код.

**С гх:**
```bash
gh pr view 123
gh pr diff 123 --name-only
gh pr checks 123
```

**С завитком:**
```bash
PR_NUMBER=123

# PR details (title, author, description, branch)
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER

# Changed files with line counts
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER/files
```

### Шаг 3. Проверьте PR на местном уровне

Это дает вам полный доступ к `read_file`, `search_files` и возможность запускать тесты.

```bash
git fetch origin pull/$PR_NUMBER/head:pr-$PR_NUMBER
git checkout pr-$PR_NUMBER
```

### Шаг 4. Прочтите разницу и поймите изменения.

```bash
# Full diff against the base branch
git diff main...HEAD

# Or file-by-file for large PRs
git diff main...HEAD --name-only
# Then for each file:
git diff main...HEAD -- path/to/file.py
```

Для каждого измененного файла используйте `read_file`, чтобы увидеть полный контекст изменений — одни только различия могут пропустить проблемы, видимые только в окружающем коде.

### Шаг 5. Запустите автоматические проверки локально (если применимо)

```bash
# Run tests if there's a test suite
python -m pytest 2>&1 | tail -20
# or: npm test, cargo test, go test ./..., etc.

# Run linter if configured
ruff check . 2>&1 | head -30
# or: eslint, clippy, etc.
```

### Шаг 6. Примените контрольный список проверки (раздел 3)

Просмотрите каждую категорию: корректность, безопасность, качество кода, тестирование, производительность, документация.

### Шаг 7. Опубликуйте отзыв на GitHub

Соберите свои выводы и отправьте их в виде официального обзора со встроенными комментариями.

**С гх:**
```bash
# If no issues — approve
gh pr review $PR_NUMBER --approve --body "Reviewed by Hermes Agent. Code looks clean — good test coverage, no security concerns."

# If issues found — request changes with inline comments
gh pr review $PR_NUMBER --request-changes --body "Found a few issues — see inline comments."
```

**С помощью Curl — атомарный обзор с несколькими встроенными комментариями:**
```bash
HEAD_SHA=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

# Build the review JSON — event is APPROVE, REQUEST_CHANGES, or COMMENT
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER/reviews \
  -d "{
    \"commit_id\": \"$HEAD_SHA\",
    \"event\": \"REQUEST_CHANGES\",
    \"body\": \"## Hermes Agent Review\n\nFound 2 issues, 1 suggestion. See inline comments.\",
    \"comments\": [
      {\"path\": \"src/auth.py\", \"line\": 45, \"body\": \"🔴 **Critical:** User input passed directly to SQL query — use parameterized queries.\"},
      {\"path\": \"src/models.py\", \"line\": 23, \"body\": \"⚠️ **Warning:** Password stored without hashing.\"},
      {\"path\": \"src/utils.py\", \"line\": 8, \"body\": \"💡 **Suggestion:** This duplicates logic in core/utils.py:34.\"}
    ]
  }"
```

### Шаг 8. Также опубликуйте сводный комментарий

Помимо встроенных комментариев, оставьте резюме верхнего уровня, чтобы автор PR сразу получил полную картину. Используйте формат вывода обзора из `references/review-output-template.md`.

**С гх:**
```bash
gh pr comment $PR_NUMBER --body "$(cat <<'EOF'
## Code Review Summary

**Verdict: Changes Requested** (2 issues, 1 suggestion)

### 🔴 Critical
- **src/auth.py:45** — SQL injection vulnerability

### ⚠️ Warnings
- **src/models.py:23** — Plaintext password storage

### 💡 Suggestions
- **src/utils.py:8** — Duplicated logic, consider consolidating

### ✅ Looks Good
- Clean API design
- Good error handling in the middleware layer

---
*Reviewed by Hermes Agent*
EOF
)"
```

### Шаг 9: Очистка

```bash
git checkout main
git branch -D pr-$PR_NUMBER
```

### Решение: утвердить, запросить изменения или прокомментировать

- **Одобрить** — никаких критических проблем или проблем уровня предупреждения, только незначительные предложения или все ясно.
- **Запросить изменения** – любая критическая проблема или проблема уровня предупреждения, которую следует устранить перед слиянием.
- **Комментарий** — замечания и предложения, но ничего блокирующего (используйте, если вы не уверены или PR является черновиком).