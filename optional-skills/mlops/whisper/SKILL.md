---
name: whisper
description: OpenAI's general-purpose speech recognition model. Supports 99 languages, transcription, translation to English, and language identification. Six model sizes from tiny (39M params) to large (1550M params). Use for speech-to-text, podcast transcription, or multilingual audio processing. Best for robust, multilingual ASR.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [openai-whisper, transformers, torch]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Whisper, Speech Recognition, ASR, Multimodal, Multilingual, OpenAI, Speech-To-Text, Transcription, Translation, Audio Processing]

---

# Whisper - Robust Speech Recognition

OpenAI's multilingual speech recognition model: 99 languages, trained on 680,000 hours of audio, MIT licensed.

## When to use Whisper

**Use when:**
- Speech-to-text transcription (99 languages)
- Podcast/video transcription
- Meeting notes automation
- Translation to English
- Noisy audio transcription
- Multilingual audio processing

**Use alternatives instead:**
- **AssemblyAI**: Managed API, speaker diarization
- **Deepgram**: Real-time streaming ASR
- **Google Speech-to-Text**: Cloud-based
- **faster-whisper**: Same models, ~4x faster — prefer it for throughput or near-real-time

## Minimal end-to-end skeleton

```bash
pip install -U openai-whisper   # Python 3.8-3.11
# ffmpeg is required: brew install ffmpeg / sudo apt install ffmpeg / choco install ffmpeg
```

```python
import whisper

model = whisper.load_model("turbo")            # tiny|base|small|medium|large|turbo
result = model.transcribe("audio.mp3", language="en")

print(result["text"])
for segment in result["segments"]:
    print(f"[{segment['start']:.2f}s - {segment['end']:.2f}s] {segment['text']}")
```

CLI equivalent: `whisper audio.mp3 --model turbo --output_format srt`.

## Where to read more

| To do this | Read |
|------------|------|
| Choose a model size against VRAM/speed budget | [references/transcription-options.md](references/transcription-options.md) |
| Set language, `task="translate"`, `initial_prompt`, word timestamps, temperature fallback | [references/transcription-options.md](references/transcription-options.md) |
| Use the CLI and its output formats (txt/srt/vtt/json) | [references/transcription-options.md](references/transcription-options.md) |
| Force CPU/GPU, read real-time-factor benchmarks | [references/transcription-options.md](references/transcription-options.md) |
| Batch a directory, stream with faster-whisper, generate subtitles, feed a RAG pipeline, extract audio with ffmpeg | [references/workflows.md](references/workflows.md) |
| Check per-language quality tiers and language codes | [references/languages.md](references/languages.md) |

## Key constraints

- **ffmpeg must be on PATH** — Whisper shells out to it for decoding.
- **`large` and `turbo` are multilingual-only**; there is no English-only variant of them.
- **Accuracy degrades past ~30 minutes** of continuous audio; chunk long recordings.
- **No speaker diarization.** If you need "who said what", use AssemblyAI/pyannote alongside.
- **Hallucination is a real failure mode** — on silence or noise Whisper can emit invented or looping text. Validate output before treating it as ground truth.
- **Specifying `language=` is faster** than auto-detection and avoids misdetection on short clips.
