---
title: Агент страницы — встраивайте встроенный в веб-приложения второй пилотный модуль
  графического пользовательского интерфейса на естественном языке.
sidebar_label: Page Agent
description: Встраивайте встроенный в веб-приложения второй пилотный модуль графического
  пользовательского интерфейса на естественном языке.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Агент страницы

Встраивайте встроенный второй пилотный модуль графического пользовательского интерфейса на естественном языке в веб-приложения.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/web-development/page-agent` |
| Путь | `optional-skills/web-development/page-agent` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `web`, `javascript`, `agent`, `browser`, `gui`, `alibaba`, `embed`, `copilot`, `saas` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# пейдж-агент

Alibaba/page-agent (https://github.com/alibaba/page-agent, более 17 тысяч звезд, Массачусетский технологический институт) — это внутристраничный агент с графическим интерфейсом, написанный на TypeScript. Он находится внутри веб-страницы, читает DOM как текст (без снимков экрана, без мультимодального LLM) и выполняет инструкции на естественном языке, такие как «нажмите кнопку входа в систему, затем введите имя пользователя как Джон» на текущей странице. Чисто на стороне клиента — хост-сайт просто включает скрипт и передает конечную точку LLM, совместимую с OpenAI.

## Когда использовать этот навык

Загрузите этот навык, когда пользователь хочет:

- **Добавьте второго пилота с искусственным интеллектом в собственное веб-приложение** (SaaS, панель администратора, инструмент B2B, ERP, CRM) — «пользователи на моей информационной панели должны иметь возможность вводить «создать счет для Acme Corp и отправить его по электронной почте» вместо того, чтобы просматривать пять экранов».
- **Модернизация устаревшего веб-приложения** без переписывания интерфейса — агент страницы устанавливается поверх существующего DOM.
- **Добавьте доступность с помощью естественного языка** — пользователи, использующие голос или средства чтения с экрана, управляют пользовательским интерфейсом, описывая, что они хотят.
- **Демонстрация или оценка агента страницы** на локальном (Ollama) или размещенном (Qwen, OpenAI, OpenRouter) LLM.
- **Создавайте интерактивное обучение/демонстрации продуктов** — позвольте искусственному интеллекту объяснить пользователю, как отправить отчет о расходах, в реальном пользовательском интерфейсе.

## Когда НЕ использовать этот навык

- Пользователь хочет, чтобы **Hermes сам управлял браузером** → используйте встроенный инструмент браузера Hermes (Browserbase/Camofox). page-agent — это *противоположное* направление.
– Пользователь хочет **автоматизацию перекрестных таблиц без встраивания** → используйте Playwright, браузер или расширение Chrome для агента страниц.
- Пользователю необходимо **визуальное обоснование/скриншоты** → page-agent работает только с текстовым DOM; вместо этого используйте мультимодальный агент браузера

## Предварительные условия

- Node 22.13+ или 24+, npm 10+ (в документах указано 11+, но 10.9 работает нормально)
- Конечная точка LLM, совместимая с OpenAI: Qwen (DashScope), OpenAI, Ollama, OpenRouter или что-нибудь говорящее `/v1/chat/completions`
- Браузер с инструментами разработчика (для отладки)

## Путь 1 — 30-секундная демонстрация через CDN (без установки)

Самый быстрый способ увидеть, как это работает. Использует бесплатный прокси-сервер LLM для тестирования Alibaba — **только для ознакомления**, в соответствии с их условиями.

Добавьте на любую HTML-страницу (или вставьте в консоль devtools как букмарклет):

```html
<script src="https://cdn.jsdelivr.net/npm/page-agent@1.8.0/dist/iife/page-agent.demo.js" crossorigin="true"></script>
```

Появится панель. Введите инструкцию. Сделанный.

Форма букмарклета (закиньте на панель закладок, нажмите на любую страницу):

```javascript
javascript:(function(){var s=document.createElement('script');s.src='https://cdn.jsdelivr.net/npm/page-agent@1.8.0/dist/iife/page-agent.demo.js';document.head.appendChild(s);})();
```

## Путь 2 — установка npm в собственное веб-приложение (использование в рабочей среде)

Внутри существующего веб-проекта (React/Vue/Svelte/plain):

```bash
npm install page-agent
```

Подключите его к своей собственной конечной точке LLM — **никогда не отправляйте демонстрационную CDN реальным пользователям**:

```javascript
import { PageAgent } from 'page-agent'

const agent = new PageAgent({
    model: 'qwen3.5-plus',
    baseURL: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    apiKey: process.env.LLM_API_KEY,   // never hardcode
    language: 'en-US',
})

// Show the panel for end users:
agent.panel.show()

// Or drive it programmatically:
await agent.execute('Click submit button, then fill username as John')
```

Примеры провайдеров (подойдет любая конечная точка, совместимая с OpenAI):

| Provider | `baseURL` | `model` |
|----------|-----------|---------|
| Qwen / DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.5-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Ollama (local) | `http://localhost:11434/v1` | `qwen3:14b` |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4.6` |

**Key config fields** (passed to `new PageAgent({...})`):

- `model`, `baseURL`, `apiKey` — LLM connection
- `language` — UI language (`en-US`, `zh-CN`, etc.)
- Allowlist and data-masking hooks exist for locking down what the agent can touch — see https://alibaba.github.io/page-agent/ for the full option list

**Security.** Don't put your `apiKey` in client-side code for a real deployment — proxy LLM calls through your backend and point `baseURL` at your proxy. The demo CDN exists because alibaba runs that proxy for evaluation.

## Path 3 — clone the source repo (contributing, or hacking on it)

Use this when the user wants to modify page-agent itself, test it against arbitrary sites via a local IIFE bundle, or develop the browser extension.

```bash
git clone https://github.com/alibaba/page-agent.git
cd page-agent
npm ci              # exact lockfile install (or `npm i` to allow updates)
```

Create `.env` in the repo root with an LLM endpoint. Example:

```
LLM_MODEL_NAME=gpt-4o-mini
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
```

Ollama flavor:

```
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=NA
LLM_MODEL_NAME=qwen3:14b
```

Common commands:

```bash
npm start           # docs/website dev server
npm run build       # build every package
npm run dev:demo    # serve IIFE bundle at http://localhost:5174/page-agent.demo.js
npm run dev:ext     # develop the browser extension (WXT + React)
npm run build:ext   # build the extension
```

**Test on any website** using the local IIFE bundle. Add this bookmarklet:

```javascript
javascript:(function(){var s=document.createElement('script');s.src=`http://localhost:5174/page-agent.demo.js?t=${Math.random()}`;s.onload=()=>console.log('PageAgent ready!');document.head.appendChild(s);})();
```

Then: `npm run dev:demo`, click the bookmarklet on any page, and the local build injects. Auto-rebuilds on save.

**Warning:** your `.env` `LLM_API_KEY` is inlined into the IIFE bundle during dev builds. Don't share the bundle. Don't commit it. Don't paste the URL into Slack. (Verified: grepping the public dev bundle returns the literal values from `.env`.)

## Repo layout (Path 3)

Monorepo with npm workspaces. Key packages:

| Package | Path | Purpose |
|---------|------|---------|
| `page-agent` | `packages/page-agent/` | Main entry with UI panel |
| `@page-agent/core` | `packages/core/` | Core agent logic, no UI |
| `@page-agent/mcp` | `packages/mcp/` | MCP server (beta) |
| — | `packages/llms/` | LLM client |
| — | `packages/page-controller/` | DOM ops + visual feedback |
| — | `packages/ui/` | Panel + i18n |
| — | `packages/extension/` | Chrome/Firefox extension |
| — | `packages/website/` | Docs + landing site |

## Verifying it works

After Path 1 or Path 2:
1. Open the page in a browser with devtools open
2. You should see a floating panel. If not, check the console for errors (most common: CORS on the LLM endpoint, wrong `baseURL`, or a bad API key)
3. Type a simple instruction matching something visible on the page ("click the Login link")
4. Watch the Network tab — you should see a request to your `baseURL`

After Path 3:
1. `npm run dev:demo` prints `Accepting connections at http://localhost:5174`
2. `curl -I http://localhost:5174/page-agent.demo.js` returns `HTTP/1.1 200 OK` with `Content-Type: application/javascript`
3. Click the bookmarklet on any site; panel appears

## Pitfalls

- **Demo CDN in production** — don't. It's rate-limited, uses alibaba's free proxy, and their terms forbid production use.
- **API key exposure** — any key passed to `new PageAgent({apiKey: ...})` ships in your JS bundle. Always proxy through your own backend for real deployments.
- **Non-OpenAI-compatible endpoints** fail silently or with cryptic errors. If your provider needs native Anthropic/Gemini formatting, use an OpenAI-compatibility proxy (LiteLLM, OpenRouter) in front.
- **CSP blocks** — sites with strict Content-Security-Policy may refuse to load the CDN script or disallow inline eval. In that case, self-host from your origin.
- **Restart dev server** after editing `.env` in Path 3 — Vite only reads env at startup.
- **Node version** — the repo declares `^22.13.0 || >=24`. Node 20 will fail `npm ci` with engine errors.
- **npm 10 vs 11** — docs say npm 11+; npm 10.9 actually works fine.

## Reference

- Репо: https://github.com/alibaba/page-agent.
- Документы: https://alibaba.github.io/page-agent/.
- Лицензия: MIT (построена на основе внутренней обработки DOM браузера, авторские права Gregor Zunic, 2024 г.)