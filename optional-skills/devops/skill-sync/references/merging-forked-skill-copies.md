# Merging forked skill copies into one master

`sync.sh` is **last-writer-wins by mtime** — it's lossy when the SAME skill forked on
two machines (each side added unique references/sections). The newer mtime wins wholesale
and silently drops the other side's unique content. When you need a UNION (keep everything
from every copy), do a manual semantic merge instead of relying on sync.

## When this applies
- Two+ machines edited the same skill independently (different references added on each).
- You want one canonical "master" dir combining all copies, optionally folding a second
  related skill into it.
- Detection: a sync dry-run shows the skill as `[^] remote newer`, but on inspection the
  LOCAL copy also has files/sections the remote lacks (not a clean superset).
  `p2p_sync.py` classifies these as `divergent`.

## Procedure

### 1. Stage every copy side by side
```bash
STAGE=$(mktemp -d)/skill-merge-staging; mkdir -p "$STAGE"
rsync -aL ~/.hermes/skills/<cat>/<skill>/ "$STAGE/<skill>-local/"
rsync -aL user@host:'.hermes/skills/<cat>/<skill>/' "$STAGE/<skill>-remote/"
# repeat per source
```

### 2. Map the divergence (don't eyeball — compute it)
```bash
# files unique to each side
comm -23 <(cd A && find references -type f|sort) <(cd B && find references -type f|sort)  # A-only
comm -13 <(cd A && find references -type f|sort) <(cd B && find references -type f|sort)  # B-only
# files in BOTH but byte-different (the dangerous ones — possible forks, not supersets)
for f in $(comm -12 <(cd A && find references -type f|sort) <(cd B && find references -type f|sort)); do
  diff -q "A/$f" "B/$f" >/dev/null || echo "DIFF $f ($(wc -l<A/$f)L vs $(wc -l<B/$f)L)"
done
```

### 3. Build the file-tree union
- For each relative path, if all copies are byte-identical → take any.
- If they differ → **diff them**. A clean superset (one strictly contains the other) →
  keep the longer. A genuine FORK (each has lines the other dropped, even a 1-line delta)
  → 3-way union the content by hand. **A length-pick is WRONG for forks** — verify
  superset-ness before trusting "keep the longer". The trap: a 254-vs-253-line file is
  almost never a superset; it's two edits that each removed a different line.
- **A near-tie can be a de-personalizing fork — keep the SCRUBBED copy, not the longer
  one.** When two copies differ by only a few bytes, diff them: if one side replaced
  personal names, hostnames, or internal service names with generic terms, that scrubbed
  copy is the canonical one even if it's a byte or two shorter. A blind length-pick or
  newest-wins RE-INTRODUCES the names you already cleaned. After merging, grep the whole
  master for the scrubbed terms to confirm zero leaked back in from the other fork.

### 4. Merge the SKILL.md body
- Pick the structurally-newer fork as base (the one with more recent top-level sections).
- Find content sections (`##`/`###` headers) present in the OTHER fork but not the base;
  graft them in at the right anchor.
- Merge the top reference-link index: append link one-liners the base lacks (match by the
  `references/<file>.md` filename, not header text).
- Fix source bugs you find: duplicate/empty headers, corrupted links (mashed/missing
  backticks). These accumulate across forks.

### 5. Folding skill B into skill A
- Strip B's frontmatter + its trailing `## Linked Files` (the loader regenerates that).
- Demote B's H1 to a banner section, append under a clear divider in A.
- Merge B's references/scripts/templates into A's tree (union, same rules as step 3).

### 6. Validate before declaring done
- Every cited artifact (`references/X.md`, `scripts/Y`, `templates/Z`) must exist in the
  tree. Parse the citations from SKILL.md and diff against an `os.walk` of the dir.
- A dangling link that points at a path in a TARGET CODEBASE (a repo file, not a skill
  asset) is fine — don't "fix" it.
- Uncited reference files are fine to keep (reachable via cross-links inside other refs);
  a master is allowed to over-include.
- **Any name scrub must survive the merge.** `grep -ri "<scrubbed-name>" <master>/` after
  building. If a folded fork re-introduced a name the canonical copy had cleaned, scrub it
  in the merged sections.

## N-way merges (more than two sources)
The same procedure scales, but order matters. A first pass may produce a "master" that a
LATER-discovered copy (a backup archive, a box that was offline during the first sync)
turns out to be a richer fork of — mostly non-overlapping references. Re-merge: treat
the new copy as just another source, union its file tree in, fold its unique SKILL.md
sections, re-apply any hand-unions (a 3-way union built in the first pass can get
clobbered by a bigger base from the new source — re-inject the lost blocks). Bump the
version each pass. Don't assume the first master is canonical; the biggest/richest fork
can arrive last.

## Pitfalls
- **Scripting the union beats hand-copying** — with 100+ files across several sources, do
  it in code (os.walk + content-dedup + longest-or-flag-conflict) and print only the
  conflicts/uniques for human review. Mechanical union is deterministic; only the
  byte-conflicts need judgment.
- **Don't install the master blind.** Build it in a scratch dir, validate, then let the
  user decide whether to install over the live copy. The existing skill keeps working
  meanwhile.
- **mtime lies about content.** A copy can be newer AND smaller (someone trimmed it).
  Newer ≠ superset. Always diff.
