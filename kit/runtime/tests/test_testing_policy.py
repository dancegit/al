"""The testing policy (adw_modules/testing.py): depths and levels, the local setting and who wins,
escalation, the plan of one check (targeted, smoke or full, and why), the miss-rate fallback, the
accounting a person reads, and the failing-test parsers in skips.py the batch needs."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import affected, selection, skips, testing
from adw_modules.ir import load
from adw_modules.state import ADWState

DATA = Path(__file__).resolve().parent / "data"
ADW_ID = "d0000001"
STEP = {"name": "unit", "kind": "check", "cmd": "unit"}
UNDECLARED = ("testing: full (this layer has no (testing …) clause, so every check runs its full form; "
              "add (testing targeted …) and #:targeted forms to test what a change touches)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def _python_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(DATA / "selection" / "python", repo, ignore=shutil.ignore_patterns("conftest.py"))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _cmd(**over) -> dict:
    return {"name": "unit", "run": "pytest -q", "covers": ["pkg", "tests"], "always": False, "reuse": True,
            "targeted": "pytest -q {tests}", "smoke": "pytest -q tests/test_core.py", "select": ["changed-tests", "imports"],
            **over}


def _layer(make_layer, repo: Path, *, cmd: dict | None = None, testing_ir: dict | None = None, ship: str = "pr",
           extra: dict | None = None):
    ir = {
        "codebase": {"root": str(repo), "commands": [cmd if cmd is not None else _cmd()]},
        "reviews": [{"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review",
                     "reviewers": 1, "maxRetries": 0, "allowedClasses": ["chore", "bug"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": ship, "deployCmd": ""}],
        "adws": [{"name": "demo", "agent": "mock-agent", "review": "auto", "environment": ["worktree"], "classes": [],
                  "steps": [{"name": "unit", "kind": "check", "cmd": "unit"}, {"name": "ship", "kind": "ship"}]}],
        **(extra or {})}
    if testing_ir is not None:
        ir["testing"] = testing_ir
    return load(make_layer(ir))


def _run(repo: Path, klass: str = "chore"):
    return ADWState(adw_id=ADW_ID, adw="demo", worktree=str(repo), klass=klass), SimpleNamespace(sandbox=None)


def _plan(layer, state, env, step=STEP, phase="check"):
    return testing.plan_check(layer, state, env, step, phase, affected.base(layer, state, env),
                              affected.tree(layer, state, env))


# ---------------------------------------------------------------- policy, levels, setting, precedence

def test_undeclared_layer_is_full_everywhere(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo)
    policy = testing.policy(layer)
    assert policy["declared"] is False
    assert [policy[key] for key in ("depth", "perRun", "reviewRounds", "onStage")] == ["full"] * 4
    state, env = _run(repo)
    for phase in ("check", "gate", "recheck"):
        assert testing.run_depth(layer, state, phase) == ("full", "the layer declares no (testing …)")
    (repo / "pkg" / "core.py").write_text("def total(p):\n    return 0\n", encoding="utf-8")
    plan = _plan(layer, state, env)
    assert plan.depth == "full" and plan.command["run"] == "pytest -q" and plan.selection is None
    assert testing.on_stage(layer) == "full"
    assert testing.explain_lines(layer) == [UNDECLARED] and testing.verify_lines(layer) == [UNDECLARED]


def test_declared_policy_resolves_inherit(tmp_path, make_layer):
    layer = _layer(make_layer, _python_repo(tmp_path), testing_ir={"depth": "targeted", "perRun": "inherit",
                                                                   "reviewRounds": "smoke", "batchEvery": 5})
    policy = testing.policy(layer)
    assert policy["declared"] is True and policy["perRun"] == "targeted" and policy["reviewRounds"] == "smoke"
    assert policy["onStage"] == "full" and policy["batchEvery"] == 5 and policy["escalateDiffLines"] == 1500
    assert policy["escalateOn"] == ["lockfile", "layer", "unmapped"] and policy["maxMissPercent"] == 10


def test_precedence_request_over_flag_over_setting_over_layer(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo, testing_ir={"depth": "targeted", "reviewRounds": "smoke", "batchEvery": 5})
    state, _env = _run(repo)
    assert testing.run_depth(layer, state, "check") == ("targeted", "layer policy")
    assert testing.run_depth(layer, state, "recheck") == ("smoke", "layer policy")
    testing.write_setting(layer.layer_dir, "heavy")
    assert testing.read_setting(layer.layer_dir) == "heavy"
    assert testing.run_depth(layer, state, "check") == ("full", "local setting")
    state.testing = {"requested": "smoke"}
    assert testing.run_depth(layer, state, "gate") == ("smoke", "--testing")
    assert testing.requested_full(layer.layer_dir, ADW_ID) is None
    testing.request_full(layer.layer_dir, ADW_ID, "dashboard")
    assert testing.requested_full(layer.layer_dir, ADW_ID)["by"] == "dashboard"
    assert testing.run_depth(layer, state, "gate") == ("full", "a person asked for full before it ships")
    assert testing.run_depth(layer, state, "check") == ("smoke", "--testing")  # the request is about what ships
    testing.write_setting(layer.layer_dir, None)
    assert testing.read_setting(layer.layer_dir) is None
    assert not (Path(layer.layer_dir) / "agents" / "settings" / "testing.json").exists()


def test_levels_and_depths_normalise():
    assert testing.LEVELS == {"light": "smoke", "standard": "targeted", "heavy": "full"}
    assert testing.DEPTHS == ("smoke", "targeted", "full")
    for value, depth in (("light", "smoke"), ("standard", "targeted"), ("heavy", "full"),
                         ("smoke", "smoke"), ("targeted", "targeted"), ("full", "full"), (" Heavy ", "full")):
        assert testing.normalise(value) == depth
    for bad in ("medium", "", None, "inherit"):
        with pytest.raises(ValueError):
            testing.normalise(bad)


def test_setting_file_format_and_bad_level(tmp_path, make_layer):
    layer = _layer(make_layer, _python_repo(tmp_path))
    path = testing.write_setting(layer.layer_dir, "standard")
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1, "level": "standard"}
    testing.write_setting(layer.layer_dir, "full")  # a depth name is stored as its level
    assert testing.read_setting(layer.layer_dir) == "heavy"
    with pytest.raises(ValueError):
        testing.write_setting(layer.layer_dir, "medium")


def test_setting_refused_for_trunk_zero_touch_without_batch(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo, ship="merge", testing_ir={"depth": "full"})
    allowed, why = testing.setting_allowed(layer, "standard")
    assert allowed is False
    assert why == ("(adw demo) ships to main without a person at testing depth targeted, but no batch verification "
                   "is declared: add #:batch ((every N)) or ((after-hours H)) to (testing …), or #:per-run full")
    assert testing.setting_allowed(layer, "heavy") == (True, "")
    batched = _layer(make_layer, repo, ship="merge", testing_ir={"depth": "full", "batchAfterHours": 6})
    assert testing.setting_allowed(batched, "light") == (True, "")
    stages = json.loads((DATA / "delivery-sprint.json").read_text(encoding="utf-8"))["stages"]
    into_sprint = _layer(make_layer, repo, ship="merge", testing_ir={"depth": "full"}, extra={"stages": stages})
    assert testing.setting_allowed(into_sprint, "light") == (True, "")  # the sprint stage is not the final one
    by_pr = _layer(make_layer, repo, ship="pr", testing_ir={"depth": "full"})
    assert testing.setting_allowed(by_pr, "light") == (True, "")


# ---------------------------------------------------------------- escalation

_QUIET = {"depth": "targeted", "batchEvery": 5, "escalatePaths": [], "escalateDiffLines": 0, "escalateFiles": 0,
          "escalateOn": [], "escalateClasses": []}


@pytest.mark.parametrize("over, changed, numstat, klass, layer_extra, expected", [
    ({"escalatePaths": ["a/core/**"]}, ["a/x.py", "a/core/x.py"], None, "chore", {},
     "core path a/core/x.py (a/core/**)"),
    ({"escalateDiffLines": 10}, ["a/x.py"], {"a/x.py": (8, 3)}, "chore", {}, "11 changed lines > 10"),
    ({"escalateFiles": 2}, ["a/x.py", "a/y.py", "a/z.py"], None, "chore", {}, "3 changed files > 2"),
    ({"escalateOn": ["lockfile"]}, ["a/x.py", "web/package-lock.json"], None, "chore", {},
     "lockfile web/package-lock.json"),
    ({"escalateOn": ["layer"]}, ["agentic/agentic.rkt"], None, "chore",
     {"layer": {"id": "in-repo", "kind": "in-repo", "dir": "agentic", "status": "stable"}},
     "the agentic layer changed (agentic/agentic.rkt)"),
    ({"escalateClasses": ["feature"]}, ["a/x.py"], None, "feature", {}, "class feature"),
], ids=["core-path", "diff-lines", "files", "lockfile", "layer", "class"])
def test_escalation_reasons(tmp_path, make_layer, over, changed, numstat, klass, layer_extra, expected):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo, testing_ir={**_QUIET, **over}, extra=layer_extra)
    state, env = _run(repo, klass)
    assert testing.escalation(layer, state, env, changed, numstat) == [expected]
    quiet = _layer(make_layer, repo, testing_ir=_QUIET, extra=layer_extra)
    assert testing.escalation(quiet, _run(repo, klass)[0], env, changed, numstat) == []


def test_escalation_is_cached_per_tree(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo, testing_ir={**_QUIET, "escalateFiles": 1})
    state, env = _run(repo)
    assert testing.escalation(layer, state, env, ["a", "b"], None, tree="t1") == ["2 changed files > 1"]
    assert testing.escalation(layer, state, env, ["a"], None, tree="t1") == ["2 changed files > 1"]
    assert state.testing["escalation"]["t1"] == ["2 changed files > 1"]
    assert testing.escalation(layer, state, env, ["a"], None, tree="t2") == []


# ---------------------------------------------------------------- the plan of one check

def test_plan_without_narrower_form_is_full(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo, cmd=_cmd(targeted="", smoke=""), testing_ir={**_QUIET})
    state, env = _run(repo)
    (repo / "pkg" / "core.py").write_text("def total(p):\n    return 0\n", encoding="utf-8")
    plan = _plan(layer, state, env)
    assert (plan.depth, plan.why, plan.command["run"]) == ("full", "no narrower form", "pytest -q")


def test_plan_targeted_fills_the_selected_tests_and_changes_the_definition(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo, testing_ir={**_QUIET, "escalateOn": ["unmapped"]})
    state, env = _run(repo)
    (repo / "pkg" / "core.py").write_text("def total(p):\n    return sum(p)\n", encoding="utf-8")
    plan = _plan(layer, state, env)
    assert (plan.depth, plan.asked, plan.why) == ("targeted", "targeted", "")
    assert plan.command["run"] == "pytest -q tests/test_core.py tests/test_util.py"
    assert plan.units == "test files" and plan.selection.by_strategy == {"imports": 2}
    full = layer.cmd("unit")
    assert affected.definition(layer, state, plan.command, STEP) != affected.definition(layer, state, full, STEP)
    staged = _plan(layer, state, env, step={**STEP, "depth": "full"})  # a promotion or batch says its depth
    assert staged.depth == "full" and staged.command["run"] == "pytest -q"


def test_plan_empty_selection_runs_smoke_else_full(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    (repo / "pkg" / "plugin.py").write_text("def discount(price):\n    return price\n", encoding="utf-8")
    layer = _layer(make_layer, repo, testing_ir={**_QUIET})
    state, env = _run(repo)
    plan = _plan(layer, state, env)
    assert (plan.depth, plan.asked) == ("smoke", "targeted")
    assert plan.command["run"] == "pytest -q tests/test_core.py"
    assert plan.why == "no test selected for a covered change"
    no_smoke = _layer(make_layer, repo, cmd=_cmd(smoke=""), testing_ir={**_QUIET})
    plan = _plan(no_smoke, state, env)
    assert (plan.depth, plan.why, plan.command["run"]) == ("full", "no test selected for a covered change", "pytest -q")


def test_plan_max_targeted_falls_back_to_full(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    (repo / "pkg" / "core.py").write_text("def total(p):\n    return 1\n", encoding="utf-8")
    layer = _layer(make_layer, repo, cmd=_cmd(maxTargeted=1), testing_ir={**_QUIET})
    state, env = _run(repo)
    plan = _plan(layer, state, env)
    assert (plan.depth, plan.why, plan.command["run"]) == ("full", "2 selected > max-targeted 1", "pytest -q")


def test_plan_unmapped_escalates(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    (repo / "pkg" / "plugin.py").write_text("def discount(price):\n    return price\n", encoding="utf-8")
    layer = _layer(make_layer, repo, testing_ir={**_QUIET, "escalateOn": ["unmapped"]})
    state, env = _run(repo)
    plan = _plan(layer, state, env)
    assert plan.depth == "full" and plan.asked == "targeted"
    assert plan.why == "escalated: unmapped path pkg/plugin.py: no selection strategy maps it to a test"


def test_plan_escalation_and_unknown_changes_run_full(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    (repo / "pkg" / "core.py").write_text("def total(p):\n    return 2\n", encoding="utf-8")
    layer = _layer(make_layer, repo, testing_ir={**_QUIET, "escalatePaths": ["pkg/core.py"]})
    state, env = _run(repo)
    plan = _plan(layer, state, env)
    assert plan.depth == "full" and plan.why == "escalated: core path pkg/core.py (pkg/core.py)"
    quiet = _layer(make_layer, repo, testing_ir={**_QUIET})
    unknown = testing.plan_check(quiet, state, env, STEP, "check", "", "")
    assert unknown.depth == "full" and unknown.why == "the changed paths could not be computed"


def _delivered_runs(layer, count: int, misses: int) -> list[str]:
    agents = Path(layer.layer_dir) / "agents"
    runs = [f"e00000{index:02d}" for index in range(count)]
    for run in runs:
        (agents / run).mkdir(parents=True)
        entry = {"check": "unit", "step": "unit", "phase": "check", "outcome": "passed", "depth": "targeted",
                 "testing": {"depth": "targeted", "strategies": {"changed-tests": 1, "imports": 2}, "seconds": 30}}
        (agents / run / "state.json").write_text(json.dumps({"adw_id": run, "adw": "demo", "started": f"2026-11-0{1 + count % 9}",
                                                            "check_results": [entry]}), encoding="utf-8")
    (agents / "testing").mkdir(parents=True, exist_ok=True)
    (agents / "testing" / "batches.jsonl").write_text(json.dumps(
        {"id": "b0000001", "branch": "main", "status": "red", "delivered": runs, "at": "2026-11-10T00:00:00+00:00"}) + "\n",
        encoding="utf-8")
    for run in runs[:misses]:
        testing.record_miss(layer, batch="b0000001", run=run, cmd="unit", strategies=["changed-tests", "imports"],
                            tests=["tests/test_plugin.py"], files=["pkg/plugin.py"])
    return runs


def test_miss_rate_above_threshold_falls_back_and_says_so(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    (repo / "pkg" / "core.py").write_text("def total(p):\n    return 3\n", encoding="utf-8")
    layer = _layer(make_layer, repo, testing_ir={**_QUIET})
    _delivered_runs(layer, 10, 2)
    rates = testing.miss_rates(layer)["unit"]
    assert (rates["targeted"], rates["misses"], rates["rate"], rates["fallback"]) == (10, 2, 20.0, True)
    assert rates["by_strategy"]["imports"] == {"targeted": 10, "misses": 2, "rate": 20.0}
    state, env = _run(repo)
    plan = _plan(layer, state, env)
    assert plan.depth == "full"
    assert plan.why == "miss rate 20% over the last 10 targeted runs is above 10%: running full"
    lenient = _layer(make_layer, repo, testing_ir={**_QUIET, "maxMissPercent": 25})
    assert testing.miss_rates(lenient)["unit"]["fallback"] is False
    assert _plan(lenient, state, env).depth == "targeted"


def test_learn_keeps_each_lesson_once(tmp_path, make_layer):
    layer = _layer(make_layer, _python_repo(tmp_path), testing_ir={**_QUIET})
    for _ in range(2):
        testing.learn(layer, "unit", ["pkg/plugin.py"], ["tests/test_plugin.py"], "b0000001")
    assert testing.learned(layer, "unit") == [{"files": ["pkg/plugin.py"], "tests": ["tests/test_plugin.py"],
                                              "batch": "b0000001"}]
    assert testing.learned(layer, "other") == []


# ---------------------------------------------------------------- accounting

def test_accounting_text(tmp_path, make_layer):
    layer = _layer(make_layer, _python_repo(tmp_path), testing_ir={**_QUIET})
    chosen = selection.Selection(tests=[f"tests/test_{n}.py" for n in range(14)],
                                 by_strategy={"changed-tests": 3, "imports": 9, "coverage": 2})
    plan = testing.CheckPlan(depth="targeted", asked="targeted", command={"run": "pytest"}, why="",
                             selection=chosen, units="test files")
    unknown = testing.accounting(plan, 41.2, "14 passed in 41.00s\n", "unit", layer)
    assert unknown["total"] is None and unknown["ran"] == 14 and unknown["selected"] == 14
    assert testing.accounting_text(unknown) == (
        "targeted: ran 14 tests (full suite size not known yet; changed-tests 3, imports 9, coverage 2): 41 s")
    testing.note_full_run(layer, "unit", 540, "800 passed, 12 skipped in 540.00s\n", "abc123")
    suites = json.loads((Path(layer.layer_dir) / "agents" / "testing" / "suites.json").read_text(encoding="utf-8"))
    assert suites["unit"]["tests"] == 812 and suites["unit"]["seconds"] == 540 and suites["unit"]["commit"] == "abc123"
    acc = testing.accounting(plan, 41.2, "14 passed in 41.00s\n", "unit", layer)
    assert acc["strategies"] == {"changed-tests": 3, "imports": 9, "coverage": 2}
    assert (acc["depth"], acc["total"], acc["full_seconds"]) == ("targeted", 812, 540)
    assert testing.accounting_text(acc) == (
        "targeted: ran 14 of 812 tests (changed-tests 3, imports 9, coverage 2): 41 s instead of about 9 min")
    no_log = testing.accounting(plan, 41.2, "nothing a parser knows\n", "unit", layer)
    assert no_log["ran"] == 14 and "ran the 14 selected test files" in testing.accounting_text(no_log)
    full = testing.CheckPlan(depth="full", asked="targeted", command={"run": "pytest"},
                             why="escalated: core path a/core/x.py (a/core/**)", selection=None, units="tests")
    assert testing.accounting_text(testing.accounting(full, 500, "", "unit", layer)) == (
        "full (escalated: core path a/core/x.py (a/core/**))")
    assert testing.accounting_text({"depth": "full", "asked": "full", "why": ""}) == "full"
    smoke = testing.CheckPlan(depth="smoke", asked="targeted", command={"run": "pytest"},
                              why="no test selected for a covered change", selection=None, units="tests")
    smoke_text = testing.accounting_text(testing.accounting(smoke, 3, "5 passed in 3.00s\n", "unit", layer))
    assert smoke_text.startswith("smoke") and "no test selected" in smoke_text and "5" in smoke_text
    for text in (smoke_text, testing.accounting_text(acc), testing.accounting_text(unknown)):
        assert "passed" not in text


def test_typical_minutes_per_level(tmp_path, make_layer):
    layer = _layer(make_layer, _python_repo(tmp_path), testing_ir={**_QUIET})
    assert testing.typical_minutes(layer) == {"light": None, "standard": None, "heavy": None}
    _delivered_runs(layer, 3, 0)
    testing.note_full_run(layer, "unit", 540, "812 passed in 540.00s\n", "abc")
    assert testing.typical_minutes(layer) == {"light": None, "standard": 0.5, "heavy": 9.0}


def test_explain_and_verify_lines_when_declared(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    cmds = [_cmd(select=["changed-tests", "coverage"], recordCoverage="al-json"),
            {"name": "lint", "run": "ruff check", "covers": [], "always": True, "reuse": True}]
    layer = load(make_layer({
        "codebase": {"root": str(repo), "commands": cmds},
        "adws": [{"name": "demo", "agent": "mock-agent", "review": "auto", "environment": ["worktree"],
                  "steps": [{"name": "unit", "kind": "check", "cmd": "unit"},
                            {"name": "lint", "kind": "check", "cmd": "lint"}]}],
        "testing": {**_QUIET, "batchEvery": 5, "batchAfterHours": 24}}))
    lines = testing.verify_lines(layer)
    assert lines[0].startswith("testing: targeted per run")
    assert any("every 5 landed changes" in line and "24 hours" in line for line in lines)
    assert any("lint" in line and "full form" in line for line in lines)
    assert any("unit" in line and "no coverage map" in line for line in lines)
    testing.write_setting(layer.layer_dir, "light")
    assert any("local setting light" in line for line in testing.verify_lines(layer))
    assert len(testing.explain_lines(layer)) >= len(lines)


# ---------------------------------------------------------------- failing tests in a log (skips.py)

def test_failing_tests_parsers():
    pytest_log = ("tests/test_a.py::test_x FAILED\n"
                  "=========================== short test summary info ============================\n"
                  "FAILED tests/test_a.py::test_x - AssertionError: no\n"
                  "FAILED tests/test_b.py::TestC::test_y[1] - KeyError\n"
                  "ERROR tests/test_c.py - ImportError\n"
                  "1 failed, 2 passed in 0.10s\n")
    assert skips.failing_tests(pytest_log) == ["tests/test_a.py::test_x", "tests/test_b.py::TestC::test_y[1]",
                                               "tests/test_c.py"]
    go_log = ("=== RUN   TestTotal\n    cart_test.go:7: wrong total\n--- FAIL: TestTotal (0.00s)\n"
              "=== RUN   TestTable\n    --- FAIL: TestTable/empty (0.00s)\n--- FAIL: TestTable (0.00s)\n"
              "FAIL\nFAIL\texample.org/shop/cart\t0.004s\nok  \texample.org/shop/price\t0.003s\nFAIL\n")
    assert skips.failing_tests(go_log) == ["example.org/shop/cart::TestTotal", "example.org/shop/cart::TestTable"]
    go_json = "\n".join(json.dumps(event) for event in (
        {"Action": "run", "Package": "example.org/shop/cart", "Test": "TestTotal"},
        {"Action": "fail", "Package": "example.org/shop/cart", "Test": "TestTotal"},
        {"Action": "fail", "Package": "example.org/shop/cart"}))
    assert skips.failing_tests(go_json) == ["example.org/shop/cart::TestTotal"]
    cargo_log = ("running 2 tests\ntest tests::adds ... ok\ntest tests::subtracts ... FAILED\n"
                 "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n")
    assert skips.failing_tests(cargo_log) == ["tests::subtracts"]
    jest_log = ("PASS src/b.test.ts\n FAIL  src/a.test.ts\n  ● add › adds\n\x1b[1m\x1b[31mFAIL\x1b[39m\x1b[22m test/c.spec.js\n"
                "Tests: 1 failed, 3 passed, 4 total\n")
    assert skips.failing_tests(jest_log) == ["src/a.test.ts", "test/c.spec.js"]
    assert skips.failing_tests("2 passed in 0.01s\n") == []
    assert skips.failing_tests("") == []
