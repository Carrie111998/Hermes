---
name: huggingface-accelerate
description: "Accelerate: keep your own PyTorch training loop and add DDP/FSDP/DeepSpeed/Megatron in 4 lines - device placement, mixed precision FP16/BF16/FP8, one launch command."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [accelerate, torch, transformers]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Distributed Training, HuggingFace, Accelerate, DeepSpeed, FSDP, Mixed Precision, PyTorch, DDP, Unified API, Simple]

---

# HuggingFace Accelerate - Unified Distributed Training

Add distributed training to an **existing** PyTorch script without restructuring it: you keep your own loop, Accelerate handles device placement, sharding and precision.

## When to use Accelerate

**Use Accelerate when**:
- You already have a hand-written PyTorch training loop and want to keep it
- You want one script that runs on CPU / 1 GPU / 8 GPUs / multi-node / TPU unchanged
- You want to switch between DDP, DeepSpeed, FSDP and Megatron by config, not code
- You are in the HuggingFace ecosystem (Transformers, TRL, PEFT all sit on Accelerate)

**Use alternatives instead**:
- **PyTorch Lightning**: you are willing to *replace* your loop with `LightningModule` + `Trainer` to get callbacks, logging and checkpoint policy for free
- **torchtitan**: from-scratch large-scale pretraining with 4D parallelism
- **pytorch-fsdp**: you want to hand-wire `torch.distributed`/FSDP yourself with no wrapper
- **DeepSpeed directly**: you need DeepSpeed-specific features and direct API control
- **Ray Train**: multi-node orchestration plus hyperparameter tuning

## Minimal end-to-end skeleton

```bash
pip install accelerate
```

```python
import torch
from accelerate import Accelerator            # +1

accelerator = Accelerator(mixed_precision="bf16")   # +2

model = torch.nn.Transformer()
optimizer = torch.optim.Adam(model.parameters())
dataloader = torch.utils.data.DataLoader(dataset)

model, optimizer, dataloader = accelerator.prepare(model, optimizer, dataloader)  # +3

for batch in dataloader:
    optimizer.zero_grad()
    loss = model(batch)
    accelerator.backward(loss)                # +4  (replaces loss.backward())
    optimizer.step()

if accelerator.is_main_process:
    accelerator.save_state("checkpoint/")
```

```bash
accelerate config          # interactive, writes the default config
accelerate launch train.py # same command for every topology
```

## Where to read more

| To do this | Read |
|------------|------|
| Convert an existing single-GPU script, and the exact `accelerate launch` flags for multi-GPU / multi-node | [references/workflows.md](references/workflows.md) |
| Turn on FP16 / BF16 / FP8 mixed precision | [references/workflows.md](references/workflows.md) |
| Enable DeepSpeed ZeRO (plugin form and `deepspeed_config.json` form) | [references/workflows.md](references/workflows.md) |
| Enable FSDP with a sharding strategy and auto-wrap policy | [references/workflows.md](references/workflows.md) |
| Do gradient accumulation correctly, compute effective batch size | [references/workflows.md](references/workflows.md) |
| Fix device-placement, accumulation, checkpointing or FSDP-nondeterminism bugs; check hardware/launcher support | [references/workflows.md](references/workflows.md) |
| Set up tensor / pipeline / sequence parallelism via Megatron | [references/megatron-integration.md](references/megatron-integration.md) |
| Write a custom distributed plugin or advanced config | [references/custom-plugins.md](references/custom-plugins.md) |
| Profile, tune memory, apply performance best practices | [references/performance.md](references/performance.md) |

## Key constraints

- **Remove every manual `.to('cuda')`/`.to(device)`** after `prepare()` — manual placement fights Accelerate and causes device-mismatch errors.
- **`accelerator.backward(loss)` replaces `loss.backward()`**; skipping this breaks gradient scaling and accumulation.
- **Gradient accumulation must use the `with accelerator.accumulate(model):` context manager**, not a manual step counter.
- **Checkpoint with `accelerator.save_state()` guarded by `accelerator.is_main_process`**; load on all processes.
- **BF16 is the safer default** over FP16 (no loss scaling); FP8 requires H100-class hardware.
- **Set `accelerate.utils.set_seed()`** or FSDP/DeepSpeed runs will not be reproducible across ranks.
