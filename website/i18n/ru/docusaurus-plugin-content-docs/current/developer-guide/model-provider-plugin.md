---
sidebar_position: 10
title: Плагины поставщика моделей
description: Как создать плагин поставщика моделей (сервер вывода) для агента Hermes
---

# Создание плагина поставщика моделей

Плагины поставщика моделей объявляют серверную часть вывода — совместимую с OpenAI конечную точку, сервер антропных сообщений, API ответов в стиле Кодекса или собственную поверхность Bedrock — через которую Hermes может маршрутизировать вызовы `AIAgent`. Каждый встроенный провайдер (OpenRouter, Anthropic, GMI, DeepSeek, Nvidia и т. д.) поставляется в виде одного из этих плагинов. Третьи лица могут добавить свои собственные, разместив каталог под `$HERMES_HOME/plugins/model-providers/` без изменений в репозитории.

:::совет
Плагины провайдера модели — это третий тип **плагина провайдера**. Остальные — это [Плагины поставщика памяти](/developer-guide/memory-provider-plugin) (межсессионные знания) и [Плагины контекстного механизма](/developer-guide/context-engine-plugin) (стратегии сжатия контекста). Все три следуют одному и тому же шаблону «удалить каталог, объявить профиль, никаких изменений в репозитории».
:::

## Как работает обнаружение

`providers/__init__.py._discover_providers()` выполняется лениво при первом вызове кода `get_provider_profile()` или `list_providers()`. Порядок открытия:

1. **Плагины в комплекте** — `<repo>/plugins/model-providers/<name>/` — поставляются с Hermes
2. **Пользовательские плагины** — `$HERMES_HOME/plugins/model-providers/<name>/` — закидываем в любую директорию; не требуется перезапуск для последующих сеансов
3. **Устаревший однофайловый** — `<repo>/providers/<name>.py` — обратная совместимость для редактируемых установок вне дерева.

**Пользовательские плагины переопределяют встроенные плагины с тем же именем**, поскольку `register_provider()` выигрывает последним. Перетащите каталог `$HERMES_HOME/plugins/model-providers/gmi/`, чтобы заменить встроенный профиль GMI, не трогая репозиторий.

## Структура каталогов

```
plugins/model-providers/my-provider/
├── __init__.py       # Calls register_provider(profile) at module-level
├── plugin.yaml       # kind: model-provider + metadata (optional but recommended)
└── README.md         # Setup instructions (optional)
```

Единственный необходимый файл — `__init__.py`. `plugin.yaml` используется `hermes plugins` для самоанализа и общим PluginManager для маршрутизации плагина к нужному загрузчику; без него общий загрузчик возвращается к эвристике исходного текста.

## Минимальный пример — простой поставщик API-ключей

```python
# plugins/model-providers/acme-inference/__init__.py
from providers import register_provider
from providers.base import ProviderProfile

acme = ProviderProfile(
    name="acme-inference",
    aliases=("acme",),
    display_name="Acme Inference",
    description="Acme — OpenAI-compatible direct API",
    signup_url="https://acme.example.com/keys",
    env_vars=("ACME_API_KEY", "ACME_BASE_URL"),
    base_url="https://api.acme.example.com/v1",
    auth_type="api_key",
    default_aux_model="acme-small-fast",
    fallback_models=(
        "acme-large-v3",
        "acme-medium-v3",
        "acme-small-fast",
    ),
)

register_provider(acme)
```

```yaml
# plugins/model-providers/acme-inference/plugin.yaml
name: acme-inference
kind: model-provider
version: 1.0.0
description: Acme Inference — OpenAI-compatible direct API
author: Your Name
```

Вот и все. После удаления этих двух файлов происходит следующее **автоматическое соединение** без каких-либо других изменений:

| Интеграция | Где | Что это дает |
|---|---|---|
| Разрешение учетных данных | `hermes_cli/auth.py` | `PROVIDER_REGISTRY["acme-inference"]` заполнено из профиля |
| `--provider` Флаг CLI | `hermes_cli/main.py` | Принимает `acme-inference` |
| `hermes model` сборщик | `hermes_cli/models.py` | Появляется в `CANONICAL_PROVIDERS`, список моделей получен из `{base_url}/models` |
| `hermes doctor` | `hermes_cli/doctor.py` | Проверка работоспособности зонда `ACME_API_KEY` + `{base_url}/models` |
| `hermes setup` | `hermes_cli/config.py` | `ACME_API_KEY` появляется в `OPTIONAL_ENV_VARS` и мастере установки |
| Обратное сопоставление URL-адресов | `agent/model_metadata.py` | Имя хоста → имя провайдера для автоопределения |
| Вспомогательная модель | `agent/auxiliary_client.py` | Использует `default_aux_model` для сжатия/суммирования |
| Разрешение во время выполнения | `hermes_cli/runtime_provider.py` | Возвращает правильные `base_url`, `api_key`, `api_mode` |
| Транспорт | `agent/transports/chat_completions.py` | Путь к профилю генерирует кварги через `prepare_messages` / `build_extra_body` / `build_api_kwargs_extras` |

## Поля ProviderProfile

Полное определение в `providers/base.py`. Самые полезные:

| Поле | Тип | Цель |
|---|---|---|
| `name` | ул | Канонический идентификатор — соответствует `model.provider` в `config.yaml` и флагу `--provider` |
| `aliases` | `tuple[str, ...]` | Альтернативные имена, разрешенные `get_provider_profile()` (например, `grok` → `xai`) |
| `api_mode` | ул | `chat_completions` \| `codex_responses` \| `anthropic_messages` \| `bedrock_converse` |
| `display_name` | ул | Человеческая метка показана в средстве выбора `hermes model` |
| `description` | ул | Выбор субтитров |
| `signup_url` | ул | Отображается при первом запуске («получите ключ API здесь») |
| `env_vars` | `tuple[str, ...]` | Переменные среды API-ключа в порядке приоритета; последняя запись `*_BASE_URL` используется в качестве переопределения базового URL-адреса пользователя |
| `base_url` | ул | Конечная точка вывода по умолчанию |
| `models_url` | ул | Явный URL-адрес каталога (возврат к `{base_url}/models`) |
| `auth_type` | ул | `api_key` \| `oauth_device_code` \| `oauth_external` \| `copilot` \| `aws_sdk` \| `external_process` |
| `fallback_models` | `tuple[str, ...]` | Кураторский список отображается при сбое получения живого каталога |
| `default_headers` | `dict[str, str]` | Отправляется по каждому запросу (например, `Editor-Version` второго пилота) |
| `fixed_temperature` | Любой | `None` = использовать значение вызывающего абонента; `OMIT_TEMPERATURE` Sentinel = вообще не отправлять температуру (Кими) |
| `default_max_tokens` | `int \| None` | Максимальное количество токенов на уровне поставщика (Nvidia: 16384) |
| `default_aux_model` | ул | Дешевая модель для вспомогательных задач (сжатие, просмотр, обобщение) |

## Переопределяемые хуки

Подкласс `ProviderProfile` для нетривиальных особенностей:

```python
from typing import Any
from providers.base import ProviderProfile

class AcmeProfile(ProviderProfile):
    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Provider-specific message preprocessing. Runs after codex
        sanitization, before developer-role swap. Default: pass-through."""
        # Example: Qwen normalizes plain-text content to a list-of-parts
        # array and injects cache_control; Kimi rewrites tool-call JSON
        return messages

    def build_extra_body(self, *, session_id=None, **context) -> dict:
        """Provider-specific extra_body fields merged into the API call.
        Context includes: session_id, provider_preferences, model, base_url,
        reasoning_config. Default: empty dict."""
        # Example: OpenRouter's provider-preferences block,
        # Gemini's thinking_config translation.
        return {}

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        """Returns (extra_body_additions, top_level_kwargs). Needed when some
        fields go top-level (Kimi's reasoning_effort, OpenRouter's verbosity for
        adaptive Anthropic models) and some go in extra_body (OpenRouter's
        reasoning dict). Default: ({}, {})."""
        return {}, {}

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0) -> list[str] | None:
        """Live catalog fetch. Default hits {models_url or base_url}/models with
        Bearer auth. Override for: custom auth (Anthropic), no REST endpoint
        (Bedrock → None), or public/unauthenticated catalogs (OpenRouter)."""
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)
```

## Справочные примеры перехватчиков

Посмотрите на эти встроенные плагины для идиом:

| Плагин | Зачем смотреть |
|---|---|
| `plugins/model-providers/openrouter/` | Агрегатор с настройками провайдера, публичный каталог моделей |
| `plugins/model-providers/gemini/` | `thinking_config` перевод (родной + вложенные формы, совместимые с OpenAI) |
| `plugins/model-providers/kimi-coding/` | `OMIT_TEMPERATURE`, `extra_body.thinking`, верхний уровень `reasoning_effort` |
| `plugins/model-providers/qwen-oauth/` | Нормализация сообщений, внедрение `cache_control`, VL с высоким разрешением |
| `plugins/model-providers/nous/` | Теги атрибуции: «пропускать рассуждения при отключении» |
| `plugins/model-providers/custom/` | Оллама `num_ctx` + `think: false` причуды |
| `plugins/model-providers/bedrock/` | `api_mode="bedrock_converse"`, `fetch_models` возвращает None (нет конечной точки REST) ​​|

## Пользовательские переопределения — замена встроенного без редактирования репозитория

Допустим, вы хотите указать `gmi` на свою частную промежуточную конечную точку для тестирования. Создайте `~/.hermes/plugins/model-providers/gmi/__init__.py`:

```python
from providers import register_provider
from providers.base import ProviderProfile

register_provider(ProviderProfile(
    name="gmi",
    aliases=("gmi-cloud", "gmicloud"),
    env_vars=("GMI_API_KEY",),
    base_url="https://gmi-staging.internal.example.com/v1",
    auth_type="api_key",
    default_aux_model="google/gemini-3.1-flash-lite-preview",
))
```

Следующий сеанс `get_provider_profile("gmi").base_url` возвращает промежуточный URL. Никакого репо-патча, никакой перестройки. Поскольку пользовательские плагины обнаруживаются после встроенных, побеждает пользовательский вызов `register_provider()`.

## выбор api_mode

Признаются четыре ценности. Гермес выбирает один на основе:

1. Пользовательское явное переопределение (`config.yaml` `model.api_mode`, если установлено)
2. Диспетчеризация OpenCode для каждой модели (`opencode_model_api_mode` для Zen и Go)
3. Автоопределение URL — суффикс `/anthropic` → `anthropic_messages`, `api.openai.com` → `codex_responses`, `api.x.ai` → `codex_responses`, `/coding` на доменах Kimi → `chat_completions`
4. **Профиль `api_mode`** в качестве запасного варианта, если обнаружение URL-адреса ничего не находит.
5. По умолчанию `chat_completions`

Установите `profile.api_mode` в соответствии со значением по умолчанию, которое предоставляет ваш провайдер — это действует как подсказка. Переопределение URL-адреса пользователя по-прежнему имеет преимущество.

## Типы аутентификации

| `auth_type` | Значение | Кто этим пользуется |
|---|---|---|
| `api_key` | Одиночная переменная env содержит статический ключ API | Большинство провайдеров |
| `oauth_device_code` | Поток кода устройства OAuth | — |
| `oauth_external` | Пользователь входит в систему в другом месте, токены попадают в `auth.json` | Антропный OAuth, MiniMax OAuth, Qwen Portal, Nous Portal |
| `copilot` | Цикл обновления токена GitHub Copilot | Только плагин `copilot` |
| `aws_sdk` | Цепочка учетных данных AWS SDK (роль IAM, профиль, среда) | Только плагин `bedrock` |
| `external_process` | Аутентификация, обрабатываемая подпроцессом, порождаемым агентом | Только плагин `copilot-acp` |

`auth_type` определяет, какие кодовые пути рассматривают вашего провайдера как «простого поставщика API-ключей» — если это не `api_key`, PluginManager по-прежнему записывает манифест, но автоматизация уровня CLI Hermes (проверка врача, флаг `--provider`, делегирование мастера настройки) может пропустить его.

## Время обнаружения

Обнаружение поставщика является **ленивым** — оно запускается при первом вызове `get_provider_profile()` или `list_providers()` в процессе. На практике это происходит на ранних этапах запуска (загрузка модуля `auth.py` быстро расширяется на `PROVIDER_REGISTRY`). Если вам нужно проверить загрузку плагина, запустите:

```bash
hermes doctor
```

— успешный профиль `auth_type="api_key"` появится в разделе «Подключение к поставщику» с пробой `/models`.

Для программной проверки:

```python
from providers import list_providers
for p in list_providers():
    print(p.name, p.base_url, p.api_mode)
```

## Тестирование вашего плагина

Укажите `HERMES_HOME` временный каталог, чтобы не загрязнять реальную конфигурацию:

```bash
export HERMES_HOME=/tmp/hermes-plugin-test
mkdir -p $HERMES_HOME/plugins/model-providers/my-provider
cat > $HERMES_HOME/plugins/model-providers/my-provider/__init__.py <<'EOF'
from providers import register_provider
from providers.base import ProviderProfile
register_provider(ProviderProfile(
    name="my-provider",
    env_vars=("MY_API_KEY",),
    base_url="https://api.my-provider.example.com/v1",
    auth_type="api_key",
))
EOF

export MY_API_KEY=your-test-key
hermes -z "hello" --provider my-provider -m some-model
```

## Общая интеграция PluginManager

Общий `PluginManager` (то, чем работает `hermes plugins`) **видит** плагины поставщика модели, но не импортирует их — `providers/__init__.py` владеет их жизненным циклом. Менеджер записывает манифест для самоанализа и классифицирует его по `kind: model-provider`. Когда вы помещаете немаркированный пользовательский плагин в `$HERMES_HOME/plugins/`, который вызывает `register_provider` с `ProviderProfile`, менеджер автоматически приводит его к `kind: model-provider` с помощью эвристики исходного текста — поэтому плагин по-прежнему правильно маршрутизируется даже без `plugin.yaml`.

## Распространение через pip

Поставщики моделей могут поставляться в виде пакета pip. Выставьте точку входа в
Группа `hermes_agent.plugins` в вашем `pyproject.toml`:

```toml
[project.entry-points."hermes_agent.plugins"]
acme-inference = "acme_hermes_plugin:register"
```

Целью может быть:

- **вызываемый** (`module:func`) — вызывается без аргументов; оно должно позвонить
  `register_provider(profile)` или
- **пустой модуль** (`module`) — импортирован на уровне модуля.
  `register_provider(...)` побочный эффект, зеркальное отображение плагина каталога
  `__init__.py` контракт.

`providers/__init__.py` сам обнаруживает эти точки входа — общий
`PluginManager` никогда не вызывает регистрацию поставщика для пакетов pip (его
Путь точки входа нацелен на общие плагины в стиле `register(ctx)`, закрытые
`plugins.enabled`), поэтому реестр провайдера выполняет собственное сканирование. Два правила
применить:

- **Требуется согласие.** Тот же список разрешений `plugins.enabled` (и
  `plugins.disabled` запрещенный список) из `config.yaml` управляет этим сканированием. Пипс
  пакет никогда не импортируется только потому, что он установлен — пользователи должны добавить
  имя точки входа в `plugins.enabled`:

  ```yaml
  plugins:
    enabled:
      - acme-inference
  ```

- **Самый низкий приоритет.** Плагины точки входа обнаруживаются **раньше**.
  плагины файловой системы: поскольку `register_provider()` выигрывает последний автор,
  связанный профиль или профиль `$HERMES_HOME` с тем же именем всегда переопределяет
  установленный по протоколу. Пакет pip может добавить действительно нового провайдера, но
  не может незаметно перехватить имя стороннего поставщика.

Цели, которым требуются аргументы (`register(ctx)` общего плагина),
пропущены при сканировании провайдера — они принадлежат `PluginManager`. Сломанный
точка входа изолирована — она регистрируется на уровне предупреждения и пропускается, и никогда
блокирует обнаружение других провайдеров.

См. [Создание плагина Hermes](/developer-guide/plugins#distribute-via-pip) для полной настройки точек входа.

## Похожие страницы

- [Provider Runtime](/developer-guide/provider-runtime) — приоритет разрешения + где каждый уровень считывает профиль.
- [Добавление провайдеров](/developer-guide/adding-providers) — сквозной контрольный список для новых бэкэндов вывода (охватывает как быстрый путь плагина, так и полную интеграцию CLI/аутентификации)
- [Плагины поставщика памяти](/developer-guide/memory-provider-plugin)
- [Плагины контекстного движка](/developer-guide/context-engine-plugin)
- [Создание плагина Hermes](/developer-guide/plugins) — общая разработка плагинов.