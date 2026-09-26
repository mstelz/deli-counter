---
name: deli
description: Run CPU- or memory-heavy commands (test suites, typecheck, lint, builds, bundling, docker builds, model evals, headless browsers) through the `deli` work queue so parallel agents on the machine do not overload it and get killed half-way. Use before any such command, when briefing sub-agents that will run one, or when a run died or timed out with no failed assertion.
---

# deli — the machine-wide shared work queue

This machine runs many AI agents (Claude, Codex, Gemini) across several projects at once. When heavy
commands overlap, the machine is throttled, tests time out, and a task killer ends runs half-way. The
work is lost and the agent sits waiting on a result that never comes.

`deli` (on PATH, `~/.local/bin/deli`) makes heavy commands wait their turn:

```bash
deli -- pnpm --filter core test src/foo.test.ts
deli --label my-task -- cargo build --release
deli --status          # who holds each slot, who is waiting
deli --slots           # the current machine-wide slot count
```

- It takes a lock per checkout (git toplevel), since agents in one checkout often share a test
  database or build output, then one of N machine-wide slots. The user sets N; agents must not
  change it with `deli --slots N`, even when the queue is slow.
- Locks die with the process, so a killed run never leaves a stale lock.
- The exit code is the command's own.

## Rules

1. **Queue anything heavy:** test runs, typecheck, lint of a whole repo, builds, bundlers, docker
   builds, headless browsers, local model or eval runs. Quick reads (`git`, `grep`, `cat`, one
   small script) do not need it.
2. **Expect to wait.** Give the Bash call a long timeout (600000 ms) or `run_in_background: true`.
   Do not kill a queued command because it is quiet: `deli --status` shows it waiting.
3. **Put it in every sub-agent brief** that will run tests or builds, with the exact command.
4. **A run that dies or times out with no failed assertion is load, not a defect.** Check
   `deli --status` and `uptime`, then re-run it through the queue before debugging.
5. Keep project test caps as well (for example `--maxWorkers=1`). The queue limits how many runs
   overlap; the caps limit how heavy each run is.
