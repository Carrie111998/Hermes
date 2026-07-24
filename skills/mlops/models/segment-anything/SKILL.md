---
name: segment-anything-model
description: "SAM: zero-shot image segmentation via points, boxes, masks."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [segment-anything, transformers>=4.30.0, torch>=1.7.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Multimodal, Image Segmentation, Computer Vision, SAM, Zero-Shot]

---

# Segment Anything Model (SAM)

Meta AI's Segment Anything Model for zero-shot image segmentation via point, box
and mask prompts.

## When to use this skill

**Use SAM when:**
- Need to segment any object in images without task-specific training
- Building interactive annotation tools with point/box prompts
- Generating training data for other vision models
- Need zero-shot transfer to new image domains
- Building object detection/segmentation pipelines
- Processing medical, satellite, or domain-specific images

**Key features:**
- **Zero-shot segmentation**: Works on any image domain without fine-tuning
- **Flexible prompts**: Points, bounding boxes, or previous masks
- **Automatic segmentation**: Generate all object masks automatically
- **High quality**: Trained on 1.1 billion masks from 11 million images
- **Multiple model sizes**: ViT-B (fastest), ViT-L, ViT-H (most accurate)
- **ONNX export**: Deploy in browsers and edge devices

**Use alternatives instead:**
- **YOLO/Detectron2**: For real-time object detection with classes
- **Mask2Former**: For semantic/panoptic segmentation with categories
- **GroundingDINO + SAM**: For text-prompted segmentation
- **SAM 2**: For video segmentation tasks

## Red lines (non-negotiable)

- **SAM outputs masks, not classes.** There is no label head. If you need
  categories, pair it with a detector or a text-grounding model.
- **Checkpoint and model type must match.** `sam_vit_h_*.pth` only loads into
  `sam_model_registry["vit_h"]`. A mismatch is a load error, not a warning.
- **Checkpoint downloads are large and manual** on the native path: ViT-H 2.4 GB,
  ViT-L 1.2 GB, ViT-B 375 MB. Budget disk and time before starting; VRAM scales
  with the same ordering, so choose ViT-B first on constrained GPUs.
- **`set_image()` is the expensive call.** It runs the ViT image encoder. Call it
  once per image and reuse the embedding for every prompt — never inside a prompt
  loop.
- **Input must be RGB, 3-channel, `(x, y)` coordinates.** `cv2.imread` gives BGR;
  grayscale needs `COLOR_GRAY2RGB`; points are `(column, row)`, not numpy
  `(row, col)`. Each of these silently produces wrong masks rather than an error.
- **Box prompts are `[x1, y1, x2, y2]` but output `bbox` is `[x, y, w, h]`.**
- **Cost is GPU time per image.** Automatic mask generation scales quadratically
  with `points_per_side` (default 32 = 1024 forward passes of the decoder) —
  never run it at default density over a large corpus without measuring first.
- **Licence:** the code is MIT; the released SAM checkpoints are Apache-2.0, but
  derived checkpoints (MedSAM and other fine-tunes) carry their own terms —
  verify before commercial use.

## Minimal end-to-end skeleton

```python
import cv2
import numpy as np
from segment_anything import sam_model_registry, SamPredictor

# 1. Load a checkpoint whose type matches the registry key
sam = sam_model_registry["vit_b"](checkpoint="sam_vit_b_01ec64.pth")
sam.to(device="cuda")
predictor = SamPredictor(sam)

# 2. Encode the image ONCE (RGB, not BGR)
image = cv2.cvtColor(cv2.imread("image.jpg"), cv2.COLOR_BGR2RGB)
predictor.set_image(image)

# 3. Prompt cheaply, as many times as you like
masks, scores, logits = predictor.predict(
    point_coords=np.array([[500, 375]]),  # (x, y)
    point_labels=np.array([1]),           # 1 = foreground, 0 = background
    multimask_output=True                 # 3 candidates
)

# 4. Pick the best candidate
best_mask = masks[np.argmax(scores)]
```

For "segment everything with no prompts", replace steps 2-4 with
`SamAutomaticMaskGenerator(sam).generate(image)`.

## Routing table

| To do this | Read |
|------------|------|
| Install SAM, download checkpoints, run the first prediction, use the HuggingFace path | `references/installation.md` |
| Choose ViT-B/L/H, understand the architecture, look up prompt or auto-generator parameters | `references/model-variants.md` |
| Write prompts (points, boxes, combined, iterative), run automatic mask generation, batch prompts, read the mask/RLE output format | `references/prompting-api.md` |
| Copy an annotation tool, object cutout or medical-ROI scaffold | `references/code-templates.md` |
| Cut VRAM, speed up inference, order the tuning levers | `references/performance-tuning.md` |
| SAM 2 video, Grounded SAM text prompts, ONNX/TensorRT deployment, FastAPI or Gradio serving, fine-tuning, mask refinement, dataset generation | `references/advanced-usage.md` |
| Fix errors: CUDA unavailable, checkpoint mismatch, OOM, empty masks, jagged edges, ONNX export failures | `references/troubleshooting.md` |

## Resources

- **GitHub**: https://github.com/facebookresearch/segment-anything
- **Paper**: https://arxiv.org/abs/2304.02643
- **Demo**: https://segment-anything.com
- **SAM 2 (Video)**: https://github.com/facebookresearch/segment-anything-2
- **HuggingFace**: https://huggingface.co/facebook/sam-vit-huge
