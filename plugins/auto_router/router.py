import os
import re
import sys
import requests
from typing import Dict, List, Optional, Any

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Automatically load credentials from %LOCALAPPDATA%\hermes\.env and auth.json
def load_hermes_credentials():
    # 1. Read .env
    env_path = os.path.expandvars(r"%LOCALAPPDATA%\hermes\.env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val

    # 2. Read auth.json credential pool
    auth_path = os.path.expandvars(r"%LOCALAPPDATA%\hermes\auth.json")
    if os.path.exists(auth_path):
        try:
            import json
            with open(auth_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                pool = data.get("credential_pool", {})
                for provider, creds in pool.items():
                    for cred in creds:
                        label = cred.get("label")
                        val = cred.get("value") or cred.get("api_key") or os.getenv(label)
                        if label and val and label not in os.environ:
                            os.environ[label] = val
        except Exception:
            pass

load_hermes_credentials()

# ==========================================
# FREE MODEL REGISTRY BY TASK CATEGORY
# ==========================================
MODEL_REGISTRY = {
    "reasoning": [
        {"provider": "sambanova", "model": "deepseek-ai/DeepSeek-R1", "base_url": "https://api.sambanova.ai/v1", "env_key": "SAMBANOVA_API_KEY"},
        {"provider": "sambanova", "model": "Meta-Llama-3.3-70B-Instruct", "base_url": "https://api.sambanova.ai/v1", "env_key": "SAMBANOVA_API_KEY"},
        {"provider": "openrouter", "model": "nousresearch/hermes-3-llama-3.8b:free", "base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY"},
    ],
    "coding": [
        {"provider": "sambanova", "model": "Qwen2.5-Coder-32B-Instruct", "base_url": "https://api.sambanova.ai/v1", "env_key": "SAMBANOVA_API_KEY"},
        {"provider": "sambanova", "model": "Meta-Llama-3.3-70B-Instruct", "base_url": "https://api.sambanova.ai/v1", "env_key": "SAMBANOVA_API_KEY"},
        {"provider": "google", "model": "gemini-1.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "env_key": "GEMINI_API_KEY"},
        {"provider": "openrouter", "model": "nousresearch/hermes-3-llama-3.8b:free", "base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY"},
    ],
    "vision": [
        {"provider": "google", "model": "gemini-1.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "env_key": "GEMINI_API_KEY"},
        {"provider": "sambanova", "model": "Llama-3.2-11B-Vision-Instruct", "base_url": "https://api.sambanova.ai/v1", "env_key": "SAMBANOVA_API_KEY"},
        {"provider": "openrouter", "model": "meta-llama/llama-3.2-11b-vision-instruct:free", "base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY"},
    ],
    "general": [
        {"provider": "sambanova", "model": "Meta-Llama-3.3-70B-Instruct", "base_url": "https://api.sambanova.ai/v1", "env_key": "SAMBANOVA_API_KEY"},
        {"provider": "openrouter", "model": "nousresearch/hermes-3-llama-3.8b:free", "base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY"},
        {"provider": "google", "model": "gemini-1.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "env_key": "GEMINI_API_KEY"},
    ]
}

def detect_task_category(prompt: str, image_url: Optional[str] = None) -> str:
    """Classifies the prompt into task categories: vision, coding, reasoning, or general."""
    if image_url or re.search(r'http[s]?://\S+\.(?:jpg|png|jpeg|webp)', prompt, re.IGNORECASE):
        return "vision"
    
    # Coding signals
    code_keywords = [
        r'\bdef\b', r'\bclass\b', r'\bimport\b', r'\bfunction\b', r'\bconst\b', r'\blet\b',
        r'\bvar\b', r'\breturn\b', r'```', r'\bdebug\b', r'\brefactor\b', r'\bsql\b', r'\bapi\b',
        r'\bscript\b', r'\bpython\b', r'\bjavascript\b', r'\bhtml\b', r'\bcss\b', r'\bjson\b'
    ]
    if any(re.search(kw, prompt, re.IGNORECASE) for kw in code_keywords):
        return "coding"

    # Reasoning / Math / Logic signals
    reasoning_keywords = [
        r'\bthink step by step\b', r'\breason\b', r'\bprove\b', r'\bproof\b', r'\bmath\b',
        r'\balgorithm\b', r'\blogic\b', r'\bderive\b', r'\bcomplex analysis\b', r'\btheorem\b'
    ]
    if any(re.search(kw, prompt, re.IGNORECASE) for kw in reasoning_keywords):
        return "reasoning"

    return "general"

def chat_auto(prompt: str, image_url: Optional[str] = None, system_prompt: str = "You are a helpful AI assistant.") -> Dict[str, Any]:
    """Dynamically routes the prompt to the best available free model with automatic failover."""
    category = detect_task_category(prompt, image_url)
    models = MODEL_REGISTRY.get(category, MODEL_REGISTRY["general"])

    print(f"🎯 Task Intent Detected: '{category.upper()}'")

    for candidate in models:
        provider = candidate["provider"]
        model_name = candidate["model"]
        base_url = candidate["base_url"]
        api_key = os.getenv(candidate["env_key"]) or os.getenv("GOOGLE_API_KEY") if provider == "google" else os.getenv(candidate["env_key"])

        if not api_key:
            print(f"  [Skipped] {provider}/{model_name} — Missing env key '{candidate['env_key']}'")
            continue

        print(f"  🚀 Calling Provider: {provider.upper()} | Model: {model_name}...")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/hermes"
            headers["X-Title"] = "Hermes Auto Router"

        # Format payload
        messages = [{"role": "system", "content": system_prompt}]
        if image_url and category == "vision":
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            })
        else:
            messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.7
        }

        url = f"{base_url.rstrip('/')}/chat/completions"

        try:
            res = requests.post(url, json=payload, headers=headers, timeout=30)
            if res.status_code == 200:
                data = res.json()
                reply = data["choices"][0]["message"]["content"]
                print(f"  ✅ SUCCESS via {provider}/{model_name}\n")
                return {
                    "status": "success",
                    "provider": provider,
                    "model": model_name,
                    "category": category,
                    "reply": reply
                }
            else:
                print(f"  ❌ Failed ({res.status_code}): {res.text[:100]}")
        except Exception as e:
            print(f"  ⚠️ Error connecting to {provider}: {str(e)}")

    return {"status": "error", "message": "All free candidate models failed or require API keys."}

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # Called with an argument via CLI
        prompt = sys.argv[1]
        res = chat_auto(prompt)
        if isinstance(res, dict) and res.get("status") == "success":
            print(res["reply"])
        else:
            print(f"Error: {res.get('message', 'No response')}")
    else:
        test_prompts = [
            "Write a Python function to quicksort an array of integers.",
            "Solve this step-by-step: If 5 cats catch 5 mice in 5 minutes, how many cats are needed to catch 100 mice in 100 minutes?",
            "Tell me a short fun fact about space exploration."
        ]
        print("==================================================")
        print("        HERMES AUTOMATIC FREE MODEL ROUTER        ")
        print("==================================================\n")
        for p in test_prompts:
            print(f"Prompt: \"{p}\"")
            res = chat_auto(p)
            if isinstance(res, dict) and res.get("status") == "success":
                print(f"Response:\n{res['reply'][:150]}...\n")
            else:
                print(f"Result: {res.get('message', 'No response')}\n")
            print("-" * 50)
