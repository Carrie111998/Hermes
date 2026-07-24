---
name: fine-tuning-with-trl
description: "TRL: RLHF and preference trainer algorithms - SFT, DPO, PPO, GRPO and reward modeling loops on top of transformers, for aligning a model after pretraining."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [trl, transformers, datasets, peft, accelerate, torch]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Post-Training, TRL, Reinforcement Learning, Fine-Tuning, SFT, DPO, PPO, GRPO, RLHF, Preference Alignment, HuggingFace]

---

# TRL - Transformer Reinforcement Learning

TRL provides post-training **algorithms** for aligning language models with human preferences: SFT, reward modeling, DPO (and 10+ loss variants), PPO, GRPO, RLOO, and OnlineDPO, all as `Trainer` subclasses over plain `transformers`.

## When to use / when NOT to use

Use TRL when you already have a pretrained (or SFT'd) model and need an **alignment algorithm**: preference data to optimize against, a reward model to train or consume, or online RL rollouts driven by a reward function.

How it differs from its siblings: **trl** = RLHF/preference trainer algorithms on single-node `transformers`; **peft** = the adapter method (LoRA/QLoRA) you plug *into* a TRL trainer, not an algorithm; **torchtitan** = from-scratch pretraining with 4D parallelism, before any alignment; **slime** = the same RL family but on a Megatron+SGLang cluster for GLM-scale rollouts; **accelerate** = you keep your own loop; **pytorch-lightning** = a Trainer replaces your loop.

**Method selection**: SFT for prompt-completion pairs -> basic instruction following; DPO for preference pairs with no reward model; PPO when you have a reward model and want maximum control; GRPO when memory-constrained and doing online RL; RewardTrainer when building a reusable scorer for an RLHF pipeline.

**Use alternatives instead**: HuggingFace `Trainer` (plain fine-tuning, no RL), Axolotl (YAML-driven config), LitGPT (educational/minimal), Unsloth (fastest LoRA).

## Routing table

| To do X | Read |
|---------|------|
| Run the full RLHF pipeline: SFT -> reward model -> PPO -> evaluate | [references/rlhf-pipeline.md](references/rlhf-pipeline.md) |
| Do SFT: dataset formats, chat templates, packing, multi-GPU, LoRA | [references/sft-training.md](references/sft-training.md) |
| Run DPO end to end, or pick a loss variant (IPO, cDPO, RPO, hinge, BCO, SPPO, DiscoPOP, APO, AOT) and its beta / label-smoothing settings | [references/dpo-variants.md](references/dpo-variants.md) |
| Train or evaluate a reward model: Bradley-Terry loss, outcome vs process rewards, dataset format | [references/reward-modeling.md](references/reward-modeling.md) |
| Configure online RL: PPO, GRPO, RLOO, OnlineDPO parameters, reward-function signatures, CLI usage, memory comparison | [references/online-rl.md](references/online-rl.md) |
| Go deep on GRPO: reward-function design, why loss rises, mode-collapse detection, multi-stage training, adaptive reward scaling, deployment | [references/grpo-training.md](references/grpo-training.md) |
| Start from a production-ready GRPO script | [templates/basic_grpo_training.py](templates/basic_grpo_training.py) |
| Size a GPU / fix VRAM budgets per method | [references/hardware-requirements.md](references/hardware-requirements.md) |
| Fix DPO OOM, poor alignment, a reward model that will not learn, unstable PPO | [references/troubleshooting.md](references/troubleshooting.md) |

## Key constraints and gotchas

- **DPO holds a frozen reference model**, so it needs roughly 2x the memory of SFT at the same batch size; PPO additionally holds a value head and the reward model (~40GB for 7B).
- **`beta` is the alignment/conservatism dial** for DPO (default 0.1): higher stays closer to the reference, lower aligns more aggressively and risks degeneration.
- **Dataset schema is per-trainer and unforgiving**: SFT wants prompt-completion, DPO/RewardTrainer want `prompt`/`chosen`/`rejected`, GRPO wants prompt-only. A wrong schema fails at collation, not at config time.
- **Rising GRPO loss is expected**, not a bug — track reward, not loss.
- Pass `processing_class=tokenizer` (not `tokenizer=`) to the newer preference trainers.
- Every trainer accepts `peft_config=`, so LoRA/QLoRA is the default answer to OOM; combine with gradient checkpointing and gradient accumulation.
- BF16 on A100/H100; fp16 is a common source of NaN in DPO/PPO.

## End-to-end skeleton

```bash
pip install trl transformers datasets peft accelerate
```

```python
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import DPOTrainer, DPOConfig

model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")
tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")
dataset = load_dataset("trl-lib/ultrafeedback_binarized", split="train")  # chosen/rejected

trainer = DPOTrainer(
    model=model,
    args=DPOConfig(output_dir="model-dpo", beta=0.1, learning_rate=5e-7),
    train_dataset=dataset,
    processing_class=tokenizer,
)
trainer.train()
trainer.save_model()
```

Swap `DPOTrainer`/`DPOConfig` for `SFTTrainer`/`SFTConfig` (prompt-completion data) or
`GRPOTrainer`/`GRPOConfig` (prompt-only data plus `reward_funcs=`); the shape is identical.

## Resources

- Docs: https://huggingface.co/docs/trl/
- GitHub: https://github.com/huggingface/trl
- Examples: https://github.com/huggingface/trl/tree/main/examples/scripts
- Papers: InstructGPT (2022, "Training language models to follow instructions with human feedback"); DPO (2023, "Direct Preference Optimization: Your Language Model is Secretly a Reward Model"); GRPO (2024, "Group Relative Policy Optimization")
