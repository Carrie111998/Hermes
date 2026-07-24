# LoRA Adapters

Loading, scaling, combining and unloading fine-tuned LoRA style/character adapters at inference time.

## Load a single LoRA

```python
from diffusers import DiffusionPipeline

pipe = DiffusionPipeline.from_pretrained(
    "stable-diffusion-v1-5/stable-diffusion-v1-5",
    torch_dtype=torch.float16
).to("cuda")

# Load LoRA weights
pipe.load_lora_weights("path/to/lora", weight_name="style.safetensors")

# Generate with LoRA style
image = pipe("A portrait in the trained style").images[0]

# Adjust LoRA strength
pipe.fuse_lora(lora_scale=0.8)

# Unload LoRA
pipe.unload_lora_weights()
```

`fuse_lora` bakes the weights into the base model (faster, but you must reload the
pipeline to undo it beyond `unload_lora_weights`).

## Multiple LoRAs

```python
# Load multiple LoRAs
pipe.load_lora_weights("lora1", adapter_name="style")
pipe.load_lora_weights("lora2", adapter_name="character")

# Set weights for each
pipe.set_adapters(["style", "character"], adapter_weights=[0.7, 0.5])

image = pipe("A portrait").images[0]
```

Name each adapter when loading more than one, otherwise the second load overwrites
the first. Combined weights well above ~1.0 in total tend to produce artifacts.

Many LoRAs also require a trigger token in the prompt — check the adapter's model card.

If a LoRA silently does nothing or two LoRAs fight each other, see
`troubleshooting.md` (LoRA Issues). For training your own LoRA, DreamBooth or
textual inversion embeddings, see `advanced-usage.md`.
