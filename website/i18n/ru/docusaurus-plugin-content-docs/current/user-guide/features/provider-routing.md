---
title: Маршрутизация провайдера
description: Настройте предпочтения поставщика OpenRouter или Nous Portal для оптимизации
  стоимости, скорости или качества.
sidebar_label: Provider Routing
sidebar_position: 7
---

# Маршрутизация провайдера

При использовании [OpenRouter](https://openrouter.ai) или [Nous Portal](/integrations/nous-portal) в качестве поставщика LLM агент Hermes поддерживает **маршрутизацию поставщика** — детальный контроль над тем, какие базовые поставщики ИИ обрабатывают ваши запросы и как им приоритезируются.

OpenRouter маршрутизирует запросы многим провайдерам (например, Anthropic, Google, AWS Bedrock, Together AI). Маршрутизация поставщика позволяет оптимизировать затраты, скорость, качество или обеспечить соблюдение конкретных требований поставщика.

:::совет
Трафик, маршрутизируемый через Nous Portal, учитывает те же предпочтения провайдера — и подписчики портала получают 10% скидку от поставщиков, оплачивающих токены.
:::

## Конфигурация

Добавьте раздел `provider_routing` в свой `~/.hermes/config.yaml`:

```yaml
provider_routing:
  sort: "price"           # How to rank providers
  only: []                # Whitelist: only use these providers
  ignore: []              # Blacklist: never use these providers
  order: []               # Explicit provider priority order
  require_parameters: false  # Only use providers that support all parameters
  data_collection: null   # Control data collection ("allow" or "deny")
```

:::информация
Маршрутизация провайдера применяется только при использовании OpenRouter или Nous Portal. Это не имеет никакого эффекта при прямом подключении к поставщику (например, при прямом подключении к Anthropic API).
:::

## Опции

### `sort`

Управляет тем, как OpenRouter ранжирует доступных поставщиков по вашему запросу.

| Значение | Описание |
|-------|-------------|
| `"price"` | Сначала самый дешевый провайдер |
| `"throughput"` | Самое быстрое количество токенов в секунду |
| `"latency"` | Самое низкое время первого токена |

```yaml
provider_routing:
  sort: "price"
```

### `only`

Белый список пулов провайдера. Если установлено, будут использоваться **только** эти поставщики. Все остальные исключены. Используйте строчные буквы, показанные OpenRouter для каждого провайдера.

```yaml
provider_routing:
  only:
    - "anthropic"
    - "google"
```

### `ignore`

Черный список имен провайдеров. Эти провайдеры **никогда** не будут использоваться, даже если они предлагают самый дешевый или быстрый вариант.

```yaml
provider_routing:
  ignore:
    - "together"
    - "deepinfra"
```

### `order`

Явный порядок приоритетов. Поставщики, перечисленные первыми, являются предпочтительными. Поставщики, не включенные в список, используются в качестве резервных.

```yaml
provider_routing:
  order:
    - "anthropic"
    - "google"
    - "amazon-bedrock"
```

### `require_parameters`

При `true` OpenRouter будет маршрутизироваться только к поставщикам, которые поддерживают **все** параметры вашего запроса (например, `temperature`, `top_p`, `tools` и т. д.). Это позволяет избежать бесшумного сброса параметров.

```yaml
provider_routing:
  require_parameters: true
```

### `data_collection`

Определяет, могут ли поставщики использовать ваши подсказки для обучения. Возможные варианты: `"allow"` или `"deny"`.

```yaml
provider_routing:
  data_collection: "deny"
```

## Практические примеры

### Оптимизация затрат

Перейдите к самому дешевому доступному провайдеру. Подходит для массового использования и разработки:

```yaml
provider_routing:
  sort: "price"
```

### Оптимизация для скорости

Отдайте приоритет поставщикам с низкой задержкой для интерактивного использования:

```yaml
provider_routing:
  sort: "latency"
```

### Оптимизация пропускной способности

Лучше всего подходит для генерации длинных форм, где имеет значение количество токенов в секунду:

```yaml
provider_routing:
  sort: "throughput"
```

### Привязка к конкретным поставщикам

Убедитесь, что все запросы проходят через определенного поставщика для обеспечения согласованности:

```yaml
provider_routing:
  only:
    - "anthropic"
```

### Избегайте конкретных поставщиков

Исключите поставщиков, которых вы не хотите использовать (например, в целях конфиденциальности данных):

```yaml
provider_routing:
  ignore:
    - "together"
    - "lepton"
  data_collection: "deny"
```

### Предпочтительный порядок с резервными вариантами

Сначала попробуйте предпочитаемых вами поставщиков, а если они недоступны, вернитесь к другим:

```yaml
provider_routing:
  order:
    - "anthropic"
    - "google"
  require_parameters: true
```

## Как это работает

Настройки маршрутизации поставщика передаются в OpenRouter или Nous Portal при запросах чата агента и сводках об ограничениях итераций через поле `extra_body.provider`. (`extra_body` — это аргумент OpenAI Python SDK; он становится объектом `provider` верхнего уровня в запросе JSON.) Вспомогательные задачи, такие как сжатие и создание заголовков, настраиваются независимо в `auxiliary.<task>.extra_body`.

- **Режим CLI** — настраивается в `~/.hermes/config.yaml`, загружается при запуске.
- **Режим шлюза** — тот же файл конфигурации, загружаемый при запуске шлюза.

Конфигурация маршрутизации считывается из `config.yaml` и передается в качестве параметров при создании `AIAgent`:

```
providers_allowed  ← from provider_routing.only
providers_ignored  ← from provider_routing.ignore
providers_order    ← from provider_routing.order
provider_sort      ← from provider_routing.sort
provider_require_parameters ← from provider_routing.require_parameters
provider_data_collection    ← from provider_routing.data_collection
```

:::совет
Вы можете комбинировать несколько вариантов. Например, можно отсортировать по цене, но исключить определенных поставщиков и потребовать поддержку параметров:

```yaml
provider_routing:
  sort: "price"
  ignore: ["together"]
  require_parameters: true
  data_collection: "deny"
```
:::

## Поведение по умолчанию

Если раздел `provider_routing` не настроен (по умолчанию), агрегатор использует собственную логику маршрутизации по умолчанию, которая обычно автоматически балансирует стоимость и доступность.

:::tip Маршрутизация поставщика и резервные модели
Поставщики контролируют маршрутизацию, которые **суб-провайдеры OpenRouter или Nous Portal** обрабатывают ваши запросы. Для автоматического переключения на совершенно другого поставщика в случае сбоя основной модели см. раздел [Резервные поставщики](/user-guide/features/fallback-providers).
:::