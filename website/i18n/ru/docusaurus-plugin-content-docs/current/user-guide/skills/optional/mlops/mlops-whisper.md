---
title: Whisper — Транскрибируйте и переводите речь на 99 языках.
sidebar_label: Whisper
description: Транскрибируйте и переводите речь на 99 языках
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Шепот

Транскрибируйте и переводите речь на 99 языках.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/whisper` |
| Путь | `optional-skills/mlops/whisper` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `openai-whisper`, `transformers`, `torch` |
| Платформы | Linux, MacOS |
| Теги | `Whisper`, `Speech Recognition`, `ASR`, `Multimodal`, `Multilingual`, `OpenAI`, `Speech-To-Text`, `Transcription`, `Translation`, `Audio Processing` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Шепот — надежное распознавание речи

Многоязычная модель распознавания речи OpenAI.

## Когда использовать Шепот

**Используйте, когда:**
- Транскрипция речи в текст (99 языков)
- Транскрипция подкаста/видео
- Автоматизация заметок о встречах
- Перевод на английский
- Шумная аудиотранскрипция
- Многоязычная обработка звука

**Показатели**:
- **72 900+ звезд GitHub**
- Поддерживается 99 языков
- Обучение на 680 000 часов аудио
- Лицензия Массачусетского технологического института

**Вместо этого используйте альтернативы**:
- **AssemblyAI**: управляемый API, дневник спикеров.
- **Deepgram**: потоковая передача ASR в реальном времени.
- **Преобразование речи в текст Google**: на базе облака.

## Быстрый старт

### Установка

```bash
# Requires Python 3.8-3.11
pip install -U openai-whisper

# Requires ffmpeg
# macOS: brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg
# Windows: choco install ffmpeg
```

### Базовая транскрипция

```python
import whisper

# Load model
model = whisper.load_model("base")

# Transcribe
result = model.transcribe("audio.mp3")

# Print text
print(result["text"])

# Access segments
for segment in result["segments"]:
    print(f"[{segment['start']:.2f}s - {segment['end']:.2f}s] {segment['text']}")
```

## Размеры модели

```python
# Available models
models = ["tiny", "base", "small", "medium", "large", "turbo"]

# Load specific model
model = whisper.load_model("turbo")  # Fastest, good quality
```

| Модель | Параметры | только на английском языке | Многоязычный | Скорость | видеопамять |
|-------|------------|--------------|--------------|-------|------|
| крошечный | 39М | ✓ | ✓ | ~32x | ~1 ГБ |
| база | 74М | ✓ | ✓ | ~16x | ~1 ГБ |
| маленький | 244М | ✓ | ✓ | ~6x | ~2 ГБ |
| средний | 769М | ✓ | ✓ | ~2x | ~5 ГБ |
| большой | 1550М | ✗ | ✓ | 1x | ~10 ГБ |
| турбо | 809М | ✗ | ✓ | ~8x | ~6 ГБ |

**Рекомендация**: используйте `turbo` для лучшей скорости и качества, `base` для прототипирования.

## Варианты транскрипции

### Спецификация языка

```python
# Auto-detect language
result = model.transcribe("audio.mp3")

# Specify language (faster)
result = model.transcribe("audio.mp3", language="en")

# Supported: en, es, fr, de, it, pt, ru, ja, ko, zh, and 89 more
```

### Выбор задачи

```python
# Transcription (default)
result = model.transcribe("audio.mp3", task="transcribe")

# Translation to English
result = model.transcribe("spanish.mp3", task="translate")
# Input: Spanish audio → Output: English text
```

### Начальное приглашение

```python
# Improve accuracy with context
result = model.transcribe(
    "audio.mp3",
    initial_prompt="This is a technical podcast about machine learning and AI."
)

# Helps with:
# - Technical terms
# - Proper nouns
# - Domain-specific vocabulary
```

### Временные метки

```python
# Word-level timestamps
result = model.transcribe("audio.mp3", word_timestamps=True)

for segment in result["segments"]:
    for word in segment["words"]:
        print(f"{word['word']} ({word['start']:.2f}s - {word['end']:.2f}s)")
```

### Резервная температура

```python
# Retry with different temperatures if confidence low
result = model.transcribe(
    "audio.mp3",
    temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
)
```

## Использование командной строки

```bash
# Basic transcription
whisper audio.mp3

# Specify model
whisper audio.mp3 --model turbo

# Output formats
whisper audio.mp3 --output_format txt     # Plain text
whisper audio.mp3 --output_format srt     # Subtitles
whisper audio.mp3 --output_format vtt     # WebVTT
whisper audio.mp3 --output_format json    # JSON with timestamps

# Language
whisper audio.mp3 --language Spanish

# Translation
whisper spanish.mp3 --task translate
```

## Пакетная обработка

```python
import os

audio_files = ["file1.mp3", "file2.mp3", "file3.mp3"]

for audio_file in audio_files:
    print(f"Transcribing {audio_file}...")
    result = model.transcribe(audio_file)

    # Save to file
    output_file = audio_file.replace(".mp3", ".txt")
    with open(output_file, "w") as f:
        f.write(result["text"])
```

## Транскрипция в реальном времени

```python
# For streaming audio, use faster-whisper
# pip install faster-whisper

from faster_whisper import WhisperModel

model = WhisperModel("base", device="cuda", compute_type="float16")

# Transcribe with streaming
segments, info = model.transcribe("audio.mp3", beam_size=5)

for segment in segments:
    print(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")
```

## ускорение графического процессора

```python
import whisper

# Automatically uses GPU if available
model = whisper.load_model("turbo")

# Force CPU
model = whisper.load_model("turbo", device="cpu")

# Force GPU
model = whisper.load_model("turbo", device="cuda")

# 10-20× faster on GPU
```

## Интеграция с другими инструментами

### Генерация субтитров

```bash
# Generate SRT subtitles
whisper video.mp4 --output_format srt --language English

# Output: video.srt
```

### С Лангчейном

```python
from langchain.document_loaders import WhisperTranscriptionLoader

loader = WhisperTranscriptionLoader(file_path="audio.mp3")
docs = loader.load()

# Use transcription in RAG
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

vectorstore = Chroma.from_documents(docs, OpenAIEmbeddings())
```

### Извлечение аудио из видео

```bash
# Use ffmpeg to extract audio
ffmpeg -i video.mp4 -vn -acodec pcm_s16le audio.wav

# Then transcribe
whisper audio.wav
```

## Лучшие практики

1. **Использовать турбо-модель** – лучшая скорость/качество для английского языка.
2. **Укажите язык** – быстрее, чем автоматическое определение.
3. **Добавить начальное приглашение** – улучшены технические условия.
4. **Используйте графический процессор** — в 10–20 раз быстрее.
5. **Пакетный процесс** – более эффективный.
6. **Конвертировать в WAV** – улучшенная совместимость.
7. **Разделение длинного аудио** – фрагменты по 30 минут.
8. **Проверьте языковую поддержку**. Качество зависит от языка.
9. **Используйте более быстрый шепот** — в 4 раза быстрее, чем опенай-шепот.
10. **Мониторинг VRAM** – масштабирование размера модели в соответствии с аппаратным обеспечением.

## Производительность

| Модель | Фактор реального времени (ЦП) | Фактор реального времени (GPU) |
|-------|------------------------|------------------------|
| крошечный | ~0,32 | ~0,01 |
| база | ~0,16 | ~0,01 |
| турбо | ~0,08 | ~0,01 |
| большой | ~1,0 | ~0,05 |

*Коэффициент реального времени: 0,1 = в 10 раз быстрее, чем в реальном времени*

## Языковая поддержка

Наиболее поддерживаемые языки:
- английский (англ.)
- Испанский (и)
- Французский (фр.)
- немецкий (де)
- Итальянский (оно)
- Португальский (пт)
- Русский (ru)
- Японский (джа)
- Корейский (ко)
- Китайский (ж)

Полный список: всего 99 языков

## Ограничения

1. **Галлюцинации**. Может повторять или придумывать текст.
2. **Точность длинных форм** – снижается при прослушивании >30 минут.
3. **Идентификация говорящего** – Без диаризации
4. **Акценты** – качество варьируется.
5. **Фоновый шум** – может повлиять на точность.
6. **Задержка в реальном времени** – не подходит для прямых субтитров.

## Ресурсы

- **GitHub**: https://github.com/openai/whisper ⭐ 72 900+
- **Бумага**: https://arxiv.org/abs/2212.04356.
- **Карточка модели**: https://github.com/openai/whisper/blob/main/model-card.md
- **Colab**: доступно в репозитории.
- **Лицензия**: MIT