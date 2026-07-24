---
name: stable-diffusion-image-generation
description: State-of-the-art text-to-image generation with Stable Diffusion models via HuggingFace Diffusers. Use when generating images from text prompts, performing image-to-image translation, inpainting, or building custom diffusion pipelines.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [diffusers>=0.30.0, transformers>=4.41.0, accelerate>=0.31.0, torch>=2.0.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Image Generation, Stable Diffusion, Diffusers, Text-to-Image, Multimodal, Computer Vision]

---

# Stable Diffusion Image Generation

Generating and editing images with Stable Diffusion through the HuggingFace
Diffusers library, on your own GPU.

## When to use Stable Diffusion

**Use Stable Diffusion when:**
- Generating images from text descriptions
- Performing image-to-image translation (style transfer, enhancement)
- Inpainting (filling in masked regions)
- Outpainting (extending images beyond boundaries)
- Creating variations of existing images
- Building custom image generation workflows

**Key features:**
- **Text-to-Image**: Generate images from natural language prompts
- **Image-to-Image**: Transform existing images with text guidance
- **Inpainting**: Fill masked regions with context-aware content
- **ControlNet**: Add spatial conditioning (edges, poses, depth)
- **LoRA Support**: Efficient fine-tuning and style adaptation
- **Multiple Models**: SD 1.5, SDXL, SD 3.0, Flux support

**Do NOT use Stable Diffusion when** — use these alternatives instead:
- **DALL-E 3**: For API-based generation without GPU
- **Midjourney**: For artistic, stylized outputs
- **Imagen**: For Google Cloud integration
- **Leonardo.ai**: For web-based creative workflows

Also a poor fit if you have no CUDA GPU (or comparable accelerator) available —
CPU inference is impractically slow.

## Routing table

| To do this | Read |
|---|---|
| Install, run a first SD 1.5 / SDXL generation, batch generate, use the high-quality or 4-step LCM workflow | `references/quick-start-and-workflows.md` |
| Understand pipeline/model/scheduler structure, choose a pipeline class or scheduler, load fp16/bf16 or a custom VAE | `references/pipelines-and-schedulers.md` |
| Tune steps, guidance scale, size, negative prompts; make a run reproducible with a seed | `references/generation-parameters.md` |
| Condition on an input image: img2img, inpainting, ControlNet and available control types | `references/image-conditioning.md` |
| Load, scale, combine or unload LoRA style/character adapters | `references/lora-adapters.md` |
| Fit into limited VRAM: CPU offload, attention slicing, xFormers, VAE slicing/tiling | `references/memory-optimization.md` |
| Custom pipelines and denoising loops, IP-Adapter, SDXL refiner, T2I-Adapter, DreamBooth/LoRA training, textual inversion, quantization, FastAPI/Docker/K8s serving, callbacks, multi-GPU | `references/advanced-usage.md` |
| Fix install conflicts, OOM, black/noisy/blurry output, scheduler, LoRA, ControlNet, download and performance problems | `references/troubleshooting.md` |

## Key constraints and gotchas

- Use `torch_dtype=torch.float16` on GPU; fp32 roughly doubles VRAM for no visible
  gain. Keep dtypes consistent across swapped-in components or you get black images.
- `height`/`width` must be multiples of 8, and should match the model's native
  resolution (512 for SD 1.x, 1024 for SDXL).
- Always create a replacement scheduler with `from_config(pipe.scheduler.config)`.
- Few-step schedulers (LCM) need `guidance_scale` ≈ 1.0, not 7.5.
- Do not call `pipe.to("cuda")` after enabling CPU offload — offloading owns device
  placement.
- Output is non-deterministic unless you pass a seeded `torch.Generator`.
- Inpainting masks: white = region to regenerate, and the mask must match image size.
- ControlNet control images must be RGB at the generation resolution.
- Name adapters when loading more than one LoRA, otherwise the second overwrites
  the first.
- First generation is slow (compile/warm-up); benchmark the second run.

## End-to-end skeleton

```python
from diffusers import AutoPipelineForText2Image, DPMSolverMultistepScheduler
import torch

# 1. Load in fp16
pipe = AutoPipelineForText2Image.from_pretrained(
    "stabilityai/stable-diffusion-xl-base-1.0",
    torch_dtype=torch.float16,
    variant="fp16"
)

# 2. Faster scheduler + memory headroom
pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
pipe.enable_model_cpu_offload()   # replaces pipe.to("cuda")

# 3. Reproducible generation
generator = torch.Generator(device="cuda").manual_seed(42)

image = pipe(
    prompt="A serene mountain landscape at sunset, highly detailed",
    negative_prompt="blurry, low quality, distorted",
    num_inference_steps=25,
    guidance_scale=7.5,
    height=1024,
    width=1024,
    generator=generator,
).images[0]

image.save("output.png")
```

## Resources

- **Documentation**: https://huggingface.co/docs/diffusers
- **Repository**: https://github.com/huggingface/diffusers
- **Model Hub**: https://huggingface.co/models?library=diffusers
- **Discord**: https://discord.gg/diffusers
