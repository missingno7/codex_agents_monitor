#!/usr/bin/env python3
"""codex-dashboard: read-only local observability for `codex exec` agents.

Nothing has to cooperate with it.  Sessions are discovered from Codex's own rollout files
($CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl, written live while a session runs), and each
rollout is mapped to the codex process that holds it open via the Windows Restart Manager
(which reports PID + process start time, so PID reuse cannot confuse it).  A small web UI is
served on localhost.  Stdlib only.
"""
import argparse
import collections
import ctypes
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.path.expanduser("~"), ".codex-dashboard", "state")
RUNS_DIR = os.path.join(os.path.expanduser("~"), ".codex-dashboard", "runs")
IS_WIN = os.name == "nt"
MY_PID = os.getpid()

# ---------------------------------------------------------------------------------------------
# Windows process / file-ownership helpers (ctypes, no dependencies)
# ---------------------------------------------------------------------------------------------
if IS_WIN:
    import ctypes.wintypes as wt
    import msvcrt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    rstrtmgr = ctypes.WinDLL("rstrtmgr")

    INVALID_HANDLE = ctypes.c_void_p(-1).value
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    TH32CS_SNAPPROCESS = 0x2
    WAIT_OBJECT_0 = 0
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _pack_ = 4  # FILETIME is only 4-byte aligned
        _fields_ = [("dwProcessId", wt.DWORD), ("ProcessStartTime", ctypes.c_ulonglong)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [("Process", RM_UNIQUE_PROCESS), ("strAppName", wt.WCHAR * 256),
                    ("strServiceShortName", wt.WCHAR * 64), ("ApplicationType", ctypes.c_int),
                    ("AppStatus", wt.ULONG), ("TSSessionId", wt.DWORD), ("bRestartable", wt.BOOL)]

    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                wt.DWORD, wt.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG,
                                                ctypes.POINTER(wt.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wt.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    rstrtmgr.RmStartSession.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, wt.LPWSTR]
    rstrtmgr.RmRegisterResources.argtypes = [wt.DWORD, wt.UINT, ctypes.POINTER(wt.LPCWSTR),
                                             wt.UINT, ctypes.c_void_p, wt.UINT, ctypes.c_void_p]
    rstrtmgr.RmGetList.argtypes = [wt.DWORD, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT),
                                   ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(wt.DWORD)]
    rstrtmgr.RmEndSession.argtypes = [wt.DWORD]

    def process_snapshot():
        """{pid: (ppid, exe_name)} for every process, via one Toolhelp snapshot."""
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE:
            return {}
        out = {}
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
                ok = k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return out

    def open_process(pid):
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        return h or None

    def close_handle(h):
        if h:
            k32.CloseHandle(h)

    def process_ctime(h):
        c, e, kt, ut = (ctypes.c_ulonglong() for _ in range(4))
        if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            return c.value
        return None

    def process_exited(h):
        """None while running, else the exit code."""
        if k32.WaitForSingleObject(h, 0) != WAIT_OBJECT_0:
            return None
        code = wt.DWORD()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value

    def process_cmdline(h):
        size = wt.ULONG(0)
        ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) != 0:
            return None
        length = ctypes.c_ushort.from_buffer(buf, 0).value
        ptr = ctypes.c_void_p.from_buffer(buf, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4).value
        return ctypes.wstring_at(ptr, length // 2) if ptr else ""

    def split_cmdline(cmd):
        n = ctypes.c_int()
        arr = shell32.CommandLineToArgvW(cmd, ctypes.byref(n))
        if not arr:
            return cmd.split()
        try:
            return [arr[i] for i in range(n.value)]
        finally:
            k32.LocalFree(arr)

    def file_holders(path):
        """[(pid, process_start_filetime)] of processes holding `path` open (Restart Manager)."""
        h = wt.DWORD()
        key = ctypes.create_unicode_buffer(64)
        if rstrtmgr.RmStartSession(ctypes.byref(h), 0, key) != 0:
            return None
        try:
            arr = (wt.LPCWSTR * 1)(path)
            if rstrtmgr.RmRegisterResources(h, 1, arr, 0, None, 0, None) != 0:
                return None
            need, n, reason = wt.UINT(0), wt.UINT(0), wt.DWORD()
            rc = rstrtmgr.RmGetList(h, ctypes.byref(need), ctypes.byref(n), None, ctypes.byref(reason))
            if rc == 0 and need.value == 0:
                return []
            for _ in range(3):
                buf = (RM_PROCESS_INFO * max(need.value, 1))()
                n = wt.UINT(len(buf))
                rc = rstrtmgr.RmGetList(h, ctypes.byref(need), ctypes.byref(n), buf, ctypes.byref(reason))
                if rc == 0:
                    return [(buf[i].Process.dwProcessId, buf[i].Process.ProcessStartTime) for i in range(n.value)]
                if rc != 234:  # ERROR_MORE_DATA
                    return None
            return None
        finally:
            rstrtmgr.RmEndSession(h)

    def open_shared(path):
        """Open for reading while letting Codex keep writing, renaming or deleting the file."""
        h = k32.CreateFileW(path, GENERIC_READ, 7, None, OPEN_EXISTING, 0x80, None)
        if not h or h == INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "cannot open", path)
        return os.fdopen(msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY), "rb")
else:  # Non-Windows: discovery + transcripts still work; liveness falls back to file activity.
    def process_snapshot():
        return {}

    def file_holders(path):
        return None

    def open_shared(path):
        return open(path, "rb")


def filetime_to_epoch(ft):
    return ft / 1e7 - 11644473600.0 if ft else None


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def clean_path(p):
    if not p:
        return p
    if p.startswith("\\\\?\\"):
        p = p[4:]
    if p.startswith("file:///"):  # file:///D:/x on Windows, file:///tmp/x elsewhere
        p = unquote(p[8:] if IS_WIN else p[7:])
    return os.path.normpath(p)


def clip(s, head=2500, tail=2500):
    if s is None:
        return ""
    if len(s) <= head + tail + 200:
        return s
    return s[:head] + f"\n… [{len(s) - head - tail:,} characters omitted] …\n" + s[-tail:]


def first_line(s, n=160, minlen=1):
    for line in (s or "").splitlines():
        line = line.strip()
        if len(line) >= minlen:
            return line[:n] + ("…" if len(line) > n else "")
    return ""


_project_cache = {}


def project_info(cwd):
    """(project_root, project_name, branch_from_HEAD) for a working directory."""
    if not cwd:
        return None, "?", None
    key = os.path.normcase(cwd)
    hit = _project_cache.get(key)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    root, d = None, cwd
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            root = d
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    branch = None
    if root:
        try:
            gitp = os.path.join(root, ".git")
            if os.path.isfile(gitp):  # worktree / submodule: "gitdir: <path>"
                with open(gitp, encoding="utf-8") as f:
                    gd = f.read().split(":", 1)[1].strip()
                gitp = gd if os.path.isabs(gd) else os.path.join(root, gd)
            with open(os.path.join(gitp, "HEAD"), encoding="utf-8") as f:
                head = f.read().strip()
            branch = head[16:] if head.startswith("ref: refs/heads/") else head[:10]
        except Exception:
            pass
    res = (root or cwd, os.path.basename((root or cwd).rstrip("\\/")) or cwd, branch)
    _project_cache[key] = (time.time(), res)
    return res


# ---------------------------------------------------------------------------------------------
# codex exec command-line parsing
# ---------------------------------------------------------------------------------------------
VALUE_OPTS = {"-c", "--config", "-m", "--model", "-p", "--profile", "-s", "--sandbox", "-a",
              "--ask-for-approval", "-C", "--cd", "-i", "--image", "--enable", "--disable",
              "--add-dir", "--local-provider", "-o", "--output-last-message", "--output-schema",
              "--color", "--thread-source"}
GENERIC_NAMES = {"final", "last", "out", "output", "result", "results", "answer", "message",
                 "last_message", "last-message", "codex", "log", "report", "summary", "tmp",
                 "temp", "build", "logs", "outputs", "response", "workers", "worker"}


def parse_codex_argv(argv):
    """Return dict(subcommand, cd, out, model, effort, resume) or None if not a codex CLI call."""
    info = {"sub": None, "cd": None, "out": None, "model": None, "effort": None, "resume": False}
    i, args = 1, argv
    while i < len(args):
        a = args[i]
        val = None
        if a.startswith("--") and "=" in a:
            a, val = a.split("=", 1)
        if a in VALUE_OPTS:
            if val is None:
                val = args[i + 1] if i + 1 < len(args) else None
                i += 1
            if a in ("-C", "--cd"):
                info["cd"] = val
            elif a in ("-o", "--output-last-message"):
                info["out"] = val
            elif a in ("-m", "--model"):
                info["model"] = val
            elif a in ("-c", "--config") and val and val.replace(" ", "").startswith("model_reasoning_effort="):
                info["effort"] = val.split("=", 1)[1].strip().strip("\"'")
        elif not a.startswith("-"):
            if info["sub"] is None:
                info["sub"] = a
            elif info["sub"] in ("exec", "e") and a == "resume":
                info["resume"] = True
        i += 1
    return info


def name_from_output_path(out, cwd):
    """`-o build/workers/sprite/FINAL.md` -> 'sprite'.  Deterministic, no guessing beyond paths."""
    if not out:
        return None
    p = out if os.path.isabs(out) else os.path.join(cwd or "", out)
    p = os.path.normpath(p)
    parent = os.path.dirname(p)
    stem = os.path.splitext(os.path.basename(p))[0]
    base_dirs = {os.path.normcase(os.path.normpath(cwd))} if cwd else set()
    if os.path.normcase(parent) not in base_dirs:
        pname = os.path.basename(parent)
        if pname and pname.lower() not in GENERIC_NAMES:
            return pname
    if stem and stem.lower() not in GENERIC_NAMES:
        return stem
    return None


NAME_RE = re.compile(r"^\W*(?:(?:your|worker|agent)\s+name\s*(?:is\b|[:=])|(?:worker|agent|name)\s*[:=])"
                     r"\s*[`\"'*]*([A-Za-z0-9][\w.\-]{0,40})", re.I)


# ---------------------------------------------------------------------------------------------
# Rollout parsing -> transcript events
# ---------------------------------------------------------------------------------------------
CMD_RE = re.compile(r"\bcmd\s*:\s*\"((?:[^\"\\]|\\.)*)\"")
PATCH_FILE_RE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: ([^\\\n\"]+)")
TOOL_RE = re.compile(r"\btools\.(\w+)\s*\(")


def describe_call(name, raw):
    """Human text for a tool call issued by the model (visible while it is still running)."""
    if name == "exec" and isinstance(raw, str):  # code-mode JS cell
        cmds = []
        for m in CMD_RE.finditer(raw):
            try:
                cmds.append(json.loads('"' + m.group(1) + '"'))
            except Exception:
                cmds.append(m.group(1))
        if cmds:
            return " ; ".join(cmds)
        files = PATCH_FILE_RE.findall(raw)
        if files:
            return "apply_patch " + ", ".join(os.path.basename(f.strip()) for f in files[:4])
        used = [t for t in dict.fromkeys(TOOL_RE.findall(raw)) if t != "exec_command"]
        lines = [l.strip() for l in raw.splitlines() if len(l.strip()) > 3]
        txt = lines[0] if lines else raw.strip()
        return (", ".join(used) + ": " if used else "") + txt[:240]
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return first_line(str(raw), 240)
    if isinstance(args, dict):
        for k in ("cmd", "command", "code", "input"):
            v = args.get(k)
            if v:
                return (" ".join(v) if isinstance(v, list) else str(v))[:600]
        return json.dumps(args, ensure_ascii=False)[:300]
    return str(args)[:300]


def command_text(item):
    parsed = item.get("parsed_cmd") or []
    cmds = [p.get("cmd") for p in parsed if isinstance(p, dict) and p.get("cmd")]
    if cmds:
        return " && ".join(dict.fromkeys(cmds))
    cmd = item.get("command")
    if isinstance(cmd, list):
        if len(cmd) >= 3 and cmd[1] in ("-Command", "-c", "/c", "-lc"):
            return cmd[2]
        return " ".join(cmd)
    return str(cmd or "")


def duration_ms(d):
    if isinstance(d, dict):
        return int(d.get("secs", 0) * 1000 + d.get("nanos", 0) / 1e6)
    return None


def rec_to_events(rec, state):
    """Translate one rollout record into zero or more display events."""
    typ, p = rec.get("type"), rec.get("payload") or {}
    ts = parse_ts(rec.get("timestamp", "")) or 0
    ev = []
    if typ == "event_msg":
        et = p.get("type")
        if et == "item_completed":
            state["items"] = True
            it = p.get("item") or {}
            k = it.get("type")
            if k == "UserMessage":
                text = "\n".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                ev.append({"kind": "prompt", "text": text})
            elif k == "AgentMessage":
                text = "\n".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                ev.append({"kind": "message", "text": text, "final": it.get("phase") == "final_answer"})
            elif k == "Reasoning":
                summ = "\n".join(s if isinstance(s, str) else s.get("text", "") for s in it.get("summary_text") or [])
                raw = "\n".join(s if isinstance(s, str) else s.get("text", "") for s in it.get("raw_content") or [])
                ms = (p.get("completed_at_ms") or 0) - (p.get("started_at_ms") or 0)
                ev.append({"kind": "thinking", "text": summ or raw, "ms": ms if ms > 0 else None})
            elif k == "CommandExecution":
                out = it.get("aggregated_output")
                if out is None:
                    out = (it.get("stdout") or "") + (it.get("stderr") or "")
                ev.append({"kind": "exec", "cmd": command_text(it), "cwd": clean_path(it.get("cwd")),
                           "exit": it.get("exit_code"), "status": it.get("status"),
                           "ms": duration_ms(it.get("duration")), "text": clip(out)})
            elif k == "FileChange":
                files = []
                for path, ch in (it.get("changes") or {}).items():
                    body = ch.get("unified_diff") or ch.get("content") or ""
                    files.append({"path": path, "type": ch.get("type"), "diff": clip(body, 3000, 1000)})
                ev.append({"kind": "patch", "files": files, "status": it.get("status")})
            elif k == "McpToolCall":
                res = it.get("result") or {}
                txt = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict)) if isinstance(res, dict) else str(res)
                ev.append({"kind": "tool", "name": f"{it.get('server')}.{it.get('tool')}",
                           "args": json.dumps(it.get("arguments"), ensure_ascii=False)[:600],
                           "status": it.get("status"), "text": clip(txt, 1500, 500),
                           "ms": duration_ms(it.get("duration"))})
            elif k == "ContextCompaction":
                ev.append({"kind": "note", "text": "context compacted"})
            elif k in ("SubAgentActivity", "CollabAgentToolCall"):
                desc = it.get("agent_path") or it.get("tool") or ""
                ev.append({"kind": "note", "text": f"{k}: {it.get('kind') or it.get('status') or ''} {desc}".strip()})
            elif k == "Extension":
                ev.append({"kind": "note", "text": f"{it.get('kind')}" + (f" {it.get('durationMs')} ms" if it.get("durationMs") else "")})
            else:
                ev.append({"kind": "note", "text": f"{k}"})
        elif et == "task_started":
            ev.append({"kind": "turn", "text": "turn started"})
        elif et == "task_complete":
            err = p.get("error")
            if err:
                msg = err.get("message") if isinstance(err, dict) else str(err)
                try:
                    msg = json.loads(msg)["error"]["message"]
                except Exception:
                    pass
                ev.append({"kind": "error", "text": f"turn failed: {msg}"})
            else:
                secs = (p.get("duration_ms") or 0) / 1000
                ev.append({"kind": "done", "text": f"turn complete ({secs:.0f}s)"})
        elif et == "turn_aborted":
            ev.append({"kind": "error", "text": f"turn aborted: {p.get('reason', '')}"})
        elif et in ("error", "stream_error", "warning"):
            ev.append({"kind": "error" if et != "warning" else "note", "text": f"{et}: {p.get('message', '')}"})
        elif et == "user_message" and not state.get("items"):
            ev.append({"kind": "prompt", "text": p.get("message", "")})
        elif et == "agent_message" and not state.get("items"):
            ev.append({"kind": "message", "text": p.get("message", ""), "final": p.get("phase") == "final_answer"})
    elif typ == "response_item":
        pt = p.get("type")
        if pt == "custom_tool_call":
            raw = p.get("input")
            ev.append({"kind": "call", "name": p.get("name"), "text": describe_call(p.get("name"), raw),
                       "code": clip(raw, 3000, 500) if isinstance(raw, str) else None})
        elif pt == "function_call" and p.get("name") != "wait":
            ev.append({"kind": "call", "name": p.get("name"), "text": describe_call(p.get("name"), p.get("arguments"))})
        elif pt == "local_shell_call":
            act = p.get("action") or {}
            ev.append({"kind": "call", "name": "shell", "text": " ".join(act.get("command") or [])})
    for e in ev:
        e["ts"] = ts
    return ev


def iter_lines(path, start):
    """Complete lines appended after byte offset `start`; returns (lines, new_offset, eof).

    Reads through a handle instead of trusting directory metadata: NTFS keeps a file's
    timestamps (and possibly size) stale while Codex holds it open."""
    with open_shared(path) as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], start, start + len(data)
    return data[:end].split(b"\n"), start + end + 1, start + len(data)


# ---------------------------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------------------------
class Session:
    def __init__(self, path, meta):
        self.path = path
        self.id = meta.get("id") or meta.get("session_id")
        self.cwd = clean_path(meta.get("cwd"))
        self.started = parse_ts(meta.get("timestamp", "")) or 0
        self.originator = meta.get("originator")
        self.source = meta.get("source")
        self.thread_source = meta.get("thread_source")
        src = meta.get("source")
        self.parent_id = None
        if isinstance(src, dict):
            spawn = (src.get("subagent") or {}).get("thread_spawn") if isinstance(src.get("subagent"), dict) else None
            self.parent_id = (spawn or {}).get("parent_thread_id") or meta.get("parent_thread_id")
        self.git = meta.get("git") or {}
        self.cli_version = meta.get("cli_version")
        self.model = self.effort = None
        self.prompt = None
        self.last_ts = self.started
        self.task_state = None          # running | complete | error | aborted
        self.task_error = None
        self.completed_at = None
        self.last_action = None         # (kind, text, ts)
        self.tokens = None
        self.offset = 0                 # bytes consumed by the summary follower
        self.size = 0
        # liveness
        self.live = False
        self.probed = False
        self.probed_size = -1
        self.proc = None                # dict(pid, ctime, cmdline, ...)
        self.exit_code = None
        self.ended_at = None
        self.name_hint = None
        self.out_path = None
        self.state = {}
        self.names = ("", "")
        self.names_stale = True
        self.recent_exits = collections.deque(maxlen=20)   # exit codes of the latest commands
        self.run = None                 # metadata written by `cx run` for managed workers

    # -- summary maintenance ---------------------------------------------------------------
    def apply(self, rec):
        typ, p = rec.get("type"), rec.get("payload") or {}
        ts = parse_ts(rec.get("timestamp", ""))
        if ts:
            self.last_ts = max(self.last_ts, ts)
        if typ == "turn_context":
            self.model = p.get("model") or self.model
            self.effort = p.get("effort") or p.get("reasoning_effort") or self.effort
            return
        if typ == "event_msg":
            et = p.get("type")
            if et == "task_started":  # a new turn (e.g. `codex exec resume`): forget the last run's outcome
                self.task_state, self.task_error, self.completed_at = "running", None, None
                if not self.live:
                    self.exit_code = self.ended_at = None
            elif et == "task_complete":
                err = p.get("error")
                self.task_state = "error" if err else "complete"
                if err:
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    try:
                        msg = json.loads(msg)["error"]["message"]
                    except Exception:
                        pass
                    self.task_error = msg
                self.completed_at = ts
            elif et == "turn_aborted":
                self.task_state, self.completed_at = "aborted", ts
            elif et == "token_count":
                info = p.get("info") or {}
                tot = (info.get("total_token_usage") or {}).get("total_tokens")
                if tot:
                    self.tokens = tot
                return
        for e in rec_to_events(rec, self.state):
            k = e["kind"]
            if k == "prompt" and self.prompt is None:
                self.prompt = e["text"]
                self.names_stale = True
            if k in ("turn",):
                continue
            if k == "exec":  # skip here-string openers like "@'" to show the meaningful line
                self.recent_exits.append(e.get("exit"))
                txt = "$ " + (first_line(e["cmd"], 200, 4) or first_line(e["cmd"], 200))
            elif k == "call":
                txt = "▶ " + (first_line(e["text"], 200, 4) or first_line(e["text"], 200))
            elif k == "patch":
                txt = "edit " + ", ".join(os.path.basename(f["path"]) for f in e["files"][:4])
            elif k == "tool":
                txt = "tool " + e["name"]
            elif k == "thinking":
                txt = first_line(e["text"], 200) or "thinking"
            else:
                txt = first_line(e.get("text", ""), 200)
            self.last_action = (k, txt, e["ts"])

    def feed(self, lines):
        for raw in lines:
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            self.apply(rec)

    def initial_scan(self):
        """Read the head (prompt/model) and the tail (latest state) without parsing everything."""
        with open_shared(self.path) as f:
            size = f.seek(0, 2)
            f.seek(0)
            head_lines, head_end = [], 0
            while head_end < 8 << 20:
                line = f.readline()
                if not line.endswith(b"\n"):  # EOF, or a record Codex is still writing
                    break
                head_end += len(line)
                head_lines.append(line)
                if b'"UserMessage"' in line or (len(head_lines) > 400):
                    break
            tail_start = max(head_end, size - (512 << 10))
            if tail_start > head_end:  # start at a record boundary
                f.seek(tail_start - 1)
                tail = f.read()
                aligned = tail[:1] == b"\n"
                tail = tail[1:]
            else:
                f.seek(tail_start)
                tail, aligned = f.read(), True
        self.feed(head_lines)
        if tail:
            end = tail.rfind(b"\n")
            body = tail[:end] if end >= 0 else b""
            lines = body.split(b"\n")
            if not aligned and lines:
                lines = lines[1:]  # first line is partial
            self.feed(lines)
            self.offset = tail_start + (end + 1 if end >= 0 else 0)
        else:
            self.offset = head_end
        if self.task_state is None and self.prompt is not None:
            self.task_state = "running"
        self.size = size

    def follow(self):
        try:
            lines, self.offset, self.size = iter_lines(self.path, self.offset)
        except OSError:
            return False
        self.feed(lines)
        return bool(lines)

    # -- presentation --------------------------------------------------------------------------
    def status(self, now):
        if self.live:
            return "running", "RUNNING"
        if self.task_state == "complete":
            if self.exit_code not in (None, 0):
                return "failed", f"EXIT {self.exit_code}"
            return "done", "DONE"
        if self.task_state == "error":
            return "failed", "FAILED"
        if self.task_state == "aborted":
            return "failed", "ABORTED"
        if not IS_WIN and now - self.last_ts < 120:
            return "running", "RUNNING?"
        if not self.probed and IS_WIN:
            return "running", "CHECKING"
        if self.exit_code is not None:  # observed exit without a completed turn (killed / crashed)
            return "failed", "INTERRUPTED" if self.exit_code == 0xC000013A else f"EXITED {self.exit_code}"
        return "failed", "EXITED"

    def summary(self, now, thread_names):
        cat, label = self.status(now)
        root, pname, head_branch = project_info(self.cwd)
        name, subtitle = self.names
        tn = thread_names.get(self.id)
        if tn:
            name, subtitle = tn, (self.names[1] or self.names[0])
        ended = None
        if cat != "running":
            ended = self.ended_at or self.completed_at or self.last_ts
        proc = self.proc or {}
        return {
            "id": self.id, "name": name, "subtitle": subtitle if subtitle != name else "",
            "project": pname, "project_root": root, "cwd": self.cwd,
            "branch": self.git.get("branch") or head_branch, "status": cat, "label": label,
            "started": self.started, "ended": ended, "last_activity": self.last_ts,
            "last_action": {"kind": self.last_action[0], "text": self.last_action[1], "ts": self.last_action[2]} if self.last_action else None,
            "pid": proc.get("pid"), "model": self.model or proc.get("model"),
            "effort": self.effort or proc.get("effort"), "exit_code": self.exit_code,
            "tokens": self.tokens, "error": self.task_error, "parent_id": self.parent_id,
            "launched_by": proc.get("ancestry"), "out_path": self.out_path,
            "cmdline": proc.get("cmdline"), "rollout": self.path, "cli_version": self.cli_version,
            "repo_url": self.git.get("repository_url"), "commit": (self.git.get("commit_hash") or "")[:10],
        }


# ---------------------------------------------------------------------------------------------
# Monitor: process tracking + session discovery + liveness mapping
# ---------------------------------------------------------------------------------------------
class Monitor:
    def __init__(self, codex_home, history_hours):
        self.home = codex_home
        self.sessions_dir = os.path.join(codex_home, "sessions")
        self.history = history_hours * 3600
        self.lock = threading.RLock()
        self.sessions = {}            # id -> Session
        self.by_path = {}             # path -> id or None (ignored)
        self.procs = {}               # pid -> dict (tracked codex processes, handle held)
        self.snapshot = {}
        self.thread_names = {}
        self._index_size = -1
        self.cache_path = os.path.join(STATE_DIR, "sessions.json")
        self.cache = self._load_cache()
        self._cache_dirty = False
        self._last_growth_check = 0
        self._names_dirty = True
        self.ready = False

    # -- persistence of the few facts Codex does not keep (cmdline, exit code) -----------
    def _load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            cutoff = time.time() - 14 * 86400
            return {k: v for k, v in data.items() if v.get("seen", 0) > cutoff}
        except Exception:
            return {}

    def _save_cache(self):
        if not self._cache_dirty:
            return
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
            os.replace(tmp, self.cache_path)
            self._cache_dirty = False
        except Exception:
            pass

    def _remember(self, s):
        proc = dict(s.proc or {})
        proc.pop("handle", None)
        self.cache[s.id] = {"proc": proc, "exit_code": s.exit_code, "ended_at": s.ended_at,
                            "name_hint": s.name_hint, "out_path": s.out_path, "seen": time.time()}
        self._cache_dirty = True

    # -- processes ------------------------------------------------------------------------------
    def _ancestry(self, pid, child_ctime):
        chain, cur, ctime = [], self.snapshot.get(pid, (0, ""))[0], child_ctime
        for _ in range(8):
            if not cur or cur not in self.snapshot:
                break
            h = open_process(cur)
            pct = process_ctime(h) if h else None
            close_handle(h)
            if pct is None or (ctime and pct > ctime):  # parent PID was reused -> stop
                break
            chain.append({"pid": cur, "name": self.snapshot[cur][1]})
            ctime, cur = pct, self.snapshot[cur][0]
        return chain

    def _track(self, pid, name):
        h = open_process(pid)
        if not h:
            return None
        ctime = process_ctime(h)
        rec = {"pid": pid, "name": name, "ctime": ctime, "handle": h, "seen": time.time(),
               "exec": False, "cmd_failed": True}
        self.procs[pid] = rec  # stored first so the handle is always closed when the process exits
        try:
            self._classify(rec)
        except Exception:
            pass
        return rec

    def _classify(self, rec):
        cmd = process_cmdline(rec["handle"])
        rec["cmd_failed"] = not cmd  # a process caught mid-startup may not expose it yet: retry later
        argv = split_cmdline(cmd) if cmd else []
        info = parse_codex_argv(argv) if argv else {"sub": None}
        is_exec = info.get("sub") in ("exec", "e")
        rec["exec"] = is_exec
        pid, ctime = rec["pid"], rec["ctime"]
        if is_exec:
            rec.update({"cmdline": cmd, "cd": info["cd"], "out": info["out"], "model": info["model"],
                        "effort": info["effort"], "resume": info["resume"],
                        "started": filetime_to_epoch(ctime), "ancestry": self._ancestry(pid, ctime)})

    def refresh_processes(self):
        self.snapshot = process_snapshot()
        exited = []
        for pid, rec in list(self.procs.items()):
            code = process_exited(rec["handle"])
            if code is not None:
                close_handle(rec["handle"])
                del self.procs[pid]
                if rec["exec"]:
                    exited.append((rec, code))
            elif rec.get("cmd_failed") and time.time() - rec["seen"] < 60:
                self._classify(rec)
        for pid, (ppid, name) in self.snapshot.items():
            if pid in self.procs or pid == MY_PID:
                continue
            ln = name.lower()
            if ln.startswith("codex") and ln.endswith(".exe"):
                self._track(pid, name)
        return exited

    # -- session discovery ----------------------------------------------------------------------
    def _day_dirs(self):
        now = dt.datetime.now()
        days = int(self.history // 86400) + 2
        out = []
        for i in range(days + 1):
            d = now - dt.timedelta(days=i)
            out.append(os.path.join(self.sessions_dir, f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}"))
        tomorrow = now + dt.timedelta(days=1)
        out.append(os.path.join(self.sessions_dir, f"{tomorrow.year:04d}", f"{tomorrow.month:02d}", f"{tomorrow.day:02d}"))
        return out

    def _read_meta(self, path):
        with open_shared(path) as f:
            line = f.readline()
        if not line.endswith(b"\n"):
            return "partial"
        rec = json.loads(line)
        return rec.get("payload") if rec.get("type") == "session_meta" else None

    def discover(self):
        cutoff = time.time() - self.history
        new = []
        for d in self._day_dirs():
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                if not (n.startswith("rollout-") and n.endswith(".jsonl")):
                    continue
                path = os.path.join(d, n)
                if path in self.by_path:
                    continue
                try:
                    meta = self._read_meta(path)
                except Exception:
                    meta = None
                if meta == "partial":
                    continue  # try again next tick
                if not meta or meta.get("originator") != "codex_exec" or meta.get("thread_source") == "guardian_review":
                    self.by_path[path] = None
                    continue
                s = Session(path, meta)
                try:
                    s.initial_scan()
                except Exception:
                    continue
                if s.last_ts < cutoff and s.task_state in ("complete", "error", "aborted"):
                    self.by_path[path] = None
                    continue
                c = self.cache.get(s.id)
                if c:
                    s.exit_code, s.ended_at = c.get("exit_code"), c.get("ended_at")
                    s.name_hint, s.out_path = c.get("name_hint"), c.get("out_path")
                    if c.get("proc"):
                        s.proc = c["proc"]
                self.by_path[path] = s.id
                self.sessions[s.id] = s
                self._load_run(s)
                new.append(s)
        if new:
            self._names_dirty = True
        return new

    def _thread_index(self):
        p = os.path.join(self.home, "session_index.jsonl")
        try:
            size = os.path.getsize(p)
        except OSError:
            return
        if size == self._index_size:
            return
        self._index_size = size
        names = {}
        try:
            with open_shared(p) as f:
                for line in f.read().decode("utf-8", "replace").splitlines():
                    try:
                        r = json.loads(line)
                        if r.get("thread_name"):
                            names[r["id"]] = r["thread_name"]
                    except Exception:
                        pass
            self.thread_names = names
        except Exception:
            pass

    def _load_run(self, s):
        """Managed workers (`cx run`) leave ~/.codex-dashboard/runs/<session>.json with a label."""
        try:
            with open(os.path.join(RUNS_DIR, s.id + ".json"), encoding="utf-8") as f:
                run = json.load(f)
        except (OSError, ValueError):
            return
        if run != s.run:
            s.run, s.names_stale = run, True
            if s.exit_code is None and run.get("exit_code") is not None and not s.live:
                s.exit_code = run["exit_code"]

    # -- liveness ------------------------------------------------------------------------------
    def probe(self, s):
        holders = file_holders(s.path)
        s.probed = True
        if holders is None:
            return
        s.probed_size = s.size
        for pid, start in holders:
            if pid == MY_PID:
                continue
            rec = self.procs.get(pid)
            if rec is None or rec["ctime"] != start:
                name = self.snapshot.get(pid, (0, "codex.exe"))[1]
                if rec is not None:  # stale PID entry
                    close_handle(rec["handle"])
                    del self.procs[pid]
                rec = self._track(pid, name)
            if rec and rec["exec"] and rec["ctime"] == start:
                self._attach(s, rec)
                return
        if s.live:
            s.live = False

    def _attach(self, s, rec):
        s.live = True
        s.exit_code = s.ended_at = None
        s.proc = {k: v for k, v in rec.items() if k != "handle"}
        s.out_path = rec.get("out")
        s.name_hint = name_from_output_path(rec.get("out"), rec.get("cd") or s.cwd)
        self._names_dirty = True
        self._remember(s)

    def compute_names(self):
        by_cwd = collections.defaultdict(list)
        for s in self.sessions.values():
            by_cwd[os.path.normcase(s.cwd or "")].append(s)
        for group in by_cwd.values():
            linesets = {}
            for s in group:
                ls = []
                for line in (s.prompt or "").splitlines():
                    t = line.strip().lstrip("#>*- ").strip()
                    if len(t) >= 4 and t not in ls:
                        ls.append(t)
                linesets[s.id] = ls
            counts = collections.Counter(l for ls in linesets.values() for l in set(ls))
            n = len(group)
            for s in group:
                ls = linesets[s.id]
                if n > 1:
                    distinct = [l for l in ls if counts[l] - 1 < max(1, (n - 1) / 2)]
                else:
                    distinct = []
                cands = distinct or ls
                regex_name = None
                for l in ls[:80]:
                    m = NAME_RE.match(l)
                    if m and (l in distinct or n == 1):
                        regex_name = (m.group(1).rstrip("."), l)
                        break
                if s.run and s.run.get("name"):
                    name, used = s.run["name"], None
                elif s.name_hint:
                    name = s.name_hint
                    used = regex_name[1] if regex_name and regex_name[0] == s.name_hint else None
                elif regex_name:
                    name, used = regex_name
                else:
                    name, used = None, None
                rest = [l for l in cands if l != used] or [l for l in ls if l != used]
                if name:
                    s.names = (name, first_line(rest[0], 200) if rest else "")
                elif cands:
                    s.names = (first_line(cands[0], 110), first_line(cands[1], 200) if len(cands) > 1 else "")
                else:
                    s.names = (first_line(s.prompt, 110) or f"session {s.id[:13]}", "")
                s.names_stale = False
        self._names_dirty = False

    # -- main loop -------------------------------------------------------------------------------
    def tick(self):
        with self.lock:
            now = time.time()
            exited = self.refresh_processes()
            new = self.discover()
            self._thread_index()
            for rec, code in exited:
                for s in self.sessions.values():
                    if s.live and s.proc and s.proc.get("pid") == rec["pid"] and s.proc.get("ctime") == rec["ctime"]:
                        try:
                            s.follow()  # pick up the final records
                        except Exception:
                            pass
                        s.live, s.exit_code, s.ended_at = False, code, now
                        s.probed_size = s.size
                        self._remember(s)
            for s in new:
                if s.task_state == "running" or s.task_state is None:
                    self.probe(s)
                else:
                    s.probed, s.probed_size = True, s.size
            live_procs = {(r["pid"], r["ctime"]) for r in self.procs.values() if r["exec"]}
            for s in self.sessions.values():
                if s.live:
                    if s.proc and (s.proc.get("pid"), s.proc.get("ctime")) not in live_procs:
                        s.live = False  # vanished without us seeing the exit (should not happen)
                        s.ended_at = s.ended_at or now
                    s.follow()
                    if not s.run:
                        self._load_run(s)
            # Finished sessions can come back to life (`codex exec resume`): watch for growth.
            if now - self._last_growth_check > 3:
                self._last_growth_check = now
                budget = time.time() + 1.5
                for s in list(self.sessions.values()):
                    if s.live:
                        continue
                    grew = s.follow()
                    if (grew or s.size != s.probed_size or not s.probed) and s.task_state == "running" and time.time() < budget:
                        self.probe(s)
                    elif grew and s.task_state == "running":
                        s.probed = False
            if self._names_dirty or any(s.names_stale for s in self.sessions.values()):
                self.compute_names()
            # Expire old finished sessions from memory.
            cutoff = now - self.history
            for sid in [k for k, s in self.sessions.items() if not s.live and s.last_ts < cutoff]:
                self.by_path[self.sessions[sid].path] = None
                del self.sessions[sid]
            self._save_cache()
            self.ready = True

    def run(self, interval):
        while True:
            try:
                self.tick()
            except Exception as e:  # never let the observer die
                print("monitor error:", repr(e), file=sys.stderr)
            time.sleep(interval)

    # -- API views -------------------------------------------------------------------------------
    def state(self):
        with self.lock:
            now = time.time()
            rows = [s.summary(now, self.thread_names) for s in self.sessions.values()]
            mapped = {(s.proc or {}).get("pid") for s in self.sessions.values() if s.live}
            # codex exec processes without a session file (e.g. --ephemeral, or just starting)
            for r in self.procs.values():
                if r["exec"] and r["pid"] not in mapped and now - r["seen"] > 6:
                    cd = clean_path(r.get("cd")) if r.get("cd") else None
                    root, pname, br = project_info(cd) if cd else (None, "?", None)
                    rows.append({"id": f"pid-{r['pid']}", "name": name_from_output_path(r.get("out"), cd) or f"codex exec (PID {r['pid']})",
                                 "subtitle": "process without a session file (e.g. --ephemeral) — no transcript available",
                                 "project": pname, "project_root": root, "cwd": cd, "branch": br,
                                 "status": "running", "label": "RUNNING", "started": r.get("started"),
                                 "ended": None, "last_activity": r.get("started"), "last_action": None,
                                 "pid": r["pid"], "model": r.get("model"), "effort": r.get("effort"),
                                 "launched_by": r.get("ancestry"), "cmdline": r.get("cmdline"),
                                 "process_only": True})
            counts = collections.Counter(r["status"] for r in rows)
            return {"now": now, "ready": self.ready, "codex_home": self.home,
                    "history_hours": self.history / 3600, "sessions": rows,
                    "counts": {"running": counts["running"], "done": counts["done"], "failed": counts["failed"]}}

    def session_path(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
            return s.path if s else None


# ---------------------------------------------------------------------------------------------
# Transcripts (parsed on demand, followed incrementally, small LRU)
# ---------------------------------------------------------------------------------------------
class Transcript:
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.events = []
        self.state = {}
        self.lock = threading.Lock()

    def update(self):
        with self.lock:
            try:
                lines, self.offset, _ = iter_lines(self.path, self.offset)
            except OSError:
                return
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                for e in rec_to_events(rec, self.state):
                    # merge consecutive "thinking" blocks without text into one line
                    prev = self.events[-1] if self.events else None
                    if e["kind"] == "thinking" and not e["text"] and prev and prev["kind"] == "thinking" and not prev["text"]:
                        prev["n"] = prev.get("n", 1) + 1
                        prev["ms"] = (prev.get("ms") or 0) + (e.get("ms") or 0)
                        prev["rev"] = prev.get("rev", 0) + 1
                        continue
                    e["i"] = len(self.events)
                    self.events.append(e)


class Transcripts:
    def __init__(self, size=12):
        self.items = collections.OrderedDict()
        self.lock = threading.Lock()
        self.size = size

    def get(self, path):
        with self.lock:
            t = self.items.pop(path, None) or Transcript(path)
            self.items[path] = t
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        t.update()
        return t


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
def make_handler(monitor, transcripts, host, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"{host}:{port}"}
    index_path = os.path.join(APP_DIR, "static", "index.html")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Refuse DNS-rebinding style requests: only answer to our own localhost origin.
            if self.headers.get("Host") not in allowed_hosts:
                return self._send(403, {"error": "forbidden host"})
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            try:
                if not parts:
                    with open(index_path, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if parts == ["api", "state"]:
                    return self._send(200, monitor.state())
                if len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "stream":
                    return self.stream(parts[2])
                if parts == ["api", "ping"]:
                    return self._send(200, {"app": "codex-dashboard"})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            return self._send(404, {"error": "not found"})

        def stream(self, sid):
            path = monitor.session_path(sid)
            if not path:
                return self._send(404, {"error": "unknown session"})
            try:
                sent = int(self.headers.get("Last-Event-ID") or -1) + 1
            except ValueError:
                sent = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(30)  # a client that stops reading must not pin this thread
            revs = {}
            last_write = time.time()
            try:
                while True:
                    t = transcripts.get(path)
                    with t.lock:
                        batch = t.events[sent:]
                        # re-send merged "thinking" rows whose counters changed
                        upd = [e for e in t.events[max(0, sent - 3):sent] if e.get("rev", 0) != revs.get(e["i"], 0)]
                    for e in upd:
                        revs[e["i"]] = e.get("rev", 0)
                    if batch or upd:
                        for e in batch:
                            revs[e["i"]] = e.get("rev", 0)
                        data = json.dumps(upd + batch, ensure_ascii=False)
                        last_id = (batch[-1]["i"] if batch else sent - 1)
                        self.wfile.write(f"id: {last_id}\nevent: events\ndata: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        sent += len(batch)
                        last_write = time.time()
                    elif time.time() - last_write > 15:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_write = time.time()
                    time.sleep(0.7)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

    return H


def already_running(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == "codex-dashboard"
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Read-only local dashboard for running `codex exec` agents.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CODEX_DASHBOARD_PORT", 8765)))
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"))
    ap.add_argument("--history-hours", type=float, default=24, help="how long finished sessions stay listed")
    ap.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = ap.parse_args()

    url = f"http://localhost:{args.port}/"
    if already_running(args.port):
        print(f"codex-dashboard is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    monitor = Monitor(args.codex_home, args.history_hours)
    t0 = time.time()
    monitor.tick()
    print(f"codex-dashboard: {sum(1 for s in monitor.sessions.values() if s.live)} running / "
          f"{len(monitor.sessions)} recent codex exec sessions (scan {time.time() - t0:.1f}s)", flush=True)
    threading.Thread(target=monitor.run, args=(args.interval,), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(monitor, Transcripts(), args.host, args.port))
    server.daemon_threads = True
    print(f"Serving on {url}  (Ctrl+C to stop; agents are unaffected)", flush=True)
    if not args.no_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        monitor._save_cache()


if __name__ == "__main__":
    main()
