---
sidebar_position: 11
title: Автоматизируйте что угодно с помощью Cron
description: Реальные шаблоны автоматизации с использованием Hermes cron — мониторинг,
  отчеты, конвейеры и рабочие процессы с участием нескольких специалистов.
---

# Автоматизируйте что угодно с помощью Cron

[Учебное пособие по боту для ежедневных брифингов](/guides/daily-briefing-bot) охватывает основы. Это руководство идет дальше — пять реальных шаблонов автоматизации, которые вы можете адаптировать для своих рабочих процессов.

Полную информацию о функциях см. в разделе [Запланированные задачи (Cron)](/user-guide/features/cron).

:::информация Ключевая концепция
Задания Cron выполняются в новых сеансах агента без сохранения текущего чата. Подсказки должны быть **полностью самостоятельными** — включать все, что необходимо знать агенту.
:::

:::tip Вам не нужна степень LLM? У вас есть два варианта без жетонов.
- **Повторяющийся сторожевой таймер**, когда сценарий уже выдает точное сообщение (предупреждения памяти, предупреждения диска, тактовые импульсы): используйте [задания cron только для сценариев](/guides/cron-script-only). Тот же планировщик, без LLM. Вы можете попросить Hermes настроить его для вас в чате — инструмент `cronjob` знает, когда выбрать `no_agent=True`, и пишет за вас сценарий.
- **Один снимок из уже запущенного сценария** (этап CI, перехват после фиксации, сценарий развертывания, монитор с внешним планированием): используйте [`hermes send`](/guides/pipe-script-output) для передачи стандартного вывода или файла прямо в Telegram/Discord/Slack/и т. д. без настройки записи cron.
:::

---

## Шаблон 1: Мониторинг изменений веб-сайта

Следите за изменениями по URL-адресу и получайте уведомления только в том случае, если что-то изменится.

Параметр `script` здесь является секретным оружием. Сценарий Python запускается перед каждым выполнением, и его стандартный вывод становится контекстом для агента. Скрипт выполняет механическую работу (извлечение, сравнение); агент обрабатывает рассуждения (интересно ли это изменение?).

Создайте скрипт мониторинга:

```bash
mkdir -p ~/.hermes/scripts
```

```python title="~/.hermes/scripts/watch-site.py"
import hashlib, json, os, urllib.request

URL = "https://example.com/pricing"
STATE_FILE = os.path.expanduser("~/.hermes/scripts/.watch-site-state.json")

# Fetch current content
req = urllib.request.Request(URL, headers={"User-Agent": "Hermes-Monitor/1.0"})
content = urllib.request.urlopen(req, timeout=30).read().decode()
current_hash = hashlib.sha256(content.encode()).hexdigest()

# Load previous state
prev_hash = None
if os.path.exists(STATE_FILE):
    with open(STATE_FILE) as f:
        prev_hash = json.load(f).get("hash")

# Save current state
with open(STATE_FILE, "w") as f:
    json.dump({"hash": current_hash, "url": URL}, f)

# Output for the agent
if prev_hash and prev_hash != current_hash:
    print(f"CHANGE DETECTED on {URL}")
    print(f"Previous hash: {prev_hash}")
    print(f"Current hash: {current_hash}")
    print(f"\nCurrent content (first 2000 chars):\n{content[:2000]}")
else:
    print("NO_CHANGE")
```

Настройте задание cron:

```bash
/cron add "every 1h" "If the script output says CHANGE DETECTED, summarize what changed on the page and why it might matter. If it says NO_CHANGE, respond with just [SILENT]." --script ~/.hermes/scripts/watch-site.py --name "Pricing monitor" --deliver telegram
```

:::tip Трюк с [ТИШИМ]
Для заданий мониторинга cron попросите агента отвечать только `[SILENT]`, если ничего не изменилось. Доставка Cron рассматривает `[SILENT]` как маркер тишины, поэтому вы получаете уведомление только тогда, когда что-то действительно происходит — никакого спама в тихие часы.
:::

---

## Схема 2: Еженедельный отчет

Объедините информацию из нескольких источников в форматированное резюме. Он проводится раз в неделю и транслируется на ваш домашний канал.

```bash
/cron add "0 9 * * 1" "Generate a weekly report covering:

1. Search the web for the top 5 AI news stories from the past week
2. Search GitHub for trending repositories in the 'machine-learning' topic
3. Check Hacker News for the most discussed AI/ML posts

Format as a clean summary with sections for each source. Include links.
Keep it under 500 words — highlight only what matters." --name "Weekly AI digest" --deliver telegram
```

Из интерфейса командной строки:

```bash
hermes cron create "0 9 * * 1" \
  "Generate a weekly report covering the top AI news, trending ML GitHub repos, and most-discussed HN posts. Format with sections, include links, keep under 500 words." \
  --name "Weekly AI digest" \
  --deliver telegram
```

`0 9 * * 1` — это стандартное выражение cron: 9:00 каждый понедельник.

---

## Шаблон 3: Наблюдатель за репозиторием GitHub

Отслеживайте репозиторий на наличие новых выпусков, PR или выпусков.

```bash
/cron add "every 6h" "Check the GitHub repository NousResearch/hermes-agent for:
- New issues opened in the last 6 hours
- New PRs opened or merged in the last 6 hours
- Any new releases

Use the terminal to run gh commands:
  gh issue list --repo NousResearch/hermes-agent --state open --json number,title,author,createdAt --limit 10
  gh pr list --repo NousResearch/hermes-agent --state all --json number,title,author,createdAt,mergedAt --limit 10

Filter to only items from the last 6 hours. If nothing new, respond with [SILENT].
Otherwise, provide a concise summary of the activity." --name "Repo watcher" --deliver discord
```

:::предупреждающие автономные подсказки
Обратите внимание, что приглашение содержит точные команды `gh`. У агента cron нет истории разговоров с предыдущих запусков — изложите все. (Постоянная память загружается, поэтому долговременные настройки, сохраненные в MEMORY.md, сохраняются, но не полагайтесь на нее для получения критически важных данных.)
:::

---

## Шаблон 4: Конвейер сбора данных

Собирайте данные через регулярные промежутки времени, сохраняйте их в файлы и выявляйте тенденции с течением времени. Этот шаблон объединяет сценарий (для сбора) с агентом (для анализа).

```python title="~/.hermes/scripts/collect-prices.py"
import json, os, urllib.request
from datetime import datetime

DATA_DIR = os.path.expanduser("~/.hermes/data/prices")
os.makedirs(DATA_DIR, exist_ok=True)

# Fetch current data (example: crypto prices)
url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
data = json.loads(urllib.request.urlopen(url, timeout=30).read())

# Append to history file
entry = {"timestamp": datetime.now().isoformat(), "prices": data}
history_file = os.path.join(DATA_DIR, "history.jsonl")
with open(history_file, "a") as f:
    f.write(json.dumps(entry) + "\n")

# Load recent history for analysis
lines = open(history_file).readlines()
recent = [json.loads(l) for l in lines[-24:]]  # Last 24 data points

# Output for the agent
print(f"Current: BTC=${data['bitcoin']['usd']}, ETH=${data['ethereum']['usd']}")
print(f"Data points collected: {len(lines)} total, showing last {len(recent)}")
print(f"\nRecent history:")
for r in recent[-6:]:
    print(f"  {r['timestamp']}: BTC=${r['prices']['bitcoin']['usd']}, ETH=${r['prices']['ethereum']['usd']}")
```

```bash
/cron add "every 1h" "Analyze the price data from the script output. Report:
1. Current prices
2. Trend direction over the last 6 data points (up/down/flat)
3. Any notable movements (>5% change)

If prices are flat and nothing notable, respond with [SILENT].
If there's a significant move, explain what happened." \
  --script ~/.hermes/scripts/collect-prices.py \
  --name "Price tracker" \
  --deliver telegram
```

Скрипт выполняет механический сбор; агент добавляет уровень рассуждений.

---

## Схема 5: Рабочий процесс с участием нескольких навыков

Объедините навыки для выполнения сложных запланированных задач. Навыки загружаются по порядку до выполнения приглашения.

```bash
# Use the arxiv skill to find papers, then the obsidian skill to save notes
/cron add "0 8 * * *" "Search arXiv for the 3 most interesting papers on 'language model reasoning' from the past day. For each paper, create an Obsidian note with the title, authors, abstract summary, and key contribution." \
  --skill arxiv \
  --skill obsidian \
  --name "Paper digest"
```

Непосредственно из инструмента:

```python
cronjob(
    action="create",
    skills=["arxiv", "obsidian"],
    prompt="Search arXiv for papers on 'language model reasoning' from the past day. Save the top 3 as Obsidian notes.",
    schedule="0 8 * * *",
    name="Paper digest",
    deliver="local"
)
```

Навыки загружаются по порядку — сначала `arxiv` (обучает агента искать бумаги), затем `obsidian` (обучает писать заметки). Подсказка связывает их вместе.

---

## Управление вашими заданиями

```bash
# List all active jobs
/cron list

# Trigger a job immediately (for testing)
/cron run <job_id>

# Pause a job without deleting it
/cron pause <job_id>

# Edit a running job's schedule or prompt
/cron edit <job_id> --schedule "every 4h"
/cron edit <job_id> --prompt "Updated task description"

# Add or remove skills from an existing job
/cron edit <job_id> --skill arxiv --skill obsidian
/cron edit <job_id> --clear-skills

# Remove a job permanently
/cron remove <job_id>
```

---

## Цели доставки

Флаг `--deliver` определяет, куда отправляются результаты:

| Цель | Пример | Вариант использования |
|--------|---------|----------|
| `origin` | `--deliver origin` | Тот же чат, в котором было создано задание (по умолчанию) |
| `local` | `--deliver local` | Сохранить только в локальный файл |
| `telegram` | `--deliver telegram` | Ваш домашний канал Telegram |
| `discord` | `--deliver discord` | Ваш домашний канал Discord |
| `slack` | `--deliver slack` | Ваш домашний канал Slack |
| Специальный чат | `--deliver telegram:-1001234567890` | Особая группа Telegram |
| Резьбовой | `--deliver telegram:-1001234567890:17585` | Конкретная тема Telegram |
| Чат с ботами | `--deliver bot-chat` | Вставьте вывод в канонический чат этого профиля — бот прочитает его и ответит |
| Бот-чат (по имени) | `--deliver bot-chat:research` | Чат-бот другого местного профиля |

### Доставка бота в чат

Цели `bot-chat` доставляют результаты задания ** в канонический файл "Bot" профиля.
Чат» как настоящее сообщение** — бот получает его как любое другое сообщение,
действует во всем, что требует действий, и отвечает в этом чате. Это
цель для использования, когда вы хотите, чтобы бот *видел* запланированный вывод и реагировал на*
вместо того, чтобы просто архивировать его в истории запусков.

Что нужно знать:

- **Локальный на компьютере.** Профиль должен существовать на компьютере, на котором выполняется
  планировщик (`hermes profile list`). Имена проверяются во время создания;
  профили на других шлюзах/машинах не могут быть нацелены.
- **Стоит ход бота.** Каждая доставка выполняет полный оборот агента в цели.
  Чат бота — соответствующий бюджет для высокочастотных заданий.
- **Комбинируется.** `--deliver bot-chat,telegram` сообщений боту И вашим
  Домашний канал Telegram. Токен `all` никогда не распространяется на цели бот-чата.
- Доставленному сообщению присваивается префикс, поэтому бот знает, что оно пришло по расписанию.
  работа, не от тебя.

---

## Советы

**Сделайте подсказки автономными.** Агент в задании cron не запоминает ваши разговоры. Включите URL-адреса, имена репозиториев, настройки формата и инструкции по доставке непосредственно в приглашение.

**Используйте `[SILENT]` сознательно.** Для задач мониторинга добавьте инструкции типа «если ничего не изменилось, отвечайте только `[SILENT]`». Не просите агента объяснить токен в тихих случаях — cron рассматривает `[SILENT]` как маркер подавления доставки.

**Используйте сценарии для сбора данных.** Параметр `script` позволяет сценарию Python выполнять скучные части (HTTP-запросы, файловый ввод-вывод, отслеживание состояния). Агент видит только стандартный вывод сценария и применяет к нему рассуждения. Это дешевле и надежнее, чем заставлять агента выполнять сбор данных самостоятельно.

**Протестируйте с помощью `/cron run`.** Прежде чем ждать запуска расписания, используйте `/cron run <job_id>` для немедленного выполнения и убедитесь, что выходные данные выглядят правильно.

**Выражения расписания.** Поддерживаемые форматы: относительные задержки (`30m`), интервалы (`every 2h`), стандартные выражения cron (`0 9 * * *`) и временные метки ISO (`2025-06-15T09:00:00`). Естественный язык, такой как `daily at 9am`, не поддерживается — вместо него используйте `0 9 * * *`.

---

*Полную информацию о cron — все параметры, крайние случаи и внутренние компоненты — см. в [Запланированные задачи (Cron)](/user-guide/features/cron).*