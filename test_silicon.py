#!/usr/bin/env python3
"""Test which SiliconFlow image models are available."""
import requests, json, os

# Read key from env 
env_path = os.path.expanduser('~/AppData/Local/hermes/.env')
key = None
with open(env_path) as f:
    for line in f:
        if 'SILICONFLOW' in line and '=' in line:
            key = line.split('=', 1)[1].strip().strip("'").strip('"')
            break

if not key:
    # Try from the raw input
    print("Key not found in env")
    exit(1)

print(f'Key: {key[:10]}...{key[-4:]}')
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

models = [
    "black-forest-labs/FLUX.1-dev",
    "black-forest-labs/FLUX.1-schnell",
    "stabilityai/stable-diffusion-3-5-large",
    "Pro/FLUX.1-schnell",
    "Pro/FLUX.1-dev",
    "Kwai-Kolors/Kolors-diffusers",
    "stabilityai/sdxl-turbo",
]

for model in models:
    data = {"model": model, "prompt": "a cute cartoon cat, white background", "n": 1, "size": "1024x1024"}
    try:
        resp = requests.post("https://api.siliconflow.cn/v1/images/generations", 
                           headers=headers, json=data, timeout=15)
        result = resp.json()
        code = result.get("code", "200")
        msg = result.get("message", "OK")
        status = "✅ OK" if resp.status_code == 200 else f"❌ {code}: {msg}"
        print(f"  {model:<45} → {status}")
        if resp.status_code == 200 and "data" in result:
            print(f"     Image URL: {result['data'][0]['url'][:60]}")
    except Exception as e:
        print(f"  {model:<45} → Error: {e}")
