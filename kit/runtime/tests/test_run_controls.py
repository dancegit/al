"""The run controls the dashboard's voice helper approves (adw_modules/launch.py resume/stop,
`adws/dispatch.py --resume/--stop`): resuming a run detached, a person's answer to a waiting run
(agents/<id>/answer.md, ADW_ANSWER_FILE), and stopping a running run with SIGTERM, which the runner
records as `failed`, "stopped by a person", without leaving its agent running. Git is real."""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adw_modules import ir, launch, state as state_module

RUNTIME = Path(__file__).resolve().parents[1]
RUN_CODE = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
            "from adw_modules.runner import main; sys.exit(main(sys.argv[2], Path(sys.argv[3]), sys.argv[4:]))")
TEXT = {"description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}
RUN_ID = "a1b2c3d4"
# waits for a person until it has an answer: exit 75 (declared as waiting), then prints the answer
ASK = ('if [ -n "$ADW_ANSWER_FILE" ]; then echo "answer-file=$ADW_ANSWER_FILE"; cat "$ADW_ANSWER_FILE"; '
       'else echo "no answer yet"; exit 75; fi')


def _ir(agent: dict, steps: list[dict]) -> dict:
    return {
        "agents": [agent],
        "codebase": {"commands": [{"name": "ask", "run": ASK, "timeout": 30, "description": ""}]},
        "commands": [{"name": "work", "template": "work", "modelSet": "base", "output": "text", **TEXT}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27980,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "adws": [{"name": "flow", "agent": agent["id"], "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [], "steps": steps}],
    }


MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
WORK = {"name": "work", "kind": "run", "command": "work", "args": ["$prompt"]}


@pytest.fixture
def layer_dir(make_layer) -> Path:
    return make_layer(_ir(MOCK, [WORK]))


def _state(layer_dir: Path, run_id: str = RUN_ID, **fields) -> state_module.ADWState:
    state = state_module.ADWState(adw_id=run_id, adw=fields.pop("adw", "flow"), prompt="tidy up", **fields)
    state.save(layer_dir)
    return state


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


class Proc:
    pid = 4243


class Spawn:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return Proc()


def _run(layer_dir: Path, *args: str, **popen):
    return subprocess.Popen([sys.executable, "-c", RUN_CODE, str(RUNTIME), "flow", str(layer_dir), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **popen)


def test_resume_waiting_run_spawns_resume(layer_dir):
    _state(layer_dir, status="waiting", pid=_dead_pid(), host=socket.gethostname())
    layer = ir.load(layer_dir)
    spawn = Spawn()
    result = launch.resume(layer, RUN_ID, spawn=spawn)
    assert result["status"] == "resumed" and result["run_id"] == RUN_ID and launch.EXIT["resumed"] == 0
    call = spawn.calls[0]
    assert call["argv"] == ["uv", "run", "--quiet", str(layer_dir / "adws" / "adw_flow.py"), "--resume",
                            "--adw-id", RUN_ID]
    assert call["start_new_session"] is True and call["cwd"] == str(layer_dir)
    assert call["stdin"] == subprocess.DEVNULL and call["stdout"].name == str(layer_dir / "agents" / RUN_ID / "launch.log")
    after = state_module.load(layer_dir, RUN_ID)
    assert (after.status, after.pid) == ("running", Proc.pid)
    assert launch.resume(layer, RUN_ID, spawn=spawn)["status"] == "refused"   # a double click launches it once
    assert len(spawn.calls) == 1
    # a single-command run continues through dispatch.py --command
    _state(layer_dir, "0badcafe", adw="command:work", status="failed")
    assert launch.resume(layer, "0badcafe", spawn=spawn)["status"] == "resumed"
    assert spawn.calls[-1]["argv"] == ["uv", "run", "--quiet", str(layer_dir / "adws" / "dispatch.py"), "--command",
                                       "work", "--resume", "--adw-id", "0badcafe"]
    assert not (layer_dir / "agents" / RUN_ID / "answer.md").exists()


def test_resume_refuses_running_and_passed(layer_dir):
    layer = ir.load(layer_dir)
    spawn = Spawn()
    _state(layer_dir, status="running", pid=_dead_pid(), host=socket.gethostname())
    assert launch.resume(layer, RUN_ID, spawn=spawn)["status"] == "refused"
    _state(layer_dir, status="failed", pid=os.getpid(), host=socket.gethostname())   # its process is still alive
    assert launch.resume(layer, RUN_ID, spawn=spawn)["status"] == "refused"
    _state(layer_dir, status="passed")
    passed = launch.resume(layer, RUN_ID, spawn=spawn)
    assert passed["status"] == "invalid" and "is passed" in passed["error"] and launch.EXIT["invalid"] == 2
    assert launch.resume(layer, "deadbeef", spawn=spawn) == {"status": "invalid", "layer": "test-layer",
                                                             "run_id": "deadbeef", "error": "no run deadbeef in this layer"}
    assert launch.resume(layer, "../../x", spawn=spawn)["status"] == "invalid"
    _state(layer_dir, status="failed")
    answered = launch.resume(layer, RUN_ID, answer="blue", spawn=spawn)
    assert answered["status"] == "invalid" and "only a waiting run takes an answer" in answered["error"]
    _state(layer_dir, status="waiting")
    assert launch.resume(layer, RUN_ID, answer="x" * (8 * 1024 + 1), spawn=spawn)["status"] == "invalid"
    assert launch.resume(layer, RUN_ID, answer="  \n", spawn=spawn)["status"] == "invalid"
    assert not spawn.calls and not (layer_dir / "agents" / RUN_ID / "answer.md").exists()


def test_answer_written_0600_and_exported(make_layer):
    layer_dir = make_layer(_ir(MOCK, [{"name": "ask", "kind": "script", "cmd": "ask", "args": [], "timeout": 0,
                                       "waitingExit": [75]}, WORK]))
    first = _run(layer_dir, "pick a colour", "--adw-id", RUN_ID)
    output, _ = first.communicate(timeout=120)
    assert first.returncode == 75, output
    assert state_module.load(layer_dir, RUN_ID).status == "waiting"
    spawn = Spawn()
    answer = "Blue, please.\nAnd keep it short."
    assert launch.resume(ir.load(layer_dir), RUN_ID, answer=answer, spawn=spawn)["status"] == "resumed"
    path = layer_dir / "agents" / RUN_ID / "answer.md"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text() == answer + "\n"
    assert spawn.calls[0]["argv"][-3:] == ["--resume", "--adw-id", RUN_ID]
    second = _run(layer_dir, *spawn.calls[0]["argv"][-3:])   # what the spawned script runs
    output, _ = second.communicate(timeout=120)
    assert second.returncode == 0, output
    data = json.loads((layer_dir / "agents" / RUN_ID / "state.json").read_text())
    assert data["status"] == "passed"
    assert data["outputs"]["ask"].splitlines() == [f"answer-file={path}", "Blue, please.", "And keep it short."]


def _wait(predicate, seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_stop_signals_the_run_group(make_layer, tmp_path):
    marker = tmp_path / "agent.pid"
    agent = {"id": "slow", "kind": "custom", "status": "stable", "modelBase": "", "modelHeavy": "", "timeout": 300,
             "command": f"echo $$ > {marker}; exec sleep 300"}
    layer_dir = make_layer(_ir(agent, [WORK]))
    process = _run(layer_dir, "take your time", "--adw-id", RUN_ID, start_new_session=True)
    try:
        assert _wait(lambda: marker.is_file() and marker.read_text().strip()), "the agent never started"
        agent_pid = int(marker.read_text())
        state = state_module.load(layer_dir, RUN_ID)
        assert state.status == "running" and state.pid == process.pid and state.host == socket.gethostname()
        layer = ir.load(layer_dir)
        result = launch.stop(layer, RUN_ID)
        assert result["status"] == "stopping" and launch.EXIT["stopping"] == 0
        output, _ = process.communicate(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 1, output
    data = json.loads((layer_dir / "agents" / RUN_ID / "state.json").read_text())
    assert data["status"] == "failed" and data["ended"]
    step = next(s for s in data["steps"] if s["name"] == "work")
    assert step["status"] == "failed" and step["detail"] == "work: stopped by a person"
    assert "run failed: work: stopped by a person" in output
    assert _wait(lambda: not _alive(agent_pid), 10), "the agent was left running"
    assert launch.stop(layer, RUN_ID)["status"] == "invalid"      # not running any more


def test_stop_refuses_other_host(layer_dir):
    layer = ir.load(layer_dir)
    _state(layer_dir, status="running", pid=os.getpid(), host="elsewhere.example")
    assert launch.stop(layer, RUN_ID) == {"status": "invalid", "error": "not running here", "layer": "test-layer",
                                          "run_id": RUN_ID}
    _state(layer_dir, status="running", pid=_dead_pid(), host=socket.gethostname())
    assert launch.stop(layer, RUN_ID)["error"] == "not running here"
    _state(layer_dir, status="waiting", pid=os.getpid(), host=socket.gethostname())
    assert launch.stop(layer, RUN_ID)["error"] == "not running here"
    assert launch.stop(layer, "nope")["status"] == "invalid"


def test_dispatch_resume_and_stop_json(layer_dir):
    adws = layer_dir / "adws"
    shutil.copytree(RUNTIME / "adw_modules", adws / "adw_modules", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(RUNTIME / "dispatch.py", adws / "dispatch.py")

    def call(*args: str, stdin: bytes = b""):
        done = subprocess.run([sys.executable, str(adws / "dispatch.py"), *args, "--json"], input=stdin,
                              capture_output=True, timeout=60, cwd=str(layer_dir))
        lines = done.stdout.decode().splitlines()
        assert len(lines) == 1, done.stdout + done.stderr
        return done.returncode, json.loads(lines[0])

    _state(layer_dir, status="passed")
    assert call("--stop", RUN_ID) == (2, {"status": "invalid", "error": "not running here", "layer": "test-layer",
                                          "run_id": RUN_ID})
    code, result = call("--resume", RUN_ID, "--answer", stdin=b"yes")
    assert code == 2 and "is passed" in result["error"]
    code, result = call("--resume", RUN_ID, "--answer", stdin=b"x" * (8 * 1024 + 1))
    assert code == 2 and result["error"] == "the answer is over 8192 bytes"
    _state(layer_dir, status="running", pid=_dead_pid(), host=socket.gethostname())
    assert call("--resume", RUN_ID)[0] == 3
    assert call("--answer")[1]["error"] == "--answer goes with --resume"
