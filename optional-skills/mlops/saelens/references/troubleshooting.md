# SAELens Troubleshooting

Diagnoses the four failure modes seen most often when training SAEs, with the wrong/right config for each.

## Issue: high dead feature ratio

```python
# WRONG: No warm-up, features die early
cfg = LanguageModelSAERunnerConfig(
    l1_coefficient=1e-4,
    l1_warm_up_steps=0,  # Bad!
)

# RIGHT: Warm-up L1 penalty
cfg = LanguageModelSAERunnerConfig(
    l1_coefficient=8e-5,
    l1_warm_up_steps=1000,  # Gradually increase
    use_ghost_grads=True,   # Revive dead features
)
```

## Issue: poor reconstruction (low CE recovery)

```python
# Reduce sparsity penalty
cfg = LanguageModelSAERunnerConfig(
    l1_coefficient=5e-5,  # Lower = better reconstruction
    d_sae=768 * 16,       # More capacity
)
```

## Issue: features not interpretable

```python
# Increase sparsity (higher L1)
cfg = LanguageModelSAERunnerConfig(
    l1_coefficient=1e-4,  # Higher = sparser, more interpretable
)
# Or use TopK architecture
cfg = LanguageModelSAERunnerConfig(
    architecture="topk",
    activation_fn_kwargs={"k": 50},  # Exactly 50 active features
)
```

## Issue: memory errors during training

```python
cfg = LanguageModelSAERunnerConfig(
    train_batch_size_tokens=2048,  # Reduce batch size
    store_batch_size_prompts=4,    # Fewer prompts in buffer
    n_batches_in_buffer=8,         # Smaller activation buffer
)
```

## Quick lookup table

| If you see... | Try... |
|---------------|--------|
| High L0 (>200) | Increase `l1_coefficient` |
| Low CE recovery (<80%) | Decrease `l1_coefficient`, increase `d_sae` |
| Many dead features (>5%) | Enable `use_ghost_grads`, increase `l1_warm_up_steps` |
| Training instability | Lower `lr`, increase `lr_warm_up_steps` |
| CUDA OOM | Lower `train_batch_size_tokens` / `n_batches_in_buffer` |
