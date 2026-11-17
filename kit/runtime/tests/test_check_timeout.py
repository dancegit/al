"""A check that hits its time limit (exit 124) is not a test failure: it runs once more with twice
the limit and without the resolve agent. Timing out again fails the step with both limits and the
last progress line of its log; every time-limit retry is recorded on the step."""
from __future__ import annotations

import json
from pathlib import Path

from adw_modules.runner import main

ADW_ID = "71e00001"


def _layer(make_layer, run: str, timeout: int, retries: int = 1) -> Path:
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "resolve-test", "template": "resolve-test", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "slow", "run": run, "timeout": timeout}]},
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 27200, "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "auto", "environment": ["worktree"], "classes": ["chore"],
                  "steps": [{"name": "suite", "kind": "check", "cmd": "slow", "resolve": "resolve-test",
                             "retries": retries}]}],
    })


def _record(layer: Path) -> dict:
    state = json.loads((layer / "agents" / ADW_ID / "state.json").read_text())
    return next(r for r in state["steps"] if r["name"] == "suite")


def test_a_check_that_times_out_twice_fails_with_both_limits_and_its_last_progress_line(make_layer, app_repo) -> None:
    layer = _layer(make_layer, "echo working on it; sleep 30", timeout=1)
    assert main("demo", layer, ["Slow suite", "--adw-id", ADW_ID]) == 1
    record = _record(layer)
    assert record["status"] == "failed"
    assert "timed out twice (1 s, then 2 s)" in record["detail"]
    assert "last progress: working on it" in record["detail"]
    assert not (layer / "agents" / ADW_ID / "suite-resolve-1").exists()
    retries = record["time_limit_retries"]
    assert len(retries) == 1 and retries[0]["limit"] == 1 and retries[0]["retry_limit"] == 2
    assert retries[0]["exit"] == 124


def test_a_check_that_times_out_once_passes_on_the_retry_with_twice_the_limit(make_layer, app_repo) -> None:
    layer = _layer(make_layer, "if [ -f .slow-done ]; then exit 0; fi; touch .slow-done; sleep 30", timeout=2)
    assert main("demo", layer, ["Slow suite", "--adw-id", ADW_ID]) == 0
    record = _record(layer)
    assert record["status"] == "passed"
    assert len(record["time_limit_retries"]) == 1 and record["time_limit_retries"][0]["exit"] == 0
    assert "time limit 2 s was too low: passed on the retry with 4 s" in record["detail"]
    assert not (layer / "agents" / ADW_ID / "suite-resolve-1").exists()


def test_a_retry_that_fails_normally_goes_to_the_resolve_agent(make_layer, app_repo) -> None:
    layer = _layer(make_layer, "if [ -f .slow-done ]; then exit 3; fi; touch .slow-done; sleep 30", timeout=1)
    assert main("demo", layer, ["Slow suite", "--adw-id", ADW_ID]) == 1
    record = _record(layer)
    assert "exited 3" in record["detail"] and "timed out twice" not in record["detail"]
    assert (layer / "agents" / ADW_ID / "suite-resolve-1").is_dir()
    assert len(record["time_limit_retries"]) == 1
