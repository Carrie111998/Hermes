#!/usr/bin/env python3
"""
moa_free.py - Quota-aware Mixture of Agents over FREE OpenRouter chat models.

Fans a prompt across several FREE OpenRouter chat models in parallel, then
synthesizes their outputs into one final answer with a free aggregator.

WHY QUOTA-AWARE: OpenRouter enforces a DAILY free-model request cap. A naive
"call all 12 free models" burns the entire day's quota on ONE task and 429s
instantly. This script runs a SMALL strength-ordered proposer set (default 3)
to conserve the daily budget, probes quota first, and QUEUES the prompt if
capped instead of failing silently.

Portable: reads OPENROUTER_API_KEY from the Hermes .env (HERMES_HOME, then
%APPDATA%/hermes on Windows, then ~/.hermes on POSIX). Queue file lives next to
this script. Stdlib only — no pip needed.

Usage:
  python moa_all.py "prompt"
  echo "prompt" | python moa_all.py
  python moa_all.py --list
  python moa_all.py --proposers 4 "prompt"
  python moa_all.py --queue-only "prompt"
  python moa_all.py --run-queue
"""
import os, sys, json, time, argparse, concurrent.futures as cf
import urllib.request as urllib_request
import urllib.error as urllib_error

API    = "https://openrouter.ai/api/v1/chat/completions"
HERE   = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(HERE, "moa_queue.json")

# strength-ordered, chat-capable free models (excludes audio/safety/vision-only)
PROPOSERS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",   # strongest, 1M ctx
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash:free",
    "openai/gpt-oss-20b:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
]
AGGREGATOR = "nvidia/nemotron-3-ultra-550b-a55b:free"

def hermes_home():
    if os.environ.get("HERMES_HOME"):
        return os.environ["HERMES_HOME"]
    if os.name == "nt":
        return os.path.expandvars(r"%APPDATA%\hermes")
    return os.path.expanduser("~/.hermes")

def load_key():
    p = os.path.join(hermes_home(), ".env")
    try:
        for line in open(p):
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get("OPENROUTER_API_KEY")

def _post(key, model, messages, max_tokens, timeout):
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.7}
    req = urllib_request.Request(API, data=json.dumps(body).encode(),
          headers={"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json",
                   "HTTP-Referer": "https://hermes-agent.local",
                   "X-Title": "MOA-Free"})
    try:
        with urllib_request.urlopen(req, timeout=timeout) as r:
            return r.read().decode(), None
    except urllib_error.HTTPError as e:
        reset = e.headers.get("X-RateLimit-Reset") if hasattr(e, "headers") else None
        return None, (e.code, e.read().decode()[:400], reset)
    except Exception as e:
        return None, (0, str(e)[:160])

def probe_quota(key):
    """Return (available:bool, reset_ts_ms:int)."""
    raw, err = _post(key, "openai/gpt-oss-20b:free",
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

def propose(key, model, prompt, tokens, timeout):
    raw, err = _post(key, model, [{"role": "user", "content": prompt}], tokens, timeout)
    if raw:
        try:
            return model, json.loads(raw)["choices"][0]["message"]["content"].strip(), None
        except Exception:
            return model, None, "bad json"
    return model, None, f"{err[0]}: {err[1]}"

def aggregate(key, agg, proposals, prompt, tokens):
    parts = [f"=== {m} ===\n{t}" for m, t in proposals]
    sys_p = ("You are a synthesis aggregator. Combine the independent proposals below "
             "into ONE coherent, high-quality final answer. Keep the best reasoning and "
             "content; resolve contradictions; drop repetition. Do not name the models.")
    user = f"REQUEST:\n{prompt}\n\nPROPOSALS:\n" + "\n\n".join(parts)
    raw, err = _post(key, agg,
                     [{"role": "system", "content": sys_p},
                      {"role": "user", "content": user}], tokens, 120)
    if raw:
        try:
            return json.loads(raw)["choices"][0]["message"]["content"].strip(), None
        except Exception:
            return None, "bad json"
    return None, f"{err[0]}: {err[1]}"

def queue_save(prompt):
    q = json.load(open(QUEUE_FILE)) if os.path.exists(QUEUE_FILE) else []
    q.append({"prompt": prompt, "ts": int(time.time())})
    json.dump(q, open(QUEUE_FILE, "w"), indent=2)

def queue_load():
    return json.load(open(QUEUE_FILE)) if os.path.exists(QUEUE_FILE) else []

def run_moa(key, prompt, args):
    avail, reset = probe_quota(key)
    if not avail:
        rt = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(reset/1000)) if reset else "unknown"
        print(f"[MOA] OpenRouter free quota CAPPED. Resets: {rt}")
        print("[MOA] Queueing prompt. Re-run with --run-queue after reset "
              "(or add $10 credit to OpenRouter to lift the daily cap).")
        queue_save(prompt)
        return
    # remaining not exposed reliably; conservatively use up to (proposers) + 1 for agg
    n = min(args.proposers, len(PROPOSERS))
    chosen = PROPOSERS[:n]
    print(f"[MOA] quota OK. Running {n} proposers (conserving daily free budget).")
    t0 = time.time()
    results = []
    with cf.ThreadPoolExecutor(max_workers=min(5, n)) as ex:
        futs = {ex.submit(propose, key, m, prompt, args.prop_tokens, 60): m
                for m in chosen}
        for f in cf.as_completed(futs):
            m, out, err = f.result()
            if out:
                results.append((m, out)); print(f"  + {m} ({len(out)}c)")
            else:
                print(f"  - {m} FAIL: {err}")
    print(f"[MOA] {len(results)}/{n} proposers in {time.time()-t0:.1f}s")
    if not results:
        print("[MOA] no proposer succeeded."); return
    out, err = aggregate(key, args.aggregator, results, prompt, args.agg_tokens)
    print("\n===== MOA FINAL =====")
    if out:
        print(out)
    else:
        print(f"[aggregator capped: {err}] -> best single proposer:\n")
        print(results[0][1])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--proposers", type=int, default=3)
    ap.add_argument("--prop-tokens", type=int, default=160)
    ap.add_argument("--agg-tokens", type=int, default=400)
    ap.add_argument("--aggregator", default=AGGREGATOR)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--queue-only", action="store_true")
    ap.add_argument("--run-queue", action="store_true")
    args = ap.parse_args()

    if args.list:
        print(f"Strength-ordered free chat proposers ({len(PROPOSERS)}):")
        for i, m in enumerate(PROPOSERS, 1):
            print(f"  {i}. {m}")
        print(f"\nAggregator: {AGGREGATOR}")
        return

    key = load_key()
    if not key:
        print("OPENROUTER_API_KEY not found in Hermes .env or env.", file=sys.stderr)
        sys.exit(1)

    if args.run_queue:
        q = queue_load()
        if not q:
            print("Queue empty."); return
        print(f"Running {len(q)} queued prompt(s)...")
        for item in q:
            print(f"\n##### QUEUED: {item['prompt'][:60]}...")
            run_moa(key, item["prompt"], args)
        os.remove(QUEUE_FILE)
        return

    prompt = args.prompt or sys.stdin.read().strip()
    if not prompt:
        print("No prompt.", file=sys.stderr); sys.exit(1)
    if args.queue_only:
        queue_save(prompt)
        print(f"Queued. File: {QUEUE_FILE}")
        return
    run_moa(key, prompt, args)

if __name__ == "__main__":
    main()
