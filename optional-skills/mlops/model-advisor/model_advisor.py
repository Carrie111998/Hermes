#!/usr/bin/env python3
"""
model_advisor.py - Task-aware model router over FREE models, local-first.

Defaults to Ollama (http://localhost:11434/v1) — TRULY free and UNCAPPED, no
API key. Optionally route via OpenRouter free models with `--backend openrouter`
(those are $0/token but rate-limited per day).

Classifies your prompt by task type and routes the real work to the BEST model
for that job (not one fixed default). This is the "advisor"/model-switching-by-
need pattern, shipped as a portable script.

Classifier:
  * Default: local keyword heuristic (FREE, instant, 0 quota).
  * --llm-classify: use a small local/free model to tag the task (1 call).

Routing table (local models you have pulled):
  code     -> qwen2.5:3b
  creative -> qwen2.5:3b
  research -> qwen2.5:3b
  long     -> qwen2.5:3b
  chat     -> qwen2.5:0.5b

OpenRouter fallback table (when --backend openrouter):
  code     -> north-mini-code > nemotron-super > gpt-oss-20b
  creative -> nemotron-ultra > gemma-4-31b > ling-3.0-flash
  research -> ling-3.0-flash > nemotron-ultra > gemma-4-31b
  long     -> nemotron-ultra (1M ctx)
  chat     -> gemma-4-31b > gpt-oss-20b > nemotron-nano-30b

Usage:
  python model_advisor.py "prompt"                       # local Ollama
  python model_advisor.py --backend openrouter "prompt"
  python model_advisor.py --show-routes
  python model_advisor.py --queue-only "prompt"
  python model_advisor.py --run-queue
"""
import os, sys, json, time, argparse
import urllib.request as urllib_request
import urllib.error as urllib_error

HERE    = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(HERE, "advisor_queue.json")

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
LOCAL_MODELS = ["qwen2.5:3b", "qwen2.5:0.5b", "llama3.2-vision"]

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_CLASSIFIER = "openai/gpt-oss-20b:free"

ROUTES_LOCAL = {
    "code":     ["qwen2.5:3b"],
    "creative": ["qwen2.5:3b"],
    "research": ["qwen2.5:3b"],
    "long":     ["qwen2.5:3b"],
    "chat":     ["qwen2.5:0.5b"],
}
ROUTES_OR = {
    "code":     ["cohere/north-mini-code:free",
                 "nvidia/nemotron-3-super-120b-a12b:free",
                 "openai/gpt-oss-20b:free"],
    "creative": ["nvidia/nemotron-3-ultra-550b-a55b:free",
                 "google/gemma-4-31b-it:free",
                 "inclusionai/ling-3.0-flash:free"],
    "research": ["inclusionai/ling-3.0-flash:free",
                 "nvidia/nemotron-3-ultra-550b-a55b:free",
                 "google/gemma-4-31b-it:free"],
    "long":     ["nvidia/nemotron-3-ultra-550b-a55b:free"],
    "chat":     ["google/gemma-4-31b-it:free",
                 "openai/gpt-oss-20b:free",
                 "nvidia/nemotron-3-nano-30b-a3b:free"],
}
KW = {
    "code": ["code", "function", "bug", "python", "script", "api", "compile",
             "deploy", "debug", "regex", "sql", "javascript", "class ", "def ",
             "rust", "c++", "html", "css", "bash", "shell", "docker", "git",
             "endpoint", "json", "variable", "loop", "error", "exception"],
    "creative": ["write", "story", "poem", "novel", "song", "tagline", "character",
                 "narrative", "chapter", "prose", "dialogue", "lyrics", "book",
                 "fiction", "scene", "worldbuild", "vtuber", "aether"],
    "research": ["research", "analyze", "summar", "compare", "paper", "study",
                 "investigate", "source", "report", "explain", "why", "how does",
                 "literature", "benchmark", "find out"],
    "long": ["long", "document", "whole file", "entire", "large context", "100k",
             "summarize this book", "full text", "paste the", "long article"],
}

# active target globals
API = OLLAMA_URL
KEY = "ollama"
ROUTES = ROUTES_LOCAL
CLASSIFIER = LOCAL_MODELS[1]  # qwen2.5:0.5b for llm-classify locally

def hermes_home():
    if os.environ.get("HERMES_HOME"):
        return os.environ["HERMES_HOME"]
    if os.name == "nt":
        return os.path.expandvars(r"%APPDATA%\hermes")
    return os.path.expanduser("~/.hermes")

def load_or_key():
    p = os.path.join(hermes_home(), ".env")
    try:
        for line in open(p):
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get("OPENROUTER_API_KEY")

def load_target(backend):
    global API, KEY, ROUTES, CLASSIFIER
    if backend == "openrouter":
        API = OR_URL
        KEY = load_or_key()
        ROUTES = ROUTES_OR
        CLASSIFIER = OR_CLASSIFIER
        if not KEY:
            print("OPENROUTER_API_KEY not found (needed for --backend openrouter).",
                  file=sys.stderr); sys.exit(1)
    else:
        API = OLLAMA_URL
        KEY = "ollama"
        ROUTES = ROUTES_LOCAL
        CLASSIFIER = LOCAL_MODELS[1]

def _post(key, model, messages, max_tokens, timeout):
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.5}
    headers = {"Content-Type": "application/json"}
    if key and key != "ollama":
        headers["Authorization"] = f"Bearer {key}"
        headers["HTTP-Referer"] = "https://hermes-agent.local"
        headers["X-Title"] = "Model-Advisor"
    req = urllib_request.Request(API, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as r:
            return r.read().decode(), None
    except urllib_error.HTTPError as e:
        reset = e.headers.get("X-RateLimit-Reset") if hasattr(e, "headers") else None
        return None, (e.code, e.read().decode()[:400], reset)
    except Exception as e:
        return None, (0, str(e)[:160])

def probe_quota(key):
    raw, err = _post(key, OR_CLASSIFIER,
                     [{"role": "user", "content": "ping"}], 3, 30)
    if raw:
        return True, 0
    code, msg, reset_hdr = err
    reset = int(reset_hdr) if (reset_hdr and str(reset_hdr).isdigit()) else 0
    if not reset:
        try:
            j = json.loads(msg)
            for path in [
                lambda d: d["metadata"]["headers"]["X-RateLimit-Reset"],
                lambda d: d["error"]["metadata"]["headers"]["X-RateLimit-Reset"],
                lambda d: d["headers"]["X-RateLimit-Reset"],
            ]:
                try:
                    v = int(path(j))
                    if v:
                        reset = v
                        break
                except Exception:
                    pass
        except Exception:
            pass
    return False, reset

def classify_local(prompt):
    low = prompt.lower()
    scores = {k: sum(1 for w in words if w in low) for k, words in KW.items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "chat", "no strong signal -> default chat"
    return best, f"keywords matched: {scores[best]} hits in '{best}'"

def classify_llm(key, prompt):
    sys_p = ("Classify the user request into exactly ONE of these tags and reply "
             "with ONLY the tag word: code, creative, research, long, chat.")
    raw, err = _post(key, CLASSIFIER,
                     [{"role": "system", "content": sys_p},
                      {"role": "user", "content": prompt}], 5, 60)
    if raw:
        tag = json.loads(raw)["choices"][0]["message"]["content"].strip().lower()
        tag = tag.strip("`. ")
        if tag in ROUTES:
            return tag, "llm classifier"
    return "chat", f"llm classify failed ({err}) -> default chat"

def run_route(key, tag, prompt, tokens):
    for model in ROUTES[tag]:
        raw, err = _post(key, model, [{"role": "user", "content": prompt}],
                         tokens, 120)
        if raw:
            try:
                return model, json.loads(raw)["choices"][0]["message"]["content"].strip()
            except Exception:
                return model, raw
        else:
            print(f"  - {model} FAIL: {err[0]} {err[1][:80]}")
    return None, None

def queue_save(prompt):
    q = json.load(open(QUEUE_FILE)) if os.path.exists(QUEUE_FILE) else []
    q.append({"prompt": prompt, "ts": int(time.time())})
    json.dump(q, open(QUEUE_FILE, "w"), indent=2)

def queue_load():
    return json.load(open(QUEUE_FILE)) if os.path.exists(QUEUE_FILE) else []

def dispatch(key, prompt, args):
    if args.backend == "openrouter":
        avail, reset = probe_quota(key)
        if not avail:
            rt = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(reset/1000)) if reset else "unknown"
            print(f"[ADVISOR] OpenRouter free quota CAPPED. Resets: {rt}")
            print("[ADVISOR] Queued. Re-run with --run-queue after reset (or add $10 credit).")
            queue_save(prompt)
            return
    tag, why = (classify_llm(key, prompt) if args.llm_classify
                else classify_local(prompt))
    print(f"[ADVISOR] backend={args.backend} task='{tag}' ({why})")
    model, out = run_route(key, tag, prompt, args.tokens)
    if model:
        print(f"[ADVISOR] routed to: {model}\n")
        print(out)
    else:
        print("[ADVISOR] all models in route failed. Check Ollama is running.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--backend", choices=["ollama", "openrouter"], default="ollama")
    ap.add_argument("--llm-classify", action="store_true")
    ap.add_argument("--tokens", type=int, default=600)
    ap.add_argument("--show-routes", action="store_true")
    ap.add_argument("--queue-only", action="store_true")
    ap.add_argument("--run-queue", action="store_true")
    args = ap.parse_args()

    if args.show_routes:
        src = ROUTES_OR if args.backend == "openrouter" else ROUTES_LOCAL
        for k, v in src.items():
            print(f"{k:9} -> " + " > ".join(v))
        return

    load_target(args.backend)

    if args.run_queue:
        q = queue_load()
        if not q:
            print("Queue empty."); return
        print(f"Running {len(q)} queued prompt(s)...")
        for item in q:
            print(f"\n##### QUEUED: {item['prompt'][:60]}...")
            dispatch(KEY, item["prompt"], args)
        os.remove(QUEUE_FILE)
        return

    prompt = args.prompt or sys.stdin.read().strip()
    if not prompt:
        print("No prompt.", file=sys.stderr); sys.exit(1)
    if args.queue_only:
        queue_save(prompt)
        print(f"Queued. File: {QUEUE_FILE}")
        return
    dispatch(KEY, prompt, args)

if __name__ == "__main__":
    main()
