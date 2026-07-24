# AudioCraft Model Variants and Parameters

## Architecture overview

```
AudioCraft Architecture:
┌──────────────────────────────────────────────────────────────┐
│                    Text Encoder (T5)                          │
│                         │                                     │
│                    Text Embeddings                            │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│              Transformer Decoder (LM)                         │
│     Auto-regressively generates audio tokens                  │
│     Using efficient token interleaving patterns               │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                EnCodec Audio Decoder                          │
│        Converts tokens back to audio waveform                 │
└──────────────────────────────────────────────────────────────┘
```

## Model variants

| Model | Size | Description | Use Case |
|-------|------|-------------|----------|
| `musicgen-small` | 300M | Text-to-music | Quick generation |
| `musicgen-medium` | 1.5B | Text-to-music | Balanced |
| `musicgen-large` | 3.3B | Text-to-music | Best quality |
| `musicgen-melody` | 1.5B | Text + melody | Melody conditioning |
| `musicgen-melody-large` | 3.3B | Text + melody | Best melody |
| `musicgen-stereo-*` | Varies | Stereo output | Stereo generation |
| `musicgen-style` | 1.5B | Style transfer | Reference-based |
| `audiogen-medium` | 1.5B | Text-to-sound | Sound effects |

Model IDs are prefixed with `facebook/`, e.g. `facebook/musicgen-medium`.

Choosing the variant is a hard constraint, not a preference:

- Melody conditioning (`generate_with_chroma`) only works on `musicgen-melody*`.
- Stereo output only comes from `musicgen-stereo-*`; base models return mono.
- Style conditioning (`generate_with_style`) only works on `musicgen-style`.
- Sound effects need `audiogen-medium`; MusicGen will produce music-like output instead.

## Generation parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `duration` | 8.0 | Length in seconds (1-120) |
| `top_k` | 250 | Top-k sampling |
| `top_p` | 0.0 | Nucleus sampling (0 = disabled) |
| `temperature` | 1.0 | Sampling temperature |
| `cfg_coef` | 3.0 | Classifier-free guidance |

Practical max duration for MusicGen is around 30 s; longer requests degrade or
fail depending on the variant. Higher `cfg_coef` = stricter text adherence,
higher `temperature` = more variation.

Style-only extras (`musicgen-style`):

| Parameter | Typical | Description |
|-----------|---------|-------------|
| `cfg_coef_beta` | 5.0 | Style influence (double CFG); `None` disables |
| `eval_q` | 3 | RVQ quantizers used by the style conditioner (1-6) |
| `excerpt_length` | 3.0 | Seconds of the reference used as the style excerpt |

## GPU memory requirements

| Model | FP32 VRAM | FP16 VRAM |
|-------|-----------|-----------|
| musicgen-small | ~4GB | ~2GB |
| musicgen-medium | ~8GB | ~4GB |
| musicgen-large | ~16GB | ~8GB |

`audiogen-medium` sits in the same band as `musicgen-medium` (1.5B).

## Sample rates

| Model family | Output sample rate | Channels |
|--------------|--------------------|----------|
| MusicGen (all) | 32000 Hz | 1 (2 for stereo variants) |
| AudioGen | 16000 Hz | 1 |
| EnCodec 32 kHz | 32000 Hz | 1 |
