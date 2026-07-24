---
name: sparse-autoencoder-training
description: Provides guidance for training and analyzing Sparse Autoencoders (SAEs) using SAELens to decompose neural network activations into interpretable features. Use when discovering interpretable features, analyzing superposition, or studying monosemantic representations in language models.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [sae-lens>=6.0.0, transformer-lens>=2.0.0, torch>=2.0.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Sparse Autoencoders, SAE, Mechanistic Interpretability, Feature Discovery, Superposition]

---

# SAELens: Sparse Autoencoders for Mechanistic Interpretability

SAELens trains and analyzes Sparse Autoencoders (SAEs), which decompose polysemantic neural network activations into sparse, interpretable features. Based on Anthropic's research on monosemanticity.

**GitHub**: [jbloomAus/SAELens](https://github.com/jbloomAus/SAELens)

## When to use this skill

**Use it when you need to:**
- Discover interpretable features in model activations
- Understand what concepts a model has learned, or study superposition/feature geometry
- Perform feature-based steering, ablation, or attribution
- Analyze safety-relevant features (deception, bias, harmful content)

**Do NOT use it when:**
- You only need raw activation caching or basic hook analysis → **TransformerLens** directly
- You want causal intervention / patching experiments → **pyvene** or **TransformerLens**
- You need production-grade steering in a serving path → direct activation engineering

## Routing table

| To do this | Read |
|------------|------|
| Understand superposition, SAE loss, metric targets, hyperparameter ranges, architecture choice | [references/concepts.md](references/concepts.md) |
| Look up `SAE`, `SAEConfig`, `LanguageModelSAERunnerConfig`, `SAETrainingRunner`, `ActivationsStore`, `HookedSAETransformer`, JumpReLU, HF upload, Neuronpedia URLs | [references/api.md](references/api.md) |
| Follow an end-to-end tutorial: loading/analysis, training, attribution, steering, ablation, cross-prompt comparison, checklists | [references/tutorials.md](references/tutorials.md) |
| Fix dead features, low CE recovery, uninterpretable features, OOM | [references/troubleshooting.md](references/troubleshooting.md) |
| Orient in the reference set, install, list pre-trained SAE releases | [references/README.md](references/README.md) |

## Installation

```bash
pip install sae-lens
```

Requires Python 3.10+ and `transformer-lens>=2.0.0`.

## Key constraints and gotchas

- **L1 warm-up is not optional.** `l1_warm_up_steps=0` with a high `l1_coefficient` kills features early; use 500-2000 plus `use_ghost_grads=True`.
- **Sparsity trades off against reconstruction.** Higher `l1_coefficient` = sparser and more interpretable but lower CE recovery. Tune against targets: L0 50-200, CE score 80-95%, dead features <5%, explained variance >90%.
- **The SAE must match the hook point.** `hook_name`/`hook_layer`/`d_in` must correspond exactly to the activation you encode, or reconstruction is meaningless.
- **Expansion factor drives capacity.** `d_sae` is typically 4-16× `d_model`.
- **TopK gives fixed sparsity.** Use `architecture="topk"` with `activation_fn_kwargs={"k": N}` when you need consistent L0 instead of tuning L1.
- **Memory scales with the activation buffer**, not just batch size — reduce `n_batches_in_buffer` and `store_batch_size_prompts` first.

## End-to-end skeleton

```python
from transformer_lens import HookedTransformer
from sae_lens import SAE, LanguageModelSAERunnerConfig, SAETrainingRunner

# --- A. Analyze with a pre-trained SAE ---
model = HookedTransformer.from_pretrained("gpt2-small", device="cuda")
sae, cfg_dict, sparsity = SAE.from_pretrained(
    release="gpt2-small-res-jb",
    sae_id="blocks.8.hook_resid_pre",
    device="cuda",
)

tokens = model.to_tokens("The capital of France is Paris")
_, cache = model.run_with_cache(tokens)
activations = cache["resid_pre", 8]            # [batch, pos, d_model]

features = sae.encode(activations)             # [batch, pos, d_sae]
reconstructed = sae.decode(features)
print("active:", (features > 0).sum().item(),
      "err:", (activations - reconstructed).norm().item())

# --- B. Or train your own ---
cfg = LanguageModelSAERunnerConfig(
    model_name="gpt2-small",
    hook_name="blocks.8.hook_resid_pre",
    hook_layer=8,
    d_in=768,
    architecture="standard",
    d_sae=768 * 8,
    lr=4e-4,
    l1_coefficient=8e-5,
    l1_warm_up_steps=1000,
    use_ghost_grads=True,
    train_batch_size_tokens=4096,
    training_tokens=100_000_000,
    dataset_path="monology/pile-uncopyrighted",
    context_size=128,
    log_to_wandb=True,
    checkpoint_path="checkpoints",
)
sae = SAETrainingRunner(cfg).run()
```

## External resources

- [SAELens docs](https://jbloomaus.github.io/SAELens/) · [Neuronpedia feature browser](https://neuronpedia.org)
- Papers: [Towards Monosemanticity](https://transformer-circuits.pub/2023/monosemantic-features) (Anthropic 2023), [Scaling Monosemanticity](https://transformer-circuits.pub/2024/scaling-monosemanticity/) (Anthropic 2024), [Sparse Autoencoders Find Highly Interpretable Features](https://arxiv.org/abs/2309.08600) (ICLR 2024)
- Notebook tutorials and the ARENA SAE curriculum are linked from [references/tutorials.md](references/tutorials.md)
