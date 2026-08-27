---
sidebar_position: 2
title: Переменные среды
description: Полная ссылка на все переменные среды, используемые агентом Hermes.
---

# Справочник по переменным среды

Hermes считывает переменные среды из среды процесса, а для секретов, управляемых пользователем, из `~/.hermes/.env`. Храните ключи API, токены ботов, секреты OAuth и другие учетные данные в `.env`; предпочитайте `config.yaml` для несекретных настроек поведения, если существует ключ конфигурации. Некоторые приведенные ниже переменные являются переопределенными только для процесса или внутренними переменными моста, и их не следует фиксировать в `.env` только потому, что они документированы здесь.

## Поставщики LLM

| Переменная | Описание |
|----------|-------------|
| `OPENROUTER_API_KEY` | Ключ API OpenRouter (рекомендуется для гибкости) |
| `OPENROUTER_BASE_URL` | Переопределить базовый URL-адрес, совместимый с OpenRouter |
| `FIREWORKS_API_KEY` | Ключ API Fireworks AI ([app.fireworks.ai](https://app.fireworks.ai/settings/users/api-keys)). Настройте переопределения конечной точки с помощью `model.base_url` в `config.yaml`. |
| `HERMES_OPENROUTER_CACHE` | Включите кэширование ответов OpenRouter (`1`/`true`/`yes`/`on`). Переопределяет `openrouter.response_cache` в config.yaml. См. [Кэширование ответов] (https://openrouter.ai/docs/guides/features/response-caching). |
| `HERMES_OPENROUTER_CACHE_TTL` | Срок жизни кэша в секундах (1-86400). Переопределяет `openrouter.response_cache_ttl` в config.yaml. |
| `NOUS_BASE_URL` | Переопределить базовый URL-адрес Nous Portal (требуется редко; только для разработки/тестирования) |
| `NOUS_INFERENCE_BASE_URL` | Переопределить конечную точку вывода Nous напрямую |
| `AI_GATEWAY_API_KEY` | Ключ API Vercel AI Gateway ([ai-gateway.vercel.sh](https://ai-gateway.vercel.sh)) |
| `AI_GATEWAY_BASE_URL` | Переопределить базовый URL-адрес шлюза AI (по умолчанию: `https://ai-gateway.vercel.sh/v1`) |
| `OPENAI_API_KEY` | Ключ API для пользовательских конечных точек, совместимых с OpenAI (используется с `OPENAI_BASE_URL`) |
| `OPENAI_BASE_URL` | Базовый URL-адрес для пользовательской конечной точки (VLLM, SGLang и т. д.) |
| `LM_API_KEY` | Ключ API для LM Studio (поставщик `lmstudio`). Часто является заполнителем для локальных серверов |
| `LM_BASE_URL` | Базовый URL-адрес LM Studio (по умолчанию: `http://localhost:1234/v1`) |
| `COPILOT_GITHUB_TOKEN` | Токен GitHub для Copilot API — первый приоритет (OAuth `gho_*` или детальный PAT `github_pat_*`; классические PAT `ghp_*` **не поддерживаются**) |
| `GH_TOKEN` | Токен GitHub — второй приоритет для Copilot (также используется `gh` CLI) |
| `GITHUB_TOKEN` | Токен GitHub — третий приоритет для Copilot |
| `HERMES_COPILOT_ACP_COMMAND` | Переопределить двоичный путь CLI Copilot ACP (по умолчанию: `copilot`) |
| `COPILOT_CLI_PATH` | Псевдоним `HERMES_COPILOT_ACP_COMMAND` |
| `HERMES_COPILOT_ACP_ARGS` | Переопределить аргументы Copilot ACP (по умолчанию: `--acp --stdio`) |
| `COPILOT_ACP_BASE_URL` | Переопределить базовый URL-адрес Copilot ACP |
| `COPILOT_API_BASE_URL` | Переопределить базовый URL-адрес Copilot API (поставщик `copilot`) |
| `GLM_API_KEY` | z.ai / Ключ API GLM ZhipuAI ([z.ai](https://z.ai)) |
| `ZAI_API_KEY` | Псевдоним `GLM_API_KEY` |
| `Z_AI_API_KEY` | Псевдоним для `GLM_API_KEY` |
| `GLM_BASE_URL` | Переопределить базовый URL-адрес z.ai (по умолчанию: `https://api.z.ai/api/paas/v4`) |
| `KIMI_API_KEY` | Ключ API Кими/Moonshot AI ([moonshot.ai](https://platform.moonshot.ai)) |
| `KIMI_CODING_API_KEY` | Ключ-псевдоним для поставщика `kimi-coding` (принимается вместе с `KIMI_API_KEY`) |
| `KIMI_BASE_URL` | Переопределить базовый URL-адрес Кими (по умолчанию: `https://api.moonshot.ai/v1`) |
| `KIMI_CN_API_KEY` | Ключ API Кими / Moonshot China ([moonshot.cn](https://platform.moonshot.cn)) |
| `ARCEEAI_API_KEY` | Ключ API Arcee AI ([chat.arcee.ai](https://chat.arcee.ai/)) |
| `ARCEE_BASE_URL` | Переопределить базовый URL-адрес Arcee (по умолчанию: `https://api.arcee.ai/api/v1`) |
| `GMI_API_KEY` | Ключ API облака GMI ([gmicloud.ai](https://www.gmicloud.ai/)) |
| `GMI_BASE_URL` | Переопределить базовый URL-адрес GMI Cloud (по умолчанию: `https://api.gmi-serving.com/v1`) |
| `ACTUAL_API_KEY` | Фактический ключ вывода компьютера (`ac_...`, [actual.inc/user/keys](https://actual.inc/user/keys)). Не требуется для локального демона. |
| `ACTUAL_BASE_URL` | Переопределить базовый URL-адрес фактического компьютера (по умолчанию: `https://api.actual.inc/v1`). Установите значение `http://127.0.0.1:8080` для локального автономного демона — узлам обратной связи не требуется ключ API. |
| `MINIMAX_API_KEY` | Ключ API MiniMax — глобальная конечная точка ([minimax.io](https://www.minimax.io)). **Не используется `minimax-oauth`** (вместо этого путь OAuth использует вход в браузер). |
| `MINIMAX_BASE_URL` | Переопределить базовый URL-адрес MiniMax (по умолчанию: `https://api.minimax.io/anthropic` — Hermes использует конечную точку MiniMax, совместимую с Anthropic Messages). **Не используется `minimax-oauth`**. |
| `MINIMAX_CN_API_KEY` | Ключ API MiniMax — конечная точка Китая ([minimaxi.com](https://www.minimaxi.com)). **Не используется `minimax-oauth`** (вместо этого путь OAuth использует вход в браузер). |
| `MINIMAX_CN_BASE_URL` | Переопределить базовый URL-адрес MiniMax для Китая (по умолчанию: `https://api.minimaxi.com/anthropic`). **Не используется `minimax-oauth`**. |
| `KILOCODE_API_KEY` | API-ключ Kilo Code ([kilo.ai](https://kilo.ai)) |
| `KILOCODE_BASE_URL` | Переопределить базовый URL-адрес кода Kilo (по умолчанию: `https://api.kilo.ai/api/gateway`) |
| `XIAOMI_API_KEY` | API-ключ Xiaomi MiMo ([platform.xiaomimimo.com](https://platform.xiaomimimo.com)) |
| `XIAOMI_BASE_URL` | Переопределить базовый URL-адрес Xiaomi MiMo (по умолчанию: `https://api.xiaomimimo.com/v1`) |
| `UPSTAGE_API_KEY` | Ключ API Upstage для моделей Solar ([console.upstage.ai](https://console.upstage.ai/api-keys)) |
| `UPSTAGE_BASE_URL` | Переопределить базовый URL-адрес Upstage (по умолчанию: `https://api.upstage.ai/v1`) |
| `TOKENHUB_API_KEY` | Десять

цент API-ключ TokenHub ([tokenhub.tencentmaas.com](https://tokenhub.tencentmaas.com)) |
| `TOKENHUB_BASE_URL` | Переопределить базовый URL-адрес Tencent TokenHub (по умолчанию: `https://tokenhub.tencentmaas.com/v1`) |
| `AZURE_FOUNDRY_API_KEY` | Ключ API Microsoft Foundry/Azure OpenAI ([ai.azure.com](https://ai.azure.com/)). Не требуется, если `model.auth_mode: entra_id` |
| `AZURE_FOUNDRY_BASE_URL` | URL-адрес конечной точки Microsoft Foundry (например, `https://<resource>.openai.azure.com/openai/v1` для стиля OpenAI или `https://<resource>.services.ai.azure.com/anthropic` для стиля Anthropic) |
| `AZURE_ANTHROPIC_KEY` | Ключ Azure Anthropic API для `provider: anthropic` + `base_url`, указывающий на развертывание Microsoft Foundry Claude (альтернатива `ANTHROPIC_API_KEY`, если настроены как Anthropic, так и Azure Anthropic) |
| `AZURE_TENANT_ID` | Идентификатор клиента Entra ID (потоки субъекта-службы; учитывается `azure-identity` при `model.auth_mode: entra_id`) |
| `AZURE_CLIENT_ID` | Идентификатор клиента Entra ID (субъект службы, удостоверение рабочей нагрузки или управляемое удостоверение, назначаемое пользователем) |
| `AZURE_CLIENT_SECRET` | Секрет участника-службы, используемый `EnvironmentCredential` |
| `AZURE_CLIENT_CERTIFICATE_PATH` | Сертификат субъекта-службы (альтернатива `AZURE_CLIENT_SECRET`) |
| `AZURE_FEDERATED_TOKEN_FILE` | Путь к файлу федеративного токена для потоков идентификации рабочей нагрузки AKS/OIDC |
| `AZURE_AUTHORITY_HOST` | Переопределение полномочий суверенного облака (например, `https://login.microsoftonline.us` для Azure Government). См. [Руководство по Azure Foundry](/guides/azure-foundry#sovereign-clouds-government-china) |
| `IDENTITY_ENDPOINT` / `MSI_ENDPOINT` | Конечная точка управляемого удостоверения для службы приложений, функций и приложений-контейнеров; Вместо этого виртуальные машины обычно используют IMDS и не устанавливают эти |
| `HF_TOKEN` | Токен Hugging Face для поставщиков логических выводов ([huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)) |
| `HF_BASE_URL` | Переопределить базовый URL-адрес «Обнимающего лица» (по умолчанию: `https://router.huggingface.co/v1`) |
| `GOOGLE_API_KEY` | Ключ API Google AI Studio ([aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)) |
| `GEMINI_API_KEY` | Псевдоним `GOOGLE_API_KEY` |
| `GEMINI_BASE_URL` | Переопределить базовый URL-адрес Google AI Studio |
| `VERTEX_CREDENTIALS_PATH` | Путь к JSON учетной записи службы Google Cloud для Vertex AI (Gemini). Vertex использует OAuth2, а не статический ключ API. Возвращается к `GOOGLE_APPLICATION_CREDENTIALS`, затем к АЦП (`gcloud auth application-default login`). Установите проект/регион под `vertex:` в `config.yaml` |
| `ANTHROPIC_API_KEY` | Ключ API консоли Anthropic ([console.anthropic.com](https://console.anthropic.com/)) |
| `ANTHROPIC_BASE_URL` | Переопределить базовый URL-адрес Anthropic API |
| `ANTHROPIC_TOKEN` | Ручное или устаревшее переопределение Anthropic OAuth/токена настройки |
| `DASHSCOPE_API_KEY` | Ключ API Qwen Cloud (Alibaba DashScope) для моделей Qwen ([modelstudio.console.alibabacloud.com](https://modelstudio.console.alibabacloud.com/)) |
| `DASHSCOPE_BASE_URL` | Пользовательский базовый URL-адрес DashScope (по умолчанию: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`; используйте `https://dashscope.aliyuncs.com/compatible-mode/v1` для региона материкового Китая) |
| `ALIBABA_CODING_PLAN_API_KEY` | Ключ API плана кодирования Qwen (поставщик `alibaba-coding-plan`) |
| `ALIBABA_CODING_PLAN_BASE_URL` | Переопределить базовый URL-адрес плана кодирования Qwen |
| `DEEPSEEK_API_KEY` | API-ключ DeepSeek для прямого доступа к DeepSeek ([platform.deepseek.com](https://platform.deepseek.com/api_keys)) |
| `DEEPSEEK_BASE_URL` | Пользовательский базовый URL-адрес DeepSeek API |
| `DEEPINFRA_API_KEY` | Ключ API DeepInfra ([deepinfra.com](https://deepinfra.com/dash/api_keys)) |
| `DEEPINFRA_BASE_URL` | Переопределение базового URL-адреса DeepInfra |
| `NOVITA_API_KEY` | Ключ NovitaAI API — собственное облако AI для API моделей, тестовой среды агента и облака графического процессора ([novita.ai/settings/key-management](https://novita.ai/settings/key-management)) |
| `NOVITA_BASE_URL` | Переопределить базовый URL-адрес NovitaAI (по умолчанию: `https://api.novita.ai/openai/v1`) |
| `NVIDIA_API_KEY` | Ключ NVIDIA NIM API — Nemotron и открытые модели ([build.nvidia.com](https://build.nvidia.com)) |
| `NVIDIA_BASE_URL` | Переопределить базовый URL-адрес NVIDIA (по умолчанию: `https://integrate.api.nvidia.com/v1`; для локальной конечной точки NIM установлено значение `http://localhost:8000/v1`) |
| `STEPFUN_API_KEY` | API-ключ StepFun — модели серии Step ([platform.stepfun.com](https://platform.stepfun.com)) |
| `STEPFUN_BASE_URL` | Переопределить базовый URL-адрес StepFun (по умолчанию: `https://api.stepfun.com/v1`) |
| `OLLAMA_API_KEY` | Ключ Ollama Cloud API — управляемый каталог Ollama без локального графического процессора ([ollama.com/settings/keys](https://ollama.com/settings/keys)) |
| `OLLAMA_BASE_URL` | Переопределить базовый URL-адрес Ollama Cloud (по умолчанию: `https://ollama.com/v1`) |
| `XAI_API_KEY` | API-ключ xAI (Grok) для чата + TTS + веб-поиск ([console.x.ai](https://console.x.ai/)) |
| `XAI_BASE_URL` | Переопределить базовый URL-адрес xAI (по умолчанию: `https://api.x.ai/v1`) |
| `MISTRAL_API_KEY` | Ключ API Mistral для Voxtral TTS и Voxtral STT ([co

nsole.mistral.ai](https://console.mistral.ai)) |
| `AWS_REGION` | Регион AWS для вывода Bedrock (например, `us-east-1`, `eu-central-1`). Читал boto3. |
| `AWS_PROFILE` | Именованный профиль AWS для аутентификации Bedrock (читается `~/.aws/credentials`). Оставьте значение отключенным, чтобы использовать цепочку учетных данных boto3 по умолчанию. |
| `BEDROCK_BASE_URL` | Переопределить базовый URL-адрес среды выполнения Bedrock (по умолчанию: `https://bedrock-runtime.us-east-1.amazonaws.com`; обычно не настраивайте и вместо этого используйте `AWS_REGION`) |
| `HERMES_QWEN_BASE_URL` | Переопределение базового URL-адреса портала Qwen (по умолчанию: `https://portal.qwen.ai/v1`) |
| `OPENCODE_ZEN_API_KEY` | Ключ OpenCode Zen API — доступ к курируемым моделям с оплатой по мере использования ([opencode.ai](https://opencode.ai/auth)) |
| `OPENCODE_ZEN_BASE_URL` | Переопределить базовый URL-адрес OpenCode Zen |
| `OPENCODE_GO_API_KEY` | Ключ OpenCode Go API — подписка на открытые модели стоимостью 10 долларов США в месяц ([opencode.ai](https://opencode.ai/auth)) |
| `OPENCODE_GO_BASE_URL` | Переопределить базовый URL-адрес OpenCode Go |
| `CLAUDE_CODE_OAUTH_TOKEN` | Явное переопределение токена Claude Code, если вы экспортируете его вручную |
| `HERMES_MODEL` | Переопределить имя модели на уровне процесса (используется планировщиком cron; для обычного использования рекомендуется `config.yaml`) |
| `VOICE_TOOLS_OPENAI_KEY` | Предпочтительный ключ OpenAI для поставщиков услуг преобразования речи в текст и текста в речь OpenAI |
| `HERMES_LOCAL_STT_COMMAND` | Дополнительный шаблон локальной команды преобразования речи в текст. Поддерживает заполнители `{input_path}`, `{output_dir}`, `{language}` и `{model}` |
| `HERMES_LOCAL_STT_LANGUAGE` | Подсказка языка по умолчанию для STT. Используется поставщиком `local` (быстрый шепот), `HERMES_LOCAL_STT_COMMAND`, локальным резервным вариантом CLI `whisper` (по умолчанию: `en`), Groq и xAI, когда в `config.yaml` не задано значение `language` для каждого поставщика |
| `HERMES_HOME` | Переопределить каталог конфигурации Hermes (по умолчанию: `~/.hermes`). Также ограничивается PID-файл шлюза и имя службы systemd, поэтому несколько установок могут выполняться одновременно |
| `HERMES_GIT_BASH_PATH` | **Только для Windows.** Переопределить обнаружение `bash.exe` для инструмента терминала. Указывает на любой bash — полная установка Git для Windows, WSL bash через символическую ссылку, MSYS2, Cygwin. Установщик автоматически устанавливает это значение в предоставленный им PortableGit. См. [Руководство по Windows (родное)](../user-guide/windows-native.md#how-hermes-runs-shell-commands-on-windows) |
| `HERMES_DISABLE_WINDOWS_UTF8` | **Только для Windows.** Установите значение `1`, чтобы отключить прокладку stdio UTF-8 (`configure_windows_stdio()`) и вернуться к кодовой странице локали консоли. Полезно для разделения ошибок кодирования; редко правильная настройка при нормальной работе |
| `HERMES_KANBAN_HOME` | Переопределить общий корень Hermes, к которому привязана доска канбан (база данных + рабочие области + журналы рабочих). Возвращается к `get_default_hermes_root()` (родителю любого активного профиля). Полезно для тестов и необычных развертываний |
| `HERMES_KANBAN_BOARD` | Закрепите активную канбан-доску для этого процесса. Имеет приоритет над `~/.hermes/kanban/current`; диспетчер вводит это в среду рабочего подпроцесса, чтобы рабочие физически не могли видеть задачи на других досках. По умолчанию `default`. Проверка фрагментов: строчные буквы и цифры + дефисы + подчеркивания, 1–64 символа |
| `HERMES_KANBAN_DB` | Закрепите путь к файлу базы данных канбан напрямую (наивысший приоритет; лучше `HERMES_KANBAN_BOARD` и `HERMES_KANBAN_HOME`). Диспетчер вводит это в среду рабочего подпроцесса, чтобы работники профиля сходились на доске диспетчера |
| `HERMES_KANBAN_WORKSPACES_ROOT` | Закрепите корень рабочих пространств канбана напрямую (наивысший приоритет для рабочих пространств; лучше `HERMES_KANBAN_HOME`). Диспетчер вводит это в рабочий подпроцесс env |
| `HERMES_KANBAN_DISPATCH_IN_GATEWAY` | Переопределение времени выполнения для `kanban.dispatch_in_gateway`. Установите значение `0`, `false`, `no` или `off`, чтобы шлюз не запускал встроенный диспетчер Канбана; любое другое непустое значение позволяет это сделать. Полезно, когда доской владеет отдельный процесс-диспетчер. |

## Аутентификация поставщика (OAuth)

Для встроенной аутентификации Anthropic Hermes предпочитает собственные файлы учетных данных Claude Code, если они существуют, поскольку эти учетные данные могут обновляться автоматически. **Для OAuth для Anthropic требуется план Claude Max с купленными дополнительными кредитами на использование** — Hermes маршрутизируется как Claude Code, который использует только дополнительные/избыточные кредиты плана Max, а не базовый лимит Max и не работает на Claude Pro. Без дополнительных кредитов Max + используйте вместо этого ключ API. Переменные среды, такие как `ANTHROPIC_TOKEN`, остаются полезными в качестве ручного переопределения, но они больше не являются предпочтительным путем для входа в систему Claude Max.

| Переменная | Описание |
|----------|-------------|
| `HERMES_PORTAL_BASE_URL` | Переопределить URL-адрес портала Nous (для разработки/тестирования) |
| `NOUS_INFERENCE_BASE_URL` | Переопределить URL-адрес API вывода Nous |
| `HERMES_NOUS_MIN_KEY_TTL_SECONDS` | Минимальный срок жизни ключа агента перед повторным выпуском (по умолчанию: 1800 = 30 минут) |
| `HERMES_NOUS_TIMEOUT_SECONDS` | Тайм-аут HTTP для потоков учетных данных/токенов Nous |
| `HERMES_DUMP_REQUESTS` | Дамп полезных данных запроса API в файлы журналов (`true`/`false`) |
| `HERMES_PREFILL_MESSAGES_FILE` | Путь к JSON-файлу эфемерных сообщений предварительного заполнения, внедряемых во время вызова API |
| `HERMES_TIMEZONE` | Переопределение часового пояса IANA (например, `America/New_York`) |

## API инструментов

| Переменная | Описание |
|----------|-------------|
| `PARALLEL_API_KEY` | Веб-поиск с использованием искусственного интеллекта ([parallel.ai](https://parallel.ai/)) |
| `FIRECRAWL_API_KEY` | Парсинг веб-страниц и облачный браузер ([firecrawl.dev](https://firecrawl.dev/)) |
| `FIRECRAWL_API_URL` | Пользовательская конечная точка API Firecrawl для автономных экземпляров (необязательно) |
| `TAVILY_API_KEY` | Дополнительный ключ API Tavily для более высоких ограничений поиска/извлечения. После выбора Tavily в качестве веб-сервера доступ без ключа работает и без него ([app.tavily.com](https://app.tavily.com/home), [keyless docs](https://docs.tavily.com/documentation/keyless)) |
| `SEARXNG_URL` | URL-адрес экземпляра SearXNG для бесплатного поиска в Интернете на собственном хостинге — ключ API не требуется ([searxng.github.io](https://searxng.github.io/searxng/)) |
| `TAVILY_BASE_URL` | Переопределить конечную точку API Tavily. Полезно для корпоративных прокси и автономных поисковых серверов, совместимых с Tavily. Та же схема, что и `GROQ_BASE_URL`. |
| `EXA_API_KEY` | API-ключ Exa для веб-поиска и контента с использованием искусственного интеллекта ([exa.ai](https://exa.ai/)) |
| `BRAVE_SEARCH_API_KEY` | Токен подписки Brave Search API для веб-поиска (доступен бесплатный уровень) ([brave.com/search/api](https://brave.com/search/api/)) |
| `BROWSERBASE_API_KEY` | Автоматизация браузера ([browserbase.com](https://browserbase.com/)) |
| `BROWSERBASE_PROJECT_ID` | Идентификатор проекта браузерной базы |
| `BROWSER_USE_API_KEY` | Браузер Используйте ключ API облачного браузера ([browser-use.com](https://browser-use.com/)) |
| `FIRECRAWL_BROWSER_TTL` | TTL сеанса браузера Firecrawl в секундах (по умолчанию: 300) |
| `BROWSER_CDP_URL` | URL-адрес протокола Chrome DevTools для локального браузера (устанавливается через `/browser connect`, например `ws://localhost:9222`) |
| `CAMOFOX_URL` | Адрес локального сервера браузера защиты от обнаружения Camofox (по умолчанию: `http://localhost:9377`). Только адрес — Camofox не выбирает в качестве бэкенда; выберите Camofox в `hermes tools` (`browser.cloud_provider: camofox`) |
| `CAMOFOX_API_KEY` | Дополнительный токен носителя, отправленный в качестве заголовка авторизации на удаленный/аутентифицированный сервер Camofox |
| `CAMOFOX_USER_ID` | Дополнительный идентификатор пользователя Camofox, управляемый извне, для общих видимых сеансов |
| `CAMOFOX_SESSION_KEY` | Дополнительный сеансовый ключ Camofox, используемый при создании вкладок для `CAMOFOX_USER_ID` |
| `CAMOFOX_ADOPT_EXISTING_TAB` | Установите значение `true`, чтобы повторно использовать существующую вкладку Camofox перед созданием новой |
| `BROWSER_INACTIVITY_TIMEOUT` | Тайм-аут бездействия сеанса браузера в секундах |
| `AGENT_BROWSER_ARGS` | Дополнительные флаги запуска Chromium (разделенные запятой или новой строкой). Hermes автоматически внедряет `--no-sandbox,--disable-dev-shm-usage` при запуске от имени пользователя root или в пространствах имен непривилегированных пользователей, ограниченных AppArmor (Ubuntu 23.10+, DGX Spark, многие образы контейнеров); установите это вручную только для переопределения или добавления других флагов. |
| `AGENT_BROWSER_ENGINE` | Браузерный движок для локального режима: `auto` (по умолчанию — семейство Chromium через CDP) или переопределение конкретного движка. |
| `FAL_KEY` | Генерация изображений ([fal.ai](https://fal.ai/)) |
| `KREA_API_KEY` | Ключ API Krea для создания изображений Krea 2 ([krea.ai](https://krea.ai/)) |
| `GROQ_API_KEY` | Ключ API Groq Whisper STT ([groq.com](https://groq.com/)) |
| `ELEVENLABS_API_KEY` | Премиальные TTS-голоса ElevenLabs ([elevenlabs.io](https://elevenlabs.io/)) |
| `PORCUPINE_ACCESS_KEY` | Движок пробуждения Picovoice Porcupine ([console.picovoice.ai](https://console.picovoice.ai/)) — только для `wake_word.provider: porcupine`; стандартным механизмам openWakeWord и sherpa ключ не нужен |
| `STT_GROQ_MODEL` | Переопределить модель Groq STT (по умолчанию: `whisper-large-v3-turbo`) |
| `GROQ_BASE_URL` | Переопределить конечную точку STT, совместимую с Groq OpenAI |
| `STT_OPENAI_MODEL` | Переопределить модель OpenAI STT (по умолчанию: `whisper-1`) |
| `STT_OPENAI_BASE_URL` | Переопределить конечную точку STT, совместимую с OpenAI |
| `GITHUB_TOKEN` | Токен GitHub для Skills Hub (более высокие ограничения скорости API, публикация навыков) |
| `HONCHO_API_KEY` | Межсессионное моделирование пользователей ([honcho.dev](https://honcho.dev/)) |
| `HONCHO_BASE_URL` | Базовый URL-адрес для самостоятельных экземпляров Honcho (по умолчанию: облако Honcho). Для локальных экземпляров ключ API не требуется |
| `HINDSIGHT_API_KEY` | Ключ Hindsight API для постоянной памяти с поддержкой графов ([hindsight.vectorize.io](https://hindsight.vectorize.io)) |
| `HINDSIGHT_API_URL` | Базовый URL-адрес Hindsight API (по умолчанию: `https://api.hindsight.vectorize.io`) |
| `HINDSIGHT_TIMEOUT` | Тайм-аут в секундах для вызовов API поставщика памяти Hindsight (по умолчанию: `60`). Используйте это, если ваш экземпляр Hindsight медленно отвечает во время `/sync` или `on_session_switch`, и вы

видим тайм-ауты в `errors.log`. |
| `MEM0_API_KEY` | Ключ API платформы Mem0 для семантической постоянной памяти ([app.mem0.ai](https://app.mem0.ai)) |
| `MEM0_MODE` | Серверный режим Mem0: `platform` (по умолчанию) или `oss` — см. [Поставщики памяти](/user-guide/features/memory-providers) |
| `MEM0_HOST` | Базовый URL-адрес автономного сервера Mem0 (отключает плагин от Platform API) |
| `MEM0_USER_ID` | Переопределить идентификатор пользователя. Воспоминания Mem0 хранятся в |
| `MEM0_AGENT_ID` | Переопределить идентификатор агента. Воспоминания Mem0 помечены |
| `RETAINDB_API_KEY` | Ключ API RetainDB для постоянной памяти ([retaindb.com](https://retaindb.com)) |
| `RETAINDB_BASE_URL` | Базовый URL-адрес для локальных экземпляров RetainDB (по умолчанию: `https://api.retaindb.com`) |
| `OPENVIKING_API_KEY` | Ключ API OpenViking (оставьте пустым для локального режима разработки) |
| `OPENVIKING_ENDPOINT` | URL-адрес сервера OpenViking (по умолчанию: `http://127.0.0.1:1933`) |
| `BRV_API_KEY` | Ключ API ByteRover (необязательно, для облачной синхронизации — по умолчанию сначала локальный) ([app.byterover.dev](https://app.byterover.dev)) |
| `SUPERMEMORY_API_KEY` | Семантическая долговременная память с вызовом профиля и захватом сеанса ([supermemory.ai](https://supermemory.ai)) |
| `DAYTONA_API_KEY` | Облачные песочницы Daytona ([daytona.io](https://daytona.io/)) |
| `VERCEL_TOKEN` | Токен доступа к Vercel Sandbox ([vercel.com](https://vercel.com/)) |
| `VERCEL_PROJECT_ID` | Идентификатор проекта Vercel (обязательно для `VERCEL_TOKEN`) |
| `VERCEL_TEAM_ID` | Идентификатор команды Vercel (обязательно для `VERCEL_TOKEN`) |
| `VERCEL_OIDC_TOKEN` | Недолговечный токен Vercel OIDC (альтернатива только для разработки) |

### Ключи API навыков

Секреты, используемые определенными встроенными/дополнительными навыками. Каждый нужен только в том случае, если вы используете соответствующий навык.

| Переменная | Используется умением | Описание |
|----------|---------------|-------------|
| `NOTION_API_KEY` | `notion` | Токен интеграции понятия. |
| `LINEAR_API_KEY` | `linear` | Линейный персональный API-ключ. |
| `AIRTABLE_API_KEY` | `airtable` | Токен личного доступа Airtable. |
| `TENOR_API_KEY` | `gif-search` | Ключ Tenor API для поиска GIF. |

### Наблюдаемость Лангфуза

Переменные среды для встроенного плагина [`observability/langfuse`](/user-guide/features/built-in-plugins#observabilitylangfuse). Установите их в `~/.hermes/.env`. Плагин также должен быть включен (`hermes plugins enable observability/langfuse` или установите флажок в `hermes plugins`), прежде чем какое-либо из этих условий вступит в силу.

| Переменная | Описание |
|----------|-------------|
| `HERMES_LANGFUSE_PUBLIC_KEY` | Открытый ключ проекта Langfuse (`pk-lf-...`). Необходимый. |
| `HERMES_LANGFUSE_SECRET_KEY` | Секретный ключ проекта Langfuse (`sk-lf-...`). Необходимый. |
| `HERMES_LANGFUSE_BASE_URL` | URL-адрес сервера Langfuse (по умолчанию: `https://cloud.langfuse.com`). Набор для самостоятельного размещения. |
| `HERMES_LANGFUSE_ENV` | Тег среды в трассировках (`production`, `staging`, …) |
| `HERMES_LANGFUSE_RELEASE` | Тег выпуска/версии на трассировках |
| `HERMES_LANGFUSE_SAMPLE_RATE` | Частота выборки SDK 0,0–1,0 (по умолчанию: `1.0`) |
| `HERMES_LANGFUSE_MAX_CHARS` | Усечение по полю для сериализованных полезных данных (по умолчанию: `12000`) |
| `HERMES_LANGFUSE_DEBUG` | `true` включает подробную регистрацию плагинов в `agent.log` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | Стандартные имена Langfuse SDK. Принимается как запасной вариант, если не установлены эквиваленты `HERMES_LANGFUSE_*`. |

### Шлюз инструментов Nous

Эти переменные настраивают [Tool Gateway](/user-guide/features/tool-gateway) для платных подписчиков Nous или развертываний самостоятельного шлюза. Большинству пользователей не требуется их устанавливать — шлюз настраивается автоматически через `hermes model` или `hermes tools`.

| Переменная | Описание |
|----------|-------------|
| `TOOL_GATEWAY_DOMAIN` | Базовый домен для маршрутизации Tool Gateway (по умолчанию: `nousresearch.com`) |
| `TOOL_GATEWAY_SCHEME` | Схема HTTP или HTTPS для URL-адресов шлюза (по умолчанию: `https`) |
| `TOOL_GATEWAY_USER_TOKEN` | Токен аутентификации для Tool Gateway (обычно заполняется автоматически при аутентификации Nous) |
| `FIRECRAWL_GATEWAY_URL` | Переопределить URL-адрес специально для конечной точки шлюза Firecrawl |

## Серверная часть терминала

| Переменная | Описание |
|----------|-------------|
| `TERMINAL_ENV` | Серверная часть: `local`, `docker`, `ssh`, `singularity`, `modal`, `daytona`, `vercel_sandbox` |
| `HERMES_DOCKER_BINARY` | Переопределить двоичный контейнер контейнера, который использует оболочка Hermes (например, `podman`, `/usr/local/bin/docker`). Если этот параметр отключен, Hermes автоматически обнаруживает `docker` или `podman` на `PATH`. Требуется, когда оба установлены и вам нужен вариант, отличный от значения по умолчанию, или когда двоичный файл находится за пределами `PATH`. |
| `TERMINAL_DOCKER_IMAGE` | Образ Docker (по умолчанию: `nikolaik/python-nodejs:python3.11-nodejs20`) |
| `TERMINAL_DOCKER_FORWARD_ENV` | JSON-массив имен переменных окружения для явной пересылки в сеансы терминала Docker. Примечание. `required_environment_variables`, объявленные навыком, пересылаются автоматически — это нужно только для переменных, не объявленных каким-либо навыком. |
| `TERMINAL_DOCKER_VOLUMES` | Дополнительные тома Docker монтируются (пары `host:container`, разделенные запятыми) |
| `TERMINAL_DOCKER_ENV` | JSON дополнительных переменных окружения для установки внутри сеансов терминала Docker (например, `{"FOO":"bar"}`) |
| `TERMINAL_DOCKER_EXTRA_ARGS` | JSON-массив дополнительных аргументов `docker run` (например, `["--memory","4g"]`) |
| `TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE` | Расширенное согласие: смонтируйте cwd запуска в Docker `/workspace` (`true`/`false`, по умолчанию: `false`) |
| `TERMINAL_SINGULARITY_IMAGE` | Изображение сингулярности или путь `.sif` |
| `TERMINAL_MODAL_IMAGE` | Модальное изображение контейнера |
| `TERMINAL_DAYTONA_IMAGE` | Изображение песочницы Daytona |
| `TERMINAL_VERCEL_RUNTIME` | Среда выполнения Vercel Sandbox (`node24`, `node22`, `python3.13`) |
| `TERMINAL_TIMEOUT` | Таймаут команды в секундах |
| `TERMINAL_LIFETIME_SECONDS` | Максимальное время жизни терминальных сессий в секундах |
| `TERMINAL_CWD` | Устарело прямое переопределение сеансов терминала шлюза/хрона. Предпочитайте `terminal.cwd` в `config.yaml`; CLI по-прежнему использует каталог запуска. |
| `SUDO_PASSWORD` | Включить sudo без интерактивной подсказки |

Для серверных частей облачной песочницы постоянство ориентировано на файловую систему. `TERMINAL_LIFETIME_SECONDS` контролирует, когда Hermes очищает бездействующий сеанс терминала, а последующие возобновления могут воссоздать песочницу, а не поддерживать те же самые активные процессы.

## SSH-бэкэнд

| Переменная | Описание |
|----------|-------------|
| `TERMINAL_SSH_HOST` | Имя хоста удаленного сервера |
| `TERMINAL_SSH_USER` | Имя пользователя SSH |
| `TERMINAL_SSH_PORT` | SSH-порт (по умолчанию: 22) |
| `TERMINAL_SSH_KEY` | Путь к закрытому ключу |
| `TERMINAL_SSH_PERSISTENT` | Переопределить постоянную оболочку для SSH (по умолчанию: соответствует `TERMINAL_PERSISTENT_SHELL`) |

## Ресурсы контейнеров (Docker, Singularity, Modal, Daytona)

| Переменная | Описание |
|----------|-------------|
| `TERMINAL_CONTAINER_CPU` | Ядра ЦП (по умолчанию: 1) |
| `TERMINAL_CONTAINER_MEMORY` | Память в МБ (по умолчанию: 5120) |
| `TERMINAL_CONTAINER_DISK` | Диск в МБ (по умолчанию: 51200) |
| `TERMINAL_CONTAINER_PERSISTENT` | Сохранять файловую систему контейнера между сеансами (по умолчанию: `true`) |
| `TERMINAL_SANDBOX_DIR` | Каталог хоста для рабочих областей и наложений (по умолчанию: `~/.hermes/sandboxes/`) |

## Постоянная оболочка

| Переменная | Описание |
|----------|-------------|
| `TERMINAL_PERSISTENT_SHELL` | Включить постоянную оболочку для нелокальных серверных частей (по умолчанию: `true`). Также можно установить через `terminal.persistent_shell` в config.yaml |
| `TERMINAL_LOCAL_PERSISTENT` | Включить постоянную оболочку для локального бэкэнда (по умолчанию: `false`) |
| `TERMINAL_SSH_PERSISTENT` | Переопределить постоянную оболочку для серверной части SSH (по умолчанию: соответствует `TERMINAL_PERSISTENT_SHELL`) |

## Выходной прокси (внедренный в песочницу)

Эти переменные окружения НЕ устанавливаются на хосте — они вводятся в песочницы Docker посредством интеграции [Egress proxy](../user-guide/egress/iron-proxy.md) при `proxy.enabled: true`. Docker — единственный проводной сервер в этом выпуске.

| Переменная | Описание |
|----------|-------------|
| `HERMES_EGRESS_PROXY` | Установите значение `1` внутри песочницы, когда выходной прокси-сервер активен. Код агента может проверить это, чтобы узнать, что он работает за прокси-сервером, перехватывающим TLS. |
| Переменные среды поставщика (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, …) | Установите непрозрачные прокси-токены, а не настоящие секреты восходящего потока, чтобы существующие SDK продолжали читать стандартные имена env. Iron-proxy меняет эти токены на настоящий восходящий секрет на границе сети. |
| `HERMES_PROXY_TOKEN_<ENV_NAME>` | Диагностический псевдоним для каждого созданного сопоставления поставщика. Например. `HERMES_PROXY_TOKEN_OPENROUTER_API_KEY=hermes-proxy-openrouter-…`. То же значение токена, что и у стандартного поставщика env var. |
| `HTTPS_PROXY` / `HTTP_PROXY` | `HTTPS_PROXY` указывает на `http://host.docker.internal:<tunnel_port>` для CONNECT/MITM. `HTTP_PROXY` указывает на `<tunnel_port + 1>` для пересылки по простому HTTP. |
| `NO_PROXY` | `127.0.0.1,localhost,::1` поэтому серверы разработки обратной связи внутри песочницы обходят прокси. |
| `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS` | Путь к смонтированному выходному сертификату CA Hermes внутри песочницы (`/etc/ssl/certs/hermes-egress-ca.crt`). Позволяет средам выполнения языка доверять листовым сертификатам MITM, созданным железным прокси. |
| `NODE_OPTIONS` | Добавлено с помощью `--use-openssl-ca` (ваши существующие флаги сохраняются), поэтому маршруты Node.js через OpenSSL сохраняют другой элемент управления переменными CA-пакета. Сужает [предупреждение об асимметричном CA Node.js](../user-guide/egress/iron-proxy.md#nodejs-asymmetric-ca-caveat). |
| `HERMES_IRON_PROXY_NONCE` | Устанавливается на самом процессе демона железного прокси (НЕ внутри песочницы). Используется `_pid_alive` для подтверждения того, что PID-кандидат по-прежнему ссылается на *наш* управляемый двоичный файл при переработке PID. |

Они устанавливаются автоматически серверной частью терминала Docker, когда `proxy.enabled: true` И запущен демон. Вы не устанавливаете их сами; соответствующие кнопки управления оператором находятся в `~/.hermes/config.yaml` в разделе `proxy:` — см. [Выходной прокси → Конфигурация](../user-guide/egress/iron-proxy.md#configuration).

## Сообщения

| Переменная | Описание |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота (от @BotFather) |
| `TELEGRAM_ALLOWED_USERS` | Идентификаторы пользователей, разделенные запятыми, которым разрешено использовать бота (применяется к личным сообщениям, группам и форумам) |
| `TELEGRAM_ALLOW_ALL_USERS` | Разрешить любому пользователю Telegram запускать бота (только для разработчиков). |
| `TELEGRAM_GROUP_ALLOWED_USERS` | Идентификаторы пользователей-отправителей, разделенные запятыми, авторизованные только в группах/форумах (НЕ предоставляют доступ к DM). Значения в форме идентификатора чата (начиная с `-`) по-прежнему учитываются как идентификаторы чата для обратной совместимости с конфигурациями до версии #17686 с предупреждением об устаревании. |
| `TELEGRAM_GROUP_ALLOWED_CHATS` | Идентификаторы групповых/форумных чатов, разделенные запятыми; любой участник имеет право |
| `TELEGRAM_HOME_CHANNEL` | Чат/канал Telegram по умолчанию для доставки cron |
| `TELEGRAM_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала Telegram |
| `TELEGRAM_CRON_THREAD_ID` | Идентификатор темы форума для получения поставок cron; переопределяет `TELEGRAM_HOME_CHANNEL_THREAD_ID` только для cron. Используйте в режиме темы, чтобы ответы на сообщения cron открывали новый сеанс вместо входа в системное лобби (#24409). |
| `TELEGRAM_WEBHOOK_URL` | Публичный URL-адрес HTTPS для режима веб-перехватчика (включает веб-перехватчик вместо опроса) |
| `TELEGRAM_WEBHOOK_PORT` | Локальный порт прослушивания для сервера веб-перехватчиков (по умолчанию: `8443`) |
| `TELEGRAM_WEBHOOK_SECRET` | Секретный токен Telegram возвращается в каждом обновлении для проверки. **Требуется всякий раз, когда установлен `TELEGRAM_WEBHOOK_URL`** — без него шлюз отказывается запускаться (GHSA-3vpc-7q5r-276h). Создайте с помощью `openssl rand -hex 32`. |
| `TELEGRAM_REACTIONS` | Включить реакцию эмодзи на сообщения во время обработки (по умолчанию: `false`) |
| `TELEGRAM_REQUIRE_MENTION` | Требуйте явного триггера, прежде чем отвечать в группах Telegram. Эквивалент `telegram.require_mention` в `config.yaml`. |
| `TELEGRAM_MENTION_PATTERNS` | Массив JSON, список, разделенный новой строкой, или список, разделенный запятыми, шаблонов пробуждающих слов регулярных выражений, принимаемых, когда включено ограничение упоминаний группы Telegram. Эквивалент `telegram.mention_patterns`. |
| `TELEGRAM_EXCLUSIVE_BOT_MENTIONS` | Если этот параметр включен, явные упоминания `@...bot` в группах Telegram направляются только к упомянутым именам пользователей ботов, прежде чем будут запущены резервные варианты ответа или пробуждающего слова. По умолчанию: `true`. Эквивалент `telegram.exclusive_bot_mentions`. |
| `TELEGRAM_REPLY_TO_MODE` | Поведение ссылки на ответ: `off`, `first` (по умолчанию) или `all`. Соответствует шаблону Discord. |
| `TELEGRAM_IGNORED_THREADS` | Разделенные запятыми идентификаторы тем/обсуждений форума Telegram, на которые бот никогда не отвечает |
| `TELEGRAM_PROXY` | URL-адрес прокси-сервера для подключений Telegram — переопределяет `HTTPS_PROXY`. Поддерживает `http://`, `https://`, `socks5://` |
| `DISCORD_BOT_TOKEN` | Токен бота Discord |
| `DISCORD_ALLOWED_USERS` | Идентификаторы пользователей Discord, разделенные запятыми, которым разрешено использовать бота |
| `DISCORD_ALLOW_ALL_USERS` | Разрешить любому пользователю Discord запускать бота (только для разработчиков). |
| `DISCORD_ALLOWED_ROLES` | Идентификаторы ролей Discord, разделенные запятыми, разрешены для использования бота (ИЛИ с `DISCORD_ALLOWED_USERS`). Автоматически включает намерение участников. Полезно, когда команды модераторов отменяются — права на роли распространяются автоматически. |
| `DISCORD_ALLOWED_CHANNELS` | Идентификаторы каналов Discord, разделенные запятыми. Если установлено, бот отвечает только по этим каналам (плюс DM, если это разрешено). Переопределяет `config.yaml` `discord.allowed_channels`. |
| `DISCORD_PROXY` | URL-адрес прокси-сервера для подключений Discord — переопределяет `HTTPS_PROXY`. Поддерживает `http://`, `https://`, `socks5://` |
| `DISCORD_HOME_CHANNEL` | Канал Discord по умолчанию для доставки cron |
| `DISCORD_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала Discord |
| `DISCORD_COMMAND_SYNC_POLICY` | Политика синхронизации при запуске Discord с использованием косой черты: `safe` (различие и согласование), `bulk` (устаревший `tree.sync()`) или `off` |
| `DISCORD_REQUIRE_MENTION` | Требовать @упоминание перед ответом в каналах сервера |
| `DISCORD_FREE_RESPONSE_CHANNELS` | Идентификаторы каналов, разделенные запятыми, где упоминание не требуется |
| `DISCORD_AUTO_THREAD` | Автоматическая подача длинных ответов, если это поддерживается |
| `DISCORD_ALLOW_ANY_ATTACHMENT` | Если `true`, принимайте вложения любого типа файлов (а не только встроенного белого списка PDF/text/zip/office). Неизвестные типы кэшируются и предоставляются агенту как локальный путь, чтобы он мог проверить их через `terminal` / `read_file` / `ffprobe`. По умолчанию `false`. |
| `DISCORD_MAX_ATTACHMENT_BYTES` | Максимальное количество байтов на вложение, которое шлюз будет кэшировать. По умолчанию `33554432` (32 МБ). Установите значение `0` для отсутствия ограничения (вложения сохраняются в памяти во время записи). |
| `DISCORD_REACTIONS` | Включить реакцию эмодзи на сообщения во время обработки (по умолчанию: `true`) |
| `DISCORD_IGNORED_CHANNELS` | Идентификаторы каналов, разделенные запятыми.

прежде чем бот никогда не ответит |
| `DISCORD_NO_THREAD_CHANNELS` | Идентификаторы каналов, разделенные запятыми, на которые бот отвечает без автоматической обработки |
| `DISCORD_REPLY_TO_MODE` | Поведение ссылки на ответ: `off`, `first` (по умолчанию) или `all` |
| `DISCORD_ALLOW_MENTION_EVERYONE` | Разрешить боту пинговать `@everyone`/`@here` (по умолчанию: `false`). См. [Контроль упоминаний](../user-guide/messaging/discord.md#mention-control). |
| `DISCORD_ALLOW_MENTION_ROLES` | Разрешить боту проверять упоминания `@role` (по умолчанию: `false`). |
| `DISCORD_ALLOW_MENTION_USERS` | Разрешить боту проверять отдельные упоминания `@user` (по умолчанию: `true`). |
| `DISCORD_ALLOW_MENTION_REPLIED_USER` | Отправьте пинг автору при ответе на его сообщение (по умолчанию: `true`). |
| `SLACK_BOT_TOKEN` | Токен Slack-бота (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Токен уровня приложения Slack (`xapp-...`, требуется для режима сокетов) |
| `SLACK_ALLOWED_USERS` | Идентификаторы пользователей Slack, разделенные запятыми |
| `SLACK_ALLOW_ALL_USERS` | Разрешить любому пользователю Slack запускать бота (только для разработчиков). |
| `SLACK_ALLOW_BOTS` | Принимать сообщения от других ботов Slack: `none` (по умолчанию), `mentions` или `all`. Бот всегда игнорирует собственные сообщения. |
| `SLACK_THREAD_REQUIRE_MENTION` | Требовать явного @упоминания для ответов в цепочках Slack, сохраняя при этом каналы с бесплатными ответами верхнего уровня |
| `SLACK_HOME_CHANNEL` | Канал Slack по умолчанию для доставки cron |
| `SLACK_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала Slack |
| `GOOGLE_CHAT_PROJECT_ID` | Проект GCP, в котором размещена тема Pub/Sub (возврат к `GOOGLE_CLOUD_PROJECT`) |
| `GOOGLE_CHAT_SUBSCRIPTION_NAME` | Полный путь подписки на Pub/Sub, `projects/{proj}/subscriptions/{sub}` (старый псевдоним: `GOOGLE_CHAT_SUBSCRIPTION`) |
| `GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` | Путь к JSON сервисного аккаунта или встроенный JSON (возврат к `GOOGLE_APPLICATION_CREDENTIALS`) |
| `GOOGLE_CHAT_ALLOWED_USERS` | Электронные адреса пользователей, разделенные запятыми, разрешены для общения с ботом |
| `GOOGLE_CHAT_ALLOW_ALL_USERS` | Разрешить любому пользователю Google Chat запускать бота (только для разработчиков) |
| `GOOGLE_CHAT_HOME_CHANNEL` | Пространство по умолчанию (например, `spaces/AAAA...`) для доставки cron |
| `GOOGLE_CHAT_HOME_CHANNEL_NAME` | Отображаемое имя главного пространства Google Chat |
| `GOOGLE_CHAT_MAX_MESSAGES` | Максимальное количество сообщений Pub/Sub FlowControl (по умолчанию: `1`) |
| `GOOGLE_CHAT_MAX_BYTES` | Максимальное количество байт Pub/Sub FlowControl (по умолчанию: `16777216`, 16 МБ) |
| `GOOGLE_CHAT_BOOTSTRAP_SPACES` | Дополнительные идентификаторы, разделенные запятыми, которые необходимо проверять при запуске при разрешении собственного `users/{id}` бота |
| `GOOGLE_CHAT_DEBUG_RAW` | Установите любое значение для регистрации отредактированных конвертов Pub/Sub на уровне DEBUG (только отладка) |
| `GOOGLE_CHAT_HTTP_EVENTS_URL` | Конечная точка HTTP с аутентификацией для событий сообщений чата (альтернатива Pub/Sub) |
| `GOOGLE_CHAT_HTTP_EVENTS_AUDIENCE` | Ожидаемая аудитория для токенов носителя событий HTTP, подписанных Google (по умолчанию `GOOGLE_CHAT_HTTP_EVENTS_URL`) |
| `GOOGLE_CHAT_HTTP_EVENTS_SERVICE_ACCOUNT_EMAIL` | Ожидаемый адрес электронной почты учетной записи службы Google для токенов носителя событий HTTP |
| `WHATSAPP_ENABLED` | Включить мост WhatsApp (`true`/`false`) |
| `WHATSAPP_MODE` | `bot` (отдельный номер) или `self-chat` (напишите сами) |
| `WHATSAPP_ALLOWED_USERS` | Номера телефонов, разделенные запятыми (с кодом страны, без `+`) или `*`, чтобы разрешить всем отправителям |
| `WHATSAPP_ALLOW_ALL_USERS` | Разрешить всем отправителям WhatsApp без белого списка (`true`/`false`) |
| `WHATSAPP_HOME_CHANNEL` | Идентификатор чата по умолчанию для cron/доставки уведомлений. |
| `WHATSAPP_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала WhatsApp. |
| `WHATSAPP_DEBUG` | Записывать события необработанных сообщений на мосту для устранения неполадок (`true`/`false`) |
| `WHATSAPP_CLOUD_PHONE_NUMBER_ID` | Мета-идентификатор номера телефона из API WhatsApp Business Cloud (15–17 цифр; **не** сам номер телефона) |
| `WHATSAPP_CLOUD_ACCESS_TOKEN` | Мета-токен доступа (начинается с `EAA`); срок действия временных токенов истекает через 24 часа, токены пользователей системы являются постоянными |
| `WHATSAPP_CLOUD_APP_SECRET` | Шестнадцатеричный секрет приложения из 32 символов, используемый для проверки подписей входящих веб-перехватчиков |
| `WHATSAPP_CLOUD_VERIFY_TOKEN` | Общий секрет для рукопожатия проверки веб-перехватчика Meta (автоматически создается мастером установки) |
| `WHATSAPP_CLOUD_ALLOWED_USERS` | Разделенные запятыми `wa_id` (номера телефонов с кодом страны, без `+`), разрешенные для отправки сообщений боту |
| `WHATSAPP_CLOUD_ALLOW_ALL_USERS` | Разрешить всем отправителям WhatsApp Cloud без белого списка (`true`/`false`) |
| `WHATSAPP_CLOUD_APP_ID` | Необязательный мета-идентификатор приложения (для будущей интеграции аналитики) |
| `WHATSAPP_CLOUD_WABA_ID` | Дополнительный идентификатор учетной записи WhatsApp Business (для будущей интеграции аналитики) |
| `WHATSAPP_CLOUD_WEBHOOK_HOST` | Интерфейс, к которому привязывается входящий сервер веб-перехватчика (по умолчанию `0.0.0.0`) |
| `WHATSAPP_CLOUD_WEBHOOK_PORT` | Порт, к которому привязывается входящий сервер веб-перехватчика (по умолчанию _

_PH_592__) |
| `WHATSAPP_CLOUD_WEBHOOK_PATH` | URL-путь Мета отправляет входящие сообщения на (по умолчанию `/whatsapp/webhook`) |
| `WHATSAPP_CLOUD_API_VERSION` | Версия Meta Graph API для вызова (по умолчанию `v20.0`) |
| `WHATSAPP_CLOUD_HOME_CHANNEL` | `wa_id` для использования в качестве домашнего канала бота (для заданий cron и т. д.) |
| `WHATSAPP_CLOUD_DM_POLICY` | стробирование DM для облачного адаптера (`open`/`allowlist`/`disabled`); возвращается к `WHATSAPP_DM_POLICY`, если не установлено |
| `WHATSAPP_CLOUD_ALLOW_FROM` | Отправители, разделенные запятыми, разрешены, когда `dm_policy: allowlist` (пустые `wa_id`; JID в стиле Baileys нормализованы) |
| `WHATSAPP_CLOUD_GROUP_POLICY` | Групповой шлюз для облачного адаптера (`open`/`allowlist`/`disabled`); возвращается к `WHATSAPP_GROUP_POLICY`, если не установлено |
| `WHATSAPP_CLOUD_GROUP_ALLOW_FROM` | Идентификаторы группового чата, разделенные запятыми, разрешены, если `group_policy: allowlist` |
| `SIGNAL_HTTP_URL` | Конечная точка HTTP демона signal-cli (например, `http://127.0.0.1:8080`) |
| `SIGNAL_ACCOUNT` | Номер телефона бота в формате E.164 |
| `SIGNAL_ALLOWED_USERS` | Номера телефонов E.164 или UUID, разделенные запятыми |
| `SIGNAL_GROUP_ALLOWED_USERS` | Идентификаторы групп, разделенные запятыми, или `*` для всех групп |
| `SIGNAL_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала Signal |
| `SIGNAL_IGNORE_STORIES` | Игнорировать истории сигналов/обновления статуса |
| `SIGNAL_ALLOW_ALL_USERS` | Разрешить всем пользователям Signal без белого списка |
| `TWILIO_ACCOUNT_SID` | SID учетной записи Twilio (совместно с навыком телефонии) |
| `TWILIO_AUTH_TOKEN` | Токен аутентификации Twilio (совместно с навыком телефонии; также используется для проверки подписи веб-перехватчика) |
| `TWILIO_PHONE_NUMBER` | Номер телефона Twilio в формате E.164 (совместно с навыком телефонии) |
| `SMS_WEBHOOK_URL` | Публичный URL-адрес для проверки подписи Twilio — должен совпадать с URL-адресом веб-перехватчика в консоли Twilio (обязательно) |
| `SMS_WEBHOOK_PORT` | Порт прослушивателя Webhook для входящих SMS (по умолчанию: `8080`) |
| `SMS_WEBHOOK_HOST` | Адрес привязки вебхука (по умолчанию: `127.0.0.1`) |
| `SMS_INSECURE_NO_SIGNATURE` | Установите значение `true`, чтобы отключить проверку подписи Twilio (только для локальной разработки — не для рабочей среды) |
| `SMS_ALLOWED_USERS` | Телефонные номера E.164, разделенные запятыми, разрешены для общения в чате |
| `SMS_ALLOW_ALL_USERS` | Разрешить всем отправителям SMS без белого списка |
| `SMS_HOME_CHANNEL` | Номер телефона для заданий cron/доставки уведомлений |
| `SMS_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала SMS |
| `EMAIL_ADDRESS` | Адрес электронной почты адаптера шлюза электронной почты |
| `EMAIL_PASSWORD` | Пароль или пароль приложения для учетной записи электронной почты |
| `EMAIL_IMAP_HOST` | Имя хоста IMAP для адаптера электронной почты |
| `EMAIL_IMAP_PORT` | IMAP-порт |
| `EMAIL_SMTP_HOST` | Имя хоста SMTP для адаптера электронной почты |
| `EMAIL_SMTP_PORT` | SMTP-порт |
| `EMAIL_ALLOWED_USERS` | Адреса электронной почты, разделенные запятыми, разрешенные для отправки сообщений боту |
| `EMAIL_HOME_ADDRESS` | Получатель по умолчанию для упреждающей доставки электронной почты |
| `EMAIL_HOME_ADDRESS_NAME` | Отображаемое имя для домашней цели электронной почты |
| `EMAIL_POLL_INTERVAL` | Интервал опроса электронной почты в секундах |
| `EMAIL_ALLOW_ALL_USERS` | Разрешить всем отправителям входящей электронной почты |
| `DINGTALK_CLIENT_ID` | Бот DingTalk AppKey с портала разработчиков ([open.dingtalk.com](https://open.dingtalk.com)) |
| `DINGTALK_CLIENT_SECRET` | Бот DingTalk AppSecret с портала разработчиков |
| `DINGTALK_ALLOWED_USERS` | Идентификаторы пользователей DingTalk, разделенные запятыми, которым разрешено отправлять сообщения боту |
| `DINGTALK_WEBHOOK_URL` | Статический URL-адрес веб-перехватчика робота для кросс-платформенной доставки/доставки cron. |
| `DINGTALK_HOME_CHANNEL` | Идентификатор диалога по умолчанию для cron/доставки уведомлений. |
| `DINGTALK_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала DingTalk. |
| `FEISHU_APP_ID` | Идентификатор приложения бота Feishu/Lark от [open.feishu.cn](https://open.feishu.cn/) |
| `FEISHU_APP_SECRET` | Секрет приложения бота Feishu/Lark |
| `FEISHU_DOMAIN` | `feishu` (Китай) или `lark` (международный). По умолчанию: `feishu` |
| `FEISHU_CONNECTION_MODE` | `websocket` (рекомендуется) или `webhook`. По умолчанию: `websocket` |
| `FEISHU_ENCRYPT_KEY` | Дополнительный ключ шифрования для режима веб-перехватчика |
| `FEISHU_VERIFICATION_TOKEN` | Дополнительный токен проверки для режима веб-перехватчика |
| `FEISHU_ALLOWED_USERS` | Идентификаторы пользователей Feishu, разделенные запятыми, разрешены для отправки сообщений боту |
| `FEISHU_ALLOW_BOTS` | `none` (по умолчанию) / `mentions` / `all` — принимать входящие сообщения от других ботов. См. [Обмен сообщениями между ботами](../user-guide/messaging/feishu.md#bot-to-bot-messaging) |
| `FEISHU_REQUIRE_MENTION` | `true` (по умолчанию) / `false` — должны ли групповые сообщения @упоминать бота. Переопределить для каждого чата через `group_rules.<chat_id>.require_mention`. |
| `FEISHU_HOME_CHANNEL` | Идентификатор чата Feishu для доставки cron и уведомлений |
| `FEISHU_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала Feishu. |
| `FEISHU_ALLOW_ALL_USERS` | Разрешить любой фейш

Пользователь, который запускает бота (только для разработчиков). |
| `WECOM_BOT_ID` | Идентификатор WeCom AI Bot из консоли администратора |
| `WECOM_SECRET` | Секрет WeCom AI Bot |
| `WECOM_WEBSOCKET_URL` | Пользовательский URL-адрес WebSocket (по умолчанию: `wss://openws.work.weixin.qq.com`) |
| `WECOM_ALLOWED_USERS` | Идентификаторы пользователей WeCom, разделенные запятыми, которым разрешено отправлять сообщения боту |
| `WECOM_HOME_CHANNEL` | Идентификатор чата WeCom для доставки cron и уведомлений |
| `WECOM_CALLBACK_CORP_ID` | WeCom Enterprise Corp ID для самостоятельного приложения обратного вызова |
| `WECOM_CALLBACK_CORP_SECRET` | Корпоративный секрет собственного приложения |
| `WECOM_CALLBACK_AGENT_ID` | Идентификатор агента собственного приложения |
| `WECOM_CALLBACK_TOKEN` | Токен подтверждения обратного вызова |
| `WECOM_CALLBACK_ENCODING_AES_KEY` | Ключ AES для шифрования обратного вызова |
| `WECOM_CALLBACK_HOST` | Адрес привязки сервера обратного вызова (по умолчанию: `0.0.0.0`) |
| `WECOM_CALLBACK_PORT` | Порт сервера обратного вызова (по умолчанию: `8645`) |
| `WECOM_CALLBACK_ALLOWED_USERS` | Идентификаторы пользователей, разделенные запятыми, для белого списка |
| `WECOM_CALLBACK_ALLOW_ALL_USERS` | Установите `true`, чтобы разрешить всем пользователям без белого списка |
| `WEIXIN_ACCOUNT_ID` | Идентификатор учетной записи Weixin, полученный посредством входа в систему QR через API iLink Bot |
| `WEIXIN_TOKEN` | Токен аутентификации Weixin, полученный посредством входа в систему QR через API iLink Bot |
| `WEIXIN_BASE_URL` | Переопределить базовый URL-адрес Weixin iLink Bot API (по умолчанию: `https://ilinkai.weixin.qq.com`) |
| `WEIXIN_CDN_BASE_URL` | Переопределить базовый URL-адрес Weixin CDN для мультимедиа (по умолчанию: `https://novac2c.cdn.weixin.qq.com/c2c`) |
| `WEIXIN_DM_POLICY` | Политика прямых сообщений: `open`, `allowlist`, `pairing`, `disabled` (по умолчанию: `open`) |
| `WEIXIN_GROUP_POLICY` | Политика групповых сообщений: `open`, `allowlist`, `disabled` (по умолчанию: `disabled`) |
| `WEIXIN_ALLOWED_USERS` | Идентификаторы пользователей Weixin, разделенные запятыми, позволяют управлять ботом |
| `WEIXIN_GROUP_ALLOWED_USERS` | Разделенные запятыми **идентификаторы группового чата Weixin** (не идентификаторы пользователей-участников) позволяют взаимодействовать с ботом. Имя переменной устаревшее — оно ожидает идентификаторы групп. Вступает в силу только тогда, когда iLink действительно доставляет групповые события; Идентификаторы ботов iLink с QR-входом (`...@im.bot`) обычно не получают обычные групповые сообщения WeChat. |
| `WEIXIN_HOME_CHANNEL` | Идентификатор чата Weixin для доставки cron и уведомлений |
| `WEIXIN_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала Weixin |
| `WEIXIN_ALLOW_ALL_USERS` | Разрешить всем пользователям Weixin без белого списка (`true`/`false`) |
| `BLUEBUBBLES_SERVER_URL` | URL-адрес сервера BlueBubbles (например, `http://192.168.1.10:1234`) |
| `BLUEBUBBLES_PASSWORD` | Пароль сервера BlueBubbles |
| `BLUEBUBBLES_WEBHOOK_HOST` | Адрес привязки прослушивателя веб-перехватчика (по умолчанию: `127.0.0.1`) |
| `BLUEBUBBLES_WEBHOOK_PORT` | Порт прослушивателя веб-перехватчика (по умолчанию: `8645`) |
| `BLUEBUBBLES_HOME_CHANNEL` | Телефон/электронная почта для доставки cron/уведомлений |
| `BLUEBUBBLES_ALLOWED_USERS` | Авторизованные пользователи, разделенные запятыми |
| `BLUEBUBBLES_ALLOW_ALL_USERS` | Разрешить всем пользователям (`true`/`false`) |
| `QQ_APP_ID` | Идентификатор приложения QQ Bot от [q.qq.com](https://q.qq.com) |
| `QQ_CLIENT_SECRET` | Секрет приложения QQ Bot от [q.qq.com](https://q.qq.com) |
| `QQ_STT_API_KEY` | Ключ API для внешнего резервного поставщика STT (необязательно, используется, когда встроенный ASR QQ не возвращает текст) |
| `QQ_STT_BASE_URL` | Базовый URL-адрес внешнего поставщика STT (необязательно) |
| `QQ_STT_MODEL` | Название модели для внешнего поставщика STT (необязательно) |
| `QQ_ALLOWED_USERS` | OpenID пользователей QQ, разделенных запятыми, которым разрешено отправлять сообщения боту |
| `QQ_GROUP_ALLOWED_USERS` | Идентификаторы групп QQ, разделенные запятыми, для группового доступа к @-сообщениям |
| `QQ_ALLOW_ALL_USERS` | Разрешить всем пользователям (`true`/`false`, переопределяет `QQ_ALLOWED_USERS`) |
| `QQBOT_HOME_CHANNEL` | OpenID пользователя/группы QQ для доставки cron и уведомлений |
| `QQBOT_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала QQ |
| `QQ_PORTAL_HOST` | Переопределить хост портала QQ (установить значение `sandbox.q.qq.com` для маршрутизации через шлюз песочницы; по умолчанию: `q.qq.com`). |
| `QQ_SANDBOX` | Включить режим песочницы QQ для тестирования разработки (`true`/`false`) |
| `MATTERMOST_URL` | Самый важный URL-адрес сервера (например, `https://mm.example.com`) |
| `MATTERMOST_TOKEN` | Токен бота или токен личного доступа для Mattermost |
| `MATTERMOST_ALLOWED_USERS` | Идентификаторы большинства пользователей Matter, разделенные запятыми, которым разрешено отправлять сообщения боту |
| `MATTERMOST_ALLOW_ALL_USERS` | Разрешить любому пользователю Mattermost запускать бота (только для разработчиков). |
| `MATTERMOST_ALLOWED_CHANNELS` | Если установлено, бот отвечает только в этих каналах (белый список). |
| `MATTERMOST_HOME_CHANNEL` | Идентификатор канала для упреждающей доставки сообщений (cron, уведомления) |
| `MATTERMOST_REQUIRE_MENTION` | Требовать `@mention` в каналах (по умолчанию: `true`). Установите значение `false`, чтобы отвечать на все сообщения. |
| `MATTERMOST_FREE_RESPONSE_CHANNELS` | Идентификаторы каналов, разделенные запятыми, на которые бот отвечает без `@mention` |
| `MATTERMOST_REPLY_MODE` | Стиль ответа: __

PH_766__ (цепочные ответы) или `off` (плоские сообщения, по умолчанию) |
| `MATRIX_HOMESERVER` | URL-адрес домашнего сервера Matrix (например, `https://matrix.org`) |
| `MATRIX_ACCESS_TOKEN` | Токен доступа Matrix для аутентификации ботов |
| `MATRIX_USER_ID` | Идентификатор пользователя Matrix (например, `@hermes:matrix.org`) — требуется для входа в систему с паролем, необязательно с токеном доступа |
| `MATRIX_PASSWORD` | Матричный пароль (альтернатива токену доступа) |
| `MATRIX_ALLOWED_USERS` | Идентификаторы пользователей Matrix, разделенные запятыми, которым разрешено отправлять сообщения боту (например, `@alice:matrix.org`) |
| `MATRIX_ALLOW_ALL_USERS` | Разрешить любому пользователю Matrix запускать бота (только для разработчиков). |
| `MATRIX_HOME_CHANNEL` | Идентификатор комнаты по умолчанию для cron/доставки уведомлений. |
| `MATRIX_HOME_CHANNEL_NAME` | Отображаемое имя домашней комнаты Matrix. |
| `MATRIX_ALLOWED_ROOMS` | Идентификаторы комнат Matrix, разделенные запятыми, позволяют запускать ответы ботов |
| `MATRIX_HOME_ROOM` | Идентификатор комнаты для упреждающей доставки сообщений (например, `!abc123:matrix.org`) |
| `MATRIX_ENCRYPTION` | Включить сквозное шифрование (`true`/`false`, по умолчанию: `false`) |
| `MATRIX_E2EE_MODE` | Поведение матрицы E2EE: `off`, `optional` или `required`. Переопределяет `MATRIX_ENCRYPTION`, если он установлен. |
| `MATRIX_DEVICE_ID` | Стабильный идентификатор устройства Matrix для сохранения E2EE после перезапуска (например, `HERMES_BOT`). Без этого ключи E2EE меняются при каждом запуске и перерывах в расшифровке исторических комнат. |
| `MATRIX_REACTIONS` | Включить эмодзи-реакции жизненного цикла обработки на входящие сообщения (по умолчанию: `true`). Установите значение `false` для отключения. |
| `MATRIX_REQUIRE_MENTION` | Требовать `@mention` в комнатах (по умолчанию: `true`). Установите значение `false`, чтобы отвечать на все сообщения. |
| `MATRIX_FREE_RESPONSE_ROOMS` | Идентификаторы комнат, разделенные запятыми, на которые бот отвечает без `@mention` |
| `MATRIX_IGNORE_USER_PATTERNS` | Регулярные выражения, разделенные запятыми, для игнорируемых идентификаторов пользователей-призраков моста или службы приложений Matrix |
| `MATRIX_PROCESS_NOTICES` | Обработка входящих событий Matrix `m.notice` (по умолчанию: `false`) |
| `MATRIX_SESSION_SCOPE` | Область сеанса матрицы для помещений проекта: `auto`, `room` или `thread` (по умолчанию: `auto`) |
| `MATRIX_TOOLS_ALLOW_REDACTION` | Разрешить выполнение инструмента редактирования сообщений Matrix (по умолчанию: `false`) |
| `MATRIX_TOOLS_ALLOW_INVITES` | Разрешить выполнение инструмента приглашения Matrix (по умолчанию: `false`) |
| `MATRIX_TOOLS_ALLOW_ROOM_CREATE` | Разрешить выполнение инструмента создания помещений Matrix (по умолчанию: `false`) |
| `MATRIX_ALLOW_ROOM_MENTIONS` | Разрешить исходящие упоминания `@room` для уведомления всех участников комнаты (по умолчанию: `false`) |
| `MATRIX_AUTO_THREAD` | Автоматически создавать темы для сообщений комнаты (по умолчанию: `true`) |
| `MATRIX_DM_AUTO_THREAD` | Автоматически создавать темы для сообщений DM в Matrix (по умолчанию: `false`) |
| `MATRIX_DM_MENTION_THREADS` | Создать тему, когда бот `@mentioned` в личном сообщении (по умолчанию: `false`) |
| `MATRIX_APPROVAL_REQUIRE_SENDER` | Требовать, чтобы реакция на утверждение/выбор модели исходила от исходного отправителя запроса, если он известен (по умолчанию: `true`) |
| `MATRIX_APPROVAL_TIMEOUT_SECONDS` | Тайм-аут утверждения реакции матрицы/подсказок выбора модели (по умолчанию: `300`) |
| `MATRIX_ALLOW_PUBLIC_ROOMS` | Разрешить инструментам создания комнат Matrix создавать общественные комнаты (по умолчанию: `false`) |
| `MATRIX_MAX_MEDIA_BYTES` | Максимальный размер загрузки/выгрузки мультимедиа Matrix в байтах (по умолчанию: `104857600`) |
| `MATRIX_RECOVERY_KEY` | Ключ восстановления для проверки перекрестной подписи после ротации ключа устройства. Рекомендуется для установок E2EE с включенной перекрестной подписью. |
| `MATRIX_RECOVERY_KEY_OUTPUT_FILE` | Необязательный одноразовый путь для сгенерированного ключа восстановления Matrix. Создано в режиме `0600` и никогда не перезаписывается. |
| `HASS_TOKEN` | Долговечный токен доступа Home Assistant (включает платформу высокой доступности + инструменты) |
| `HASS_URL` | URL-адрес домашнего помощника (по умолчанию: `http://homeassistant.local:8123`) |
| `WEBHOOK_ENABLED` | Включите адаптер платформы веб-перехватчика (`true`/`false`) |
| `WEBHOOK_PORT` | Порт HTTP-сервера для получения веб-перехватчиков (по умолчанию: `8644`) |
| `WEBHOOK_SECRET` | Глобальный секрет HMAC для проверки подписи веб-перехватчика (используется как запасной вариант, когда маршруты не указывают свои собственные) |
| `API_SERVER_ENABLED` | Включите сервер API, совместимый с OpenAI (`true`/`false`). Работает вместе с другими платформами. |
| `API_SERVER_KEY` | Токен носителя для аутентификации сервера API. Требуется всякий раз, когда включен сервер API. |
| `API_SERVER_CORS_ORIGINS` | Источники браузера, разделенные запятыми, позволяли напрямую вызывать сервер API (например, `http://localhost:3000,http://127.0.0.1:3000`). По умолчанию: отключено. |
| `API_SERVER_PORT` | Порт для сервера API (по умолчанию: `8642`) |
| `API_SERVER_HOST` | Адрес хоста/привязки для сервера API (по умолчанию: `127.0.0.1`). __PH_8

57__ по-прежнему требуется для обратной связи; используйте узкий список разрешений `API_SERVER_CORS_ORIGINS` для доступа через браузер. |
| `API_SERVER_MODEL_NAME` | Название модели рекламируется на `/v1/models`. По умолчанию используется имя профиля (или `hermes-agent` для профиля по умолчанию). Полезно для многопользовательских настроек, где интерфейсы, такие как Open WebUI, требуют отдельных названий моделей для каждого соединения. |
| `GATEWAY_PROXY_URL` | URL-адрес удаленного сервера API Hermes для пересылки сообщений ([режим прокси](/user-guide/messaging/matrix#proxy-mode-e2ee-on-macos)). Если этот параметр установлен, шлюз обрабатывает только ввод-вывод платформы — вся работа агента делегируется удаленному серверу. Также можно настроить через `gateway.proxy_url` в `config.yaml`. |
| `GATEWAY_PROXY_KEY` | Токен носителя для аутентификации на удаленном сервере API в режиме прокси. Должно соответствовать `API_SERVER_KEY` на удаленном хосте. |
| `MESSAGING_CWD` | Устаревший запасной вариант совместимости для рабочего каталога шлюза. Предпочитайте `terminal.cwd` в `config.yaml`. |
| `GATEWAY_ALLOWED_USERS` | Идентификаторы пользователей, разделенные запятыми, разрешены на всех платформах |
| `GATEWAY_ALLOW_ALL_USERS` | Разрешить всем пользователям без списков разрешенных (`true`/`false`, по умолчанию: `false`) |

### Веб-панель и рабочий стол Hermes

Авторизация для [веб-панели](/user-guide/features/web-dashboard) и для подключения [Hermes Desktop к удаленному серверу](/user-guide/features/web-dashboard#connecting-hermes-desktop-to-a-remote-backend). Согласно соглашению «только секреты», учетные данные принадлежат `~/.hermes/.env`; OAuth `client_id` лучше установить в `dashboard.oauth` в `config.yaml` (env выигрывает, если он установлен).

В комплект поставки входят три поставщика аутентификации информационной панели. Для удаленного подключения к Hermes Desktop или любой информационной панели с выходом в Интернет рекомендуемым поставщиком является **OAuth (Nous Portal)** — установите `HERMES_DASHBOARD_OAUTH_CLIENT_ID` (предоставьте ему `hermes dashboard register`). Входящий в комплект поставщик **имя пользователя и пароль** (`HERMES_DASHBOARD_BASIC_AUTH_*`) — это самый быстрый вариант для серверной части в доверенной локальной сети или за VPN, но он не подходит для прямого доступа к общедоступному Интернету. Для аутентификации у вашего собственного поставщика удостоверений используйте **автономный поставщик OIDC** (`HERMES_DASHBOARD_OIDC_*`). В любом случае привязка без обратной связи (`hermes dashboard --host 0.0.0.0`) задействует шлюз аутентификации. Полную картину см. в разделе [Веб-панель → Аутентификация](/user-guide/features/web-dashboard#authentication-gated-mode).

| Переменная | Описание |
|----------|-------------|
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` | Имя пользователя для связанного поставщика проверки подлинности имени пользователя и пароля (`plugins/dashboard_auth/basic`). Активирует провайдера при установке вместе с паролем. Переопределяет `dashboard.basic_auth.username`. |
| `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | Открытый пароль для базового поставщика (хешируется в памяти при загрузке). Выигрывает конфигурацию `password_hash`, поэтому вы можете вращать ее через env. Переопределяет `dashboard.basic_auth.password`. |
| `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` | scrypt хэш пароля для базового провайдера (предпочтительно — без открытого текста). Вычислите с помощью `python -c "from plugins.dashboard_auth.basic import hash_password; print(hash_password('PW'))"`. Переопределяет `dashboard.basic_auth.password_hash`. |
| `HERMES_DASHBOARD_BASIC_AUTH_SECRET` | Ключ HMAC (32+ байта, base64/hex/raw), подписывающий токены сеанса базового поставщика без сохранения состояния. Установите явно, чтобы сеансы выдерживали перезапуски/охватывали несколько рабочих процессов; пусто → случайно для каждого процесса (вы будете выходить из системы при каждом перезапуске). Переопределяет `dashboard.basic_auth.secret`. |
| `HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS` | Срок действия токена доступа для базового поставщика (по умолчанию 12 часов). Переопределяет `dashboard.basic_auth.session_ttl_seconds`. |
| `HERMES_DASHBOARD_OAUTH_CLIENT_ID` | Идентификатор клиента OAuth (`agent:{instance_id}`) для закрытой/общедоступной информационной панели, активирующий поставщика Nous (`plugins/dashboard_auth/nous`). Переопределяет `dashboard.oauth.client_id`. Предоставьте его с помощью `hermes dashboard register`. |
| `HERMES_DASHBOARD_PUBLIC_URL` | Полный общедоступный URL-адрес, по которому панель управления доступна за обратным прокси-сервером. Он контролирует построение обратного вызова OAuth, добавляет свое точное имя хоста в защиту HTTP Host/WebSocket Origin и требует шлюза аутентификации для общедоступных хостов без шлейфа, даже если серверная часть привязывается к шлейфу. Переопределяет `dashboard.public_url`. |
| `HERMES_DASHBOARD_OIDC_ISSUER` | URL-адрес эмитента OIDC для встроенного автономного поставщика OIDC (`plugins/dashboard_auth/self_hosted`). Требуется для его активации. Переопределяет `dashboard.oauth.self_hosted.issuer`. |
| `HERMES_DASHBOARD_OIDC_CLIENT_ID` | Публичный идентификатор клиента OIDC (код авторизации + PKCE) для локального поставщика OIDC. Требуется для его активации. Переопределяет `dashboard.oauth.self_hosted.client_id`. |
| `HERMES_DASHBOARD_OIDC_SCOPES` | Запрошенные области OIDC для локального поставщика OIDC (по умолчанию `openid profile email`). Переопределяет `dashboard.oauth.self_hosted.scopes`. |
| `HERMES_DESKTOP_REMOTE_URL` | (На рабочем столе) Базовый URL-адрес удаленного бэкэнда, например. `http://host:9119`. Если установлено, переопределяет URL-адрес шлюза в приложении; вы по-прежнему входите в систему с панели настроек шлюза (перенаправление OAuth или имя пользователя/пароль, в зависимости от того, что объявляет серверная часть). |
| `HERMES_DESKTOP_HERMES` | Переопределение серверной команды рабочего стола. Используется упаковщиками/Nix или при устранении неполадок, чтобы указать Electron на конкретный исполняемый файл `hermes` после проверки серверной части. |
| `HERMES_DESKTOP_HERMES_ROOT` | Переопределение извлечения исходного кода рабочего стола, используемое `hermes desktop --hermes-root`; проверяется перед пакетной установкой при первом запуске или перед существующей версией `hermes` на `PATH`. |
| `HERMES_DESKTOP_IGNORE_EXISTING` | Установите значение `1`, чтобы Desktop игнорировал существующий `hermes` на `PATH` во время внутреннего разрешения. Эквивалент `hermes desktop --ignore-existing`. |
| `HERMES_DESKTOP_CWD` | Начальный каталог проекта для сеансов чата на рабочем столе. Установлен `hermes desktop --cwd`. |
| `HERMES_DESKTOP_PYTHON` | Абсолютный путь к интерпретатору Python для серверной части, проверенный до того, как Electron автоматически разрешит его для извлечения исходного кода. Используется помощниками разработчиков рабочего дерева (см. [TUI и рабочий стол из Worktrees](../developer-guide/worktree-ui-dev.md)) для повторного использования общего venv. |
| `HERMES_DESKTOP_DEV_SERVER` | URL-адрес сервера разработки Vite, который оболочка Electron загружает вместо упакованного пакета (например, `http://127.0.0.1:5174`). Устанавливается автоматически `npm run dev`; актуально только при взломе приложения. |
| `HERMES_DESKTOP_CDP_PORT` | Переопределяет порт протокола Chrome DevTools, который средство рендеринга предоставляет на `127.0.0.1` для инструментов проверки DOM/CSS (по умолчанию `9222`). Запускается Dev-сервер (`npm run dev`, `hgui`) и открывает его автоматически; упакованное приложение никогда этого не делает, и никакое значение здесь этого не меняет. Установите значение `off`, чтобы отключить его при запуске разработки. Все, что может достичь порта, может выполнить код в рендерере. |

### Microsoft Graph (собрания команд)

Учетные данные только для приложения для клиента REST Microsoft Graph, который будет использоваться в конвейере сводных отчетов о предстоящих собраниях Teams. См. раздел [Регистрация приложения Microsoft Graph](/guides/microsoft-graph-app-registration) для ознакомления с пошаговым руководством по порталу Azure и точными необходимыми разрешениями API.

| Переменная | Описание |
|----------|-------------|
| `MSGRAPH_TENANT_ID` | Идентификатор клиента Azure AD (GUID каталога) для регистрации приложения Graph. |
| `MSGRAPH_CLIENT_ID` | Идентификатор приложения (клиента) для регистрации приложения Azure. |
| `MSGRAPH_CLIENT_SECRET` | Значение секрета клиента для регистрации приложения. Сохраните в `~/.hermes/.env` с `chmod 600`; периодически чередуйте их через портал Azure. |
| `MSGRAPH_SCOPE` | Область OAuth2 для запроса токена учетных данных клиента (по умолчанию: `https://graph.microsoft.com/.default`). |
| `MSGRAPH_AUTHORITY_URL` | Полномочия платформы идентификации Microsoft (по умолчанию: `https://login.microsoftonline.com`). Переопределить только для национальных/суверенных облаков (например, `https://login.microsoftonline.us` для GCC High). |

### Прослушиватель веб-перехватчиков Microsoft Graph

Прослушиватель входящих уведомлений об изменениях для событий Graph (собрания Teams, календарь, чат и т. д.). См. [Microsoft Graph Webhook Listener](/user-guide/messaging/msgraph-webhook) для настройки и усиления безопасности.

| Переменная | Описание |
|----------|-------------|
| `MSGRAPH_WEBHOOK_ENABLED` | Включите платформу шлюза `msgraph_webhook` (`true`/`1`/`yes`). |
| `MSGRAPH_WEBHOOK_PORT` | Порт, к которому привязан прослушиватель (по умолчанию: `8646`). |
| `MSGRAPH_WEBHOOK_CLIENT_STATE` | Общий секретный график отображается в каждом уведомлении; по сравнению с `hmac.compare_digest`. Создайте с помощью `openssl rand -hex 32`. |
| `MSGRAPH_WEBHOOK_ACCEPTED_RESOURCES` | Разделенный запятыми белый список путей/шаблонов ресурсов Graph (например, `communications/onlineMeetings,chats/*/messages`). Завершающий `*` соответствует префиксу. Пусто = принять все. |
| `MSGRAPH_WEBHOOK_ALLOWED_SOURCE_CIDRS` | Диапазоны CIDR, разделенные запятыми, разрешенные для POST прослушивателю (например, `52.96.0.0/14,52.104.0.0/14`). Пусто = разрешить все (по умолчанию). Ограничьте опубликованные выходные диапазоны Microsoft Graph в рабочей среде. |

### Доставка сводки собрания Teams

Используется только тогда, когда включен [`teams_pipeline` плагин](/user-guide/messaging/msgraph-webhook). Параметры также можно настроить в `platforms.teams.extra` в `config.yaml` — переменные env имеют приоритет, если установлены оба. См. раздел [Microsoft Teams → Доставка сводки собрания](/user-guide/messaging/teams#meeting-summary-delivery-teams-meeting-pipeline).

| Переменная | Описание |
|----------|-------------|
| `TEAMS_DELIVERY_MODE` | `graph` или `incoming_webhook`. |
| `TEAMS_INCOMING_WEBHOOK_URL` | URL-адрес веб-перехватчика, созданный Teams; требуется, когда `TEAMS_DELIVERY_MODE=incoming_webhook`. |
| `TEAMS_GRAPH_ACCESS_TOKEN` | Предварительно полученный токен делегированного доступа для доставки Graph. Требуется редко — средство записи возвращается к учетным данным приложения `MSGRAPH_*`, если они не установлены. |
| `TEAMS_TEAM_ID` | Идентификатор целевой группы для доставки канала (режим `graph`). |
| `TEAMS_CHANNEL_ID` | Идентификатор целевого канала (в сочетании с `TEAMS_TEAM_ID`). |
| `TEAMS_CHAT_ID` | Целевой 1:1 или идентификатор группового чата (альтернатива команде+каналу для режима `graph`). |

### API обмена сообщениями LINE

Используется встроенным плагином платформы LINE (`plugins/platforms/line/`). См. [Шлюз обмена сообщениями → LINE](/user-guide/messaging/line) для полной настройки.

| Переменная | Описание |
|----------|-------------|
| `LINE_CHANNEL_ACCESS_TOKEN` | Долговечный токен доступа к каналу из консоли разработчиков LINE (вкладка API сообщений). Необходимый. |
| `LINE_CHANNEL_SECRET` | Секрет канала (вкладка Основные настройки); используется для проверки подписи веб-перехватчика HMAC-SHA256. Необходимый. |
| `LINE_HOST` | Хост привязки веб-перехватчика (по умолчанию: `0.0.0.0`). |
| `LINE_PORT` | Порт привязки вебхука (по умолчанию: `8646`). |
| `LINE_PUBLIC_URL` | Общедоступный базовый URL-адрес HTTPS (например, `https://my-tunnel.example.com`). Требуется для отправки изображений, аудио и видео — LINE принимает только URL-адреса, доступные по протоколу HTTPS. |
| `LINE_ALLOWED_USERS` | Идентификаторы пользователей, разделенные запятыми, позволяют управлять ботом (с префиксом `U`). |
| `LINE_ALLOWED_GROUPS` | Идентификаторы групп, разделенные запятыми, на которые будет отвечать бот (с префиксом `C`). |
| `LINE_ALLOWED_ROOMS` | Идентификаторы комнат, разделенные запятыми, на которые будет отвечать бот (с префиксом `R`). |
| `LINE_ALLOW_ALL_USERS` | Аварийный люк только для разработчиков — принимает любой источник. По умолчанию: `false`. |
| `LINE_HOME_CHANNEL` | Цель доставки по умолчанию для заданий cron с `deliver: line`. |
| `LINE_SLOW_RESPONSE_THRESHOLD` | За несколько секунд до срабатывания обратной передачи кнопок шаблона LLM (по умолчанию: `45`). Установите `0` для отключения и всегда принудительного возврата. |
| `LINE_PENDING_TEXT` | Текст в виде всплывающей подсказки, отображаемый рядом с кнопкой обратной передачи. |
| `LINE_BUTTON_LABEL` | Метка кнопки обратной передачи (по умолчанию: `Get answer`). |
| `LINE_DELIVERED_TEXT` | Ответить, когда уже доставленный постбэк будет повторно использован (по умолчанию: `Already replied ✅`). |
| `LINE_INTERRUPTED_TEXT` | Ответить при нажатии потерянной кнопки обратной передачи `/stop` (по умолчанию: `Run was interrupted before completion.`). |

### ntfy (push-уведомления)

[ntfy](https://ntfy.sh/) — это облегченная служба push-уведомлений на основе HTTP. Подпишитесь на тему в [мобильном приложении ntfy](https://ntfy.sh/docs/subscribe/phone/), опубликуйте ее в этой теме, чтобы поговорить с агентом.

| Переменная | Описание |
|----------|-------------|
| `NTFY_TOPIC` | Тема для подписки (входящие сообщения). Необходимый. |
| `NTFY_SERVER_URL` | URL-адрес сервера (по умолчанию: `https://ntfy.sh`). Для обеспечения конфиденциальности укажите на собственный ntfy. |
| `NTFY_TOKEN` | Необязательный токен аутентификации. Токен носителя (например, `tk_xyz`) или `user:pass` для базовой аутентификации. |
| `NTFY_PUBLISH_TOPIC` | Тема для исходящих ответов (по умолчанию `NTFY_TOPIC`). |
| `NTFY_MARKDOWN` | Установите `true` для отправки ответов с заголовком `X-Markdown: true`. По умолчанию: `false`. |
| `NTFY_ALLOWED_USERS` | Список разрешенных (рассматривается как идентификаторы пользователей; в ntfy это названия тем). Обычно устанавливается то же значение, что и `NTFY_TOPIC`. |
| `NTFY_ALLOW_ALL_USERS` | Аварийный люк только для разработчиков — безопасен только в частных темах с контролируемым доступом. По умолчанию: `false`. |
| `NTFY_HOME_CHANNEL` | Цель доставки по умолчанию для заданий cron с `deliver: ntfy`. |
| `NTFY_HOME_CHANNEL_NAME` | Человеческая метка домашнего канала (по умолчанию — название темы). |

Перед развертыванием с ненадежными темами ознакомьтесь с [руководством по обмену сообщениями ntfy](/user-guide/messaging/ntfy), особенно с разделом **модель идентификации**.

### IRC

Подключите Hermes к IRC-серверу. Никаких внешних зависимостей. См. [руководство по обмену сообщениями IRC](/user-guide/messaging/irc).

| Переменная | Описание |
|----------|-------------|
| `IRC_SERVER` | Имя хоста IRC-сервера (например, `irc.libera.chat`). Необходимый. |
| `IRC_CHANNEL` | Каналы, к которым нужно присоединиться (например, `#hermes`); через запятую для нескольких. Необходимый. |
| `IRC_NICKNAME` | Ник бота (по умолчанию: `hermes-bot`). Необходимый. |
| `IRC_PORT` | Порт сервера (по умолчанию: `6697` с TLS, `6667` без). |
| `IRC_USE_TLS` | Используйте TLS (`true`/`false`; по умолчанию `true` на порту 6697). |
| `IRC_SERVER_PASSWORD` | Пароль сервера для команды `PASS` (необязательно). |
| `IRC_NICKSERV_PASSWORD` | Пароль NickServ для автоматической ИДЕНТИФИКАЦИИ при подключении (необязательно). |
| `IRC_ALLOWED_USERS` | Ники, разделенные запятыми, позволяют общаться с ботом. |
| `IRC_ALLOW_ALL_USERS` | Разрешить любому пользователю канала общаться с ботом (только для разработчиков). |
| `IRC_HOME_CHANNEL` | Канал для cron/доставки уведомлений (по умолчанию `IRC_CHANNEL`). |

### SimpleX

Подключите Hermes к сети [SimpleX Chat](https://simplex.chat/) через локальный демон `simplex-chat`. См. [руководство по обмену сообщениями SimpleX](/user-guide/messaging/simplex).

| Переменная | Описание |
|----------|-------------|
| `SIMPLEX_WS_URL` | URL-адрес WebSocket демона симплекс-чата (например, `ws://127.0.0.1:5225`). |
| `SIMPLEX_ALLOWED_USERS` | Идентификаторы контактов SimpleX, разделенные запятыми, позволяют общаться с ботом. |
| `SIMPLEX_ALLOW_ALL_USERS` | Разрешить любому контакту общаться с ботом (только для разработчиков — отключает белый список). |
| `SIMPLEX_AUTO_ACCEPT` | Автоматически принимать входящие запросы на контакт (по умолчанию: `true`). |
| `SIMPLEX_GROUP_ALLOWED` | Разделенные запятыми идентификаторы групп SimpleX, в которых должен участвовать бот, или `*`, чтобы разрешить любую группу. Опустить полное игнорирование групповых сообщений (более безопасное значение по умолчанию — в противном случае бот в группе обрабатывает трафик каждого участника). |
| `SIMPLEX_HOME_CHANNEL` | Идентификатор контакта/группы по умолчанию для cron/доставки уведомлений. |
| `SIMPLEX_HOME_CHANNEL_NAME` | Человеческая метка домашнего канала (по умолчанию — идентификатор). |

### Фотон

Подключите Hermes к [Photon](https://photon.codes/)/Spectrum (iMessage и другие платформы Spectrum) через сайдкар Node. См. [руководство по обмену сообщениями Photon](/user-guide/messaging/photon).

| Переменная | Описание |
|----------|-------------|
| `PHOTON_PROJECT_ID` | Идентификатор проекта Spectrum (`spectrumProjectId` проекта; установлен `hermes photon setup`). |
| `PHOTON_PROJECT_SECRET` | Секрет проекта в сочетании с идентификатором проекта Spectrum (устанавливается `hermes photon setup`). |
| `PHOTON_ALLOWED_USERS` | Номера телефонов E.164, разделенные запятыми, позволяют разговаривать с ботом. |
| `PHOTON_ALLOW_ALL_USERS` | Разрешить любому отправителю запускать бота (только для разработчиков — отключает белый список). |
| `PHOTON_REQUIRE_MENTION` | Игнорировать сообщения группового чата, если они не соответствуют упоминаемому слову пробуждения (`true`/`false`, по умолчанию `false`). |
| `PHOTON_MENTION_PATTERNS` | Упоминайте регулярные выражения слов пробуждения для групповых чатов (список JSON или разделенные запятыми/новой строкой; по умолчанию слова пробуждения Hermes). |
| `PHOTON_HOME_CHANNEL` | Цель Photon по умолчанию для cron/доставки уведомлений: идентификатор пространства Spectrum, DM GUID или пустой номер телефона E.164. |
| `PHOTON_HOME_CHANNEL_NAME` | Человеческий ярлык для домашнего канала. |
| `PHOTON_MARKDOWN` | Отправляйте ответы агента с уценкой — iMessage отображает их изначально, другие платформы Spectrum преобразуют их в обычный текст (`true`/`false`, по умолчанию `true`). |
| `PHOTON_REACTIONS` | Ответные нажатия 👀/👍/👎 на сообщения в качестве статуса обработки и перенаправление ответных сообщений на сообщения бота агенту (`true`/`false`, по умолчанию `false`). |
| `PHOTON_TELEMETRY` | Включите телеметрию Spectrum SDK в коляске (`true`/`false`, по умолчанию `false`; переключите с помощью `hermes photon telemetry on|off`). |
| `PHOTON_SIDECAR_PORT` | Шлейфовый порт для управления коляской узла + входящий канал (по умолчанию `8789`). |
| `PHOTON_SIDECAR_AUTOSTART` | Создайте коляску Node при подключении (`true`/`false`, по умолчанию `true`). |
| `PHOTON_NODE_BIN` | Путь к двоичному файлу узла (по умолчанию: `shutil.which('node')`). |
| `PHOTON_DASHBOARD_HOST` | Хост Photon Dashboard API (по умолчанию `https://app.photon.codes`). |
| `PHOTON_SPECTRUM_HOST` | Хост Photon Spectrum API (по умолчанию `https://spectrum.photon.codes`). |

### Живая информация (сообщества Nostr)

| Переменная | Описание |
|----------|-------------|
| `BUZZ_RELAY_URL` | Базовый URL-адрес ретранслятора сообщества Живой ленты (например, `https://mycommunity.communities.buzz.xyz`) |
| `BUZZ_PRIVATE_KEY` | Закрытый ключ Nostr для идентификации агента в Buzz (nsec или шестнадцатеричный) — единственный секрет Buzz |
| `BUZZ_CREDENTIALS_FILE` | Файл учетных данных JSON, содержащий nsec (резервный вариант, если `BUZZ_PRIVATE_KEY` не установлен) |
| `BUZZ_CHANNELS` | UUID каналов, разделенных запятыми, для просмотра (по умолчанию: все присоединенные каналы) |
| `BUZZ_HOME_CHANNEL` | UUID канала для cron/доставки уведомлений (по умолчанию первый просматриваемый канал) |
| `BUZZ_ALLOWED_USERS` | Разделенные запятыми npub или шестнадцатеричные pubkeys разрешены для общения с агентом |
| `BUZZ_ALLOW_ALL_USERS` | Разрешить любому участнику сообщества общаться с агентом (`true`/`false`) |
| `BUZZ_TRANSPORT` | Входящий транспорт: `auto` (WebSocket с резервным опросом, по умолчанию), `websocket` или `poll` |
| `BUZZ_POLL_INTERVAL` | Секунды между входящими опросами (по умолчанию: `4`) |
| `BUZZ_AUTH_TAG` | Дополнительный тег аутентификации NIP-OA в формате JSON для аутентификации NIP-42 WebSocket |
| `BUZZ_CLI_PATH` | Путь к двоичному файлу интерфейса командной строки (по умолчанию: `buzz` в PATH, затем `~/bin/buzz`) |

### Microsoft Teams (адаптер)

Адаптер платформы Microsoft Teams (Bot Framework/Azure AD), отличный от интеграции [Microsoft Graph (Teams Meetings)](#microsoft-graph-teams-meetings), описанной выше. См. [руководство по обмену сообщениями в Teams](/user-guide/messaging/teams).

| Переменная | Описание |
|----------|-------------|
| `TEAMS_CLIENT_ID` | Идентификатор клиента приложения Azure AD (Bot Framework). |
| `TEAMS_CLIENT_SECRET` | Секрет клиента приложения Azure AD. |
| `TEAMS_TENANT_ID` | Идентификатор клиента Azure AD, на котором размещено приложение-бот. |
| `TEAMS_HOST` | Хост привязки Webhook (по умолчанию: отключено → двойной стек, все интерфейсы IPv4+IPv6). |
| `TEAMS_PORT` | Порт прослушивания веб-перехватчика (по умолчанию Bot Framework: `3978`). |
| `TEAMS_ALLOWED_USERS` | Идентификаторы пользователей/UPN Teams, разделенные запятыми, разрешены для общения с ботом. |
| `TEAMS_ALLOW_ALL_USERS` | Разрешить любому пользователю Teams запускать бота (только для разработчиков). |
| `TEAMS_HOME_CHANNEL` | Идентификатор чата/канала по умолчанию для cron/доставки уведомлений. |
| `TEAMS_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала Teams. |

### Плот

| Переменная | Описание |
|----------|-------------|
| `RAFT_PROFILE` | Пуля профиля агента Raft — автоматически включает адаптер, если он установлен. |

### Расширенная настройка обмена сообщениями

Расширенные настройки для каждой платформы для регулирования пакетирования исходящих сообщений. Большинству пользователей никогда не придется прикасаться к ним; настройки по умолчанию настроены таким образом, чтобы соблюдать ограничения скорости каждой платформы, не чувствуя себя вялым.

| Переменная | Описание |
|----------|-------------|
| `HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS` | Окно льготного периода перед очисткой текстового фрагмента Telegram в очереди (по умолчанию: `0.6`). |
| `HERMES_TELEGRAM_TEXT_BATCH_SPLIT_DELAY_SECONDS` | Задержка между разделенными фрагментами, когда одно сообщение Telegram превышает предел длины (по умолчанию: `2.0`). |
| `HERMES_SIMPLEX_TEXT_BATCH_DELAY` | Секунды периода молчания (по умолчанию: `0.8`), используемые для объединения быстрых входящих текстовых сообщений в одно MessageEvent — тот же шаблон, что и пакетная обработка текста в Telegram. |
| `HERMES_TELEGRAM_MEDIA_BATCH_DELAY_SECONDS` | Окно отсрочки перед очисткой медиафайлов Telegram в очереди (по умолчанию: `0.6`). |
| `HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS` | Задержка перед отправкой последующего сообщения после завершения работы агента, чтобы избежать ускорения последнего фрагмента потока. |
| `HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT` / `_READ_TIMEOUT` / `_WRITE_TIMEOUT` / `_POOL_TIMEOUT` | Переопределить базовые тайм-ауты HTTP `python-telegram-bot` (в секундах). |
| `HERMES_TELEGRAM_INIT_TIMEOUT` | Ограничение количества попыток (в секундах) в цепочке подключений Telegram `initialize()` во время запуска шлюза, поэтому недоступная цепочка резервных IP-адресов не может блокировать запуск на неопределенный срок (по умолчанию: `30`). |
| `HERMES_TELEGRAM_HTTP_POOL_SIZE` | Максимальное количество одновременных HTTP-соединений к API Telegram. |
| `HERMES_TELEGRAM_DISABLE_FALLBACK_IPS` | Отключите жестко запрограммированные резервные IP-адреса Cloudflare, используемые при сбое DNS (`true`/`false`). |
| `HERMES_DISCORD_TEXT_BATCH_DELAY_SECONDS` | Окно отсрочки перед очисткой текстового фрагмента Discord в очереди (по умолчанию: `0.6`). |
| `HERMES_DISCORD_TEXT_BATCH_SPLIT_DELAY_SECONDS` | Задержка между разделенными фрагментами, когда сообщение Discord превышает предел длины (по умолчанию: `2.0`). |
| `HERMES_DISCORD_LIVENESS_INTERVAL_SECONDS` | Совместимость/ручное переопределение для `discord.websocket_liveness_interval_seconds`. Интервал выборки активного веб-сокета Discord Gateway (по умолчанию: `15`; установите значение `0` для отключения). Предпочитайте ключ `config.yaml`. |
| `HERMES_DISCORD_LIVENESS_FAILURE_THRESHOLD` | Совместимость/ручное переопределение для `discord.websocket_liveness_failure_threshold`. Последовательные неработоспособные образцы WebSocket перед принудительным повторным подключением (по умолчанию: `2`). Предпочитайте ключ `config.yaml`. |
| `HERMES_MATRIX_TEXT_BATCH_DELAY_SECONDS` / `_SPLIT_DELAY_SECONDS` | Матричные эквиваленты кнопок пакетной обработки Telegram. |
| `HERMES_FEISHU_TEXT_BATCH_DELAY_SECONDS` / `_SPLIT_DELAY_SECONDS` / `_MAX_CHARS` / `_MAX_MESSAGES` | Настройка дозатора Feishu — задержка, разделенная задержка, максимальное количество символов в сообщении, максимальное количество сообщений в пакете. |
| `HERMES_FEISHU_MEDIA_BATCH_DELAY_SECONDS` | Задержка очистки носителя Feishu. |
| `HERMES_FEISHU_DEDUP_CACHE_SIZE` | Размер кэша дедупликации веб-перехватчика Feishu (по умолчанию: `1024`). |
| `HERMES_WECOM_TEXT_BATCH_DELAY_SECONDS` / `_SPLIT_DELAY_SECONDS` | Настройка дозатора WeCom. |
| `HERMES_VISION_DOWNLOAD_TIMEOUT` | Тайм-аут в секундах для загрузки изображения перед его передачей моделям машинного зрения (по умолчанию: `30`). |
| `HERMES_VISION_MAX_CONCURRENCY` | Максимальное количество одновременных **кодирования/изменения размера** изображений в течение всего процесса (переопределение для `auxiliary.vision.max_concurrency`; по умолчанию: количество ядер ЦП хоста, без потолка). Ограничивает только этап кодирования, связанный с процессором, поэтому разветвление видеокадра не может перегрузить каждое ядро ​​и истощить цикл событий — вызовы LLM остаются полностью параллельными. Значения `< 1` игнорируются. |
| `HERMES_RESTART_DRAIN_TIMEOUT` | Шлюз: секунды ожидания истощения активных запусков на `/restart` перед принудительным перезапуском (по умолчанию: `900`). |
| `HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT` | Тайм-аут подключения для каждой платформы во время запуска шлюза и повторного подключения (секунды; `0`/отрицательное ожидание неопределенное время). Применяется к попытке подключения *и* ожиданию готовности адаптера Discord, поэтому учетные записи с большим количеством косых команд для синхронизации не отключаются в середине запуска. Соединение с `gateway.platform_connect_timeout` в `config.yaml` (по умолчанию `30`); эта переменная окружения является ручным переопределением и выигрывает, если установлена ​​явно. |
| `HERMES_GATEWAY_BUSY_INPUT_MODE` | Поведение шлюза по умолчанию при занятом входе: `queue`, `steer` или `interrupt`. Можно переопределить в чате с `/busy`. |
| `HERMES_GATEWAY_BUSY_ACK_ENABLED` | Отправляет ли шлюз подтверждающее сообщение (⚡/⏳/⏩), когда пользователь отправляет входные данные, когда агент занят (по умолчанию: `true`). Установите значение `false`, чтобы полностью подавить эти сообщения — ввод по-прежнему ставится в очередь/управляется/прерывается как обычно, только ответ в чате отключается. Соединение с `display.busy_ack_enabled` в `config.yaml`. |
| `HERMES_GATEWAY_NO_SUPERVISE` | Внутри образа Docker с наложением s6 отключите автоматический контроль при запуске `hermes gateway run` и используйте семантику переднего плана до s6 (без автоматического перезапуска, шлюз — это основной процесс контейнера). Истинные значения: `1`, `true`, `yes`. Эквивалент флага CLI `--no-supervise`. No-op за пределами s

6 изображение. |
| `HERMES_GATEWAY_BOOTSTRAP_STATE` | Внутри образа Docker с наложением s6 объявите **исходное** контролируемое состояние шлюза на новом томе. На пустом томе нет сохраненного `gateway_state.json`, поэтому программа согласования загрузки регистрирует слот `gateway-default`, но оставляет его **неработающим** (он запускается автоматически только тогда, когда последнее записанное состояние было `running`). Установите для этого параметра значение `running`, а перехватчик настройки первой загрузки задает значение `gateway_state.json` *до* запуска примирителя, поэтому шлюз открывается при самой первой загрузке. Учитывается только буквальное значение `running`. Только при первой загрузке: существующий `gateway_state.json` никогда не перезаписывается, поэтому намеренно остановленный шлюз остается остановленным при перезапуске. Никаких операций за пределами изображения 6-го сезона. |
| `GATEWAY_RELAY_URL` | Базовый URL-адрес экспериментального релейного соединителя WebSocket. Если этот параметр установлен, шлюз регистрирует универсальный адаптер `relay` и осуществляет исходящий вызов соединителя. Зеркала `gateway.relay_url` в `config.yaml`. |
| `GATEWAY_RELAY_ID` | Идентификатор ретрансляционного шлюза, назначенный `hermes gateway enroll` или управляемой самоподготовкой. Зеркала `gateway.relay_id`. |
| `GATEWAY_RELAY_SECRET` | Секрет ретрансляции каждого шлюза, используемый для аутентификации WebSocket. Если это уже настроено, управляемая самоподготовка пропускается. Зеркала `gateway.relay_secret`. |
| `GATEWAY_RELAY_DELIVERY_KEY` | Ключ доставки, выданный соединителем, сохраняется для совместимости с ретрансляционной/сквозной аутентификацией. Текущие входящие ретрансляционные сообщения поступают на исходящий WebSocket, а не на HTTP-приемник на стороне шлюза. |
| `GATEWAY_RELAY_ENROLL_TOKEN` | Токен регистрации, используемый `hermes gateway enroll`, когда `--token` не передается явно. |
| `GATEWAY_RELAY_PLATFORM` | Необязательное имя платформы, объявленное в дескрипторе возможностей ретрансляции. |
| `GATEWAY_RELAY_BOT_ID` | Необязательный идентификатор бота, объявленный в дескрипторе возможности ретрансляции. |
| `GATEWAY_RELAY_ENDPOINT` | Дополнительная конечная точка шлюза, объявленная для режимов соединителя, которым требуется URL-адрес обратного вызова или сквозной передачи; не требуется для входящего пути ретрансляции только для WS по умолчанию. Зеркала `gateway.relay_endpoint`. |
| `GATEWAY_RELAY_ROUTE_KEYS` | Ключи маршрута ретрансляции, разделенные запятыми, объявляются соединителю. Зеркала `gateway.relay_route_keys`. |
| `HERMES_FILE_MUTATION_VERIFIER` | Включить нижний колонтитул проверки изменений файлов за ход (по умолчанию: `true`). Если эта функция включена, Hermes добавляет рекомендательный список со списком всех вызовов `write_file` / `patch`, которые завершились неудачей во время хода и не были заменены успешной записью. Установите `0`, `false`, `no` или `off` для подавления. Зеркала `display.file_mutation_verifier` в `config.yaml`; env var выигрывает, если установлен. |
| `HERMES_CRON_TIMEOUT` | Тайм-аут бездействия агента задания cron исчисляется секундами (по умолчанию: `600`). Агент может работать бесконечно, активно вызывая инструменты или получая токены потока — это срабатывает только в режиме ожидания. Установите `0` для неограниченного доступа. |
| `HERMES_CRON_SCRIPT_TIMEOUT` | Тайм-аут для сценариев предварительного запуска, прикрепленных к заданиям cron, в секундах (по умолчанию: `3600`). Ограничивает только сценарий — задания навыков/агентов используют отдельный бюджет неактивности `HERMES_CRON_TIMEOUT`. Также можно настроить через `cron.script_timeout_seconds` в `config.yaml`. |
| `HERMES_CRON_MEDIA_SEND_TIMEOUT` | Тайм-аут для каждого мультимедийного вложения, отправляемого во время доставки cron через адаптер живого шлюза, в секундах (по умолчанию: `300`). Поднимите этот параметр, если большие вложения (длинный звук TTS, большой экспорт) истекают во время загрузки. Также можно настроить через `cron.media_send_timeout_seconds` в `config.yaml`. |
| `HERMES_CRON_MAX_PARALLEL` | Максимальное количество заданий cron, выполняемых параллельно за такт (по умолчанию: `4`). |

## Реле НеМо

| Переменная | Описание |
|----------|-------------|
| `HERMES_NEMO_RELAY_PLUGINS_TOML` | Явный путь к стандартному реле NeMo Relay `plugins.toml`, загруженному ядром Hermes для всего процесса. Если этот параметр не установлен, Hermes не инициализирует промежуточное программное обеспечение Relay, динамические плагины или средства экспорта. Удаленные переменные `HERMES_NEMO_RELAY_ATOF_*` и `HERMES_NEMO_RELAY_ATIF_*` игнорируются; Вместо этого настройте эти выходные данные в выбранном файле. См. [Конфигурация наблюдения NeMo Relay] (https://docs.nvidia.com/nemo/relay/configure-plugins/observability/about). |

## Поведение агента

| Переменная | Описание |
|----------|-------------|
| `HERMES_MAX_ITERATIONS` | Максимальное количество итераций вызова инструмента за разговор (по умолчанию: 500) |
| `HERMES_INFERENCE_MODEL` | Переопределить имя модели на уровне процесса (имеет приоритет над `config.yaml` для сеанса). Также можно установить с помощью флага `-m`/`--model`. |
| `HERMES_YOLO_MODE` | Установите значение `1`, чтобы обойти запросы на одобрение опасных команд. Эквивалент `--yolo`. |
| `HERMES_ACCEPT_HOOKS` | Автоматически утверждать любые невидимые перехватчики оболочки, объявленные в `config.yaml`, без запроса TTY. Эквивалент `--accept-hooks` или `hooks_auto_accept: true`. |
| `HERMES_IGNORE_USER_CONFIG` | Пропустите `~/.hermes/config.yaml` и используйте встроенные значения по умолчанию (учетные данные в `.env` все равно загружаются). Эквивалент `--ignore-user-config`. |
| `HERMES_IGNORE_RULES` | Пропустить автоматическое внедрение `AGENTS.md`, `SOUL.md`, `.cursorrules`, памяти и предварительно загруженных навыков. Эквивалент `--ignore-rules`. |
| `HERMES_SAFE_MODE` | Режим устранения неполадок: отключить ВСЕ настройки — пропускает обнаружение плагинов, загрузку сервера MCP и регистрацию перехватчика оболочки. Устанавливается автоматически с помощью `--safe-mode` (который также устанавливает два вышеуказанных флага). |
| `HERMES_TOOL_PROGRESS` | Не поддерживается с момента поддержки config-v12 — переменная игнорируется. Используйте `display.tool_progress` в `config.yaml`. |
| `HERMES_TOOL_PROGRESS_MODE` | Устаревшая переменная совместимости для режима работы инструмента (все еще считывается шлюзом как запасной вариант). Предпочитайте `display.tool_progress` в `config.yaml`. |
| `HERMES_HUMAN_DELAY_MODE` | Скорость ответа: `off`/`natural`/`custom` |
| `HERMES_HUMAN_DELAY_MIN_MS` | Минимальный диапазон пользовательской задержки (мс) |
| `HERMES_HUMAN_DELAY_MAX_MS` | Максимальный диапазон пользовательской задержки (мс) |
| `HERMES_QUIET` | Подавить несущественный вывод (`true`/`false`) |
| `CODEX_HOME` | Когда [время выполнения сервера приложений Codex](../user-guide/features/codex-app-server-runtime) включено, переопределите каталог, из которого CLI Codex считывает свою конфигурацию + аутентификацию (по умолчанию: `~/.codex`). Миграция Гермеса записывает управляемый блок в `<CODEX_HOME>/config.toml`. |
| `HERMES_KANBAN_TASK` | Устанавливается диспетчером канбана при создании работника (UUID задачи). Рабочие процессы и порожденный подпроцесс `hermes-tools` MCP наследуют его, поэтому инструменты канбана работают правильно. Не устанавливайте вручную. |
| `HERMES_ACP_SKIP_CONFIGURED_MCP` | Устанавливается [хостом ACP](../user-guide/features/acp#host-integration) в подпроцессе Hermes, который он порождает. `1` пропускает запуск глобально настроенных серверов `config.yaml` MCP перед циклом ACP JSON-RPC для хостов, которые сами пропускают серверы MCP сеанса через `session/new`. Серверы, предоставленные сеансом ACP, по-прежнему зарегистрированы; любое другое значение сохраняет значение по умолчанию. Не устанавливайте вручную. |
| `HERMES_API_TIMEOUT` | Тайм-аут вызова LLM API в секундах (по умолчанию: `1800`) |
| `HERMES_API_CALL_STALE_TIMEOUT` | Тайм-аут устаревшего вызова без потоковой передачи в секундах (по умолчанию: `90`). Автоматически отключается для местных провайдеров, если не настроено, и может масштабироваться вверх для очень больших контекстов. Также можно настроить через `providers.<id>.stale_timeout_seconds` или `providers.<id>.models.<model>.stale_timeout_seconds` в `config.yaml`. |
| `HERMES_STREAM_READ_TIMEOUT` | Тайм-аут чтения потокового сокета в секундах (по умолчанию: `120`). Автоматически увеличено до `HERMES_API_TIMEOUT` для местных провайдеров. Увеличьте значение, если время ожидания локальных LLM истекло во время генерации длинного кода. |
| `HERMES_STREAM_STALE_TIMEOUT` | Тайм-аут обнаружения устаревшего потока в секундах (по умолчанию: `180`). Автоматически отключено для местных провайдеров. Запускает прекращение соединения, если в течение этого окна не поступает ни одного фрагмента. |
| `HERMES_LOCAL_STREAM_STALE_TIMEOUT` | Потолок устаревшего потока для местных провайдеров (Ollama, oMLX, llama-cpp) в секундах (по умолчанию: `900`). Когда базовый тайм-аут устаревания установлен по умолчанию и обнаружена локальная конечная точка, этот конечный потолок заменяет прежнее бесконечное отключение, поэтому заклинивший локальный сервер в конечном итоге отключает детектор, а не зависает навсегда. Также можно настроить через `agent.local_stream_stale_timeout` в `config.yaml`. |
| `HERMES_STREAM_RETRIES` | Количество попыток переподключения в середине потока при временных сетевых ошибках (по умолчанию: `3`). |
| `HERMES_STREAM_STALE_GIVEUP` | Перекрестный автоматический выключатель: после такого количества последовательных устаревших уничтожений (потоковых или непоточных) без завершенного ответа немедленно прерывайте каждый вызов с ошибкой, требующей принятия мер, вместо повторного ожидания устаревшего тайм-аута (по умолчанию: `5`, `0` отключает). Сбрасывается при любом завершенном ответе, переключении `/model`, резервной активации или первичном восстановлении с пошаговым запуском. |

| `HERMES_AGENT_TIMEOUT` | Тайм-аут бездействия шлюза для работающего агента в секундах (по умолчанию: `1800`, 30 минут). Сбрасывается при каждом вызове инструмента и потоковом токене. Установите значение `0` для отключения. |
| `HERMES_GATEWAY_MAX_STARTS` | Автоматический выключатель возрождения и шторма: максимальное количество (пере)запусков шлюза, разрешенное в пределах окна, прежде чем экспоненциальная отсрочка засыпает, чтобы остановить шторм (по умолчанию: `5`, `0` отключает). Также можно настроить через `gateway.respawn_storm.max_starts` в `config.yaml`. |
| `HERMES_GATEWAY_START_WINDOW_S` | Окно возрождения-прерывателя шторма в секундах (по умолчанию: `120`). Также можно настроить через `gateway.respawn_storm.window_seconds` в `config.yaml`. |
| `HERMES_AGENT_TIMEOUT_WARNING` | Шлюз: отправить предупреждающее сообщение после указанного количества секунд бездействия (по умолчанию: 75% от `HERMES_AGENT_TIMEOUT`). |
| `HERMES_AGENT_NOTIFY_INTERVAL` | Шлюз: интервал в секундах между уведомлениями о ходе длительных ходов агента. |
| `HERMES_CHECKPOINT_TIMEOUT` | Тайм-аут создания контрольной точки файловой системы в секундах (по умолчанию: `30`). |
| `HERMES_EXEC_ASK` | Включить запросы на подтверждение выполнения в режиме шлюза (`true`/`false`) |
| `HERMES_ENABLE_PROJECT_PLUGINS` | Включите автоматическое обнаружение локальных репо-плагинов из `./.hermes/plugins/` как для загрузчика агента, так и для веб-сервера панели мониторинга. Принимает стандартный набор правдивости: `1` / `true` / `yes` / `on` (регистронезависимо). Все остальное, включая `0`, `false`, `no`, `off` и пустую строку, рассматривается как **отключенное** (по умолчанию). Примечание. Начиная с GHSA-5qr3-c538-wm9j (#29156), веб-сервер панели мониторинга отказывается автоматически импортировать файл Python `api` плагина проекта, даже если эта переменная включена — плагины проекта могут расширять пользовательский интерфейс через статический JS/CSS, но их внутренние маршруты загружаются только при перемещении под `~/.hermes/plugins/`. |
| `HERMES_PLUGINS_DEBUG` | `1`/`true` для отображения подробных журналов обнаружения плагинов на stderr — сканированные каталоги, анализируемые манифесты, причины пропуска и полная обратная трассировка при синтаксическом анализе или сбое `register()`. Предназначен для авторов плагинов. |
| `HERMES_BACKGROUND_NOTIFICATIONS` | Режим уведомления о фоновых процессах на шлюзе: `concise` (по умолчанию), `all`, `result`, `error`, `off` |
| `HERMES_EPHEMERAL_SYSTEM_PROMPT` | Эфемерное системное приглашение, внедряемое во время вызова API (никогда не сохранялось в сеансах) |
| `HERMES_PREFILL_MESSAGES_FILE` | Путь к JSON-файлу эфемерных сообщений предварительного заполнения, добавляемых во время вызова API. |
| `HERMES_ALLOW_PRIVATE_URLS` | `true`/`false` — разрешить инструментам получать URL-адреса локального хоста/частной сети. По умолчанию в режиме шлюза выключено. |
| `HERMES_REDACT_SECRETS` | `true`/`false` — управляйте редактированием секретов в выходных данных инструмента, журналах и ответах чата (по умолчанию: `true`). |
| `HERMES_WRITE_SAFE_ROOT` | Необязательный префикс каталога, который **жестко блокирует** `write_file`/`patch` за пределами перечисленных корней (без запроса на одобрение). Поддерживает несколько каталогов, разделенных `os.pathsep` (`:` в Unix, `;` в Windows). См. [HERMES_WRITE_SAFE_ROOT](#hermes_write_safe_root) ниже. |
| `HERMES_DISABLE_LAZY_INSTALLS` | Внутренняя переменная моста устанавливается автоматически в официальном образе Docker, чтобы предотвратить установку зависимостей во время выполнения в неизменяемое дерево `/opt/hermes`. Эквивалент для пользователя — `security.allow_lazy_installs: false` в `config.yaml`; не устанавливайте это в `.env`. |
| `HERMES_DISABLE_FILE_STATE_GUARD` | Установите значение `1`, чтобы отключить защиту «файл изменился с момента его прочтения» на `patch`/`write_file`. |
| `HERMES_BUNDLED_SKILLS` | Переопределение через запятую для списка связанных навыков, загружаемых при запуске. |
| `HERMES_OPTIONAL_SKILLS` | Разделенный запятыми список имен дополнительных навыков, которые будут автоматически устанавливаться при первом запуске. |
| `HERMES_DEBUG_INTERRUPT` | Установите значение `1` для регистрации подробной трассировки прерываний/отмен в `agent.log`. |
| `HERMES_DUMP_REQUESTS` | Дамп полезных данных запроса API в файлы журналов (`true`/`false`) |
| `HERMES_DUMP_REQUEST_STDOUT` | Сбрасывайте полезные данные запроса API в стандартный вывод вместо файлов журналов. |
| `HERMES_OAUTH_TRACE` | Установите значение `1`, чтобы регистрировать обмен токенами OAuth и попытки обновления. Включает отредактированную информацию о времени. |
| `HERMES_AGENT_HELP_GUIDANCE` | Добавьте дополнительный текст руководства в системное приглашение для пользовательских развертываний. |
| `HERMES_AGENT_LOGO` | Переопределить логотип баннера ASCII при запуске CLI. |
| `DELEGATION_MAX_CONCURRENT_CHILDREN` | Максимальное количество параллельных субагентов на пакет `delegate_task` (по умолчанию: `3`, этаж 1, без потолка). Также конфи

можно настроить через `delegation.max_concurrent_children` в `config.yaml` — значение конфигурации имеет приоритет. |

### HERMES_WRITE_SAFE_ROOT {#hermes_write_safe_root}

Если эта переменная установлена, `write_file` и `patch` могут указывать только на пути внутри перечисленных префиксов каталогов. Любой путь за пределами этих корней **немедленно отклоняется** — запись не проходит через систему одобрения опасных команд, и нет запроса на ее отмену.

Официальный образ Docker устанавливает `HERMES_WRITE_SAFE_ROOT=/opt/data` рядом с `HERMES_HOME=/opt/data`, поэтому агент не может покинуть смонтированный том данных.

**Не добавляйте это в `~/.hermes/.env`, если вы не планируете выполнять запись в песочнице.** Распространенной ошибкой является указание каталога проекта при ожидании, что агент будет редактировать `~/.hermes/cron/jobs.json`, `~/.hermes/skills/` или сценарии в профиле — эти пути находятся за пределами песочницы, и каждый `write_file`/`patch` к ним завершается с ошибкой `outside HERMES_WRITE_SAFE_ROOT`.

Чтобы разрешить как рабочую область, так и состояние Hermes, укажите оба префикса (порядок не имеет значения):

```bash
export HERMES_WRITE_SAFE_ROOT=/path/to/project:/home/you/.hermes
```

Снимите настройку переменной или удалите ее из `.env`, чтобы восстановить нормальную запись (все еще подлежит списку запрещенных путей учетных данных — см. [Безопасность записи файлов](../user-guide/security.md#file-write-safety)).

## Интерфейс

| Переменная | Описание |
|----------|-------------|
| `HERMES_TUI` | Запустите [TUI](../user-guide/tui.md) вместо классического интерфейса командной строки, если для него установлено значение `1`. Эквивалентно передаче `--tui`. |
| `HERMES_TUI_DIR` | Путь к предварительно созданному каталогу `ui-tui/` (должен содержать `dist/entry.js` и заполненный `node_modules`). Используется дистрибутивами и Nix для пропуска первого запуска `npm install`. |
| `HERMES_TUI_RESUME` | Возобновите определенный сеанс TUI по идентификатору при запуске. Если установлено, `hermes --tui` пропускает создание нового сеанса и вместо этого выбирает указанный сеанс, что полезно для повторного подключения после отключения или сбоя терминала. |
| `HERMES_TUI_THEME` | Примените цветовую тему TUI: `light`, `dark` или необработанный шестизначный фоновый шестнадцатеричный код (например, `ffffff` или `1a1a2e`). Если этот параметр не установлен, Hermes автоматически обнаруживает использование `COLORFGBG` и фоновых запросов терминала; эта переменная переопределяет обнаружение на терминалах (Ghostty, Warp, iTerm2 и т. д.), которые не устанавливают `COLORFGBG`. |
| `HERMES_INFERENCE_MODEL` | Принудительно использовать модель для `hermes -z`/`hermes chat` без изменения `config.yaml`. Сочетается с флагом `--provider`. Полезно для вызывающих программ по сценарию (Sweeper, CI, пакетные исполнители), которым необходимо переопределить модель по умолчанию при каждом запуске. |

## Настройки сеанса

| Переменная | Описание |
|----------|-------------|
| `SESSION_IDLE_MINUTES` | Сбрасывать сеансы после N минут бездействия (по умолчанию: 1440) |
| `SESSION_RESET_HOUR` | Час ежедневного сброса в 24-часовом формате (по умолчанию: 4 = 4 утра) |
| `HERMES_SESSION_ID` | **Автоматически экспортируется в каждый подпроцесс инструмента** Создается Hermes (`terminal`, `execute_code`, постоянная оболочка, серверные части Docker/Singularity, запуск делегированного субагента). Устанавливается агентом для текущего идентификатора сеанса; пользовательские сценарии, вызываемые из инструментов, могут прочитать его, чтобы сопоставить свои выходные данные, телеметрию или побочные эффекты с исходным сеансом Hermes. **Не следует устанавливать это значение вручную** — его переопределение из родительской оболочки вступает в силу только вне запуска агента и перезаписывается в тот момент, когда агент запускает сеанс. |
| `AI_AGENT` | **Устанавливается в `hermes-agent` точками входа в интерфейсе командной строки и шлюзе** (только если он еще не установлен во внешней обвязке) и экспортируется в каждую оболочку терминального инструмента, включая удаленные серверные части (Docker, SSH, Modal, Daytona, Singularity, Vercel). Появляющийся межагентный стандарт атрибуции дочерних процессов — универсальный инструмент (например, обнаружение агента Huggingface_hub) считывает его, чтобы знать, что он работает под управлением агента ИИ. Значение соответствует идентификатору Гермеса в общедоступном реестре агентов. Не устанавливайте вручную. |
| `HERMES_AGENT` | **Устанавливается в `true` точками входа CLI и шлюза** и экспортируется в каждую оболочку терминального инструмента, чтобы дочерние процессы могли обнаружить, что они выполняются конкретно внутри Hermes. Не устанавливайте вручную. |

## Сжатие контекста (только config.yaml)

Сжатие контекста настраивается исключительно через `config.yaml` — для него нет переменных окружения. Настройки пороговых значений находятся в блоке `compression:`, а модель/поставщик суммирования — в блоке `auxiliary.compression:`.

```yaml
compression:
  enabled: true
  threshold: 0.50
  target_ratio: 0.20         # fraction of threshold to preserve as recent tail
  protect_last_n: 20         # minimum recent messages to keep uncompressed
```

:::info Устаревшая миграция
Старые конфигурации с `compression.summary_model`, `compression.summary_provider` и `compression.summary_base_url` автоматически переносятся в `auxiliary.compression.*` при первой загрузке.
:::

## Переопределения вспомогательных задач

| Переменная | Описание |
|----------|-------------|
| `AUXILIARY_VISION_PROVIDER` | Переопределить поставщика для задач машинного зрения |
| `AUXILIARY_VISION_MODEL` | Переопределить модель для задач машинного зрения |
| `AUXILIARY_VISION_BASE_URL` | Direct OpenAI-совместимая конечная точка для задач машинного зрения |
| `AUXILIARY_VISION_API_KEY` | Ключ API в паре с `AUXILIARY_VISION_BASE_URL` |

:::примечание
Переменные `AUXILIARY_WEB_EXTRACT_*` устарели: `web_extract` и снимки браузера больше не используют вспомогательный LLM. Длинные страницы и снимки детерминированно усекаются, а полный текст сохраняется на диске для подкачки `read_file`.
:::

Для прямых конечных точек, специфичных для конкретной задачи, Hermes использует настроенный ключ API задачи или `OPENAI_API_KEY`. Он не использует повторно `OPENROUTER_API_KEY` для этих пользовательских конечных точек.

## Резервные поставщики (только config.yaml)

Резервная цепочка первичной модели настраивается исключительно через `config.yaml` — для нее нет переменных среды. Добавьте список `fallback_providers` верхнего уровня с ключами `provider` и `model`, чтобы включить автоматический переход на другой ресурс при возникновении ошибок в основной модели. Вспомогательные задачи, поставщиком которых является `auto`, также обращаются к этой цепочке перед встроенной вспомогательной цепочкой обнаружения Hermes.

```yaml
fallback_providers:
  - provider: openrouter
    model: anthropic/claude-sonnet-4
```

Старая форма верхнего уровня `fallback_model` с одним поставщиком по-прежнему читается для обратной совместимости, но в новой конфигурации следует использовать `fallback_providers`. Для дополнительной политики для конкретной задачи используйте `auxiliary.<task>.fallback_chain` в `config.yaml`; нет эквивалента переменной среды.

Подробную информацию см. в разделе [Резервные поставщики](/user-guide/features/fallback-providers).

## Маршрутизация провайдера (только config.yaml)

Они находятся в `~/.hermes/config.yaml` в разделе `provider_routing`:

| Ключ | Описание |
|-----|-------------|
| `sort` | Поставщики сортировки: `"price"` (по умолчанию), `"throughput"` или `"latency"` |
| `only` | Список разрешенных пулов провайдеров (например, `["anthropic", "google"]`) |
| `ignore` | Список сообщений провайдеров, которые следует пропустить |
| `order` | Список провайдеров, которые можно попробовать по порядку |
| `require_parameters` | Используйте только поставщиков, поддерживающих все параметры запроса (`true`/`false`) |
| `data_collection` | `"allow"` (по умолчанию) или `"deny"` для исключения поставщиков услуг хранения данных |

:::совет
Используйте `hermes config set` для установки переменных среды — они автоматически сохраняют их в нужный файл (`.env` для секретов, `config.yaml` для всего остального).
:::