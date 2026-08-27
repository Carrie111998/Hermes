---
title: 'Heartmula — HeartMuLa: создание песен в стиле Suno из текстов и тегов'
sidebar_label: Heartmula
description: 'HeartMuLa: создание песен в стиле Suno из текстов и тегов'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Хартмула

HeartMuLa: создание песен в стиле Suno из текстов + тегов.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/creative/heartmula` |
| Путь | `optional-skills/creative/heartmula` |
| Версия | `1.0.0` |
| Автор | Текниум (текниум1), Агент Гермеса |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `music`, `audio`, `generation`, `ai`, `heartmula`, `heartcodec`, `lyrics`, `songs` |
| Сопутствующие навыки | [`audiocraft-audio-generation`](/docs/user-guide/skills/optional/creative/creative-audiocraft-audio-generation), [`songwriting-and-ai-music`](/docs/user-guide/skills/bundled/creative/creative-songwriting-and-ai-music) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# HeartMuLa — создание музыки с открытым исходным кодом

## Обзор
HeartMuLa — это семейство музыкальных базовых моделей с открытым исходным кодом (Apache-2.0), которые генерируют музыку на основе текстов и тегов с многоязычной поддержкой. Генерирует полные песни из текстов + тегов. Сравним с Suno с открытым исходным кодом. Включает:
- **HeartMuLa** - Модель музыкального языка (3B/7B) для генерации из текстов песен + тегов.
- **HeartCodec** — музыкальный кодек 12,5 Гц для высококачественной реконструкции звука.
- **HeartTranscriptor** — транскрипция текстов песен на основе шепота.
- **HeartCLAP** — модель выравнивания аудио-текста.

## Когда использовать
- Пользователь хочет создавать музыку/песни из текстовых описаний.
- Пользователь хочет альтернативу Suno с открытым исходным кодом.
- Пользователь хочет создавать локальную/оффлайн музыку.
- Пользователь спрашивает о HeartMuLa, heartlib или генерации музыки с помощью искусственного интеллекта.

## Требования к оборудованию
- **Минимум**: 8 ГБ видеопамяти с `--lazy_load true` (последовательная загрузка/выгрузка моделей)
- **Рекомендуется**: более 16 ГБ видеопамяти для комфортного использования одного графического процессора.
- **Мульти-GPU**: используйте `--mula_device cuda:0 --codec_device cuda:1` для разделения между графическими процессорами.
- Модель 3B с пиковой нагрузкой lazy_load ~6,2 ГБ видеопамяти

## Этапы установки

### 1. Репозиторий клонов
```bash
cd ~/  # or desired directory
git clone https://github.com/HeartMuLa/heartlib.git
cd heartlib
```

### 2. Создайте виртуальную среду (требуется Python 3.10)
```bash
uv venv --python 3.10 .venv
. .venv/bin/activate
uv pip install -e .
```

### 3. Исправление проблем совместимости зависимостей

**ВАЖНО**. По состоянию на февраль 2026 г. закрепленные зависимости конфликтуют с более новыми пакетами. Примените эти исправления:

```bash
# Upgrade datasets (old version incompatible with current pyarrow)
uv pip install --upgrade datasets

# Upgrade transformers (needed for huggingface-hub 1.x compatibility)
uv pip install --upgrade transformers
```

### 4. Исходный код патча (требуется для трансформеров 5.x)

**Патч 1 — исправление кэша RoPE** в `src/heartlib/heartmula/modeling_heartmula.py`:

В методе `setup_caches` класса `HeartMuLa` добавьте повторную инициализацию RoPE после блока try/Exception `reset_caches` и перед блоком `with device:`:

```python
# Re-initialize RoPE caches that were skipped during meta-device loading
from torchtune.models.llama3_1._position_embeddings import Llama3ScaledRoPE
for module in self.modules():
    if isinstance(module, Llama3ScaledRoPE) and not module.is_cache_built:
        module.rope_init()
        module.to(device)
```

**Почему**: `from_pretrained` сначала создает модель на мета-устройстве; `Llama3ScaledRoPE.rope_init()` пропускает построение кеша на метатензорах, а затем никогда не перестраивает его после загрузки весов на реальное устройство.

**Патч 2 — исправление загрузки HeartCodec** в `src/heartlib/pipelines/music_generation.py`:

Добавьте `ignore_mismatched_sizes=True` ко ВСЕМ вызовам `HeartCodec.from_pretrained()` (их 2: нетерпеливая загрузка в `__init__` и отложенная загрузка в свойстве `codec`).

**Почему**: буферы кодовой книги VQ `initted` имеют форму `[1]` в контрольной точке и `[]` в модели. Те же данные, только скаляр против 0-мерного тензора. Безопасно игнорировать.

### 5. Загрузите контрольные точки модели
```bash
cd heartlib  # project root
hf download --local-dir './ckpt' 'HeartMuLa/HeartMuLaGen'
hf download --local-dir './ckpt/HeartMuLa-oss-3B' 'HeartMuLa/HeartMuLa-oss-3B-happy-new-year'
hf download --local-dir './ckpt/HeartCodec-oss' 'HeartMuLa/HeartCodec-oss-20260123'
```

Все 3 можно загружать параллельно. Общий размер составляет несколько ГБ.

## GPU/CUDA

HeartMuLa по умолчанию использует CUDA (`--mula_device cuda --codec_device cuda`). Никакой дополнительной настройки не требуется, если у пользователя установлен графический процессор NVIDIA с поддержкой PyTorch CUDA.

- Установленный `torch==2.4.1` включает поддержку CUDA 12.1 из коробки.
- `torchtune` может сообщить о версии `0.4.0+cpu` — это всего лишь метаданные пакета, он по-прежнему использует CUDA через PyTorch.
- Чтобы убедиться, что используется графический процессор, найдите в выводе строки «Память CUDA» (например, «Память CUDA перед выгрузкой: 6,20 ГБ»).
- **Нет графического процессора?** Вы можете работать на процессоре с помощью `--mula_device cpu --codec_device cpu`, но ожидайте, что генерация будет **чрезвычайно медленной** (потенциально 30-60+ минут для одной песни против ~4 минут на графическом процессоре). Режим ЦП также требует значительного объема оперативной памяти (около 12 ГБ+ бесплатно). Если у пользователя нет графического процессора NVIDIA, рекомендуется вместо этого использовать облачный сервис графических процессоров (уровень бесплатного пользования Google Colab с T4, Lambda Labs и т. д.) или онлайн-демонстрацию по адресу https://heartmula.github.io/.

## Использование

### Базовое поколение
```bash
cd heartlib
. .venv/bin/activate
python ./examples/run_music_generation.py \
  --model_path=./ckpt \
  --version="3B" \
  --lyrics="./assets/lyrics.txt" \
  --tags="./assets/tags.txt" \
  --save_path="./assets/output.mp3" \
  --lazy_load true
```

### Форматирование ввода

**Теги** (через запятую, без пробелов):
```
piano,happy,wedding,synthesizer,romantic
```
или
```
rock,energetic,guitar,drums,male-vocal
```

**Текст** (используйте структурные теги в квадратных скобках):
```
[Intro]

[Verse]
Your lyrics here...

[Chorus]
Chorus lyrics...

[Bridge]
Bridge lyrics...

[Outro]
```

### Ключевые параметры
| Параметр | По умолчанию | Описание |
|-----------|---------|-------------|
| `--max_audio_length_ms` | 240000 | Максимальная продолжительность в мс (240 с = 4 мин) |
| `--topk` | 50 | Топ-к выборки |
| `--temperature` | 1.0 | Температура отбора проб |
| `--cfg_scale` | 1,5 | Шкала без классификатора |
| `--lazy_load` | ложный | Загрузка/выгрузка моделей по требованию (экономит VRAM) |
| `--mula_dtype` | bfloat16 | Dtype для HeartMuLa (рекомендуется bf16) |
| `--codec_dtype` | поплавок32 | Dtype для HeartCodec (для качества рекомендуется fp32) |

### Производительность
- RTF (коэффициент реального времени) ≈ 1,0 — для создания 4-минутной песни требуется ~4 минуты.
- Выход: MP3, стерео 48 кГц, 128 кбит/с.

## Подводные камни
1. **НЕ используйте bf16 для HeartCodec** — ухудшается качество звука. Используйте fp32 (по умолчанию).
2. **Теги могут игнорироваться** — известная проблема (#90). Тексты имеют тенденцию доминировать; поэкспериментируйте с порядком тегов.
3. **Triton недоступен в macOS** — Linux/CUDA только для ускорения графического процессора.
4. **Несовместимость RTX 5080** сообщается в разделах восходящего потока.
5. Конфликты контактов зависимостей требуют ручных обновлений и исправлений, описанных выше.

## Ссылки
- Репо: https://github.com/HeartMuLa/heartlib.
- Модели: https://huggingface.co/HeartMuLa
- Статья: https://arxiv.org/abs/2601.10547.
- Лицензия: Апач-2.0