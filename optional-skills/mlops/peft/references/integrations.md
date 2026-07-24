# PEFT Integrations

Purpose: passing a PEFT config through TRL, Axolotl, and vLLM instead of driving `get_peft_model` yourself.

## With TRL (SFTTrainer)

```python
from trl import SFTTrainer, SFTConfig
from peft import LoraConfig

lora_config = LoraConfig(r=16, lora_alpha=32, target_modules="all-linear")

trainer = SFTTrainer(
    model=model,
    args=SFTConfig(output_dir="./output", max_seq_length=512),
    train_dataset=dataset,
    peft_config=lora_config,  # Pass LoRA config directly
)
trainer.train()
```

The same `peft_config=` argument works for TRL's DPOTrainer and GRPOTrainer.

## With Axolotl (YAML config)

```yaml
# axolotl config.yaml
adapter: lora
lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_modules:
  - q_proj
  - v_proj
  - k_proj
  - o_proj
lora_target_linear: true  # Target all linear layers
```

## With vLLM (inference)

```python
from vllm import LLM
from vllm.lora.request import LoRARequest

# Load base model with LoRA support
llm = LLM(model="meta-llama/Llama-3.1-8B", enable_lora=True)

# Serve with adapter
outputs = llm.generate(
    prompts,
    lora_request=LoRARequest("adapter1", 1, "./lora-adapter")
)
```

Serving unmerged adapters via vLLM keeps one base model in memory for many tasks; merging
(see [adapters-loading-serving.md](adapters-loading-serving.md)) removes per-request adapter
overhead but costs one full model copy per variant.
