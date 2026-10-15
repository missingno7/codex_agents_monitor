# Scaling Claude ↔ Codex orchestration for large fleets — research assessment

> **Research record (2026-10-15), not a specification.** Current behaviour is defined by `README.md`, the skill
> and the tests. Outcome: no change to cx or the dashboard. A few conventions were adopted into the
> `codex-workers` skill (§8); the optional patterns in §8.1 and the experiment in §9 were not adopted. Raw
> measurements and scripts stayed in the session scratchpad (not retained); external sources are listed at the end.

## 0. Answer in one paragraph

Don't add a hierarchy, and don't add infrastructure. The supervisor is not a latency bottleneck: notifications
never queue and Opus is idle 10–83% of the time while workers run. What costs is the **number of Opus turns**:
every turn re-reads a context that averages ~480k tokens. A routine completion takes ~3 turns (read result → gate →
launch next), and 72–83% of post-completion decisions are routine. So the lever is **fewer Opus wakes per unit of
mechanical work**, plus a handful of worker-side habits that waste Codex tokens. Both can be served by conventions
in the existing skill and by mechanisms that already exist:

- **One background task per *wave*** instead of per worker, where the project can accept results in batches.
- **A read-only synthesis worker** over a batch of reports, when shared blockers need spotting.
- **Codex's native sub-agents** inside a single cx worker, which already gives a one-process "lead + N workers"
  hierarchy with no new code.
- **AGENTS.md** as the shared context (Codex already auto-loads it).

A true Sol → Luna process hierarchy works only unsandboxed and needs the middle layer to poll, and the one real
deployment (blood2) didn't pay off. The smallest useful next step is a skill update plus one comparison on work that
needs doing anyway (§9).

---

## 1. Where the flat architecture works well (measured)

| Property | Evidence |
|---|---|
| Notification latency | Completion enqueue → delivery median 0 s (max 10 s); notification → first action median 4–14 s in every project |
| No backlog | Completed-but-untouched workers per supervisor turn ≤0.4 on average (1.07 in duke1 during batch intake) |
| Scale | Peaks of 31 (simantw), 37 (duke1), 45 machine-wide; 18 sandboxed workers without stalls (E11) |
| Integration overhead | One output read per completion; no transcript ever entered supervisor context |
| Cancel / resume / containment | Verified (TaskStop tree kill, job object, resume in the right cwd) |
| Duplicate assignment | ~0 where a lease/task tool exists (duke1: 241 targets, 0 double-assigned) |

## 2. The real bottlenecks

### 2.1 Supervisor cost scales with Opus turns × context size
Per main supervisor session (Opus 5.5). "ieq" = input + 1.25·cache-write + 0.1·cache-read + 5·output: a relative
price weight, not a bill.

| Project | Completions | API calls / completion | ieq / completion | Avg context per call | ROUTINE share (sampled) |
|---|---|---|---|---|---|
| simantw | 613 | 5.9 | 0.34M | ~480k | 5/5 |
| duke1 | 323 | 5.9 | 0.33M | ~480k | 5/5 |
| sam | 140 | 6.6 | 0.38M | ~480k | 5/5 |
| stunts | 315 | 6.2 | 0.37M | ~480k | 3/5 |
| kegg | 134 | 8.2 | 0.46M | ~480k | 2/5 |
| simant (port) | 81 | 19.1 | 1.04M | ~480k | 3/5 |
| tales | 65 | 38.0 | 2.24M | ~480k | 3/5 |
| blood2 | 40 | 79.1 | 4.87M | ~480k | 3/5 |

- Cache reads are ~85% of the supervisor's cost. An orchestration-only turn ("cat output, launch") costs about as
  much as a technical turn (~50k ieq each).
- duke1's batch intake tooling handled completions in groups, but Opus was still woken per notification, so its
  cost per completion matched simantw's.
- Decision difficulty (40 sampled windows): **72% routine, 20% synthesis, 8% hard** (≈83% routine by volume). Hard
  work comes in bursts the supervisor does itself (e.g. duke1 deriving three Turbo C rules, then resuming 15 blocked
  workers).

### 2.2 Worker-side waste (Codex tokens)
- **Input is ~97.5% cached re-send:** a session's cost ≈ context size × number of requests.
- **Unbounded workers.** Sessions with 11+ compactions (simantw's "no attempt limit" wave) used **27% of all Codex
  input** for ~0.07× the per-token promotion yield of short sessions. Per-segment productivity didn't collapse; the
  targets were the hardest leftovers, so this is confounded. But it is the largest single sink.
- **AGENTS.md read twice.** Codex injects it automatically, yet prompts say "Read AGENTS.md": 509/590 simantw,
  307/332 stunts and 98/134 kegg sessions re-read it.
- **Carried orientation.** ~25% of input is the fixed preamble (14%) plus ~30k tokens of shared docs carried until
  the first compaction (10%). Direct orientation itself is cheap (1.6% of input), except in short single-function
  tasks (duke1 29%, sam 18%).
- **Questions nobody can answer.** Workers called `request_user_input` 25 times in exec mode; nobody answers.

### 2.3 Shared blockers hit many workers before anyone notices
| Blocker | Workers | Time to central fix | Kind |
|---|---|---|---|
| Sandbox shell hangs | 40 sessions, 4 projects | minutes (kegg) to hours | environment |
| `rg` missing | 44 (simantw, one day) | ~1 day | environment |
| Compiler service down | 15+ | ~2 days | project tooling |
| capstone missing in sandbox Python | 8 | hours | environment |
| Store-Python alias hang | 8 grinders | 22 min | environment |
| Acceptance lock contention | 8 | ~23 h | project tooling |
| Selector-pool placement | 16 workers' outputs | ~32 h to a central diagnosis | inherent |

Environment blockers were noticed within minutes of the first affected completion. Semantic ones took a day or more
to be seen across workers.

### 2.4 Other limits
Throughput limits were mostly **outside the integration**:
- project gates (tales: 26 h of gate runs);
- serial promotion locks (stunts);
- host and Codex concurrency;
- quotas: the Claude weekly limit was hit, made worse by Opus subagents; Sol hit rate limits.

Compaction was costly for the supervisor: 26% of the 20 calls after each compaction went to re-orientation. A
maintained ledger (stunts NOTES.md) made recovery take ~5 calls; without one it took ~19.

## 3. Lessons from other systems

- **No production source shows a mid-level LLM supervisor reducing top-level load or cost at 20–50 workers.**
- **Waking an LLM per worker event is the dominant overhead everywhere it was measured:**
  - agent-deck: ~1.2M tokens per conductor wake, and 84% of completion wakes did nothing;
  - Orchestrator.inc: shipped auto-waking the orchestrator twice and removed it twice;
  - Gas Town: took its Witness supervisor out of the completion path ("one completion per 30–90 s patrol cycle").
- **The fixes are deterministic:** batch the events, derive status from facts (exit code, gates, git), treat worker
  reports as claims, cap report size, and interrupt the LLM only for `stuck` / `needs-input`.
- **Cost multipliers:**
  - multi-agent ≈ 15× chat (Anthropic, measured);
  - Claude agent teams ≈ 7× (docs);
  - runaway nested Codex delegation reached 5.39B tokens against ~97M for direct work (issue report).
- **Practitioners closest to this setup stay flat.** steipete's rule is "workers never create or manage other
  workers"; Cognition's is "writes stay single-threaded". Cheap-worker / expensive-reviewer setups are the measured
  win (Devin Fusion: 41–60% cheaper).
- **Claude Code's own answer for dozens to hundreds of agents is a *script* that holds intermediate results**
  (dynamic workflows), not a supervising model.
- **Codex native multi-agent:** depth 1, shared cwd/sandbox, `wait_agent` without polling, and known runaway and
  long-wait issues.

## 4. Feasibility of hierarchy with our stack (tested 2026-10-15)

| Variant | Works today? | Evidence | Problems |
|---|---|---|---|
| **D1. Sol worker launches cx workers** (true nested processes) | Only with `danger-full-access` | Sandboxed: the inner Codex session started but never reached the model (sandbox network/home); the outer gave up after 4 min. Unsandboxed: works (12 s) | The middle layer must start background commands and poll them (Codex tokens). Lifecycle is safe: the parent's job object killed the stuck inner worker. No per-child TaskStop or notification for Opus |
| **D2. Sol lead with Codex native sub-agents, inside one cx worker** | **Yes, no code** | 8 Luna children, 8/8 verified-correct outputs, 40 s, one notification for Opus | Lead overhead +60% on trivial tasks; the blood2 coordinators measured +22% on long ones, and their children still re-oriented (6 read-only commands, ~85 s). Children share one sandbox and cwd (no write isolation). Depth 1. Results are a lossy relay (a child's refusal surfaced as one line; blood2 children were 42% negative / 38% report-only while their parents mostly reported success). Concurrency cap comes from `agents.max_concurrent_threads_per_session` |
| **C. Sol plans, Opus launches** | Yes | — | Opus becomes a forwarder: same number of Opus wakes, plus a Sol hop. No gain |
| **B. Opus launches; a Sol/Luna synthesizer reads a batch of reports** | Yes, no code | One read-only cx worker over N RESULT files | Pays only when Opus would otherwise read and cross-compare raw outputs: the ~20% synthesis cases and shared-blocker detection |
| **Wave as one background task** (N cx workers in one command, one notification when all finish) | Yes, no code (`cx … & … wait` inside one `run_in_background` call) | TaskStop kills the whole tree (job objects + Claude Code's tree kill) | Per-worker cancel and notification are lost by design; stragglers delay the wave; the shell plumbing must not detach (the skill currently forbids `&` for good reason — it needs a precise pattern) |
| **Claude subagents as area leads** | Possible, not recommended | stunts: 59 Opus subagents cost 4× the supervisor's own tokens and burned the weekly quota | Subagent background shells are reaped when it ends; Claude tokens per worker |
| **Shared context: identical prompt prefix** | No measurable effect | E: same 10k-token briefing first vs last → identical cached tokens (~12k system prefix only) | Separate sessions don't share user-content cache |
| **Shared context: `codex exec fork` of a primed session** | Works, not worth it | Forks: ~29.5k uncached input vs ~5–6k for a fresh worker reading the docs in one command; a few seconds faster | Snapshot staleness; no token saving |

**Conclusion on feasibility.** Anything that changes *who is woken* is already possible without changing cx: D2,
B and waves. Nested process supervision (D1) needs unsandboxed middle layers and polling, which buys nothing over D2.

## 5. Would Sol supervisors improve our workflows?

**As standing area supervisors (D1/C): no.** That's every external report, plus our own blood2 deployment: +22%
input, children still orienting, results mostly negative, and the project later dropped Codex altogether. They also
add the failure modes others report: duplicate dispatch, a lossy relay, and quality capped by the cheaper decider.

**As short-lived, on-demand helpers: sometimes.**
- **D2 wave lead**, for a batch of similar, *independent, single-directory* mechanical tasks where the routine
  follow-up ("retry once with the error", "skip if gate X fails") can be stated in the brief. Opus gets one
  notification and a capped digest per wave. The Codex cost is a +20–60% lead overhead. This is worth measuring
  (§9). It is not worth adopting blindly.
- **B synthesizer**, periodically or when ≥2 workers report similar blockers: one read-only worker clusters N
  reports into a short blocker census. Cheap and directly aimed at the 1–2-day shared-blocker latency.

Neither needs code. Both are conventions Claude can choose per task.

## 6. Roles and shared context

- **Roles stay conventions in the brief** ("you are a read-only reviewer", "you are the single writer for X",
  "you lead this wave"), chosen by the supervisor or project. Only two have evidence behind them: a **single
  writer/integrator** (every project converged on it) and an **independent reviewer** (codex-first, simant
  reviews). The tool shouldn't name or enforce roles.
- **Shared context already has a native, cache-friendly home: AGENTS.md** (auto-loaded by Codex for the `-C`
  directory, and project-owned) plus the project's docs. The improvement is a habit, not a mechanism:
  - don't tell workers to read AGENTS.md;
  - put fleet-wide worker rules there, or in one doc it links, instead of repeating them in every prompt;
  - keep the docs every worker re-reads short (simantw workers re-read ~100 KB each).
- **Propagating a discovery** to running workers is a `--resume` with the fact (used 54 times for broadcasts), and
  to future workers it's an edit to the project's docs. Both already work. The integration should not hold
  knowledge.

## 7. Costs, benefits and new failure modes

| Option | Opus effect | Codex effect | New failure modes |
|---|---|---|---|
| Skill conventions (§8) | Fewer wasted turns (poll loops, re-orientation, full-file reads) | Fewer unbounded sessions, no double AGENTS.md reads | None |
| Waves | Per-wave instead of per-worker turns (up to N× fewer) | None | Straggler waits; a coarser cancel; shell plumbing errors |
| D2 wave lead | Same as waves, plus routine retries off Opus | +20–60% lead tokens | Lossy relay; shared cwd; depth 1; runaway delegation if the brief is open-ended (cap N explicitly) |
| B synthesizer | Replaces Opus cross-reading; faster blocker detection | One worker per census | Digest is a claim; Opus must still verify flagged items |
| Standing Sol hierarchy | Unclear; external evidence negative | +22% or more | Duplicate dispatch, restarts, stuck middle layer |

## 8. What belongs where

**codex_agents_monitor: no code changes recommended.**
- Considered and rejected:
  - `--fork` pass-through (measured: no token benefit);
  - pushing answers into the supervisor via cross-session messaging (risks duplicate wakes; saves one read);
  - a `cx batch` primitive (try the skill pattern first; build it only if the plumbing proves error-prone in use);
  - per-worker token counts in the header (useful, but it drifts toward resource accounting, which you asked to
    avoid).
- **Adopted into the skill** (generic, all ignorable). Each is a convention, not a mechanism:

  | Convention | Evidence (§2) |
  |---|---|
  | **Autonomous workers:** keep going while there are productive directions; return at a natural decision point (done, genuine blocker, directions exhausted, or a finding that needs coordination) with a compact report: outcome, progress, verification evidence, blockers, next step | 72–83% of post-completion Opus turns were routine; workers stopping early became a standing complaint (stunts) |
  | **Supervisor turns are the cost:** handle workers that finish together in one turn; bounded reads; don't re-read summaries | ~50k ieq per Opus turn at ~480k context; ~3 turns per routine completion |
  | **Recognise stagnation, don't cap attempts:** far-longer-than-peers, repeated compaction or repeated failed experiments → redirect, replace from the report, or stop | 11+-compaction sessions: 27% of Codex input, ~0.07× yield |
  | **AGENTS.md is auto-loaded:** don't ask workers to read it | Re-read in 509/590 simantw and 307/332 stunts sessions |
  | **Non-interactive:** never wait for input or approval; report the decision needed | 25 unanswered `request_user_input` calls |
  | **Canary before a fleet** after tooling or environment changes | 40 sandbox-hang sessions; 44 `rg`-missing sessions; 8 kegg grinders hung together |
  | **Shared blockers:** stop similar launches, fix centrally, resume the blocked; a read-only worker over the reports can spot them | Selector pool ~32 h to diagnosis; acceptance lock ~23 h |

- **Deliberately not adopted:**
  - fixed attempt counts, time limits and mandatory checkpoints (you asked for none, and the evidence points to
    stagnation signals rather than budgets);
  - wave execution as a default (it gives up per-worker notification and cancel).

### 8.1 Optional patterns (not in the skill; use when a task calls for them)

| Pattern | How, with existing tools | Measured cost and limits |
|---|---|---|
| **Synthesis worker** | One `-s read-only` cx worker given the paths of N finished reports: cluster the outcomes and blockers into ≤15 lines | ≈ the size of the reports read. The digest is a claim: verify flagged items. Pays off only when you would otherwise cross-read the reports yourself |
| **Wave lead** (Codex native sub-agents) | One cx worker (Sol or Luna) whose brief says: spawn exactly N sub-agents (model, effort), one task each; give an explicit retry rule; wait; return a ≤15-line digest | +60% (trivial tasks) to +22% (long tasks, blood2) lead tokens; children still re-orient; one shared cwd and sandbox (no write isolation); depth 1; the concurrency cap is the Codex `agents` config; child failures surface only through the lead's summary; no per-child TaskStop or notification. Always cap N; open-ended delegation has run away elsewhere (openai/codex #38989) |
| **Wave as one background task** | One `run_in_background` Bash call that starts N `cx run` processes and `wait`s, then prints their outputs | One notification per wave; TaskStop kills the whole wave; no per-worker cancel or notification; stragglers hold the wave; the plumbing must stay inside the one background call (never detach) |
| **Nested cx (a worker launching workers)** | Only with an unsandboxed parent | Sandboxed parents can't reach the model from the child; the parent must poll; no visibility for the supervisor. Not recommended |
| **Fork a primed session** (`codex exec fork`) | Not exposed by cx | ~5× the uncached input of a fresh worker reading the same docs; saves a few seconds. Not recommended |

**Projects keep:** task decomposition and leases, prompts and preambles, acceptance, promotion and locks,
evidence and blocker files, AGENTS.md content, handoff files, batch intake tooling, and gate performance.

## 9. Recommendation and the smallest useful experiment

1. **Done:** the skill conventions in §8. No cx change.
2. **Experiment (deferred until a suitable real workload exists; not run):** in a project with independent, batch-
   acceptable packets (duke1- or sam-style), take 3 waves of 10–12 comparable packets:
   - **A** — current: one background task per worker;
   - **B** — one background task per wave (N cx processes + `wait`);
   - **C** — one Sol lead cx worker with N Luna sub-agents (explicit N, explicit retry rule, ≤15-line digest).

   Measure from the transcripts and the Codex thread index: Opus API calls and ieq per *verified* result
   (project gate); Codex tokens (lead + children); wall time from first launch to last verified result; failures
   and recoveries; and digest errors (claims the gate contradicts).

   Adopt B or C as a documented option only if it cuts Opus ieq per verified result by more than ~2× without
   lowering the verified yield. Otherwise keep A.
3. **Don't** pursue standing Sol supervisors, nested cx, shared-context machinery or fork-based priming without new
   evidence.

## Sources

- **Internal, 2026-09-25…10-15:**
  - Claude transcripts of simantw, stunts, simant, blood2, tales, duke1, sam and kegg (de-duplicated);
  - 1,711 Codex exec sessions plus 176 sub-agent children (`state_5.sqlite`, rollouts);
  - experiments E1–E11 and the 2026-10-15 feasibility tests (nested cx, native sub-agents, prefix caching, fork,
    wave lead).
- **External:**
  - Anthropic, "How we built our multi-agent research system";
  - Cognition, "Don't Build Multi-Agents", "Multi-Agents: What's Actually Working", "Devin Fusion";
  - Claude Code docs: agent teams, workflows, sub-agents, costs;
  - Codex subagent docs and `codex-rs` multi_agents source;
  - gastownhall/gastown (design docs; issues #1379, #1545, #4044, #4787);
  - OrchestratorInc/agent-orchestrator (#3038, #4951, #6119);
  - asheshgoplani/agent-deck (#1225, #2482);
  - steipete/agent-scripts (codex-first, maintainer-orchestrator);
  - openai/codex issues #24951, #33196, #38989, #39854.
