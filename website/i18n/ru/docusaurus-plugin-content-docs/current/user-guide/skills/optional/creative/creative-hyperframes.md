---
title: Гиперкадры — рендеринг видео MP4/WebM из HTML-композиций.
sidebar_label: Hyperframes
description: Рендеринг видео MP4/WebM из HTML-композиций
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Гиперкадры

Рендеринг видео MP4/WebM из HTML-композиций.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/creative/hyperframes` |
| Путь | `optional-skills/creative/hyperframes` |
| Версия | `1.0.0` |
| Автор | хейген-ком |
| Лицензия | Апач-2.0 |
| Платформы | Linux, MacOS, Windows |
| Теги | `creative`, `video`, `animation`, `html`, `gsap`, `motion-graphics` |
| Сопутствующие навыки | [`manim-video`](/docs/user-guide/skills/bundled/creative/creative-manim-video), [`meme-generation`](/docs/user-guide/skills/optional/creative/creative-meme-generation) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Гиперфреймы

HTML — источник правды для видео. Композиция представляет собой HTML-файл с атрибутами `data-*` для определения времени, временной шкалой GSAP для анимации и CSS для внешнего вида. Механизм HyperFrames захватывает страницу покадрово и кодирует в MP4/WebM с помощью FFmpeg.

**Дополнение к `manim-video`:** Используйте `manim-video` для математических/геометрических пояснений (уравнений в стиле 3B1B). Используйте `hyperframes` для анимационной графики, говорящей головы с подписями, экскурсий по продукту, социальных наложений, переходов шейдеров и всего, что связано с реальными видео/аудиоматериалами.

## Когда использовать

- Пользователь запрашивает визуализированное видео из текста, сценария или веб-сайта.
- Анимированные заставки, нижние трети или типографские вступления.
- Видео с субтитрами (TTS + субтитры, синхронизированные с формой волны)
- Аудио-реактивные визуальные эффекты (синхронизация ритма, полосы спектра, пульсирующее свечение)
- Переходы между сценами (перекрестное затухание, вытеснение, деформация шейдера, вспышка через белый цвет)
- Социальные наложения (стиль Instagram/TikTok/YouTube)
- Конвейер перехода от веб-сайта к видео (захват URL-адреса, создание промо-материала)
- Любая анимация HTML/CSS/JS, которая должна детерминированно отображаться в видеофайле.

**Не** используйте этот навык для:
- Чистая математика/анимация уравнений (→ `manim-video`)
- Генерация изображений или мемов (→ `meme-generation`, модели изображений)
- Живая видеоконференция или потоковое вещание

## Краткий справочник

```bash
npx hyperframes init my-video               # scaffold a project
cd my-video
npx hyperframes lint                        # validate before preview/render
npx hyperframes preview                     # live-reload browser preview (port 3002)
npx hyperframes render --output final.mp4   # render to MP4
npx hyperframes doctor                      # diagnose environment issues
```

Флаги рендеринга: `--quality draft|standard|high` · `--fps 24|30|60` · `--format mp4|webm` · `--docker` (воспроизводимо) · `--strict`.

Полная ссылка на CLI: [references/cli.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/cli.md).

## Настройка (однократно)

```bash
bash "$(dirname "$(find ~/.hermes/skills -path '*/hyperframes/SKILL.md' 2>/dev/null | head -1)")/scripts/setup.sh"
```

Сценарий:
1. Проверяет, что Node.js >= 22 и FFmpeg установлены (если нет, печатает инструкции по исправлению).
2. Устанавливает интерфейс командной строки `hyperframes` глобально (`npm install -g hyperframes@>=0.4.2`).
3. Предварительно кэширует `chrome-headless-shell` с помощью Puppeteer — **обязательно** для рендеринга наилучшего качества через путь захвата `HeadlessExperimental.beginFrame` Chrome.
4. Запускает `npx hyperframes doctor` и сообщает результат.

См. [references/troubleshooting.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/troubleshooting.md), если установка не удалась.

## Процедура

### 1. Планируйте перед написанием HTML

Прежде чем прикасаться к коду, сформулируйте на высоком уровне:
- **Что** — сюжетная линия, ключевые моменты, эмоциональные моменты.
- **Структура** — композиции, треки (видео/аудио/наложения), длительность.
- **Визуальная идентичность** — цвета, шрифты, характер движения (взрывной/кинематографический/плавный/технический)
- **Герой кадр** — для каждой сцены момент, когда одновременно видно наибольшее количество элементов. Это статический макет, который вы создадите первым.

**Ворота визуальной идентификации (HARD-GATE).** Прежде чем писать ЛЮБОЙ состав HTML, необходимо определить визуальную идентичность. НЕ пишите композиции со стандартными или общими цветами (`#333`, `#3b82f6`, `Roboto` означают, что этот шаг был пропущен). Проверка по порядку:

1. **`DESIGN.md` в корне проекта?** → Используйте точные цвета, шрифты, правила движения и ограничения «Чего НЕ делать».
2. **Пользователь назвал стиль** (например, «Swiss Pulse», «мрачный и технологичный», «люксовый бренд»)? → Создайте минимальный `DESIGN.md` с помощью `## Style Prompt`, `## Colors` (3–5 шестнадцатеричных символов с ролями), `## Typography` (1–2 семейства), `## What NOT to Do` (3–5 антишаблонов).
3. **Ничего из вышеперечисленного?** → Прежде чем писать HTML, задайте 3 вопроса:
   - Настроение? (взрывной/кинематографический/жидкий/технический/хаотичный/теплый)
   - Светлый или темный холст?
   - Есть ли фирменные цвета, шрифты или визуальные отсылки?

   Затем сгенерируйте `DESIGN.md` из ответов. Каждая композиция должна прослеживать свою палитру и типографику до `DESIGN.md` или явного указания пользователя.

### 2. Подмости

```bash
npx hyperframes init my-video --non-interactive
```

Шаблоны: `blank`, `warm-grain`, `play-mode`, `swiss-grid`, `vignelli`, `decision-tree`, `kinetic-type`, `product-promo`, `nyt-graph`. Передайте `--example <name>`, чтобы выбрать один, `--video clip.mp4` или `--audio track.mp3`, чтобы засеять его носителем.

### 3. Макет перед анимацией

Напишите статический HTML+CSS для **сначала главного кадра** — GSAP пока нет. Контейнер `.scene-content` должен заполнить сцену (`width:100%; height:100%; padding:Npx`) `display:flex` + `gap`. Используйте отступы для перемещения содержимого внутрь — никогда не используйте `position: absolute; top: Npx` в контейнере содержимого (содержимое переполняется, если его высота превышает оставшееся пространство).

Только после того, как главный кадр станет правильным, добавьте входы `gsap.from()` (анимируйте **до** позиции CSS) и выходы `gsap.to()` (анимируйте **из** него).

Полную схему атрибутов данных и правила композиции см. в [references/composition.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/composition.md).

### 4. Анимация с помощью GSAP

Каждая композиция должна:
- Зарегистрируйте его хронологию: `window.__timelines["<composition-id>"] = tl`.
- Старт на паузе: `gsap.timeline({ paused: true })` — проигрыватель управляет воспроизведением
- Используйте конечные значения `repeat` (нет `repeat: -1` — нарушается механизм захвата). Вычислите: `repeat: Math.ceil(duration / cycleDuration) - 1`.
- Будьте детерминистичны — никакой логики `Math.random()`, `Date.now()` или настенных часов. Используйте начальный ГПСЧ, если вам нужна псевдослучайность.
- Сборка синхронная — никаких `async`/`await`, `setTimeout` или обещаний относительно построения временной шкалы.

См. [references/gsap.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/gsap.md) для получения информации об основном API GSAP (анимация, замедление, разнесение, сроки).

### 5. Переходы между сценами

Многосценные композиции требуют переходов. Правила:
1. **Всегда используйте переходы между сценами** — без переходов.
2. **Всегда используйте анимацию входа** для каждого элемента сцены (`gsap.from(...)`).
3. **Никогда не используйте анимацию выхода**, кроме финальной сцены: переход ЯВЛЯЕТСЯ выходом.
4. Финальная сцена может исчезнуть.

Используйте `npx hyperframes add <transition-name>` для установки переходов шейдера (`flash-through-white`, `liquid-wipe` и т. д.). Полный список: `npx hyperframes add --list`.

### 6. Аудио, субтитры, TTS, аудиореактивность, выделение

– **Аудио:** всегда отдельный элемент `<audio>` (видео — `muted playsinline`).
- **ТТС:** `npx hyperframes tts "Script text" --voice af_nova --output narration.wav`. Список голосов с помощью `--list`. Первая буква голосового идентификатора кодирует язык (`a`/`b`=английский, `e`=испанский, `f`=французский, `j`=японский, `z`=мандарин и т. д.) — CLI автоматически определяет локаль фонемайзера; передать `--lang` только для переопределения. Для неанглийской фонемизации требуется `espeak-ng`, установленный во всей системе.
- **Подписи:** `npx hyperframes transcribe narration.wav` → расшифровка на уровне слов. Выберите стиль стенограммы (хайповый / корпоративный / обучающий / сторителлинг / социальный — см. таблицу в `references/features.md`). **Правило языка:** никогда не используйте модели `.en` шепота, если аудио не подтверждено на английском языке — `.en` переводит неанглоязычный звук, а не расшифровывает его. Каждая группа подписей ДОЛЖНА иметь жесткое уничтожение `tl.set(el, { opacity: 0, visibility: "hidden" }, group.end)` после выхода анимации — в противном случае группы будут видны в более поздних группах.
- **Визуальные эффекты, реагирующие на звук:** предварительно извлекаются звуковые диапазоны (низкие, средние и высокие частоты) и производится покадровая выборка внутри временной шкалы с помощью цикла `for` из `tl.call(draw, [], f / fps)` — один длинный анимационный ролик НЕ реагирует на звук. Карта бас → `scale` (импульс), высокие частоты → `textShadow`/`boxShadow` (свечение), общая амплитуда → `opacity`/`y`/`backgroundColor`. Избегайте клише в виде эквалайзера — позвольте контенту управлять визуальным эффектом, а звук — его поведением.
- **Выделение в стиле маркера:** эффекты выделения, круга, взрыва, каракулей и эскизов для выделения текста являются детерминированными CSS+GSAP — см. `references/features.md#marker-highlighting`. Полностью доступен для поиска, без анимированных SVG-фильтров.
- **Переходы сцен:** в каждой композиции из нескольких сцен ДОЛЖНЫ использоваться переходы (без переходов). Выбирайте примитивы CSS (перемещение слайда, размытие, плавное затухание, масштабирование, шахматные блоки) или переходы шейдеров (`flash-through-white`, `liquid-wipe`, `cross-warp-morph`, `chromatic-split` и т. д.) с помощью `npx hyperframes add`. Таблицы настроения и энергии живут в `references/features.md#transitions`. Не смешивайте переходы CSS и шейдеров в одной композиции.

### 7. Анализ, проверка, проверка, предварительный просмотр, рендеринг

```bash
npx hyperframes lint              # catches missing data-composition-id, overlapping tracks, unregistered timelines
npx hyperframes validate          # WCAG contrast audit at 5 timestamps
npx hyperframes inspect           # visual layout audit — overflow, off-frame elements, occluded text
npx hyperframes preview           # live browser preview
npx hyperframes render --quality draft --output draft.mp4    # fast iteration
npx hyperframes render --quality high --output final.mp4     # final delivery
```

`hyperframes validate` производит выборку пикселей фона за каждым текстовым элементом и предупреждает, если коэффициент контрастности ниже 4,5:1 (или 3:1 для большого текста). `hyperframes inspect` — это компаньон на стороне макета — запускает страницу с несколькими временными метками и помечает проблемы, которые не видит статический анализатор (заголовок, который обходит безопасную область только через 4,5 с, карточка, которая переполняется, когда ее заголовок является самым длинным вариантом, элемент, который оказывается за шейдером перехода). Используйте `inspect`, особенно для композиций с надписями, карточками, подписями или плотной типографикой.

### 8. Преобразование веб-сайта в видео (если пользователь указывает URL-адрес)

Используйте 7-этапный рабочий процесс захвата видео в [references/website-to-video.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/website-to-video.md): захват → DESIGN.md → SCRIPT.md → раскадровка → композиция → рендеринг → доставка.

## Подводные камни

- **`HeadlessExperimental.beginFrame' wasn't found`** — Chromium 147+ удалил этот протокол. Убедитесь, что вы используете `hyperframes@>=0.4.2` (автоматически обнаруживается и возвращается в режим снимков экрана). Аварийный люк: `export PRODUCER_FORCE_SCREENSHOT=true`. См. [hyperframes#294](https://github.com/heygen-com/hyperframes/issues/294) и [references/troubleshooting.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/troubleshooting.md).
- **Системный Chrome (не `chrome-headless-shell`)** — рендеринг зависает на 120 секунд, а затем происходит тайм-аут. Запустите `npx puppeteer browsers install chrome-headless-shell` (это делает setup.sh). `hyperframes doctor` сообщает, какой двоичный файл будет использоваться.
- **`repeat: -1` где угодно** — ломает механизм захвата. Всегда рассчитывайте конечное количество повторений.
- **`gsap.set()` для элементов клипа, которые появляются позже** — элемент не существует при загрузке страницы. Вместо этого используйте `tl.set(selector, vars, timePosition)` внутри временной шкалы, после `data-start` клипа.
- **`<br>` внутри текста содержимого** — принудительные разрывы не учитывают отображаемую ширину шрифта, поэтому естественный перенос + `<br>` выполняет двойные разрывы. Используйте `max-width`, чтобы разрешить перенос текста. Исключение: короткие отображаемые заголовки, в которых каждое слово намеренно находится на отдельной строке.
- **Анимация `visibility` или `display`** — GSAP не может их анимировать. Используйте `autoAlpha` (управляет видимостью и непрозрачностью).
- **Вызов `video.play()` или `audio.play()`** — воспроизведение принадлежит платформе. Никогда не звоните им сами.
- **Асинхронное построение временных шкал** – механизм захвата считывает `window.__timelines` синхронно после загрузки страницы. Никогда не переносите построение временной шкалы в `async`, `setTimeout` или обещание.
- **Автономный `index.html`, завернутый в `<template>`** — скрывает весь контент из браузера. Только **подкомпозиции**, загруженные через `data-composition-src`, используют `<template>`.
- **Использование видео вместо аудио** — всегда отключен звук `<video>` + отдельный `<audio>`.

## Проверка

До и после рендеринга:

1. **Lint + проверка + проверка:** `npx hyperframes lint --strict && npx hyperframes validate && npx hyperframes inspect` (lint выявляет структурные проблемы, проверка выявляет контрастность, проверка выявляет проблемы с визуальным макетом/переполнением — см. Troubleshooting.md, если появляются предупреждения).
2. **Хореография анимации** — при появлении новых композиций или значительных изменений анимации запустите карту анимации. `npx hyperframes init` копирует сценарии навыков в проект, поэтому путь является локальным для проекта:
   ```bash
   node skills/hyperframes/scripts/animation-map.mjs <composition-dir> \
     --out <composition-dir>/.hyperframes/anim-map
   ```
   Выводит один `animation-map.json` со сводками для каждой анимации, временной шкалой ASCII Ганта, обнаружением смещения, мертвыми зонами (>1 с без анимации), жизненными циклами элементов и флагами (`offscreen`, `collision`, `invisible`, `paced-fast` &lt;0,2 с, `paced-slow` >2 с). Сканируйте сводки и флажки — исправьте или обоснуйте каждую. Пропустите небольшие правки.
3. **Файл существует + ненулевое значение:** `ls -lh final.mp4`.
4. **Продолжительность соответствует `data-duration`:** `ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 final.mp4`.
5. **Визуальная проверка**: извлеките средний кадр композиции: `ffmpeg -i final.mp4 -ss 00:00:05 -vframes 1 preview.png`.
6. **Аудио присутствует, если ожидается:** `ffprobe -v error -show_streams -select_streams a -of default=nw=1:nk=1 final.mp4 | head -1`.

В случае сбоя `hyperframes render` запустите `npx hyperframes doctor` и прикрепите его выходные данные к отчету.

## Ссылки

- [composition.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/composition.md) — атрибуты данных, контракт временной шкалы, не подлежащие обсуждению правила, типографика/правила активов
- [cli.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/cli.md) — каждая команда CLI (инициализация, захват, анализ, проверка, проверка, предварительный просмотр, рендеринг, расшифровка, tts, доктор, браузер, информация, обновление, тест)
- [gsap.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/gsap.md) — основной API GSAP для HyperFrames (анимация, замедление, разнесение, временные шкалы, matchMedia)
- [features.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/features.md) — субтитры, TTS, аудиореактивность, подсветка маркеров, переходы (загрузка по требованию)
- [website-to-video.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/website-to-video.md) — 7-этапный рабочий процесс преобразования видео в видео
- [troubleshooting.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/hyperframes/references/troubleshooting.md) — исправление OpenClaw, переменные env, распространенные ошибки рендеринга