---
title: Openhue — управляйте освещением, сценами и комнатами Philips Hue через OpenHue
  CLI.
sidebar_label: Openhue
description: Control Philips Hue lights, scenes, rooms via OpenHue CLI
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Опенхью

Управляйте освещением, сценами и комнатами Philips Hue через интерфейс командной строки OpenHue.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/smart-home/openhue` |
| Версия | `1.0.1` |
| Автор | сообщество |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Smart-Home`, `Hue`, `Lights`, `IoT`, `Automation` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Интерфейс командной строки OpenHue

Управляйте освещением и сценами Philips Hue с помощью Hue Bridge с терминала.

## Предварительные условия

```bash
# Linux (pre-built binary — releases ship tarballs, not bare binaries)
curl -sL "https://github.com/openhue/openhue-cli/releases/latest/download/openhue_Linux_x86_64.tar.gz" \
  | tar -xz -C /tmp openhue \
  && install -m 0755 /tmp/openhue ~/.local/bin/openhue
# (use openhue_Linux_arm64.tar.gz on ARM64)

# macOS
brew install openhue/cli/openhue-cli
```

Для первого запуска необходимо нажать кнопку на Hue Bridge для сопряжения. Мост должен находиться в той же локальной сети.

## Когда использовать

- «Включить/выключить свет»
- «Приглуши свет в гостиной»
- «Установить сцену» или «режим фильма»
- Управление конкретными комнатами, зонами или отдельными лампочками Hue.
- Регулировка яркости, цвета или цветовой температуры

## Общие команды

### Список ресурсов

```bash
openhue get light       # List all lights
openhue get room        # List all rooms
openhue get scene       # List all scenes
```

### Контрольные лампы

```bash
# Turn on/off
openhue set light "Bedroom Lamp" --on
openhue set light "Bedroom Lamp" --off

# Brightness (0-100)
openhue set light "Bedroom Lamp" --on --brightness 50

# Color temperature (warm to cool: 153-500 mirek)
openhue set light "Bedroom Lamp" --on --temperature 300

# Color (by name or hex)
openhue set light "Bedroom Lamp" --on --color red
openhue set light "Bedroom Lamp" --on --rgb "#FF5500"
```

### Диспетчерские

```bash
# Turn off entire room
openhue set room "Bedroom" --off

# Set room brightness
openhue set room "Bedroom" --on --brightness 30
```

### Сцены

```bash
openhue set scene "Relax" --room "Bedroom"
openhue set scene "Concentrate" --room "Office"
```

## Быстрые пресеты

```bash
# Bedtime (dim warm)
openhue set room "Bedroom" --on --brightness 20 --temperature 450

# Work mode (bright cool)
openhue set room "Office" --on --brightness 100 --temperature 250

# Movie mode (dim)
openhue set room "Living Room" --on --brightness 10

# Everything off
openhue set room "Bedroom" --off
openhue set room "Office" --off
openhue set room "Living Room" --off
```

## Примечания

- Мост должен находиться в той же локальной сети, что и машина, на которой работает Hermes.
- Для первого запуска требуется физическое нажатие кнопки на Hue Bridge для авторизации.
- Цвета работают только с цветными лампами (не только с белыми моделями)
- Названия источников света и комнат чувствительны к регистру — используйте `openhue get light`, чтобы проверить точные названия.
- Отлично работает с заданиями cron для запланированного освещения (например, тусклое перед сном, яркое при пробуждении)