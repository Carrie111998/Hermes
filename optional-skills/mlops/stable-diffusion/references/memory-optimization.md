# Memory Optimization

Techniques for fitting Stable Diffusion into limited VRAM: CPU offloading, attention slicing, xFormers attention, and VAE slicing/tiling.

Apply in roughly this order — cheapest quality/latency cost first.

## CPU offloading

```python
# Model CPU offload - moves models to CPU when not in use
pipe.enable_model_cpu_offload()

# Sequential CPU offload - more aggressive, slower
pipe.enable_sequential_cpu_offload()
```

Do not call `pipe.to("cuda")` after enabling offload — offloading manages device
placement itself.

## Attention slicing

```python
# Reduce memory by computing attention in chunks
pipe.enable_attention_slicing()

# Or specific chunk size
pipe.enable_attention_slicing("max")
```

## xFormers memory-efficient attention

```python
# Requires xformers package
pipe.enable_xformers_memory_efficient_attention()
```

The xFormers build must match your installed CUDA/PyTorch version; see
`troubleshooting.md` if the install fails.

## VAE slicing for large images

```python
# Decode latents in tiles for large images
pipe.enable_vae_slicing()
pipe.enable_vae_tiling()
```

VAE decoding is often the actual OOM point at high resolutions, so try these
before dropping resolution.

## Also worth trying

- Load in `torch.float16` with `variant="fp16"` (see `pipelines-and-schedulers.md`).
- Reduce `num_images_per_prompt` / batch size.
- 8-bit or NF4 quantization for very tight VRAM budgets — see `advanced-usage.md`.
- Full OOM triage checklist: `troubleshooting.md` (Memory Issues).
