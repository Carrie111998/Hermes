# Плагины поставщика среды терминала

Hermes запускает команды оболочки через подключаемый набор **бэкендов терминала**.
Встроенные бэкенды (локальные, Docker, Singularity, Modal, Daytona, Vercel).
Песочница, SSH) находятся в основном репозитории под `tools/environments/`. Сторонние
Вместо этого поставщики песочниц интегрируют их как **плагины** — отдельный репозиторий плагинов.
установлен под `~/.hermes/plugins/`, регистрирует серверную часть, выбранную пользователем
точно так же, как встроенный через `terminal.backend` в `config.yaml`.

Эта страница отражает [Плагины поставщика браузера](/developer-guide/browser-provider-plugin)
руководство — тот же процесс регистрации, та же семантика области действия.

## Что контролирует провайдер

Зарегистрированный бэкэнд автоматически участвует в каждой основной поверхности:

| Поверхность | Движимый |
|---|---|
| Отправка команд (`terminal`, `execute_code`, файловые инструменты) | `create_environment()` |
| `hermes setup` средство выбора серверной части | `display_name`, `description`, `setup_instructions()`, `post_setup()` |
| Средство выбора серверной части терминала информационной панели (состояние зонда) | `probe()` |
| `hermes status` / `hermes doctor` | `doctor_checks()` |
| Системные подсказки по окружению | `is_remote`, `env_description` |
| Пропуск утверждения опасной команды | `skip_container_guards` |
| Обработка пути контейнера/cwd | `is_container` |
| Синхронизированный перевод пути к файлу кэша | `cache_path_base` |
| Секретное удаление из порожденных подпроцессов | `strip_env_keys` |
| Изоляция песочницы для каждого сеанса (`container_persistent: false`) | `session_isolated_when_nonpersistent` |

Объявление этих флагов у провайдера закрывает классическую ситуацию «новый бэкэнд пропущен».
Класс ошибки «сайт классификации N» — ядро обращается к реестру при каждом
site вместо жестко закодированного списка имен.

## Минимальный поставщик

```python title="~/.hermes/plugins/acmebox/__init__.py"
from agent.terminal_env_provider import TerminalEnvironmentProvider


class AcmeBoxEnvironment:
    """Must satisfy the BaseEnvironment duck-typed contract."""

    def __init__(self, cwd, timeout, task_id):
        self.cwd, self.timeout, self.task_id = cwd, timeout, task_id

    def execute(self, command, timeout=None, **kwargs):
        ...  # run the command in the sandbox
        return {"output": "...", "exit_code": 0}

    def cleanup(self):
        ...  # tear down / detach


class AcmeBoxProvider(TerminalEnvironmentProvider):
    name = "acmebox"
    display_name = "AcmeBox"
    is_remote = True          # commands don't run on the host
    is_container = True       # container-style path/cwd semantics

    @property
    def description(self):
        return "Run commands in an AcmeBox cloud sandbox."

    @property
    def cache_path_base(self):
        return "~/.hermes"    # where synced cache files land, or None

    @property
    def strip_env_keys(self):
        return frozenset({"ACMEBOX_TOKEN"})

    def is_available(self):
        import importlib.util, os
        return (
            importlib.util.find_spec("acmebox") is not None
            and bool(os.getenv("ACMEBOX_TOKEN"))
        )

    def create_environment(self, *, cwd, timeout, task_id="default",
                           image=None, container_config=None, **kwargs):
        return AcmeBoxEnvironment(cwd, timeout, task_id)


def register(ctx):
    ctx.register_terminal_environment_provider(AcmeBoxProvider())
```

```yaml title="~/.hermes/plugins/acmebox/plugin.yaml"
name: acmebox
version: 0.1.0
description: AcmeBox cloud sandbox terminal backend
kind: backend
```

Включите его, выберите его, запустите:

```bash
hermes plugins enable acmebox
hermes config set terminal.backend acmebox
```

## Правила

- **Зарезервированные имена.** Регистрации, которые конфликтуют со встроенным серверным именем.
  (`local`, `docker`, `singularity`, `modal`, `managed_modal`, `daytona`,
  `vercel_sandbox`, `ssh`) отклонены. Плагины расширяют набор серверной части; они
  никогда не скрывайте бэкэнды внутри дерева.
- **`create_environment` должен принять `**kwargs`** и игнорировать неизвестные ключи —
  контракт прямой совместимости, который позволяет заводской сигнатуре развиваться без
  взлом старых плагинов.
- **`is_available()` / `probe()` должно быть дешево.** Никаких сетевых вызовов — они работают.
  во время проверок требований и отрисовки пользовательского интерфейса.
- **Fail-soft везде.** Атрибут провайдера, который повышает значение, рассматривается как
  ядром по умолчанию (например, повышение `skip_container_guards` сохраняет
  уровень утверждения включен). Не полагайтесь на исключения для потока управления.
- **Секреты принадлежат `strip_env_keys`.** Ваш токен поставщика никогда не должен быть
  читаемый командой оболочки, созданной моделью; листинг лишает его всех
  порождает подпроцесс безоговорочно, как и встроенный `MODAL_*`/
  `DAYTONA_API_KEY` обработка.

## Контракт объекта среды

`create_environment()` возвращает объект, удовлетворяющий тому же типу утиного типа.
интерфейс как `tools.environments.base.BaseEnvironment`:

- `execute(command, timeout=None, ...)` → `{"output": str, "exit_code": int}`
- `cleanup()` — освободить ресурсы; вызов при разрыве сеанса / простое жатва
- Необязательно: перехватчики сохраняемости, зеркально отображающие встроенные облачные серверы.

Рекомендуется создать подкласс `BaseEnvironment` (вы наследуете общий класс синхронизации файлов).
и сантехника фонового процесса), но это не обязательно.

## Семантика изоляции сеанса

Если ваша песочница **возобновляется по имени** (надежная виртуальная машина, серверная часть повторно подсоединяется
to), установите `session_isolated_when_nonpersistent = True`. С
`terminal.container_persistent: false`, каждый сеанс получает свой собственный
идентификатор песочницы вместо совместного использования одного — без этого два независимых
эфемерные прогоны могут подключить одну работающую виртуальную машину и удалить ее из-под каждой
другое.