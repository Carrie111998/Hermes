---
title: Findmy — отслеживайте устройства Apple/AirTags через FindMy.app на macOS
sidebar_label: Findmy
description: Отслеживайте устройства Apple/AirTags через FindMy.app на macOS
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

#Найдми

Отслеживайте устройства Apple/AirTags через FindMy.app на macOS.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/apple/findmy` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | макос |
| Теги | `FindMy`, `AirTag`, `location`, `tracking`, `macOS`, `Apple` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Найди мое (Яблоко)

Отслеживайте устройства Apple и AirTags с помощью приложения FindMy.app на macOS. Поскольку Apple не
предоставить CLI для FindMy, этот навык использует AppleScript для открытия приложения и
снимок экрана для чтения местоположения устройства.

## Предварительные условия

- **macOS** с приложением «Найти мое» и входом в iCloud.
- Устройства/AirTags уже зарегистрированы в Find My
- Разрешение на запись экрана для терминала (Настройки системы → Конфиденциальность → Запись экрана).
- **Необязательно, но рекомендуется**: установите `peekaboo` для лучшей автоматизации пользовательского интерфейса:
  `brew install steipete/tap/peekaboo`

## Когда использовать

- Пользователь спрашивает: «Где мое [устройство/кошка/ключи/сумка]?»
- Отслеживание местоположения AirTag
- Проверка местоположения устройства (iPhone, iPad, Mac, AirPods)
- Мониторинг перемещения домашних животных или предметов с течением времени (маршруты патрулирования AirTag)

## Способ 1: AppleScript + снимок экрана (базовый)

### Откройте FindMy и найдите

```bash
# Open Find My app
osascript -e 'tell application "FindMy" to activate'

# Wait for it to load
sleep 3

# Take a screenshot of the Find My window
screencapture -w -o /tmp/findmy.png
```

Затем используйте `vision_analyze`, чтобы прочитать скриншот:
```
vision_analyze(image_url="/tmp/findmy.png", question="What devices/items are shown and what are their locations?")
```

### Переключение между вкладками

```bash
# Switch to Devices tab
osascript -e '
tell application "System Events"
    tell process "FindMy"
        click button "Devices" of toolbar 1 of window 1
    end tell
end tell'

# Switch to Items tab (AirTags)
osascript -e '
tell application "System Events"
    tell process "FindMy"
        click button "Items" of toolbar 1 of window 1
    end tell
end tell'
```

## Способ 2: автоматизация пользовательского интерфейса Peekaboo (рекомендуется)

Если установлен `peekaboo`, используйте его для более надежного взаимодействия с пользовательским интерфейсом:

```bash
# Open Find My
osascript -e 'tell application "FindMy" to activate'
sleep 3

# Capture and annotate the UI
peekaboo see --app "FindMy" --annotate --path /tmp/findmy-ui.png

# Click on a specific device/item by element ID
peekaboo click --on B3 --app "FindMy"

# Capture the detail view
peekaboo image --app "FindMy" --path /tmp/findmy-detail.png
```

Затем проанализируйте зрительно:
```
vision_analyze(image_url="/tmp/findmy-detail.png", question="What is the location shown for this device/item? Include address and coordinates if visible.")
```

## Рабочий процесс: отслеживание местоположения AirTag с течением времени

Для мониторинга AirTag (например, отслеживания маршрута кошачьего патруля):

```bash
# 1. Open FindMy to Items tab
osascript -e 'tell application "FindMy" to activate'
sleep 3

# 2. Click on the AirTag item (stay on page — AirTag only updates when page is open)

# 3. Periodically capture location
while true; do
    screencapture -w -o /tmp/findmy-$(date +%H%M%S).png
    sleep 300  # Every 5 minutes
done
```

Анализируйте каждый скриншот с помощью зрения, чтобы извлечь координаты, а затем составьте маршрут.

## Ограничения

- У FindMy **нет CLI или API** — необходимо использовать автоматизацию пользовательского интерфейса.
- AirTags обновляют местоположение только тогда, когда страница FindMy активно отображается.
- Точность определения местоположения зависит от ближайших устройств Apple в сети FindMy.
- Для снимков экрана требуется разрешение на запись экрана.
- Автоматизация пользовательского интерфейса AppleScript может не работать в разных версиях macOS.

## Правила

1. Держите приложение FindMy на переднем плане при отслеживании AirTags (обновления прекращаются при сворачивании)
2. Используйте `vision_analyze` для чтения содержимого скриншота — не пытайтесь анализировать пиксели.
3. Для постоянного отслеживания используйте cronjob для периодического захвата и регистрации местоположений.
4. Соблюдайте конфиденциальность — отслеживайте только те устройства/предметы, которыми владеет пользователь.