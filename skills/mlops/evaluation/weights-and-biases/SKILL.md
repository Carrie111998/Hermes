---
name: weights-and-biases
description: "W&B: log ML experiments, sweeps, model registry, dashboards."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [wandb]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [MLOps, Weights And Biases, WandB, Experiment Tracking, Hyperparameter Tuning, Model Registry, Collaboration, Real-Time Visualization, PyTorch, TensorFlow, HuggingFace]

---

# Weights & Biases: ML Experiment Tracking & MLOps

## When to Use This Skill

Use Weights & Biases (W&B) when you need to:
- **Track ML experiments** with automatic metric logging
- **Visualize training** in real-time dashboards
- **Compare runs** across hyperparameters and configurations
- **Optimize hyperparameters** with automated sweeps
- **Manage model registry** with versioning and lineage
- **Collaborate on ML projects** with team workspaces
- **Track artifacts** (datasets, models, code) with lineage

**Users**: 200,000+ ML practitioners | **GitHub Stars**: 10.5k+ | **Integrations**: 100+

## Red lines (non-negotiable)

1. **Never hardcode `WANDB_API_KEY`.** It comes from `wandb login` or the environment,
   never from a committed script, notebook cell, or Dockerfile. A leaked key grants
   write access to every project in the entity.
2. **Check project visibility before logging.** The free tier's unlimited quota applies
   to *public* projects — a run logged to the wrong project publishes your config,
   metrics, sample predictions, and model files to the internet.
3. **Never upload large datasets or weights as raw files.** Use artifact references
   (`add_reference` to S3/GCS) for anything large; naive `add_file` on a multi-GB tarball
   burns storage quota and makes every later `download()` slow. See `references/artifacts.md`.
4. **Sweeps are unbounded spend by default.** Always pass an explicit `count=` to
   `wandb.agent`, and prefer `bayes` + early termination over `grid` on continuous
   parameters — a grid over 4 continuous params is effectively an infinite job queue.
5. **Treat training + sweep runs as long-running jobs.** Launch agents detached
   (`tmux`/`nohup`/SLURM), and use `WANDB_MODE=offline` + `wandb sync` when the network
   is unreliable, so a dropped connection never kills or silently loses a run.
6. **Always `wandb.finish()`** (or use a context manager) so the run is marked finished
   rather than left crashed and excluded from comparisons.

## End-to-end skeleton

```bash
pip install wandb
wandb login
```

```python
import wandb

# 1. Start a run and record the full config
run = wandb.init(
    project="my-project",
    config={
        "learning_rate": 0.001,
        "epochs": 10,
        "batch_size": 32,
        "architecture": "ResNet50",
    },
)

# 2. Train, logging metrics each epoch
for epoch in range(run.config.epochs):
    train_loss = train_epoch()
    val_loss, val_acc = validate()

    wandb.log({
        "epoch": epoch,
        "train/loss": train_loss,
        "val/loss": val_loss,
        "val/accuracy": val_acc,
    })

# 3. Version the result as an artifact (not a bare file upload)
artifact = wandb.Artifact("final-model", type="model")
artifact.add_file("model.pth")
wandb.log_artifact(artifact)

# 4. Close the run
wandb.finish()
```

## Where to go next

| To do this | Read this |
|---|---|
| Log runs, config, metrics, media, histograms, checkpoints; offline mode; run naming/tagging hygiene | `references/experiment-tracking.md` |
| Run hyperparameter sweeps: search strategies, parameter distributions, early termination, parallel/multi-GPU agents | `references/sweeps.md` |
| Version datasets and models, track lineage, promote through the model registry | `references/artifacts.md` |
| Wire W&B into HuggingFace Transformers, PyTorch Lightning, Keras/TensorFlow, fast.ai, XGBoost/LightGBM, or raw PyTorch | `references/integrations.md` |
| Build custom charts and confusion matrices, publish reports, share runs, set up teams, check plan limits | `references/visualization-and-collaboration.md` |

## Resources

- **Documentation**: https://docs.wandb.ai
- **GitHub**: https://github.com/wandb/wandb (10.5k+ stars)
- **Examples**: https://github.com/wandb/examples
- **Community**: https://wandb.ai/community
- **Discord**: https://wandb.me/discord
