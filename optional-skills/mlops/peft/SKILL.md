---
name: peft-fine-tuning
description: "PEFT: LoRA/QLoRA adapter fine-tuning on a single GPU - train under 1% of params of a 7B-70B model, 25+ adapter methods, multi-adapter serving, HuggingFace official."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [peft>=0.13.0, transformers>=4.45.0, torch>=2.0.0, bitsandbytes>=0.43.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Fine-Tuning, PEFT, LoRA, QLoRA, Parameter-Efficient, Adapters, Low-Rank, Memory Optimization, Multi-Adapter]

---

# PEFT (Parameter-Efficient Fine-Tuning)

Fine-tune LLMs by training <1% of parameters using LoRA, QLoRA, and 25+ adapter methods.

## When to use / when NOT to use

Use PEFT when you want **adapter-based fine-tuning of a 7B-70B model on a single GPU** — LoRA on a 24-80GB card, QLoRA when 4-bit quantization is the only way it fits, and multi-adapter serving from one base model.

How it differs from its siblings: **peft** = adapter methods on a single GPU (the model and your loop stay ordinary `transformers`); **trl** = RLHF/preference *algorithms* (SFT/DPO/PPO/GRPO) that consume a PEFT config; **torchtitan** = from-scratch pretraining with 4D parallelism across many nodes; **accelerate** = you keep your own loop and it handles device placement; **pytorch-lightning** = a Trainer replaces your loop; **slime** = Megatron+SGLang RL rollouts.

**Use QLoRA (PEFT + quantization) when:** fine-tuning 70B on a single 24GB GPU, memory is the primary constraint, and you accept a ~5% quality trade-off.

**Use full fine-tuning instead when:** the model is <1B parameters, you need maximum quality and have the compute, or a large domain shift requires updating all weights.

## Routing table

| To do X | Read |
|---------|------|
| Install PEFT, run a full LoRA or QLoRA training script, pick rank / alpha / target modules per architecture | [references/lora-and-qlora.md](references/lora-and-qlora.md) |
| Load a trained adapter, merge for deployment, serve or switch several adapters at runtime | [references/adapters-loading-serving.md](references/adapters-loading-serving.md) |
| Choose between LoRA, QLoRA, AdaLoRA, IA3, Prefix/Prompt Tuning, P-Tuning v2 | [references/method-catalog.md](references/method-catalog.md) |
| Use PEFT through TRL's SFTTrainer, an Axolotl YAML, or serve adapters with vLLM | [references/integrations.md](references/integrations.md) |
| Look up memory / throughput / MMLU numbers vs full fine-tuning | [references/benchmarks.md](references/benchmarks.md) |
| Use DoRA, LoftQ, rsLoRA, LoRA+, layer-pattern targeting, embedding/vocab training, weighted adapter composition, gradient checkpointing, GGUF/ONNX export, task-specific training recipes | [references/advanced-usage.md](references/advanced-usage.md) |
| Fix bitsandbytes/Triton install errors, OOM, NaN or flat loss, adapter-not-training, load failures, QLoRA merge failures, multi-adapter conflicts, slow generation | [references/troubleshooting.md](references/troubleshooting.md) |

## Key constraints and gotchas

- **Start at r=8-16 with `lora_alpha = 2 * r`**; raise rank only if quality is insufficient.
- **Target attention + MLP projections** (or `target_modules="all-linear"`, PEFT 0.6.0+) for the best quality/efficiency ratio. `target_modules` names are architecture-specific — Llama's `q_proj` does not exist in GPT-2 or Falcon.
- **QLoRA requires `prepare_model_for_kbit_training(model)`** after loading the quantized model, or gradients will not flow.
- **Use bf16, not fp16**, for QLoRA compute dtype; fp16 is the usual source of NaN loss.
- Always call `model.print_trainable_parameters()` — a 0% result means `target_modules` matched nothing.
- Save adapters frequently (they are megabytes, so rollback is cheap) and evaluate on held-out data **before** merging, since `merge_and_unload()` is not reversible in place.
- Enable gradient checkpointing for memory savings; use QLoRA for 70B+ on consumer hardware.

## End-to-end skeleton

```python
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import get_peft_model, LoraConfig, TaskType
from trl import SFTTrainer, SFTConfig

model = AutoModelForCausalLM.from_pretrained("meta-llama/Llama-3.1-8B", device_map="auto")
tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-3.1-8B")

lora_config = LoraConfig(
    task_type=TaskType.CAUSAL_LM,
    r=16, lora_alpha=32, lora_dropout=0.05,
    target_modules="all-linear", bias="none",
)
model = get_peft_model(model, lora_config)
model.print_trainable_parameters()   # expect ~0.1-1%

trainer = SFTTrainer(
    model=model,
    args=SFTConfig(output_dir="./lora-llama", num_train_epochs=3, learning_rate=2e-4),
    train_dataset=dataset,
)
trainer.train()
model.save_pretrained("./lora-llama-adapter")   # ~6MB, not 16GB
```

## Resources

- **GitHub**: https://github.com/huggingface/peft
- **Docs**: https://huggingface.co/docs/peft
- **LoRA Paper**: arXiv:2106.09685
- **QLoRA Paper**: arXiv:2305.14314
- **Models**: https://huggingface.co/models?library=peft
