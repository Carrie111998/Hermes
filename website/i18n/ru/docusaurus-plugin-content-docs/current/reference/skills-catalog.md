---
sidebar_position: 5
title: Каталог комплексных навыков
description: Каталог комплексных навыков, поставляемых с агентом Hermes
---

# Каталог комплексных навыков

Hermes поставляется с большой встроенной библиотекой навыков, скопированной в `~/.hermes/skills/` при установке. Каждый навык ниже связан с отдельной страницей с его полным определением, настройкой и использованием.

Hermes также синхронизирует связанные навыки на `hermes update`, но манифест синхронизации учитывает локальные удаления и пользовательские изменения. Если указанный здесь навык отсутствует в дереве `~/.hermes/skills/` вашего профиля, он все равно поставляется с Hermes; восстановите его с помощью `hermes skills reset <name> --restore`.

Если навык отсутствует в этом списке, но присутствует в репозитории, каталог заново создается `website/scripts/generate-skill-docs.py`.

## яблоко

| Навык | Описание | Путь |
|-------|-------------|------|
| [`apple-notes`](/docs/user-guide/skills/bundled/apple/apple-apple-notes) | Управляйте Apple Notes через интерфейс командной строки Memo: создавайте, ищите, редактируйте. | `apple/apple-notes` |
| [`apple-reminders`](/docs/user-guide/skills/bundled/apple/apple-apple-reminders) | Напоминания Apple через напоминание: добавить, перечислить, завершить. | `apple/apple-reminders` |
| [`findmy`](/docs/user-guide/skills/bundled/apple/apple-findmy) | Отслеживайте устройства Apple/AirTags через FindMy.app на macOS. | `apple/findmy` |
| [`imessage`](/docs/user-guide/skills/bundled/apple/apple-imessage) | Отправляйте и получайте iMessages/SMS через интерфейс командной строки imsg в macOS. | `apple/imessage` |

## автономные-ИИ-агенты

| Навык | Описание | Путь |
|-------|-------------|------|
| [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code) | Делегируйте кодирование Claude Code CLI (функции, PR). | `autonomous-ai-agents/claude-code` |
| [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex) | Делегирование кодирования в OpenAI Codex CLI (функции, PR). | `autonomous-ai-agents/codex` |
| [`computer-use`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-computer-use) | Управляйте рабочим столом в фоновом режиме, не отвлекая внимание. | `autonomous-ai-agents/computer-use` |
| [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) | Используйте, настраивайте, тематизируйте, расширяйте и координируйте агент Hermes. | `autonomous-ai-agents/hermes-agent` |
| [`opencode`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-opencode) | Делегирование кодирования в OpenCode CLI (функции, PR-обзор). | `autonomous-ai-agents/opencode` |

## креатив

| Навык | Описание | Путь |
|-------|-------------|------|
| [`architecture-diagram`](/docs/user-guide/skills/bundled/creative/creative-architecture-diagram) | Архитектурные/облачные/инфраструктурные диаграммы SVG в темной тематике в формате HTML. | `creative/architecture-diagram` |
| [`ascii-art`](/docs/user-guide/skills/bundled/creative/creative-ascii-art) | ASCII-изображение: pyfiglet, Cowsay, Boxes, Image-to-ASCII. | `creative/ascii-art` |
| [`ascii-video`](/docs/user-guide/skills/bundled/creative/creative-ascii-video) | Видео ASCII: конвертируйте видео/аудио в цветной ASCII MP4/GIF. | `creative/ascii-video` |
| [`baoyu-infographic`](/docs/user-guide/skills/bundled/creative/creative-baoyu-infographic) | Инфографика: 21 макет x 21 стиль (信息图, 可视化). | `creative/baoyu-infographic` |
| [`claude-design`](/docs/user-guide/skills/bundled/creative/creative-claude-design) | Создавайте уникальные HTML-артефакты (лендинг, презентацию, прототип). | `creative/claude-design` |
| [`comfyui`](/docs/user-guide/skills/bundled/creative/creative-comfyui) | Создавайте изображения, видео и аудио с помощью рабочих процессов распространения. | `creative/comfyui` |
| [`design-md`](/docs/user-guide/skills/bundled/creative/creative-design-md) | Создание, проверка и экспорт файлов спецификаций токена Google DESIGN.md. | `creative/design-md` |
| [`excalidraw`](/docs/user-guide/skills/bundled/creative/creative-excalidraw) | Нарисованные от руки диаграммы Excalidraw JSON (арка, поток, последовательность). | `creative/excalidraw` |
| [`humanizer`](/docs/user-guide/skills/bundled/creative/creative-humanizer) | Очеловечьте текст: избавьтесь от ИИ-измов и добавьте настоящий голос. | `creative/humanizer` |
| [`manim-video`](/docs/user-guide/skills/bundled/creative/creative-manim-video) | Анимации Manim CE: 3Blue1Brown математические и алгоритмические видеоролики. | `creative/manim-video` |
| [`p5js`](/docs/user-guide/skills/bundled/creative/creative-p5js) | Эскизы p5.js: художественное оформление, шейдеры, интерактивность, 3D. | `creative/p5js` |
| [`popular-web-designs`](/docs/user-guide/skills/bundled/creative/creative-popular-web-designs) | 54 реальных системы дизайна (Stripe, Linear, Vercel) в формате HTML/CSS. | `creative/popular-web-designs` |
| [`pretext`](/docs/user-guide/skills/bundled/creative/creative-pretext) | Создавайте креативные демо-версии браузера с помощью текстового макета без DOM. | `creative/pretext` |
| [`sketch`](/docs/user-guide/skills/bundled/creative/creative-sketch) | Одноразовые HTML-макеты: 2-3 варианта дизайна для сравнения. | `creative/sketch` |
| [`songwriting-and-ai-music`](/docs/user-guide/skills/bundled/creative/creative-songwriting-and-ai-music) | Написание песен и музыкальные подсказки Suno AI. | `creative/songwriting-and-ai-music` |
| [`touchdesigner-mcp`](/docs/user-guide/skills/bundled/creative/creative-touchdesigner-mcp) | Управляйте TouchDesigner через twozero MCP. | `creative/touchdesigner-mcp` |

## электронная почта

| Навык | Описание | Путь |
|-------|-------------|------|
| [`email-inbox-triage`](/docs/user-guide/skills/bundled/email/email-email-inbox-triage) | Отсортируйте входящие сообщения: расставьте приоритеты в обсуждениях, безопасно составляйте черновики ответов. | `email/email-inbox-triage` |
| [`himalaya`](/docs/user-guide/skills/bundled/email/email-himalaya) | Himalaya CLI: электронная почта IMAP/SMTP с терминала. | `email/himalaya` |

## гитхаб

| Навык | Описание | Путь |
|-------|-------------|------|
| [`codebase-inspection`](/docs/user-guide/skills/bundled/github/github-codebase-inspection) | Проверьте кодовые базы с помощью pygount: LOC, языки, соотношения. | `github/codebase-inspection` |
| [`github-auth`](/docs/user-guide/skills/bundled/github/github-github-auth) | Настройка аутентификации GitHub: токены HTTPS, ключи SSH, вход в CLI. | `github/github-auth` |
| [`github-code-review`](/docs/user-guide/skills/bundled/github/github-github-code-review) | Обзор PR: различия, встроенные комментарии через gh или REST. | `github/github-code-review` |
| [`github-issue-to-pr`](/docs/user-guide/skills/bundled/github/github-github-issue-to-pr) | Перенесите проблему GitHub в проверенный PR с честным состоянием CI. | `github/github-issue-to-pr` |
| [`github-issues`](/docs/user-guide/skills/bundled/github/github-github-issues) | Создавайте, сортируйте, маркируйте и назначайте проблемы GitHub с помощью gh или REST. | `github/github-issues` |
| [`github-pr-workflow`](/docs/user-guide/skills/bundled/github/github-github-pr-workflow) | Жизненный цикл PR GitHub: разветвление, фиксация, открытие, CI, слияние. | `github/github-pr-workflow` |
| [`github-repo-management`](/docs/user-guide/skills/bundled/github/github-github-repo-management) | Клонирование/создание/форк репозиториев; управлять пультами, релизами. | `github/github-repo-management` |

## медиа

| Навык | Описание | Путь |
|-------|-------------|------|
| [`gif-search`](/docs/user-guide/skills/bundled/media/media-gif-search) | Найдите/загрузите GIF-файлы Tenor с помощью Curl + JQ. | `media/gif-search` |
| [`songsee`](/docs/user-guide/skills/bundled/media/media-songsee) | Аудиоспектрограммы/функции (mel, цветность, MFCC) через CLI. | `media/songsee` |
| [`youtube-content`](/docs/user-guide/skills/bundled/media/media-youtube-content) | Расшифровки YouTube для резюме, тем, блогов. | `media/youtube-content` |

## млопс

| Навык | Описание | Путь |
|-------|-------------|------|
| [`evaluating-llms-harness`](/docs/user-guide/skills/bundled/mlops/mlops-evaluation-evaluating-llms-harness) | lm-eval-harness: эталонные LLM (MMLU, GSM8K и т. д.). | `mlops/evaluation/evaluating-llms-harness` |
| [`huggingface-hub`](/docs/user-guide/skills/bundled/mlops/mlops-huggingface-hub) | HuggingFace hf CLI: поиск/скачивание/выгрузка моделей, наборов данных. | `mlops/huggingface-hub` |
| [`llama-cpp`](/docs/user-guide/skills/bundled/mlops/mlops-inference-llama-cpp) | llama.cpp локальный вывод GGUF + обнаружение модели HF Hub. | `mlops/inference/llama-cpp` |
| [`serving-llms-vllm`](/docs/user-guide/skills/bundled/mlops/mlops-inference-serving-llms-vllm) | vLLM: высокопроизводительное обслуживание LLM, OpenAI API, квантование. | `mlops/inference/serving-llms-vllm` |
| [`weights-and-biases`](/docs/user-guide/skills/bundled/mlops/mlops-evaluation-weights-and-biases) | W&B: регистрируйте эксперименты ML, проверки, реестр моделей, информационные панели. | `mlops/evaluation/weights-and-biases` |

## ведение заметок

| Навык | Описание | Путь |
|-------|-------------|------|
| [`obsidian`](/docs/user-guide/skills/bundled/заметок/заметок-обсидиан) | Читайте, ищите, создавайте и редактируйте заметки в хранилище Obsidian. | `note-taking/obsidian` |

## производительность

| Навык | Описание | Путь |
|-------|-------------|------|
| [`airtable`](/docs/user-guide/skills/bundled/productivity/productivity-airtable) | REST API Airtable через Curl. Записывает CRUD, фильтрует, обновляет. | `productivity/airtable` |
| [`box`](/docs/user-guide/skills/bundled/productivity/productivity-box) | Box управляет облачными файлами, общим доступом, поиском и метаданными. | `productivity/box` |
| [`document-to-action-items`](/docs/user-guide/skills/bundled/productivity/productivity-document-to-action-items) | Извлекать из документов указанные обязательства, сроки, задачи. | `productivity/document-to-action-items` |
| [`docx`](/docs/user-guide/skills/bundled/productivity/productivity-docx) | Создавайте, читайте, редактируйте и шаблонизируйте файлы Word .docx. | `productivity/docx` |
| [`google-workspace`](/docs/user-guide/skills/bundled/productivity/productivity-google-workspace) | Gmail, Календарь, Диск, Документы, Таблицы через интерфейс командной строки gws или Python. | `productivity/google-workspace` |
| [`maps`](/docs/user-guide/skills/bundled/productivity/productivity-maps) | Геокодирование, POI, маршруты, часовые пояса через OpenStreetMap/OSRM. | `productivity/maps` |
| [`meeting-action-items`](/docs/user-guide/skills/bundled/productivity/productivity-meeting-action-items) | Превратите записи совещаний в цитируемые решения, владельцев и билеты. | `productivity/meeting-action-items` |
| [`nano-pdf`](/docs/user-guide/skills/bundled/productivity/productivity-nano-pdf) | Редактируйте текст в существующих PDF-файлах с помощью подсказок на естественном языке. | `productivity/nano-pdf` |
| [`notion`](/docs/user-guide/skills/bundled/productivity/productivity-notion) | Notion API + ntn CLI: страницы, базы данных, уценка, Workers. | `productivity/notion` |
| [`ocr-and-documents`](/docs/user-guide/skills/bundled/productivity/productivity-ocr-and-documents) | Извлечение текста из PDF-файлов/сканов (pymupdf, маркер-pdf). | `productivity/ocr-and-documents` |
| [`pdf`](/docs/user-guide/skills/bundled/productivity/productivity-pdf) | Создавайте, читайте, объединяйте, заполняйте и защищайте PDF-файлы. | `productivity/pdf` |
| [`powerpoint`](/docs/user-guide/skills/bundled/productivity/productivity-powerpoint) | Создавайте, читайте и редактируйте колоды .pptx с помощью python-pptx. | `productivity/powerpoint` |
| [`product-price-monitor`](/docs/user-guide/skills/bundled/productivity/productivity-product-price-monitor) | Следите за ценами на продукты, рейсы или листинги; оповещение о цели. | `productivity/product-price-monitor` |
| [`session-librarian`](/docs/user-guide/skills/bundled/productivity/productivity-session-librarian) | Организуйте сеансы по подсказкам: найдите, переименуйте, заархивируйте, удалите. | `productivity/session-librarian` |
| [`teams-meeting-pipeline`](/docs/user-guide/skills/bundled/productivity/productivity-teams-meeting-pipeline) | Сводки совещаний команд, повторы заданий, подписки на графики. | `productivity/teams-meeting-pipeline` |
| [`weekly-review-planning`](/docs/user-guide/skills/bundled/productivity/productivity-еженедельный-обзор-планирование) | Еженедельный сброс: обязательства, застопорившаяся работа, план на следующую неделю. | `productivity/weekly-review-planning` |
| [`xlsx`](/docs/user-guide/skills/bundled/productivity/productivity-xlsx) | Создавайте, читайте и редактируйте книги Excel .xlsx и CSV. | `productivity/xlsx` |

## исследовать

| Навык | Описание | Путь |
|-------|-------------|------|
| [`arxiv`](/docs/user-guide/skills/bundled/research/research-arxiv) | Выполняйте поиск статей arXiv по ключевому слову, автору, категории или идентификатору. | `research/arxiv` |
| [`blocked-page-recovery`](/docs/user-guide/skills/bundled/research/research-blocked-page-recovery) | Восстанавливайте заблокированные страницы, страницы с платным доступом или WAF с помощью снимков архива и резервных средств чтения. Используйте, когда web_extract или браузер попадает на страницы 403/429/challenge, платный доступ или межстраничные объявления для обнаружения ботов. | `research/blocked-page-recovery` |
| [`blogwatcher`](/docs/user-guide/skills/bundled/research/research-blogwatcher) | Мониторинг блогов и каналов RSS/Atom с помощью инструмента blogwatcher-cli. | `research/blogwatcher` |
| [`competitor-news-monitor`](/docs/user-guide/skills/bundled/research/research-competitor-news-monitor) | Следите за важными новостями названных компаний; цитируемые дайджесты. | `research/competitor-news-monitor` |
| [`grounded-citations`](/docs/user-guide/skills/bundled/research/research-based-citations) | Обосновайте ответы и документы в цитируемых, проверяемых источниках. | `research/grounded-citations` |
| [`llm-wiki`](/docs/user-guide/skills/bundled/research/research-llm-wiki) | LLM Wiki Карпати: сборка/запрос взаимосвязанных КБ уценки. | `research/llm-wiki` |
| [`research-paper-writing`](/docs/user-guide/skills/bundled/research/research-research-paper-writing) | Напишите документы по машинному обучению для NeurIPS/ICML/ICLR: разработать → отправить. | `research/research-paper-writing` |

## умный дом

| Навык | Описание | Путь |
|-------|-------------|------|
| [`openhue`](/docs/user-guide/skills/bundled/smart-home/smart-home-openhue) | Управляйте освещением, сценами и комнатами Philips Hue через интерфейс командной строки OpenHue. | `smart-home/openhue` |

## социальные сети

| Навык | Описание | Путь |
|-------|-------------|------|
| [`xurl`](/docs/user-guide/skills/bundled/social-media/social-media-xurl) | X/Twitter через интерфейс командной строки xurl: поиск необработанных сообщений, публикации, личные сообщения, медиа. | `social-media/xurl` |

##-разработка программного обеспечения

| Навык | Описание | Путь |
|-------|-------------|------|
| [`dogfood`](/docs/user-guide/skills/bundled/software-development/software-development-dogfood) | Исследовательский контроль качества веб-приложений: найдите ошибки, доказательства, отчеты. | `software-development/dogfood` |
| [`hermes-agent-skill-authoring`](/docs/user-guide/skills/bundled/software-development/software-development-hermes-agent-skill-authoring) | Авторские файлы SKILL.md в репозитории: оформление и структура. | `software-development/hermes-agent-skill-authoring` |
| [`inspecting-hermes-desktop-dom`](/docs/user-guide/skills/bundled/software-development/software-development-inspecting-hermes-desktop-dom) | Прочтите действующую версию DOM/CSS рабочего стола Hermes через CDP. | `software-development/inspecting-hermes-desktop-dom` |
| [`node-inspect-debugger`](/docs/user-guide/skills/bundled/software-development/software-development-node-inspect-debugger) | Отладка Node.js с помощью --inspect + CLI протокола Chrome DevTools. | `software-development/node-inspect-debugger` |
| [`plan`](/docs/user-guide/skills/bundled/software-development/software-development-plan) | Напишите план уценки в .hermes/plans/; никакого исполнения. | `software-development/plan` |
| [`python-debugpy`](/docs/user-guide/skills/bundled/software-development/software-development-python-debugpy) | Отладка Python: pdb REPL + удаленная отладка (DAP). | `software-development/python-debugpy` |
| [`requesting-code-review`](/docs/user-guide/skills/bundled/software-development/software-development-requesting-code-review) | Проверка перед фиксацией: сканирование безопасности, контроль качества, автоматическое исправление. | `software-development/requesting-code-review` |
| [`simplify-code`](/docs/user-guide/skills/bundled/software-development/software-development-simplify-code) | Параллельная очистка недавних изменений кода с помощью четырех агентов. | `software-development/simplify-code` |
| [`spike`](/docs/user-guide/skills/bundled/software-development/software-development-spike) | Одноразовые эксперименты для проверки идеи перед сборкой. | `software-development/spike` |
| [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging) | 4-этапная отладка первопричин: выясните ошибки, прежде чем их исправлять. | `software-development/systematic-debugging` |
| [`test-driven-development`](/docs/user-guide/skills/bundled/software-development/software-development-test-driven-development) | TDD: применять RED-GREEN-REFACTOR, тесты перед кодом. | `software-development/test-driven-development` |