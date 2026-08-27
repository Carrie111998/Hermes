---
sidebar_position: 14
title: AWS Основа
description: Используйте агент Hermes с Amazon Bedrock — собственный API Converse,
  маршрутизация Anthropic SDK, модели OpenAI через Bedrock Mantle, аутентификация
  IAM, Guardrails и межрегиональный вывод.
---

# Основа AWS

Агент Hermes поддерживает Amazon Bedrock в качестве собственного поставщика. Это дает вам полный доступ к экосистеме Bedrock: аутентификация IAM, Guardrails, профили межрегионального вывода и все базовые модели.

Hermes направляет каждое семейство моделей через API, который лучше всего подходит для него:

| Модельная семья | API-маршрут | Почему |
|---|---|---|
| Антропный Клод | Антропный SDK (`AnthropicBedrock`) | Оперативное кэширование, планирование бюджета, адаптивное мышление — функции, недоступные в Converse |
| OpenAI GPT-5.5/GPT-5.6 (Солнце, Терра, Луна) | Конечная точка Bedrock Mantle **OpenAI Responses** (`bedrock-mantle.<region>.api.aws/openai/v1`) | Эти модели предназначены только для Mantle — на карточках моделей Bedrock-Runtime/Converse указаны как неподдерживаемые |
| Все остальное (Nova, DeepSeek, Llama, GPT-OSS, …) | Нативный **Converse API** (`bedrock-runtime`) | Полный набор функций Bedrock: ограждения, профили вывода, потоковая передача |

Все три маршрута используют одну и ту же цепочку учетных данных AWS и разрешение региона — отдельная настройка не требуется. Запросы к конечной точке Mantle аутентифицируются с помощью `AWS_BEARER_TOKEN_BEDROCK`, если он установлен, или подписываются SigV4 через стандартную цепочку учетных данных boto3, в противном случае.

## Предварительные условия

- **Учетные данные AWS** — любой источник, поддерживаемый [цепочкой учетных данных boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html):
  — Роль экземпляра IAM (EC2, ECS, Lambda — нулевая конфигурация)
  - `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` переменные среды
  – `AWS_PROFILE` для единого входа или именованных профилей.
  - `aws configure` для местного развития
- **boto3** — установить с помощью `cd ~/.hermes/hermes-agent && uv pip install -e ".[bedrock]"`
- **Разрешения IAM** — минимум:
  - `bedrock:InvokeModel` и `bedrock:InvokeModelWithResponseStream` (для вывода)
  - `bedrock:ListFoundationModels` и `bedrock:ListInferenceProfiles` (для обнаружения модели)

:::совет EC2/ECS/Lambda
В вычислениях AWS прикрепите роль IAM с помощью `AmazonBedrockFullAccess`, и все готово. Никаких ключей API, никакой конфигурации `.env` — Hermes автоматически определяет роль экземпляра.
:::

## Быстрый старт

```bash
# Install with Bedrock support
cd ~/.hermes/hermes-agent && uv pip install -e ".[bedrock]"

# Select Bedrock as your provider
hermes model
# → Choose "More providers..." → "AWS Bedrock"
# → Select your region and model

# Start chatting
hermes chat
```

## Конфигурация

После запуска `hermes model` ваш `~/.hermes/config.yaml` будет содержать:

```yaml
model:
  default: us.anthropic.claude-sonnet-4-6
  provider: bedrock
  base_url: https://bedrock-runtime.us-east-2.amazonaws.com

bedrock:
  region: us-east-2
```

### Регион

Установите регион AWS любым из этих способов (сначала высший приоритет):

1. `bedrock.region` в `config.yaml`
2. Переменная среды `AWS_REGION`.
3. Переменная среды `AWS_DEFAULT_REGION`.
4. По умолчанию: `us-east-1`

### Ограждения

Чтобы применить [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) ко всем вызовам модели:

```yaml
bedrock:
  region: us-east-2
  guardrail:
    guardrail_identifier: "abc123def456"  # From the Bedrock console
    guardrail_version: "1"                # Version number or "DRAFT"
    stream_processing_mode: "async"       # "sync" or "async"
    trace: "disabled"                     # "enabled", "disabled", or "enabled_full"
```

### Обнаружение модели

Гермес автоматически обнаруживает доступные модели через плоскость управления Bedrock. Вы можете настроить обнаружение:

```yaml
bedrock:
  discovery:
    enabled: true
    provider_filter: ["anthropic", "amazon"]  # Only show these providers
    refresh_interval: 3600                     # Cache for 1 hour
```

### Быстрое кэширование (cachePoint)

Hermes автоматически применяет кэширование подсказок к пути Bedrock **Converse API**, вставляя маркеры `cachePoint` после системного приглашения, определений инструментов и последнего сообщения. Поскольку отправка блока `cachePoint` модели, которая его не поддерживает, вызывает `ValidationException`, маркеры добавляются только для моделей из заведомо исправного белого списка (идентификаторы моделей Anthropic Claude и Amazon Nova); неизвестные модели по умолчанию не имеют маркеров кэша. Модели Claude обычно используют путь AnthropicBedrock SDK, который имеет собственное кэширование подсказок — путь Converse `cachePoint` охватывает Nova и резервный вариант Claude с токеном-носителем. Никакой настройки не требуется; Чтение/запись кэша отображаются в учете использования.

### Проверка контекстного окна

Для моделей, контекстное окно которых отсутствует в статической таблице Hermes, Hermes может проверить реальный предел, отправляя запросы слишком большого размера на фиксированных уровнях (~ 1,3 млн и ~ 2,2 млн токенов) и анализируя `maximum`, указанный в ошибке проверки длины Bedrock. Зондируемые значения передаются в тот же кэш метаданных, что и статическая таблица; устаревшие кэшированные записи, которые занижают значение окна модели (например, записи, посеянные до того, как окно 1M модели стало общедоступным), автоматически отбрасываются в пользу большего известного значения.

## Доступные модели

Модели Bedrock используют **идентификаторы профилей вывода** для вызова по требованию. Средство выбора `hermes model` отображает их автоматически, причем рекомендуемые модели находятся вверху:

| Модель | удостоверение личности | Заметки |
|-------|-----|-------|
| Клод Сонет 4.6 | `us.anthropic.claude-sonnet-4-6` | Рекомендуется — лучший баланс скорости и возможностей |
| Клод Опус 4.6 | `us.anthropic.claude-opus-4-6-v1` | Самый способный |
| Клод Хайку 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Самый быстрый Клод |
| OpenAI GPT-5.6 Сол | `openai.gpt-5.6-sol` | Пограничная модель OpenAI (через Bedrock Mantle) |
| OpenAI GPT-5.6 Терра | `openai.gpt-5.6-terra` | Сбалансированный (через Bedrock Mantle) |
| OpenAI GPT-5.6 Луна | `openai.gpt-5.6-luna` | Быстро, доступно (через Bedrock Mantle) |
| OpenAI GPT-5.5 | `openai.gpt-5.5` | Предыдущий флагман OpenAI (через Bedrock Mantle) |
| Амазон Нова Про | `us.amazon.nova-pro-v1:0` | Флагман Amazon |
| Амазон Нова Микро | `us.amazon.nova-micro-v1:0` | Самый быстрый, дешевый |
| ДипСик V3.2 | `deepseek.v3.2` | Сильная открытая модель |
| Лама 4 Скаут 17Б | `us.meta.llama4-scout-17b-instruct-v1:0` | Последние новости Меты |

:::info Межрегиональный вывод
Модели с префиксом `us.` используют межрегиональные профили вывода, которые обеспечивают лучшую пропускную способность и автоматическое переключение при сбое в разных регионах AWS. Модели с префиксом `global.` маршрутизируются во все доступные регионы по всему миру. Идентификаторы моделей OpenAI `openai.*` обслуживаются Bedrock Mantle в настроенном регионе и не используют префиксы профиля вывода.
:::

## Переключение моделей в середине сеанса

Используйте команду `/model` во время разговора:

```
/model us.amazon.nova-pro-v1:0
/model deepseek.v3.2
/model us.anthropic.claude-opus-4-6-v1
```

## Диагностика

```bash
hermes doctor
```

Врач проверяет:
- Доступны ли учетные данные AWS (переменные среды, роль IAM, SSO)
- Установлен ли `boto3`
— Доступен ли API Bedrock (ListFoundationModels).
- Количество доступных моделей в вашем регионе

## Шлюз (платформы обмена сообщениями)

Bedrock работает со всеми шлюзовыми платформами Hermes (Telegram, Discord, Slack, Feishu и т. д.). Настройте Bedrock в качестве своего провайдера, затем запустите шлюз как обычно:

```bash
hermes gateway setup
hermes gateway start
```

Шлюз читает `config.yaml` и использует ту же конфигурацию поставщика Bedrock.

## Устранение неполадок

### «Ключ API не найден» / «Нет учетных данных AWS»

Гермес проверяет учетные данные в следующем порядке:
1. `AWS_BEARER_TOKEN_BEDROCK`
2. `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`
3. `AWS_PROFILE`
4. Метаданные экземпляра EC2 (IMDS)
5. Учетные данные контейнера ECS
6. Роль исполнения Lambda

Если ничего не найдено, запустите `aws configure` или прикрепите роль IAM к своему вычислительному экземпляру.

### «Вызов идентификатора модели... с пропускной способностью по требованию не поддерживается»

Используйте **идентификатор профиля вывода** (с префиксом `us.` или `global.`) вместо идентификатора базовой модели. Например:
- ❌ `anthropic.claude-sonnet-4-6`
- ✅ `us.anthropic.claude-sonnet-4-6`

### "Исключение регулирования"

Вы достигли предела скорости Bedrock для каждой модели. Гермес автоматически повторяет попытку с отсрочкой. Чтобы увеличить лимиты, запросите увеличение квоты в [консоли AWS Service Quotas](https://console.aws.amazon.com/servicequotas/).

## Развертывание AWS в один клик

Для полностью автоматического развертывания в EC2 с помощью CloudFormation:

**[sample-hermes-agent-on-aws-with-bedrock](https://github.com/JiaDe-Wu/sample-hermes-agent-on-aws-with-bedrock)** — создает VPC, роль IAM, экземпляр EC2 и автоматически настраивает Bedrock. Развертывание в любом регионе одним щелчком мыши.