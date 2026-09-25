# codex-dashboard

Read-only local dashboard for `codex exec` agents. It observes Codex from the outside; nothing
that launches Codex needs to know it exists.

    codex-dashboard                 # starts server + opens http://localhost:8765/
    codex-dashboard --no-browser --port 9000 --history-hours 48

Close it at any time; agents are unaffected, and on the next start it rediscovers them.

## How it works

* **Sessions**: every `codex exec` run writes a live JSONL rollout to
  `%CODEX_HOME%\sessions\YYYY\MM\DD\rollout-*.jsonl` (`session_meta` gives id, cwd, git branch;
  `turn_context` model/effort; `event_msg/item_completed` the transcript; `task_complete` the
  outcome). Only sessions with `originator == "codex_exec"` are shown (auto-review threads are skipped).
* **Liveness / PID**: the Windows Restart Manager (`RmGetList`) reports which process holds a
  rollout open, including the process start time, so PID reuse cannot mislead it. The dashboard keeps
  a handle to each `codex exec` process it sees, so it can read the real exit code afterwards.
* **Status**: RUNNING = a live codex exec process holds the file; DONE = last turn has
  `task_complete`; FAILED = `task_complete` carries an error; EXITED = process gone without a
  completed turn (killed/crashed).
* **Names**: `-o/--output-last-message` path (`build/workers/sprite/FINAL.md` → `sprite`), a
  `Your name: X` line in the prompt, else the first prompt line that differs from other sessions in
  the same directory. Nothing is summarised by a model.
* **Files it writes**: only `~/.codex-dashboard/state/sessions.json` (command line, PID, exit code of
  sessions it saw live; Codex does not persist these). It reads `~/.codex-dashboard/runs/` written by `cx`.

Note: NTFS does not update a file's modified time while Codex holds it open, so the dashboard reads
file contents from the last offset rather than trusting timestamps.

## cx — Codex workers for a supervising agent

    python ~/.codex-dashboard/cx.py run [-n NAME] [-C DIR] [-m MODEL] [-e EFFORT] [-s SANDBOX] [--resume SESSION] < prompt
    python ~/.codex-dashboard/cx.py ps

`cx run` wraps one `codex exec --json` run: it finds the Codex binary (the app's path embeds a version
hash), skips the git-repo check, disables approvals, blocks until the worker ends and prints only
`[cx] NAME · DONE|FAILED · runtime · session ID` plus the final answer or the real error (exit 0/1).
Run it as a Claude Code background task: the completion notification is the "worker finished" event,
TaskStop cancels it (invoke via `python …`, not a shell wrapper, or cancel does not reach Codex).
`--resume` continues a finished worker with its context (`codex exec resume`). It records
`~/.codex-dashboard/runs/<session>.json` (name, outcome) so the dashboard shows managed workers by
name. `cx ps` prints one health line per running worker. Agent-facing instructions live in the
`codex-workers` skill (`~/.claude/skills/codex-workers/SKILL.md`).

## Limitations

* `codex exec --ephemeral` writes no rollout: shown as a process-only row, without a transcript.
* Reasoning is stored encrypted unless `model_reasoning_summary` is enabled, so "thinking" rows only
  show count and duration.
* Exit codes are known only for processes that ended while the dashboard was running; otherwise
  status is inferred from the rollout.
* Uses the default `%CODEX_HOME%` (or `--codex-home`). Liveness detection is Windows-only.
