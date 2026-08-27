---
title: 'Python Debugpy — Отладка Python: pdb REPL + удаленная отладка (DAP)'
sidebar_label: Python Debugpy
description: 'Отладка Python: pdb REPL + удаленная отладка (DAP)'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Отладка Python

Отладка Python: pdb REPL + удаленная отладка (DAP).

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/software-development/python-debugpy` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS |
| Теги | `debugging`, `python`, `pdb`, `debugpy`, `breakpoints`, `dap`, `post-mortem` |
| Сопутствующие навыки | [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugger), [`node-inspect-debugger`](/docs/user-guide/skills/bundled/software-development/software-development-node-inspect-debugger) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Отладчик Python (pdb + debugpy)

## Обзор

Три инструмента, выбираемых по ситуации:

| Инструмент | Когда |
|---|---|
| **`breakpoint()` + PDB** | Локальный, интерактивный, самый простой. Добавьте `breakpoint()` в исходный код, запустите в обычном режиме, получите REPL в этой строке. |
| **`python -m pdb`** | Запустите существующий скрипт в pdb без редактирования исходного кода. Полезно для быстрого тыкания. |
| **`debugpy`** | Удаленный/безголовый/"присоединиться к уже запущенному процессу". Talks DAP, скриптируемый с терминала, работает для долгоживущих процессов (шлюз, демон, дочерние элементы PTY). |

**Начните с `breakpoint()`.** Это самый дешевый вариант.

## Когда использовать

- Тест не пройден, и обратная трассировка не показывает, почему значение неверно.
- Вам нужно выполнить функцию и наблюдать за изменением коллекции.
- Длительный процесс (шлюз Hermes, tui_gateway) работает неправильно, и вы не можете его перезапустить.
- Посмертное исследование: в рабочем коде генерируется исключение, и вы хотите проверить местных жителей на месте сбоя.
- Подпроцесс/дочерний процесс (Python `_SlashWorker`, рабочий мост PTY) является фактическим местом ошибки.

**Не используйте для:** задач, которые `print()`/`logging.debug` решают менее чем за минуту, или вещей, которые `pytest -vv --tb=long --showlocals` уже раскрывает.

## Краткий справочник по pdb

Внутри любого приглашения pdb (`(Pdb)`):

| Команда | Действие |
|---|---|
| `h` / `h cmd` | помощь |
| `n` | следующая строка (перешагнуть) |
| `s` | шагнуть в |
| `r` | возврат из текущей функции |
| `c` | продолжить |
| `unt N` | продолжать до строки N |
| `j N` | перейти на строку N (только та же функция) |
| `l` / `ll` | список источников вокруг текущей строки/полная функция |
| `w` | где (трассировка стека) |
| `u` / `d` | перемещаться вверх/вниз по стопке |
| `a` | вывести аргументы текущей функции |
| `p expr` / `pp expr` | печать / красивое выражение |
| `display expr` | автоматическая печать выражения на каждой остановке |
| `b file:line` | установить точку останова |
| `b func` | перерыв при входе в функцию |
| `b file:line, cond` | условная точка останова |
| `cl N` | очистить точку останова N |
| `tbreak file:line` | одноразовая точка останова |
| `!stmt` | выполнить произвольный Python (задания включены) |
| `interact` | перейдите в полную версию Python REPL в текущей области (Ctrl+D для выхода) |
| `q` | бросить |

Команда `interact` является самой мощной — вы можете импортировать что угодно, проверять сложные объекты и даже вызывать методы, изменяющие состояние. По умолчанию локальные файлы доступны только для чтения; используйте `!x = 42` из приглашения `(Pdb)` для изменения.

## Рецепт 1: Локальная точка останова

Самый простой. Отредактируйте файл:

```python
def compute(x, y):
    result = some_helper(x)
    breakpoint()           # <-- drops into pdb here
    return result + y
```

Запустите код в обычном режиме. Вы приземлитесь на линии `breakpoint()` с полным доступом к местным жителям.

**Не забудьте удалить `breakpoint()` перед фиксацией.** Используйте `git diff` или команду grep перед фиксацией:
```bash
rg -n 'breakpoint\(\)' --type py
```

## Рецепт 2: Запускаем скрипт под pdb (без редактирования исходного кода)

```bash
python -m pdb path/to/script.py arg1 arg2
# Lands at first line of script
(Pdb) b path/to/script.py:42
(Pdb) c
```

## Рецепт 3: Отладка теста pytest

Программа запуска тестов Hermes и pytest поддерживают это:

```bash
# Drop to pdb on failure (or on any raised exception):
scripts/run_tests.sh tests/path/to/test_file.py::test_name --pdb

# Drop to pdb at the START of the test:
scripts/run_tests.sh tests/path/to/test_file.py::test_name --trace

# Show locals in tracebacks without pdb:
scripts/run_tests.sh tests/path/to/test_file.py --showlocals --tb=long
```

Примечание. `scripts/run_tests.sh` запускает каждый тестовый файл в захваченном подпроцессе через `run_tests_parallel.py` (без xdist), поэтому интерактивный pdb НЕ работает под оболочкой. Запустите pytest напрямую для `--pdb`:

```bash
source .venv/bin/activate
python -m pytest tests/foo_test.py::test_bar --pdb
```

Это обходит гарантии hermetic-env — хорошо для отладки, но перед отправкой необходимо повторно запустить под оболочкой для подтверждения.

## Рецепт 4: Вскрытие любого исключения

```python
import pdb, sys
try:
    run_the_thing()
except Exception:
    pdb.post_mortem(sys.exc_info()[2])
```

Или оберните весь скрипт:

```bash
python -m pdb -c continue script.py
# When it crashes, pdb catches it and you're in the frame of the exception
```

Или установите глобальный хук в repl/jupyter:

```python
import sys
def excepthook(etype, value, tb):
    import pdb; pdb.post_mortem(tb)
sys.excepthook = excepthook
```

## Рецепт 5: Удаленная отладка с помощью debugpy (подключение к запущенному процессу)

Для долгоживущих процессов: шлюз Hermes, tui_gateway, демон, процесс, который уже работает неправильно и не может быть перезапущен в чистом виде.

### Настройка

```bash
source <hermes-agent-repo>/.venv/bin/activate
pip install debugpy
```

### Шаблон A: Source-edit — процесс ожидает отладчика при запуске

Добавьте в верхней части точки входа (или внутри функции, которую вы хотите отладить):

```python
import debugpy
debugpy.listen(("127.0.0.1", 5678))
print("debugpy listening on 5678, waiting for client...", flush=True)
debugpy.wait_for_client()
debugpy.breakpoint()       # optional: pause immediately once attached
```

Запустите процесс; он блокируется на `wait_for_client()`.

### Шаблон B: без редактирования исходного кода — запуск с помощью `-m debugpy`

```bash
python -m debugpy --listen 127.0.0.1:5678 --wait-for-client your_script.py arg1
```

Эквивалент для входа в модуль:

```bash
python -m debugpy --listen 127.0.0.1:5678 --wait-for-client -m your.module
```

### Шаблон C: подключение к уже запущенному процессу

Требуется, чтобы PID и отладочная программа были предварительно установлены в целевой среде:

```bash
python -m debugpy --listen 127.0.0.1:5678 --pid <pid>
# debugpy injects itself into the process. Then attach a client as below.
```

Некоторые конфигурации ядра/безопасности блокируют внедрение на основе ptrace (`/proc/sys/kernel/yama/ptrace_scope`). Исправьте с помощью:
```bash
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

### Подключение клиента из терминала

Самый простой клиент DAP на стороне терминала — это VS Code CLI или небольшой скрипт. Изнутри Hermes у вас есть два практических варианта:

**Вариант 1: собственный CLI REPL `debugpy`** — не официальная функция, а небольшой клиентский скрипт DAP:

```python
# /tmp/dap_client.py
import socket, json, itertools, time, sys

HOST, PORT = "127.0.0.1", 5678
s = socket.create_connection((HOST, PORT))
seq = itertools.count(1)

def send(msg):
    msg["seq"] = next(seq)
    body = json.dumps(msg).encode()
    s.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

def recv():
    header = b""
    while b"\r\n\r\n" not in header:
        header += s.recv(1)
    length = int(header.decode().split("Content-Length:")[1].split("\r\n")[0].strip())
    body = b""
    while len(body) < length:
        body += s.recv(length - len(body))
    return json.loads(body)

send({"type": "request", "command": "initialize", "arguments": {"adapterID": "python"}})
print(recv())
send({"type": "request", "command": "attach", "arguments": {}})
print(recv())
send({"type": "request", "command": "setBreakpoints",
      "arguments": {"source": {"path": sys.argv[1]},
                    "breakpoints": [{"line": int(sys.argv[2])}]}})
print(recv())
send({"type": "request", "command": "configurationDone"})
# ... loop reading events and sending continue/stepIn/etc.
```

Это хорошо для разовой автоматизации, но болезненно для интерактивного UX.

**Вариант 2. Прикрепите из VS Code/Cursor/Zed** — если у пользователя открыта одна из них, он может добавить `launch.json`:

```json
{
  "name": "Attach to Hermes",
  "type": "debugpy",
  "request": "attach",
  "connect": { "host": "127.0.0.1", "port": 5678 },
  "justMyCode": false,
  "pathMappings": [
    { "localRoot": "${workspaceFolder}", "remoteRoot": "<hermes-agent-repo>" }
  ]
}
```

**Вариант 3. Откажитесь от DAP, используйте `remote-pdb`** — обычно это то, что вы действительно хотите от терминального агента:

```bash
pip install remote-pdb
```

В вашем коде:
```python
from remote_pdb import set_trace
set_trace(host="127.0.0.1", port=4444)   # blocks until connection
```

Затем из терминала:
```bash
nc 127.0.0.1 4444
# You get a (Pdb) prompt exactly as if debugging locally.
```

`remote-pdb` — самый чистый и удобный для агентов выбор, когда протокол DAP `debugpy` является излишним. Используйте `debugpy` только тогда, когда вам действительно нужна интеграция с IDE.

## Отладка процессов, специфичных для Hermes

### Тесты
См. рецепт 3. Оболочка захватывает выходные данные подпроцесса, поэтому запускайте pytest напрямую для интерактивного pdb.

### `run_agent.py` / CLI — одноразовый
Самый простой: добавьте `breakpoint()` рядом с подозрительной строкой, а затем запустите `hermes` в обычном режиме. Управление возвращается к вашему терминалу в точке паузы.

### Подпроцесс `tui_gateway` (создан `hermes --tui`)
Шлюз работает как дочерний элемент Node TUI. Опции:

**А. Исходный код — отредактируйте шлюз:**
```python
# tui_gateway/server.py near the top of serve()
import debugpy
debugpy.listen(("127.0.0.1", 5678))
debugpy.wait_for_client()
```
Запустите `hermes --tui`. TUI будет зависшим (его серверная часть ожидает). Прикрепить клиента; выполнение возобновится, когда вы `continue`.

**Б. Используйте `remote-pdb` в конкретном обработчике:**
```python
from remote_pdb import set_trace
set_trace(host="127.0.0.1", port=4444)   # in the RPC handler you want to trap
```
Запустите соответствующую команду косой черты из TUI, затем `nc 127.0.0.1 4444` в другом терминале.

### `_SlashWorker` подпроцесс
Тот же шаблон — `remote-pdb` с `set_trace()` внутри пути `exec` работника. Рабочий объект сохраняется при выполнении команд косой черты, поэтому первый триггер блокируется до тех пор, пока вы не подключитесь; последующие команды косой черты выполняются нормально, если вы не перевооружитесь.

### Шлюз (`gateway/run.py`)
Долговечный. Используйте `remote-pdb` в обработчике или `debugpy` с `--wait-for-client`, если вы все равно перезапускаете шлюз.

## Распространенные ошибки

1. **pdb под параллельной программой/исполнителем захвата вывода молча ничего не делает.** Вы не увидите приглашение, тест просто зависает (верно для pytest-xdist и для `scripts/run_tests.sh`, захваченных пофайловых подпроцессов). Запустите pytest непосредственно в одном файле для интерактивной отладки.

2. **`breakpoint()` в контекстах CI/без TTY зависает процесс.** Безопасно локально; никогда не совершайте этого. Добавьте grep перед фиксацией в качестве страховки.

3. **`PYTHONBREAKPOINT=0`** отключает все вызовы `breakpoint()`. Проверьте окружение, если ваша точка останова не сработала:
   ```bash
   echo $PYTHONBREAKPOINT
   ```

4. **`debugpy.listen` блокируется, только если вы также вызываете `wait_for_client()`.** Без него выполнение продолжается, и ваша первая точка останова может сработать до подключения клиента.

5. **Присоединение к PID не удается в усиленных ядрах.** `ptrace_scope=1` (по умолчанию в Ubuntu) разрешает только однопользовательскую трассировку дочерних процессов. Обходной путь: `echo 0 > /proc/sys/kernel/yama/ptrace_scope` (нужен root) или запустите под `debugpy` с самого начала.

6. **Потоки.** `pdb` отлаживает только текущий поток. Для многопоточного кода используйте `debugpy` (DAP с поддержкой потоков) или установите `threading.settrace()` для каждого потока.

7. **asyncio.** `pdb` работает в сопрограммах, но `await` внутри pdb требует Python 3.13+ или `await` из режима `interact` в более старых версиях. В версиях 3.11/3.12 используйте трюки `asyncio.run_coroutine_threadsafe` или ожидания на основе `!stmt` через `asyncio.ensure_future`.

8. **`scripts/run_tests.sh` удаляет учетные данные и устанавливает `HOME=<tmpdir>`.** Если ваша ошибка зависит от конфигурации пользователя или реальных ключей API, она не будет воспроизводиться под оболочкой. Сначала выполните отладку с необработанным `pytest` для воспроизведения, а затем повторно подтвердите его под оболочкой.

9. **Разветвление/многопроцессорность.** pdb не следует за разветвлениями. Каждому ребенку нужен свой `breakpoint()` или `set_trace()`. Для субагентов Hermes выполняйте отладку по одному процессу за раз.

## Контрольный список проверки

- [ ] После `pip install debugpy` подтвердите: `python -c "import debugpy; print(debugpy.__version__)"`
- [ ] Для удаленной отладки убедитесь, что порт действительно прослушивается: `ss -tlnp | grep 5678`
- [ ] Первая точка останова действительно достигает (если это не так, у вас, скорее всего, `PYTHONBREAKPOINT=0`, вы находитесь под параллельным/захватывающим бегуном или выполнение завершено до присоединения)
- [ ] `where`/`w` показывает ожидаемый стек вызовов
- [ ] Очистка после отладки: в зафиксированном коде нет случайных `breakpoint()`/`set_trace()`.
  ```bash
  rg -n 'breakpoint\(\)|set_trace\(|debugpy\.listen' --type py
  ```

## Одноразовые рецепты

**"Почему в этом диктовке отсутствует ключ?"**
```python
# add above the KeyError site
breakpoint()
# then in pdb:
(Pdb) pp d
(Pdb) pp list(d.keys())
(Pdb) w                # how did we get here
```

**"Этот тест проходит изолированно, но не проходит в пакете."**
```bash
scripts/run_tests.sh tests/the_test.py   # confirm it fails under the isolated runner first
# For interactive debugging, or if it only fails WITH other tests:
source .venv/bin/activate
python -m pytest tests/ -x --pdb
# Now it pdb-traps at the exact failing test after state accumulated.
```

**"Мой асинхронный обработчик блокируется."**
```python
# Add at handler entry
import remote_pdb; remote_pdb.set_trace(host="127.0.0.1", port=4444)
```
Запустите обработчик. `nc 127.0.0.1 4444`, затем `w`, чтобы увидеть приостановленный кадр, `!import asyncio; asyncio.all_tasks()`, чтобы увидеть, что еще ожидается.

**"Вскрытие при сбое в дочернем процессе/подпроцессе Ink."**
```bash
PYTHONFAULTHANDLER=1 python -m pdb -c continue path/to/entrypoint.py
# On crash, pdb lands at the frame of the exception with full locals
```