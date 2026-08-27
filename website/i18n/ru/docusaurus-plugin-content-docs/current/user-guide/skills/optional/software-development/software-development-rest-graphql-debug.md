---
title: 'Rest Graphql Debug — отладка API REST/GraphQL: коды состояния, аутентификация,
  схемы, воспроизведение.'
sidebar_label: Rest Graphql Debug
description: 'Отладка API REST/GraphQL: коды состояния, аутентификация, схемы, воспроизведение'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Остальная отладка Graphql

Отладка API REST/GraphQL: коды состояния, аутентификация, схемы, воспроизведение.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/software-development/rest-graphql-debug` |
| Путь | `optional-skills/software-development/rest-graphql-debug` |
| Версия | `1.2.0` |
| Автор | Эрен-Каракус0 |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `api`, `rest`, `graphql`, `http`, `debugging`, `testing`, `curl`, `integration` |
| Сопутствующие навыки | [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging), [`test-driven-development`](/docs/user-guide/skills/bundled/software-development/software-development-test-driven-development) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Тестирование и отладка API

Диагностика REST и GraphQL с помощью инструментов Hermes — `terminal` для `curl`, `execute_code` для Python, `requests`, `web_extract` для документации поставщиков. Изолируйте неисправный слой, прежде чем пытаться исправить ситуацию.

## Когда использовать

- API возвращает неожиданный статус или тело
- Ошибка аутентификации (401/403 после обновления токена, OAuth, ключ API)
- Работает в Postman, но не работает в коде
- Отладка интеграции Webhook/обратного вызова
- Создание или проверка интеграционных тестов API.
- Проблемы с ограничением скорости или нумерацией страниц.

Пропустите рендеринг пользовательского интерфейса, настройку запросов к базе данных или инфраструктуру DNS/брандмауэра (эскалация).

## Основной принцип

**Изолируйте слой, а затем исправьте.** Нажатием 200 OK можно скрыть поврежденные данные. 500 может замаскировать односимвольную опечатку аутентификации. Пройдите по цепочке по порядку; никогда не пропускай шаг.

```
1. Connectivity   → can we reach the host at all?
1.5 Timeouts      → connect-slow vs read-slow?
2. TLS/SSL        → cert valid and trusted?
3. Auth           → credentials correct and unexpired?
4. Request format → payload shape match server expectations?
5. Response parse → does our code accept what came back?
6. Semantics      → does the data mean what we assume?
```

## Краткое руководство за 5 минут

### ОТДЫХ через терминал

```python
# Verbose request/response exchange
terminal('curl -v https://api.example.com/users/1')

# POST with JSON
terminal("""curl -X POST https://api.example.com/users \\
  -H 'Content-Type: application/json' \\
  -H "Authorization: Bearer $TOKEN" \\
  -d '{"name":"test","email":"test@example.com"}'""")

# Headers only
terminal('curl -sI https://api.example.com/health')

# Pretty-print JSON
terminal('curl -s https://api.example.com/users | python3 -m json.tool')
```

### GraphQL через терминал

```python
terminal("""curl -X POST https://api.example.com/graphql \\
  -H 'Content-Type: application/json' \\
  -H "Authorization: Bearer $TOKEN" \\
  -d '{"query":"{ user(id: 1) { name email } }"}'""")
```

**Подсказка GraphQL:** серверы часто возвращают HTTP 200, даже если запрос не выполнен. Всегда проверяйте поле `errors` независимо от кода состояния:

```python
execute_code('''
import os, requests
resp = requests.post(
    "https://api.example.com/graphql",
    json={"query": "{ user(id: 1) { name email } }"},
    headers={"Authorization": f"Bearer {os.environ['TOKEN']}"},
    timeout=10,
)
data = resp.json()
if data.get("errors"):
    for err in data["errors"]:
        print(f"GraphQL error: {err['message']} (path: {err.get('path')})")
print(data.get("data"))
''')
```

### Python (запросы) через Execute_code

```python
execute_code('''
import requests
resp = requests.get(
    "https://api.example.com/users/1",
    headers={"Authorization": "Bearer <TOKEN>"},
    timeout=(3.05, 30),  # (connect, read)
)
print(resp.status_code, dict(resp.headers))
print(resp.text[:500])
''')
```

## Многоуровневый поток отладки

### Шаг 1 — Подключение

```python
terminal('nslookup api.example.com')
terminal('curl -v --connect-timeout 5 https://api.example.com/health')
```

Сбои: DNS не разрешается, брандмауэр, требуется VPN, отсутствует прокси.

### Шаг 1.5 — Таймауты

Отличайте *не могу дотянуться* от *достигает, но медленно*:

```python
terminal('''curl -w "dns:%{time_namelookup}s connect:%{time_connect}s tls:%{time_appconnect}s ttfb:%{time_starttransfer}s total:%{time_total}s\\n" \\
  -o /dev/null -s https://api.example.com/endpoint''')
```

В Python всегда передавайте таймаут кортежа — `requests` не имеет значения по умолчанию и будет зависать навсегда:

```python
execute_code('''
import requests
from requests.exceptions import ConnectTimeout, ReadTimeout
try:
    requests.get(url, timeout=(3.05, 30))
except ConnectTimeout:
    print("Cannot reach host — DNS, firewall, VPN")
except ReadTimeout:
    print("Connected but server is slow")
''')
```

Диагностика: высокий уровень `time_connect` — сеть/брандмауэр; высокий `time_starttransfer` с низким `time_connect` — медленный сервер.

### Шаг 2 — TLS/SSL

```python
terminal('curl -vI https://api.example.com 2>&1 | grep -E "SSL|subject|expire|issuer"')
```

Сбои: сертификат с истекшим сроком действия, самоподписанный сертификат, несоответствие имени хоста, отсутствие пакета CA. Используйте `-k` только для специальной отладки, а не в коде.

### Шаг 3 — Аутентификация

```python
# Token validity check
terminal('curl -s -o /dev/null -w "%{http_code}\\n" -H "Authorization: Bearer $TOKEN" https://api.example.com/me')

# Decode JWT exp claim — handles base64url padding correctly
execute_code('''
import json, base64, os
tok = os.environ["TOKEN"]
payload = tok.split(".")[1]
payload += "=" * (-len(payload) % 4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(payload)), indent=2))
''')
```

Контрольный список:
- Срок действия токена истек? (претензия `exp` в JWT)
- Верная схема? Носитель против базового против токена против `X-Api-Key`
- Правильное окружение? Простановка ключа на проде - это классика
- Ключ API в заголовке или параметр запроса (`?api_key=…`)?

### Шаг 4 — Формат запроса

```python
terminal("""curl -v -X POST https://api.example.com/endpoint \\
  -H 'Content-Type: application/json' \\
  -d '{"key":"value"}' 2>&1""")
```

**Несоответствие типа контента и тела — бесшумный 415/400:**

```python
# WRONG — data= sends form-encoded, header lies
requests.post(url, data='{"k":"v"}', headers={"Content-Type": "application/json"})

# RIGHT — json= auto-sets header AND serializes
requests.post(url, json={"k": "v"})

# WRONG — Accept says XML, code calls .json()
requests.get(url, headers={"Accept": "text/xml"})

# RIGHT — let requests build multipart with boundary
requests.post(url, files={"file": open("doc.pdf", "rb")})
```

Часто встречающееся: кодировка формы или JSON, отсутствие обязательных полей, неправильный метод HTTP, незакодированные параметры запроса.

### Шаг 5 — Анализ ответа

Всегда проверяйте тип контента перед вызовом `.json()`:

```python
execute_code('''
import requests
resp = requests.post(url, json=payload, timeout=10)
print(f"status={resp.status_code}")
print(f"headers={dict(resp.headers)}")
ct = resp.headers.get("Content-Type", "")
if "application/json" in ct:
    print(resp.json())
else:
    print(f"unexpected content-type {ct!r}, body={resp.text[:500]!r}")
''')
```

Сбои: страница ошибки HTML, где ожидался JSON, пустое тело, неверная кодировка.

### Шаг 6 — Семантическая проверка

Разобрано чисто — но являются ли данные *правильными*?

- Означает ли `"status": "active"` то, что думает ваш код?
- ID в ответе совпадает с запрошенным?
- Временные метки в ожидаемом часовом поясе?
- Пагинация возвращает все результаты или только страницу 1?

## Таблица статуса HTTP

### 401 Unauthorized — учетные данные отсутствуют или недействительны.

1. Заголовок `Authorization` действительно присутствует? (`curl -v` для подтверждения)
2. Токен правильный и срок его действия не истек?
3. Правильная схема авторизации? (`Bearer` против `Basic` против `Token`)
4. Некоторые API используют параметр запроса (`?api_key=…`) вместо заголовка.

### 403 Запрещено — проверено, но не авторизовано

1. Токен имеет необходимые области действия/разрешения?
2. Ресурс принадлежит другому аккаунту?
3. Белый список IP-адресов блокирует вас?
4. CORS в браузере? (проверьте `Access-Control-Allow-Origin`)

### 404 Not Found — ресурс не существует или URL неправильный.

1. Путь правильный? (конечная косая черта, опечатка, префикс версии)
2. Идентификатор ресурса существует?
3. Правильная версия API (`/v1/` или `/v2/`)?
4. Правильный базовый URL (промежуточный или рабочий)?

###409 Конфликт — коллизия состояний

1. Ресурс уже существует (создать дубликат)?
2. Устаревший `ETag` / `If-Match`?
3. Одновременная модификация другим процессом?

### 422 Необрабатываемый объект — действительный JSON, неверные данные

В теле ошибки обычно указываются неправильные поля. Проверьте:
- Типы полей (строка или целое число, формат даты)
- Обязательный или необязательный
- Значения перечисления внутри разрешенного набора

### 429 Слишком много запросов — скорость ограничена

Проверьте заголовки `Retry-After` и `X-RateLimit-*`. Экспоненциальный откат:

```python
execute_code('''
import time, requests

def with_backoff(method, url, **kwargs):
    for attempt in range(5):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        wait = int(resp.headers.get("Retry-After", 2 ** attempt))
        time.sleep(wait)
    return resp
''')
```

### 5xx — на стороне сервера, обычно это не ваша вина

- **500** — ошибка сервера. Захват идентификатора корреляции, файл у провайдера.
- **502** — вверх по течению. Откат + повтор.
- **503** — перегружен/обслуживание. Проверьте страницу статуса.
- **504** — тайм-аут восходящего потока. Уменьшите полезную нагрузку или увеличьте время ожидания.

Для всех 5xx: задержка с джиттером, предупреждение о постоянстве.

## Пагинация и идемпотентность

**Разбивка на страницы.** Убедитесь, что вы получаете *все* результаты. Найдите `next_cursor`, `next_page`, `total_count`. Два шаблона:
- Смещение (`?limit=100&offset=200`) — простое, можно пропускать элементы при смещении данных.
- Курсор (`?cursor=abc123`) — предпочтителен для живых или больших наборов данных.

**Идемпотентность.** Для неидемпотентных операций (POST) отправьте `Idempotency-Key: <uuid>`, чтобы повторные попытки не вызывали двойную оплату или двойное создание. Обязателен для платежей и заказов.

## Проверка контракта

Уловите дрейф схемы до того, как она попадет в рабочую среду:

```python
execute_code('''
import requests

def validate_user(data: dict) -> list[str]:
    errors = []
    required = {"id": int, "email": str, "created_at": str}
    for field, expected in required.items():
        if field not in data:
            errors.append(f"missing field: {field}")
        elif not isinstance(data[field], expected):
            errors.append(f"{field}: want {expected.__name__}, got {type(data[field]).__name__}")
    return errors

resp = requests.get(f"{BASE}/users/1", headers=HEADERS, timeout=10)
issues = validate_user(resp.json())
if issues:
    print(f"contract violations: {issues}")
''')
```

Запускайте после обновлений API, при интеграции новых сторонних разработчиков или в дымовых тестах CI.

## Идентификаторы корреляции

Всегда фиксируйте идентификатор запроса поставщика — самый быстрый путь к поддержке поставщика:

```python
execute_code('''
import requests
resp = requests.post(url, json=payload, headers=headers, timeout=10)
request_id = (
    resp.headers.get("X-Request-Id")
    or resp.headers.get("X-Trace-Id")
    or resp.headers.get("CF-Ray")  # Cloudflare
)
if resp.status_code >= 400:
    print(f"failed status={resp.status_code} req_id={request_id} ts={resp.headers.get('Date')}")
''')
```

**Шаблон отчета об ошибках поставщика:**

```
Endpoint:    POST /api/v1/orders
Request ID:  req_abc123xyz
Timestamp:   2026-03-17T14:30:00Z
Status:      500
Expected:    201 with order object
Actual:      500 {"error":"internal server error"}
Repro:       curl -X POST … (auth: <REDACTED>)
```

## Шаблон регрессионного теста

Поместите это в `tests/` и запустите через `terminal('pytest tests/test_api_smoke.py -v')`:

```python
import os, requests, pytest

BASE_URL = os.environ.get("API_BASE_URL", "https://api.example.com")
TOKEN    = os.environ.get("API_TOKEN", "")
HEADERS  = {"Authorization": f"Bearer {TOKEN}"}

class TestAPISmoke:
    def test_health(self):
        resp = requests.get(f"{BASE_URL}/health", timeout=5)
        assert resp.status_code == 200

    def test_list_users_returns_array(self):
        resp = requests.get(f"{BASE_URL}/users", headers=HEADERS, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data.get("data", data), list)

    def test_get_user_required_fields(self):
        resp = requests.get(f"{BASE_URL}/users/1", headers=HEADERS, timeout=10)
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            user = resp.json()
            assert "id" in user and "email" in user

    def test_invalid_auth_returns_401(self):
        resp = requests.get(
            f"{BASE_URL}/users",
            headers={"Authorization": "Bearer invalid-token"},
            timeout=10,
        )
        assert resp.status_code == 401
```

## Безопасность

### Обработка токенов
- Никогда не регистрируйте полные токены. Отредактировать: `Bearer <REDACTED>`.
- Никогда не прописывайте токены в скриптах жестко. Чтение из окружения (`os.environ["API_TOKEN"]`) или `${HERMES_HOME:-~/.hermes}/.env`.
— Немедленно выполняйте ротацию, если токен появляется в журналах, сообщениях об ошибках или истории git.

### Безопасное ведение журнала

```python
def redact_auth(headers: dict) -> dict:
    sensitive = {"authorization", "x-api-key", "cookie", "set-cookie"}
    return {k: ("<REDACTED>" if k.lower() in sensitive else v) for k, v in headers.items()}
```

### Контрольный список утечек

- [ ] **Учетные данные в URL-адресах.** Ключи API в строках запроса попадают в журналы сервера, историю браузера, заголовки реферера — используйте заголовки.
- [ ] **PII в ответах об ошибках.** `404 on /users/123` не должен указывать, существует ли пользователь (перечисление).
- [ ] **Трассы стека в prod.** 500-е не должны пропускать пути к файлам и версии платформы.
- [ ] **Внутренние имена хостов/IP-адреса.** `10.x.x.x`, `internal-api.corp.local` в телах ошибок.
- [ ] **Токены возвращаются обратно.** Некоторые API включают токен аутентификации в сведения об ошибке. Убедитесь, что это не так.
- [ ] **Подробный `Server` / `X-Powered-By`.** Утечка информации стека. Примечание для проверки безопасности.

## Шаблоны инструментов Hermes

### терминал — для curl, dig, openssl

```python
terminal('curl -sI https://api.example.com')
terminal('openssl s_client -connect api.example.com:443 -servername api.example.com </dev/null 2>/dev/null | openssl x509 -noout -dates')
```

###execute_code — для многошаговых потоков Python

Когда отладка охватывает аутентификацию → выборку → разбивку на страницы → проверку, используйте `execute_code`. Переменные сохраняются для сценария, результаты выводятся на стандартный вывод, риска спама токенов в вашем контексте нет:

```python
execute_code('''
import os, requests

token = os.environ["API_TOKEN"]
base  = "https://api.example.com"
H     = {"Authorization": f"Bearer {token}"}

# 1. auth
me = requests.get(f"{base}/me", headers=H, timeout=10)
print(f"auth {me.status_code}")

# 2. paginate
all_users, cursor = [], None
while True:
    params = {"cursor": cursor} if cursor else {}
    r = requests.get(f"{base}/users", headers=H, params=params, timeout=10)
    body = r.json()
    all_users.extend(body["data"])
    cursor = body.get("next_cursor")
    if not cursor:
        break
print(f"users={len(all_users)}")
''')
```

### web_extract — для документации API поставщика

Возьмите спецификацию конечной точки, которую вы отлаживаете, вместо того, чтобы гадать:

```python
web_extract(urls=["https://docs.example.com/api/v1/users"])
```

### Delegate_task — для полного тестирования CRUD

```python
delegate_task(
    goal="Test all CRUD endpoints for /api/v1/users",
    context="""
Follow the rest-graphql-debug skill (optional-skills/software-development/rest-graphql-debug).
Base URL: https://api.example.com
Auth: Bearer token from API_TOKEN env var.

For each verb (POST, GET, PATCH, DELETE):
  - happy path: assert status + response schema
  - error cases: 400, 404, 422
  - log a repro curl for any failure (redact tokens)

Output: pass/fail per endpoint + correlation IDs for failures.
""",
    toolsets=["terminal", "file"],
)
```

## Формат вывода

При сообщении о результатах:

```
## Finding
Endpoint: POST /api/v1/users
Status:   422 Unprocessable Entity
Req ID:   req_abc123xyz

## Repro
curl -X POST https://api.example.com/api/v1/users \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <REDACTED>' \
  -d '{"name":"test"}'

## Root Cause
Missing required field `email`. Server validation rejects before processing.

## Fix
-d '{"name":"test","email":"test@example.com"}'
```

## Похожие

- `systematic-debugging` — как только сбойный уровень API будет изолирован, определите первопричину вашего кода.
- `test-driven-development` — напишите регрессионный тест перед отправкой исправления.