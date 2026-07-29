# Headhunter Review: Curriculum Overhaul Recommendations

**Based on:** Anthropic Skilljar catalog (actual course content), Coursera syllabi research
**For:** Chin (0xsteamboat), Singapore SGT
**Current level:** Intermediate — has running agent, MCP, basic RAG

---

## Phase 1 (Weeks 1-2): Master Your Agent — ✅ Keep, minor tweak

| Course | Status | Score | Notes |
|--------|--------|:-----:|-------|
| Intro to Agent Skills | ✅ Done | 7 | Correct level |
| Intro to Subagents | ✅ Done | 7 | Correct level |
| Intro to MCP | 🔜 Tue | 8 | Essential — keep |
| MCP Advanced Topics | 🔜 Wed | 9 | **Critical** — production MCP patterns |
| AI Capabilities & Limitations | 🔜 Thu | 5 | Quick 15min, no harm keeping |

**Verdict:** Phase 1 is solid. No changes needed.

---

## Phase 2 (Weeks 3-5): Business Application — 🔴 Pivot needed

**Current:** Agentic AI & AI Agents for Leaders (Vanderbilt) + AI Mastery for Professionals (Vanderbilt)

**Hirability Score: 3/10.** Why:
- These are **executive/managerial** courses. They teach what agents *are*, not how to build/debug them.
- For a technical practitioner, these are resume filler, not skill builder.
- Singapore employers want **builders** who can ship, not leaders who can talk about AI strategy.

**Replace with these Skilljar courses:**

| Course | Duration | Score | Why |
|--------|:--------:|:-----:|-----|
| **Building with the Claude API** | Full course | **9/10** | Covers tool-use, function calling, API patterns — the exact skills senior AI roles test in interviews |
| **MCP: Advanced Topics** | Full course | **9/10** | Production MCP: sampling, notifications, transport, file system — immediately deployable in Hermes |
| **Claude with Amazon Bedrock** | Full course | **8/10** | Enterprise deployment (GovTech, DBS, OCBC all use AWS). Huge Singapore relevance |

**Why this swap:**

| Vanderbilt (Current) → | Skilljar (Proposed) |
|---|---|
| Managerial theory | Production skills |
| Generic frameworks | Specific Claude/MCP patterns |
| Doesn't differentiate you | Makes you a certified builder |
| No interview impact | Directly testable in technical screens |

---

## Phase 3 (Weeks 6-8): Build Sprint — ✅ Keep, add enterprise edge

**Hirability Score: 8/10** as structured, but can go to 10 with one tweak.

| Current | Recommended | Why |
|---------|-------------|-----|
| Practical AI Agents 2026 (Packt) | **Drop** — too generic, overlaps what you know | Replace with Bedrock/Vertex AI course time |
| Build Sprint — pick ONE product | **Keep**, but target Singapore-specific | See below |
| Case study + LinkedIn content | **Keep** | Essential for signalling seniority |

**Build Sprint recommendation:** Build and publish a **custom MCP server** that solves a Singapore-relevant problem:
- Integration with a local data source (e.g. MAS financial data, LTA transport APIs, or a mock ERP)
- Security layer (auth, rate limiting, audit logging) — signals production thinking
- Publish on GitHub + write the LinkedIn case study about the *challenges* (latency, auth, token costs)

This single project checks every box: technical depth, Singapore relevance, production thinking, public signal.

---

## Full Revised Curriculum

| Phase | Week | What | Score |
|:-----|:----:|------|:-----:|
| **1** | W1 | Intro to Agent Skills ✅ → Subagents ✅ → MCP Intro → Experiments | 8 |
| | W2 | MCP Advanced Topics → AI Capabilities → Free Build | 9 |
| **2** | W3 | **Building with the Claude API** (Skilljar) → Apply to Hermes | 9 |
| | W4 | **MCP: Advanced Topics** deep-dive → Build custom MCP server prototype | 9 |
| | W5 | **Claude with Amazon Bedrock** (or Vertex AI) → Deploy MCP server to cloud | 8 |
| **3** | W6 | Singapore-specific Build Sprint — production MCP server with security | 10 |
| | W7 | Polish, test, benchmark → write case study | 10 |
| | W8 | LinkedIn content series, portfolio page, next phase planning | 9 |

**Overall hirability impact: 7.5/10** (up from original 4.5/10)

---

## Immediate Action Items

1. Replace the 2 Vanderbilt courses with **Building with the Claude API** + **MCP: Advanced Topics** on Skilljar
2. Move **Claude with Amazon Bedrock** into Phase 2 (or Phase 3 if time permits)
3. Drop the Packt course entirely
4. Target the Build Sprint at a Singapore-specific MCP server with security/governance

These changes make the curriculum **immediately relevant**, technically deep, and Singapore-market aligned. No theory, no fluff — just skills that get tested in interviews.