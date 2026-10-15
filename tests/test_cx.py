"""Regression tests for cx.py against a fake Codex (no model calls).  Run:  python -m unittest discover tests

Each test runs cx as a real subprocess, so the job object, threads and pipes behave as in production.
HOME/USERPROFILE and CODEX_HOME point into a temporary directory, so nothing touches the real ~/.codex*."""
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(REPO, "tests", "fake_codex.py")


def unpackaged_python():
    """A Python that is not the Microsoft Store one.  The containment test needs it: a Store "App Execution
    Alias" started from a non-packaged process (here the cmd.exe shim) is activated outside every job object,
    which is the known escape documented in the README, not something cx can contain."""
    candidates = [os.environ.get("CX_TEST_PYTHON")]
    candidates += [os.path.join(d, "python.exe") for d in os.environ.get("PATH", "").split(os.pathsep)]
    candidates += [r"C:\msys64\mingw64\bin\python.exe", r"C:\msys64\ucrt64\bin\python.exe"]
    for c in candidates:
        if c and os.path.isfile(c) and "windowsapps" not in c.lower():
            return c
    return None


def alive(pid):
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


@unittest.skipUnless(os.name == "nt", "cx's process handling is Windows-specific")
class CxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.home = os.path.join(self.dir, "home")
        self.codex_home = os.path.join(self.dir, "codex_home")
        self.work = os.path.join(self.dir, "work")
        for d in (self.home, self.codex_home, self.work):
            os.makedirs(d)
        shim = self.shim(sys.executable)
        self.record = os.path.join(self.dir, "record.json")
        self.env = dict(os.environ, CODEX_BIN=shim, CODEX_HOME=self.codex_home, USERPROFILE=self.home,
                        HOME=self.home, FAKE_RECORD=self.record, PYTHONIOENCODING="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def shim(self, python):
        """A codex.cmd that runs the fake with the given interpreter (cx finds it through CODEX_BIN)."""
        path = os.path.join(self.dir, f"codex{abs(hash(python))}.cmd")
        with open(path, "w") as f:
            f.write(f'@"{python}" "{FAKE}" %*\n')
        return path

    def cx(self, *args, scenario="ok", cwd=None, grace=None, **env):
        """Run `cx run ARGS` to completion; returns (exit code, output lines)."""
        code = f"import sys; sys.path.insert(0, {REPO!r}); import cx; sys.argv = ['cx', 'run', *{list(args)!r}]"
        code += f"; cx.EXIT_GRACE = {grace}" if grace is not None else ""
        p = subprocess.run([sys.executable, "-c", code + "; cx.main()"], input=b"do the task", capture_output=True,
                           cwd=cwd or self.work, env={**self.env, "FAKE_SCENARIO": scenario, **env}, timeout=90)
        return p.returncode, p.stdout.decode("utf-8").splitlines()

    def called_with(self):
        with open(self.record, encoding="utf-8") as f:
            return json.load(f)

    # -- outcome ---------------------------------------------------------------------------------------
    def test_success_prints_start_line_first_then_header_and_answer(self):
        code, out = self.cx("-n", "w1", "-C", self.work, "-m", "m1", "-e", "high")
        self.assertEqual(code, 0)
        self.assertEqual(out[0], "[cx] w1 · started · m1/high · session sid-1")
        self.assertTrue(out[1].startswith("[cx] w1 · DONE · "), out)
        self.assertIn("session sid-1", out[1])
        self.assertNotIn("commands succeeded", out[1])
        self.assertEqual(out[2], "the answer")
        rec = self.called_with()
        self.assertEqual(rec["prompt"], "do the task")
        self.assertIn("m1", rec["argv"])
        with open(os.path.join(self.home, ".codex-dashboard", "runs", "sid-1.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["name"], "w1")

    def test_failed_turn_reports_the_api_error(self):
        code, out = self.cx("-n", "w2", scenario="turn_failed")
        self.assertEqual(code, 1)
        self.assertTrue(out[1].startswith("[cx] w2 · FAILED"), out)
        self.assertIn("error: usage limit reached", out)

    def test_stream_ending_without_turn_completed_is_not_done(self):
        code, out = self.cx("-n", "w3", scenario="truncated")
        self.assertEqual(code, 1)
        self.assertTrue(out[1].startswith("[cx] w3 · FAILED (exit 0)"), out)
        self.assertTrue(any("before finishing its turn" in line for line in out), out)
        self.assertIn("last message: the answer", out)

    def test_malformed_lines_do_not_lose_later_events(self):
        code, out = self.cx("-n", "w4", scenario="junk")
        self.assertEqual(code, 0, out)
        self.assertIn("the answer", out)

    def test_worker_whose_commands_all_failed_is_flagged(self):
        code, out = self.cx("-n", "w5", scenario="cmds_fail")
        self.assertEqual(code, 0)  # the turn completed; judging the work is the supervisor's job
        self.assertIn("· 0 of 2 commands succeeded", out[1])

    def test_codex_hanging_after_its_turn_is_stopped_and_still_done(self):
        python = unpackaged_python()
        if not python:
            self.skipTest("needs a non-Store Python for the fake Codex (set CX_TEST_PYTHON)")
        code, out = self.cx("-n", "w6", scenario="hang_after_turn", grace=1, CODEX_BIN=self.shim(python))
        self.assertEqual(code, 0, out)
        self.assertIn("· DONE", out[1])
        self.assertIn("did not exit 1s after finishing; stopped it", out[1])

    # -- resume ------------------------------------------------------------------------------------------
    def make_thread_index(self, sid, model, effort, cwd):
        con = sqlite3.connect(os.path.join(self.codex_home, "state_5.sqlite"))
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model TEXT, reasoning_effort TEXT, cwd TEXT)")
        con.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (sid, model, effort, "\\\\?\\" + cwd))
        con.commit()
        con.close()

    def test_resume_uses_the_sessions_directory_model_and_effort(self):
        self.make_thread_index("sid-r", "m-orig", "xhigh", self.work)
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.makedirs(elsewhere)
        code, out = self.cx("-n", "r1", "--resume", "sid-r", cwd=elsewhere, FAKE_SID="sid-r")
        self.assertEqual(code, 0, out)
        rec = self.called_with()
        self.assertEqual(os.path.normcase(rec["cwd"]), os.path.normcase(self.work))
        self.assertIn("m-orig", rec["argv"])
        self.assertTrue(any("xhigh" in a for a in rec["argv"]), rec["argv"])
        self.assertIn("sid-r", rec["argv"])
        self.assertEqual(out[0], "[cx] r1 · started · m-orig/xhigh · session sid-r")

    def test_resume_falls_back_to_the_rollout(self):
        day = os.path.join(self.codex_home, "sessions", "2026", "10", "15")
        os.makedirs(day)
        with open(os.path.join(day, "rollout-2026-10-15T00-00-00-sid-f.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {}}) + "\n")
            f.write(json.dumps({"type": "turn_context",
                                "payload": {"model": "m-roll", "effort": "low", "cwd": self.work}}) + "\n")
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.makedirs(elsewhere)
        code, out = self.cx("-n", "r2", "--resume", "sid-f", cwd=elsewhere, FAKE_SID="sid-f")
        self.assertEqual(code, 0, out)
        rec = self.called_with()
        self.assertEqual(os.path.normcase(rec["cwd"]), os.path.normcase(self.work))
        self.assertIn("m-roll", rec["argv"])

    def test_resume_without_metadata_runs_where_started_and_explicit_flags_win(self):
        code, out = self.cx("-n", "r3", "--resume", "sid-x", "-m", "m-explicit", FAKE_SID="sid-x")
        self.assertEqual(code, 0, out)
        rec = self.called_with()
        self.assertEqual(os.path.normcase(rec["cwd"]), os.path.normcase(self.work))
        self.assertIn("m-explicit", rec["argv"])

    def test_missing_directory_fails_before_starting(self):
        code, out = self.cx("-n", "w7", "-C", os.path.join(self.dir, "nope"))
        self.assertEqual(code, 1)
        self.assertIn("working directory does not exist", "\n".join(out))

    # -- cancellation -----------------------------------------------------------------------------------
    def test_killing_cx_kills_everything_the_worker_started_and_keeps_the_session_id(self):
        python = unpackaged_python()
        if not python:
            self.skipTest("needs a non-Store Python for the fake Codex (set CX_TEST_PYTHON)")
        child_pid_file = os.path.join(self.dir, "child.pid")
        code = f"import sys; sys.path.insert(0, {REPO!r}); import cx; sys.argv = ['cx', 'run', '-n', 'k1']; cx.main()"
        p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             cwd=self.work, env=dict(self.env, CODEX_BIN=self.shim(python),
                                                     FAKE_SCENARIO="child_then_hang", FAKE_CHILD_PID=child_pid_file))
        p.stdin.write(b"task")
        p.stdin.close()
        first = p.stdout.readline().decode("utf-8").strip()
        self.assertEqual(first, "[cx] k1 · started · session sid-1")
        with open(child_pid_file) as f:
            child = int(f.read())
        self.assertTrue(alive(child))
        p.kill()  # what TaskStop does to cx
        p.wait()
        p.stdout.close()
        deadline = time.time() + 10
        while alive(child) and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(alive(child), "the worker's grandchild outlived cx")


if __name__ == "__main__":
    unittest.main()
