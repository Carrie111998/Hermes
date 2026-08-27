---
sidebar_position: 9
title: Каталог дополнительных навыков
description: Официальные дополнительные навыки, поставляемые вместе с hermes-agent
  — установите через Гермес навыки installofficial/<category>/<skill>
---

# Каталог дополнительных навыков

Дополнительные навыки поставляются с Hermes-Agent под `optional-skills/`, но **не активны по умолчанию**. Установите их явно:

```bash
hermes skills install official/<category>/<skill>
```

Например:

```bash
hermes skills install official/blockchain/solana
hermes skills install official/mlops/flash-attention
```

Каждый навык ниже связан с отдельной страницей с его полным определением, настройкой и использованием.

Чтобы удалить:

```bash
hermes skills uninstall <skill-name>
```

## автономные-ИИ-агенты

| Навык | Описание |
|-------|-------------|
| [**antigravity-cli**](/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-antigravity-cli) | Работа с интерфейсом командной строки Antigravity (agy): плагины, аутентификация, песочница. |
| [**blackbox**](/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-blackbox) | Делегируйте задачи кодирования многомодельному интерфейсу командной строки Blackbox AI. |
| [**grok**](/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-grok) | Делегируйте кодирование в интерфейс командной строки xAI Grok Build (функции, PR). |
| [**honcho**](/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-honcho) | Настройте и устраните неполадки памяти Honcho для Hermes. |
| [**openhands**](/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-openhands) | Делегируйте кодирование OpenHands CLI (независимо от модели, LiteLLM). |

## блокчейн

| Навык | Описание |
|-------|-------------|
| [**evm**](/docs/user-guide/skills/optional/blockchain/blockchain-evm) | Клиент EVM только для чтения: кошельки, токены, газ в 8 цепочках. |
| [**гиперликвид**](/docs/user-guide/skills/optional/blockchain/blockchain-hyperliquid) | Данные гиперликвидного рынка, история счета, обзор сделок. |
| [**solana**](/docs/user-guide/skills/optional/blockchain/blockchain-solana) | Запрашивайте кошельки, токены, транзакции и NFT Solana в долларах США. |

## общение

| Навык | Описание |
|-------|-------------|
| [**правило «один-три-один»**](/docs/user-guide/skills/optional/communication/communication-one-three-one-rule) | Краткое описание решения 1-3-1: проблема, три варианта, один выбор. |

## креатив

| Навык | Описание |
|-------|-------------|
| [**audiocraft-audio-generation**](/docs/user-guide/skills/optional/creative/creative-audiocraft-audio-generation) | AudioCraft: преобразование текста в музыку MusicGen, преобразование текста в звук AudioGen. |
| [**baoyu-article-illustrator**](/docs/user-guide/skills/optional/creative/creative-baoyu-article-illustrator) | Иллюстрации к статьям: шрифт × стиль × согласованность палитры. |
| [**baoyu-comic**](/docs/user-guide/skills/optional/creative/creative-baoyu-comic) | Комиксы «Знание» (知识漫画): образовательные, биографические, обучающие. |
| [**концептуальные диаграммы**](/docs/user-guide/skills/optional/creative/creative-concept-diagrams) | Создавайте плоские, минимальные образовательные визуальные эффекты SVG в формате HTML. |
| [**творческое-идея**](/docs/user-guide/skills/optional/creative/creative-creative-ideation) | Генерируйте идеи с помощью названных методов из творческой практики. |
| [**нарисуйте свой шрифт**](/docs/user-guide/skills/optional/creative/creative-draw-your-font) | Превратите фотографию рукописного текста в устанавливаемый шрифт (TTF/WOFF). |
| [**heartmula**](/docs/user-guide/skills/optional/creative/creative-heartmula) | HeartMuLa: создание песен в стиле Suno из текстов + тегов. |
| [**гиперфреймы**](/docs/user-guide/skills/optional/creative/creative-hyperframes) | Рендеринг видео MP4/WebM из HTML-композиций. |
| [**kanban-video-orchestrator**](/docs/user-guide/skills/optional/creative/creative-kanban-video-orchestrator) | Планируйте и запускайте конвейеры мультиагентного производства видео. |
| [**генерация мемов**](/docs/user-guide/skills/optional/creative/creative-meme-generation) | Создавайте мемы PNG из шаблонов с помощью наложения текста «Подушка». |
| [**pixel-art**](/docs/user-guide/skills/optional/creative/creative-pixel-art) | Пиксельная графика с палитрами эпох (NES, Game Boy, PICO-8). |
| [**простой-английский**](/docs/user-guide/skills/optional/creative/creative-simple-english) | Перепишите технический текст на упрощенный технический английский ASD-STE100. |
| [**social-media-content-calendar**](/docs/user-guide/skills/optional/creative/creative-social-media-content-calendar) | Планируйте мультиплатформенные социальные кампании: от краткого описания к публикации. |
| [**tldraw-offline**](/docs/user-guide/skills/optional/creative/creative-tldraw-offline) | Управляйте и создайте сценарии для создания автономных холстов с помощью агента. |
| [**unreal-mcp**](/docs/user-guide/skills/optional/creative/creative-unreal-mcp) | Автоматизируйте сцены, актеров и рендеринг редактора Unreal Engine. |

## наука о данных

| Навык | Описание |
|-------|-------------|
| [**jupyter-notebook**](/docs/user-guide/skills/optional/data-science/data-science-jupyter-notebook) | Итеративный Python с использованием живого ядра Jupyter (hamelnb). |

## девопс

| Навык | Описание |
|-------|-------------|
| [**фактическая настройка**](/docs/user-guide/skills/optional/devops/devops-actual-setup) | Настройте вывод Actual Computer (actual.inc) в Hermes. |
| [**docker-management**](/docs/user-guide/skills/optional/devops/devops-docker-management) | Управляйте контейнерами Docker, изображениями, томами и Compose. |
| [**hermes-s6-container-supervision**](/docs/user-guide/skills/optional/devops/devops-hermes-s6-container-supervision) | Измените или отладьте сервисы s6 в образе Hermes Docker. |
| [**inference-sh-cli**](/docs/user-guide/skills/optional/devops/devops-inference-sh-cli) | Запускайте более 150 приложений искусственного интеллекта (изображения, видео, LLM) через интерфейс командной строки inference.sh. |
| [**pinggy-tunnel**](/docs/user-guide/skills/optional/devops/devops-pinggy-tunnel) | Туннели локального хоста без установки через SSH через Pinggy. |
| [**watchers**](/docs/user-guide/skills/optional/devops/devops-watchers) | Опрос RSS, API JSON и GitHub с дедупликацией водяных знаков. |

## тестовая версия

| Навык | Описание |
|-------|-------------|
| [**adversarial-ux-test**](/docs/user-guide/skills/optional/dogfood/dogfood-adversarial-ux-test) | Разыграйте враждебно настроенного пользователя, чтобы найти и выявить болевые точки UX. |

## электронная почта

| Навык | Описание |
|-------|-------------|
| [**agentmail**](/docs/user-guide/skills/optional/email/email-agentmail) | Предоставьте агенту собственный почтовый ящик: отправляйте и получайте электронную почту. |

## финансы

| Навык | Описание |
|-------|-------------|
| [**модель-3-выражения**](/docs/user-guide/skills/optional/finance/finance-3-statement-model) | Создавайте интегрированные финансовые книги IS/BS/CF в Excel. |
| [**комп-анализ**](/docs/user-guide/skills/optional/finance/finance-comps-analysis) | Создавайте книги оценки сопоставимых компаний в Excel. |
| [**dcf-model**](/docs/user-guide/skills/optional/finance/finance-dcf-model) | Создавайте книги оценки дисконтированных денежных потоков в Excel. |
| [**excel-author**](/docs/user-guide/skills/optional/finance/finance-excel-author) | Создавайте проверяемые финансовые книги без головы с помощью openpyxl. |
| [**lbo-модель**](/docs/user-guide/skills/optional/finance/finance-lbo-model) | Создавайте книги по выкупу заемных средств с помощью IRR/MOIC в Excel. |
| [**модель слияния**](/docs/user-guide/skills/optional/finance/finance-merger-model) | Создавайте книги по увеличению/разводнению слияний и поглощений в Excel. |
| [**polymarket**](/docs/user-guide/skills/optional/finance/finance-polymarket) | Запрос Полимаркет: рынки, цены, книги заказов, история. |
| [**pptx-author**](/docs/user-guide/skills/optional/finance/finance-pptx-author) | Создавайте презентации PowerPoint без головы с помощью python-pptx. |
| [**акции**](/docs/user-guide/skills/optional/finance/finance-stocks) | Котировки акций, история, поиск, сравнение, криптография через Yahoo. |

## игры

| Навык | Описание |
|-------|-------------|
| [**minecraft-modpack-server**](/docs/user-guide/skills/optional/gaming/gaming-minecraft-modpack-server) | Хостинг модифицированных серверов Minecraft (CurseForge, Modrinth). |
| [**pokemon-player**](/docs/user-guide/skills/optional/gaming/gaming-pokemon-player) | Играйте в покемонов через безголовый эмулятор + чтение ОЗУ. |

## здоровье

| Навык | Описание |
|-------|-------------|
| [**фитнес-питание**](/docs/user-guide/skills/optional/health/health-fitness-nutrition) | Планирование тренировок, макросы и показатели тела через wger/USDA. |
| [**neuroskill-bci**](/docs/user-guide/skills/optional/health/health-neuroskill-bci) | Используйте живое когнитивное состояние BCI и состояние настроения от NeuroSkill. |

## мкп

| Навык | Описание |
|-------|-------------|
| [**fastmcp**](/docs/user-guide/skills/optional/mcp/mcp-fastmcp) | Создавайте, тестируйте и развертывайте серверы Python MCP. |
| [**mcp-oauth-remote-gateway**](/docs/user-guide/skills/optional/mcp/mcp-mcp-oauth-remote-gateway) | Ручной OAuth для удаленных серверов MCP на безголовых шлюзах. |
| [**mcporter**](/docs/user-guide/skills/optional/mcp/mcp-mcporter) | Список, аутентификация и вызов серверов/инструментов MCP с терминала. |

## миграция

| Навык | Описание |
|-------|-------------|
| [**openclaw-migration**](/docs/user-guide/skills/optional/migration/migration-openclaw-migration) | Импортируйте настройки OpenClaw (воспоминания, навыки) в Hermes. |

## млопс

| Навык | Описание |
|-------|-------------|
| [**ускорение**](/docs/user-guide/skills/optional/mlops/mlops-accelerate) | Запускайте обучение PyTorch на всех графических процессорах с минимальными изменениями. |
| [**axolotl**](/docs/user-guide/skills/optional/mlops/mlops-training-axolotl) | Аксолотль: тонкая настройка YAML LLM (LoRA, DPO, GRPO). |
| [**chroma**](/docs/user-guide/skills/optional/mlops/mlops-chroma) | Встраивание базы данных для RAG и семантического поиска. |
| [**clip**](/docs/user-guide/skills/optional/mlops/mlops-clip) | Классификация изображений с нулевым кадром и поиск по изображению и тексту. |
| [**dspy**](/docs/user-guide/skills/optional/mlops/mlops-research-dspy) | DSPy: декларативные программы LM, подсказки автооптимизации, RAG. |
| [**faiss**](/docs/user-guide/skills/optional/mlops/mlops-faiss) | Быстрый поиск сходства векторов в миллиардном масштабе. |
| [**flash-attention**](/docs/user-guide/skills/optional/mlops/mlops-flash-attention) | Ускорьте обучение и вывод трансформаторов длинных последовательностей. |
| [**руководство**](/docs/user-guide/skills/optional/mlops/mlops-guidance) | Ограничьте вывод LLM с помощью грамматик; гарантировать действительный JSON. |
| [**huggingface-tokenizers**](/docs/user-guide/skills/optional/mlops/mlops-huggingface-tokenizers) | Быстрая токенизация BPE/WordPiece и индивидуальное обучение словарному запасу. |
| [**инструктор**](/docs/user-guide/skills/optional/mlops/mlops-instructor) | Структурированные результаты LLM проверены с помощью Pydantic. |
| [**lambda-labs**](/docs/user-guide/skills/optional/mlops/mlops-lambda-labs) | Облачные экземпляры графического процессора по требованию для обучения машинному обучению. |
| [**llava**](/docs/user-guide/skills/optional/mlops/mlops-llava) | Чат на языке видения: VQA, субтитры, диалоги изображений. |
| [**модальный**](/docs/user-guide/skills/optional/mlops/mlops-modal) | Бессерверное облако графических процессоров для заданий машинного обучения и API-интерфейсов моделей. |
| [**nemo-curator**](/docs/user-guide/skills/optional/mlops/mlops-nemo-curator) | Курировать данные обучения LLM: дедупликация, фильтрация, редактирование личных данных. |
| [**obliteratus**](/docs/user-guide/skills/optional/mlops/mlops-obliteratus) | OBLITERATUS: аннулирование отказов от LLM (разница в средствах). |
| [**outlines**](/docs/user-guide/skills/optional/mlops/mlops-inference-outlines) | Краткое описание: структурированная генерация JSON/regex/Pydantic LLM. |
| [**peft**](/docs/user-guide/skills/optional/mlops/mlops-peft) | Точная настройка больших LLM с помощью LoRA на ограниченной памяти графического процессора. |
| [**сосновая шишка**](/docs/user-guide/skills/optional/mlops/mlops-pinecone) | Управляемая векторная БД для производства РАГ и поиска. |
| [**pytorch-fsdp**](/docs/user-guide/skills/optional/mlops/mlops-pytorch-fsdp) | Полностью сегментированное параллельное обучение для больших моделей. |
| [**pytorch-lightning**](/docs/user-guide/skills/optional/mlops/mlops-pytorch-lightning) | Чистые циклы обучения со встроенной распределенной поддержкой. |
| [**qdrant**](/docs/user-guide/skills/optional/mlops/mlops-qdrant) | Векторный поисковик для производственных RAG-систем. |
| [**saelens**](/docs/user-guide/skills/optional/mlops/mlops-saelens) | Обучите разреженные автоэнкодеры интерпретировать функции модели. |
| [**segment-anything-model**](/docs/user-guide/skills/optional/mlops/mlops-models-segment-anything-model) | SAM: сегментация изображения с нулевым кадром с помощью точек, прямоугольников, масок. |
| [**simpo**](/docs/user-guide/skills/optional/mlops/mlops-simpo) | Выравнивание предпочтений без ссылок, проще, чем DPO. |
| [**slime**](/docs/user-guide/skills/optional/mlops/mlops-slime) | Пост-тренинг RL для LLM с Megatron и SGLang. |
| [**stable-diffusion**](/docs/user-guide/skills/optional/mlops/mlops-stable-diffusion) | Генерация текста в изображение, рисование и img2img. |
| [**tensorrt-llm**](/docs/user-guide/skills/optional/mlops/mlops-tensorrt-llm) | Высокопроизводительный вывод LLM на графических процессорах NVIDIA. |
| [**torchtitan**](/docs/user-guide/skills/optional/mlops/mlops-torchtitan) | Предварительно обучайте LLM в нужном масштабе с помощью 4D-параллелизма PyTorch. |
| [**trl-fine-tuning**](/docs/user-guide/skills/optional/mlops/mlops-training-trl-fine-tuning) | TRL: моделирование вознаграждений SFT, DPO, GRPO, RLOO для LLM RLHF. |
| [**unsloth**](/docs/user-guide/skills/optional/mlops/mlops-training-unsloth) | Unsloth: точная настройка LoRA/QLoRA в 2–5 раз быстрее, меньше видеопамяти. |
| [**whisper**](/docs/user-guide/skills/optional/mlops/mlops-whisper) | Транскрибируйте и переводите речь на 99 языках. |

## платежей

| Навык | Описание |
|-------|-------------|
| [**mpp-agent**](/docs/user-guide/skills/optional/Payments/Payments-mpp-agent) | Оплачивайте API HTTP 402 через протокол машинных платежей (MPP). |
| [**stripe-link-cli**](/docs/user-guide/skills/optional/Payments/Payments-stripe-link-cli) | Платежи агентам через Stripe Link — карты, SPT, одобрения. |
| [**stripe-projects**](/docs/user-guide/skills/optional/Payments/Payments-Stripe-Projects) | Предоставление услуг SaaS + учетные данные для синхронизации через Stripe Projects. |

## производительность

| Навык | Описание |
|-------|-------------|
| [**canvas**](/docs/user-guide/skills/optional/productivity/productivity-canvas) | Получайте курсы и задания Canvas LMS через токен API. |
| [**здесь-сейчас**](/docs/user-guide/skills/optional/productivity/productivity-here-now) | Публикуйте сайты в &#123;slug&#125;.here.now и сохраняйте файлы на Дисках. |
| [**памятные карточки**](/docs/user-guide/skills/optional/productivity/productivity-memento-flashcards) | Карточки с интервальным повторением: создавайте, просматривайте, тестируйте, экспортируйте. |
| [**магазин**](/docs/user-guide/skills/optional/productivity/productivity-shop) | Поиск по каталогу магазина, оформление заказа, отслеживание заказов, возвраты. |
| [**shopify**](/docs/user-guide/skills/optional/productivity/productivity-shopify) | Запросить API администратора Shopify/витрины GraphQL через Curl. |
| [**сиюань**](/docs/user-guide/skills/optional/productivity/productivity-siyuan) | Запрашивайте и редактируйте базу знаний SiYuan через API. |
| [**телефония**](/docs/user-guide/skills/optional/productivity/productivity-telephony) | Предоставление номеров Twilio, исходящих вызовов SMS/MMS и AI. |

## исследование

| Навык | Описание |
|-------|-------------|
| [**биоинформатика**](/docs/user-guide/skills/optional/research/research-bioinformatics) | Доступ к более чем 400 навыкам в области геномики и вычислительной биологии. |
| [**darwinian-evolver**](/docs/user-guide/skills/optional/research/research-darwinian-evolver) | Развивайте запросы/регулярные выражения/SQL/код с помощью цикла эволюции Imbue. |
| [**domain-intel**](/docs/user-guide/skills/optional/research/research-domain-intel) | Пассивная проверка поддоменов, сертификатов SSL, WHOIS и DNS. |
| [**открытие лекарств**](/docs/user-guide/skills/optional/research/research-drug-discovery) | Открытие лекарств: поиск ChEMBL, сходство лекарств, взаимодействие. |
| [**duckduckgo-search**](/docs/user-guide/skills/optional/research/research-duckduckgo-search) | Бесплатный поиск в Интернете, новостях и изображениях без ключа через ddgs. |
| [**gitnexus-explorer**](/docs/user-guide/skills/optional/research/research-gitnexus-explorer) | Предоставляйте интерактивный веб-интерфейс графа знаний базы кода. |
| [**osint-investigation**](/docs/user-guide/skills/optional/research/research-osint-investigation) | Следите за деньгами через публичные записи и данные о санкциях. |
| [**parallel-cli**](/docs/user-guide/skills/optional/research/research-parallel-cli) | Собственный агентский веб-поиск, глубокие исследования и обогащение. |
| [**pinecone-research**](/docs/user-guide/skills/optional/research/research-pinecone-research) | Агент РАГ и долговременная память с Шишкой. |
| [**qmd**](/docs/user-guide/skills/optional/research/research-qmd) | Гибридный локальный поиск по заметкам, документам и расшифровкам. |
| [**скраппинг**](/docs/user-guide/skills/optional/research/research-скрапинг) | Парсинг сайтов с помощью скрытого просмотра и обхода Cloudflare. |
| [**searxng-search**](/docs/user-guide/skills/optional/research/research-searxng-search) | Бесплатный метапоиск без ключа, объединяющий более 70 систем. |

## безопасность

| Навык | Описание |
|-------|-------------|
| [**1пароль**](/docs/user-guide/skills/optional/security/security-1password) | Настройте интерфейс командной строки, войдите в систему и прочитайте или внедрите секреты. |
| [**godmode**](/docs/user-guide/skills/optional/security/security-godmode) | Магистр побега из тюрьмы: Parseltongue, GODMODE, ULTRAPLINIAN. |
| [**oss-forensics**](/docs/user-guide/skills/optional/security/security-oss-forensics) | Экспертиза цепочки поставок GitHub: восстановление, IOC, отчетность. |
| [**шерлок**](/docs/user-guide/skills/optional/security/security-sherlock) | Найдите учетные записи по имени пользователя на более чем 400 платформах. |
| [**unbroker**](/docs/user-guide/skills/optional/security/security-unbroker) | Автономно удаляйте свою информацию с сайтов брокеров данных. |
| [**веб-пентест**](/docs/user-guide/skills/optional/security/security-web-pentest) | Авторизованный веб-пентест: разведка, эксплойты, основанные на доказательствах, отчет. |

##-разработка программного обеспечения

| Навык | Описание |
|-------|-------------|
| [**код-вики**](/docs/user-guide/skills/optional/software-development/software-development-code-wiki) | Создавайте вики-документы + диаграммы Mermaid для любой кодовой базы. |
| [**rest-graphql-debug**](/docs/user-guide/skills/optional/software-development/software-development-rest-graphql-debug) | Отладка API REST/GraphQL: коды состояния, аутентификация, схемы, воспроизведение. |
| [**Разработка-под агентом**](/docs/user-guide/skills/optional/software-development/software-development-subagent-driven-development) | Выполнять планы через субагенты Delegate_task (двухэтапная проверка). |

## веб-разработка

| Навык | Описание |
|-------|-------------|
| [**cloudflare-temporary-deploy**](/docs/user-guide/skills/optional/web-development/web-development-cloudflare-temporary-deploy) | Разверните Worker в реальном времени, без учетной записи, через wrangler --temporary. |
| [**page-agent**](/docs/user-guide/skills/optional/web-development/web-development-page-agent) | Встраивайте встроенный второй пилотный модуль графического пользовательского интерфейса на естественном языке в веб-приложения. |

## юаньбао

| Навык | Описание |
|-------|-------------|
| [**yuanbao**](/docs/user-guide/skills/optional/yuanbao/yuanbao-yuanbao) | Группы Юаньбао (元宝): @упоминание пользователей, запрос информации/участников. |

---

## Вклад дополнительных навыков

Чтобы добавить новый дополнительный навык в репозиторий:

1. Создайте каталог в `optional-skills/<category>/<skill-name>/`.
2. Добавьте `SKILL.md` со стандартной заставкой (имя, описание, версия, автор)
3. Включите все вспомогательные файлы в подкаталоги `references/`, `templates/` или `scripts/`.
4. Отправьте запрос на включение — навык появится в этом каталоге и после объединения получит собственную страницу документации.