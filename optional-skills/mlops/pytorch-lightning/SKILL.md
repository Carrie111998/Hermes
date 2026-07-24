---
name: pytorch-lightning
description: "Lightning: replace your training loop with LightningModule + Trainer - callbacks, checkpointing, and DDP/FSDP/DeepSpeed switched by a flag, same code laptop to cluster."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [lightning, torch, transformers]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [PyTorch Lightning, Training Framework, Distributed Training, DDP, FSDP, DeepSpeed, High-Level API, Callbacks, Best Practices, Scalable]

---

# PyTorch Lightning - High-Level Training Framework

Lightning organizes PyTorch code to eliminate training-loop boilerplate while keeping full PyTorch flexibility. `Trainer` owns devices, distribution, precision, checkpointing and logging.

## When to use this skill

**Use PyTorch Lightning when:**
- You want a clean, organized, production-ready training loop
- You switch between single GPU, multi-GPU, multi-node, or TPU with the same code
- You want built-in callbacks, checkpointing, and logging
- A team needs a standardized project structure

**Do NOT use it when:**
- You want minimal edits to an existing bespoke loop → **Accelerate**
- Your main need is multi-node orchestration + HPO scheduling → **Ray Train**
- You need maximum control or are learning the internals → **raw PyTorch**
- You are in the TensorFlow ecosystem → **Keras**

## Routing table

| To do this | Read |
|------------|------|
| Convert a raw PyTorch loop, add validation/test steps, configure optimizers and LR schedulers | [references/lightningmodule.md](references/lightningmodule.md) |
| Use or write callbacks: ModelCheckpoint, EarlyStopping, LearningRateMonitor, SWA, progress bars, custom hooks | [references/callbacks.md](references/callbacks.md) |
| Configure DDP / FSDP / DeepSpeed ZeRO, multi-node, SLURM, Kubernetes, mixed precision, distributed checkpointing, hardware/precision matrix | [references/distributed.md](references/distributed.md) |
| Run HPO with Ray Tune, Optuna, WandB sweeps, Hyperopt, or Lightning's LR/batch-size finders | [references/hyperparameter-tuning.md](references/hyperparameter-tuning.md) |
| Fix loss not decreasing, OOM, validation not running, unexpected DDP processes | [references/troubleshooting.md](references/troubleshooting.md) |

## Installation

```bash
pip install lightning
```

## Key constraints and gotchas

- **Never call `.to(device)` or `.cuda()` in a LightningModule.** Trainer owns device placement; manual moves break DDP/FSDP.
- **Validation only runs if you pass `val_loader`** to `trainer.fit()` — a missing loader silently skips validation and any `val_loss`-monitored callback.
- **Callbacks that monitor a metric require that exact `self.log()` key**, logged in the matching step hook.
- **Do not wrap your dataloader in a `DistributedSampler`** — Lightning does it automatically under DDP.
- **Strategy is a flag, but memory behavior is not.** DDP replicates the model per GPU; use FSDP/DeepSpeed ZeRO-3 once the model no longer fits.
- **`return loss` from `training_step`** — Lightning calls `zero_grad`/`backward`/`step`; doing it yourself double-steps.
- **Prefer `precision='bf16'` on A100/H100**; fp16 on those cards is a regression risk without loss scaling care.

## End-to-end skeleton

```python
import lightning as L
import torch
from torch import nn
from torch.utils.data import DataLoader
from lightning.pytorch.callbacks import ModelCheckpoint, EarlyStopping

class LitModel(L.LightningModule):
    def __init__(self, hidden_size=128):
        super().__init__()
        self.save_hyperparameters()
        self.model = nn.Sequential(
            nn.Linear(28 * 28, hidden_size), nn.ReLU(), nn.Linear(hidden_size, 10)
        )

    def training_step(self, batch, batch_idx):
        x, y = batch
        loss = nn.functional.cross_entropy(self.model(x), y)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        self.log('val_loss', nn.functional.cross_entropy(self.model(x), y))

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)

train_loader = DataLoader(train_dataset, batch_size=32)
val_loader = DataLoader(val_dataset, batch_size=32)

trainer = L.Trainer(
    max_epochs=10,
    accelerator='gpu', devices=8, strategy='ddp',   # or 'fsdp' / 'deepspeed'
    precision='bf16',
    callbacks=[
        ModelCheckpoint(monitor='val_loss', mode='min', save_top_k=3),
        EarlyStopping(monitor='val_loss', mode='min', patience=5),
    ],
)
trainer.fit(LitModel(), train_loader, val_loader)
trainer.test(dataloaders=test_loader)
```

## Resources

- Docs: https://lightning.ai/docs/pytorch/stable/
- GitHub: https://github.com/Lightning-AI/pytorch-lightning
- Version: 2.5.5+
- Examples: https://github.com/Lightning-AI/pytorch-lightning/tree/master/examples
