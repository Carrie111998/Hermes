# Headhunter Final Report: Complete Course Catalog & Recommendations

**Date:** 1 Jun 2026
**For:** Chin (0xsteamboat), Singapore
**Goals (2-month, re-eval Aug 2026):**
1. Build products (passive income)
2. Coach/teach others about AI agents
3. Be hyper-hirable in Singapore AI market

---

## A. TOP 10 Course Recommendations (Prioritised)

| # | Course | Source | Cost | Duration | Score | Why for Chin |
|:-:|--------|--------|:----:|:--------:|:----:|--------------|
| 1 | **MCP: Advanced Topics** | Skilljar | Free | Full course | **10** | Already in W1 — production MCP is your biggest differentiator. Builds immediately into Hermes |
| 2 | **Building with the Claude API** | Skilljar | Free | Full course | **9** | Full API mastery — tool-use, function calling, streaming. Interview gold |
| 3 | **Machine Learning Eng. for Production (MLOps)** | Coursera | Sub | 3mo spec | **9** | Industry gold standard (DeepLearning.AI). #1 hiring gap in Singapore right now |
| 4 | **AI Agents and MLOps for Production-Ready AI** | Coursera | Sub | Course | **9** | LangGraph, CrewAI, containerisation — directly into your stack |
| 5 | **Claude with Amazon Bedrock** | Skilljar | Free | Full course | **8** | Enterprise deployment — GovTech, DBS, OCBC all on AWS. Massive SG relevance |
| 6 | **Claude with Google Cloud Vertex AI** | Skilljar | Free | Full course | **8** | Same, GCP track. Dual cloud coverage = stronger resume signal |
| 7 | **MLOps and Responsible AI Practices** | Coursera | Sub | Course | **8** | Microsoft. Governance, compliance — maps to Singapore's AI Verify framework |
| 8 | **Advanced RAG** | DeepLearning.AI | Free | 1-2h | **8** | Short, focused. Covers vector DBs, retrieval eval — gaps in your current RAG |
| 9 | **LLMOps** | DeepLearning.AI | Free | 1-2h | **8** | Production LLM deployment patterns. Complements the Coursera MLOps |
| 10 | **Agentic AI** | DeepLearning.AI | Free | 1-2h | **7** | Good overview — quick win, overlaps some knowledge but fills gaps |

**Total cost: $0** (Skilljar + DeepLearning.AI are free, Coursera is already paid for)

---

## B. Full Course Catalog by Source

### Anthropic Skilljar (Free) — 13 courses found

| Course | For Chin? | Score | Notes |
|--------|:---------:|:-----:|-------|
| Claude 101 | ❌ Skip | 2 | Too basic |
| Claude Code 101 | ❌ Skip | 3 | You use Hermes/DeepSeek, not Claude Code |
| Intro to Claude Cowork | ❌ Skip | 3 | Different paradigm |
| Claude Code in Action | ❌ Skip | 3 | Claude Code specific |
| AI Fluency: Framework & Foundations | ❌ Skip | 2 | Beginner |
| **Building with the Claude API** | **✅ Take** | **9** | **Full API patterns — tool-use, fn calling, streaming** |
| **Intro to Model Context Protocol** | ✅ Done | 8 | Already scheduled |
| AI Fluency for Educators | ❌ Skip | 3 | Not your audience |
| AI Fluency for Students | ❌ Skip | 2 | Way too basic |
| **MCP: Advanced Topics** | **✅ Take** | **10** | **Production MCP — sampling, notifications, transport** |
| **Claude with Amazon Bedrock** | **✅ Take** | **8** | **Enterprise AWS deployment** |
| **Claude with Google Cloud Vertex AI** | **✅ Take** | **8** | **Enterprise GCP deployment** |
| Teaching AI Fluency | ❌ Skip | 4 | If you teach, maybe later |
| AI Fluency for Nonprofits | ❌ Skip | 2 | Not relevant |
| Intro to Agent Skills | ✅ Done | 7 | Completed |
| Intro to Subagents | ✅ Done | 7 | Completed |
| AI Capabilities & Limitations | 🔜 Scheduled | 5 | Quick, keep |

### Coursera (Subscription — utilise it)

| Course | Partner | Score | Why |
|--------|---------|:-----:|-----|
| **Machine Learning Eng. for Production (MLOps)** | DeepLearning.AI | **9** | Gold standard. CI/CD for ML, deployment, monitoring |
| **AI Agents and MLOps for Production-Ready AI** | Packt | **9** | LangGraph, CrewAI, containerisation — most directly applicable |
| **MLOps and Responsible AI Practices** | Microsoft | **8** | Governance, compliance, AI Verify mapping |
| Generative AI with LLMs | DeepLearning.AI | 7 | Good but overlaps what you know — lower priority |
| Vanderbilt Leadership specs | ❌ Skip | 3 | Managerial, not technical |

### DeepLearning.AI (Free short courses)

| Course | Score | Why |
|--------|:-----:|-----|
| **Advanced RAG** | **8** | Vector DBs, retrieval evaluation — gaps in your RAG |
| **LLMOps** | **8** | Production LLM deployment — complements Coursera MLOps |
| **Agentic AI** | **7** | Good overview — overlaps some knowledge |
| Building Systems with ChatGPT API | 6 | Good patterns but OpenAI-specific |

### Hugging Face (Free)

| Course | Score | Why |
|--------|:-----:|-----|
| **Hugging Face Course** | **7** | Deep transformers understanding — good foundation but not urgent given your current level |

### Weights & Biases (Free)

| Course | Score | Why |
|--------|:-----:|-----|
| **MLOps and Experiment Tracking** | **7** | Useful for production monitoring — take alongside Coursera MLOps |

### Google Cloud Skills Boost (Free tier)

| Course | Score | Why |
|--------|:-----:|-----|
| Vertex AI Search & Conversation | 7 | Useful if you go GCP route |
| MLOps on Vertex AI | 7 | Complements Skilljar's Vertex AI course |

### Microsoft Learn (Free)

| Course | Score | Why |
|--------|:-----:|-----|
| Azure AI Fundamentals | 5 | Basic, but if you teach NTUC workshops might be useful |
| Build AI Agents with Azure | 7 | Agent patterns on Azure |

### OpenAI (Free)

| Course | Score | Why |
|--------|:-----:|-----|
| OpenAI Cookbook / Docs | 6 | Reference, not structured course. Good for patterns |

---

## C. Revised 60-Day Curriculum (Final)

```
PHASE 1: MASTER YOUR AGENT (Weeks 1-2) — Build the foundation
  W1 Mon:  Agent Skills ✅ + Subagents ✅
  W1 Tue:  Intro to MCP [Skilljar] + Experiment
  W1 Wed:  MCP: Advanced Topics [Skilljar] + Build custom tool
  W1 Thu:  AI Capabilities [Skilljar] + Write-up
  W1 Fri-Sat: Free Build — apply MCP patterns to Hermes
  W1 Sun:  Rest
  W2:      Deepen experiments, review Phase 1

PHASE 2: PRODUCTION ENGINEERING (Weeks 3-5) — The pivot
  W3 Skilljar: Building with the Claude API
  W3 Coursera: MLOps Specialization (DeepLearning.AI) — start
  W3 Free: Advanced RAG + Agentic AI [DeepLearning.AI] — quick wins
  W4 Skilljar: MCP Advanced deep-dive — build custom MCP server prototype
  W4 Coursera: AI Agents + MLOps for Production (Packt) — start
  W4 Free: LLMOps [DeepLearning.AI] — quick win
  W5 Skilljar: Claude with Amazon Bedrock + Vertex AI
  W5 Coursera: Responsible AI Practices (Microsoft) — start
  W5 Free: MLOps + Experiment Tracking [W&B]

PHASE 3: BUILD + TEACH (Weeks 6-8) — Ship + signal
  W6: Build production MCP server (Singapore-specific — MAS API, mock ERP, or local connector)
      Add: security layer (auth, rate limiting), CI/CD, monitoring
  W7: Production hardening — tests, benchmarks, latency/cost optimization
      Write: HN/technical blog post about the challenges
  W8: LinkedIn content series (3 posts)
      Prepare: NTUC workshop material based on what you built
      Portfolio page + GitHub README
```

---

## D. Hirability Impact Assessment

### Skills This Curriculum Builds

| Skill | Source | Interview relevance |
|-------|--------|:------------------:|
| MCP server development (production-grade) | Skilljar W1-W4 | 🔥 High — MCP is exploding |
| Claude API mastery (tool-use, function calling) | Skilljar W3 | 🔥 High |
| MLOps (CI/CD, deployment, monitoring) | Coursera W3-W5 | 🔥 High — #1 SG gap |
| Multi-agent orchestration (LangGraph, CrewAI) | Coursera W4 | 🔥 High |
| Cloud deployment (AWS Bedrock + GCP Vertex) | Skilljar W5 | 🔥 High |
| AI governance & compliance (AI Verify) | Coursera W5 | ✅ Medium — differentiator |
| Advanced RAG (vector DBs, retrieval eval) | DeepLearning.AI W3 | ✅ Medium |
| LLMOps (production deployment patterns) | DeepLearning.AI W4 | ✅ Medium |

### Target Singapore Job Roles
- **AI Engineer** (GovTech, DBS, OCBC, Grab, Sea)
- **ML Ops Engineer** (emerging as separate role in SG)
- **Agentic Systems Developer** (new — few people have this)
- **AI Solutions Architect** (consulting, enterprise)

### Estimated Hirability Trajectory

| Time | Score | What changed |
|:----:|:-----:|--------------|
| **Now** | 5/10 | Has agent, MCP, basic RAG — above average |
| **After Phase 1** | 6/10 | MCP expert, deeper Hermes mastery |
| **After Phase 2** | 8/10 | MLOps + production skills + cloud deployment |
| **After Phase 3** | 9/10 | Public portfolio, case studies, teaching material |

### How Each Goal Is Served

| Goal | How the curriculum serves it |
|------|-----------------------------|
| **Build products (passive income)** | Phase 2 production skills + Phase 3 build sprint = shippable agent products. MCP server can be sold/templated |
| **Coach/teach others** | Phase 3 workshop prep. Deep MCP + MLOps knowledge = content you can teach at NTUC |
| **Be hyper-hirable** | Phase 2 MLOps + cloud + governance = exactly what Singapore employers ask for in technical screens |
