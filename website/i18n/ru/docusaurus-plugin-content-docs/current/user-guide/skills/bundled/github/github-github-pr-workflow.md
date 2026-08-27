---
title: 'Github Pr Workflow — GitHub PR lifecycle: branch, commit, open, CI, merge'
sidebar_label: Github Pr Workflow
description: 'Жизненный цикл PR GitHub: ветка, фиксация, открытие, CI, слияние'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Рабочий процесс Github Pr

Жизненный цикл PR GitHub: разветвление, фиксация, открытие, CI, слияние.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/github/github-pr-workflow` |
| Версия | `1.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `GitHub`, `Pull-Requests`, `CI/CD`, `Git`, `Automation`, `Merge` |
| Сопутствующие навыки | [`github-auth`](/docs/user-guide/skills/bundled/github/github-github-auth), [`github-code-review`](/docs/user-guide/skills/bundled/github/github-github-code-review) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Рабочий процесс запроса на извлечение GitHub

Полное руководство по управлению жизненным циклом PR. В каждом разделе сначала показан путь `gh`, затем резервный вариант `git` + `curl` для компьютеров без `gh`.

## Предварительные условия

- Аутентифицирован с помощью GitHub (см. навык `github-auth`)
- Внутри репозитория git с помощью пульта GitHub.

### Быстрое обнаружение аутентификации

```bash
# Determine which method to use throughout this workflow
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  # Ensure we have a token for API calls
  if [ -z "$GITHUB_TOKEN" ]; then
    if _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(uv run python3 "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py")
    fi
  fi
fi
echo "Using: $AUTH"
```

### Извлечение владельца/репо из Git Remote

Для многих команд `curl` требуется `owner/repo`. Извлеките его из удаленного git:

```bash
# Works for both HTTPS and SSH remote URLs
REMOTE_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
OWNER=$(echo "$OWNER_REPO" | cut -d/ -f1)
REPO=$(echo "$OWNER_REPO" | cut -d/ -f2)
echo "Owner: $OWNER, Repo: $REPO"
```

---

## 1. Создание ветки

Эта часть чистая `git` — идентична в любом случае:

```bash
# Make sure you're up to date
git fetch origin
git checkout main && git pull origin main

# Create and switch to a new branch
git checkout -b feat/add-user-authentication
```

Соглашения об именах ветвей:
- `feat/description` — новые возможности
- `fix/description` — исправлены ошибки.
- `refactor/description` — реструктуризация кода
- `docs/description` — документация
- `ci/description` — Изменения CI/CD

## 2. Создание коммитов

Используйте файловые инструменты агента (`write_file`, `patch`), чтобы внести изменения, а затем зафиксируйте:

```bash
# Stage specific files
git add src/auth.py src/models/user.py tests/test_auth.py

# Commit with a conventional commit message
git commit -m "feat: add JWT-based user authentication

- Add login/register endpoints
- Add User model with password hashing
- Add auth middleware for protected routes
- Add unit tests for auth flow"
```

Формат сообщения фиксации (обычные фиксации):
```
type(scope): short description

Longer explanation if needed. Wrap at 72 characters.
```

Типы: `feat`, `fix`, `refactor`, `docs`, `test`, `ci`, `chore`, `perf`.

## 3. Продвижение и создание пиара

### Нажмите на ветку (в любом случае одинаково)

```bash
git push -u origin HEAD
```

### Создайте пиар

**С гх:**

```bash
gh pr create \
  --title "feat: add JWT-based user authentication" \
  --body "## Summary
- Adds login and register API endpoints
- JWT token generation and validation

## Test Plan
- [ ] Unit tests pass

Closes #42"
```

Опции: `--draft`, `--reviewer user1,user2`, `--label "enhancement"`, `--base develop`.

**С помощью git + Curl:**

```bash
BRANCH=$(git branch --show-current)

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/$OWNER/$REPO/pulls \
  -d "{
    \"title\": \"feat: add JWT-based user authentication\",
    \"body\": \"## Summary\nAdds login and register API endpoints.\n\nCloses #42\",
    \"head\": \"$BRANCH\",
    \"base\": \"main\"
  }"
```

Ответ JSON включает PR `number` — сохраните его для последующих команд.

Чтобы создать черновик, добавьте `"draft": true` в тело JSON.

## 4. Мониторинг состояния CI

### Проверка статуса ЭК

**С гх:**

```bash
# One-shot check
gh pr checks

# Watch until all checks finish (polls every 10s)
gh pr checks --watch
```

**С помощью git + Curl:**

```bash
# Get the latest commit SHA on the current branch
SHA=$(git rev-parse HEAD)

# Query the combined status
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/status \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"Overall: {data['state']}\")
for s in data.get('statuses', []):
    print(f\"  {s['context']}: {s['state']} - {s.get('description', '')}\")"

# Also check GitHub Actions check runs (separate endpoint)
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/check-runs \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for cr in data.get('check_runs', []):
    print(f\"  {cr['name']}: {cr['status']} / {cr['conclusion'] or 'pending'}\")"
```

### Опрос до завершения (git + Curl)

```bash
# Simple polling loop — check every 30 seconds, up to 10 minutes
SHA=$(git rev-parse HEAD)
for i in $(seq 1 20); do
  STATUS=$(curl -s \
    -H "Authorization: token $GITHUB_TOKEN" \
    https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/status \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])")
  echo "Check $i: $STATUS"
  if [ "$STATUS" = "success" ] || [ "$STATUS" = "failure" ] || [ "$STATUS" = "error" ]; then
    break
  fi
  sleep 30
done
```

## 5. Автоматическое исправление сбоев CI

При сбое CI проведите диагностику и устраните. Этот цикл работает с любым методом аутентификации.

### Шаг 1. Получите подробную информацию о сбое

**С гх:**

```bash
# List recent workflow runs on this branch
gh run list --branch $(git branch --show-current) --limit 5

# View failed logs
gh run view <RUN_ID> --log-failed
```

**С помощью git + Curl:**

```bash
BRANCH=$(git branch --show-current)

# List workflow runs on this branch
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/actions/runs?branch=$BRANCH&per_page=5" \
  | python3 -c "
import sys, json
runs = json.load(sys.stdin)['workflow_runs']
for r in runs:
    print(f\"Run {r['id']}: {r['name']} - {r['conclusion'] or r['status']}\")"

# Get failed job logs (download as zip, extract, read)
RUN_ID=<run_id>
curl -s -L \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/runs/$RUN_ID/logs \
  -o /tmp/ci-logs.zip
cd /tmp && unzip -o ci-logs.zip -d ci-logs && cat ci-logs/*.txt
```

### Шаг 2: Исправьте и нажмите

После выявления проблемы используйте файловые инструменты (`patch`, `write_file`), чтобы исправить ее:

```bash
git add <fixed_files>
git commit -m "fix: resolve CI failure in <check_name>"
git push
```

### Шаг 3. Проверьте

Повторно проверьте статус CI, используя команды из раздела 4 выше.

### Шаблон петли с автоисправлением

Когда вас попросят автоматически исправить CI, выполните следующий цикл:

1. Проверка статуса ЭК → выявление сбоев
2. Прочитайте журналы сбоев → поймите ошибку.
3. Используйте `read_file` + `patch`/`write_file` → исправьте код.
4. `git add . && git commit -m "fix: ..." && git push`
5. Подождите CI → еще раз проверьте статус.
6. Повторите, если по-прежнему не получается (до 3 попыток, затем спросите пользователя).

## 6. Слияние

**С гх:**

```bash
# Squash merge + delete branch (cleanest for feature branches)
gh pr merge --squash --delete-branch

# Enable auto-merge (merges when all checks pass)
gh pr merge --auto --squash --delete-branch
```

**С помощью git + Curl:**

```bash
PR_NUMBER=<number>

# Merge the PR via API (squash)
curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/merge \
  -d "{
    \"merge_method\": \"squash\",
    \"commit_title\": \"feat: add user authentication (#$PR_NUMBER)\"
  }"

# Delete the remote branch after merge
BRANCH=$(git branch --show-current)
git push origin --delete $BRANCH

# Switch back to main locally
git checkout main && git pull origin main
git branch -d $BRANCH
```

Методы слияния: `"merge"` (фиксация слияния), `"squash"`, `"rebase"`.

### Включить автоматическое объединение (завиток)

```bash
# Auto-merge requires the repo to have it enabled in settings.
# This uses the GraphQL API since REST doesn't support auto-merge.
PR_NODE_ID=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['node_id'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/graphql \
  -d "{\"query\": \"mutation { enablePullRequestAutoMerge(input: {pullRequestId: \\\"$PR_NODE_ID\\\", mergeMethod: SQUASH}) { clientMutationId } }\"}"
```

## 7. Полный пример рабочего процесса

```bash
# 1. Start from clean main
git checkout main && git pull origin main

# 2. Branch
git checkout -b fix/login-redirect-bug

# 3. (Agent makes code changes with file tools)

# 4. Commit
git add src/auth/login.py tests/test_login.py
git commit -m "fix: correct redirect URL after login

Preserves the ?next= parameter instead of always redirecting to /dashboard."

# 5. Push
git push -u origin HEAD

# 6. Create PR (picks gh or curl based on what's available)
# ... (see Section 3)

# 7. Monitor CI (see Section 4)

# 8. Merge when green (see Section 6)
```

## Справочник полезных PR-команд

| Действие | хх | git + локон |
|--------|-----|-----------|
| Перечислите мои PR | `gh pr list --author @me` | `curl -s -H "Authorization: token $GITHUB_TOKEN" "https://api.github.com/repos/$OWNER/$REPO/pulls?state=open"` |
| Посмотреть разницу в PR | `gh pr diff` | `git diff main...HEAD` (локальный) или `curl -H "Accept: application/vnd.github.diff" ...` |
| Добавить комментарий | `gh pr comment N --body "..."` | `curl -X POST .../issues/N/comments -d '{"body":"..."}'` |
| Запросить обзор | `gh pr edit N --add-reviewer user` | `curl -X POST .../pulls/N/requested_reviewers -d '{"reviewers":["user"]}'` |
| Закрыть PR | `gh pr close N` | `curl -X PATCH .../pulls/N -d '{"state":"closed"}'` |
| Проверить чей-то пиар | `gh pr checkout N` | `git fetch origin pull/N/head:pr-N && git checkout pr-N` |