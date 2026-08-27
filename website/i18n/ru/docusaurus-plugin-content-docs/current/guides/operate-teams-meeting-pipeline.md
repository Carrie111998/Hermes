---
title: Управление конвейером собраний Teams
description: Runbook, контрольный список запуска и рабочий лист оператора для конвейера
  собраний Microsoft Teams
---

# Управление конвейером собраний команд

Используйте это руководство после того, как вы уже включили эту функцию в [Собрания Teams](/user-guide/messaging/teams-meetings).

На этой странице представлены:
- потоки CLI оператора
- регулярное обслуживание подписки
- сортировка отказов
- оперативные проверки
- рабочий лист развертывания

## Основные команды оператора

### Проверка снимка конфигурации

```bash
hermes teams-pipeline validate
```

Используйте это в первую очередь после любого изменения конфигурации.

### Проверка работоспособности токена

```bash
hermes teams-pipeline token-health
hermes teams-pipeline token-health --force-refresh
```

Используйте `--force-refresh`, если вы подозреваете, что состояние аутентификации устарело.

### Проверка подписок

```bash
hermes teams-pipeline subscriptions
```

### Продлите подписки с истекающим сроком действия

```bash
hermes teams-pipeline maintain-subscriptions
hermes teams-pipeline maintain-subscriptions --dry-run
```

### Автоматическое продление подписки (ТРЕБУЕТСЯ для рабочей версии)

** Срок действия подписки Microsoft Graph истекает не более чем через 72 часа. ** Если ничего не продлевается, уведомления о собраниях автоматически прекращаются через 3 дня, и конвейер выглядит «сломанным». Это режим эксплуатационного сбоя №1 для любой интеграции с поддержкой Graph.

Вы ДОЛЖНЫ запускать `maintain-subscriptions` по расписанию. Выберите один из этих трех вариантов:

#### Вариант 1: Hermes cron (рекомендуется, если у вас уже запущен шлюз Hermes)

Hermes предлагает встроенный планировщик cron. В режиме `--no-agent` в качестве задания выполняется сценарий (вместо использования LLM), а `--script` должен указывать на файл под `~/.hermes/scripts/`. Сначала создайте скрипт:

```bash
mkdir -p ~/.hermes/scripts
cat > ~/.hermes/scripts/maintain-teams-subscriptions.sh <<'EOF'
#!/usr/bin/env bash
exec hermes teams-pipeline maintain-subscriptions
EOF
chmod +x ~/.hermes/scripts/maintain-teams-subscriptions.sh
```

Затем зарегистрируйте задание cron только для сценариев, которое запускается каждые 12 часов (даёт 6-кратный запас по сравнению с окном истечения 72 часа):

```bash
hermes cron create "0 */12 * * *" \
  --name "teams-pipeline-maintain-subscriptions" \
  --no-agent \
  --script maintain-teams-subscriptions.sh \
  --deliver local
```

Убедитесь, что он зарегистрирован, и проверьте время следующего выполнения:

```bash
hermes cron list
hermes cron status        # scheduler status
```

#### Вариант 2: таймер systemd (рекомендуется для производственных развертываний Linux)

Создайте `/etc/systemd/system/hermes-teams-pipeline-maintain.service`:

```ini
[Unit]
Description=Hermes Teams pipeline subscription maintenance
After=network-online.target

[Service]
Type=oneshot
User=hermes
EnvironmentFile=/etc/hermes/env
ExecStart=/usr/local/bin/hermes teams-pipeline maintain-subscriptions
```

И `/etc/systemd/system/hermes-teams-pipeline-maintain.timer`:

```ini
[Unit]
Description=Run Hermes Teams pipeline subscription maintenance every 12 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=12h
Persistent=true

[Install]
WantedBy=timers.target
```

Включить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-teams-pipeline-maintain.timer
systemctl list-timers hermes-teams-pipeline-maintain.timer
```

#### Вариант 3: обычный кронтаб

```cron
0 */12 * * * /usr/local/bin/hermes teams-pipeline maintain-subscriptions >> /var/log/hermes/teams-pipeline-maintain.log 2>&1
```

Убедитесь, что среда cron имеет учетные данные `MSGRAPH_*`. Самое простое исправление: источник `~/.hermes/.env` в верхней части сценария-оболочки, который вызывает crontab.

#### Проверка работоспособности продления

После настройки расписания проверьте активность продления после первого запланированного запуска:

```bash
hermes teams-pipeline subscriptions   # should show expirationDateTime advanced
hermes teams-pipeline maintain-subscriptions --dry-run   # should show "0 expiring soon" most of the time
```

Если вы когда-нибудь увидите, что ваш веб-хук Graph загадочным образом «перестает работать» примерно через 72 часа, это первое, что нужно проверить: действительно ли задание обновления выполнялось?

### Проверяйте последние вакансии

```bash
hermes teams-pipeline list
hermes teams-pipeline list --status failed
hermes teams-pipeline show <job-id>
```

### Воспроизведение сохраненного задания

```bash
hermes teams-pipeline run <job-id>
```

### Извлечение артефактов собрания в пробном режиме

```bash
hermes teams-pipeline fetch --meeting-id <meeting-id>
hermes teams-pipeline fetch --join-web-url "<join-url>"
hermes teams-pipeline fetch --join-web-url "<join-url>" --organizer-user-id <entra-user-id>
```

Передайте `--organizer-user-id` (идентификатор пользователя Microsoft Entra организатора), чтобы решить проблему.
через путь к графу `/users/{id}/onlineMeetings` в области организатора. Это
требуется для коротких URL-адресов Teams `/meet/`, которые Graph отклоняет на
`/communications/onlineMeetings` конечная точка. Задания, управляемые веб-перехватчиками, получают
органайзер автоматически из уведомления `@odata.id`.

## Рутинный блокнот

### После первой настройки

Запустите их по порядку:

```bash
hermes teams-pipeline validate
hermes teams-pipeline token-health --force-refresh
hermes teams-pipeline subscriptions
```

Затем запустите или дождитесь реального события встречи и подтвердите:

```bash
hermes teams-pipeline list
hermes teams-pipeline show <job-id>
```

### Ежедневные или периодические проверки

- запустить `hermes teams-pipeline maintain-subscriptions --dry-run`
- осмотреть `hermes teams-pipeline list --status failed`
- убедитесь, что целью доставки Teams по-прежнему является правильный чат или канал.

### Перед изменением URL-адресов веб-перехватчиков или целей доставки

- обновить URL-адрес общедоступного уведомления или целевую конфигурацию Teams.
- запустить `hermes teams-pipeline validate`
- продлить или воссоздать затронутые подписки
- подтвердить, что новые события попадают в ожидаемый сток

## Сортировка сбоев

### Рабочие места не создаются

Проверьте:
- `msgraph_webhook` включен
- URL-адрес общедоступного уведомления указывает на `/msgraph/webhook`.
- состояние клиента в подписке соответствует `MSGRAPH_WEBHOOK_CLIENT_STATE`
- подписки все еще существуют удаленно и не истекли

### Задания продолжают повторяться или завершаются неудачей до суммирования

Проверьте:
- разрешения и доступность транскрипции
- разрешения на запись и наличие артефактов
- Доступность `ffmpeg`, если включена резервная запись.
- Состояние токена графика

### Сводки создаются, но не доставляются в Teams

Проверьте:
- `platforms.teams.enabled: true`
- `delivery_mode`
- `incoming_webhook_url` для режима веб-перехватчика
- `chat_id` или `team_id` плюс `channel_id` для режима графика.
— Конфигурация аутентификации Teams, если используется публикация графика.

### Дублирующиеся или неожиданные повторы

Проверьте:
- переиграли ли вы задание вручную с `hermes teams-pipeline run`
- существует ли уже запись приемника для этой встречи
- намеренно ли вы включили путь повторной отправки в локальной конфигурации

## Контрольный список запуска

- [ ] Учетные данные графика присутствуют и верны
- [ ] `msgraph_webhook` включен и доступен из общедоступного Интернета.
- [ ] `MSGRAPH_WEBHOOK_CLIENT_STATE` установлен и соответствует подпискам
- [ ] создана подписка на стенограмму
- [ ] подписка на запись создается, если требуется откат STT
- [ ] `ffmpeg` устанавливается, если включена резервная запись
- [ ] Цель исходящей доставки Teams настроена и проверена.
- [ ] Приемники Notion и Linear настраиваются только в том случае, если это действительно необходимо.
- [ ] `hermes teams-pipeline validate` возвращает снимок состояния «ОК».
- [ ] `hermes teams-pipeline token-health --force-refresh` успешно
- [ ] **`maintain-subscriptions` запланирован** (Hermes cron, системный таймер или crontab — см. [Автоматическое продление подписки](#automating-subscription-renewal-required-for-production)). Без этого срок действия подписки Graph истекает автоматически в течение 72 часов.
- [ ] реальное сквозное собрание создало сохраненное задание
- [ ] хотя бы одно резюме дошло до назначенного получателя доставки

## Руководство по принятию решений в режиме доставки

| Режим | Используйте, когда | Компромисс |
|------|----------|----------|
| `incoming_webhook` | вам нужна только простая публикация в Teams | простейшая настройка, меньше контроля |
| `graph` | вам нужно публиковать сообщения в канале или чате через Graph | больше контроля, больше аутентификации и целевой конфигурации |

## Рабочий лист оператора

Заполните это перед развертыванием:

| Товар | Значение |
|------|-------|
| URL-адрес публичного уведомления | |
| Идентификатор клиента графа | |
| Идентификатор клиента графика | |
| Состояние клиента Webhook | |
| Подписка на ресурсы транскрипции | |
| Подписка на ресурс записи | |
| Режим доставки команд | |
| Идентификатор чата Teams или команда/канал | |
| Идентификатор базы данных Notion | |
| Линейный идентификатор команды | |
| Переопределение пути к хранилищу, если таковое имеется | |
| Владелец для ежедневных проверок | |

## Лист обзора изменений

Используйте это перед изменением развертывания:

| Вопрос | Ответ |
|----------|--------|
| Изменяем ли мы общедоступный URL-адрес веб-перехватчика? | |
| Меняем ли мы учетные данные Graph? | |
| Изменяем ли мы режим доставки Teams? | |
| Переезжаем ли мы на новый чат или канал Teams? | |
| Нужно ли заново создавать или продлевать подписки? | |
| Нужен ли нам новый прогон сквозной проверки? | |

## Сопутствующие документы

- [Настройка собраний Teams](/user-guide/messaging/teams-meetings)
- [Настройка бота Microsoft Teams](/user-guide/messaging/teams)