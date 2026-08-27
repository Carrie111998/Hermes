---
sidebar_position: 12
title: Работа с навыками
description: Находите, внедряйте, используйте и создавайте навыки — знания по требованию,
  которые обучают Hermes новым рабочим процессам.
---

# Работа с навыками

Навыки — это документы знаний, которые можно получить по требованию, которые учат Hermes тому, как решать конкретные задачи — от создания изображений ASCII до управления PR на GitHub. Это руководство поможет вам использовать их изо дня в день.

Полную техническую информацию см. в [Системе навыков](/user-guide/features/skills).

---

## Поиск навыков

Каждая установка Hermes поставляется с набором навыков. Посмотрите, что доступно:

```bash
# In any chat session:
/skills

# Or from the CLI:
hermes skills list
```

Это показывает компактный список с именами и описаниями:

```
ascii-art         Generate ASCII art using pyfiglet, cowsay, boxes...
arxiv             Search and retrieve academic papers from arXiv...
github-pr-workflow Full PR lifecycle — create branches, commit...
plan              Plan mode — inspect context, write a markdown...
excalidraw        Create hand-drawn style diagrams using Excalidraw...
```

### В поисках навыка

```bash
# Search by keyword
/skills search docker
/skills search music
```

### Центр навыков

Официальные дополнительные навыки (более тяжелые или нишевые навыки, неактивные по умолчанию) доступны через Хаб:

```bash
# Browse official optional skills
/skills browse

# Search the hub
/skills search blockchain
```

---

## Использование навыка

Каждый установленный навык автоматически является командой слэша. Просто введите его имя:

```bash
# Load a skill and give it a task
/ascii-art Make a banner that says "HELLO WORLD"
/plan Design a REST API for a todo app
/github-pr-workflow Create a PR for the auth refactor

# Just the skill name (no task) loads it and lets you describe what you need
/excalidraw
```

Вы также можете активировать навыки посредством естественного разговора — попросите Гермеса использовать определенный навык, и он загрузит его с помощью инструмента `skill_view`.

### Прогрессивное раскрытие информации

Навыки используют шаблон загрузки с эффективным использованием токенов. Агент не загружает все сразу:

1. **`skills_list()`** — компактный список всех навыков (~3 тыс. токенов). Загружается в начале сеанса.
2. **`skill_view(name)`** — полный контент SKILL.md для одного навыка. Загружается, когда агент решает, что ему нужен этот навык.
3. **`skill_view(name, file_path)`** — конкретный справочный файл в рамках навыка. Загружается только при необходимости.

Это означает, что навыки не требуют жетонов до тех пор, пока они не будут фактически использованы.

---

## Установка из хаба

Официальные дополнительные навыки поставляются с Гермесом, но по умолчанию не активны. Установите их явно:

```bash
# Install an official optional skill
hermes skills install official/research/arxiv

# Install from the hub in a chat session
/skills install official/creative/songwriting-and-ai-music

# Install SKILL.md and its referenced support files from an HTTP(S) URL
hermes skills install https://sharethis.chat/SKILL.md
/skills install https://example.com/SKILL.md --name my-skill
```

Что происходит:
1. Каталог навыков копируется в `~/.hermes/skills/`.
2. Он появится в выводе `skills_list`.
3. Он становится доступен как косая черта.

:::совет
Установленные навыки вступают в силу в новых сессиях. Если вы хотите, чтобы он был доступен в текущем сеансе, используйте `/reset`, чтобы начать заново, или добавьте `--now`, чтобы немедленно аннулировать кэш подсказок (на следующем ходу будет стоить больше жетонов).
:::

### Проверка установки

```bash
# Check it's there
hermes skills list | grep arxiv

# Or in chat
/skills search arxiv
```

---

## Навыки, предоставляемые плагином

Плагины могут объединять свои собственные навыки, используя имена в пространстве имен (`plugin:skill`). Это предотвращает конфликты имен со встроенными навыками.

```bash
# Load a plugin skill by its qualified name
skill_view("superpowers:writing-plans")

# Built-in skill with the same base name is unaffected
skill_view("writing-plans")
```

Навыки использования плагинов **не** указаны в системном приглашении и не отображаются в `skills_list`. Они добровольны — загружайте их явно, если знаете, что плагин их предоставляет. При загрузке агент видит баннер со списком однотипных навыков из того же плагина.

О том, как добавить навыки в свой собственный плагин, см. в разделе [Создание плагина Hermes → Пакет навыков](/developer-guide/plugins#bundle-skills).

---

## Настройка параметров навыков

Некоторые навыки декларируют необходимую им конфигурацию во вступительной части:

```yaml
metadata:
  hermes:
    config:
      - key: tenor.api_key
        description: "Tenor API key for GIF search"
        prompt: "Enter your Tenor API key"
        url: "https://developers.google.com/tenor/guides/quickstart"
```

При первой загрузке навыка с конфигурацией Hermes запрашивает значения. Они хранятся в `config.yaml` под `skills.config.*`.

Управляйте конфигурацией навыков из CLI:

```bash
# Interactive config for a specific skill
hermes skills config gif-search

# View all skill config
hermes config get skills.config --json
```

---

## Создание собственного навыка

Навыки — это просто файлы уценки с форматом YAML. Создание одного занимает менее пяти минут.

### 1. Создайте каталог

```bash
mkdir -p ~/.hermes/skills/my-category/my-skill
```

### 2. Напишите SKILL.md

```markdown title="~/.hermes/skills/my-category/my-skill/SKILL.md"
---
name: my-skill
description: Brief description of what this skill does
version: 1.0.0
metadata:
  hermes:
    tags: [my-tag, automation]
    category: my-category
---

# My Skill

## When to Use
Use this skill when the user asks about [specific topic] or needs to [specific task].

## Procedure
1. First, check if [prerequisite] is available
2. Run `command --with-flags`
3. Parse the output and present results

## Pitfalls
- Common failure: [description]. Fix: [solution]
- Watch out for [edge case]

## Verification
Run `check-command` to confirm the result is correct.
```

### 3. Добавьте справочные файлы (необязательно)

Навыки могут включать вспомогательные файлы, которые агент загружает по требованию:

```
my-skill/
├── SKILL.md                    # Main skill document
├── references/
│   ├── api-docs.md             # API reference the agent can consult
│   └── examples.md             # Example inputs/outputs
├── templates/
│   └── config.yaml             # Template files the agent can use
└── scripts/
    └── setup.sh                # Scripts the agent can execute
```

Укажите их в своем SKILL.md:

```markdown
For API details, load the reference: `skill_view("my-skill", "references/api-docs.md")`
```

### 4. Проверьте это

Начните новую сессию и проверьте свои навыки:

```bash
hermes chat -q "/my-skill help me with the thing"
```

Навык появляется автоматически — регистрация не требуется. Добавьте его в `~/.hermes/skills/`, и он будет доступен.

:::информация
Агент также может сам создавать и обновлять навыки, используя `skill_manage`. После решения сложной задачи Гермес может предложить сохранить этот подход как навык для следующего раза.
:::

---

## Управление навыками для каждой платформы

Контролируйте, какие навыки доступны на каких платформах:

```bash
hermes skills
```

Откроется интерактивный TUI, в котором вы можете включать или отключать навыки для каждой платформы (CLI, Telegram, Discord и т. д.). Полезно, если вы хотите, чтобы определенные навыки были доступны только в определенных контекстах — например, чтобы навыки разработки не были доступны в Telegram.

---

## Навыки против памяти

Оба являются постоянными во всех сеансах, но служат разным целям:

| | Навыки | Память |
|---|---|---|
| **Что** | Процедурные знания — как делать | Фактическое знание — что такое |
| **Когда** | Загружается по требованию, только при необходимости | Автоматически вводится в каждый сеанс |
| **Размер** | Может быть большим (сотни строк) | Должен быть компактным (только ключевые факты) |
| **Стоимость** | Ноль жетонов до загрузки | Небольшая, но постоянная стоимость токена |
| **Примеры** | «Как развернуть в Kubernetes» | «Пользователь предпочитает темный режим, живет в PST» |
| **Кто создаёт** | Вы, агент, или установили из Hub | Агент, основанный на разговорах |

**Практическое правило:** Если вы поместите это в справочный документ, это навык. Если бы вы записали это на стикер, то это память.

---

## Советы

**Сосредоточьтесь на навыках.** Навык, который пытается охватить «все DevOps», будет слишком длинным и слишком расплывчатым. Навык «развертывание приложения Python в Fly.io» достаточно специфичен, чтобы быть действительно полезным.

**Позвольте агенту создавать навыки.** После выполнения сложной многоэтапной задачи Hermes часто предлагает сохранить подход как навык. Скажите «да» — эти навыки, созданные агентами, отражают точный рабочий процесс, включая подводные камни, обнаруженные на этом пути.

**Используйте категории.** Разбивайте навыки по подкаталогам (`~/.hermes/skills/devops/`, `~/.hermes/skills/research/` и т. д.). Это обеспечивает управляемость списка и помогает агенту быстрее находить подходящие навыки.

**Обновляйте навыки, когда они устаревают.** Если вы используете навык и столкнулись с проблемами, не охваченными им, попросите Hermes обновить навык, используя то, что вы узнали. Навыки, которые не поддерживаются, становятся помехами.

---

*Полную информацию о навыках — поля заголовка, условную активацию, внешние каталоги и многое другое — см. в [Системе навыков](/user-guide/features/skills).*