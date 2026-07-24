# Hardware Compatibility and Troubleshooting

The GPU/CUDA/dtype support matrix for Flash Attention plus fixes for the common install, performance, and accuracy failures.

## Hardware requirements

- **GPU**: NVIDIA Ampere+ (A100, A10, A30) or AMD MI200+
- **VRAM**: Same as standard attention (Flash Attention doesn't increase memory)
- **CUDA**: 12.0+ (11.8 minimum)
- **PyTorch**: 2.2+ for native SDPA support

**Not supported**: V100 (Volta), CPU inference

## GPU architecture support

| Architecture | Example GPUs | Support |
|--------------|--------------|---------|
| Hopper | H100, H800 | Full, incl. FP8 (FlashAttention-3) |
| Ampere | A100, A10, A30 | Full |
| Turing | T4 | Supported |
| Volta | V100 | Not supported |
| CPU | — | Not supported |

Check capability at runtime:

```python
import torch
print(torch.cuda.get_device_capability())
# Should be ≥(7, 5) for Turing+
```

## Issue: ImportError: cannot import flash_attn

Install with no-build-isolation flag:

```bash
pip install flash-attn --no-build-isolation
```

Or install CUDA toolkit first:

```bash
conda install cuda -c nvidia
pip install flash-attn --no-build-isolation
```

## Issue: slower than expected (no speedup)

Flash Attention benefits increase with sequence length:

- <512 tokens: Minimal speedup (10-20%)
- 512-2K tokens: 2-3x speedup
- >2K tokens: 3-4x speedup

Check sequence length is sufficient.

## Issue: RuntimeError: CUDA error

Verify the GPU supports Flash Attention using the capability check above. Volta and older will fail.

## Issue: accuracy degradation

Check dtype is float16 or bfloat16 (not float32):

```python
q = q.to(torch.float16)  # Or torch.bfloat16
```

Flash Attention uses float16/bfloat16 for speed. Float32 is not supported.

For model-level output mismatches inside HuggingFace models, see `transformers-integration.md`.
