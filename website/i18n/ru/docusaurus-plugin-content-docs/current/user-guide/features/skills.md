---
sidebar_position: 2
title: Система навыков
description: Документы знаний по запросу — постепенное раскрытие информации, навыки,
  управляемые агентом, и Центр навыков
---

# Система навыков

Навыки — это документы знаний, которые агент может загрузить по требованию. Они следуют шаблону **прогрессивного раскрытия**, чтобы минимизировать использование токенов, и совместимы с открытым стандартом [agentskills.io](https://agentskills.io/specification).

Все навыки хранятся в **`~/.hermes/skills/`** — основном каталоге и источнике истины. При новой установке связанные навыки копируются из репозитория. Сюда также относятся навыки, установленные в хабе и созданные агентом. Агент может изменить или удалить любой навык.

Вы также можете указать Гермесу **внешние каталоги навыков** — дополнительные папки, сканируемые наряду с локальной. См. [Внешние каталоги навыков](#external-skill-directory) ниже.

См. также:

- [Каталог комплексных навыков](/reference/skills-catalog)
- [Официальный каталог дополнительных навыков](/reference/optional-skills-catalog)

## Начинаем с чистого листа

По умолчанию каждый профиль заполняется каталогом связанных навыков, и каждый `hermes update` добавляет новые объединенные навыки. Если вам нужен профиль **без объединенных навыков** — и он остается пустым при обновлении — у вас есть два пути:

**Во время установки** (применяется к профилю `~/.hermes` по умолчанию):

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --no-skills
```

**Во время создания профиля** (именованные профили):

```bash
hermes profile create research --no-skills
```

**В уже установленном профиле** (по умолчанию или с именем) переключите его во время выполнения:

```bash
hermes skills opt-out            # stop future seeding — nothing on disk is touched
hermes skills opt-out --remove   # also delete UNMODIFIED bundled skills (confirms first)
hermes skills opt-in --sync      # undo: remove the marker and re-seed now
```

Все три пути записывают маркер `.no-bundled-skills` в каталог профиля. Пока маркер присутствует, установщик `hermes update` и любая синхронизация навыков пропускают заполнение набора навыков для этого профиля. Удалите маркер (или запустите `hermes skills opt-in`), чтобы снова включить его.

:::note Безопасно по умолчанию
`hermes skills opt-out` только останавливает *будущее* заполнение — он никогда не удаляет что-либо, уже находящееся на диске. Необязательный флаг `--remove` удаляет связанные навыки **только**, если они не изменены (идентичны по байтам установленной версии Hermes). Навыки, которые вы отредактировали, навыки, установленные из хаба, а также навыки, которые вы написали самостоятельно, всегда сохраняются.
:::

## Использование навыков

Каждый установленный навык автоматически доступен с помощью косой черты:

```bash
# In the CLI or any messaging platform:
/gif-search funny cats
/axolotl help me fine-tune Llama 3 on my dataset
/github-pr-workflow create a PR for the auth refactor
/plan design a rollout for migrating our auth provider

# Just the skill name loads it and lets the agent ask what you need:
/excalidraw
```

### Объединение нескольких навыков в одну команду

Вы можете активировать несколько навыков в одном сообщении, объединяя команды с косой чертой.
в начале — загружается каждый ведущий токен `/skill` (до 5), а остальные
станет вашей инструкцией:

```bash
/github-pr-workflow /test-driven-development fix issue #123 and open a PR
```

Анализ останавливается на первом токене, который не является установленным навыком, поэтому аргументы
которые начинаются с `/` (например, пути к файлам), никогда не проглатываются:

```bash
/ocr-and-documents /tmp/scan.pdf extract the tables   # loads one skill; /tmp/scan.pdf is the argument
```

Для комбинаций, которые вы используете неоднократно, отдайте предпочтение [набору навыков](#skill-bundles) —
тот же эффект при одной короткой команде.

Хорошим примером является встроенный навык `plan`. Запуск `/plan [request]` загружает инструкции навыка, сообщая Hermes о необходимости проверить контекст, написать план реализации уценки вместо выполнения задачи и сохранить результат в `.hermes/plans/` относительно активной рабочей области/рабочего каталога серверной части.

Вы также можете взаимодействовать с навыками посредством естественного разговора:

```bash
hermes chat --toolsets skills -q "What skills do you have?"
hermes chat --toolsets skills -q "Show me the axolotl skill"
```

## Изучение навыка из источников (`/learn`)

`/learn` — это быстрый способ перевернуть что-то, что вы уже знаете, или кучу
справочный материал — в навык многократного использования, без написания от руки
`SKILL.md`. Он открыт: направьте его на *все, что вы можете описать* и
агент собирает материал с помощью уже имеющихся у него инструментов, а затем разрабатывает навык
который соответствует [собственным стандартам разработки](#skillmd-format) (≤60 символов)
описание, стандартный порядок сечения, корпус инструмента Гермес, не изобретено
команды).

```bash
# A local SDK or doc directory — read with read_file / search_files
/learn the REST client in ~/projects/acme-sdk, focus on auth + pagination

# An online doc page — fetched with web_extract
/learn https://docs.example.com/api/quickstart

# The workflow you just walked the agent through in this conversation
/learn how I just deployed the staging server

# Pasted notes / a described procedure
/learn filing an expense: open the portal, New > Expense, attach the receipt, submit

# A whole book, paper stack, or large docs corpus — becomes a knowledge-base skill
/learn ~/books/designing-data-intensive-applications.pdf
```

### Большие источники превращаются в навыки работы с базой знаний

Если источником является книга, стопка документов, спецификация или большая папка с документами,
агент не сжимает их в один файл и не сводит к сводке с потерями.
Вместо этого он создает **навык обширной базы знаний**: бережливый `SKILL.md`
содержащий основные ментальные модели источника плюс индекс, с одним очищенным
файл на главу или тему в папке `references/` (плюс глоссарий или шпаргалка).
когда источник их зарабатывает). Справочные файлы ничего не стоят, пока не возникнет вопрос.
нужен один — агент загружает их по требованию с помощью `skill_view`, поэтому стоимость запроса
остается пропорциональным ответу, а не источнику. Повторный запуск `/learn` с новым
материал по той же теме складывает его в уже имеющийся навык, а не
создание дубликата.

Дистилляция синтезирует структуру — рамки, определения, решения.
правила, антишаблоны — и никогда не воспроизводит отрывки исходного текста.

Поскольку источниками данных занимается живой агент, `/learn` работает в CLI точно так же,
шлюз обмена сообщениями, TUI и панель мониторинга — и на любой серверной части терминала
(локальный, Docker, удаленный), поскольку отдельного механизма приема нет. В
**панель управления**, на странице «Навыки» есть кнопка **Изучить навык**, которая открывает панель.
с полем каталога, полем URL-адреса и открытым текстовым полем; это составляет
`/learn` запрос и запускает его в чате.

Нет необходимости использовать инструмент модели: `/learn` создает подсказку, основанную на стандартах, и
передает его агенту как обычный ход. Агент сохраняет результат с помощью
`skill_manage`, поэтому [ворота утверждения записи](#gating-agent-skill-writes-skillswrite_approval)
применяется, если он у вас включен.

## Прогрессивное раскрытие информации

Навыки используют шаблон загрузки с эффективным использованием токенов:

```
Level 0: skills_list()           → [{name, description, category}, ...]   (~3k tokens)
Level 1: skill_view(name)        → Full content + metadata       (varies)
Level 2: skill_view(name, path)  → Specific reference file       (varies)
```

Агент загружает полный контент навыков только тогда, когда он действительно в этом нуждается.

## Формат SKILL.md

```markdown
---
name: my-skill
description: Brief description of what this skill does
version: 1.0.0
platforms: [macos, linux]     # Optional — restrict to specific OS platforms
metadata:
  hermes:
    tags: [python, automation]
    category: devops
    fallback_for_toolsets: [web]    # Optional — conditional activation (see below)
    requires_toolsets: [terminal]   # Optional — conditional activation (see below)
    config:                          # Optional — config.yaml settings
      - key: my.setting
        description: "What this controls"
        default: "value"
        prompt: "Prompt for setup"
---

# Skill Title

## When to Use
Trigger conditions for this skill.

## Procedure
1. Step one
2. Step two

## Pitfalls
- Known failure modes and fixes

## Verification
How to confirm it worked.
```

### Навыки, специфичные для платформы

Навыки могут ограничиваться конкретными операционными системами с помощью поля `platforms`:

| Значение | Матчи |
|-------|---------|
| `macos` | macOS (Дарвин) |
| `linux` | Линукс |
| `windows` | Окна |

```yaml
platforms: [macos]            # macOS only (e.g., iMessage, Apple Reminders, FindMy)
platforms: [macos, linux]     # macOS and Linux
```

Если этот навык установлен, он автоматически скрывается из системной подсказки `skills_list()` и команд косой черты на несовместимых платформах. Если этот параметр опущен, навык загружается на всех платформах.

## Результативные навыки и доставка медиа

Когда ответ навыка (или любой ответ агента) включает в себя пустой абсолютный путь к медиафайлу — например, `/home/user/screenshots/diagram.png` — шлюз автоматически обнаруживает его, удаляет его из видимого текста и доставляет файл в чат пользователя (фото Telegram, вложение Discord и т. д.) вместо того, чтобы оставлять необработанный путь в сообщении.

В частности, для аудио директива `[[audio_as_voice]]` продвигает аудиофайлы в собственные всплывающие сообщения голосовых сообщений на платформах, которые их поддерживают (Telegram, WhatsApp).

### Принудительная доставка в стиле документа: `[[as_document]]`

Иногда вам нужно **противоположное** встроенному предварительному просмотру: вы хотите, чтобы файл был доставлен в виде загружаемого вложения, а не в виде повторно сжатого пузырька изображения. Классическим примером является скриншот или диаграмма с высоким разрешением: Telegram `sendPhoto` повторно сжимает его до ~200 КБ при разрешении 1280 пикселей, ухудшая читабельность. PNG-файл размером 1–2 МБ, отправленный через `sendDocument`, сохраняет исходные байты нетронутыми.

Если ответ (или любой текст внутри него — обычно последняя строка) содержит литеральную директиву `[[as_document]]`, каждый путь к медиафайлу, извлеченный из этого ответа, доставляется в виде вложения документа/файла, а не в виде пузырька изображения:

```
Here is your rendered chart:

/home/user/.hermes/cache/chart-q4-2025.png

[[as_document]]
```

Директива удаляется перед доставкой, поэтому пользователи ее никогда не увидят. Детализация намеренно задается по принципу «все или ничего» для каждого ответа: выдайте `[[as_document]]` один раз, и каждый путь к изображению в одном ответе будет доставлен как документ. Это отражает область действия `[[audio_as_voice]]`.

Используйте его из навыка, когда:

- Вы создаете скриншоты или диаграммы, необходимые пользователю, в виде файлов (для редактирования в другом инструменте, архивирования и совместного использования без изменений).
- Предварительный просмотр с потерями по умолчанию будет скрывать детали (мелкий текст, диаграммы с точностью до пикселя, цветочувствительные рендеры).

Платформы без отдельного пути к документу (например, SMS) прибегают к любому имеющемуся у них механизму прикрепления.

### Условная активация (запасные навыки)

Навыки могут автоматически отображаться или скрываться в зависимости от того, какие инструменты доступны в текущем сеансе. Это наиболее полезно для **запасных навыков** — бесплатных или локальных альтернатив, которые должны появляться только тогда, когда премиум-инструмент недоступен.

```yaml
metadata:
  hermes:
    fallback_for_toolsets: [web]      # Show ONLY when these toolsets are unavailable
    requires_toolsets: [terminal]     # Show ONLY when these toolsets are available
    fallback_for_tools: [web_search]  # Show ONLY when these specific tools are unavailable
    requires_tools: [terminal]        # Show ONLY when these specific tools are available
```

| Поле | Поведение |
|-------|----------|
| `fallback_for_toolsets` | Навык **скрыт**, когда доступны перечисленные наборы инструментов. Показано, когда они отсутствуют. |
| `fallback_for_tools` | То же, но проверяется отдельные инструменты, а не наборы инструментов. |
| `requires_toolsets` | Навык **скрыт**, когда перечисленные наборы инструментов недоступны. Показано, когда они присутствуют. |
| `requires_tools` | То же, но проверяет отдельные инструменты. |

**Пример:** встроенный навык `duckduckgo-search` использует `fallback_for_toolsets: [web]`. Если у вас установлен `FIRECRAWL_API_KEY`, набор веб-инструментов доступен и агент использует `web_search` — навык DuckDuckGo остается скрытым. Если ключ API отсутствует, набор веб-инструментов недоступен, а навык DuckDuckGo автоматически отображается в качестве резервного варианта.

Навыки без каких-либо условных полей ведут себя точно так же, как и раньше — они всегда отображаются.

## Безопасная настройка при загрузке

Навыки могут объявлять необходимые переменные среды, не исчезая из поля зрения:

```yaml
required_environment_variables:
  - name: TENOR_API_KEY
    prompt: Tenor API key
    help: Get a key from https://developers.google.com/tenor
    required_for: full functionality
```

При обнаружении отсутствующего значения Hermes безопасно запрашивает его только тогда, когда навык действительно загружен в локальный CLI. Вы можете пропустить настройку и продолжить использование навыка. Обмен сообщениями никогда не запрашивает секреты в чате — вместо этого они советуют вам использовать `hermes setup` или `~/.hermes/.env` локально.

После установки объявленные переменные окружения **автоматически передаются** в песочницы `execute_code` и `terminal` — сценарии навыка могут использовать `$TENOR_API_KEY` напрямую. Для переменных окружения, не требующих навыков, используйте параметр конфигурации `terminal.env_passthrough`. Подробности см. в разделе [Передача переменных среды](/user-guide/security#environment-variable-passthrough).

### Настройки конфигурации навыков

Навыки также могут объявлять несекретные настройки конфигурации (пути, настройки), хранящиеся в `config.yaml`:

```yaml
metadata:
  hermes:
    config:
      - key: myplugin.path
        description: Path to the plugin data directory
        default: "~/myplugin-data"
        prompt: Plugin data directory path
```

Настройки хранятся в папке `skills.config` вашего config.yaml. `hermes config migrate` запрашивает ненастроенные параметры, а `hermes config show` отображает их. Когда навык загружается, его разрешенные значения конфигурации вводятся в контекст, поэтому агент автоматически узнает настроенные значения.

Подробности см. в [Настройки навыков](/user-guide/configuration#skill-settings) и [Создание навыков — Настройки конфигурации](/developer-guide/creating-skills#config-settings-configyaml).

## Структура каталога навыков

```text
~/.hermes/skills/                  # Single source of truth
├── mlops/                         # Category directory
│   ├── axolotl/
│   │   ├── SKILL.md               # Main instructions (required)
│   │   ├── references/            # Additional docs
│   │   ├── templates/             # Output formats
│   │   ├── scripts/               # Helper scripts callable from the skill
│   │   ├── examples/              # Referenced example outputs
│   │   └── assets/                # Supplementary files
│   └── vllm/
│       └── SKILL.md
├── devops/
│   └── deploy-k8s/                # Agent-created skill
│       ├── SKILL.md
│       └── references/
├── .hub/                          # Skills Hub state
│   ├── lock.json
│   ├── quarantine/
│   └── audit.log
└── .bundled_manifest              # Tracks seeded bundled skills
```

Сторонние URL-адреса и установки GitHub включают `SKILL.md` плюс точный локальный
файлы, на которые он ссылается, под `references/`, `templates/`, `scripts/`, `assets/`,
и `examples/`. Файлы репозитория, на которые нет ссылок, не копируются. Гермес сканирует
заполняет пакет, помещенный в карантин, и записывает исходный URL-адрес, точный хэш контента,
версия сканера, результаты, временная метка и статус «свежий» или «кэшированный» в
`skills/.hub/lock.json`.

### Консультативное сканирование SkillEvaluator

В дополнение к встроенному сканеру безопасности (который обеспечивает установку
указанную выше политику), Hermes может запускать [NVIDIA SkillEvaluator](https://github.com/NVIDIA/SkillEvaluator)
Уровень 1 проверяет каждую установку концентратора в качестве второго мнения. Уровень 1
детерминированный и бесключевой доступ — обнаружение PII (утечка электронной почты, личные пути,
строки подключения), обнаружение контрабанды Юникода, анализ сценариев, лицензия
соответствие требованиям и статическое сканирование безопасности с помощью
[NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector).

Сканирование носит **только рекомендательный характер**: результаты распечатываются в виде файла и строки.
перед подтверждением установки, и установка продолжается. Выводы, которые
выглядеть как настоящие учетные данные (закрытые ключи, ключи доступа к облаку, токены,
строки подключения с учетными данными) выделены красным, чтобы вы могли просмотреть
отмеченные строки перед принятием решения. Результаты класса PII носят информационный характер —
вышестоящему сканеру известны ложноположительные классы (например,
`git@github.com` Синтаксис SSH, электронные письма с примерами документации), поэтому они никогда
заблокировать что-либо.

Чтобы включить его, установите дополнительные двоичные файлы сканера (второй
проверка `security`; без этого проверка просто сообщает "не запущена"):

```bash
uv tool install --python 3.13 \
  "skillevaluator @ git+https://github.com/NVIDIA/SkillEvaluator.git@v0.1.0"
uv tool install "git+https://github.com/NVIDIA/SkillSpector.git@v2.9.5"
```

Без двоичного файла в PATH сканирование автоматически пропускается. Чтобы выключить его
полностью:

```yaml
skills:
  tier1_advisory: false
```

Кнопка сканирования Browse-hub на панели мониторинга возвращает те же консультативные данные, что и в
его ответ (поле `tier1`) вместе с вердиктом встроенного сканера.

## Внешние каталоги навыков

Если у вас есть навыки за пределами Hermes — например, общий каталог `~/.agents/skills/`, используемый несколькими инструментами искусственного интеллекта, — вы можете поручить Hermes сканировать и эти каталоги.

Добавьте `external_dirs` в раздел `skills` в `~/.hermes/config.yaml`:

```yaml
skills:
  external_dirs:
    - ~/.agents/skills
    - /home/shared/team-skills
    - ${SKILLS_REPO}/skills
```

Пути поддерживают расширение `~` и замену переменных среды `${VAR}`.

### Как это работает

- **Создать локально, обновить на месте**: новые навыки, созданные оператором, записываются в `~/.hermes/skills/`. Существующие навыки изменяются там, где они находятся, включая навыки под `external_dirs`, когда оператор использует действия `skill_manage`, такие как `patch`, `edit`, `write_file`, `remove_file` или `delete`.
- **Внешние каталоги не являются границей защиты от записи**: если внешний каталог навыков доступен для записи процессу Hermes, обновления навыков, управляемые агентом, могут изменить файлы в этом каталоге. Используйте разрешения файловой системы или отдельную настройку профиля/набора инструментов, если общие внешние навыки должны оставаться доступными только для чтения.
- **Локальный приоритет**: если одно и то же имя навыка существует как в локальном, так и во внешнем каталоге, побеждает локальная версия.
- **Полная интеграция**: внешние навыки отображаются в индексе системных подсказок `skills_list`, `skill_view` и в виде косой черты `/skill-name` — ничем не отличаясь от локальных навыков.
- **Несуществующие пути автоматически пропускаются**: Если настроенный каталог не существует, Hermes игнорирует его без ошибок. Полезно для дополнительных общих каталогов, которые могут присутствовать не на каждой машине.

### Пример

```text
~/.hermes/skills/               # Local (primary, read-write)
├── devops/deploy-k8s/
│   └── SKILL.md
└── mlops/axolotl/
    └── SKILL.md

~/.agents/skills/               # External (shared, mutable if writable)
├── my-custom-workflow/
│   └── SKILL.md
└── team-conventions/
    └── SKILL.md
```

Все четыре навыка отображаются в вашем индексе навыков. Если вы создадите новый навык под названием `my-custom-workflow` локально, он затенит внешнюю версию.

## Проектные местные навыки

Репозитории могут иметь свои собственные навыки, активные только для сеансов, начатых внутри этого проекта — тот же шаблон, который используют другие агенты для локальной конфигурации репо. Когда вы запускаете Hermes внутри git checkout, он ищет навыки в:

```text
<project-root>/.hermes/skills/    # Hermes-native location
<project-root>/.agents/skills/    # cross-tool convention (shared with other agent CLIs)
```

Корневой каталог проекта — это ближайший родительский каталог, содержащий `.git` (количество рабочих деревьев и подмодулей).

### Доверие к проекту

Навыки — это документы процедур, которым следует агент, поэтому Hermes **не** автоматически загружает их из произвольных клонированных репозиториев. При первом запуске Hermes в репозитории с навыками проекта на баннере отображается уведомление:

```text
◆ 3 project skill(s) found in /home/you/myproject but not loaded — run `hermes skills trust` to enable them.
```

Доверьтесь репо один раз (изнутри него или передав путь):

```bash
hermes skills trust             # trust the current repo
hermes skills trust ~/myproject # or explicitly
hermes skills untrust           # revoke
```

Доверенные корни хранятся в `skills.trusted_project_dirs` в `~/.hermes/config.yaml`. Установите `skills.project_discovery: false`, чтобы полностью отключить эту функцию (без сканирования и уведомлений).

### Приоритет

Навыки проекта имеют **уровень наивысшего приоритета**: `project → local (~/.hermes/skills/) → external_dirs`. Навык проекта с именем `deploy` переопределяет одноименный профиль или связанный навык для сеансов внутри этого репозитория — в этом вся суть: навыки, предоставляемые поставщиком репозитория, выигрывают на своей домашней территории, не затрагивая ваш глобальный профиль. Навыки проекта отмечены тегом `[project]` в индексе навыков агента, поэтому происхождение остается видимым.

Как и внешние каталоги, каталоги навыков проекта считаются принадлежащими репозиторию: автономное обслуживание навыков (куратор) никогда не изменяет их, а новые навыки, созданные агентом, всегда передаются в `~/.hermes/skills/`.

### Карантин на время сканирования

Доверие — это решение на уровне репо, но содержание навыков репо меняется с каждым `git pull`. Чтобы устранить этот пробел, каждый навык проекта сканируется с помощью того же сканера безопасности, который используется для установок Skills Hub, прежде чем он попадает в индекс. Навык, вердикт сканирования которого является **опасным** (директивы быстрого внедрения, команды эксфильтрации учетных данных, трюки со скрытым текстом), помещается в карантин: он не отображается в индексе навыка, `skills_list`, командах с косой чертой и отказывается загружаться по имени с поясняющей ошибкой. Сканирования кэшируются в виде хэша контента под `~/.hermes/cache/project_skill_scans/` (никогда не внутри вашего репозитория) и автоматически перезапускаются при изменении содержимого навыка.

### Неинтерактивные поверхности (cron, API, ACP)

Задания Cron и другие неинтерактивные поверхности наследуют ваше интерактивное решение о доверии — они никогда не запрашивают и никогда не устанавливают автоматическое доверие. Корень проекта определяется из рабочего каталога поверхности (`workdir` задания cron, с помощью того же механизма, который использует инструмент терминала). Задание cron, `workdir` которого находится внутри репозитория, которому вы ранее доверяли, загружает навыки проекта этого репозитория; задание в ненадежном или неопределенном репозитории не загружает ничего.

## Наборы навыков

Пакеты навыков — это крошечные файлы YAML, которые группируют несколько навыков под одной косой чертой. При запуске `/<bundle-name>` все навыки, перечисленные в пакете, загружаются одновременно — это полезно, когда для конкретной задачи всегда используется один и тот же набор навыков вместе.

### Быстрый пример

```bash
# Create a bundle for backend feature work
hermes bundles create backend-dev \
  --skill github-code-review \
  --skill test-driven-development \
  --skill github-pr-workflow \
  -d "Backend feature work — review, test, PR workflow"
```

Затем в CLI или любой платформе шлюза:

```
/backend-dev refactor the auth middleware
```

Агент получает все три навыка, загруженные в одно пользовательское сообщение, с любым текстом после косой черты, прикрепленным в качестве инструкции пользователя.

### YAML-схема

Пакеты живут в **`~/.hermes/skill-bundles/<slug>.yaml`** и выглядят следующим образом:

```yaml
name: backend-dev
description: Backend feature work — review, test, PR workflow.
skills:
  - github-code-review
  - test-driven-development
  - github-pr-workflow
instruction: |
  Always start by writing failing tests, then implement.
  Open the PR through the standard workflow with co-author tags.
```

Поля:
- `name` (необязательно — по умолчанию используется основа имени файла) — отображаемое имя пакета. Нормализовано к дефису для команды косой черты (`Backend Dev` → `/backend-dev`).
- `description` (необязательно) — краткий текст, показанный в `/bundles` и `hermes bundles list`.
- `skills` (обязательный, непустой список) — названия навыков или пути относительно вашего каталога навыков. Используйте тот же идентификатор, который вы передаете `/<skill-name>`.
- `instruction` (необязательно) — к загруженному содержимому навыков добавляется дополнительное руководство. Полезно для систематизации того, «как мы всегда используем их вместе».

### Управление пакетами

```bash
# List all installed bundles
hermes bundles list

# Inspect one bundle
hermes bundles show backend-dev

# Create a bundle interactively (omit --skill flags to enter them one per line)
hermes bundles create research

# Overwrite an existing bundle
hermes bundles create backend-dev --skill ... --force

# Delete a bundle
hermes bundles delete backend-dev

# Re-scan ~/.hermes/skill-bundles/ and report changes
hermes bundles reload
```

В сеансе чата `/bundles` перечисляет все установленные пакеты и их навыки.

### Поведение

- **Связки имеют приоритет над отдельными навыками** при столкновении слизней. Если вы называете пакет `research` и у вас также есть навык `research`, `/research` вызывает пакет. Это сделано намеренно — вы выбрали пакет, присвоив ему имя.
- **Отсутствующие навыки пропускаются, а не являются фатальными.** Если в пакете указан `skill-foo` и вы его не установили, пакет все равно загружает навыки, которые разрешаются, и агент получает заметку со списком того, что было пропущено.
- **Пакеты работают на всех платформах** — в интерактивном интерфейсе командной строки, TUI, чате панели управления и на каждой платформе шлюза (Telegram, Discord, Slack…) — поскольку диспетчеризация централизована в том же месте, что и отдельные команды навыков.
- **Пакеты не делают недействительным кэш подсказок.** Они генерируют новое сообщение пользователя во время вызова, так же, как это делает `/<skill-name>` — без изменения системных подсказок.

### Когда пакеты лучше, чем установка каждого навыка вручную

Используйте пакет, когда:
– Вы всегда сочетаете одни и те же навыки для повторяющихся задач (`/backend-dev`, `/release-prep`, `/incident-response`).
- Вам нужна ментальная модель короче на один символ, чем ввод нескольких вызовов `/skill` подряд.
- Вы хотите отправить «профиль задачи» для всей команды, проверив пакет YAML в общем репозитории точечных файлов и создав символическую ссылку на него в `~/.hermes/skill-bundles/`.

Пакет — это всего лишь псевдоним YAML — он не устанавливает для вас навыки. Сами навыки уже должны присутствовать (в `~/.hermes/skills/` или во внешнем каталоге навыков). В противном случае вызов пакета просто пропускает недостающие.

## Навыки, управляемые агентом (инструмент Skill_manage)

Агент может создавать, обновлять и удалять свои собственные навыки с помощью инструмента `skill_manage`. Это **процедурная память** агента: когда он определяет нетривиальный рабочий процесс, он сохраняет этот подход как навык для будущего повторного использования.

Навыки и память работают вместе в цикле самосовершенствования: память хранит
небольшие прочные факты, которые всегда должны быть в контексте, а навыки сохраняются дольше
процедуры, которые должны загружаться только при необходимости. Обзор предыстории может
предлагать или инсценировать изменения навыков после сеанса, но ворота одобрения записи
ниже позволяет вам потребовать проверки человеком, прежде чем эти изменения вступят в силу.

### Когда агент создает навыки

В системном приглашении агенту предлагается записать нетривиальный рабочий процесс с `skill_manage` для
будущее повторное использование. На практике это касается:

- Когда выработался многоэтапный рабочий процесс, который стоит повторить
- Когда он сталкивается с ошибками или тупиками и находит рабочий путь
- Когда пользователь исправил свой подход

### Действия

| Действие | Использовать для | Ключевые параметры |
|--------|---------|------------|
| `create` | Новый навык с нуля | `name`, `content` (полный SKILL.md), опционально `category` |
| `patch` | Целевые исправления (предпочтительно) | `name`, `old_string`, `new_string` |
| `edit` | Основные структурные изменения | `name`, `content` (полная замена SKILL.md) |
| `delete` | Полностью удалить навык | `name` |
| `write_file` | Добавить/обновить вспомогательные файлы | `name`, `file_path`, `file_content` |
| `remove_file` | Удалить вспомогательный файл | `name`, `file_path` |

:::совет
Действие `patch` предпочтительнее для обновлений — оно более эффективно использует токены, чем `edit`, поскольку при вызове инструмента отображается только измененный текст.
:::

### Запись навыка агента-врата (`skills.write_approval`)

По умолчанию агент свободно записывает навыки — в том числе из [background
обзор самосовершенствования](/user-guide/features/memory#controlling-memory-writes-write_approval)
который работает после поворота. Если вы предпочитаете одобрить каждый навык, сначала напишите
(небольшие модели, которые неправильно оценивают то, чему они научились, защищают среду или просто
желающим следить за циклом самосовершенствования), включите шлюз одобрения записи:

```yaml
skills:
  write_approval: false     # false = write freely (default) | true = require approval
```

Когда `write_approval: true` каждый `skill_manage` пишет (создает/редактирует/
patch/delete/write_file/remove_file) **проиндексирован**, а не зафиксирован —
файл SKILL.md слишком велик для просмотра в режиме реального времени, поэтому промежуточный вариант применяется независимо от
независимо от того, была ли запись произведена в ходе переднего плана или в ходе фонового обзора.
Поэтапная запись сохраняется при перезапуске под `~/.hermes/pending/skills/` и
проверяются с помощью того же знакомого процесса одобрения/отклонения, что и опасные команды:

```
/skills pending             # list staged skill writes + a one-line gist each
/skills diff <id>           # full unified diff (best viewed in CLI or dashboard)
/skills approve <id>        # apply it (or 'all')
/skills reject <id>         # drop it (or 'all')
/skills approval on         # turn the gate on (or 'off') and persist it
```

Поверхность обзора работает в интерактивном интерфейсе командной строки и на платформах обмена сообщениями.
(вывод различий обрезается для всплывающих окон чата — прочитайте полный текст различий в CLI или
в ожидающем файле JSON). Запись в память имеет один и тот же вентиль под
`memory.write_approval` — см. [Управление записью в память](/user-guide/features/memory#controlling-memory-writes-write_approval).

> Отдельная настройка `skills.guard_agent_created` — сканер контента
> (эвристика опасных шаблонов), а не ворота одобрения — это два
> независимый. См. [Защита от записи навыков, созданных агентом](/user-guide/configuration#guard-on-agent-created-skill-writes).

## Центр навыков

Просматривайте, ищите, устанавливайте навыки и управляйте ими из онлайн-реестров, `skills.sh`, прямых конечных точек известных навыков и официальных дополнительных навыков.

### Общие команды

```bash
hermes skills browse                              # Browse all hub skills (official first)
hermes skills browse --source official            # Browse only official optional skills
hermes skills search kubernetes                   # Search all sources
hermes skills search react --source skills-sh     # Search the skills.sh directory
hermes skills search https://mintlify.com/docs --source well-known
hermes skills inspect openai/skills/k8s           # Preview before installing
hermes skills install openai/skills/k8s           # Install with security scan
hermes skills install official/security/1password
hermes skills install skills-sh/vercel-labs/json-render/json-render-react --force
hermes skills install well-known:https://mintlify.com/docs/.well-known/skills/mintlify
hermes skills install https://sharethis.chat/SKILL.md              # Direct URL (+ referenced support files)
hermes skills install https://example.com/SKILL.md --name my-skill # Override name when frontmatter has none
hermes skills list --source hub                   # List hub-installed skills
hermes skills check                               # Check installed hub skills for upstream updates
hermes skills update                              # Reinstall hub skills with upstream changes when needed
hermes skills audit                               # Re-scan all hub skills for security
hermes skills uninstall k8s                       # Remove a hub skill
hermes skills reset google-workspace              # Un-stick a bundled skill from "user-modified" (see below)
hermes skills reset google-workspace --restore    # Also restore the bundled version, deleting your local edits
hermes skills publish skills/my-skill --to github --repo owner/repo
hermes skills snapshot export setup.json          # Export skill config
hermes skills tap add myorg/skills-repo           # Add a custom GitHub source
```

### Поддерживаемые источники хаба

| Источник | Пример | Заметки |
|--------|---------|-------|
| `official` | `official/security/1password` | Дополнительные навыки поставляются вместе с Hermes. |
| `skills-sh` | `skills-sh/vercel-labs/agent-skills/vercel-react-best-practices` | Доступен для поиска через `hermes skills search <query> --source skills-sh`. Hermes разрешает навыки в стиле псевдонимов, когда фрагмент файлаkills.sh отличается от папки репо. |
| `well-known` | `well-known:https://mintlify.com/docs/.well-known/skills/mintlify` | Навыки предоставляются непосредственно из `/.well-known/skills/index.json` на веб-сайте. Выполните поиск по URL-адресу сайта или документов. |
| `url` | `https://sharethis.chat/SKILL.md` | Прямой URL-адрес HTTP(S) на `SKILL.md` плюс явные ссылки на файлы поддержки. Разрешение имени: заголовок → URL-адрес → интерактивная подсказка → флаг `--name`. |
| `github` | `openai/skills/k8s` | Прямая установка репозитория/пути GitHub и пользовательские нажатия. |
| `clawhub`, `lobehub`, `browse-sh` | Идентификаторы источника | Интеграция сообщества или рынка. |

### Интегрированные хабы и реестры

В настоящее время Hermes интегрируется со следующими экосистемами навыков и источниками открытий:

#### 1. Официальные дополнительные навыки (`official`)

Они хранятся в самом репозитории Hermes и устанавливаются со встроенным доверием.

- Каталог: [Официальный каталог дополнительных навыков](../../reference/optional-skills-catalog)
- Источник в репозитории: `optional-skills/`
- Пример:

```bash
hermes skills browse --source official
hermes skills install official/security/1password
```

#### 2.skills.sh (`skills-sh`)

Это каталог общедоступных навыков Vercel. Hermes может выполнять поиск напрямую, просматривать страницы с подробными сведениями о навыках, разрешать фрагменты в стиле псевдонимов и устанавливать из базового репозитория исходного кода.

- Каталог: [skills.sh](https://skills.sh/)
- Репозиторий CLI/инструментов: [vercel-labs/skills](https://github.com/vercel-labs/skills)
- Официальный репозиторий навыков Vercel: [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills)
- Пример:

```bash
hermes skills search react --source skills-sh
hermes skills inspect skills-sh/vercel-labs/json-render/json-render-react
hermes skills install skills-sh/vercel-labs/json-render/json-render-react --force
```

#### 3. Конечные точки общеизвестных навыков (`well-known`)

Это обнаружение на основе URL-адресов с сайтов, публикующих `/.well-known/skills/index.json`. Это не единый централизованный хаб — это соглашение по веб-обнаружению.

– Пример активной конечной точки: [Индекс навыков документации Mintlify](https://mintlify.com/docs/.well-known/skills/index.json)
— Реализация эталонного сервера: [vercel-labs/skills-handler](https://github.com/vercel-labs/skills-handler)
- Пример:

```bash
hermes skills search https://mintlify.com/docs --source well-known
hermes skills inspect well-known:https://mintlify.com/docs/.well-known/skills/mintlify
hermes skills install well-known:https://mintlify.com/docs/.well-known/skills/mintlify
```

#### 4. Навыки работы с GitHub (`github`)

Hermes можно установить непосредственно из репозиториев GitHub и кранов на базе GitHub. Это полезно, если вы уже знаете репозиторий/путь или хотите добавить свой собственный репозиторий с исходным кодом.

Касания по умолчанию (доступны для просмотра без каких-либо настроек):
- [openai/skills](https://github.com/openai/skills)
- [антропика/навыки](https://github.com/anthropics/skills)
- [huggingface/skills](https://github.com/huggingface/skills)
- [NVIDIA/skills](https://github.com/NVIDIA/skills) — навыки, проверенные NVIDIA (подпись `skill.oms.sig` + управление `skill-card.md`)
- [garrytan/gstack](https://github.com/garrytan/gstack)

- Пример:

```bash
hermes skills install openai/skills/k8s
hermes skills tap add myorg/skills-repo
```

**Группы категорий (`skills.sh.json`).** Кран GitHub может отправить
`skills.sh.json` в корне репозитория после
[схемаskills.sh](https://skills.sh/schemas/skills.sh.schema.json). Это
`groupings` (каждый с `title` и списком названий навыков) считывается по индексу
время и станут метками категорий, показанными в
Страница [Skills Hub](https://hermes-agent.nousresearch.com/docs) — вместо
предположение на основе тега. Это общий принцип: любое нажатие, при котором файл отправляется, становится реальным.
категоризации, никаких изменений со стороны Гермеса не требуется.

```json
{
  "$schema": "https://skills.sh/schemas/skills.sh.schema.json",
  "groupings": [
    { "title": "Inference AI", "skills": ["dynamo-recipe-runner", "dynamo-router-sla"] },
    { "title": "Decision Optimization", "skills": ["cuopt-developer", "cuopt-install"] }
  ]
}
```

#### 5. ClawHub (`clawhub`)

Сторонний рынок навыков, интегрированный в качестве источника сообщества.

- Сайт: [clawhub.ai](https://clawhub.ai/)
- Идентификатор источника Hermes: `clawhub`

#### 6. LobeHub (`lobehub`)

Hermes может искать и конвертировать записи агентов из общедоступного каталога LobeHub в устанавливаемые навыки Hermes.

- Сайт: [LobeHub](https://lobehub.com/)
- Индекс публичных агентов: [chat-agents.lobehub.com](https://chat-agents.lobehub.com/)
- Репозиторий поддержки: [lobehub/lobe-chat-agents](https://github.com/lobehub/lobe-chat-agents)
- Идентификатор источника Hermes: `lobehub`

#### 7. Browse.sh (`browse-sh`)

Hermes интегрируется с [browse.sh](https://browse.sh), каталогом Browserbase, содержащим более 200 файлов SKILL.md для автоматизации браузера для конкретного сайта (Airbnb, Amazon, arXiv, 12306.cn, Etsy, Xero и многие другие). Каждый навык описывает, как комплексно управлять одним веб-сайтом, и подходит для использования с инструментами браузера Hermes и любыми навыками автоматизации браузера, которые вы уже установили.

- Сайт: [browse.sh](https://browse.sh/)
- API каталога: `https://browse.sh/api/skills`
- Идентификатор источника Гермеса: `browse-sh`
- Уровень доверия: `community`

```bash
hermes skills search airbnb --source browse-sh
hermes skills inspect browse-sh/airbnb.com/search-listings-ddgioa
hermes skills install browse-sh/airbnb.com/search-listings-ddgioa
```

Идентификаторы используют форму `browse-sh/<hostname>/<task-id>` и соответствуют фрагменту, представленному в каталоге Browse.sh. Контент обрабатывается через конечную точку детализации каждого навыка (`/api/skills/<slug>` → `skillMdUrl`), а не через GitHub каталога `sourceUrl`.

#### 8. Прямой URL (`url`)

Установите `SKILL.md` непосредственно с любого URL-адреса HTTP(S) — полезно, когда автор размещает навык на своем собственном сайте (без списка хаба, без пути GitHub для ввода). Hermes также извлекает файлы с явными ссылками в каталогах `references/`, `templates/`, `scripts/`, `assets/` и `examples/`, затем сканирует и устанавливает полный пакет.

- Идентификатор источника Hermes: `url`
- Идентификатор: сам URL-адрес (префикс не требуется).
– Область: `SKILL.md` плюс точные вспомогательные файлы в каталогах из разрешенного списка. Hermes не перечисляет и не копирует несвязанные файлы с хоста.

```bash
hermes skills install https://sharethis.chat/SKILL.md
hermes skills install https://example.com/my-skill/SKILL.md --category productivity
```

Разрешение имени, по порядку:
1. Поле `name:` во вступительной части YAML SKILL.md (рекомендуется — оно есть у каждого правильно сформированного навыка).
2. Имя родительского каталога из URL-пути (например, `.../my-skill/SKILL.md` → `my-skill` или `.../my-skill.md` → `my-skill`), если это действительный идентификатор (`^[a-z][a-z0-9_-]*$`).
3. Интерактивная подсказка на терминале с телетайпом.
4. На неинтерактивных поверхностях (косая черта `/skills install` внутри TUI, шлюзовых платформ, скриптов) чистая ошибка, указывающая на переопределение `--name`.

```bash
# Frontmatter has no name and the URL slug is unhelpful — supply one:
hermes skills install https://example.com/SKILL.md --name sharethis-chat

# Or inside a chat session:
/skills install https://example.com/SKILL.md --name sharethis-chat
```

Уровень доверия всегда равен `community` — выполняется такое же сканирование безопасности, как и для любого другого источника. URL-адрес сохраняется как идентификатор установки, поэтому `hermes skills update` автоматически выполняет повторную выборку с того же URL-адреса, когда вы хотите обновить его.

### Сканирование безопасности и `--force`

Все навыки, установленные в хабе, проходят через **сканер безопасности**, который проверяет на предмет кражи данных, быстрого внедрения, деструктивных команд, сигналов цепочки поставок и других угроз.

`hermes skills inspect ...` теперь также отображает вышестоящие метаданные, если они доступны:
- URL-адрес репо
- URL-адрес страницы с подробными сведениями о навыках.sh
- команда установки
- еженедельные установки
- статусы аудита безопасности восходящего потока
- известные URL-адреса индексов/конечных точек

Используйте `--force`, если вы просмотрели сторонний навык и хотите отменить неопасную блокировку политики:

```bash
hermes skills install skills-sh/anthropics/skills/pdf --force
```

Важное поведение:
- `--force` может переопределить блокировку политики для выводов в стиле предостережения/предупреждения.
- `--force` **не** отменяет вердикт сканирования `dangerous`.
- Официальные дополнительные навыки (`official/...`) рассматриваются как встроенные и не отображаются на сторонней панели предупреждений.

### Уровни доверия

| Уровень | Источник | Политика |
|-------|--------|--------|
| `builtin` | Корабли с Гермесом | Всегда доверяли |
| `official` | `optional-skills/` в репозитории | Встроенное доверие, никаких предупреждений третьих лиц |
| `trusted` | Доверенные реестры/репозитории, такие как `openai/skills`, `anthropics/skills`, `huggingface/skills`, `NVIDIA/skills` | Более либеральная политика, чем общественные источники |
| `community` | Все остальное (`skills.sh`, общеизвестные конечные точки, пользовательские репозитории GitHub, большинство торговых площадок) | Неопасные выводы можно отменить с помощью `--force`; `dangerous` приговоров остаются заблокированными |

### Жизненный цикл обновления

Теперь хаб отслеживает достаточно источников, чтобы перепроверять исходные копии установленных навыков:

```bash
hermes skills check          # Report which installed hub skills changed upstream
hermes skills update         # Reinstall only the skills with updates available
hermes skills update react   # Update one specific installed hub skill
hermes skills update react --force   # Overwrite a skill you've edited locally
```

При этом используется сохраненный идентификатор источника плюс текущий хэш содержимого восходящего пакета для обнаружения отклонения.

Навыки, которые вы редактировали локально (содержимое на диске больше не соответствует хешу, записанному во время установки), **пропускаются** `hermes skills update`, поэтому ваши изменения никогда не перезаписываются автоматически. Передайте `--force`, чтобы в любом случае заменить их исходной версией.

:::подсказка об ограничениях скорости GitHub
В операциях Центра навыков используется API GitHub, который имеет ограничение скорости 60 запросов в час для неаутентифицированных пользователей. Если вы видите ошибки ограничения скорости во время установки или поиска, установите `GITHUB_TOKEN` в файле `.env`, чтобы увеличить лимит до 5000 запросов в час. Сообщение об ошибке содержит полезную подсказку, если это произойдет.
:::

### Публикация пользовательского крана навыков

Если вы хотите поделиться тщательно подобранным набором навыков — для вашей команды, вашей организации или публично — вы можете опубликовать их как **tap**: репозиторий GitHub, который другие пользователи Hermes добавляют с помощью `hermes skills tap add <owner/repo>`. Нет сервера, нет регистрации в реестре, нет конвейера выпуска. Просто каталог с файлами `SKILL.md`.

#### Макет репозитория

Tap — это любой репозиторий GitHub (публичный или частный — для частных нужд `GITHUB_TOKEN`), оформленный следующим образом:

```
owner/repo
├── skills/                       # default path; configurable per-tap
│   ├── my-workflow/
│   │   ├── SKILL.md              # required
│   │   ├── references/           # optional supporting files
│   │   ├── templates/
│   │   └── scripts/
│   ├── another-skill/
│   │   └── SKILL.md
│   └── third-skill/
│       └── SKILL.md
└── README.md                     # optional but helpful
```

Правила:
- Каждый навык находится в своем собственном каталоге в корневом пути крана (по умолчанию `skills/`).
- Имя каталога становится ярлыком установки навыка.
- Каждый каталог навыков должен содержать `SKILL.md` со стандартным [SKILL.md frontmatter](#skillmd-format) (`name`, `description` плюс необязательные `metadata.hermes.tags`, `version`, `author`, `platforms`, `metadata.hermes.config`).
– Такие подкаталоги, как `references/`, `templates/`, `scripts/`, `assets/`, загружаются вместе с `SKILL.md` во время установки.
- Навыки, имя каталога которых начинается с `.` или `_`, игнорируются.

Гермес обнаруживает навыки, перечисляя все подкаталоги пути касания и проверяя каждый на наличие `SKILL.md`.

#### Пример минимального касания

```
my-org/hermes-skills
└── skills/
    └── deploy-runbook/
        └── SKILL.md
```

`skills/deploy-runbook/SKILL.md`:

```markdown
---
name: deploy-runbook
description: Our deployment runbook — services, rollback, Slack channels
version: 1.0.0
author: My Org Platform Team
metadata:
  hermes:
    tags: [deployment, runbook, internal]
---

# Deploy Runbook

Step 1: ...
```

После публикации этого на GitHub любой пользователь Hermes сможет подписаться и установить:

```bash
hermes skills tap add my-org/hermes-skills
hermes skills search deploy
hermes skills install my-org/hermes-skills/deploy-runbook
```

#### Пути не по умолчанию

Если ваши навыки не указаны в `skills/` (это часто случается при добавлении поддерева `skills/` в существующий проект), отредактируйте запись касания в `~/.hermes/skills/.hub/taps.json`:

```json
{
  "taps": [
    {"repo": "my-org/platform-docs", "path": "internal/skills/"}
  ]
}
```

Интерфейс командной строки `hermes skills tap add` по умолчанию назначает новым касаниям `path: "skills/"`; отредактируйте файл напрямую, если вам нужен другой путь. `hermes skills tap list` показывает эффективный путь для каждого касания.

#### Установка отдельных навыков напрямую (без добавления тапа)

Пользователи также могут установить один навык из любого общедоступного репозитория GitHub, не добавляя весь репозиторий одним нажатием:

```bash
hermes skills install owner/repo/skills/my-workflow
```

Полезно, когда вы хотите поделиться одним навыком, не прося пользователя подписаться на весь ваш реестр.

#### Уровни доверия для кранов

Новым кранам по умолчанию присваивается уровень доверия `community`. Установленные с их помощью навыки проходят стандартное сканирование безопасности и при первой установке отображают стороннюю панель предупреждений. Если ваша организация или источник, пользующийся широким доверием, должен получить более высокий уровень доверия, добавьте его репозиторий в `TRUSTED_REPOS` в `tools/skills_hub.py` (требуется основной PR Hermes).

#### Управление касанием

```bash
hermes skills tap list                                # show all configured taps
hermes skills tap add myorg/skills-repo               # add (default path: skills/)
hermes skills tap remove myorg/skills-repo            # remove
```

Внутри работающего сеанса:

```
/skills tap list
/skills tap add myorg/skills-repo
/skills tap remove myorg/skills-repo
```

Отводы хранятся в `~/.hermes/skills/.hub/taps.json` (создаются по требованию).

## Пакетные обновления навыков (`hermes skills reset`)

Гермес поставляется с набором встроенных навыков в `skills/` внутри репозитория. При установке и на каждом `hermes update` проход синхронизации копирует их в `~/.hermes/skills/` и записывает манифест в `~/.hermes/skills/.bundled_manifest`, сопоставляя каждое имя навыка с хэшем контента на момент синхронизации (**исходный хеш**).

При каждой синхронизации Hermes пересчитывает хэш вашей локальной копии и сравнивает его с исходным хешем:

- **Без изменений** → безопасно извлекать изменения из исходной версии, копировать новую связанную версию, записывать новый хэш источника.
- **Изменено** → рассматривается как **измененное пользователем** и пропускается навсегда, поэтому ваши изменения никогда не будут отменены.

Защита хорошая, но есть одна острая кромка. Если вы отредактируете связанный навык, а затем позже захотите отказаться от своих изменений и вернуться к связанной версии, просто скопировав из `~/.hermes/hermes-agent/skills/`, манифест по-прежнему будет содержать *старый* хэш источника, начиная с момента последней успешной синхронизации. Ваше свежее содержимое копирования и вставки (текущий связанный хэш) не будет соответствовать устаревшему исходному хэшу, поэтому синхронизация продолжает помечать его как измененное пользователем.

`hermes skills reset` — аварийный люк:

```bash
# Safe: clears the manifest entry for this skill. Your current copy is preserved,
# but the next sync re-baselines against it so future updates work normally.
hermes skills reset google-workspace

# Full restore: also deletes your local copy and re-copies the current bundled
# version. Use this when you want the pristine upstream skill back.
hermes skills reset google-workspace --restore

# Non-interactive (e.g. in scripts or TUI mode) — skip the --restore confirmation.
hermes skills reset google-workspace --restore --yes
```

Эта же команда работает в чате как косая черта:

```text
/skills reset google-workspace
/skills reset google-workspace --restore
```

:::Примечание Профили
Каждый профиль имеет свой собственный `.bundled_manifest` под собственным `HERMES_HOME`, поэтому `hermes -p coder skills reset <name>` влияет только на этот профиль.
:::

### Слэш-команды (внутри чата)

С `/skills` работают все те же команды:

```text
/skills browse
/skills search react --source skills-sh
/skills search https://mintlify.com/docs --source well-known
/skills inspect skills-sh/vercel-labs/json-render/json-render-react
/skills install openai/skills/skill-creator --force
/skills check
/skills update
/skills reset google-workspace
/skills list
```

Официальные дополнительные навыки по-прежнему используют такие идентификаторы, как `official/security/1password` и `official/migration/openclaw-migration`.