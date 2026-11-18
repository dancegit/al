"""Full integration runs (integrate.main through runner.main, mock agent, real git, temporary
repositories). The application's checkout is on a side branch, so `main` is not checked out, except
where a test says otherwise."""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import integrate, ir, state as state_module
from integrate_fixtures import (BOTH_INDEX, BOTH_STOCK, REPORT, RUN_FILES, commit_on, conflict_layer, events_of, git,
                                handed_over_run, integrate_main, integration_runs, move_main, record_of,
                                resolve_script, review_script, state_of)

RUN = "f0000001"
CLEAN = "f0000002"
MOVED = ("ship-failed:git push . HEAD:refs/heads/main failed: non-fast-forward",)


def _scenario(make_layer, app_repo, **options):
    """The usual case: run f0000001 conflicts with what a0000003 landed on main since."""
    layer = conflict_layer(make_layer, app_repo, **options)
    state = handed_over_run(layer, app_repo, RUN)
    (layer / "agents" / "a0000003").mkdir()
    moved = move_main(app_repo)
    resolve_script(layer)
    return layer, state, moved


def _new(layer: Path) -> str:
    runs = integration_runs(layer)
    assert len(runs) == 1, runs
    return runs[0]


def _step(state: dict, name: str) -> dict:
    return next(step for step in state["steps"] if step["name"] == name)


def _main(app: Path) -> str:
    return git(app, "rev-parse", "refs/heads/main")


# ---------------------------------------------------------------- it lands

@pytest.mark.parametrize("main_checked_out", [False, True])
def test_conflict_in_source_and_generated_file_lands_and_settles(make_layer, app_repo, main_checked_out):
    layer, original, moved = _scenario(make_layer, app_repo, main_checked_out=main_checked_out)
    branch_head = git(app_repo, "rev-parse", f"refs/heads/{original['branch']}")
    code, out = integrate_main(layer, "run", RUN)
    assert code == 0, out
    new = _new(layer)
    state = state_of(layer, new)
    assert state["status"] == "passed" and state["adw"] == "integrate"
    assert [(s["name"], s["status"]) for s in state["steps"]] == [
        ("prepare", "passed"), ("resolve", "passed"), ("test", "passed"), ("review", "passed"), ("ship", "passed")], out
    info = state["integration"]
    assert (info["run"], info["branch"], info["tip"], info["target"], info["target_head"]) == (
        RUN, original["branch"], branch_head, "main", moved)
    assert info["workflow"] == "demo" and info["by"] == "cli" and info["clean"] is False
    assert [(c["file"], c["generated"], c["regenerate"]) for c in info["conflicts"]] == [
        ("INDEX.md", True, "./index.sh"), ("stock.py", False, "")]
    touched = info["conflicts"][1]["touched_by"]
    assert touched == [{"commit": moved[:7], "run": "a0000003", "summary": "feature: Add a tax [adw a0000003]"}]
    assert info["report"] == REPORT
    assert info["brief"] == f"specs/integrate-{new}.md" and state["plan"] == str(layer / info["brief"])
    brief = (layer / info["brief"]).read_text()
    assert f"# Integration of run {RUN} onto main" in brief and "reviewed" in brief
    assert f"`git diff {moved}`" in brief and f"`git diff {branch_head}`" in brief
    assert "Give stock a discount" in brief and f"specs/plan-{RUN}.md" in brief
    assert "one total() that takes both" in brief and "regenerated with `./index.sh`" in brief
    assert str(layer) not in brief and str(app_repo) not in brief
    assert "prepare: 2 conflicted files (1 generated); target main at " + moved[:7] in out
    assert f"integrate: run {RUN} onto main at {moved[:7]} (2 conflicted files: 1 resolved, 1 regenerated)" in out
    assert git(app_repo, "rev-parse", f"refs/heads/{original['branch']}") == branch_head, "the original branch moved"
    landed = state["ship"]["commit"]
    assert git(app_repo, "rev-list", "--parents", "-n", "1", landed).split() == [landed, moved]
    assert git(app_repo, "log", "-1", "--format=%s", landed) == (
        f"chore: Integrate run {RUN} (Give stock a discount) onto main [adw {new}]")
    assert git(app_repo, "show", f"{landed}:stock.py") + "\n" == BOTH_STOCK
    assert git(app_repo, "show", f"{landed}:INDEX.md") + "\n" == BOTH_INDEX
    for name in ("tests/test_stock.py", "tests/test_discount.py", "tests/test_tax.py", "shelf.py", "shop.py"):
        assert git(app_repo, "cat-file", "-t", f"{landed}:{name}") == "blob", name
    record = record_of(layer, RUN)
    if main_checked_out:     # the target is checked out: the result is staged, a person fast-forwards
        assert _main(app_repo) == moved and git(app_repo, "rev-parse", "refs/heads/al/ship/main") == landed
        assert record["status"] == "failed" and "git merge --ff-only al/ship/main" in record["why"]
        assert not (layer / "agents" / RUN / "settlement.json").exists()
        assert info["result"] == "staged"
        assert out.rstrip().endswith(f"{RUN} stays under Needs you")
        return
    assert _main(app_repo) == landed
    assert (record["run"], record["status"], record["commit"], record["target"], record["by"]) == (
        new, "landed", landed, "main", "cli")
    settlement = record_of(layer, RUN, "settlement.json")
    assert (settlement["as"], settlement["by"], settlement["via"]) == ("landed", new, "integrate")
    assert settlement["note"] == f"landed by integration run {new} in {landed[:7]} on main"
    assert info["result"] == "landed"
    assert out.rstrip().splitlines()[-1] == (
        f"integrate:  run {RUN} onto main: landed in {landed[:7]}; {RUN} settled as landed by {new}")
    types = {event["type"] for event in events_of(layer, new)}
    assert {"integrate.lock", "integrate.prepared", "integrate.resolved", "integrate.unlock", "integrate.result"} <= types
    assert git(app_repo, "status", "--porcelain") == "" and git(app_repo, "symbolic-ref", "--short", "HEAD") == "work"
    # the next integration is told what conflicted before
    code, out = integrate_main(layer, "conflicts")
    assert code == 0 and "stock.py  1 time" in out and "INDEX.md + stock.py  1 time" in out


def test_clean_branch_needs_no_agent(make_layer, app_repo):
    layer = conflict_layer(make_layer, app_repo)
    handed_over_run(layer, app_repo, CLEAN, {"prices.py": "PRICES = {}\n"}, reasons=MOVED, title="Add a price table")
    moved = move_main(app_repo)
    code, out = integrate_main(layer, "run", CLEAN)
    assert code == 0, out
    new = _new(layer)
    state = state_of(layer, new)
    assert state["status"] == "passed"
    assert _step(state, "resolve")["detail"] == "no conflicts: the branch applies cleanly"
    assert not [call for call in state["agent_calls"] if call["step"] == "resolve"]
    assert not (layer / "agents" / new / "resolve").exists()
    assert state["integration"]["clean"] is True and state["integration"]["conflicts"] == []
    assert "prepare: 0 conflicted files; target main at " + moved[:7] in out
    assert "(applies cleanly)" in out
    landed = _main(app_repo)
    assert git(app_repo, "rev-list", "--parents", "-n", "1", landed).split() == [landed, moved]
    assert git(app_repo, "show", f"{landed}:prices.py") == "PRICES = {}"
    assert record_of(layer, CLEAN, "settlement.json")["by"] == new


# ---------------------------------------------------------------- it does not land, and both runs say why

def _not_landed(layer: Path, app: Path, moved: str) -> tuple[dict, dict]:
    new = _new(layer)
    assert _main(app) == moved, "nothing may land"
    assert not (layer / "agents" / RUN / "settlement.json").exists()
    return state_of(layer, new), record_of(layer, RUN)


def test_undecidable_conflict_stops_and_both_runs_say_why(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    resolve_script(layer, files={"INDEX.md": BOTH_INDEX}, report={
        "resolved": [], "regenerated": [{"file": "INDEX.md", "command": "./index.sh"}],
        "undecidable": [{"file": "stock.py", "question": "is the discount taken before or after the tax?"}]})
    code, out = integrate_main(layer, "run", RUN)
    assert code == 1, out
    state, record = _not_landed(layer, app_repo, moved)
    assert state["status"] == "failed"
    detail = _step(state, "resolve")["detail"]
    assert "integrate:undecidable: stock.py: is the discount taken before or after the tax?" in detail
    assert state["integration"]["stopped"]["step"] == "resolve"
    assert state["integration"]["result"] == "failed"
    assert (record["status"], record["run"]) == ("failed", state["adw_id"])
    assert record["why"].startswith("stopped at resolve: integrate:undecidable: stock.py: is the discount")
    assert "not landed: stopped at resolve: integrate:undecidable" in out
    assert out.rstrip().endswith(f"{RUN} stays under Needs you")
    # the run is still handed over and can be integrated again once a person answered
    assert integrate.eligible(ir.load(layer), RUN).id == RUN


def test_markers_left_fail_the_resolve_step(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    resolve_script(layer, files={"INDEX.md": BOTH_INDEX})     # stock.py keeps its markers; the report claims it
    code, out = integrate_main(layer, "run", RUN)
    assert code == 1, out
    state, record = _not_landed(layer, app_repo, moved)
    assert "integrate:markers-remain: stock.py" in _step(state, "resolve")["detail"]
    assert "markers-remain" in record["why"]


def test_unreported_conflict_fails_the_resolve_step(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    resolve_script(layer, report={"resolved": [], "regenerated": [{"file": "INDEX.md", "command": "./index.sh"}],
                                  "undecidable": []})
    code, out = integrate_main(layer, "run", RUN)
    assert code == 1, out
    state, _ = _not_landed(layer, app_repo, moved)
    assert "integrate:markers-remain: stock.py" in _step(state, "resolve")["detail"]


def test_without_a_mock_script_nothing_is_resolved(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    (layer / "mock" / "integrate" / "resolve.json").unlink()
    code, out = integrate_main(layer, "run", RUN, "--dry-run")
    assert code == 1, out
    state, _ = _not_landed(layer, app_repo, moved)
    assert "integrate:markers-remain: INDEX.md, stock.py" in _step(state, "resolve")["detail"]


def test_dropped_test_fails_the_resolve_step(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    resolve_script(layer, delete=["tests/test_stock.py"])
    code, out = integrate_main(layer, "run", RUN)
    assert code == 1, out
    state, record = _not_landed(layer, app_repo, moved)
    assert ("integrate:markers-remain: a test of one side was dropped: tests/test_stock.py"
            in _step(state, "resolve")["detail"])
    assert "a test of one side was dropped" in record["why"]


def test_a_test_one_side_deleted_itself_may_stay_deleted(make_layer, app_repo):
    layer = conflict_layer(make_layer, app_repo)
    handed_over_run(layer, app_repo, RUN, {**RUN_FILES, "tests/test_stock.py": None})
    move_main(app_repo)
    resolve_script(layer)
    code, out = integrate_main(layer, "run", RUN)
    assert code == 0, out
    assert git(app_repo, "cat-file", "-t", "refs/heads/main:tests/test_stock.py", check=False) == ""


def test_agent_commit_is_refused(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    resolve_script(layer, commit=True)
    code, out = integrate_main(layer, "run", RUN)
    assert code == 1, out
    state, record = _not_landed(layer, app_repo, moved)
    assert "integrate:head-moved: the agent committed" in _step(state, "resolve")["detail"]
    assert "head-moved" in record["why"]


def test_rejected_review_of_the_integration_does_not_land(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    review_script(layer, success=False)
    code, out = integrate_main(layer, "run", RUN)
    assert code == 0, out
    state, record = _not_landed(layer, app_repo, moved)
    assert state["status"] == "handed-over" and _step(state, "review")["status"] == "rejected"
    assert state["integration"]["result"] == "pull-request"
    assert record["status"] == "failed"
    assert record["why"].startswith("the gate did not ship it: review-rejected")
    assert f"agents/{state['adw_id']}/PR.md" in record["why"]
    assert out.rstrip().endswith(f"{RUN} stays under Needs you")
    with pytest.raises(integrate.IntegrateError) as caught:     # an integration run is not integrated itself
        integrate.eligible(ir.load(layer), state["adw_id"])
    assert caught.value.reason == "not-handed-over"


def test_target_moves_again_before_ship_does_not_land(make_layer, app_repo, tmp_path):
    hook = tmp_path / "hook.sh"
    flag = tmp_path / "moved-again"
    tree = tmp_path / "again"
    hook.write_text(
        "#!/bin/sh\n"
        f"[ -e '{flag}' ] && exit 0\n"
        f"touch '{flag}'\n"
        f"git -C '{app_repo}' worktree add -q '{tree}' main || exit 9\n"
        f"printf 'def total(items, tax=0, fee=0):\\n    return sum(items) + tax + fee\\n' > '{tree}/stock.py'\n"
        f"git -C '{tree}' -c user.name=t -c user.email=t@example.org commit -qam 'main moves again' || exit 9\n"
        f"git -C '{app_repo}' worktree remove --force '{tree}'\n", encoding="utf-8")
    layer, _, moved = _scenario(make_layer, app_repo, test_run=f"sh {hook}")
    code, out = integrate_main(layer, "run", RUN)
    assert code == 0, out
    again = _main(app_repo)
    assert again != moved and git(app_repo, "log", "-1", "--format=%s", again) == "main moves again"
    new = _new(layer)
    state, record = state_of(layer, new), record_of(layer, RUN)
    assert state["status"] == "handed-over"
    assert any(reason.startswith("merge-conflict") for reason in state["ship"]["reasons"])
    assert record["status"] == "failed" and "the gate did not ship it: merge-conflict" in record["why"]
    assert not (layer / "agents" / RUN / "settlement.json").exists()


# ---------------------------------------------------------------- refused before any worktree

def _refused(layer: Path, app: Path, reason: str) -> str:
    before = (sorted(os.listdir(layer / "agents")), git(app, "for-each-ref"), git(app, "worktree", "list"))
    code, out = integrate_main(layer, "run", RUN)
    assert code == 3, out
    assert f"integrate: {reason}: " in out
    assert (sorted(os.listdir(layer / "agents")), git(app, "for-each-ref"), git(app, "worktree", "list")) == before
    assert not (layer / "trees").exists() or not os.listdir(layer / "trees")
    return out


def test_original_with_rejected_review_is_refused_before_any_worktree(make_layer, app_repo):
    layer = conflict_layer(make_layer, app_repo)
    handed_over_run(layer, app_repo, RUN, rejected=True)
    move_main(app_repo)
    _refused(layer, app_repo, "review-rejected")


def test_protected_path_is_refused(make_layer, app_repo, monkeypatch):
    layer = conflict_layer(make_layer, app_repo)
    handed_over_run(layer, app_repo, RUN, {**RUN_FILES, "deploy/release.cfg": "channel = beta\n"})
    moved = move_main(app_repo)
    resolve_script(layer)
    assert "deploy/release.cfg" in _refused(layer, app_repo, "protected-path")
    # were the check not there, the unchanged gate stops the same change
    real = integrate.eligible

    def lenient(layer_, run, *, me=""):
        codebase = {**layer_.ir["codebase"], "paths": {**layer_.ir["codebase"]["paths"], "protected": []}}
        return real(dataclasses.replace(layer_, ir={**layer_.ir, "codebase": codebase}), run, me=me)

    monkeypatch.setattr(integrate, "eligible", lenient)
    code, out = integrate_main(layer, "run", RUN)
    assert code == 0, out
    state, record = _not_landed(layer, app_repo, moved)
    assert "protected-path:deploy/release.cfg" in state["ship"]["reasons"]
    assert "protected-path:deploy/release.cfg" in record["why"]


def test_branch_changed_by_a_person_is_refused(make_layer, app_repo):
    layer = conflict_layer(make_layer, app_repo)
    state = handed_over_run(layer, app_repo, RUN)
    move_main(app_repo)
    commit_on(app_repo, state["branch"], {"note.txt": "a person was here\n"}, "a person's commit")
    out = _refused(layer, app_repo, "branch-changed")
    assert f"a person changed {state['branch']} since the run ended" in out


def test_switched_off_is_refused(make_layer, app_repo):
    layer = conflict_layer(make_layer, app_repo, settings={"enabled": False})
    handed_over_run(layer, app_repo, RUN)
    move_main(app_repo)
    assert "templates/integrate.json" in _refused(layer, app_repo, "off")
    code, out = integrate_main(layer, "preview", RUN)
    assert code == 3 and "integrate: off: " in out


# ---------------------------------------------------------------- one at a time

def _wait(condition, seconds: float = 90.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = condition()
        if found:
            return found
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_two_integrations_run_one_after_the_other(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    handed_over_run(layer, app_repo, CLEAN, {"prices.py": "PRICES = {}\n"}, reasons=MOVED, title="Add a price table",
                    base=git(app_repo, "rev-parse", "refs/heads/main~1"))
    for run_id in (RUN, CLEAN):
        assert integrate.run(layer, run_id, detach=True) == 0
    assert record_of(layer, RUN)["status"] in ("queued", "running", "landed")

    def ended():
        runs = integration_runs(layer)
        states = [state_of(layer, run_id) for run_id in runs]
        done = len(runs) == 2 and all(s["status"] != "running" and s.get("integration", {}).get("result") for s in states)
        return states if done else None

    states = _wait(ended)
    logs = {s["adw_id"]: (layer / "agents" / s["adw_id"] / "launch.log").read_text() for s in states}
    assert all(s["status"] == "passed" for s in states), logs
    for run_id in (RUN, CLEAN):
        assert record_of(layer, run_id)["status"] == "landed", logs
        assert record_of(layer, run_id, "settlement.json")["via"] == "integrate"
    spans = {}
    for state in states:
        found = {e["type"]: e["ts"] for e in events_of(layer, state["adw_id"]) if e["type"].startswith("integrate.")}
        spans[state["adw_id"]] = (found["integrate.lock"], found["integrate.unlock"])
    first, second = sorted(spans, key=lambda run_id: spans[run_id][0])
    assert spans[first][1] <= spans[second][0], f"both held the lock: {spans}"
    commits = {s["adw_id"]: s["ship"]["commit"] for s in states}
    assert _main(app_repo) == commits[second]
    assert git(app_repo, "rev-list", "--parents", "-n", "1", commits[second]).split() == [commits[second], commits[first]]
    assert git(app_repo, "rev-list", "--parents", "-n", "1", commits[first]).split() == [commits[first], moved]
    assert f"queued behind integration {first}" in (layer / "agents" / second / "run.log").read_text()
    assert any(e["type"] == "integrate.queued" and e["behind"] == first for e in events_of(layer, second))


def _held_lock(layer: Path, holder: str = "c0ffee00"):
    path = layer / integrate.LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(handle, fcntl.LOCK_EX)
    os.write(handle, f"{os.getpid()} {holder}\n".encode())
    return handle


def test_queued_request_rechecks_eligibility(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    handle = _held_lock(layer)
    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(code=integrate.main(layer, ["run", RUN, "--json"])))
    thread.start()
    try:
        new = _wait(lambda: next(iter(integration_runs(layer)), None))
        _wait(lambda: any(e["type"] == "integrate.queued" for e in events_of(layer, new))
              if (layer / "agents" / new / "events.jsonl").exists() else False)
        assert state_of(layer, new)["status"] == "running"
        assert integrate.running_integrations(ir.load(layer)) == [new]
        # meanwhile a person settles the original
        (layer / "agents" / RUN / "settlement.json").write_text(json.dumps({"as": "abandoned", "who": "Ada Example"}))
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    thread.join(60)
    assert result["code"] == 3
    state = state_of(layer, new)
    assert state["status"] == "failed" and state["steps"] == [] and state["ended"]
    assert "settled already" in state["integration"]["why"]
    record = record_of(layer, RUN)
    assert (record["run"], record["status"]) == (new, "failed") and "settled already" in record["why"]
    assert _main(app_repo) == moved and not (layer / "trees").exists()
    assert "queued behind integration c0ffee00" in (layer / "agents" / new / "run.log").read_text()


def test_second_request_for_same_run_is_refused(make_layer, app_repo):
    layer, _, _ = _scenario(make_layer, app_repo)
    spawned: list[list[str]] = []

    def spawn(argv, **popen):
        spawned.append(argv)
        return SimpleNamespace(pid=os.getpid())      # a process that is alive

    assert integrate.run(layer, RUN, detach=True, spawn=spawn) == 0
    new = _new(layer)
    assert spawned[0][1:] == [str(layer / "adws" / "integrate.py"), "run", RUN, "--adw-id", new, "--by", "cli"]
    record = record_of(layer, RUN)
    assert (record["run"], record["status"], record["target"]) == (new, "queued", "main")
    skeleton = state_of(layer, new)
    assert skeleton["status"] == "running" and skeleton["definition"]["name"] == "integrate"
    assert skeleton["integration"]["run"] == RUN and skeleton["prompt"].startswith(f"Integrate run {RUN} (Give stock")
    code, out = integrate_main(layer, "run", RUN)
    assert code == 3 and f"integrate: already-integrating: run {RUN} is being integrated by {new}" in out
    assert integrate.preview(ir.load(layer), RUN)["reason"] == "already-integrating"
    assert integrate.preview(ir.load(layer), RUN)["eligible"] is False
    # a launch that died is not in the way for ever
    skeleton["pid"], skeleton["heartbeat"] = 2 ** 22 + 12345, "2026-01-01T00:00:00+00:00"
    (layer / "agents" / new / "state.json").write_text(json.dumps(skeleton))
    assert integrate.eligible(ir.load(layer), RUN).id == RUN


# ---------------------------------------------------------------- a person approves

def test_approval_waits_then_ships(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo, approval="person")
    code, out = integrate_main(layer, "run", RUN)
    assert code == 75, out
    new = _new(layer)
    state = state_of(layer, new)
    assert state["status"] == "waiting" and _step(state, "approve")["status"] == "waiting"
    assert f"waiting for a person: al integrate LAYER {RUN} --approve" in _step(state, "approve")["detail"]
    assert _main(app_repo) == moved
    record = record_of(layer, RUN)
    assert (record["status"], record["run"]) == ("waiting", new)
    assert f"waiting for a person: al integrate LAYER {RUN} --approve" in out.rstrip().splitlines()[-1]
    assert f"resume:    uv run adws/integrate.py approve {RUN} --who NAME" in out
    assert Path(state["worktree"]).is_dir(), "the worktree waits with the run"
    code, out = integrate_main(layer, "approve", RUN)
    assert code == 2 and "--who" in out
    code, out = integrate_main(layer, "approve", new, "--who", "Ada Example")      # either id
    assert code == 0, out
    assert "approved by Ada Example" in out
    state = state_of(layer, new)
    assert state["status"] == "passed" and _step(state, "approve")["detail"] == "approved by Ada Example"
    assert state["integration"]["approval"]["required"] is True and state["integration"]["approval"]["by"] == "Ada Example"
    assert record_of(layer, new, "approval.json")["by"] == "Ada Example"
    assert _main(app_repo) == state["ship"]["commit"] != moved
    assert record_of(layer, RUN)["status"] == "landed"
    assert record_of(layer, RUN, "settlement.json")["by"] == new
    code, out = integrate_main(layer, "approve", RUN, "--who", "Ada Example")
    assert code == 3 and "integrate: not-waiting: " in out


# ---------------------------------------------------------------- the stand-in decider

@pytest.mark.parametrize("decider, mode, rejected, starts", [
    (True, "auto", False, True), (False, "auto", False, False), (True, "suggest", False, False),
    (True, "auto", True, False), (True, None, False, False)])
def test_decider_auto_starts_integration_when_allowed(make_layer, app_repo, capsys, monkeypatch, decider, mode,
                                                      rejected, starts):
    monkeypatch.delenv("AL_DECIDER", raising=False)
    layer = conflict_layer(make_layer, app_repo, settings={"decider": decider})
    handed_over_run(layer, app_repo, RUN, rejected=rejected)
    move_main(app_repo)
    spawned: list[list[str]] = []
    before = (layer / "agents" / RUN / "state.json").read_text()
    integrate.after_run(ir.load(layer), state_module.load(layer, RUN), SimpleNamespace(decider=mode),
                        spawn=lambda argv, **popen: spawned.append(argv) or SimpleNamespace(pid=0))
    out = capsys.readouterr().out
    assert (layer / "agents" / RUN / "state.json").read_text() == before, "the ended run is never changed"
    if not starts:
        assert spawned == [] and integration_runs(layer) == []
        assert ("decider: not integrated (review-rejected" in out) == (rejected and decider and mode == "auto")
        return
    new = _new(layer)
    assert spawned[0][1:] == [str(layer / "adws" / "integrate.py"), "run", RUN, "--adw-id", new, "--by", "decider"]
    assert f"[adw {RUN}] decider: integrating with an agent: run {new}" in out
    assert record_of(layer, RUN)["by"] == "decider" and record_of(layer, RUN)["status"] == "queued"


def test_decider_hook_never_raises(make_layer, app_repo, capsys):
    layer = conflict_layer(make_layer, app_repo)
    handed_over_run(layer, app_repo, RUN)
    (layer / "templates" / "integrate.json").write_text("{broken")
    integrate.after_run(ir.load(layer), state_module.load(layer, RUN), SimpleNamespace(decider="auto"))
    assert "the run stays with a person" in capsys.readouterr().out


# ---------------------------------------------------------------- never a force, never the original branch

def test_never_force_pushes_or_moves_the_original_branch(make_layer, app_repo, monkeypatch):
    layer, original, _ = _scenario(make_layer, app_repo)
    ref = f"refs/heads/{original['branch']}"
    before = git(app_repo, "rev-parse", ref)
    commands: list[list[str]] = []
    real = subprocess.run

    def recording(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            commands.append([str(a) for a in argv])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    code, out = integrate_main(layer, "run", RUN)
    monkeypatch.undo()
    assert code == 0, out
    assert len(commands) > 20, "the run's git commands were recorded"
    pushes = [c for c in commands if "push" in c]
    assert pushes, "the ship step pushes the result onto main"
    for command in pushes:
        assert not any(a in ("--force", "-f", "--force-with-lease", "--mirror", "--delete") or a.startswith("+")
                       for a in command), command
        assert original["branch"] not in " ".join(command), command
    for command in commands:
        if "update-ref" in command or ("branch" in command and {"-f", "-D", "-d", "-M", "-m"} & set(command)):
            assert original["branch"] not in " ".join(command), command
        assert not ("reset" in command and "--hard" in command), command
    assert git(app_repo, "rev-parse", ref) == before


def test_detached_start_refused_afterwards_says_why(make_layer, app_repo):
    layer, _, moved = _scenario(make_layer, app_repo)
    assert integrate.run(layer, RUN, detach=True, spawn=lambda argv, **popen: SimpleNamespace(pid=0)) == 0
    new = _new(layer)
    # a person settles the original before the detached process comes up
    (layer / "agents" / RUN / "settlement.json").write_text(json.dumps({"as": "abandoned", "who": "Ada Example"}))
    code, out = integrate_main(layer, "run", RUN, "--adw-id", new, "--by", "dashboard")
    assert code == 3 and "integrate: settled: " in out
    state = state_of(layer, new)
    assert state["status"] == "failed" and state["ended"] and "settled already" in state["integration"]["why"]
    record = record_of(layer, RUN)
    assert (record["run"], record["status"]) == (new, "failed") and "settled already" in record["why"]
    assert _main(app_repo) == moved
