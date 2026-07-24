# Loading, Merging and Serving Adapters

Purpose: how to load a trained adapter, merge it into the base model for deployment, and serve several adapters from one base at runtime.

## Load trained adapter

```python
from peft import PeftModel, AutoPeftModelForCausalLM
from transformers import AutoModelForCausalLM

# Option 1: Load with PeftModel
base_model = AutoModelForCausalLM.from_pretrained("meta-llama/Llama-3.1-8B")
model = PeftModel.from_pretrained(base_model, "./lora-llama-adapter")

# Option 2: Load directly (recommended)
model = AutoPeftModelForCausalLM.from_pretrained(
    "./lora-llama-adapter",
    device_map="auto"
)
```

## Merge adapter into base model

```python
# Merge for deployment (no adapter overhead)
merged_model = model.merge_and_unload()

# Save merged model
merged_model.save_pretrained("./llama-merged")
tokenizer.save_pretrained("./llama-merged")

# Push to Hub
merged_model.push_to_hub("username/llama-finetuned")
```

Merging is irreversible for that in-memory model — keep the adapter directory. Merging a
QLoRA adapter requires dequantizing the base first; see the "QLoRA Merge Fails" section of
[troubleshooting.md](troubleshooting.md).

## Multi-adapter serving

```python
from peft import PeftModel

# Load base with first adapter
model = AutoPeftModelForCausalLM.from_pretrained("./adapter-task1")

# Load additional adapters
model.load_adapter("./adapter-task2", adapter_name="task2")
model.load_adapter("./adapter-task3", adapter_name="task3")

# Switch between adapters at runtime
model.set_adapter("task1")  # Use task1 adapter
output1 = model.generate(**inputs)

model.set_adapter("task2")  # Switch to task2
output2 = model.generate(**inputs)

# Disable adapters (use base model)
with model.disable_adapter():
    base_output = model.generate(**inputs)
```

For weighted adapter composition (`add_weighted_adapter` with `linear` / `cat` / `svd`),
adapter stacking, ensemble generation, and batch inference with different adapters per
request, see [advanced-usage.md](advanced-usage.md).
