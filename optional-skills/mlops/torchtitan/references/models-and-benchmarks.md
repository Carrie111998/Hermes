# TorchTitan Supported Models and Benchmarks

Purpose: which model families/sizes torchtitan ships and their maturity, plus measured H100 throughput per parallelism configuration.

## Supported models

| Model | Sizes | Status |
|-------|-------|--------|
| Llama 3.1 | 8B, 70B, 405B | Production |
| Llama 4 | Various | Experimental |
| DeepSeek V3 | 16B, 236B, 671B (MoE) | Experimental |
| GPT-OSS | 20B, 120B (MoE) | Experimental |
| Qwen 3 | Various | Experimental |
| Flux | Diffusion | Experimental |

Only Llama 3.1 is considered production-grade; everything else may change shape between
releases. To add an architecture not in this list, see [custom-models.md](custom-models.md).

## Performance benchmarks (H100)

| Model | GPUs | Parallelism | TPS/GPU | Techniques |
|-------|------|-------------|---------|------------|
| Llama 8B | 8 | FSDP | 5,762 | Baseline |
| Llama 8B | 8 | FSDP+compile+FP8 | 8,532 | +48% |
| Llama 70B | 256 | FSDP+TP+AsyncTP | 876 | 2D parallel |
| Llama 405B | 512 | FSDP+TP+PP | 128 | 3D parallel |

Headline claim: 65%+ speedups over baselines on H100 with composable 4D parallelism plus
Float8 and `torch.compile`. The Float8-specific breakdown (FSDP only -> +compile ->
+Float8) is in [float8.md](float8.md).
