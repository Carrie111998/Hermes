# Hybrid Routing — Beginner's Tutorial

**Send simple questions to a fast, free model running on your own computer, and
hard questions to a powerful cloud model — automatically.**

This guide assumes **zero prior knowledge**. If you can copy-paste commands into
a terminal, you can do this.

---

## Table of contents

1. [What is hybrid routing?](#1-what-is-hybrid-routing)
2. [Why would I want it?](#2-why-would-i-want-it)
3. [How it decides (in plain English)](#3-how-it-decides-in-plain-english)
4. [What you need before you start](#4-what-you-need-before-you-start)
5. [Step 1 — Run a local model](#5-step-1--run-a-local-model)
6. [Step 2 — Tell Hermes about your local model](#6-step-2--tell-hermes-about-your-local-model)
7. [Step 3 — Start Hermes and check the setup](#7-step-3--start-hermes-and-check-the-setup)
8. [Step 4 — Try it out](#8-step-4--try-it-out)
9. [The commands you'll use](#9-the-commands-youll-use)
10. [How to *prove* it's actually working](#10-how-to-prove-its-actually-working)
11. [Tuning the behaviour](#11-tuning-the-behaviour)
12. [Troubleshooting](#12-troubleshooting)
13. [FAQ](#13-faq)

---

## 1. What is hybrid routing?

Hermes normally talks to **one** AI model — for example a cloud model like
Claude or GPT. Every message you send goes to that one model, and (if it's a
paid cloud model) every message costs money.

**Hybrid routing** puts a small "traffic cop" in front of that. For each message
you type, the traffic cop makes a quick decision:

- **Simple message?** → send it to a **local** model (running on *your* machine —
  free and private).
- **Complex message?** → send it to your usual **cloud** model (smart and
  powerful, but costs money / needs internet).

You don't have to do anything — it happens automatically on every turn. You can
also override it manually whenever you want.

```
                 ┌─────────────────────┐
   You type  ──► │   Traffic cop        │
   a message     │  (looks at message)  │
                 └──────────┬───────────┘
                            │
             simple? ───────┴─────── complex?
                │                        │
                ▼                        ▼
        ┌───────────────┐        ┌────────────────┐
        │  LOCAL model  │        │  CLOUD model    │
        │  (free, fast, │        │  (smart, paid,  │
        │   private)    │        │   needs net)    │
        └───────────────┘        └────────────────┘
```

---

## 2. Why would I want it?

- **Save money.** Small talk, quick lookups, and simple edits go to the free
  local model instead of burning cloud credits.
- **Faster for easy stuff.** A local model answers instantly with no network
  round-trip.
- **Privacy.** Simple prompts never leave your computer.
- **Keep the power when you need it.** Hard tasks (refactoring, debugging,
  analysis, anything with images) still go to the strong cloud model.

---

## 3. How it decides (in plain English)

A message is sent to the **cloud** if **any** of these are true:

| Trigger | Example |
|--------|---------|
| It's **long** (more than ~1500 characters) | A big wall of text |
| It contains a **hard-task keyword** (see the full list below) | "**refactor** this", "**debug** that", "**analyze** the design" |
| It contains a **code block** (text wrapped in ` ``` `) | Pasting code to fix |
| It has an **image or file attachment** | "What's in this screenshot?" |

Otherwise, it stays **local**.

### The full keyword list

If your message contains **any** of these words (anywhere, case-insensitive),
it goes to the cloud. This is the complete default list:

```
refactor    debug       architect    architecture   analyze
analyse     prove       design       optimize       optimise
migrate     security    vulnerab     trace          root cause
why does    why is      explain why  step by step   algorithm
```

> You can add or remove words yourself — see
> [Tuning the behaviour](#11-tuning-the-behaviour). Prompt length, code blocks,
> and attachments are configurable there too.

### ⚠️ Important: it's a plain word-match, NOT "understanding"

This is a **simple, rule-based** check (no AI, no extra cost, no delay). It does
**not** actually understand how hard your question is — it just looks for the
words above, a code block, an image, or a long message.

That means it sometimes guesses "wrong" in an obvious way:

- *"**Analyze** my grocery list"* → **cloud** (the word "analyze" matched, even
  though it's trivial).
- *"Rewrite my entire authentication system from scratch"* → **local** (none of
  the trigger words appear, even though it's genuinely hard).

This is by design: it's crude but **predictable and free**. When it guesses
wrong, you simply override it by hand with `/local` or `/cloud` (see
[Section 9](#9-the-commands-youll-use)).

---

## 4. What you need before you start

1. **Hermes already installed and working** with a cloud model. If `hermes`
   starts and answers a question, you're good. (If not, see the main
   [README](../README.md) first.)
2. **A local model runner.** This tutorial covers the two most beginner-friendly:
   - **Ollama** — easiest, one command to install. *(recommended for beginners)*
   - **LM Studio** — a friendly desktop app with a GUI.
3. About **10 minutes** and a few GB of disk space for the local model.

You do **not** need a GPU. Small models run fine on a normal laptop CPU (just a
bit slower).

---

## 5. Step 1 — Run a local model

Pick **one** of the options below.

### Option A — Ollama (recommended)

1. **Install Ollama:** go to <https://ollama.com/download> and follow the
   installer for your OS. (On macOS/Linux you can also run
   `curl -fsSL https://ollama.com/install.sh | sh`.)

2. **Download a small model** (this also starts it). In a terminal:

   ```bash
   ollama pull qwen2.5:3b
   ```

   `qwen2.5:3b` is a good, small, fast general model. (Bigger = smarter but
   slower; `qwen2.5:7b` is also fine on most machines.)

3. **Make sure Ollama is running.** It usually runs in the background after
   install. To be sure, run:

   ```bash
   ollama serve
   ```

   If it says the address is already in use, that's fine — it's already running.

4. **Note your endpoint.** Ollama listens at:

   ```
   http://localhost:11434/v1
   ```

   Remember this — you'll paste it into the config in the next step.

### Option B — LM Studio

1. Download and install LM Studio from <https://lmstudio.ai>.
2. In the app, search for and download a small model (e.g. a 3B–7B "instruct"
   model).
3. Go to the **"Local Server"** tab (the `↔` / developer icon) and click
   **"Start Server"**.
4. LM Studio shows the model name and its endpoint, usually:

   ```
   http://localhost:1234/v1
   ```

   Note both the **model name** (as LM Studio displays it) and the endpoint.

---

## 6. Step 2 — Tell Hermes about your local model

Hermes keeps its settings in a file called `config.yaml`. It lives at:

```
~/.hermes/config.yaml
```

> `~` means your home folder. On Windows it's usually
> `C:\Users\<you>\.hermes\config.yaml` (or `%LOCALAPPDATA%\hermes` on a native
> Windows install).

Open that file in any text editor. Look for a section called `routing:`. It
looks like this out of the box:

```yaml
routing:
  enabled: true
  local:
    provider: ""
    model: ""
    base_url: ""
```

Fill in the `local:` part to match the model you started in Step 1.

### If you chose Ollama:

```yaml
routing:
  enabled: true
  local:
    provider: ollama
    model: qwen2.5:3b
    base_url: http://localhost:11434/v1
```

### If you chose LM Studio:

```yaml
routing:
  enabled: true
  local:
    provider: lmstudio
    model: your-model-name-here    # exactly as LM Studio shows it
    base_url: http://localhost:1234/v1
```

**Save the file.**

> **There's no "cloud" section to configure.** Complex prompts stay on your
> normal (primary) model — the one you already selected with `hermes model`.
> You only ever configure the *local* side; the cloud side is just "business as
> usual."

> **Important:** while `local.model` is blank, hybrid routing does **nothing** —
> everything goes to your normal model. It only switches on once you fill in a
> local model. This is the safety default so nothing breaks by surprise.

---

## 7. Step 3 — Start Hermes and check the setup

Start Hermes as you normally do:

```bash
hermes
```

Once you're at the chat prompt, type:

```
/route status
```

You should see something like:

```
Hybrid routing: auto (classify each prompt)
Local model: ollama / qwen2.5:3b
Usage: /route [auto|local|cloud|off] · /local · /cloud
```

If it says **"inert (local endpoint not configured)"**, your `local.model` is
still blank — recheck Step 2 and make sure you saved the file and restarted
Hermes.

---

## 8. Step 4 — Try it out

Now just chat normally. Try these to see routing in action:

**A simple message (should go LOCAL):**

```
what is the capital of France?
```

**A complex message (should go CLOUD):**

```
refactor this function and analyze its time complexity step by step
```

The keyword *refactor* (and *analyze*, and *step by step*) triggers the cloud
route.

**An image (should go CLOUD):**

Attach a screenshot and ask "what's in this image?" — attachments always route
to cloud.

You won't necessarily *see* which model answered — see
[Section 10](#10-how-to-prove-its-actually-working) for how to confirm.

---

## 9. The commands you'll use

You type these at the Hermes chat prompt. They control routing for **your
current session**.

| Command | What it does |
|---------|--------------|
| `/route status` | Show the current mode and which local model is set. |
| `/route auto` | (Default) Let Hermes decide each message automatically. |
| `/local` | **Pin to local.** Force *every* upcoming message to the local model until you change it. |
| `/cloud` | **Pin to cloud.** Force every upcoming message to the cloud model. |
| `/route off` | Turn routing off for this session — everything goes to your normal model. |
| `/route on` | Same as `/route auto` — turn automatic routing back on. |

**Typical uses:**

- About to do a long, hard coding session? Type `/cloud` so nothing gets sent to
  the weaker local model by mistake.
- Just brainstorming or doing quick lookups and want to save money? Type
  `/local`.
- Want it to think for you? Leave it on `/route auto` (the default).

To go back to automatic at any time: `/route auto`.

---

## 10. How to *prove* it's actually working

Routing happens silently, so here are three ways to confirm which model
answered:

1. **Watch the local server's log (clearest proof).**
   - **Ollama:** the terminal running `ollama serve` prints a line every time it
     receives a request. Send a *simple* message in Hermes → you should see
     Ollama light up. Send a *complex* one → Ollama stays quiet (it went to the
     cloud).
   - **LM Studio:** the "Local Server" tab shows a live request log.

2. **Run Hermes in verbose mode.** Start it with:

   ```bash
   hermes --verbose
   ```

   Verbose logs show the provider/model being used for each turn.

3. **Use `/local` and `/cloud` as a sanity check.** Type `/local`, then ask
   something — your local server should receive it. Type `/cloud`, ask again —
   the local server should stay silent.

---

## 11. Tuning the behaviour

All the knobs live under `routing.complexity` in `~/.hermes/config.yaml`:

```yaml
routing:
  complexity:
    max_prompt_chars: 1500        # longer than this → cloud
    max_prompt_tokens: 400        # roughly "words × 1.3" → cloud
    cloud_keywords:               # any of these words → cloud
      - refactor
      - debug
      - architect
      - architecture
      - analyze
      - analyse
      - prove
      - design
      - optimize
      - optimise
      - migrate
      - security
      - vulnerab
      - trace
      - root cause
      - why does
      - why is
      - explain why
      - step by step
      - algorithm
    escalate_on_images: true      # attachments → cloud
    escalate_on_code_fence: true  # pasted code blocks → cloud
```

> **Note:** if you set `cloud_keywords` in your config, your list **replaces**
> the built-in defaults entirely — it isn't merged. So copy the full list above
> and then add or remove words, rather than listing only your new ones.

**Common adjustments:**

- **"Too much goes to cloud."** Raise `max_prompt_chars` (e.g. to `3000`) and
  remove keywords you don't care about.
- **"I want more on cloud to be safe."** Lower `max_prompt_chars` (e.g. `800`)
  and add more keywords.
- **"Keep code on the local model."** Set `escalate_on_code_fence: false`.
- **"Always send screenshots locally too."** Set `escalate_on_images: false`
  (only do this if your local model can actually see images!).

Save the file and restart Hermes for changes to take effect.

---

## 12. Troubleshooting

**`/route status` says "inert (local endpoint not configured)".**
Your `routing.local.model` is empty. Fill it in (Step 2), save, restart Hermes.

**Everything still goes to the cloud, even simple messages.**
- Check `routing.enabled: true` in the config.
- Check `/route status` shows `auto` and your local model (not "inert").
- Make sure you didn't type `/cloud` earlier — type `/route auto` to reset.

**"Local model unavailable — falling back to … for this turn."**
This is expected, not an error. When a prompt routes local but the local model
can't start — the server is down, or its context window is below Hermes'
64K-token minimum — Hermes automatically answers that turn with your primary
(cloud) model instead of dropping it. Fix the underlying cause (start the
server, or pick a local model with ≥64K context) and the next turn routes local
again. Run `/route status` to confirm your local endpoint.

**Errors / timeouts when a message routes to local.**
- Is your local server actually running? Test it:
  ```bash
  # Ollama:
  curl http://localhost:11434/v1/models
  # LM Studio:
  curl http://localhost:1234/v1/models
  ```
  You should get a JSON list of models, not an error.
- Is the `base_url` in your config **exactly** right (including `/v1` at the
  end for the OpenAI-compatible endpoint)?
- Is the `model` name spelled exactly as your server lists it?

**Hermes won't send auth to my local server / complains about API key.**
That's handled automatically — local servers don't need a key and Hermes fills
in a harmless placeholder. If you still see key errors, double-check the
`base_url` points at *localhost*, not a cloud URL.

**I want to turn the whole feature off permanently.**
Set `enabled: false` under `routing:` in the config, or just leave
`local.model` blank.

---

## 13. FAQ

**Q: Does this cost anything?**
The local model is completely free (it runs on your machine). You only pay for
messages that route to the cloud, exactly as before.

**Q: Will it send my private stuff to the cloud?**
Only messages classified as "complex" go to the cloud. Simple ones stay local.
If privacy matters for a specific message, type `/local` first to force it local
(as long as your local model can handle it).

**Q: Does it slow anything down?**
No. The decision is a fast rule check — no extra AI call, no network request.

**Q: Can I use it with WhatsApp / Telegram / the desktop app?**
The automatic routing and the config live at the core, so the model selection
applies wherever the CLI turn path runs. The `/local` `/cloud` `/route` commands
are CLI/terminal commands.

**Q: What if my local model gives worse answers?**
That's expected — small local models are weaker. Either raise the sensitivity so
more goes to cloud (Section 11), use a bigger local model, or just type
`/cloud` when you want the good stuff.

**Q: How do I go back to exactly how Hermes was before?**
Set `routing.enabled: false` (or leave `local.model` blank). Hermes behaves
100% as it did before — one model, no routing.

---

### That's it! 🎉

You now have a money-saving, privacy-friendly, automatically-routing Hermes.
Start with `/route auto` and let it do its thing, and reach for `/local` /
`/cloud` whenever you want manual control.
