---
title: Домашний помощник
description: Управляйте своим умным домом с помощью Hermes Agent через интеграцию
  Home Assistant.
sidebar_label: Home Assistant
sidebar_position: 5
---

# Интеграция домашнего помощника

Агент Hermes интегрируется с [Home Assistant](https://www.home-assistant.io/) двумя способами:

1. **Платформа шлюза** — подписывается на изменения состояния в реальном времени через WebSocket и реагирует на события.
2. **Инструменты «умного дома»** — четыре вызываемых из LLM инструмента для запроса и управления устройствами через REST API.

## Настройка

### 1. Создайте долгосрочный токен доступа

1. Откройте экземпляр Home Assistant.
2. Перейдите в свой **Профиль** (нажмите свое имя на боковой панели).
3. Прокрутите до пункта **Долгосрочные токены доступа**.
4. Нажмите **Создать токен**, присвойте ему имя, например «Агент Гермеса».
5. Скопируйте токен

### 2. Настройка переменных среды

```bash
# Add to ~/.hermes/.env

# Required: your Long-Lived Access Token
HASS_TOKEN=your-long-lived-access-token

# Optional: HA URL (default: http://homeassistant.local:8123)
HASS_URL=http://192.168.1.100:8123
```

:::информация
Группа инструментов `homeassistant` автоматически включается при установке `HASS_TOKEN`. И платформа шлюза, и инструменты управления устройствами активируются с помощью этого одного токена.
:::

### 3. Запустите шлюз

```bash
hermes gateway
```

Home Assistant появится как подключенная платформа наряду с любыми другими платформами обмена сообщениями (Telegram, Discord и т. д.).

## Доступные инструменты

Агент Hermes регистрирует четыре инструмента для управления умным домом:

### `ha_list_entities`

Перечислите объекты Home Assistant, при необходимости отфильтрованные по домену или региону.

**Параметры:**
- `domain` *(необязательно)* — фильтрация по домену объекта: `light`, `switch`, `climate`, `sensor`, `binary_sensor`, `cover`, `fan`, `media_player` и т. д.
- `area` *(необязательно)* — фильтрация по названию области/комнаты (совпадает с понятными именами): `living room`, `kitchen`, `bedroom` и т. д.

**Пример:**
```
List all lights in the living room
```

Возвращает идентификаторы объектов, состояния и понятные имена.

### `ha_get_state`

Получите подробное состояние одного объекта, включая все атрибуты (яркость, цвет, заданное значение температуры, показания датчиков и т. д.).

**Параметры:**
- `entity_id` *(обязательно)* — объект для запроса, например `light.living_room`, `climate.thermostat`, `sensor.temperature`.

**Пример:**
```
What's the current state of climate.thermostat?
```

Возвращает: состояние, все атрибуты, временные метки последнего изменения/обновления.

### `ha_list_services`

Список доступных сервисов (действий) для управления устройством. Показывает, какие действия можно выполнять над каждым типом устройств и какие параметры они принимают.

**Параметры:**
- `domain` *(необязательно)* — фильтровать по домену, например `light`, `climate`, `switch`.

**Пример:**
```
What services are available for climate devices?
```

### `ha_call_service`

Вызовите службу Home Assistant, чтобы управлять устройством.

**Параметры:**
- `domain` *(обязательно)* — Домен службы: `light`, `switch`, `climate`, `cover`, `media_player`, `fan`, `scene`, `script`
- `service` *(обязательно)* — Имя службы: `turn_on`, `turn_off`, `toggle`, `set_temperature`, `set_hvac_mode`, `open_cover`, `close_cover`, `set_volume_level`
- `entity_id` *(необязательно)* — целевой объект, например `light.living_room`
- `data` *(необязательно)* — Дополнительные параметры в виде объекта JSON.

**Примеры:**

```
Turn on the living room lights
→ ha_call_service(domain="light", service="turn_on", entity_id="light.living_room")
```

```
Set the thermostat to 22 degrees in heat mode
→ ha_call_service(domain="climate", service="set_temperature",
    entity_id="climate.thermostat", data={"temperature": 22, "hvac_mode": "heat"})
```

```
Set living room lights to blue at 50% brightness
→ ha_call_service(domain="light", service="turn_on",
    entity_id="light.living_room", data={"brightness": 128, "color_name": "blue"})
```

## Платформа шлюза: события в реальном времени

Адаптер шлюза Home Assistant подключается через WebSocket и подписывается на события `state_changed`. Когда состояние устройства изменяется и соответствует вашим фильтрам, оно пересылается агенту в виде сообщения.

### Фильтрация событий

:::предупреждение Необходимая конфигурация
По умолчанию **события не пересылаются**. Для получения событий необходимо настроить хотя бы один из `watch_domains`, `watch_entities` или `watch_all`. Без фильтров при запуске регистрируется предупреждение, и все изменения состояния автоматически удаляются.
:::

Настройте, какие события агент будет видеть в `~/.hermes/config.yaml` в разделе `extra` платформы Home Assistant:

```yaml
platforms:
  homeassistant:
    enabled: true
    extra:
      watch_domains:
        - climate
        - binary_sensor
        - alarm_control_panel
        - light
      watch_entities:
        - sensor.front_door_battery
      ignore_entities:
        - sensor.uptime
        - sensor.cpu_usage
        - sensor.memory_usage
      cooldown_seconds: 30
```

| Настройка | По умолчанию | Описание |
|---------|---------|-------------|
| `watch_domains` | *(нет)* | Просматривайте только эти домены объектов (например, `climate`, `light`, `binary_sensor`) |
| `watch_entities` | *(нет)* | Смотрите только эти конкретные идентификаторы объектов |
| `watch_all` | `false` | Установите значение `true`, чтобы получать **все** изменения состояния (не рекомендуется для большинства настроек) |
| `ignore_entities` | *(нет)* | Всегда игнорировать эти объекты (применяются перед фильтрами домена/сущностей) |
| `cooldown_seconds` | `30` | Минимальное количество секунд между событиями для одного и того же объекта |

:::совет
Начните с целевого набора доменов: `climate`, `binary_sensor` и `alarm_control_panel` охватывают наиболее полезные средства автоматизации. Добавьте больше по мере необходимости. Используйте `ignore_entities` для подавления шумов датчиков, таких как температура процессора или счетчики времени безотказной работы.
:::

### Форматирование событий

Изменения состояния форматируются как удобочитаемые сообщения на основе домена:

| Домен | Формат |
|--------|--------|
| `climate` | «Режим HVAC изменен с «выключено» на «нагрев» (текущий: 21, целевой: 23)» |
| `sensor` | «изменилась с 21°C на 22°C» |
| `binary_sensor` | «сработало» / «очистилось» |
| `light`, `switch`, `fan` | "включил"/"выключил" |
| `alarm_control_panel` | «Состояние тревоги изменено с «armed_away» на «сработало»» |
| *(другое)* | «изменился со старого на новый» |

### Ответы агента

Исходящие сообщения от агента доставляются в виде **постоянных уведомлений Home Assistant** (через `persistent_notification.create`). Они появляются на панели уведомлений HA под заголовком «Агент Гермеса».

### Управление соединениями

- **WebSocket** с 30-секундным контрольным сигналом для событий в реальном времени.
- **Автоматическое переподключение** с отсрочкой: 5 с → 10 с → 30 с → 60 с.
- **REST API** для исходящих уведомлений (отдельный сеанс во избежание конфликтов WebSocket).
- **Авторизация** – события высокой доступности всегда авторизуются (список разрешенных пользователей не требуется, поскольку `HASS_TOKEN` аутентифицирует соединение).

## Безопасность

Инструменты Home Assistant обеспечивают соблюдение ограничений безопасности:

:::предупреждение о заблокированных доменах
Следующие домены служб **заблокированы**, чтобы предотвратить выполнение произвольного кода на узле высокой доступности:

- `shell_command` — произвольные команды оболочки
- `command_line` — датчики/переключатели, выполняющие команды
- `python_script` — выполнение скрипта Python.
- `pyscript` — более широкая интеграция сценариев.
- `hassio` — управление модулями, выключение/перезагрузка хоста
- `rest_command` — HTTP-запросы от HA-сервера (вектор SSRF)

Попытка вызвать службы в этих доменах возвращает ошибку.
:::

Идентификаторы объектов проверяются по шаблону `^[a-z_][a-z0-9_]*\.[a-z0-9_]+$` для предотвращения атак путем внедрения.

## Примеры автоматизации

### Утренняя рутина

```
User: Start my morning routine

Agent:
1. ha_call_service(domain="light", service="turn_on",
     entity_id="light.bedroom", data={"brightness": 128})
2. ha_call_service(domain="climate", service="set_temperature",
     entity_id="climate.thermostat", data={"temperature": 22})
3. ha_call_service(domain="media_player", service="turn_on",
     entity_id="media_player.kitchen_speaker")
```

### Проверка безопасности

```
User: Is the house secure?

Agent:
1. ha_list_entities(domain="binary_sensor")
     → checks door/window sensors
2. ha_get_state(entity_id="alarm_control_panel.home")
     → checks alarm status
3. ha_list_entities(domain="lock")
     → checks lock states
4. Reports: "All doors closed, alarm is armed_away, all locks engaged."
```

### Реактивная автоматизация (через события шлюза)

При подключении в качестве шлюзовой платформы агент может реагировать на события:

```
[Home Assistant] Front Door: triggered (was cleared)

Agent automatically:
1. ha_get_state(entity_id="binary_sensor.front_door")
2. ha_call_service(domain="light", service="turn_on",
     entity_id="light.hallway")
3. Sends notification: "Front door opened. Hallway lights turned on."
```

## Устранение неполадок

**Переменные среды не выбраны.**
Адаптер считывает учетные данные из `~/.hermes/.env` (автоматически объединяется при запуске) или
с `config.yaml`. Дважды проверьте, что файл находится под активным профилем Hermes.
home и чтобы вокруг URL/токена не было случайных кавычек. Перезапустите шлюз
после редактирования — изменения env применяются только при запуске процесса.


**Ошибка аутентификации REST (`401 Unauthorized`).**
Токен должен быть *долгоживущим токеном доступа*, созданным на основе вашего профиля пользователя высокой доступности.
(**Профиль → Безопасность → Долгосрочные токены доступа**). Недолговечный пользовательский интерфейс
токены сеанса не будут работать. Также убедитесь, что базовый URL-адрес содержит схему и
порт (например, `http://homeassistant.local:8123`) и доступен с хоста
работает с Гермесом — `curl -H "Authorization: Bearer <token>" <url>/api/` должен
верните `{"message": "API running."}`.