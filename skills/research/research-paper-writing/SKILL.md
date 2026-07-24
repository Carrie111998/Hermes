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
    related_skills: [arxiv, ml-paper-writing, subagent-driven-development, plan]
    requires_toolsets: [terminal, files]

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
- **Writing non-empirical papers** — theory, survey, benchmark, or position papers
- **Designing human evaluations** for NLP, HCI, or alignment research
- **Preparing post-acceptance deliverables** — posters, talks, code releases

Do **not** use it for: general technical writing, blog posts, or documentation.

---

## Core Philosophy

1. **Be proactive.** Deliver complete drafts, not questions. Scientists are busy — produce something concrete they can react to, then iterate.
2. **Never hallucinate citations.** AI-generated citations have ~40% error rate. Always fetch programmatically. Mark unverifiable citations as `[CITATION NEEDED]`.
3. **Paper is a story, not a collection of experiments.** Every paper needs one clear contribution stated in a single sentence. If you can't do that, the paper isn't ready.
4. **Experiments serve claims.** Every experiment must explicitly state which claim it supports. Never run experiments that don't connect to the paper's narrative.
5. **Commit early, commit often.** Every completed experiment batch, every paper draft update — commit with descriptive messages. Git log is the experiment history.

### Red Lines (never violate)

- **No BibTeX from memory.** Search → verify in 2+ sources → retrieve via DOI content negotiation → validate the claim → add. If any step fails, write `[CITATION NEEDED]` and tell the scientist how many placeholders exist.
- **Numbers in the paper must trace to a result file.** Anything untraceable gets flagged `[VERIFY]`, never smoothed over.
- **Never edit conference `.sty` files**, never shrink font/margins to fit a page limit, never drop a required section (Limitations, Broader Impact).
- **Every experiment maps to a claim.** If it doesn't, don't run it.
- **Report failures honestly.** Failed experiments and negative results go in the paper, not the trash.
- **Anonymity is binary.** For double-blind venues, one leaked author name or personal repo URL can mean desk rejection.

### Proactivity: Draft First, Ask With the Draft

| Confidence Level | Action |
|-----------------|--------|
| **High** (clear repo, obvious contribution) | Write full draft, deliver, iterate on feedback |
| **Medium** (some ambiguity) | Write draft with flagged uncertainties, continue |
| **Low** (major unknowns) | Ask 1-2 targeted questions via `clarify`, then draft |

Draft every section autonomously (abstract, intro, methods, experiments, related work) and flag the assumption you made. **Block for input only when**: target venue unclear, multiple contradictory framings, results seem incomplete, or the user explicitly asked to review first.

---

## Routing Table — read the reference before doing the work

| To do this | Read |
|-----------|------|
| Set up the workspace, git, TODO, compute budget, multi-author workflow | [references/project-setup.md](references/project-setup.md) |
| Find related work, run iterative literature search, verify citations | [references/literature-review.md](references/literature-review.md) |
| Full citation APIs and the `CitationManager` class | [references/citation-workflow.md](references/citation-workflow.md) |
| Map claims to experiments, design baselines and evaluation protocol, plan human eval | [references/experiment-design.md](references/experiment-design.md) |
| Experiment infrastructure, judge panels, statistical tests, task design, plotting | [references/experiment-patterns.md](references/experiment-patterns.md) |
| Launch/monitor long runs, recover from failures, keep an experiment journal | [references/execution-monitoring.md](references/execution-monitoring.md) |
| Aggregate results, compute significance, handle negative results, write `experiment_log.md` | [references/result-analysis.md](references/result-analysis.md) |
| Design annotation guidelines, agreement metrics, crowdsourcing QC, IRB | [references/human-evaluation.md](references/human-evaluation.md) |
| Choose a refinement strategy for a model tier / task type | [references/refinement-strategy.md](references/refinement-strategy.md) |
| Run the full autoreason loop (prompts, Borda scoring, scope constraints, budgets) | [references/autoreason-methodology.md](references/autoreason-methodology.md) |
| Draft the paper section by section (title → abstract → Figure 1 → ... → ethics, datasheets) | [references/drafting-workflow.md](references/drafting-workflow.md) |
| Sentence-level writing craft, word choice, figure design | [references/writing-guide.md](references/writing-guide.md) |
| Set up a conference template, LaTeX preamble, tables, TikZ, algorithm2e, latexdiff, SciencePlots | [references/latex-toolkit.md](references/latex-toolkit.md) |
| Compile a template (VS Code / CLI / Overleaf) | [templates/README.md](templates/README.md) |
| Simulate reviews, run visual + claim-verification passes, revise, write a rebuttal | [references/self-review-revision.md](references/self-review-revision.md) |
| Understand what reviewers score and how, per venue | [references/reviewer-guidelines.md](references/reviewer-guidelines.md) |
| Anonymize, validate, compile, convert venues, prepare camera-ready, post to arXiv, release code | [references/submission-prep.md](references/submission-prep.md) |
| Complete a venue's mandatory checklist (NeurIPS 16-item, ICML, ICLR, ACL) | [references/checklists.md](references/checklists.md) |
| Build a poster, talk, or blog post after acceptance | [references/post-acceptance.md](references/post-acceptance.md) |
| Write a theory / survey / benchmark / position / workshop / short paper | [references/paper-types.md](references/paper-types.md) |
| Use Hermes tools, delegation, cron monitoring, `memory`/`todo` state | [references/hermes-integration.md](references/hermes-integration.md) |
| Diagnose a specific symptom ("abstract too generic", "reviewers question reproducibility") | [references/common-issues.md](references/common-issues.md) |
| Trace guidance back to its source (Nanda, Farquhar, Gopen & Swan, Lipton, ...) | [references/sources.md](references/sources.md) |

---

## End-to-End Skeleton

The minimum viable path. Each step names the reference that expands it.

```
Phase 0  Setup            → explore repo, copy template dir, git init, TODO list,
                            one-sentence contribution, compute budget
                            [project-setup.md]
Phase 1  Literature       → breadth-then-depth search (2-3 rounds), verify every
                            citation programmatically, group by methodology
                            [literature-review.md, citation-workflow.md]
Phase 2  Design           → claim→experiment table, baselines (naive/strong/ablation/
                            compute-matched), metrics + stats + sample sizes,
                            incremental-saving scripts, human eval if needed
                            [experiment-design.md, experiment-patterns.md]
Phase 3  Execute          → nohup runs, cron status checks with [SILENT], resume-safe
                            re-runs, commit each batch, append to experiment journal
                            [execution-monitoring.md]
Phase 4  Analyze          → aggregate, significance + error bars, name the story,
                            vector figures + booktabs tables, write experiment_log.md
                            [result-analysis.md]
         Gate             → claims supported? → Phase 5. Otherwise → Phase 2.
Phase 5  Draft            → Figure 1 → abstract (5 sentences) → intro (≤1.5pp) →
                            methods → results → related work → limitations →
                            conclusion → appendix → ethics; two-pass refinement;
                            LaTeX quality checklist after every edit
                            [drafting-workflow.md, latex-toolkit.md, writing-guide.md]
Phase 6  Self-review      → 3-5 negatively-biased reviews + meta-review, VLM visual
                            pass, claim verification by a fresh sub-agent, revise
                            [self-review-revision.md, reviewer-guidelines.md]
Phase 7  Submit           → anonymization, chktex + cite/figure/label validation,
                            clean compile, venue checklist, arXiv timing, code release
                            [submission-prep.md, checklists.md]
Phase 8  Post-acceptance  → camera-ready, poster, talk, blog
                            [post-acceptance.md, submission-prep.md]
```

**Time allocation while drafting**: equal effort on (1) the abstract, (2) the introduction, (3) the figures, (4) everything else combined. Most reviewers form a judgment before reaching your methods.

**Context discipline**: `experiment_log.md` (Phase 4) is the bridge between results and prose. Load it — plus only the section-specific context — when drafting. Never load raw result JSONs into a drafting context; see [references/drafting-workflow.md](references/drafting-workflow.md).

---

## Venue Quick Facts

| Conference | Main File | Style File | Page Limit |
|------------|-----------|------------|------------|
| NeurIPS 2025 | `main.tex` | `neurips.sty` | 9 pages |
| ICML 2026 | `example_paper.tex` | `icml2026.sty` | 8 pages |
| ICLR 2026 | `iclr2026_conference.tex` | `iclr2026_conference.sty` | 9 pages |
| ACL 2025 | `acl_latex.tex` | `acl.sty` | 8 pages (long) |
| AAAI 2026 | `aaai2026-unified-template.tex` | `aaai2026.sty` | 7 pages |
| COLM 2025 | `colm2025_conference.tex` | `colm2025_conference.sty` | 9 pages |

**Universal**: double-blind, references don't count toward the limit, appendices unlimited, LaTeX required. Templates live in `templates/`.

Venue-specific extras (NeurIPS checklist, ICML broader impact, ICLR LLM disclosure, ACL limitations, AAAI style strictness) are in [references/submission-prep.md](references/submission-prep.md) and [references/checklists.md](references/checklists.md).

---

## Related Skills

| Skill | When | Load |
|-------|------|------|
| **arxiv** | Phase 1 literature search, BibTeX generation | `skill_view("arxiv")` |
| **subagent-driven-development** | Phase 5 parallel section drafting | `skill_view("subagent-driven-development")` |
| **plan** | Phase 0 structured planning | `skill_view("plan")` |
| **data-science** | Phase 4 interactive analysis | `skill_view("data-science")` |
| **diagramming** | Phase 4-5 figures and architecture diagrams | `skill_view("diagramming")` |

**This skill supersedes `ml-paper-writing`.** Full tool mapping and delegation patterns: [references/hermes-integration.md](references/hermes-integration.md).

---

## Key External Sources

**Writing philosophy:** [Neel Nanda](https://www.alignmentforum.org/posts/eJGptPbbFPZGLpjsp/highly-opinionated-advice-on-how-to-write-ml-papers) | [Sebastian Farquhar](https://sebastianfarquhar.com/on-research/2024/11/04/how_to_write_ml_papers/) | [Gopen & Swan](https://cseweb.ucsd.edu/~swanson/papers/science-of-writing.pdf) | [Lipton](https://www.approximatelycorrect.com/2018/01/29/heuristics-technical-scientific-writing-machine-learning-perspective/) | [Perez](https://ethanperez.net/easy-paper-writing-tips/)

**APIs:** [Semantic Scholar](https://api.semanticscholar.org/api-docs/) | [CrossRef](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | [arXiv](https://info.arxiv.org/help/api/basics.html)

**Venues:** [NeurIPS](https://neurips.cc/Conferences/2025/PaperInformation/StyleFiles) | [ICML](https://icml.cc/Conferences/2025/AuthorInstructions) | [ICLR](https://iclr.cc/Conferences/2026/AuthorGuide) | [ACL](https://github.com/acl-org/acl-style-files)

The writing-philosophy layer was originally compiled by [Orchestra Research](https://github.com/orchestra-research) as the `ml-paper-writing` skill. Full bibliography: [references/sources.md](references/sources.md).
