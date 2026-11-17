"""`al integrate LAYER RUN|BRANCH`: an agent brings a handed-over run that could not merge onto its
target branch. On the scenario of kit/dashboard/tests/integrate_fixtures.py: an invented pantry with
real git history and a layer (mock agent) whose runs conflict with what landed on main since."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import KIT, al

sys.path.insert(0, str(KIT / "dashboard" / "tests"))
import integrate_fixtures as fx  # noqa: E402

RUN = fx.ACCEPTANCE


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("al-integrate") / "s"
    fx.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path) -> dict[str, Path]:
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return {"root": root, "repo": root / "pantry", "layer": root / "pantry.al"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def integrate(scenario, *args: str, ok: bool = False) -> subprocess.CompletedProcess:
    return al("integrate", scenario["layer"], *args, ok=ok)


def new_run(layer: Path) -> str:
    runs = fx.integration_runs(layer)
    assert runs, "no integration run"
    return runs[-1]


def test_al_integrate_lands_run_and_settles_original(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    before = git(repo, "rev-parse", "refs/heads/main")
    branch = git(repo, "rev-parse", f"refs/heads/{fx.branch_of(RUN)}")
    cp = integrate(scenario, RUN)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    new = new_run(layer)
    landed = git(repo, "rev-parse", "refs/heads/main")
    lines = cp.stdout.rstrip().splitlines()
    assert lines[-1] == f"integrate:  run {RUN} onto main: landed in {landed[:7]}; {RUN} settled as landed by {new}"
    assert f"integrate: run {RUN} onto main at {before[:7]} (2 conflicted files: 1 resolved, 1 regenerated)" in cp.stdout
    assert "prepare: 2 conflicted files (1 generated); target main at " + before[:7] in cp.stdout
    for step in ("prepare (integrate)", "resolve (integrate)", "test (check)", "review (review)", "ship (ship)"):
        assert f"step {step} passed" in cp.stdout, step
    assert git(repo, "rev-list", "--parents", "-n", "1", landed).split() == [landed, before]
    assert git(repo, "log", "-1", "--format=%s", landed) == (
        f"chore: Integrate run {RUN} (Give stock a discount) onto main [adw {new}]")
    assert git(repo, "show", f"{landed}:stock.py") + "\n" == fx.BOTH_STOCK
    assert git(repo, "rev-parse", f"refs/heads/{fx.branch_of(RUN)}") == branch, "the run's branch is not changed"
    listed = al("runs", layer, "--view", "landed").stdout
    row = next(line for line in listed.splitlines() if line.startswith(RUN))
    assert f"landed by {new} (integration)" in row
    needs = al("runs", layer).stdout
    assert needs.splitlines()[0].startswith("4 need you") and not re.search(rf"^{RUN}\b", needs, re.M)
    rows = json.loads(al("runs", layer, "--view", "landed", "--json").stdout)
    original = next(r for r in rows if r["id"] == RUN)
    assert original["landed_by"] == new and original["integration"]["status"] == "landed"
    assert original["integration"]["commit"] == landed and original["settled"]["via"] == "integrate"


def test_al_integrate_by_branch_name(scenario):
    cp = integrate(scenario, fx.branch_of(fx.CLEAN))
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "prepare: 0 conflicted files; target main at " in cp.stdout
    assert "step resolve (integrate) passed: no conflicts: the branch applies cleanly" in cp.stdout
    new = new_run(scenario["layer"])
    assert re.search(rf"^integrate:  run {fx.CLEAN} onto main: landed in [0-9a-f]{{7}}; {fx.CLEAN} settled as landed "
                     rf"by {new}$", cp.stdout, re.M)
    assert git(scenario["repo"], "show", "refs/heads/main:prices.py") == "PRICES = {}"


def test_al_integrate_preview_prints_plan_and_changes_nothing(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    before = (git(repo, "for-each-ref"), sorted(p.name for p in (layer / "agents").iterdir()),
              git(repo, "status", "--porcelain"), git(repo, "worktree", "list"))
    cp = integrate(scenario, RUN, "--preview")
    assert cp.returncode == 0, cp.stderr
    main, tip = git(repo, "rev-parse", "refs/heads/main"), git(repo, "rev-parse", f"refs/heads/{fx.branch_of(RUN)}")
    assert cp.stdout.splitlines() == [
        f"run:        {RUN} (Give stock a discount)",
        f"branch:     {fx.branch_of(RUN)} at {tip[:7]}",
        f"target:     main at {main[:7]}",
        "conflicts:  2: INDEX.md (generated), stock.py",
        "steps:      prepare, resolve, test, review, ship",
        "approval:   none"]
    found = json.loads(integrate(scenario, RUN, "--preview", "--json").stdout)
    assert found["eligible"] is True and found["conflicts"] == ["INDEX.md", "stock.py"] and found["clean"] is False
    clean = integrate(scenario, fx.CLEAN, "--preview")
    assert "conflicts:  none: the branch applies cleanly" in clean.stdout
    assert (git(repo, "for-each-ref"), sorted(p.name for p in (layer / "agents").iterdir()),
            git(repo, "status", "--porcelain"), git(repo, "worktree", "list")) == before


@pytest.mark.parametrize("run, reason, words", [
    (fx.REJECTED, "review-rejected", "the review of run f0000003 rejected it"),
    (fx.PROTECTED, "protected-path", "deploy/release.cfg"),
    (fx.CHANGED, "branch-changed", "a person changed chore/f0000005-round-the-total since the run ended"),
    (fx.LANDED, "not-a-conflict", ""),
    ("deadbeef", "unknown-run", "deadbeef"),
])
def test_al_integrate_refusals_exit_3(scenario, run, reason, words):
    layer, repo = scenario["layer"], scenario["repo"]
    if run == fx.LANDED:     # settled by a person meanwhile
        al("settle", layer, run, "--as", "landed", "--who", "Ada Example")
        reason = "settled"
    before = (git(repo, "for-each-ref"), sorted(p.name for p in (layer / "agents").iterdir()))
    cp = integrate(scenario, run)
    assert cp.returncode == 3, cp.stdout + cp.stderr
    assert cp.stderr.startswith(f"al integrate: {reason}: ") and words in cp.stderr
    assert cp.stdout == ""
    assert (git(repo, "for-each-ref"), sorted(p.name for p in (layer / "agents").iterdir())) == before
    preview = integrate(scenario, run, "--preview")
    assert preview.returncode == 3 and preview.stderr.startswith(f"al integrate: {reason}: ")


def _wait_landed(layer: Path, run: str, seconds: float = 120.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        path = layer / "agents" / run / "integration.json"
        if path.is_file():
            record = json.loads(path.read_text())
            if record["status"] not in ("queued", "running"):
                return record
        time.sleep(0.3)
    raise AssertionError("the detached integration did not end")


def test_al_integrate_detach_prints_run_id(scenario):
    layer = scenario["layer"]
    cp = integrate(scenario, RUN, "--detach")
    assert cp.returncode == 0, cp.stderr
    match = re.fullmatch(rf"started integration run ([0-9a-f]{{8}}) of {RUN}; log agents/\1/launch\.log\n", cp.stdout)
    assert match, cp.stdout
    new = match.group(1)
    again = integrate(scenario, RUN)       # a second request while the first one runs (or after it landed)
    assert again.returncode == 3 and re.match(r"al integrate: (already-integrating|settled): ", again.stderr)
    record = _wait_landed(layer, RUN)
    assert (record["run"], record["status"]) == (new, "landed"), (layer / "agents" / new / "launch.log").read_text()
    assert "=== ADW summary ===" in (layer / "agents" / new / "launch.log").read_text()
    answer = json.loads(integrate(scenario, fx.CLEAN, "--detach", "--json").stdout)
    assert answer["status"] == "started" and answer["original"] == fx.CLEAN
    assert _wait_landed(layer, fx.CLEAN)["status"] == "landed"


def test_al_integrate_conflicts_report(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    assert integrate(scenario, "--conflicts").stdout == "no integration had a conflict yet\n"
    base = git(repo, "rev-list", "--max-parents=0", "main")
    fx.add_conflicting_run(layer, repo, "f0000008", "Weigh the stock", {
        "stock.py": "def total(items, scale=1):\n    return sum(items) * scale\n", "scale.py": "SCALE = 1\n",
        "INDEX.md": None}, base)
    for run in (RUN, "f0000008"):
        cp = integrate(scenario, run)
        assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "conflicted before: INDEX.md (1 time), stock.py (1 time)" in cp.stdout
    assert "conflicted before: INDEX.md, stock.py" in cp.stdout
    second = new_run(layer)
    assert "## Conflicted before" in (layer / "specs" / f"integrate-{second}.md").read_text()
    out = integrate(scenario, "--conflicts").stdout
    assert out.splitlines() == [
        "2 integrations had conflicts", "files:", "  INDEX.md  2 times  (generated)", "  stock.py  2 times",
        "together:", "  INDEX.md + stock.py  2 times",
        "a file that conflicts often: split it, or mark it generated in templates/integrate.json"]
    report = json.loads(integrate(scenario, "--conflicts", "--json").stdout)
    assert report == {"integrations": 2,
                      "files": [{"file": "INDEX.md", "times": 2, "generated": True},
                                {"file": "stock.py", "times": 2, "generated": False}],
                      "pairs": [{"files": ["INDEX.md", "stock.py"], "times": 2}]}


def test_al_integrate_set_and_reset_settings(scenario):
    layer = scenario["layer"]
    path = layer / "templates" / "integrate.json"
    shown = integrate(scenario)
    assert shown.returncode == 0
    assert shown.stdout.splitlines()[:4] == [
        "integrate: enabled (templates/integrate.json)", "approval:  none", "decider:   may not start it",
        "generated: INDEX.md (./index.sh)"]
    assert "usage: al integrate LAYER RUN|BRANCH" in shown.stdout
    cp = integrate(scenario, "--set", "approval=person", "--set", "decider=true")
    assert cp.returncode == 0, cp.stderr
    assert "approval:  a person approves before it ships" in cp.stdout and "decider:   may start it (mode auto)" in cp.stdout
    saved = json.loads(path.read_text())
    assert saved == {"version": 1, "enabled": True, "approval": "person", "decider": True,
                     "generated": [{"path": "INDEX.md", "regenerate": "./index.sh"}]}
    off = integrate(scenario, "--set", "enabled=false")
    assert "integrate: switched off (templates/integrate.json)" in off.stdout
    refused = integrate(scenario, RUN)
    assert refused.returncode == 3 and refused.stderr.startswith("al integrate: off: ")
    for bad, words in (("colour=red", "KEY one of enabled, approval, decider"), ("approval=maybe", "templates/integrate.json"),
                       ("enabled=perhaps", "templates/integrate.json"), ("generated=x", "edited in templates/integrate.json")):
        cp = integrate(scenario, "--set", bad)
        assert cp.returncode == 2 and words in cp.stderr, (bad, cp.stderr)
    assert json.loads(path.read_text())["enabled"] is False, "a refused --set writes nothing"
    assert integrate(scenario, "--set", "enabled=true", "--reset").returncode == 2
    assert integrate(scenario, RUN, "--conflicts").returncode == 2
    reset = integrate(scenario, "--reset")
    assert reset.returncode == 0 and not path.exists()
    assert reset.stdout.splitlines() == ["integrate: enabled (templates/integrate.json)", "approval:  none",
                                         "decider:   may not start it", "generated: none named"]
    assert json.loads(integrate(scenario, "--json").stdout)["generated"] == []


def test_al_integrate_model_set_reaches_the_resolve_step(scenario):
    layer = scenario["layer"]
    cp = integrate(scenario, RUN, "--model-set", "base")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    state = fx.state_of(layer, new_run(layer))
    resolve = next(step for step in state["steps"] if step["name"] == "resolve")
    assert resolve["tier"] == "base"
    call = next(c for c in state["agent_calls"] if c["step"] == "resolve")
    assert call["tier"] == "base" and state["overrides"]["tier"] == "base"
    assert (layer / "agents" / state["adw_id"] / "resolve" / "prompt.md").read_text().count("CONFLICTS: [{") == 1


def test_al_integrate_default_tier_is_heavy(scenario):
    layer = scenario["layer"]
    assert integrate(scenario, RUN).returncode == 0
    state = fx.state_of(layer, new_run(layer))
    assert next(c for c in state["agent_calls"] if c["step"] == "resolve")["tier"] == "heavy"


def test_al_integrate_approval_waits_for_a_person(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    integrate(scenario, "--set", "approval=person", ok=True)
    before = git(repo, "rev-parse", "refs/heads/main")
    cp = integrate(scenario, RUN)
    assert cp.returncode == 75, cp.stdout + cp.stderr
    new = new_run(layer)
    assert cp.stdout.rstrip().splitlines()[-1] == (
        f"integrate:  run {RUN} onto main: waiting for a person: al integrate LAYER {RUN} --approve")
    assert git(repo, "rev-parse", "refs/heads/main") == before
    needs = al("runs", layer).stdout
    row = next(line for line in needs.splitlines() if line.startswith(RUN))
    assert f"integration {new} waits for approval" in row and f"→ approve integration {new}" in needs
    cp = integrate(scenario, RUN, "--approve", "--who", "Ada Example")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "approved by Ada Example" in cp.stdout
    assert re.search(rf"^integrate:  run {RUN} onto main: landed in [0-9a-f]{{7}}; {RUN} settled as landed by {new}$",
                     cp.stdout, re.M)
    assert git(repo, "rev-parse", "refs/heads/main") != before
    again = integrate(scenario, RUN, "--approve", "--who", "Ada Example")
    assert again.returncode == 3 and again.stderr.startswith("al integrate: not-waiting: ")


def test_al_runs_next_names_al_integrate(scenario):
    layer = scenario["layer"]
    text = al("runs", layer).stdout
    lines = text.splitlines()
    for run in (RUN, fx.CLEAN):
        at = next(i for i, line in enumerate(lines) if line.startswith(run))
        assert lines[at + 1].strip() == (f"→ merge {fx.branch_of(run)} into main, or al integrate {layer} {run}, "
                                         f"or al settle {layer} {run} --as …")
    at = next(i for i, line in enumerate(lines) if line.startswith(fx.REJECTED))
    assert "al integrate" not in lines[at + 1]


def test_layer_generated_before_integrate_says_run_al_gen(scenario):
    layer = scenario["layer"]
    (layer / "adws" / "integrate.py").unlink()
    cp = integrate(scenario, RUN)
    assert cp.returncode != 0 and "was generated before `al integrate`: run `al gen" in cp.stderr
    text = al("runs", layer).stdout
    assert "al integrate" not in text, "a layer without the script is not offered it"
