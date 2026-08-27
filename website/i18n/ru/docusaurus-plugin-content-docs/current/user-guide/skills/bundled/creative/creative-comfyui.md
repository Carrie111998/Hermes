---
title: Comfyui — генерация изображений, видео и аудио с помощью рабочих процессов
  распространения.
sidebar_label: Comfyui
description: Создавайте изображения, видео и аудио с помощью рабочих процессов распространения.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

#Комфьюи

Создавайте изображения, видео и аудио с помощью рабочих процессов распространения.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/creative/comfyui` |
| Версия | `5.1.0` |
| Автор | ['kshitijk4poor', 'alt-glitch', 'purzbeats'] |
| Лицензия | Массачусетский технологический институт |
| Платформы | Macos, Linux, Windows |
| Теги | `comfyui`, `image-generation`, `stable-diffusion`, `flux`, `sd3`, `wan-video`, `hunyuan-video`, `creative`, `generative-ai`, `video-generation` |
| Сопутствующие навыки | [`stable-diffusion`](/docs/user-guide/skills/optional/mlops/mlops-stable-diffusion) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Удобный пользовательский интерфейс

Создавайте изображения, видео, аудио и 3D-контент с помощью ComfyUI, используя
официальный `comfy-cli` для настройки/жизненного цикла и прямого API REST/WebSocket
для выполнения рабочего процесса.

## Что в этом навыке

**Справочная документация (`references/`):**

- `official-cli.md` — каждая команда `comfy ...` с флагами.
- `rest-api.md` — конечные точки REST + WebSocket (локальные + облако), схемы полезной нагрузки.
- `workflow-format.md` — API-формат JSON, общие типы узлов, сопоставление параметров.
- `template-integrity.md` — преобразование `comfyui-workflow-templates` из
  формат редактора в формат API: перенаправление обхода, пунктирные клавиши динамического ввода
  (`values.a`, `resize_type.width`), особенности облака (перенаправление 302, 1 одновременный
  работа бесплатного уровня, потолок VRAM 1080p), Discord-совместимый стежок ffmpeg.
  Автор: [@purzbeats](https://github.com/purzbeats). Загружайте это когда угодно
  вы начинаете с официального шаблона.

**Скрипты (`scripts/`):**

| Скрипт | Цель |
|--------|---------|
| `_common.py` | Общий HTTP, облачная маршрутизация, каталоги узлов (не запускать напрямую) |
| `hardware_check.py` | Проверьте графический процессор/VRAM/диск → рекомендуйте локальное или Comfy Cloud |
| `comfyui_setup.sh` | Проверка оборудования + comfy-cli + установка ComfyUI + запуск + проверка |
| `extract_schema.py` | Прочтите рабочий процесс → список управляемых параметров + описания модели |
| `check_deps.py` | Проверьте рабочий процесс на работающем сервере → перечислите недостающие узлы/модели |
| `auto_fix_deps.py` | Запустите check_deps, затем `comfy node install` / `comfy model download` |
| `run_workflow.py` | Внедрение параметров, отправка, мониторинг, загрузка результатов (HTTP или WS) |
| `run_batch.py` | Отправьте рабочий процесс N раз с развертками параллельно до вашего уровня |
| `ws_monitor.py` | Средство просмотра WebSocket в реальном времени для выполнения заданий (прогресс в реальном времени) |
| `health_check.py` | Средство проверки контрольного списка — comfy-cli + сервер + модели + дымовой тест |
| `fetch_logs.py` | Получение сообщений трассировки/статуса для данного Prompt_id |

**Примеры рабочих процессов (`workflows/`):** SD 1.5, SDXL, Flux Dev, SDXL img2img,
SDXL Inpaint, ESRGAN Upscale, видео AnimateDiff, Wan T2V. См.
`workflows/README.md`.

## Когда использовать

- Пользователь просит создать изображения с помощью Stable Diffusion, SDXL, Flux, SD3 и т. д.
- Пользователь хочет запустить определенный файл рабочего процесса ComfyUI.
- Пользователь хочет объединить генеративные шаги (txt2img → масштабирование → восстановление лица)
- Пользователю необходимы ControlNet, inpainting, img2img или другие продвинутые конвейеры.
- Пользователь просит управлять очередью ComfyUI, проверять модели или устанавливать собственные узлы.
- Пользователь хочет создать видео/аудио/3D с помощью AnimateDiff, Hunyuan, Wan, AudioCraft и т. д.

## Архитектура: два слоя

<!-- ascii-guard-ignore -->
```
┌─────────────────────────────────────────────────────┐
│ Layer 1: comfy-cli (official lifecycle tool)        │
│   Setup, server lifecycle, custom nodes, models     │
│   → comfy install / launch / stop / node / model    │
└─────────────────────────┬───────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────┐
│ Layer 2: REST/WebSocket API + skill scripts         │
│   Workflow execution, param injection, monitoring   │
│   POST /api/prompt, GET /api/view, WS /ws           │
│   → run_workflow.py, run_batch.py, ws_monitor.py    │
└─────────────────────────────────────────────────────┘
```
<!-- ascii-guard-ignore-end -->

**Почему два уровня?** Официальный интерфейс командной строки отлично подходит для установки и сервера.
управление, но имеет минимальную поддержку выполнения рабочих процессов. API REST/WS заполняет
этот пробел — сценарии обрабатывают внедрение параметров, мониторинг выполнения и
выходная загрузка, которую CLI не делает.

## Быстрый старт

### Определить среду

```bash
# What's available?
command -v comfy >/dev/null 2>&1 && echo "comfy-cli: installed"
curl -s http://127.0.0.1:8188/system_stats 2>/dev/null && echo "server: running"

# Can this machine run ComfyUI locally? (GPU/VRAM/disk check)
python3 scripts/hardware_check.py
```

Если ничего не установлено, см. раздел **Настройка и подключение** ниже, но всегда запускайте
сначала проверьте оборудование.

### Проверка работоспособности в одну строку

```bash
python3 scripts/health_check.py
# → JSON: comfy_cli on PATH? server reachable? at least one checkpoint? smoke-test passes?
```

## Основной рабочий процесс

### Шаг 1. Получите JSON рабочего процесса в формате API

Рабочие процессы должны быть в формате API (каждый узел имеет `class_type`). Они происходят из:

- Веб-интерфейс ComfyUI → **Рабочий процесс → Экспорт (API)** (более новый интерфейс) или
  устаревшая кнопка «Сохранить (формат API)» (более старый пользовательский интерфейс)
- Каталог `workflows/` этого навыка (готовые примеры)
- Загрузки сообщества (civitai, Reddit, Discord) — обычно формат редактора,
  необходимо загрузить в ComfyUI, а затем повторно экспортировать

Формат редактора (массивы верхнего уровня `nodes` и `links`) **не напрямую
исполняемый файл**. Сценарии обнаруживают это и предлагают выполнить повторный экспорт.

### Шаг 2. Посмотрите, что можно контролировать

```bash
python3 scripts/extract_schema.py workflow_api.json --summary-only
# → {"parameter_count": 12, "has_negative_prompt": true, "has_seed": true, ...}

python3 scripts/extract_schema.py workflow_api.json
# → full schema with parameters, model deps, embedding refs
```

### Шаг 3. Запуск с параметрами

```bash
# Local (defaults to http://127.0.0.1:8188)
python3 scripts/run_workflow.py \
  --workflow workflow_api.json \
  --args '{"prompt": "a beautiful sunset over mountains", "seed": -1, "steps": 30}' \
  --output-dir ./outputs

# Cloud (export API key once; uses correct /api routing automatically)
export COMFY_CLOUD_API_KEY="comfyui-..."
python3 scripts/run_workflow.py \
  --workflow workflow_api.json \
  --args '{"prompt": "..."}' \
  --host https://cloud.comfy.org \
  --output-dir ./outputs

# Real-time progress via WebSocket (requires `pip install websocket-client`)
python3 scripts/run_workflow.py \
  --workflow flux_dev.json \
  --args '{"prompt": "..."}' \
  --ws

# img2img / inpaint: pass --input-image to upload + reference automatically
python3 scripts/run_workflow.py \
  --workflow sdxl_img2img.json \
  --input-image image=./photo.png \
  --args '{"prompt": "make it watercolor", "denoise": 0.6}'

# Batch / sweep: 8 random seeds, parallel up to cloud tier limit
python3 scripts/run_batch.py \
  --workflow sdxl.json \
  --args '{"prompt": "abstract"}' \
  --count 8 --randomize-seed --parallel 3 \
  --output-dir ./outputs/batch
```

`-1` для `seed` (или его отсутствие с помощью `--randomize-seed`) генерирует новый
случайное семя за прогон.

### Шаг 4: Представление результатов

Скрипты выдают JSON на стандартный вывод, описывая каждый выходной файл:

```json
{
  "status": "success",
  "prompt_id": "abc-123",
  "outputs": [
    {"file": "./outputs/sdxl_00001_.png", "node_id": "9",
     "type": "image", "filename": "sdxl_00001_.png"}
  ]
}
```

## Дерево решений

| Пользователь говорит | Инструмент | Команда |
|-----------|------|---------|
| **Жизненный цикл (используйте comfy-cli)** | | |
| «установить ComfyUI» | удобный-кли | `bash scripts/comfyui_setup.sh` |
| «запустить ComfyUI» | удобный-кли | `comfy launch --background` |
| «остановить ComfyUI» | удобный-кли | `comfy stop` |
| «установить X-узел» | удобный-кли | `comfy node install <name>` |
| «скачать модель X» | удобный-кли | `comfy model download --url <url> --relative-path models/checkpoints` |
| "список установленных моделей" | удобный-кли | `comfy model list` |
| "список установленных узлов" | удобный-кли | `comfy node show installed` |
| **Выполнение (используйте скрипты)** | | |
| "все готово?" | сценарий | `health_check.py` (необязательно с `--workflow X --smoke-test`) |
| «Что я могу изменить в этом рабочем процессе?» | сценарий | `extract_schema.py W.json` |
| "проверьте, соблюдены ли требования W" | сценарий | `check_deps.py W.json` |
| "исправить недостающие данные" | сценарий | `auto_fix_deps.py W.json` |
| «создать изображение» | сценарий | `run_workflow.py --workflow W --args '{...}'` |
| «используйте это изображение» (img2img) | сценарий | `run_workflow.py --input-image image=./x.png ...` |
| «8 вариантов со случайными семенами» | сценарий | `run_batch.py --count 8 --randomize-seed ...` |
| «покажи мне прогресс вживую» | сценарий | `ws_monitor.py --prompt-id <id>` |
| «получить ошибку из задания X» | сценарий | `fetch_logs.py <prompt_id>` |
| **Прямой ОТДЫХ** | | |
| «Что в очереди?» | ОТДЫХ | `curl http://HOST:8188/queue` (локальный) или `--host https://cloud.comfy.org` |
| "отменить это" | ОТДЫХ | `curl -X POST http://HOST:8188/interrupt` |
| «свободная память графического процессора» | ОТДЫХ | `curl -X POST http://HOST:8188/free` |

## Настройка и адаптация

Когда пользователь просит настроить ComfyUI, **ПЕРВОЕ, что нужно сделать, — это спросить,
им нужен Comfy Cloud (хостинг, нулевая установка, ключ API) или локальный (установка
ComfyUI на своей машине)**. Не запускайте команды установки или оборудование
проверяет, пока не ответят.

**Официальная документация:** https://docs.comfy.org/installation.
**Документация CLI:** https://docs.comfy.org/comfy-cli/getting-started.
**Облачная документация:** https://docs.comfy.org/get_started/cloud.
**Облачный API:** https://docs.comfy.org/development/cloud/overview.

### Шаг 0. Спросите локальное или облачное решение (ВСЕГДА ПЕРВЫМ)

Предлагаемый сценарий:

> «Хотите ли вы запустить ComfyUI локально на своем компьютере или использовать Comfy Cloud?
>
> - **Comfy Cloud** — размещение на графических процессорах RTX 6000 Pro, все распространенные модели предустановлены,
>нулевая настройка. Требуется ключ API (для фактического запуска требуется платная подписка).
> рабочие процессы; бесплатный уровень доступен только для чтения). Лучше всего, если у вас нет подходящего графического процессора.
> - **Локально** — бесплатно, но ваша машина ДОЛЖНА соответствовать аппаратным требованиям:
> - Графический процессор NVIDIA с **≥6 ГБ видеопамяти** (≥8 ГБ для SDXL, ≥12 ГБ для Flux/видео), ИЛИ
> - Графический процессор AMD с поддержкой ROCm (Linux), ИЛИ
> - Apple Silicon Mac (M1+) с **унифицированной памятью ≥16 ГБ** (рекомендуется ≥32 ГБ).
> - Компьютеры Intel Mac и машины без графического процессора НЕ будут работать — вместо этого используйте Cloud.
>
> Что бы ты хотел?»

Маршрутизация:

- **Облако** → перейдите к **Пути A**.
- **Локальный** → сначала запустите проверку оборудования, а затем выберите путь из путей B–E на основе вердикта.
- **Не уверен** → запустите проверку оборудования и вынесите вердикт.

### Шаг 1. Проверьте оборудование (ТОЛЬКО если пользователь выбрал локальное устройство)

```bash
python3 scripts/hardware_check.py --json
# Optional: also probe `torch` for actual CUDA/MPS:
python3 scripts/hardware_check.py --json --check-pytorch
```

| Вердикт | Значение | Действие |
|------------|---------------------------------------------------------------|--------|
| `ok` | ≥8 ГБ видеопамяти (дискретная) ИЛИ ≥32 ГБ унифицированной (Apple Silicon) | Локальная установка — используйте `comfy_cli_flag` из отчета |
| `marginal` | SD1.5 работает; SDXL плотный; Flux/видео маловероятно | Локально — ОК для легких рабочих процессов, иначе **Путь A (Облако)** |
| `cloud` | Нет доступного графического процессора, &lt;6 ГБ видеопамяти, &lt;16 ГБ Apple unified, Intel Mac, Rosetta Python | **Переключиться на облако**, если пользователь явно не принудит локальный |

Сценарий также отображает `wsl: true` (WSL2 с пробросом NVIDIA) и
`rosetta: true` (x86_64 Python на Apple Silicon — необходимо переустановить как ARM64).

Если вердикт — `cloud`, но пользователю нужен локальный вариант, не действуйте молча.
Покажите массив `notes` дословно и спросите, хотят ли они (а) переключиться на
Облако или (б) принудительная локальная установка (будет неудобна или будет неприемлемо медленной на современных моделях).

### Выбор пути установки

Сначала используйте проверку оборудования. В таблице ниже приведен запасной вариант на случай, если
пользователь уже сообщил вам свое оборудование:

| Ситуация | Рекомендуемый путь |
|-----------|------------------|
| `verdict: cloud` из проверки оборудования | **Путь А: Комфортное облако** |
| Нет графического процессора / хочу попробовать без обязательств | **Путь А: Комфортное облако** |
| Windows + NVIDIA + нетехнические | **Путь Б: ComfyUI Desktop** |
| Windows + NVIDIA + техническая | **Путь C: Портативный** или **Путь D: comfy-cli** |
| Linux + любой графический процессор | **Путь D: comfy-cli** (самый простой) |
| macOS + Apple Silicon | **Путь Б: Рабочий стол** или **Путь D: comfy-cli** |
| Безголовый/сервер/CI/агенты | **Путь D: comfy-cli** |

Для полностью автоматического пути (проверка оборудования → установка → запуск → проверка):

```bash
bash scripts/comfyui_setup.sh
# Or with overrides:
bash scripts/comfyui_setup.sh --m-series --port=8190 --workspace=/data/comfy
```

Он запускает `hardware_check.py` внутри себя, отказывается устанавливаться локально, когда
вердикт: `cloud` (если только `--force-cloud-override`), выбирает правильный вариант
`comfy-cli` флаг и предпочитает `pipx`/`uvx` глобальному `pip`, чтобы избежать загрязнения
система Питон.

---

### Путь A: Comfy Cloud (без локальной установки)

Для пользователей без подходящего графического процессора или для тех, кто не хочет выполнять настройку. Хостинг на RTX 6000 Pro.

**Документация:** https://docs.comfy.org/get_started/cloud.

1. Зарегистрируйтесь на https://comfy.org/cloud.
2. Сгенерируйте ключ API на https://platform.comfy.org/login.
3. Установите ключ:
   ```bash
   export COMFY_CLOUD_API_KEY="your-comfyui-key"
   ```
4. Запустите рабочие процессы:
   ```bash
   python3 scripts/run_workflow.py \
     --workflow workflows/flux_dev_txt2img.json \
     --args '{"prompt": "..."}' \
     --host https://cloud.comfy.org \
     --output-dir ./outputs
   ```

**Цены:** https://www.comfy.org/cloud/pricing.
**Параллельные задания:** Бесплатная/Стандартная 1, Creator 3, Pro 5. Уровень бесплатного пользования.
**невозможно запускать рабочие процессы через API** — только просматривать модели. Платная подписка
требуется для `/api/prompt`, `/api/upload/*`, `/api/view` и т. д.

---

### Путь B: Рабочий стол ComfyUI (Windows/macOS)

Установка в один клик для нетехнических пользователей. В настоящее время бета.

**Документация:** https://docs.comfy.org/installation/desktop.
- **Windows (NVIDIA):** https://download.comfy.org/windows/nsis/x64
- **macOS (Apple Silicon):** https://comfy.org

Linux **не поддерживается** для настольных компьютеров — используйте путь D.

---

### Путь C: ComfyUI Portable (только для Windows)

**Документация:** https://docs.comfy.org/installation/comfyui_portable_windows.

Загрузите с https://github.com/comfyanonymous/ComfyUI/releases, извлеките,
запустите `run_nvidia_gpu.bat`. Обновите через `update/update_comfyui_stable.bat`.

---

### Путь D: comfy-cli (все платформы — рекомендуется для агентов)

Официальный интерфейс командной строки — лучший способ для автономной/автоматической настройки.

**Документация:** https://docs.comfy.org/comfy-cli/getting-started.

#### Установите comfy-cli

```bash
# Recommended:
pipx install comfy-cli
# Or use uvx without installing:
uvx --from comfy-cli comfy --help
# Or (if pipx/uvx unavailable):
pip install --user comfy-cli
```

Отключите аналитику в неинтерактивном режиме:
```bash
comfy --skip-prompt tracking disable
```

#### Установите ComfyUI

```bash
comfy --skip-prompt install --nvidia              # NVIDIA (CUDA)
comfy --skip-prompt install --amd                 # AMD (ROCm, Linux)
comfy --skip-prompt install --m-series            # Apple Silicon (MPS)
comfy --skip-prompt install --cpu                 # CPU only (slow)
comfy --skip-prompt install --nvidia --fast-deps  # uv-based dep resolution
```

Местоположение по умолчанию: `~/comfy/ComfyUI` (Linux), `~/Documents/comfy/ComfyUI`.
(macOS/Win). Переопределить с помощью `comfy --workspace /custom/path install`.

#### Запуск/проверка

```bash
comfy launch --background                       # background daemon on :8188
comfy launch -- --listen 0.0.0.0 --port 8190    # LAN-accessible custom port
curl -s http://127.0.0.1:8188/system_stats      # health check
```

---

### Путь E: Установка вручную (дополнительно/неподдерживаемое оборудование)

Для Ascend NPU, Cambricon MLU, Intel Arc или другого неподдерживаемого оборудования.

**Документация:** https://docs.comfy.org/installation/manual_install.

```bash
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130
pip install -r requirements.txt
python main.py
```

---

### После установки: загрузка моделей

```bash
# SDXL (general purpose, ~6.5 GB)
comfy model download \
  --url "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" \
  --relative-path models/checkpoints

# SD 1.5 (lighter, ~4 GB, good for 6 GB cards)
comfy model download \
  --url "https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors" \
  --relative-path models/checkpoints

# Flux Dev fp8 (smaller variant, ~12 GB)
comfy model download \
  --url "https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors" \
  --relative-path models/checkpoints

# CivitAI (set token first):
comfy model download \
  --url "https://civitai.com/api/download/models/128713" \
  --relative-path models/checkpoints \
  --set-civitai-api-token "YOUR_TOKEN"
```

Список установленных: `comfy model list`.

### После установки: установка пользовательских узлов

```bash
comfy node install comfyui-impact-pack             # popular utility pack
comfy node install comfyui-animatediff-evolved     # video generation
comfy node install comfyui-controlnet-aux          # ControlNet preprocessors
comfy node install comfyui-essentials              # common helpers
comfy node update all
comfy node install-deps --workflow=workflow.json   # install everything a workflow needs
```

### После установки: проверьте

```bash
python3 scripts/health_check.py
# → comfy_cli on PATH? server reachable? checkpoints? smoke test?

python3 scripts/check_deps.py my_workflow.json
# → are this workflow's nodes/models/embeddings installed?

python3 scripts/run_workflow.py \
  --workflow workflows/sd15_txt2img.json \
  --args '{"prompt": "test", "steps": 4}' \
  --output-dir ./test-outputs
```

## Загрузка изображения (img2img/Inpainting)

Самый простой способ — использовать `--input-image` с `run_workflow.py`:

```bash
python3 scripts/run_workflow.py \
  --workflow workflows/sdxl_img2img.json \
  --input-image image=./photo.png \
  --args '{"prompt": "make it cyberpunk", "denoise": 0.6}'
```

Флаг загружает `photo.png`, а затем вставляет его имя файла на стороне сервера в
любой параметр схемы имеет имя `image`. Для рисования передайте оба:

```bash
python3 scripts/run_workflow.py \
  --workflow workflows/sdxl_inpaint.json \
  --input-image image=./photo.png \
  --input-image mask_image=./mask.png \
  --args '{"prompt": "fill with flowers"}'
```

Ручная загрузка через REST:
```bash
curl -X POST "http://127.0.0.1:8188/upload/image" \
  -F "image=@photo.png" -F "type=input" -F "overwrite=true"
# Returns: {"name": "photo.png", "subfolder": "", "type": "input"}

# Cloud equivalent:
curl -X POST "https://cloud.comfy.org/api/upload/image" \
  -H "X-API-Key: $COMFY_CLOUD_API_KEY" \
  -F "image=@photo.png" -F "type=input" -F "overwrite=true"
```

## Особенности облака

- **Базовый URL:** `https://cloud.comfy.org`
- **Auth:** заголовок `X-API-Key` (или `?token=KEY` для WebSocket).
- **Ключ API:** установите `$COMFY_CLOUD_API_KEY` один раз, и сценарии подберут его автоматически.
- **Выходная загрузка:** `/api/view` возвращает код 302 для подписанного URL-адреса; сценарии
  следуйте за ним и удалите `X-API-Key` перед извлечением из хранилища
  (не передавайте ключ API в S3/CloudFront).
- **Отличия конечной точки от локального ComfyUI:**
  - `/api/object_info`, `/api/queue`, `/api/userdata` — **403 на бесплатном уровне**;
    только платный.
  - `/history` переименован в `/history_v2` в облаке (маршрут скриптов
    автоматически).
  - `/models/<folder>` переименован в `/experiment/models/<folder>` в облаке.
    (скрипты маршрутизируются автоматически).
  - `clientId` в WebSocket в настоящее время игнорируется — все соединения для
    пользователь получает ту же трансляцию. Фильтровать по `prompt_id` на стороне клиента.
  - `subfolder` принимается при загрузке, но игнорируется — облако имеет плоское пространство имен.
- **Одновременные задания:** Бесплатные/Стандартные: 1, Автор: 3, Про: 5. Очередь дополнительных услуг.
  автоматически. Используйте `run_batch.py --parallel N`, чтобы насытить свой уровень.

## Управление очередью и системой

```bash
# Local
curl -s http://127.0.0.1:8188/queue | python3 -m json.tool
curl -X POST http://127.0.0.1:8188/queue -d '{"clear": true}'    # cancel pending
curl -X POST http://127.0.0.1:8188/interrupt                      # cancel running
curl -X POST http://127.0.0.1:8188/free \
  -H "Content-Type: application/json" \
  -d '{"unload_models": true, "free_memory": true}'

# Cloud — same paths under /api/, plus:
python3 scripts/fetch_logs.py --tail-queue --host https://cloud.comfy.org
```

## Подводные камни

1. **Требуется формат API** — ожидается каждый скрипт и конечная точка `/api/prompt`.
   Рабочий процесс в формате API JSON. Скрипты определяют формат редактора (верхнего уровня).
   массивы `nodes` и `links`) и предложите повторно экспортировать через
   «Рабочий процесс → Экспорт (API)» (более новый пользовательский интерфейс) или «Сохранить (формат API)» (более старый пользовательский интерфейс).

2. **Сервер должен быть запущен** — для любого выполнения требуется работающий сервер.
   `comfy launch --background` запускает его. Подтвердите с помощью
   `curl http://127.0.0.1:8188/system_stats`.

3. **Названия моделей указаны точно** — с учетом регистра, включая расширение файла.
   `check_deps.py` выполняет нечеткое сопоставление (с расширением и папкой или без него).
   префикс), но сам рабочий процесс должен использовать каноническое имя. Использование
   `comfy model list`, чтобы узнать, что установлено.

4. **Отсутствуют пользовательские узлы** — «class_type не найден» означает необходимый узел.
   не установлен. `check_deps.py` сообщает, какой пакет установить;
   `auto_fix_deps.py` выполнит установку за вас.

5. **Рабочая директория** — `comfy-cli` автоматически определяет рабочую область ComfyUI.
   Если команды завершаются неудачно с сообщением «рабочая область не найдена», используйте
   `comfy --workspace /path/to/ComfyUI <command>` или
   `comfy set-default /path/to/ComfyUI`.

6. **Ограничения API уровня бесплатного использования в облаке** — `/api/prompt`, `/api/view`, `/api/upload/*`,
   `/api/object_info` все возвращают 403 на бесплатных аккаунтах. `health_check.py` и
   `check_deps.py` отнеситесь к этому изящно и ясно дайте понять.

7. **Тайм-аут для рабочих процессов видео/аудио** — автоматически определяется, когда выходной узел
   это `VHS_VideoCombine`, `SaveVideo` и т. д.; значение по умолчанию увеличивается с 300 с до
   900 с. Явно переопределить с помощью `--timeout 1800`.

8. **Обход пути в именах выходных файлов** — имена файлов, предоставляемые сервером,
   прошел через `safe_path_join`, чтобы запретить все, что выходит за рамки `--output-dir`.
   Сохраняйте эту защиту включенной — рабочие процессы с настраиваемыми узлами сохранения могут создавать
   произвольные пути.

9. **JSON рабочего процесса представляет собой произвольный код** — пользовательские узлы работают на Python, поэтому
   отправка неизвестного рабочего процесса имеет тот же профиль доверия, что и `eval`.
   Проверяйте рабочие процессы из ненадежных источников перед запуском.

10. **Авторандомизированное начальное число** — передайте `seed: -1` в `--args` (или используйте
    `--randomize-seed` и опустите семя), чтобы получить новое семя за один прогон.
    Фактическое начальное значение записывается в stderr.

11. **`tracking` приглашение** — при первом запуске `comfy` может потребоваться аналитика.
    Используйте `comfy --skip-prompt tracking disable` для неинтерактивного пропуска.
    `comfyui_setup.sh` сделает это за вас.

## Контрольный список проверки

Используйте `python3 scripts/health_check.py`, чтобы запустить весь список одновременно. Руководство:

- [ ] `hardware_check.py` вердикт: `ok` ИЛИ пользователь явно выбрал Comfy Cloud
- [ ] `comfy --version` работает (или `uvx --from comfy-cli comfy --help`)
- [ ] `curl http://HOST:PORT/system_stats` возвращает JSON
- [ ] `comfy model list` показывает хотя бы одну контрольную точку (локальную) ИЛИ
      `/api/experiment/models/checkpoints` возвращает модели (облако)
- [ ] Рабочий процесс JSON имеет формат API.
- [ ] `check_deps.py` сообщает `is_ready: true` (или только `node_check_skipped`
      на бесплатном уровне облака)
- [ ] Тестовый запуск небольшого рабочего процесса завершен; выходные данные приземляются в `--output-dir`