#!/usr/bin/env python3
"""The scripted stand-in for the build of the demo video's testbed story: no LLM, no cost.

It is an `(agent custom …)` like kit/dashboard/demo/replay_agent.py: it reads the rendered prompt on
stdin, works in the run's worktree and writes Claude Code stream-json events to stdout, one line at a
time with pauses, so the dashboard has a transcript that grows while the step runs.

    flask_replay.py CWD PAUSE CONFIG_JSON        CONFIG_JSON holds {"test": "<command>"}

What it reports is real: in a checkout of the pinned flask it makes `get_debug_flag()` in
src/flask/helpers.py treat FLASK_DEBUG=off as off (today that value turns debug mode on), adds a test
to tests/test_helpers.py and runs the test command. When helpers.py is not the pinned one it changes
nothing and exits 1.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OLD = '    return bool(val and val.lower() not in {"0", "false", "no"})'
NEW = '    return bool(val and val.lower() not in {"0", "false", "no", "off"})'
IMPORT = "from flask.helpers import get_debug_flag"
TEST = '''

def test_get_debug_flag_off(monkeypatch):
    monkeypatch.setenv("FLASK_DEBUG", "off")
    assert get_debug_flag() is False
'''
MODEL = "flask-replay (no LLM)"
SESSION = "replay-0000"


def main(argv: list[str]) -> int:
    cwd = Path(argv[1] if len(argv) > 1 else ".").resolve()
    pause = float(argv[2]) if len(argv) > 2 else 1.0
    config = json.loads(Path(argv[3]).read_text(encoding="utf-8")) if len(argv) > 3 else {}
    command = str(config.get("test") or "python3 -m pytest -q tests/test_helpers.py")
    sys.stdin.read()                                # the rendered prompt: this script's steps are fixed
    helpers, tests = cwd / "src" / "flask" / "helpers.py", cwd / "tests" / "test_helpers.py"
    try:
        pinned = helpers.read_text(encoding="utf-8").count(OLD) == 1 and IMPORT in tests.read_text(encoding="utf-8")
    except OSError:
        pinned = False
    if not pinned:
        print("flask_replay: src/flask/helpers.py is not the pinned get_debug_flag; the pinned flask changed", file=sys.stderr)
        return 1
    counter = {"tool": 0, "msg": 0}

    def emit(event: dict, wait: float = pause) -> None:
        if event.get("type") in {"assistant", "user"}:
            event["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        print(json.dumps(event), flush=True)
        time.sleep(wait)

    def say(block: dict) -> None:
        counter["msg"] += 1
        emit({"type": "assistant", "message": {"id": f"msg_{counter['msg']}", "model": MODEL, "role": "assistant",
              "content": [block], "usage": {"input_tokens": 0, "output_tokens": 0}}, "session_id": SESSION})

    def tool(name: str, data: dict, run) -> None:
        counter["tool"] += 1
        tid = f"toolu_{counter['tool']:03d}"
        say({"type": "tool_use", "id": tid, "name": name, "input": data})
        result, error, meta = run()
        emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result,
                                                                        "is_error": error}]},
              "tool_use_result": meta, "session_id": SESSION}, wait=pause * 0.5)

    def read(path: Path):
        def run():
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            return "\n".join(f"{i}\t{line}" for i, line in enumerate(text.splitlines(), 1)), False, {"type": "text"}
        return run

    def grep():
        found = []
        for path in sorted((cwd / "src" / "flask").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "FLASK_DEBUG" in line:
                    found.append(f"{path.relative_to(cwd).as_posix()}:{number}:{line}")
        return "\n".join(found), False, {"mode": "content", "numLines": len(found)}

    def edit_helper():
        helpers.write_text(helpers.read_text(encoding="utf-8").replace(OLD, NEW, 1), encoding="utf-8")
        return f"The file {helpers} has been updated successfully.", False, {"filePath": str(helpers)}

    def add_test():
        text = tests.read_text(encoding="utf-8")
        tests.write_text(text.rstrip("\n") + "\n" + TEST, encoding="utf-8")
        return f"The file {tests} has been updated successfully.", False, {"filePath": str(tests)}

    def run_tests():
        done = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=540)
        lines = (done.stdout + done.stderr).strip().splitlines()[-40:]
        return ("\n".join(lines) + f"\n(exit code {done.returncode})", done.returncode != 0,
                {"stdout": done.stdout, "stderr": done.stderr, "interrupted": False})

    started = time.time()
    emit({"type": "system", "subtype": "init", "cwd": str(cwd), "session_id": SESSION, "model": MODEL,
          "permissionMode": "acceptEdits", "tools": ["Read", "Edit", "Bash", "Grep"]}, wait=0.3 if pause else 0)
    say({"type": "text", "text": "I'll find where FLASK_DEBUG is read, make `off` mean off, add a test and run the tests."})
    tool("Grep", {"pattern": "FLASK_DEBUG", "path": str(cwd / "src" / "flask")}, grep)
    tool("Read", {"file_path": str(helpers)}, read(helpers))
    tool("Edit", {"file_path": str(helpers), "old_string": OLD, "new_string": NEW}, edit_helper)
    tool("Edit", {"file_path": str(tests), "old_string": "", "new_string": TEST.lstrip("\n")}, add_test)
    tool("Bash", {"command": command, "description": "Run Flask's helper tests"}, run_tests)
    summary = "get_debug_flag() now treats FLASK_DEBUG=off as off; tests/test_helpers.py has a test for it."
    say({"type": "text", "text": summary})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": summary,
          "num_turns": counter["tool"] + 2, "duration_ms": int((time.time() - started) * 1000), "total_cost_usd": 0.0,
          "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
          "session_id": SESSION}, wait=0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
