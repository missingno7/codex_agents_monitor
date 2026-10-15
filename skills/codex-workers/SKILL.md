---
name: codex-workers
description: Delegate work to ChatGPT/Codex (GPT) workers via `codex exec` — briefing, launching, waiting for, reading results of, continuing, redirecting and cancelling Codex agents, alone or many in parallel. Use whenever you are about to run `codex exec`, supervise Codex/GPT workers, or delegate tasks to Codex. Invoke it again after a compaction before launching more workers.
---

# Codex workers

`cx` (`python ~/.codex-dashboard/cx.py`) runs one `codex exec` worker to completion. Its output is:

```
[cx] <name> · started · gpt-6-luna/xhigh · session <id>          ← printed as soon as the worker exists
[cx] <name> · DONE · 12m03s · gpt-6-luna/xhigh · session <id>    ← at the end (or FAILED (exit N) + the real error)
<the worker's final answer>
```

Exit code 0 = done, 1 = failed. The header shows the model/effort that actually ran. It adds
`· 0 of N commands succeeded` when the worker's shell never worked (treat such a DONE as a failure) and
`· ran in <dir>` when Codex used a different directory (e.g. a managed worktree). The worker's
transcript never reaches you; the human watches it in the dashboard.

## When to delegate

Delegate independent, bounded units: investigating a function or module, implementing a change confined to
known files, running and analysing an experiment, reviewing a diff. Keep design decisions, acceptance and
writes to shared or canonical files for yourself, or give them to exactly one worker at a time.

## Brief

Each worker starts from zero: give it everything it needs in the prompt.
- Goal, and the files or area to work in (absolute paths).
- Constraints: what it must not touch, and which commands it may run.
- What "done" looks like, plus a stop rule: "if blocked, stop and report the exact evidence; don't guess".
- Where bulky output goes (a path the project owns) and the final-answer contract. The first line must be
  `STATUS: done | partial | blocked | failed`, followed by ≤15 lines: what changed, evidence (command → result),
  open questions.

Write the prompt to a file and redirect it (`< PROMPT.md`), or use a heredoc for short prompts. Don't
assemble prompts with `{ cat a; cat b; } | cx`: a wrong path silently truncates the prompt.

## Model and effort

Always pass `-m` and `-e`; the config default may be the most expensive model. Use a cheaper model at high
effort for bounded or mechanical work, and the strongest model for shared blockers, ambiguous problems or
integration. `--resume` keeps the session's model and effort.

## Launching (one background task per worker)

```bash
python ~/.codex-dashboard/cx.py run -n NAME -C D:/abs/repo -m MODEL -e EFFORT < D:/abs/path/PROMPT.md
```

- Each worker is its **own** Bash call with `run_in_background: true` and `description` = NAME. For N workers,
  make N such calls in one message. Never use a loop, `&`, `nohup` or output redirection: they lose the
  notification and TaskStop.
- **Never pass `timeout`** on a worker launch: it kills the worker at that time (2 h at most). Background commands
  have no time limit in an interactive session. (Headless `-p`/SDK runs do: 30 min default, raised with
  `BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS`.)
- Launch from your own session, not from inside a subagent: a subagent's background shells are reaped when it ends.
- Invoke exactly as `python …/cx.py`, never through a shell wrapper, or cancel won't reach Codex.
- Options: `-n NAME`, `-C DIR` (default: current dir), `-m`, `-e` (low/medium/high/xhigh), `-s SANDBOX`, and extra
  `codex exec` arguments after `--` (e.g. `-- --worktree`).

## While workers run

**Do other work, or end your turn. Never poll:** no sleep loops, no grepping output files, no `tail`. The
completion notification arrives by itself, naming the task. Then read its output file **once**.
`cx ps` (one line per running worker: model/effort, uptime, idle time, failed commands, last step) is only for
when something looks wrong, e.g. a worker taking far longer than its peers.

## Sandbox and parallel writers

- `workspace-write` (the default) lets the worker write only inside `-C`. `read-only` means no file writes at all,
  not even notes, so give it only pure investigation. `danger-full-access` has no protection and lets helper
  processes outlive the worker; use it only when the user agrees.
- Parallel writers need disjoint files, or each its own git worktree **passed as `-C`**. Telling a worker to
  "work in a worktree" while `-C` is the main checkout leaves the main checkout writable. Codex can also create
  one: `-- --worktree` (the header shows where it ran). Shared or canonical files get a single writer.
- Workers must not leave background processes running. Say so in the brief when they start servers or long
  scripts.
- The sandbox adds a few seconds to every command, more with many workers (measured: ~1.4 s alone, ~4.6 s at 18
  concurrent workers). That's expected, not a reason to drop the sandbox. If even trivial commands take *minutes*
  (e.g. `echo`), stop launching, run one small probe, and ask the user how to proceed.

## Results, continuation, cancellation

- **Verify before accepting.** The final answer is the worker's claim; check the diff, the test output or the
  artifact.
- **Continue with context:** `cx.py run -n NAME-2 --resume <session> < FOLLOWUP.md` (same launch pattern). This is
  much faster than a fresh worker re-reading everything. It keeps the session's directory, model and effort. Use
  resume for deltas on the same task, and a fresh worker for a new work order (point it at the previous worker's
  output files). If a resume fails at sandbox start, use a fresh worker.
- **Cancel:** TaskStop the background task; it kills the Codex process tree. **Redirect:** TaskStop, then
  `--resume` the session id from the `started` line with the correction.
- **Time box:** state it in the brief; if a worker overruns, TaskStop it and resume or replace it.

## Failures

- Many workers failing at once with `websocket closed` / `401 Unauthorized` / usage-limit errors is a backend
  outage or exhausted quota. Stop launching, don't relaunch in a loop, and `--resume` the affected sessions later
  (or switch model family if the user agrees).
- `flagged for possible cybersecurity risk`: rephrase the task neutrally or use another model. `model is at
  capacity`: use another model or retry later.
- If a result looks like the worker never got its mission, check the prompt file: cx can't tell a malformed prompt
  from a good one.
