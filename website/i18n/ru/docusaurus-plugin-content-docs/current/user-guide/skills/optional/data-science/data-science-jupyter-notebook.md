---
title: Jupyter Notebook — итеративный Python с использованием живого ядра Jupyter
  (hamelnb)
sidebar_label: Jupyter Notebook
description: Итеративный Python через живое ядро ​​Jupyter (hamelnb)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Блокнот Jupyter

Итеративный Python с использованием живого ядра Jupyter (hamelnb).

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/data-science/jupyter-notebook` |
| Путь | `optional-skills/data-science/jupyter-notebook` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `jupyter`, `notebook`, `repl`, `data-science`, `exploration`, `iterative` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Jupyter Notebook (живое ядро hamelnb)

Предоставляет вам **REPL Python с сохранением состояния** через живое ядро Jupyter. Переменные сохраняются
среди казней. Используйте это вместо `execute_code`, когда вам нужно накопить
состояние постепенно, исследуйте API, проверяйте DataFrames или выполняйте итерацию сложного кода.

## Когда использовать этот и другие инструменты

| Инструмент | Используйте, когда |
|------|----------|
| **Этот навык** | Итеративное исследование, состояние на всех этапах, наука о данных, машинное обучение, «дайте мне попробовать и проверить» |
| `execute_code` | Одноразовые сценарии, требующие доступа к инструменту Hermes (web_search, file ops). Без гражданства. |
| `terminal` | Команды оболочки, сборка, установка, git, управление процессами |

**Практическое правило:** Если для этой задачи вам нужен блокнот Jupyter, воспользуйтесь этим навыком.

## Предварительные условия

1. Должен быть установлен **uv** (проверьте: `which uv`)
2. Должен быть установлен **JupyterLab**: `uv tool install jupyterlab`.
3. Сервер Jupyter должен быть запущен (см. «Настройка» ниже).

## Настройка

Расположение скрипта hamelnb:
```
SCRIPT="$HOME/.agent-skills/hamelnb/skills/jupyter-live-kernel/scripts/jupyter_live_kernel.py"
```

Если еще не клонировано:
```
git clone https://github.com/hamelsmu/hamelnb.git ~/.agent-skills/hamelnb
```

### Запуск JupyterLab

Проверьте, запущен ли сервер:
```
uv run "$SCRIPT" servers
```

Если серверы не найдены, запустите один:
```
jupyter-lab --no-browser --port=8888 --notebook-dir=$HOME/notebooks \
  --IdentityProvider.token='' --ServerApp.password='' > /tmp/jupyter.log 2>&1 &
sleep 3
```

Примечание. Токен/пароль отключен для доступа к локальному агенту. Сервер работает без головы.

### Создание блокнота для использования REPL

Если вам просто нужен REPL (нет существующего блокнота), создайте минимальный файл блокнота:
```
mkdir -p ~/notebooks
```
Напишите минимальный JSON-файл .ipynb с одной пустой ячейкой кода, затем запустите ядро.
сеанс через Jupyter REST API:
```
curl -s -X POST http://127.0.0.1:8888/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"path":"scratch.ipynb","type":"notebook","name":"scratch.ipynb","kernel":{"name":"python3"}}'
```

## Основной рабочий процесс

Все команды возвращают структурированный JSON. Всегда используйте `--compact` для сохранения токенов.

### 1. Откройте для себя серверы и ноутбуки

```
uv run "$SCRIPT" servers --compact
uv run "$SCRIPT" notebooks --compact
```

### 2. Выполнение кода (основная операция)

```
uv run "$SCRIPT" execute --path <notebook.ipynb> --code '<python code>' --compact
```

Состояние сохраняется во время вызовов выполнения. Переменные, импорт, объекты — все сохраняется.

Многострочный код работает с цитированием $'...':
```
uv run "$SCRIPT" execute --path scratch.ipynb --code $'import os\nfiles = os.listdir(".")\nprint(f"Found {len(files)} files")' --compact
```

### 3. Проверка текущих переменных

```
uv run "$SCRIPT" variables --path <notebook.ipynb> list --compact
uv run "$SCRIPT" variables --path <notebook.ipynb> preview --name <varname> --compact
```

### 4. Редактирование ячеек блокнота

```
# View current cells
uv run "$SCRIPT" contents --path <notebook.ipynb> --compact

# Insert a new cell
uv run "$SCRIPT" edit --path <notebook.ipynb> insert \
  --at-index <N> --cell-type code --source '<code>' --compact

# Replace cell source (use cell-id from contents output)
uv run "$SCRIPT" edit --path <notebook.ipynb> replace-source \
  --cell-id <id> --source '<new code>' --compact

# Delete a cell
uv run "$SCRIPT" edit --path <notebook.ipynb> delete --cell-id <id> --compact
```

###5. Проверка (перезагрузка + запуск всего)

Используйте только тогда, когда пользователь запрашивает чистую проверку или вам нужно подтвердить
ноутбук работает сверху вниз:

```
uv run "$SCRIPT" restart-run-all --path <notebook.ipynb> --save-outputs --compact
```

## Практические советы из опыта

1. **Первое выполнение после запуска сервера может истечь по таймауту** — ядру нужен момент
   инициализировать. Если вы получили тайм-аут, просто повторите попытку.

2. **Ядро Python — это Python от JupyterLab** — пакеты необходимо устанавливать в
   эта среда. Если вам нужны дополнительные пакеты, установите их в папку
   Сначала инструментальная среда JupyterLab.

3. **--компактный флаг экономит значительные токены** — всегда используйте его. Вывод JSON может
   быть очень многословным без этого.

4. **Для чистого использования REPL** создайте Scratch.ipynb и не беспокойтесь о редактировании ячеек.
   Просто используйте `execute` несколько раз.

5. **Порядок аргументов имеет значение** — флаги подкоманды типа `--path` идут ПЕРЕД
   субподкоманда. Например: `variables --path nb.ipynb list`, а не `variables list --path nb.ipynb`.

6. **Если сеанс еще не существует**, вам необходимо запустить его через REST API.
   (см. раздел «Настройка»). Инструмент не может работать без живого сеанса ядра.

7. **Ошибки возвращаются в формате JSON** с помощью трассировки — прочтите `ename` и `evalue`.
   поля, чтобы понять, что пошло не так.

8. **Случайные тайм-ауты веб-сокетов** — некоторые операции могут истечь по тайм-ауту с первой попытки.
   особенно после перезапуска ядра. Повторите попытку, прежде чем переходить на более высокий уровень.

9. **Если время ожидания веб-сокета на этом хосте постоянно истекает**, принудительно включите транспортировку zmq:
   `uv run "$SCRIPT" execute --transport zmq ...`. Признак: каждое выполнение возвращает возврат
   «Выполнение веб-сокета, возможно, уже достигло ядра, поэтому автоматический возврат был
   пропущен». Ядро на самом деле работало нормально (REST показывает выполнение_state=idle и
   Execution_count приращения) — сломан только канал ответа веб-сокета.
   Транспорт zmq напрямую использует jupyter_client и обходит проблему.

10. **При запуске нового сервера для использования только REST** добавьте
    `--ServerApp.disable_check_xsrf=True` — в противном случае возвращается POST /api/sessions.
    `"'_xsrf' argument missing from POST"` и создание сеанса ядра завершается неудачей.

## Значения тайм-аута по умолчанию

Скрипт имеет 30-секундный тайм-аут по умолчанию на каждое выполнение. Для долгосрочного
операций, передайте `--timeout 120`. Используйте большие таймауты (60+) для начального
настройка или тяжелые вычисления.