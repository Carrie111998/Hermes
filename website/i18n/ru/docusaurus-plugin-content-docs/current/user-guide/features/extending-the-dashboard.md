---
sidebar_position: 17
title: Расширение панели мониторинга
description: Создавайте темы и плагины для веб-панели Hermes — палитры, типографика,
  макеты, пользовательские вкладки, слоты оболочки, слоты на уровне страниц и маршруты
  серверного API.
---

# Расширение панели инструментов

Веб-панель Hermes (`hermes dashboard`) создана для изменения оформления и расширения без разветвления кодовой базы. Обнажаются три слоя:

1. **Темы** — файлы YAML, которые перерисовывают палитру, типографику, макет и хромирование каждого компонента панели управления. Перетащите файл в `~/.hermes/dashboard-themes/`; он появляется в переключателе тем.
2. **Плагины пользовательского интерфейса** — каталог с `manifest.json` + пакетом JavaScript, который регистрирует вкладку, заменяет встроенную страницу, дополняет ее через слоты на уровне страницы или внедряет компоненты в именованные слоты оболочки.
3. **Бэкенд-плагины** — файл Python внутри каталога плагинов, который предоставляет FastAPI `router`; маршруты монтируются в `/api/plugins/<name>/` и вызываются из пользовательского интерфейса плагина.

Все три **подключаются во время выполнения**: нет клонирования репозитория, нет `npm run build`, нет исправлений исходного кода информационной панели. Эта страница является канонической ссылкой для всех трех.

Если вы просто хотите использовать панель мониторинга, см. [Веб-панель](./web-dashboard). Если вы хотите изменить оформление интерфейса командной строки терминала (а не веб-панели управления), см. раздел [Скины и темы](./skins) — система оформления интерфейса командной строки не связана с темами информационной панели.

:::note Не настольное приложение
На этой странице описана система плагинов **веб-панели** (`hermes dashboard`) — `window.__HERMES_PLUGIN_SDK__`, `manifest.json` и готовый пакет JS. **Нативное настольное приложение** (`hermes desktop`) имеет собственный, несвязанный SDK — `@hermes/plugin-sdk`, один файл ESM, без этапа сборки — документировано в [Desktop Plugin SDK](/developer-guide/desktop-plugin-sdk). Между ними используется только внутреннее пространство имен `plugin_api.py` (`/api/plugins/<name>`).
:::

:::note Как складываются фигуры
Темы и плагины независимы, но синергичны. Тема может быть автономной (просто файл YAML). Плагин может стоять отдельно (просто вкладка). Вместе они позволяют вам создать полный визуальный рескин с пользовательскими HUD — пример демо `strike-freedom-cockpit` (находится в сопутствующем репозитории `hermes-example-plugins` — см. [Демо-версия комбинированной темы + плагина](#combined-theme--plugin-demo) для инструкций по установке) делает именно это.
:::

---

## Содержание

- [Темы](#темы)
  - [Быстрый старт — ваша первая тема](#quick-start--ваша-первая-тема)
  - [Палитра, типографика, макет](#palette-typography-layout)
  - [Варианты макета](#layout-variants)
  - [Ресурсы темы (изображения в виде переменных CSS)](#theme-assets-images-as-css-vars)
  - [Переопределения хрома компонентов](#comComponent-chrome-overrides)
  - [Переопределения цвета](#color-overrides)
  - [Raw `customCSS`](#raw-customcss)
  - [Встроенные темы](#built-in-themes)
  - [Полная ссылка на YAML темы](#full-theme-yaml-reference)
- [Плагины](#плагины)
  - [Быстрый старт — ваш первый плагин](#quick-start--ваш-первый-плагин)
  - [Макет каталога](#directory-layout)
  - [Ссылка на манифест](#manifest-reference)
  - [The Plugin SDK](#the-plugin-sdk)
  - [Слоты для оболочек](#shell-slots)
  - [Замена встроенных страниц (`tab.override`)](#replaceing-built-in-pages-taboverride)
  - [Дополнение встроенных страниц (слотов на уровне страницы)](#augmenting-built-in-pages-page-scoped-slots)
  - [Плагины только для слотов (`tab.hidden`)](#slot-only-plugins-tabhidden)
  - [Маршруты Backend API](#backend-api-routes)
  - [Пользовательский CSS для каждого плагина](#custom-css-per-plugin)
  - [Обнаружение и перезагрузка плагина](#plugin-discovery--reload)
- [Объединенная тема + демо-версия плагина](#combined-theme--plugin-demo)
- [ссылка на API](#api-ссылка)
- [Устранение неполадок](#устранение неполадок)

---

## Темы

Темы — это файлы YAML, хранящиеся в `~/.hermes/dashboard-themes/`. Имя файла не имеет значения (поле `name:` темы — это то, что использует система), но соглашение — `<name>.yaml`. Каждое поле является необязательным — отсутствующие ключи относятся к встроенной теме `default`, поэтому тема может содержать всего один цвет.

### Быстрый старт — ваша первая тема

```bash
mkdir -p ~/.hermes/dashboard-themes
```

```yaml
# ~/.hermes/dashboard-themes/neon.yaml
name: neon
label: Neon
description: Pure magenta on black

palette:
  background: "#000000"
  midground: "#ff00ff"
```

Обновите панель мониторинга. Нажмите значок палитры в заголовке и выберите **Неон**. Фон становится черным, текст и акценты становятся пурпурными, и каждый производный цвет (карточка, рамка, приглушенный цвет, кольцо и т. д.) пересчитывается из этого двухцветного триплета с помощью `color-mix()` в CSS.

Вот и весь онбординг: один файл, два цвета. Все, что ниже, является необязательной доработкой.

### Палитра, типографика, верстка

Эти три блока являются сердцем темы. Каждый из них независим: отмените один, оставьте остальные.

#### Палитра (3-слойная)

Палитра представляет собой тройку цветовых слоев, а также цвет виньетки теплого свечения и множитель зернистости шума. Каскад системы дизайна панели управления извлекает каждый токен, совместимый с Shadcn (карточка, поповер, отключенный звук, граница, основной, деструктивный, кольцевой и т. д.) из этого триплета через CSS `color-mix()`. Переопределение трех цветов распространяется на весь пользовательский интерфейс.

| Ключ | Описание |
|-----|-------------|
| `palette.background` | Самый глубокий цвет холста — обычно почти черный. Управляет фоном страницы и заполнением карточки. |
| `palette.midground` | Основной текст и ударение. Большая часть хрома пользовательского интерфейса читает это (текст переднего плана, контуры кнопок, кольца фокусировки). |
| `palette.foreground` | Подсветка верхнего слоя. Тема по умолчанию устанавливает белый цвет с альфа-0 (невидимый); темы, которым нужен яркий акцент сверху, могут повысить его альфу. |
| `palette.warmGlow` | Строка `rgba(...)`, используемая в качестве цвета виньетки `<Backdrop />`. |
| `palette.noiseOpacity` | Множитель 0–1,2 на наложении зерна. Ниже = мягче, выше = жестче. |

Каждый слой принимает либо `{hex: "#RRGGBB", alpha: 0.0–1.0}`, либо пустую шестнадцатеричную строку (по умолчанию значение альфа равно 1,0).

```yaml
palette:
  background:
    hex: "#05091a"
    alpha: 1.0
  midground: "#d8f0ff"          # bare hex, alpha = 1.0
  foreground:
    hex: "#ffffff"
    alpha: 0                    # invisible top layer
  warmGlow: "rgba(255, 199, 55, 0.24)"
  noiseOpacity: 0.7
```

#### Типографика

| Ключ | Тип | Описание |
|-----|------|-------------|
| `fontSans` | строка | Стек семейства шрифтов CSS для основного текста (применяется к `html`, `body`). |
| `fontMono` | строка | Стек семейств шрифтов CSS для блоков кода, утилиты `<code>`, `.font-mono`. |
| `fontDisplay` | строка | Дополнительный стек заголовков/отображений. Возвращается к `fontSans`. |
| `fontUrl` | строка | Необязательный URL-адрес внешней таблицы стилей. Внедряется как `<link rel="stylesheet">` в `<head>` при переключении темы. Один и тот же URL-адрес никогда не вводится дважды. Работает с Google Fonts, Bunny Fonts, самостоятельными листами `@font-face` — со всем, что можно связать. |
| `baseSize` | строка | Размер основного шрифта — управляет масштабом. Например. `"14px"`, `"16px"`. |
| `lineHeight` | строка | Высота строки по умолчанию. Например. `"1.5"`, `"1.65"`. |
| `letterSpacing` | строка | Расстояние между буквами по умолчанию. Например. `"0"`, `"0.01em"`, `"-0.01em"`. |

```yaml
typography:
  fontSans: '"Orbitron", "Eurostile", "Impact", sans-serif'
  fontMono: '"Share Tech Mono", ui-monospace, monospace'
  fontDisplay: '"Orbitron", "Eurostile", sans-serif'
  fontUrl: "https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700&family=Share+Tech+Mono&display=swap"
  baseSize: "14px"
  lineHeight: "1.5"
  letterSpacing: "0.04em"
```

##### Изменение шрифта в пользовательском интерфейсе (без YAML)

В средстве выбора тем в заголовке информационной панели есть раздел **Шрифт** под
список тем. Выберите там любой шрифт, и он переопределит основной шрифт любого другого шрифта.
тема активна — выбор не зависит от темы и сохраняется во всем
переключатели тем (хранятся в `config.yaml` в `dashboard.font`). Выберите
**Тема по умолчанию**, чтобы отменить переопределение и вернуться к активной теме.
собственный `fontSans`.

Средство выбора предлагает тщательно подобранный каталог (системные стеки плюс набор Google-шрифтов).
семейства без/засечек/моно). Он намеренно **не** принимает
URL-адрес шрифта с произвольным текстом — таблица стилей шрифта вводится как `<link>`, поэтому
Каталог сохраняет введенные источники фиксированными. Для полностью индивидуального лица установите
`fontSans` + `fontUrl` в YAML-теме, как показано выше. Тема `fontMono`
(блоки кода, терминал) всегда остаются нетронутыми переопределением пользовательского интерфейса.

#### Макет

| Ключ | Ценности | Описание |
|-----|--------|-------------|
| `radius` | любая длина CSS (`"0"`, `"0.25rem"`, `"0.5rem"`, `"1rem"`, ...) | Жетон углового радиуса. Сопоставляется с `--radius` и каскадом с `--radius-sm/md/lg/xl` — каждый закругленный элемент сдвигается вместе. |
| `density` | `compact` \| `comfortable` \| `spacious` | Множитель интервала применяется как переменная CSS `--spacing-mul`. `compact = 0.85×`, `comfortable = 1.0×` (по умолчанию), `spacious = 1.2×`. Масштабирует базовый интервал Tailwind, поэтому отступы, пробелы и пространство между утилитами смещаются пропорционально. |

```yaml
layout:
  radius: "0"
  density: compact
```

### Варианты планировки

`layoutVariant` выбирает общий макет оболочки. По умолчанию используется `"standard"`, если отсутствует.

| Вариант | Поведение |
|---------|-----------|
| `standard` | Один столбец, максимальная ширина 1600 пикселей (по умолчанию). |
| `cockpit` | Левая боковая панель (260 пикселей) + основной контент. Заполняется плагинами через слот `sidebar` — см. [слоты оболочки](#shell-slots). Без плагина на рельсе отображается заполнитель. |
| `tiled` | Удаляет ограничение максимальной ширины, чтобы страницы могли использовать всю ширину области просмотра. |

```yaml
layoutVariant: cockpit
```

Текущий вариант представлен как `document.documentElement.dataset.layoutVariant`, поэтому необработанный CSS в `customCSS` может быть нацелен на него через `:root[data-layout-variant="cockpit"] ...`.

### Ресурсы темы (изображения в виде переменных CSS)

Отправляйте URL-адреса иллюстраций вместе с темой. Каждый именованный слот становится переменной CSS (`--theme-asset-<name>`), которую может прочитать встроенная оболочка и любой плагин. Слот `bg` автоматически подключается к фону; другие слоты ориентированы на плагины.

```yaml
assets:
  bg: "https://example.com/hero-bg.jpg"           # auto-wired into <Backdrop />
  hero: "/my-images/strike-freedom.png"           # for plugin sidebars
  crest: "/my-images/crest.svg"                   # for header-left plugins
  logo: "/my-images/logo.png"
  sidebar: "/my-images/rail.png"
  header: "/my-images/header-art.png"
  custom:
    scanLines: "/my-images/scanlines.png"         # → --theme-asset-custom-scanLines
```

Значения принимаются:

– Пустые URL-адреса — автоматически оборачиваются `url(...)`.
— Предварительно упакованные выражения `url(...)`, `linear-gradient(...)`, `radial-gradient(...)` — используются как есть.
- `"none"` — явный отказ.

Каждый ресурс также генерируется как `--theme-asset-<name>-raw` (развёрнутый URL-адрес), на случай, если плагину потребуется передать его в `<img src>` вместо `background-image`.

Плагины считывают их с помощью простого CSS или JS:

```javascript
// In a plugin slot
const hero = getComputedStyle(document.documentElement)
  .getPropertyValue("--theme-asset-hero").trim();
```

### Переопределения хрома компонентов

`componentStyles` изменяет стиль отдельных компонентов оболочки без написания селекторов CSS. Записи каждого сегмента становятся переменными CSS (`--component-<bucket>-<kebab-property>`), которые считываются общими компонентами оболочки. Таким образом, переопределения `card:` применяются к каждому `<Card>`, `header:` к панели приложения и т. д.

```yaml
componentStyles:
  card:
    clipPath: "polygon(12px 0, 100% 0, 100% calc(100% - 12px), calc(100% - 12px) 100%, 0 100%, 0 12px)"
    background: "linear-gradient(180deg, rgba(10, 22, 52, 0.85), rgba(5, 9, 26, 0.92))"
    boxShadow: "inset 0 0 0 1px rgba(64, 200, 255, 0.28)"
  header:
    background: "linear-gradient(180deg, rgba(16, 32, 72, 0.95), rgba(5, 9, 26, 0.9))"
  tab:
    clipPath: "polygon(6px 0, 100% 0, calc(100% - 6px) 100%, 0 100%)"
  sidebar: {}
  backdrop: {}
  footer: {}
  progress: {}
  badge: {}
  page: {}
```

Поддерживаемые сегменты: `card`, `header`, `footer`, `sidebar`, `tab`, `progress`, `badge`, `backdrop`, `page`.

В именах свойств используется верблюжий регистр (`clipPath`) и они оформляются как кебаб (`clip-path`). Значения представляют собой простые строки CSS — все, что принимает CSS (`clip-path`, `border-image`, `background`, `box-shadow`, `animation`, ...).

### Переопределение цвета

Большинству тем это не понадобится — трехслойная палитра извлекает каждый токен Shadcn. Используйте `colorOverrides`, если вам нужен определенный акцент, которого не будет при выводе (более мягкий разрушительный красный для пастельной темы, особый зеленый для бренда).

```yaml
colorOverrides:
  primary: "#ffce3a"
  primaryForeground: "#05091a"
  accent: "#3fd3ff"
  ring: "#3fd3ff"
  destructive: "#ff3a5e"
  border: "rgba(64, 200, 255, 0.28)"
```

Поддерживаемые ключи: `card`, `cardForeground`, `popover`, `popoverForeground`, `primary`, `primaryForeground`, `secondary`, `secondaryForeground`, `muted`, `mutedForeground`, `accent`, `accentForeground`, `destructive`, `destructiveForeground`, `success`, `warning`, `border`, `input`, `ring`.

Каждый ключ сопоставляется 1:1 с переменной CSS `--color-<kebab>` (например, `primaryForeground` → `--color-primary-foreground`). Любой ключ, установленный здесь, имеет приоритет над каскадом палитр только для активной темы — переключение на другую тему очищает переопределения.

### Сырой `customCSS`

Для Chrome на уровне селектора, который `componentStyles` не может выразить — псевдоэлементы, анимация, медиа-запросы, переопределения на уровне темы — добавьте необработанный CSS в `customCSS`:

```yaml
customCSS: |
  /* Scanline overlay — only visible when cockpit variant is active. */
  :root[data-layout-variant="cockpit"] body::before {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 100;
    background: repeating-linear-gradient(to bottom,
      transparent 0px, transparent 2px,
      rgba(64, 200, 255, 0.035) 3px, rgba(64, 200, 255, 0.035) 4px);
    mix-blend-mode: screen;
  }
```

CSS вводится как один тег `<style data-hermes-theme-css>` с областью действия при применении темы и очищается при переключении темы. **Ограничение — 32 КиБ на тему.**

### Встроенные темы

Каждая встроенная функция имеет собственную палитру, типографику и макет — переключение приводит к видимым изменениям, выходящим за рамки одного лишь цвета.

| Тема | Палитра | Типография | Макет |
|-------|---------|------------|--------|
| **Гермес Тил** (`default`) | Темно-бирюзовый + кремовый | Системный стек, 15 пикселей | Радиус 0,5 rem, удобно |
| **Гермес Тил (Большой)** (`default-large`) | То же, что и по умолчанию | Системный стек, 18 пикселей, высота строки 1,65 | Радиус 0,5 рем, просторный |
| **Полночь** (`midnight`) | Глубокий сине-фиолетовый | Интер + JetBrains Mono, 14 пикселей | Радиус 0,75 rem, удобно |
| **Эмбер** (`ember`) | Теплый малиновый + бронза | Спектральный (с засечками) + IBM Plex Mono, 15 пикселей | Радиус 0,25 rem, удобно |
| **Моно** (`mono`) | оттенки серого | IBM Plex Sans + IBM Plex Mono, 13 пикселей | радиус 0, компактный |
| **Киберпанк** (`cyberpunk`) | Неоново-зеленый на черном | Поделитесь Tech Mono везде, 14 пикселей | радиус 0, компактный |
| **Розовое** (`rose`) | Розовый + слоновая кость | Fraunces (засечки) + DM Mono, 16 пикселей | Радиус 1 метр, просторный |

Темы, ссылающиеся на шрифты Google (все, кроме Hermes Teal), загружают таблицу стилей по требованию — при первом переключении на них тег `<link>` вводится в `<head>`.

### Полная ссылка на YAML темы

Каждая ручка в одном файле — скопируйте и обрежьте то, что вам не нужно:

```yaml
# ~/.hermes/dashboard-themes/ocean.yaml
name: ocean
label: Ocean Deep
description: Deep sea blues with coral accents

# 3-layer palette (accepts {hex, alpha} or bare hex)
palette:
  background:
    hex: "#0a1628"
    alpha: 1.0
  midground:
    hex: "#a8d0ff"
    alpha: 1.0
  foreground:
    hex: "#ffffff"
    alpha: 0.0
  warmGlow: "rgba(255, 107, 107, 0.35)"
  noiseOpacity: 0.7

typography:
  fontSans: "Poppins, system-ui, sans-serif"
  fontMono: "Fira Code, ui-monospace, monospace"
  fontDisplay: "Poppins, system-ui, sans-serif"   # optional
  fontUrl: "https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&family=Fira+Code:wght@400;500&display=swap"
  baseSize: "15px"
  lineHeight: "1.6"
  letterSpacing: "-0.003em"

layout:
  radius: "0.75rem"
  density: comfortable

layoutVariant: standard        # standard | cockpit | tiled

assets:
  bg: "https://example.com/ocean-bg.jpg"
  hero: "/my-images/kraken.png"
  crest: "/my-images/anchor.svg"
  logo: "/my-images/logo.png"
  custom:
    pattern: "/my-images/waves.svg"

componentStyles:
  card:
    boxShadow: "inset 0 0 0 1px rgba(168, 208, 255, 0.18)"
  header:
    background: "linear-gradient(180deg, rgba(10, 22, 40, 0.95), rgba(5, 9, 26, 0.9))"

colorOverrides:
  destructive: "#ff6b6b"
  ring: "#ff6b6b"

customCSS: |
  /* Any additional selector-level tweaks */
```

Обновите панель мониторинга после создания файла. Переключайте темы в реальном времени из панели заголовка — щелкните значок палитры. Выбор сохраняется до `config.yaml` в `dashboard.theme` и восстанавливается при перезагрузке.

---

## Плагины

Плагин информационной панели представляет собой каталог с `manifest.json`, предварительно созданным пакетом JS и, при необходимости, файлом CSS и файлом Python с маршрутами FastAPI. Плагины находятся рядом с другими плагинами Hermes в `~/.hermes/plugins/<name>/` — расширение информационной панели представляет собой подпапку `dashboard/` внутри этого каталога плагинов, поэтому один плагин может расширять как CLI/шлюз, так и панель мониторинга за одну установку.

Плагины не объединяют компоненты React или пользовательского интерфейса. Они используют **Plugin SDK**, представленный на `window.__HERMES_PLUGIN_SDK__`. Это сохраняет размер пакетов плагинов небольшими (обычно несколько КБ) и позволяет избежать конфликтов версий.

### Быстрый старт — ваш первый плагин

Создайте структуру каталогов:

```bash
mkdir -p ~/.hermes/plugins/my-plugin/dashboard/dist
```

Напишите манифест:

```json
// ~/.hermes/plugins/my-plugin/dashboard/manifest.json
{
  "name": "my-plugin",
  "label": "My Plugin",
  "icon": "Sparkles",
  "version": "1.0.0",
  "tab": {
    "path": "/my-plugin",
    "position": "after:skills"
  },
  "entry": "dist/index.js"
}
```

Напишите пакет JS (простой IIFE — этап сборки не требуется):

```javascript
// ~/.hermes/plugins/my-plugin/dashboard/dist/index.js
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { Card, CardHeader, CardTitle, CardContent } = SDK.components;

  function MyPage() {
    return React.createElement(Card, null,
      React.createElement(CardHeader, null,
        React.createElement(CardTitle, null, "My Plugin"),
      ),
      React.createElement(CardContent, null,
        React.createElement("p", { className: "text-sm text-muted-foreground" },
          "Hello from my custom dashboard tab.",
        ),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("my-plugin", MyPage);
})();
```

Обновите панель управления — ваша вкладка появится на панели навигации после **Навыки**.

:::tip Пропустить React.createElement
Если вы предпочитаете JSX, используйте любой сборщик (esbuild, Vite, накопительный пакет) с React в качестве внешнего выхода и IIFE. Единственное жесткое требование — чтобы конечный файл представлял собой один файл JS, загружаемый через `<script>`. React никогда не поставляется в комплекте; оно происходит из `SDK.React`.
:::

### Макет каталога

```
~/.hermes/plugins/my-plugin/
├── plugin.yaml              # optional — existing CLI/gateway plugin manifest
├── __init__.py              # optional — existing CLI/gateway hooks
└── dashboard/               # dashboard extension
    ├── manifest.json        # required — tab config, icon, entry point
    ├── dist/
    │   ├── index.js         # required — pre-built JS bundle (IIFE)
    │   └── style.css        # optional — custom CSS
    └── plugin_api.py        # optional — backend API routes (FastAPI)
```

Один каталог плагинов может содержать три ортогональных расширения:

- `plugin.yaml` + `__init__.py` — плагин CLI/шлюза ([см. страницу плагинов](./plugins)).
- `dashboard/manifest.json` + `dashboard/dist/index.js` — плагин пользовательского интерфейса панели управления.
- `dashboard/plugin_api.py` — внутренние маршруты панели управления.

Ни один из них не требуется; включать только те слои, которые вам нужны.

### Ссылка на манифест

```json
{
  "name": "my-plugin",
  "label": "My Plugin",
  "description": "What this plugin does",
  "icon": "Sparkles",
  "version": "1.0.0",
  "tab": {
    "path": "/my-plugin",
    "position": "after:skills",
    "override": "/",
    "hidden": false
  },
  "slots": ["sidebar", "header-left"],
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

| Поле | Требуется | Описание |
|-------|----------|-------------|
| `name` | Да | Уникальный идентификатор плагина. Строчные буквы, дефисы — ок. Используется в URL-адресах и регистрации. |
| `label` | Да | Отображаемое имя, отображаемое на вкладке навигации. |
| `description` | Нет | Краткое описание (отображается на панели администратора панели управления). |
| `icon` | Нет | Название значка Люцида. По умолчанию `Puzzle`. Неизвестные имена возвращаются к `Puzzle`. |
| `version` | Нет | Семверская струна. По умолчанию `0.0.0`. |
| `tab.path` | Да | URL-путь для вкладки (например, `/my-plugin`). |
| `tab.position` | Нет | Куда вставить вкладку. `"end"` (по умолчанию), `"after:<path>"` или `"before:<path>"` — значение после двоеточия представляет собой **сегмент пути** целевой вкладки (без косой черты в начале). Примеры: `"after:skills"`, `"before:config"`. |
| `tab.override` | Нет | Установите встроенный путь маршрута (`"/"`, `"/sessions"`, `"/config"`, ...), чтобы **заменить** эту страницу вместо добавления новой вкладки. См. [Замена встроенных страниц](#replacing-built-in-pages-taboverride). |
| `tab.hidden` | Нет | Если это правда, зарегистрируйте компонент и все слоты, не добавляя вкладку в навигацию. Используется плагинами только для слотов. См. [Плагины только для слотов](#slot-only-plugins-tabhidden). |
| `slots` | Нет | Именованные слоты оболочки, которые заполняет этот плагин. **Только документация** — фактическая регистрация происходит из пакета JS через `registerSlot()`. Размещение слотов здесь делает поверхность обнаружения более информативной. |
| `entry` | Да | Путь к пакету JS относительно `dashboard/`. По умолчанию `dist/index.js`. |
| `css` | Нет | Путь к файлу CSS для внедрения в виде тега `<link>`. |
| `api` | Нет | Путь к файлу Python с маршрутами FastAPI. Установлен на `/api/plugins/<name>/`. |

#### Доступные значки

Плагины используют имена значков Lucid. Панель управления сопоставляет их по именам — неизвестные имена автоматически возвращаются к `Puzzle`.

На данный момент сопоставлены: `Activity`, `BarChart3`, `Clock`, `Code`, `Database`, `Eye`, `FileText`, `Globe`, `Heart`, `KeyRound`, `MessageSquare`, `Package`, `Puzzle`, `Settings`, `Shield`, `Sparkles`, `Star`, `Terminal`, `Wrench`, `Zap`.

Нужен другой значок? Откройте PR на `ICON_MAP` от `web/src/App.tsx` — чистое аддитивное изменение.

### SDK плагина

Все, что нужно плагину, находится на `window.__HERMES_PLUGIN_SDK__`. Плагины никогда не должны импортировать React напрямую.

```javascript
const SDK = window.__HERMES_PLUGIN_SDK__;

// React + hooks
SDK.React                    // the React instance
SDK.hooks.useState
SDK.hooks.useEffect
SDK.hooks.useCallback
SDK.hooks.useMemo
SDK.hooks.useRef
SDK.hooks.useContext
SDK.hooks.createContext

// UI components (shadcn/ui primitives)
SDK.components.Card
SDK.components.CardHeader
SDK.components.CardTitle
SDK.components.CardContent
SDK.components.Badge
SDK.components.Button
SDK.components.Input
SDK.components.Label
SDK.components.Select
SDK.components.SelectOption
SDK.components.Separator
SDK.components.Tabs
SDK.components.TabsList
SDK.components.TabsTrigger
SDK.components.PluginSlot    // render a named slot (useful for nested plugin UIs)

// Hermes API client + raw fetcher
SDK.api                      // typed client — getStatus, getSessions, getConfig, ...
SDK.fetchJSON                // raw fetch for custom endpoints (plugin-registered routes)

// Utilities
SDK.utils.cn                 // Tailwind class merger (clsx + twMerge)
SDK.utils.timeAgo            // "5m ago" from unix timestamp
SDK.utils.isoTimeAgo         // "5m ago" from ISO string

// Hooks
SDK.useI18n                  // i18n hook for multi-language plugins
```

#### Вызов бэкэнда вашего плагина

```javascript
SDK.fetchJSON("/api/plugins/my-plugin/data")
  .then((data) => console.log(data))
  .catch((err) => console.error("API call failed:", err));
```

`fetchJSON` внедряет токен аутентификации сеанса, отображает ошибки в виде исключений и автоматически анализирует JSON.

#### Вызов встроенных конечных точек Hermes

```javascript
// Agent status
SDK.api.getStatus().then((s) => console.log("Version:", s.version));

// Recent sessions
SDK.api.getSessions(10).then((resp) => console.log(resp.sessions.length));
```

Полный список см. в разделе [Веб-панель → REST API](./web-dashboard#rest-api).

### Слоты для ракушек

Слоты позволяют плагину вставлять компоненты в именованные места оболочки приложения — боковую панель, заголовок, нижний колонтитул, слой наложения — не требуя целой вкладки. Несколько плагинов могут занимать один и тот же слот; они отображаются сложенными в порядке регистрации.

Зарегистрируйтесь изнутри пакета плагина:

```javascript
window.__HERMES_PLUGINS__.registerSlot("my-plugin", "sidebar", MySidebar);
window.__HERMES_PLUGINS__.registerSlot("my-plugin", "header-left", MyCrest);
```

#### Каталог слотов

**Слоты по всей оболочке** (отображаются в любом месте Chrome приложения):

| Слот | Расположение |
|------|----------|
| `backdrop` | Внутри стека слоев `<Backdrop />`, над слоем шума. |
| `header-left` | Перед брендом Hermes в верхнем баре. |
| `header-right` | До переключателей темы/языка в верхней панели. |
| `header-banner` | Полоса во всю ширину под навигационной панелью. |
| `sidebar` | Боковая панель кабины — **отображается только при `layoutVariant === "cockpit"`**. |
| `pre-main` | Над выходом на маршрут (внутри `<main>`). |
| `post-main` | Под выходом на маршрут (внутри `<main>`). |
| `footer-left` | Содержимое ячейки нижнего колонтитула (заменяет значение по умолчанию). |
| `footer-right` | Содержимое ячейки нижнего колонтитула (заменяет значение по умолчанию). |
| `overlay` | Слой с фиксированным положением выше всего остального. Полезно для хрома (линии развертки, виньетки). `customCSS` не может достичь в одиночку. |

**Слоты на уровне страницы** (отображаются только на указанной встроенной странице — используйте их для внедрения виджетов, карточек или панелей инструментов на существующую страницу без переопределения всего маршрута):

| Слот | Где он отображается |
|------|------------------|
| `sessions:top` / `sessions:bottom` | Верхняя/нижняя часть страницы `/sessions`. |
| `analytics:top` / `analytics:bottom` | Верхняя/нижняя часть страницы `/analytics`. |
| `logs:top` / `logs:bottom` | Верх (над панелью фильтров)/низ (под средством просмотра журнала) `/logs`. |
| `cron:top` / `cron:bottom` | Верхняя/нижняя часть страницы `/cron`. |
| `skills:top` / `skills:bottom` | Верхняя/нижняя часть страницы `/skills`. |
| `config:top` / `config:bottom` | Верхняя/нижняя часть страницы `/config`. |
| `env:top` / `env:bottom` | Верхняя/нижняя часть страницы `/env` (Ключи). |
| `docs:top` / `docs:bottom` | Верх (над iframe)/низ `/docs`. |
| `chat:top` / `chat:bottom` | Верх/низ `/chat` (активен только при включенном встроенном чате). |

Пример — добавьте баннер в верхнюю часть страницы «Сессии»:

```javascript
function PinnedSessionsBanner() {
  return React.createElement(Card, null,
    React.createElement(CardContent, { className: "py-2 text-xs" },
      "Pinned note injected by my-plugin"),
  );
}

window.__HERMES_PLUGINS__.registerSlot("my-plugin", "sessions:top", PinnedSessionsBanner);
```

Объедините слоты на уровне страниц с `tab.hidden: true`, если ваш плагин только дополняет существующие страницы и не нуждается в собственной вкладке боковой панели.

Оболочка отображает только `<PluginSlot name="..." />` для слотов выше. Дополнительные имена принимаются реестром для вложенных пользовательских интерфейсов плагинов — плагин может предоставлять свои собственные слоты через `SDK.components.PluginSlot`.

#### Перерегистрация и HMR

Если одна и та же пара `(plugin, slot)` зарегистрирована дважды, более поздний вызов заменяет предыдущий — это соответствует тому, как React HMR ожидает поведения повторного монтирования плагина.

### Замена встроенных страниц (`tab.override`)

Установка `tab.override` для встроенного пути маршрута заставляет компонент плагина заменять эту страницу вместо добавления новой вкладки. Полезно, когда теме нужна пользовательская домашняя страница (`/`), но требуется сохранить остальную часть панели управления нетронутой.

```json
{
  "name": "my-home",
  "label": "Home",
  "tab": {
    "path": "/my-home",
    "override": "/",
    "position": "end"
  },
  "entry": "dist/index.js"
}
```

С установленным `override`:

- Исходный компонент страницы `/` удален из маршрутизатора.
- Вместо этого ваш плагин отображается по адресу `/`.
- Для `tab.path` не добавлена ​​вкладка навигации (переопределение является сутью).

Только один плагин может переопределить заданный путь. Если два плагина заявляют об одном и том же переопределении, первый выигрывает, а второй игнорируется с предупреждением режима разработки.

Если вам нужно только добавить карточку или панель инструментов на существующую страницу, не захватывая ее, используйте вместо этого [слоты на уровне страницы](#augmenting-built-in-pages-page-scoped-slots).

### Расширение встроенных страниц (страничных слотов)

Полная замена через `tab.override` сложна — ваш плагин теперь владеет всей страницей, включая все будущие обновления, которые мы на него отправим. В большинстве случаев вам просто нужно добавить баннер, карточку или панель инструментов на существующую страницу. Вот для чего нужны **места на уровне страниц**.

На каждой встроенной странице имеются слоты `<page>:top` и `<page>:bottom`, отображаемые вверху и внизу области содержимого. Ваш плагин заполняет его, вызывая `registerSlot()` — встроенная страница продолжает работать нормально, и ваш компонент отображается вместе с ней.

Доступные слоты: `sessions:*`, `analytics:*`, `logs:*`, `cron:*`, `skills:*`, `config:*`, `env:*`, `docs:*`, `chat:*` (каждый с `:top` и `:bottom`). Полный каталог смотрите в разделе [Слоты для снарядов → Каталог слотов](#slot-catalogue).

Минимальный пример — закрепите баннер вверху страницы «Сессии»:

```json
// ~/.hermes/plugins/session-notes/dashboard/manifest.json
{
  "name": "session-notes",
  "label": "Session Notes",
  "tab": { "path": "/session-notes", "hidden": true },
  "slots": ["sessions:top"],
  "entry": "dist/index.js"
}
```

```javascript
// ~/.hermes/plugins/session-notes/dashboard/dist/index.js
(function () {
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { Card, CardContent } = SDK.components;

  function Banner() {
    return React.createElement(Card, null,
      React.createElement(CardContent, { className: "py-2 text-xs" },
        "Remember to label important sessions before archiving."),
    );
  }

  // Placeholder for the hidden tab.
  window.__HERMES_PLUGINS__.register("session-notes", function () { return null; });

  // The real work.
  window.__HERMES_PLUGINS__.registerSlot("session-notes", "sessions:top", Banner);
})();
```

Ключевые моменты:

- `tab.hidden: true` убирает плагин из боковой панели — у него нет отдельной страницы.
– Поле манифеста `slots` предназначено только для документации. Фактическая привязка происходит в пакете JS через `registerSlot()`.
- Несколько плагинов могут претендовать на один и тот же слот на странице. Они рендерятся в порядке регистрации.
- Нулевой след, когда ни один плагин не регистрируется: встроенная страница отображается точно так же, как и раньше.

Эталонный плагин (`example-dashboard` в [`hermes-example-plugins`](https://github.com/NousResearch/hermes-example-plugins/tree/main/example-dashboard)) предоставляет живую демо-версию, которая встраивает баннер в `sessions:top` — установите его, чтобы увидеть шаблон от начала до конца.

### Плагины только для слотов (`tab.hidden`)

При `tab.hidden: true` плагин регистрирует свой компонент (для прямого посещения URL-адреса) и любые слоты, но никогда не добавляет вкладку в навигацию. Используется плагинами, которые существуют только для внедрения в слоты — гребень заголовка, HUD боковой панели, наложение.

```json
{
  "name": "header-crest",
  "label": "Header Crest",
  "tab": {
    "path": "/header-crest",
    "position": "end",
    "hidden": true
  },
  "slots": ["header-left"],
  "entry": "dist/index.js"
}
```

Пакет по-прежнему вызывает `register()` с компонентом-заполнителем (хорошая практика на случай, если кто-то напрямую обращается к URL-адресу), а затем `registerSlot()` для выполнения реальной работы.

### Маршруты серверного API

Плагины могут регистрировать маршруты FastAPI, установив `api` в манифесте. Создайте файл и экспортируйте `router`:

```python
# ~/.hermes/plugins/my-plugin/dashboard/plugin_api.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/data")
async def get_data():
    return {"items": ["one", "two", "three"]}

@router.post("/action")
async def do_action(body: dict):
    return {"ok": True, "received": body}
```

Маршруты монтируются под `/api/plugins/<name>/`, поэтому приведенное выше становится:

- `GET  /api/plugins/my-plugin/data`
- `POST /api/plugins/my-plugin/action`

Маршруты API плагинов находятся за обычным шлюзом аутентификации панели управления — неаутентифицированные запросы получают `401` перед запуском маршрута плагина, а запросы к маршрутам отключенного плагина отклоняются во время запроса. Тем не менее, **не открывайте панель мониторинга в общедоступном интерфейсе с помощью `--host 0.0.0.0`, если вы используете ненадежные плагины** — сеанс, прошедший проверку подлинности, также может получить доступ к их маршрутам.

#### Доступ к внутренним компонентам Hermes

Внутренние маршруты выполняются внутри процесса информационной панели, поэтому их можно импортировать напрямую из базы кода агента Hermes:

```python
from fastapi import APIRouter
from hermes_state import SessionDB
from hermes_cli.config import load_config

router = APIRouter()

@router.get("/session-count")
async def session_count():
    db = SessionDB()
    try:
        count = len(db.list_sessions(limit=9999))
        return {"count": count}
    finally:
        db.close()

@router.get("/config-snapshot")
async def config_snapshot():
    cfg = load_config()
    return {"model": cfg.get("model", {})}
```

### Пользовательский CSS для каждого плагина

Если вашему плагину нужны стили помимо классов Tailwind и встроенного `style=`, добавьте файл CSS и укажите его в манифесте:

```json
{
  "css": "dist/style.css"
}
```

Файл внедряется как тег `<link>` при загрузке плагина. Используйте определенные имена классов, чтобы избежать конфликтов со стилями панели мониторинга, и ссылайтесь на переменные CSS панели мониторинга, чтобы оставаться в курсе тем:

```css
/* dist/style.css */
.my-plugin-chart {
  border: 1px solid var(--color-border);
  background: var(--color-card);
  color: var(--color-card-foreground);
  padding: 1rem;
}
.my-plugin-chart:hover {
  border-color: var(--color-ring);
}
```

На информационной панели каждый токен Shadcn отображается как `--color-*`, а также дополнительные темы (`--theme-asset-*`, `--component-<bucket>-*`, `--radius`, `--spacing-mul`). Обратитесь к ним, и ваш плагин автоматически обновится с активной темой.

### Обнаружение и перезагрузка плагина

Панель мониторинга сканирует три каталога на предмет `dashboard/manifest.json`:

| Приоритет | Каталог | Ярлык источника |
|----------|-----------|--------------|
| 1 (победа в конфликте) | `~/.hermes/plugins/<name>/dashboard/` | `user` |
| 2 | `<repo>/plugins/memory/<name>/dashboard/` | `bundled` |
| 2 | `<repo>/plugins/<name>/dashboard/` | `bundled` |
| 3 | `./.hermes/plugins/<name>/dashboard/` | `project` — только если установлен `HERMES_ENABLE_PROJECT_PLUGINS` |

Результаты обнаружения кэшируются для каждого процесса информационной панели. После добавления нового плагина:

```bash
# Force a rescan without restart
curl http://127.0.0.1:9119/api/dashboard/plugins/rescan
```

…или перезапустите `hermes dashboard`.

#### Жизненный цикл загрузки плагина

1. Панель мониторинга загружается. `main.tsx` предоставляет SDK на `window.__HERMES_PLUGIN_SDK__` и реестр на `window.__HERMES_PLUGINS__`.
2. `App.tsx` вызывает `usePlugins()` → получает `GET /api/dashboard/plugins`.
3. Для каждого манифеста: внедряется CSS `<link>` (если он объявлен), затем тег `<script>` загружает пакет JS.
4. IIFE плагина запускается и вызывает `window.__HERMES_PLUGINS__.register(name, Component)` — и, возможно, `.registerSlot(name, slot, Component)` для каждого слота.
5. Панель мониторинга сопоставляет зарегистрированный компонент с манифестом, добавляет вкладку к навигации (кроме `hidden`) и монтирует компонент как маршрут.

У плагинов есть до **2 секунд** после загрузки скрипта для вызова `register()`. После этого панель мониторинга перестанет ждать и завершит первоначальный рендеринг. Если плагин позже зарегистрируется, он все равно появится — навигация будет реактивной.

Если скрипт плагина не загружается (404, синтаксическая ошибка, исключение во время IIFE), панель мониторинга записывает предупреждение в консоль браузера и продолжает работу без него.

---

## Комбинированная тема + демо-версия плагина

Плагин [`strike-freedom-cockpit`](https://github.com/NousResearch/hermes-example-plugins/tree/main/strike-freedom-cockpit) (сопутствующий репозиторий `hermes-example-plugins`) представляет собой полную демоверсию с измененным скином. Он объединяет тему YAML с плагином, предназначенным только для слотов, для создания HUD в стиле кабины без разделения панели управления.

**Что это демонстрирует:**

- Полная тема с использованием палитры, типографики, `fontUrl`, `layoutVariant: cockpit`, `assets`, `componentStyles` (зазубренные углы карточек, градиентный фон), `colorOverrides` и `customCSS` (наложение развертки).
- Плагин только для слотов (`tab.hidden: true`), который регистрируется в трех слотах:
  - `sidebar` — панель MS-STATUS со строками телеметрии в реальном времени, управляемая `SDK.api.getStatus()`.
  - `header-left` — герб фракции с надписью `--theme-asset-crest` из активной темы.
  - `footer-right` — пользовательский слоган, заменяющий строку организации по умолчанию.
- Плагин считывает предоставленные темой изображения через переменные CSS, поэтому замена тем меняет героя/герб без изменения кода плагина.

**Установить:**

```bash
git clone https://github.com/NousResearch/hermes-example-plugins.git

# Theme
cp hermes-example-plugins/strike-freedom-cockpit/theme/strike-freedom.yaml \
   ~/.hermes/dashboard-themes/

# Plugin
cp -r hermes-example-plugins/strike-freedom-cockpit ~/.hermes/plugins/
```

Откройте панель управления и выберите **Strike Freedom** в переключателе тем. Появится боковая панель кабины, в заголовке появится герб, а слоган заменяет нижний колонтитул. Вернитесь к **Hermes Teal**, и плагин останется установленным, но невидимым (слот `sidebar` отображается только в варианте макета `cockpit`).

Прочтите исходный код плагина (`strike-freedom-cockpit/dashboard/dist/index.js` в сопутствующем репозитории), чтобы узнать, как он читает переменные CSS, защищает от старых панелей мониторинга без поддержки слотов и регистрирует три слота из одного пакета.

---

## Справочник по API

### Конечные точки темы

| Конечная точка | Метод | Описание |
|----------|--------|-------------|
| `/api/dashboard/themes` | ПОЛУЧИТЬ | Список доступных тем + активное имя. Встроенные функции возвращают `{name, label, description}`; Пользовательские темы также включают поле `definition` с полностью нормализованным объектом темы. |
| `/api/dashboard/theme` | ПУТЬ | Установить активную тему. Тело: `{"name": "midnight"}`. Сохраняется до `config.yaml` под `dashboard.theme`. |

### Конечные точки плагина

| Конечная точка | Метод | Описание |
|----------|--------|-------------|
| `/api/dashboard/plugins` | ПОЛУЧИТЬ | Список обнаруженных плагинов (с манифестами, без внутренних полей). |
| `/api/dashboard/plugins/rescan` | ПОЛУЧИТЬ | Принудительно повторно просканировать каталоги плагинов без перезапуска. |
| `/dashboard-plugins/<name>/<path>` | ПОЛУЧИТЬ | Предоставляйте статические ресурсы из каталога `dashboard/` плагина. Обход пути заблокирован. |
| `/api/plugins/<name>/*` | * | Серверные маршруты, зарегистрированные в плагине. |

### SDK на `window`

| Глобальный | Тип | Провайдер |
|--------|------|----------|
| `window.__HERMES_PLUGIN_SDK__` | объект | `registry.ts` — React, перехватчики, компоненты пользовательского интерфейса, клиент API, утилиты. |
| `window.__HERMES_PLUGINS__.register(name, Component)` | функция | Зарегистрируйте основной компонент плагина. |
| `window.__HERMES_PLUGINS__.registerSlot(name, slot, Component)` | функция | Зарегистрируйтесь в именованном слоте оболочки. |

---

## Поиск неисправностей

**Моя тема не отображается в средстве выбора.**
Убедитесь, что файл находится в `~/.hermes/dashboard-themes/` и заканчивается на `.yaml` или `.yml`. Обновите страницу. Запустите `curl http://127.0.0.1:9119/api/dashboard/themes` — в ответе должна быть ваша тема. Если в YAML есть ошибка синтаксического анализа, панель мониторинга регистрируется в `errors.log` под `~/.hermes/logs/`.

**Вкладка моего плагина не отображается.**
1. Убедитесь, что манифест находится по адресу `~/.hermes/plugins/<name>/dashboard/manifest.json` (обратите внимание на подкаталог `dashboard/`).
2. `curl http://127.0.0.1:9119/api/dashboard/plugins/rescan` для принудительного повторного обнаружения.
3. Откройте инструменты разработки браузера → Сеть — подтвердите `manifest.json`, `index.js` и любой CSS, загруженный без ошибок 404.
4. Откройте инструменты разработки браузера → Консоль — найдите ошибки во время IIFE или `window.__HERMES_PLUGINS__ is undefined` (означает, что SDK не инициализировался, обычно это сбой при рендеринге React ранее).
5. Убедитесь, что ваш пакет вызывает `window.__HERMES_PLUGINS__.register(...)` с тем же именем**, что и `manifest.json:name`.

**Компоненты, зарегистрированные в слотах, не отображаются.**
Слот `sidebar` отображается только в том случае, если активная тема имеет `layoutVariant: cockpit`. Другие слоты всегда рендерятся. Если вы регистрируетесь в слоте без обращений, добавьте `console.log` внутри `registerSlot`, чтобы убедиться, что пакет плагина вообще запущен.

**Внутренние маршруты плагина возвращают 404.**
1. Убедитесь, что в манифесте `"api": "plugin_api.py"` указывает на существующий файл внутри `dashboard/`.
2. Перезапустите `hermes dashboard` — маршруты API плагина монтируются один раз при запуске, а не при повторном сканировании.
3. Убедитесь, что `plugin_api.py` экспортирует `router = APIRouter()` уровня модуля. Другие экспортные названия не подбираются.
4. Хвост `~/.hermes/logs/errors.log` для `Failed to load plugin <name> API routes` — туда записываются ошибки импорта.

**Изменение темы приводит к удалению переопределения цвета.**
`colorOverrides` привязаны к активной теме и очищаются при переключении темы — так задумано. Если вы хотите, чтобы переопределения сохранялись, поместите их в YAML-файл вашей темы, а не в живой переключатель.

**Тема customCSS обрезается.**
Блок `customCSS` ограничен 32 КиБ на тему. Разделите большие таблицы стилей на несколько тем или переключитесь на плагин, который вставляет полную таблицу стилей через поле `css` (без ограничения размера).

**Я хочу выпустить плагин для PyPI.**
Плагины информационной панели устанавливаются по макету каталога, а не по точке входа в pip. Самый чистый путь распространения на сегодняшний день — это репозиторий git, который пользователь клонирует в `~/.hermes/plugins/`. Установщик на основе pip для плагинов информационной панели в настоящее время не подключен.