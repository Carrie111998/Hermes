---
sidebar_label: Desktop Plugin SDK
title: SDK плагина для рабочего стола (@hermes/plugin-sdk)
description: Расширьте собственное приложение Hermes Desktop — панели, страницы, навигацию
  на боковой панели, строку состояния, команды палитры, привязки клавиш, темы и ограниченное
  внутреннее пространство имен с помощью одного импорта и без этапа сборки.
---

# SDK плагина для рабочего стола

Родное приложение [Hermes Desktop](/user-guide/desktop) ориентировано на вклад: каждый
поверхность в окне — панели, маршруты, навигация на боковой панели, элементы строки состояния, палитра
записи, привязки клавиш, темы — регистрируются в одном центральном реестре. Основные регистры
его внешний вид точно такой же, как у плагина, поэтому история плагина реальна,
это не запоздалая мысль.

**Плагин рабочего стола** — это отдельный файл ESM, который по умолчанию экспортирует `HermesPlugin`.
Он импортирует один модуль — `@hermes/plugin-sdk` — и получает все:
живое состояние, дверь JSON-RPC шлюза, ограниченное пространство имен REST/сокетов,
React Query и собственный набор пользовательского интерфейса приложения, поэтому пользовательский интерфейс плагина по умолчанию выглядит как родной. Нет
клон репо, без `npm run build`, без исправлений исходного кода приложения. Перетащите файл в
`$HERMES_HOME/desktop-plugins/<id>/plugin.js`, и приложение загрузит его за считанные секунды.
и горячая перезагрузка каждого сохранения.

:::предупреждение Это не SDK плагина веб-панели
«Плагин» означает несколько несвязанных между собой вещей в Hermes. Эта страница является **родной
SDK настольного приложения** (`hermes desktop`) — модуль `@hermes/plugin-sdk` и
`$HERMES_HOME/desktop-plugins/`. **Веб-панель** (`hermes dashboard`) имеет
своя собственная, несвязанная система плагинов на `window.__HERMES_PLUGIN_SDK__` с
`manifest.json` — задокументировано по адресу
[Расширение информационной панели](/user-guide/features/extending-the-dashboard). Питон
Плагины CLI/шлюза описаны в [Создать плагин Hermes](/developer-guide/plugins).
У этих троих нет общего кода, API или доставки. Только серверная часть `plugin_api.py`
Пространство имен (`/api/plugins/<id>`) используется совместно SDK рабочего стола и информационной панели.
:::

## Ментальная модель

SDK следует модели модуля VS Code. Автор плагина импортирует ровно один
модуль и никогда не затрагивает внутренние компоненты приложения (они защищены от ворса из комплекта поставки).
плагин и не удается разрешить его в дисковом плагине). Возможности делятся на уровни:

- **`host.state.*`** — просмотр текущего состояния приложения только для чтения (nanostore
  атомы): активный сеанс, индикатор занятости для каждого сеанса, cwd, состояние сокета шлюза,
  модель, профиль, видовое окно. `gateway` — это WebSocket, а не режим занятости.
– **`host.*` действия** — тщательно подобранные безопасные глаголы: тост, навигация, хвостовые журналы,
  перезапустите шлюз, подпишитесь на поток событий шлюза.
- **`host.request`** — дверь JSON-RPC шлюза: сеансы, конфиг, навыки,
  cron — все, что вызывает само приложение.
- **`ctx.rest` / `ctx.socket`** — собственное пространство имен вашего плагина.
  (`/api/plugins/<id>`), если вы отправляете `plugin_api.py`.
- **`ui.*`** — язык дизайна: реальные компоненты приложения, переменные темы,
  значки и средства форматирования, чтобы ваш пользовательский интерфейс соответствовал пиксель в пикселях приложения.

## Два режима доставки

| Режим | Где | Кто | Шаг сборки |
|------|-------|-----|------------|
| **Диск** (рекомендуется) | `$HERMES_HOME/desktop-plugins/<id>/plugin.js` | пользователи, агенты | none — простой ESM, загруженный в некомпилированном виде |
| **Единый пакет** | `$HERMES_HOME/plugins/<id>/desktop/plugin.js` | плагины, которые также доставляют код на стороне агента | нет — тот же дисковый конвейер |
| **В комплекте** | `apps/desktop/src/plugins/<id>/plugin.tsx` | в дереве, поставляется вместе с приложением | собственная сборка приложения Vite |

Все три используют один и тот же контракт `HermesPlugin`, который отображается в **Настройки → Плагины**.
и включить/отключить прямой эфир. Унифицированный пакет — это просто сканирование дверцы диска изнутри
папка плагина вашего агента — см.
[Один пакет, оба SDK](#one-package-both-sdks). Все на этой странице есть
написано на дверце диска (то, что пишете вы и агент);
[Плагины в комплекте](#bundled-plugins) отмечают два
различия. На сегодняшний день плагины рабочего стола в основном дереве не поставляются — справочные демоверсии
жить в компаньоне
[`hermes-example-plugins`](https://github.com/NousResearch/hermes-example-plugins)
репо.

## Быстрый старт — ваш первый плагин

Создайте `$HERMES_HOME/desktop-plugins/hello/plugin.js` (это `~/.hermes/...`
по умолчанию или `~/.hermes/profiles/<name>/...` в именованном профиле). Папка
имя должно соответствовать плагину `id`.

```javascript
// ~/.hermes/desktop-plugins/hello/plugin.js
import { host, haptic, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

function HelloPane() {
  const gateway = useValue(host.state.gateway)

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 p-3 text-sm',
    children: [
      jsx('div', { className: 'font-medium', children: 'Hello, Hermes' }),
      jsx('div', {
        className: 'text-(--ui-text-tertiary)',
        children: `gateway: ${gateway}`
      })
    ]
  })
}

export default {
  id: 'hello', // must match the folder name
  name: 'Hello',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'hello',
      data: { placement: 'right', width: '260px' },
      render: () => jsx(HelloPane, {})
    })
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 130,
      render: () =>
        jsx('button', {
          type: 'button',
          className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
          onClick: () => {
            haptic('tap')
            host.notify({ kind: 'info', message: 'Hello from my plugin!' })
          },
          children: 'hello'
        })
    })
  }
}
```

Сохраните это. Приложение просматривает `desktop-plugins/`, загружает файл в течение нескольких секунд,
и горячая перезагрузка каждого последующего сохранения на месте. Если он не появился, запустите ⌘K →
**Перезагрузите плагины рабочего стола**. Если загрузка не удалась, тост сообщает об ошибке — исправьте и
сохранитесь еще раз.

:::note Нет JSX, нет сборки
Файл на диске загружается **некомпилированным**, поэтому синтаксис JSX не будет анализироваться. Написать пользовательский интерфейс
при звонках `jsx()` / `jsxs()` с `react/jsx-runtime` (или `React.createElement`).
Единственными импортируемыми спецификаторами являются `@hermes/plugin-sdk`, `react` и
`react/jsx-runtime` — все остальное намеренно не разрешается.
:::

## Контракт плагина

Плагин по умолчанию экспортирует `HermesPlugin`:

```ts
interface HermesPlugin {
  /** Stable slug — becomes the `plugin:<id>` source and the id namespace. */
  id: string
  /** Human name for Settings / about UI. Defaults to `id`. */
  name?: string
  /** Registers on load when the user hasn't chosen (default true). Set false
   *  for opt-in plugins: they inventory in Settings ▸ Plugins, off until the
   *  user flips the switch. */
  defaultEnabled?: boolean
  /** Called once at load; wire contributions through `ctx`. */
  register: (ctx: PluginContext) => void
}
```

`register` получает **с областью действия** `PluginContext`. Он никогда не касается реестра
напрямую — происхождение контекстных автотегов (`source: 'plugin:<id>'`) и
пространства имен каждого идентификатора вклада (`<id>:<localId>`), поэтому два плагина никогда не смогут
столкнуться.

```ts
interface PluginContext {
  /** Resolved source tag, e.g. `'plugin:hello'`. */
  readonly source: string
  /** Register one contribution (id namespaced, source stamped). Returns a disposer. */
  register: (c: PluginContribution) => () => void
  /** Register several at once; the returned disposer removes all of them. */
  registerMany: (cs: PluginContribution[]) => () => void
  /** REST to this plugin's own backend namespace (`/api/plugins/<id>`). */
  rest: <T>(path: string, opts?: PluginRestOptions) => Promise<T>
  /** Live WebSocket to this plugin's own namespace. Returns a disposer. */
  socket: (path: string, onMessage: (data: unknown) => void) => () => void
  /** The curated OS door: native notification, open-external, reveal-in-file-manager, clipboard. */
  os: PluginOs
  /** Plugin-scoped JSON persistence (keys live under `hermes.plugin.<id>.`). */
  storage: PluginStorage
}
```

**Вклад** — это примитив, общий для каждой поверхности:

```ts
interface Contribution {
  id: string          // you write the local id; the host namespaces it
  area: string        // WHERE it goes (a contribution-area constant)
  title?: string
  order?: number      // sort within the area (lower = earlier)
  when?: () => boolean // dynamic visibility; re-evaluated by the area
  enabled?: boolean
  render?: () => ReactNode  // the component to mount
  data?: unknown      // area-specific payload (see the cookbook)
}
```

Вы предоставляете `render`, `data` или оба, в зависимости от региона.

## Области вклада — кулинарная книга

Импортируйте константы площади из SDK; каждая область имеет свою собственную полезную нагрузку `data`.

| Поверхность | `area` | Вы предоставляете |
|---------|--------|-------------|
| Панель макета | `PANES_AREA` (`'panes'`) | `title` + `render` + `data: { placement, dock?, width?, height? }` |
| Полная страница | `ROUTES_AREA` | `data: { path }` + `render` |
| Боковая панель навигации | `SIDEBAR_NAV_AREA` | `data: { path, label, codicon }` |
| Строка состояния | `STATUSBAR_AREAS.left` / `.right` | `render` (или `data` как `StatusbarItem`) |
| Строка заголовка | `TITLEBAR_AREAS.left` / `.center` / `.right` | `data` как `TitlebarTool` или `<Contribute>` с областью монтирования |
| ⌘К палитра | `PALETTE_AREA` | `data: PaletteContribution` |
| Привязка клавиш | `KEYBINDS_AREA` | `data: KeybindContribution` |
| Тема | `THEMES_AREA` | `data` как `DesktopTheme` |
| Композитор | `COMPOSER_AREAS.*` | слоты рендеринга или поставщики промежуточного программного обеспечения/приложений |

### Панели

Панель — это плитка в дереве макета. `placement` — семантическая роль — панель
складывается (как вкладки) с существующими панелями этой роли; пользователь может перетащить его куда угодно
потом.

```javascript
ctx.register({
  id: 'pane',
  area: 'panes',
  title: 'my pane',
  data: { placement: 'right', width: '260px' },
  render: () => jsx(MyPane, {})
})
```

`placement` — это `'main' | 'left' | 'right' | 'top' | 'bottom'`. Чтобы приземлиться на
конкретный **край** вместо наложения добавьте жест `dock` — то же самое, что и
перетаскивание на чип панели:

```javascript
// Below the conversation, 200px tall.
data: {
  placement: 'bottom',
  dock: { pane: 'workspace', pos: 'bottom' },
  height: '200px'
}
```

`dock.pane` — любой идентификатор панели (`workspace` — основной поток; также `sessions`,
`terminal`, `files`, `review`, `logs`); `dock.pos` — это
`'top' | 'bottom' | 'left' | 'right' | 'center'`. Объявите `width`/`height` так
панель не претендует на половину зоны.

Закрытие единственной панели, созданной плагином, отключает этот плагин, что может
можно снова включить в **Настройки → Плагины**. Когда плагин вносит несколько
панели, закрытие одной из них закрывает только эту панель и оставляет другие панели плагина,
команды и промежуточное программное обеспечение активно. **Сбросить макет** восстанавливает отклоненные вклады.
стекла.

### Страницы и навигация на боковой панели

Маршрут монтирует полную страницу на панели рабочей области, как и любое встроенное представление. Соедините это
с помощью строки навигации на боковой панели (и/или команды палитры), чтобы сделать ее доступной.

```javascript
import { ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'

ctx.registerMany([
  {
    id: 'page',
    area: ROUTES_AREA,
    data: { path: '/my-page' },
    render: () => jsx(MyPage, {})
  },
  {
    id: 'nav',
    area: SIDEBAR_NAV_AREA,
    data: { path: '/my-page', label: 'My Page', codicon: 'project' }
  }
])
```

`codicon` — это [кодикон VS Code] (https://microsoft.github.io/vscode-codicons/dist/codicon.html).
идентификатор. Перейдите к маршруту из любого места с помощью `host.navigate('/my-page')`.

### Строка состояния и строка заголовка

Элементы строки состояния отображаются в левом или правом кластере нижней панели.
Простейшей является функция `render`; для простой кнопки используйте `data` в качестве
`StatusbarItem` (`{ id, label?, icon?, detail?, variant?, menuItems?, … }`).

```javascript
import { STATUSBAR_AREAS, TITLEBAR_AREAS } from '@hermes/plugin-sdk'

ctx.register({
  id: 'count',
  area: STATUSBAR_AREAS.right,
  order: 120,
  render: () => jsx(MyStatus, {})
})
```

Инструменты заголовка находятся в `TITLEBAR_AREAS.left | .center | .right` как `TitlebarTool`.
данные (`{ id, label, icon, active?, onSelect? }`).

### Команды палитры и привязки клавиш

```javascript
import { PALETTE_AREA, KEYBINDS_AREA } from '@hermes/plugin-sdk'

ctx.registerMany([
  {
    id: 'open',
    area: PALETTE_AREA,
    data: {
      id: 'my-page.open',
      label: 'Open My Page',
      keywords: ['my', 'page'],
      run: () => host.navigate('/my-page')
    }
  },
  {
    id: 'refresh',
    area: KEYBINDS_AREA,
    data: {
      id: 'my-page.refresh',
      label: 'Refresh My Page',
      category: 'My Plugin',
      defaults: ['mod+shift+r'],
      run: () => void doRefresh()
    }
  }
])
```

Привязки клавиш можно переназначать пользователем в настройках; `defaults` — это всего лишь начальная привязка.

### Темы

Вклад темы содержит полный `DesktopTheme` в качестве `data` (имя, метка,
цвета, …). Он отображается в средстве выбора тем как встроенный.

```javascript
import { THEMES_AREA } from '@hermes/plugin-sdk'

ctx.register({ id: 'noir', area: THEMES_AREA, data: myDesktopTheme })
```

При регистрации темы она отображается в списке; он не выбирает его. `useTheme()` читает
окрашенный внешний вид (`theme`, `themeName`, `availableThemes`, `resolvedMode`) и
меняет его (`setTheme`, `setMode`, `previewTheme`) из компонента:

```javascript
import { Button, useTheme } from '@hermes/plugin-sdk'

function ThemePicker() {
  const { availableThemes, setTheme, themeName } = useTheme()

  return availableThemes.map(t => (
    <Button key={t.name} disabled={t.name === themeName} onClick={() => setTheme(t.name)}>
      {t.label}
    </Button>
  ))
}
```

Переключатель, управляемый чем-то отличным от рендеринга — подключением шлюза,
событие сокета, любой обратный вызов `host.onEvent` — не имеет компонента для подвешивания перехвата
дальше. Используйте там `requestTheme(name)`. Неразрешимое имя отклоняется, а не
привязан к скину по умолчанию, поэтому возвращаемое значение удваивается по мере доступности
проверьте, и неправильное имя никогда не сможет незаметно сбросить чей-то внешний вид:

```javascript
import { host, requestTheme } from '@hermes/plugin-sdk'

host.onEvent('gateway.ready', () => {
  if (!requestTheme('noir')) {
    host.notifyError('Connected, but the noir theme is not installed.')
  }
})
```

Обе двери сохраняются в каждом профиле, поэтому переключатель, управляемый плагином, работает точно так же, как и
ручной подбор. Чтобы оттенить *активную* тему, а не заменить ее, используйте
`setAccentOverride(hex)` и очистите его в `ctx.onDispose` — входящем в комплект `accent`
Плагин — это рабочий пример.

### Расширения композитора

`COMPOSER_AREAS` (`top`, `bottom`, `leading`, `actions`, `attachments`,
`middleware`) позволить плагину добавлять элементы управления вокруг композитора сообщения, предоставлять
источник вложения или преобразовать черновик перед его отправкой (`ComposerMiddleware`
с `handler(draft) => draft | null`).

### Директивы транскрипта — встроенные компоненты, к которым обращается модель.

`TRANSCRIPT_DIRECTIVE_AREA` делает расшифровку областью вклада.
Зарегистрируйте именованную директиву, и агент сможет визуализировать ваш компонент в режиме реального времени.
сообщение помощника, отправив абзац формы `::name{key="value"}`:

```javascript
import { TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'

ctx.register({
  id: 'task-card',
  area: TRANSCRIPT_DIRECTIVE_AREA,
  data: {
    name: 'task', // the model writes ::task{id="BB-12"}
    render: ({ attrs, streaming }) => jsx(TaskCard, { taskId: attrs.id, streaming })
  }
})
```

Правила, которые соблюдает хост, чтобы поверхность оставалась безопасной:

- Директива должна представлять собой **весь абзац** — `::name` остается в середине текста.
  прозу, поэтому компоненты плагина никогда не смогут перехватить работающий текст.
– Атрибуты представляют собой **ненадежные выходные данные модели** (пары `key="value"`, только строки).
  Подтвердите свои собственные поля; ничего не визуализировать на мусоре, а не гадать.
- Директива **невостребованная** (для этого имени не зарегистрирован плагин) отображается как
  простой абзац, каким он всегда был — ничего не ломается, когда плагин выключен.
- Рендеры заключены в границу ошибки вклада: бросок ухудшается до
  встроенный чип ошибки, а не мертвое сообщение.
- Первая регистрация выигрывает в случае конфликта имен; авантюрные имена пространства имен
  с вашим пулеметом (`myplugin-board`, а не `board`).

Core отправляет одну директиву в качестве эталонного потребителя: `::preview{file="…"}`.
отображает HTML-файл рабочей области **живым внутри сообщения** — изолированный
`srcdoc` iframe с непрозрачным происхождением (скрипты запускаются и виджет полностью
интерактивный; нет доступа к приложению, его хранилищу или мосту). Рамка
размеры соответствуют содержимому (высота в реальном времени, ширина, взятая из содержимого
внутренний диапазон, сдвинутый по левому краю потока сообщений) и руки-прелюдии темы.
документ, в котором разрешены токены приложения (`--foreground`, `--muted-foreground`,
`--accent`, `--border`, `--card`), шрифт приложения и прозрачный
фон — поэтому HTML-код в форме виджета читается как нативный, в то время как вся страница сохраняется.
свой собственный дизайн. Цели, не относящиеся к HTML, и удаленные шлюзы возвращаются к
классическая карта предварительного просмотра. Сообщите агенту о вашей директиве в навыке (это
как он учится его излучать).

Предварительно просмотренные виджеты также могут **ответить**. Внутри кадра,
`window.hermes.send('get-price eth')` (или декларативный
`<button data-hermes-send="get-price eth">` — скрипт не нужен) руки это
запрос агенту при повороте пользователя, за пределами экрана: ни один пузырь не занимает
расшифровка, обновление виджета является видимым ответом. очередь все еще
реальный — он будит агента, использует правила управления/очереди композитора и
сохраняется (введено `hidden`), поэтому возобновите работу, и база данных сеанса сохранит полную запись.
Подсказки обрезаются, ограничиваются 500 символами и сокращаются до одного в секунду.
за кадр.

### Chrome с областью монтирования (`Contribute`)

`ctx.register` предназначен для **постоянных** взносов. Когда хром должен жить и
умереть с компонентом, который уже находится на экране (собственный элемент управления в строке заголовка страницы).
уходит, когда страница размонтируется), вместо этого визуализируйте `<Contribute>` внутри нее:

```javascript
import { Contribute, TITLEBAR_AREAS } from '@hermes/plugin-sdk'

jsx(Contribute, {
  area: TITLEBAR_AREAS.center,
  id: 'my-page:switcher', // namespace with your slug
  children: jsx(MySwitcher, {})
})
```

Он регистрируется при монтировании и автоматически удаляется при размонтировании.

## API хоста

Все, что есть на `host`, доступно из любого места плагина. Атомы состояния
только для чтения — чтение с помощью `.get()` в обработчиках, подписка с помощью `useValue(atom)` в
компоненты.

```ts
host.state.activeSessionId  // ReadableAtom<string | null>
host.state.awaitingResponse // ReadableAtom<boolean>  true until the first assistant payload
host.state.busy             // ReadableAtom<boolean>  focused chat is working after a send
host.state.busyBySession    // ReadableAtom<Record<string, boolean>>  runtime id → mid-turn
host.state.focusedSessionId // ReadableAtom<string | null>  (runtime id of the FOCUSED session — tile-aware; prefer for session.* RPC)
host.state.focusedSessionProfile // ReadableAtom<string>  (owner profile of the focused chat — prefer over `profile` for per-bot/profile readouts)
host.state.focusedStoredSessionId // ReadableAtom<string | null>  (durable id — navigation / session-list matching)
host.state.focusedUsage     // ReadableAtom<UsageStats | null>  (live streamed usage of the focused session, no RPC needed)
host.state.cwd              // ReadableAtom<string>
host.state.gateway          // ReadableAtom<string>  socket state ('idle' | 'connecting' | 'open' | …)
host.state.model            // ReadableAtom<string>
host.state.profile          // ReadableAtom<string>
host.state.viewport         // ReadableAtom<{ width, height, narrow }>
```

`host.state.gateway` — это соединение WebSocket, а не то, включен ли поворот в чате.
бег. Сеанс может находиться в середине хода, пока сокет равен `open`; еще один сеанс
одновременно может простаивать. Отключите действия композитора или плагина из
**сосредоточенный сеанс** занят (`host.state.busyBySession[sessionId]` или что-то вроде этого)
сеанса `view.$busy`) — никогда из `gateway` и никогда из глобального процесса.
занятый флаг.

```ts
host.notify({ kind, message, title?, detail?, action? })  // toast; returns id
host.notifyError(error, fallbackMessage)                   // toast an error
ctx.os.notify({ title, body?, silent?, icon?, activate?, onActivate?, actions? })
                                           // native OS notification (attributed to your plugin)
ctx.os.openExternal(url)                   // OS default handler (browser, mail, spotify:) → Promise<boolean>
ctx.os.revealPath(path)                    // reveal in Finder / Explorer → Promise<boolean>
ctx.os.writeClipboard(text)                // system clipboard → Promise<boolean>
host.navigate('/route')                    // hash-route navigation
host.openSession(id, { profile?, intent? }) // open a stored session core-style;
                                           //   profile: soft-swap to that profile's backend first
                                           //   intent: 'in-place' (default) | 'stack' | 'tab' | 'window'
host.newChat(profile?)                     // fresh chat draft, optionally in another profile
host.openWorkspace(id, { render, title?, minWidth?, onClose? })
                                           // dock a plugin-rendered tab into the MAIN
                                           //   workspace zone and reveal it; returns a disposer
host.paneVisibility(paneId)                // ReadableAtom<boolean> — is a contributed pane
                                           //   actually on screen (its zone's active tab)?
host.onEvent(type, fn)                     // gateway event stream ('*' = all); returns disposer
host.logs(...)                             // tail an app log file
host.status()                              // one-shot system status snapshot
host.restartGateway()                      // restart the backend gateway
host.profileRoutes()                       // [{ profile, targetProfile, connectionId, mode }]
host.requestProfile<T>(route, method, params?)   // registry-routed RPC; no foreground swap
host.requestProfile<T>(profile, method, params?) // legacy v1/local overload
host.request<T>(method, params?)           // active-gateway JSON-RPC — the real power
```

`host.request` — это тот же JSON-RPC, который использует само приложение (сеансы, конфигурация, навыки,
cron, канбан, …). `host.requestProfile` принимает дескриптор от
`host.profileRoutes()` и направляет этот RPC через его точный источник реестра и
профиль без изменения активного чата или шлюза. Перегрузка только для профиля
сохраняется только для топологии «единственно-локальная/устаревшая»; Плагины с поддержкой реестра должны пройти
дескриптор, чтобы два источника, предоставляющие одно и то же имя профиля, не могли конфликтовать.

`host.openWorkspace(id, { render, title?, minWidth?, onClose? })` пристыковывается
вид, визуализируемый плагином, в **основную зону рабочего пространства** — та же самая центральная область
использование плиток сеанса и предварительного просмотра — как вкладка и ее отображение. Повторный вызов его с помощью
тот же `id` обновляет содержимое на месте и меняет вкладку вместо
открываю дубликат. Закрытие вкладки (контроллер закрытия вкладки или ⌘W) разрывает
регистрация прекращается и активируется ваш `onClose`; возвращенный диспоузер закрывает его
программно. Обнаружьте его (`typeof host.openWorkspace ===
'function'`) и вернуться к обычной добавленной панели на старом рабочем столе.
сборки — групповые чаты в режиме бота являются эталонным потребителем (главное окно
поглощение, если оно доступно, в противном случае — просмотр на панели).

`host.paneVisibility(paneId)` возвращает реактивный атом только для чтения, то есть `true`.
в то время как добавленная панель фактически находится на экране: присутствует в дереве макета,
не закрыт и не скрыт, его зона не свернута и удерживает свою зону активной
слот для вкладок (единственная панель в отдельной зоне считается). Идентификатор — это
Идентификатор области вклада, `<pluginId>:<paneId>`. Атомы запоминаются для каждого идентификатора,
поэтому вызывать его при рендеринге безопасно. Используйте его для регистрации сопутствующего пользовательского интерфейса только в то время, когда
ваша панель видна — панель Cronjobs в режиме бота является эталонным потребителем: она
регистрируется, пока панель «Боты» содержит вкладку боковой панели, и отменяет регистрацию, когда
пользовательские вкладки обратно в сеансы. Обнаружение функций на старых настольных компьютерах
(`typeof host.paneVisibility === 'function'`) и вернуться к
всегда регистрируемое поведение.

`host.profileRoutes()` инвентаризирует каждый зарегистрированный источник в текущем соединении
реестр. Источники SSH с подключением по требованию предоставляют начальное значение `default` без учетных данных.
маршрутизировать без открытия туннеля, поэтому плагин может быть первым вызывающим абонентом, который их наберет;
SSH `remoteProfile` остается серверной частью маршрута `targetProfile`. `connectionId`
— идентификатор маршрутизации реестра;
соедините его с `profile` для ключей и устойчивости. Конечная точка, токен, хост/ключ SSH и
другие поля необработанного соединения никогда не пересекают границу IPC плагина. `profile` — это
используется локальный маршрут источника
для запросов; `targetProfile` — это внутренний профиль Hermes, обслуживаемый этим маршрутом.
Они различаются, когда маршрут явно сопоставляется с другим профилем серверной части (например,
переопределение SSH `remoteProfile` или устаревший псевдоним URL-адреса для каждого профиля). Это различие
сохраняет идентичность серверной части, не раскрывая секреты соединения.

Плагины в форме профиля также получают первоклассные методы:
`profiles.list` (каждый профиль + его последний разговор как
`last_session`; передайте `include_sessions: false`, чтобы пропустить базу данных для каждого профиля
зонд; пройти `preferred_session_ids: { profileName: sessionId }` для
точный, проверенный на существование поиск одного закрепленного сеанса для каждого профиля — каждый
именованная строка получает сводку `preferred_session`, которая разрешает скрытые строки
и линии сжатия к их живому наконечнику или `null`, если идентификатор равен
окончательно ушел; старые шлюзы игнорируют этот параметр и опускают поле)
и `profiles.create` (`name`, `description`, `clone_from`,
`clone_all`, `no_skills`, `soul`, дополнительный контакт `model` + `provider`) —
ws-близнецы маршрутов `/api/profiles` REST панели мониторинга.
`host.state.busy` — это прямая трансляция целенаправленного чата (обдумывание и трансляция).
`host.state.awaitingResponse` остается верным с момента отправки до первого помощника.
полезная нагрузка. Оба следят за чатом, на который на самом деле смотрит пользователь.
плитка сеанса, когда вы удерживаете фокус, в противном случае — основной чат рабочей области (тот же
сигнализирует о том, что в строке состояния отображается импульс занятости). Подпишитесь на компонент:

```javascript
const busy = useValue(host.state.busy)
```

Для получения подробной информации на уровне токена прослушайте `host.onEvent` (`message.start`,
`message.delta`, `message.complete`).

`host.onEvent` транслирует события шлюза в реальном времени (разницы в сообщениях,
жизненный цикл сеанса, активность инструмента). Слушатели изолированы.
прослушиватель не может повлиять на отправку приложения. Каждая дверь `host` асинхронно безопасна: синхронный бросок
из внутреннего помощника (например, без моста рабочего стола в обычном браузере) становится
отказ, который видит ваш `.catch()`, никогда не приводит к сбою на границе ошибки.

`ctx.os` — это курируемая дверь ОС — каждый способ выхода плагина за пределы приложения
window, в одном пространстве имен, присвоенном вашему плагину. `ctx.os.notify` публикует
**собственное уведомление ОС** — тот же конвейер Electron, что и приложение.
Использование предупреждений об одобрении/повороте. Он срабатывает только тогда, когда пользователь находится вдали от Гермеса.
(фоновый/несфокусированный); используйте `host.notify` для всплывающего уведомления в приложении, когда
они смотрят приложение. Пользователи могут отключить его для каждого устройства в настройках ▸
Уведомления ▸ «Уведомления плагина», и повторы из одного и того же плагина
ограничено, поэтому воспринимайте это как сигнал действительно значимых событий, а не как журнал.

Богатая презентация + активация (расширяет исходную дверь `ctx.os`):

```ts
ctx.os.notify({
  title: 'New match found',
  body: 'Someone matched your signal',
  icon: '/abs/path/to/icon.png', // Electron Notification icon
  // Body click → focus Hermes + navigate. Same vocabulary as OS deep links:
  activate: 'hermes://index-network/intent/1',
  // or: activate: '/index-network/intent/1'
  // or: activate: { path: '/index-network/intent/1' }
  onActivate: () => focusLocalState('1'), // optional renderer callback
  actions: [
    { id: 'open', label: 'Open', activate: 'hermes://index-network/intent/1' },
    { id: 'dismiss', label: 'Dismiss', onAction: () => dismiss('1') },
  ],
})
```

`activate` совместим с глубокими ссылками: `hermes://index-network/intent/1` и
хэш-путь `/index-network/intent/1` разрешается в один и тот же маршрут внутри приложения (и
тот же URL-адрес `hermes://…` работает как глубокая ссылка ОС). Кнопки действий отображаются только
подписанные сборки macOS; в другом месте щелчок по телу все еще активируется. Только навигация
происходит при щелчке пользователя, а не только в результате фонового события.

Остальные двери (`openExternal`, `revealPath`, `writeClipboard`) разрешаются.
`false` вместо того, чтобы выдавать сообщение, когда эта возможность недоступна (более старые настольные версии
оболочка, обычный браузер) — переход по результату, а не прослушивание моста.

## Уровень данных — React Query + наномагазины

Плагины используют единый `QueryClient` приложения, поэтому плагин запрашивает кеш, дедупликацию,
опросить и аннулировать точно так же, как основные экраны — никогда не запускайте цикл выборки вручную.

```javascript
import { useQuery, useMutation, useQueryClient, atom, computed, useValue } from '@hermes/plugin-sdk'

function MyPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ['my-plugin', 'items'],
    queryFn: () => host.request('my.list', {})
  })
  // …
}
```

Для состояния, совместно используемого триггером и его панелью (или циклом опроса), используйте `atom` /
`computed` — тот же примитив, который использует `host.state`. Подпишитесь на лист, что
отображает значение с помощью `useValue`. Чтобы сделать запрос недействительным из **вне** React
(например, прибывает кадр `ctx.socket`), импортируйте общий `queryClient`:

```javascript
import { queryClient } from '@hermes/plugin-sdk'

ctx.socket('/events', () => {
  queryClient.invalidateQueries({ queryKey: ['my-plugin', 'items'] })
})
```

## Комплект пользовательского интерфейса и темы

Импортируйте реальные компоненты приложения напрямую, чтобы ваш пользовательский интерфейс по умолчанию был нативным:

> `Button`, `Input`, `Textarea`, `Select*`, `Switch`, `Checkbox`,
> `SegmentedControl`, `Tabs*`, `Dialog*`, `ConfirmDialog`, `DropdownMenu*`,
> `ContextMenu*`, `Popover*`, `Tip`/`Tooltip*`, `Badge`, `Kbd`/`KbdGroup`,
> `SearchField`, `ScrollArea`, `Separator`, `Skeleton`, `GlyphSpinner`, `Loader`,
> `EmptyState`, `ErrorState`, `CopyButton`, `StatusDot`, `LogView`, `Codicon`,
> `DecodeText`.

Плюс помощники: `cn` (слияние классов), `icons.*` (набор понятий приложения), `haptic`,
`profileColor` / `profileColorSoft` (детерминированные идентификационные цвета), время
форматтеры `relativeTime` / `fmtDateTime` / `fmtDayTime` / `coarseElapsed`,
`useI18n` (локализованная копия — ваш плагин остается переводимым) и
`evaluateRuntimeReadiness`.

**Стиль с переменными темы, без жестко запрограммированных цветов.** Панели уже расположены на
фон редактора приложения — оставьте фон в покое и используйте переменные для всего
еще: `var(--ui-text-secondary)`, `var(--ui-text-tertiary)`,
`var(--ui-text-quaternary)`, `var(--ui-stroke-secondary)`, `var(--ui-accent)`.
Для рисования на холсте разрешите их один раз с помощью
`getComputedStyle(canvas).getPropertyValue('--ui-accent')`. Это то, что делает
Плагин автоматически меняет облик с каждой темой.

## Серверная часть вашего плагина

Если вашему плагину требуется работа на стороне сервера, отправьте Python `plugin_api.py` и получите его.
через `ctx.rest` / `ctx.socket` — пространство имен, ограниченное вашим плагином **by
строительство**.

### Один пакет, оба SDK {#one-package-both-sdks}

Функция, для которой требуется пользовательский интерфейс рабочего стола **и** код на стороне агента (плагин Python, его
внутренние маршруты, навыки) не обязательно должны поставляться как две взаимозависимые установки.
настольное приложение также сканирует `$HERMES_HOME/plugins/<id>/` — обычный плагин-агент
root — для `desktop/plugin.js` и загружает его через тот же конвейер
в качестве отдельной дисковой двери (включая горячую перезагрузку):

```
~/.hermes/plugins/<id>/           # ONE installable folder
├── plugin.yaml                   # the agent half: tools, hooks, commands
├── skills/…
├── dashboard/
│   ├── manifest.json             # { "name": "<id>", "api": "plugin_api.py" }
│   └── plugin_api.py             # backend routes → /api/plugins/<id>/
└── desktop/
    └── plugin.js                 # the desktop half: panes, commands, ctx.rest
```

Половина `desktop/plugin.js` — это обычный дисковый плагин — тот же контракт, тот же
импорт, тот же `ctx.rest('/…')` достигает `plugin_api.py`, сидящего рядом с ним.
Установка, совместное использование или удаление функции осуществляется в одной папке.

Два переключателя включения по-прежнему применяются намеренно, и оба по умолчанию находятся в положении **выкл.**:
половина поставок для настольных компьютеров включена — он инвентаризируется в **Настройки → Плагины**, но остается
отключено до тех пор, пока пользователь не переключит его, что соответствует половине Python
`plugins.enabled` ворота в `config.yaml` (граница безопасности внизу). падение
пакет в `~/.hermes/plugins` инертен на любой поверхности, пока пользователь
говорит иначе. Половина рабочего стола изящно деградирует, когда серверная половина
off — `ctx.rest` возвращает ошибки, а не завершает работу.

:::примечание
Сканирование выполняется локально для компьютера, на котором запущено настольное приложение. Против пульта
серверной части, `~/.hermes/plugins` удаленного компьютера недоступен как файловая система —
только локально установленные пакеты вносят половину рабочего стола (то же правило, что и для
отдельная дверь).
:::

### Распространение по ссылке для установки {#install-link}

Отправьте репозиторий плагина (половину агента, половину рабочего стола или обе) и дайте ссылку на него с помощью
схема `hermes://` — простой анкор на вашем сайте или README:

```html
<a href="hermes://plugin/install?repo=owner/repo&enable=1">Install in Hermes</a>
```

Пользователь получает диалоговое окно подтверждения (идентификатор репозитория, ссылки на источники, запрос того, что
репозиторий поставляется) и выбирает компоненты до того, как что-либо будет установлено — глубокие ссылки
никогда не устанавливайте автоматически. `force=1` заменяет существующую установку; использование сборок разработчиков
`hermes-dev://`. Полная ссылка на ссылку:
[Ссылки для установки в один клик](/user-guide/features/plugins#one-click-install-links-desktop).

### Сторона Python

Плагины рабочего стола повторно используют серверное монтирование плагина информационной панели. Поместите бэкэнд в
`dashboard/` подпапку обычного плагина Hermes и объявите ее в
`manifest.json`:

```
~/.hermes/plugins/<id>/
└── dashboard/
    ├── manifest.json      # { "name": "<id>", "api": "plugin_api.py" }
    └── plugin_api.py      # exports `router = APIRouter()`
```

```python
# plugin_api.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/board")
async def board():
    return {"items": ["one", "two", "three"]}

@router.post("/action")
async def action(body: dict):
    return {"ok": True, "received": body}
```

Маршруты монтируются под `/api/plugins/<id>/` (`GET /api/plugins/<id>/board`, …).
Внутренний код выполняется внутри процесса шлюза, поэтому его можно импортировать из
непосредственно кодовую базу агента Hermes (`hermes_state`, `hermes_cli.config`, …). См.
[Расширение панели мониторинга → Маршруты API бэкенда](/user-guide/features/extending-the-dashboard#backend-api-routes)
для полной ссылки на серверную часть — монтирование идентично.

:::осторожно! Серверная часть Python закрывается отдельно.
Включение плагина на рабочем столе **Панель «Настройки → Плагины»** осуществляется на стороне рендерера.
выбор; он **не** импортирует Python. Пользовательский плагин `plugin_api.py`
импортируется только в том случае, если плагин находится в списке разрешенных `plugins.enabled` в
`config.yaml` (а не в `plugins.disabled`). Плагины проекта (`./.hermes/`)
никогда не импортируйте Python автоматически. Это граница безопасности, а не надзор
(GHSA-mcfc-hp25-cjv7).
:::

### Вызов из плагина

```javascript
register(ctx) {
  // REST — namespace-relative path.
  const load = () => ctx.rest('/board')                 // GET /api/plugins/<id>/board
  const act  = () => ctx.rest('/action', { method: 'POST', body: { go: true } })

  // Live twin — a WebSocket to your own namespace.
  const stop = ctx.socket('/events', frame => {
    queryClient.invalidateQueries({ queryKey: [ctx.source, 'board'] })
  })
}
```

`ctx.rest` учитывает профиль и отклоняет обход пути (`..`), поэтому вы никогда не сможете
обратитесь к API другого плагина или к основному маршруту через него. `PluginRestOptions` — это
`{ method?, body?, upload?: { filename, contentType?, bytes }, timeoutMs? }`.

`ctx.socket` автоматически повторно подключается с откатом до тех пор, пока не будет удален. **Это приводит к прекращению операции.
на удаленных устройствах OAuth** (одноразовые билеты WS управляются ядром) — рассматривайте сокет как
ускоритель по опросам, никогда не замена. Каждому потребителю нужен опрос
в любом случае запасной вариант, поскольку любой сокет может упасть.

Для данных всего шлюза (а не вашего собственного пространства имен) используйте `host.request` (JSON-RPC) и
Вместо этого `host.onEvent` (поток событий шлюза).

## Настройки, состояние включения и хранилище

Каждый плагин, включенный или нет, отображается в разделе **Настройки → Плагины**, где
пользователь переключает его в режим реального времени (без перезапуска приложения), открывает его папку или выполняет повторное сканирование. пользователя
выбор запоминается:

- Выбора пока нет → собственный `defaultEnabled` плагина (по умолчанию `true`). Установить
  `defaultEnabled: false` для поставки плагина с возможностью подписки, который остается темным, пока пользователь
  включает его.
- Явный выбор → сохраняется и соблюдается при перезапусках. Отключенный плагин
  остается отключенным — не боритесь с этим; пользователь отключил вас.

Сохраните свое собственное состояние с помощью `ctx.storage`, размещенного в пространстве имен вашего плагина.
(`hermes.plugin.<id>.*`), чтобы плагины не могли читать или затирать друг друга:

```javascript
ctx.storage.set('lastTab', 'board')
const tab = ctx.storage.get('lastTab', 'summary')
ctx.storage.remove('lastTab')
```

## Плагины в комплекте

Плагин может быть отправлен в дереве по адресу `apps/desktop/src/plugins/<id>/plugin.tsx` (по умолчанию).
export a __PH_329__). It's discovered by __PH_330__ at boot —
без импорта, без редактирования реестра — и делится точным инвентарем + в реальном времени
включить/отключить контракт как дисковый плагин. Два различия:

1. Он проходит через сборку приложения Vite, поэтому вы можете писать **настоящий JSX** и импортировать
   SDK по его псевдониму `@hermes/plugin-sdk`.
2. Он по-прежнему защищен только от `@hermes/plugin-sdk` + `react` — без приложения `@/…`.
   внутренности.

На сегодняшний день плагины рабочего стола в основном дереве не поставляются; отправленное приложение остается незагроможденным
и демо живут в
[`hermes-example-plugins`](https://github.com/NousResearch/hermes-example-plugins)
сопутствующее репо.

## Модель безопасности

Загруженный плагин оценивается как ESM в области рендеринга с **полным приложением.
авторитет** — синглтон React, весь SDK (`host.request` шлюз RPC,
`ctx.rest`, хранилище, `navigate`). Изоляция, которую обеспечивает загрузчик, равна **error.
только изоляция**: плагин не может привести к сбою приложения (вклады ограничены ошибками,
слушатели изолированы), но он может делать все, что может приложение.

Это приемлемо для **локальных** источников — файл на диске уже может запускать код.
вашей машине — вот почему дверь диска загружает только локальные файлы, которые вы (или ваш
агент) написал. Необязательная проверка `integrity` (`sha256-…`) подтверждает только байты.
сопоставить хеш; это **не** песочница. Будущей двери с дистанционным управлением потребуется
реальная граница (iframe/worker + CSP + шлюз возможностей), прежде чем она сможет приземлиться; делать
не рассматривайте этот конвейер как границу доверия.

## Подводные камни

- **JSX не анализируется в дисковом плагине.** Файл загружается в некомпилированном виде — используйте `jsx()` /
  `jsxs()` (или `React.createElement`), а не синтаксис JSX. (Встроенные плагины создаются,
  так что с JSX все в порядке.)
- **Только три спецификатора разрешают:** `@hermes/plugin-sdk`, `react`,
  `react/jsx-runtime`. Любой другой импорт приводит к ошибке предварительной загрузки.
- **Никогда не кодируйте цвета жестко** (`#000`, `black`, `rgb(...)`). Оставьте фон
  один; используйте переменные темы (`var(--ui-*)`) для всего.
- **Ссылайтесь только на то, что вы импортировали.** Компонент, который вы забыли импортировать (например,
  `StatusDot`) — это `ReferenceError` при рендеринге — дважды проверьте каждый идентификатор в
  ваши вызовы `jsx()` появятся в строке импорта.
- **Обязательно считывать состояние в обработчиках** (`$atom.get()`), а не при рендеринге.
  закрытие — в противном случае быстрые события будут иметь устаревшие значения. Подписаться (`useValue`)
  только в листе, который отображает значение.
- **Панели холста должны отслеживать свой контейнер** с помощью `ResizeObserver` и изменять размер.
  холст (атрибуты ширины/высоты, а не только CSS) — размеры панелей постоянно изменяются.
- **Не опрашивайте быстрее, чем несколько секунд** с помощью `host.request`; предпочитаю
  `host.onEvent` / `ctx.socket` и позвольте React Query выполнить дедупликацию.
- **`ctx.socket` не работает на удаленных устройствах OAuth.** Всегда имейте резервный вариант опроса.

## Ссылка

### Экспорт SDK с первого взгляда

| Категория | Экспорт |
|----------|---------|
| Хозяин | `host` (`.state.*`, `.notify`, `.notifyError`, `.navigate`, `.onEvent`, `.logs`, `.status`, `.restartGateway`, `.request`) |
| Контракт плагина | `HermesPlugin`, `PluginContext`, `PluginContribution`, `PluginStorage`, `PluginOs`, `PluginRestOptions`, `PluginNativeNotificationInput`, `PluginNotificationAction`, `HermesOpenTarget`, `Contribution` |
| Константы площади | `PANES_AREA`, `ROUTES_AREA`, `SIDEBAR_NAV_AREA`, `STATUSBAR_AREAS`, `TITLEBAR_AREAS`, `PALETTE_AREA`, `KEYBINDS_AREA`, `THEMES_AREA`, `COMPOSER_AREAS` |
| Полезная нагрузка области | `RouteContribution`, `SidebarNavContribution`, `StatusbarItem`, `TitlebarTool`, `PaletteContribution`, `KeybindContribution`, `ComposerMiddleware`, `ComposerAttachmentProvider` |
| Реагировать/состояние | `useValue`, `atom`, `computed`, `useQuery`, `useMutation`, `useQueryClient`, `queryClient`, `Contribute` |
| Тематика | `useTheme`, `requestTheme`, `setAccentOverride`, `$accentOverride`, `retintTheme`, `themeHue`, `DesktopTheme`, `DesktopThemeColors`, плюс математические вычисления OKLCH (`hexToOklch`, `oklchToHex`, `oklchToSrgb255`, `mixOklab`, `maxChroma`, `hueDelta`, `contrastRatio`, `readableOn`, `normalizeHex`) |
| Комплект пользовательского интерфейса | `Button`, `Input`, `Textarea`, `Select*`, `Switch`, `Checkbox`, `SegmentedControl`, `Tabs*`, `Dialog*`, `ConfirmDialog`, `DropdownMenu*`, `ContextMenu*`, `Popover*`, `Tip`/`Tooltip*`, `Badge`, `Kbd`/`KbdGroup`, `SearchField`, `ScrollArea`, `Separator`, `Skeleton`, `GlyphSpinner`, `Loader`, `EmptyState`, `ErrorState`, `CopyButton`, `StatusDot`, `LogView`, `Codicon`, `DecodeText` |
| Помощники | `cn`, `icons`, `haptic`, `useI18n`, `profileColor`, `profileColorSoft`, `relativeTime`, `fmtDateTime`, `fmtDayTime`, `coarseElapsed`, `evaluateRuntimeReadiness` |

Канонический, всегда актуальный список экспорта — `apps/desktop/src/sdk/index.ts`.

###Агенты: навык `hermes-desktop-plugins`

Когда агент пишет плагин рабочего стола, он должен загрузить входящий в комплект пакет.
**`hermes-desktop-plugins`** навык — он содержит тот же контракт, что и эта страница в
форма для агента с готовым к копированию `templates/plugin.js`. Эта страница является
рекомендация человека/разработчика; навык – это рабочий контрольный список.

## Устранение неполадок

**Мой плагин не отображается.** Убедитесь, что файл находится по адресу.
`$HERMES_HOME/desktop-plugins/<id>/plugin.js` и имя папки соответствует
export __PH_470__. Run ⌘K → **Reload desktop plugins**. Check the app for an error
тост с указанием сбоя и хвост `hermes logs gui -f`.

**"неподдерживаемый импорт" при загрузке.** Дисковый плагин может импортировать только
`@hermes/plugin-sdk`, `react` и `react/jsx-runtime`. Удалите любой другой импорт.

**Элемент `jsx` ничего не отображает/выдает `ReferenceError`.** Используемый идентификатор
в вызове `jsx()` не импортируется. Добавьте его в строку импорта.

**`ctx.rest` возвращает 404.** Серверная часть не смонтирована: подтвердите.
`~/.hermes/plugins/<id>/dashboard/manifest.json` имеет `"api": "plugin_api.py"`,
что плагин находится в `plugins.enabled` в `config.yaml`, и перезапустите шлюз
(внутренние маршруты монтируются при запуске). Хвост `~/.hermes/logs/errors.log` для
`Failed to load plugin <id> API routes`.

**`ctx.socket` никогда не срабатывает.** На удаленном OAuth по замыслу это неактивно — используйте свой
резервный вариант опроса. В противном случае убедитесь, что серверная часть предоставляет соответствующие
`@router.websocket(...)` маршрут в своем пространстве имен.

**После переключения темы цвета выглядят неправильно.** Вы жестко запрограммировали цвет. Замените его на
переменная темы `var(--ui-*)`.