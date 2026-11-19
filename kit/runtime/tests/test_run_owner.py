"""One owner per run: a run's process holds agents/<id>/run.lock for its life, every party that
resumes a run checks it first, and the run's own decide step and the stand-in's loop decide each stop
once (agents/<id>/decide.claim). The holder of a lock is a child process (state.hold_run), so the
tests see a live owner from outside, as a second resume or the loop would."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_decide import ADW_ID, _layer, _state  # noqa: E402
from test_standin import declared, decider, item_for, run, set_ir, stopped_run  # noqa: E402,F401
from test_standin import no_settings_of_the_person  # noqa: E402,F401 — the same autouse fixture

from adw_modules import approvals, assign, decide, envs, events, ir, launch, runner, standin, state as state_module  # noqa: E402
from adw_modules.errors import LayerError  # noqa: E402
from adw_modules.runner import main  # noqa: E402

RUNTIME = Path(__file__).resolve().parent.parent
HOLD = ("import sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from adw_modules import state\n"
        "lock = state.hold_run(sys.argv[2], sys.argv[3])\n"
        "print('held', flush=True)\n"
        "time.sleep(600)\n")


@contextlib.contextmanager
def held_by_child(layer_dir: Path, run_id: str = ADW_ID):
    """A child process that holds the run's lock until the block ends (or the test kills it)."""
    child = subprocess.Popen([sys.executable, "-c", HOLD, str(RUNTIME), str(layer_dir), run_id],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        yield child
    finally:
        child.kill()
        child.wait(timeout=30)
        child.stdout.close()


def resume(layer_dir: Path, run_id: str = ADW_ID, *flags: str) -> int:
    return main("demo", layer_dir, ["--resume", "--adw-id", run_id, *flags])


def run_events(layer_dir: Path, run_id: str = ADW_ID) -> list[dict]:
    path = layer_dir / "agents" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def strictly_increasing(found: list[dict]) -> bool:
    seqs = [event["seq"] for event in found]
    return seqs == list(range(1, len(seqs) + 1))


def auto_layer(make_layer, tmp_path) -> Path:
    """A layer whose stand-in may resume alone; its worktree is kept when a run stops."""
    layer_dir = _layer(make_layer, tmp_path)
    environments = json.loads((layer_dir / "agentic-layer.json").read_text())["environments"]
    environments[0]["keep"] = "on-failure"
    set_ir(layer_dir, environments=environments, standIn=declared("auto", may=["resume"]))
    decider(layer_dir, "resume")
    return layer_dir


@pytest.fixture
def resumes(monkeypatch):
    found: list[str] = []
    monkeypatch.setattr(decide, "spawn_resume", lambda layer, state: found.append(state.adw_id))
    return found


# ------------------------------------------------------------------------------------ the run's lock

def test_a_second_resume_while_the_holder_lives_exits_3_naming_it(make_layer, tmp_path, capsys):
    layer_dir = stopped_run(make_layer, tmp_path)
    before = (layer_dir / "agents" / ADW_ID / "state.json").read_text(encoding="utf-8")
    capsys.readouterr()
    with held_by_child(layer_dir) as child:
        assert resume(layer_dir) == 3
        err = capsys.readouterr().err
        assert f"run {ADW_ID} is held by process {child.pid}" in err
    assert (layer_dir / "agents" / ADW_ID / "state.json").read_text(encoding="utf-8") == before


def test_a_resume_takes_over_after_the_holder_died(make_layer, tmp_path, monkeypatch):
    layer_dir = stopped_run(make_layer, tmp_path)
    with held_by_child(layer_dir) as child:
        child.kill()
        child.wait(timeout=30)
        holders: list[dict | None] = []
        original = envs.build

        def build(layer, state, ids):   # a step of the resumed run: the lock names this process
            holders.append(json.loads((layer_dir / "agents" / ADW_ID / state_module.RUN_LOCK).read_text()))
            holders.append(state_module.run_holder(layer_dir, ADW_ID))
            return original(layer, state, ids)
        monkeypatch.setattr(envs, "build", build)
        assert resume(layer_dir) == 1                             # it ran, and stopped at `test` again
    assert holders[0]["pid"] == os.getpid() and holders[1]["pid"] == os.getpid()
    assert state_module.run_holder(layer_dir, ADW_ID) is None    # released when the run ended
    assert [e["resumed"] for e in run_events(layer_dir) if e["type"] == "run.start"] == [False, True]


# ------------------------------------------------------------------------------------ one decision per stop

def test_the_loop_and_the_runs_decide_step_decide_a_stop_once(make_layer, tmp_path, monkeypatch, resumes):
    layer_dir = auto_layer(make_layer, tmp_path)
    seen: list[dict] = []
    original = decide.render_prompt

    def during(layer, state, adw):           # the loop looks while the run is inside its decide step
        seen.append(standin.consider(ir.load(layer_dir), item_for()))
        return original(layer, state, adw)
    monkeypatch.setattr(decide, "render_prompt", during)
    assert run(layer_dir, "--decider", "auto") == 1
    assert [r["status"] for r in seen] == ["skipped"], seen
    assert resumes == [ADW_ID]
    decisions = [e for e in standin.entries(ir.load(layer_dir), run=ADW_ID) if e["type"] == "decision"]
    assert [e["via"] for e in decisions] == ["run"]
    found = run_events(layer_dir)
    skipped = [e for e in found if e["type"] == "decision.skipped"]
    assert [e["who"] for e in skipped] == ["stand-in"] and skipped[0]["stop_seq"] == decide.stop_seq(layer_dir, ADW_ID) > 0
    assert strictly_increasing(found)
    assert standin.consider(ir.load(layer_dir), item_for())["status"] == "seen"


def test_the_runs_decide_step_skips_a_stop_the_loop_claimed(make_layer, tmp_path, monkeypatch, resumes):
    layer_dir = auto_layer(make_layer, tmp_path)
    original = runner._hand_off

    def hand_off(layer, state, adw, stack, failure):           # the loop claims the stop as it is handed over
        original(layer, state, adw, stack, failure)
        assert decide.claim_stop(layer.layer_dir, ADW_ID, "stand-in") == ""
    monkeypatch.setattr(runner, "_hand_off", hand_off)
    assert run(layer_dir, "--decider", "auto") == 1
    state = _state(layer_dir)
    assert not [c for c in state["agent_calls"] if c["step"] == decide.STEP]
    assert "action" not in (state.get("decision") or {})
    skipped = [e for e in run_events(layer_dir) if e["type"] == "decision.skipped"]
    assert [e["who"] for e in skipped] == ["run"]
    assert resumes == []
    assert strictly_increasing(run_events(layer_dir))


# ------------------------------------------------------------------------------------ every resume path

def test_every_resume_path_refuses_a_live_run(make_layer, tmp_path, monkeypatch):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=declared("suggest"))
    decider(layer_dir, "resume")
    assert standin.consider(ir.load(layer_dir), item_for())["status"] == "suggested"
    started: list[list[str]] = []
    monkeypatch.setattr(decide, "resume_argv", lambda layer, state: started.append(["decide"]) or None)
    layer = ir.load(layer_dir)
    before = (state_module.run_dir(layer_dir, ADW_ID) / "state.json").read_bytes()
    with held_by_child(layer_dir) as child:
        pid = str(child.pid)
        result = launch.resume(layer, ADW_ID, spawn=lambda argv, **kw: started.append(list(argv)))
        assert result["status"] == "refused" and launch.EXIT[result["status"]] == 3 and pid in result["error"]
        answered = decide.answer(layer, ADW_ID, "go", "", via="cli")
        assert (answered["status"], answered["reason"]) == ("refused", "running") and pid in answered["message"]
        applied = standin.apply_stored(layer, ADW_ID)
        assert applied["status"] == "refused" and pid in applied["reason"]
        given = assign.plan(layer, ADW_ID)
        assert given["blocked"] == "running" and pid in given["blocked_why"]
        granted = approvals.grant_and_resume(layer, ADW_ID, ["vocabulary"], "tester", "dashboard")
        assert (granted["status"], granted["reason"]) == ("refused", "running") and pid in granted["message"]
        assert (state_module.run_dir(layer_dir, ADW_ID) / "state.json").read_bytes() == before
        with pytest.raises(LayerError, match=pid):
            decide.spawn_resume(layer, state_module.load(layer_dir, ADW_ID))
    assert started == []
    assert _state(layer_dir)["decision"]["status"] == "suggested"


def test_a_container_start_with_a_live_owner_refuses(make_layer, tmp_path, monkeypatch):
    layer_dir = stopped_run(make_layer, tmp_path)
    calls: list[str] = []
    original_build, original_ensure = envs.build, envs.EnvStack._ensure_container

    def build(layer, state, ids):
        calls.append("build")
        return original_build(layer, state, ids)

    def ensure(self):
        calls.append("container")
        return original_ensure(self)
    monkeypatch.setattr(envs, "build", build)
    monkeypatch.setattr(envs.EnvStack, "_ensure_container", ensure)
    with held_by_child(layer_dir) as child:
        assert resume(layer_dir) == 3
        assert calls == []                                        # no environment built, no container removed
        child.kill()
        child.wait(timeout=30)
        assert resume(layer_dir) != 3
    assert "build" in calls                                       # a dead owner's run is taken over as before
