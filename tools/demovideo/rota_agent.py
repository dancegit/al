#!/usr/bin/env python3
"""A scripted stand-in for the build step of the demo video's Rota layer: no LLM, no cost.

    python3 rota_agent.py WORKTREE [PAUSE]

It is an `(agent custom …)` of tools/demovideo/demo_layer.rkt.in, written like
kit/dashboard/demo/replay_agent.py: it reads the rendered prompt on stdin, works in the run's worktree
(WORKTREE) and writes Claude Code stream-json events to stdout, one line at a time with PAUSE seconds
(default 0.9) between them, so the dashboard has a transcript that grows while the step runs.

The change it reports is real: it adds the 11-hour rest rule to rules.py (a worker needs 11 hours between
the end of one shift and the start of the next, either way round) and two tests to tests/test_rules.py,
then really runs tests/test_rules.py with Flask and pytest resolved by `uv run --with` (the pins of the
app's requirements.txt). The edits are anchored on exact text of tests/rotademo/app: when the app has
drifted, an anchor is missing and the step fails instead of making a wrong edit.

Standard library only; importing it runs nothing (tests/tools/test_demo_video.py calls `apply`).
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Rota's Python: Flask and pytest with the pins of tests/rotademo/app/requirements.txt, no manifest
PYTHON = ("uv", "run", "--quiet", "--no-project", "--with", "flask>=3.1,<4", "--with", "pytest>=8,<9", "python")
PYTEST = shlex.join(PYTHON + ("-m", "pytest", "-q"))

_LAST_TEST = '''def test_the_shift_itself_is_ignored():
    assert rules.assignment_problem(nurse(), DAY, [DAY], TODAY) is None
'''

# (path, old, new): each old text is exact text of tests/rotademo/app, replaced once
EDITS: list[tuple[str, str, str]] = [
    ("rules.py",
     "from datetime import date, datetime\n",
     "from datetime import date, datetime, timedelta\n"),
    ("rules.py",
     'ALREADY_WORKING = "Already working at that time"\n',
     'ALREADY_WORKING = "Already working at that time"\n'
     'NEEDS_REST = "Needs 11 hours rest"\n'
     "REST = timedelta(hours=11)\n"),
    ("rules.py",
     "            return ALREADY_WORKING\n    return None",
     "            return ALREADY_WORKING\n"
     "    for other in workers_shifts:\n"
     "        if other.id != shift.id and too_little_rest(other, shift):\n"
     "            return NEEDS_REST\n"
     "    return None\n"
     "\n"
     "\n"
     "def too_little_rest(first: Shift, second: Shift) -> bool:\n"
     '    """Fewer than 11 hours between the end of one shift and the start of the other, either way round."""\n'
     "    return second.starts_at - first.ends_at < REST and first.starts_at - second.ends_at < REST"),
    ("tests/test_rules.py",
     _LAST_TEST,
     _LAST_TEST + '''

def test_less_than_11_hours_rest_is_refused():
    before = shift(14, "2026-03-02T13:30", "2026-03-02T21:30")
    after = shift(15, "2026-03-04T05:30", "2026-03-04T13:30")
    assert rules.assignment_problem(nurse(), DAY, [before], TODAY) == rules.NEEDS_REST
    assert rules.assignment_problem(nurse(), DAY, [after], TODAY) == rules.NEEDS_REST


def test_exactly_11_hours_rest_is_allowed():
    before = shift(16, "2026-03-02T12:30", "2026-03-02T20:30")
    assert rules.assignment_problem(nurse(), DAY, [before], TODAY) is None
'''),
]


def edit(cwd: Path, path: str, old: str, new: str) -> Path:
    """One edit of EDITS in the worktree `cwd`; ValueError when its text is not there."""
    target = cwd / path
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    if old not in text:
        raise ValueError(f"{path}: the text to replace is not there")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target


def apply(cwd: Path) -> list[str]:
    """Every edit of EDITS in `cwd`; the changed paths in order, each once."""
    changed: list[str] = []
    for path, old, new in EDITS:
        edit(Path(cwd), path, old, new)
        if path not in changed:
            changed.append(path)
    return changed


def main() -> None:
    pause = float(sys.argv[2]) if len(sys.argv) > 2 else 0.9
    cwd = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    prompt = sys.stdin.read()
    session, model = "replay-0000", "scripted stand-in (no LLM)"
    counter = {"tool": 0, "msg": 0}
    started = time.time()

    def emit(event: dict, wait: float = pause) -> None:
        if event.get("type") in {"assistant", "user"}:
            event["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        print(json.dumps(event), flush=True)
        time.sleep(wait)

    def say(block: dict) -> None:
        counter["msg"] += 1
        emit({"type": "assistant", "message": {"id": f"msg_{counter['msg']}", "model": model, "role": "assistant",
              "content": [block], "usage": {"input_tokens": 0, "output_tokens": 0}}, "session_id": session})

    def result(text: str, error: bool) -> None:
        emit({"type": "result", "subtype": "error_during_execution" if error else "success", "is_error": error,
              "result": text, "num_turns": counter["tool"] + 2, "duration_ms": int((time.time() - started) * 1000),
              "total_cost_usd": 0.0,
              "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
              "session_id": session}, wait=0)

    def tool(name: str, data: dict, run) -> bool:
        counter["tool"] += 1
        tid = f"toolu_{counter['tool']:03d}"
        say({"type": "tool_use", "id": tid, "name": name, "input": data})
        content, error, meta = run()
        emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                                         "content": content, "is_error": error}]},
              "tool_use_result": meta, "session_id": session}, wait=pause * 0.5)
        return not error

    def read(path: Path):
        def run():
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            return "\n".join(f"{i}\t{line}" for i, line in enumerate(text.splitlines(), 1)), False, {"type": "text"}
        return run

    emit({"type": "system", "subtype": "init", "cwd": str(cwd), "session_id": session, "model": model,
          "permissionMode": "acceptEdits", "tools": ["Read", "Edit", "Write", "Bash", "Grep"]}, wait=0.3)
    say({"type": "thinking", "thinking": ""})
    say({"type": "text", "text": "I'll read the plan and the rules, add the rest rule with its tests, and run them."})

    plan_match = re.search(r"(/\S+/specs/\S+\.md)", prompt)
    if plan_match:
        tool("Read", {"file_path": plan_match.group(1)}, read(Path(plan_match.group(1))))
    tool("Read", {"file_path": str(cwd / "rules.py")}, read(cwd / "rules.py"))
    line = next((n for n, text in enumerate((cwd / "rules.py").read_text(encoding="utf-8").splitlines(), 1)
                 if text.startswith("def assignment_problem")), 0) if (cwd / "rules.py").is_file() else 0
    tool("Grep", {"pattern": "def assignment_problem", "path": str(cwd)},
         lambda: (f"rules.py:{line}:def assignment_problem(worker: Worker, shift: Shift, workers_shifts: list[Shift], "
                  "today: date) -> str | None:" if line else "No matches found", False, {"mode": "content", "numLines": 1 if line else 0}))
    say({"type": "thinking", "thinking": ""})

    for path, old, new in EDITS:
        def run_edit(path=path, old=old, new=new):
            try:
                target = edit(cwd, path, old, new)
            except ValueError as error:
                return f"<tool_use_error>{error}</tool_use_error>", True, {"filePath": str(cwd / path)}
            return f"The file {target} has been updated successfully.", False, {"filePath": str(target)}
        if not tool("Edit", {"file_path": str(cwd / path), "old_string": old, "new_string": new}, run_edit):
            result(f"{path}: the text to replace is not there; the app has changed, so nothing more is edited.", True)
            sys.exit(1)

    def run_tests():
        try:
            done = subprocess.run(list(PYTHON) + ["-m", "pytest", "-q", "tests/test_rules.py"], cwd=cwd,
                                  capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as error:
            return str(error), True, {"stdout": "", "stderr": str(error), "interrupted": True}
        output = (done.stdout + done.stderr).strip()
        return output, done.returncode != 0, {"stdout": done.stdout, "stderr": done.stderr, "interrupted": False}

    tool("Bash", {"command": PYTEST + " tests/test_rules.py", "description": "Run the rules tests"}, run_tests)
    closing = "Added the 11-hour rest rule to rules.py and two tests to tests/test_rules.py."
    say({"type": "text", "text": closing})
    result(closing, False)


if __name__ == "__main__":
    main()
