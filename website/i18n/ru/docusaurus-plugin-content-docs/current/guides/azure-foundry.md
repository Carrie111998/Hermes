---
sidebar_position: 15
title: Microsoft Литейный завод
description: Используйте агент Hermes с Microsoft Foundry — конечные точки в стиле
  OpenAI и Anthropic, автоматическое обнаружение транспорта и развернутых моделей.
---

# Литейный завод Microsoft

Поставщик `azure-foundry` агента Hermes поддерживает Microsoft Foundry (ранее Azure AI Foundry) и Azure OpenAI. На одном ресурсе Foundry могут размещаться модели с двумя разными форматами проводов:

- **В стиле OpenAI** — `POST /v1/chat/completions` на таких конечных точках, как `https://<resource>.openai.azure.com/openai/v1`. Используется для GPT-4.x, GPT-5.x, Llama, Mistral и большинства моделей с открытым весом.
- **Антропный стиль** — `POST /v1/messages` на конечных точках, таких как `https://<resource>.services.ai.azure.com/anthropic`. Используется, когда Microsoft Foundry обслуживает модели Claude через формат API Anthropic Messages.

Мастер установки проверяет вашу конечную точку и автоматически определяет, какой транспорт она использует, какие развертывания доступны, а также длину контекста каждой модели.

## Предварительные условия

- Ресурс Microsoft Foundry или Azure OpenAI с хотя бы одним развертыванием.
– URL-адрес конечной точки развертывания.
— **Либо** ключ API (на портале Azure в разделе «Ключи и конечная точка»), **или** роль **Пользователь Azure AI** RBAC на ресурсе Foundry, если вы планируете использовать Microsoft Entra ID (путь без ключа, рекомендуемый Microsoft). Некоторые арендаторы могут отображать роль **Foundry User** во время переименования Microsoft.

## Быстрый старт

```bash
hermes model
# → Select "Azure Foundry"
# → Enter your endpoint URL
# → Choose Authentication:
#     1. API key
#     2. Microsoft Entra ID  (managed identity / workload identity / az login)
# → (Entra) Hermes probes DefaultAzureCredential; on success it never asks for a key
# → (API key) Enter your API key
# Hermes probes the endpoint and auto-detects transport + models
# → Pick a model from the list (or type a deployment name manually)
```

Мастер:

1. **Проанализируйте путь URL-адреса** — URL-адреса, заканчивающиеся на `/anthropic`, распознаются как маршруты Microsoft Foundry Claude.
2. **Зонд `GET <base>/models`** — если конечная точка возвращает список моделей в форме OpenAI, Hermes переключается на `chat_completions` и предварительно заполняет средство выбора возвращенными идентификаторами развертывания.
3. **Форма зондирования антропных сообщений** — запасной вариант для конечных точек, которые не предоставляют `/models`, но принимают формат антропных сообщений.
4. **Вернуться к ручному вводу** — частные/закрытые конечные точки, которые отклоняют все запросы, по-прежнему работают; вы выбираете режим API и вводите имя развертывания вручную.

Длина контекста для выбранной модели определяется с помощью стандартной цепочки метаданных Hermes (`models.dev`, метаданные поставщика и жестко запрограммированные резервные варианты семейства) и сохраняется в `config.yaml`, чтобы модель могла правильно определить размер собственного контекстного окна.

## Microsoft Entra ID (без ключа, RBAC) — рекомендуется

Microsoft рекомендует [аутентификацию без ключа с помощью Microsoft Entra ID] (https://learn.microsoft.com/azure/ai-foundry/foundry-models/how-to/configure-entra-id) для производственных рабочих нагрузок Foundry. Hermes поддерживает Entra ID для **обеих** поверхностей API:

- **В стиле OpenAI** (`api_mode: chat_completions` / `codex_responses`) — GPT-4/5, Llama, Mistral, DeepSeek и т. д.
- **Антропный стиль** (`api_mode: anthropic_messages`) — модели Клода в Microsoft Foundry.

RBAC Foundry применяется для каждого ресурса (`Azure AI User` предоставляет обе поверхности; некоторые арендаторы могут отображать `Foundry User`), и Microsoft документирует одну и ту же область вывода (`https://ai.azure.com/.default`) для обеих. Под капотом:

- В стиле OpenAI используется собственный вызываемый контракт `api_key=` OpenAI Python SDK — SDK автоматически создает новый JWT для каждого запроса.
- В стиле Anthropic используется `httpx.Client` с перехватчиком событий запроса, установленным `agent.azure_identity_adapter.build_bearer_http_client`, поскольку Anthropic SDK изначально не принимает вызываемый `auth_token`. Перехватчик перезаписывает `Authorization: Bearer <fresh-jwt>` для каждого исходящего запроса. Тот же Microsoft RBAC, та же область действия Foundry — единственное отличие — контракт SDK.

### Зачем использовать Entra ID?

- Никаких долгоживущих ключей API, которые можно было бы менять или отзывать.
- Доступ на основе RBAC — разрешите или удалите `Azure AI User` на ресурсе Foundry, переписывание конфигурации не требуется.
- Журналы доступа и аудита сегментируются по исполнителям, а не по всем вызывающим абонентам, использующим один статический ключ.
— Единая поверхность аутентификации для виртуальных машин Azure, модулей AKS, Службы приложений, функций, приложений-контейнеров и службы агента Foundry через управляемое удостоверение.
- Идентификация рабочей нагрузки и потоки субъектов службы для конвейеров CI/CD.

### Одноразовая установка (сторона Azure)

1. На портале Azure откройте ресурс Foundry → **Контроль доступа (IAM)** → **Добавить → Добавить назначение роли**.
2. Выберите роль **Пользователь Azure AI** (или **Пользователь Foundry**, если ваш клиент имеет переименованную роль).
3. Назначьте его:
   - **Ваша учетная запись** для локальной разработки с помощью `az login`.
   — **Управляемое удостоверение или удостоверение рабочей нагрузки** для вычислений, размещенных в Azure (рекомендуется для рабочей среды).
   - **Удостоверение агента, размещенное в службе Foundry Agent Service**, когда Hermes работает внутри размещенного агента.
   — **Субъект службы** для конвейеров CI/CD, когда удостоверение рабочей нагрузки недоступно.
4. Подождите около 5 минут, пока роль распространится.

Эквивалент Azure CLI:

```bash
az role assignment create \
  --assignee <principal-or-agent-identity-client-id> \
  --role "Azure AI User" \
  --scope <foundry-resource-id>
```

### Одноразовая установка (сторона Гермеса)

```bash
hermes model
# → Select "Azure Foundry"
# → Enter your endpoint URL
# → Authentication: 2 (Microsoft Entra ID)
# → (optional) user-assigned managed identity client ID
# → (optional) Azure tenant ID
# → Hermes probes DefaultAzureCredential() and reports which inner
#    credential succeeded (e.g. AzureCliCredential, ManagedIdentityCredential)
```

Мастер запускает ограниченную предполетную проверку (тайм-аут 10 секунд). В случае сбоя он предлагает «в любом случае сохранить, проверить позже» — полезно при настройке на машине, у которой еще нет учетных данных, но они будут во время выполнения (например, подготовка конфигурации для развертывания с управляемой идентификацией).

`azure-identity` устанавливается автоматически при первом использовании по пути отложенной установки Hermes. Для предварительной установки:

```bash
pip install azure-identity
```

### Конфигурация записана в `config.yaml`

```yaml
model:
  provider: azure-foundry
  base_url: https://my-resource.openai.azure.com/openai/v1
  api_mode: chat_completions
  auth_mode: entra_id
  default: gpt-4o
  context_length: 128000
  entra:
    scope: https://ai.azure.com/.default        # only when overriding the default
```

Hermes управляет только одной специфичной для Entra ручкой в `config.yaml`:

- **`scope`** — область действия ресурса OAuth. По умолчанию соответствует документированной области вывода Microsoft (`https://ai.azure.com/.default`). Переопределять только в том случае, если ваш ресурс был предназначен для нестандартной аудитории.

Все остальное (клиент, секрет субъекта службы, файл федеративного токена, полномочия независимого облака, настройки брокера) считывается `azure-identity` непосредственно из стандартных переменных среды `AZURE_*` — см. [порядок разрешения учетных данных](#credential-solve-order) ниже. Установите их в `~/.hermes/.env` или в своей среде развертывания точно так, как описано в справочнике Microsoft по SDK.

Никакие секреты не попадают в `~/.hermes/.env` для режима Entra — `azure-identity` кэширует токены в процессе (и, если возможно, в связке ключей вашей ОС / `~/.IdentityService`).

### Порядок разрешения учетных данных

`DefaultAzureCredential` из `azure-identity` проходит по этой цепочке при каждом запросе токена, останавливаясь на первых учетных данных, возвращающих токен:

1. **Учетные данные среды** — `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET` (или `AZURE_CLIENT_CERTIFICATE_PATH` / `AZURE_FEDERATED_TOKEN_FILE`).
2. **Идентификация рабочей нагрузки** — `AZURE_FEDERATED_TOKEN_FILE` (федеративные токены AKS/OIDC).
3. **Управляемая идентификация** — конечная точка IMDS (`169.254.169.254`) для виртуальных машин; `IDENTITY_ENDPOINT` для Службы приложений/Функций/Приложений-контейнеров. Агенты, размещенные в службе Foundry Agent, используют удостоверение агента размещенного агента.
4. **Visual Studio Code** — расширение учетной записи Azure.
5. **Azure CLI** — сеанс `az login`.
6. **Azure Developer CLI** — `azd auth login`.
7. **Azure PowerShell** — `Connect-AzAccount`.
8. **Брокер** (только для Windows/WSL) — веб-менеджер учетных записей.

Учетные данные интерактивного браузера по умолчанию исключены для автоматических запусков Hermes; Вместо этого используйте Azure CLI, Azure Developer CLI, управляемое удостоверение, удостоверение рабочей нагрузки или учетные данные субъекта-службы.

### Шаблоны развертывания

**Местная застройка:**
```bash
az login
hermes model   # pick Azure Foundry → Entra ID
hermes         # uses your az login token
```

**ВМ Azure/Функции/Служба приложений/Приложения-контейнеры (управляемое удостоверение, назначаемое системой):**
1. Включите назначаемое системой удостоверение на вычислительном ресурсе.
2. Предоставьте идентификатор `Azure AI User` (или `Foundry User`) для ресурса Foundry.
3. Установите `model.auth_mode: entra_id` в config.yaml — переменные env не нужны.

**ВМ Azure/Функции/Служба приложений/Приложения-контейнеры (управляемое удостоверение, назначаемое пользователем):**
– Установите `AZURE_CLIENT_ID` в идентификатор клиента, назначенный пользователем, чтобы `DefaultAzureCredential` выбрал правильный.

**Агент, размещенный в службе Foundry Agent:**
– Создайте размещенный агент и предоставьте этому агенту идентификатор `Azure AI User` (или `Foundry User`) в ресурсе Foundry. Hermes использует `ManagedIdentityCredential` изнутри размещенного агента; Назначение роли принадлежит удостоверению агента, а не только родительскому проекту или вашему пользователю.

**Идентификатор рабочей нагрузки AKS (заменяет идентификатор модуля AAD):**
– Добавьте к учетной записи службы модуля идентификатор клиента идентификации рабочей нагрузки.
— Файл федеративного токена модуля автоматически определяется через `AZURE_FEDERATED_TOKEN_FILE`.
- `model.auth_mode: entra_id` работает без дальнейших изменений конфигурации.

**Субъект службы в CI:**
- Установите `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` в среде бегуна.

#### Суверенные облака (Правительство, Китай)

Экспортируйте `AZURE_AUTHORITY_HOST` (например, `https://login.microsoftonline.us` для Azure Government, `https://login.partner.microsoftonline.cn` для Azure China). `azure-identity` читает его напрямую.

### Проверка здоровья

`hermes doctor` запускает 10-секундную проверку `DefaultAzureCredential` при `model.auth_mode: entra_id`, сообщая, какие внутренние учетные данные выиграли (присутствуют переменные среды, достижимая конечная точка управляемого удостоверения и т. д.).

`hermes auth` показывает структурированный блок состояния:

```
azure-foundry (Microsoft Entra ID):
  Endpoint: https://my-resource.openai.azure.com/openai/v1
  Scope: https://ai.azure.com/.default
  Status: configured; live token probe is skipped here
```

### Ограничения

- **Конечные точки в стиле Anthropic используют перехватчик событий httpx.** Anthropic Python SDK не принимает вызываемый `auth_token` изначально (≤ 0.86.0). Hermes устанавливает перехватчик событий запроса на специальный `httpx.Client`, который создает новый JWT для каждого исходящего запроса и перезаписывает `Authorization: Bearer <jwt>`. Это функционально эквивалентно собственному контракту `Callable[[], str]` OpenAI SDK, но добавляет один уровень косвенности. Если Anthropic SDK добавит первоклассную поддержку вызываемой аутентификации в будущем выпуске, Hermes перейдет на нее прозрачно.
- **Пакетные задания и `multiprocessing.Pool`.** Поставщик токенов Entra представляет собой замыкание, которое нельзя выбрать за пределами границ процесса. `batch_runner.py` автоматически удаляет вызываемый объект из конфигурации рабочего процесса и позволяет каждому рабочему процессу перестроить своего собственного поставщика из `config.yaml` — никаких действий пользователя не требуется, но каждый рабочий процесс платит один проход по цепочке при запуске.
- **Нет сохранения JWT носителя в `auth.json`.** Hermes не дублирует внутренний кэш токенов `azure-identity`; холодные старты проходят цепочку учетных данных при первом выводе.

## Конфигурация (записана в `config.yaml`)

После запуска мастера вы увидите что-то вроде этого:

```yaml
model:
  provider: azure-foundry
  base_url: https://my-resource.openai.azure.com/openai/v1
  api_mode: chat_completions         # or "anthropic_messages"
  default: gpt-5.4-mini              # your deployment / model name
  context_length: 400000             # auto-detected
```

И в `~/.hermes/.env`:

```
AZURE_FOUNDRY_API_KEY=<your-azure-key>
```

## Конечные точки в стиле OpenAI (GPT, Llama и т. д.)

Конечная точка Azure OpenAI v1 GA принимает стандартный клиент Python `openai` с минимальными изменениями:

```yaml
model:
  provider: azure-foundry
  base_url: https://my-resource.openai.azure.com/openai/v1
  api_mode: chat_completions
  default: gpt-5.4
```

Важное поведение:

- **GPT-5.x, codex и o-series автоматически перенаправляются к API Responses.** Microsoft Foundry развертывает модели GPT-5/codex/o1/o3/o4 только для Responses-API — вызов `/chat/completions` для них возвращает `400 "The requested operation is unsupported."`. Hermes определяет эти семейства моделей по имени и прозрачно обновляет `api_mode` до `codex_responses`, даже если `config.yaml` по-прежнему читается как `api_mode: chat_completions`. GPT-4, GPT-4o, Llama, Mistral и другие развертывания остаются на `/chat/completions`.
- **`max_completion_tokens` используется автоматически.** Azure OpenAI (как и прямой OpenAI) требуется `max_completion_tokens` для моделей gpt-4o, o-series и gpt-5.x. Hermes отправляет правильный параметр в зависимости от конечной точки.
- **Конечные точки версии Pre-v1, которым требуется `api-version`.** Если у вас есть устаревший базовый URL-адрес, например `https://<resource>.openai.azure.com/openai?api-version=2025-04-01-preview`, Hermes извлекает строку запроса и пересылает ее через `default_query` при каждом запросе (в противном случае OpenAI SDK удаляет ее при объединении путей).

## Конечные точки в антропном стиле (Клод через Microsoft Foundry)

Для развертываний Claude используйте маршрут в стиле Anthropic:

```yaml
model:
  provider: azure-foundry
  base_url: https://my-resource.services.ai.azure.com/anthropic
  api_mode: anthropic_messages
  default: claude-sonnet-4-6
```

Важное поведение:

- **`/v1` удаляется из базового URL-адреса.** Anthropic SDK добавляет `/v1/messages` к каждому URL-адресу запроса. Hermes удаляет все конечные `/v1` перед передачей URL-адреса в SDK, чтобы избежать двойных путей `/v1`.
— **`api-version` отправляется через `default_query`, а не добавляется к URL-адресу.** Для Azure Anthropic требуется строка запроса `api-version`. Включение его в базовый URL-адрес создает неверные пути, такие как `/anthropic?api-version=.../v1/messages`, и возвращает 404. Вместо этого Hermes передает `api-version=2025-04-15` через `default_query` Anthropic SDK.
- **Аутентификация на предъявителя используется вместо `x-api-key`.** Для маршрута Azure, совместимого с Anthropic, требуется `Authorization: Bearer <key>`, а не собственный заголовок `x-api-key` Anthropic. Hermes обнаруживает `azure.com` в базовом URL-адресе и направляет ключ API через поле `auth_token` SDK, чтобы правый заголовок достиг восходящего потока.
— **Бета-заголовок окна контекста 1M сохраняется.** Azure по-прежнему ограничивает контекст Claude с токеном 1M (Opus 4.6/4.7, Sonnet 4.6) за заголовком `anthropic-beta: context-1m-2025-08-07`. Hermes сохраняет этот бета-заголовок в путях Azure (он удален из собственных запросов Anthropic OAuth, поскольку некоторые подписки отклоняют его, но Azure требует его).
— **Обновление токена OAuth отключено.** В развертываниях Azure используются статические ключи API. Цикл обновления токена OAuth `~/.claude/.credentials.json`, применимый к консоли Anthropic, явно пропускается для конечных точек Azure, чтобы не допустить перезаписи токена OAuth Claude Code вашего ключа Azure в середине сеанса.

## Альтернатива: `provider: anthropic` + базовый URL-адрес Azure.

Если у вас уже настроен `provider: anthropic` и вы просто хотите указать его на Microsoft Foundry for Claude, вы можете полностью пропустить поставщик `azure-foundry`:

```yaml
model:
  provider: anthropic
  base_url: https://my-resource.services.ai.azure.com/anthropic
  key_env: AZURE_ANTHROPIC_KEY
  default: claude-sonnet-4-6
```

Если `AZURE_ANTHROPIC_KEY` установлен в `~/.hermes/.env`. Hermes обнаруживает `azure.com` в базовом URL-адресе и замыкает цепочку токенов OAuth Claude Code, поэтому ключ Azure используется напрямую с аутентификацией `x-api-key`.

`key_env` — каноническое имя поля Snake_case; `api_key_env` (и CamelCase `keyEnv` / `apiKeyEnv`) принимаются в качестве псевдонимов. Если установлены оба `key_env` и `AZURE_ANTHROPIC_KEY`/`ANTHROPIC_API_KEY`, побеждает переменная окружения с именем `key_env`.

## Обнаружение модели

Azure **не** предоставляет конечную точку с чистым ключом API для вывода списка развертываний *развернутой* модели. Для перечисления развертывания требуется проверка подлинности Azure Resource Manager (`az cognitiveservices account deployment list`) с использованием субъекта Azure AD, а не ключа API вывода.

Что может Гермес:

— Конечные точки Azure OpenAI v1 (`<resource>.openai.azure.com/openai/v1`) предоставляют `GET /models` с **доступным** каталогом моделей ресурса. Hermes использует этот список для предварительного заполнения средства выбора модели.
- Маршруты Microsoft Foundry `/anthropic`: обнаружены по URL-адресу, название модели введено вручную.
- Частные конечные точки/конечные точки с брандмауэром: ввод вручную с дружественным сообщением «не удалось проверить».

Вы всегда можете ввести имя развертывания напрямую — Hermes не проверяет возвращаемый список.

## Переменные среды

| Переменная | Цель |
|----------|---------|
| `AZURE_FOUNDRY_API_KEY` | Первичный ключ API для Microsoft Foundry/Azure OpenAI (режим api_key) |
| `AZURE_FOUNDRY_BASE_URL` | URL-адрес конечной точки (устанавливается через `hermes model`; переменная env используется как запасной вариант) |
| `AZURE_ANTHROPIC_KEY` | Используется `provider: anthropic` + базовый URL-адрес Azure (альтернатива `ANTHROPIC_API_KEY`) |
| `AZURE_TENANT_ID` | Арендатор Entra ID для потоков субъекта-службы |
| `AZURE_CLIENT_ID` | Идентификатор клиента Entra ID (субъект службы, удостоверение рабочей нагрузки или управляемое удостоверение, назначаемое пользователем) |
| `AZURE_CLIENT_SECRET` | Секрет участника службы |
| `AZURE_CLIENT_CERTIFICATE_PATH` | Сертификат субъекта службы (альтернатива секретному) |
| `AZURE_FEDERATED_TOKEN_FILE` | Путь федеративного токена Workload Identity (AKS) |
| `AZURE_AUTHORITY_HOST` | Переопределение хоста суверенного облака |
| `IDENTITY_ENDPOINT` / `MSI_ENDPOINT` | Конечная точка управляемого удостоверения для службы приложений, функций и приложений-контейнеров; Вместо этого виртуальные машины обычно используют IMDS |

Azure SDK напрямую считывает переменные среды `AZURE_*`. Гермес никогда их не проверяет, кроме как сообщать, какие источники присутствуют в выводе `hermes doctor`.

## Устранение неполадок

**401 Несанкционировано при развертывании gpt-5.x.**
Azure обслуживает gpt-5.x на `/chat/completions`, а не на `/responses`. Hermes обрабатывает это автоматически, если URL-адрес содержит `openai.azure.com`, но если вы видите 401 с телом `Invalid API key`, убедитесь, что `api_mode` в вашем `config.yaml` — это `chat_completions`.

**404 от `/v1/messages?api-version=.../v1/messages`.**
Это ошибка неправильного URL-адреса в предварительных настройках Azure Anthropic. Обновите Hermes — параметр `api-version` теперь передается через `default_query`, а не встроен в базовый URL-адрес, поэтому SDK не сможет повредить его во время объединения URL-адресов.

**Мастер сообщает: «Автоопределение не завершено».**
Конечная точка отклонила как проверку `/models`, так и проверку антропных сообщений. Это нормально для частных конечных точек, находящихся за брандмауэром или со списком разрешенных IP-адресов. Вернитесь к выбору режима API вручную и введите имя развертывания — все по-прежнему работает, Hermes просто не может предварительно заполнить окно выбора.

**Выбран неправильный транспорт.**
Запустите `hermes model` еще раз, и мастер повторит проверку. Если зонд по-прежнему выбирает неправильный режим, вы можете напрямую отредактировать `config.yaml`:

```yaml
model:
  provider: azure-foundry
  api_mode: anthropic_messages   # or chat_completions
```

**Идентификатор Entra: «цепочка учетных данных исчерпана» или 401 «Не авторизовано» после перехода на `auth_mode: entra_id`.**
– Запустите `az login`, чтобы обновить сеанс разработчика (возможно, срок действия кэшированного токена истек).
– Убедитесь, что назначение роли `Azure AI User` (или `Foundry User`) вступило в силу: `az role assignment list --assignee <user-or-identity-id>` должен указать ее в вашем ресурсе Foundry. Распространение ролей может занять до 5 минут.
– Для управляемых удостоверений, назначенных пользователем, дважды проверьте, соответствует ли `AZURE_CLIENT_ID` удостоверению, прикрепленному к вычислительному ресурсу.
- Запустите `hermes doctor` — проверка Azure Entra сообщает, удалось ли получить токен, и включает подсказку по исправлению.

**Entra ID: предварительная проверка мастера зависает или истекает время ожидания.**
10-секундная предполетная проверка — это мягкая проверка. Выберите «Сохранить в любом случае и проверить позже» и запустите `hermes doctor` после развертывания в целевой среде. Общие причины включают недоступность службы токенов или устаревшее локальное состояние входа в систему — отдайте предпочтение удостоверению рабочей нагрузки в CI, установите `AZURE_TENANT_ID`+`AZURE_CLIENT_ID`+`AZURE_CLIENT_SECRET` при использовании субъекта-службы или запустите `az login` для локальной разработки.

**401 на конечной точке в антропном стиле с идентификатором Entra.**
Убедитесь, что ресурсу Foundry назначена одна и та же роль `Azure AI User` (или `Foundry User`) (она охватывает пути `/openai/v1` и `/anthropic`). Если зонд в стиле OpenAI работает во время работы мастера, но запросы `claude-*` завершаются с ошибкой во время выполнения, наиболее распространенной причиной является устаревший `model.entra.scope`, оставшийся от предыдущего запуска мастера — удалите строку `entra.scope` из `config.yaml`, чтобы среда выполнения вернулась к области действия `https://ai.azure.com/.default` по умолчанию.

## Связанный

- [Переменные среды](/reference/environment-variables)
- [Конфигурация](/руководство пользователя/конфигурация)
- [AWS Bedrock](/guides/aws-bedrock) — еще один крупный интегратор облачных услуг.
- [Microsoft: Настройка Entra ID для Foundry](https://learn.microsoft.com/azure/ai-foundry/foundry-models/how-to/configure-entra-id) — исходная документация для бесключевого пути.