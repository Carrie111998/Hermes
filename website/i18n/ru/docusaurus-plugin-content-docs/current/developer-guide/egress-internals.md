---
sidebar_position: 14
title: Внутреннее устройство выходного прокси
description: Как выходной межсетевой экран с железным прокси интегрируется с Hermes
  — структура модуля, жизненный цикл, инварианты безопасности и точки расширения
---

# Внутреннее устройство выходного прокси

На этой странице описана архитектура исходящего брандмауэра ввода учетных данных (`hermes egress` / Iron-Proxy) с точки зрения участника / автора плагина. Документы по настройке и использованию для конечных пользователей доступны по адресу [Egress proxy](../user-guide/egress/iron-proxy.md).

Модель угроз и общий дизайн представлены на странице пользователя; на этой странице рассказывается о том, *как* оно подключено, где находится код, связанный с безопасностью, и какие инварианты вам придется сохранить, если вы к нему прикоснетесь.

## Расположение модуля

```text
agent/proxy_sources/iron_proxy.py     Core: binary install, CA gen, config build,
                                       subprocess lifecycle, mappings I/O, PID/nonce
                                       defense.  Pure-function surface where possible.

hermes_cli/proxy_cli.py               Wizard + slash command handlers.
                                       `hermes egress {install,setup,start,stop,
                                       status,disable,config}`.  Wires the
                                       core module into argparse.

hermes_cli/main.py:_dispatch_egress   Top-level subparser dispatcher.
                                       dest='egress_command' (intentionally
                                       disjoint from the inbound OAuth
                                       `hermes proxy` subparser, which uses
                                       dest='proxy_command').

hermes_cli/config.py: proxy schema    The `proxy:` block in DEFAULT_CONFIG.
                                       Adding a knob means: add it here, add a
                                       wizard prompt or `setdefault` in
                                       proxy_cli.cmd_setup, and document it
                                       in the user-guide page.

tools/environments/docker.py
  _egress_proxy_args_for_docker()     Builds the volume_args / env_overrides /
                                       host_args triple that the Docker backend
                                       injects when `proxy.enabled: true`.

  DockerEnvironment.__init__          Docker-side merge logic: collision
                                       detection against critical egress vars,
                                       NODE_OPTIONS append-merge via the
                                       _HERMES_EGRESS_NODE_OPTIONS_APPEND
                                       sentinel, enforce_on_docker precedence.

tests/test_iron_proxy.py              Hermetic tests (~70).  Binary install
                                       path, config build, mappings I/O,
                                       subprocess lifecycle, docker arg builder,
                                       deny CIDR defaults, bind policy, CA
                                       TOCTOU, ensure_audit_log behaviour, etc.

tests/test_iron_proxy_cli.py          CLI handler unit tests (~20).  Argparse
                                       wiring, fail-loud paths, BWS refresh
                                       wire-up, dest='egress_command'
                                       regression guard.

tests/test_iron_proxy_e2e.py          Live E2E (gated on HERMES_RUN_E2E=1).
                                       Real iron-proxy binary, real curl,
                                       end-to-end token swap verified.
```

## Жизненный цикл

```text
hermes egress install
  -> agent.proxy_sources.iron_proxy.install_iron_proxy(force=...)
       Downloads pinned tarball + checksums.txt from GitHub Releases.
       SHA-256 verification before extraction.
       tarfile.extract(..., filter="data") on Python 3.12+ (PEP 706);
         falls back to plain extract on older Python with member-name
         sanitisation via _pick_tar_member.
       Stage into ~/.hermes/bin/.iron-proxy_XXXX, chmod 755, os.replace
         to ~/.hermes/bin/iron-proxy (atomic).
       _VERSION_CACHE.pop(target) so a forced reinstall re-probes
         --version on next call.

hermes egress setup [--from-bitwarden | --no-bitwarden] [--rotate-tokens]
  -> proxy_cli.cmd_setup
       Step 1. find_iron_proxy(install_if_missing=False) -> install if absent.
       Step 2. ensure_ca_cert()
                 Run openssl genrsa + req via subprocess.
                 Write CA key via os.open(O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW, 0o600)
                   + os.replace.  Never exists on disk under default umask.
                 Write CA cert with 0o644 (public).
       Step 3. discover_provider_mappings() or pull names from BWS via
                 fetch_bitwarden_secrets() when --from-bitwarden.
                 merge_mappings(existing=load_mappings(), discovered,
                                rotate=args.rotate_tokens) preserves prior
                 tokens unless --rotate-tokens is passed.
                 discover_uncovered_providers() and surface warnings.
       Step 4. ensure_audit_log(audit_log_path)   # raises on OSError
               build_proxy_config(...) with defaults applied at the call site
                 (deny CIDRs default, bind policy from _default_http_listen).
               write_proxy_config(cfg)            # atomic via .tmp + os.replace, 0o600
               write_mappings(mappings)           # atomic, 0o600
       Step 5. proxy_cfg["enabled"] = True; credential_source preservation logic
               (do NOT silently downgrade bitwarden -> env on re-run);
               save_config(cfg).

hermes egress start
  -> proxy_cli.cmd_start
       Pre-checks (refuse-start path):
         - credential_source=bitwarden? -> pre-validate access_token_env + project_id
       -> iron_proxy.start_proxy(
            refresh_secrets_from_bitwarden=...,
            bitwarden_config=...,
          )
            existing=_read_pid(); if alive, idempotent return.
            _build_proxy_subprocess_env(...):  ALLOWLIST + mapped real_env_names,
              strip HTTPS_PROXY/etc. to avoid recursion, optional BWS refresh
              (raises on missing values unless allow_env_fallback=true).
            Plant nonce: _proxy_nonce = sha256(urandom(16)); env[NONCE_ENV] = ...
            Open log_path via O_NOFOLLOW + 0o600 + st_uid check.
            Popen with stdin=DEVNULL, stdout=log_fd, stderr=STDOUT,
              start_new_session=True (POSIX).
            Close parent's log_fd in finally.
            _write_pidfile_safely(pidfile, proc.pid)
              O_EXCL + O_NOFOLLOW + uid check + persisted nonce sidecar.
              FileExistsError -> discriminate live vs stale, retry once if stale.
            Install SIGINT/SIGTERM handlers (main-thread only).
            Poll loop (do-while shape):
              while True:
                if proc.poll() is not None: tail log + unlink pidfile + raise
                if _port_listening(probe_host, tunnel_port): break  # probe_host = configured bind host
                if time.time() >= deadline: break  (do-while: checked AFTER first probe)
                time.sleep(0.1)
            If not listening at exit: _kill_and_wait(proc) + unlink pidfile + raise.

hermes egress stop
  -> iron_proxy.stop_proxy
       _read_pid + _pid_alive guard.
       starttime_before = _pid_proc_starttime(pid)   # Linux only; None elsewhere
       os.kill(pid, SIGTERM)
       Wait up to 5s for graceful exit.
       After grace: re-check starttime + _pid_alive.
         If recycled (starttime drift OR _pid_alive False), DO NOT SIGKILL.
         Otherwise os.kill(pid, _KILL_SIGNAL).
       _cleanup_state_files: unlink pidfile + nonce sibling.
```

## Инварианты безопасности

Это несущие свойства.  Если вы прикоснетесь к модулю, вы должны их сохранить.  Там, где есть регрессионный тест, он имеет название.

### разрешения файловой системы

| Путь | Режим | Тест |
|---|---|---|
| `~/.hermes/proxy/` (реж.) | `0o700` | `test_proxy_state_dir_is_0o700` |
| `ca.key` | `0o600` | `test_ca_key_created_with_0o600` |
| `ca.crt` | `0o644` | (неявно; вызов chmod в `ensure_ca_cert`) |
| `proxy.yaml` | `0o600` | (chmod после атомарного переименования в `write_proxy_config`) |
| `mappings.json` | `0o600` | (chmod после атомарного переименования в `write_mappings`) |
| `iron-proxy.pid` | `0o600` | (режим `os.open(..., 0o600)` в `_write_pidfile_safely`) |
| `iron-proxy.nonce` | `0o600` | (режим `os.open(..., 0o600)` в `_write_pidfile_safely`) |
| `audit.log` | `0o600` | `test_ensure_audit_log_creates_with_0o600` |
| `iron-proxy.log` | `0o600` | (`os.open(..., 0o600)` + `fchmod`) |

Все пути записи используют проверку `os.open(O_WRONLY | O_CREAT | O_NOFOLLOW, 0o600)` + `os.fstat().st_uid`.  `shutil.copy2` + `os.chmod` запрещено, поскольку оно пропускает окно маски по умолчанию.

### Минимизация окружения подпроцесса

`_build_proxy_subprocess_env` НЕ ДОЛЖЕН использовать `os.environ.copy()`.  Список разрешенных — `_PROXY_SUBPROCESS_ENV_ALLOWLIST` (PATH, HOME, локаль и т. д.) плюс имена окружения, на которые ссылается `load_mappings()`.  Все остальное остается на хосте.

Регрессия: `test_subprocess_env_strips_unrelated_secrets`, `test_subprocess_env_strips_proxy_recursion_vars`, `test_subprocess_env_keeps_infrastructure_vars`.

### Политика привязки

`_default_http_listen` возвращает список из одного элемента: в Linux IP-адрес шлюза моста Docker (контейнеры достигают прокси через `host.docker.internal:host-gateway`, который разрешается в шлюз моста — там петлевая привязка недоступна изнутри контейнеров); на macOS/Windows Docker Desktop — петлевая проверка (VPNkit направляет `host.docker.internal` на хост).  Linux без обнаруживаемого моста docker0 возвращается к шлейфу с предупреждением.  Никогда `0.0.0.0`, никогда `:PORT` (INADDR_ANY).

`_detect_docker_bridge_ip` проверяет через `ipaddress.IPv4Address` и отклоняет `is_unspecified` / `is_loopback` / `is_multicast` / `is_reserved` / `is_link_local` / `is_global`.  Враждебная прокладка `ip` в PATH не может внедрить `0.0.0.0`.

**ограничение схемы v0.39 и роли прослушивателя (проверено в реальном времени на соответствие двоичному файлу):** структура `config.Proxy` двоичного файла имеет только поля прослушивателя в единственном числе — список `http_listens` (множественное число) отсутствует.  `tunnel_listen` — прослушиватель CONNECT + MITM (какой трафик попадает на `HTTPS_PROXY`); `http_listen` обрабатывает только пересылку HTTP в абсолютной форме (отправленное на него сообщение CONNECT передается в восходящем направлении как обычный запрос и 400).  Таким образом, `build_proxy_config` связывает `tunnel_listen` с `tunnel_port` и `http_listen` с `tunnel_port + 1`, оба на узле привязки платформы.  Серверная часть Docker устанавливает для `HTTPS_PROXY` значение `tunnel_port` и для `HTTP_PROXY` значение `tunnel_port + 1`.

Зонды работоспособности (цикл опроса `start_proxy`, `get_status`) считывают настроенный хост привязки через `_read_http_listen_from_config()` и проверяют ЭТОТ хост — жестко закодированный зонд обратной связи сообщит о работоспособном демоне, связанном с мостом, как мертвом.

Регрессия: `test_default_bind_is_loopback_not_zero_zero` (утверждает отсутствие INADDR_ANY И что `http_listens` НЕ находится в визуализированном yaml), `test_default_bind_uses_docker_bridge_on_linux`, `test_default_bind_falls_back_to_loopback_without_bridge`, `test_default_bind_is_loopback_on_macos`, `test_detect_docker_bridge_ip_rejects_dangerous` (параметризовано более чем 8 входными данными атаки).

### Коллизия портов метрик

`metrics.listen` по умолчанию имеет значение `:9090` в Iron-Proxy v0.39 — тот же порт, что и порт Hermes по умолчанию `tunnel_port: 9090`.  `build_proxy_config` ДОЛЖЕН явно закрепить `metrics.listen: 127.0.0.1:0`, чтобы привязка метрик получала эфемерный порт обратной связи, который никогда не может конфликтовать с прослушивателем прокси, независимо от выбранного оператором `tunnel_port`.

Регрессия: `test_metrics_listener_pinned_to_loopback_ephemeral`.

### Запретить CIDR по умолчанию

`_DEFAULT_UPSTREAM_DENY_CIDRS` охватывает обратную связь (v4 + v6), локальную связь (включая IMDS по адресу 169.254.169.254 и форму с отображением IPv4-v6), RFC1918, IPv6 ULA, CGNAT и диапазон тестов RFC2544.  `build_proxy_config(..., upstream_deny_cidrs=None)` ДОЛЖЕН выдать значение по умолчанию; только явный пустой список отказывается.

Регрессия: `test_default_deny_cidrs_present_when_unspecified`, `test_default_deny_includes_ipv4_mapped_v6`.

### Журнал аудита сбой-громкий

`ensure_audit_log` повышает `RuntimeError` на любом `OSError`.  В закрепленной версии 0.39 демон никогда не записывает этот файл (нет поля `log.audit_path`), поэтому `cmd_setup` рассматривает сбой как ПРЕДУПРЕЖДЕНИЕ (файл не является несущим до обновления версии) и квалифицирует строку успеха как «зарезервированную».  Когда вывод перейдет в версию с `log.audit_path`, вернитесь к нему: предварительное создание становится несущим нагрузку для гарантии 0o600 из первого байта, и мастер снова должен громко завершить работу.

**Ограничение схемы v0.39:** `log.audit_path` НЕ является полем в структуре `config.Log` Iron-Proxy v0.39, поэтому `build_proxy_config` принимает kwarg `audit_log`, но НЕ передает его в визуализируемый yaml.  Записи каждого запроса в версии 0.39 размещаются в `iron-proxy.log` вместе с событиями уровня демона.  Файл `audit.log` по-прежнему предварительно создается в `0o600` с помощью `O_NOFOLLOW`, поэтому контракт конфиденциальности сохраняется, когда закрепленная версия сталкивается с версией, поддерживающей отдельный поток.

Регрессия: `test_ensure_audit_log_raises_on_immutable_parent`, `test_audit_log_kwarg_does_not_inject_audit_path_v039`.

### Режим Bitwarden сбой-громкий

Когда `credential_source: bitwarden` И `proxy.allow_env_fallback: false` (по умолчанию):
- Отсутствует токен доступа. env var -> `cmd_start` отказывается.
- Отсутствует `project_id` -> `cmd_start` отказывается.
- `bws secret list` не возвращает значений для одного или нескольких сопоставленных поставщиков -> `_build_proxy_subprocess_env` повышает значение.

Возврат к хосту env в режиме BW повторно вводит именно ту ошибку устаревания, которую предназначен путь BW.

Регрессия: `test_cmd_start_refuses_when_bitwarden_token_missing` (уровень CLI); утверждения строгого режима в `_build_proxy_subprocess_env` (уровень демона).

### обнаружение столкновений docker_env

Когда `enforce_on_docker: true`, `docker_env` переопределяет любую из переменных, управляющих выходом (HTTPS_PROXY, SSL_CERT_FILE, NODE_EXTRA_CA_CERTS и т. д.) ИЛИ любой сопоставленный `real_env_name` (OPENROOUTER_API_KEY и т. д.), вызывает `RuntimeError` ДО запуска контейнера.

Регрессия: `test_docker_env_collision_with_proxy_raises_when_enforce`.

### Защита от повторного использования PID

`_pid_alive` ДОЛЖЕН проконсультироваться либо с внутрипроцессным `_proxy_nonce` (случай того же процесса), ИЛИ с дисковым `iron-proxy.nonce` (случай перекрестного интерфейса командной строки), прежде чем доверять совпадению базового имени `argv[0]`.  `stop_proxy` ДОЛЖЕН перепроверить `/proc/<pid>/stat` время начала перед SIGKILL и подавить сигнал при отклонении времени начала.

Регрессия: `test_stop_proxy_suppresses_sigkill_on_pid_recycle`, `test_pid_proc_starttime_parses_comm_with_parens`, `test_persisted_nonce_roundtrip`.

### Сохранение токена при перенастройке

`merge_mappings(existing, discovered, rotate=False)` ДОЛЖЕН возвращать предыдущие токены для перекрывающихся поставщиков.  Повторный запуск `hermes egress setup` не может автоматически запустить песочницу 401.  `--rotate-tokens` – это явное согласие.

Регрессия: `test_merge_mappings_preserves_existing_tokens`, `test_merge_mappings_rotate_mints_fresh_tokens`.

### `credential_source` сохранение

`cmd_setup` НЕ ДОЛЖЕН понижать версию `credential_source: bitwarden` до `env` при повторном запуске без явного флага `--no-bitwarden`.  Запуск `hermes egress setup` (без флага) сохраняет все, что было настроено ранее.

Протестировано с помощью потока `cmd_setup` в тестах CLI (путь сохранения битов используется, когда за `--from-bitwarden` следует простой повторный запуск `setup`).

## Точки расширения

### Добавление нового поставщика токенов на предъявителя

`_BEARER_PROVIDERS` в `iron_proxy.py` сопоставляет имя переменной env -> кортеж вышестоящих хостов.  Добавление записи делает ее доступной для обнаружения `discover_provider_mappings()`; мастер автоматически создает для него токен, когда присутствует переменная env.

```python
_BEARER_PROVIDERS: Dict[str, Tuple[str, ...]] = {
    ...,
    "MY_PROVIDER_API_KEY": ("api.myprovider.com",),
}
```

Также обновите `_DEFAULT_ALLOWED_HOSTS`, чтобы прокси-сервер по умолчанию разрешал восходящий поток.  Запустите `test_discover_provider_mappings_*` для подтверждения.

### Добавление нового поставщика токенов заголовка (семейство x-api-key)

Если провайдер выполняет аутентификацию с помощью статического заголовка NON-Authorization (например, `x-api-key` Anthropic, `api-key` Azure или `x-goog-api-key` Gemini), добавьте его в `_HEADER_AUTH_PROVIDERS` — `secrets.replace.match_headers` железного прокси предназначен для произвольных имен заголовков, поэтому это первоклассные замененные провайдеры:

```python
_HEADER_AUTH_PROVIDERS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    ...,
    "MY_PROVIDER_API_KEY": {
        "hosts": ("api.myprovider.com",),
        "match_headers": ("x-my-auth-header", "Authorization"),
        "aliases": (),
    },
}
```

Используйте `aliases` ТОЛЬКО для взаимозаменяемых имен env-var с *одними* учетными данными (например, `GOOGLE_API_KEY` для `GEMINI_API_KEY`) — псевдонимы объединяются в одно сопоставление, поскольку два правила `require: true` на одном хосте отклоняют запросы друг друга. Также обновите `_DEFAULT_ALLOWED_HOSTS`.

### Добавление нового поставщика аутентификации подписи (не раскрыто)

Если провайдер использует SigV4/SDK-подписи OAuth/запроса, статическая замена заголовка не может охватить это.  Добавьте переменную env в `_NON_BEARER_PROVIDERS`, чтобы мастер и `hermes egress status` предупреждали об этом:

```python
_NON_BEARER_PROVIDERS: Tuple[str, ...] = (
    ...,
    "MY_SIGNED_PROVIDER_ACCESS_KEY",
)
```

### Подключение железного прокси к бэкэнду, отличному от Docker

`_egress_proxy_args_for_docker` зависит от Docker.  Серверным станциям, которым нужна аналогичная проводка, нужен собственный аналог, который:

1. Читает `load_config().get("proxy", {})`; возвращает пустые аргументы, если `enabled` имеет значение false.
2. Звонит `iron_proxy.get_status()`; отображает семантику `enforce` на путях отказа `configured` / `pid` / `listening` / `ca_cert_path`.
3. Звонит `iron_proxy.load_mappings()`; отказывается монтировать, если пусто И `enforce_on_docker: true`.
4. Устанавливает семь переменных окружения (HTTPS_PROXY, NO_PROXY, REQUESTS_CA_BUNDLE, SSL_CERT_FILE, CURL_CA_BUNDLE, NODE_EXTRA_CA_CERTS, HERMES_EGRESS_PROXY) и переменные для каждого сопоставления `HERMES_PROXY_TOKEN_<NAME>`.
5. Распространяет сертификат CA в изолированную программную среду по пути, которому доверяет среда выполнения (обычно `/etc/ssl/certs/hermes-egress-ca.crt`).
6. Реализует обнаружение коллизий на основе конфигурации окружения, специфичной для серверной части пользователя.

Реализация Docker занимает ~150 строк; ожидайте аналогичного объема для Modal/Daytona/SSH.

### Подписка на события аудита по запросу

Iron-proxy записывает JSON с разделителями строк в `~/.hermes/proxy/iron-proxy.log` на текущей закрепленной версии v0.39 (объединенные записи демона и каждого запроса; см. «Вход в систему Iron-proxy v0.39» в руководстве пользователя).  Плагин/внешний наблюдатель может следить за этим файлом и реагировать на отказы в белом списке, секретные замены или ошибки восходящего потока.  Когда закрепленная версия переключается на версию, поддерживающую `log.audit_path`, поток каждого запроса перемещается на `audit.log`, и наблюдатели, подключенные к этому пути, активируются без каких-либо действий со стороны оператора.  Схема документирована по адресу [docs.iron.sh/audit](https://docs.iron.sh/audit) (ссылка).

## Тестирование

```bash
# Hermetic suite (no network, no real binary)
scripts/run_tests.sh tests/test_iron_proxy.py tests/test_iron_proxy_cli.py

# Live E2E (real binary, real curl, real CONNECT tunnel)
HERMES_RUN_E2E=1 scripts/run_tests.sh tests/test_iron_proxy_e2e.py

# Live PTY smoke against `hermes egress`
HERMES_HOME=/tmp/hermes-egress-test python3 -m hermes_cli.main egress --help
HERMES_HOME=/tmp/hermes-egress-test python3 -m hermes_cli.main egress setup --help
```

CLI использует argparse, поэтому `--help` — хороший первый тест на предмет «правильно ли зарегистрирован мой новый флаг».

## См. также

- Настройка с участием пользователя + устранение неполадок: [Выходной прокси](https://hermes-agent.nousresearch.com/docs/user-guide/egress/iron-proxy)
- Внутреннее устройство Docker: [Docker](https://hermes-agent.nousresearch.com/docs/user-guide/docker)
- Интеграция Bitwarden Secrets Manager: [`hermes secrets bitwarden`](https://hermes-agent.nousresearch.com/docs/user-guide/secrets/bitwarden)
- Справочник команд CLI: [`hermes egress`](https://hermes-agent.nousresearch.com/docs/reference/cli-commands#hermes-egress)
- Переменные среды, внедренные в песочницу: [Выходной прокси (внедренный в песочницу)](https://hermes-agent.nousresearch.com/docs/reference/environment-variables#egress-proxy-sandbox-injected)