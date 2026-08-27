---
sidebar_position: 6
title: Используйте MCP с Гермесом
description: Практическое руководство по подключению серверов MCP к агенту Hermes,
  фильтрации их инструментов и безопасному их использованию в реальных рабочих процессах.
---

# Используйте MCP с Гермесом

В этом руководстве показано, как на самом деле использовать MCP с агентом Hermes в повседневных рабочих процессах.

Если на странице функций объясняется, что такое MCP, то это руководство посвящено тому, как быстро и безопасно получить от него выгоду.

## Когда следует использовать MCP?

Используйте MCP, когда:
- инструмент уже существует в форме MCP, и вы не хотите создавать собственный инструмент Hermes
- вы хотите, чтобы Hermes работал с локальной или удаленной системой через чистый уровень RPC
- вам нужен детальный контроль воздействия на каждый сервер
- вы хотите подключить Hermes к внутренним API, базам данных или системам компании без изменения ядра Hermes

Не используйте MCP, если:
- встроенный инструмент Hermes уже хорошо решает задачу
- сервер предоставляет огромную поверхность опасных инструментов, и вы не готовы ее фильтровать
- вам нужна только одна очень узкая интеграция, и собственный инструмент будет проще и безопаснее

## Ментальная модель

Думайте о MCP как об уровне адаптера:

- Гермес остается агентом
- Серверы MCP предоставляют инструменты
- Гермес обнаруживает эти инструменты при запуске или во время перезагрузки.
- модель может использовать их как обычные инструменты
- вы контролируете, какая часть каждого сервера видна

Последняя часть имеет значение. Правильное использование MCP — это не просто «соединить все». Это «подключить правильную вещь, с наименьшей полезной поверхностью».

## Шаг 1: установите поддержку MCP

Если вы установили Hermes с помощью стандартного сценария установки, поддержка MCP уже включена (установщик запускает `uv pip install -e ".[all]"`).

Если вы устанавливали без дополнений и вам нужно добавить MCP отдельно:

```bash
cd ~/.hermes/hermes-agent
uv pip install -e ".[mcp]"
```

Для серверов на базе npm убедитесь, что Node.js и `npx` доступны.

Для многих серверов Python MCP `uvx` является хорошим значением по умолчанию.

## Шаг 2: сначала добавьте один сервер

Начните с одного безопасного сервера.

Пример: доступ файловой системы только к одному каталогу проекта.

```yaml
mcp_servers:
  project_fs:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/my-project"]
```

Затем запустите Гермес:

```bash
hermes chat
```

Теперь спросите что-нибудь конкретное:

```text
Inspect this project and summarize the repo layout.
```

## Шаг 3: проверьте, загружен ли MCP

Проверить MCP можно несколькими способами:

- Баннер/статус Hermes должен показывать интеграцию MCP при настройке.
- спросите Гермеса, какие инструменты у него есть в наличии
- используйте `/reload-mcp` после изменения конфигурации
- проверить логи, если серверу не удалось подключиться

Практический тестовый запрос:

```text
Tell me which MCP-backed tools are available right now.
```

## Шаг 4: немедленно начните фильтрацию

Не откладывайте на потом, если сервер предоставляет много инструментов.

### Пример: в белый список входите только то, что вы хотите

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "***"
    tools:
      include: [list_issues, create_issue, search_code]
```

Обычно это лучший вариант по умолчанию для чувствительных систем.

## WSL2: мост Hermes в WSL к Windows Chrome

Это практическая установка, когда:

- Гермес работает внутри WSL2.
- браузер, которым вы хотите управлять, — это обычный Chrome с входом в систему в Windows.
- `/browser connect` неуклюж или ненадежен из WSL

В этой настройке Hermes **не** подключается к Chrome напрямую. Вместо этого:

- Гермес бежит в WSL
- Гермес запускает локальный сервер MCP stdio.
- этот сервер MCP запускается через взаимодействие с Windows (`cmd.exe` или `powershell.exe`)
- Сервер MCP подключается к вашему живому сеансу Windows Chrome.

Ментальная модель:

```text
Hermes (WSL) -> MCP stdio bridge -> Windows Chrome
```

### Почему этот режим полезен

- вы сохраняете свой настоящий профиль браузера Windows, файлы cookie и логины
- Hermes остается в поддерживаемой среде Unix (WSL2).
- управление браузером представлено в виде инструментов MCP вместо использования основного транспорта браузера Hermes.

### Рекомендуемый сервер

Используйте `chrome-devtools-mcp`.

Если в вашем Windows Chrome уже включена удаленная отладка в реальном времени из `chrome://inspect/#remote-debugging`, добавьте ее из WSL следующим образом:

```bash
hermes mcp add chrome-devtools-win --command cmd.exe --args /c npx -y chrome-devtools-mcp@latest --autoConnect --no-usage-statistics
```

После сохранения сервера:

```bash
hermes mcp test chrome-devtools-win
```

Затем запустите новый сеанс Hermes или запустите:

```text
/reload-mcp
```

### Типичная подсказка

После загрузки Hermes может напрямую использовать инструменты браузера с префиксом MCP. Например:

```text
调用 MCP 工具 mcp_chrome_devtools_win_list_pages，列出当前浏览器标签页。
```

### Когда `/browser connect` — неправильный инструмент

Если Hermes работает в WSL, а Chrome — в Windows, `/browser connect` может выйти из строя, даже если Chrome открыт и доступен для отладки.

Распространенные причины:

- WSL не может достичь той же локальной конечной точки хоста, которую Chrome предоставляет инструментам Windows.
- новые потоки оперативной отладки Chrome отличаются от классического `ws://localhost:9222`.
- к браузеру проще подключиться с помощью помощника на стороне Windows, например `chrome-devtools-mcp`.

В таких случаях сохраните `/browser connect` для настроек в той же среде и используйте MCP для моста браузера WSL-Windows.

### Известные подводные камни

- Запускайте Hermes из пути, смонтированного в Windows, например `/mnt/c/Users/<you>` или `/mnt/c/workspace/...`, при использовании исполняемых файлов Windows stdio через MCP.
- Если вы запустите Hermes с `/root` или `/home/...`, Windows может выдать предупреждение о текущем каталоге `UNC` до запуска сервера MCP.
– Если при перечислении страниц истекает время `chrome-devtools-mcp --autoConnect`, уменьшите количество фоновых/замороженных вкладок в Chrome и повторите попытку.

### Пример: опасные действия в черный список

```yaml
mcp_servers:
  stripe:
    url: "https://mcp.stripe.com"
    headers:
      Authorization: "Bearer ***"
    tools:
      exclude: [delete_customer, refund_payment]
```

### Пример: отключить также обертки утилит

```yaml
mcp_servers:
  docs:
    url: "https://mcp.docs.example.com"
    tools:
      prompts: false
      resources: false
```

## На что на самом деле влияет фильтрация?

В Hermes есть две категории функциональных возможностей, предоставляемых MCP:

1. Серверные инструменты MCP
- фильтруется с помощью:
  - `tools.include`
  - `tools.exclude`

2. Обертки утилит, добавленные Hermes
- фильтруется с помощью:
  - `tools.resources`
  - `tools.prompts`

### Обертки утилит, которые вы можете увидеть

Ресурсы:
- `list_resources`
- `read_resource`

Подсказки:
- `list_prompts`
- `get_prompt`

Эти оболочки появляются только в том случае, если:
- ваша конфигурация это позволяет, и
- сеанс сервера MCP фактически поддерживает эти возможности

Таким образом, Hermes не будет притворяться, что на сервере есть ресурсы/подсказки, если на самом деле их нет.

## Общие шаблоны

### Схема 1: местный помощник проекта

Используйте MCP для локальной файловой системы репозитория или сервера git, если вы хотите, чтобы Hermes обрабатывал ограниченное рабочее пространство.

```yaml
mcp_servers:
  fs:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/project"]

  git:
    command: "uvx"
    args: ["mcp-server-git", "--repository", "/home/user/project"]
```

Хорошие подсказки:

```text
Review the project structure and identify where configuration lives.
```

```text
Check the local git state and summarize what changed recently.
```

### Схема 2: репо-родная рабочая запись с Open Scaffold

Используйте [Open Scaffold](https://github.com/graphanov/open-scaffold), если вы хотите, чтобы компания Hermes прочитала надежные записи о работе ИИ в репозитории: миссии, планы, заметки с доказательствами, пакеты передачи и результаты проверки/контроля. Гермес остается агентом; Open Scaffold остается рекордсменом местного репо.

Добавьте сервер для одного резервного репозитория:

```bash
hermes mcp add open_scaffold --command npx --args -y open-scaffold@latest mcp serve --repo /absolute/path/to/repo
hermes mcp test open_scaffold
```

Затем держите открытую поверхность ориентированной на чтение. Выберите `select` в приглашении `hermes mcp add` или отредактируйте `config.yaml` позже:

```yaml
mcp_servers:
  open_scaffold:
    command: "npx"
    args: ["-y", "open-scaffold@latest", "mcp", "serve", "--repo", "/absolute/path/to/repo"]
    tools:
      include:
        - list_plans
        - get_plan
        - get_mission
        - list_evidence
        - get_evidence
        - get_status
        - search_plans
        - list_amendments
        - get_handoff
        - analyze_loop
        - gate_loop
      prompts: false
```

Хорошие подсказки:

```text
Use the Open Scaffold MCP tools to compile the current handoff packet and tell me the next legal action.
```

```text
Inspect the active plans and evidence notes, then say whether this repo is ready for human review or needs another attempt.
```

Граничные примечания:

- Open Scaffold MCP по умолчанию является локальным и доступен только для чтения.
- Его инструменты записи требуют запуска сервера с `--allow-write`; не включайте это до тех пор, пока вы явно не захотите, чтобы Hermes изменил файлы `.osc`.
- Работают открытые записи лесов и ворот; он не разрешает Hermes объединять, публиковать, развертывать или создавать среды выполнения.
- Закрепите `open-scaffold@<version>` вместо `@latest`, если вам нужны воспроизводимые схемы инструментов.

### Шаблон 3: Помощник по сортировке GitHub

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "***"
    tools:
      include: [list_issues, create_issue, update_issue, search_code]
      prompts: false
      resources: false
```

Хорошие подсказки:

```text
List open issues about MCP, cluster them by theme, and draft a high-quality issue for the most common bug.
```

```text
Search the repo for uses of _discover_and_register_server and explain how MCP tools are registered.
```

### Шаблон 4: внутренний API-помощник

```yaml
mcp_servers:
  internal_api:
    url: "https://mcp.internal.example.com"
    headers:
      Authorization: "Bearer ***"
    tools:
      include: [list_customers, get_customer, list_invoices]
      resources: false
      prompts: false
```

Хорошие подсказки:

```text
Look up customer ACME Corp and summarize recent invoice activity.
```

Это место, где строгий белый список намного лучше, чем список исключений.

### Схема 4: серверы документации/знаний

Некоторые серверы MCP предоставляют подсказки или ресурсы, которые больше похожи на общие ресурсы знаний, чем на прямые действия.

```yaml
mcp_servers:
  docs:
    url: "https://mcp.docs.example.com"
    tools:
      prompts: true
      resources: true
```

Хорошие подсказки:

```text
List available MCP resources from the docs server, then read the onboarding guide and summarize it.
```

```text
List prompts exposed by the docs server and tell me which ones would help with incident response.
```

## Учебник: комплексная настройка с фильтрацией

Вот практический прогресс.

### Этап 1: добавьте GitHub MCP с жестким белым списком

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "***"
    tools:
      include: [list_issues, create_issue, search_code]
      prompts: false
      resources: false
```

Запустите Гермес и спросите:

```text
Search the codebase for references to MCP and summarize the main integration points.
```

### Этап 2: расширяйте только при необходимости

Если позже вам также понадобятся обновления проблем:

```yaml
tools:
  include: [list_issues, create_issue, update_issue, search_code]
```

Затем перезагрузите:

```text
/reload-mcp
```

### Этап 3: добавьте второй сервер с другой политикой

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "***"
    tools:
      include: [list_issues, create_issue, update_issue, search_code]
      prompts: false
      resources: false

  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/project"]
```

Теперь Hermes может их объединить:

```text
Inspect the local project files, then create a GitHub issue summarizing the bug you find.
```

Именно здесь MCP становится мощным: мультисистемные рабочие процессы без изменения ядра Hermes.

## Рекомендации по безопасному использованию

### Предпочитать белые списки для опасных систем

Для любых финансовых, клиентоориентированных или разрушительных действий:
- используйте `tools.include`
- начните с минимально возможного набора

### Отключите неиспользуемые утилиты

Если вы не хотите, чтобы модель просматривала ресурсы/подсказки, предоставленные сервером, отключите их:

```yaml
tools:
  resources: false
  prompts: false
```

### Сохраняйте узкую область действия серверов

Примеры:
- Сервер файловой системы привязан к одному каталогу проекта, а не ко всему домашнему каталогу
- сервер git указал на одно репо
- внутренний сервер API с доступом к инструментам с большим объемом чтения по умолчанию

### Перезагрузка после изменения конфигурации

```text
/reload-mcp
```

Сделайте это после изменения:
- включать/исключать списки
- включенные флаги
- ресурсы/подсказки переключаются
- заголовки аутентификации / env

## Устранение неполадок по признаку

### «Сервер подключается, но ожидаемые инструменты отсутствуют»

Возможные причины:
- отфильтровано `tools.include`
- исключен `tools.exclude`
- Обертки утилит отключены через `resources: false` или `prompts: false`.
- сервер фактически не поддерживает ресурсы/подсказки

### "Сервер настроен, но ничего не загружается"

Проверьте:
- `enabled: false` не осталось в конфиге
- существует команда/время выполнения (`npx`, `uvx` и т. д.)
- Конечная точка HTTP доступна
- окружение аутентификации или заголовки верны

### «Почему я вижу меньше инструментов, чем рекламирует сервер MCP?»

Потому что Hermes теперь уважает вашу политику в отношении каждого сервера и регистрацию с учетом возможностей. Это ожидаемо и обычно желательно.

### «Как удалить сервер MCP, не удаляя конфигурацию?»

Использование:

```yaml
enabled: false
```

Это сохраняет конфигурацию, но предотвращает подключение и регистрацию.

## Рекомендуемые первые настройки MCP

Хорошие первые серверы для большинства пользователей:
- файловая система
- мерзавец
- Гитхаб
- выборка/документация серверов MCP
- один узкий внутренний API

Не очень хорошие первые сервера:
- гигантские бизнес-системы с множеством деструктивных действий и отсутствием фильтрации
- все, что вы не понимаете достаточно хорошо, чтобы сдерживать

## Связанные документы

- [MCP (Протокол контекста модели)](/user-guide/features/mcp)
- [Часто задаваемые вопросы](/ссылка/часто задаваемые вопросы)
- [Команды слэша](/reference/slash-commands)