# TorchTitan Troubleshooting

Purpose: symptom-to-fix table for the failures most commonly hit when scaling torchtitan runs (OOM, TP memory blowup, Float8 giving no speedup, checkpoint reshard errors, PP init).

> Commands referencing `run_train.sh` or `scripts/` are run from the root of the cloned
> upstream torchtitan repo, not from this skill directory.

## Issue: Out of memory on large models

Enable activation checkpointing and reduce batch size:
```toml
[activation_checkpoint]
mode = "full"  # Instead of "selective"

[training]
local_batch_size = 1
```

Or use gradient accumulation:
```toml
[training]
local_batch_size = 1
global_batch_size = 32  # Accumulates gradients
```

## Issue: TP causes high memory with async collectives

Set environment variable:
```bash
export TORCH_NCCL_AVOID_RECORD_STREAMS=1
```

## Issue: Float8 training not faster

Float8 only benefits large GEMMs. Filter small layers:
```toml
[quantize.linear.float8]
filter_fqns = ["attention.wk", "attention.wv", "output", "auto_filter_small_kn"]
```

Also confirm `[compile] enable = true` — Float8 without `torch.compile` loses the fused
scaling/casting kernels and can be slower than bf16. See [float8.md](float8.md) for the
GEMM-size rule of thumb (K,N > 4096).

## Issue: Checkpoint loading fails after parallelism change

Use DCP's resharding capability:
```bash
# Convert sharded checkpoint to single file
python -m torch.distributed.checkpoint.format_utils \
  dcp_to_torch checkpoint/step-1000 checkpoint.pt
```

More conversion paths (HuggingFace <-> torchtitan, async checkpointing, excluding keys)
are in [checkpoint.md](checkpoint.md).

## Issue: Pipeline parallelism initialization

Create a seed checkpoint before launching any run with
`pipeline_parallel_degree > 1` — see Workflow 4, Step 1 in
[training-workflows.md](training-workflows.md).
