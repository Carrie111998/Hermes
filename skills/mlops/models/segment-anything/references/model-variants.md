# SAM Model Variants and Prompt Types

## Model architecture

<!-- ascii-guard-ignore -->
```
SAM Architecture:
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Image Encoder  │────▶│ Prompt Encoder  │────▶│  Mask Decoder   │
│     (ViT)       │     │ (Points/Boxes)  │     │ (Transformer)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │                       │
   Image Embeddings      Prompt Embeddings         Masks + IoU
   (computed once)       (per prompt)             predictions
```
<!-- ascii-guard-ignore-end -->

The cost asymmetry matters: the image encoder is the expensive part and runs once
per `set_image()`; the prompt encoder and mask decoder are cheap and can be
re-run many times on the same cached embedding.

## Model variants

| Model | Checkpoint | Size | Speed | Accuracy |
|-------|------------|------|-------|----------|
| ViT-H | `vit_h` | 2.4 GB | Slowest | Best |
| ViT-L | `vit_l` | 1.2 GB | Medium | Good |
| ViT-B | `vit_b` | 375 MB | Fastest | Good |

Registry keys are the `Checkpoint` column values:
`sam_model_registry["vit_b"](checkpoint="sam_vit_b_01ec64.pth")`.
Download URLs are in `installation.md`.

## Prompt types

| Prompt | Description | Use Case |
|--------|-------------|----------|
| Point (foreground) | Click on object | Single object selection |
| Point (background) | Click outside object | Exclude regions |
| Bounding box | Rectangle around object | Larger objects |
| Previous mask | Low-res mask input | Iterative refinement |

Prompts compose: box + points is the most reliable combination for objects with
ambiguous extent. `multimask_output=True` returns three candidate masks with
scores (use it when the prompt is ambiguous); set it to `False` once the prompt
uniquely identifies the object.

## `SamAutomaticMaskGenerator` knobs

| Parameter | Default | Effect |
|-----------|---------|--------|
| `points_per_side` | 32 | Grid density; more = more masks, slower |
| `pred_iou_thresh` | 0.88 | Drops low predicted-quality masks |
| `stability_score_thresh` | 0.95 | Drops masks unstable under threshold shifts |
| `crop_n_layers` | 0 | Multi-scale crops; raises small-object recall |
| `crop_n_points_downscale_factor` | 1 | Point-grid downscaling per crop layer |
| `min_mask_region_area` | 0 | Removes tiny disconnected regions (needs `pycocotools`) |
