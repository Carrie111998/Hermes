# LLaVA Inference Guide

Model variants, CLI/Gradio serving, multi-turn conversation handling, quantization, task prompts, benchmarks and limitations — moved out of SKILL.md.

## Available models

| Model | Parameters | VRAM | Quality |
|-------|------------|------|---------|
| LLaVA-v1.5-7B | 7B | ~14 GB | Good |
| LLaVA-v1.5-13B | 13B | ~28 GB | Better |
| LLaVA-v1.6-34B | 34B | ~70 GB | Best |

```python
# Load different models
model_7b = "liuhaotian/llava-v1.5-7b"
model_13b = "liuhaotian/llava-v1.5-13b"
model_34b = "liuhaotian/llava-v1.6-34b"

# 4-bit quantization for lower VRAM
load_4bit = True  # Reduces VRAM by ~4x
```

## CLI usage

> These `python -m llava.serve.*` entry points come from the cloned upstream LLaVA package, so run them in the environment where you did `pip install -e .`.

```bash
# Single image query
python -m llava.serve.cli \
    --model-path liuhaotian/llava-v1.5-7b \
    --image-file image.jpg \
    --query "What is in this image?"

# Multi-turn conversation
python -m llava.serve.cli \
    --model-path liuhaotian/llava-v1.5-7b \
    --image-file image.jpg
# Then type questions interactively
```

## Web UI (Gradio)

```bash
# Launch Gradio interface
python -m llava.serve.gradio_web_server \
    --model-path liuhaotian/llava-v1.5-7b \
    --load-4bit  # Optional: reduce VRAM

# Access at http://localhost:7860
```

## Multi-turn conversations

The previous assistant reply must be written back into `conv.messages[-1][1]` before appending the next user turn.

```python
# Initialize conversation
conv = conv_templates["llava_v1"].copy()

# Turn 1
conv.append_message(conv.roles[0], DEFAULT_IMAGE_TOKEN + "\nWhat is in this image?")
conv.append_message(conv.roles[1], None)
response1 = generate(conv, model, image)  # "A dog playing in a park"

# Turn 2
conv.messages[-1][1] = response1  # Add previous response
conv.append_message(conv.roles[0], "What breed is the dog?")
conv.append_message(conv.roles[1], None)
response2 = generate(conv, model, image)  # "Golden Retriever"

# Turn 3
conv.messages[-1][1] = response2
conv.append_message(conv.roles[0], "What time of day is it?")
conv.append_message(conv.roles[1], None)
response3 = generate(conv, model, image)
```

## Common tasks

### Image captioning

```python
question = "Describe this image in detail."
response = ask(model, image, question)
```

### Visual question answering

```python
question = "How many people are in the image?"
response = ask(model, image, question)
```

### Object detection (textual)

```python
question = "List all the objects you can see in this image."
response = ask(model, image, question)
```

### Scene understanding

```python
question = "What is happening in this scene?"
response = ask(model, image, question)
```

### Document understanding

```python
question = "What is the main topic of this document?"
response = ask(model, document_image, question)
```

## Quantization (reduce VRAM)

```python
# 4-bit quantization
tokenizer, model, image_processor, context_len = load_pretrained_model(
    model_path="liuhaotian/llava-v1.5-13b",
    model_base=None,
    model_name=get_model_name_from_path("liuhaotian/llava-v1.5-13b"),
    load_4bit=True  # Reduces VRAM ~4x
)

# 8-bit quantization
load_8bit=True  # Reduces VRAM ~2x
```

## Best practices

1. **Start with 7B model** - Good quality, manageable VRAM
2. **Use 4-bit quantization** - Reduces VRAM significantly
3. **GPU required** - CPU inference extremely slow
4. **Clear prompts** - Specific questions get better answers
5. **Multi-turn conversations** - Maintain conversation context
6. **Temperature 0.2-0.7** - Balance creativity/consistency
7. **max_new_tokens 512-1024** - For detailed responses
8. **Batch processing** - Process multiple images sequentially

## Performance

| Model | VRAM (FP16) | VRAM (4-bit) | Speed (tokens/s) |
|-------|-------------|--------------|------------------|
| 7B | ~14 GB | ~4 GB | ~20 |
| 13B | ~28 GB | ~8 GB | ~12 |
| 34B | ~70 GB | ~18 GB | ~5 |

*On A100 GPU*

## Benchmarks

LLaVA achieves competitive scores on:
- **VQAv2**: 78.5%
- **GQA**: 62.0%
- **MM-Vet**: 35.4%
- **MMBench**: 64.3%

## Limitations

1. **Hallucinations** - May describe things not in image
2. **Spatial reasoning** - Struggles with precise locations
3. **Small text** - Difficulty reading fine print
4. **Object counting** - Imprecise for many objects
5. **VRAM requirements** - Need powerful GPU
6. **Inference speed** - Slower than CLIP

## Integration with frameworks

### LangChain

```python
from langchain.llms.base import LLM

class LLaVALLM(LLM):
    def _call(self, prompt, stop=None):
        # Custom LLaVA inference
        return response

llm = LLaVALLM()
```

### Gradio App

```python
import gradio as gr

def chat(image, text, history):
    response = ask_llava(model, image, text)
    return response

demo = gr.ChatInterface(
    chat,
    additional_inputs=[gr.Image(type="pil")],
    title="LLaVA Chat"
)
demo.launch()
```

## Resources

- **GitHub**: https://github.com/haotian-liu/LLaVA
- **Paper**: https://arxiv.org/abs/2304.08485
- **Demo**: https://llava.hliu.cc
- **Models**: https://huggingface.co/liuhaotian
- **License**: Apache 2.0
