---
name: audiocraft-audio-generation
description: "AudioCraft: MusicGen text-to-music, AudioGen text-to-sound."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [audiocraft, torch>=2.0.0, transformers>=4.30.0]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Multimodal, Audio Generation, Text-to-Music, Text-to-Audio, MusicGen]

---

# AudioCraft: Audio Generation

Meta's AudioCraft for text-to-music and text-to-audio generation with MusicGen, AudioGen and EnCodec.

## When to use this skill

**Use AudioCraft when:**
- Need to generate music from text descriptions
- Creating sound effects and environmental audio
- Building music generation applications
- Need melody-conditioned music generation
- Want stereo audio output
- Require controllable music generation with style transfer

**Key features:**
- **MusicGen**: Text-to-music generation with melody conditioning
- **AudioGen**: Text-to-sound effects generation
- **EnCodec**: High-fidelity neural audio codec
- **Multiple model sizes**: Small (300M) to Large (3.3B)
- **Stereo support**: Full stereo audio generation
- **Style conditioning**: MusicGen-Style for reference-based generation

**Use alternatives instead:**
- **Stable Audio**: For longer commercial music generation
- **Bark**: For text-to-speech with music/sound effects
- **Riffusion**: For spectogram-based music generation
- **OpenAI Jukebox**: For raw audio generation with lyrics

## Red lines (non-negotiable)

- **VRAM is a hard ceiling.** musicgen-small ~4GB FP32 / ~2GB FP16,
  medium ~8GB / ~4GB, large ~16GB / ~8GB. Pick the variant that fits the GPU
  *before* writing generation code; OOM is the default failure mode otherwise.
- **Capability is bound to the variant.** Melody conditioning requires
  `musicgen-melody*`, stereo requires `musicgen-stereo-*`, style transfer
  requires `musicgen-style`, sound effects require `audiogen-medium`. There is no
  flag that adds these to a base model.
- **Sample rate is not a choice.** MusicGen outputs 32000 Hz, AudioGen outputs
  16000 Hz. Saving with the wrong rate silently changes pitch and speed.
- **Duration ceiling ~30 s.** Longer requests degrade or fail; concatenate
  continuations instead of raising `duration`.
- **First run downloads GB-scale weights** (up to 3.3B params for
  musicgen-large). Expect long, resumable downloads; set a cache directory on a
  disk with room before starting.
- **Licensing:** the AudioCraft code is MIT, but the model weights are released
  under their own research/CC-BY-NC terms. Verify the weight licence on the
  HuggingFace model card before any commercial or redistributed use.
- **Generation cost is GPU time, not API calls.** A 30 s clip on
  musicgen-large is orders of magnitude more compute than on musicgen-small —
  prototype small, then scale up.

## Minimal end-to-end skeleton

```python
import torchaudio
from audiocraft.models import MusicGen

# 1. Load a variant that fits your VRAM
model = MusicGen.get_pretrained('facebook/musicgen-small')

# 2. Configure generation
model.set_generation_params(duration=8, top_k=250, temperature=1.0, cfg_coef=3.0)

# 3. Generate (list in -> [batch, channels, samples] out)
wav = model.generate(["happy upbeat electronic dance music with synths"])

# 4. Save at the model's native sample rate (MusicGen = 32000)
torchaudio.save("output.wav", wav[0].cpu(), sample_rate=32000)
```

Swap step 1 for `AudioGen.get_pretrained('facebook/audiogen-medium')` and step 4
for `sample_rate=16000` to generate sound effects instead of music.

## Routing table

| To do this | Read |
|------------|------|
| Install AudioCraft, choose native vs HuggingFace path, run a smoke test | `references/installation.md` |
| Pick a model variant, look up generation parameters, VRAM or sample rates | `references/model-variants.md` |
| Call MusicGen / MusicGen-Style / AudioGen / EnCodec (melody, stereo, continuation, style) | `references/generation-api.md` |
| Copy a pipeline class, batch sound-design script or Gradio demo | `references/code-templates.md` |
| Reduce VRAM, speed up generation, tune batching | `references/performance-tuning.md` |
| Fine-tune, train multi-GPU, deploy a server, evaluate quality, write better prompts | `references/advanced-usage.md` |
| Fix errors: install failures, OOM, silent output, wrong sample rate, artifacts | `references/troubleshooting.md` |

## Resources

- **GitHub**: https://github.com/facebookresearch/audiocraft
- **Paper (MusicGen)**: https://arxiv.org/abs/2306.05284
- **Paper (AudioGen)**: https://arxiv.org/abs/2209.15352
- **HuggingFace**: https://huggingface.co/facebook/musicgen-small
- **Demo**: https://huggingface.co/spaces/facebook/MusicGen
