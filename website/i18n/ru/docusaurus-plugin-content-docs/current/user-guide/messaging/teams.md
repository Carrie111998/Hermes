---
sidebar_position: 5
title: Команды Майкрософт
description: Настройте агент Hermes в качестве бота Microsoft Teams
---

# Настройка команд Microsoft

Подключите агента Hermes к Microsoft Teams в качестве бота. В отличие от режима сокетов Slack, Teams доставляет сообщения, вызывая **общедоступный веб-перехватчик HTTPS**, поэтому вашему экземпляру требуется общедоступная конечная точка — либо туннель разработки (локальная разработка), либо реальный домен (производственная версия).

Вам нужны сводки совещаний по мероприятиям Microsoft Graph, а не обычные беседы с ботами? Используйте специальную страницу настройки: [Собрания Teams](/user-guide/messaging/teams-meetings).

> Запустите `hermes gateway setup` и выберите **Microsoft Teams** для пошагового руководства.

## Как реагирует бот

| Контекст | Поведение |
|---------|----------|
| **Личный чат (DM)** | Бот отвечает на каждое сообщение. Никакого @упоминания не требуется. |
| **Групповой чат** | Бот отвечает только тогда, когда @упоминается. |
| **Канал** | Бот отвечает только тогда, когда @упоминается. |

Teams доставляет @упоминания как обычные сообщения с тегами `<at>BotName</at>`, которые Hermes автоматически удаляет перед обработкой.

---

Для исходной или локальной установки включите дополнительный модуль Teams, чтобы прилагаемый адаптер мог
импортируйте SDK Microsoft Teams:

```bash
uv sync --extra teams
# or, for editable installs:
uv pip install -e ".[teams]"
```

## Шаг 1. Установите интерфейс командной строки Teams

`@microsoft/teams.cli` автоматизирует регистрацию ботов — портал Azure не требуется.

```bash
npm install -g @microsoft/teams.cli@preview
teams login
```

Чтобы подтвердить свой логин и найти собственный идентификатор объекта AAD (необходим для `TEAMS_ALLOWED_USERS`):

```bash
teams status --verbose
```

---

## Шаг 2. Откройте доступ к порту веб-перехватчика

Команды не могут доставлять сообщения на `localhost`. Для локальной разработки используйте любой инструмент туннелирования, чтобы получить общедоступный URL-адрес HTTPS. Порт по умолчанию — `3978`. При необходимости измените его на `TEAMS_PORT`.

```bash
# devtunnel (Microsoft)
devtunnel create hermes-bot --allow-anonymous
devtunnel port create hermes-bot -p 3978 --protocol http  # replace 3978 with TEAMS_PORT if changed
devtunnel host hermes-bot

# ngrok
ngrok http 3978  # replace 3978 with TEAMS_PORT if changed

# cloudflared
cloudflared tunnel --url http://localhost:3978  # replace 3978 with TEAMS_PORT if changed
```

Скопируйте URL-адрес `https://` из выходных данных — он понадобится вам на следующем шаге. Оставьте туннель работающим во время разработки.

URL-адрес общедоступного туннеля использует HTTPS, но локальный прослушиватель веб-перехватчика Hermes использует обычный HTTP. Туннель завершает TLS и перенаправляет HTTP на порт `3978`; не настраивайте порт локального туннеля как HTTPS.

Для производства вместо этого укажите конечную точку вашего бота в общедоступном домене вашего сервера (см. [Производственное развертывание](#production-deployment)).

---

## Шаг 3: Создайте бота

```bash
teams app create \
  --name "Hermes" \
  --endpoint "https://<your-tunnel-url>/api/messages"
```

Интерфейс командной строки выводит ваши `CLIENT_ID`, `CLIENT_SECRET` и `TENANT_ID`, а также ссылку для установки для шага 6. Сохраните секрет клиента — он больше не будет отображаться.

---

## Шаг 4. Настройка переменных среды

Добавьте в `~/.hermes/.env`:

```bash
# Required
TEAMS_CLIENT_ID=<your-client-id>
TEAMS_CLIENT_SECRET=<your-client-secret>
TEAMS_TENANT_ID=<your-tenant-id>

# Restrict access to specific users (recommended)
# Use AAD object IDs from `teams status --verbose`
TEAMS_ALLOWED_USERS=<your-aad-object-id>
```

---

## Шаг 5: Запустите шлюз

**Docker** (должен запускаться из каталога, содержащего `docker-compose.yml` — обычно это клонированный репозиторий `hermes-agent`, а не `~`):

```bash
cd /path/to/hermes-agent
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d gateway
```

**Встроенная установка/systemd** (типичный однострочный установщик `hermes` под `~/.hermes/hermes-agent`):

```bash
hermes gateway restart
# or foreground: hermes gateway run
```

Teams SDK не является обязательным; когда Teams включен, шлюз лениво устанавливает его в собственный venv Hermes при первом запуске (**не** используйте систему `pip install` в Ubuntu 24.04 — это соответствует PEP 668 `externally-managed-environment`). Для установки вручную в Hermes venv:

```bash
~/.hermes/hermes-agent/venv/bin/pip install microsoft-teams-apps aiohttp
# or from a clone of the agent: uv sync --extra teams
```

Порт веб-перехватчика по умолчанию — `3978` (переопределить с помощью `TEAMS_PORT`). Проверьте, что он работает:

```bash
curl http://localhost:3978/health   # should return: ok
# Docker:
docker logs -f hermes
# Native:
hermes gateway status -l
```

Ищите:
```
[teams] Webhook server listening on * (all interfaces, IPv4+IPv6):3978/api/messages
```

---

## Шаг 6. Установите приложение в Teams

```bash
teams app get <teamsAppId> --install-link
```

Откройте распечатанную ссылку в браузере — она открывается прямо в клиенте Teams. После установки отправьте сообщение в директ своему боту — всё готово.

---

## Справочник по конфигурации

### Переменные среды

| Переменная | Описание |
|----------|-------------|
| `TEAMS_CLIENT_ID` | Идентификатор приложения (клиента) Azure AD |
| `TEAMS_CLIENT_SECRET` | Секрет клиента Azure AD |
| `TEAMS_TENANT_ID` | Идентификатор клиента Azure AD |
| `TEAMS_ALLOWED_USERS` | Идентификаторы объектов AAD, разделенные запятыми, разрешены для использования бота |
| `TEAMS_ALLOW_ALL_USERS` | Установите `true`, чтобы пропустить белый список и разрешить всем |
| `TEAMS_HOME_CHANNEL` | Идентификатор диалога для cron/проактивной доставки сообщений |
| `TEAMS_HOME_CHANNEL_NAME` | Отображаемое имя домашнего канала |
| `TEAMS_PORT` | Порт вебхука (по умолчанию: `3978`) |

### конфиг.yaml

Альтернативно настройте через `~/.hermes/config.yaml`:

```yaml
platforms:
  teams:
    enabled: true
    extra:
      client_id: "your-client-id"
      client_secret: "your-secret"
      tenant_id: "your-tenant-id"
      port: 3978
```

---

## Особенности

### Интерактивные карточки одобрения

Когда агенту необходимо выполнить потенциально опасную команду, он отправляет адаптивную карточку с четырьмя кнопками вместо того, чтобы просить вас ввести `/approve`:

- **Разрешить один раз** — одобрить данную конкретную команду.
- **Разрешить сеанс** — утвердить этот шаблон для оставшейся части сеанса.
- **Всегда разрешать** — навсегда утвердить этот шаблон.
- **Запретить** — отклонить команду

Нажатие кнопки разрешает одобрение в режиме онлайн и заменяет карточку с решением.

### Доставка сводки собрания (конвейер собраний Teams)

Если [плагин конвейера собраний Teams](/user-guide/messaging/msgraph-webhook) включен, этот адаптер также обрабатывает исходящую доставку сводок собраний — одну поверхность интеграции Teams, а не две. После суммирования стенограммы собрания автор публикует сводку в выбранную вами цель Teams.

Доставка сводных данных по конвейеру настраивается в записи платформы `teams` вместе с конфигурацией бота:

```yaml
platforms:
  teams:
    enabled: true
    extra:
      # existing bot config (client_id, client_secret, tenant_id, port) ...

      # Meeting summary delivery (only used when the teams_pipeline plugin is enabled)
      delivery_mode: "graph"       # or "incoming_webhook"
      # For delivery_mode: graph — pick ONE of:
      chat_id: "19:meeting_..."    # post into a Teams chat
      # team_id: "..."             # OR post into a channel
      # channel_id: "..."
      # access_token: "..."        # optional; falls back to MSGRAPH_* app credentials
      # For delivery_mode: incoming_webhook:
      # incoming_webhook_url: "https://outlook.office.com/webhook/..."
```

| Режим | Используйте, когда | Компромисс |
|------|----------|-----------|
| `incoming_webhook` | Просто «опубликовать сводку на этом канале» со статическим URL-адресом, созданным Teams. | Ни цепочки ответов, ни реакций — все это отображается как настроенная личность веб-перехватчика. |
| `graph` | Сообщения в цепочке каналов или сообщения в групповом чате один на один под именем бота через Microsoft Graph. | Требуется [регистрация приложения Graph](/guides/microsoft-graph-app-registration) с разрешениями приложения `ChannelMessage.Send` (канал) или `Chat.ReadWrite.All` (чат). |

Если плагин `teams_pipeline` **не** включен, эти настройки неактивны — они подключаются только тогда, когда среда выполнения конвейера привязывается к входу веб-перехватчика Graph.

---

## Производственное развертывание

Для постоянного сервера завершите TLS на обратном прокси-сервере и перенаправьте запросы на простой прослушиватель HTTP Hermes, обычно `http://127.0.0.1:3978`. Зарегистрируйте общедоступную конечную точку HTTPS прокси-сервера в Teams:

```bash
teams app create \
  --name "Hermes" \
  --endpoint "https://your-domain.com/api/messages"
```

Если вы уже создали бота и вам просто нужно обновить конечную точку:

```bash
teams app update --id <teamsAppId> --endpoint "https://your-domain.com/api/messages"
```

Убедитесь, что общедоступная конечная точка HTTPS доступна из Интернета и использует действительный сертификат TLS. Teams отклоняет самозаверяющие сертификаты. Держите прослушиватель Hermes за прокси; порт `3978` сам по себе не обслуживает HTTPS.

---

## Поиск неисправностей

| Проблема | Решение |
|---------|----------|
| `Can't find a suitable configuration file` из `docker compose` | Вы не находитесь в репозитории с `docker-compose.yml` или используете собственную установку — вместо этого используйте `hermes gateway restart` или сначала `cd` в клоне |
| `requirements not met` / `Teams SDK missing` / `No adapter available for teams` | Перезапустите шлюз, чтобы можно было запустить отложенную установку, или установите его в **Hermes venv**: `~/.hermes/hermes-agent/venv/bin/pip install microsoft-teams-apps aiohttp`. Система `pip` дает сбой в Ubuntu 24.04 (PEP 668) и в любом случае не повлияет на службу |
| `health` конечная точка работает, но бот не отвечает | Убедитесь, что ваш туннель все еще работает и конечная точка обмена сообщениями бота соответствует URL-адресу туннеля |
| В журналах отображается `"UNKNOWN / HTTP/1.0" 400`, когда Teams отправляет сообщение | Туннель или обратный прокси-сервер перенаправляет HTTPS на простой HTTP-прослушиватель Hermes. Завершить TLS на прокси-сервере и перенаправить HTTP на порт `3978` |
| `KeyError: 'teams'` в журналах | Перезапустите контейнер — в текущей версии это исправлено |
| Бот отвечает ошибками авторизации | Убедитесь, что `TEAMS_CLIENT_ID`, `TEAMS_CLIENT_SECRET` и `TEAMS_TENANT_ID` установлены правильно |
| `No inference provider configured` | Убедитесь, что `ANTHROPIC_API_KEY` (или другой ключ провайдера) установлен в `~/.hermes/.env` |
| Бот получает сообщения, но игнорирует их | Идентификатор вашего объекта AAD может отличаться от `TEAMS_ALLOWED_USERS`. Запустите `teams status --verbose`, чтобы найти его |
| URL-адрес туннеля изменяется при перезапуске | URL-адреса devtunnel являются постоянными, если вы используете именованный туннель (`devtunnel create hermes-bot`). ngrok и cloudflared генерируют новый URL-адрес при каждом запуске, если у вас нет платного плана — обновляйте конечную точку бота с помощью `teams app update` при ее изменении |
| Команды показывают «Этот бот не отвечает» | Вебхук вернул ошибку. Проверьте `docker logs hermes`/`hermes gateway status -l` на наличие обратных трассировок |
| `[teams] Failed to connect` в журналах | SDK не прошел аутентификацию. Дважды проверьте свои учетные данные и убедитесь, что идентификатор клиента соответствует учетной записи, которую вы использовали в `teams login` |

---

## Безопасность

:::предупреждение
**Всегда задавайте `TEAMS_ALLOWED_USERS`** с идентификаторами объектов AAD авторизованных пользователей. Без этого с ним сможет взаимодействовать любой, кто сможет найти или установить вашего бота.

Относитесь к `TEAMS_CLIENT_SECRET` как к паролю — периодически меняйте его через портал Azure или интерфейс командной строки Teams.
:::

- Хранить учетные данные в `~/.hermes/.env` с разрешениями `600` (`chmod 600 ~/.hermes/.env`).
- Бот принимает сообщения только от пользователей `TEAMS_ALLOWED_USERS`; несанкционированные сообщения молча удаляются
- Ваша общедоступная конечная точка (`/api/messages`) аутентифицируется с помощью Teams Bot Framework — запросы без действительных JWT отклоняются.

## Сопутствующие документы

- [Встречи команд](/user-guide/messaging/teams-meetings)
- [Управление конвейером собраний команд](/guides/operate-teams-meeting-pipeline)