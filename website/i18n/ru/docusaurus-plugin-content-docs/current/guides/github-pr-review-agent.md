---
sidebar_position: 10
title: 'Учебное пособие: Агент PR-ревью GitHub'
description: Создайте автоматизированную программу проверки кода с использованием
  искусственного интеллекта, которая отслеживает ваши репозитории, просматривает запросы
  на включение и предоставляет обратную связь — без помощи рук.
---

# Учебное пособие: Создание агента PR-обзора GitHub

**Проблема:** Ваша команда открывает запросы на запросы быстрее, чем вы успеваете их просмотреть. Пиарщики сидят целыми днями в ожидании глазных яблок. Младшие разработчики сливают ошибки, потому что никто не успел проверить. Вы проводите утро, разбирая различия, а не строя.

**Решение:** ИИ-агент, который круглосуточно наблюдает за вашими репозиториями, проверяет каждый новый запрос на наличие ошибок, проблем с безопасностью и качеством кода и отправляет вам сводку — так что вы тратите время только на те запросы, которые действительно нуждаются в человеческом суждении.

**Что вы построите:**

```
┌───────────────────────────────────────────────────────────────────┐
│                                                                   │
│   Cron Timer  ──▶  Hermes Agent  ──▶  GitHub API  ──▶  Review     │
│   (every 2h)       + gh CLI           (PR diffs)       delivery   │
│                    + skill                             (Telegram, │
│                    + memory                            Discord,   │
│                                                        local)     │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

В этом руководстве используются **задания cron** для опроса PR по расписанию — сервер или общедоступная конечная точка не требуются. Работает за NAT и межсетевыми экранами.

:::tip Хотите обзоры в реальном времени?
Если у вас есть общедоступная конечная точка, ознакомьтесь с [Автоматизированные PR-комментарии GitHub с помощью веб-хуков] (./webhook-github-pr-review.md). GitHub мгновенно отправляет события в Hermes при открытии или обновлении PR.
:::

---

## Предварительные условия

- **Агент Hermes установлен** — см. [Руководство по установке](/getting-started/installation)
- **Шлюз работает** для заданий cron:
  ```bash
  hermes gateway install   # Install as a service
  # or
  hermes gateway           # Run in foreground
  ```
- **GitHub CLI (`gh`) установлен и прошел проверку подлинности**:
  ```bash
  # Install
  brew install gh        # macOS
  sudo apt install gh    # Ubuntu/Debian

  # Authenticate
  gh auth login
  ```
- **Настроен обмен сообщениями** (необязательно) — [Telegram](/user-guide/messaging/telegram) или [Discord](/user-guide/messaging/discord)

:::tip Нет сообщений? Нет проблем
Используйте `deliver: "local"`, чтобы сохранить отзывы в `~/.hermes/cron/output/`. Отлично подходит для тестирования перед подключением уведомлений.
:::

---

## Шаг 1. Проверьте настройку

Убедитесь, что у Hermes есть доступ к GitHub. Начать чат:

```bash
hermes
```

Проверьте с помощью простой команды:

```
Run: gh pr list --repo NousResearch/hermes-agent --state open --limit 3
```

Вы должны увидеть список открытых PR. Если это сработает, вы готовы.

---

## Шаг 2. Попробуйте выполнить проверку вручную

Еще в чате попросите Гермеса сделать обзор на настоящий пиар:

```
Review this pull request. Read the diff, check for bugs, security issues,
and code quality. Be specific about line numbers and quote problematic code.

Run: gh pr diff 3888 --repo NousResearch/hermes-agent
```

Гермес будет:
1. Выполните `gh pr diff`, чтобы получить изменения кода.
2. Прочтите весь дифф.
3. Проведите структурированный обзор с конкретными выводами.

Если вас устраивает качество, пришло время автоматизировать его.

---

## Шаг 3: Создайте навык обзора

Навык дает Hermes последовательные рекомендации по проверке, которые сохраняются во всех сеансах и запусках cron. Без него качество обзора меняется.

```bash
mkdir -p ~/.hermes/skills/code-review
```

Создайте `~/.hermes/skills/code-review/SKILL.md`:

```markdown
---
name: code-review
description: Review pull requests for bugs, security issues, and code quality
---

# Code Review Guidelines

When reviewing a pull request:

## What to Check
1. **Bugs** — Logic errors, off-by-one, null/undefined handling
2. **Security** — Injection, auth bypass, secrets in code, SSRF
3. **Performance** — N+1 queries, unbounded loops, memory leaks
4. **Style** — Naming conventions, dead code, missing error handling
5. **Tests** — Are changes tested? Do tests cover edge cases?

## Output Format
For each finding:
- **File:Line** — exact location
- **Severity** — Critical / Warning / Suggestion
- **What's wrong** — one sentence
- **Fix** — how to fix it

## Rules
- Be specific. Quote the problematic code.
- Don't flag style nitpicks unless they affect readability.
- If the PR looks good, say so. Don't invent problems.
- End with: APPROVE / REQUEST_CHANGES / COMMENT
```

Убедитесь, что он загружен — запустите `hermes`, и при запуске вы должны увидеть `code-review` в списке навыков.

---

## Шаг 4: Научите его своим условностям

Именно это делает рецензента действительно полезным. Начните занятие и научите Hermes стандартам вашей команды:

```
Remember: In our backend repo, we use Python with FastAPI.
All endpoints must have type annotations and Pydantic models.
We don't allow raw SQL — only SQLAlchemy ORM.
Test files go in tests/ and must use pytest fixtures.
```

```
Remember: In our frontend repo, we use TypeScript with React.
No `any` types allowed. All components must have props interfaces.
We use React Query for data fetching, never useEffect for API calls.
```

Эти воспоминания сохраняются навсегда — рецензент будет обеспечивать соблюдение ваших соглашений, даже не сообщая об этом каждый раз.

---

## Шаг 5. Создайте автоматическое задание Cron

Теперь соедините все это вместе. Создайте задание cron, которое запускается каждые 2 часа:

```bash
hermes cron create "0 */2 * * *" \
  "Check for new open PRs and review them.

Repos to monitor:
- myorg/backend-api
- myorg/frontend-app

Steps:
1. Run: gh pr list --repo REPO --state open --limit 5 --json number,title,author,createdAt
2. For each PR created or updated in the last 4 hours:
   - Run: gh pr diff NUMBER --repo REPO
   - Review the diff using the code-review guidelines
3. Format output as:

## PR Reviews — today

### [repo] #[number]: [title]
**Author:** [name] | **Verdict:** APPROVE/REQUEST_CHANGES/COMMENT
[findings]

If no new PRs found, say: No new PRs to review." \
  --name "pr-review" \
  --deliver telegram \
  --skill code-review
```

Убедитесь, что это запланировано:

```bash
hermes cron list
```

### Другие полезные расписания

| Расписание | Когда |
|----------|------|
| `0 */2 * * *` | Каждые 2 часа |
| `0 9,13,17 * * 1-5` | Три раза в день, только по будням |
| `0 9 * * 1` | Еженедельный обзор утра понедельника |
| `30m` | Каждые 30 минут (репозитории с высоким трафиком) |

---

## Шаг 6: Запускайте по требованию

Не хотите ждать расписания? Запустите его вручную:

```bash
hermes cron run pr-review
```

Или из сеанса чата:

```
/cron run pr-review
```

---

## Идем дальше

### Публикуйте отзывы прямо на GitHub

Вместо доставки в Telegram попросите агента прокомментировать сам пиар:

Добавьте это в приглашение cron:

```
After reviewing, post your review:
- For issues: gh pr review NUMBER --repo REPO --comment --body "YOUR_REVIEW"
- For critical issues: gh pr review NUMBER --repo REPO --request-changes --body "YOUR_REVIEW"
- For clean PRs: gh pr review NUMBER --repo REPO --approve --body "Looks good"
```

:::осторожно
Убедитесь, что `gh` имеет токен с областью действия `repo`. Отзывы публикуются от лица, под которым зарегистрирован `gh`.
:::

### Еженедельный PR-панель

Создайте обзор всех ваших репозиториев в понедельник утром:

```bash
hermes cron create "0 9 * * 1" \
  "Generate a weekly PR dashboard:
- myorg/backend-api
- myorg/frontend-app
- myorg/infra

For each repo show:
1. Open PR count and oldest PR age
2. PRs merged this week
3. Stale PRs (older than 5 days)
4. PRs with no reviewer assigned

Format as a clean summary." \
  --name "weekly-dashboard" \
  --deliver telegram
```

### Мониторинг нескольких репозиториев

Увеличьте масштаб, добавив в приглашение больше репозиториев. Агент обрабатывает их последовательно — никакой дополнительной настройки не требуется.

---

## Устранение неполадок

### "gh: команда не найдена"
Шлюз работает в минимальной среде. Убедитесь, что `gh` находится в системном PATH, и перезапустите шлюз.

### Отзывы слишком общие
1. Добавьте навык `code-review` (шаг 3).
2. Обучите Гермеса своим условностям по памяти (Шаг 4).
3. Чем больше информации о вашей стопке, тем лучше отзывы.

### Задание Cron не запускается
```bash
hermes gateway status    # Is the gateway running?
hermes cron list         # Is the job enabled?
```

### Ограничения скорости
GitHub допускает 5000 запросов API в час для аутентифицированных пользователей. Каждый PR-обзор использует ~3-5 запросов (список + различия + дополнительные комментарии). Даже просмотр 100 PR в день остается в пределах допустимого.

---

## Что дальше?

- **[PR-обзоры на основе Webhook](./webhook-github-pr-review.md)** — получайте мгновенные отзывы при открытии PR-отзывов (требуется публичная конечная точка)
- **[Daily Briefing Bot](/guides/daily-briefing-bot)** — объедините PR-обзоры с утренним дайджестом новостей
- **[Создать плагин](/developer-guide/plugins)** — оберните логику проверки в общий плагин.
- **[Профили](/user-guide/profiles)** — запустить специальный профиль рецензента с собственной памятью и настройками.
- **[Резервные поставщики](/user-guide/features/fallback-providers)** — проверка выполняется даже в случае неработоспособности одного поставщика.