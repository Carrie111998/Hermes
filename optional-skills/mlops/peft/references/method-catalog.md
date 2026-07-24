# PEFT Method Catalog

Purpose: side-by-side comparison of the adapter families PEFT ships, with minimal configs for the non-LoRA methods.

## Comparison

| Method | Trainable % | Memory | Speed | Best For |
|--------|------------|--------|-------|----------|
| **LoRA** | 0.1-1% | Low | Fast | General fine-tuning |
| **QLoRA** | 0.1-1% | Very Low | Medium | Memory-constrained |
| AdaLoRA | 0.1-1% | Low | Medium | Automatic rank selection |
| IA3 | 0.01% | Minimal | Fastest | Few-shot adaptation |
| Prefix Tuning | 0.1% | Low | Medium | Generation control |
| Prompt Tuning | 0.001% | Minimal | Fast | Simple task adaptation |
| P-Tuning v2 | 0.1% | Low | Medium | NLU tasks |

LoRA and QLoRA recipes live in [lora-and-qlora.md](lora-and-qlora.md); AdaLoRA, DoRA,
LoRA+, rsLoRA and LoftQ are covered in [advanced-usage.md](advanced-usage.md).

## IA3 (minimal parameters)

```python
from peft import IA3Config

ia3_config = IA3Config(
    target_modules=["q_proj", "v_proj", "k_proj", "down_proj"],
    feedforward_modules=["down_proj"]
)
model = get_peft_model(model, ia3_config)
# Trains only 0.01% of parameters!
```

## Prefix Tuning

```python
from peft import PrefixTuningConfig

prefix_config = PrefixTuningConfig(
    task_type="CAUSAL_LM",
    num_virtual_tokens=20,      # Prepended tokens
    prefix_projection=True       # Use MLP projection
)
model = get_peft_model(model, prefix_config)
```

Prompt Tuning and P-Tuning v2 follow the same shape — a `*Config` object passed to
`get_peft_model` — with `num_virtual_tokens` as the main capacity knob.
