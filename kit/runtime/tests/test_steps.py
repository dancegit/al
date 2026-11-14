from __future__ import annotations

from pathlib import Path

import pytest

from adw_modules import ir, state
from adw_modules.envs import build
from adw_modules.errors import StepFailed
from adw_modules.steps import run_check, run_step


def setup_workflow(make_layer, app_repo, overrides=None):
    layer_dir = make_layer({
        "commands": [
            {"name": "classify", "template": "classify", "modelSet": "base", "output": "json"},
            {"name": "resolve", "template": "resolve", "modelSet": "base", "output": "text"},
            {"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
        ],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 5}]},
        "adws": [{"name": "demo", "agent": "mock-agent", "environment": ["worktree"], "steps": []}],
        "environments": [{"id": "worktree", "kind": "worktree", "portsPerRun": 0}],
        **(overrides or {}),
    })
    layer = ir.load(layer_dir)
    run_state = state.ADWState("b1b2b3b4", "demo", prompt="fix tests")
    env = build(layer, run_state, ["worktree"])
    env.setup()
    return layer, run_state, env


def test_when_skip_and_classify_routing(make_layer, app_repo):
    layer, run_state, env = setup_workflow(make_layer, app_repo)
    adw = layer.adw("demo")
    skipped = {"name": "plan-feature", "kind": "run", "command": "plan", "args": [], "when": "class-feature"}
    run_step(layer, run_state, adw, skipped, env)
    assert run_state.step_record("plan-feature", "run")["status"] == "skipped"
    classify = layer.layer_dir / "mock/demo/classify.json"
    classify.parent.mkdir(parents=True)
    classify.write_text('{"output":"{\\"class\\":\\"bug\\"}"}', encoding="utf-8")
    run_step(layer, run_state, adw, {"name": "classify", "kind": "run", "command": "classify"}, env)
    assert run_state.klass == "bug"
    run_state.class_fixed = True
    classify.write_text('{"output":"{\\"class\\":\\"feature\\"}"}', encoding="utf-8")
    run_step(layer, run_state, adw, {"name": "classify-fixed", "kind": "run", "command": "classify"}, env)
    assert run_state.klass == "bug"


def test_check_resolve_passes_and_logs_each_attempt(make_layer, app_repo):
    layer, run_state, env = setup_workflow(make_layer, app_repo)
    resolve = layer.layer_dir / "mock/demo/test-resolve-1.json"
    resolve.parent.mkdir(parents=True)
    resolve.write_text('{"files":{"test.sh":"#!/bin/sh\\nexit 0\\n"}}', encoding="utf-8")
    Path(run_state.worktree, "test.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # break it in the worktree only
    step = {"name": "test", "kind": "check", "cmd": "test", "resolve": "resolve", "retries": 1, "collect": []}
    run_step(layer, run_state, layer.adw("demo"), step, env)
    assert run_state.checks["test"]["exit"] == 0
    assert sorted(p.name for p in (layer.layer_dir / "agents/b1b2b3b4/checks").glob("*.log")) == ["test.1.log", "test.2.log"]
    assert (layer.layer_dir / "agents/b1b2b3b4/test-resolve-1/prompt.md").read_text().startswith("# resolve\n./test.sh")
    assert run_state.step_record("test", "check")["status"] == "passed"


def test_exhausted_check_retries_raise_and_record_logs(make_layer, app_repo):
    layer, run_state, env = setup_workflow(make_layer, app_repo)
    step = {"name": "test", "kind": "check", "cmd": "test", "retries": 1, "collect": []}
    with pytest.raises(StepFailed, match="exited 1"):
        # The initial fixture test succeeds; replace only the isolated worktree script.
        Path(run_state.worktree, "test.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        run_step(layer, run_state, layer.adw("demo"), step, env)


def test_check_timeout_records_124(make_layer, app_repo):
    layer, run_state, env = setup_workflow(make_layer, app_repo, {
        "codebase": {"commands": [{"name": "test", "run": "sleep 5", "timeout": 1}]}
    })
    step = {"name": "slow", "kind": "check", "cmd": "test", "retries": 0}
    with pytest.raises(StepFailed):
        run_step(layer, run_state, layer.adw("demo"), step, env)
    assert run_state.checks["slow"]["exit"] == 124
    log = Path(layer.layer_dir) / run_state.checks["slow"]["log"]
    assert "timed out" in log.read_text()


def test_check_rerun_attempts_continue_existing_numbers(make_layer, app_repo):
    layer, run_state, env = setup_workflow(make_layer, app_repo)
    assert run_check(layer, run_state, {"name": "test", "cmd": "test"}, env) == 0
    assert run_check(layer, run_state, {"name": "test", "cmd": "test"}, env) == 0
    assert Path(layer.layer_dir, "agents/b1b2b3b4/checks/test.2.log").exists()
