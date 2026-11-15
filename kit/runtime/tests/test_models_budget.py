"""Model tiers (which model each agent call runs on, and why) and spending limits (a run that
crosses a limit is handed to a person). Git is real; the agent is the mock, or a fake `claude`
executable that records its command line and reports a cost the way Claude Code does."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adw_modules import budget, events, inputs, ir, models, state, steps, tasklist
from adw_modules.envs import build
from adw_modules.errors import LayerError
from adw_modules.runner import main

ADW_ID = "b0d9e700"
TIERS = [{"tier": "base", "model": "claude-opus-5-5", "effort": "default"},
         {"tier": "heavy", "model": "claude-fable-5-2", "effort": "high"},
         {"tier": "fast", "model": "claude-haiku-5-5", "effort": "default"}]
CLAUDE = {"id": "claude", "kind": "claude-code", "status": "stable", "models": TIERS, "permissionMode": "acceptEdits",
          "safeMode": True, "allowedTools": [], "maxTurns": 5, "timeout": 20}
MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
TEXT = {"template": "build", "output": "text", "description": "", "file": "", "focus": [], "fabric": [],
        "relevant": [], "validation": []}


def _layer(make_layer, *, agents=None, steps_=None, commands=None, budget_=None, review=None, extra=None) -> Path:
    ir_ = {
        "agents": agents or [MOCK],
        "commands": commands or [{"name": "plan", "modelSet": "heavy", **TEXT},
                                 {"name": "build", "modelSet": "base", **TEXT},
                                 {"name": "commit", "modelSet": "fast", **TEXT},
                                 {"name": "review", "template": "review", "modelSet": "heavy", "output": "json",
                                  "description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27900,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [review or {"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "adws": [{"name": "demo", "agent": (agents or [MOCK])[0]["id"], "review": (review or {"id": "human"})["id"],
                  "environment": ["wt"], "input": ["cli"], "trigger": ["manual"], "classes": [],
                  "steps": steps_ or [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
                                      {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
                                      {"name": "review", "kind": "review"},
                                      {"name": "ship", "kind": "ship"}]}],
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": budget_,
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        **(extra or {}),
    }
    return make_layer(ir_)


def _mock(layer: Path, step: str, **script) -> None:
    path = layer / "mock" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script), encoding="utf-8")


def _state(layer: Path, adw_id: str = ADW_ID) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _events(layer: Path, adw_id: str = ADW_ID) -> list[dict]:
    return events.read(layer / "agents" / adw_id / "events.jsonl")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that records its argv and reports the cost in $FAKE_COST."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "claude-argv.jsonl"
    exe = bindir / "claude"
    exe.write_text("#!/usr/bin/env python3\nimport json, os, sys\nsys.stdin.read()\n"
                   f"open({str(record)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                   "cost = float(os.environ.get('FAKE_COST', '0.1'))\n"
                   "print(json.dumps({'type': 'result', 'subtype': os.environ.get('FAKE_SUBTYPE', 'success'),"
                   " 'is_error': os.environ.get('FAKE_SUBTYPE', 'success') != 'success', 'result': 'done',"
                   " 'total_cost_usd': cost, 'num_turns': 1}))\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    for key in (models.ENV_TIER, models.ENV_MODELS, models.ENV_MAX_COST):
        monkeypatch.delenv(key, raising=False)

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    return calls


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (models.ENV_TIER, models.ENV_MODELS, models.ENV_MAX_COST):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------------------------------ tiers

def test_overrides_flags_win_over_environment():
    env = {"AL_MODEL_SET": "fast", "AL_MODELS": "heavy=x-1, base=x-2", "AL_MAX_COST": "4"}
    over = models.overrides(None, None, None, env)
    assert (over["tier"], over["models"], over["max_cost"]) == ("fast", {"heavy": "x-1", "base": "x-2"}, 4.0)
    assert over["from"] == {"tier": "AL_MODEL_SET", "models": "AL_MODELS", "max_cost": "AL_MAX_COST"}
    over = models.overrides("heavy", ["heavy=y-1"], "2.5", env)
    assert (over["tier"], over["models"], over["max_cost"]) == ("heavy", {"heavy": "y-1", "base": "x-2"}, 2.5)
    assert over["from"]["tier"] == "--model-set" and over["from"]["max_cost"] == "--max-cost"
    for bad in (("Heavy", None, None), (None, ["heavy"], None), (None, ["=x"], None), (None, None, "0"),
                (None, None, "lots")):
        with pytest.raises(ValueError):
            models.overrides(*bad, environ={})


def test_old_ir_with_model_base_and_heavy_still_resolves():
    old = {"id": "c", "kind": "claude-code", "modelBase": "s-1", "modelHeavy": "o-1"}
    assert models.choose(old, "heavy", "command", {}).model == "o-1"
    assert models.agent_tiers(old) == {"base": {"model": "s-1", "effort": "default"},
                                       "heavy": {"model": "o-1", "effort": "default"}}
    assert models.agent_tiers(MOCK) is None and models.choose(MOCK, "anything", "step", {}).model == ""
    with pytest.raises(LayerError, match="no model tier `fast`.*declares: base, heavy"):
        models.choose(old, "fast", "request", {})


def test_resolution_order_step_request_command_and_override(make_layer, fake_claude):
    """step #:model-set > request tier > command tier; --model-set beats all; --model remaps a tier."""
    layer_dir = _layer(make_layer, agents=[CLAUDE], steps_=[
        {"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},     # command: heavy
        {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},   # command: base
        {"name": "commit", "kind": "run", "command": "build", "modelSet": "fast"}])    # step: fast
    assert main("demo", layer_dir, ["Add a thing", "--adw-id", ADW_ID]) == 0
    first = [(c["step"], c["tier"], c["tier_from"], c["model"], c["model_from"]) for c in _state(layer_dir)["agent_calls"]]
    assert first == [("plan", "heavy", "command", "claude-fable-5-2", "agent"),
                     ("build", "base", "command", "claude-opus-5-5", "agent"),
                     ("commit", "fast", "step", "claude-haiku-5-5", "agent")]
    argvs = fake_claude()
    assert [_flag(a, "--model") for a in argvs] == ["claude-fable-5-2", "claude-opus-5-5", "claude-haiku-5-5"]
    assert _flag(argvs[0], "--effort") == "high" and "--effort" not in argvs[1]   # effort only where declared
    assert "--max-budget-usd" not in argvs[0]                                     # no limit, no cap
    # the step record and the events say the same
    plan_record = next(r for r in _state(layer_dir)["steps"] if r["name"] == "plan")
    assert (plan_record["tier"], plan_record["tier_from"], plan_record["model"]) == ("heavy", "command", "claude-fable-5-2")
    start = next(e for e in _events(layer_dir) if e["type"] == "agent.start" and e["step"] == "commit")
    assert (start["tier"], start["tier_from"], start["model"], start["model_set"]) == ("fast", "step", "claude-haiku-5-5", "fast")

    # a request tier (task tag / issue keyword) beats the command's tier, not the step's own
    run_state = state.ADWState("b0d9e701", "demo", prompt="x", model_set="fast")
    lay = ir.load(layer_dir)
    plan_step, _build, commit_step = lay.adw("demo")["steps"]
    assert steps.step_tier(lay, run_state, plan_step, lay.command("plan")) == ("fast", "request")
    commit_step = dict(commit_step, modelSet="heavy")
    assert steps.step_tier(lay, run_state, commit_step, lay.command("build")) == ("heavy", "step")

    # flags: every call on `base`, and base remapped to another model id
    assert main("demo", layer_dir, ["Again", "--adw-id", "b0d9e702", "--model-set", "base",
                                    "--model", "base=claude-opus-5-5-test"]) == 0
    calls = _state(layer_dir, "b0d9e702")["agent_calls"]
    assert {(c["tier"], c["tier_from"], c["model"], c["model_from"]) for c in calls} == {
        ("base", "override", "claude-opus-5-5-test", "override")}
    assert _state(layer_dir, "b0d9e702")["overrides"]["from"] == {"tier": "--model-set", "models": "--model"}


def test_environment_variables_and_their_errors(make_layer, fake_claude, monkeypatch, capsys):
    layer_dir = _layer(make_layer, agents=[CLAUDE], steps_=[{"name": "plan", "kind": "run", "command": "plan",
                                                              "modelSet": "inherit"}])
    monkeypatch.setenv("AL_MODELS", "heavy=claude-fable-5-2-env")
    assert main("demo", layer_dir, ["x", "--adw-id", "b0d9e710"]) == 0
    call = _state(layer_dir, "b0d9e710")["agent_calls"][0]
    assert (call["model"], call["model_from"]) == ("claude-fable-5-2-env", "override")
    monkeypatch.setenv("AL_MODEL_SET", "turbo")
    assert main("demo", layer_dir, ["x", "--adw-id", "b0d9e711"]) == 2
    assert "agent `claude` (used by ADW `demo`) has no tier `turbo`; it declares: base, heavy, fast" in capsys.readouterr().err
    monkeypatch.delenv("AL_MODEL_SET")
    assert main("demo", layer_dir, ["x", "--adw-id", "b0d9e712", "--model", "turbo=x"]) == 2
    assert "no agent of this layer has a tier `turbo`" in capsys.readouterr().err
    assert not (layer_dir / "agents" / "b0d9e711").exists()


def test_help_documents_the_overrides(make_layer, capsys):
    layer_dir = _layer(make_layer)
    assert main("demo", layer_dir, ["--help"]) == 0
    out = capsys.readouterr().out
    for needle in ("--model-set TIER", "--model TIER=MODEL_ID", "--max-cost USD", "AL_MODEL_SET", "AL_MODELS",
                   "AL_MAX_COST", "Flags win", "3 refused"):
        assert needle in out


def test_any_declared_tier_can_be_requested_by_a_tag_or_keyword(make_layer):
    layer = ir.load(_layer(make_layer, agents=[CLAUDE]))
    assert inputs.layer_tiers(layer) == ["base", "heavy", "fast"]
    task = tasklist.Task("default", 1, "open", "Do it", tags=["sdlc", "fast"])
    assert task.model_set is None and task.tier(inputs.layer_tiers(layer)) == "fast"
    assert inputs.keyword_selection("adw: sdlc fast", "adw", inputs.layer_tiers(layer)) == ("sdlc", "fast")
    assert inputs.keyword_selection("adw: sdlc turbo", "adw", inputs.layer_tiers(layer)) == ("sdlc", None)
    assert inputs.keyword_selection("adw: sdlc heavy", "adw") == ("sdlc", "heavy")


def test_reviewers_run_on_the_review_options_agent(make_layer, monkeypatch):
    other = {"id": "second", "kind": "mock", "scriptDir": "mock-second", "status": "stable", "timeout": 20}
    review = {"id": "agent-review", "kind": "agent", "status": "stable", "command": "review", "artefacts": [],
              "reviewers": 1, "maxRetries": 0, "bodyCmd": "", "agent": "second"}
    layer_dir = _layer(make_layer, agents=[MOCK, other], review=review)
    _mock(layer_dir, "build", files={"feature.txt": "x\n"})
    second = layer_dir / "mock-second" / "review-1.json"
    second.parent.mkdir()
    second.write_text(json.dumps({"output": '{"success": true, "summary": "second opinion: fine", "issues": []}'}))
    assert main("demo", layer_dir, ["Add a feature", "--adw-id", ADW_ID]) == 0
    calls = {c["step"]: c["agent"] for c in _state(layer_dir)["agent_calls"]}
    assert calls == {"plan": "mock", "build": "mock", "review-1": "second"}
    assert "second opinion" in _state(layer_dir)["review"]["verdict"]["summary"]


# ------------------------------------------------------------------------------------ budgets

def _limits(**kw) -> dict:
    return {"id": "limits", "kind": "limits", "status": "stable", "runUsd": kw.get("run"), "stepUsd": kw.get("step"),
            "dayUsd": kw.get("day")}


def test_a_run_over_its_limit_is_handed_to_a_person(make_layer, capsys):
    layer_dir = _layer(make_layer, budget_=_limits(run=2.5))
    _mock(layer_dir, "plan", output="planned", cost=1.5)
    _mock(layer_dir, "build", files={"feature.txt": "half done\n"}, output="built", cost=1.5)
    assert main("demo", layer_dir, ["Add a feature", "--adw-id", ADW_ID]) == 1
    data = _state(layer_dir)
    assert data["status"] == "failed"
    assert data["cost"]["total"] == 3.0 and data["cost"]["calls"] == 2
    assert data["budget"]["stopped"] == {"limit": "run", "spent": 3.0, "of": 2.5, "step": "build",
                                         "reason": "budget:run: spent $3.00 of $2.50"}
    build = next(r for r in data["steps"] if r["name"] == "build")
    assert build["status"] == "failed" and "budget:run: spent $3.00 of $2.50" in build["detail"]
    assert data["ship"]["action"] == "handoff" and data["ship"]["stopped_at"] == "build"
    assert data["ship"]["reasons"] == ["run-stopped:build: budget:run: spent $3.00 of $2.50"]
    pr = (layer_dir / data["ship"]["target"]).read_text()
    assert "budget:run: spent $3.00 of $2.50" in pr
    stop = next(e for e in _events(layer_dir) if e["type"] == "budget.stop")
    assert (stop["limit"], stop["spent"], stop["of"], stop["step"]) == ("run", 3.0, 2.5, "build")
    assert next(e for e in _events(layer_dir) if e["type"] == "handoff")["reason"].startswith("budget:run:")
    out = capsys.readouterr().out
    assert "cost:      $3.00 of run limit $2.50; STOPPED by budget:run: spent $3.00 of $2.50" in out


def test_a_step_over_the_step_limit_stops_the_run_and_is_not_retried(make_layer):
    layer_dir = _layer(make_layer, budget_=_limits(step=1.0), steps_=[
        {"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit", "retries": 2},
        {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
        {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    _mock(layer_dir, "plan", files={"plan.txt": "x\n"}, cost=1.25)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 1
    data = _state(layer_dir)
    assert len(data["agent_calls"]) == 1                       # retries: 2, but a budget stop is final
    assert data["budget"]["stopped"]["reason"] == "budget:step: spent $1.25 of $1.00"
    assert data["ship"]["action"] == "handoff"


def test_max_cost_flag_replaces_the_layer_run_limit(make_layer, monkeypatch):
    layer_dir = _layer(make_layer, budget_=_limits(run=100))
    _mock(layer_dir, "plan", cost=0.75)
    _mock(layer_dir, "build", files={"f.txt": "x\n"}, cost=0.75)
    monkeypatch.setenv("AL_MAX_COST", "5")
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID, "--max-cost", "1"]) == 1
    data = _state(layer_dir)
    assert data["budget"]["limits"] == {"run": 1.0, "step": None, "day": None, "run_from": "--max-cost"}
    assert data["budget"]["stopped"]["reason"] == "budget:run: spent $1.50 of $1.00"


def test_agents_that_report_no_cost_count_as_zero_and_the_summary_says_so(make_layer, capsys):
    layer_dir = _layer(make_layer, budget_=_limits(run=1))
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    data = _state(layer_dir)
    assert data["cost"] == {"total": 0.0, "calls": 2, "unreported": ["mock (mock)"]}
    assert "reports no cost, counted as $0: mock (mock)" in capsys.readouterr().out


def _past_run(layer_dir: Path, adw_id: str, cost: float, started: datetime, with_state_cost: bool = True) -> None:
    run = layer_dir / "agents" / adw_id
    run.mkdir(parents=True)
    data = {"adw_id": adw_id, "adw": "demo", "status": "passed", "started": started.isoformat()}
    if with_state_cost:
        data["cost"] = {"total": cost, "calls": 1}
    else:  # a run from before state.json had a cost: its events still do
        (run / "events.jsonl").write_text(json.dumps({"type": "agent.end", "seq": 1, "cost": cost}) + "\n")
    (run / "state.json").write_text(json.dumps(data))


def test_the_day_limit_refuses_to_start_a_run(make_layer, capsys):
    layer_dir = _layer(make_layer, budget_=_limits(day=10))
    now = datetime.now(timezone.utc)
    _past_run(layer_dir, "aaaa0001", 6.0, now)
    _past_run(layer_dir, "aaaa0002", 4.5, now, with_state_cost=False)
    _past_run(layer_dir, "aaaa0003", 50.0, now - timedelta(days=2))   # another day: not counted
    assert budget.day_spent(layer_dir) == 10.5
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == budget.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "refused: budget:day: spent $10.50 of $10.00 today" in err and "no run starts until tomorrow" in err
    assert not (layer_dir / "agents" / ADW_ID).exists()


def test_the_day_limit_stops_a_run_before_its_next_agent_step(make_layer):
    layer_dir = _layer(make_layer, budget_=_limits(day=4), steps_=[
        {"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
        {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
        {"name": "polish", "kind": "run", "command": "build", "modelSet": "inherit"},
        {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    _past_run(layer_dir, "aaaa0001", 1.0, datetime.now(timezone.utc))
    _mock(layer_dir, "plan", cost=1.0)
    _mock(layer_dir, "build", files={"f.txt": "x\n"}, cost=2.5)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 1
    data = _state(layer_dir)
    assert [c["step"] for c in data["agent_calls"]] == ["plan", "build"]   # polish never started
    assert data["budget"]["stopped"]["reason"] == "budget:day: spent $4.50 of $4.00"
    assert data["ship"]["stopped_at"] == "polish"


def test_claude_gets_a_native_cap_and_a_cap_stop_is_a_budget_stop(make_layer, fake_claude, monkeypatch):
    layer_dir = _layer(make_layer, agents=[CLAUDE], budget_=_limits(run=3, step=2),
                       steps_=[{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
                               {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"}])
    monkeypatch.setenv("FAKE_COST", "1.5")
    assert main("demo", layer_dir, ["x", "--adw-id", "b0d9e7a0"]) == 0   # 1.5 + 1.5 is not over 3
    monkeypatch.setenv("FAKE_COST", "1.6")
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 1     # 1.6 + 1.6 is
    caps = [_flag(a, "--max-budget-usd") for a in fake_claude()][2:]
    assert caps == ["2.0000", "1.4000"]   # the step limit, then what is left of the run limit
    assert _state(layer_dir)["budget"]["stopped"]["reason"] == "budget:run: spent $3.20 of $3.00"
    # the CLI ended a call at its cap: reported as the limit that set the cap
    monkeypatch.setenv("FAKE_COST", "1.99")
    monkeypatch.setenv("FAKE_SUBTYPE", "error_max_budget_usd")
    assert main("demo", layer_dir, ["x", "--adw-id", "b0d9e7a1"]) == 1
    stopped = _state(layer_dir, "b0d9e7a1")["budget"]["stopped"]
    assert (stopped["limit"], stopped["step"]) == ("step", "plan")


def test_the_cron_trigger_claims_nothing_once_the_day_limit_is_reached(make_layer):
    import importlib.util
    spec = importlib.util.spec_from_file_location("trigger_cron", Path(__file__).resolve().parents[1] / "triggers" / "trigger_cron.py")
    cron = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cron)
    layer_dir = _layer(make_layer, budget_=_limits(day=1))
    _past_run(layer_dir, "aaaa0001", 2.0, datetime.now(timezone.utc))
    said: list[str] = []
    sched = cron.Scheduler(ir.load(layer_dir), "cron", launch=lambda *a: pytest.fail("launched"), out=said.append)
    assert sched.poll_once() == 0 and sched.poll_once() == 0
    assert said == ["not starting runs: budget:day: spent $2.00 of $1.00 today (" + budget.today()
                    + ") in this layer; no run starts until tomorrow, or raise #:day-usd in (budget limits …)"]
