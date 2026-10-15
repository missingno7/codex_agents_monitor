"""Stand-in for `codex exec --json` in tests: replays a scripted event stream chosen by FAKE_SCENARIO.

It records how it was called (argv, cwd, prompt) to FAKE_RECORD so tests can check what cx asked for."""
import json
import os
import subprocess
import sys
import time


def emit(*events):
    for ev in events:
        sys.stdout.write(ev if isinstance(ev, str) else json.dumps(ev))
        sys.stdout.write("\n")
    sys.stdout.flush()


def cmd(i, exit_code):
    item = {"id": f"c{i}", "type": "command_execution", "command": "x", "aggregated_output": ""}
    return ({"type": "item.started", "item": dict(item, status="in_progress", exit_code=None)},
            {"type": "item.completed",
             "item": dict(item, status="completed" if exit_code == 0 else "failed", exit_code=exit_code)})


prompt = sys.stdin.read()
with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt}, f)

scenario = os.environ.get("FAKE_SCENARIO", "ok")
started = {"type": "thread.started", "thread_id": os.environ.get("FAKE_SID", "sid-1")}
answer = {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "the answer"}}
done = {"type": "turn.completed", "usage": {}}

if scenario == "ok":
    emit(started, {"type": "turn.started"}, *cmd(0, 0), answer, done)
elif scenario == "turn_failed":
    emit(started, {"type": "turn.started"},
         {"type": "turn.failed", "error": {"message": json.dumps({"error": {"message": "usage limit reached"}})}})
    sys.exit(1)
elif scenario == "truncated":  # stream ends before the turn does, yet the process exits 0
    emit(started, {"type": "turn.started"}, answer)
elif scenario == "junk":
    emit("not json", "[1, 2]", '"text"', started, {"type": "item.completed", "item": "not a dict"}, answer, done)
elif scenario == "cmds_fail":
    emit(started, *cmd(0, 1), *cmd(1, 1), answer, done)
elif scenario == "hang_after_turn":
    emit(started, answer, done)
    time.sleep(120)
elif scenario == "child_then_hang":  # a grandchild the worker leaves running, then no progress
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKE_CHILD_PID"], "w") as f:
        f.write(str(child.pid))
    emit(started)
    time.sleep(120)
