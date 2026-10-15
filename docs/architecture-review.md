# Claude Code ↔ Codex worker integration — architecture review

Date: 2026-10-15 · Scope: `codex_agents_monitor` (cx.py, the `codex-workers` skill, install.ps1, dashboard) and how
it is used in practice. simant_recon, simantw_recon, stunts_recon and empires_reconstruction were read only as case
studies. No code was changed.

## 0. Verdict

The core design is right and should stay: **one blocking `codex exec --json` process per worker, run as a Claude Code
background Bash task.** The harness notification is the completion event, TaskStop is cancel, and `exec resume` is
continuation. OpenAI's own TypeScript SDK drives Codex the same way (it spawns `codex exec --experimental-json`).
Two of the four external projects studied (`codex-first`, and the review path of OpenAI's `codex-plugin-cc`) landed
on it independently. Every alternative is worse for this workload: an app-server broker, MCP, tmux, a Claude
forwarding subagent, or Codex's internal multi-agent mode (§3).

So this is **not a rewrite**. The real friction in 1,678 production workers comes from five things:

1. **A component we added that does harm.** The uncommitted `Leftovers` process hunter killed `explorer.exe`, Chrome,
   iCUE, Adobe services, the Claude app's crash handler, MSBuild nodes and other projects' compiler services, and it
   still stops 0 of 4 of the escapes it was written for. → **Delete it** (−263 lines, −314 test lines).
2. **Information cx prints too late or not at all.**
   - The session id only appears at the end, so any killed or stopped worker loses it. A session id was lost in S2
     (10-07 21:22), and supervisors kept hand-made SESSIONS.md / failed_sessions.txt files to compensate.
   - `DONE` hides workers that could not run a single command.
   - `--resume` silently runs in the supervisor's directory.
   → **Three small cx fixes** (~+25 lines).
3. **A self-inflicted kill.** simant S2 lost 6 workers at exactly 2:00:00. The cause was not a harness limit: the
   supervisor passed `timeout: 7200000` on 83 of 86 background launches (blood2 did the same 43 times). Every other
   project omitted it and ran workers for up to 20 h. → **One skill line: never pass `timeout` on a worker launch.**
4. **Supervisor habits the skill doesn't prevent.**
   - 118 grep-and-sleep wait loops blocked one supervisor for 13.2 h.
   - Detached `for … &` launches came back after compaction.
   - Read-only workers were given write tasks.
   - Workers were told to "work in a worktree" while `-C` pointed at the main repo.
   → **Rewrite the skill around delegation, put it in the repo, and install it** (no code).
5. **The Windows sandbox.** In September, supervisors abandoned it under load (52% of workers ran
   `danger-full-access`), and that is what made the process-escape incidents possible. Today, 16 concurrent sandboxed
   workers ran fine (E10). → **Re-test under sustained load before choosing to run unsandboxed** (experiment).

---

## 1. Current architecture — what we actually have

### 1.1 Components

| Component | Size | Role | Consumer |
|---|---|---|---|
| `cx.py run` | `cmd_run` 147 lines + helpers | Finds the binary (hashed path); `exec --json`, skip git check, approvals never, model/effort/sandbox; parses JSONL; prints header + final message; exit 0/1 | Claude |
| `bind_to_our_lifetime` | 30 | Kill-on-close job object around codex.exe | cx |
| `Leftovers`, `WinProcs`, `repo_root`, path helpers (**uncommitted**) | 263 + 314 test lines | Tracks descendants by parent PID; kills survivors at exit | cx |
| `session_settings` | 15 | Model/effort of a session's last turn, read from its rollout | cx `--resume`, header |
| EXIT_GRACE watchdog | ~8 | Kills Codex 20 s after `turn.completed` if it hasn't exited | cx |
| `write_run` → `~/.codex-dashboard/runs/<sid>.json` | ~15 | Name, cwd, times, status | dashboard; supervisors use it as a name→session index |
| `cx ps` | 21 (imports the dashboard's `Monitor`) | One health line per running worker | Claude |
| `codex_dashboard.py` + `static/index.html` | 1,349 + 395 | Human observer: rollouts + Restart Manager liveness, transcripts, tokens/s | human |
| `install.ps1` | 37 | Copies sources to `~/.codex-dashboard`, PATH shims | human |
| `~/.claude/skills/codex-workers/SKILL.md` | 45, **not in the repo** | Launch / notify / read / resume / cancel pattern | Claude |

### 1.2 Execution flow (traced and verified in this session)

```
Claude decides to delegate
 ├─ Write build/workers/NAME/PROMPT.md   (project convention; doubles as an audit trail)
 ├─ Bash(run_in_background, description=NAME):
 │    python ~/.codex-dashboard/cx.py run -n NAME -C DIR -m M -e E < PROMPT.md
 │    claude.exe → bash ×3 → python (cx) → codex.exe exec --json
 │                                         → codex-command-runner (elevated sandbox user) → shell → tools
 │  … Claude keeps working; zero supervisor tokens are spent while the worker runs …
 ├─ <task-notification> status, exit code, output path        ← no output in it (verified)
 ├─ Read output file → "[cx] NAME · DONE · 47m02s · gpt-6-luna/xhigh · session <sid>" + final answer
 ├─ (often) read the RESULT.md/REPORT.md the prompt asked for   ← project convention
 └─ verify → accept, or  cx run --resume <sid> < FOLLOWUP.md,  or TaskStop
```

Best case per worker is **one launch, one notification, one Read**. simantw ran 613 workers at 1.2 lifecycle calls per
worker, with a median of 4 s from notification to action. That's the floor the harness allows.

### 1.3 Usage (all projects, 2026-09-25 → 10-15)

- **1,678 cx workers** in 9 projects: 1,507 done, 31 done-but-hung (cx stopped Codex after the grace period),
  40 failed (2.4%), and **88 records stuck at "running" forever** (killed or stopped workers; cx never wrote an end).
- Duration: median 28.6 min, p90 91.5 min, p99 5.5 h, max 20.3 h. Machine-wide peak concurrency **45**.
- First-turn model/effort: luna/xhigh 1,541; 6.1-sol/high 148; luna/high 95; others small. 28 accidental astra runs
  (the resume-model bug, since fixed).
- Sandbox: **danger-full-access 1,021 of 1,958 rollouts (52%)**, workspace-write 859, read-only 78. October alone:
  8 of 476.
- 324 sessions resumed (309 distinct resume launches); 41% of worker sessions compacted their own context.
- Weekly Codex quota reached 98% (09-26) and 100% (10-08); twice the supervisor moved work to Claude subagents.

---

## 2. Experiments run for this review

codex-cli 0.162.0-alpha.17.2, Windows 11; gpt-6-luna/low, trivial prompts in a scratch directory. ~50 short runs in
total. The probe script and raw outputs are in the session scratchpad (`exp/`).

| # | Experiment | Result |
|---|---|---|
| E1 | 8 concurrent trivial runs (5 user config, 3 `--ignore-user-config`) | thread.started 1.1–1.8 s; turn done 3.3–5.3 s; **exit 0.0–0.7 s after turn end** |
| E2 | 3 runs executing shell commands; 3 concurrent `exec resume` | All exited ≤0.6 s after `turn.completed`. **No exit hang in 14 runs** |
| E3 | Background cx worker running `sleep 300`, then **TaskStop** | cx, codex.exe, codex-command-runner, powershell and python all gone. Output file: `[killed]`, **no session id** |
| E4 | Background cx worker, normal completion | Notification = status + exit code + output path only; one Read returns the header + answer |
| E5 | Detached helper started with `Start-Process` (9 variants; note that `-ArgumentList` needs one quoted string or the helper dies at once, which invalidated the first round) | See the next table |
| E6 | `cx run --resume <sid>` launched from another directory | **Worker ran in the supervisor's cwd**, not its original directory |
| E7 | `codex exec --worktree` (via `cx … -- --worktree`) | Creates `~/.codex/worktrees/<id>/<repo>`, detached HEAD; `session_meta.cwd` records it. Git in the sandbox fails with "dubious ownership" — as it does for a plain sandboxed worker in a fresh repo, so it's `safe.directory` setup for the sandbox user, not a worktree issue |
| E8 | Input tokens of a trivial turn with user config vs `--ignore-user-config` | 16,476 vs 13,745–14,933: plugins/MCP/notify add ~1.5–2.7k tokens per request, mostly cached |
| E9 | Worker asked to report a failed command's output | It replied "STARTED" anyway. **Final answers are claims** |
| E10 | 16 concurrent workspace-write workers, each running a command | **16/16 correct**; first command 8–12 s after launch (single worker ~8 s). No starvation in a burst; a sustained hour-long load was not tested |

E5 — does a detached helper outlive its worker?

| Launcher | Sandbox | No job object | cx, job only (HEAD) | cx with `Leftovers` (installed) |
|---|---|---|---|---|
| Store-alias `python.exe` | read-only, workspace-write | cannot start (alias invisible to the sandbox user) | cannot start | — |
| Normal `python.exe` | workspace-write | **survives** | **killed** | — |
| Store-alias `python.exe` | danger-full-access | survives | survives | **survives 4/4** (2 reported "NOT stopped (unverified)", 2 unnoticed) |

The job object handles every sandboxed case. The only escape needs `danger-full-access` + `Start-Process` of a
packaged app. That combination was common in September (half the workers ran unsandboxed) and caused the 20.5 GB
runaway helper on 09-25. `Leftovers` doesn't stop it, though. Moving workers back into the sandbox does.

---

## 3. Comparison with existing solutions

| Project | Runtime / integration | Genuinely better | Worse |
|---|---|---|---|
| **openai/codex-plugin-cc** | `codex app-server` + per-session broker; full plugin (commands, Sonnet forwarding subagent, hooks, `state.json` job index) | Graceful `turn/interrupt`; thread names; `outputSchema` reviews | Broker serves one stream at a time, so parallel jobs spawn separate app-servers anyway. Completion is inferred (≥5 open "hangs forever" issues). Windows broker/process leaks. `state.json` races lose jobs and delete logs. Stale "running" jobs. Shells owned by subagents get reaped (#432, #198). A run lost its session id at a time cap (#542) |
| **steipete `codex-first`** | Plain `codex exec … -o OUT - < ORDER`; skill only; one `run_in_background` task per worker | Same core as ours, plus field rules: capture the session id immediately; explicit UUID, never `--last`; resume from the recorded cwd; one worktree per writer; "worker reports are advisory, verify the diff"; fresh sessions for new orders, resume only for deltas | bash/macOS tooling; `--yolo` everywhere |
| **agent-deck** | Interactive Codex TUI in tmux; skill + CLI that polls every 5 s | Human-visible fleets; worktree/Docker isolation | WSL only; heuristic idle detection; polling |
| **codex-dispatch** | `codex mcp-server` tool call | — | Blocks Claude's turn; no background, cancel or resume; `codex mcp-server` no longer exists in the CLI |
| *Native Claude subagents* (empires: 108 Sonnet subagents; stunts: 59 Opus integrators) | Agent tool | Answer arrives inside the notification; SendMessage continuation | Supervisors still polled for artifacts (`until [ -f REPORT.md ]`); no Codex models; Claude quota |

**What app-server would buy:** graceful interrupt, mid-turn steering, many threads per process, live token and
rate-limit notifications. **What it would cost:** no exit-code completion signal (completion has to be inferred,
which is the plugin's main bug class), a long-lived process to manage on Windows, and a custom client. In the
transcripts, "redirect" always meant stop + new instructions, which TaskStop + `--resume` already does. Not worth it today.

**Codex's own multi-agent mode** (`spawn_agent`, up to 64 threads in one exec): the router would be a GPT model, not
the supervisor; all children share one sandbox and cwd; and the supervisor can't cancel or steer an individual child.
Unsuitable for supervisor-controlled fleets.

---

## 4. Key weaknesses (demonstrated)

Cause codes: **A** integration, **B** supervisor strategy, **C** project tooling, **D** inherent.
Fix owner: **1** cx, **2** skill wording, **3** project (out of scope).

| # | Weakness | Evidence | Cause → fix |
|---|---|---|---|
| W1 | `Leftovers` kills unrelated processes and doesn't stop real escapes | Killed (per cx's own notes): `explorer.exe` + Chrome, 61 processes in one run (simantw 09-27 09:35); iCUE; Claude crashpad handler; Adobe CCXProcess/IPCBroker (simant 10-06, after two hand patches to cx.py by supervisors); MoNotificationUx; MSBuild nodes; other projects' `compiler_service.py`/DOSBox/WinUAE; other sessions' `tail -f`. E5: 0/4 escapes stopped | A → 1 (delete) |
| W2 | Session id printed only at the end | TaskStop / 2-h kill / crash → `[killed]` or `[exited with code 1]`, no id (E3). S2 lost one ("wasn't recorded, so I'm starting a fresh one"). S1 kept SESSIONS.md by hand; stunts kept failed_sessions.txt. Compaction summaries kept task ids but **0 Codex session ids**. 88 stale "running" records | A → 1 |
| W3 | `--resume` runs in the supervisor's cwd | E6. Codex's thread index then shows the session's cwd **and sandbox writable root** moved to the supervisor's directory. The skill claimed the opposite | A → 1 |
| W4 | `DONE` / exit 0 for workers that did nothing | S1: 9 workers whose commands never ran (sandbox stall) reported DONE. S2 09:49: resume failed with `helper_unknown_error`, reported DONE | A → 1 |
| W5 | Workers killed by the supervisor's own `timeout` | S2: 6 workers killed at exactly 2:00:00, status `killed`, no header; partial work salvaged by hand from worktrees. The launches carried `timeout: 7200000` (simant 83, blood2 43). Docs: in the desktop app/terminal a background command has no time limit unless Claude passes `timeout`; 2 h is that parameter's maximum | B → 2 |
| W6 | Poll loops and detached launches despite the skill | S2: 118 `grep [cx] … ; sleep 10` loops, 13.2 h blocked, 39 min after loading the skill. Stunts: after compaction, `for n …; do (cx … &); done` + 34 `until grep` watchers; S0/S4 then needed `taskkill`, and an orphan watcher kept polling. The stunts skill was only `cat`'d, never invoked through the Skill tool, so it was not re-attached after compaction | B → 2 |
| W7 | Isolation advice missing | S2 phase 2: workers told to "work in build/workers/NAME/wt" while `-C` was the main repo, so the sandbox allowed main-checkout writes; p2-vga amended a main-repo commit, then cherry-pick and patch conflicts. S2 11:28: a read-only worker given a writing task | B → 2 |
| W8 | Sandbox stalls under load | S1 09-29: `echo hi` pending >2 min, 9 workers stopped twice, Codex abandoned for Claude subagents. simantw 09-25: per-command `setup refresh`, read-only probe 5m01s vs full-access 11 s, so the fleet went unsandboxed. The lesson stayed in one project's memory. E10 today: not reproduced in a burst of 16 | Codex platform → 2 + experiment |
| W9 | Skill unversioned and wrapper-centric | Lives only in `~/.claude/skills`; install.ps1 doesn't install it; supervisors patched the installed cx.py in place (twice) | A → 1 + 2 |
| W10 | Exit hang (Codex bug) | 31–33 real runs needed the watchdog (09-25…09-30, once 10-07); 0 in 14 runs today | Codex → 1, already handled |

**Not a weakness: `cx ps`.** 105 calls in the three simant/simantw sessions. Besides health checks, it was the
source of the session id for 39 of 309 resumes. Once the start line exists (S1 below), it becomes a pure health tool.

---

## 5. Case-study findings

### 5.1 How the supervisors actually work

- **Decomposition.** Per-function or per-family research workers (Luna/xhigh). Selective Sol/high for shared blockers.
  Single-writer integration passes (stunts `integ1…56`). Review workers. Batch sizes: S2 mostly 1 per message (max 6),
  simantw up to 30, stunts up to 15.
- **Context transfer.** A shared preamble plus a per-task file, concatenated into `build/workers/NAME/PROMPT.md`
  (median 2.4–3.4 KB). Final answers capped at ~15–20 lines; details go to project-owned RESULT/REPORT.md. 82 prompt
  files in S2, 621 in simantw; only 4 and 12 were reused.
- **Acceptance.** Single writer in every project: stunts `promote.py` (OS lock + journal + rebuild); simant
  `promote.py` + CanonicalLock; empires `check_candidate.py --promote`. Workers write only to their own `build/workers/NAME/`.
- **Completion handling.** When the supervisor waits for notifications it is efficient: notification → read in a
  median of 4 s (simantw) or 14 s (S2). Integration text made up 7–25% of all tool-result text, mostly the
  workers' answers themselves.

### 5.2 Friction, classified

| Friction | Owner | Where it's handled |
|---|---|---|
| Leftover killer hits user apps / other projects | 1 | R1 |
| Session id lost on stop/kill/compaction; hand-kept id files | 1 | S2 |
| DONE for workers that ran nothing; silent `exit 1` with no header | 1 | S3, S2 |
| Resume in the wrong directory | 1 | S1 |
| 2-hour kill from the supervisor's own `timeout: 7200000` | 2 | skill §M |
| Poll loops; detached `&` launches after compaction | 2 | skill §M, S4 |
| Read-only worker given a write task; worktree without `-C` | 2 | skill §I |
| Sandbox stall diagnosis lived in one project's memory | 2 + experiment | skill, X2 |
| Resume unreliable (sandbox helper error, lost shell — 6 in simantw) | 2 | skill: fallback to a fresh worker with hand-over |
| Cyber-filter / capacity failures | 2 | skill: one line |
| Outage + 401 storm → relaunch loop, ~25 min auth debugging | 2 (already in the skill) | keep |
| Quota running out → switch model family | 2 | skill: one line (quota visible in the dashboard) |
| Long workers exceed the time box written in the prompt | 2 (+ optional 1) | skill; `--timeout` only if needed (X5) |
| Malformed prompt from a `cat` pipeline / not-yet-written PROMPT.md | 2 | skill: write the file, then redirect; a missing file already fails the launch |
| promote.py lock contention, verify aborts, `promote_retry.sh` | 3 | out of scope |
| search.py compile-environment scratch explosion (tens of GB) | 3 | out of scope |
| 12 GB of worker worktrees under build/workers; binary patch-apply failures | 3 | out of scope |
| Capstone missing in the sandbox Python; shared live tools | 3 | out of scope |
| Heredoc/MSYS path escaping in project scripts | 3 / Bash tool | out of scope |
| Near-miss exhaustion; shared blockers found by many workers; one serialized publisher | D | — |

---

## 6. Recommendations

For each: problem · why it's ours · native alternative · code delta · new state · simpler for Claude? · opt-out ·
if not done · verification.

### 6.1 Remove or simplify

**R1. Delete `Leftovers`, `WinProcs`, `repo_root`, `windows_path`/`PATH_RE` and `tests/test_leftovers.py`. Keep the job object.**
- Problem: W1. Ours because we added it. Native: the job object + Codex's sandbox (E5) + TaskStop's tree kill (E3).
- **−263 lines, −314 test lines**; `cx run` stops importing the dashboard. New state: none.
- Simpler: yes. The "left N processes / NOT stopped" notes disappear from answers and from the skill.
- Residual: unsandboxed workers that `Start-Process` a packaged app can still leave it running, exactly as today.
  Mitigated by the skill (no background processes; prefer the sandbox) and X2.
- If not done: more user applications get killed.
- Verify: re-run E5 (job-only stays "killed" in sandboxed modes); `cx stopped them` count = 0 in new transcripts.

**R2. Slim the run record; keep it as the name→session index.**
- `runs/<sid>.json` is the only place that maps the `-n` name to a session (Codex exec can't name threads); supervisors
  used it to recover ids. Its status fields go stale, though (88 records) and duplicate the rollout.
- Change: write `{name, cwd, started}` once at `thread.started`; drop the end-of-run update (−~6 lines). The dashboard
  already derives status from the rollout.
- Verify: dashboard names unchanged; no record says "running" for a dead worker.

**R3. Delete the process-cleanup paragraphs from the skill and the README Limitations** (follows R1).

### 6.2 Native — rely on it, don't rebuild

| Need | Mechanism | Status |
|---|---|---|
| Wait for any of N | One background Bash task per worker → notification (median 4–14 s to action) | verified |
| Cancel | TaskStop kills the whole tree on Windows | E3 |
| Track running workers | Claude's own background-task list | — |
| Continue with context | `codex exec resume` | E2 |
| Branch from a worker's state | `codex exec fork <sid>` (pass-through) | help only |
| Write isolation | A separate worktree passed as `-C`, or `codex exec --worktree` | E7 |
| Structured result, when a project wants one | `--output-schema FILE` (pass-through) | help only |
| Human visibility | Rollouts → dashboard | existing |

**Do not build:** a job list or wait-any command, an MCP server, a job DB, a broker or daemon, a Claude forwarding
subagent, Codex's internal fan-out, an app-server client, or an integration-owned temp-storage manager. Projects already
own worker workspaces (`build/workers/NAME`); `--worktree` / `-C <worktree>` covers write isolation; and
integration-owned temp files would need a retention policy for artifacts whose value only the project can judge.

### 6.3 Small generic improvements

**S1. Resume in the session's own directory** (W3)
- Read model, effort and cwd from Codex's own thread index (`~/.codex/state_N.sqlite`, table `threads`), with the
  plain rollout as fallback. The index also covers old sessions whose rollouts were compressed to `.jsonl.zst`,
  which the rollout-only lookup silently missed (it fell back to config defaults). Use the cwd as `Popen(cwd=…)` when
  `--resume` is given without `-C`.
- +~6 lines, no new state. Verify: E6 → the worker reports its original directory.

**S2. Start line, flushed** (W2)
- On `thread.started`: `[cx] NAME · started · <model>/<effort> · <cwd if not the requested one> · session <sid>`.
- +~5 lines. Claude impact: one more line in the one Read it already does.
- What it fixes: the id survives TaskStop, the 2-h kill, crashes and compaction (the output file stays readable).
  Redirect becomes "TaskStop → `--resume <sid>` with the correction" without bookkeeping, which removes the hand-kept
  SESSIONS.md / failed_sessions.txt and most `cx ps` id lookups.
- Verify: E3 → the output file has the id before `[killed]`.

**S3. Say when a worker did nothing useful** (W4)
- cx already sees every `command_execution` item (status, exit code). Add to the header only when abnormal:
  `· 0 of 7 commands succeeded` or `· no commands ran`. Also make the process-exit-without-turn case explicit:
  `FAILED · codex exited 1 before finishing the turn` plus the stderr tail (today it can print nothing).
- +~10 lines, no state, no change for healthy workers.
- Verify: replay S1 (worker whose shell stalls) → header flags it; a `--resume` that hits a sandbox helper error is no
  longer reported as a plain DONE.

**S4. Version the skill and install it**
- Move it to `skills/codex-workers/SKILL.md`; install.ps1 copies it. +1 line. Opt-out: delete the installed copy.

**S5. Rewrite the skill around delegation** (generic supervisor instructions, ≤ ~90 lines; addresses W5–W8)
- **When:** independent, bounded units. Keep design decisions, acceptance and canonical writes yourself (or in one
  single-writer worker).
- **Brief:** goal; files and area; constraints; done-criteria; a STOP rule ("if blocked, stop and report exact
  evidence"); a project-owned path for bulky output; final-answer contract: first line
  `STATUS: done|partial|blocked|failed`, then ≤15 lines of what changed, evidence (command → result) and open questions.
- **Model/effort:** always pass `-m/-e` (the config default is the expensive model). Cheaper model at high effort for
  bounded work; strongest model for shared blockers or ambiguity. The header shows what ran. Resume keeps both. On
  quota exhaustion or a capacity error, switch model or wait.
- **Sandbox and isolation:**
  - workspace-write is the default; read-only means *no file writes at all*.
  - Parallel writers get disjoint files, or their own worktree **passed as `-C`** (telling a worker to "use a worktree"
    while `-C` is the main repo leaves the main repo writable), or `-- --worktree`.
  - Canonical files have one writer.
  - Workers must not leave background processes running.
  - If commands stall in the sandbox (`echo` taking minutes), stop, probe once, and ask the user before going
    unsandboxed.
- **Mechanics (M):**
  - N workers = N separate background Bash calls in one message. Never a loop, `&` or `nohup`, and never a
    `timeout` parameter.
  - Launch from the main session, not from a subagent (its shells are reaped when it exits).
  - **After launching, do other work or end your turn; never poll, grep or sleep on output files.** Notifications
    arrive by themselves.
  - One Read per notification.
  - Invoke this skill with the Skill tool (it is then re-attached after compaction); re-invoke it after compaction if
    launches are coming.
- **Time:** never pass `timeout` on a worker launch; it kills the worker at that point, 2 h at most. In a desktop or
  terminal session, background commands have no limit otherwise. To time-box a worker, put the box in the brief and
  TaskStop it if it overruns; a stopped worker can be resumed from the id on its start line.
- **Resume vs fresh:** resume for deltas on the same task. A fresh worker for a new work order (pointing at the previous
  worker's RESULT file). If a resume fails at sandbox start, go fresh.
- **Verify:** the final answer is a claim (E9). Check the diff, tests or artifact before accepting.
- **Failures:** a burst of 401/websocket/usage-limit failures means an outage: stop launching, resume later.
  Cyber-filter: rephrase the task or change model.

### 6.4 Configuration

None needed. Background commands have no time limit in a desktop or terminal session unless the launch passes
`timeout` (docs: tools-reference, "Time limit for background commands"). Unattended runs (`-p`, Agent SDK, CI) do have
one (30 min default, 2 h max, raised via `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS`), so the skill should
mention that in one line.

### 6.5 Changes that need evidence first

| Idea | Gain | Needs proof of | Experiment |
|---|---|---|---|
| X1. `--worktree` as the default for writing workers | No main-checkout writes; no clobbering | Git works in `~/.codex/worktrees` for the sandbox user (`safe.directory`); cleanup of that directory; merge cost for the supervisor | 3 writers on one repo; record merge steps and leftover worktrees |
| X2. Sandboxed fleets at scale | Removes the need for danger-full-access, and with it the escape risk | That W8 is gone in current Codex under *sustained* load | 15–20 sandboxed workers × 30 min of real commands; per-command latency from rollouts (`item_completed` timings) |
| X3. Worker config profile (`-c notify=[] -c mcp_servers.node_repl.enabled=false`, or `-p worker`) | ~10–15% fewer input tokens per request; no Desktop `notify` per turn | That workers don't need those tools; any effect on hangs or stalls | A/B 20 real tasks |
| X4. app-server runtime | Graceful interrupt, mid-turn steering | That mid-flight redirect is needed often enough to beat TaskStop + resume (0 such cases in the transcripts) | Don't start until the evidence exists |
| X5. `cx run --timeout DUR` | Prompt time boxes become real; the id and a partial answer survive | That time boxes in the brief + TaskStop aren't enough (stunts: two user complaints about 3 h+ workers) | Add only if overruns recur |
| X6. Plugin packaging | Versioned updates, namespaced skill | That a `bin/` shim keeps the TaskStop tree kill on Windows (a shell shim broke it before) | Install as a plugin, TaskStop a worker, check the tree |
| X7. Push the answer into the supervisor (cross-session messaging) | Saves 1 Read per worker | That the Read is a real cost (median answer 0.5–1.1 KB, action 4–14 s) | Not worth it |

### 6.6 Stays project-specific

Candidate and evidence management, compiler experiments, match/verification logic, promotion and canonical-source
ownership, locks and journals, progress databases, task prioritisation, prompt preambles and fragment layout,
RESULT/REPORT conventions, `build/workers/*` retention, and scratch explosion. The integration neither reads nor cleans
these files.

---

## 7. What to preserve

- One blocking process per worker as a Claude background task. It's the right primitive, and it costs no supervisor
  tokens while workers run.
- One header line + the final message. No transcript ever entered supervisor context (largest worker answer read:
  13.5 KB).
- Binary discovery, `--skip-git-repo-check`, approvals never, prompt on stdin.
- Resume keeps model/effort; the header shows what actually ran (fixed the astra incident).
- The kill-on-close job object (30 lines; demonstrably kills sandboxed descendants, E5).
- The EXIT_GRACE watchdog (31 real hangs; cheap insurance).
- Error compaction (reconnect retries counted, not listed).
- The dashboard as a pure observer that supervisors never need.
- Invoking as `python …/cx.py`, never through a shell shim.

## 8. Implementation plan

**Status (2026-10-15): steps 1–4 implemented, installed and verified (§8.1). Not committed.**

| Step | Files | Change | Lines |
|---|---|---|---|
| 1 | `cx.py`, `tests/` | Drop the uncommitted `Leftovers` work; keep the EXIT_GRACE=20 and error-compaction hunks | −263, −314 tests |
| 2 | `cx.py` | S1 resume cwd, S2 start line, S3 "did nothing" signal, R2 run record written once | +~20 |
| 3 | `skills/codex-workers/SKILL.md`, `install.ps1` | S4 + S5 | skill moves into the repo |
| 4 | `README.md` | Remove the leftover-cleanup limitation; document resume cwd and the start line | — |
| 5 | — | Validation (§9), then decide X1/X2 | — |

### 8.1 Verification of the implementation (installed copy, real Codex)

| Check | Result |
|---|---|
| Normal background worker | `started` line, then header + answer; notification unchanged |
| TaskStop mid-command | Output keeps `[cx] … started · … · session <id>` before `[killed]`; no codex/python survivor |
| Redirect: resume that id from another directory | Worker ran in its original directory |
| Worker whose commands all fail | `DONE · … · 0 of 2 commands succeeded` |
| `-- --worktree` | Header ends with `· ran in C:\Users\…\.codex\worktrees\<id>\wtrepo` |
| Settings of a compressed (`.jsonl.zst`) September session | `gpt-6-luna`, `xhigh`, `D:\Prog\stunts_recon`, from the thread index |
| Detached normal helper, workspace-write, job object only | Killed when the worker ended |
| `cx ps` | Works |
| Skill | Installed by install.ps1; the harness lists the new description |

Migration: supervisors pick up the installed cx on their next launch; running workers keep the old behaviour.
Long-running supervisor sessions should re-invoke the skill.

## 9. Validation strategy

Use the same instruments as this review: the probe (event timing), a transcript miner over
`~/.claude/projects/*/*.jsonl` (de-duplicated by uuid) and the run records. Baselines are from §5.

| Scenario | Metric | Baseline | Target |
|---|---|---|---|
| 1 short task | Supervisor calls; latency | 2 calls | 2 calls |
| 5 parallel tasks | Lifecycle calls per worker; poll loops | 2.8 (S2) / 1.2 (simantw); 118 loops (S2) | ≤1.3; 0 loops |
| 20 concurrent | Same + orphans + failures | simantw: 1.2/worker | Same per-worker cost; 0 orphan codex.exe |
| Long worker + continuation | Correct cwd/model on resume; id available | Resume in the wrong cwd (E6) | Original cwd; id on the start line |
| Interrupted worker (TaskStop / time limit) | Recoverable without bookkeeping | Id lost (S2 21:22); hand-kept id files | Resume from the output file alone |
| Cancellation mid-command | Survivors | 0 (E3) | 0 |
| Several writers, one repo | Clobbered edits; main-checkout writes | p2-vga amended main; cherry-pick conflict | 0 with `-C <worktree>` |
| Do-nothing worker | Detected in the header | Reported DONE (S1, S2) | Flagged |
| Unrelated processes killed | Count | ≥8 incidents | 0 |

Measure on the next real campaign rather than staging a fleet for the benchmark. One focused check (X2) is worth
running deliberately, because its answer decides whether workers can go back into the sandbox.
