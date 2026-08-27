---
title: Openclaw Migration — Import an OpenClaw setup (memories, skills) into Hermes
sidebar_label: Openclaw Migration
description: Import an OpenClaw setup (memories, skills) into Hermes
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Миграция Openclaw

Импортируйте настройки OpenClaw (воспоминания, навыки) в Hermes.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/migration/openclaw-migration` |
| Путь | `optional-skills/migration/openclaw-migration` |
| Версия | `1.0.0` |
| Автор | Агент Гермеса (Nous Research) |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Migration`, `OpenClaw`, `Hermes`, `Memory`, `Persona`, `Import` |
| Сопутствующие навыки | [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# OpenClaw -> Миграция Гермеса

Используйте этот навык, когда пользователь хочет перенести свою установку OpenClaw в агент Hermes с минимальной ручной очисткой.

## Команда CLI

Для быстрой неинтерактивной миграции используйте встроенную команду CLI:

```bash
hermes claw migrate              # Full interactive migration
hermes claw migrate --dry-run    # Preview what would be migrated
hermes claw migrate --preset user-data   # Migrate without secrets
hermes claw migrate --overwrite  # Overwrite existing conflicts
hermes claw migrate --source /custom/path/.openclaw  # Custom source
```

Команда CLI запускает тот же сценарий миграции, который описан ниже. Используйте этот навык (через агента), если вам нужна интерактивная управляемая миграция с предварительным просмотром и разрешением конфликтов для каждого элемента.

**Первоначальная настройка.** Мастер `hermes setup` автоматически обнаруживает `~/.openclaw` и предлагает выполнить миграцию до начала настройки.

## Что делает этот навык

Он использует `scripts/openclaw_to_hermes.py` для:

- импортируйте `SOUL.md` в домашний каталог Hermes как `SOUL.md`.
- преобразовать OpenClaw `MEMORY.md` и `USER.md` в записи памяти Hermes
- объединить шаблоны утверждения команд OpenClaw с Hermes `command_allowlist`
- перенести настройки обмена сообщениями, совместимые с Hermes, такие как `TELEGRAM_ALLOWED_USERS`, и сопоставить настройки рабочего пространства OpenClaw с конфигурацией рабочего каталога Hermes.
- скопировать навыки OpenClaw в `~/.hermes/skills/openclaw-imports/`
- при необходимости скопируйте файл инструкций рабочего пространства OpenClaw в выбранное рабочее пространство Hermes.
- зеркально отображать совместимые ресурсы рабочей области, такие как `workspace/tts/`, в `~/.hermes/tts/`.
- архивировать несекретные документы, не имеющие прямого назначения Гермесу
- создать структурированный отчет со списком перенесенных элементов, конфликтов, пропущенных элементов и причин.

## Разрешение пути

Вспомогательный скрипт находится в этом каталоге навыков по адресу:

- `scripts/openclaw_to_hermes.py`

Когда этот навык установлен из Центра навыков, обычное расположение:

- `~/.hermes/skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py`

Не угадывайте более короткий путь, например `~/.hermes/skills/openclaw-migration/...`.

Прежде чем запустить помощник:

1. Предпочитайте путь установки `~/.hermes/skills/migration/openclaw-migration/`.
2. Если этот путь не подходит, проверьте каталог установленных навыков и разрешите сценарий относительно установленного `SKILL.md`.
3. Используйте `find` только в качестве запасного варианта, если место установки отсутствует или навык был перемещен вручную.
4. При вызове терминального инструмента не передавайте `workdir: "~"`. Используйте абсолютный каталог, например домашний каталог пользователя, или полностью опустите `workdir`.

С помощью `--migrate-secrets` он также импортирует небольшой набор секретов, совместимых с Hermes, из разрешенного списка, на данный момент:

- `TELEGRAM_BOT_TOKEN`

## Рабочий процесс по умолчанию

1. Inspect first with a dry run.
2. Present a simple summary of what can be migrated, what cannot be migrated, and what would be archived.
3. If the `clarify` tool is available, use it for user decisions instead of asking for a free-form prose reply.
4. If the dry run finds imported skill directory conflicts, ask how those should be handled before executing.
5. Ask the user to choose between the two supported migration modes before executing.
6. Ask for a target workspace path only if the user wants the workspace instructions file brought over.
7. Execute the migration with the matching preset and flags.
8. Summarize the results, especially:
   - what was migrated
   - what was archived for manual review
   - what was skipped and why

## User interaction protocol

Hermes CLI supports the `clarify` tool for interactive prompts, but it is limited to:

- one choice at a time
- up to 4 predefined choices
- an automatic `Other` free-text option

It does **not** support true multi-select checkboxes in a single prompt.

For every `clarify` call:

- always include a non-empty `question`
- include `choices` only for real selectable prompts
- keep `choices` to 2-4 plain string options
- never emit placeholder or truncated options such as `...`
- never pad or stylize choices with extra whitespace
- never include fake form fields in the question such as `enter directory here`, blank lines to fill in, or underscores like `_____`
- for open-ended path questions, ask only the plain sentence; the user types in the normal CLI prompt below the panel

If a `clarify` call returns an error, inspect the error text, correct the payload, and retry once with a valid `question` and clean choices.

When `clarify` is available and the dry run reveals any required user decision, your **next action must be a `clarify` tool call**.
Do not end the turn with a normal assistant message such as:

- "Let me present the choices"
- "What would you like to do?"
- "Here are the options"

If a user decision is required, collect it via `clarify` before producing more prose.
If multiple unresolved decisions remain, do not insert an explanatory assistant message between them. After one `clarify` response is received, your next action should usually be the next required `clarify` call.

Treat `workspace-agents` as an unresolved decision whenever the dry run reports:

- `kind="workspace-agents"`
- `status="skipped"`
- reason containing `No workspace target was provided`

In that case, you must ask about workspace instructions before execution. Do not silently treat that as a decision to skip.

Because of that limitation, use this simplified decision flow:

1. For `SOUL.md` conflicts, use `clarify` with choices such as:
   - `keep existing`
   - `overwrite with backup`
   - `review first`
2. If the dry run shows one or more `kind="skill"` items with `status="conflict"`, use `clarify` with choices such as:
   - `keep existing skills`
   - `overwrite conflicting skills with backup`
   - `import conflicting skills under renamed folders`
3. For workspace instructions, use `clarify` with choices such as:
   - `skip workspace instructions`
   - `copy to a workspace path`
   - `decide later`
4. If the user chooses to copy workspace instructions, ask a follow-up open-ended `clarify` question requesting an **absolute path**.
5. If the user chooses `skip workspace instructions` or `decide later`, proceed without `--workspace-target`.
5. For migration mode, use `clarify` with these 3 choices:
   - `user-data only`
   - `full compatible migration`
   - `cancel`
6. `user-data only` means: migrate user data and compatible config, but do **not** import allowlisted secrets.
7. `full compatible migration` means: migrate the same compatible user data plus the allowlisted secrets when present.
8. If `clarify` is not available, ask the same question in normal text, but still constrain the answer to `user-data only`, `full compatible migration`, or `cancel`.

Execution gate:

- Do not execute while a `workspace-agents` skip caused by `No workspace target was provided` remains unresolved.
- The only valid ways to resolve it are:
  - user explicitly chooses `skip workspace instructions`
  - user explicitly chooses `decide later`
  - user provides a workspace path after choosing `copy to a workspace path`
- Absence of a workspace target in the dry run is not itself permission to execute.
- Do not execute while any required `clarify` decision remains unresolved.

Use these exact `clarify` payload shapes as the default pattern:

- `{"question":"Your existing SOUL.md conflicts with the imported one. What should I do?","choices":["keep existing","overwrite with backup","review first"]}`
- `{"question":"One or more imported OpenClaw skills already exist in Hermes. How should I handle those skill conflicts?","choices":["keep existing skills","overwrite conflicting skills with backup","import conflicting skills under renamed folders"]}`
- `{"question":"Choose migration mode: migrate only user data, or run the full compatible migration including allowlisted secrets?","choices":["user-data only","full compatible migration","cancel"]}`
- `{"question":"Do you want to copy the OpenClaw workspace instructions file into a Hermes workspace?","choices":["skip workspace instructions","copy to a workspace path","decide later"]}`
- `{"question":"Please provide an absolute path where the workspace instructions should be copied."}`

## Decision-to-command mapping

Map user decisions to command flags exactly:

- If the user chooses `keep existing` for `SOUL.md`, do **not** add `--overwrite`.
- If the user chooses `overwrite with backup`, add `--overwrite`.
- If the user chooses `review first`, stop before execution and review the relevant files.
- If the user chooses `keep existing skills`, add `--skill-conflict skip`.
- If the user chooses `overwrite conflicting skills with backup`, add `--skill-conflict overwrite`.
- If the user chooses `import conflicting skills under renamed folders`, add `--skill-conflict rename`.
- If the user chooses `user-data only`, execute with `--preset user-data` and do **not** add `--migrate-secrets`.
- If the user chooses `full compatible migration`, execute with `--preset full --migrate-secrets`.
- Only add `--workspace-target` if the user explicitly provided an absolute workspace path.
- If the user chooses `skip workspace instructions` or `decide later`, do not add `--workspace-target`.

Before executing, restate the exact command plan in plain language and make sure it matches the user's choices.

## Post-run reporting rules

After execution, treat the script's JSON output as the source of truth.

1. Все расчеты основывайте на `report.summary`.
2. Указывайте элемент в разделе «Успешно перенесен», только если его `status` равен точно `migrated`.
3. Не заявляйте, что конфликт разрешен, если в отчете этот элемент не указан как `migrated`.
4. Не говорите, что `SOUL.md` был перезаписан, если в элементе отчета для `kind="soul"` нет `status="migrated"`.
5. Если `report.summary.conflict > 0`, включите раздел о конфликте вместо молчаливого намека на успех.
6. Если количество и перечисленные элементы не совпадают, исправьте список, чтобы он соответствовал отчету, прежде чем отвечать.
7. Включите путь `output_dir` из отчета, если он доступен, чтобы пользователь мог проверить `report.json`, `summary.md`, резервные копии и архивированные файлы.
8. В случае переполнения памяти или профиля пользователя не говорите, что записи были заархивированы, если в отчете явно не указан путь к архиву. Если `details.overflow_file` существует, скажем, туда был экспортирован полный список переполнения.
9. Если навык был импортирован в переименованную папку, укажите конечный пункт назначения и укажите `details.renamed_from`.
10. Если `report.skill_conflict_mode` присутствует, используйте его как источник достоверных данных для выбранной политики конфликта импортированных навыков.
11. Если элемент имеет `status="skipped"`, не описывайте его как перезаписанный, зарезервированный, перенесенный или решенный.
12. Если `kind="soul"` имеет `status="skipped"` с причиной `Target already matches source`, скажите, что он остался без изменений и не упоминайте резервную копию.
13. Если переименованный импортированный навык имеет пустой `details.backup`, это не означает, что существующий навык Hermes был переименован или зарезервирован. Скажите только, что импортированная копия была помещена в новое место назначения, и укажите `details.renamed_from` как существующую папку, которая осталась на месте.

## Предварительные настройки миграции

Предпочитайте эти два пресета при обычном использовании:

- `user-data`
- `full`

`user-data` включает в себя:

- `soul`
- `workspace-agents`
- `memory`
- `user-profile`
- `messaging-settings`
- `command-allowlist`
- `skills`
- `tts-assets`
- `archive`

`full` включает в себя все, что есть в `user-data`, а также:

- `secret-settings`

Вспомогательный скрипт по-прежнему поддерживает `--include`/`--exclude` на уровне категории, но воспринимает это как расширенный запасной вариант, а не как UX по умолчанию.

## Команды

Пробный прогон с полным открытием:

```bash
python3 ~/.hermes/skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py
```

При использовании инструмента терминала отдавайте предпочтение шаблону абсолютного вызова, например:

```json
{"command":"python3 /home/USER/.hermes/skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py","workdir":"/home/USER"}
```

Пробный прогон с предустановленными пользовательскими данными:

```bash
python3 ~/.hermes/skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py --preset user-data
```

Выполните миграцию пользовательских данных:

```bash
python3 ~/.hermes/skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py --execute --preset user-data --skill-conflict skip
```

Выполните полностью совместимую миграцию:

```bash
python3 ~/.hermes/skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py --execute --preset full --migrate-secrets --skill-conflict skip
```

Выполнить с включенными инструкциями рабочей области:

```bash
python3 ~/.hermes/skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py --execute --preset user-data --skill-conflict rename --workspace-target "/absolute/workspace/path"
```

Не используйте `$PWD` или домашний каталог в качестве целевой рабочей области по умолчанию. Сначала запросите явный путь к рабочей области.

## Важные правила

1. Запустите пробный прогон перед записью, если только пользователь явно не скажет продолжить немедленно.
2. Не переносите секреты по умолчанию. Токены, BLOB-объекты аутентификации, учетные данные устройства и необработанная конфигурация шлюза не должны передаваться Hermes, если только пользователь явно не запросит секретную миграцию.
3. Не перезаписывайте непустые целевые объекты Hermes автоматически, если этого явно не хочет пользователь. Вспомогательный сценарий сохранит резервные копии, если включена перезапись.
4. Всегда предоставляйте пользователю отчет о пропущенных элементах. Этот отчет является частью миграции, а не дополнительной опцией.
5. Предпочитайте основное рабочее пространство OpenClaw (`~/.openclaw/workspace/`) вместо `workspace.default/`. Используйте рабочую область по умолчанию только в качестве резервной, если основные файлы отсутствуют.
6. Даже в режиме секретной миграции переносите только секреты с чистым местом назначения Hermes. Неподдерживаемые BLOB-объекты аутентификации по-прежнему должны отображаться как пропущенные.
7. Если пробный прогон показывает большую копию ресурса, конфликтующую запись `SOUL.md` или переполненную память, вызовите их отдельно перед выполнением.
8. По умолчанию — `user-data only`, если пользователь не уверен.
9. Включайте `workspace-agents` только в том случае, если пользователь явно указал путь к целевой рабочей области.
10. Рассматривайте уровень категории `--include` / `--exclude` как дополнительный аварийный выход, а не как обычный поток.
11. Не заканчивайте пробное резюме расплывчатым вопросом: «Чем бы вы хотели заняться?» если `clarify` доступен. Вместо этого используйте структурированные последующие подсказки.
12. Не используйте открытую подсказку `clarify`, когда сработает подсказка с реальным выбором. Сначала отдайте предпочтение выбираемым вариантам, а затем свободному тексту только для абсолютных путей или запросов на проверку файлов.
13. После пробного прогона никогда не останавливайтесь после подведения итогов, если еще осталось нерешенное решение. Используйте `clarify` немедленно для принятия решения о блокировке с наивысшим приоритетом.
14. Порядок приоритетности дополнительных вопросов:
    - `SOUL.md` конфликт
    - импортированные конфликты навыков
    - режим миграции
    - назначение инструкций рабочей области
15. Не обещайте представить варианты выбора позже в том же сообщении. Предъявите их, позвонив по номеру `clarify`.
16. После ответа в режиме миграции явно проверьте, остается ли `workspace-agents` неразрешенным. Если да, то вашим следующим действием должен быть вызов workspace-instructions `clarify`.
17. Если после любого `clarify` ответа остается еще одно необходимое решение, не пересказывайте то, что было только что решено. Немедленно задайте следующий необходимый вопрос.

## Ожидаемый результат

После успешного запуска у пользователя должно быть:

- Импортировано состояние личности Гермеса.
- Файлы памяти Hermes, заполненные конвертированными знаниями OpenClaw.
- Навыки OpenClaw доступны под `~/.hermes/skills/openclaw-imports/`.
- отчет о миграции, показывающий любые конфликты, упущения или неподдерживаемые данные.