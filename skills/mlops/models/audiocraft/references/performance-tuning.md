# AudioCraft Performance and Memory Tuning

VRAM tables per variant are in `model-variants.md`. Deeper diagnostics
(memory leaks, CPU fallback, slow generation) are in `troubleshooting.md`.

## Memory optimization

```python
# Use smaller model
model = MusicGen.get_pretrained('facebook/musicgen-small')

# Clear cache between generations
torch.cuda.empty_cache()

# Generate shorter durations
model.set_generation_params(duration=10)  # Instead of 30

# Use half precision
model = model.half()
```

Levers ordered by impact: smaller variant > shorter `duration` > FP16 >
cache clearing. Duration dominates KV-cache growth because generation is
auto-regressive over audio tokens (~50 tokens per second of audio).

## Batch processing efficiency

```python
# Process multiple prompts at once (more efficient)
descriptions = ["prompt1", "prompt2", "prompt3", "prompt4"]
wav = model.generate(descriptions)  # Single batch

# Instead of
for desc in descriptions:
    wav = model.generate([desc])  # Multiple batches (slower)
```

Batching multiplies peak VRAM roughly linearly with batch size, so trade batch
size against the per-variant VRAM budget rather than maximising both.

## Throughput checklist

- Keep one loaded model per process; reloading `get_pretrained` per request
  dominates latency.
- Wrap generation in `torch.no_grad()`.
- Prefer a single `generate(list_of_prompts)` call over a Python loop.
- Warm up once at startup — the first generation pays compilation/allocation cost.
- For concurrent serving, serialize GPU access (single worker + queue) instead of
  running several models on one GPU; see the async service template in
  `advanced-usage.md`.
