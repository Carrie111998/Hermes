# Test run

Command (from the worktree root):

```bash
npx vitest run src/components/pane-shell --root apps/desktop
```

`apps/desktop/package.json` exposes the same runner as `"test": "vitest run"`
and `"test:ui": "vitest run --project ui"`; `--root apps/desktop` is the
invocation that loads `apps/desktop/vitest.config.ts` from the repo root.

Output tail:

```
 RUN  v4.1.10 /private/tmp/claude-501/-Users-thomasbekkers/bf1669cd-23fa-44d8-b3fd-a4cac1c102f6/scratchpad/hermes-fix/apps/desktop

 Test Files  31 passed (31)
      Tests  173 passed (173)
   Start at  19:30:12
   Duration  9.60s (transform 9.10s, setup 7.80s, import 14.93s, tests 12.04s, environment 39.66s)
```

Includes the new `renderer/collapse-restore-affordance.test.tsx` (31st file)
covering both #91223 and the docked-tile repro.
