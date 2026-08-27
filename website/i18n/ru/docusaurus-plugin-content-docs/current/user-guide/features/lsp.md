---
sidebar_position: 16
title: ЛСП — Семантическая диагностика
description: Реальные языковые серверы (pyright, gopls, Rust-analyzer и т. д.), подключенные
  к проверке после записи, используемой write_file и patch.
---

# Протокол языкового сервера (LSP)

У Гермеса есть полноценные языковые серверы — пирайт, гоплс, ржавчина-анализатор,
typescript-language-server, clangd и еще около 20 — в качестве фона
подпроцессы и передает их семантическую диагностику в пост-запись
проверка ворса, используемая `write_file` и `patch`. Когда агент редактирует
файл, он видит именно те ошибки, которые были внесены редактированием, а не только
синтаксические ошибки, но **ошибки типа, неопределенные имена, отсутствующий импорт,
и семантические проблемы в масштабе проекта**, которые обнаруживает языковой сервер.

Это та же самая архитектура, которую используют агенты кодирования высшего уровня. Гермес
поставляется самодостаточным: не требуется хост-редактор, не требуются плагины для
установить, нет отдельного демона для управления.

## Когда LSP запускается

LSP привязан к **обнаружению рабочей области git**. Когда агент работает
каталог (или редактируемый файл) находится внутри репозитория git, LSP
работает против этого рабочего пространства. Если ни того, ни другого нет в репозитории git, LSP
остается бездействующим — полезно для шлюзов обмена сообщениями, где cwd является
домашний каталог пользователя, и нет проекта для диагностики.

Проверка многоуровневая: сначала проверяется синтаксис в процессе (микросекунды),
затем вторая диагностика LSP, когда синтаксис чист. Шелушащийся или отсутствующий
языковой сервер никогда не сможет прервать запись — каждый путь отказа LSP
молча возвращается к результату, имеющему только синтаксис.

Конкретно, при каждом успешном `write_file` или `patch`:

1. Hermes фиксирует базовую информацию о текущей диагностике файла.
2. Выполняет запись.
3. Повторно запрашивает языковой сервер, отфильтровывает диагностику, которая была
   уже в базовой версии, и появляются только новые.

Агент видит вывод вроде:

```
{
  "bytes_written": 42,
  "dirs_created": false,
  "lint": {"status": "ok", "output": ""},
  "lsp_diagnostics": "LSP diagnostics introduced by this edit:\n<diagnostics file=\"/path/to/foo.py\">\nERROR [42:5] Cannot find name 'foo' [reportUndefinedVariable] (Pyright)\nERROR [50:1] Argument of type \"str\" is not assignable to \"int\" [reportArgumentType] (Pyright)\n</diagnostics>"
}
```

Поле `lint` содержит результат проверки синтаксиса (микросекунда).
внутрипроцессный анализ через `ast.parse`, `json.loads` и т. д.); тот
Поле `lsp_diagnostics` несет семантическую диагностику из
настоящий языковой сервер. Два канала, независимые сигналы —
агент видит синтаксически чистый файл с семантическими проблемами как
``lint: ok`` plus a populated ``lsp_diagnostics``.

## Поддерживаемые языки

| Язык | Сервер | Автоматическая установка |
|----------|--------|--------------|
| Питон | `pyright-langserver` | НПМ |
| TypeScript/JavaScript/JSX/TSX | `typescript-language-server` | НПМ |
| Вуэ | `@vue/language-server` | НПМ |
| Стройная | `svelte-language-server` | НПМ |
| Астро | `@astrojs/language-server` | НПМ |
| Перейти | `gopls` | `go install` |
| Ржавчина | `rust-analyzer` | руководство (ржавчина) |
| Си/С++ | `clangd` | руководство (LLVM) |
| Баш/Зш | `bash-language-server` | НПМ |
| ЯМЛ | `yaml-language-server` | НПМ |
| Луа | `lua-language-server` | руководство (выпуски GitHub) |
| PHP | `intelephense` | НПМ |
| OCaml | `ocaml-lsp` | руководство (опам) |
| Докерфайл | `dockerfile-language-server-nodejs` | НПМ |
| Терраформировать | `terraform-ls` | руководство |
| Дарт | `dart language-server` | руководство (дротик SDK) |
| Хаскелл | `haskell-language-server` | руководство (ghcup) |
| Юлия | `julia` + LanguageServer.jl | руководство |
| Кложур | `clojure-lsp` | руководство |
| Никс | `nixd` | руководство |
| Зиг | `zls` | руководство |
| Блеск | `gleam lsp` | инструкция (блеск установки) |
| Эликсир | `elixir-ls` | руководство |
| Призма | `prisma language-server` | руководство |
| Котлин | `kotlin-language-server` | руководство |
| Ява | `jdtls` | руководство |
| PowerShell | `PowerShellEditorServices` (хост `pwsh`) | руководство (расстегните почтовый индекс) |

Для «ручных» записей установите сервер с помощью любой цепочки инструментов.
менеджер имеет смысл для этого языка (rustup, ghcup, opam, Brew,
…). Hermes автоматически обнаруживает двоичный файл в PATH или в
`<HERMES_HOME>/lsp/bin/`.

### PowerShell

PowerShellEditorServices — это не один двоичный файл, а PowerShell.
пакет модулей, запускаемый с помощью `pwsh` (PowerShell 7+) или `powershell`
хозяин. Настраивать:

1. Установите [PowerShell](https://github.com/PowerShell/PowerShell), чтобы
   `pwsh` (или Windows `powershell`) находится в PATH.
2. Загрузите последнюю версию zip-файла с
   [Выпуски PowerShellEditorServices](https://github.com/PowerShell/PowerShellEditorServices/releases)
   и извлеките его.
3. Наведите Hermes на извлеченный пакет — каталог, содержащий
   `PowerShellEditorServices/Start-EditorServices.ps1`. Либо:
   - установите `lsp.servers.powershell.command: ["/path/to/bundle"]` в
     `config.yaml` или
   - распакуйте его в `<HERMES_HOME>/lsp/PowerShellEditorServices`, или
   - экспортировать `PSES_BUNDLE_PATH=/path/to/bundle`.

`hermes lsp status` сообщает `installed`, как только `pwsh` найден; если
отсутствует, вы увидите в журналах одноразовое предупреждение с надписью
ссылка для скачивания.

Несколько серверов установлены вместе с одноранговой зависимостью, которую npm
не будет автоматически тянуться. Текущий случай — `typescript-language-server`,
для которого требуется `typescript` SDK, импортируемый из того же
`node_modules` дерево — Hermes устанавливает оба пакета вместе, когда вы
запустите `hermes lsp install typescript` или сначала запустится автоматическая установка
использовать.

## интерфейс командной строки

```
hermes lsp status          # service state + per-server install status
hermes lsp list            # registry, optionally --installed-only
hermes lsp install <id>    # eagerly install one server
hermes lsp install-all     # try every server with a known recipe
hermes lsp restart         # tear down running clients
hermes lsp which <id>      # print resolved binary path
```

`hermes lsp status` — лучшая отправная точка: он показывает, какие
языки сегодня получат семантическую диагностику и нуждаются в
установлен бинарный файл.

## Конфигурация

Значения по умолчанию подходят для типичных настроек; нечего устанавливать, если двоичные файлы
находятся в PATH.

```yaml
# config.yaml
lsp:
  # Master toggle. Disabling skips the entire subsystem — no servers
  # spawn, no background event loop runs.
  enabled: true

  # How long to wait for diagnostics after each write.
  wait_mode: document      # "document" or "full"
  # Max seconds to wait for the server to re-check the file after an
  # edit. Only *fresh* diagnostics (produced for the post-edit
  # content) are ever reported; if the server doesn't finish within
  # this budget, the edit reports "no LSP data" rather than stale
  # errors from before the edit. Raise this for slow servers on big
  # projects (tsserver, rust-analyzer mid-indexing).
  wait_timeout: 5.0

  # How to handle missing server binaries.
  #   auto    — install via npm/pip/go install into <HERMES_HOME>/lsp/bin
  #   manual  — only use binaries already on PATH
  install_strategy: auto

  # How long an unused language-server client stays alive (seconds).
  # Idle servers are shut down automatically and respawned on the next
  # relevant file operation. Set to 0 to disable idle reaping and keep
  # servers alive for the life of the process. Values below 30s are
  # clamped to 30 so a sweep can never reap a client mid-operation.
  idle_timeout: 600

  # Per-server overrides (all optional).
  servers:
    pyright:
      disabled: false
      command: ["/abs/path/to/pyright-langserver", "--stdio"]
      env: { PYRIGHT_LOG_LEVEL: "info" }
      initialization_options:
        python:
          analysis:
            typeCheckingMode: "strict"
    typescript:
      disabled: true       # skip TS even when its extensions match
```

### Ключи для каждого сервера

* `disabled: true` — полностью пропустить этот сервер, даже если он
  расширения соответствуют файлу.
* `command: [bin, ...args]` — закрепите собственный двоичный путь. Обходит
  автоматическая установка.
* `env: {KEY: value}` — в порожденный процесс передаются дополнительные переменные окружения.
* `initialization_options: {...}` — объединено в ЛСП
  Полезная нагрузка `initializationOptions` отправлена в `initialize`
  рукопожатие. специфичный для сервера; обратитесь к документации языкового сервера.

## Места установки

Когда `install_strategy: auto`, Hermes устанавливает двоичные файлы в
`<HERMES_HOME>/lsp/bin/`. Пакеты NPM попадают в
`<HERMES_HOME>/lsp/node_modules/` с символическими ссылками bin на один уровень выше.
Бинарные файлы Go берутся из `go install`, где `GOBIN` указывает на
постановка реж.

Ничего не устанавливается на `/usr/local/`, `~/.local/` или любой другой
общее местоположение — промежуточный каталог полностью принадлежит компании Hermes и
удаляется при сбросе профиля.

## Эксплуатационные характеристики

Серверы LSP создаются **лениво** при первом использовании. Редактирование файла Python
в проекте, который никогда не видел, трафик `.py` порождает пирайт; тот
Для большинства серверов спавн занимает 1-3 секунды (анализатор ржавчины может занять 10+ секунд).
на холодном проекте). Последующие изменения в том же рабочем пространстве повторно используются.
работающий сервер.

Уровень LSP добавляет несколько миллисекунд для очистки записи, когда ее нет.
выдается диагностика. Когда выдается диагностическая информация, ожидание
бюджет составляет `wait_timeout` секунд — обычно сервер отвечает через
десятки миллисекунд дляpyright/tsserver и несколько секунд для
ржавчина-анализатор средней индексации.

Диагностика **ограничена по обновлению**: результат засчитывается только в том случае, если
сервер создал его для содержимого текущего редактирования (
`publishDiagnostics` push-уведомление после изменения или запрос на включение
ответил после этого). Медленные серверы, еще не перепроверенные результат
в «нет данных» для этого редактирования — никогда во вчерашних ошибках
повторно зарегистрировано как текущее.

Серверы поддерживаются в рабочем состоянии, пока они используются, и отключаются после
`lsp.idle_timeout` секунд (по умолчанию 600) без активности файлов —
длительный шлюз, затрагивающий множество рабочих деревьев, больше не накапливается
один процесс языкового сервера на каждую рабочую область навсегда. Пожатый сервер
автоматически возрождается при следующей соответствующей операции с файлом. Установить
`idle_timeout: 0`, чтобы отключить сбор данных и поддерживать индекс каждого сервера в тепле
для жизни процесса.

## Отключение

Установите `lsp.enabled: false` в `config.yaml`, чтобы отключить всю
подсистема. Проверка после записи возвращается к внутрипроцессному синтаксису.
проверьте (`ast.parse` для Python, `json.loads` для JSON и т. д.), который
поставляется без изменений по сравнению с более ранними версиями.

Чтобы отключить один язык, не отключая весь слой:

```yaml
lsp:
  servers:
    rust-analyzer:
      disabled: true
```

## Устранение неполадок

**`hermes lsp status` показывает, что сервер «отсутствует»**

Бинарный файл не находится в PATH и не в `<HERMES_HOME>/lsp/bin/`. Беги
`hermes lsp install <server_id>`, чтобы попытаться выполнить автоматическую установку, или
установите двоичный файл вручную с помощью обычной цепочки инструментов языка.

Раздел **`Backend warnings` в `hermes lsp status`**

Некоторые серверы поставляются в виде тонкой оболочки вокруг внешнего интерфейса командной строки.
диагностика — они спавнятся чисто и принимают запросы, но никогда не испускают
ошибки, когда двоичный файл сопроводительного кода отсутствует. Наиболее распространенным случаем является
`bash-language-server`, который делегирует диагностику `shellcheck`.
Когда `hermes lsp status` отобразит раздел `Backend warnings`, установите
названный инструмент через менеджер пакетов вашей ОС:

```
apt install shellcheck      # Debian / Ubuntu
brew install shellcheck     # macOS
scoop install shellcheck    # Windows
```

Такое же предупреждение регистрируется один раз во время появления сервера в
`~/.hermes/logs/agent.log`.

**Сервер запускается, но не возвращает диагностику**

Проверьте `~/.hermes/logs/agent.log` на наличие `[agent.lsp.client]` записей —
как stderr от языкового сервера, так и ошибки протокола
там. Некоторым серверам (особенно анализатору ржавчины) необходимо завершить
индекс всего проекта, прежде чем они выдадут диагностику для каждого файла; первый
редактирование после запуска сервера может завершиться без диагностики, с
последующие правки подхватывают их.

**Сервер сломался**

Сбойный сервер добавляется в набор сломанных и не будет повторно использоваться для
остальная часть сессии. Запустите `hermes lsp restart`, чтобы очистить набор;
следующее редактирование возобновится.

**Редактирование файла вне любого репозитория git**

По замыслу LSP работает только внутри репозитория git. Если проект не
еще не инициализирован, запустите `git init`, чтобы включить диагностику LSP. В противном случае
Применяется резервный вариант только для внутрипроцессного синтаксиса.