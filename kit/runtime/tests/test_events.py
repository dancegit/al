"""Run events (agents/<id>/events.jsonl), streaming check logs, liveness, timestamped run.log,
fan-out parent and the agent recorded on each step record."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from adw_modules import events, slots, state as state_module, steps
from adw_modules.runner import main

from test_runner_e2e import _layer, _script

ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00$")


def _events(layer: Path, adw_id: str) -> list[dict]:
    return events.read(layer / "agents" / adw_id / "events.jsonl")


# ---------------------------------------------------------------- the format

def test_recorder_appends_one_line_per_event_with_increasing_seq_and_resumes(tmp_path):
    with events.recording(tmp_path, "aaaa0001"):
        events.emit("run.start", adw="demo")
        events.emit("step.start", step="plan", kind="run", attempt=1)
    path = tmp_path / "agents/aaaa0001/events.jsonl"
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 3
    first, second = (json.loads(line) for line in lines[:2])
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["type"] == "run.start" and first["adw"] == "demo" and ISO.match(first["ts"])
    with events.recording(tmp_path, "aaaa0001"):   # a resumed run continues the count
        events.emit("run.start", adw="demo", resumed=True)
    assert [e["seq"] for e in events.read(path)] == [1, 2, 3]


def test_two_recorders_on_one_file_never_share_a_seq(tmp_path):
    # the run's process and the stand-in's loop (or a second process) write one events.jsonl
    path = tmp_path / "agents/aaaa0003/events.jsonl"
    first, second = events.Recorder(path), events.Recorder(path)
    for index in range(3):
        first.emit("step.start", {"step": f"a{index}"})
        second.emit("decision.skipped", {"who": "stand-in", "index": index})

    def work(recorder: events.Recorder, name: str) -> None:
        for index in range(50):
            recorder.emit("script.event", {"by": name, "index": index})
    threads = [threading.Thread(target=work, args=(recorder, f"{n}-{i}"))
               for n, recorder in enumerate((first, second)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    seqs = [e["seq"] for e in events.read(path)]
    assert seqs == list(range(1, 6 + 4 * 50 + 1))


def test_emit_outside_a_run_is_a_no_op_and_a_failed_write_never_raises(tmp_path, capsys):
    assert events.emit("run.start") is None
    (tmp_path / "agents/aaaa0002/events.jsonl").mkdir(parents=True)   # cannot be written
    with events.recording(tmp_path, "aaaa0002"):
        assert events.emit("run.start") is None
        assert events.emit("run.end") is None
    assert capsys.readouterr().err.count("cannot write") == 1


def test_reader_skips_a_torn_last_line_and_foreign_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"ts":"x","seq":1,"type":"run.start"}\nnot json\n[1]\n{"ts":"x","seq":2,"type":"ru', encoding="utf-8")
    assert [e["seq"] for e in events.read(path)] == [1]


def test_concurrent_emits_never_interleave_inside_a_line(tmp_path):
    with events.recording(tmp_path, "aaaa0003"):
        threads = [threading.Thread(target=lambda: [events.emit("slot.acquire", who="x" * 300) for _ in range(50)])
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    found = events.read(tmp_path / "agents/aaaa0003/events.jsonl")
    assert len(found) == 200 and sorted(e["seq"] for e in found) == list(range(1, 201))


# ---------------------------------------------------------------- a whole run

def test_a_mock_run_records_every_span_in_order(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, keep="on-failure", adw_id="ee000001")
    _script(layer, "build", {"files": {"test.sh": "#!/bin/sh\necho failing\nexit 1\n"}})
    _script(layer, "test-resolve-1", {"files": {"test.sh": "#!/bin/sh\necho ok\nexit 0\n"}})
    assert main("demo", layer, ["Fix tests", "--adw-id", "ee000001"]) == 0
    found = _events(layer, "ee000001")
    types = [e["type"] for e in found]
    assert [e["seq"] for e in found] == list(range(1, len(found) + 1))
    assert all(ISO.match(e["ts"]) for e in found)
    assert types[0] == "run.start" and types[-1] == "run.end"
    start = found[0]
    assert start["adw"] == "demo" and start["pid"] == os.getpid() and start["host"] == socket.gethostname()
    assert start["fanout_parent"] is None and start["resumed"] is False
    assert found[-1]["status"] == "passed" and found[-1]["exit"] == 0
    # runs slot held for the whole run
    assert types[1:3] == ["slot.acquire", "step.start"] and found[1]["pool"] == "runs"
    assert "slot.release" in types[-3:]
    # every step starts and ends, in ADW order
    step_events = [(e["type"], e["step"]) for e in found if e["type"] in {"step.start", "step.end"}]
    assert step_events == [(t, s) for s in ("plan", "build", "test", "review", "ship") for t in ("step.start", "step.end")]
    assert all(e["status"] == "passed" and e["seconds"] >= 0 for e in found if e["type"] == "step.end")
    # agents: kind, model, owner
    agent_starts = [e for e in found if e["type"] == "agent.start"]
    assert [e["step"] for e in agent_starts] == ["plan", "build", "test-resolve-1", "review-1"]
    assert {e["agent_kind"] for e in agent_starts} == {"mock"} and {e["agent"] for e in agent_starts} == {"mock"}
    assert [e["owner"] for e in agent_starts] == ["plan", "build", "test", "review"]
    assert all(e["status"] == "passed" for e in found if e["type"] == "agent.end")
    # check attempts: start, end with exit and the log, then the resolve agent in between
    checks = [e for e in found if e["type"] in {"check.start", "check.end"}]
    assert [(e["type"], e["attempt"], e.get("exit")) for e in checks if e["phase"] == "check"] == [
        ("check.start", 1, None), ("check.end", 1, 1), ("check.start", 2, None), ("check.end", 2, 0)]
    assert checks[0]["log"] == "agents/ee000001/checks/test.1.log" and checks[0]["cmd"] == "test"
    resolve_at = types.index("agent.start", types.index("check.end"))
    assert found[resolve_at]["step"] == "test-resolve-1"
    # the gate does not run the cmd again: it passed in the check step on exactly the tree that ships
    assert [e for e in checks if e["phase"] == "gate"] == []
    (reused,) = [e for e in found if e["type"] == "check.decision"]
    assert (reused["phase"], reused["step"], reused["cmd"], reused["outcome"]) == ("gate", "gate", "test", "reused")
    assert (reused["from_step"], reused["from_phase"]) == ("test", "check") and len(reused["tree"]) == 40
    assert reused["log"] == "agents/ee000001/checks/gate-test.log"
    gate_log = (layer / reused["log"]).read_text()
    assert gate_log.startswith(f"al: check test not run: reused from test, same tree {reused['tree'][:8]}\n")
    assert "agents/ee000001/checks/test.2.log" in gate_log
    review = next(e for e in found if e["type"] == "review.round")
    assert review["round"] == 1 and review["success"] is True and review["issues"] == {
        "blocker": 0, "tech_debt": 0, "skippable": 0}
    gate = next(e for e in found if e["type"] == "gate.decision")
    assert gate["action"] == "merge" and gate["reasons"] == []
    ship = next(e for e in found if e["type"] == "ship.result")
    assert ship["action"] == "merge" and ship["pr_url"] == ""
    assert types.index("gate.decision") < types.index("ship.result") < types.index("run.end")


def test_failed_step_and_handoff_are_recorded(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, keep="on-failure", adw_id="ee000002", steps=[
        {"name": "build", "kind": "run", "command": "build"},
        {"name": "test", "kind": "check", "cmd": "test", "retries": 0},
        {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    _script(layer, "build", {"files": {"test.sh": "#!/bin/sh\nexit 3\n"}})
    assert main("demo", layer, ["Break it", "--adw-id", "ee000002"]) == 1
    found = _events(layer, "ee000002")
    failed = next(e for e in found if e["type"] == "step.end" and e["step"] == "test")
    assert failed["status"] == "failed" and "exited 3" in failed["detail"]
    handoff = next(e for e in found if e["type"] == "handoff")
    assert handoff["status"] == "opened" and handoff["stopped_at"] == "test"
    assert handoff["target"] == "agents/ee000002/PR.md"
    assert found[-1]["type"] == "run.end" and found[-1]["status"] == "failed" and found[-1]["exit"] == 1


def test_skipped_step_is_recorded(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, adw_id="ee000003", steps=[
        {"name": "classify", "kind": "run", "command": "classify"},
        {"name": "plan-bug", "kind": "run", "command": "plan", "when": "class-bug"}])
    _script(layer, "classify", {"output": '{"class":"chore"}'})
    assert main("demo", layer, ["Route", "--adw-id", "ee000003"]) == 0
    skipped = next(e for e in _events(layer, "ee000003") if e["type"] == "step.end" and e["step"] == "plan-bug")
    assert skipped["status"] == "skipped" and "class=chore" in skipped["detail"]


def test_an_unwritable_events_file_never_fails_the_run(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, adw_id="ee000004")
    (layer / "agents/ee000004/events.jsonl").mkdir(parents=True)
    assert main("demo", layer, ["Still works", "--adw-id", "ee000004"]) == 0


# ---------------------------------------------------------------- step records, run.log, state

def test_step_records_name_the_agent_and_model_that_ran(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, adw_id="ee000005")
    ir_path = layer / "agentic-layer.json"
    ir = json.loads(ir_path.read_text())
    ir["agents"].append({"id": "cheap", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20,
                         "modelBase": "small-1", "modelHeavy": "big-1"})
    ir["adws"][0]["steps"][0]["agent"] = "cheap"
    ir_path.write_text(json.dumps(ir))
    assert main("demo", layer, ["Agents", "--adw-id", "ee000005"]) == 0
    records = {r["name"]: r for r in json.loads((layer / "agents/ee000005/state.json").read_text())["steps"]}
    assert (records["plan"]["agent"], records["plan"]["agent_kind"], records["plan"]["model"]) == ("cheap", "mock", "small-1")
    assert (records["build"]["agent"], records["build"]["agent_kind"]) == ("mock", "mock")
    assert records["review"]["agent"] == "mock"      # the reviewers' agent
    assert "agent" not in records["test"]            # no resolve agent was needed


def test_run_log_lines_carry_a_utc_timestamp_before_the_usual_prefix(make_layer, app_repo, capsys):
    layer = _layer(make_layer, app_repo, adw_id="ee000006")
    assert main("demo", layer, ["Stamped", "--adw-id", "ee000006"]) == 0
    lines = (layer / "agents/ee000006/run.log").read_text().splitlines()
    stamped = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) (.*)$")
    assert lines and all(stamped.match(line) for line in lines), lines[:3]
    bodies = [stamped.match(line).group(2) for line in lines]
    assert bodies[0] == "[adw ee000006] demo start: class=chore dry_run=False"
    assert "[adw ee000006] step plan (run) start" in bodies
    assert "=== ADW summary ===" in bodies
    terminal = capsys.readouterr().out.splitlines()   # the terminal (and anything piping it) is unchanged
    assert terminal[0] == "[adw ee000006] demo start: class=chore dry_run=False"


def test_state_records_pid_host_and_a_heartbeat_that_stops_with_the_run(make_layer, app_repo, monkeypatch):
    monkeypatch.setattr(state_module, "HEARTBEAT_SECONDS", 0.05)
    layer = _layer(make_layer, app_repo, adw_id="ee000007", steps=[
        {"name": "wait", "kind": "check", "cmd": "slow"}],
        commands=[{"name": "slow", "run": "sleep 0.6", "timeout": 10}])
    path = layer / "agents/ee000007/state.json"
    beats: list[str] = []

    def watch() -> None:
        deadline = time.time() + 20
        while time.time() < deadline and len(set(beats)) < 3:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if data.get("status") == "running" and data.get("heartbeat"):
                beats.append(data["heartbeat"])
            time.sleep(0.02)

    watcher = threading.Thread(target=watch)
    watcher.start()
    assert main("demo", layer, ["Beat", "--adw-id", "ee000007"]) == 0
    watcher.join()
    assert len(set(beats)) >= 3      # refreshed while a check was running, without any step changing
    final = json.loads(path.read_text())
    assert final["pid"] == os.getpid() and final["host"] == socket.gethostname()
    assert datetime.fromisoformat(final["heartbeat"]) <= datetime.fromisoformat(final["ended"])
    time.sleep(0.2)
    assert json.loads(path.read_text())["heartbeat"] == final["heartbeat"]   # the thread stopped


def test_old_state_files_without_the_new_keys_still_load_and_resume(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, adw_id="ee000008", steps=[{"name": "plan", "kind": "run", "command": "plan"}])
    run = layer / "agents/ee000008"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({
        "adw_id": "ee000008", "adw": "demo", "prompt": "old", "source": "cli", "klass": "chore", "class_fixed": False,
        "dry_run": False, "model_set": "", "branch": "", "worktree": "", "ports": [], "plan": "", "outputs": {},
        "steps": [], "checks": {}, "artefacts": [], "review": {}, "ship": {}, "fanout": {}, "status": "failed",
        "started": "2026-01-01T00:00:00+00:00", "ended": "2026-01-01T00:01:00+00:00"}))
    loaded = state_module.load(layer, "ee000008")
    assert loaded.pid == 0 and loaded.heartbeat == "" and loaded.fanout_parent == {}
    assert main("demo", layer, ["--resume", "--adw-id", "ee000008"]) == 0
    assert _events(layer, "ee000008")[0]["resumed"] is True


def test_a_fan_out_candidate_records_its_parent(make_layer, app_repo, monkeypatch):
    monkeypatch.setenv("ADW_FANOUT_PARENT", "0a0b0c0d")
    monkeypatch.setenv("ADW_FANOUT_STEP", "race")
    monkeypatch.setenv("ADW_FANOUT_INDEX", "2")
    layer = _layer(make_layer, app_repo, adw_id="ee000009", steps=[{"name": "plan", "kind": "run", "command": "plan"}])
    assert main("demo", layer, ["Candidate", "--adw-id", "ee000009"]) == 0
    saved = json.loads((layer / "agents/ee000009/state.json").read_text())
    assert saved["fanout_parent"] == {"run": "0a0b0c0d", "step": "race", "index": 2}
    assert _events(layer, "ee000009")[0]["fanout_parent"] == {"run": "0a0b0c0d", "step": "race", "index": 2}


# ---------------------------------------------------------------- slots

def test_slot_wait_acquire_release_events(make_layer, app_repo, tmp_path):
    layer_dir = _layer(make_layer, app_repo, adw_id="ee00000a")
    ir = json.loads((layer_dir / "agentic-layer.json").read_text())
    ir["concurrency"] = {"maxRuns": 1, "maxAgents": 1, "maxChecks": 1, "waitSeconds": 10}
    (layer_dir / "agentic-layer.json").write_text(json.dumps(ir))
    from adw_modules import ir as ir_module
    layer = ir_module.load(layer_dir)
    (layer_dir / "agents/.slots").mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(layer_dir / 'agents/.slots/checks.0.lock')!r}, os.O_CREAT | os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(0.6)\n")],
        stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    with events.recording(layer_dir, "ee00000a"):
        with slots.slot(layer, "checks", "adw ee00000a check test", poll=0.05) as index:
            assert index == 0
    holder.wait(timeout=10)
    found = _events(layer_dir, "ee00000a")
    assert [e["type"] for e in found] == ["slot.wait", "slot.acquire", "slot.release"]
    assert found[0]["pool"] == "checks" and found[0]["limit"] == 1 and found[0]["who"] == "adw ee00000a check test"
    assert found[1]["slot"] == 0 and found[1]["waited"] >= 0.3
    assert found[2]["held"] >= 0


# ---------------------------------------------------------------- streaming check logs

def test_check_output_reaches_its_log_while_the_check_runs(tmp_path):
    log = tmp_path / "checks" / "test.1.log"
    log.parent.mkdir()
    script = "printf 'first line\\n'; sleep 1.5; printf 'second line\\r\\n'; printf 'tail'"
    seen: list[str] = []

    def peek() -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            if log.is_file() and "first line" in log.read_text(errors="replace"):
                seen.append(log.read_text(errors="replace"))
                return
            time.sleep(0.05)

    peeker = threading.Thread(target=peek)
    peeker.start()
    code, output = steps.run_command(["bash", "-c", script], tmp_path, dict(os.environ), 30, log_path=log)
    peeker.join()
    assert seen and "second line" not in seen[0]     # written incrementally, not only at the end
    assert code == 0
    assert output == "first line\nsecond line\ntail"  # the same text as before (universal newlines)
    assert log.read_text(encoding="utf-8") == output


def test_check_timeout_still_kills_the_group_and_says_so(tmp_path):
    log = tmp_path / "slow.log"
    started = time.time()
    code, output = steps.run_command(["bash", "-c", "echo before; (sleep 30 &) ; sleep 30"], tmp_path,
                                     dict(os.environ), 1, log_path=log)
    assert code == 124 and time.time() - started < 15
    assert output.startswith("before\n") and output.endswith("command timed out after 1s\n")
    assert log.read_text() == output


def test_a_command_that_cannot_start_is_logged(tmp_path):
    log = tmp_path / "missing.log"
    code, output = steps.run_command(["/nonexistent/binary"], tmp_path, dict(os.environ), 5, log_path=log)
    assert code == 127 and "could not start" in output and log.read_text() == output


def test_run_command_without_a_log_behaves_as_before(tmp_path):
    assert steps.run_command(["bash", "-c", "echo hi"], tmp_path, dict(os.environ), 5) == (0, "hi\n")
