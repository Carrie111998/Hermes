---
sidebar_position: 6
title: Встречи команд
description: Настройка конвейера сводки собраний Microsoft Teams с помощью веб-перехватчиков
  Microsoft Graph
---

# собраний Microsoft Teams

Используйте конвейер собраний Teams, если вы хотите, чтобы Hermes принимал события собраний Microsoft Graph, сначала получал стенограммы, при необходимости возвращался к записям и STT и доставлял структурированную сводку нижестоящим приемникам.

Предварительные требования: см. [Microsoft Teams](./teams.md) для базовой настройки бота/учетных данных.

> Запустите `hermes gateway setup` и выберите **Собрания команд** для пошагового руководства.

На этой странице основное внимание уделяется настройке и включению:
- Учетные данные графика
- конфигурация прослушивателя вебхука
- Режимы доставки команд
- форма конфигурации конвейера

Для операций второго дня, проверок ввода в эксплуатацию и рабочего листа оператора используйте специальное руководство: [Управление конвейером собраний Teams](/guides/operate-teams-meeting-pipeline).

## Что делает эта функция

Трубопровод:
1. получает события веб-перехватчика Microsoft Graph.
2. разрешает встречу и в первую очередь предпочитает артефакты стенограммы
3. возвращается к загрузке записи плюс STT, когда расшифровка недоступна.
4. долговременно сохраняет состояние заданий и локально сохраняет записи.
5. может писать резюме в Notion, Linear и Microsoft Teams.

Действия оператора остаются в CLI (подкоманда `teams-pipeline` зарегистрирована плагином `teams_pipeline` — включите ее через `hermes plugins enable teams_pipeline` или установите `plugins.enabled: [teams_pipeline]` в `config.yaml`):

```bash
hermes teams-pipeline validate
hermes teams-pipeline list
hermes teams-pipeline maintain-subscriptions
```

## Предварительные условия

Прежде чем включать конвейер собраний, убедитесь, что у вас есть:

- рабочая установка Гермеса
- существующая [настройка бота Microsoft Teams](/user-guide/messaging/teams), если вы хотите исходящую доставку Teams.
- Учетные данные приложения Microsoft Graph с разрешениями, необходимыми для ресурсов собрания, на которые вы планируете подписаться.
— общедоступный URL-адрес HTTPS, который Microsoft Graph может вызывать для доставки веб-перехватчика.
- `ffmpeg` устанавливается, если вы хотите использовать резервную версию записи плюс STT.

## Шаг 1. Добавьте учетные данные Microsoft Graph

Добавьте учетные данные только для приложения Graph в `~/.hermes/.env`:

```bash
MSGRAPH_TENANT_ID=<tenant-id>
MSGRAPH_CLIENT_ID=<client-id>
MSGRAPH_CLIENT_SECRET=<client-secret>
```

Эти учетные данные используются:
- клиентский фонд Graph
- команды обслуживания подписки
- Разрешение собраний и выборка артефактов
— Исходящая доставка Teams на основе графов, если вы не предоставляете выделенный токен доступа к Teams.

## Шаг 2. Включите прослушиватель веб-перехватчика графа

Прослушиватель веб-перехватчика — это шлюзовая платформа с именем `msgraph_webhook`. Как минимум, включите его и установите значение состояния клиента:

```bash
MSGRAPH_WEBHOOK_ENABLED=true
MSGRAPH_WEBHOOK_PORT=8646
MSGRAPH_WEBHOOK_CLIENT_STATE=<random-shared-secret>
MSGRAPH_WEBHOOK_ACCEPTED_RESOURCES=communications/onlineMeetings
```

Хост привязки считывается из `extra.host` платформы в `config.yaml` (переменная env `MSGRAPH_WEBHOOK_HOST` отсутствует — см. [ссылку на прослушиватель webhook](msgraph-webhook.md)).

Слушатель раскрывает:
- `/msgraph/webhook` для уведомлений графика.
– `/health` для простой проверки работоспособности.

Вам необходимо направить вашу общедоступную конечную точку HTTPS к этому прослушивателю. Например, если ваш общедоступный домен — `https://ops.example.com`, URL-адрес уведомлений Graph обычно будет таким:

```text
https://ops.example.com/msgraph/webhook
```

## Шаг 3. Настройка доставки команд и поведения конвейера

Конвейер собраний считывает свою конфигурацию времени выполнения из существующей записи платформы `teams`. Ручки, специфичные для конвейера, живут под номером `teams.extra.meeting_pipeline`. Исходящая доставка Teams остается на обычной поверхности конфигурации платформы Teams.

Пример `~/.hermes/config.yaml`:

```yaml
platforms:
  msgraph_webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8646
      client_state: "replace-me"
      accepted_resources:
        - "communications/onlineMeetings"

  teams:
    enabled: true
    extra:
      client_id: "your-teams-client-id"
      client_secret: "your-teams-client-secret"
      tenant_id: "your-teams-tenant-id"

      # outbound summary delivery
      delivery_mode: "graph" # or incoming_webhook
      team_id: "team-id"
      channel_id: "channel-id"
      # incoming_webhook_url: "https://..."

      meeting_pipeline:
        transcript_min_chars: 80
        transcript_required: false
        transcription_fallback: true
        ffmpeg_extract_audio: true
        notion:
          enabled: false
        linear:
          enabled: false
```

Если вы привязываете прослушиватель к узлу без обратной связи, например `0.0.0.0`, вы также должны установить `allowed_source_cidrs` в выходные диапазоны веб-перехватчика Microsoft. Привязки Loopback (`127.0.0.1` / `::1`) — это предполагаемая настройка туннеля разработки и локального обратного прокси-сервера.

## Режимы доставки команд

Конвейер поддерживает два режима доставки сводных данных Teams внутри существующего плагина Teams.

### `incoming_webhook`

Используйте это, если вам нужна простая публикация веб-перехватчика в Teams без создания сообщений канала через Graph.

Необходимая конфигурация:

```yaml
platforms:
  teams:
    enabled: true
    extra:
      delivery_mode: "incoming_webhook"
      incoming_webhook_url: "https://..."
```

### `graph`

Используйте это, если хотите, чтобы Hermes опубликовал сводку через Microsoft Graph в чат или канал Teams.

Поддерживаемые цели:
- `chat_id`
- `team_id` + `channel_id`
- `team_id` + `home_channel` резерв для существующей платформы Teams.

Пример:

```yaml
platforms:
  teams:
    enabled: true
    extra:
      delivery_mode: "graph"
      team_id: "team-id"
      channel_id: "channel-id"
```

## Шаг 4: Запустите шлюз

Запустите Hermes в обычном режиме после обновления конфигурации:

```bash
hermes gateway run
```

Или, если вы запускаете Hermes в Docker, запустите шлюз так же, как вы это уже делали для своего развертывания.

Проверьте слушателя:

```bash
curl http://localhost:8646/health
```

## Шаг 5. Создание подписок на графы

Используйте плагин CLI для создания и проверки подписок.

Примеры:

```bash
hermes teams-pipeline subscribe \
  --resource communications/onlineMeetings/getAllTranscripts \
  --notification-url https://ops.example.com/msgraph/webhook \
  --client-state "$MSGRAPH_WEBHOOK_CLIENT_STATE"

hermes teams-pipeline subscribe \
  --resource communications/onlineMeetings/getAllRecordings \
  --notification-url https://ops.example.com/msgraph/webhook \
  --client-state "$MSGRAPH_WEBHOOK_CLIENT_STATE"
```

:::warning Срок действия подписки Graph истекает через 72 часа.

Microsoft Graph ограничивает подписку на веб-перехватчик 72 часами и не продлевает ее автоматически. ВЫ ДОЛЖНЫ запланировать `hermes teams-pipeline maintain-subscriptions` перед запуском в эксплуатацию, иначе уведомления автоматически прекратятся через три дня после создания любой подписки вручную. См. [Автоматическое продление подписки](/guides/operate-teams-meeting-pipeline#automating-subscription-renewal-required-for-production) в Runbook оператора — три варианта (Hermes cron, системный таймер, простой crontab).

:::

Для обслуживания подписки и потоков операторов на второй день продолжайте работу с руководством: [Управление конвейером собраний Teams](/guides/operate-teams-meeting-pipeline).

## Проверка

Запустите встроенный снимок проверки:

```bash
hermes teams-pipeline validate
```

Полезные проверки компаньонов:

```bash
hermes teams-pipeline token-health
hermes teams-pipeline subscriptions
```

## Устранение неполадок

| Проблема | Что проверить |
|---------|---------------|
| Проверка вебхука графа не удалась | Убедитесь, что общедоступный URL-адрес правильный и доступен, а также что Graph вызывает точный путь `/msgraph/webhook` |
| Вакансии не появляются в `hermes teams-pipeline list` | Убедитесь, что `msgraph_webhook` включен и что подписки указывают на правильный URL-адрес уведомлений |
| Транскрипт-сначала никогда не приводит к успеху | Проверьте разрешения Graph для ресурсов стенограммы и наличие артефакта стенограммы для этого собрания |
| Не удалось выполнить резервную запись | Убедитесь, что `ffmpeg` установлен и приложение Graph имеет доступ к артефактам записи |
| Сбой доставки сводки Teams | Перепроверьте `delivery_mode`, целевые идентификаторы и конфигурацию аутентификации Teams |

## Сопутствующие документы

- [Настройка бота Microsoft Teams](/user-guide/messaging/teams)
- [Управление конвейером собраний команд](/guides/operate-teams-meeting-pipeline)