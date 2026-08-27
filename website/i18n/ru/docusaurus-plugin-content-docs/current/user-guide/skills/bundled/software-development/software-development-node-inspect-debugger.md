---
title: Отладчик Node Inspect — отладка Node.js через --inspect + CLI протокола Chrome
  DevTools
sidebar_label: Node Inspect Debugger
description: Отладка Node.js через --inspect + CLI протокола Chrome DevTools
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Отладчик проверки узла

Отладка Node.js с помощью --inspect + CLI протокола Chrome DevTools.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/software-development/node-inspect-debugger` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `debugging`, `nodejs`, `node-inspect`, `cdp`, `breakpoints`, `ui-tui` |
| Сопутствующие навыки | [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging), [`python-debugpy`](/docs/user-guide/skills/bundled/software-development/software-development-python-debugpy) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Проверка отладчика Node.js

## Обзор

Если `console.log` недостаточно, запустите встроенный в Node инспектор V8 программно с терминала. Вы получаете настоящие точки останова, вход/переход/выход, обход стека вызовов, дампы локальной области/закрытия и оценку произвольного выражения в приостановленном кадре.

Два инструмента, выберите один:

- **`node inspect`** — встроенный, без установки, CLI REPL. Лучше всего для быстрого тыкания.
- **`ndb` / CDP через `chrome-remote-interface`** — можно использовать в сценариях Node/Python; Лучше всего, если вы хотите автоматизировать множество точек останова, собирать состояние во время выполнения или выполнять неинтерактивную отладку из цикла агента.

**Сначала выберите `node inspect`.** Он всегда доступен, а REPL работает быстро.

## Когда использовать

- Тест узла не пройден, и вам нужно увидеть промежуточное состояние.
- ui-tui аварийно завершает работу или ведет себя неправильно, и вы хотите проверить состояние предварительного рендеринга React/Ink.
- дочерние процессы tui_gateway (`_SlashWorker`, рабочие моста PTY) ведут себя неправильно
– Вам необходимо проверить значение в замыкании, которого `console.log` не может достичь без исправления.
- Perf: подключение к запущенному процессу для захвата профиля ЦП или снимка кучи.

**Не используйте для:** задач, которые `console.log` решает менее чем за минуту. Отладка с использованием точек останова сложнее; используйте его, когда выигрыш реален.

## Краткий справочник: `node inspect` REPL

Запуск приостановлен на первой строке:

```bash
node inspect path/to/script.js
# or with tsx
node --inspect-brk $(which tsx) path/to/script.ts
```

Приглашение `debug>` принимает:

| Команда | Действие |
|---|---|
| `c` или `cont` | продолжить |
| `n` или `next` | перешагнуть |
| `s` или `step` | шагнуть в |
| `o` или `out` | выйти |
| `pause` | приостановить выполнение кода |
| `sb('file.js', 42)` | установить точку останова в строке 42 файла file.js |
| `sb(42)` | установить точку останова в строке 42 текущего файла |
| `sb('functionName')` | прерывание при вызове функции |
| `cb('file.js', 42)` | очистить точку останова |
| `breakpoints` | список всех точек останова |
| `bt` | обратная трассировка (стек вызовов) |
| `list(5)` | показать 5 строк источника вокруг текущей позиции |
| `watch('expr')` | оценивать expr на каждой паузе |
| `watchers` | показать просмотренные выражения |
| `repl` | перейти в REPL в текущей области (Ctrl+C для выхода из REPL) |
| `exec expr` | оценить выражение один раз |
| `restart` | сценарий перезапуска |
| `kill` | убить скрипт |
| `.exit` | выйти из отладчика |

**В подрежиме `repl`:** введите любое выражение JS, включая доступ к локальным переменным и переменным закрытия. `Ctrl+C` возвращается в `debug>`.

## Подключение к запущенному процессу

Когда процесс уже запущен (например, долгоживущий сервер разработки или шлюз TUI):

```bash
# 1. Send SIGUSR1 to enable the inspector on an existing process
kill -SIGUSR1 <pid>
# Node prints: Debugger listening on ws://127.0.0.1:9229/<uuid>

# 2. Attach the debugger CLI
node inspect -p <pid>
# or by URL
node inspect ws://127.0.0.1:9229/<uuid>
```

Чтобы начать процесс с инспектором с самого начала:

```bash
node --inspect script.js           # listen on 127.0.0.1:9229, keep running
node --inspect-brk script.js       # listen AND pause on first line
node --inspect=0.0.0.0:9230 script.js   # custom host:port
```

Для TypeScript через tsx:

```bash
node --inspect-brk --import tsx script.ts
# or older tsx
node --inspect-brk -r tsx/cjs script.ts
```

## Programmatic CDP (скрипты с терминала)

Если вы хотите автоматизировать — установить множество точек останова, захватить состояние области, создать сценарий воспроизведения — используйте `chrome-remote-interface`:

```bash
npm i -g chrome-remote-interface        # or project-local
# Start your target:
node --inspect-brk=9229 target.js &
```

Скрипт драйвера (сохранить как `/tmp/cdp-debug.js`):

```javascript
const CDP = require('chrome-remote-interface');

(async () => {
  const client = await CDP({ port: 9229 });
  const { Debugger, Runtime } = client;

  Debugger.paused(async ({ callFrames, reason }) => {
    const top = callFrames[0];
    console.log(`PAUSED: ${reason} @ ${top.url}:${top.location.lineNumber + 1}`);

    // Walk scopes for locals
    for (const scope of top.scopeChain) {
      if (scope.type === 'local' || scope.type === 'closure') {
        const { result } = await Runtime.getProperties({
          objectId: scope.object.objectId,
          ownProperties: true,
        });
        for (const p of result) {
          console.log(`  ${scope.type}.${p.name} =`, p.value?.value ?? p.value?.description);
        }
      }
    }

    // Evaluate an expression in the paused frame
    const { result } = await Debugger.evaluateOnCallFrame({
      callFrameId: top.callFrameId,
      expression: 'typeof state !== "undefined" ? JSON.stringify(state) : "n/a"',
    });
    console.log('state =', result.value ?? result.description);

    await Debugger.resume();
  });

  await Runtime.enable();
  await Debugger.enable();

  // Set a breakpoint by URL regex + line
  await Debugger.setBreakpointByUrl({
    urlRegex: '.*app\\.tsx$',
    lineNumber: 119,       // 0-indexed
    columnNumber: 0,
  });

  await Runtime.runIfWaitingForDebugger();
})();
```

Запустите его:

```bash
node /tmp/cdp-debug.js
```

Специальное примечание Hermes: `chrome-remote-interface` НЕТ в `ui-tui/package.json`. Установите его в мусорное место, если не хотите испортить проект:

```bash
mkdir -p /tmp/cdp-tools && cd /tmp/cdp-tools && npm i chrome-remote-interface
NODE_PATH=/tmp/cdp-tools/node_modules node /tmp/cdp-debug.js
```

## Отладка Hermes ui-tui

TUI построен Ink + tsx. Два распространенных сценария:

### Отладка одного компонента Ink в dev

`ui-tui/package.json` имеет `npm run dev` (tsx --watch). Добавьте `--inspect-brk`, запустив tsx напрямую:

```bash
cd <hermes-agent-repo>/ui-tui
npm run build    # produce dist/ once so transpile isn't needed on first load
node --inspect-brk dist/entry.js
# In another terminal:
node inspect -p <node pid>
```

Затем внутри `debug>`:

```
sb('dist/app.js', 220)     # or wherever the suspect render is
cont
```

Когда он приостанавливается, `repl` → проверяет `props`, ссылки на состояние, значения обработчика `useInput` и т. д.

### Отладка работающего `hermes --tui`

TUI порождает Node из CLI Python. Самый простой путь:

```bash
# 1. Launch TUI
hermes --tui &
TUI_PID=$(pgrep -f 'ui-tui/dist/entry' | head -1)

# 2. Enable inspector on that Node PID
kill -SIGUSR1 "$TUI_PID"

# 3. Find the WS URL
curl -s http://127.0.0.1:9229/json/list | jq -r '.[0].webSocketDebuggerUrl'

# 4. Attach
node inspect ws://127.0.0.1:9229/<uuid>
```

Взаимодействие с TUI (ввод текста в его окне) продолжает ускорять выполнение; ваш отладчик может приостановить его на точке останова в любой точке `sb(...)`.

### Отладка дочерних процессов `_SlashWorker`/PTY

Это Python, а не Node — используйте для них навык `python-debugpy`. Этот навык используется только в узлах Node (пользовательский интерфейс Ink, клиент tui_gateway, тесты, выполняемые с помощью tsx под `ui-tui/`).

## Запуск тестов Vitest под отладчиком

```bash
cd <hermes-agent-repo>/ui-tui
# Run a single test file paused on entry
node --inspect-brk ./node_modules/vitest/vitest.mjs run --no-file-parallelism src/app/foo.test.tsx
```

В другом терминале: `node inspect -p <pid>`, затем `sb('src/app/foo.tsx', 42)`, `cont`.

Используйте `--no-file-parallelism` (vitest) или `--runInBand` (jest), чтобы существовал только один рабочий процесс — отладка пула является болезненной задачей.

## Снимки кучи и профили ЦП (неинтерактивные)

В приведенном выше драйвере CDP замените Debugger на `HeapProfiler`/`Profiler`:

```javascript
// CPU profile for 5 seconds
await client.Profiler.enable();
await client.Profiler.start();
await new Promise(r => setTimeout(r, 5000));
const { profile } = await client.Profiler.stop();
require('fs').writeFileSync('/tmp/cpu.cpuprofile', JSON.stringify(profile));
// Open /tmp/cpu.cpuprofile in Chrome DevTools → Performance tab
```

```javascript
// Heap snapshot
await client.HeapProfiler.enable();
const chunks = [];
client.HeapProfiler.addHeapSnapshotChunk(({ chunk }) => chunks.push(chunk));
await client.HeapProfiler.takeHeapSnapshot({ reportProgress: false });
require('fs').writeFileSync('/tmp/heap.heapsnapshot', chunks.join(''));
```

## Распространенные ошибки

1. **Неправильные номера строк в исходном коде TS.** Точки останова попали в созданный JS, а не в `.ts`. Либо (а) внесите изменения во встроенный `dist/*.js`, либо (б) включите исходные карты (`node --enable-source-maps`) и используйте `sb('src/app.tsx', N)` — но только с клиентами CDP, которые следуют за исходными картами. `node inspect` CLI нет.

2. **`--inspect` и `--inspect-brk`.** `--inspect` запускает инспектор, но не приостанавливает его; ваш скрипт пройдет мимо первой точки останова, если вы присоединитесь слишком поздно. Используйте `--inspect-brk`, когда вам нужно установить точки останова перед запуском любого кода.

3. **Коллизии портов.** Значение по умолчанию: `9229`. Если проверку выполняют несколько процессов Node, передайте `--inspect=0` (случайный порт) и прочитайте фактический URL-адрес из `/json/list`:
   ```bash
   curl -s http://127.0.0.1:9229/json/list   # lists all inspectable targets on the host
   ```

4. **Дочерние процессы.** `--inspect` родительского процесса НЕ проверяет дочерние процессы. Используйте `NODE_OPTIONS='--inspect-brk' node parent.js` для распространения на каждого дочернего элемента; имейте в виду, что всем им нужны уникальные порты (узел автоматически увеличивается при наследовании `NODE_OPTIONS='--inspect'`).

5. **Фоновые убийства.** Если вы `Ctrl+C` из `node inspect`, пока цель находится на паузе, цель останется на паузе. Либо сначала `cont`, либо `kill` цель явно.

6. **Запуск `node inspect` через терминал агента.** Это REPL, поддерживающий PTY. В Гермесе запустите его с помощью `terminal(pty=true)` или `background=true` + `process(action='submit', data='...')`. Режим переднего плана без PTY будет работать для одноразовых команд, но не для интерактивного пошагового выполнения.

7. **Безопасность.** `--inspect=0.0.0.0:9229` обеспечивает выполнение произвольного кода. Всегда привязывайтесь к `127.0.0.1` (по умолчанию), если у вас нет изолированной сети.

## Контрольный список проверки

После настройки сеанса отладки проверьте:

- [ ] `curl -s http://127.0.0.1:9229/json/list` возвращает именно ту цель, которую вы ожидаете
- [ ] Первая точка останова действительно достигает (если это не так, вы, вероятно, пропустили `--inspect-brk` или присоединились после завершения выполнения)
- [ ] В списке исходных текстов при паузе отображается правильный файл (несоответствие = проблема с исходной картой, см. ошибку 1).
- [ ] `exec process.pid` в `repl` возвращает PID, к которому вы хотели присоединить

## Одноразовые рецепты

**"Почему эта переменная не определена в строке X?"**
```bash
node --inspect-brk script.js &
node inspect -p $!
# debug>
sb('script.js', X)
cont
# paused. Now:
repl
> myVariable
> Object.keys(this)
```

**"Каков путь вызова этой функции?"**
```
debug> sb('suspectFn')
debug> cont
# paused on entry
debug> bt
```

**"Эта асинхронная цепочка висит — где?"**
```
# Start with --inspect (no -brk), let it run to the hang, then:
debug> pause
debug> bt
# Now you see the stuck frame
```