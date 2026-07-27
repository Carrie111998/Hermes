"""Native slash command /image for generating images using pollinations.ai (no API required)."""

import os
import random
import re
import urllib.parse
import urllib.request
from typing import Optional

QUALITY_SUFFIX = (
    ", masterpiece, best quality, insanely detailed, ultra-realistic, "
    "hyper-detailed, cinematic lighting, 8k resolution, photorealistic, "
    "unreal engine 5, octane render, sharp focus, award-winning"
)

def _download_image(prompt: str, output_file: str) -> bool:
    enhance = True
    model = "flux"
    
    width = 1024
    height = 1024
    
    # Parse explicit width and height
    w_match = re.search(r'--width\s+(\d+)|-w\s+(\d+)', prompt, re.IGNORECASE)
    h_match = re.search(r'--height\s+(\d+)|-h\s+(\d+)', prompt, re.IGNORECASE)
    
    if w_match:
        width = int(w_match.group(1) or w_match.group(2))
    if h_match:
        height = int(h_match.group(1) or h_match.group(2))
        
    # Check for aspect ratio if exact dimensions aren't specified
    ar_match = re.search(r'--ar\s+([\d:]+)|--ratio\s+([\d:]+)|--aspect\s+([\d:]+)', prompt, re.IGNORECASE)
    
    if not (w_match or h_match):
        if ar_match:
            ar = ar_match.group(1) or ar_match.group(2) or ar_match.group(3)
            if ar == "16:9":
                width, height = 1920, 1080
            elif ar == "9:16":
                width, height = 1080, 1920
            elif ar == "4:3":
                width, height = 1440, 1080
            elif ar == "3:4":
                width, height = 1080, 1440
            elif ar == "21:9":
                width, height = 2560, 1080
            elif ar == "1:1":
                width, height = 1024, 1024
        elif "horizontal" in prompt.lower():
            width, height = 1920, 1080
        elif "vertical" in prompt.lower():
            width, height = 1080, 1920
        elif "square" in prompt.lower():
            width, height = 1024, 1024
            
    # Clean the prompt of those flags so they don't leak into the actual AI prompt
    clean_prompt = re.sub(r'--width\s+\d+|-w\s+\d+|--height\s+\d+|-h\s+\d+|--ar\s+[\d:]+|--ratio\s+[\d:]+|--aspect\s+[\d:]+', '', prompt, flags=re.IGNORECASE)
    # Remove standalone words if used as a generic ratio hint
    for word in ["horizontal", "vertical", "square"]:
        if word in clean_prompt.lower():
            # Only remove if it feels like a command, we don't want to remove "a square box"
            if f"--{word}" in clean_prompt.lower():
                clean_prompt = re.sub(fr'--{word}', '', clean_prompt, flags=re.IGNORECASE)
                
    clean_prompt = clean_prompt.strip(" ,-")
    if not clean_prompt:
        clean_prompt = prompt  # Fallback
    
    if enhance and not any(kw in clean_prompt.lower() for kw in ["8k", "hd", "detailed", "photorealistic"]):
        full_prompt = clean_prompt + QUALITY_SUFFIX
    else:
        full_prompt = clean_prompt

    seed = random.randint(1, 999999)
    encoded_prompt = urllib.parse.quote(full_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&model={model}&nologo=true&seed={seed}"
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=45) as response:
            if response.status == 200:
                with open(output_file, "wb") as f:
                    f.write(response.read())
                return True
    except Exception as e:
        print(f"Error downloading image: {e}")
    return False

def _handle_image(raw_args: str) -> Optional[str]:
    prompt = raw_args.strip()
    if not prompt:
        return "Usage: /image <prompt> [--width 1920] [--height 1080] [--ratio 16:9]"
    
    output_dir = os.path.expandvars(r"%LOCALAPPDATA%\hermes")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "generated_image.jpg")
    
    if os.path.exists(output_file):
        try:
            os.remove(output_file)
        except OSError:
            pass

    success = _download_image(prompt, output_file)
        
    if success and os.path.exists(output_file):
        return f"Generated image successfully:\n{output_file}"
    else:
        return "Failed to generate image. Please try again."

def register(ctx) -> None:
    ctx.register_command(
        "image",
        handler=_handle_image,
        description="Generate an HD image. Supports --width, --height, --ratio 16:9, or --horizontal.",
        args_hint="<prompt> [--ratio 16:9] [--width 1024]"
    )
