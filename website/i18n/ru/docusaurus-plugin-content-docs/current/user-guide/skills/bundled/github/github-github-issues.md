---
title: Проблемы Github — создавайте, сортируйте, маркируйте и назначайте проблемы
  GitHub через gh или REST.
sidebar_label: Github Issues
description: Создавайте, сортируйте, маркируйте и назначайте проблемы GitHub с помощью
  gh или REST.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Проблемы с Github

Создавайте, сортируйте, маркируйте и назначайте проблемы GitHub с помощью gh или REST.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/github/github-issues` |
| Версия | `1.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `GitHub`, `Issues`, `Project-Management`, `Bug-Tracking`, `Triage` |
| Сопутствующие навыки | [`github-auth`](/docs/user-guide/skills/bundled/github/github-github-auth), [`github-pr-workflow`](/docs/user-guide/skills/bundled/github/github-github-pr-workflow) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Управление проблемами GitHub

Создавайте, ищите, сортируйте и управляйте проблемами GitHub. В каждом разделе сначала отображается `gh`, а затем резервный вариант `curl`.

## Предварительные условия

- Аутентифицирован с помощью GitHub (см. навык `github-auth`)
- Внутри репозитория git с помощью пульта GitHub или явно укажите репозиторий.

### Настройка

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

## 1. Просмотр проблем

**С гх:**

```bash
gh issue list
gh issue list --state open --label "bug"
gh issue list --assignee @me
gh issue list --search "authentication error" --state all
gh issue view 42
```

**С завитком:**

```bash
# List open issues
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/issues?state=open&per_page=20" \
  | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    if 'pull_request' not in i:  # GitHub API returns PRs in /issues too
        labels = ', '.join(l['name'] for l in i['labels'])
        print(f\"#{i['number']:5}  {i['state']:6}  {labels:30}  {i['title']}\")"

# Filter by label
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/issues?state=open&labels=bug&per_page=20" \
  | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    if 'pull_request' not in i:
        print(f\"#{i['number']}  {i['title']}\")"

# View a specific issue
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/42 \
  | python3 -c "
import sys, json
i = json.load(sys.stdin)
labels = ', '.join(l['name'] for l in i['labels'])
assignees = ', '.join(a['login'] for a in i['assignees'])
print(f\"#{i['number']}: {i['title']}\")
print(f\"State: {i['state']}  Labels: {labels}  Assignees: {assignees}\")
print(f\"Author: {i['user']['login']}  Created: {i['created_at']}\")
print(f\"\n{i['body']}\")"

# Search issues
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/search/issues?q=authentication+error+repo:$OWNER/$REPO" \
  | python3 -c "
import sys, json
for i in json.load(sys.stdin)['items']:
    print(f\"#{i['number']}  {i['state']:6}  {i['title']}\")"
```

## 2. Создание проблем

**С гх:**

```bash
gh issue create \
  --title "Login redirect ignores ?next= parameter" \
  --body "## Description
After logging in, users always land on /dashboard.

## Steps to Reproduce
1. Navigate to /settings while logged out
2. Get redirected to /login?next=/settings
3. Log in
4. Actual: redirected to /dashboard (should go to /settings)

## Expected Behavior
Respect the ?next= query parameter." \
  --label "bug,backend" \
  --assignee "username"
```

**С завитком:**

```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues \
  -d '{
    "title": "Login redirect ignores ?next= parameter",
    "body": "## Description\nAfter logging in, users always land on /dashboard.\n\n## Steps to Reproduce\n1. Navigate to /settings while logged out\n2. Get redirected to /login?next=/settings\n3. Log in\n4. Actual: redirected to /dashboard\n\n## Expected Behavior\nRespect the ?next= query parameter.",
    "labels": ["bug", "backend"],
    "assignees": ["username"]
  }'
```

### Шаблон отчета об ошибке

```
## Bug Description
<What's happening>

## Steps to Reproduce
1. <step>
2. <step>

## Expected Behavior
<What should happen>

## Actual Behavior
<What actually happens>

## Environment
- OS: <os>
- Version: <version>
```

### Шаблон запроса функции

```
## Feature Description
<What you want>

## Motivation
<Why this would be useful>

## Proposed Solution
<How it could work>

## Alternatives Considered
<Other approaches>
```

## 3. Управление проблемами

### Добавление/удаление ярлыков

**С гх:**

```bash
gh issue edit 42 --add-label "priority:high,bug"
gh issue edit 42 --remove-label "needs-triage"
```

**С завитком:**

```bash
# Add labels
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/42/labels \
  -d '{"labels": ["priority:high", "bug"]}'

# Remove a label
curl -s -X DELETE \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/42/labels/needs-triage

# List available labels in the repo
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/labels \
  | python3 -c "
import sys, json
for l in json.load(sys.stdin):
    print(f\"  {l['name']:30}  {l.get('description', '')}\")"
```

### Назначение

**С гх:**

```bash
gh issue edit 42 --add-assignee username
gh issue edit 42 --add-assignee @me
```

**С завитком:**

```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/42/assignees \
  -d '{"assignees": ["username"]}'
```

### Комментирование

**С гх:**

```bash
gh issue comment 42 --body "Investigated — root cause is in auth middleware. Working on a fix."
```

**С завитком:**

```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/42/comments \
  -d '{"body": "Investigated — root cause is in auth middleware. Working on a fix."}'
```

### Закрытие и повторное открытие

**С гх:**

```bash
gh issue close 42
gh issue close 42 --reason "not planned"
gh issue reopen 42
```

**С завитком:**

```bash
# Close
curl -s -X PATCH \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/42 \
  -d '{"state": "closed", "state_reason": "completed"}'

# Reopen
curl -s -X PATCH \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/42 \
  -d '{"state": "open"}'
```

### Связь проблем с PR

Проблемы автоматически закрываются при слиянии PR с правильными ключевыми словами в теле:

```
Closes #42
Fixes #42
Resolves #42
```

Чтобы создать ветку из задачи:

**С гх:**

```bash
gh issue develop 42 --checkout
```

**С помощью git (эквивалент вручную):**

```bash
git checkout main && git pull origin main
git checkout -b fix/issue-42-login-redirect
```

## 4. Рабочий процесс сортировки проблем

Когда вас просят сортировать проблемы:

1. **Перечислите неотсортированные проблемы:**

```bash
# With gh
gh issue list --label "needs-triage" --state open

# With curl
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/issues?labels=needs-triage&state=open" \
  | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    if 'pull_request' not in i:
        print(f\"#{i['number']}  {i['title']}\")"
```

2. **Прочитайте и классифицируйте** каждую проблему (просмотрите подробности, поймите ошибку/функцию).

3. **Примените ярлыки и приоритет** (см. «Управление проблемами» выше).

4. **Назначьте**, если владелец ясен.

5. **При необходимости добавьте комментарий с примечаниями к сортировке**.

## 5. Массовые операции

Для пакетных операций объедините вызовы API со сценариями оболочки:

**С гх:**

```bash
# Close all issues with a specific label
gh issue list --label "wontfix" --json number --jq '.[].number' | \
  xargs -I {} gh issue close {} --reason "not planned"
```

**С завитком:**

```bash
# List issue numbers with a label, then close each
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/issues?labels=wontfix&state=open" \
  | python3 -c "import sys,json; [print(i['number']) for i in json.load(sys.stdin)]" \
  | while read num; do
    curl -s -X PATCH \
      -H "Authorization: token $GITHUB_TOKEN" \
      https://api.github.com/repos/$OWNER/$REPO/issues/$num \
      -d '{"state": "closed", "state_reason": "not_planned"}'
    echo "Closed #$num"
  done
```

## Краткая справочная таблица

| Действие | хх | конечная точка завитка |
|--------|-----|--------------|
| Список проблем | `gh issue list` | `GET /repos/{o}/{r}/issues` |
| Посмотреть выпуск | `gh issue view N` | `GET /repos/{o}/{r}/issues/N` |
| Создать задачу | `gh issue create ...` | `POST /repos/{o}/{r}/issues` |
| Добавить ярлыки | `gh issue edit N --add-label ...` | `POST /repos/{o}/{r}/issues/N/labels` |
| Назначить | `gh issue edit N --add-assignee ...` | `POST /repos/{o}/{r}/issues/N/assignees` |
| Комментарий | `gh issue comment N --body ...` | `POST /repos/{o}/{r}/issues/N/comments` |
| Закрыть | `gh issue close N` | `PATCH /repos/{o}/{r}/issues/N` |
| Поиск | `gh issue list --search "..."` | `GET /search/issues?q=...` |