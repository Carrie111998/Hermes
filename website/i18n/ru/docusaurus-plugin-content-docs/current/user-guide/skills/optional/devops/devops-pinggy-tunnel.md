---
title: Pinggy Tunnel — туннели локального хоста без установки через SSH через Pinggy
sidebar_label: Pinggy Tunnel
description: Туннели локального хоста без установки через SSH через Pinggy
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Туннель Пингги

Туннели локального хоста без установки через SSH через Pinggy.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/devops/pinggy-tunnel` |
| Путь | `optional-skills/devops/pinggy-tunnel` |
| Версия | `0.1.0` |
| Автор | Текниум (текниум1), Агент Гермеса |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Pinggy`, `Tunnel`, `Networking`, `SSH`, `Webhook`, `Localhost` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Навык Пингги Туннель

Предоставьте локальную службу (сервер разработки, приемник веб-перехватчиков, конечную точку MCP, демонстрацию) общедоступному Интернету с помощью обратного туннеля Pinggy SSH. Демон не требуется устанавливать — стандартный SSH-клиент пользователя подключается к `a.pinggy.io:443`, а Pinggy возвращает общедоступный URL-адрес HTTP/HTTPS.

Уровень бесплатного пользования: 60-минутные туннели, случайный поддомен, без регистрации. Уровень Pro (3 доллара США в месяц) предполагает подписку с использованием токена.

## Когда использовать

- Пользователь просит «разместить это локально», «поделиться моим сервером разработки», «сделать этот URL-адрес общедоступным», «туннельный порт N», «получить общедоступный URL-адрес для веб-перехватчика».
- Необходимо получить обратный вызов вебхука во время локальной задачи (Stripe, GitHub, Discord, AgentMail).
- Совместное использование разовой демонстрации HTTP (сервер MCP, конечная точка Ollama/vLLM, панель мониторинга) с удаленной стороной.
- На хосте есть SSH, но нет двоичного файла `cloudflared` / `ngrok`, и его установка будет излишним.

Если на хосте уже настроен `cloudflared`, отдайте предпочтение навыку `cloudflared-quick-tunnel` — срок действия быстрых туннелей Cloudflare не истекает через 60 минут.

## Предварительные условия

- `ssh` в PATH (`ssh -V`). По умолчанию в Linux, macOS и Windows 10+. Никакой другой установки.
- Локальная служба прослушивает `127.0.0.1:<port>` перед запуском туннеля. Pinggy будет возвращать URL-адреса, но они будут 502, пока не будет активирован локальный источник.

Необязательно:

- `PINGGY_TOKEN` переменная env для платных функций Pro (постоянный поддомен, собственный домен, несколько туннелей, без 60-минутного ограничения). Уровень бесплатного пользования не требует учетных данных.

## Краткий справочник

```bash
# Plain HTTP/HTTPS tunnel for port 8000 (free tier)
ssh -p 443 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
    -R0:localhost:8000 free@a.pinggy.io

# TCP tunnel (databases, raw SSH, etc.)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:5432 tcp@a.pinggy.io

# TLS tunnel (Pinggy can't decrypt — bring your own certs at origin)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:443 tls@a.pinggy.io

# Basic auth gate (b:user:pass)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "b:admin:secret+free@a.pinggy.io"

# Bearer token gate (k:token)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "k:mysecrettoken+free@a.pinggy.io"

# IP whitelist (w:CIDR)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "w:203.0.113.0/24+free@a.pinggy.io"

# Enable CORS + force HTTPS redirect
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "co+x:https+free@a.pinggy.io"

# Pro tier (persistent URL, no 60-min cap)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 "$PINGGY_TOKEN+a.pinggy.io"
```

## Процедура — запустить туннель и получить URL-адрес

Модель ДОЛЖНА использовать инструмент `terminal`. Туннель должен оставаться активным в течение всего срока использования общего ресурса, поэтому запустите его как фоновый процесс и проанализируйте общедоступный URL-адрес из стандартного вывода.

### 1. Убедитесь, что локальное происхождение включено.

```bash
curl -sI http://127.0.0.1:8000/ | head -1
# expect HTTP/1.x 200 (or any non-connection-refused response)
```

Если еще ничего не прослушивается, сначала запустите его (например, `python3 -m http.server 8000 --bind 127.0.0.1`). Пингги с радостью вернет URL-адрес, ни на что не указывающий — пользователь будет видеть 502, пока не появится источник.

### 2. Запустить туннель как фоновый процесс

Используйте `terminal(background=True)` и записывайте выходные данные в файл журнала (Pinggy печатает URL-адреса на стандартный вывод, а затем сохраняет соединение открытым):

```bash
LOG=/tmp/pinggy-8000.log
nohup ssh -p 443 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -R0:localhost:8000 free@a.pinggy.io \
    > "$LOG" 2>&1 &
echo $! > /tmp/pinggy-8000.pid
```

`StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` пропускает запрос ключа хоста при первом запуске. `ServerAliveInterval=30` предотвращает разрыв сеанса SSH из-за простоя NAT.

### 3. Анализируем URL-адрес из журнала

```bash
sleep 4
grep -oE 'https://[a-z0-9-]+\.[a-z]+\.pinggy\.link' /tmp/pinggy-8000.log | head -1
```

Ожидаемый результат выглядит так:

```
You are not authenticated.
Your tunnel will expire in 60 minutes.
http://yqycl-98-162-69-48.a.free.pinggy.link
https://yqycl-98-162-69-48.a.free.pinggy.link
```

Передайте пользователю URL-адрес `https://...pinggy.link`.

### 4. Проверьте

```bash
curl -sI https://<the-url>/ | head -3
# expect 200/302/whatever the local origin actually returns
```

Если вы получаете `502 Bad Gateway`, сеанс SSH запущен, но локальный источник не прослушивает — сначала исправьте шаг 1.

### 5. Демонтаж

```bash
kill "$(cat /tmp/pinggy-8000.pid)"
# or, if the pid file got lost:
pkill -f 'ssh -p 443 .* free@a\.pinggy\.io'
```

Если у вас есть session_id от `terminal(background=True)`, выберите `process(action='kill', session_id=...)`.

## Контроль доступа с помощью ключевых слов имени пользователя

Пингги помещает флаги управления в имя пользователя SSH, разделенное `+`. Всегда цитируйте весь аргумент `user@host`, если он содержит `+`:

| Ключевое слово | Эффект |
|---------|--------|
| `b:user:pass` | Базовый шлюз аутентификации HTTP |
| `k:token` | Шлюз заголовка токена-носителя (`Authorization: Bearer <token>`) |
| `w:CIDR` | Белый список IP-адресов (одиночный IP-адрес или CIDR, повторяемый) |
| `co` | Добавить `Access-Control-Allow-Origin: *` (CORS) |
| `x:https` | Force HTTPS — автоматическое перенаправление HTTP на HTTPS |
| `a:Name:Value` | Добавить заголовок запроса |
| `u:Name:Value` | Обновить заголовок запроса |
| `r:Name` | Удалить заголовок запроса |
| `qr` | Распечатайте QR-код URL-адреса на стандартный вывод (удобно для обмена мобильными устройствами) |

Комбинируйте свободно: `"b:admin:secret+co+x:https+free@a.pinggy.io"`.

## Веб-отладчик (необязательно)

Пингги может зеркально отразить входящий трафик на `localhost:4300` для проверки. Добавьте локальную пересылку к команде SSH:

```bash
ssh -p 443 -L4300:localhost:4300 -R0:localhost:8000 free@a.pinggy.io
```

Затем откройте `http://localhost:4300` в браузере, чтобы увидеть живые пары запрос/ответ.

## Подводные камни

- **жесткое ограничение в 60 минут на бесплатном уровне.** Сеанс SSH завершается через 60 минут; URL-адрес не работает. Для более длинных общих ресурсов используйте `PINGGY_TOKEN` (Pro) или автоматический перезапуск с помощью цикла оболочки (обратите внимание, что URL-адрес меняется при каждом перезапуске для бесплатного уровня).
- **URL-адрес бесплатного уровня является случайным и изменяется при перезапуске.** Не добавляйте его в закладки и не вставляйте в файл конфигурации. Каждый раз повторяйте анализ журнала.
- **Одновременные свободные туннели ограничены одним на каждый IP-адрес источника.** Запуск второго туннеля с той же машины обычно приводит к уничтожению первого. Уровень Pro поднимает это.
- **`+` в именах пользователей должны быть заключены в кавычки.** Пустой `ssh ... b:admin:secret+free@a.pinggy.io` работает в bash, но не работает под оболочками, которые обрабатывают `+` особым образом или при программной сборке. Всегда заключайте в двойные кавычки.
- **Не туннелируйте ничего конфиденциального без флага контроля доступа.** Простой HTTP-туннель доступен любому, у кого есть URL-адрес. Используйте `b:`, `k:` или `w:` для негосударственных услуг.
- **`process(action='log')` может пропустить вывод баннера SSH.** Пингги распечатывает URL-адреса, после чего сеанс SSH становится интерактивным. Всегда перенаправляйте файл журнала и `grep` напрямую в файл — тот же шаблон, что и `cloudflared-quick-tunnel`.
- **Запрос ключа хоста при первом запуске.** Конфигурация OpenSSH по умолчанию просит пользователя принять ключ хоста Pinggy. Всегда передайте `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` для автоматических запусков.
- **Туннели TCP и TLS возвращают пару `<subdomain>.a.pinggy.online:<port>`, а не URL-адрес https.** Анализ с другим регулярным выражением (`tcp://` и порт). Не думайте, что каждый туннель Pinggy является HTTP.
- **В режиме Pro требуется токен в качестве имени пользователя, а не флага.** Используйте `"$PINGGY_TOKEN+a.pinggy.io"` (без `free@`). С помощью токена вы также можете добавить `:persistent` для стабильного поддомена — см. `pinggy.io/docs/`.

## Рецепты

Составные узоры, сочетающие местное происхождение с туннелем Пингги. Каждый рецепт самодостаточен — запустите источник, запустите туннель, проанализируйте URL-адрес и передайте его пользователю.

### Рецепт 1 — Получение обратного вызова вебхука

Используйте это, когда внешней службе (Stripe, GitHub, Discord, AgentMail и т. д.) необходимо отправить POST на общедоступный URL-адрес во время локальной задачи.

```bash
# 1. Tiny capturing server: every request gets appended to /tmp/webhook-hits.log
cat >/tmp/webhook-server.py <<'PY'
import http.server, json, datetime, pathlib
LOG = pathlib.Path("/tmp/webhook-hits.log")
class H(http.server.BaseHTTPRequestHandler):
    def _capture(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        rec = {"t": datetime.datetime.utcnow().isoformat(), "path": self.path,
               "method": self.command, "headers": dict(self.headers), "body": body}
        with LOG.open("a") as f: f.write(json.dumps(rec) + "\n")
        self.send_response(200); self.send_header("content-type","application/json")
        self.end_headers(); self.wfile.write(b'{"ok":true}\n')
    def do_GET(self): self._capture()
    def do_POST(self): self._capture()
    def log_message(self,*a,**k): pass
http.server.HTTPServer(("127.0.0.1", 18080), H).serve_forever()
PY
nohup python3 /tmp/webhook-server.py >/tmp/webhook-server.log 2>&1 &
echo $! >/tmp/webhook-server.pid

# 2. Tunnel — bearer-token-gate so randos can't pollute the capture log
nohup ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -R0:localhost:18080 "k:$(openssl rand -hex 12)+free@a.pinggy.io" \
    >/tmp/webhook-pinggy.log 2>&1 &
echo $! >/tmp/webhook-pinggy.pid
sleep 5
URL=$(grep -oE 'https://[a-z0-9-]+\.[a-z]+\.pinggy\.link' /tmp/webhook-pinggy.log | head -1)
echo "Webhook URL: $URL"

# 3. While the agent works, watch hits land
tail -f /tmp/webhook-hits.log
```

Передайте `$URL` службе, которой необходимо вам позвонить. Разборка: `kill $(cat /tmp/webhook-server.pid) $(cat /tmp/webhook-pinggy.pid)`.

### Рецепт 2. Откройте доступ к серверу MCP через HTTP/SSE

Используйте, когда удаленному клиенту MCP (Claude Desktop на другом компьютере, редактору товарища по команде и т. д.) необходимо подключиться к серверу MCP, работающему на локальном компьютере. Работает только для серверов MCP, которые поддерживают HTTP-транспорт — серверы в режиме stdio не могут быть туннелированы.

```bash
# 1. Start the MCP server in HTTP mode (example: a FastMCP server on port 8765)
nohup python3 my_mcp_server.py --transport http --port 8765 \
    >/tmp/mcp-server.log 2>&1 &
echo $! >/tmp/mcp-server.pid

# 2. Tunnel with a bearer token — MCP traffic should not be open to the internet
TOKEN=$(openssl rand -hex 16)
nohup ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -R0:localhost:8765 "k:$TOKEN+free@a.pinggy.io" \
    >/tmp/mcp-pinggy.log 2>&1 &
echo $! >/tmp/mcp-pinggy.pid
sleep 5
URL=$(grep -oE 'https://[a-z0-9-]+\.[a-z]+\.pinggy\.link' /tmp/mcp-pinggy.log | head -1)
echo "MCP URL: $URL"
echo "Bearer token: $TOKEN"
```

Удаленный клиент подключается к `$URL` с помощью `Authorization: Bearer $TOKEN`. Собственная конфигурация клиента MCP Hermes: `{"transport": "http", "url": "<URL>", "headers": {"Authorization": "Bearer <TOKEN>"}}`.

### Рецепт 3 — Предоставьте локальную конечную точку LLM (Ollama/vLLM/llama.cpp)

Поделитесь локальной моделью с удаленным абонентом (другим агентом, телефоном, товарищем по команде). Ollama прослушивает `:11434`, vLLM и llama.cpp обычно на `:8000`.

```bash
# Pre-req: the model server is already running on 127.0.0.1:11434 (Ollama default)
TOKEN=$(openssl rand -hex 16)
nohup ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -R0:localhost:11434 "k:$TOKEN+co+free@a.pinggy.io" \
    >/tmp/llm-pinggy.log 2>&1 &
echo $! >/tmp/llm-pinggy.pid
sleep 5
URL=$(grep -oE 'https://[a-z0-9-]+\.[a-z]+\.pinggy\.link' /tmp/llm-pinggy.log | head -1)
echo "Endpoint: $URL"
echo "Token:    $TOKEN"

# Verify
curl -s "$URL/api/tags" -H "Authorization: Bearer $TOKEN" | head
```

`co` включает CORS, чтобы вызывающий браузер мог достичь конечной точки. Отбросьте `co` для абонентов, использующих только серверную часть. Для OpenAI-совместимой конечной точки vLLM/llama.cpp вызывающие стороны используют базовый URL-адрес `$URL/v1` с `Authorization: Bearer $TOKEN` — но обратите внимание, что Pinggy ничего не удаляет и не заменяет в теле, поэтому сервер модели сам видит токен Pinggy; локальный сервер должен быть настроен на игнорирование аутентификации (он уже находится на `127.0.0.1`) и позволить Пингги выполнять шлюзование.

### Рецепт 4. Поделитесь сервером разработки с одноразовым паролем

Самый быстрый шаблон «позволь товарищу по команде потыкать в мое беговое приложение». Случайный пароль, печатается один раз, исчезает при нажатии Ctrl-C.

```bash
PASS=$(openssl rand -base64 12 | tr -d '+/=' | head -c 12)
echo "Dev server password: $PASS"
ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -R0:localhost:3000 "b:dev:$PASS+co+x:https+free@a.pinggy.io"
# URL prints to the terminal. Share URL + password. Ctrl-C to tear down.
```

`b:dev:$PASS` пропускает URL-адрес с помощью базовой аутентификации HTTP. `x:https` принудительно активирует TLS. `co` добавляет CORS для интерфейсов SPA.

## Проверка

```bash
# End-to-end: spin up a trivial origin, tunnel it, hit it, tear down
python3 -m http.server 18000 --bind 127.0.0.1 >/tmp/origin.log 2>&1 &
ORIGIN_PID=$!

nohup ssh -p 443 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -R0:localhost:18000 free@a.pinggy.io >/tmp/pinggy-verify.log 2>&1 &
SSH_PID=$!

sleep 5
URL=$(grep -oE 'https://[a-z0-9-]+\.[a-z]+\.pinggy\.link' /tmp/pinggy-verify.log | head -1)
echo "URL: $URL"
curl -sI "$URL/" | head -1

kill "$SSH_PID" "$ORIGIN_PID"
```

Ожидается: URL-адрес `pinggy.link` и `HTTP/2 200` на завитке.