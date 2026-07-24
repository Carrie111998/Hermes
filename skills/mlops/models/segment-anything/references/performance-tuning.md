# SAM Performance and Memory Tuning

Deeper material — TensorRT acceleration, memory-efficient context managers,
parallel automatic mask generation — is in `advanced-usage.md`. Error-driven
diagnosis is in `troubleshooting.md`.

## GPU memory

```python
# Use smaller model for limited VRAM
sam = sam_model_registry["vit_b"](checkpoint="sam_vit_b_01ec64.pth")

# Process images in batches
# Clear CUDA cache between large batches
torch.cuda.empty_cache()
```

Peak memory is dominated by the image encoder, which scales with input
resolution (SAM resizes the long side to 1024). Downscaling very large images
before `set_image()` is usually the cheapest fix after switching to ViT-B.

## Speed

```python
# Use half precision
sam = sam.half()

# Reduce points for automatic generation
mask_generator = SamAutomaticMaskGenerator(
    model=sam,
    points_per_side=16,  # Default is 32
)

# Use ONNX for deployment
# Export with --return-single-mask for faster inference
```

`points_per_side` cost is quadratic: 32 → 16 cuts the prompt grid from 1024 to
256 points. Combine with `crop_n_layers=0` when small-object recall is not needed.

## Ordered tuning levers

1. Cache image embeddings — call `set_image()` once per image, never per prompt.
2. Switch ViT-H → ViT-B (2.4 GB → 375 MB, largest single win).
3. FP16 (`sam.half()`).
4. Lower `points_per_side` for automatic generation.
5. Export to ONNX with `--return-single-mask` for the decoder-only serving path
   (see the ONNX section of `advanced-usage.md`).
