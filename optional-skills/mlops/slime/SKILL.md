---
name: slime-rl-training
description: "slime: Megatron+SGLang RL rollout framework for GLM post-training - custom data generation workflows, GRPO at scale, tight Megatron-LM integration."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [sglang-router>=0.2.3, ray, torch>=2.0.0, transformers>=4.40.0]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Reinforcement Learning, Megatron-LM, SGLang, GRPO, Post-Training, GLM]

---

# slime: LLM Post-Training Framework for RL Scaling

slime is an LLM post-training framework from Tsinghua's THUDM team, powering GLM-4.5, GLM-4.6, and GLM-4.7. It connects Megatron-LM for training with SGLang for high-throughput rollout generation, orchestrated by Ray.

## When to use / when NOT to use

Use slime when you are running **RL rollouts at scale on a Megatron-LM backend** — GRPO/GSPO/PPO with SGLang generation, custom data-generation workflows, and a flexible data buffer, for GLM/Qwen3/DeepSeek-V3/Llama-3.

How it differs from its siblings: **slime** = Megatron+SGLang RL rollouts for GLM-class models on multi-node clusters; **trl** = RLHF/preference trainer algorithms on plain `transformers` (single node, no Megatron); **torchtitan** = from-scratch pretraining with 4D parallelism, not post-training; **peft** = adapter methods on a single GPU.

Consider alternatives when: you need enterprise-grade stability features (**miles**), flexible backend swapping (**verl**), or PyTorch-native abstractions (**torchforge**).

## Upstream-repo paths

Paths like `scripts/models/*.sh`, `examples/search-r1/`, `examples/`, `train.py` and `train_async.py` are inside the cloned upstream slime repo (https://github.com/THUDM/slime), **not** files shipped in this skill directory. Run them from the repo root after `git clone`, or from `/root/slime` inside the `slimerl/slime` Docker image.

## Routing table

| To do X | Read |
|---------|------|
| Install (Docker or source), run standard synchronous GRPO training, prepare JSONL data | [references/setup-and-workflows.md](references/setup-and-workflows.md) |
| Run asynchronous (overlapped rollout/training) mode | [references/setup-and-workflows.md](references/setup-and-workflows.md) |
| Build multi-turn / tool-calling agentic training | [references/setup-and-workflows.md](references/setup-and-workflows.md) |
| Use co-location mode to share GPUs between train and rollout | [references/setup-and-workflows.md](references/setup-and-workflows.md) |
| Look up any CLI argument (Megatron / `--sglang-*` / slime-specific), the `Sample` dataclass, `Status` enum | [references/api-reference.md](references/api-reference.md) |
| Implement a custom generate function, custom reward model, or data-buffer filter | [references/api-reference.md](references/api-reference.md) |
| Configure multi-task evaluation, or check the supported-model matrix and model-script format | [references/api-reference.md](references/api-reference.md) |
| Fix SGLang crashes, weight-sync timeouts, OOM, slow data loading, checkpoint/TP mismatches, async or multi-turn errors | [references/troubleshooting.md](references/troubleshooting.md) |

## Key constraints and gotchas

- **Batch-size identity must hold**: `rollout_batch_size × n_samples_per_prompt = global_batch_size × num_steps_per_rollout` (e.g. `32 × 8 = 256 × 1`).
- **Three argument namespaces**: Megatron args pass through unchanged, SGLang args take a `--sglang-` prefix, slime's own args are defined in `slime/utils/arguments.py`. Mixing up the namespace is the most common launch failure.
- **`--colocate` is incompatible with `train_async.py`**; async requires disjoint train/rollout GPUs.
- When colocated, lower `--sglang-mem-fraction-static` (~0.4) so the trainer has device memory left.
- Checkpoints are parallelism-specific: a checkpoint saved with TP=2 must be loaded with TP=2.
- Megatron format is required for training; convert HuggingFace checkpoints first via `CKPT_ARGS`.

## End-to-end skeleton

```bash
# In the cloned upstream slime repo (or /root/slime in the Docker image)
source scripts/models/qwen3-4B.sh          # sets MODEL_ARGS / CKPT_ARGS

python train.py \
    --actor-num-nodes 1 --actor-num-gpus-per-node 4 \
    --rollout-num-gpus 4 \
    --advantage-estimator grpo \
    --use-kl-loss --kl-loss-coef 0.001 \
    --prompt-data /path/to/data.jsonl \
    --input-key prompt --label-key label --apply-chat-template \
    --rollout-batch-size 32 --n-samples-per-prompt 8 \
    --global-batch-size 256 --num-rollout 3000 \
    ${MODEL_ARGS[@]} ${CKPT_ARGS[@]}
```

`data.jsonl` lines look like `{"prompt": "What is 2 + 2?", "label": "4"}`.

## Resources

- **Documentation**: https://thudm.github.io/slime/
- **GitHub**: https://github.com/THUDM/slime
- **Blog**: https://lmsys.org/blog/2025-07-09-slime/
