#!/usr/bin/env python3
"""cx: run one Codex worker the way a supervising agent needs it.

    cx run [-n NAME] [-C DIR] [-m MODEL] [-e EFFORT] [-s SANDBOX] [--resume SESSION] [PROMPT | < prompt]
    cx ps

`cx run` blocks until the worker finishes.  It prints a `started · session ID` line as soon as Codex has
a session, then a one-line header plus the worker's final answer (or the actual error).  Launch it as a
background task and the completion notification *is* the "worker finished" event; the transcript stays
in Codex's own session file (and the dashboard).  `--resume` keeps the session's directory, model and
effort.  Exit code: 0 done, 1 failed.  `cx ps` prints one health line per running `codex exec` worker.
"""
import argparse
import collections
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(os.path.expanduser("~"), ".codex-dashboard", "runs")
EXIT_GRACE = 20  # seconds codex may linger after reporting its turn finished before cx stops it
                 # (a normal exit takes <1 s; under load Codex failed to exit in ~2 of 3 runs, 2026-09-25)


def find_codex():
    """The Codex CLI binary.  Not reliably on PATH on this machine, and its path embeds a version
    hash that changes with Codex app updates, so resolve it at every call."""
    if os.environ.get("CODEX_BIN"):
        return os.environ["CODEX_BIN"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        app_bins = glob.glob(os.path.join(os.environ["LOCALAPPDATA"], "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if app_bins:
            return max(app_bins, key=os.path.getmtime)
    found = shutil.which("codex")
    if found:
        return found
    sys.exit("cx: cannot find the codex CLI (set CODEX_BIN)")


def bind_to_our_lifetime(proc):
    """Windows: put Codex in a kill-on-close job.  Everything it starts inherits the job, so when cx ends for
    any reason (normal exit, TaskStop, crash) all of it ends too.  Returns None, or why that failed.

    Codex itself is assigned, not cx: when cx runs on the Microsoft Store Python (a packaged app), children of
    a job-assigned cx are created outside that job (verified 2026-10-15), while an explicitly assigned Codex
    and its descendants stay in it.  Codex is assigned microseconds after it starts, long before it can
    start anything of its own."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes as wt

    class LIMIT(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on x64)
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD),
                    ("IoCounters", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes, k32.CreateJobObjectW.restype = [ctypes.c_void_p, wt.LPCWSTR], wt.HANDLE
    k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return f"CreateJobObject failed (error {ctypes.get_last_error()})"
    info = LIMIT(LimitFlags=0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return f"SetInformationJobObject failed (error {ctypes.get_last_error()})"
    if not k32.AssignProcessToJobObject(job, int(proc._handle)):  # Popen's own handle has the needed rights
        return f"AssignProcessToJobObject failed (error {ctypes.get_last_error()})"
    bind_to_our_lifetime.job = job  # never closed: the OS closes it when cx exits, which kills the job
    return None


def session_settings(sid):
    """(model, effort, cwd) of a session's latest turn; None for anything unknown.

    Codex's own thread index (state_N.sqlite) covers every session, including old ones whose rollout has been
    compressed to .jsonl.zst; the plain rollout is the fallback if the index's name or schema changes."""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    model = effort = cwd = None
    dbs = sorted(glob.glob(os.path.join(home, "state_*.sqlite")),
                 key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    try:
        con = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT model, reasoning_effort, cwd FROM threads WHERE id = ?", (sid,)).fetchone()
        finally:
            con.close()
        if row:
            model, effort, cwd = row
    except (IndexError, sqlite3.Error):
        pass
    if not (model and cwd):
        paths = glob.glob(os.path.join(home, "sessions", "*", "*", "*", f"rollout-*-{sid}.jsonl"))
        try:
            with open(paths[0], "rb") as f:
                for line in f:
                    if b'"turn_context"' in line[:120]:
                        p = json.loads(line).get("payload") or {}
                        model = p.get("model") or model
                        effort = p.get("effort") or (p.get("collaboration_mode") or {}).get("settings", {}).get("reasoning_effort") or effort
                        cwd = p.get("cwd") or cwd
        except (IndexError, OSError, ValueError):
            pass
    if cwd and cwd.startswith("\\\\?\\"):
        cwd = cwd[4:]
    return model, effort, cwd


def dur(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def write_run(sid, data):
    """Label a session with its latest cx invocation ({name, cwd, started}): the dashboard's display name, and a
    name -> session lookup.  A resume replaces the record (resumes are usually named NAME-2, NAME-3); outcomes
    live in Codex's own rollout, so nothing here can go stale.  Best effort, never fatal."""
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        path = os.path.join(RUNS_DIR, sid + ".json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def error_text(err):
    msg = err.get("message") if isinstance(err, dict) else str(err)
    try:  # API errors arrive as a JSON document inside the message
        msg = json.loads(msg)["error"]["message"]
    except Exception:
        pass
    return msg


def cmd_run(a):
    prompt = " ".join(a.prompt) if a.prompt else sys.stdin.buffer.read().decode("utf-8", "replace")
    if not prompt.strip():
        sys.exit("cx: empty prompt (pass it as arguments or on stdin)")
    name = a.name or " ".join(prompt.split()[:6])[:48]
    cwd = os.path.abspath(a.cd or os.getcwd())
    codex = find_codex()
    if a.resume:
        # `codex exec resume` falls back to config.toml defaults, not the session's own model/effort, and it
        # runs (and moves the sandbox's writable root) wherever it is started.  Keep all three unless overridden.
        model, effort, session_cwd = session_settings(a.resume)
        a.model, a.effort = a.model or model, a.effort or effort
        if not a.cd and session_cwd:
            cwd = session_cwd
    common = ["--json", "--skip-git-repo-check", "-c", 'approval_policy="never"']
    if a.model:
        common += ["-m", a.model]
    if a.effort:
        common += ["-c", f'model_reasoning_effort="{a.effort}"']
    if a.resume:  # `exec resume` has no -s/-C; the session keeps its own unless overridden via config
        if a.sandbox:
            common += ["-c", f'sandbox_mode="{a.sandbox}"']
        argv = [codex, "exec", "resume", *common, *a.extra, a.resume, "-"]
    else:
        argv = [codex, "exec", *common, "-s", a.sandbox or "workspace-write", "-C", cwd, *a.extra, "-"]

    t0 = time.time()
    if not os.path.isdir(cwd):
        print(f"[cx] {name} · FAILED · not started\nerror: working directory does not exist: {cwd}")
        return 1
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as e:
        print(f"[cx] {name} · FAILED · not started\nerror: cannot run {codex}: {e.strerror or e}")
        return 1
    try:
        uncontained = bind_to_our_lifetime(proc)
    except Exception as e:  # e.g. ctypes unavailable
        uncontained = repr(e)
    if uncontained:
        print(f"note: {uncontained}; processes this worker starts may outlive it or a TaskStop", flush=True)
    stderr_tail = collections.deque(maxlen=15)

    def drain_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.decode("utf-8", "replace").rstrip())

    def feed_stdin():  # in its own thread so a large prompt cannot deadlock against our reads
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:  # codex exited early; its error output tells the story
            pass

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()
    threading.Thread(target=feed_stdin, daemon=True).start()

    st = {"sid": a.resume, "final": None, "errors": [], "failed": False, "completed": False, "turn_done": None,
          "cmds": 0, "cmds_ok": 0}

    def read_events():
        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):  # anything unexpected must not kill this reader and lose later events
                continue
            t = ev.get("type")
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if t == "thread.started":
                st["sid"] = ev.get("thread_id")
                write_run(st["sid"], {"name": name, "cwd": cwd, "started": t0})
                # First line of output, so the session survives TaskStop, a crash or a lost notification.
                me = f" · {a.model or 'default'}/{a.effort or 'default'}" if (a.model or a.effort) else ""
                print(f"[cx] {name} · started{me} · session {st['sid']}", flush=True)
            elif t == "item.started" and item.get("type") == "command_execution":
                st["cmds"] += 1
            elif t == "item.completed":
                if item.get("type") == "agent_message":
                    st["final"] = item.get("text")
                elif item.get("type") == "command_execution" and item.get("exit_code") == 0:
                    st["cmds_ok"] += 1
            elif t == "turn.failed":
                st["failed"] = True
                st["turn_done"] = time.time()
                st["errors"].append(error_text(ev.get("error")))
            elif t == "turn.completed":
                st["completed"] = True
                st["turn_done"] = time.time()
            elif t == "error":
                st["errors"].append(error_text(ev))

    out_thread = threading.Thread(target=read_events, daemon=True)
    out_thread.start()
    # Codex has been seen to hang after finishing its turn (observed 2026-09-25 with `exec resume`:
    # task_complete written, process idle for 20+ min). Don't let that block the supervisor forever.
    stuck = False
    while True:
        try:
            code = proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            if st["turn_done"] and time.time() - st["turn_done"] > EXIT_GRACE:
                stuck = True
                proc.kill()
    # Bounded joins: a sandboxed grandchild may inherit stdout/stderr and keep them open after codex exits.
    out_thread.join(timeout=10)
    err_thread.join(timeout=5)
    sid, final, errors, failed = st["sid"], st["final"], list(st["errors"]), st["failed"]
    err_lines = []
    for _ in range(20):
        try:
            err_lines = list(stderr_tail)
            break
        except RuntimeError:  # still being appended to
            time.sleep(0.05)
    # DONE means Codex reported its turn completed.  A clean process exit alone is not enough (a truncated
    # event stream must not look like success), and a non-zero exit after a completed turn (e.g. the watchdog
    # stopping a Codex that hung on the way out) does not undo the work.  Whether the work is any good is
    # for the supervisor to judge.
    ok = st["completed"] and not failed
    status = "DONE" if ok else "FAILED"

    model, effort, ran_in = session_settings(sid) if sid else (None, None, None)  # what actually ran
    model, effort = model or a.model, effort or a.effort
    head = f"[cx] {name} · {status}" + ("" if ok else f" (exit {code})") + f" · {dur(time.time() - t0)}"
    head += f" · {model or '?'}/{effort or '?'}" if (model or effort) else ""
    head += f" · session {sid}" if sid else ""
    if ran_in and os.path.normcase(os.path.normpath(ran_in)) != os.path.normcase(cwd):
        head += f" · ran in {ran_in}"  # e.g. a Codex-managed worktree (`-- --worktree`)
    # A worker can report DONE although its shell never worked (sandbox stalls, a resume whose sandbox
    # failed to start); the commands it tried are the cheap tell.
    if st["cmds"] and not st["cmds_ok"]:
        head += f" · 0 of {st['cmds']} commands succeeded"
    if stuck:
        head += f" · (codex did not exit {EXIT_GRACE}s after finishing; stopped it)"
    elif ok and code != 0:
        head += f" · (codex exited {code} after finishing)"
    print(head)
    if ok:
        print(final if final is not None else "(worker finished without a final message)")
    else:
        # Codex reports every reconnect attempt as an error; during an outage that is ~10 long lines per
        # worker.  Keep the final errors, count the retries.
        retries = [e for e in errors if e.startswith("Reconnecting...")]
        final_errors = [e for e in errors if not e.startswith("Reconnecting...")] or retries[-1:]
        for e in list(dict.fromkeys(final_errors))[-3:]:
            print("error:", e if len(e) <= 400 else e[:400] + " …")
        if retries:
            print(f"(after {len(retries)} reconnect attempts; last: {retries[-1][:160]})")
        if not st["turn_done"]:
            print(f"error: codex exited (code {code}) before finishing its turn")
        if not errors:
            print("\n".join(l for l in err_lines if l) or "(no error output)")
        if final:
            print("last message:", final)
    sys.stdout.flush()
    return 0 if ok else 1


def cmd_ps(a):
    sys.path.insert(0, HERE)
    import codex_dashboard as cd
    m = cd.Monitor(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"), 24)
    m.tick()
    now = time.time()
    rows = [s for s in m.sessions.values() if s.status(now)[0] == "running"]
    if not rows:
        print("no running codex exec workers")
        return 0
    thread_names = m.thread_names
    for s in sorted(rows, key=lambda s: s.started):
        r = s.summary(now, thread_names)
        fails = sum(1 for x in s.recent_exits if x not in (0, None))
        act = (r["last_action"] or {}).get("text", "")
        me = f"{r['model'] or '?'}/{r['effort'] or '?'}"
        print(f"{r['name'][:28]:28} {s.id}  {me:18} up {dur(now - s.started):>7}  idle {dur(now - s.last_ts):>7}  "
              f"cmds-failed {fails}/{len(s.recent_exits)}  {r['project']}  | {act[:70]}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="cx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one worker to completion; prints header + final answer")
    r.add_argument("-n", "--name", help="label shown in the header and the dashboard")
    r.add_argument("-C", "--cd", help="working directory (default: current)")
    r.add_argument("-m", "--model")
    r.add_argument("-e", "--effort", help="model_reasoning_effort, e.g. low/medium/high/xhigh")
    r.add_argument("-s", "--sandbox", choices=["read-only", "workspace-write", "danger-full-access"],
                   help="default workspace-write (resume: the session's own)")
    r.add_argument("--resume", metavar="SESSION", help="continue an earlier worker with its full context")
    r.add_argument("prompt", nargs="*", help="prompt text (default: read stdin)")
    sub.add_parser("ps", help="one health line per running codex exec worker")
    argv = sys.argv[1:]
    extra = []
    if "--" in argv:  # anything after -- goes to `codex exec` verbatim
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    a.extra = extra
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(cmd_run(a) if a.cmd == "run" else cmd_ps(a))


if __name__ == "__main__":
    main()
