---
title: 'Ascii Art — ASCII-искусство: pyfiglet, Cowsay, Boxes, image-to-ascii'
sidebar_label: Ascii Art
description: 'ASCII-изображение: pyfiglet, Cowsay, Boxes, Image-to-ASCII'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Ascii-арт

ASCII-изображение: pyfiglet, Cowsay, Boxes, Image-to-ASCII.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/creative/ascii-art` |
| Версия | `4.0.0` |
| Автор | 0xbyt4, Агент Гермеса |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `ASCII`, `Art`, `Banners`, `Creative`, `Unicode`, `Text-Art`, `pyfiglet`, `figlet`, `cowsay`, `boxes` |
| Сопутствующие навыки | [`excalidraw`](/docs/user-guide/skills/bundled/creative/creative-excalidraw) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# ASCII Художественный навык

Несколько инструментов для различных художественных нужд ASCII. Все инструменты представляют собой локальные программы CLI или бесплатные API REST — ключи API не требуются.

## Инструмент 1: текстовые баннеры (pyfiglet — локально)

Отображение текста в виде больших художественных баннеров ASCII. 571 встроенный шрифт.

### Настройка

```bash
pip install pyfiglet --break-system-packages -q
```

### Использование

```bash
python3 -m pyfiglet "YOUR TEXT" -f slant
python3 -m pyfiglet "TEXT" -f doom -w 80    # Set width
python3 -m pyfiglet --list_fonts             # List all 571 fonts
```

### Рекомендуемые шрифты

| Стиль | Шрифт | Лучшее для |
|-------|------|----------|
| Чистый и современный | `slant` | Названия проектов, заголовки |
| Смелый и блочный | `doom` | Названия, логотипы |
| Большой и читаемый | `big` | Баннеры |
| Классический баннер | `banner3` | Широкие дисплеи |
| Компактный | `small` | Субтитры |
| Киберпанк | `cyberlarge` | Технические темы |
| 3D-эффект | `3-d` | Заставки |
| Готика | `gothic` | Драматический текст |

### Советы

- Предварительный просмотр 2–3 шрифтов и предоставление пользователю возможности выбрать любимый.
– Короткий текст (1–8 символов) лучше всего работает с подробными шрифтами, такими как `doom` или `block`.
– Длинный текст лучше работает с компактными шрифтами, такими как `small` или `mini`.

## Инструмент 2: текстовые баннеры (ассоциированный API — удаленно, без установки)

Бесплатный REST API, который преобразует текст в изображение ASCII. Более 250 шрифтов Figlet. Возвращает простой текст напрямую — анализ не требуется. Используйте это, если pyfiglet не установлен, или в качестве быстрой альтернативы.

### Использование (через терминал Curl)

```bash
# Basic text banner (default font)
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello+World"

# With a specific font
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Slant"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Doom"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Star+Wars"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=3-D"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Banner3"

# List all available fonts (returns JSON array)
curl -s "https://asciified.thelicato.io/api/v2/fonts"
```

### Советы

- URL-кодирует пробелы как `+` в текстовом параметре.
- Ответ представляет собой обычный текст в формате ASCII — без упаковки JSON, готов к отображению.
- Названия шрифтов чувствительны к регистру; используйте конечную точку шрифтов, чтобы получить точные имена
- Работает с любого терминала с помощью Curl — не требуется Python или pip.

## Инструмент 3: Cowsay (Искусство сообщений)

Классический инструмент, который помещает текст в речевой пузырь с символом ASCII.

### Настройка

```bash
sudo apt install cowsay -y    # Debian/Ubuntu
# brew install cowsay         # macOS
```

### Использование

```bash
cowsay "Hello World"
cowsay -f tux "Linux rules"       # Tux the penguin
cowsay -f dragon "Rawr!"          # Dragon
cowsay -f stegosaurus "Roar!"     # Stegosaurus
cowthink "Hmm..."                  # Thought bubble
cowsay -l                          # List all characters
```

### Доступные персонажи (50+)

`beavis.zen`, `bong`, `bunny`, `cheese`, `daemon`, `default`, `dragon`,
`dragon-and-cow`, `elephant`, `eyes`, `flaming-skull`, `ghostbusters`,
`hellokitty`, `kiss`, `kitty`, `koala`, `luke-koala`, `mech-and-cow`,
`meow`, `moofasa`, `moose`, `ren`, `sheep`, `skeleton`, `small`,
`stegosaurus`, `stimpy`, `supermilker`, `surgery`, `three-eyes`,
`turkey`, `turtle`, `tux`, `udder`, `vader`, `vader-koala`, `www`

### Модификаторы глаз/языка

```bash
cowsay -b "Borg"       # =_= eyes
cowsay -d "Dead"       # x_x eyes
cowsay -g "Greedy"     # $_$ eyes
cowsay -p "Paranoid"   # @_@ eyes
cowsay -s "Stoned"     # *_* eyes
cowsay -w "Wired"      # O_O eyes
cowsay -e "OO" "Msg"   # Custom eyes
cowsay -T "U " "Msg"   # Custom tongue
```

## Инструмент 4: Коробки (декоративные бордюры)

Нарисуйте декоративные рамки/рамки ASCII вокруг любого текста. 70+ встроенных дизайнов.

### Настройка

```bash
sudo apt install boxes -y    # Debian/Ubuntu
# brew install boxes         # macOS
```

### Использование

```bash
echo "Hello World" | boxes                    # Default box
echo "Hello World" | boxes -d stone           # Stone border
echo "Hello World" | boxes -d parchment       # Parchment scroll
echo "Hello World" | boxes -d cat             # Cat border
echo "Hello World" | boxes -d dog             # Dog border
echo "Hello World" | boxes -d unicornsay      # Unicorn
echo "Hello World" | boxes -d diamonds        # Diamond pattern
echo "Hello World" | boxes -d c-cmt           # C-style comment
echo "Hello World" | boxes -d html-cmt        # HTML comment
echo "Hello World" | boxes -a c               # Center text
boxes -l                                       # List all 70+ designs
```

### Комбинируйте с пифиглетом или асцифицированным

```bash
python3 -m pyfiglet "HERMES" -f slant | boxes -d stone
# Or without pyfiglet installed:
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=HERMES&font=Slant" | boxes -d stone
```

## Инструмент 5: ТУАЛЕТ (цветной текст)

Как pyfiglet, но с цветовыми эффектами ANSI и визуальными фильтрами. Отлично подходит для конечной радости глаз.

### Настройка

```bash
sudo apt install toilet toilet-fonts -y    # Debian/Ubuntu
# brew install toilet                      # macOS
```

### Использование

```bash
toilet "Hello World"                    # Basic text art
toilet -f bigmono12 "Hello"            # Specific font
toilet --gay "Rainbow!"                 # Rainbow coloring
toilet --metal "Metal!"                 # Metallic effect
toilet -F border "Bordered"             # Add border
toilet -F border --gay "Fancy!"         # Combined effects
toilet -f pagga "Block"                 # Block-style font (unique to toilet)
toilet -F list                          # List available filters
```

### Фильтры

`crop`, `gay` (радуга), `metal`, `flip`, `flop`, `180`, `left`, `right`, `border`

**Примечание**: туалет выводит escape-коды ANSI для цветов — работает в терминалах, но не может отображаться во всех контекстах (например, в текстовых файлах, на некоторых платформах чата).

## Инструмент 6: преобразование изображения в ASCII-изображение

Конвертируйте изображения (PNG, JPEG, GIF, WEBP) в формат ASCII.

### Вариант A: ascii-image-converter (рекомендуется, современный вариант)

```bash
# Install
sudo snap install ascii-image-converter
# OR: go install github.com/TheZoraiz/ascii-image-converter@latest
```

```bash
ascii-image-converter image.png                  # Basic
ascii-image-converter image.png -C               # Color output
ascii-image-converter image.png -d 60,30         # Set dimensions
ascii-image-converter image.png -b               # Braille characters
ascii-image-converter image.png -n               # Negative/inverted
ascii-image-converter https://url/image.jpg      # Direct URL
ascii-image-converter image.png --save-txt out   # Save as text
```

### Вариант Б: jp2a (облегченный, только JPEG)

```bash
sudo apt install jp2a -y
jp2a --width=80 image.jpg
jp2a --colors image.jpg              # Colorized
```

## Инструмент 7: Поиск готовых изображений ASCII

Найдите в Интернете рекомендованные изображения ASCII. Используйте `terminal` с `curl`.

### Источник A: ascii.co.uk (рекомендуется для готовых рисунков)

Большая коллекция классического искусства ASCII, организованная по темам. Искусство находится внутри тегов HTML `<pre>`. Получите страницу с помощью Curl, затем извлеките иллюстрацию с помощью небольшого фрагмента Python.

**Шаблон URL:** `https://ascii.co.uk/art/{subject}`

**Шаг 1. Получите страницу:**

```bash
curl -s 'https://ascii.co.uk/art/cat' -o /tmp/ascii_art.html
```

**Шаг 2. Извлеките иллюстрацию из pre-тегов:**

```python
import re, html
with open('/tmp/ascii_art.html') as f:
    text = f.read()
arts = re.findall(r'<pre[^>]*>(.*?)</pre>', text, re.DOTALL)
for art in arts:
    clean = re.sub(r'<[^>]+>', '', art)
    clean = html.unescape(clean).strip()
    if len(clean) > 30:
        print(clean)
        print('\n---\n')
```

**Доступные темы** (используйте в качестве URL-адреса):
- Животные: `cat`, `dog`, `horse`, `bird`, `fish`, `dragon`, `snake`, `rabbit`, `elephant`, `dolphin`, `butterfly`, `owl`, `wolf`, `bear`, `penguin`, `turtle`
- Объекты: `car`, `ship`, `airplane`, `rocket`, `guitar`, `computer`, `coffee`, `beer`, `cake`, `house`, `castle`, `sword`, `crown`, `key`
- Природа: `tree`, `flower`, `sun`, `moon`, `star`, `mountain`, `ocean`, `rainbow`.
- Персонажи: `skull`, `robot`, `angel`, `wizard`, `pirate`, `ninja`, `alien`.
- Праздники: `christmas`, `halloween`, `valentine`.

**Советы:**
- Сохраняйте подписи/инициалы художников — важный этикет
- Несколько произведений искусства на странице — выберите лучшее для пользователя.
- Надежно работает через Curl, JavaScript не требуется.

### Источник B: GitHub Octocat API (забавная пасхалка)

Возвращает случайный Octocat GitHub с мудрой цитатой. Никакой авторизации не требуется.

```bash
curl -s https://api.github.com/octocat
```

## Инструмент 8: забавные ASCII-утилиты (через Curl)

Эти бесплатные службы напрямую возвращают изображения ASCII — отлично подходят для развлечения.

### QR-коды в формате ASCII Art

```bash
curl -s "qrenco.de/Hello+World"
curl -s "qrenco.de/https://example.com"
```

### Погода в формате ASCII Art

```bash
curl -s "wttr.in/London"          # Full weather report with ASCII graphics
curl -s "wttr.in/Moon"            # Moon phase in ASCII art
curl -s "v2.wttr.in/London"       # Detailed version
```

## Инструмент 9: Пользовательское оформление, созданное LLM (резервный вариант)

Если в приведенных выше инструментах нет того, что необходимо, сгенерируйте изображение ASCII напрямую, используя эти символы Юникода:

### Палитра символов

**Чертеж коробки:** `╔ ╗ ╚ ╝ ║ ═ ╠ ╣ ╦ ╩ ╬ ┌ ┐ └ ┘ │ ─ ├ ┤ ┬ ┴ ┼ ╭ ╮ ╰ ╯`

**Элементы блока:** `░ ▒ ▓ █ ▄ ▀ ▌ ▐ ▖ ▗ ▘ ▝ ▚ ▞`

**Геометрия и символы:** `◆ ◇ ◈ ● ○ ◉ ■ □ ▲ △ ▼ ▽ ★ ☆ ✦ ✧ ◀ ▶ ◁ ▷ ⬡ ⬢ ⌂`

### Правила

- Максимальная ширина: 60 символов в строке (безопасно для терминала)
- Максимальная высота: 15 строк для баннеров, 25 для сцен.
- Только моноширинный формат: вывод должен корректно отображаться в шрифтах фиксированной ширины.

## Поток принятия решений

1. **Текст в виде баннера** → pyfiglet, если установлен, в противном случае — API-интерфейс с использованием Curl.
2. **Оформите сообщение забавным изображением персонажа** → Cowsay
3. **Добавить декоративную рамку/рамку** → коробки (можно комбинировать с pyfiglet/asciified)
4. **Искусство конкретной вещи** (кошка, ракета, дракон) → ascii.co.uk через Curl + синтаксический анализ
5. **Преобразовать изображение в ASCII** → ascii-image-converter или jp2a.
6. **QR-код** → qrenco.de через Curl
7. **Погода/луна** → wttr.in через Curl
8. **Что-то нестандартное/креативное** → Генерация LLM с палитрой Unicode
9. **Любой инструмент не установлен** → установите его или вернитесь к следующему варианту.