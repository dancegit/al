"""Whole runs given to an agent (assign.main through integrate.run / runner.main, mock agent, real
git, temporary repositories): every row of the table of adw_modules/assign.py that starts something."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from adw_modules import assign, integrate, ir, state as state_module
from assign_fixtures import (BLOCKER, FIXED_BOTH, FIXED_STOCK, LOGIN, PERSON_BLOCKER, PLAN_COMMANDS, PROMPT, RUN,
                             assign_main, conflict_run, failed_check_run, fix_scripts, login_run, nothing_changed_run,
                             person_run, proof_run, record, rejected_run, reviewers, script)
from integrate_fixtures import (events_of, git, handed_over_run, integration_runs, record_of, resolve_script, state_of)


def _main(app: Path) -> str:
    return git(app, "rev-parse", "refs/heads/main")


def _steps(state: dict) -> list[tuple[str, str]]:
    return [(step["name"], step["status"]) for step in state["steps"]]


def _prompt_of(layer: Path, run: str, step: str) -> str:
    return (layer / "agents" / run / step / "prompt.md").read_text(encoding="utf-8")


def _attempts(layer: Path, run: str = RUN) -> list[dict]:
    return assign.attempts(ir.load(layer), run)


def test_blockers_land_through_the_gate(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo)
    before = _main(app_repo)
    branch = state_of(layer, run)["branch"]
    branch_head = git(app_repo, "rev-parse", f"refs/heads/{branch}")
    fix_scripts(layer)
    code, out = assign_main(layer, "give", run, "--note", "keep the signature")
    assert code == 0, out
    [new] = integration_runs(layer)
    state = state_of(layer, new)
    assert state["status"] == "passed" and state["ship"]["shipped"] is True, out
    assert _steps(state) == [("prepare", "passed"), ("resolve", "passed"), ("address", "passed"), ("test", "passed"),
                             ("review", "passed"), ("ship", "passed")]
    assert len(state["review"]["rounds"]) == 1, "the review ran exactly one round"
    info = state["integration"]
    assert (info["mode"], info["run"], info["result"], info["address"]["blockers"]) == ("blockers", run, "landed", 1)
    assert state["prompt"].startswith(f"Address the review of run {run} (Give stock a discount) onto main")
    request = _prompt_of(layer, new, "address-patch")
    assert f"- {BLOCKER['description']}\n  Resolution: {BLOCKER['resolution']}" in request
    assert "The person adds: keep the signature" in request
    brief = (layer / info["brief"]).read_text()
    assert f"Run {run} was reviewed and rejected. This run addressed the blockers below" in brief
    assert BLOCKER["description"] in brief and "keep the signature" in brief and str(layer) not in brief
    landed = _main(app_repo)
    assert landed != before and git(app_repo, "show", f"{landed}:stock.py") + "\n" == FIXED_STOCK
    assert git(app_repo, "rev-parse", f"refs/heads/{branch}") == branch_head, "the original branch is never changed"
    settlement = record_of(layer, run, "settlement.json")
    assert (settlement["as"], settlement["by"], settlement["via"]) == ("landed", new, "integrate")
    [attempt] = _attempts(layer)
    assert (attempt["kind"], attempt["by"], attempt["run"], attempt["outcome"], attempt["reason"]) == (
        "blockers", "cli", new, "landed", "review:blockers")
    assert attempt["note"] == "keep the signature"
    types = [event["type"] for event in events_of(layer, run)]
    assert types.count("assign.attempt") == 1 and types.count("assign.outcome") == 1
    _attempts(layer)
    assert [event["type"] for event in events_of(layer, run)].count("assign.outcome") == 1, "emitted once"
    assert f"blockers of run {run} onto main" in out
    assert f"run {run} given to an agent (blockers): run {new}; attempt 1" in out
    found = assign.plan(ir.load(layer), run)
    assert (found["kind"], found["blocked"]) == ("none", "settled")


def test_blockers_rejected_again_stays_with_the_person_with_history(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo)
    before = _main(app_repo)
    fix_scripts(layer, passes=False)
    code, out = assign_main(layer, "give", run)
    assert code == 0, out
    [new] = integration_runs(layer)
    state = state_of(layer, new)
    assert state["status"] == "handed-over" and state["ship"]["shipped"] is False
    assert len(state["review"]["rounds"]) == 1 and "patch" not in state["review"]["rounds"][0]
    assert not any(step["name"].startswith("patch-") for step in state["steps"])
    assert not (layer / "agents" / new / "patch-1").exists(), "no patch round ran"
    assert not [call for call in state["agent_calls"] if call["step"] in ("patch-1", "implement-1")]
    assert _main(app_repo) == before, "nothing merged into main"
    assert state_of(layer, run)["status"] == "handed-over"
    assert not (layer / "agents" / run / "settlement.json").exists()
    failed = record_of(layer, run)
    assert failed["status"] == "failed" and failed["why"].startswith("its review rejected it again: ")
    [attempt] = _attempts(layer)
    assert (attempt["outcome"], attempt["after"], attempt["reason"]) == ("stopped", "review:blockers", "review:blockers")
    assert attempt["why"].startswith("its review rejected it again")
    # the second time the attempt run is the source: its branch (with the first fix) and its blockers
    found = assign.plan(ir.load(layer), run)
    assert (found["kind"], found["source"], found["blocked"], found["left"]) == ("blockers", new, "", 1)
    assert found["integrate"]["branch"] == state["branch"] and found["integrate"]["tip"] == state["ship"]["commit"]
    spawned = []
    result = assign.give(layer, run, by="dashboard", detach=True, spawn=lambda argv, **kw: spawned.append(argv))
    second = state_of(layer, result["run_id"])
    assert second["definition"]["integration"]["source"] == new
    assert second["definition"]["integration"]["branch"] == state["branch"]
    assert spawned[0][1:] == [str(layer / "adws" / "integrate.py"), "run", run, "--adw-id", result["run_id"],
                              "--by", "dashboard"]
    assert assign.plan(ir.load(layer), run)["blocked"] == "running"


def test_blockers_with_a_moved_target_in_one_run(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo, moved=True)
    moved = _main(app_repo)
    resolve_script(layer, files={"stock.py": "def total(items, discount=0, tax=0):\n"
                                             "    return sum(items) - discount + tax\n"},
                   report={"resolved": [{"file": "stock.py", "sides": "a discount and a tax",
                                         "resolution": "one total() that takes both"}],
                           "regenerated": [], "undecidable": []})
    fix_scripts(layer, stock=FIXED_BOTH)
    code, out = assign_main(layer, "give", run)
    assert code == 0, out
    [new] = integration_runs(layer)
    state = state_of(layer, new)
    assert state["status"] == "passed", out
    assert dict(_steps(state))["resolve"] == "passed" and dict(_steps(state))["address"] == "passed"
    assert [call["step"] for call in state["agent_calls"]][:3] == ["resolve", "address-patch", "address-implement"]
    assert state["integration"]["clean"] is False
    landed = _main(app_repo)
    assert git(app_repo, "rev-list", "--parents", "-n", "1", landed).split() == [landed, moved]
    assert git(app_repo, "show", f"{landed}:stock.py") + "\n" == FIXED_BOTH
    assert git(app_repo, "cat-file", "-t", f"{landed}:shop.py") == "blob"
    assert _attempts(layer)[0]["outcome"] == "landed"


def test_proof_found_is_collected_without_code_change(make_layer, app_repo):
    layer, run = proof_run(make_layer, app_repo, found=True)
    original = state_of(layer, run)
    reviewers(layer, adw="integrate")
    code, out = assign_main(layer, "give", run)
    assert code == 0, out
    [new] = integration_runs(layer)
    state = state_of(layer, new)
    assert state["status"] == "passed", out
    assert _steps(state) == [("prepare", "passed"), ("resolve", "passed"), ("proof", "passed"), ("test", "passed"),
                             ("review", "passed"), ("ship", "passed")]
    assert not [call for call in state["agent_calls"] if call["step"].startswith("address")], "no address step ran"
    manifest = json.loads((layer / "artefacts" / new / "manifest.json").read_text())
    found = [item for item in manifest if item["id"] == "found-image"]
    assert [(item["type"], item["step"]) for item in found] == [("image", "proof")]
    assert found[0]["path"].endswith("found-image/proof/shelf.png")
    assert state["integration"]["proof"] == {"found": ["proof/shelf.png"], "collected": 1}
    assert state["prompt"].startswith(f"Collect proof for run {run}")
    landed = git(app_repo, "rev-parse", "refs/heads/main")
    assert git(app_repo, "rev-parse", f"{landed}^{{tree}}") == git(app_repo, "rev-parse", f"{original['ship']['commit']}^{{tree}}"), \
        "the tree is the original branch applied on the target: no code change"
    brief = (layer / state["integration"]["brief"]).read_text()
    assert "rejected only for missing proof. No code was changed." in brief and "`proof/shelf.png`" in brief
    assert _attempts(layer)[0]["kind"] == "proof" and _attempts(layer)[0]["outcome"] == "landed"


def test_proof_missing_runs_the_plan_commands_then_reviews(make_layer, app_repo):
    layer, run = proof_run(make_layer, app_repo, found=False)
    script(layer, "address-patch", "integrate", output="plan: run the two commands")
    script(layer, "address-implement", "integrate", raw_files={"shots/shelf.png": "the shelf\n"}, output="ran them")
    reviewers(layer, adw="integrate")
    code, out = assign_main(layer, "give", run)
    assert code == 0, out
    [new] = integration_runs(layer)
    state = state_of(layer, new)
    assert state["status"] == "passed", out
    assert [name for name, _ in _steps(state)] == ["prepare", "resolve", "address", "proof", "test", "review", "ship"]
    request = _prompt_of(layer, new, "address-patch")
    assert "The review rejected this change only because proof is missing" in request
    assert "Change no application code." in request and "shots/*.png" in request
    assert all(f"- {command}" in request for command in PLAN_COMMANDS)
    assert [Path(item["path"]).name for item in state["artefacts"] if item["id"] == "shots"] == ["shelf.png"]
    assert state["integration"]["proof"]["collected"] == 1 and len(state["review"]["rounds"]) == 1
    assert _attempts(layer)[0]["outcome"] == "landed"


def test_proof_round_that_changes_code_fails(make_layer, app_repo):
    layer, run = proof_run(make_layer, app_repo, found=False)
    before = _main(app_repo)
    script(layer, "address-implement", "integrate", output="edited the code instead",
           raw_files={"shots/shelf.png": "the shelf\n", "stock.py": "def total(items):\n    return 0\n"})
    code, out = assign_main(layer, "give", run)
    assert code == 1, out
    [new] = integration_runs(layer)
    state = state_of(layer, new)
    failed = next(step for step in state["steps"] if step["status"] == "failed")
    assert failed["name"] == "address"
    assert "integrate:markers-remain: the proof round changed code: stock.py" in failed["detail"]
    assert _main(app_repo) == before and state_of(layer, run)["status"] == "handed-over"
    [attempt] = _attempts(layer)
    assert (attempt["outcome"], attempt["after"]) == ("stopped", "step:address")


def test_failed_check_resumes_in_place(make_layer, app_repo):
    layer, run = failed_check_run(make_layer, app_repo)
    script(layer, "test-resolve-1", delete=["broken.flag"], output="removed what the build left")
    reviewers(layer)
    code, out = assign_main(layer, "give", run)
    assert code == 0, out
    state = state_of(layer, run)
    assert state["status"] == "passed" and state["ship"]["shipped"] is True
    assert integration_runs(layer) == [], "the same run continued: no new run"
    calls = [call for call in state["agent_calls"] if call["step"] == "test-resolve-1"]
    assert calls[-1]["tier"] == "heavy" and calls[-1]["tier_from"] != "command"
    assert state["overrides"]["tier"] == "heavy"
    [attempt] = _attempts(layer)
    assert (attempt["kind"], attempt["run"], attempt["outcome"], attempt["reason"]) == ("check", run, "passed",
                                                                                        "check:test")
    assert "resumed in place" in out
    assert assign.plan(ir.load(layer), run)["blocked"] == "not-stopped"


def test_login_failure_resumes_unchanged(make_layer, app_repo):
    layer, run = login_run(make_layer, app_repo)
    prompt = state_of(layer, run)["prompt"]
    assert LOGIN in state_of(layer, run)["steps"][1]["detail"]
    script(layer, "build", raw_files={"stock.py": FIXED_STOCK})          # the login works again
    reviewers(layer)
    code, out = assign_main(layer, "give", run)
    assert code == 0, out
    state = state_of(layer, run)
    assert state["status"] == "passed"
    assert state["prompt"] == prompt == PROMPT, "no note was given: the prompt is unchanged"
    assert not state["overrides"].get("tier")
    assert [step["attempts"] for step in state["steps"] if step["name"] in ("plan", "build")] == [1, 2]
    assert _attempts(layer)[0]["kind"] == "resume" and _attempts(layer)[0]["outcome"] == "passed"


def test_a_note_goes_into_the_resumed_prompt(make_layer, app_repo):
    layer, run = login_run(make_layer, app_repo)
    spawned = []
    result = assign.give(layer, run, note="mind the tax", detach=True, spawn=lambda argv, **kw: spawned.append((argv, kw)))
    state = state_of(layer, run)
    assert state["prompt"] == f"{PROMPT}\n\n## Given to an agent (attempt 1)\nmind the tax"
    assert state["status"] == "running" and [s["status"] for s in state["steps"]] == ["passed", "pending"]
    argv, kw = spawned[0]
    assert argv[1:] == [str(layer / "adws" / "adw_demo.py"), "--resume", "--adw-id", run, "--decider", "off"]
    assert kw["start_new_session"] is True and kw["cwd"] == str(layer)
    assert result == {"status": "started", "kind": "resume", "run": run, "run_id": run, "attempt": 1}
    assert assign.plan(ir.load(layer), run)["blocked"] == "running"


def test_nothing_changed_starts_again(make_layer, app_repo, monkeypatch):
    layer, run = nothing_changed_run(make_layer, app_repo)
    spawned = []
    monkeypatch.setattr(assign, "_spawn", lambda argv, **kw: spawned.append(argv))
    code, out = assign_main(layer, "give", run, "--detach", "--json")
    assert code == 0, out
    result = json.loads(out.strip().splitlines()[-1])
    new = result["run_id"]
    assert result["kind"] == "start-again" and new != run
    assert state_of(layer, new)["replaces"] == run and state_of(layer, new)["adw"] == "demo"
    assert (layer / "agents" / new / "request.md").read_text().strip() == PROMPT
    argv = spawned[0]
    assert argv[argv.index("--replaces") + 1] == run and argv[argv.index("--adw-id") + 1] == new
    assert record(layer)["attempts"][0]["run"] == new and record(layer)["attempts"][0]["kind"] == "start-again"


def test_start_again_keeps_the_class_label(make_layer, app_repo, monkeypatch):
    from adw_modules import runner

    labelled = "# BUGFIX run for the demo layer\n\nThe total ignores a negative discount."
    layer, run = nothing_changed_run(make_layer, app_repo, prompt=labelled)
    first = state_of(layer, run)
    assert (first["klass"], first["class_fixed"], first["class_from"]) == ("bug", True, "request-label")
    spawned = []
    monkeypatch.setattr(assign, "_spawn", lambda argv, **kw: spawned.append(argv))
    code, out = assign_main(layer, "give", run, "--detach", "--json")
    assert code == 0, out
    result = json.loads(out.strip().splitlines()[-1])
    new = result["run_id"]
    assert result["kind"] == "start-again" and new != run
    request = (layer / "agents" / new / "request.md").read_text()
    assert request.strip() == labelled, "the request is reused verbatim: the label is still its first line"
    assert runner.request_label(request)[0] == "bug"
    argv = spawned[0]
    assert argv[argv.index("--class") + 1] == "bug" and argv[argv.index("--replaces") + 1] == run


def test_stand_in_gives_a_restarted_request_once_over_the_runs_it_replaces(make_layer, app_repo, monkeypatch):
    layer, run = nothing_changed_run(make_layer, app_repo)
    spawned = []
    monkeypatch.setattr(assign, "_spawn", lambda argv, **kw: spawned.append(argv))
    code, out = assign_main(layer, "give", run, "--by", "decider", "--detach", "--json")
    assert code == 0, out
    result = json.loads(out.strip().splitlines()[-1])
    new = result["run_id"]
    assert result["kind"] == "start-again" and new != run
    assert state_of(layer, new)["replaces"] == run
    [attempt] = record(layer)["attempts"]
    assert (attempt["by"], attempt["kind"], attempt["run"]) == ("decider", "start-again", new)
    # the new run stops the same way, without running it: the first run's steps and ship
    first = state_of(layer, run)
    path = layer / "agents" / new / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.update(status="failed", ended=first.get("ended") or "2026-01-01T00:00:00+00:00", pid=0,
                 steps=first["steps"], ship=first["ship"])
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    before = sorted(p.name for p in (layer / "agents").iterdir())
    code, out = assign_main(layer, "give", new, "--by", "decider", "--detach", "--json")
    assert code == 3, out
    refusal = json.loads(out.strip().splitlines()[0])
    assert refusal["reason"] == "limit" and run in refusal["error"] and new in refusal["error"]
    assert refusal["error"] == (f"the stand-in gave run {run}, which run {new} replaces, to an agent already; "
                                "it stays with a person")
    assert sorted(p.name for p in (layer / "agents").iterdir()) == before, "no further run was started"
    assert not (layer / "agents" / new / "assign.json").exists() and len(spawned) == 1
    assert len(record(layer)["attempts"]) == 1
    # the limit is the stand-in's: a person can still give the run
    code, out = assign_main(layer, "preview", new, "--json")
    assert code == 0, out
    found = json.loads(out.strip().splitlines()[-1])
    assert found["kind"] != "none" and found["blocked"] == ""


def test_person_item_then_continue(make_layer, app_repo):
    layer, run = person_run(make_layer, app_repo)
    code, out = assign_main(layer, "give", run)
    assert code == 3 and "assign: needs-a-person: " in out and integration_runs(layer) == []
    assert not (layer / "agents" / run / "assign.json").exists(), "a refused give records no attempt"
    item = assign.plan(ir.load(layer), run)["decisions"][0]["id"]
    code, out = assign_main(layer, "approve", run, item, "--who", "Robin Example")
    assert code == 0 and f"approved by Robin Example: item {item} of run {run}; nothing started" in out
    assert integration_runs(layer) == []
    fix_scripts(layer)
    code, out = assign_main(layer, "give", run)
    assert code == 0, out
    [new] = integration_runs(layer)
    request = _prompt_of(layer, new, "address-patch")
    assert f"Approved by a person: {PERSON_BLOCKER['description']} (Robin Example, " in request
    assert BLOCKER["description"] in request
    state = state_of(layer, new)
    assert state["status"] == "passed"
    brief = (layer / state["integration"]["brief"]).read_text()
    assert "## Approved by a person" in brief and "Robin Example" in brief


def test_give_all_starts_in_order(make_layer, app_repo):
    layer, run = failed_check_run(make_layer, app_repo)
    handed_over_run(layer, app_repo, "f0000002", {"door.py": "DOOR = 1\n"}, reasons=("review-failed",), rejected=True,
                    title="Add a door")
    handed_over_run(layer, app_repo, "f0000003", title="Give stock a rebate")
    handed_over_run(layer, app_repo, "f0000004", {"lamp.py": "LAMP = 1\n"}, reasons=("class-not-allowed:feature",),
                    title="Add a lamp")
    from integrate_fixtures import move_main
    move_main(app_repo)
    spawned = []
    results = assign.give_all(layer, by="dashboard", spawn=lambda argv, **kw: spawned.append(argv))
    assert [(r["run"], r["kind"], r["status"]) for r in results] == [
        (run, "check", "started"), ("f0000003", "integrate", "started"), ("f0000002", "blockers", "started"),
        ("f0000004", "person", "skipped")]
    expected = [item["run"] for item in assign.order(ir.load(layer), [run, "f0000002", "f0000003", "f0000004"])]
    assert [r["run"] for r in results] == [run, "f0000003", "f0000002", "f0000004"] and len(expected) == 4
    assert [Path(argv[1]).name for argv in spawned] == ["adw_demo.py", "integrate.py", "integrate.py"]
    assert [argv[3] for argv in spawned[1:]] == ["f0000003", "f0000002"]
    assert all(record(layer, r["run"])["attempts"][0]["by"] == "dashboard" for r in results[:3])


def test_stand_in_gives_a_rejected_run_once(make_layer, app_repo, capsys):
    layer, run = rejected_run(make_layer, app_repo, settings={"decider": True})
    loaded, state = ir.load(layer), state_module.load(layer, run)
    spawned = []
    spawn = lambda argv, **kw: spawned.append(argv)   # noqa: E731
    assign.after_run(loaded, state, SimpleNamespace(decider="suggest"), spawn=spawn)
    assert spawned == [] and not (layer / "agents" / run / "assign.json").exists()
    assign.after_run(loaded, state, SimpleNamespace(decider="auto"), spawn=spawn)
    out = capsys.readouterr().out
    [new] = integration_runs(layer)
    assert f"[adw {run}] decider: given to an agent (blockers): run {new}" in out
    assert spawned[0][1:] == [str(layer / "adws" / "integrate.py"), "run", run, "--adw-id", new, "--by", "decider"]
    [attempt] = record(layer)["attempts"]
    assert (attempt["by"], attempt["kind"], attempt["run"]) == ("decider", "blockers", new)
    # the attempt run is rejected again: it starts no further one, and neither does the original
    fix_scripts(layer, passes=False)
    code = integrate.run(layer, run, adw_id=new, by="decider")
    assert code == 0 and state_of(layer, new)["status"] == "handed-over"
    capsys.readouterr()
    assign.after_run(loaded, state_module.load(layer, new), SimpleNamespace(decider="auto"), spawn=spawn)
    assign.after_run(loaded, state, SimpleNamespace(decider="auto"), spawn=spawn)
    out = capsys.readouterr().out
    assert len(spawned) == 1 and integration_runs(layer) == [new]
    assert "given to an agent once already; the run stays with a person" in out
    found = assign.plan(loaded, run)
    assert (found["clicks"], found["left"], found["blocked"]) == (0, 2, ""), "the stand-in's attempt is not a click"


def test_stand_in_still_integrates_a_conflict(make_layer, app_repo, capsys):
    layer, run = conflict_run(make_layer, app_repo, settings={"decider": True})
    spawned = []
    assign.after_run(ir.load(layer), state_module.load(layer, run), SimpleNamespace(decider="auto"),
                     spawn=lambda argv, **kw: spawned.append(argv))
    [new] = integration_runs(layer)
    assert f"[adw {run}] decider: integrating with an agent: run {new}" in capsys.readouterr().out
    assert record_of(layer, run)["by"] == "decider" and len(spawned) == 1


def test_main_refuses_with_the_reason(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo)
    code, out = assign_main(layer, "preview", "0badc0de")
    assert code == 3 and out.startswith("assign: unknown-run: ")
    code, out = assign_main(layer, "preview", run, "--note", "x" * 501, "--json")
    assert code == 3 and json.loads(out.splitlines()[0])["reason"] == "bad-note"
    code, out = assign_main(layer, "approve", run, "0000000000", "--who", "Robin")
    assert code == 3 and "assign: bad-item: " in out
    code, out = assign_main(layer, "approve", run, "0000000000")
    assert code == 2 and "--who" in out
    code, out = assign_main(layer, "give")
    assert code == 2
    code, out = assign_main(layer, "preview", run)
    assert code == 0 and out.splitlines()[0] == f"run:        {run} (Give stock a discount)"
    assert "stopped:    handed-over at review: the review rejected it" in out
    assert "plan:       address 1 blocker (the review's patch round) and review once more" in out
    assert "  1. apply branch " in out and "limit:      0 of 2 used" in out
    code, out = assign_main(layer, "all", "--preview")
    assert code == 0 and out.startswith(f"{run}  blockers")
    assert subprocess.run(["git", "-C", str(app_repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""
