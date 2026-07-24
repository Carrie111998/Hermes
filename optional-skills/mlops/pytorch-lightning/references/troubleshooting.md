# PyTorch Lightning Troubleshooting

Fixes for the everyday Lightning failures: loss not moving, OOM, validation silently skipped, and unexpected DDP process spawning.

## Issue: loss not decreasing

Check data and model setup:

```python
# Add to training_step
def training_step(self, batch, batch_idx):
    if batch_idx == 0:
        print(f"Batch shape: {batch[0].shape}")
        print(f"Labels: {batch[1]}")
    loss = ...
    return loss
```

## Issue: out of memory

Reduce batch size or use gradient accumulation:

```python
trainer = L.Trainer(
    accumulate_grad_batches=4,  # Effective batch = batch_size × 4
    precision='bf16'  # Or 'fp16', reduces memory 50%
)
```

For sharded strategies and FSDP-specific OOM, see `distributed.md`.

## Issue: validation not running

Ensure you pass `val_loader`:

```python
# WRONG
trainer.fit(model, train_loader)

# CORRECT
trainer.fit(model, train_loader, val_loader)
```

## Issue: DDP spawns multiple processes unexpectedly

Lightning auto-detects GPUs. Explicitly set devices:

```python
# Test on CPU first
trainer = L.Trainer(accelerator='cpu', devices=1)

# Then GPU
trainer = L.Trainer(accelerator='gpu', devices=1)
```

## Related

- NCCL timeouts, inter-node debugging, DDP nondeterminism, DeepSpeed config errors: `distributed.md`
- Trials OOM / slow sweeps / non-reproducible best trial: `hyperparameter-tuning.md`
