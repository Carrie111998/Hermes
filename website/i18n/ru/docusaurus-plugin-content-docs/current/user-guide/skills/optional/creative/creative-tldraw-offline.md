---
title: Tldraw Offline — управление автономными холстами tldraw и создание сценариев
  с помощью агента.
sidebar_label: Tldraw Offline
description: Управляйте и создайте сценарии для создания автономных холстов с помощью
  агента
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Tldraw в автономном режиме

Управляйте и создайте сценарии для создания автономных холстов с помощью агента.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/creative/tldraw-offline` |
| Путь | `optional-skills/creative/tldraw-offline` |
| Версия | `1.0.0` |
| Автор | Текниум + Гермес Агент |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `tldraw`, `canvas`, `whiteboard`, `document-script`, `diagramming` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# tldraw офлайн-навык

Работайте с настольным офлайн-приложением tldraw (offline.tldraw.com): прочтите открытый
Canvas, вносить изменения и писать **сценарии документов** — JavaScript, встроенный в
`.tldraw`, который запускается при загрузке и обеспечивает устойчивое поведение файла. Приложение
запускает **локальный HTTP API** (по умолчанию `localhost:7236`), которым управляет агент кодирования.
с помощью обычного `curl` со своего терминала — именно так выглядит домашняя страница приложения.
демо (редактирование холста в Кодексе в реальном времени) работает. Агент НЕ использует использование компьютера/
Щелчок графического пользовательского интерфейса и НЕ редактирует вручную файл `.tldraw` напрямую. Держите tldraw
оффлайн открыт, пока вы работаете.

## Когда использовать

- Пользователь открыл tldraw в автономном режиме и просит вас создать или изменить холст.
  (схемы, каркасы, макеты).
- Вы хотите добавить к рисунку устойчивое поведение (реактивные формы, интерактивные
  кнопки, анимация, логика подключения) через встроенный скрипт документа.

НЕ размещайте фигуры вручную, чтобы имитировать рисунок — напишите код, который генерирует
их. Агенты гораздо лучше пишут сценарии на холсте, чем рисуют на нем.

## Предварительные условия

- **tldraw offline установлен и работает**, с открытым документом. Релизы:
  https://github.com/tldraw/tldraw-offline/releases/latest (macOS DMG, Windows
  x64/Arm64, Linux `x86_64`/`arm64` AppImage или amd64/arm64 `.deb`).
- **Навыки агента, установленные в приложении**: `Develop → Install Agent Skills`.
  приложение записывает свой собственный навык tldraw в `~/.codex/skills/`, `~/.claude/skills/`,
  `~/.cursor/skills/` и `~/.gemini/skills/` — обучение этого агента `curl`
  рецепты ниже. (Этот навык Гермеса отражает это руководство для Гермеса.)
- **API локального управления.** При запуске приложение записывает `server.json` в свою конфигурацию.
  каталог (Linux `~/.config/tldraw/`, macOS `~/Library/Application Support/tldraw/`,
  Windows `%APPDATA%\tldraw\`) с `port` (по умолчанию `7236`), носителем `token`,
  `pid` и `startedAt`. Каждый запрос, кроме `GET /`, требует
  `Authorization: Bearer <token>`. Чистый выход удаляет `server.json`; если это
  присутствует, но порт не отвечает, приложение некорректно завершило работу — считайте, что нет
  бег.
- **Перечитывайте порт + токен при КАЖДОМ вызове оболочки.** Каждый вызов терминала — это новый вызов.
  оболочки, поэтому токен `export`ed не сохраняется — «экспортировать один раз и повторно использовать» отправляет
  пустой токен и 401. Прочтите обе строки в верхней части каждого вызова:
  `PORT=$(jq -r .port <server.json>); TOKEN=$(jq -r .token <server.json>)`.
- Для локального редактирования не требуется учетная запись или сеть.

## Как бежать

Два разных рабочих процесса. Выберите, должно ли изменение пережить перезагрузку.

**А. Разовые правки холста (`/exec`)** — макет, создание фигур, очистка. Это
это живое редактирование, а не сохраненный скрипт:

```bash
BASE=http://localhost:7236
TOKEN=$(python3 -c "import json;print(json.load(open('$HOME/.config/tldraw/server.json'))['token'])")
# find the focused document id
DOC=$(curl -s "$BASE/api/search" -X POST -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"code":"return (await api.getFocusedDoc()).id"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['result'])")
# run code with the live `editor` + `helpers` in scope
curl -s "$BASE/api/doc/$DOC/exec" -X POST -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"code":"const {createShapeId,toRichText}=await import(\"tldraw\"); editor.createShape({id:createShapeId(),type:\"geo\",x:0,y:0,props:{geo:\"rectangle\",w:200,h:100,color:\"blue\",fill:\"solid\",richText:toRichText(\"hello\")}}); return editor.getCurrentPageShapes().length"}'
```

**Б. Устойчивое поведение (`script/main.js`)** — реактивная/интерактивная логика, которая должна
пережить перезагрузку. Отредактируйте файл на диске; наблюдатель приложения применяет его:

```bash
# get the live script file path for the doc
curl -s "$BASE/api/doc/$DOC/script-workspace" -X POST \
  -H "Authorization: Bearer $TOKEN"          # -> result.mainJsPath, result.isDefaultScript
# edit result.mainJsPath with read_file / patch / write_file (see scripts/main.js)
# then confirm the watcher applied it:
curl -s "$BASE/api/doc/$DOC/script-status" -H "Authorization: Bearer $TOKEN"
```

Готовый к адаптации скрипт документа — `scripts/main.js`.

## Краткий справочник

Контракт документ-скрипт (проверенный на соответствие пакету приложения)
`script-context.d.ts`):

```js
import { createShapeId, toRichText } from 'tldraw'   // primitives: import, not globals

export default function ({ editor, helpers, signal }) {
  editor.run(() => {                                 // batch = one undo step
    helpers.createShapeIfMissing({                   // idempotent furniture
      id: createShapeId('node-1'), type: 'geo', x: 0, y: 0,
      props: { geo: 'rectangle', w: 200, h: 100, richText: toRichText('hi') },
    })
  })

  const stop = editor.store.listen(() => { /* react */ })  // fires the tick AFTER a commit
  signal.addEventListener('abort', () => stop())           // REQUIRED cleanup on rerun/close
}
```

- `ctx.editor` — живой `Editor` (`createShape`, `updateShape`, `deleteShapes`,
  `getCurrentPageShapes`, `getShape`, `getBindingsFromShape`, `zoomToFit`,
  `on('tick'|'event', fn)`, `run(fn, { history: 'ignore' })`).
- `ctx.helpers` — `createShapeIfMissing`, `createShapesIfMissing`,
  `createArrowBetweenShapes(from, to, { arrowheadEnd })`, `translateShapes`,
  `onShapeTranslate(id, fn, { signal })`, `richTextToPlainText`, `boxShapes`,
  `getLints`.
- `ctx.signal` — `AbortSignal`; прикрепите к нему каждый прослушиватель/интервал.
- `config.js` (отдельный файл) регистрирует пользовательские утилиты формы/инструмента/компонента и
  запускается перед монтированием; `main.js` запускается с смонтированным редактором и повторяется при сохранении.

## Интерактивный пользовательский интерфейс (нажимаемые кнопки, которые управляют состоянием)

Нарисованные фигуры могут вести себя как настоящее приложение, чего не может сделать статическая доска.
Полный пример: `scripts/counter.js` (числовой дисплей + кнопки МИНУС/СБРОС/ПЛЮС).

Граница проверки — прочтите это, прежде чем утверждать, что взаимодействие работает или нет.
В СОБСТВЕННОМ агентском справочнике приложения указано, что необходимо проверить сценарий кликабельного пользовательского интерфейса с помощью «одного
имитация щелчка и чтение одного состояния» через `/exec` (`editor.dispatch` указатель
событие, дождитесь тика, прочтите состояние фигуры) — НЕ управляя настоящей мышью. Автор
этот стандарт счетчик проверен: отправленные клики шагнули вперед
`0 → 1 → 2 → 1 → 0`. Два предостережения, которые стоит записать:
- **Скрипт запускается только после того, как его применит наблюдатель за файлами приложения.** В Linux это
  наблюдатель использует inotify; хост с изнуренным `fs.inotify.max_user_instances`
  журналы `inotify_add_watch ... No space left on device`, `script-status` показывают
  `state: "not-watching"` / `hasEntry: false`, и сценарий никогда не выполняется.
  Это ограничение хоста, а не ошибка скрипта — обычный рабочий стол не затрагивается.
- **Управление холстом с использованием компьютера требует доставки ПЕРЕДНЕГО ПЛАНА, а не
  фон.** возврат доставки по умолчанию `background` cua-driver
  `background_unavailable` против окклюдированного рендерера Electron — но это
  первая ступенька, а не стена. cua-driver возвращает подсказку `escalation: "foreground"`;
  установите `delivery_mode: "foreground"` (в паре с `bring_to_front`) и его X11 XTest
  путь (`x11_xtest_fg`) щелкает по земле — это отменяет согласие «Выполнить сценарий»
  диалоговое окно и щелкает холст. Проверено командой Cua именно для этой версии 1.11.0.
  Изображение приложения (Linux/X11). НЕ делайте вывод: «Электрон отвергает синтетические щелчки» и
  отказаться от фонового режима — подняться на передний план. (Реальный путь продукта
  по-прежнему `/exec`, щелчков нет; это примечание предназначено для тестирования с использованием компьютера.)

Узор:

```js
export default function ({ editor, helpers, signal }) {
  // 1. Build buttons idempotently; tag each with meta so the handler finds them.
  //    Give buttons a visible label AND a meta.action.
  // 2. Hit-test pointer_down in PAGE coordinates against the button bounds:
  const inside = (b, p) => p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h
  function onEvent(info) {
    if (!info || info.name !== 'pointer_down') return
    let p = null
    try { if (info.point && editor.screenToPage) p = editor.screenToPage(info.point) } catch {}
    p = p ?? editor.inputs?.currentPagePoint
    if (!p) return
    const hit = editor.getCurrentPageShapes().find(
      (s) => s.meta?.ui === 'button' &&
        inside({ x: s.x, y: s.y, w: s.props.w, h: s.props.h }, p)
    )
    if (hit) runAction(hit.meta.action)   // mutate state; store it in a shape's meta
  }
  editor.on('event', onEvent)
  signal.addEventListener('abort', () => editor.off('event', onEvent))  // REQUIRED
}
```

– Найти кнопки по `meta` (или видимую метку по `helpers.richTextToPlainText`),
  не по жестко запрограммированным координатам.
- **Один скрипт управляет сборкой и чтением.** Если фигуры созданы одним кодом
  путь (с `meta.action: 'inc'`), и обработчик читает другое соглашение
  (`meta.action === 'PLUS'`), щелчки без звука ничего не дают. Отправьте готовые кнопки
  с помощью того же сценария, который их обрабатывает, или отправьте пустой холст, чтобы сценарий
  создает их заново — никогда не записывайте заранее несовпадающие формы в базу данных файла.
– Сохраняйте состояние приложения в `meta` фигуры (например, `meta.count`) и визуализируйте его так.
  метку `richText` формы, поэтому она сохраняется при сохранении и может быть прочитана для проверки.
- **Отключение прослушивателя при прерывании `signal`.** Пропуск этого шага не является косметическим: включен
  при следующем сохранении старый `onEvent` остается прикрепленным рядом с новым, поэтому каждый раз
  щелчок срабатывает дважды, и счетчик подскакивает на 2 вместо 1.
- Для непрерывного движения используйте `editor.on('tick', fn)`; для движущегося якоря с
  прикрепленные фрагменты используют `helpers.onShapeTranslate(id, fn, { signal })`.

### Доставка самозапускаемого сценария `.tldraw`

`.tldraw` — это почтовый индекс `metadata.json` + `session.json` + `db.sqlite` + `assets/`.
+ `script/` (упаковываются только эти записи). Чтобы скрипт автоматически запускался без
диалоговое окно согласия «Этот документ содержит сценарий → Запустить сценарий»:

- `metadata.json` должен содержать манифест `script`: `{ "sha256": "<digest>" }`, где
  дайджест представляет собой `sha256` по каждому отсортированному пути `script/` как `` `${path}\0${sha256hex(bytes)}\n` ``.
  Несоответствие отклоняется как подделанное.
– Предварительно доверьтесь дайджесту, добавив его в `~/.tldraw/script-trust.json`.
  (`{ "trusted": ["<digest>"] }` или `$TLDRAW_SCRIPT_TRUST`). Приложение пропускает согласие
  когда `isScriptTrusted(digest)` истинно.

## Процедура

1. Считайте текущий токен/порт из `server.json`. Найдите целевой документ с помощью
   `api.getFocusedDoc()` (или `api.getDocs()`); назовите его явно, если их несколько
   открытый.
2. Для макета/генерации используйте `/exec`. Для устойчивого поведения отредактируйте
   `script/main.js` через `/script-workspace`.
3. Сделайте скрипты идемпотентными: создавайте устойчивые фигуры с помощью `helpers.createShapeIfMissing`.
   и стабильные идентификаторы `createShapeId('name')`. Скрипты перезапускаются при каждой загрузке.
4. Не допускайте записи, принадлежащие сценарию, из стека отмены действий пользователя:
   `editor.run(fn, { history: 'ignore' })` (или `helpers.translateShapes`, который
   уже делает).
5. Для реактивности `editor.store.listen(cb)` и уничтожьте его при отмене `signal`.
   Для взаимодействия `editor.on('event', h)` (хит-тест `pointer_down` на странице
   координаты); для анимации — `editor.on('tick', h)`.
6. Для одного подвижного анкера + прикрепленных внутренних устройств отдавайте предпочтение
   `helpers.onShapeTranslate(anchorId, fn, { signal })` в большом магазине
   слушатель — широкий слушатель может превратить ваши собственные записи в петли обратной связи.

## Свойства формы (проверено на соответствие схеме tldraw SDK v5)

`editor.createShape` / `createShapeIfMissing` принимают частичные реквизиты (утилиты формы
заполнить значения по умолчанию). При создании **необработанных записей** для снимка файла каждый реквизит
требуется ниже (запустите `scripts/validate_shapes.mjs`):

| Форма | Необходимый реквизит |
|-------|----------------|
| `note` | `richText`, `color`, `labelColor`, `size`, `font`, `align`, `verticalAlign`, `growY`, `fontSizeAdjustment`, `url`, `scale`, `textLastEditedBy` |
| `text` | `richText`, `color`, `size`, `font`, `textAlign`, `w`, `scale`, `autoSize` |
| `frame` | `w`, `h`, `name`, `color` |
| `geo` | `geo`, `w`, `h`, `color`, `fill`, `richText` (+ тире/размер/и т. д. по умолчанию) |

`richText` должно быть `toRichText('...')` — пустая строка отклоняется. `color` перечисление:
`черный серый светло-фиолетовый фиолетовый синий голубой желтый оранжевый зеленый светло-зеленый
светло-красный красный белый`. `font` enum: `рисовать без засечек моно`.

## Подводные камни

- **`store.listen` срабатывает ПОСЛЕ фиксации, а не синхронно.** Если вы
  напишите форму и немедленно прочитайте состояние, ожидая, что слушатель запустится, это
  нет. Проверено в реальном времени: поочередное чтение показывает 0 пожаров; через один `setTimeout`
  отметьте, что отображается 1. По той же причине, по которой заметки приложения `editor.dispatch` являются асинхронными — дождитесь
  отметьте перед проверкой.
- **`ctx`, а не глобальные переменные.** Запись — `экспортировать функцию по умолчанию ({ editor,
  помощники, сигнал })`. There is no bare `editor` global в скрипте документа.
  `createShapeId` / `toRichText` / `Vec` происходят из `import ... from 'tldraw'`.
– **`richText`, а не `text`.** Для текстовых/примечаний/географических меток используется `richText: toRichText(s)`.
- **Необработанным записям нужна каждая опора; `createShape` нет.** В приложении передаются только
  реквизит, который вам дорог; для созданного вручную снимка `.tldraw` требуется полный набор (таблица).
- **Скрипты повторяются при каждой загрузке — будьте идемпотентны.** Используйте `createShapeIfMissing`
  со стабильными идентификаторами, иначе вы дублируете контент и блокируете пользовательские правки.
- **Уборка `signal`.** `signal.addEventListener('abort', () => stop())` за
  каждые `store.listen` / `editor.on` / `setInterval`; сигнал срабатывает раньше
  повторный запуск и закрытие.
- **Не допускать записи скриптов при отмене:** `editor.run(fn, { history: 'ignore' })`.
- **`editor.on('tick')` приостанавливается, когда окно скрыто** (это цикл RAF);
  `setInterval` продолжает стрелять, но Электрон в фоновом режиме снижает ее до ~1/с.
- **API требуется токен носителя** от `server.json`; порт может быть не по умолчанию
  (`server.listen(0)` выбирает один) — всегда читайте файл, не прописывайте `7236` жестко.
- **Только импорт `tldraw` / `react` / `react-dom`** — не проект Node.

## Проверка

- **Схема формы (офлайн, без приложения):** `node scripts/validate_shapes.mjs` — строит
  реальная схема tldraw и проверяет заметку/текст/фрейм. Проходящие отпечатки `3/3`.
- **Редактирование холста в реальном времени:** после `/exec` читайте снова с помощью `/api/search` →
  `api.getShapes(docId)` (возвращает `{ page, viewport, shapes }`) и
  `api.getBindings(docId)` (массив). Убедитесь, что ожидаемые формы/привязки существуют. Захватить
  `api.getScreenshot(docId)` (возвращает `{ filePath, ... }`) и проверьте PNG/JPEG.
  с `vision_analyze`.
- **Применён долговечный скрипт:** `GET /api/doc/:id/script-status`. Успех – это
  `state: "applied"` (`currentDiskDigest === lastAppliedDigest === manifestSha256`,
  `pendingApply === false`, `lastApplyError === null`). Если останется `"pending"`
  после короткой повторной попытки сообщите об этом вместо сообщения об успехе; `"error"` означает
  применить не удалось — прочитайте `errorLogPath`.