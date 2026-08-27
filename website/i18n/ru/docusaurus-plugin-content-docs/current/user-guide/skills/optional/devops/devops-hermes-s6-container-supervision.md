---
title: Hermes S6 Container Supervision — изменение или отладка сервисов s6 в образе
  Hermes Docker.
sidebar_label: Hermes S6 Container Supervision
description: Изменение или отладка сервисов s6 в образе Hermes Docker.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Гермес S6 Контейнерный надзор

Измените или отладьте сервисы s6 в образе Hermes Docker.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/devops/hermes-s6-container-supervision` |
| Путь | `optional-skills/devops/hermes-s6-container-supervision` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux |
| Теги | `docker`, `s6`, `supervision`, `gateway`, `profiles` |
| Сопутствующие навыки | [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Hermes s6-overlay Надзор за контейнером

## Когда использовать этот навык

Используйте этот навык, когда работаете над:
- Добавление или удаление статического сервиса в образе Hermes Docker (то, что должно контролироваться при каждом запуске контейнера, например, панель мониторинга)
- Диагностика того, почему шлюз каждого профиля не запускается, не перезапускается или не сохраняется `docker restart`.
- Понимание того, почему CMD контейнера равен `/opt/hermes/docker/main-wrapper.sh` и как аргументы с ведущим тире достигают программы пользователя.
- Изменение загрузочных сценариев `cont-init.d` (переназначение UID, заполнение тома, согласование профилей)
- Изменение визуализированного сценария запуска для шлюзов каждого профиля (этап 4).

Если вы просто используете агент Hermes и хотите использовать Docker, см. `website/docs/user-guide/docker.md`.

## Архитектура с первого взгляда

<!-- ascii-guard-ignore -->
```
/init                                  ← PID 1 (s6-overlay v3.2.3.0)
├── cont-init.d                        ← oneshot setup, runs as root
│   ├── 01-hermes-setup                ← docker/stage2-hook.sh
│   │   ├── UID/GID remap
│   │   ├── chown /opt/data
│   │   ├── chown /opt/data/profiles (every boot)
│   │   ├── seed .env / config.yaml / SOUL.md
│   │   └── skills_sync.py
│   └── 02-reconcile-profiles          ← hermes_cli.container_boot
│       ├── chown /run/service (hermes-writable for runtime register)
│       └── walk $HERMES_HOME/profiles/<name>/gateway_state.json
│           → recreate /run/service/gateway-<name>/
│           → auto-start only those with prior_state == "running"
│
├── s6-rc.d (static services, in /etc/s6-overlay/s6-rc.d/)
│   ├── main-hermes/run                ← exec sleep infinity (no-op slot)
│   └── dashboard/run                  ← if HERMES_DASHBOARD=1, runs `hermes dashboard`
│
├── /run/service (s6-svscan watches; tmpfs)
│   ├── gateway-coder/                 ← runtime-registered per-profile
│   │   ├── type        ("longrun")
│   │   ├── run         ("#!/command/with-contenv sh ... exec s6-setuidgid hermes hermes -p coder gateway run")
│   │   ├── down        (marker — present means "registered but don't auto-start")
│   │   └── log/run     (s6-log → $HERMES_HOME/logs/gateways/coder/current)
│   └── ...
│
└── CMD ("main program")               ← /opt/hermes/docker/main-wrapper.sh
    └── routes user args: bare exec | hermes subcommand | hermes (no args)
        — exec'd by /init with stdin/stdout/stderr inherited (TTY for --tui)
```
<!-- ascii-guard-ignore-end -->

## Ключевые файлы

| Путь | Роль |
|---|---|
| `Dockerfile` | s6-overlay install + cont-init.d проводка + `ENTRYPOINT ["/init", "/opt/hermes/docker/main-wrapper.sh"]` |
| `docker/stage2-hook.sh` | «Старая логика точки входа» — переназначение UID, chown, начальное значение, синхронизация навыков. Запускается как cont-init.d/01-hermes-setup. |
| `docker/cont-init.d/02-reconcile-profiles` | Вызывает `hermes_cli.container_boot` при каждой загрузке для восстановления слотов шлюза профиля из постоянного тома. |
| `docker/main-wrapper.sh` | CMD контейнера. Маршрутизирует пользовательские аргументы, передает в Hermes через `s6-setuidgid`, выполняет выбранную программу. |
| `docker/s6-rc.d/main-hermes/run` | No-op `sleep infinity` — слот существует, поэтому пользовательский пакет s6-rc действителен; main hermes работает как CMD, а не как контролируемая служба. |
| `docker/s6-rc.d/dashboard/run` | Условное обслуживание — `exec sleep infinity`, если только `HERMES_DASHBOARD` не является правдивым. |
| `docker/entrypoint.sh` | Прокладка обратной совместимости, которая `exec` является крючком stage2. Внешние сценарии, в которых жестко запрограммирован старый путь к точке входа, все еще работают. |
| `hermes_cli/service_manager.py` | `S6ServiceManager`: `register_profile_gateway`, `unregister_profile_gateway`, `start/stop/restart/is_running`, `list_profile_gateways`. |
| `hermes_cli/container_boot.py` | `reconcile_profile_gateways()` — ходит по постоянным профилям, регенерирует слоты s6, выдает `container-boot.log`. |
| `hermes_cli/gateway.py::_dispatch_via_service_manager_if_s6` | Перехватывает `hermes gateway start/stop/restart` и направляет к s6 при работе в контейнере. |

## Почему архитектура B (CMD в качестве основной программы, а не под контролем s6)

Первоначальный план (v1–v3) предусматривал, что основной Hermes будет работать как контролируемая служба s6-rc. Две настоящие механики s6-overlay v3 заблокировали это:

1. **Скрипты cont-init.d не получают аргументов CMD** — поэтому перехватчик stage2 не может проанализировать `docker run <image> chat -q "hi"`, чтобы установить `HERMES_ARGS` для использования скриптом службы `run`.
2. **`/run/s6/basedir/bin/halt` НЕ распространяет код выхода**, записанный в `/run/s6-linux-init-container-results/exitcode`. Контейнеры всегда выходят из 143 (SIGTERM) независимо от этого. Подтверждено skarnet (автор s6) в [выпуске № 477] (https://github.com/just-containers/s6-overlay/issues/477): _"если вы хотите завершить работу контейнера, вам нужно либо выполнить выход CMD, либо, если у вас нет CMD, написать нужный код выхода контейнера, а затем вызвать остановку"_.

Поэтому мы используем собственный шаблон CMD s6-overlay: `ENTRYPOINT ["/init", "/opt/hermes/docker/main-wrapper.sh"]`. /init автоматически добавляет обертку к пользовательским аргументам — поэтому `docker run <image> --version` становится `/init main-wrapper.sh --version`, а `--version` не перехватывается оболочкой POSIX /init. Обертка передается в Hermes через `s6-setuidgid`, затем выполняется выбранная программа. Код выхода программы становится кодом выхода контейнера, точно соответствующим контракту Tini до версии s6.

Компромисс: главный Гермес остается без присмотра в 6 сезоне. Это точно соответствует его поведению под Tini (изображение до s6). Контроль информационной панели — единственная **новая** гарантия, а шлюзы каждого профиля под `/run/service/` получают полный контроль.

## Быстрые рецепты

### Убедитесь, что s6 имеет PID 1 в работающем контейнере

```sh
docker exec <c> sh -c 'cat /proc/1/comm; readlink /proc/1/exe'
# Expect: s6-svscan or init / /package/admin/s6/.../s6-svscan
```

### Проверка службы шлюза профилей

```sh
# /command/ isn't on docker-exec PATH — use absolute path
docker exec <c> /command/s6-svstat /run/service/gateway-<name>
# "up (pid …) … seconds"            → running
# "down (exitcode N) … seconds, normally up, want up, …" → s6 wants it up but the process keeps exiting (crash loop)
# "down … normally up, ready …"     → user stopped it
```

### Включение/выключение службы вручную

```sh
docker exec <c> /command/s6-svc -u /run/service/gateway-<name>   # up
docker exec <c> /command/s6-svc -d /run/service/gateway-<name>   # down
docker exec <c> /command/s6-svc -t /run/service/gateway-<name>   # SIGTERM (restart)
```

### Посмотрите журнал примирителя cont-init

```sh
docker exec <c> tail -n 50 /opt/data/logs/container-boot.log
# 2026-05-21T06:18:05+0000 profile=coder prior_state=running action=started
# 2026-05-21T06:18:05+0000 profile=writer prior_state=stopped action=registered
```

### Добавляем новый статический сервис

1. Создайте `docker/s6-rc.d/<name>/type` с помощью `longrun\n` и `docker/s6-rc.d/<name>/run` (используйте `#!/command/with-contenv sh` + `# shellcheck shell=sh`).
2. Заходим в Hermes через `s6-setuidgid hermes` в начале запуска (если вам специально не нужен root).
3. Создайте пустой `docker/s6-rc.d/<name>/dependencies.d/base`, чтобы он ждал базового пакета.
4. Создайте пустой `docker/s6-rc.d/user/contents.d/<name>`, чтобы он присоединился к пользовательскому пакету.
5. `COPY docker/s6-rc.d/` в Dockerfile подхватывает его автоматически — никаких других изменений.

### Изменение команды запуска шлюза для каждого профиля

Отредактируйте `S6ServiceManager._render_run_script` в `hermes_cli/service_manager.py`. Функция также вызывается `hermes_cli/container_boot.py::_register_service` во время согласования загрузки, поэтому это единственный источник истины. Обновите соответствующее утверждение в `tests/hermes_cli/test_service_manager.py::test_s6_register_creates_service_dir_and_triggers_scan`.

### Запустите тестовую программу Docker

```sh
docker build -t hermes-agent-harness:latest .
HERMES_TEST_IMAGE=hermes-agent-harness:latest scripts/run_tests.sh tests/docker/ -v
# Expect 19 passed, 0 xfailed against the s6 image
```

Обвязка находится в `tests/docker/` и пропускается, когда Docker недоступен. Тайм-аут для каждого теста увеличен до 180 с (см. `tests/docker/conftest.py`).

## Распространенные ошибки

### «команда не найдена» через `docker exec`

`/command/` (куда s6-overlay помещает свои двоичные файлы) находится в PATH только для процессов, порожденных деревом контроля — Services, cont-init.d, main-wrapper.sh. `docker exec <c> s6-svstat …` завершится ошибкой с сообщением «команда не найдена»; всегда используйте абсолютный путь `/command/s6-svstat`. Бинарный файл `hermes` работает, поскольку Dockerfile добавляет `/opt/hermes/.venv/bin` к среде выполнения `ENV PATH`.

### Владение каталогом профилей

Согласователь cont-init работает как hermes (`s6-setuidgid hermes` в `02-reconcile-profiles`). Если каталог профиля оказывается принадлежащим пользователю root (например, потому что `docker exec <c> hermes profile create …` по умолчанию запускался как root), средство согласования не сможет прочитать SOUL.md и выдаст ошибку `PermissionError`. Смягчение: `stage2-hook.sh` идемпотентно передает `$HERMES_HOME/profiles` Hermes при **каждой** загрузке. Не удаляйте этот блок.

### Файлы, написанные `docker exec`, принадлежат пользователю root

`docker exec` по умолчанию имеет root-права. Либо передайте `--user hermes`, либо используйте команду chown stage2 при следующей перезагрузке. Не записывайте файлы под `$HERMES_HOME/profiles/<name>/` вручную от имени пользователя root — следующий этап согласования удалит их, но текущие операции могут привести к постоянным ошибкам.

### Сервисный слот существует, но s6-svstat сообщает: «s6-supervise не работает»

Каталог службы находится в tmpfs и был удален при перезапуске контейнера. Либо средство согласования cont-init еще не запущено (дайте ему время после `docker restart`), либо оно не удалось. Проверьте `docker logs <c> | grep '02-reconcile'`.

### Шлюз запускается и немедленно завершает работу (`down (exitcode 1)` в svstat)

Скорее всего, в профиле не настроена модель или аутентификация. Сервисный слот правильный — сам шлюз не настроен. Сначала запустите `hermes -p <profile> setup`. Супервизор s6 будет продолжать его перезапускать; это желаемое поведение (когда вы исправите конфигурацию, следующая попытка будет успешной и продолжится).

### Согласователь пропустил профиль

Устройство согласования указывает на **присутствие `SOUL.md`** в качестве маркера «реального профиля». `hermes profile create` всегда его закладывает. Если в каталоге профиля отсутствует SOUL.md (случайный каталог, частичное восстановление, выполнение резервного копирования), средство согласования намеренно пропускает его. Добавьте `SOUL.md` (даже пустой), чтобы снова принять участие.

### "Помогите, контейнер выходит из 143!"

Проверьте, не вызывает ли что-то `s6-svscanctl -t` или `/run/s6/basedir/bin/halt` — оба заставляют /init начать этап завершения работы 3, но возвращают 143 (SIGTERM), а не желаемый код выхода. Это был поворот архитектуры Фазы 2 от A к B. Для завершения работы контейнера с реальным кодом выхода вы должны позволить CMD (main-wrapper.sh) завершить работу в обычном режиме; **не** пытайтесь контролировать выход из сценария завершения.

## Сопутствующие навыки

- `hermes-agent-dev`: Общая навигация по кодовой базе агента Hermes.
- `hermes-tool-quirks`: специальные обходные пути для инструментов Hermes (sed/grep/и т. д.) — загружаются при отладке взаимодействия стека s6 со встроенными инструментами Hermes.