# TorchTitan Training Workflows

Purpose: installation, tokenizer/asset download, and the full TOML + launch recipes for single-node, multi-node SLURM, and 4D-parallel (405B-class) pretraining runs.

> **Upstream-repo paths**: `scripts/download_hf_assets.py`, `run_train.sh`, and
> `torchtitan/models/*/train_configs/*.toml` are inside the cloned upstream torchtitan repo
> (https://github.com/pytorch/torchtitan), **not** files shipped in this skill directory.
> Run them from the repo root after `git clone`. If you installed via `pip install torchtitan`
> you have the `torchtitan.train` module but not these helper scripts — clone the repo to get them.

## Installation

```bash
# From PyPI (stable)
pip install torchtitan

# From source (latest features, requires PyTorch nightly)
git clone https://github.com/pytorch/torchtitan
cd torchtitan
pip install -r requirements.txt
```

## Download tokenizer

```bash
# Get HF token from https://huggingface.co/settings/tokens
python scripts/download_hf_assets.py --repo_id meta-llama/Llama-3.1-8B --assets tokenizer --hf_token=...
```

## Smoke test: 8 GPUs, stock config

```bash
CONFIG_FILE="./torchtitan/models/llama3/train_configs/llama3_8b.toml" ./run_train.sh
```

---

## Workflow 1: Pretrain Llama 3.1 8B on a single node

Copy this checklist:

```
Single Node Pretraining:
- [ ] Step 1: Download tokenizer
- [ ] Step 2: Configure training
- [ ] Step 3: Launch training
- [ ] Step 4: Monitor and checkpoint
```

**Step 1: Download tokenizer**

```bash
python scripts/download_hf_assets.py \
  --repo_id meta-llama/Llama-3.1-8B \
  --assets tokenizer \
  --hf_token=YOUR_HF_TOKEN
```

**Step 2: Configure training**

Edit or create a TOML config file:

```toml
# llama3_8b_custom.toml
[job]
dump_folder = "./outputs"
description = "Llama 3.1 8B training"

[model]
name = "llama3"
flavor = "8B"
hf_assets_path = "./assets/hf/Llama-3.1-8B"

[optimizer]
name = "AdamW"
lr = 3e-4

[lr_scheduler]
warmup_steps = 200

[training]
local_batch_size = 2
seq_len = 8192
max_norm = 1.0
steps = 1000
dataset = "c4"

[parallelism]
data_parallel_shard_degree = -1  # Use all GPUs for FSDP

[activation_checkpoint]
mode = "selective"
selective_ac_option = "op"

[checkpoint]
enable = true
folder = "checkpoint"
interval = 500
```

**Step 3: Launch training**

```bash
# 8 GPUs on single node
CONFIG_FILE="./llama3_8b_custom.toml" ./run_train.sh

# Or explicitly with torchrun
torchrun --nproc_per_node=8 \
  -m torchtitan.train \
  --job.config_file ./llama3_8b_custom.toml
```

**Step 4: Monitor and checkpoint**

TensorBoard logs are saved to `./outputs/tb/`:
```bash
tensorboard --logdir ./outputs/tb
```

---

## Workflow 2: Multi-node training with SLURM

```
Multi-Node Training:
- [ ] Step 1: Configure parallelism for scale
- [ ] Step 2: Set up SLURM script
- [ ] Step 3: Submit job
- [ ] Step 4: Resume from checkpoint
```

**Step 1: Configure parallelism for scale**

For a 70B model on 256 GPUs (32 nodes):
```toml
[parallelism]
data_parallel_shard_degree = 32  # FSDP across 32 ranks
tensor_parallel_degree = 8        # TP within node
pipeline_parallel_degree = 1      # No PP for 70B
context_parallel_degree = 1       # Increase for long sequences
```

**Step 2: Set up SLURM script**

```bash
#!/bin/bash
#SBATCH --job-name=llama70b
#SBATCH --nodes=32
#SBATCH --ntasks-per-node=8
#SBATCH --gpus-per-node=8

srun torchrun \
  --nnodes=32 \
  --nproc_per_node=8 \
  --rdzv_backend=c10d \
  --rdzv_endpoint=$MASTER_ADDR:$MASTER_PORT \
  -m torchtitan.train \
  --job.config_file ./llama3_70b.toml
```

**Step 3: Submit job**

```bash
sbatch multinode_trainer.slurm
```

**Step 4: Resume from checkpoint**

Training auto-resumes if a checkpoint exists in the configured folder. See
[checkpoint.md](checkpoint.md) for resharding and HuggingFace conversion.

---

## Workflow 3: Enable Float8 training on H100s

Float8 provides roughly a 30-50% speedup on H100 GPUs.

```
Float8 Training:
- [ ] Step 1: Install torchao
- [ ] Step 2: Configure Float8
- [ ] Step 3: Launch with compile
```

Full details — tensorwise vs rowwise recipes, layer filtering, how Float8 interacts with
FSDP/TP, and MXFP8 on Blackwell — are in [float8.md](float8.md). The three-step summary:

```bash
# Step 1
USE_CPP=0 pip install git+https://github.com/pytorch/ao.git
```

```toml
# Step 2: add to your TOML config
[model]
converters = ["quantize.linear.float8"]

[quantize.linear.float8]
enable_fsdp_float8_all_gather = true
precompute_float8_dynamic_scale_for_fsdp = true
filter_fqns = ["output"]  # Exclude output layer

[compile]
enable = true
components = ["model", "loss"]
```

```bash
# Step 3
CONFIG_FILE="./llama3_8b.toml" ./run_train.sh \
  --model.converters="quantize.linear.float8" \
  --quantize.linear.float8.enable_fsdp_float8_all_gather \
  --compile.enable
```

---

## Workflow 4: 4D parallelism for 405B models

```
4D Parallelism (FSDP + TP + PP + CP):
- [ ] Step 1: Create seed checkpoint
- [ ] Step 2: Configure 4D parallelism
- [ ] Step 3: Launch on 512 GPUs
```

**Step 1: Create seed checkpoint**

Required for consistent initialization across PP stages:
```bash
NGPU=1 CONFIG_FILE=./llama3_405b.toml ./run_train.sh \
  --checkpoint.enable \
  --checkpoint.create_seed_checkpoint \
  --parallelism.data_parallel_shard_degree 1 \
  --parallelism.tensor_parallel_degree 1 \
  --parallelism.pipeline_parallel_degree 1
```

**Step 2: Configure 4D parallelism**

```toml
[parallelism]
data_parallel_shard_degree = 8   # FSDP
tensor_parallel_degree = 8       # TP within node
pipeline_parallel_degree = 8     # PP across nodes
context_parallel_degree = 1      # CP for long sequences

[training]
local_batch_size = 32
seq_len = 8192
```

**Step 3: Launch on 512 GPUs**

```bash
# 64 nodes x 8 GPUs = 512 GPUs
srun torchrun --nnodes=64 --nproc_per_node=8 \
  -m torchtitan.train \
  --job.config_file ./llama3_405b.toml
```

## Parallelism degree selection matrix

| Scale | Model | Recommended degrees |
|-------|-------|---------------------|
| 8 GPUs (1 node) | 8B | `data_parallel_shard_degree = -1` (pure FSDP2) |
| 256 GPUs (32 nodes) | 70B | DP shard 32, TP 8, PP 1, CP 1 |
| 512 GPUs (64 nodes) | 405B | DP shard 8, TP 8, PP 8, CP 1 |
| Long-context runs | any | raise `context_parallel_degree`, keep TP within a node |

Rules of thumb: keep TP inside a single node (NVLink), use PP only when the model no
longer fits with FSDP+TP, and raise CP only for long sequences. Enabling PP requires a
seed checkpoint (Step 1 above). See [fsdp.md](fsdp.md) for FSDP2/HSDP degrees.
