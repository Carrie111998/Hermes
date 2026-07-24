---
name: distributed-llm-pretraining-torchtitan
description: "torchtitan: from-scratch LLM pretraining with 4D parallelism on 8 to 512+ GPUs - FSDP2 plus TP/PP/CP, Float8, torch.compile, distributed checkpointing, PyTorch native."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [torch>=2.6.0, torchtitan>=0.2.0, torchao>=0.5.0]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Model Architecture, Distributed Training, TorchTitan, FSDP2, Tensor Parallel, Pipeline Parallel, Context Parallel, Float8, Llama, Pretraining]

---

# TorchTitan - PyTorch Native Distributed LLM Pretraining

TorchTitan is PyTorch's official platform for large-scale LLM **pretraining** with composable 4D parallelism (FSDP2, TP, PP, CP), achieving 65%+ speedups over baselines on H100 GPUs.

## When to use / when NOT to use

Use torchtitan when you are **pretraining from scratch** at 8 to 512+ GPUs and want a PyTorch-native stack: composable FSDP2 + TP + PP + CP, Float8 on H100, `torch.compile`, and distributed (DCP) checkpoints interoperable with torchtune/HuggingFace.

How it differs from its siblings: **torchtitan** = from-scratch pretraining with 4D parallelism; **peft** = adapter (LoRA/QLoRA) fine-tuning on one GPU; **trl** = RLHF/preference algorithms after pretraining; **slime** = Megatron+SGLang RL rollouts; **accelerate** = you keep your own training loop; **pytorch-lightning** = a Trainer replaces your loop.

Do NOT use torchtitan for fine-tuning or alignment (use TRL/Axolotl/PEFT), for maximum NVIDIA-only throughput (**Megatron-LM**), for the broader ZeRO/inference ecosystem (**DeepSpeed**), or for small-scale educational runs (**LitGPT**).

## Upstream-repo paths

Paths like `scripts/download_hf_assets.py`, `run_train.sh`, and `torchtitan/models/*/train_configs/*.toml` are inside the cloned upstream torchtitan repo (https://github.com/pytorch/torchtitan), **not** files shipped in this skill directory. Run them from the repo root after `git clone`. A `pip install torchtitan` gives you the `torchtitan.train` module but not these helper scripts.

## Routing table

| To do X | Read |
|---------|------|
| Install, download tokenizer assets, write a TOML config, run single-node 8-GPU pretraining | [references/training-workflows.md](references/training-workflows.md) |
| Scale to multi-node with SLURM, or pick parallelism degrees for 70B / 405B | [references/training-workflows.md](references/training-workflows.md) |
| Configure 4D parallelism and create the seed checkpoint PP requires | [references/training-workflows.md](references/training-workflows.md) |
| Understand FSDP2 vs FSDP1, ZeRO equivalents, HSDP, meta-device init, mixed precision | [references/fsdp.md](references/fsdp.md) |
| Enable Float8 (tensorwise vs rowwise), filter layers, MXFP8 on Blackwell | [references/float8.md](references/float8.md) |
| Save/resume, reshard, async checkpointing, HuggingFace <-> torchtitan conversion | [references/checkpoint.md](references/checkpoint.md) |
| Add a new model architecture (TrainSpec protocol, parallelize fn, state dict adapter) | [references/custom-models.md](references/custom-models.md) |
| Check which models/sizes are supported and their measured H100 throughput | [references/models-and-benchmarks.md](references/models-and-benchmarks.md) |
| Fix OOM, TP memory blowup, Float8 not faster, checkpoint reshard failure, PP init errors | [references/troubleshooting.md](references/troubleshooting.md) |

## Key constraints and gotchas

- **Float8 requires `torch.compile`** (`[compile] enable = true`); without it the scaling/casting kernels are unfused and Float8 can be slower than bf16. It also needs H100 or newer.
- **`pipeline_parallel_degree > 1` requires a seed checkpoint** created first with all parallel degrees set to 1, otherwise PP stages initialize inconsistently.
- **Keep tensor parallelism inside a node** (NVLink); span nodes with FSDP/PP instead.
- **Checkpoints are DCP-sharded.** Changing parallelism degrees between runs requires resharding (`dcp_to_torch`), not a plain load.
- `data_parallel_shard_degree = -1` means "use all available GPUs" — set it explicitly once you add TP/PP, or the degrees will not multiply out to your world size.
- Source installs track PyTorch nightly; the PyPI release tracks stable PyTorch. Mixing them breaks FSDP2 APIs.
- `export TORCH_NCCL_AVOID_RECORD_STREAMS=1` when using TP with async collectives.

## End-to-end skeleton

```bash
# From the root of the cloned upstream torchtitan repo
python scripts/download_hf_assets.py \
  --repo_id meta-llama/Llama-3.1-8B --assets tokenizer --hf_token=YOUR_HF_TOKEN

cat > llama3_8b_custom.toml <<'TOML'
[job]
dump_folder = "./outputs"
[model]
name = "llama3"
flavor = "8B"
hf_assets_path = "./assets/hf/Llama-3.1-8B"
[optimizer]
name = "AdamW"
lr = 3e-4
[training]
local_batch_size = 2
seq_len = 8192
steps = 1000
dataset = "c4"
[parallelism]
data_parallel_shard_degree = -1
[checkpoint]
enable = true
folder = "checkpoint"
interval = 500
TOML

torchrun --nproc_per_node=8 -m torchtitan.train \
  --job.config_file ./llama3_8b_custom.toml

tensorboard --logdir ./outputs/tb
```

## Resources

- GitHub: https://github.com/pytorch/torchtitan
- Paper: https://arxiv.org/abs/2410.06511
- ICLR 2025: https://iclr.cc/virtual/2025/poster/29620
- PyTorch Forum: https://discuss.pytorch.org/c/distributed/torchtitan/44
