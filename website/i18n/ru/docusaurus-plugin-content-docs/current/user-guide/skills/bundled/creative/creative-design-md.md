---
title: Design Md — создание, проверка и экспорт файлов спецификаций токена Google
  DESIGN.md.
sidebar_label: Design Md
description: Создание, проверка и экспорт файлов спецификаций токена Google DESIGN.md.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

#Дизайн Мд

Создание, проверка и экспорт файлов спецификаций токена Google DESIGN.md.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/creative/design-md` |
| Версия | `1.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `design`, `design-system`, `tokens`, `ui`, `accessibility`, `wcag`, `tailwind`, `dtcg`, `google` |
| Сопутствующие навыки | [`popular-web-designs`](/docs/user-guide/skills/bundled/creative/creative-popular-web-designs), [`claude-design`](/docs/user-guide/skills/bundled/creative/creative-claude-design), [`excalidraw`](/docs/user-guide/skills/bundled/creative/creative-excalidraw), [`architecture-diagram`](/docs/user-guide/skills/bundled/creative/creative-architecture-diagram) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# DESIGN.md Навык

DESIGN.md — это открытая спецификация Google (Apache-2.0, `google-labs-code/design.md`) для
описание визуальной идентичности агентам кодирования. Один файл объединяет:

- **Вступительная часть YAML** — машиночитаемые токены проектирования (нормативные значения).
- **Тело Markdown** — удобочитаемое обоснование, организованное в канонические разделы.

Токены дают точные значения. Проза рассказывает агентам, почему* эти ценности существуют и как их достичь.
применять их. Структура CLI (`npx @google/design.md`) + контраст WCAG,
сравнивает версии для регрессий и экспортирует в Tailwind или W3C DTCG JSON.

## Когда использовать этот навык

- Пользователь запрашивает файл DESIGN.md, токены дизайна или спецификацию системы дизайна.
- Пользователь хочет единообразный пользовательский интерфейс/бренд в нескольких проектах или инструментах.
- Пользователь вставляет существующий файл DESIGN.md и запрашивает его анализ, сравнение, экспорт или расширение.
- Пользователь просит перенести руководство по стилю в формат, который могут использовать агенты.
- Пользователь хочет проверить контрастность/доступность WCAG для своей цветовой палитры.

Для чисто визуального вдохновения или примеров макетов используйте `popular-web-designs`.
вместо этого. Для *обработки и вкуса* при разработке одноразового HTML-артефакта.
с нуля (прототип, колода, лендинг, лаборатория компонентов), использовать
`claude-design`. Этот навык предназначен для самого *формального файла спецификации*.

## Анатомия файла

```md
---
version: alpha
name: Heritage
description: Architectural minimalism meets journalistic gravitas.
colors:
  primary: "#1A1C1E"
  secondary: "#6C7278"
  tertiary: "#B8422E"
  neutral: "#F7F5F2"
typography:
  h1:
    fontFamily: Public Sans
    fontSize: 3rem
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  body-md:
    fontFamily: Public Sans
    fontSize: 1rem
rounded:
  sm: 4px
  md: 8px
  lg: 16px
spacing:
  sm: 8px
  md: 16px
  lg: 24px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "#FFFFFF"
    rounded: "{rounded.sm}"
    padding: 12px
  button-primary-hover:
    backgroundColor: "{colors.primary}"
---

## Overview

Architectural Minimalism meets Journalistic Gravitas...

## Colors

- **Primary (#1A1C1E):** Deep ink for headlines and core text.
- **Tertiary (#B8422E):** "Boston Clay" — the sole driver for interaction.

## Typography

Public Sans for everything except small all-caps labels...

## Components

`button-primary` is the only high-emphasis action on a page...
```

## Типы токенов

| Тип | Формат | Пример |
|------|--------|---------|
| Цвет | любой цвет CSS (шестнадцатеричный, `rgb()`, `oklch()`, именованный) | `"#1A1C1E"`, `"oklch(62% 0.18 250)"` |
| Размерность | число + единица измерения (`px`, `em`, `rem`) | `48px`, `-0.02em` |
| Ссылка на токен | `{path.to.token}` | `{colors.primary}` |
| Типография | объект с `fontFamily`, `fontSize`, `fontWeight`, `lineHeight`, `letterSpacing`, `fontFeature`, `fontVariation` | см. выше |

Белый список свойств компонента: `backgroundColor`, `textColor`, `typography`,
`rounded`, `padding`, `size`, `height`, `width`. Варианты (наведение, активный,
нажаты) представляют собой **отдельные записи компонентов** со связанными именами клавиш.
(`button-primary-hover`), не вложенный.

## Канонический порядок разделов

Разделы не являются обязательными, но существующие должны располагаться именно в этом порядке.
линтер помечает разделы не в порядке (`section-order`, предупреждение) и дублирует
заголовки — потребители согласно спецификации отклоняют дубликаты, поэтому исправьте оба перед
возврат файла.

1. Обзор (псевдоним: Бренд и стиль)
2. Цвета
3. Типографика
4. Макет (псевдоним: Макет и интервал)
5. Высота и глубина (псевдоним: Высота)
6. Формы
7. Компоненты
8. Что можно и чего нельзя делать

Неизвестные разделы сохраняются, не содержат ошибок. Принимаются неизвестные имена токенов
если тип значения допустим. Неизвестные свойства компонента выдают предупреждение.

## Рабочий процесс: создание нового DESIGN.md

1. **Спросите пользователя** (или сделайте вывод) об тоне бренда, цветовом акценте и типографике.
   направление. Если они предоставили сайт, изображение или атмосферу, переведите их на
   форму токена выше.
2. **Запишите `DESIGN.md`** в корне проекта, используя `write_file`. Всегда
   включить `name:` и `colors:`; другие разделы необязательны, но приветствуются.
3. **Используйте ссылки на токены** (`{colors.primary}`) в разделе `components:`.
   вместо повторного ввода шестнадцатеричных значений. Сохраняет единый источник палитры.
4. **Соберите** (см. ниже). Исправьте все неработающие ссылки или сбои WCAG.
   перед возвращением.
5. **Если у пользователя есть существующий проект**, также напишите Tailwind или DTCG.
   экспортируется рядом с файлом (`tailwind.theme.json`, `tokens.json`).

## Рабочий процесс: lint/diff/export

CLI — `@google/design.md` (узел). Используйте `npx` — глобальная установка не требуется.

```bash
# Validate structure + token references + WCAG contrast
npx -y @google/design.md lint DESIGN.md

# Compare two versions, fail on regression (exit 1 = regression)
npx -y @google/design.md diff DESIGN.md DESIGN-v2.md

# Export to Tailwind v3 theme JSON (`tailwind` is a back-compat alias)
npx -y @google/design.md export --format json-tailwind DESIGN.md > tailwind.theme.json

# Export to a Tailwind v4 CSS @theme block (--color-*, --text-*, --radius-*, ...)
npx -y @google/design.md export --format css-tailwind DESIGN.md > theme.css

# Export to W3C DTCG (Design Tokens Format Module) JSON
npx -y @google/design.md export --format dtcg DESIGN.md > tokens.json

# Print the spec itself — useful when injecting into an agent prompt
npx -y @google/design.md spec --rules-only --format json
```

Все команды принимают `-` в качестве стандартного ввода. `lint` возвращает выход 1 при ошибках (предупреждения
один выход 0). `export` выходит из 0 при успешном экспорте независимо от ворса
выводы в исходном коде — запустите `lint` отдельно, чтобы получить их. Выход:
JSON по умолчанию; проанализируйте его, если вам нужно структурировать результаты.

В Windows имя контейнера `design.md` может конфликтовать с файлом `.md`.
ассоциация (тихий отказ или файл открывается в редакторе). Используйте формат без точек
псевдоним: `npx -y -p @google/design.md designmd lint DESIGN.md`.

### Ссылка на правила Lint (9 правил, начиная с CLI 0.3.0)

- `broken-ref` (ошибка) — `{colors.missing}` указывает на несуществующий токен
- `contrast-ratio` (предупреждение) — компонент `textColor` vs `backgroundColor`
  ниже WCAG AA (4,5:1)
- `missing-primary` (предупреждение) — цвета определены, но нет токена `primary`.
- `missing-typography` (предупреждение) — цвета определены, но нет маркеров типографики.
- `orphaned-tokens` (предупреждение) — цветные токены, на которые никогда не ссылается компонент.
- `section-order` (предупреждение) — разделы не в каноническом порядке
- `unknown-key` (предупреждение) — ключ YAML верхнего уровня, который выглядит как опечатка.
  ключ схемы (`colours:` → `colors:`); пользовательские ключи расширения молчат
- `token-summary`, `missing-sections` (информация) — учитывается и отсутствует по желанию.
  разделы

Если пользователь заботится о доступности, явно укажите это в своем
Резюме — выводы WCAG являются наиболее весомой причиной использования CLI.

## Подводные камни

- **Не вкладывать варианты компонентов.** `button-primary.hover` неверно;
  `button-primary-hover` как родственный ключ верен.
- **Шестнадцатеричные цвета должны быть заключены в кавычки.** В противном случае YAML захлебнется `#` или
  странным образом обрезать значения типа `#1A1C1E`.
- **Отрицательные размеры также нуждаются в кавычках.** `letterSpacing: -0.02em` анализирует как
  поток YAML — напишите `letterSpacing: "-0.02em"`.
- **Порядок разделов имеет значение, хотя линтер только предупреждает.** Если пользователь
  дает вам прозу в случайном порядке, измените ее порядок, чтобы он соответствовал каноническому списку
  перед сохранением — этого ожидают потребители, соответствующие спецификациям.
- **Опечатки в подсвойстве «Типографика» автоматически удаляются.** Начиная с CLI 0.3.0a.
  опечатка типа `fontwight:` не дает результатов, и значение исчезает из
  экспорт — дважды проверьте имена подсвойств на соответствие схеме.
  (`fontFamily`, `fontSize`, `fontWeight`, `lineHeight`, `letterSpacing`,
  `fontFeature`, `fontVariation`).
- **`version: alpha` — текущая версия спецификации** (по состоянию на июль 2026 г., CLI
  0.3.0). Спецификация помечена как альфа — следите за критическими изменениями.
- **Ссылки на токены разрешаются по пунктирному пути.** `{colors.primary}` работает;
  `{primary}` нет.

## Спецификация источника истины

- Репозиторий: https://github.com/google-labs-code/design.md (Apache-2.0)
- CLI: `@google/design.md` в npm.
- Лицензия на сгенерированные файлы DESIGN.md: независимо от того, что использует проект пользователя;
  сама спецификация — Apache-2.0.