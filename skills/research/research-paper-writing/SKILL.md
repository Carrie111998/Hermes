---
name: research-paper-writing
title: Research Paper Writing Pipeline
description: "Write ML papers for NeurIPS/ICML/ICLR: design→submit."
version: 1.1.0
author: Orchestra Research
license: MIT
dependencies: [semanticscholar, arxiv, habanero, requests, scipy, numpy, matplotlib, SciencePlots]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Research, Paper Writing, Experiments, ML, AI, NeurIPS, ICML, ICLR, ACL, AAAI, COLM, LaTeX, Citations, Statistical Analysis]
    category: research
    related_skills: [arxiv, subagent-driven-development, plan]
    requires_toolsets: [terminal, file]

---

# Research Paper Writing Pipeline

End-to-end pipeline for producing publication-ready ML/AI research papers targeting **NeurIPS, ICML, ICLR, ACL, AAAI, and COLM**. This skill covers the full research lifecycle: experiment design, execution, monitoring, analysis, paper writing, review, revision, and submission.

This is **not a linear pipeline** — it is an iterative loop. Results trigger new experiments. Reviews trigger new analysis. The agent must handle these feedback loops.

<!-- ascii-guard-ignore -->
```
┌─────────────────────────────────────────────────────────────┐
│                    RESEARCH PAPER PIPELINE                  │
│                                                             │
│  Phase 0: Project Setup ──► Phase 1: Literature Review      │
│       │                          │                          │
│       ▼                          ▼                          │
│  Phase 2: Experiment     Phase 5: Paper Drafting ◄──┐      │
│       Design                     │                   │      │
│       │                          ▼                   │      │
│       ▼                    Phase 6: Self-Review      │      │
│  Phase 3: Execution &           & Revision ──────────┘      │
│       Monitoring                 │                          │
│       │                          ▼                          │
│       ▼                    Phase 7: Submission               │
│  Phase 4: Analysis ─────► (feeds back to Phase 2 or 5)     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```
<!-- ascii-guard-ignore-end -->

---

## When To Use This Skill

Use this skill when:
- **Starting a new research paper** from an existing codebase or idea
- **Designing and running experiments** to support paper claims
- **Writing or revising** any section of a research paper
- **Preparing for submission** to a specific conference or workshop
- **Responding to reviews** with additional experiments or revisions
- **Converting** a paper between conference formats
- **Writing non-empirical papers** — theory, survey, benchmark, or position papers (see [Paper Types Beyond Empirical ML](#paper-types-beyond-empirical-ml))
- **Designing human evaluations** for NLP, HCI, or alignment research
- **Preparing post-acceptance deliverables** — posters, talks, code releases

## Core Philosophy

1. **Be proactive.** Deliver complete drafts, not questions. Scientists are busy — produce something concrete they can react to, then iterate.
2. **Never hallucinate citations.** AI-generated citations have ~40% error rate. Always fetch programmatically. Mark unverifiable citations as `[CITATION NEEDED]`.
3. **Paper is a story, not a collection of experiments.** Every paper needs one clear contribution stated in a single sentence. If you can't do that, the paper isn't ready.
4. **Experiments serve claims.** Every experiment must explicitly state which claim it supports. Never run experiments that don't connect to the paper's narrative.
5. **Commit early, commit often.** Every completed experiment batch, every paper draft update — commit with descriptive messages. Git log is the experiment history.

### Proactivity and Collaboration

**Default: Be proactive. Draft first, ask with the draft.**

| Confidence Level | Action |
|-----------------|--------|
| **High** (clear repo, obvious contribution) | Write full draft, deliver, iterate on feedback |
| **Medium** (some ambiguity) | Write draft with flagged uncertainties, continue |
| **Low** (major unknowns) | Ask 1-2 targeted questions via `clarify`, then draft |

| Section | Draft Autonomously? | Flag With Draft |
|---------|-------------------|-----------------|
| Abstract | Yes | "Framed contribution as X — adjust if needed" |
| Introduction | Yes | "Emphasized problem Y — correct if wrong" |
| Methods | Yes | "Included details A, B, C — add missing pieces" |
| Experiments | Yes | "Highlighted results 1, 2, 3 — reorder if needed" |
| Related Work | Yes | "Cited papers X, Y, Z — add any I missed" |

**Block for input only when**: target venue unclear, multiple contradictory framings, results seem incomplete, explicit request to review first.

---

## Phase 0: Project Setup

**Goal**: Establish the workspace, understand existing work, identify the contribution.

### Step 0.1: Explore the Repository

```bash
# Understand project structure
ls -la
find . -name "*.py" | head -30
find . -name "*.md" -o -name "*.txt" | xargs grep -l -i "result\|conclusion\|finding"
```

Look for:
- `README.md` — project overview and claims
- `results/`, `outputs/`, `experiments/` — existing findings
- `configs/` — experimental settings
- `.bib` files — existing citations
- Draft documents or notes

### Step 0.2: Organize the Workspace

Establish a consistent workspace structure:

```
workspace/
  paper/               # LaTeX source, figures, compiled PDFs
  experiments/         # Experiment runner scripts
  code/                # Core method implementation
  results/             # Raw experiment results (auto-generated)
  tasks/               # Task/benchmark definitions
  human_eval/          # Human evaluation materials (if needed)
```

### Step 0.3: Set Up Version Control

```bash
git init  # if not already
git remote add origin <repo-url>
git checkout -b paper-draft  # or main
```

**Git discipline**: Every completed experiment batch gets committed with a descriptive message. Example:
```
Add Monte Carlo constrained results (5 runs, Sonnet 4.6, policy memo task)
Add Haiku baseline comparison: autoreason vs refinement baselines at cheap model tier
```

### Step 0.4: Identify the Contribution

Before writing anything, articulate:
- **The What**: What is the single thing this paper contributes?
- **The Why**: What evidence supports it?
- **The So What**: Why should readers care?

> Propose to the scientist: "Based on my understanding, the main contribution is: [one sentence]. The key results show [Y]. Is this the framing you want?"

### Step 0.5: Create a TODO List

Use the `todo` tool to create a structured project plan:

```
Research Paper TODO:
- [ ] Define one-sentence contribution
- [ ] Literature review (related work + baselines)
- [ ] Design core experiments
- [ ] Run experiments
- [ ] Analyze results
- [ ] Write first draft
- [ ] Self-review (simulate reviewers)
- [ ] Revise based on review
- [ ] Submission prep
```

Update this throughout the project. It serves as the persistent state across sessions.

### Step 0.6: Estimate Compute Budget

Before running experiments, estimate total cost and time:

```
Compute Budget Checklist:
- [ ] API costs: (model price per token) × (estimated tokens per run) × (number of runs)
- [ ] GPU hours: (time per experiment) × (number of experiments) × (number of seeds)
- [ ] Human evaluation costs: (annotators) × (hours) × (hourly rate)
- [ ] Total budget ceiling and contingency (add 30-50% for reruns)
```

Track actual spend as experiments run:
```python
# Simple cost tracker pattern
import json, os
from datetime import datetime

COST_LOG = "results/cost_log.jsonl"

def log_cost(experiment: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "experiment": experiment,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }
    with open(COST_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

**When budget is tight**: Run pilot experiments (1-2 seeds, subset of tasks) before committing to full sweeps. Use cheaper models for debugging pipelines, then switch to target models for final runs.

### Step 0.7: Multi-Author Coordination

Most papers have 3-10 authors. Establish workflows early:

| Workflow | Tool | When to Use |
|----------|------|-------------|
| **Overleaf** | Browser-based | Multiple authors editing simultaneously, no git experience |
| **Git + LaTeX** | `git` with `.gitignore` for aux files | Technical teams, need branch-based review |
| **Overleaf + Git sync** | Overleaf premium | Best of both — live collab with version history |

**Section ownership**: Assign each section to one primary author. Others comment but don't edit directly. Prevents merge conflicts and style inconsistency.

```
Author Coordination Checklist:
- [ ] Agree on section ownership (who writes what)
- [ ] Set up shared workspace (Overleaf or git repo)
- [ ] Establish notation conventions (before anyone writes)
- [ ] Schedule internal review rounds (not just at the end)
- [ ] Designate one person for final formatting pass
- [ ] Agree on figure style (colors, fonts, sizes) before creating figures
```

**LaTeX conventions to agree on early**:
- `\method{}` macro for consistent method naming
- Citation style: `\citet{}` vs `\citep{}` usage
- Math notation: lowercase bold for vectors, uppercase bold for matrices, etc.
- British vs American spelling

---

## Phase 1: Literature Review

**Goal**: Find related work, identify baselines, gather citations.

### Step 1.1: Identify Seed Papers

Start from papers already referenced in the codebase:

```bash
# Via terminal:
grep -r "arxiv\|doi\|cite" --include="*.md" --include="*.bib" --include="*.py"
find . -name "*.bib"
```

### Step 1.2: Search for Related Work

**Load the `arxiv` skill** for searching and downloading papers. Use `web_search` for broader queries:

```
web_search("<method name> machine learning 2024 2025")
web_search("<task name> LLM reasoning benchmark")
web_search("site:arxiv.org <specific topic>")
```

**Systematic search strategy**:
1. Start with 3-5 seed papers
2. **Backward chaining**: check their references for foundational work
3. **Forward chaining**: search Semantic Scholar "cited by" for newer work
4. **Keyword expansion**: extract terms from relevant papers, search those
5. Stop when new searches return >80% papers you've already seen (saturation)

### Step 1.3: Verify Citations

**NEVER type a BibTeX entry from memory.** Use APIs:

```python
# Semantic Scholar API — no key needed for basic use
import requests

def get_paper(query):
    r = requests.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": query, "limit": 5, "fields": "title,authors,year,externalIds,url"}
    )
    return r.json()["data"]

# CrossRef API for DOI → BibTeX
def doi_to_bibtex(doi):
    r = requests.get(
        f"https://api.crossref.org/works/{doi}/transform/application/x-bibtex"
    )
    return r.text
```

If using a citation manager, load the `zotero` skill when available.

### Step 1.4: Build a Literature Matrix

Create `paper/literature_review.md`:

```markdown
| Paper | Year | Method | Task | Key Finding | Relevance | Diff from Ours |
|-------|------|--------|------|-------------|-----------|----------------|
| Smith et al. | 2024 | CoT | GSM8K | +5% acc | Baseline | No refinement |
```

### Step 1.5: Identify Baselines

For every claim your paper makes, identify the strongest fair comparison:
- **Methodological baseline**: Previous best approach to the same problem
- **Ablation baseline**: Your method with component removed
- **Simple baseline**: The obvious naive approach (often surprisingly strong)
- **Upper bound**: Oracle or expensive method that shows headroom

---

## Phase 2: Experiment Design

**Goal**: Design experiments that directly support paper claims.

### Step 2.1: Map Claims to Experiments

Create a claims-evidence matrix **before running anything**:

```markdown
| Claim | Experiment | Metric | Expected Result | Falsification |
|-------|-----------|--------|-----------------|---------------|
| Method improves accuracy | Main benchmark | Accuracy | +5% vs baseline | <2% gain |
| Component X is necessary | Ablation | Accuracy drop | -3% without X | <1% drop |
| Scales to harder tasks | Difficulty sweep | Acc vs difficulty | Graceful decay | Cliff at medium |
```

Every paper claim must have at least one supporting experiment. Every experiment must support at least one claim.

### Step 2.2: Define Metrics Before Seeing Results

Pre-commit to primary metrics to avoid cherry-picking:

```yaml
# experiments/config.yaml
primary_metric: accuracy
secondary_metrics:
  - latency
  - cost_per_example
significance_test: paired_bootstrap
alpha: 0.05
num_seeds: 5
```

### Step 2.3: Statistical Planning

**Minimum requirements**:
- Report **mean ± standard deviation** across ≥3 runs (5 preferred)
- Use **paired tests** when comparing methods on same examples
- Report **effect size**, not just p-values
- For multiple comparisons, apply correction (Bonferroni or Benjamini-Hochberg)
- **Pre-specify** the significance threshold (typically α = 0.05)
- **Power analysis**: Before experiments, estimate required sample size for your expected effect. For a two-sample t-test with medium effect (Cohen's d=0.5), α=0.05, power=0.8, you need ~64 samples per group. Use `scipy.stats` or `statsmodels.stats.power` to compute for your design.
- If sample size is too small for adequate power, report this as a limitation rather than over-interpreting null results.

### Step 2.4: Reproducibility Setup

Before experiments:
```bash
# Pin environment
pip freeze > requirements.txt
# or
conda env export > environment.yml

# Record hardware
nvidia-smi > results/hardware.txt
python --version >> results/hardware.txt

# Fix seeds everywhere
export PYTHONHASHSEED=42
```

In code:
```python
import random, numpy as np, torch

def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
```

### Step 2.5: Human Evaluation Design

When automated metrics are insufficient (open-ended generation, helpfulness, creativity, safety), design a human evaluation.

**Core requirements**:
- **≥3 annotators per item** for reliability
- **Blind evaluation** — annotators don't know which system produced which output
- **Randomize presentation order** to prevent position bias
- **Clear rubric** with concrete examples for each rating level
- **Calibration round** — all annotators rate 10-20 shared examples, discuss disagreements, refine rubric
- **Inter-annotator agreement** — report Krippendorff's α (ordinal/nominal) or Fleiss' κ (categorical). Target α ≥ 0.67 for tentative conclusions, ≥ 0.8 for strong claims
- **Report annotator details** — expertise level, compensation, recruitment method (anonymized as needed)
- **Quality control** — include attention checks; exclude annotators who fail >20%

See [references/human-evaluation.md](references/human-evaluation.md) for templates and full guidance.

### Step 2.6: Compute-Aware Experiment Planning

For GPU or API-heavy experiments, design a staged approach:

**Stage 1 — Pilot** (5-10% of budget):
- 1-2 seeds, 10-20% of tasks
- Validate pipeline works end-to-end
- Estimate variance → refine power analysis
- Kill ideas that clearly don't work

**Stage 2 — Core** (60-70% of budget):
- Full task set, required seeds
- Main baselines + your method
- Run in parallel where infrastructure allows

**Stage 3 — Robustness** (20-30% of budget):
- Ablations, sensitivity analyses
- Additional datasets/tasks
- Error analysis

**Decision gate after Stage 1**: If pilot effect size is <50% of expected, reassess before spending remaining budget.

---

## Phase 3: Experiment Execution & Monitoring

**Goal**: Run experiments reliably, recover from failures, track everything.

### Step 3.1: Implement Experiment Runner

Every experiment runner should:
- Log config + git commit hash at start
- Save intermediate results (not just final)
- Handle interruptions gracefully (checkpoint/resume)
- Record timestamps and durations
- Output machine-readable results (JSON/CSV)

```python
# Experiment metadata
metadata = {
    "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
    "timestamp": datetime.now().isoformat(),
    "config": vars(args),
    "hostname": socket.gethostname(),
}
```

### Step 3.2: Launch Experiments

For local/remote execution:
```bash
# Background with logging
nohup python experiments/run.py --config experiments/config.yaml \
    > logs/experiment_$(date +%Y%m%d_%H%M).log 2>&1 &
echo $! > logs/experiment.pid
```

For multiple runs:
```bash
for seed in 42 123 456 789 1024; do
    python experiments/run.py --seed $seed > logs/run_$seed.log 2>&1 &
done
wait
```

### Step 3.3: Monitor with `process`

Use the `process` tool for long-running experiments:
```bash
# Check processes
ps aux | grep run.py
# GPU utilization
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
# Tail logs
tail -50 logs/experiment_*.log
```

**Do not poll constantly.** Use cron for periodic checks (see [Long-Running Experiments](#long-running-experiments--state-management)).

### Step 3.4: Failure Recovery

When an experiment fails:
1. **Diagnose before restarting.** Read the error, check resources (OOM? disk? API rate limit?)
2. **Fix root cause**, commit the fix
3. **Resume from checkpoint** if possible
4. **Log the failure** in experiment notes (failures are data too)
5. **Never silently drop failed seeds** — report how many runs failed and why

Common failures:
| Failure | Recovery |
|---------|----------|
| GPU OOM | Reduce batch size, enable gradient checkpointing |
| API rate limit | Exponential backoff, reduce concurrency |
| Disk full | Clean intermediate checkpoints, compress logs |
| Process killed | Resume from latest checkpoint |
| NaN loss | Check learning rate, gradient clipping, data |

### Step 3.5: Track Experiments

Maintain `results/experiment_log.md`:
```markdown
| ID | Date | Config | Seeds | Status | Key Result | Git Commit |
|----|------|--------|-------|--------|------------|------------|
| exp001 | 2025-03-15 | baseline.yaml | 5 | Done | 82.3±1.2 | abc123 |
| exp002 | 2025-03-16 | method.yaml | 5 | Done | 87.1±0.8 | def456 |
```

---

## Phase 4: Results Analysis

**Goal**: Turn raw results into defensible claims.

### Step 4.1: Aggregate Results

```python
import pandas as pd
import scipy.stats as stats

results = pd.read_csv("results/all_results.csv")
summary = results.groupby("method")["score"].agg(["mean", "std", "count"])
print(summary)

# Paired significance test
baseline = results[results.method == "baseline"].sort_values("seed").score
method = results[results.method == "ours"].sort_values("seed").score
t, p = stats.ttest_rel(method, baseline)
print(f"Paired t-test: t={t:.3f}, p={p:.4f}")
```

### Step 4.2: Create Publication Figures

Load the `matplotlib` skill if available. Standards:
- Vector formats (PDF/SVG) for plots
- 300+ DPI for raster images
- Colorblind-safe palettes
- Readable at single-column width (~3.25 inches)
- Font sizes ≥8pt in final size
- No chartjunk — remove unnecessary gridlines, borders
- Error bars on all aggregated results

```python
import matplotlib.pyplot as plt
plt.rcParams.update({
    'font.size': 9,
    'font.family': 'serif',
    'figure.figsize': (3.25, 2.2),
})
fig, ax = plt.subplots()
# ... plot ...
fig.savefig("paper/figures/main_result.pdf", bbox_inches="tight")
```

### Step 4.3: Interpret Honestly

For each result:
1. What does the data show? (factual)
2. What can we claim from this? (inference)
3. What can't we claim? (limitation)
4. What alternative explanations exist?
5. Does this change our original hypothesis?

**Negative results are valuable.** If the method doesn't work in some setting, that's a finding. Don't hide it — explain the boundary condition.

### Step 4.4: Error Analysis

Sample failures systematically:
- Categorize error types
- Report distribution of error categories
- Compare error patterns across methods
- Include 3-5 representative examples in paper/appendix

```python
# Stratified error sampling
errors = results[results.correct == False]
sample = errors.groupby("error_category").apply(
    lambda x: x.sample(min(5, len(x)), random_state=42)
)
```

### Step 4.5: Sensitivity and Robustness Analysis

Before writing claims, stress-test them:
- **Hyperparameter sensitivity**: Vary key hyperparameters ±20% or across a reasonable range. Does the main conclusion hold?
- **Seed sensitivity**: Plot per-seed results. Are gains driven by one lucky seed?
- **Data subset sensitivity**: Does the result hold across task categories, difficulty levels, demographic slices (where applicable)?
- **Metric sensitivity**: Does the conclusion hold under alternative reasonable metrics?
- **Baseline strength**: Re-run with the strongest available baseline configuration, not the default one.

If a result is fragile, say so. A robust 3% gain is more publishable than a brittle 10% gain.

### Step 4.6: Cost and Efficiency Analysis

For LLM/API-based methods, report compute cost alongside quality:

```markdown
| Method | Score ↑ | Cost/example ↓ | Latency (s) ↓ | Tokens/example |
|--------|---------|---------------|---------------|----------------|
| Baseline | 82.3 | $0.012 | 2.1 | 1,200 |
| Ours | 87.1 | $0.018 | 3.4 | 1,850 |
| Expensive oracle | 89.2 | $0.095 | 12.3 | 8,400 |
```

**Cost-quality Pareto frontier**: Plot score vs. cost. If your method is dominated (another method is both cheaper and better), address this honestly.

### Step 4.7: Data Leakage and Contamination Checks

For benchmark-based work, verify your model hasn't seen the test data:
- Check benchmark publication date vs. model training cutoff
- Search for test examples appearing verbatim online or in training corpora proxies
- For LLM evaluations, consider using recently created or private test sets
- Report potential contamination as a limitation
- If using synthetic data, verify no test-set information leaked into generation prompts

---

## Phase 5: Paper Drafting

**Goal**: Write a complete, coherent paper.

### Step 5.1: Choose Template

Templates are in `templates/` for major venues. Copy the target template:
```bash
cp -r templates/neurips/ paper/
```

### Step 5.2: Write in Story Order, Not Section Order

**Recommended writing order**:
1. Figures and tables (forces you to decide what matters)
2. Methods (you know what you did)
3. Experiments (you know the results)
4. Related Work (context now clear)
5. Introduction (now you know the story)
6. Abstract (last — it's the compressed story)
7. Conclusion

### Step 5.3: Abstract Structure

Target 150-250 words. Every sentence has a job:

```
1. [Context] What problem exists? (1 sentence)
2. [Gap] Why aren't current approaches enough? (1 sentence)
3. [Approach] What do we propose? (1-2 sentences)
4. [Results] What did we find? Include NUMBERS. (2 sentences)
5. [Impact] Why does this matter? (1 sentence)
```

**Bad**: "We conduct extensive experiments showing our method is effective."
**Good**: "Across 6 tasks and 3 model families, REFINER improves constraint satisfaction by 23% (p<0.01) while adding 12% compute overhead."

### Step 5.4: Introduction Structure

Page 1 should answer:
- What is the problem? (¶1)
- Why is it hard? (¶2)
- What's missing from existing work? (¶3)
- What do we do? (¶4)
- What do we find? (¶5)
- What are our contributions? (bullets)

**Contribution bullets** — 2-4, each starts with a verb:
```latex
\begin{itemize}
  \item We \textbf{introduce} ...
  \item We \textbf{demonstrate} ...
  \item We \textbf{show} ...
\end{itemize}
```

### Step 5.5: Methods Section

**Reproducibility test**: Could an expert reimplement your method from this section alone?

Include:
- Formal problem definition and notation
- Algorithm (pseudocode if >3 steps)
- All hyperparameters with values
- Implementation details that affect results
- Computational complexity if non-trivial

### Step 5.6: Experiments Section

Structure around **research questions**, not datasets:

```markdown
### 4.1 Does REFINER improve constraint satisfaction? (RQ1)
[Setup] We evaluate on X, Y, Z with baselines A, B.
[Result] Table 1 shows... REFINER improves by X% (p<0.05).
[Interpretation] This supports our hypothesis that...

### 4.2 Which components matter? (RQ2)
[Ablation study]
```

Every table/figure gets referenced in text. Every number in text should be traceable to a result file.

### Step 5.7: Related Work

Group by **themes/methodologies**, not paper-by-paper chronology:

**Bad**: "Smith (2020) did X. Jones (2021) did Y. Lee (2022) did Z."

**Good**: "Prior approaches fall into three categories: single-pass generation [refs], iterative refinement [refs], and search-based methods [refs]. Unlike the first category, we..."

End each paragraph by connecting to your work: "Our method differs by..."

### Step 5.8: Limitations

Be specific and honest. Good limitations:
- "Evaluated only on English-language tasks; multilingual generalization is unknown."
- "Requires 2.3× more inference compute than single-pass baseline."
- "Human evaluation used 3 expert annotators; results may not reflect general population preferences."

Bad limitations:
- "Future work could explore more datasets." (vague)
- "Our method may have limitations." (says nothing)

### Step 5.9: Conclusion

3-5 sentences:
1. Restate the problem and contribution
2. Key finding (with number)
3. Implication
4. Future direction (specific, not generic)

Don't introduce new results or claims.

### Step 5.10: Broader Impact / Ethics Statement

Most major venues now require or strongly encourage impact statements. Address:

- **Who benefits?** Which communities, industries, or populations gain from this work?
- **Who could be harmed?** Consider misuse, displacement, bias amplification, environmental cost.
- **Failure modes at scale**: What happens if this method is widely deployed and fails? Are failures detectable?
- **Dual-use potential**: Could the method enable harmful capabilities? What mitigations exist?
- **Environmental impact**: Report compute used (GPU hours, model API calls) and energy implications for large-scale experiments.
- **Data ethics**: If using human data, document consent, licensing, privacy safeguards, demographic representation.

**Don't write "This work has no foreseeable negative societal impacts."** Reviewers increasingly flag this as insufficient. If genuinely low-risk, explain *why* concretely.

### Writing Style — Key Principles

These are synthesized from Gopen & Swan, Neel Nanda, Sebastian Farquhar, Zachary Lipton, John Schulman, and Ethan Perez. See [references/writing-guide.md](references/writing-guide.md) for the full guide.

**Sentence level**:
- Put the subject and verb close together
- Put new/emphasized information at the **end** of the sentence (stress position)
- Start sentences with old/contextual information (topic position)
- One unit of discourse = one idea
- Prefer active voice: "We evaluate" not "An evaluation was performed"
- Cut hedges: "We believe that X may possibly..." → "X may..."

**Paper level**:
- Give away the punchline early — no suspense in scientific writing
- Use consistent terminology — one concept, one name
- Figures should be understandable without reading the main text
- First sentence of each paragraph = topic sentence
- Last sentence = transition or takeaway

**Words to eliminate**:
- "very", "really", "quite", "rather" — almost always removable
- "utilize" → "use"
- "in order to" → "to"
- "it is important to note that" → delete
- "novel" — let reviewers decide
- "obviously", "clearly" — if obvious, don't say it; if not, prove it

---

## Phase 6: Self-Review & Revision

**Goal**: Catch weaknesses before reviewers do.

### Step 6.1: Simulate Three Reviewers

Use `delegate_task` to get independent reviews:

```
delegate_task("You are a skeptical NeurIPS reviewer. Review this paper focusing on technical soundness, missing baselines, and unsupported claims. Score 1-6.")

delegate_task("You are a domain expert in <area>. Review for correctness of technical details and related work coverage. Identify the 3 strongest and 3 weakest aspects.")

delegate_task("You are a clarity-focused reviewer. Identify confusing sections, undefined terms, notation issues, and claims that are hard to verify.")
```

### Step 6.2: Build a Revision Matrix

```markdown
| Issue | Reviewer | Severity | Action | Status |
|-------|----------|----------|--------|--------|
| Missing GPT-4 baseline | R1 | Major | Run experiment | Done |
| Section 3.2 unclear | R3 | Medium | Rewrite | In progress |
| No significance test | R1,R2 | Major | Add bootstrap CI | Todo |
```

### Step 6.3: Adversarial Claim Checking

For every claim in abstract/introduction:
1. Locate supporting evidence in results
2. Check if evidence actually supports the exact wording
3. Try to find a counterexample
4. Weaken claim if evidence is insufficient

```bash
# Extract strong claim words to audit
grep -in "significantly\|outperform\|state-of-the-art\|novel\|first\|always\|all" paper/*.tex
```

### Step 6.4: Final Consistency Pass

Check:
- [ ] All figures referenced in text
- [ ] All citations resolve
- [ ] All table numbers match source data
- [ ] Notation consistent throughout
- [ ] No undefined acronyms
- [ ] Abstract numbers match results section
- [ ] Page limit satisfied (excluding references per venue rules)
- [ ] Anonymization requirements met
- [ ] Supplementary material referenced correctly

---

## Phase 7: Submission

### Step 7.1: Pre-Submission Checklist

See [references/checklists.md](references/checklists.md) for venue-specific checklists.

**Universal checklist**:
- [ ] Correct template and year
- [ ] Page limit (check what counts/excludes)
- [ ] Anonymized (if double-blind)
- [ ] No identifying URLs in paper/code
- [ ] All references complete
- [ ] Figures legible at print size
- [ ] Supplementary materials anonymized
- [ ] Ethics/impact statement included if required
- [ ] Reproducibility checklist completed
- [ ] Abstract character limit met in submission system
- [ ] Author names/affiliations correct (camera-ready)

### Step 7.2: Final Compile

```bash
cd paper/
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
# Check warnings
grep -i "warning\|undefined\|overfull" main.log
```

### Step 7.3: Submission Archive

```bash
# Create clean submission archive — exclude aux files
zip -r submission.zip paper/ \
    -x "*.aux" "*.log" "*.out" "*.bbl" "*.blg" "*.synctex.gz"
```

### Step 7.4: Conference Resubmission

When a paper is rejected or redirected to another venue:

1. **Archive the rejected version** — tag the git commit: `git tag submission-neurips-2025`
2. **Collect all reviewer feedback** into `paper/reviews/<venue>-reviews.md`
3. **Triage feedback** into:
   - Must fix (technical errors, missing critical baselines)
   - Should fix (clarity, additional analysis)
   - Disagree (document reasoning — you may still clarify to prevent future misreading)
4. **Create revision plan** mapping each reviewer concern to an action
5. **Update venue template** and page limits
6. **Do NOT mention prior rejection** in the new submission (unless venue requires it)
7. **Re-run experiments** if reviewer concerns reveal genuine gaps

### Step 7.5: Camera-Ready Preparation

After acceptance:

- [ ] De-anonymize: author names, affiliations, acknowledgments
- [ ] Incorporate reviewer-requested clarifications
- [ ] Update citations to published versions where available
- [ ] Verify all URLs work
- [ ] Check venue-specific copyright/license forms
- [ ] Ensure figures use CMYK-safe colors if print proceedings
- [ ] Add funding acknowledgments and conflict-of-interest statements
- [ ] Final proofread by at least one co-author who did NOT write each section
- [ ] Verify metadata in submission system (title, authors, abstract) exactly matches PDF

### Step 7.6: Archival and Preprint

- Upload preprint to arXiv (check venue policy on timing)
- Update project README with paper link, citation BibTeX, and abstract
- Create a git release/tag for the camera-ready version
- Archive exact experiment environment (Docker image hash or lockfile)
- Deposit datasets/artifacts in long-term repository (Zenodo, Hugging Face) with DOI where appropriate

### Step 7.7: Code Release Checklist

Before making the repository public:

- [ ] Remove API keys, credentials, absolute local paths
- [ ] Add `LICENSE` (confirm with institution policy)
- [ ] Add `README.md` with: setup, data preparation, training, evaluation, expected results
- [ ] Pin dependencies (`requirements.txt`, `environment.yml`, or lockfile)
- [ ] Include a minimal reproduction script that runs in <30 minutes if possible
- [ ] Add pre-trained checkpoints or clear download instructions
- [ ] Include evaluation scripts that reproduce paper tables/figures
- [ ] Add `CITATION.cff` or BibTeX citation
- [ ] Test fresh install in a clean environment

### Step 7.8: Artifact Evaluation

Many conferences (NeurIPS D&B, MLSys, ACM venues) offer artifact evaluation. Prepare:

- **README**: Exact reproduction commands with expected runtime and output
- **Hardware requirements**: GPU model/memory, CPU, disk space
- **Expected results**: Tables with tolerance ranges (e.g., "accuracy 87.1 ± 0.5%")
- **Fallback**: Pre-computed results if full experiments exceed evaluation time budget
- **License**: Clear licenses for code, data, and models
- **DOI**: Archive on Zenodo for permanent reference

### Step 7.9: Responsible Code and Data Release

Before public release, run a security/privacy audit:
- Scan git history for accidentally committed secrets (`git log -p | grep -i 'api_key\|password\|token'` or use a secret scanner)
- Remove PII from datasets and logs
- Check model checkpoints for memorized sensitive data if applicable
- Document known failure modes and intended use
- Include a model card or dataset card where appropriate

---

## Long-Running Experiments & State Management

Experiments may run for hours or days. Use the agent's persistence tools to maintain continuity.

### Parallel Work with `delegate_task`

Delegate independent work streams:

```
delegate_task("Draft the Methods section. Read code/ and experiments/ for implementation details. Follow NeurIPS style. Return complete LaTeX. Do not hallucinate citations. Flag any missing implementation details as TODOs.")

delegate_task("Draft the Related Work section. Use web_search and web_extract to 
  find papers. Verify every citation via Semantic Scholar. Group by methodology.")

delegate_task("Draft the Experiments section. Read all result files in results/. 
  State which claim each experiment supports. Include error bars and significance.")
```

Each delegate runs as a **fresh subagent** with no shared context — provide all necessary information in the prompt. Collect outputs and integrate.

**Citation verification** (using execute_code):
```python
# In execute_code:
from semanticscholar import SemanticScholar
import requests

sch = SemanticScholar()
results = sch.search_paper("attention mechanism transformers", limit=5)
for paper in results:
    doi = paper.externalIds.get('DOI', 'N/A')
    if doi != 'N/A':
        bibtex = requests.get(f"https://doi.org/{doi}", 
                              headers={"Accept": "application/x-bibtex"}).text
        print(bibtex)
```

### State Management with `memory` and `todo`

**`memory` tool** — persist key decisions (bounded: ~2200 chars for MEMORY.md):

```
memory("add", "Paper: autoreason. Venue: NeurIPS 2025 (9 pages). 
  Contribution: structured refinement works when generation-evaluation gap is wide.
  Key results: Haiku 42/42, Sonnet 3/5, S4.6 constrained 2/3.
  Status: Phase 5 — drafting Methods section.")
```

Update memory after major decisions or phase transitions. This persists across sessions.

**`todo` tool** — track granular progress:

```
todo("add", "Design constrained task experiments for Sonnet 4.6")
todo("add", "Run Haiku baseline comparison")
todo("add", "Draft Methods section")
todo("update", id=3, status="in_progress")
todo("update", id=1, status="completed")
```

**Session startup protocol:**
```
1. todo("list")                           # Check current task list
2. memory("read")                         # Recall key decisions
3. terminal("git log --oneline -10")      # Check recent commits
4. terminal("ps aux | grep python")       # Check running experiments
5. terminal("ls results/ | tail -20")     # Check for new results
6. Report status to user, ask for direction
```

### Cron Monitoring with `cronjob`

Use the `cronjob` tool to schedule periodic experiment checks:

```
cronjob("create", {
  "schedule": "*/30 * * * *",  # Every 30 minutes
  "prompt": "Check experiment status:
    1. ps aux | grep run_experiment
    2. tail -30 logs/experiment_haiku.log
    3. ls results/haiku_baselines/
    4. If complete: read results, compute Borda scores, 
       git add -A && git commit -m 'Add Haiku results' && git push
    5. Report: table of results, key finding, next step
    6. If nothing changed: respond with [SILENT]"
})
```

**[SILENT] protocol**: When nothing has changed since the last check, respond with exactly `[SILENT]`. This suppresses notification delivery to the user. Only report when there are genuine changes worth knowing about.

**Deadline tracking**:
```
cronjob("create", {
  "schedule": "0 9 * * *",  # Daily at 9am
  "prompt": "NeurIPS 2025 deadline: May 22. Today is {date}. 
    Days remaining: {compute}. 
    Check todo list — are we on track? 
    If <7 days: warn user about remaining tasks."
})
```

### Communication Patterns

**When to notify the user** (via your direct/final response, or a cron `deliver:` target for unattended runs):
- Experiment batch completed (with results table)
- Unexpected finding or failure requiring decision
- Draft section ready for review
- Deadline approaching with incomplete tasks

**When NOT to notify:**
- Experiment still running, no new results → `[SILENT]`
- Routine monitoring with no changes → `[SILENT]`
- Intermediate steps that don't need attention

**Report format** — always include structured data:
```
## Experiment: <name>
Status: Complete / Running / Failed

| Task | Method A | Method B | Method C |
|------|---------|---------|---------|
| Task 1 | 85.2 | 82.1 | **89.4** |

Key finding: <one sentence>
Next step: <what happens next>
```

### Decision Points Requiring Human Input

Use `clarify` for targeted questions when genuinely blocked:

| Decision | When to Ask |
|----------|-------------|
| Target venue | Before starting paper (affects page limits, framing) |
| Contribution framing | When multiple valid framings exist |
| Experiment priority | When TODO list has more experiments than time allows |
| Submission readiness | Before final submission |

**Do NOT ask about** (be proactive, make a choice, flag it):
- Word choice, section ordering
- Which specific results to highlight
- Citation completeness (draft with what you find, note gaps)

---

## Reviewer Evaluation Criteria

Understanding what reviewers look for helps focus effort:

| Criterion | What They Check |
|-----------|----------------|
| **Quality** | Technical soundness, well-supported claims, fair baselines |
| **Clarity** | Clear writing, reproducible by experts, consistent notation |
| **Significance** | Community impact, advances understanding |
| **Originality** | New insights (doesn't require new method) |

**Scoring (NeurIPS 6-point scale):**
- 6: Strong Accept — groundbreaking, flawless
- 5: Accept — technically solid, high impact
- 4: Borderline Accept — solid, limited evaluation
- 3: Borderline Reject — weaknesses outweigh
- 2: Reject — technical flaws
- 1: Strong Reject — known results or ethics issues

See [references/reviewer-guidelines.md](references/reviewer-guidelines.md) for detailed guidelines, common concerns, and rebuttal strategies.

---

## Common Issues and Solutions

| Issue | Solution |
|-------|----------|
| Abstract too generic | Delete first sentence if it could prepend any ML paper. Start with your specific contribution. |
| Introduction exceeds 1.5 pages | Split background into Related Work. Front-load contribution bullets. |
| Experiments lack explicit claims | Add: "This experiment tests whether [specific claim]..." before each one. |
| Reviewers find paper hard to follow | Add signposting, use consistent terminology, make figure captions self-contained. |
| Missing statistical significance | Add error bars, number of runs, statistical tests, confidence intervals. |
| Scope creep in experiments | Every experiment must map to a specific claim. Cut experiments that don't. |
| Paper rejected, need to resubmit | See Conference Resubmission in Phase 7. Address reviewer concerns without referencing reviews. |
| Missing broader impact statement | See Step 5.10. Most venues require it. "No negative impacts" is almost never credible. |
| Human eval criticized as weak | See Step 2.5 and [references/human-evaluation.md](references/human-evaluation.md). Report agreement metrics, annotator details, compensation. |
| Reviewers question reproducibility | Release code (Step 7.9), document all hyperparameters, include seeds and compute details. |
| Theory paper lacks intuition | Add proof sketches with plain-language explanations before formal proofs. See [references/paper-types.md](references/paper-types.md). |
| Results are negative/null | See Phase 4.3 on handling negative results. Consider workshops, TMLR, or reframing as analysis. |

---

## Reference Documents

| Document | Contents |
|----------|----------|
| [references/writing-guide.md](references/writing-guide.md) | Gopen & Swan 7 principles, Perez micro-tips, Lipton word choice, Steinhardt precision, figure design |
| [references/citation-workflow.md](references/citation-workflow.md) | Citation APIs, Python code, CitationManager class, BibTeX management |
| [references/checklists.md](references/checklists.md) | NeurIPS 16-item, ICML, ICLR, ACL requirements, universal pre-submission checklist |
| [references/reviewer-guidelines.md](references/reviewer-guidelines.md) | Evaluation criteria, scoring, common concerns, rebuttal template |
| [references/sources.md](references/sources.md) | Complete bibliography of all writing guides, conference guidelines, APIs |
| [references/experiment-patterns.md](references/experiment-patterns.md) | Experiment design patterns, evaluation protocols, monitoring, error recovery |
| [references/autoreason-methodology.md](references/autoreason-methodology.md) | Autoreason loop, strategy selection, model guide, prompts, scope constraints, Borda scoring |
| [references/human-evaluation.md](references/human-evaluation.md) | Human evaluation design, annotation guidelines, agreement metrics, crowdsourcing QC, IRB guidance |
| [references/paper-types.md](references/paper-types.md) | Theory papers (proof writing, theorem structure), survey papers, benchmark papers, position papers |

### LaTeX Templates

Templates in `templates/` for: **NeurIPS 2025**, **ICML 2026**, **ICLR 2026**, **ACL**, **AAAI 2026**, **COLM 2025**.

See [templates/README.md](templates/README.md) for compilation instructions.

### Key External Sources

**Writing Philosophy:**
- [Neel Nanda: How to Write ML Papers](https://www.alignmentforum.org/posts/eJGptPbbFPZGLpjsp/highly-opinionated-advice-on-how-to-write-ml-papers)
- [Sebastian Farquhar: How to Write ML Papers](https://sebastianfarquhar.com/on-research/2024/11/04/how_to_write_ml_papers/)
- [Gopen & Swan: Science of Scientific Writing](https://cseweb.ucsd.edu/~swanson/papers/science-of-writing.pdf)
- [Lipton: Heuristics for Scientific Writing](https://www.approximatelycorrect.com/2018/01/29/heuristics-technical-scientific-writing-machine-learning-perspective/)
- [Perez: Easy Paper Writing Tips](https://ethanperez.net/easy-paper-writing-tips/)

**APIs:** [Semantic Scholar](https://api.semanticscholar.org/api-docs/) | [CrossRef](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | [arXiv](https://info.arxiv.org/help/api/basics.html)

**Venues:** [NeurIPS](https://neurips.cc/Conferences/2025/PaperInformation/StyleFiles) | [ICML](https://icml.cc/Conferences/2025/AuthorInstructions) | [ICLR](https://iclr.cc/Conferences/2026/AuthorGuide) | [ACL](https://github.com/acl-org/acl-style-files)
