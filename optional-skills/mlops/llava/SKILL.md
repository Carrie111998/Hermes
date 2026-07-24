---
name: llava
description: Large Language and Vision Assistant. Enables visual instruction tuning and image-based conversations. Combines CLIP vision encoder with Vicuna/LLaMA language models. Supports multi-turn image chat, visual question answering, and instruction following. Use for vision-language chatbots or image understanding tasks. Best for conversational image analysis.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [transformers, torch, pillow]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [LLaVA, Vision-Language, Multimodal, Visual Question Answering, Image Chat, CLIP, Vicuna, Conversational AI, Instruction Tuning, VQA]

---

# LLaVA - Large Language and Vision Assistant

Open-source vision-language model (CLIP ViT vision encoder + Vicuna/LLaMA) for conversational image understanding. Apache 2.0, 7B-34B variants.

## When to use LLaVA

**Use when:**
- Building vision-language chatbots
- Visual question answering (VQA)
- Image description and captioning
- Multi-turn image conversations
- Visual instruction following
- Document understanding with images

**Use alternatives instead:**
- **GPT-4V class hosted APIs**: Highest quality, no GPU to manage
- **CLIP**: Simple zero-shot classification / retrieval, no dialogue
- **BLIP-2**: Better for captioning only
- **Flamingo**: Research, not open-source

## Install and minimal end-to-end skeleton

```bash
git clone https://github.com/haotian-liu/LLaVA
cd LLaVA
pip install -e .
```

> **Path note**: LLaVA is used as a cloned upstream repo. Anything like `scripts/v1_5/*.sh` or `python -m llava.serve.*` refers to files **inside that clone**, not files shipped in this skill directory. Run them from the repo root.

```python
from llava.model.builder import load_pretrained_model
from llava.mm_utils import get_model_name_from_path, process_images, tokenizer_image_token
from llava.constants import IMAGE_TOKEN_INDEX, DEFAULT_IMAGE_TOKEN
from llava.conversation import conv_templates
from PIL import Image
import torch

model_path = "liuhaotian/llava-v1.5-7b"
tokenizer, model, image_processor, context_len = load_pretrained_model(
    model_path=model_path,
    model_base=None,
    model_name=get_model_name_from_path(model_path),
)

image = Image.open("image.jpg")
image_tensor = process_images([image], image_processor, model.config)
image_tensor = image_tensor.to(model.device, dtype=torch.float16)

conv = conv_templates["llava_v1"].copy()
conv.append_message(conv.roles[0], DEFAULT_IMAGE_TOKEN + "\nWhat is in this image?")
conv.append_message(conv.roles[1], None)
prompt = conv.get_prompt()

input_ids = tokenizer_image_token(
    prompt, tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt"
).unsqueeze(0).to(model.device)

with torch.inference_mode():
    output_ids = model.generate(
        input_ids, images=image_tensor,
        do_sample=True, temperature=0.2, max_new_tokens=512,
    )

print(tokenizer.decode(output_ids[0], skip_special_tokens=True).strip())
```

## Where to read more

| To do this | Read |
|------------|------|
| Pick a 7B/13B/34B variant against VRAM, enable 4-bit/8-bit quantization | [references/inference.md](references/inference.md) |
| Serve via CLI or the Gradio web UI | [references/inference.md](references/inference.md) |
| Hold a correct multi-turn conversation (writing replies back into `conv`) | [references/inference.md](references/inference.md) |
| Prompt for captioning / VQA / object listing / scene or document understanding | [references/inference.md](references/inference.md) |
| Read benchmarks, throughput numbers and known limitations | [references/inference.md](references/inference.md) |
| Wire LLaVA into LangChain or a Gradio ChatInterface | [references/inference.md](references/inference.md) |
| Run stage-1 feature alignment and stage-2 visual instruction tuning, prepare instruction data, LoRA fine-tune, size hardware | [references/training.md](references/training.md) |

## Key constraints

- **`DEFAULT_IMAGE_TOKEN` must appear in the prompt**, and input ids must be built with `tokenizer_image_token` — plain tokenization silently drops the image.
- **The image tensor dtype must match the model** (`torch.float16` for the standard checkpoints).
- **GPU required.** CPU inference is impractically slow.
- **Multi-turn requires writing the previous reply into `conv.messages[-1][1]`** before appending the next user turn, otherwise context is lost.
- **Hallucination and weak spatial/counting ability** are known failure modes — do not treat descriptions as ground truth.
- **4-bit quantization cuts VRAM ~4x** (13B: ~28 GB -> ~8 GB) at some quality cost.
