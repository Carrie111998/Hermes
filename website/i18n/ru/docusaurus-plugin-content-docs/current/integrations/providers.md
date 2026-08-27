---
title: LLM и поставщики моделей
sidebar_label: AI Providers
sidebar_position: 1
---

# LLM и поставщики моделей

На этой странице описывается настройка поставщиков логических выводов для агента Hermes — от облачных API, таких как OpenRouter и Anthropic, до самостоятельных конечных точек, таких как Ollama и vLLM, а также расширенных конфигураций маршрутизации и резервных конфигураций. Вам нужен хотя бы один провайдер, настроенный для использования Hermes.

## Поставщики выводов

Вам нужен хотя бы один способ подключения к LLM. Используйте `hermes model` для интерактивного переключения поставщиков и моделей или настройте напрямую:

| Провайдер | Настройка |
|----------|-------|
| **Портал Ноус** | `hermes model` (OAuth, на основе подписки) |
| **Кодекс OpenAI** | `hermes model` → **ChatGPT или подписка Codex** (ChatGPT OAuth, использует модели Codex) |
| **Второй пилот GitHub** | `hermes model` (поток кода устройства OAuth, `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` или `gh auth token`) |
| **GitHub Copilot ACP** | `hermes model` (порождает локальный `copilot --acp --stdio`) |
| **Антропный** | `hermes model` (Клод Макс + дополнительные кредиты на использование через OAuth; также поддерживает ключ Anthropic API или токен ручной настройки — см. примечание ниже) |
| **OpenRouter** | `OPENROUTER_API_KEY` в `~/.hermes/.env` |
| **Фейерверк ИИ** | `FIREWORKS_API_KEY` в `~/.hermes/.env` (поставщик: `fireworks`; псевдонимы: `fireworks-ai`, `fw`) |
| **НовитаАИ** | `NOVITA_API_KEY` в `~/.hermes/.env` (поставщик: `novita`, более 200 моделей, Model API, тестовая среда агента, облако графического процессора) |
| **Шлюз искусственного интеллекта** | `AI_GATEWAY_API_KEY` в `~/.hermes/.env` (поставщик: `ai-gateway`) |
| **z.ai / GLM** | `GLM_API_KEY` в `~/.hermes/.env` (поставщик: `zai`) |
| **Кими / Муншот** | `KIMI_API_KEY` в `~/.hermes/.env` (поставщик: `kimi-coding`) |
| **Кими / Муншот (Китай)** | `KIMI_CN_API_KEY` в `~/.hermes/.env` (поставщик: `kimi-coding-cn`; псевдонимы: `kimi-cn`, `moonshot-cn`) |
| **Арси ИИ** | `ARCEEAI_API_KEY` в `~/.hermes/.env` (поставщик: `arcee`; псевдонимы: `arcee-ai`, `arceeai`) |
| **Облако GMI** | `GMI_API_KEY` в `~/.hermes/.env` (поставщик: `gmi`; псевдонимы: `gmi-cloud`, `gmicloud`) |
| **Настоящий компьютер** | `ACTUAL_API_KEY` в `~/.hermes/.env` для размещенного ретранслятора или `ACTUAL_BASE_URL=http://127.0.0.1:8080` для локального демона — при обратной связи ключ не требуется (поставщик: `actual`; псевдонимы: `actual-computer`, `actualcomputer`, `aci`) |
| **МиниМакс** | `MINIMAX_API_KEY` в `~/.hermes/.env` (поставщик: `minimax`) |
| **МиниМакс Китай** | `MINIMAX_CN_API_KEY` в `~/.hermes/.env` (поставщик: `minimax-cn`) |
| **xAI (Grok) — API ответов** | `XAI_API_KEY` в `~/.hermes/.env` (поставщик: `xai`) |
| **xAI Grok OAuth (SuperGrok)** | `hermes model` → «xAI Grok OAuth (SuperGrok / Premium+)» — вход через браузер, без ключа API. См. [руководство](../guides/xai-grok-oauth.md) |
| **Qwen Cloud (Alibaba DashScope)** | `DASHSCOPE_API_KEY` в `~/.hermes/.env` (поставщик: `alibaba`) |
| **Облако Alibaba (план кодирования)** | `DASHSCOPE_API_KEY` (поставщик: `alibaba-coding-plan`, псевдоним: `alibaba_coding`) — отдельный номер SKU для выставления счетов, другая конечная точка |
| **Код килограмма** | `KILOCODE_API_KEY` в `~/.hermes/.env` (поставщик: `kilocode`) |
| **Xiaomi MiMo** | `XIAOMI_API_KEY` в `~/.hermes/.env` (поставщик: `xiaomi`, псевдонимы: `mimo`, `xiaomi-mimo`) |
| **Tencent TokenHub** | `TOKENHUB_API_KEY` в `~/.hermes/.env` (поставщик: `tencent-tokenhub`, псевдонимы: `tencent`, `tokenhub`, `tencentmaas`) |
| **OpenCode Дзен** | `OPENCODE_ZEN_API_KEY` в `~/.hermes/.env` (поставщик: `opencode-zen`) |
| **Кодкоманды** | `COMMANDCODE_API_KEY` в `~/.hermes/.env` (поставщик: `commandcode`, псевдоним: `commandcode-chat`; Клод моделирует через `commandcode-anthropic`, псевдоним: `commandcode-claude`). Работает с планами GOAT/Pro/Max/Provider (кроме плана Go за 1 доллар — без доступа к API). |
| **OpenCode Go** | `OPENCODE_GO_API_KEY` в `~/.hermes/.env` (поставщик: `opencode-go`) |
| **Бесплатный OpenCode** | Бесключевой доступ — ключ API или учетная запись не требуются (поставщик: `opencode-free`, псевдонимы: `free`, `opencode_free`). Выберите через `hermes model` или `/model free`; запросы отправляются анонимно |
| **Глубокий поиск** | `DEEPSEEK_API_KEY` в `~/.hermes/.env` (поставщик: `deepseek`) |
| **Обнимающее лицо** | `HF_TOKEN` в `~/.hermes/.env` (поставщик: `huggingface`, псевдонимы: `hf`) |
| **Google/Близнецы** | `GOOGLE_API_KEY` (или `GEMINI_API_KEY`) в `~/.hermes/.env` (поставщик: `gemini`) |
| **Google Vertex AI** | `hermes model` → «Google Vertex AI» (поставщик: `vertex`; OAuth2 через JSON или ADC сервисного аккаунта, выставление счетов GCP) |
| **OpenAI API (прямой)** | `OPENAI_API_KEY` в `~/.hermes/.env` (поставщик: `openai-api`, необязательно `OPENAI_BASE_URL`) |
| **Azure AI Foundry** | `hermes model` → «Azure AI Foundry» (поставщик: `azure-foundry`; использует конечную точку и ключ Azure OpenAI/Foundry) |
| **основание AWS** | `hermes model` → «AWS Bedrock» (поставщик: `bedrock`; стандартная цепочка учетных данных AWS через boto3) |
| **Сборка NVIDIA** | `NVIDIA_API_KEY` в `~/.hermes/.env` (поставщик: `nvidia`; модели, размещенные на NIM, на build.nvidia.com) |
| **Оллама Облако** | `hermes model` → «Ollama Cloud» (поставщик: `ollama-cloud`; облачный API Ollama) |
| **Квен OAuth** | `hermes model` → «Qwen OAuth» (провайдер: `qwen-oauth`; вход в браузер PKCE) |
| **МиниМакс OAuth** | `hermes model` → «MiniMax (OAuth)» (провайдер: `minimax-oauth`; вход в браузер PKCE) |
| **СтепФан** | `STEPFUN_API_KEY` в `~/.hermes/.env` (поставщик: `stepfun`) |
| **ЛМ Студия** | `hermes model` → «LM Studio» (поставщик: `lmstudio`, дополнительно `LM_API_KEY`) |
| **Пользовательская конечная точка** | `hermes model` → выберите «Пользовательская конечная точка» (сохранено в `config.yaml`) |

Официальный путь к ключу API см. в специальном [руководстве по Google Gemini](/guides/google-gemini).

:::tip Псевдоним ключа модели
В разделе конфигурации `model:` вы можете использовать `default:` или `model:` в качестве имени ключа для идентификатора вашей модели. И `model: { default: my-model }`, и `model: { model: my-model }` работают одинаково.
:::


### Портал Ноуса

[Nous Portal](https://portal.nousresearch.com) — это единый шлюз подписки Nous Research и **рекомендуемый способ запуска агента Hermes**. Один вход OAuth охватывает более 300 пограничных агентных моделей (Claude, GPT, Gemini, DeepSeek, Qwen, Kimi, GLM, MiniMax, Grok, ...), а также [Tool Gateway](/user-guide/features/tool-gateway) (веб-поиск, генерация изображений, TTS, автоматизация браузера) — счет взимается в рамках вашей подписки Nous, а не отдельных учетных записей каждого поставщика.

```bash
hermes setup --portal     # fresh install — OAuth + provider + gateway in one command
hermes model              # existing install — pick "Nous Portal" from the list
hermes portal info        # inspect login + routing at any time
```

У вас еще нет подписки? Получите его на [portal.nousresearch.com/manage-subscription](https://portal.nousresearch.com/manage-subscription).

**Для получения полной информации:** см. специальную [страницу интеграции Nous Portal](/integrations/nous-portal) (содержимое подписки, каталог моделей, устранение неполадок) и пошаговое [Руководство по запуску агента Hermes с Nous Portal](/guides/run-hermes-with-nous-portal).

**Идентификация клиента.** Каждый запрос портала от агента Hermes содержит тег `client=hermes-client-v<version>` (например, `client=hermes-client-v0.13.0`), автоматически привязанный к установленной вами версии. Он отправляется по всем путям Портала — основной цикл чата, дополнительные вызовы, сумматор сжатия, веб-извлечение — и позволяет телеметрии на стороне Портала отличать трафик Hermes от трафика других клиентов. Никакой конфигурации не требуется; тег обновляется автоматически, когда вы `hermes update`.

**Аутентификация JWT (автоматическая).** Hermes предпочитает JWT с ограниченной областью действия `inference:invoke` для запросов портала с устаревшим непрозрачным путем сеансового ключа в качестве запасного варианта. Никакой настройки не требуется — учетные данные управляются потоком OAuth и меняются прозрачно. Отозванные токены обновления помещаются в карантин, чтобы избежать циклов воспроизведения.


:::info Примечание Кодекса
Поставщик OpenAI Codex выполняет аутентификацию через код устройства (откройте URL-адрес, введите код). Hermes сохраняет полученные учетные данные в своем собственном хранилище аутентификации под `~/.hermes/auth.json` и может импортировать существующие учетные данные Codex CLI из `~/.codex/auth.json`, если они есть. Установка Codex CLI не требуется.

Если обновление токена завершается сбоем из-за ошибки терминала (HTTP 4xx, `invalid_grant`, отозван грант и т. д.), Hermes помечает токен обновления как мертвый и прекращает его воспроизведение, чтобы вы не видели поток идентичных ошибок аутентификации. Вместо этого следующий запрос отображает типизированное сообщение повторной аутентификации. Запустите `hermes auth add openai-codex` (или `hermes model` → **ChatGPT или Codex Subscription**), чтобы начать новый вход в систему с использованием кода устройства; Карантин снимается при следующем успешном обмене.
:::

:::предупреждение
Даже при использовании Nous Portal, Codex или пользовательской конечной точки некоторые инструменты (зрение, веб-суммирование, MoA) используют отдельную «вспомогательную» модель. По умолчанию (`auxiliary.*.provider: "auto"`) Hermes направляет эти задачи в вашу **основную модель чата** — ту же модель, которую вы выбрали в `hermes model`. Вы можете переопределить каждую задачу индивидуально, чтобы перенаправить ее на более дешевую/быструю модель (например, Gemini Flash на OpenRouter) — см. [Вспомогательные модели](/user-guide/configuration#auxiliary-models).
:::

:::tip Шлюз инструментов Nous
Платные подписчики Nous Portal также получают доступ к **[Tool Gateway](/user-guide/features/tool-gateway)** — веб-поиску, генерации изображений, TTS и автоматизации браузера, которые осуществляются через вашу подписку. Никаких дополнительных ключей API не требуется. При новой установке `hermes setup --portal` регистрирует вас, устанавливает Nous в качестве провайдера и включает шлюз одной командой. Существующие пользователи могут включить его с `hermes model` или для каждого инструмента с `hermes tools`. Проверяйте маршрутизацию в любое время с помощью `hermes portal info`.
:::

### Две команды для управления моделью

В Hermes есть **две** команды модели, которые служат разным целям:

| Команда | Куда бежать | Что он делает |
|---------|-------------|--------------|
| **`hermes model`** | Ваш терминал (вне сеанса) | Мастер полной настройки — добавьте провайдеров, запустите OAuth, введите ключи API, настройте конечные точки |
| **`/model`** | Внутри чата Hermes | Быстрое переключение между **уже настроенными** поставщиками и моделями |

Если вы пытаетесь переключиться на провайдера, который еще не настроили (например, у вас настроен только OpenRouter и вы хотите использовать Anthropic), вам понадобится `hermes model`, а не `/model`. Сначала выйдите из сеанса (`Ctrl+C` или `/quit`), запустите `hermes model`, завершите настройку поставщика, а затем начните новый сеанс.


### Планы подписки: за что платит ваш план

Некоторые провайдеры позволяют вам войти в Hermes с помощью **потребительской подписки** (Claude Max, ChatGPT, SuperGrok / X Premium+, …) вместо ключа API. То, за что на самом деле платит эта подписка, а что нет, зависит от провайдера, и это единственный наиболее распространенный источник сюрпризов при выставлении счетов. Таблица ниже представляет собой сокращенную версию; Подробности есть в разделе каждого провайдера.

> Ячейки, помеченные *не документированы*, означают именно это: документы Hermes еще не определяют поведение. Не предполагайте — проверьте панель выставления счетов вашего провайдера и относитесь к ним как к открытым вопросам.

| План/путь | Может ли Гермес использовать его? | Что потребляется | Что НЕ потребляется | Общий сюрприз |
|---|---|---|---|---|
| **Антропный — Клод Макс + OAuth** | ✅ Да — `hermes model` → Антропный OAuth. Требуется Макс **и** приобретенные дополнительные кредиты на использование | **Дополнительные/избыточные баллы**, добавленные вами к плану Max | **Базовый максимальный лимит** (использование включено в Claude Code по умолчанию) | Все счета за использование Hermes выставляются как «дополнительные расходы», даже если включенная вами максимальная норма остается нетронутой |
| **Антропный — Клод Про** | ❌ Нет — подписчики Pro не могут использовать путь OAuth | Ничего (путь недоступен) | Ваша подписка Pro | Похоже, Pro должно работать; это не так. Вместо этого используйте `ANTHROPIC_API_KEY` (оплата за токен, независимо от какой-либо подписки Claude) |
| **Кодекс OpenAI — план ChatGPT OAuth** | ✅ Да — `hermes model` → **Подписка ChatGPT или Codex** (вход по коду устройства ChatGPT OAuth, используются модели Codex) | *В настоящее время не документировано* | *В настоящее время не документировано* | Документы охватывают только аутентификацию и обновление токена; семантика план-квота еще не документирована |
| **xAI — SuperGrok / X Premium+ OAuth** | ✅ Да — OAuth браузера, ключ API не требуется | Ваша **квота подписки** (явно задокументированная для X Search: OAuth предпочтительнее ключа API и «использует вашу квоту подписки вместо расходов API»). Семантика квоты вывода помимо этого: *в настоящее время не документирована* | `XAI_API_KEY` / расходы на API с оплатой за токен, если учетные данные OAuth настроены и являются предпочтительными | `HTTP 403` после успешного входа в систему — xAI ограничил доступ API OAuth к определенным уровням SuperGrok, несмотря на активную подписку в приложении |
| **Google — потребительский план Gemini (Google AI Pro/Ultra)** | ❌ Нет документированного пути — поставщик `gemini` предоставляет только ключ API (`GOOGLE_API_KEY` / `GEMINI_API_KEY`); Vertex AI использует биллинг GCP | Ваша **квота ключа API** (уровень бесплатного пользования или проект Google Cloud с включенной оплатой) — *потребление по потребительскому плану в настоящее время не документировано* | *В настоящее время не документировано* | Ключи бесплатного уровня могут быть исчерпаны после нескольких ходов агента, поскольку Hermes может сделать несколько вызовов моделей за один ход пользователя |

**Anthropic.** Путь OAuth маршрутизируется как код Claude для вашей учетной записи Anthropic и **работает только в плане Claude Max с купленными дополнительными кредитами на использование** — базовый лимит Max никогда не расходуется Hermes, а только дополнительные/избыточные кредиты сверху. Подписчики Claude Pro не могут использовать этот путь; Поддерживаемой альтернативой является `ANTHROPIC_API_KEY`, оплата за токен осуществляется организацией, использующей этот ключ, по стандартной цене API. См. [Антропный (Родной)](#антропный-родной) ниже.

**Кодекс OpenAI.** Гермес проходит аутентификацию через OAuth с кодом устройства ChatGPT, сохраняет учетные данные в `~/.hermes/auth.json` и может импортировать существующие учетные данные Codex CLI из `~/.codex/auth.json`. Какие уровни плана ChatGPT соответствуют критериям и как использование Hermes учитывается в рамках ограничений Кодекса вашего плана, **в настоящее время не документировано** — примечание Кодекса в разделе [Nous Portal](#nous-portal) описывает только аутентификацию и обновление токенов.

**xAI (SuperGrok / X Premium+).** OAuth браузера работает либо с активной подпиской SuperGrok, либо с подпиской X Premium+ в связанной учетной записи X, и один и тот же токен носителя повторно используется инструментами прямого доступа к xAI (TTS, генерация изображений, генерация видео, транскрипция, поиск X). Если вывод возвращает `HTTP 403` после успешного входа в систему, это ограничение уровня/прав на стороне xAI, а не устаревший токен — обходным путем является переключение на `XAI_API_KEY`. См. [xAI (Grok)](#xai-grok--responses-api--prompt-caching) ниже и [руководство по xAI Grok OAuth](../guides/xai-grok-oauth.md).

**Google Gemini.** В настоящее время невозможно войти в Hermes с потребительской подпиской Gemini — поставщик `gemini` берет ключ API, и [Google Vertex AI](#google-vertex-ai) выставляет счет вашему проекту GCP. Для использования агентом рекомендуется проект Google Cloud с поддержкой биллинга; Квоты бесплатного уровня слишком малы для длительных сеансов агента. См. [руководство Google Gemini](/guides/google-gemini).

:::tip Одна подписка вместо пяти
Если вы вообще не хотите отслеживать семантику планов для каждого поставщика, [Nous Portal](#nous-portal) охватывает более 300 моделей в рамках одной подписки с одним входом в систему OAuth.
:::

### Антропный (Родной)

Используйте модели Claude напрямую через Anthropic API — прокси-сервер OpenRouter не требуется. Поддерживает три метода аутентификации:

:::Осторожно. Требуются кредиты Клода Макса на «дополнительное использование».
Когда вы проходите аутентификацию через `hermes model` → Anthropic OAuth (или через `hermes auth add anthropic --type oauth`), Hermes маршрутизируется как Claude Code к вашей учетной записи Anthropic. **Это работает только в том случае, если вы пользуетесь планом Claude Max и приобрели дополнительные кредиты на использование.** Базовый лимит плана Max (использование, включенное в Claude Code по умолчанию) не расходуется Hermes — используются только дополнительные/избыточные кредиты, которые вы добавили сверху. Подписчики Claude Pro не могут использовать этот путь.

Если у вас нет дополнительных кредитов Max +, вместо этого используйте `ANTHROPIC_API_KEY` — запросы оплачиваются с оплатой за токен в зависимости от организации этого ключа (стандартные цены API, не зависящие от какой-либо подписки Claude).
:::

```bash
# With an API key (pay-per-token)
export ANTHROPIC_API_KEY=***
hermes chat --provider anthropic --model claude-sonnet-4-6

# Preferred: authenticate through `hermes model`
# Hermes will use Claude Code's credential store directly when available
hermes model

# Manual override with a setup-token (fallback / legacy)
export ANTHROPIC_TOKEN=***  # setup-token or manual OAuth token
hermes chat --provider anthropic

# Auto-detect Claude Code credentials (if you already use Claude Code)
hermes chat --provider anthropic  # reads Claude Code credential files automatically
```

Когда вы выбираете Anthropic OAuth через `hermes model`, Hermes предпочитает собственное хранилище учетных данных Claude Code, а не копирование токена в `~/.hermes/.env`. Это позволяет обновлять учетные данные Claude.

Или установите его навсегда:
```yaml
model:
  provider: "anthropic"
  default: "claude-sonnet-4-6"
```

:::подсказка Псевдонимы
`--provider claude` и `--provider claude-code` также служат сокращением для `--provider anthropic`.
:::

### Второй пилот GitHub

Hermes поддерживает GitHub Copilot как первоклассного провайдера с двумя режимами:

**`copilot` — API Direct Copilot** (рекомендуется). Использует вашу подписку GitHub Copilot для доступа к моделям GPT-5.x, Claude, Gemini и другим через Copilot API.

```bash
hermes chat --provider copilot --model gpt-5.4
```

**Параметры аутентификации** (проверяются в следующем порядке):

1. Переменная среды `COPILOT_GITHUB_TOKEN`.
2. Переменная среды `GH_TOKEN`.
3. Переменная среды `GITHUB_TOKEN`.
4. `gh auth token` Резервный вариант CLI

Если токен не найден, `hermes model` предлагает **вход с использованием кода устройства OAuth** — тот же процесс, который используется Copilot CLI и открытым кодом.

:::предупреждение Типы токенов
Copilot API **не** поддерживает классические токены личного доступа (`ghp_*`). Поддерживаемые типы токенов:

| Тип | Префикс | Как получить |
|------|--------|------------|
| Токен OAuth | `gho_` | `hermes model` → GitHub Copilot → Войти через GitHub |
| Мелкозернистый PAT | `github_pat_` | Настройки GitHub → Настройки разработчика → Детализированные токены (требуется разрешение **Запросы второго пилота**) |
| Токен приложения GitHub | `ghu_` | Через установку приложения GitHub |

Если ваш `gh auth token` возвращает токен `ghp_*`, вместо этого используйте `hermes model` для аутентификации через OAuth.
:::

:::info Поведение аутентификации Copilot в Hermes
Hermes отправляет поддерживаемый токен GitHub (`gho_*`, `github_pat_*` или `ghu_*`) непосредственно на `api.githubcopilot.com` и включает заголовки, специфичные для Copilot (`Editor-Version`, `Copilot-Integration-Id`, `Openai-Intent`, `x-initiator`).

По HTTP 401 Hermes теперь выполняет однократное восстановление учетных данных перед откатом:

1. Повторно разрешить токен через обычную цепочку приоритетов (`COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN` → `gh auth token`).
2. Пересоберите общий клиент OpenAI с обновленными заголовками.
3. Повторите запрос один раз.

Некоторые старые прокси-серверы сообщества используют потоки обмена `api.github.com/copilot_internal/v2/token`. Эта конечная точка может быть недоступна для некоторых типов учетных записей (возвращает 404). Поэтому Hermes сохраняет прямую аутентификацию по токену в качестве основного пути и полагается на обновление учетных данных во время выполнения + повторную попытку для обеспечения надежности.
:::

**Маршрутизация API**: модели GPT-5+ (кроме `gpt-5-mini`) автоматически используют API ответов. Все остальные модели (GPT-4o, Claude, Gemini и т. д.) используют завершение чата. Модели автоматически определяются из актуального каталога Copilot.

**`copilot-acp` — Серверная часть агента Copilot ACP**. Создает локальный CLI Copilot как подпроцесс:

```bash
hermes chat --provider copilot-acp --model copilot-acp
# Requires the GitHub Copilot CLI in PATH and an existing `copilot login` session
```

**Постоянная конфигурация:**
```yaml
model:
  provider: "copilot"
  default: "gpt-5.4"
```

| Переменная среды | Описание |
|-----|-----------------------------|
| `COPILOT_GITHUB_TOKEN` | Токен GitHub для API Copilot (первый приоритет) |
| `HERMES_COPILOT_ACP_COMMAND` | Переопределить двоичный путь CLI Copilot (по умолчанию: `copilot`) |
| `HERMES_COPILOT_ACP_ARGS` | Переопределить аргументы ACP (по умолчанию: `--acp --stdio`) |

### Первоклассные поставщики ключей API

Эти поставщики имеют встроенную поддержку с выделенными идентификаторами поставщиков. Установите ключ API и используйте `--provider`, чтобы выбрать:

```bash
# Fireworks AI
hermes chat --provider fireworks --model accounts/fireworks/models/kimi-k2p6
# Requires: FIREWORKS_API_KEY in ~/.hermes/.env

# NovitaAI Model API
hermes chat --provider novita --model moonshotai/kimi-k2.5
# Requires: NOVITA_API_KEY in ~/.hermes/.env

# z.ai / ZhipuAI GLM
hermes chat --provider zai --model glm-5
# Requires: GLM_API_KEY in ~/.hermes/.env

# Kimi / Moonshot AI (international: api.moonshot.ai)
hermes chat --provider kimi-coding --model kimi-for-coding
# Requires: KIMI_API_KEY in ~/.hermes/.env

# Kimi / Moonshot AI (China: api.moonshot.cn)
hermes chat --provider kimi-coding-cn --model kimi-k2.5
# Requires: KIMI_CN_API_KEY in ~/.hermes/.env

# MiniMax (global endpoint)
hermes chat --provider minimax --model MiniMax-M2.7
# Requires: MINIMAX_API_KEY in ~/.hermes/.env

# MiniMax (China endpoint)
hermes chat --provider minimax-cn --model MiniMax-M2.7
# Requires: MINIMAX_CN_API_KEY in ~/.hermes/.env

# Qwen Cloud / DashScope (Qwen models)
hermes chat --provider alibaba --model qwen3.5-plus
# Requires: DASHSCOPE_API_KEY in ~/.hermes/.env

# Xiaomi MiMo
hermes chat --provider xiaomi --model mimo-v2-pro
# Requires: XIAOMI_API_KEY in ~/.hermes/.env

# Tencent TokenHub (Hy3 Preview)
hermes chat --provider tencent-tokenhub --model hy3-preview
# Requires: TOKENHUB_API_KEY in ~/.hermes/.env

# Arcee AI (Trinity models)
hermes chat --provider arcee --model trinity-large-thinking
# Requires: ARCEEAI_API_KEY in ~/.hermes/.env

# Meta Model API (Muse Spark family)
hermes chat --provider meta-ai --model muse-spark-1.2
# Requires: MODEL_API_KEY in ~/.hermes/.env

# GMI Cloud
# Use the exact model ID returned by GMI's /v1/models endpoint.
hermes chat --provider gmi --model zai-org/GLM-5.1-FP8
# Requires: GMI_API_KEY in ~/.hermes/.env
```

Fireworks использует собственные идентификаторы каталога в форме косой черты, например `accounts/fireworks/models/kimi-k2p6`. Запустите `hermes model`, выберите **Fireworks AI** и выберите его из оперативного каталога или введите другой идентификатор модели Fireworks. Конечная точка по умолчанию — `https://api.fireworks.ai/inference/v1`; настройте другую конечную точку через `model.base_url` в `config.yaml`, а не через `.env`.

Или навсегда установите провайдера в `config.yaml`:
```yaml
model:
  provider: "gmi"
  default: "zai-org/GLM-5.1-FP8"
```

Базовые URL-адреса можно переопределить с помощью переменных среды `NOVITA_BASE_URL`, `GLM_BASE_URL`, `KIMI_BASE_URL`, `MINIMAX_BASE_URL`, `MINIMAX_CN_BASE_URL`, `DASHSCOPE_BASE_URL`, `XIAOMI_BASE_URL`, `GMI_BASE_URL`, `META_BASE_URL` или `TOKENHUB_BASE_URL`.

:::обратите внимание на мета-уровень участника
`muse-spark-1.2-contributor` — это уровень со скидкой для Meta. Meta может обучаться на основе ваших подсказок и дополнений, поэтому [интерактивный выбор модели запрашивает подтверждение](../user-guide/configuring-models.md) перед его использованием. Используйте `muse-spark-1.2` (стандартная цена, без обучения) для конфиденциальной работы.
:::

:::Примечание: Автоматическое обнаружение конечной точки Z.AI
При использовании поставщика Z.AI/GLM Hermes автоматически проверяет несколько конечных точек (глобальные, китайские, варианты кодирования), чтобы найти ту, которая принимает ваш ключ API. Вам не нужно устанавливать `GLM_BASE_URL` вручную — рабочая конечная точка обнаруживается и кэшируется автоматически.
:::

### xAI (Grok) — API ответов + кэширование подсказок

xAI подключен через API ответов (транспорт `codex_responses`) для поддержки автоматического рассуждения в моделях Grok 4 — параметр `reasoning_effort` не требуется, сервер рассуждает по умолчанию. Установите `XAI_API_KEY` в `~/.hermes/.env` и выберите xAI в `hermes model` или перетащите `grok` в качестве ярлыка в `/model grok-4-fast-reasoning`.

Подписчики SuperGrok и X Premium+ могут войти в систему с помощью OAuth браузера вместо использования ключа API — выберите **xAI Grok OAuth (SuperGrok / Premium+)** в `hermes model` или запустите `hermes auth add xai-oauth`. Один и тот же токен носителя OAuth автоматически повторно используется инструментами прямого доступа к xAI (TTS, генерация изображений, генерация видео, транскрипция). Подробную информацию см. в [руководстве по xAI Grok OAuth](../guides/xai-grok-oauth.md), а если Hermes работает на удаленном хосте, также см. [OAuth over SSH/Remote Hosts](../guides/oauth-over-ssh.md) для получения информации о необходимом туннеле `ssh -L`.

При использовании xAI в качестве поставщика (любой базовый URL-адрес, содержащий `x.ai`), Hermes автоматически включает кэширование подсказок, отправляя заголовок `x-grok-conv-id` с каждым запросом API. Это направляет запросы на один и тот же сервер в рамках сеанса разговора, позволяя инфраструктуре xAI повторно использовать кэшированные системные подсказки и историю разговоров.

Никакой настройки не требуется — кэширование активируется автоматически, когда обнаруживается конечная точка xAI и доступен идентификатор сеанса. Это уменьшает задержку и стоимость многооборотных разговоров.

xAI также предоставляет выделенную конечную точку TTS (`/v1/tts`). Выберите **xAI TTS** в `hermes tools` → Голос и TTS или см. страницу [Голос и TTS](../user-guide/features/tts.md#text-to-speech) для настройки.

**Миграция устаревшей модели xAI (15 мая 2026 г.):** xAI прекращает поддержку моделей `grok-4*`, `grok-3`, `grok-code-fast-1` и `grok-imagine-image-pro` 15 мая 2026 г. `hermes doctor` и `hermes chat` при запуске обнаруживают любую конфигурацию, все еще указывающую на устаревшую ссылку, и печатают рекомендуемую замену. Используйте `hermes migrate xai` для однократной перезаписи конфигурации — по умолчанию пробный запуск, добавьте `--apply` для записи изменений (резервная копия `config.yaml.bak-pre-migrate-xai-*` с отметкой времени создается автоматически).

```bash
hermes migrate xai          # preview replacements
hermes migrate xai --apply  # rewrite ~/.hermes/config.yaml in place
```

**Бэкенд веб-поиска xAI.** Если включен набор инструментов [Веб-поиск](../user-guide/features/web-search.md), `web.backend: xai` направляет поиск через размещенную конечную точку поиска xAI, используя те же учетные данные `XAI_API_KEY` / OAuth. Никакой дополнительной настройки не требуется, если xAI уже настроен в качестве поставщика.

### НовитаАИ

[NovitaAI](https://novita.ai) — это облако с искусственным интеллектом для строителей и агентов. Три линейки продуктов компании — Model API для более чем 200 моделей, Agent Sandbox для создания и запуска ИИ-агентов и GPU Cloud для масштабируемых вычислений — все они доступны на одной платформе.

```bash
# Use any available model
hermes chat --provider novita --model moonshotai/kimi-k2.5
# Requires: NOVITA_API_KEY in ~/.hermes/.env

# Short alias
hermes chat --provider novita-ai --model deepseek/deepseek-v3-0324
```

Или установите его навсегда в `config.yaml`:
```yaml
model:
  provider: "novita"
  default: "moonshotai/kimi-k2.5"
  base_url: "https://api.novita.ai/openai/v1"
```

Получите ключ API по адресу [novita.ai/settings/key-management](https://novita.ai/settings/key-management). Базовый URL-адрес можно переопределить с помощью `NOVITA_BASE_URL`.

### Ollama Cloud — управляемые модели Ollama, OAuth + ключ API

[Ollama Cloud](https://ollama.com/cloud) содержит тот же открытый каталог, что и локальный Ollama, но без требований к графическому процессору. Выберите его в `hermes model` как **Ollama Cloud**, вставьте свой ключ API с сайта [ollama.com/settings/keys](https://ollama.com/settings/keys), и Hermes автоматически обнаружит доступные модели.

```bash
hermes model
# → pick "Ollama Cloud"
# → paste your OLLAMA_API_KEY
# → select from discovered models (gpt-oss:120b, glm-4.6:cloud, qwen3-coder:480b-cloud, etc.)
```

Или напрямую `config.yaml`:
```yaml
model:
  provider: "ollama-cloud"
  default: "gpt-oss:120b"
```

Каталог моделей динамически извлекается из `ollama.com/v1/models` и кэшируется на один час. Обозначение `model:tag` (например, `qwen3-coder:480b-cloud`) сохраняется при нормализации — не используйте дефисы.

:::tip Оллама Клауд против местной Олламы
Оба используют один и тот же OpenAI-совместимый API. Облако — первоклассный провайдер (`--provider ollama-cloud`, `OLLAMA_API_KEY`); локальный Ollama доступен через поток пользовательской конечной точки (базовый URL-адрес `http://localhost:11434/v1`, без ключа). Используйте облако для больших моделей, которые невозможно запустить локально; используйте local для конфиденциальности или работы в автономном режиме.
:::

### Основа AWS

Anthropic Claude, Amazon Nova, DeepSeek v3.2, Meta Llama 4 и другие модели через AWS Bedrock. Использует цепочку учетных данных AWS SDK (`boto3`) — без ключа API, только стандартная аутентификация AWS.

```bash
# Simplest — named profile in ~/.aws/credentials
hermes chat --provider bedrock --model us.anthropic.claude-sonnet-4-6

# Or with explicit env vars
AWS_PROFILE=myprofile AWS_REGION=us-east-1 hermes chat --provider bedrock --model us.anthropic.claude-sonnet-4-6
```

Или навсегда в `config.yaml`:
```yaml
model:
  provider: "bedrock"
  default: "us.anthropic.claude-sonnet-4-6"
bedrock:
  region: "us-east-1"          # or set AWS_REGION
  # profile: "myprofile"       # or set AWS_PROFILE
  # discovery: true            # auto-discover region from IAM
  # guardrail:                 # optional Bedrock Guardrails
  #   guardrail_identifier: "your-guardrail-id"
  #   guardrail_version: "DRAFT"
```

Для аутентификации используется стандартная цепочка boto3: явная `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE` из `~/.aws/credentials`, роль IAM на EC2/ECS/Lambda, IMDS или SSO. Переменная env var не требуется, если вы уже прошли аутентификацию с помощью AWS CLI.

Bedrock использует **Converse API** под капотом — запросы преобразуются в независимую от модели форму Bedrock, поэтому одна и та же конфигурация работает для моделей Claude, Nova, DeepSeek и Llama. Установите `BEDROCK_BASE_URL` только в том случае, если вы звоните в региональную конечную точку, отличную от стандартной.

См. [Руководство по AWS Bedrock](/guides/aws-bedrock) для получения подробной информации о настройке IAM, выборе региона и межрегиональном анализе.

### Google Вертекс AI

Модели Gemini в Google Cloud Vertex AI через конечную точку Vertex, совместимую с OpenAI. Аутентификация – **OAuth2** — маркер доступа с коротким сроком действия (около 1 часа), созданный на основе JSON сервисной учетной записи или учетных данных приложения по умолчанию (ADC). Статического ключа API **нет**; Hermes чеканит и автоматически обновляет токен для вас, включая повторную чеканку в середине сеанса `401`.

```bash
# Service account JSON (recommended for servers / gateways)
echo "VERTEX_CREDENTIALS_PATH=/path/to/service-account.json" >> ~/.hermes/.env
# or Application Default Credentials
gcloud auth application-default login

hermes model   # → "Google Vertex AI" → project → region → model
```

Или в `config.yaml` (проект/регион не являются секретными и находятся здесь; путь к учетным данным остается в `.env`):
```yaml
model:
  provider: "vertex"
  default: "google/gemini-3-flash-preview"   # Vertex requires the google/ prefix
vertex:
  project_id: "my-gcp-project"   # blank → use the project embedded in the credentials
  region: "global"               # required for the Gemini 3.x previews
```

Переменные среды `VERTEX_PROJECT_ID`/`VERTEX_REGION` переопределяют значения `config.yaml`. Hermes лениво устанавливает `google-auth` при первом использовании; запустите `hermes setup`, если управляемая установка требует ремонта. Полное пошаговое руководство см. в [руководстве Google Vertex AI](/guides/google-vertex) и в [руководстве Google Gemini](/guides/google-gemini) для получения информации о пути статического API-ключа AI Studio.

### Портал Квен (OAuth)

Портал Qwen от Alibaba с входом в систему OAuth через браузер. Выберите **Qwen OAuth (портал)** в `hermes model`, войдите в систему через браузер, и Hermes сохранит токен обновления.

```bash
hermes model
# → pick "Qwen OAuth (Portal)"
# → browser opens; sign in with your Alibaba account
# → confirm — credentials are saved to ~/.hermes/auth.json

hermes chat   # uses portal.qwen.ai/v1 endpoint
```

Или настройте `config.yaml`:
```yaml
model:
  provider: "qwen-oauth"
  default: "qwen3-coder-plus"
```

Устанавливайте `HERMES_QWEN_BASE_URL` только в том случае, если конечная точка портала перемещается (по умолчанию: `https://portal.qwen.ai/v1`).

:::tip Qwen OAuth против Qwen Cloud (Alibaba DashScope)
`qwen-oauth` использует портал Qwen, ориентированный на потребителя, с входом в систему OAuth, что идеально подходит для отдельных пользователей. Поставщик `alibaba` использует Qwen Cloud (Alibaba DashScope) с `DASHSCOPE_API_KEY` — идеально подходит для программных/производственных рабочих нагрузок. Оба маршрутизируются к моделям семейства Qwen, но живут в разных конечных точках.
:::

### Alibaba Cloud (план кодирования)

Если вы подписаны на **Coding Plan** от Alibaba (ценовой артикул, отдельный от стандартного доступа к DashScope API), Hermes предоставляет его как своего собственного первоклассного поставщика: `alibaba-coding-plan`. Конечная точка: `https://coding-intl.dashscope.aliyuncs.com/v1`. Он совместим с OpenAI, как и обычный поставщик `alibaba`, но с другим базовым URL-адресом и платежной системой.

```yaml
model:
  provider: alibaba_coding     # alias for alibaba-coding-plan
  model: qwen3-coder-plus
```

Или из CLI:

```bash
hermes chat --provider alibaba_coding --model qwen3-coder-plus
```

`alibaba_coding` использует тот же `DASHSCOPE_API_KEY`, который уже используется в вашей записи `alibaba` — отдельный ключ не требуется, просто другая цель маршрутизации. До регистрации этого провайдера пользователи, установившие `provider: alibaba_coding` в `config.yaml`, автоматически переходили на маршрутизацию OpenRouter.

### МиниМакс (OAuth)

MiniMax-M2.7 через вход в браузер по OAuth — ключ API не требуется. Выберите **MiniMax (OAuth)** в `hermes model`, войдите в систему через браузер, и Hermes сохранит токены доступа и обновления. Внутри используется конечная точка, совместимая с Anthropic Messages (`/anthropic`).

```bash
hermes model
# → pick "MiniMax (OAuth)"
# → browser opens; sign in with your MiniMax account (global or CN region)
# → confirm — credentials are saved to ~/.hermes/auth.json

hermes chat   # uses api.minimax.io/anthropic endpoint
```

Или настройте `config.yaml`:
```yaml
model:
  provider: "minimax-oauth"
  default: "MiniMax-M2.7"
```

Поддерживаемые модели: `MiniMax-M2.7` (основной) и `MiniMax-M2.7-highspeed` (подключенный как вспомогательная модель по умолчанию). Путь OAuth игнорирует `MINIMAX_API_KEY`/`MINIMAX_BASE_URL`.

:::tip MiniMax OAuth против ключа API
`minimax-oauth` использует портал MiniMax, ориентированный на потребителя, с входом в систему OAuth — настройка выставления счетов не требуется. Провайдеры `minimax` и `minimax-cn` используют `MINIMAX_API_KEY` / `MINIMAX_CN_API_KEY` — для программного доступа. Подробное описание см. в [Руководстве MiniMax OAuth](/guides/minimax-oauth).
:::

### NVIDIA НИМ

Nemotron и другие модели с открытым исходным кодом через [build.nvidia.com](https://build.nvidia.com) (бесплатный ключ API) или локальную конечную точку NIM.

```bash
# Cloud (build.nvidia.com)
hermes chat --provider nvidia --model nvidia/nemotron-3-super-120b-a12b
# Requires: NVIDIA_API_KEY in ~/.hermes/.env

# Local NIM endpoint — override base URL
NVIDIA_BASE_URL=http://localhost:8000/v1 hermes chat --provider nvidia --model nvidia/nemotron-3-super-120b-a12b
```

Или установите его навсегда в `config.yaml`:
```yaml
model:
  provider: "nvidia"
  default: "nvidia/nemotron-3-super-120b-a12b"
```

:::tip Местный NIM
Для локальных развертываний (DGX Spark, локальный графический процессор) установите `NVIDIA_BASE_URL=http://localhost:8000/v1`. NIM предоставляет тот же OpenAI-совместимый API завершения чата, что и build.nvidia.com, поэтому переключение между облачным и локальным режимами — это однострочное изменение env-var.
:::

Hermes автоматически прикрепляет заголовок источника выставления счетов NIM к каждому запросу к `build.nvidia.com` — настройка не требуется. Это маршрутизирует потребление по правильному источнику на панели выставления счетов NVIDIA.

### Облако GMI

Открытые и логические модели через [GMI Cloud](https://www.gmicloud.ai/) — API-интерфейс, совместимый с OpenAI, аутентификация по ключу API.

```bash
# GMI Cloud
hermes chat --provider gmi --model deepseek-ai/DeepSeek-V3.2
# Requires: GMI_API_KEY in ~/.hermes/.env
```

Или установите его навсегда в `config.yaml`:
```yaml
model:
  provider: "gmi"
  default: "deepseek-ai/DeepSeek-V3.2"
```

Базовый URL-адрес можно переопределить с помощью `GMI_BASE_URL` (по умолчанию: `https://api.gmi-serving.com/v1`).

### Настоящий компьютер

Ваше собственное оборудование в качестве частного кластера вывода через [Фактический компьютер](https://actual.inc). Два режима обслуживания, оба совместимые с OpenAI (Hermes использует транспорт API Responses):

- **Размещенный ретранслятор** — `https://api.actual.inc`, сквозное шифрование, маршрут к *вашему* кластеру. Выполните аутентификацию с помощью ключа вывода `ac_` из [actual.inc/user/keys](https://actual.inc/user/keys).
- **Локальный демон** — на устройстве по адресу `http://127.0.0.1:8080`, полностью оффлайн. Ключ API не требуется: Hermes обнаруживает базовый URL-адрес обратной связи и автоматически выполняет аутентификацию с помощью внутреннего заполнителя.

```bash
# Hosted relay (ACTUAL_API_KEY in ~/.hermes/.env)
hermes chat --provider actual --model <model-id-from-your-cluster>

# Local daemon (ACTUAL_BASE_URL=http://127.0.0.1:8080 in ~/.hermes/.env, no key)
hermes chat --provider actual --model <installed-model-name>
```

Или установите его навсегда в `config.yaml`:
```yaml
model:
  provider: "actual"
  default: "<model-id>"
```

Примечания:
– Идентификаторы моделей берутся из `GET /v1/models` вашего кластера. Найдите их с помощью `hermes model` или `curl -s https://api.actual.inc/v1/models -H "Authorization: Bearer $ACTUAL_API_KEY"`.
— Голые хосты нормализованы: `ACTUAL_BASE_URL=http://127.0.0.1:8080` автоматически становится `http://127.0.0.1:8080/v1`.
- Усилие рассуждения ограничено поддерживаемым диапазоном Actual (`none/low/medium/high/max`) — глобальная настройка `xhigh`/`ultra` не будет обрабатывать 400 запросов.
- Маленькие локальные модели: полный набор инструментов Hermes по умолчанию плюс системное приглашение могут превышать контекстное окно 32 КБ, вызывая ошибку пустого потока на серверах семейства llama.cpp. Ограничьте набор инструментов (`-t file,web`) или загрузите модель с более широким контекстом. Дополнительный навык `actual-setup` (`hermes skills install official/devops/actual-setup`) подробно описывает настройку и устранение неполадок.
- Псевдонимы: `actual-computer`, `actualcomputer`, `aci`.

### StepFun

Модели серии Step через [StepFun](https://platform.stepfun.com) — OpenAI-совместимый API, аутентификация по ключу API.

```bash
# StepFun
hermes chat --provider stepfun --model step-3.5-flash
# Requires: STEPFUN_API_KEY in ~/.hermes/.env
```

Или установите его навсегда в `config.yaml`:
```yaml
model:
  provider: "stepfun"
  default: "step-3.5-flash"
```

Базовый URL-адрес можно переопределить с помощью `STEPFUN_BASE_URL` (по умолчанию: `https://api.stepfun.com/v1`).

### Поставщики вывода обнимающих лиц

[Поставщики Hugging Face Inference](https://huggingface.co/docs/inference-providers) направляют к более чем 20 открытым моделям через единую конечную точку, совместимую с OpenAI (`router.huggingface.co/v1`). Запросы автоматически перенаправляются на самый быстрый доступный бэкэнд (Groq, Together, SambaNova и т. д.) с автоматическим переключением при сбое.

```bash
# Use any available model
hermes chat --provider huggingface --model Qwen/Qwen3.5-397B-A17B
# Requires: HF_TOKEN in ~/.hermes/.env

# Short alias
hermes chat --provider hf --model deepseek-ai/DeepSeek-V3.2
```

Или установите его навсегда в `config.yaml`:
```yaml
model:
  provider: "huggingface"
  default: "Qwen/Qwen3.5-397B-A17B"
```

Получите свой токен на [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) — обязательно включите разрешение «Совершать вызовы поставщикам вывода». Включен уровень бесплатного пользования (кредит в размере 0,10 доллара США в месяц, без надбавки к тарифам поставщика).

К именам моделей можно добавить суффиксы маршрутизации: `:fastest` (по умолчанию), `:cheapest` или `:provider_name`, чтобы принудительно использовать определенный серверный компонент.

Базовый URL-адрес можно переопределить с помощью `HF_BASE_URL`.

## Индивидуальные и самостоятельные поставщики LLM

Агент Hermes работает с **любой конечной точкой API, совместимой с OpenAI**. Если сервер реализует `/v1/chat/completions`, вы можете указать на него Hermes. Это означает, что вы можете использовать локальные модели, серверы вывода графического процессора, маршрутизаторы с несколькими поставщиками или любой сторонний API.

### Общие настройки

Три способа настройки пользовательской конечной точки:

**Интерактивная настройка (рекомендуется):**
```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter: API base URL, API key, Model name
```

**Ручная настройка (`config.yaml`):**
```yaml
# In ~/.hermes/config.yaml
model:
  default: your-model-name
  provider: custom
  base_url: http://localhost:8000/v1
  api_key: your-key-or-leave-empty-for-local
```

:::предупреждение Legacy env vars
`LLM_MODEL` в `.env` **удален** — `config.yaml` является единственным источником достоверной информации о конфигурации модели и конечной точки. `OPENAI_BASE_URL` по-прежнему учитывается, но **только** для провайдера `openai-api` (он переопределяет конечную точку OpenAI для прямого доступа к ключу API). Для других поставщиков и пользовательских конечных точек используйте `hermes model` или установите `model.base_url` напрямую в `config.yaml`. Если в вашем `.env` есть устаревшие записи, они автоматически удаляются при следующем `hermes setup` или переносе конфигурации.
:::

Оба подхода сохраняются до `config.yaml`, который является источником истинной информации о модели, поставщике и базовом URL-адресе.

### Переключение моделей с помощью `/model`

::: предупреждение модели Гермеса против /model
**`hermes model`** (запускается с вашего терминала вне сеанса чата) — это **мастер полной настройки провайдера**. Используйте его для добавления новых поставщиков, запуска потоков OAuth, ввода ключей API и настройки пользовательских конечных точек.

**`/model`** (введенный в активном сеансе чата Hermes) может только **переключаться между поставщиками и моделями, которые вы уже настроили**. Он не может добавлять новых поставщиков, запускать OAuth или запрашивать ключи API. Если вы настроили только одного поставщика (например, OpenRouter), `/model` будет отображать модели только для этого поставщика.

**Чтобы добавить нового поставщика**: выйдите из сеанса (`Ctrl+C` или `/quit`), запустите `hermes model`, настройте нового поставщика, затем начните новый сеанс.
:::

Если у вас настроена хотя бы одна пользовательская конечная точка, вы можете переключать модели в середине сеанса:

```
/model custom:qwen-2.5          # Switch to a model on your custom endpoint
/model custom                    # Auto-detect the model from the endpoint
/model openrouter:claude-sonnet-4 # Switch back to a cloud provider
```

Если у вас настроены **именованные пользовательские поставщики** (см. ниже), используйте тройной синтаксис:

```
/model custom:local:qwen-2.5    # Use the "local" custom provider with model qwen-2.5
/model custom:work:llama3       # Use the "work" custom provider with llama3
```

При смене поставщика Hermes сохраняет базовый URL-адрес и поставщика для настройки, чтобы изменения сохранялись при перезапуске. При переключении с пользовательской конечной точки на встроенного поставщика устаревший базовый URL-адрес автоматически очищается.

:::совет
`/model custom` (пустой, без названия модели) запрашивает API `/models` вашей конечной точки и автоматически выбирает модель, если загружена ровно одна. Полезно для локальных серверов, на которых работает одна модель.
:::

Все нижеследующее следует тому же шаблону — просто измените URL-адрес, ключ и название модели.

---

### Оллама — локальные модели, нулевая конфигурация

[Ollama](https://ollama.com/) запускает модели открытого веса локально с помощью одной команды. Подходит для: быстрых локальных экспериментов, работы с конфиденциальностью, использования в автономном режиме. Поддерживает вызов инструментов через OpenAI-совместимый API.

```bash
# Install and run a model
ollama pull qwen2.5-coder:32b
ollama serve   # Starts on port 11434
```

Затем настройте Гермес:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:11434/v1
# Skip API key (Ollama doesn't need one)
# Enter model name (e.g. qwen2.5-coder:32b)
```

Или настройте `config.yaml` напрямую:

```yaml
model:
  default: qwen2.5-coder:32b
  provider: custom
  base_url: http://localhost:11434/v1
  context_length: 64000   # See warning below
```

:::осторожно. Оллама по умолчанию использует очень малую длину контекста.
По умолчанию Оллама **не** использует полное контекстное окно вашей модели. В зависимости от вашей видеопамяти по умолчанию используется следующее:

| Доступная видеопамять | Контекст по умолчанию |
||--------------------------------|
| Менее 24 ГБ | **4096 токенов** |
| 24–48 ГБ | 32 768 жетонов |
| 48+ ГБ | 256 000 жетонов |

Агенту Hermes требуется не менее **64 000 токенов** контекста для использования агента с инструментами. Окна меньшего размера отклоняются при запуске, поскольку системному приглашению, схемам инструментов и состоянию рабочего диалога требуется достаточно места для надежных многоэтапных рабочих процессов.

**Как его увеличить** (выберите один):

```bash
# Option 1: Set server-wide via environment variable (recommended)
OLLAMA_CONTEXT_LENGTH=64000 ollama serve

# Option 2: For systemd-managed Ollama
sudo systemctl edit ollama.service
# Add: Environment="OLLAMA_CONTEXT_LENGTH=64000"
# Then: sudo systemctl daemon-reload && sudo systemctl restart ollama

# Option 3: Bake it into a custom model (persistent per-model)
echo -e "FROM qwen2.5-coder:32b\nPARAMETER num_ctx 64000" > Modelfile
ollama create qwen2.5-coder-64k -f Modelfile
```

**Вы не можете установить длину контекста через OpenAI-совместимый API** (`/v1/chat/completions`). Его необходимо настроить на стороне сервера или через файл модели. Это источник путаницы №1 при интеграции Ollama с такими инструментами, как Hermes.
:::

**Убедитесь, что контекст настроен правильно:**

```bash
ollama ps
# Look at the CONTEXT column — it should show your configured value
```

:::совет
Список доступных моделей с `ollama list`. Возьмите любую модель из [библиотеки Ollama](https://ollama.com/library) с помощью `ollama pull <model>`. Ollama автоматически выполняет разгрузку графического процессора — для большинства настроек настройка не требуется.
:::

---

### vLLM — вывод высокопроизводительного графического процессора

[vLLM](https://docs.vllm.ai/) — это стандарт для обслуживания рабочих LLM. Лучше всего подходит для: максимальной пропускной способности оборудования графического процессора, обслуживания больших моделей, непрерывной пакетной обработки.

```bash
pip install vllm
vllm serve meta-llama/Llama-3.1-70B-Instruct \
  --port 8000 \
  --max-model-len 65536 \
  --tensor-parallel-size 2 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
```

Затем настройте Гермес:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:8000/v1
# Skip API key (or enter one if you configured vLLM with --api-key)
# Enter model name: meta-llama/Llama-3.1-70B-Instruct
```

**Длина контекста**: vLLM по умолчанию считывает `max_position_embeddings` модели. Если это превышает объем памяти вашего графического процессора, произойдет ошибка и будет предложено установить `--max-model-len` ниже. Вы также можете использовать `--max-model-len auto`, чтобы автоматически найти подходящий максимум. Установите `--gpu-memory-utilization 0.95` (по умолчанию 0,9), чтобы втиснуть больше контекста во VRAM.

**Вызов инструмента требует явных флагов:**

| Флаг | Цель |
|------|---------|
| `--enable-auto-tool-choice` | Требуется для `tool_choice: "auto"` (по умолчанию в Hermes) |
| `--tool-call-parser <name>` | Парсер формата вызова инструментов модели |

Поддерживаемые парсеры: `hermes` (Qwen 2.5, Hermes 2/3), `llama3_json` (Llama 3.x), `mistral`, `deepseek_v3`, `deepseek_v31`, `xlam`, `pythonic`. Без этих флагов вызовы инструментов не будут работать — модель будет выводить вызовы инструментов в виде текста.

**Парсеры рассуждений Qwen.** Hermes сохраняет метаданные структурированных рассуждений, такие как `reasoning`, `reasoning_content`, а также потоковые дельты рассуждений, когда серверы, совместимые с OpenAI, возвращают их. Эти метаданные рассматриваются как данные трассировки рассуждений/мышлений, а не как замена видимого ответа помощника. Для моделей рассуждений Qwen, обслуживаемых vLLM, убедитесь, что окончательный видимый пользователю ответ по-прежнему отображается в `content`. Если `--reasoning-parser qwen3` оставляет `content` пустым в вашем развертывании, либо отключите этот синтаксический анализатор, либо передайте поддерживаемый сервером вариант запроса, например от `chat_template_kwargs.enable_thinking: false` до `extra_body`.

:::совет
vLLM поддерживает удобочитаемые размеры: `--max-model-len 64k` (строчные k = 1000, прописные K = 1024).
:::

---

### SGLang — быстрое обслуживание с помощью RadixAttention

[SGLang](https://github.com/sgl-project/sglang) — альтернатива vLLM с RadixAttention для повторного использования кэша KV. Лучше всего подходит для: многоходовых диалогов (кэширование префиксов), ограниченного декодирования, структурированного вывода.

```bash
pip install "sglang[all]"
python -m sglang.launch_server \
  --model meta-llama/Llama-3.1-70B-Instruct \
  --port 30000 \
  --context-length 65536 \
  --tp 2 \
  --tool-call-parser qwen
```

Затем настройте Гермес:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:30000/v1
# Enter model name: meta-llama/Llama-3.1-70B-Instruct
```

**Длина контекста.** SGLang по умолчанию считывает данные из конфигурации модели. Используйте `--context-length` для переопределения. Если вам необходимо превысить заявленный максимум модели, установите `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`.

**Вызов инструмента:** Используйте `--tool-call-parser` с соответствующим синтаксическим анализатором для вашего семейства моделей: `qwen` (Qwen 2.5), `llama3`, `llama4`, `deepseekv3`, `mistral`, `glm`. Без этого флага вызовы инструментов возвращаются в виде обычного текста.

::: Внимание: SGLang по умолчанию использует максимальное количество выходных токенов 128.
Если ответы кажутся усеченными, добавьте `max_tokens` к своим запросам или установите `--default-max-tokens` на сервере. По умолчанию SGLang составляет только 128 токенов на ответ, если они не указаны в запросе.
:::

---

### llama.cpp / llama-server — определение процессора и металла

[llama.cpp](https://github.com/ggml-org/llama.cpp) запускает квантованные модели на процессорах, Apple Silicon (Metal) и потребительских графических процессорах. Лучше всего подходит для: запуска моделей без графического процессора центра обработки данных, пользователей Mac, периферийного развертывания.

```bash
# Build and start llama-server
cmake -B build && cmake --build build --config Release
./build/bin/llama-server \
  --jinja -fa \
  -c 64000 \
  -ngl 99 \
  -m models/qwen2.5-coder-32b-instruct-Q4_K_M.gguf \
  --port 8080 --host 0.0.0.0
```

**Длина контекста (`-c`):** Последние сборки по умолчанию имеют значение `0`, которое считывает контекст обучения модели из метаданных GGUF. Для моделей с обучающим контекстом 128 тыс.+ это может привести к попытке OOM выделить весь KV-кеш. Явно задайте для `-c` значение не менее 64 000 токенов для Hermes. При использовании параллельных слотов (`-np`) общий контекст делится между слотами — при использовании `-c 64000 -np 4` каждый слот получает только 16 КБ, что ниже минимума Hermes за активный сеанс.

Затем настройте Hermes, чтобы он указывал на него:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:8080/v1
# Skip API key (local servers don't need one)
# Enter model name — or leave blank to auto-detect if only one model is loaded
```

При этом конечная точка сохраняется в `config.yaml`, поэтому она сохраняется между сеансами.

:::Осторожно! Для вызова инструмента требуется `--jinja`.
Без `--jinja` llama-server полностью игнорирует параметр `tools`. Модель попытается вызвать инструменты, написав JSON в тексте ответа, но Hermes не распознает это как вызов инструмента — вместо фактического поиска вы увидите необработанный JSON, например `{"name": "web_search", ...}`, напечатанный как сообщение.

Встроенная поддержка вызова инструментов (наилучшая производительность): Llama 3.x, Qwen 2.5 (включая Coder), Hermes 2/3, Mistral, DeepSeek, Functionary. Все остальные модели используют общий обработчик, который работает, но может оказаться менее эффективным. Полный список см. в [документации по вызову функций llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md).

Вы можете убедиться, что поддержка инструмента активна, проверив `http://localhost:8080/props` — поле `chat_template` должно присутствовать.
:::

:::совет
Загрузите модели GGUF из [Hugging Face](https://huggingface.co/models?library=gguf). Квантование Q4_K_M обеспечивает наилучший баланс качества и использования памяти.
:::

---

### LM Studio — настольное приложение с локальными моделями

[LM Studio](https://lmstudio.ai/) — настольное приложение для запуска локальных моделей с графическим интерфейсом. Подходит для: пользователей, предпочитающих визуальный интерфейс, быстрое тестирование моделей, разработчиков на macOS/Windows/Linux.

Запустите сервер из приложения LM Studio (вкладка «Разработчик» → «Запустить сервер») или используйте CLI:

```bash
lms server start                        # Starts on port 1234
lms load qwen2.5-coder --context-length 64000
```

Затем настройте Гермес:

```bash
hermes model
# Select "LM Studio"
# Press Enter to use http://localhost:1234/v1
# Pick one of the discovered models
# If LM Studio server auth is enabled, enter LM_API_KEY when prompted
```

Hermes сохраняет контекст уже загруженного экземпляра LM Studio. Для выгруженной модели в явном режиме по умолчанию Hermes опускает `context_length`, если вы не настроили его в Hermes, поэтому LM Studio может применить свои собственные настройки модели. Затем Hermes использует только отчеты LM Studio о длине контекста после загрузки.

Чтобы изменить длину контекста в LM Studio:

1. Нажмите значок шестеренки рядом со средством выбора модели.
2. Установите для параметра «Длина контекста» значение не менее 64000, чтобы обеспечить плавность работы.
3. Перезагрузите модель, чтобы изменения вступили в силу.
4. Если ваша машина не может вместить 64000, рассмотрите возможность использования модели меньшего размера с большей длиной контекста.

Альтернативно используйте CLI: `lms load model-name --context-length 64000`

Вы можете использовать CLI, чтобы оценить, подойдет ли модель: `lms load model-name --context-length 64000 --estimate-only`

Чтобы установить постоянные значения по умолчанию для каждой модели: вкладка «Мои модели» → значок шестеренки на модели → установить размер контекста.
:::

Если вы используете функцию загрузки / автоматического выселения LM Studio «Just-In-Time» и хотите, чтобы LM Studio управляла загрузкой и удалением модели из обычных запросов чата, пропустите явный этап предварительной загрузки Hermes:

```bash
hermes config set model.lmstudio_load_mode jit
```

Верните его к явному поведению предварительной загрузки по умолчанию с помощью:

```bash
hermes config set model.lmstudio_load_mode explicit
```

**Вызов инструмента:** Поддерживается начиная с LM Studio 0.3.6. Модели со встроенным обучением использованию инструментов (Qwen 2.5, Llama 3.x, Mistral, Hermes) обнаруживаются автоматически и отображаются со значком инструмента. Другие модели используют общий запасной вариант, который может быть менее надежным.

---

### Сеть WSL2 (пользователи Windows)

Поскольку для агента Hermes требуется среда Unix, пользователи Windows запускают его внутри WSL2. Если сервер вашей модели (Ollama, LM Studio и т. д.) работает на **хосте Windows**, вам необходимо устранить разрыв в сети — WSL2 использует виртуальный сетевой адаптер с собственной подсетью, поэтому `localhost` внутри WSL2 относится к виртуальной машине Linux, **а не** к хосту Windows.

:::tip Оба в WSL2? Без проблем.
Если сервер вашей модели также работает внутри WSL2 (обычно для vLLM, SGLang и llama-server), `localhost` работает должным образом — они используют одно и то же сетевое пространство имен. Пропустите этот раздел.
:::

#### Вариант 1: режим зеркальной сети (рекомендуется)

Зеркальный режим, доступный в **Windows 11 22H2+**, обеспечивает двустороннюю работу `localhost` между Windows и WSL2 — самое простое решение.

1. Создайте или отредактируйте `%USERPROFILE%\.wslconfig` (например, `C:\Users\YourName\.wslconfig`):
   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

2. Перезапустите WSL из PowerShell:
   ```powershell
   wsl --shutdown
   ```

3. Снова откройте терминал WSL2. `localhost` теперь достигает служб Windows:
   ```bash
   curl http://localhost:11434/v1/models   # Ollama on Windows — works
   ```

:::обратите внимание на брандмауэр Hyper-V
В некоторых сборках Windows 11 брандмауэр Hyper-V по умолчанию блокирует зеркальные соединения. Если `localhost` по-прежнему не работает после включения зеркального режима, запустите его в **Admin PowerShell**:
```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```
:::

#### Вариант 2. Используйте IP-адрес хоста Windows (Windows 10/более ранние сборки)

Если вы не можете использовать зеркальный режим, найдите IP-адрес хоста Windows внутри WSL2 и используйте его вместо `localhost`:

```bash
# Get the Windows host IP (the default gateway of WSL2's virtual network)
ip route show | grep -i default | awk '{ print $3 }'
# Example output: 172.29.192.1
```

Используйте этот IP в вашей конфигурации Hermes:

```yaml
model:
  default: qwen2.5-coder:32b
  provider: custom
  base_url: http://172.29.192.1:11434/v1   # Windows host IP, not localhost
```

:::tip Динамический помощник
IP-адрес хоста может измениться при перезапуске WSL2. Вы можете получить его динамически в своей оболочке:
```bash
export WSL_HOST=$(ip route show | grep -i default | awk '{ print $3 }')
echo "Windows host at: $WSL_HOST"
curl http://$WSL_HOST:11434/v1/models   # Test Ollama
```

Или используйте имя mDNS вашего компьютера (требуется `libnss-mdns` в WSL2):
```bash
sudo apt install libnss-mdns
curl http://$(hostname).local:11434/v1/models
```
:::

#### Адрес привязки сервера (требуется для режима NAT)

Если вы используете **Вариант 2** (режим NAT с IP-адресом узла), сервер модели в Windows должен принимать соединения извне `127.0.0.1`. По умолчанию большинство серверов прослушивают только локальный хост — соединения WSL2 в режиме NAT поступают из другой виртуальной подсети и будут отклонены. В зеркальном режиме `localhost` сопоставляется напрямую, поэтому привязка по умолчанию `127.0.0.1` работает нормально.

| Сервер | Привязка по умолчанию | Как исправить |
|--------|-------------|------------|
| **Оллама** | `127.0.0.1` | Установите переменную среды `OLLAMA_HOST=0.0.0.0` перед запуском Ollama (Настройки системы → Переменные среды в Windows или отредактируйте службу Ollama) |
| **ЛМ Студия** | `127.0.0.1` | Включите **"Сервис по сети"** на вкладке "Разработчик" → Настройки сервера |
| **лама-сервер** | `127.0.0.1` | Добавьте `--host 0.0.0.0` в команду запуска |
| **vLLM** | `0.0.0.0` | По умолчанию уже привязывается ко всем интерфейсам |
| **СГЛанг** | `127.0.0.1` | Добавьте `--host 0.0.0.0` в команду запуска |

**Ollama для Windows (подробно):** Ollama работает как служба Windows. Чтобы установить `OLLAMA_HOST`:
1. Откройте **Свойства системы** → **Переменные среды**.
2. Добавьте новую **Системную переменную**: `OLLAMA_HOST` = `0.0.0.0`.
3. Перезапустите службу Ollama (или перезагрузитесь)

#### Брандмауэр Windows

Брандмауэр Windows рассматривает WSL2 как отдельную сеть (как в режиме NAT, так и в зеркальном режиме). Если соединения по-прежнему не работают после описанных выше шагов, добавьте правило брандмауэра для порта вашей модели сервера:

```powershell
# Run in Admin PowerShell — replace PORT with your server's port
New-NetFirewallRule -DisplayName "Allow WSL2 to Model Server" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11434
```

Общие порты: Ollama `11434`, vLLM `8000`, SGLang `30000`, llama-server `8080`, LM Studio `1234`.

#### Быстрая проверка

Изнутри WSL2 проверьте, можете ли вы получить доступ к серверу вашей модели:

```bash
# Replace URL with your server's address and port
curl http://localhost:11434/v1/models          # Mirrored mode
curl http://172.29.192.1:11434/v1/models       # NAT mode (use your actual host IP)
```

Если вы получите ответ в формате JSON со списком ваших моделей, все в порядке. Используйте тот же URL-адрес, что и `base_url` в вашей конфигурации Hermes.

---

### Устранение неполадок локальных моделей

Эти проблемы затрагивают **все** локальные серверы вывода при использовании с Hermes.

#### «Соединение отклонено» от WSL2 к серверу модели, размещенному на Windows.

Если вы используете Hermes внутри WSL2 и сервер вашей модели на хосте Windows, `http://localhost:<port>` не будет работать в сетевом режиме NAT WSL2 по умолчанию. Исправление см. в разделе [Сеть WSL2](#wsl2-networking-windows-users) выше.

#### Вызовы инструментов отображаются в виде текста, а не выполняются

Модель выводит что-то вроде `{"name": "web_search", "arguments": {...}}` в виде сообщения вместо фактического вызова инструмента.

**Причина:** На вашем сервере не включен вызов инструментов, или модель не поддерживает его посредством реализации вызова инструментов на сервере.

| Сервер | Исправить |
|--------|-----|
| **llama.cpp** | Добавьте `--jinja` в команду запуска |
| **vLLM** | Добавьте `--enable-auto-tool-choice --tool-call-parser hermes` |
| **СГЛанг** | Добавьте `--tool-call-parser qwen` (или соответствующий парсер) |
| **Оллама** | Вызов инструмента включен по умолчанию — убедитесь, что ваша модель его поддерживает (проверьте с помощью `ollama show model-name`) |
| **ЛМ Студия** | Обновитесь до версии 0.3.6+ и используйте модель со встроенной поддержкой инструментов |

#### Кажется, модель забывает контекст или дает бессвязные ответы

**Причина:** Контекстное окно слишком маленькое. Когда разговор превышает ограничение контекста, большинство серверов молча удаляют старые сообщения. Только схемы системных подсказок и инструментов Hermes могут использовать токены 4–8 тыс.

**Диагноз:**

```bash
# Check what Hermes thinks the context is
# Look at startup line: "Context limit: X tokens"

# Check your server's actual context
# Ollama: ollama ps (CONTEXT column)
# llama.cpp: curl http://localhost:8080/props | jq '.default_generation_settings.n_ctx'
# vLLM: check --max-model-len in startup args
```

**Исправление:** Установите для контекста значение не менее **64 000 токенов** для использования агентом. См. раздел каждого сервера выше, чтобы узнать конкретный флаг.

#### «Ограничение контекста: 2048 токенов» при запуске

Hermes автоматически определяет длину контекста по конечной точке `/v1/models` вашего сервера. Если сервер сообщает о низком значении (или вообще не сообщает его), Hermes использует заявленный предел модели, который может быть неправильным.

**Исправление:** Задайте это явно в `config.yaml`:

```yaml
model:
  default: your-model
  provider: custom
  base_url: http://localhost:11434/v1
  context_length: 64000
```

#### Ответы обрезаются на полуслове

**Возможные причины:**
1. **Низкое ограничение вывода (`max_tokens`) на сервере** — по умолчанию SGLang составляет 128 токенов на ответ. Установите `--default-max-tokens` на сервере или настройте Hermes с помощью `model.max_tokens` в config.yaml. Примечание. `max_tokens` контролирует только длину ответа — она не связана с тем, насколько длинной может быть история вашего разговора (то есть `context_length`).
2. **Исчерпание контекста** — модель заполнила контекстное окно. Увеличьте `model.context_length` или включите [сжатие контекста](/user-guide/configuration#context-compression) в Hermes.

---

### LiteLLM Proxy — шлюз для нескольких провайдеров

[LiteLLM](https://docs.litellm.ai/) — это OpenAI-совместимый прокси-сервер, который объединяет более 100 поставщиков LLM под единым API. Лучше всего подходит для: переключения между провайдерами без изменения конфигурации, балансировки нагрузки, резервных цепочек, контроля бюджета.

```bash
# Install and start
pip install "litellm[proxy]"
litellm --model anthropic/claude-sonnet-4 --port 4000

# Or with a config file for multiple models:
litellm --config litellm_config.yaml --port 4000
```

Затем настройте Hermes с помощью `hermes model` → Пользовательская конечная точка → `http://localhost:4000/v1`.

Пример `litellm_config.yaml` с резервным вариантом:
```yaml
model_list:
  - model_name: "best"
    litellm_params:
      model: anthropic/claude-sonnet-4
      api_key: sk-ant-...
  - model_name: "best"
    litellm_params:
      model: openai/gpt-4o
      api_key: sk-...
router_settings:
  routing_strategy: "latency-based-routing"
```

---

### ClawRouter — маршрутизация с оптимизацией затрат

[ClawRouter](https://github.com/BlockRunAI/ClawRouter) от BlockRunAI — это прокси-сервер локальной маршрутизации, который автоматически выбирает модели в зависимости от сложности запроса. Он классифицирует запросы по 14 измерениям и направляет к самой дешевой модели, способной справиться с задачей. Оплата осуществляется через криптовалюту USDC (без ключей API).

```bash
# Install and start
npx @blockrun/clawrouter    # Starts on port 8402
```

Затем настройте Hermes с помощью `hermes model` → Пользовательская конечная точка → `http://localhost:8402/v1` → имя модели `blockrun/auto`.

Профили маршрутизации:
| Профиль | Стратегия | Экономия |
|---------|----------|---------|
| `blockrun/auto` | Сбалансированное качество/стоимость | 74-100% |
| `blockrun/eco` | Самый дешевый | 95-100% |
| `blockrun/premium` | Модели лучшего качества | 0% |
| `blockrun/free` | Только бесплатные модели | 100% |
| `blockrun/agentic` | Оптимизирован для использования с инструментами | варьируется |

:::примечание
Для оплаты ClawRouter требуется кошелек на Base или Solana, финансируемый в долларах США. Все запросы направляются через внутренний API BlockRun. Запустите `npx @blockrun/clawrouter doctor`, чтобы проверить состояние кошелька.
:::

---

### Другие совместимые поставщики

Любой сервис с API-интерфейсом, совместимым с OpenAI, работает. Некоторые популярные варианты:

| Провайдер | Базовый URL | Заметки |
|----------|----------|-------|
| [Вместе AI](https://together.ai) | `https://api.together.xyz/v1` | Открытые модели, размещенные в облаке |
| [Грок](https://groq.com) | `https://api.groq.com/openai/v1` | Сверхбыстрый вывод |
| [DeepSeek](https://deepseek.com) | `https://api.deepseek.com/v1` | Модели DeepSeek |
| [Фейерверк AI](https://fireworks.ai) | `https://api.fireworks.ai/inference/v1` | Быстрый хостинг открытой модели |
| [Облако GMI](https://www.gmicloud.ai/) | `https://api.gmi-serving.com/v1` | Управляемый вывод, совместимый с OpenAI |
| [Настоящий компьютер](https://actual.inc) | `https://api.actual.inc/v1` | Частная ретрансляция в ваш собственный кластер; локальный демон по адресу `http://127.0.0.1:8080/v1` |
| [Церебра](https://cerebras.ai) | `https://api.cerebras.ai/v1` | Вывод на уровне пластины |
| [Мистраль ИИ](https://mistral.ai) | `https://api.mistral.ai/v1` | Модели Мистраль |
| [OpenAI](https://openai.com) | `https://api.openai.com/v1` | Прямой доступ к OpenAI |
| [Azure OpenAI](https://azure.microsoft.com) | `https://YOUR.openai.azure.com/` | Предприятие OpenAI |
| [LocalAI](https://localai.io) | `http://localhost:8080/v1` | Автономный, многомодельный |
| [Январь](https://jan.ai) | `http://localhost:1337/v1` | Настольное приложение с локальными моделями |

Настройте любой из них с помощью `hermes model` → Пользовательская конечная точка или `config.yaml`:

```yaml
model:
  default: meta-llama/Llama-3.1-70B-Instruct-Turbo
  provider: custom
  base_url: https://api.together.xyz/v1
  api_key: your-together-key
```

---

### Определение длины контекста

:::note Две настройки, легко перепутать
**`context_length`** — это **общее контекстное окно** — совокупный бюджет для входных *и* токенов вывода (например, 200 000 для Claude Opus 4.6). Hermes использует это, чтобы решить, когда сжимать историю и проверять запросы API.

**`model.max_tokens`** — это **ограничение вывода** — максимальное количество токенов, которые модель может сгенерировать за *один ответ*. Это не имеет никакого отношения к тому, насколько длинной может быть история вашего разговора. Стандартное имя `max_tokens` является частым источником путаницы; Собственный API Anthropic с тех пор для ясности переименовал его в `max_output_tokens`.

Установите `context_length`, если при автоматическом определении размер окна указан неправильно.
Устанавливайте `model.max_tokens` только в том случае, если вам нужно ограничить длину отдельных ответов.
:::

Hermes использует цепочку разрешения из нескольких источников, чтобы определить правильное контекстное окно для вашей модели и поставщика:

1. **Переопределение конфигурации** — `model.context_length` в config.yaml (наивысший приоритет)
2. **Индивидуальный поставщик для каждой модели** — `providers.<name>.models.<id>.context_length`
3. **Постоянный кеш** — ранее обнаруженные значения (выдерживает перезагрузку)
4. **Конечная точка `/models`** — запрашивает API вашего сервера (локальные/настраиваемые конечные точки).
5. **Anthropic `/v1/models`** — запрашивает API Anthropic для `max_input_tokens` (только для пользователей API-ключа).
6. **OpenRouter API** — метаданные живой модели из OpenRouter.
7. **Nous Portal** — суффикс сопоставляет идентификаторы моделей Nous с метаданными OpenRouter.
8. **[models.dev](https://models.dev)** — реестр, поддерживаемый сообществом, с длиной контекста, зависящей от поставщика, для более чем 3800 моделей от более чем 100 поставщиков.
9. **Резервные настройки по умолчанию** – общие шаблоны семейства моделей (по умолчанию 128 КБ).

Для большинства настроек это работает «из коробки». Система учитывает провайдера — одна и та же модель может иметь разные контекстные ограничения в зависимости от того, кто ее обслуживает (например, `claude-opus-4.6` — это 1M на Anthropic Direct, но 128K на GitHub Copilot).

Чтобы явно указать длину контекста, добавьте `context_length` в конфигурацию вашей модели:

```yaml
model:
  default: "qwen3.5:9b"
  base_url: "http://localhost:8080/v1"
  context_length: 131072  # tokens
```

Для пользовательских конечных точек вы также можете установить длину контекста для каждой модели:

```yaml
providers:
  my-local-llm:
    api: "http://localhost:11434/v1"
    models:
      qwen3.5:27b:
        context_length: 64000
      deepseek-r1:70b:
        context_length: 65536
```

`hermes model` запросит длину контекста при настройке пользовательской конечной точки. Оставьте это поле пустым для автоматического определения.

:::tip Когда устанавливать это вручную
- Вы используете Ollama с пользовательским `num_ctx`, значение которого ниже максимального значения модели.
- Вы хотите ограничить контекст ниже максимального значения модели (например, 8 КБ на модели 128 КБ для экономии видеопамяти).
- Вы используете прокси, который не раскрывает `/v1/models`.
:::

---

### Именованные пользовательские поставщики

Если вы работаете с несколькими пользовательскими конечными точками (например, локальным сервером разработки и удаленным сервером графического процессора), вы можете определить их как именованные пользовательские поставщики в соответствии с диктатом `providers:` в `config.yaml`, с ключом по имени поставщика:

```yaml
providers:
  local:
    api: http://localhost:8080/v1
    # api_key omitted — Hermes uses "no-key-required" for keyless local servers
  work:
    api: https://gpu-server.internal.corp/v1
    key_env: CORP_API_KEY
    transport: chat_completions   # set explicitly by `hermes model` → Custom Endpoint wizard; auto-detection still happens as a fallback
  anthropic-proxy:
    api: https://proxy.example.com/anthropic
    key_env: ANTHROPIC_PROXY_KEY
    transport: anthropic_messages  # for Anthropic-compatible proxies
```

Каждая запись принимает: `api` (базовый URL-адрес конечной точки — `base_url`/`url` являются принятыми псевдонимами), `name` (необязательное отображаемое имя; по умолчанию используется ключ dict), `key_env` или встроенный `api_key` или `key_cmd` (см. ниже), `transport` (`chat_completions` / `anthropic_messages` / `codex_responses`), `default_model`, `models`, `context_length`, `discover_models`, `extra_body`, `extra_headers`, `ssl_ca_cert` / `ssl_verify` и `enabled: false`, чтобы скрыть запись, не удаляя ее.

#### Учетные данные, созданные командой (`key_cmd`)

Корпоративные шлюзы часто выдают кратковременные токены-носители (брокеры SSO/OIDC, облачный IAM, внутренние прокси-серверы аутентификации), а не статические ключи API, поэтому токен, скопированный в `.env`, устаревает в середине сеанса, и запросы начинают возвращать 401. `key_cmd` называет команду, которая *печатает* токен; Hermes запускает его и кэширует результат незадолго до истечения срока действия, поэтому длинные сеансы продолжают работать без перезапуска:

```yaml
providers:
  my-gateway:
    base_url: "https://gateway.internal.example.com/v1"
    api_mode: chat_completions
    key_cmd: "my-auth-cli print-token --profile prod"
```

Работает с любым помощником, который печатает токен — `databricks auth token`, `gcloud auth print-access-token`, `az account get-access-token`, `vault read` или сценариями `apiKeyHelper` в стиле Claude Code.

Команда должна вывести **только** токен на стандартный вывод: либо в чистом виде, либо в виде JSON с полем `access_token` (`expires_in` учитывается; также абсолютные временные метки `expiry`/`expiresOn` ISO). Многострочный вывод скорее отвергается, чем предполагается. Если срок действия не объявлен, токен повторно создается в ограниченном окне.

Приоритет: явный флаг `--api-key` по-прежнему имеет преимущество; в противном случае `key_cmd` превосходит статический `api_key`/`key_env` в той же записи. Выданные учетные данные применяются как к основной работе агента, так и к вспомогательным задачам (генерация заголовков, сжатие, просмотр, внедрение).

Не путать с `secrets.command`, который запускает помощник **один раз при запуске** для заполнения переменных env для всего процесса. Используйте это для помощника хранилища/связки ключей, передающего множество секретов; используйте `key_cmd`, когда учетные данные одного поставщика необходимо повторно создать *во время* сеанса.

:::Примечание Устаревший формат
В старых конфигурациях вместо этого использовался список `custom_providers:` верхнего уровня. Он по-прежнему работает — Hermes читает оба — и `hermes update` автоматически переносит его в словарь `providers:` (конфигурация v12). Имена полей немного отличаются в формате dict: устаревшее `model` — `default_model`, а устаревшее `api_mode` — `transport`.
:::

Некоторым OpenAI-совместимым конечным точкам требуются поля тела запроса, специфичные для поставщика. Добавьте карту `extra_body` к соответствующему пользовательскому поставщику, и Hermes объединит ее с каждым запросом завершения чата для этой конечной точки:

```yaml
providers:
  gemma-local:
    api: http://localhost:8080/v1
    default_model: google/gemma-4-31b-it
    extra_body:
      enable_thinking: true
      reasoning_effort: high
```

Используйте форму документов вашего сервера. Например, развертывания vLLM Gemma и некоторые конечные точки NVIDIA NIM ожидают `enable_thinking` в поле `chat_template_kwargs` вместо поля `extra_body` верхнего уровня:

```yaml
extra_body:
  chat_template_kwargs:
    enable_thinking: true
```

Для моделей рассуждений Qwen, обслуживаемых vLLM, эту же форму можно использовать для отключения мышления, когда анализатор рассуждений разделяет весь сгенерированный текст на поля рассуждений и оставляет помощника `content` пустым:

```yaml
extra_body:
  chat_template_kwargs:
    enable_thinking: false
```

Мастер `hermes model` → Пользовательская конечная точка теперь явно запрашивает режим API и сохраняет ваш ответ на `config.yaml` (как `transport` в записи поставщика). Автоматическое определение на основе URL-адреса (например, пути `/anthropic` → `anthropic_messages`) по-прежнему выполняется в качестве резервного варианта, если поле остается пустым.

**Встроенное видение для моделей настраиваемого поставщика.** Если ваша пользовательская конечная точка обслуживает модель с поддержкой машинного зрения, которой нет в models.dev, установите `model.supports_vision: true`, чтобы Hermes маршрутизировал прикрепленные изображения в исходном виде (как части `image_url`) вместо предварительной обработки их через `vision_analyze`. Одна ручка — не нужно дополнительно устанавливать `agent.image_input_mode: native`.

```yaml
model:
  provider: custom
  base_url: http://localhost:8080/v1
  default: qwen3.6-35b-a3b
  supports_vision: true   # send images natively; otherwise vision_analyze pre-describes them
```

Тот же ключ учитывается в моделях с именованным поставщиком (`providers.<name>.models.<id>.supports_vision`) и принимает стандартные логические значения YAML (`true/false/yes/no/on/off/1/0`).

Переключайтесь между ними в середине сеанса с помощью тройного синтаксиса:

```
/model custom:local:qwen-2.5       # Use the "local" endpoint with qwen-2.5
/model custom:work:llama3-70b      # Use the "work" endpoint with llama3-70b
/model custom:anthropic-proxy:claude-sonnet-4  # Use the proxy
```

Вы также можете выбрать именованных пользовательских поставщиков в интерактивном меню `hermes model`.

---

### Поваренная книга: AI, Groq, Perplexity вместе

Все поставщики облачных услуг, перечисленные в списке [Другие совместимые поставщики](#other-совместимые-провайдеры), говорят на диалекте REST OpenAI, поэтому они подключаются одинаково под диктовкой `providers:`. Далее следуют три рабочих рецепта. Каждый из них попадает в `~/.hermes/config.yaml`, а соответствующий ключ API — в `~/.hermes/.env`.

#### Вместе ИИ

Размещает модели открытого веса (Llama, MiniMax, Gemma, DeepSeek, Qwen) по ценам, значительно ниже сторонних API. Хороший вариант по умолчанию для многомодельных автопарков.

```yaml
# ~/.hermes/config.yaml
providers:
  together:
    api: https://api.together.xyz/v1
    key_env: TOGETHER_API_KEY
    # transport: chat_completions  # default — no need to set

model:
  default: MiniMaxAI/MiniMax-M2.7   # or any model from together.ai/models
  provider: custom:together
```

```bash
# ~/.hermes/.env
TOGETHER_API_KEY=your-together-key
```

Смена моделей в середине сессии:

```
/model custom:together:meta-llama/Llama-3.3-70B-Instruct-Turbo
/model custom:together:google/gemma-4-31b-it
/model custom:together:deepseek-ai/DeepSeek-V3
```

Конечная точка Together `/v1/models` работает, поэтому `hermes model` может автоматически обнаруживать доступные модели.

#### Грок

Сверхбыстрый вывод (~500 ток/с на Llama-3.3-70B). Небольшой каталог, но мощный для интерактивного использования, чувствительного к задержке.

```yaml
# ~/.hermes/config.yaml
providers:
  groq:
    api: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY

model:
  default: llama-3.3-70b-versatile
  provider: custom:groq
```

```bash
# ~/.hermes/.env
GROQ_API_KEY=your-groq-key
```

#### Недоумение

Полезно, если вам нужна модель, которая автоматически выполняет онлайн-поиск и цитирование. Строгое определение доступных моделей — проверьте текущий список на [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api).

```yaml
# ~/.hermes/config.yaml
providers:
  perplexity:
    api: https://api.perplexity.ai
    key_env: PERPLEXITY_API_KEY

model:
  default: sonar
  provider: custom:perplexity
```

```bash
# ~/.hermes/.env
PERPLEXITY_API_KEY=your-perplexity-key
```

#### Несколько провайдеров в одной конфигурации

Составляются три рецепта — используйте их все вместе и переключайтесь по очереди с помощью `/model custom:<name>:<model>`:

```yaml
providers:
  together:
    api: https://api.together.xyz/v1
    key_env: TOGETHER_API_KEY
  groq:
    api: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY
  perplexity:
    api: https://api.perplexity.ai
    key_env: PERPLEXITY_API_KEY

model:
  default: MiniMaxAI/MiniMax-M2.7
  provider: custom:together      # boot to Together; switch freely after
```

:::совет Устранение неполадок
- `hermes doctor` не должен выводить никаких предупреждений `Unknown provider` для любого из этих имен после исправлений средства проверки CLI в #15083.
- Если конечная точка `/v1/models` провайдера недоступна (обычно используется Perplexity), `hermes model` сохранит модель с предупреждением, а не с жестким отклонением — см. #15136.
— Чтобы полностью пропустить именованные поставщики и использовать простой `provider: custom` с переменной env `CUSTOM_BASE_URL`, см. #15103.
:::

---

### Выбор правильной настройки

| Вариант использования | Рекомендуется |
|----------|-------------|
| **Просто хочу, чтобы это сработало** | OpenRouter (по умолчанию) или Nous Portal |
| **Локальные модели, простая настройка** | Оллама |
| **Обслуживание графического процессора** | vLLM или SGLang |
| **Mac / без графического процессора** | Оллама или llama.cpp |
| **Маршрутизация между несколькими провайдерами** | Прокси-сервер LiteLLM или OpenRouter |
| **Оптимизация затрат** | ClawRouter или OpenRouter с `sort: "price"` |
| **Максимальная конфиденциальность** | Ollama, vLLM или llama.cpp (полностью локальный) |
| **Корпоративный/Azure** | Azure OpenAI с настраиваемой конечной точкой |
| **Китайские модели искусственного интеллекта** | z.ai (GLM), Kimi/Moonshot (`kimi-coding` или `kimi-coding-cn`), MiniMax, Xiaomi MiMo или Tencent TokenHub (первоклассные провайдеры) |

:::совет
Вы можете переключаться между поставщиками в любое время с помощью `hermes model` — перезагрузка не требуется. Ваша история разговоров, память и навыки сохраняются независимо от того, каким провайдером вы пользуетесь.
:::

## Дополнительные ключи API

| Особенность | Провайдер | Переменная окружения |
|---------|----------|--------------|
| Парсинг веб-страниц | [Firecrawl](https://firecrawl.dev/) | `FIRECRAWL_API_KEY`, `FIRECRAWL_API_URL` |
| Автоматизация браузера | [База браузера](https://browserbase.com/) | `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` |
| Генерация изображений | [ФАЛ](https://fal.ai/) | `FAL_KEY` |
| Премиум-голоса TTS | [ElevenLabs](https://elevenlabs.io/) | `ELEVENLABS_API_KEY` |
| OpenAI TTS + транскрипция голоса | [OpenAI](https://platform.openai.com/api-keys) | `VOICE_TOOLS_OPENAI_KEY` |
| Mistral TTS + транскрипция голоса | [Мистраль](https://console.mistral.ai/) | `MISTRAL_API_KEY` |
| Межсессионное моделирование пользователей | [Хончо](https://honcho.dev/) | `HONCHO_API_KEY` |
| Семантическая долговременная память | [Суперпамять](https://supermemory.ai) | `SUPERMEMORY_API_KEY` |

### Самостоятельный Firecrawl

По умолчанию Hermes использует [облачный API Firecrawl](https://firecrawl.dev/) для веб-поиска и парсинга. Если вы предпочитаете запускать Firecrawl локально, вместо этого вы можете указать Hermes на автономный экземпляр. Полные инструкции по настройке см. в файле [SELF_HOST.md] (https://github.com/firecrawl/firecrawl/blob/main/SELF_HOST.md) Firecrawl.

**Что вы получаете:** Не требуется ключ API, нет ограничений по скорости, нет затрат на каждую страницу, полная независимость данных.

**Что вы теряете:** Облачная версия использует фирменную «Пожарную машину» Firecrawl для расширенного обхода защиты от ботов (Cloudflare, CAPTCHA, ротация IP-адресов). При самостоятельном размещении используется базовая выборка + Playwright, поэтому некоторые защищенные сайты могут выйти из строя. Поиск использует DuckDuckGo вместо Google.

**Настройка:**

1. Клонируйте и запустите стек Firecrawl Docker (5 контейнеров: API, Playwright, Redis, RabbitMQ, PostgreSQL — требуется ~4–8 ГБ ОЗУ):
   ```bash
   git clone https://github.com/firecrawl/firecrawl
   cd firecrawl
   # In .env, set: USE_DB_AUTHENTICATION=false, HOST=0.0.0.0, PORT=3002
   docker compose up -d
   ```

2. Наведите Hermes на свой экземпляр (ключ API не требуется):
   ```bash
   hermes config set FIRECRAWL_API_URL http://localhost:3002
   ```

Вы также можете установить `FIRECRAWL_API_KEY` и `FIRECRAWL_API_URL`, если на вашем локальном экземпляре включена аутентификация.

## Маршрутизация провайдера OpenRouter

Используя OpenRouter, вы можете контролировать маршрутизацию запросов между поставщиками. Добавьте раздел `provider_routing` в `~/.hermes/config.yaml`:

```yaml
provider_routing:
  sort: "throughput"          # "price" (default), "throughput", or "latency"
  # only: ["anthropic"]      # Only use these providers
  # ignore: ["deepinfra"]    # Skip these providers
  # order: ["anthropic", "google"]  # Try providers in this order
  # require_parameters: true  # Only use providers that support all request params
  # data_collection: "deny"   # Exclude providers that may store/train on data
```

**Ярлыки:** добавьте `:nitro` к любому названию модели для сортировки по пропускной способности (например, `anthropic/claude-sonnet-4:nitro`) или `:floor` для сортировки по цене.

## OpenRouter Маршрутизатор с кодом Парето

OpenRouter поставляет экспериментальный маршрутизатор с моделью кодирования по адресу `openrouter/pareto-code`, который автоматически перенаправляет запросы к самой дешевой модели, соответствующей планке качества кодирования (рейтинг по рейтингу [Искусственный анализ](https://artificialanaлиз.ai/)). Выберите эту модель и настройте ручку `min_coding_score` в `~/.hermes/config.yaml`:

```yaml
model:
  provider: openrouter
  model: openrouter/pareto-code

openrouter:
  min_coding_score: 0.65   # 0.0–1.0; higher = stronger (more expensive) coders. Default 0.65.
```

Примечания:

- `min_coding_score` отправляется **только**, когда `model.model` равен `openrouter/pareto-code`. В любой другой модели это значение не используется.
— Установите пустую строку (или удалите строку), чтобы позволить OpenRouter выбрать самый сильный доступный кодировщик — его задокументированное поведение, когда блок плагинов опущен.
- Выбор детерминирован по баллам в конкретный день, но фактическая выбранная модель может меняться по мере перемещения границы Парето (новые модели, обновления тестов).
- См. [Документацию по маршрутизатору Pareto] OpenRouter (https://openrouter.ai/docs/guides/routing/routers/pareto-router) для получения полного описания поведения маршрутизатора.
- Чтобы использовать маршрутизатор с кодом Парето для конкретной **вспомогательной задачи** (сжатие, просмотр и т. д.) вместо основного агента, установите `extra_body.plugins` для этой задачи — см. [Вспомогательные модели → Маршрутизация OpenRouter и код Парето для вспомогательных задач](/user-guide/configuration#openrouter-routing--pareto-code-for-вспомогательных-задач).

## Резервные поставщики

Настройте цепочку поставщиков резервного копирования, которую Hermes пытается выполнить в случае сбоя основной модели (ограничения скорости, ошибки сервера, сбои аутентификации). Канонический формат представляет собой список `fallback_providers:` верхнего уровня:

```yaml
fallback_providers:
  - provider: openrouter
    model: anthropic/claude-sonnet-4
  - provider: anthropic
    model: claude-sonnet-4
    # base_url: http://localhost:8000/v1    # optional, for custom endpoints
    # api_mode: chat_completions           # optional override
```

Устаревший однопарный диктофон `fallback_model:` по-прежнему принимается для обратной совместимости:

```yaml
fallback_model:
  provider: openrouter
  model: anthropic/claude-sonnet-4
```

При активации резервный вариант меняет модель и поставщика в середине сеанса, не теряя разговора. Цепочка проверяется запись за записью; активация осуществляется один раз за сеанс.

Поддерживаемые поставщики: `openrouter`, `nous`, `novita`, `openai-codex`, `copilot`, `copilot-acp`, `anthropic`, `gemini`, `qwen-oauth`, `huggingface`, `zai`, `kimi-coding`, `kimi-coding-cn`, `minimax`, `minimax-cn`, `minimax-oauth`, `deepseek`, `nvidia`, `xai`, `xai-oauth`, `ollama-cloud`, `bedrock`, `ai-gateway`, `azure-foundry`, `opencode-zen`, `opencode-go`, `commandcode`, `commandcode-anthropic`, `kilocode`, `xiaomi`, `arcee`, `gmi`, `actual`, `stepfun`, `lmstudio`, `alibaba`, `alibaba-coding-plan`, `tencent-tokenhub`, `custom`.

:::совет
Резервный режим настраивается исключительно через `config.yaml` или интерактивно через `hermes fallback`. Полную информацию о том, когда он срабатывает, как развивается цепочка и как она взаимодействует со вспомогательными задачами и делегированием, см. в разделе [Резервные поставщики](/user-guide/features/fallback-providers).
:::

---

## См. также

- [Конфигурация](/user-guide/configuration) — общая конфигурация (структура каталогов, приоритет конфигурации, серверные части терминала, память, сжатие и т. д.)
- [Переменные среды](/reference/environment-variables) — Полная ссылка на все переменные среды.