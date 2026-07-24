# Stable Diffusion Generation Parameters

The knobs on a pipeline call — steps, guidance scale, size, seeds and negative prompts — and how to make a generation reproducible.

## Key parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `prompt` | Required | Text description of desired image |
| `negative_prompt` | None | What to avoid in the image |
| `num_inference_steps` | 50 | Denoising steps (more = better quality) |
| `guidance_scale` | 7.5 | Prompt adherence (7-12 typical) |
| `height`, `width` | 512/1024 | Output dimensions (multiples of 8) |
| `generator` | None | Torch generator for reproducibility |
| `num_images_per_prompt` | 1 | Batch size |

Notes:
- `height`/`width` must be multiples of 8; use the model's native resolution
  (512 for SD 1.x, 1024 for SDXL) or composition degrades.
- Very high `guidance_scale` over-saturates and can produce artifacts.
- LCM-style few-step setups want `guidance_scale` around 1.0 instead of 7.5.

## Reproducible generation

Pass an explicitly seeded generator; without it every call samples fresh noise.

```python
import torch

generator = torch.Generator(device="cuda").manual_seed(42)

image = pipe(
    prompt="A cat wearing a top hat",
    generator=generator,
    num_inference_steps=50
).images[0]
```

## Negative prompts

```python
image = pipe(
    prompt="Professional photo of a dog in a garden",
    negative_prompt="blurry, low quality, distorted, ugly, bad anatomy",
    guidance_scale=7.5
).images[0]
```

Negative prompts are the first fix for soft/low-quality output and for distorted
faces and hands. See `troubleshooting.md` for prompt weighting and further fixes.
