---
sidebar_position: 15
title: Google Вертекс ИИ
description: Используйте агент Hermes с Gemini в Google Cloud Vertex AI — учетная
  запись службы OAuth2 или ADC, выставление счетов и квоты GCP, без статического ключа
  API
---

# Google Вершинный ИИ

Агент Hermes поддерживает **модели Gemini в Google Cloud Vertex AI** через конечную точку Vertex, совместимую с OpenAI. В отличие от [поставщика Google AI Studio](/guides/google-gemini) (который использует статический ключ API для `generativelanguage.googleapis.com`), Vertex предоставляет вам **ограничения ставок корпоративного уровня и выставление счетов/кредитов GCP** и является правильным выбором, если вы хотите, чтобы использование Gemini использовалось в вашей учетной записи Google Cloud, а не в ключе AI Studio.

:::info Vertex аутентифицируется с помощью OAuth2, а не ключа API
У Vertex **нет статического ключа API** для стандартной конечной точки. Для каждого запроса требуется кратковременный **токен доступа OAuth2** (время жизни около 1 часа), созданный либо из JSON сервисной учетной записи, либо из учетных данных приложения по умолчанию (ADC). Hermes чеканит и **автоматически обновляет** эти токены за вас — вам никогда не придется вставлять жетоны вручную. Вот почему вставка временного токена в поле `api_key` пользовательского поставщика не работает: срок его действия истекает в середине сеанса.
:::

## Предварительные условия

– **Проект Google Cloud** с **включенным API Vertex AI** и активным выставлением счетов.
- **Учетные данные**, один из:
  - JSON-файл **ключевого аккаунта службы** с ролью `roles/aiplatform.user` или
  - **Учетные данные приложения по умолчанию** через `gcloud auth application-default login` (или сервер метаданных при работе на виртуальной машине GCP).
- **`google-auth`** — устанавливается автоматически при первом выборе Vertex (отложенная установка). Запустите `hermes setup`, чтобы восстановить управляемую установку, если это не удалось.

## Быстрый старт

```bash
# Option A — service account JSON (recommended for servers / gateways)
echo "VERTEX_CREDENTIALS_PATH=/path/to/service-account.json" >> ~/.hermes/.env

# Option B — Application Default Credentials (good for local dev)
gcloud auth application-default login

# Select Vertex as your provider
hermes model
# → Choose "More providers..." → "Google Vertex AI"
# → Enter your GCP project ID (or leave blank to use the one in your credentials)
# → Choose a region (default: global)
# → Select a Gemini model

# Start chatting
hermes chat
```

## Конфигурация

Vertex разделяет свои настройки по чувствительности:

– **Путь учетных данных** является указателем на секрет и находится в `~/.hermes/.env`.
- **Идентификатор проекта и регион** не являются секретными настройками маршрутизации и находятся в `~/.hermes/config.yaml`.

`~/.hermes/.env`:

```bash
# One of these (checked in this order); omit both to use ADC:
VERTEX_CREDENTIALS_PATH=/path/to/service-account.json
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

`~/.hermes/config.yaml`:

```yaml
model:
  default: google/gemini-3-flash-preview
  provider: vertex

vertex:
  project_id: my-gcp-project   # blank → use the project embedded in the credentials
  region: global               # "global" is required for the Gemini 3.x previews
```

:::tip Переменные среды выигрывают у config.yaml
`VERTEX_PROJECT_ID` и `VERTEX_REGION` переопределяют значения `vertex.project_id`/`vertex.region` в `config.yaml`. Используйте их для переопределения каждой оболочки; сохраните постоянные настройки в `config.yaml`.
:::

### Как работает аутентификация

1. Hermes разрешает учетные данные в следующем порядке: `VERTEX_CREDENTIALS_PATH` → `GOOGLE_APPLICATION_CREDENTIALS` → ADC.
2. Он создает токен доступа OAuth2 (область `cloud-platform`) и кэширует его, обновляя, когда срок действия токена истекает в течение 5 минут.
3. Токен передается стандартному клиенту OpenAI, указанному в конечной точке Vertex:
   ```text
   https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{region}/endpoints/openapi
   ```
   Вместо этого в региональных местоположениях используется хост `{region}-aiplatform.googleapis.com`.
4. Если сеанс длится дольше, чем срок действия токена, и запрос возвращает `401`, Hermes повторно выпускает токен и автоматически повторяет попытку. На долгоработающем шлюзе, если срок действия токена обновления ADC истек, Hermes возвращается к JSON сервисной учетной записи, когда он настроен.

## Доступные модели

Для Vertex требуется префикс поставщика `google/` для идентификаторов моделей. Средство выбора `hermes model` предлагает:

| Модель | удостоверение личности |
|-------|----|
| Предварительный просмотр Gemini 3.1 Pro | `google/gemini-3.1-pro-preview` |
| Предварительный обзор Gemini 3 Pro | `google/gemini-3-pro-preview` |
| Предварительный просмотр Близнецов 3 | `google/gemini-3-flash-preview` |
| Предварительный просмотр Gemini 3.1 Flash Lite | `google/gemini-3.1-flash-lite-preview` |
| Близнецы 2.5 Про | `google/gemini-2.5-pro` |
| Близнецы 2.5 Флэш | `google/gemini-2.5-flash` |

:::обратите внимание на регион `global` для Gemini 3.x
Предварительные модели Gemini 3.x обслуживаются через конечную точку `global`. Региональные конечные точки (`us-central1` и т. д.) могут 404 их. Оставьте `region: global`, если у вас нет особой причины закрепить регион.
:::

## Переключение моделей в середине сеанса

```text
/model google/gemini-3-pro-preview
/model google/gemini-3-flash-preview
```

`/model` переключается между уже настроенными поставщиками и моделями; он не собирает новые учетные данные. Сначала настройте Vertex с помощью `hermes model`.

## Рассуждение/мышление

Vertex раскрывает мыслительный бюджет Gemini через поверхность, совместимую с OpenAI. Гермес автоматически отображает настройки своих рассуждений на `extra_body.google.thinking_config`, поэтому `reasoning_effort` работает так же, как и на других поверхностях Gemini.

## Диагностика

```bash
hermes doctor
```

Доктор сообщает, можно ли разрешить учетные данные Vertex (путь к учетной записи службы или ADC) и настроен ли поставщик.

## Устранение неполадок

### «Не удалось разрешить учетные данные Vertex AI»

Гермес не нашел ни JSON сервисного аккаунта, ни работающего ADC. Либо установите `VERTEX_CREDENTIALS_PATH` в `~/.hermes/.env`, либо запустите `gcloud auth application-default login`. Если ваш проект не встроен в учетные данные, установите `vertex.project_id` в `config.yaml`.

### `google-auth` не установлен

Hermes выполняет ленивую установку при первом выборе поставщика Vertex. Если это не помогло, запустите `hermes setup`, чтобы восстановить управляемую установку.

### 404 на моделях Gemini 3.x

Вероятно, вы находитесь на региональной конечной точке. Установите `region: global` в разделе `vertex:` `config.yaml` (или отключите `VERTEX_REGION`).

### 403 / разрешение отклонено

Учетной записи службы (или вашему идентификатору ADC) требуется роль `roles/aiplatform.user` в проекте, и для этого проекта должен быть включен API Vertex AI.

## Похожие

- [Google Gemini (AI Studio)](/guides/google-gemini) — Gemini со статическим API-ключом без GCP
- [AWS Bedrock](/guides/aws-bedrock) — еще одна встроенная интеграция с облачным провайдером.
- [Поставщики ИИ](/интеграции/поставщики)
- [Конфигурация](/руководство пользователя/конфигурация)