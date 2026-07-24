# Performance, Scaling and Cost

Quantifies GPU vs CPU speedups per operation, multi-GPU scaling behavior, published benchmarks, and the cloud cost comparison behind the "40% lower TCO" claim.

## GPU vs CPU per operation

| Operation | CPU (16 cores) | GPU (A100) | Speedup |
|-----------|----------------|------------|---------|
| Fuzzy dedup (8TB) | 120 hours | 7.5 hours | 16× |
| Exact dedup (1TB) | 8 hours | 0.5 hours | 16× |
| Quality filtering | 2 hours | 0.2 hours | 10× |

## Headline performance

- **16× faster** fuzzy deduplication (8TB RedPajama v2)
- **40% lower TCO** vs CPU alternatives
- **Near-linear scaling** across GPU nodes

## Detailed benchmarks

### Fuzzy deduplication (8TB RedPajama v2)
- **CPU (256 cores)**: 120 hours
- **GPU (8× A100)**: 7.5 hours
- **Speedup**: 16×

### Exact deduplication (1TB)
- **CPU (64 cores)**: 8 hours
- **GPU (4× A100)**: 0.5 hours
- **Speedup**: 16×

### Quality filtering (100GB)
- **CPU (32 cores)**: 2 hours
- **GPU (2× A100)**: 0.2 hours
- **Speedup**: 10×

## Cost comparison

**CPU-based curation** (AWS c5.18xlarge × 10):
- Cost: $3.60/hour × 10 = $36/hour
- Time for 8TB: 120 hours
- **Total**: $4,320

**GPU-based curation** (AWS p4d.24xlarge × 2):
- Cost: $32.77/hour × 2 = $65.54/hour
- Time for 8TB: 7.5 hours
- **Total**: $491.55

**Savings**: 89% reduction ($3,828 saved)

## Multi-GPU scaling

See `pipeline.md` for the `get_client` / `LocalCUDACluster` setup. Scaling is near-linear across GPU nodes for dedup and filtering workloads.

## Production deployments

- NVIDIA used NeMo Curator to prepare Nemotron-4 training data
- Open-source datasets curated: RedPajama v2, The Pile
