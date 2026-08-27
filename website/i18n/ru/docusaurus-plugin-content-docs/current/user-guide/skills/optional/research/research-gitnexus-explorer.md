---
title: Gitnexus Explorer — интерактивный веб-интерфейс графа знаний базы кода.
sidebar_label: Gitnexus Explorer
description: Предоставление интерактивного веб-интерфейса графа знаний базы кода.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Гитнексус Эксплорер

Предоставляйте интерактивный веб-интерфейс графа знаний базы кода.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/gitnexus-explorer` |
| Путь | `optional-skills/research/gitnexus-explorer` |
| Версия | `1.0.0` |
| Автор | Гермес Агент + Текниум |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `gitnexus`, `code-intelligence`, `knowledge-graph`, `visualization` |
| Сопутствующие навыки | [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent), [`codebase-inspection`](/docs/user-guide/skills/bundled/github/github-codebase-inspection) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# GitNexus Explorer

Индексируйте любую кодовую базу в граф знаний и предоставляйте интерактивный веб-интерфейс для изучения.
символы, цепочки вызовов, кластеры и потоки выполнения. Туннелируется через Cloudflare для удаленного доступа.

## Когда использовать

- Пользователь хочет визуально изучить архитектуру кодовой базы.
- Пользователь запрашивает граф знаний/график зависимостей репо.
- Пользователь хочет поделиться с кем-нибудь интерактивным проводником кодовой базы.

## Предварительные условия

- **Node.js** (v18+) — требуется для GitNexus и прокси.
- **git** — репозиторий должен иметь каталог `.git`.
- **cloudflared** — для туннелирования (автоматически устанавливается в ~/.local/bin, если отсутствует)

## Предупреждение о размере

Веб-интерфейс отображает все узлы в браузере. Репозитории до ~5000 файлов работают хорошо. Большой
репозитории (более 30 тыс. узлов) будут работать медленно или вызывать сбой вкладки браузера. Инструменты CLI/MCP работают
в любом масштабе — такой предел есть только у веб-визуализации.

## Шаги

### 1. Клонирование и сборка GitNexus (однократная установка)

```bash
GITNEXUS_DIR="${GITNEXUS_DIR:-$HOME/.local/share/gitnexus}"

if [ ! -d "$GITNEXUS_DIR/gitnexus-web/dist" ]; then
  git clone https://github.com/abhigyanpatwari/GitNexus.git "$GITNEXUS_DIR"
  cd "$GITNEXUS_DIR/gitnexus-shared" && npm install && npm run build
  cd "$GITNEXUS_DIR/gitnexus-web" && npm install
fi
```

### 2. Исправление веб-интерфейса для удаленного доступа

Веб-интерфейс по умолчанию имеет значение `localhost:4747` для вызовов API. Исправьте его, чтобы использовать одно и то же происхождение.
поэтому он работает через туннель/прокси:

**Файл: `$GITNEXUS_DIR/gitnexus-web/src/config/ui-constants.ts`**
Изменение:
```typescript
export const DEFAULT_BACKEND_URL = 'http://localhost:4747';
```
Кому:
```typescript
export const DEFAULT_BACKEND_URL = typeof window !== 'undefined' && window.location.hostname !== 'localhost' ? window.location.origin : 'http://localhost:4747';
```

**Файл: `$GITNEXUS_DIR/gitnexus-web/vite.config.ts`**
Добавьте `allowedHosts: true` внутри блока `server: { }` (необходимо только при запуске dev
режим вместо производственной сборки):
```typescript
server: {
    allowedHosts: true,
    // ... existing config
},
```

Затем создайте производственный пакет:
```bash
cd "$GITNEXUS_DIR/gitnexus-web" && npx vite build
```

### 3. Индексируйте целевой репозиторий

```bash
cd /path/to/target-repo
npx gitnexus analyze --skip-agents-md
rm -rf .claude/    # remove Claude Code-specific artifacts
```

Добавьте `--embeddings` для семантического поиска (медленнее — минуты вместо секунд).

Индекс находится в `.gitnexus/` внутри репозитория (автоматически игнорируется).

### 4. Создайте прокси-скрипт

Запишите это в файл (например, `$GITNEXUS_DIR/proxy.mjs`). Он обслуживает производство
веб-интерфейс и прокси `/api/*` для серверной части GitNexus — тот же источник, никаких проблем с CORS,
ни sudo, ни nginx.

```javascript
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const API_PORT = parseInt(process.env.API_PORT || '4747');
const DIST_DIR = process.argv[2] || './dist';
const PORT = parseInt(process.argv[3] || '8888');

const MIME = {
  '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
  '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff',
  '.wasm': 'application/wasm',
};

function proxyToApi(req, res) {
  const opts = {
    hostname: '127.0.0.1', port: API_PORT,
    path: req.url, method: req.method, headers: req.headers,
  };
  const proxy = http.request(opts, (upstream) => {
    res.writeHead(upstream.statusCode, upstream.headers);
    upstream.pipe(res, { end: true });
  });
  proxy.on('error', () => { res.writeHead(502); res.end('Backend unavailable'); });
  req.pipe(proxy, { end: true });
}

function serveStatic(req, res) {
  let filePath = path.join(DIST_DIR, req.url === '/' ? 'index.html' : req.url.split('?')[0]);
  if (!fs.existsSync(filePath)) filePath = path.join(DIST_DIR, 'index.html');
  const ext = path.extname(filePath);
  const mime = MIME[ext] || 'application/octet-stream';
  try {
    const data = fs.readFileSync(filePath);
    res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'public, max-age=3600' });
    res.end(data);
  } catch { res.writeHead(404); res.end('Not found'); }
}

http.createServer((req, res) => {
  if (req.url.startsWith('/api')) proxyToApi(req, res);
  else serveStatic(req, res);
}).listen(PORT, () => console.log(`GitNexus proxy on http://localhost:${PORT}`));
```

### 5. Запустите службы

```bash
# Terminal 1: GitNexus backend API
npx gitnexus serve &

# Terminal 2: Proxy (web UI + API on one port)
node "$GITNEXUS_DIR/proxy.mjs" "$GITNEXUS_DIR/gitnexus-web/dist" 8888 &
```

Проверьте: `curl -s http://localhost:8888/api/repos` должен вернуть проиндексированные репозитории.

###6. Туннель с Cloudflare (опционально — для удаленного доступа)

```bash
# Install cloudflared if needed (no sudo)
if ! command -v cloudflared &>/dev/null; then
  mkdir -p ~/.local/bin
  curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -o ~/.local/bin/cloudflared
  chmod +x ~/.local/bin/cloudflared
  export PATH="$HOME/.local/bin:$PATH"
fi

# Start tunnel (--config /dev/null avoids conflicts with existing named tunnels)
cloudflared tunnel --config /dev/null --url http://localhost:8888 --no-autoupdate --protocol http2
```

URL-адрес туннеля (например, `https://random-words.trycloudflare.com`) выводится в поток stderr.
Поделитесь — любой, у кого есть ссылка, сможет изучить график.

### 7. Очистка

```bash
# Stop services
pkill -f "gitnexus serve"
pkill -f "proxy.mjs"
pkill -f cloudflared

# Remove index from the target repo
cd /path/to/target-repo
npx gitnexus clean
rm -rf .claude/
```

## Подводные камни

- **`--config /dev/null` требуется для Cloudflared**, если у пользователя есть существующая
  конфигурация туннеля с именем `~/.cloudflared/config.yml`. Без этого всеобъемлющее
  Правило входа в конфигурации возвращает 404 для всех запросов быстрого туннеля.

- **Производственная сборка обязательна для туннелирования.** Сервер разработки Vite блокирует
  нелокальные хосты по умолчанию (`allowedHosts`). Производственная сборка + узел
  прокси полностью избегает этого.

- **Веб-интерфейс НЕ создает `.claude/` или `CLAUDE.md`.** Они созданы
  `npx gitnexus analyze`. Используйте `--skip-agents-md` для подавления файлов уценки,
  затем `rm -rf .claude/` до конца. Это интеграции Claude Code, которые
  пользователям Hermes-Agent не нужен.

- **Ограничение памяти браузера.** Веб-интерфейс загружает весь график в память браузера.
  Репозитории с более чем 5 тысячами файлов могут работать медленно. Более 30 тысяч файлов, скорее всего, приведут к сбою вкладки.

- **Внедрения не являются обязательными.** `--embeddings` включает семантический поиск, но требует
  минут в крупных репозиториях. Пропустите его для быстрого изучения; добавь, если хочешь
  запросы на естественном языке через панель чата AI.

- **Несколько репозиториев.** `gitnexus serve` обслуживает ВСЕ проиндексированные репозитории. Укажите несколько
  репозитории, запустите обслуживание один раз, и веб-интерфейс позволит вам переключаться между ними.