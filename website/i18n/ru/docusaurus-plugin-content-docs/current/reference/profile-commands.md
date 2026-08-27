---
sidebar_position: 7
---

# Справочник по командам профиля

На этой странице описаны все команды, относящиеся к [профилям Hermes](../user-guide/profiles.md). Общие сведения о командах CLI см. в [Справочнике команд CLI](./cli-commands.md).

## `hermes profile`

```bash
hermes profile <subcommand>
```

Команда верхнего уровня для управления профилями. Запуск `hermes profile` без подкоманды показывает помощь.

| Подкоманда | Описание |
|------------|-------------|
| `list` | Перечислите все профили. |
| `use` | Установите активный профиль (по умолчанию). |
| `create` | Создайте новый профиль. |
| `describe` | Прочтите или задайте описание профиля (используется оркестратором канбана для маршрутизации). |
| `delete` | Удалить профиль. |
| `show` | Показать подробную информацию о профиле. |
| `alias` | Восстановите псевдоним оболочки для профиля. |
| `rename` | Переименуйте профиль. |
| `export` | Экспортируйте профиль в архив tar.gz. |
| `import` | Импортируйте профиль из архива tar.gz. |
| `install` | Установите дистрибутив профиля из URL-адреса git или локального каталога. См. [Распространение профиля](../user-guide/profile-distributions.md). |
| `update` | Повторно извлеките профиль, управляемый распространением, и повторно примените его пакет. |
| `info` | Показать метаданные распространения для профиля (исходный URL, фиксация, последнее обновление). |

## `hermes profile list`

```bash
hermes profile list
```

Перечисляет все профили. Активный в данный момент профиль отмечен `*`.

**Пример:**

```bash
$ hermes profile list
  default
* work
  dev
  personal
```

Никаких вариантов.

## `hermes profile use`

```bash
hermes profile use <name>
```

Устанавливает `<name>` в качестве активного профиля. Все последующие команды `hermes` (без `-p`) будут использовать этот профиль.

| Аргумент | Описание |
|----------|-------------|
| `<name>` | Имя профиля для активации. Используйте `default`, чтобы вернуться к базовому профилю. |

**Пример:**

```bash
hermes profile use work
hermes profile use default
```

## `hermes profile create`

```bash
hermes profile create <name> [options]
```

Создает новый профиль.

| Аргумент/Опция | Описание |
|-------------------|-------------|
| `<name>` | Имя нового профиля. Должно быть допустимое имя каталога (буквенно-цифровое, дефисы, подчеркивания). |
| `--clone` | Скопируйте `config.yaml`, `.env`, `SOUL.md` и навыки из текущего профиля. |
| `--clone-all` | Скопируйте все (конфигурацию, воспоминания, навыки, cron, плагины) из текущего профиля. Исключает историю каждого профиля: сеансы, `state.db`, резервные копии, снимки состояния, контрольные точки. |
| `--clone-from <profile>` | Клонировать конфиг/навыки/SOUL из определенного профиля вместо текущего. Подразумевается `--clone`, если он не соединен с `--clone-all`. |
| `--no-alias` | Пропустить создание скрипта-обертки. |
| `--description "<text>"` | Описание в одном или двух предложениях того, в чем хорош этот профиль. Используется оркестратором канбана для маршрутизации задач на основе роли, а не только имени профиля. Пропустите и добавьте позже через `hermes profile describe`. Сохранился в `<profile_dir>/profile.yaml`. |
| `--no-skills` | Создайте **пустой** профиль с нулевым включенным набором навыков. Записывает маркер `.no-bundled-skills` в профиль, чтобы будущие запуски `hermes update` не приводили к повторному заполнению связанного набора, и отказывается комбинироваться с `--clone`, `--clone-from` или `--clone-all` (которые в любом случае будут копировать навыки). Полезно для узких профилей оркестратора или профилей песочницы, которые не должны наследовать полный каталог навыков. Чтобы переключить это на уже созданный профиль (включая профиль по умолчанию `~/.hermes`), используйте `hermes skills opt-out` / `hermes skills opt-in`. |

Создание профиля **не** не делает этот каталог профиля каталогом проекта/рабочей области по умолчанию для команд терминала. Если вы хотите, чтобы профиль запускался в определенном проекте, установите `terminal.cwd` в `config.yaml` этого профиля.

**Примеры:**

```bash
# Blank profile — needs full setup
hermes profile create mybot

# Clone config only from current profile
hermes profile create work --clone

# Clone everything from current profile
hermes profile create backup --clone-all

# Clone config from a specific profile
hermes profile create work2 --clone-from work

# Clone everything from a specific profile
hermes profile create work2-backup --clone-from work --clone-all
```

## `hermes profile describe`

```bash
hermes profile describe [<name>] [options]
```

Прочтите или установите описание профиля. Описание используется оркестратором канбана для маршрутизации задач на основе того, в чем хорош каждый профиль, а не только по имени профиля. Сохраняется в `<profile_dir>/profile.yaml`, поэтому выдерживает перезагрузки и используется совместно со шлюзом.

Без флагов печатает текущее описание (или `(no description set for '<name>')`, если оно пусто).

| Аргумент/Опция | Описание |
|-------------------|-------------|
| `<name>` | Профиль для описания. Требуется, если не используется `--all --auto`. |
| `--text "<text>"` | Установите описание именно на этот текст (авторский). Перезаписывает любое существующее описание. |
| `--auto` | Автоматически сгенерируйте описание из 1–2 предложений с помощью вспомогательного LLM на основе установленных навыков профиля, настроенной модели и имени. Настройте модель под `auxiliary.profile_describer` в `config.yaml`. Автоматически созданные описания помечаются `description_auto: true`, чтобы панель мониторинга могла пометить их для проверки. |
| `--overwrite` | С помощью `--auto` также заменяйте описания, созданные пользователем (по умолчанию: пропускать профили, описание которых было задано явно). |
| `--all` | С помощью `--auto` просмотрите все профили, у которых нет описания. |

**Примеры:**

```bash
# Read the current description
hermes profile describe researcher

# Set it explicitly
hermes profile describe researcher --text "Reads source code and writes findings."

# Let the LLM generate one
hermes profile describe researcher --auto

# Fill in descriptions for every profile that doesn't have one
hermes profile describe --all --auto
```

## `hermes profile delete`

```bash
hermes profile delete <name> [options]
```

Удаляет профиль и удаляет его псевдоним оболочки.

| Аргумент/Опция | Описание |
|-------------------|-------------|
| `<name>` | Профиль, который нужно удалить. |
| `--yes`, `-y` | Пропустить запрос подтверждения. |

**Пример:**

```bash
hermes profile delete mybot
hermes profile delete mybot --yes
```

:::предупреждение
При этом будет безвозвратно удален весь каталог профиля, включая все настройки, воспоминания, сеансы и навыки. Профиль `default` (`~/.hermes`) невозможно удалить — используйте `hermes uninstall`, чтобы удалить все.
:::

## `hermes profile show`

```bash
hermes profile show <name>
```

Отображает сведения о профиле, включая его домашний каталог, настроенную модель, состояние шлюза, количество навыков и состояние файла конфигурации.

Это показывает домашний каталог Hermes профиля, а не рабочий каталог терминала. Команды терминала начинаются с `terminal.cwd` (или каталога запуска на локальном сервере, если `cwd: "."`).

| Аргумент | Описание |
|----------|-------------|
| `<name>` | Профиль для проверки. |

**Пример:**

```bash
$ hermes profile show work
Profile: work
Path:    ~/.hermes/profiles/work
Model:   anthropic/claude-sonnet-4 (anthropic)
Gateway: stopped
Skills:  12
.env:    exists
SOUL.md: exists
Alias:   ~/.local/bin/work
```

## `hermes profile alias`

```bash
hermes profile alias <name> [options]
```

Восстанавливает сценарий псевдонима оболочки по адресу `~/.local/bin/<name>`. Полезно, если псевдоним был случайно удален или вам нужно обновить его после перемещения установки Hermes.

| Аргумент/Опция | Описание |
|-------------------|-------------|
| `<name>` | Профиль, для которого нужно создать/обновить псевдоним. |
| `--remove` | Удалите сценарий-оболочку вместо того, чтобы создавать его. |
| `--name <alias>` | Пользовательское имя псевдонима (по умолчанию: имя профиля). |

**Пример:**

```bash
hermes profile alias work
# Creates/updates ~/.local/bin/work

hermes profile alias work --name mywork
# Creates ~/.local/bin/mywork

hermes profile alias work --remove
# Removes the wrapper script
```

## `hermes profile rename`

```bash
hermes profile rename <old-name> <new-name>
```

Переименовывает профиль. Обновляет каталог и псевдоним оболочки.

| Аргумент | Описание |
|----------|-------------|
| `<old-name>` | Текущее имя профиля. |
| `<new-name>` | Новое имя профиля. |

**Пример:**

```bash
hermes profile rename mybot assistant
# ~/.hermes/profiles/mybot → ~/.hermes/profiles/assistant
# ~/.local/bin/mybot → ~/.local/bin/assistant
```

## `hermes profile export`

```bash
hermes profile export <name> [options]
```

Экспортирует профиль в виде сжатого архива tar.gz — портативного снимка, резервную копию которого можно создать, перенести на другой компьютер или передать кому-то другому. `auth.json` и `.env` всегда исключаются.

Также доступно в чате как [`/export`](./slash-commands.md) и в настольном приложении через **⌘K → Экспорт профиля…** или контекстное меню квадрата профиля. Экспорт рабочего стола дополнительно помещает в архив `desktop.json` (оформление, светлый/темный режим, пользовательские темы, цвет направляющих, расположение окон).

| Аргумент/Опция | Описание |
|-------------------|-------------|
| `<name>` | Профиль для экспорта. |
| `-o`, `--output <path>` | Путь к выходному файлу (по умолчанию: `<name>.tar.gz`). |

**Пример:**

```bash
hermes profile export work
# Creates work.tar.gz in the current directory

hermes profile export work -o ./work-2026-03-29.tar.gz
```

См. [Экспорт и импорт файла профиля](../user-guide/profile-distributions.md#export-and-import-a-profile-file), чтобы узнать, что именно попадает в архив и что нужно проверить перед отправкой его кому-то другому.

## `hermes profile import`

```bash
hermes profile import <archive> [options]
```

Импортирует профиль из архива tar.gz как новый профиль. Отказывается перезаписывать существующий профиль и не может импортировать как `default` (встроенный корневой профиль) — в любом случае передайте `--name`. Оболочка-оболочка создается, когда имя не конфликтует с существующей командой.

Также доступен в чате как [`/import`](./slash-commands.md) и в настольном приложении через **⌘K → Импортировать профиль…** или кнопку импорта рядом с **+** на направляющей профиля. Импорт рабочего стола также применяет любое связанное наложение `desktop.json` (тема, макет) и переключает вас в новый профиль.

| Аргумент/Опция | Описание |
|-------------------|-------------|
| `<archive>` | Путь к архиву tar.gz для импорта. |
| `--name <name>` | Имя импортируемого профиля (по умолчанию: получено из архива). |

**Пример:**

```bash
hermes profile import ./work-2026-03-29.tar.gz
# Infers profile name from the archive

hermes profile import ./work-2026-03-29.tar.gz --name work-restored
```

## Команды распространения

:::совет
**Новичок в дистрибутивах?** Начните с [Руководства пользователя по профильным дистрибутивам](../user-guide/profile-distributions.md) — в нем подробно рассказывается, почему, когда и как, с полными примерами. Разделы ниже представляют собой сухой справочник по CLI, если вы знаете, чего хотите.
:::

Распространения превращают профиль в опубликованный артефакт с общими версиями.
как **git-репозиторий**. Получатель устанавливает дистрибутив с помощью одного
команду и может обновить ее позже, не затрагивая локальную
воспоминания, сеансы или учетные данные.

`auth.json` и `.env` никогда не являются частью дистрибутива — они остаются в
установка машины пользователя.

Пользовательские данные получателя (воспоминания, сеансы, авторизация, собственные правки в
`.env`) всегда сохраняется при первоначальной установке и последующих
обновления.

:::информация
Два способа поделиться профилем, и они дополняют друг друга. `hermes profile export` / `import` (также `/export` и `/import` в чате) создают **один файл** — без репозитория, без манифеста, а при экспорте на рабочий стол также сохраняется ваша тема и макет. Распространение (`install` / `update` / `info`) публикует профиль как **git-репозиторий**, чтобы получатели могли позже получить версии обновлений. Резервное копирование и восстановление — это еще одна задача экспортируемого файла. См. [Два способа поделиться профилем](../user-guide/profile-distributions.md#two-ways-to-share-a-profile).
:::

### `hermes profile install`

```bash
hermes profile install <source> [--name <name>] [--alias] [--force] [--yes]
```

Устанавливает дистрибутив профиля из URL-адреса git или локального каталога.

| Вариант | Описание |
|--------|-------------|
| `<source>` | URL-адрес Git (`github.com/user/repo`, `https://...`, `git@...`, `ssh://`, `git://`) или локальный каталог, содержащий `distribution.yaml` в корне. |
| `--name NAME` | Переопределить имя профиля из манифеста. |
| `--alias` | Также создайте оболочку-оболочку (например, `telemetry` → `hermes -p telemetry`). |
| `--force` | Перезапишите существующий профиль с таким же именем. Данные пользователя по-прежнему сохраняются. |
| `-y`, `--yes` | Пропустите запрос подтверждения предварительного просмотра манифеста. |

Установщик показывает манифест, перечисляет необходимые переменные окружения и предупреждает о
cron, прежде чем запрашивать подтверждение. Обязательные переменные окружения входят в
`.env.EXAMPLE` файл, который вы копируете в `.env` и заполняете.

**Примеры:**

```bash
# Install from a GitHub repo (shorthand)
hermes profile install github.com/kyle/telemetry-distribution --alias

# Install from a full HTTPS git URL
hermes profile install https://github.com/kyle/telemetry-distribution.git

# Install from SSH
hermes profile install git@github.com:kyle/telemetry-distribution.git

# Install from a local directory during development
hermes profile install ./telemetry/
```

### `hermes profile update`

```bash
hermes profile update <name> [--force-config] [--yes]
```

Повторно клонирует дистрибутив из записанного источника и применяет обновления.
Файлы, принадлежащие дистрибутиву (SOUL.md,kills/, cron/, mcp.json),
перезаписано; пользовательские данные (память, сеансы, аутентификация, .env) никогда не затрагиваются.

`config.yaml` сохраняется по умолчанию, чтобы сохранить ваши локальные переопределения.
Передайте `--force-config`, чтобы сбросить его до конфигурации, поставляемой в дистрибутиве.

### `hermes profile info`

```bash
hermes profile info <name>
```

Печатает манифест распространения профиля — имя, версия, обязательно.
Версия Hermes, автор, требования к переменной окружения, исходный URL/путь и
временная метка `Installed:`, записанная при последней раздаче
`install`-ред. или `update`-д. Полезно для проверки общего профиля.
необходимо перед его установкой и для обнаружения «данный профиль был установлен
6 месяцев назад и не обновлялось».

`hermes profile list` также показывает имя и версию дистрибутива в
столбец `Distribution` и `hermes profile show <name>`/`delete <name>`
отображать исходный URL-адрес, чтобы вы могли сразу определить, какие профили пришли
из репозитория git или были созданы локально.

### Частные раздачи

Частный репозиторий git работает как источник распространения без каких-либо дополнительных действий.
конфигурация — установочные оболочки выполняются в обычном двоичном файле `git`, поэтому
какая бы аутентификация ни была настроена в вашей оболочке (ключ SSH,
`git credential` helper, применяются сохраненные учетные данные HTTPS в GitHub CLI).
прозрачно.

```bash
# Uses your SSH key, the same as any other `git clone`
hermes profile install git@github.com:your-org/internal-assistant.git

# Uses your git credential helper
hermes profile install https://github.com/your-org/internal-assistant.git
```

Если клон в интерактивном режиме запрашивает учетные данные на вашем терминале во время
install, это приглашение проходит. Настройте свою авторизацию так, как вы
обычно сначала используйте `git clone` для того же репозитория, а затем устанавливайте.

### Манифест распространения (`distribution.yaml`)

Каждый дистрибутив имеет `distribution.yaml` в корне репозитория:

```yaml
name: telemetry
version: 0.1.0
description: "Compliance monitoring harness"
hermes_requires: ">=0.12.0"
author: "Your Name"
license: "MIT"
env_requires:
  - name: OPENAI_API_KEY
    description: "OpenAI API key"
    required: true
  - name: GRAPHITI_MCP_URL
    description: "Memory graph URL"
    required: false
    default: "http://127.0.0.1:8000/sse"
distribution_owned:   # optional; defaults to SOUL.md, config.yaml,
                      #   mcp.json, skills/, cron/, distribution.yaml
  - SOUL.md
  - skills/compliance/
  - cron/
```

`hermes_requires` поддерживает `>=`, `<=`, `==`, `!=`, `>`, `<` или пустой
версия (трактуется как `>=`). Установка завершается с явной ошибкой, если текущий
Версия Hermes не соответствует спецификации.

`distribution_owned` не является обязательным. Если установлено, заменяются только эти пути.
обновление; все остальное в профиле остается собственностью пользователя. Если опущено,
применяются значения по умолчанию, указанные выше.

### Публикация дистрибутива

Создание дистрибутива — это просто нажатие git:

1. В каталоге вашего профиля создайте `distribution.yaml` как минимум с `name`.
   и `version`.
2. Инициализируйте репозиторий git (или используйте существующий) и отправьте его на GitHub/
   GitLab/любой хост, с которого Hermes может клонировать.
3. Попросите получателей запустить `hermes profile install <your-repo-url>`.

Используйте теги git для версий релизов — получатели, клонирующие `HEAD`, получат ваш
последнее состояние, и вы всегда можете добавить `version:` в манифест.

## `hermes -p` / `hermes --profile`

```bash
hermes -p <name> <command> [options]
hermes --profile <name> <command> [options]
```

Глобальный флаг для запуска любой команды Hermes в определенном профиле без изменения закрепленного значения по умолчанию. Это отменяет активный профиль на время действия команды.

| Вариант | Описание |
|--------|-------------|
| `-p <name>`, `--profile <name>` | Профиль, используемый для этой команды. |

**Примеры:**

```bash
hermes -p work chat -q "Check the server status"
hermes --profile dev gateway start
hermes -p personal skills list
hermes -p work config edit
```

## `hermes completion`

```bash
hermes completion <shell>
```

Генерирует сценарии завершения оболочки. Включает дополнения для имен профилей и подкоманд профиля.

| Аргумент | Описание |
|----------|-------------|
| `<shell>` | Оболочка для создания дополнений для: `bash`, `zsh` или `fish`. |

**Примеры:**

```bash
# Install completions
hermes completion bash >> ~/.bashrc
hermes completion zsh >> ~/.zshrc
hermes completion fish > ~/.config/fish/completions/hermes.fish

# Reload shell
source ~/.bashrc
```

После установки завершение табуляции работает для:
- `hermes profile <TAB>` — подкоманды (список, использование, создание и т.д.)
- `hermes profile use <TAB>` — имена профилей
- `hermes -p <TAB>` — имена профилей

## См. также

- [Руководство пользователя профилей](../user-guide/profiles.md)
- [Справочник команд CLI](./cli-commands.md)
- [FAQ — Раздел Профили](./faq.md#profiles)