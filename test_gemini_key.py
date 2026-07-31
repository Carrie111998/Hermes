#!/usr/bin/env python3
"""Add Gemini key to env and test it."""
import os

env_path = os.path.expanduser('~/AppData/Local/hermes/.env')
with open(env_path, 'r') as f:
    lines = f.readlines()

# Remove old GEMINI/GOOGLE entries
new_lines = [l for l in lines if 'GEMINI_API_KEY' not in l and 'GOOGLE_API_KEY' not in l]

# Add new entries
key = "***"
new_lines.append(f'\n# Gemini API Key (added for image generation)\n')
new_lines.append(f'GEMINI_API_KEY=***       new_lines.append(f'GOOGLE_API_KEY=***# Save
with open(env_path, 'w') as f:
    f.writelines(new_lines)

print('Added Gemini API key to env file')

# Now test it
import requests, json
resp = requests.post(
    f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}',
    headers={'Content-Type': 'application/json'},
    json={'contents': [{'parts': [{'text': 'say OK'}]}]},
    timeout=15
)
print(f'Status: {resp.status_code}')
if resp.status_code == 200:
    print('✅ API key works! Full access.')
    text = resp.json()['candidates'][0]['content']['parts'][0]['text']
    print(f'Response: {text}')
elif resp.status_code == 429:
    print('⚠️ Key valid but quota exceeded. Need billing setup.')
    print(f'Details: {resp.text[:200]}')
else:
    print(f'❌ Key invalid or error: {resp.text[:200]}')

# Now test Imagen - try the correct model name
models_to_try = [
    'gemini-2.0-flash-exp-image-generation',
    'imagen-3.0-generate-001',
]
for model in models_to_try:
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
    resp2 = requests.post(
        url,
        headers={'Content-Type': 'application/json'},
        json={
            'contents': [{'parts': [{'text': 'Generate a simple comic illustration of a tree'}]}],
            'generationConfig': {'response_modalities': ['Text', 'Image']}
        },
        timeout=15
    )
    print(f'\nModel {model}: {resp2.status_code}')
    if resp2.status_code == 200:
        result = resp2.json()
        parts = result.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        for p in parts:
            if 'inlineData' in p:
                print(f'  ✅ IMAGE generated! MIME: {p["inlineData"]["mimeType"]}, size: {len(p["inlineData"]["data"])} bytes')
            elif 'text' in p:
                print(f'  Text: {p["text"][:80]}')
    elif resp2.status_code == 404:
        print(f'  Model not available: {resp2.text[:100]}')
    else:
        print(f'  Other error: {resp2.status_code}: {resp2.text[:200]}')
