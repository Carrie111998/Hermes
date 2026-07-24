# Issue Triage Workflow

When asked to triage issues:

1. **List untriaged issues:**

```bash
# With gh
gh issue list --label "needs-triage" --state open

# With curl
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/issues?labels=needs-triage&state=open" \
  | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    if 'pull_request' not in i:
        print(f\"#{i['number']}  {i['title']}\")"
```

2. **Read and categorize** each issue (view details, understand the bug/feature)

3. **Apply labels and priority** — see `issue-commands.md` → "Labels". Use only
   labels that already exist in the repo.

4. **Assign** if the owner is clear — only to a person the user named.

5. **Comment with triage notes** if needed

## Categorization Guide

| Signal in the issue | Typical label |
|---|---|
| Reproducible defect with steps | `bug` |
| Request for new capability | `enhancement` / `feature` |
| Docs wrong, missing, or unclear | `documentation` |
| Question, not an actionable change | `question` — answer and close |
| Cannot reproduce, no steps given | ask for repro, apply `needs-info` |
| Duplicate of an existing issue | comment with the original number, close as `not planned` |
| Out of scope / won't do | `wontfix` — needs the maintainer's call, not yours |

## Priority Heuristics

- Data loss, security issue, or total outage → highest priority, flag to the user
  immediately rather than just labelling
- Broken core workflow with no workaround → high
- Broken edge case or has a workaround → medium
- Cosmetic, nice-to-have, or speculative → low

## Reporting Back

Summarize the triage pass for the user: how many issues reviewed, what label and
priority each got, which need a human decision (duplicates, `wontfix`, scope calls),
and which were left untouched and why. Do not close anything that requires a
maintainer judgement call without the user's approval.
