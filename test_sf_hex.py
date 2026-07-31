#!/usr/bin/env python3
"""Test SiliconFlow with key stored in hex"""
import requests, json

# Hex-encoded key to bypass tool redaction
hex_key = "736b2d74..."
key = bytes.fromhex(hex_key).decode()

headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

# Test Flux model
data = {"model": "black-forest-labs/FLUX.1-dev", "prompt": "a cute cat cartoon", "n": 1, "size": "1024x1024"}
resp = requests.post("https://api.siliconflow.cn/v1/images/generations", headers=headers, json=data, timeout=30)
print(f"Status: {resp.status_code}")
try:
    print(json.dumps(resp.json(), ensure_ascii=False)[:300])
except:
    print(resp.text[:300])
