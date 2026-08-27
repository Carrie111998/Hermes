---
title: Parallel Cli — собственный веб-поиск, глубокие исследования и расширение возможностей
  агента.
sidebar_label: Parallel Cli
description: Собственный для агента веб-поиск, глубокие исследования и обогащение
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Параллельный клик

Собственный агентский веб-поиск, глубокие исследования и обогащение.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/parallel-cli` |
| Путь | `optional-skills/research/parallel-cli` |
| Версия | `1.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Research`, `Web`, `Search`, `Deep-Research`, `Enrichment`, `CLI` |
| Сопутствующие навыки | [`duckduckgo-search`](/docs/user-guide/skills/optional/research/research-duckduckgo-search), [`mcporter`](/docs/user-guide/skills/optional/mcp/mcp-mcporter) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Параллельный CLI

Используйте `parallel-cli`, когда пользователю явно нужен Parallel или когда собственный рабочий процесс терминала выиграет от стека Parallel, специфичного для конкретного поставщика, для веб-поиска, извлечения, глубокого исследования, обогащения, обнаружения объектов или мониторинга.

Это дополнительный сторонний рабочий процесс, а не основная возможность Hermes.

Важные ожидания:
- Parallel — это платная услуга с бесплатным уровнем, а не полностью бесплатный локальный инструмент.
- Он пересекается с родным для Hermes `web_search`/`web_extract`, поэтому не выбирайте его по умолчанию для обычного поиска.
- Предпочитайте этот навык, когда пользователь конкретно упоминает Parallel или ему нужны такие возможности, как расширение Parallel, FindAll или мониторинг рабочих процессов.

`parallel-cli` предназначен для агентов:
- Вывод JSON через `--json`
- Неинтерактивное выполнение команд
— Асинхронные длительные задания с `--no-wait`, `status` и `poll`.
- Цепочка контекстов с помощью `--previous-interaction-id`.
- Поиск, извлечение, исследование, обогащение, обнаружение объектов и мониторинг в одном интерфейсе командной строки.

## Когда это использовать

Предпочитайте этот навык, когда:
- Пользователь явно упоминает Parallel или `parallel-cli`.
- Задача требует более сложных рабочих процессов, чем простой однократный проход поиска/извлечения.
- Вам нужны асинхронные глубокие исследовательские работы, которые можно будет запустить и опросить позже.
- Вам необходимо структурированное обогащение, обнаружение объектов FindAll или мониторинг.

Предпочитайте родной код Hermes `web_search` / `web_extract` для быстрого одноразового поиска, когда Parallel не требуется специально.

## Установка

Попробуйте наименее инвазивный путь установки, доступный для данной среды.

### Домашнее пиво

```bash
brew install parallel-web/tap/parallel-cli
```

### нпм

```bash
npm install -g parallel-web-cli
```

### Пакет Python

```bash
pip install "parallel-web-tools[cli]"
```

### Автономный установщик

```bash
curl -fsSL https://parallel.ai/install.sh | bash
```

Если вам нужна изолированная установка Python, `pipx` также может работать:

```bash
pipx install "parallel-web-tools[cli]"
pipx ensurepath
```

## Аутентификация

Интерактивный вход:

```bash
parallel-cli login
```

Безголовый/SSH/CI:

```bash
parallel-cli login --device
```

Ключевая переменная среды API:

```bash
export PARALLEL_API_KEY="***"
```

Проверьте текущий статус аутентификации:

```bash
parallel-cli auth
```

Если для аутентификации требуется взаимодействие с браузером, запустите с помощью `pty=true`.

## Основной набор правил

1. Всегда выбирайте `--json`, если вам нужен машиночитаемый результат.
2. Предпочитайте явные аргументы и неинтерактивные потоки.
3. Для длительных заданий используйте `--no-wait`, а затем `status`/`poll`.
4. Цитируйте только URL-адреса, возвращаемые выходными данными CLI.
5. Сохраняйте большие выходные данные JSON во временный файл, если вероятны дополнительные вопросы.
6. Используйте фоновые процессы только для действительно длительных рабочих процессов; в противном случае запустите на переднем плане.
7. Отдавайте предпочтение собственным инструментам Hermes, если только пользователю не нужен именно Parallel или ему не нужны рабочие процессы, основанные только на Parallel.

## Краткая справка

<!-- ascii-guard-ignore -->
```text
parallel-cli
├── auth
├── login
├── logout
├── search
├── extract / fetch
├── research run|status|poll|processors
├── enrich run|status|poll|plan|suggest|deploy
├── findall run|ingest|status|poll|result|enrich|extend|schema|cancel
└── monitor create|list|get|update|delete|events|event-group|simulate
```
<!-- ascii-guard-ignore-end -->

## Общие флаги и шаблоны

Обычно полезные флаги:
- `--json` для структурированного вывода
- `--no-wait` для асинхронных заданий.
- `--previous-interaction-id <id>` для последующих задач, которые повторно используют предыдущий контекст.
- `--max-results <n>` для количества результатов поиска
- `--mode one-shot|agentic` за поведение при поиске
- `--include-domains domain1.com,domain2.com`
- `--exclude-domains domain1.com,domain2.com`
- `--after-date YYYY-MM-DD`

Чтение из стандартного ввода, когда это удобно:

```bash
echo "What is the latest funding for Anthropic?" | parallel-cli search - --json
echo "Research question" | parallel-cli research run - --json
```

## Поиск

Используйте для текущего поиска в Интернете со структурированными результатами.

```bash
parallel-cli search "What is Anthropic's latest AI model?" --json
parallel-cli search "SEC filings for Apple" --include-domains sec.gov --json
parallel-cli search "bitcoin price" --after-date 2026-01-01 --max-results 10 --json
parallel-cli search "latest browser benchmarks" --mode one-shot --json
parallel-cli search "AI coding agent enterprise reviews" --mode agentic --json
```

Полезные ограничения:
- `--include-domains` для сужения надежных источников
- `--exclude-domains` для удаления шумных доменов.
- `--after-date` для фильтрации недавности
– `--max-results`, когда вам нужно более широкое покрытие

Если вы ожидаете дополнительных вопросов, сохраните вывод:

```bash
parallel-cli search "latest React 19 changes" --json -o /tmp/react-19-search.json
```

При подведении итогов:
- направить ответ
- включать даты, имена и конкретные факты
- цитировать только возвращенные источники
- избегайте придумывания URL-адресов или названий источников.

## Извлечение

Используйте для извлечения чистого контента или уценки из URL-адреса.

```bash
parallel-cli extract https://example.com --json
parallel-cli extract https://company.com --objective "Find pricing info" --json
parallel-cli extract https://example.com --full-content --json
parallel-cli fetch https://example.com --json
```

Используйте `--objective`, если страница большая и вам нужен только один фрагмент информации.

## Глубокие исследования

Используйте для более глубоких многоэтапных исследовательских задач, которые могут занять время.

Общие уровни процессоров:
- `lite` / `base` для более быстрых и дешевых проходов.
- `core`/`pro` для более тщательного синтеза
- `ultra` для самых тяжелых исследовательских работ

### Синхронный

```bash
parallel-cli research run \
  "Compare the leading AI coding agents by pricing, model support, and enterprise controls" \
  --processor core \
  --json
```

### Асинхронный запуск + опрос

```bash
parallel-cli research run \
  "Compare the leading AI coding agents by pricing, model support, and enterprise controls" \
  --processor ultra \
  --no-wait \
  --json

parallel-cli research status trun_xxx --json
parallel-cli research poll trun_xxx --json
parallel-cli research processors --json
```

### Цепочка контекстов/последующие действия

```bash
parallel-cli research run "What are the top AI coding agents?" --json
parallel-cli research run \
  "What enterprise controls does the top-ranked one offer?" \
  --previous-interaction-id trun_xxx \
  --json
```

Рекомендуемый рабочий процесс Гермеса:
1. запустить с помощью `--no-wait --json`
2. захватить возвращенный идентификатор запуска/задачи
3. если пользователь хочет продолжить другую работу, продолжайте двигаться
4. позже позвоните `status` или `poll`.
5. резюмируйте окончательный отчет с цитатами из возвращенных источников.

## Обогащение

Используйте, когда у пользователя есть входные данные в формате CSV/JSON/таблицы, и ему нужны дополнительные столбцы, полученные в результате веб-исследований.

### Предлагать столбцы

```bash
parallel-cli enrich suggest "Find the CEO and annual revenue" --json
```

### Планируем конфигурацию

```bash
parallel-cli enrich plan -o config.yaml
```

### Встроенные данные

```bash
parallel-cli enrich run \
  --data '[{"company": "Anthropic"}, {"company": "Mistral"}]' \
  --intent "Find headquarters and employee count" \
  --json
```

### Неинтерактивный запуск файла

```bash
parallel-cli enrich run \
  --source-type csv \
  --source companies.csv \
  --target enriched.csv \
  --source-columns '[{"name": "company", "description": "Company name"}]' \
  --intent "Find the CEO and annual revenue"
```

### Запуск конфигурации YAML

```bash
parallel-cli enrich run config.yaml
```

### Статус/опрос

```bash
parallel-cli enrich status <task_group_id> --json
parallel-cli enrich poll <task_group_id> --json
```

Используйте явные массивы JSON для определений столбцов при работе в неинтерактивном режиме.
Проверьте выходной файл, прежде чем сообщать об успехе.

## Найти все

Используйте для обнаружения объектов веб-масштаба, когда пользователю нужен обнаруженный набор данных, а не короткий ответ.

```bash
parallel-cli findall run "Find AI coding agent startups with enterprise offerings" --json
parallel-cli findall run "AI startups in healthcare" -n 25 --json
parallel-cli findall status <run_id> --json
parallel-cli findall poll <run_id> --json
parallel-cli findall result <run_id> --json
parallel-cli findall schema <run_id> --json
```

Это лучше, чем обычный поиск, когда пользователю нужен обнаруженный набор сущностей, который можно просмотреть, отфильтровать или пополнить позже.

## Монитор

Используйте для постоянного обнаружения изменений с течением времени.

```bash
parallel-cli monitor list --json
parallel-cli monitor get <monitor_id> --json
parallel-cli monitor events <monitor_id> --json
parallel-cli monitor delete <monitor_id> --json
```

Создание обычно является деликатной частью, потому что ритм и подача имеют значение:

```bash
parallel-cli monitor create --help
```

Используйте это, когда пользователю нужно периодическое отслеживание страницы или источника, а не однократное получение.

## Рекомендуемые шаблоны использования Hermes

### Быстрый ответ с цитатами
1. Запустите `parallel-cli search ... --json`
2. Анализ заголовков, URL-адресов, дат, выдержек.
3. Подведите итог, используя только встроенные цитаты из возвращенных URL-адресов.

### URL-расследование
1. Запустите `parallel-cli extract URL --json`
2. При необходимости повторите запуск с помощью `--objective` или `--full-content`.
3. Процитируйте или суммируйте полученную уценку.

### Длительный рабочий процесс исследования
1. Запустите `parallel-cli research run ... --no-wait --json`
2. Сохраните возвращенный идентификатор.
3. Продолжайте другую работу или периодически проводите опросы.
4. Подведите итоги итогового отчета с цитатами.

### Рабочий процесс структурированного обогащения
1. Проверьте входной файл и столбцы.
2. Используйте `enrich suggest` или предоставьте явные расширенные столбцы.
3. Запустите `enrich run`.
4. При необходимости опрос для завершения
5. Проверьте выходной файл, прежде чем сообщать об успехе.

## Обработка ошибок и коды выхода

CLI документирует эти коды выхода:
- `0` успеха
- `2` неправильный ввод
- `3` ошибка авторизации
- `4` ошибка API
- Тайм-аут `5`

Если вы нажмете ошибки аутентификации:
1. проверьте `parallel-cli auth`
2. подтвердите `PARALLEL_API_KEY` или запустите `parallel-cli login` / `parallel-cli login --device`.
3. убедитесь, что `parallel-cli` находится на `PATH`.

## Техническое обслуживание

Проверьте текущее состояние аутентификации/установки:

```bash
parallel-cli auth
parallel-cli --help
```

Команды обновления:

```bash
parallel-cli update
pip install --upgrade parallel-web-tools
parallel-cli config auto-update-check off
```

## Подводные камни

- Не опускайте `--json`, если пользователь явно не хочет, чтобы выходные данные были отформатированы человеком.
- Не цитируйте источники, которых нет в выводе CLI.
- `login` может потребовать взаимодействия PTY/браузера.
- Предпочитайте выполнение на переднем плане для коротких задач; не злоупотребляйте фоновыми процессами.
– Для больших наборов результатов сохраняйте JSON в `/tmp/*.json` вместо того, чтобы помещать все в контекст.
- Не выбирайте Parallel молча, если встроенных инструментов Hermes уже достаточно.
– Помните, что это рабочий процесс поставщика, который обычно требует аутентификации учетной записи и платного использования, выходящего за рамки бесплатного уровня.