---
sidebar_position: 9
title: Голос и TTS
description: Преобразование текста в речь и голосовые сообщения на всех платформах
---

# Голос и TTS

Hermes Agent поддерживает как преобразование текста в речь, так и транскрипцию голосовых сообщений на всех платформах обмена сообщениями.

:::tip Подписчики
Если у вас есть платная подписка на [Nous Portal](https://portal.nousresearch.com), OpenAI TTS доступен через **[Tool Gateway](tool-gateway.md)** без отдельного ключа OpenAI API. Новые установки могут запускать `hermes setup --portal` для входа в систему и одновременного включения всех инструментов шлюза; существующие установки могут выбрать **Подписку Nous** только для TTS через `hermes model` или `hermes tools`.
:::

## Преобразование текста в речь

Преобразование текста в речь с помощью одиннадцати поставщиков:

| Провайдер | Качество | Стоимость | API-ключ |
|----------|---------|------|---------|
| **Пограничный TTS** (по умолчанию) | Хорошо | Бесплатно | Ничего не нужно |
| **ЭлевенЛабс** | Отлично | Платный | `ELEVENLABS_API_KEY` |
| **OpenAI TTS** | Хорошо | Платный | `VOICE_TOOLS_OPENAI_KEY` |
| **МиниМакс ТТС** | Отлично | Платный | `MINIMAX_API_KEY` или `MINIMAX_CN_API_KEY` |
| **Мистраль (Вокстрал ТТС)** | Отлично | Платный | `MISTRAL_API_KEY` |
| **Google Gemini TTS** | Отлично | Уровень бесплатного пользования | `GEMINI_API_KEY` |
| **xAI TTS** | Отлично | Платный | `XAI_API_KEY` |
| **DeepInfra TTS** | Хорошо | Платный | `DEEPINFRA_API_KEY` |
| **НейТТС** | Хорошо | Бесплатно (локально) | Ничего не нужно |
| **КотенокTTS** | Хорошо | Бесплатно (локально) | Ничего не нужно |
| **Пайпер** | Хорошо | Бесплатно (локально) | Ничего не нужно |

### Доставка платформы

| Платформа | Доставка | Формат |
|----------|----------|--------|
| Телеграмма | Голосовой пузырь (играет в режиме онлайн) | Опус `.ogg` |
| Раздор | Голосовой пузырь (Opus/OGG), возвращается к вложенному файлу | Опус/MP3 |
| WhatsApp | Вложенный аудиофайл | MP3 |
| интерфейс командной строки | Сохранено в `~/.hermes/audio_cache/` | MP3 |

### Конфигурация

```yaml
# In ~/.hermes/config.yaml
tts:
  provider: "edge"              # "edge" | "elevenlabs" | "openai" | "minimax" | "mistral" | "gemini" | "xai" | "deepinfra" | "neutts" | "kittentts" | "piper" — or "nous" for the managed Tool Gateway (written when you pick Nous Subscription in `hermes tools`)
  speed: 1.0                    # Global speed multiplier (provider-specific settings override this)
  edge:
    voice: "en-US-AriaNeural"   # 322 voices, 74 languages
    speed: 1.0                  # Converted to rate percentage (+/-%)
  elevenlabs:
    voice_id: "pNInz6obpgDQGcFmaJgB"  # Adam
    model_id: "eleven_multilingual_v2"
  openai:
    model: "gpt-4o-mini-tts"
    voice: "alloy"              # alloy, echo, fable, onyx, nova, shimmer
    base_url: "https://api.openai.com/v1"  # Override for OpenAI-compatible TTS endpoints
    speed: 1.0                  # 0.25 - 4.0
    # language: "es"            # Sent as lang_code — only for OpenAI-compatible endpoints that support it (e.g. Kokoro)
  minimax:
    region: "global"           # "global" or "cn"; see selection rules below
    model: "speech-02-hd"     # speech-02-hd (default), speech-02-turbo
    voice_id: "English_expressive_narrator"  # See https://platform.minimax.io/faq/system-voice-id
    speed: 1                    # 0.5 - 2.0
    vol: 1                      # 0 - 10
    pitch: 0                    # -12 - 12
    # base_url: "https://tts.example/v1/t2a_v2"  # Optional endpoint override for the selected region
  mistral:
    model: "voxtral-mini-tts-2603"
    voice_id: "c69964a6-ab8b-4f8a-9465-ec0925096ec8"  # Paul - Neutral (default)
  gemini:
    model: "gemini-2.5-flash-preview-tts"  # or gemini-3.1-flash-tts-preview
    voice: "Kore"               # 30 prebuilt voices: Zephyr, Puck, Kore, Enceladus, Gacrux, etc.
    audio_tags: false           # Enable hidden Gemini 3.1 TTS audio-tag insertion
    persona_prompt_file: ""      # Optional Markdown/text file with Gemini voice direction
  xai:
    voice_id: "eve"             # or a custom voice ID — see docs below
    language: "en"              # BCP-47 code (e.g. "en", "pt-BR") or "auto" for detection
    speed: 1.0                  # 0.7–1.5, playback speed (default: 1.0)
    auto_speech_tags: false     # insert expressive audio tags via LLM rewrite
    text_normalization: false   # normalize numbers/abbreviations/symbols to spoken form
    optimize_streaming_latency: 0  # 0–2, trades quality for lower latency (default: 0)
    sample_rate: 24000          # 22050 / 24000 (default) / 44100 / 48000
    bit_rate: 128000            # MP3 bitrate; only applies when codec=mp3
    # base_url: "https://api.x.ai/v1"   # Override via XAI_BASE_URL env var
  neutts:
    ref_audio: ''
    ref_text: ''
    model: neuphonic/neutts-air-q4-gguf
    device: cpu
  kittentts:
    model: KittenML/kitten-tts-nano-0.8-int8   # 25MB int8; also: kitten-tts-micro-0.8 (41MB), kitten-tts-mini-0.8 (80MB)
    voice: Jasper                               # Jasper, Bella, Luna, Bruno, Rosie, Hugo, Kiki, Leo
    speed: 1.0                                  # 0.5 - 2.0
    clean_text: true                            # Expand numbers, currencies, units
  piper:
    voice: en_US-lessac-medium                  # voice name (auto-downloaded) OR absolute path to .onnx
    # voices_dir: ''                            # default: ~/.hermes/cache/piper-voices/
    # use_cuda: false                           # requires onnxruntime-gpu
    # length_scale: 1.0                         # 2.0 = twice as slow
    # noise_scale: 0.667
    # noise_w_scale: 0.8
    # volume: 1.0                               # 0.5 = half as loud
    # normalize_audio: true
```

MiniMax TTS выбирает регион, конечную точку и учетные данные вместе:

- `region: "global"` использует `https://api.minimax.io/v1/t2a_v2` с `MINIMAX_API_KEY`.
- `region: "cn"` использует `https://api.minimaxi.com/v1/t2a_v2` с `MINIMAX_CN_API_KEY`.
- Если `region` опущен, `MINIMAX_API_KEY` сохраняет приоритет для обеспечения обратной совместимости. Если настроен только `MINIMAX_CN_API_KEY`, Hermes выбирает `cn`.
- Явно выбранный регион должен иметь соответствующие учетные данные. Гермес никогда не заимствует ключ другого региона. Переопределение `base_url` не меняет выбранные учетные данные, а переопределение, указывающее на официальную конечную точку другого региона, отклоняется.

**Контроль скорости**. Глобальное значение `tts.speed` по умолчанию применяется ко всем поставщикам. Каждый поставщик может переопределить его с помощью собственного параметра `speed` (например, `tts.openai.speed: 1.5`). Скорость, зависящая от поставщика, имеет приоритет над глобальным значением. По умолчанию — `1.0` (нормальная скорость).

### Подсказки для персонажа Близнецы

Gemini TTS может следовать направлению исполнения на естественном языке. Задайте `tts.gemini.persona_prompt_file` локальный Markdown или текстовый файл, описывающий голосовой персонаж. Файл может включать разделы в стиле Gemini, такие как `AUDIO PROFILE`, `SCENE`, `DIRECTOR'S NOTES`, `SAMPLE CONTEXT` и `TRANSCRIPT`.

Если файл содержит `{transcript}` или `{{ transcript }}`, Hermes заменяет этот заполнитель живым текстом TTS. В противном случае Hermes автоматически добавит раздел с меткой `TRANSCRIPT`. Персональное приглашение остается локальным и не отображается в ответе чата.

```yaml
tts:
  provider: gemini
  gemini:
    voice: Algieba
    persona_prompt_file: ~/.hermes/tts/butler-voice.md
```

### Аудио теги (Gemini, xAI)

Gemini 3.1 Flash TTS от Google и Grok TTS от xAI поддерживают аудиотеги произвольной формы в квадратных скобках, такие как `[whispers]`, `[excitedly]`, `[very slow]`, `[laughs]` и другие выразительные примечания к доставке. Включите `tts.gemini.audio_tags` или `tts.xai.auto_speech_tags`, чтобы Hermes выполнял скрытый проход перезаписи перед TTS. При перезаписи встроенные теги вставляются только в сценарий TTS; видимый ответ чата остается неизменным.

```yaml
tts:
  provider: gemini
  gemini:
    model: gemini-3.1-flash-tts-preview
    audio_tags: true
  xai: 
    auto_speech_tags: true
```

При перезаписи используется `auxiliary.tts_audio_tags` и по умолчанию используется ваша основная модель чата. Отмените эту вспомогательную задачу, если хотите, чтобы вставка тегов выполнялась более дешевой или быстрой моделью.

**Язык (конечные точки, совместимые с OpenAI)**: `tts.openai.language` пересылается в конечную точку как параметр запроса `lang_code`. Он предназначен для OpenAI-совместимых TTS-серверов, поддерживающих `lang_code` — например, [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI), где `language: "es"` выбирает испанский фонемайзер вместо английского по умолчанию. Оставьте его отключенным при использовании официального API OpenAI, который не принимает этот параметр. Если параметр не установлен, ничего лишнего не отправляется.


### Ограничения длины ввода

У каждого провайдера есть документированное ограничение на количество входных символов для каждого запроса. Hermes перед вызовом провайдера разбивает более длинные ответы на упорядоченные фрагменты с учетом предложений, поэтому полный нормализованный текст сохраняется, а не усекается автоматически:

| Провайдер | Размер по умолчанию (символы) |
|----------|---------------------|
| Край TTS | 5000 |
| ОпенАИ | 4096 |
| хАИ | 15000 |
| МиниМакс | 10000 |
| Мистраль | 4000 |
| Google Близнецы | 32000 |
| ОдиннадцатьЛабс | С учетом моделей (см. ниже) |
| НойТТС | 2000 |
| КотенокТТС | 2000 |
| Пайпер | 5000 |

**ElevenLabs** выбирает ограничение из настроенного `model_id`:

| `model_id` | Кепка (символы) |
|------------|-------------|
| `eleven_flash_v2_5` | 40000 |
| `eleven_flash_v2` | 30000 |
| `eleven_multilingual_v2` (по умолчанию), `eleven_multilingual_v1`, `eleven_english_sts_v2`, `eleven_english_sts_v1` | 10000 |
| `eleven_v3`, `eleven_ttv_v3` | 5000 |
| Неизвестная модель | Возвращается к настройкам поставщика по умолчанию (10000) |

**Переопределить каждого поставщика** с помощью `max_text_length:` в разделе поставщика вашей конфигурации TTS:

```yaml
tts:
  openai:
    max_text_length: 8192   # raise or lower the provider cap
```

Учитываются только положительные целые числа. Нулевые, отрицательные, нечисловые или логические значения попадают в значение по умолчанию поставщика, поэтому сломанная конфигурация не может случайно обойти ограничение запросов поставщика.

### Голосовые пузыри Telegram и ffmpeg

Для голосовых сообщений Telegram требуется аудиоформат Opus/OGG:

- **OpenAI, ElevenLabs и Mistral** создают Opus изначально — без дополнительной настройки.
- **Edge TTS** (по умолчанию) выводит MP3, и для преобразования требуется **ffmpeg**:
- **MiniMax TTS** выводит MP3 и требует **ffmpeg** для преобразования в голосовые сообщения Telegram.
- **Google Gemini TTS** выводит необработанный PCM и использует **ffmpeg** для кодирования Opus напрямую для голосовых пузырей Telegram.
- **xAI TTS** выводит MP3 и требует **ffmpeg** для преобразования в голосовые пузырьки Telegram.
- **NeuTTS** выводит WAV, а также требует **ffmpeg** для преобразования в голосовые пузырьки Telegram.
- **KittenTTS** выводит WAV, а также требует **ffmpeg** для преобразования в голосовые сообщения Telegram.
- **Piper** выводит WAV, а также требует **ffmpeg** для преобразования в голосовые сообщения Telegram.

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Fedora
sudo dnf install ffmpeg
```

Без ffmpeg аудио Edge TTS, MiniMax TTS, NeuTTS, KittenTTS и Piper отправляются как обычные аудиофайлы (воспроизводимые, но отображаются в виде прямоугольного проигрывателя вместо голосового пузыря).

:::совет
Если вам нужны голосовые пузыри без установки ffmpeg, переключитесь на поставщика OpenAI, ElevenLabs или Mistral.
:::

### xAI Custom Voices (клонирование голоса)

xAI поддерживает клонирование вашего голоса и использование его с TTS. Создайте собственный голос в [консоли xAI](https://console.x.ai/team/default/voice/voice-library), затем установите полученный `voice_id` в свою конфигурацию:

```yaml
tts:
  provider: xai
  xai:
    voice_id: "nlbqfwie"   # your custom voice ID
```

Подробную информацию о записи, поддерживаемых форматах и ограничениях см. в [документации по xAI Custom Voices](https://docs.x.ai/developers/model-capabilities/audio/custom-voices).

### Piper (местный, 44 языка)

Piper — это быстрый локальный нейронный TTS-движок от Open Home Foundation (сопровождающие Home Assistant). Он полностью работает на процессоре, поддерживает **44 языка** с заранее обученными голосами и не требует ключа API.

**Установить через `hermes tools`** → Голос и TTS → Piper — Hermes запускает для вас `pip install piper-tts`. Или установите вручную: `pip install piper-tts`.

**Переключиться на Пайпер:**

```yaml
tts:
  provider: piper
  piper:
    voice: en_US-lessac-medium
```

При первом вызове TTS для голоса, который не кэшируется локально, Hermes запускает `python -m piper.download_voices <name>` и загружает модель (~20–90 МБ в зависимости от уровня качества) в `~/.hermes/cache/piper-voices/`. Последующие вызовы повторно используют кэшированную модель.

**Выбор голоса.** [Полный каталог голосов](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md) охватывает английский, испанский, французский, немецкий, итальянский, голландский, португальский, русский, польский, турецкий, китайский, арабский, хинди и другие языки — каждый с `x_low` / `low` / `medium` / `high` уровней качества. Примеры голосов можно найти на [rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/).

**Использование предварительно загруженного голоса.** Задайте для `tts.piper.voice` абсолютный путь, заканчивающийся на `.onnx`:

```yaml
tts:
  piper:
    voice: /path/to/my-custom-voice.onnx
```

**Расширенные ручки** (`tts.piper.length_scale` / `noise_scale` / `noise_w_scale` / `volume` / `normalize_audio`, `use_cuda`) соответствуют `SynthesisConfig` Piper в соотношении 1:1. Они игнорируются в более старых версиях `piper-tts`.

### Поставщики пользовательских команд

Если нужный вам движок TTS не поддерживается изначально (VoxCPM, MLX-Kokoro, XTTS CLI, сценарий клонирования голоса, что-либо еще, предоставляющее CLI), вы можете подключить его как **провайдер командного типа** без написания какого-либо Python. Hermes записывает входной текст во временный файл UTF-8, запускает команду оболочки и читает аудиофайл, созданный этой командой.

Объявите одного или нескольких поставщиков в `tts.providers.<name>` и переключайтесь между ними с помощью `tts.provider: <name>` — так же, как вы переключаетесь между встроенными модулями, такими как `edge` и `openai`.

```yaml
tts:
  provider: voxcpm                 # pick any name under tts.providers
  providers:
    voxcpm:
      type: command
      command: "voxcpm --ref ~/voice.wav --text-file {input_path} --out {output_path}"
      output_format: mp3
      timeout: 180
      voice_compatible: true       # try to deliver as a Telegram voice bubble

    mlx-kokoro:
      type: command
      command: "python -m mlx_kokoro --in {input_path} --out {output_path} --voice {voice}"
      voice: af_sky
      output_format: wav

    piper-custom:                  # native Piper also supports custom .onnx via tts.piper.voice
      type: command
      command: "piper -m /path/to/custom.onnx -f {output_path} < {input_path}"
      output_format: wav
```

**Поддерживаемые значения `output_format`:** `mp3` (по умолчанию), `wav`, `ogg`, `flac`, `m4a`, `aac`, `amr`, `opus`. Ваша команда должна фактически создать этот формат (например, через `ffmpeg`); Hermes только проверяет заявленное значение и соответствующим образом называет выходной файл. Неизвестное значение возвращается к `mp3`. Выбранный формат также отображается в команде как заполнитель `{format}`.

**Среда подпроцесса**: поставщики команд (TTS и STT) работают с удаленными из дочерней среды секретами Hermes — токены шлюзового бота, ключи API поставщика LLM и учетные данные внутренней ретрансляции удаляются; `PATH`, `HOME`, локаль и другие обычные переменные сохраняются. Если вашему шаблону команды нужен собственный ключ API из среды (например, однострочный `curl`), перечислите имена переменных в разделе `env_passthrough` в конфигурации провайдера:

```yaml
tts:
  providers:
    mycloud:
      type: command
      command: 'curl -s -H "Authorization: Bearer $MYCLOUD_API_KEY" ... -o {output_path}'
      env_passthrough: [MYCLOUD_API_KEY]
```


#### Пример: Дубао (китайское семя-tts-2.0)

Для высококачественного китайского TTS через API двунаправленной потоковой передачи ByteDance [seed-tts-2.0](https://www.volcengine.com/docs/6561/1257544) установите пакет PyPI [`doubao-speech`](https://pypi.org/project/doubao-speech/) и подключите его в качестве поставщика команд:

```bash
pip install doubao-speech
export VOLCENGINE_APP_ID="your-app-id"
export VOLCENGINE_ACCESS_TOKEN="your-access-token"
```

```yaml
tts:
  provider: doubao
  providers:
    doubao:
      type: command
      command: "doubao-speech say --text-file {input_path} --out {output_path}"
      output_format: mp3
      max_text_length: 1024
      timeout: 30
```

Учетные данные берутся из вашей среды оболочки (`VOLCENGINE_APP_ID` / `VOLCENGINE_ACCESS_TOKEN`) или `~/.doubao-speech/config.yaml`. Выберите голос, добавив к команде `--voice zh-female-warm` (или любой другой псевдоним из `doubao-speech list-voices`). `doubao-speech` также включает в себя потоковую передачу ASR — см. [раздел STT ниже](#example-doubao--volcengine-asr) для интеграции с Hermes. Исходный код и полная документация: [github.com/Hypnus-Yuan/doubao-speech](https://github.com/Hypnus-Yuan/doubao-speech).

#### Заполнители

Ваш шаблон команды может ссылаться на эти заполнители. Hermes заменяет их во время рендеринга и заключает каждое значение в кавычки для окружающего контекста (голые/одинарные/двойные кавычки), поэтому пути с пробелами и другими символами, чувствительными к оболочке, безопасны.

| Заполнитель | Значение |
|------------------|------------------------------------------------------|
| `{input_path}` | Путь к временному текстовому файлу UTF-8 Гермес написал |
| `{text_path}` | Псевдоним `{input_path}` |
| `{output_path}` | Путь, куда команда должна записать аудио |
| `{format}` | `mp3` / `wav` / `ogg` / `flac` |
| `{voice}` | `tts.providers.<name>.voice`, пусто, если не установлено |
| `{model}` | `tts.providers.<name>.model` |
| `{speed}` | Разрешенный множитель скорости (поставщик или глобальный) |

Используйте `{{` и `}}` для буквальных фигурных скобок.

#### Дополнительные клавиши

| Ключ | По умолчанию | Значение |
|----|---------|------------------------------------------------------------------------------------------------------------|
| `timeout` | `120` | Секунды простоя; Вывод stdout или stderr сбрасывает крайний срок. Дерево процессов уничтожается после бездействия (Unix `killpg`, Windows `taskkill /T`). |
| `output_format` | `mp3` | Один из `mp3` / `wav` / `ogg` / `flac`. Автоматически выводится из выходного расширения, если Гермес выбирает путь.      |
| `voice_compatible` | `false` | Когда `true`, Hermes преобразует выходные данные MP3/WAV в Opus/OGG через ffmpeg, поэтому Telegram отображает голосовой пузырь.      |
| `max_text_length` | `5000` | Максимальное количество входных символов на вызов команды; более длинный текст разбивается на упорядоченные фрагменты.                  |
| `voice` / `model` | пустой | Передаются команде только как значения-заполнители.                                                           |

#### Замечания по поведению

- **Встроенные имена всегда выигрывают.** Запись `tts.providers.openai` никогда не затеняет собственного провайдера OpenAI, поэтому никакая пользовательская конфигурация не может незаметно заменить встроенную.
- **По умолчанию доставка осуществляется в виде документа.** Поставщики команд доставляются в виде обычных аудиовложений на каждой платформе. Включите доставку голосовых сообщений для каждого поставщика с помощью `voice_compatible: true`.
- **Ошибки выполнения команд становятся известны агенту.** Ненулевой выход, пустой вывод или тайм-аут возвращают ошибку с включенным потоком stderr/stdout команды, поэтому вы можете отладить поставщика из диалога.
- **`type: command` используется по умолчанию, если установлен `command:`.** Явное написание `type: command` является хорошей практикой, но не является обязательным; запись с непустой строкой `command` рассматривается как поставщик команд.
- **`{input_path}` / `{text_path}` взаимозаменяемы.** Используйте тот, который лучше читается в вашей команде.

#### Безопасность

Поставщики командного типа запускают любую настроенную вами команду оболочки с разрешениями вашего пользователя. Hermes цитирует значения-заполнители и применяет настроенный тайм-аут, но сам шаблон команды является доверенным локальным вводом — относитесь к нему так же, как к сценарию оболочки в вашем PATH.

### Поставщики плагинов Python

Для механизмов TTS, которые не могут быть выражены в виде одной команды оболочки — SDK Python без CLI, механизмов потоковой передачи, API голосовых списков, аутентификации с обновлением OAuth — зарегистрируйте плагин Python через `ctx.register_tts_provider()`. Плагин **сосуществует** (не заменяет) реестр [Пользовательские поставщики команд](#custom-command-providers); выберите поверхность, которая подходит вашему двигателю.

#### Когда выбирать, что

| В вашем бэкэнде есть… | Использование |
|---|---|
| Один CLI, читающий текст из файла/stdin и записывающий звук в файл/stdout | **Поставщик команд** (Python не требуется) |
| Два или три CLI, связанных трубами-оболочками | **Поставщик команд** |
| Только Python SDK — без CLI | **Плагин** |
| Потоковая передача байтов, которые вы хотите доставить по частям (голосовые пузырьки среднего поколения) | **Плагин** (переопределить `stream()`) |
| API голосового списка, используемый `hermes setup` | **Плагин** (переопределить `list_voices()`) |
| Поток обновления OAuth (не статический токен носителя) | **Плагин** |

Встроенные модули всегда выигрывают, а поставщики команд выигрывают у плагинов с тем же именем, поэтому плагины можно безопасно регистрировать по любому невстроенному имени, не беспокоясь о том, что они будут дублировать существующую конфигурацию.

#### Минимальный плагин

Добавьте это в `~/.hermes/plugins/my-tts/`:

`plugin.yaml`:
```yaml
name: my-tts
version: 0.1.0
description: "My custom Python TTS backend"
```

`__init__.py`:
```python
from agent.tts_provider import TTSProvider


class MyTTSProvider(TTSProvider):
    @property
    def name(self) -> str:
        return "my-tts"  # what tts.provider matches against

    @property
    def display_name(self) -> str:
        return "My Custom TTS"

    def is_available(self) -> bool:
        # Return False when credentials/deps are missing — picker skips
        # this row but the dispatcher still routes here on explicit config.
        import os
        return bool(os.environ.get("MY_TTS_API_KEY"))

    def synthesize(self, text, output_path, *, voice=None, model=None,
                   speed=None, format="mp3", **extra) -> str:
        # Write audio bytes to output_path, return the path.
        # Raise on failure — the dispatcher converts exceptions to a
        # standard error envelope.
        import my_tts_sdk
        client = my_tts_sdk.Client()
        audio_bytes = client.synthesize(text=text, voice=voice or "default")
        with open(output_path, "wb") as f:
            f.write(audio_bytes)
        return output_path


def register(ctx):
    ctx.register_tts_provider(MyTTSProvider())
```

Включите его (`hermes plugins enable my-tts`), укажите на него `tts.provider` (`tts.provider: my-tts` в `config.yaml`), и инструмент `text_to_speech` будет маршрутизироваться через ваш плагин.

#### Дополнительные крючки

Переопределите их в своем классе провайдера для более широкой интеграции:

- `list_voices()` → список `{id, display, language, gender, preview_url}` диктов, показанных в `hermes tools`.
- `list_models()` → список диктов `{id, display, languages, max_text_length}`.
- `get_setup_schema()` → верните `{name, badge, tag, env_vars: [{key, prompt, url}]}`, чтобы включить строку сборщика в `hermes tools` / `hermes setup`. Без этого плагин по-прежнему работает, но его строка в сборщике минимальна.
- `stream(text, *, voice, model, format, **extra)` → итератор, возвращающий аудиобайты для потоковой доставки (по умолчанию вызывает `NotImplementedError`).
- Свойство `voice_compatible` → установите `True`, если ваш вывод совместим с Opus и шлюз должен доставлять его в виде голосового пузырька (по умолчанию `False` = обычное аудиоприложение).

Полный текст ABC, включая строки документации, см. в разделе `agent/tts_provider.py`.

## Транскрипция голосовых сообщений (STT)

Голосовые сообщения, отправленные в Telegram, Discord, WhatsApp, Slack или Signal, автоматически расшифровываются и вставляются в разговор в виде текста. Агент видит стенограмму как обычный текст.

| Провайдер | Качество | Стоимость | API-ключ |
|----------|---------|------|---------| 
| **Локальный шепот** (по умолчанию) | Хорошо | Бесплатно | Ничего не нужно |
| **API Грока Шепота** | Хорошо–Лучший | Уровень бесплатного пользования | `GROQ_API_KEY` |
| **API OpenAI Whisper** | Хорошо–Лучший | Платный | `VOICE_TOOLS_OPENAI_KEY` или `OPENAI_API_KEY` |

:::информация Нулевая конфигурация
Локальная транскрипция работает сразу после установки `faster-whisper`. Если это недоступно, Hermes также может использовать локальный интерфейс командной строки `whisper` из обычных мест установки (например, `/opt/homebrew/bin`) или пользовательскую команду через `HERMES_LOCAL_STT_COMMAND`.
:::

### Конфигурация

```yaml
# In ~/.hermes/config.yaml
stt:
  provider: "local"           # "local" | "groq" | "openai" | "mistral" | "xai" | "elevenlabs" | "deepinfra"
  language: "en"              # Global language hint applied to every provider unless a per-provider language overrides it; set "" to restore auto-detect
  local:
    model: "base"             # tiny, base, small, medium, large-v3
    language: ""              # optional ISO-639-1 hint; blank = use HERMES_LOCAL_STT_LANGUAGE if set, else auto-detect
  groq:
    language: ""              # optional ISO-639-1 hint; blank = use HERMES_LOCAL_STT_LANGUAGE if set, else auto-detect
  openai:
    model: "whisper-1"        # whisper-1, gpt-4o-mini-transcribe, gpt-4o-transcribe, gpt-transcribe
  mistral:
    model: "voxtral-mini-latest"  # voxtral-mini-latest, voxtral-mini-2602
  xai:
    model: "grok-stt"         # xAI Grok STT
    language: ""              # optional ISO-639-1 hint; blank = use HERMES_LOCAL_STT_LANGUAGE if set, else "en"
```

### Сведения о поставщике

**Локальный (faster-whisper)** — Whisper запускается локально через [faster-whisper](https://github.com/SYSTRAN/faster-whisper). По умолчанию используется процессор, графический процессор, если доступен. Размеры модели:

| Модель | Размер | Скорость | Качество |
|-------|------|-------|---------|
| `tiny` | ~75 МБ | Самый быстрый | Базовый |
| `base` | ~150 МБ | Быстро | Хорошо (по умолчанию) |
| `small` | ~500 МБ | Средний | Лучше |
| `medium` | ~1,5 ГБ | Медленнее | Отлично |
| `large-v3` | ~3 ГБ | Самый медленный | Лучшее |

**Groq API** — требуется `GROQ_API_KEY`. Хороший запасной вариант в облаке, если вам нужен бесплатный вариант STT с хостингом. Установите `stt.groq.language` (или глобальную переменную окружения `HERMES_LOCAL_STT_LANGUAGE`), чтобы пропустить автоматическое обнаружение Whisper и уменьшить задержку при воспроизведении звука на известном языке.

**OpenAI API** — сначала принимает `VOICE_TOOLS_OPENAI_KEY` и возвращается к `OPENAI_API_KEY`. Поддерживает `whisper-1`, `gpt-4o-mini-transcribe`, `gpt-4o-transcribe` и `gpt-transcribe`.

**Mistral API (Voxtral Transcribe)** — требуется `MISTRAL_API_KEY`. Использует модели Mistral [Voxtral Transcribe](https://docs.mistral.ai/capabilities/audio/speech_to_text/). Поддерживает 13 языков, диаризацию говорящих и временные метки на уровне слов. Установите с помощью `cd ~/.hermes/hermes-agent && uv pip install -e ".[mistral]"`.

**xAI Grok STT** — Требуется `XAI_API_KEY`. Публикация в `https://api.x.ai/v1/stt` как multipart/form-data. Хороший выбор, если вы уже используете xAI для чата или TTS и вам нужен один ключ API для всего. Порядок автоматического обнаружения ставит его после Groq — явно задайте `stt.provider: xai`, чтобы принудительно использовать его.

**Пользовательский локальный резервный вариант CLI** — установите `HERMES_LOCAL_STT_COMMAND`, если вы хотите, чтобы Hermes напрямую вызывал локальную команду транскрипции. Шаблон команды поддерживает заполнители `{input_path}`, `{output_dir}`, `{language}` и `{model}`. Hermes преобразует отображаемый шаблон в список аргументов и выполняет его без оболочки, поэтому такие операторы, как `|`, `>`, `&&` и `;`, передаются как литеральные аргументы. Ваша команда должна записать транскрипт `.txt` где-то под `{output_dir}`.

#### Пример: Doubao/Volcengine ASR

Если вы используете [`doubao-speech`](https://pypi.org/project/doubao-speech/) для Doubao TTS (см. [выше](#example-doubao-chinese-seed-tts-20)), тот же пакет обрабатывает преобразование речи в текст через поверхность STT локальной команды:

```bash
pip install doubao-speech
export VOLCENGINE_APP_ID="your-app-id"
export VOLCENGINE_ACCESS_TOKEN="your-access-token"
export HERMES_LOCAL_STT_COMMAND='doubao-speech transcribe {input_path} --out {output_dir}/transcript.txt'
```

Если доверенному локальному шаблону намеренно нужны каналы, перенаправления или другая функция оболочки, вызовите оболочку явно. Сохраняйте динамические пути вне программы оболочки и передавайте их как позиционные аргументы:

```bash
export HERMES_LOCAL_STT_COMMAND='sh -c '\''whisper "$1" --output_format txt --output_dir "$2" | tee "$2/whisper.log"'\'' _ {input_path} {output_dir}'
```

В Windows вместо этого используйте явную оболочку `cmd /c` или PowerShell. Явная оболочка делает интерпретацию оболочки дополнительной частью настроенного argv, а не неявным свойством каждого локального шаблона STT.

```yaml
stt:
  provider: local_command
```

Гермес записывает входящее голосовое сообщение на `{input_path}`, запускает команду и читает файл `.txt`, созданный под `{output_dir}`. Язык автоматически определяется конечной точкой большой модели Volcengine.

### Резервное поведение

**Явный** выбор `stt.provider` (записанный в `config.yaml`, например, через `hermes tools`) строго соблюдается — если этот поставщик не может работать, транскрипция завершается с явной ошибкой (`stt is configured to use <provider> (set via hermes tools), but <failure>. Run 'hermes tools' to change it.`) вместо автоматического переключения механизмов. Обратите внимание, что `stt.provider: local`, записанный в вашей конфигурации, считается явным выбором.

Если **ни один поставщик не был выбран**, Hermes автоматически определяет из доступных:
- **Локальный fast-whisper недоступен** → пробует локальный `whisper` CLI или `HERMES_LOCAL_STT_COMMAND` перед облачными провайдерами.
- **Ключ Groq не установлен** → Пропущено; следующий доступный провайдер
- **Ключ OpenAI не установлен** → Пропущено; следующий доступный провайдер
- **Ключ Mistral/SDK не установлен** → Пропускается при автоматическом обнаружении; переходит к следующему доступному провайдеру
- **Ничего недоступно** → Голосовые сообщения передаются пользователю с точными примечаниями.

### Поставщики пользовательских команд STT

Если нужный вам движок STT не поддерживается изначально (Doubao ASR, NVIDIA Parakeet, сборка quiet.cpp, интерфейс командной строки SenseVoice с открытым исходным кодом, что-либо еще, предоставляющее команды оболочки), подключите его как **поставщик командного типа** без написания какого-либо Python. Hermes запускает команду оболочки для аудиофайла и считывает расшифровку.

Объявите одного или нескольких провайдеров в `stt.providers.<name>` и переключайтесь между ними с помощью `stt.provider: <name>` — той же формы, что и TTS [реестр команд-поставщиков](#custom-command-providers), адаптированной для направления вход=аудио → вывод=расшифровка.

```yaml
stt:
  provider: parakeet                # pick any name under stt.providers
  providers:
    parakeet:
      type: command
      command: "parakeet-asr --model nvidia/parakeet-tdt-0.6b-v2 --in {input_path} --out {output_path}"
      format: txt
      language: en
      timeout: 300

    whispercpp:
      type: command
      command: "whisper-cli -m ~/models/ggml-large-v3.bin -f {input_path} -otxt -of {output_dir}/transcript"
      format: txt

    sensevoice:
      type: command
      command: "sensevoice-cli {input_path} --json | tee {output_path}"
      format: json
```

Это дополняет устаревший аварийный люк `HERMES_LOCAL_STT_COMMAND` через встроенный путь `local_command`. В отличие от реестра поставщика команд, управляемого оболочкой, устаревший шаблон токенизирован в argv и запускается без неявной интерпретации оболочки. Используйте `stt.providers.<name>`, если вам нужно **несколько** механизмов STT, управляемых оболочкой, имя, которое вы можете выбрать через `stt.provider`, или что-либо еще, для чего требуется `language` / `model` / `timeout` для каждого поставщика.

#### заполнители STT

Ваш шаблон команды может ссылаться на эти заполнители. Hermes заменяет их во время рендеринга и заключает каждое значение в кавычки для окружающего контекста (голые/одинарные/двойные кавычки), поэтому пути с пробелами безопасны.

| Заполнитель | Значение |
|-------------------|----------------------------------------------------------------------|
| `{input_path}` | Абсолютный путь к входному аудиофайлу (исходное расположение, только для чтения) |
| `{output_path}` | Абсолютный путь, куда команда должна записать транскрипт |
| `{output_dir}` | Родительский каталог `{output_path}` (удобен для инструментов в стиле шепота) |
| `{format}` | Настроенный формат вывода: `txt` / `json` / `srt` / `vtt` |
| `{language}` | Настроенный код языка (по умолчанию `en`) |
| `{model}` | `stt.providers.<name>.model`, пусто, если не установлено |

Используйте `{{` и `}}` для буквальных фигурных скобок (удобно при встраивании фрагментов JSON в команду).

#### Как расшифровывается стенограмма

После успешного завершения вашей команды:

1. Если `{output_path}` существует и не пуст, → Hermes читает его как текст UTF-8.
2. В противном случае, если команда записала в стандартный вывод → это использует Hermes.
3. В противном случае → ошибка: «Поставщик команды STT не записал выходной файл и не выдал стандартный вывод».

Это позволяет использовать реестр как для CLI для записи файлов (`whisper-cli`, `parakeet-asr`), так и для однострочных команд в стиле Curl, которые выдают расшифровку на стандартный вывод (`curl … | jq -r .text`).

Для `format: json` / `srt` / `vtt` Hermes возвращает необработанное содержимое файла в виде поля `transcript`. Извлечение `.text` из JSON выходит за рамки возможностей исполнителя — либо настройте `format: txt`, либо выполните постобработку JSON в дальнейшем.

#### Дополнительные ключи поставщика команд STT

| Ключ | По умолчанию | Значение |
|-----------------|---------|------------------------------------------------------------------------------------------------------|
| `timeout` | `300` | Секунды; дерево процессов уничтожается по истечении срока действия (Unix `start_new_session`, Windows `taskkill /T`).     |
| `format` | `txt` | Один из `txt` / `json` / `srt` / `vtt`. Устанавливает расширение `{output_path}`.                       |
| `language` | `en` | Перенаправлено на `{language}`. По умолчанию `stt.language`, затем `en`.                                     |
| `model` | пустой | Перенаправлено на `{model}`. Аргумент `model=` для `transcribe_audio()` переопределяет это.                |

#### Примечания к поведению поставщика команд STT

- **Встроенные модули всегда побеждают.** Объявление `stt.providers.openai: type: command` НЕ отменяет настоящий обработчик OpenAI Whisper. Встроенное имя закорачивается перед запуском преобразователя поставщика команд.
- **Очистка дерева процессов.** Команда, запущенная через `timeout`, уничтожает все дерево процессов, а не только оболочку. Долго работающие конвейеры ASR, которые разветвляют подпроцессы загрузки модели, получают надежную работу.
- **Заключение оболочки в кавычки выполняется автоматически.** Заполнители внутри `'…'` получают безопасное экранирование с использованием одиночных кавычек; внутри `"…"` получите `$`/`` ` ``/`"` escaping; outside quotes get `shlex.quote`. Не заключайте значения заполнителей в кавычки.

#### Безопасность поставщика команд STT

Команда оболочки выполняется от того же пользователя, что и Hermes, с полным доступом к файловой системе — та же модель доверия, что и `tts.providers.<name>: type: command` и `HERMES_LOCAL_STT_COMMAND`. Указывайте поставщиков команд только из источников, которым вы доверяете.

### Поставщики плагинов Python (STT)

Для механизмов STT, которые не являются встроенными И не могут быть выражены в виде команды оболочки (нужен Python SDK, аутентификация с обновлением OAuth, потоковые фрагменты и т. д.), зарегистрируйте плагин Python через `ctx.register_transcription_provider()`. Плагин **сосуществует** с 8 встроенными поставщиками (`local`, `local_command`, `groq`, `openai`, `mistral`, `xai`, `elevenlabs`, `deepinfra`) и реестром `stt.providers.<name>: type: command` — встроенные модули сохраняют свои собственные реализации и всегда выигрывают при конфликте имен; поставщики команд выигрывают у одноименных плагинов (конфигурация более локальна, чем установка плагина).

#### Когда выбирать (STT)

| Бэкэнд имеет… | Использование |
|--------------------------------------------------------------|------------------------------------------------------------------|
| Одна команда оболочки, которая принимает аудиофайл и выдает текст | `stt.providers.<name>: type: command` (Python не требуется) |
| Требуется только устаревший аварийный люк с одним управлением | `HERMES_LOCAL_STT_COMMAND` env var (токенизированный argv; нет неявной оболочки) |
| Python SDK без CLI | `register_transcription_provider()` плагин |
| OAuth-обновление аутентификации, потоковые фрагменты, метаданные голосового списка | Плагин `register_transcription_provider()` |
| Это уже встроенная функция (`local`, `groq`, `openai`, …) | Установить `stt.provider: <name>` — встроенные модули |

#### Порядок разрешения

1. **`stt.provider` — встроенное имя** → встроенная отправка. **Всегда побеждает.**
2. **`stt.provider` соответствует `stt.providers.<name>` с набором `command:`** → средство запуска поставщика команд (см. [Поставщики пользовательских команд STT](#stt-custom-command-providers)). Выигрывает над одноименным плагином.
3. **`stt.provider` соответствует зарегистрированному в плагине `TranscriptionProvider`** → отправке плагина:
   - если `is_available()` плагина возвращает `False` (отсутствуют учетные данные или SDK), вызов отображает конверт ошибки недоступности, идентифицирующий плагин, а не общее сообщение «Нет доступного поставщика STT».
   - в противном случае `transcribe()` плагина вызывается с `model` (из общедоступного аргумента `model=`, возвращаясь к `stt.<provider>.model`) и `language` (из `stt.<provider>.language`).
4. **Нет совпадения** → ошибка «Нет доступного поставщика STT».

#### Пространство имен конфигурации для каждого провайдера

Плагины считывают свою конфигурацию для каждого провайдера из `stt.<provider>` в `config.yaml`, отражая то, как встроенные модули читают `stt.openai.model` / `stt.mistral.model`:

```yaml
stt:
  provider: my-stt
  my-stt:
    model: whisper-large-v3
    language: ja          # forwarded as language= to transcribe()
    # any other plugin-specific keys go here; read them via your
    # own config.yaml access in __init__/is_available/transcribe
```

Диспетчер пересылает `model` и `language` из этого раздела; все остальное плагин умеет читать сам.

#### Минимальный плагин

Добавьте это в `~/.hermes/plugins/my-stt/`:

`plugin.yaml`:
```yaml
name: my-stt
version: 0.1.0
description: "My custom Python STT backend"
```

`__init__.py`:
```python
from agent.transcription_provider import TranscriptionProvider


class MySTTProvider(TranscriptionProvider):
    @property
    def name(self) -> str:
        return "my-stt"  # what stt.provider matches against

    @property
    def display_name(self) -> str:
        return "My Custom STT"

    def is_available(self) -> bool:
        # Return False when credentials/deps are missing — picker skips
        # this row but the dispatcher still routes here on explicit config.
        import os
        return bool(os.environ.get("MY_STT_API_KEY"))

    def transcribe(self, file_path, *, model=None, language=None, **extra):
        # Return the standard transcribe envelope:
        #   {"success": bool, "transcript": str, "provider": str, "error": str}
        # Do NOT raise — convert exceptions to the error envelope so the
        # gateway/CLI caller sees a consistent shape on failure.
        try:
            import my_stt_sdk
            client = my_stt_sdk.Client()
            text = client.transcribe(open(file_path, "rb"))
            return {
                "success": True,
                "transcript": text,
                "provider": "my-stt",
            }
        except Exception as exc:
            return {
                "success": False,
                "transcript": "",
                "error": f"my-stt failed: {exc}",
                "provider": "my-stt",
            }


def register(ctx):
    ctx.register_transcription_provider(MySTTProvider())
```

Включите его (`hermes plugins enable my-stt`), установите `stt.provider: my-stt` в `config.yaml`, и транскрипция голосовых сообщений будет маршрутизироваться через ваш плагин.

#### Дополнительные крючки

Переопределите их в своем классе провайдера для более широкой интеграции:

- `list_models()` → список `{id, display, languages, max_audio_seconds}` диктов.
- `default_model()` → строка, возвращаемая, когда пользователь не переопределяет модель.
- `get_setup_schema()` → вернуть `{name, badge, tag, env_vars: [{key, prompt, url}]}` в строки выбора мощности в `hermes tools` / `hermes setup` (категория выбора для STT еще не поставляется — эти метаданные доступны плагинам для совместимости).

Полный текст ABC, включая строки документации, см. в разделе `agent/transcription_provider.py`.