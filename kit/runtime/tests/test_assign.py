"""Unit tests of "Give to an agent" (adw_modules/assign.py): the plan for every reason a run
stopped, the approvals, the limits and the bulk order. Nothing is started here; whole assigned runs
are in test_assign_runs.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adw_modules import assign, ir
from assign_fixtures import (BLOCKER, PERSON_BLOCKER, PLAN_COMMANDS, PROOF_BLOCKER, RUN, assign_layer, conflict_run,
                             died_run, failed_check_run, login_run, nothing_changed_run, person_run, proof_run, record,
                             rejected_run, time_limit_run, write_attempts)
from integrate_fixtures import handed_over_run, move_main, state_of


def _plan(layer: Path, run: str = RUN, **more) -> dict:
    return assign.plan(ir.load(layer), run, **more)


def _set_state(layer: Path, run: str = RUN, **changes) -> None:
    path = layer / "agents" / run / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- one test per row of the table

def test_plan_conflict_is_integrate(make_layer, app_repo):
    layer, run = conflict_run(make_layer, app_repo)
    found = _plan(layer)
    assert (found["run"], found["kind"], found["blocked"]) == (run, "integrate", "")
    assert found["stop"]["key"] == "conflict" and found["stop"]["status"] == "handed-over"
    assert found["moved"] is True and found["decisions"] == []
    assert found["integrate"]["eligible"] is True and found["integrate"]["conflicts"] == ["INDEX.md", "stock.py"]
    assert found["title"] == "Give stock a discount" and (found["clicks"], found["left"]) == (0, 2)
    assert "main" in found["summary"] and found["steps"]


def test_plan_rejected_is_blockers(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo)
    found = _plan(layer)
    assert (found["kind"], found["blocked"], found["moved"]) == ("blockers", "", False)
    assert found["stop"]["key"] == "review:blockers" and found["stop"]["step"] == "review"
    assert found["blockers"] == [BLOCKER]
    assert found["summary"] == "address 1 blocker (the review's patch round) and review once more"
    assert found["integrate"]["eligible"] is True
    assert found["integrate"]["steps"] == ["prepare", "resolve", "address", "test", "review", "ship"]
    assert found["source"] == run


def test_plan_rejected_and_moved_integrates_first(make_layer, app_repo):
    layer, _run = rejected_run(make_layer, app_repo, moved=True)
    found = _plan(layer)
    assert found["kind"] == "blockers" and found["moved"] is True
    assert found["summary"].startswith("bring it onto main first, then address 1 blocker")
    assert found["integrate"]["conflicts"] == ["stock.py"]


def test_plan_proof_found_collects(make_layer, app_repo):
    layer, _run = proof_run(make_layer, app_repo, found=True)
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("proof", "")
    assert found["stop"]["key"] == "review:proof" and found["blockers"] == [PROOF_BLOCKER]
    assert found["found"] == ["proof/shelf.png"] and found["commands"] == []
    assert found["summary"] == ("the proof exists but was not collected: collect 1 file for this run and ask the "
                                "review again; no code changes")
    assert found["integrate"]["steps"] == ["prepare", "resolve", "proof", "test", "review", "ship"]


def test_plan_proof_left_out_by_the_limit(make_layer, app_repo):
    layer, run = proof_run(make_layer, app_repo, found=False, left_out=True)
    state = state_of(layer, run)
    assert Path(state["worktree"]).is_dir(), "the worktree is kept"
    assert sorted(Path(item["path"]).name for item in state["artefacts"]) == ["a-shelf.png", "b-till.png"]
    found = _plan(layer)
    assert found["kind"] == "proof" and found["found"] == ["shots/c-door.png"]
    assert "collect 1 file" in found["summary"]


def test_plan_proof_missing_runs_the_plan_commands(make_layer, app_repo):
    layer, _run = proof_run(make_layer, app_repo, found=False)
    found = _plan(layer)
    assert found["kind"] == "proof" and found["found"] == []
    assert found["commands"] == PLAN_COMMANDS
    assert found["summary"] == "run the 2 commands the plan lists for proof, then review"
    assert found["integrate"]["steps"] == ["prepare", "resolve", "address", "proof", "test", "review", "ship"]


def test_plan_needs_a_person_names_the_decision(make_layer, app_repo):
    layer, run = person_run(make_layer, app_repo)
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("person", "needs-a-person")
    assert found["stop"]["key"] == "person"
    [item] = found["decisions"]
    assert item["text"] == PERSON_BLOCKER["description"] and item["kind"] == "protected-path"
    assert item["path"] == "deploy/release.cfg" and item["approvable"] is True and item["approved"] is None
    assert len(item["id"]) == 10 and int(item["id"], 16) >= 0
    assert found["summary"] == f"a person decides: {item['text']}"
    assert any(f"al assign LAYER {run} --approve {item['id']} --who NAME" in line for line in found["steps"])
    assert _plan(layer)["decisions"][0]["id"] == item["id"], "the id is stable across calls"
    assert "needs-a-person" in "\n".join(assign.plan_lines(found)) or item["id"] in "\n".join(assign.plan_lines(found))


def test_plan_failed_check_resumes_heavy(make_layer, app_repo):
    layer, _run = failed_check_run(make_layer, app_repo)
    found = _plan(layer)
    assert (found["kind"], found["from_step"], found["tier"], found["blocked"]) == ("check", "test", "heavy", "")
    assert found["stop"]["key"] == "check:test" and found["stop"]["status"] == "failed"
    assert found["summary"] == ("resume from check test: it runs again, and when it still fails a fresh fixing agent "
                                "gets its log, on the heavy tier")
    assert found["integrate"] is None


@pytest.mark.parametrize("builder, key, step", [(login_run, "login", "build"), (time_limit_run, "time-limit", "test"),
                                                (died_run, "died", "build")])
def test_plan_login_time_limit_died_resume_unchanged(make_layer, app_repo, builder, key, step):
    layer, _run = builder(make_layer, app_repo)
    found = _plan(layer)
    assert (found["kind"], found["from_step"], found["tier"], found["blocked"]) == ("resume", step, "", "")
    assert found["stop"]["key"] == key
    assert found["stop"]["status"] == ("died" if key == "died" else "failed")
    assert found["summary"] == f"resume from {step}, unchanged"


def test_plan_nothing_changed_starts_again(make_layer, app_repo):
    layer, _run = nothing_changed_run(make_layer, app_repo)
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("start-again", "")
    assert found["stop"]["key"] == "nothing-changed"
    assert found["summary"] == ("it failed before changing anything: start the same request again; the new run "
                                "replaces it")


# ---------------------------------------------------------------- the edges

def test_plan_not_for_settled_running_or_passed(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo)
    (layer / "agents" / run / "settlement.json").write_text(json.dumps({"as": "landed", "by": "a person"}))
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("none", "settled")
    (layer / "agents" / run / "settlement.json").unlink()
    _set_state(layer, status="passed")
    assert (_plan(layer)["kind"], _plan(layer)["blocked"]) == ("none", "not-stopped")
    _set_state(layer, status="running", pid=0, host="", heartbeat=assign.state_module.now())
    assert (_plan(layer)["kind"], _plan(layer)["blocked"]) == ("none", "running")
    _set_state(layer, status="handed-over", fanout_parent={"run": "a0000009", "step": "fan", "index": 0})
    found = _plan(layer)
    assert found["kind"] == "none" and "its parent decides" in found["blocked_why"]
    with pytest.raises(assign.AssignError) as caught:
        _plan(layer, "0badc0de")
    assert caught.value.reason == "unknown-run" and str(caught.value).startswith("assign:unknown-run: ")


def test_plan_layer_change_and_spending_limit_stay_with_a_person(make_layer, app_repo):
    layer, _run = rejected_run(make_layer, app_repo, changes="layer")
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("person", "needs-a-person")
    assert [(d["kind"], d["approvable"]) for d in found["decisions"]] == [("layer-change", False)]
    assert "a person always approves" in found["decisions"][0]["text"]
    with pytest.raises(assign.AssignError) as caught:
        assign.approve(layer, RUN, found["decisions"][0]["id"], "Robin")
    assert caught.value.reason == "bad-item"


def test_spending_limit_is_not_approvable(make_layer, app_repo):
    layer, _run = login_run(make_layer, app_repo)
    _set_state(layer, budget={"stopped": {"limit": "run", "spent": 3.0, "of": 2.0, "step": "build", "reason": "over"}})
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("person", "needs-a-person")
    assert [(d["kind"], d["approvable"]) for d in found["decisions"]] == [("spending-limit", False)]


def test_plan_of_an_integration_run_is_its_originals(make_layer, app_repo):
    layer, run = conflict_run(make_layer, app_repo)
    attempt = "abcd0001"
    (layer / "agents" / attempt).mkdir()
    (layer / "agents" / attempt / "state.json").write_text(json.dumps({
        "adw_id": attempt, "adw": "integrate", "status": "failed", "prompt": "Integrate run", "started": "x",
        "definition": {"name": "integrate", "integration": {"run": run}}, "integration": {"run": run},
        "steps": [{"name": "resolve", "kind": "integrate", "status": "failed", "detail": "resolve: integrate:undecidable: x"}]}))
    found = _plan(layer, attempt)
    assert (found["run"], found["asked"], found["kind"]) == (run, attempt, "integrate")


def test_plan_of_a_passed_review_that_is_no_conflict_stays_with_a_person(make_layer, app_repo):
    layer = assign_layer(make_layer, app_repo)
    handed_over_run(layer, app_repo, RUN, reasons=("class-not-allowed:feature",))
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("person", "needs-a-person")
    [item] = found["decisions"]
    assert item["approvable"] is False and item["text"].startswith("review and merge the pull request: class-not-allowed")


def test_no_patch_round_blocks(make_layer, app_repo):
    layer = assign_layer(make_layer, app_repo, patch=False)
    handed_over_run(layer, app_repo, RUN, reasons=("review-failed",), rejected=True)
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("blockers", "needs-a-person")
    assert "patch" in found["blocked_why"]


def test_approval_is_recorded_for_exactly_that_item(make_layer, app_repo):
    second = {"description": "the price list holds a password in clear text", "resolution": "a person rotates it"}
    layer, run = rejected_run(make_layer, app_repo, blockers=(BLOCKER, PERSON_BLOCKER, second))
    first, other = _plan(layer)["decisions"]
    assert other["kind"] == "credential"
    after = assign.approve(layer, run, first["id"], "Robin Example")
    saved = record(layer)["approvals"]
    assert [(a["id"], a["text"], a["by"], a["via"]) for a in saved] == [
        (first["id"], PERSON_BLOCKER["description"], "Robin Example", "cli")]
    assert saved[0]["at"] and saved[0]["kind"] == "protected-path" and saved[0]["path"] == "deploy/release.cfg"
    assert (after["kind"], after["blocked"]) == ("person", "needs-a-person")
    states = {d["id"]: d["approved"] for d in after["decisions"]}
    assert states[first["id"]]["by"] == "Robin Example" and states[other["id"]] is None
    assert after["summary"] == f"a person decides: {other['text']}"
    for bad in ("0000000000", first["id"]):          # unknown, and approved already
        with pytest.raises(assign.AssignError) as caught:
            assign.approve(layer, run, bad, "Robin Example")
        assert caught.value.reason == "bad-item"
    assert len(record(layer)["approvals"]) == 1
    done = assign.approve(layer, run, other["id"], "Robin Example", via="dashboard")
    assert (done["kind"], done["blocked"]) == ("blockers", "")
    assert len(done["blockers"]) == 3 and all(d["approved"] for d in done["decisions"])
    types = [json.loads(line)["type"] for line in (layer / "agents" / run / "events.jsonl").read_text().splitlines()]
    assert types.count("assign.approval") == 2
    assert state_of(layer, run)["status"] == "handed-over", "an approval starts nothing"


def test_limit_two_clicks_then_override(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo)
    write_attempts(layer, [{"by": "decider", "after": "step:resolve"}, {"by": "dashboard", "after": "step:resolve"}])
    found = _plan(layer)
    assert (found["clicks"], found["left"], found["blocked"]) == (1, 1, ""), "a decider attempt is not a click"
    write_attempts(layer, [{"by": "decider", "after": "step:resolve"}, {"by": "dashboard", "after": "step:resolve"},
                           {"by": "cli", "after": "step:address"}])
    found = _plan(layer)
    assert (found["clicks"], found["left"], found["blocked"]) == (2, 0, "limit")
    assert "given to an agent twice already" in found["blocked_why"] and "--override" in found["blocked_why"]
    with pytest.raises(assign.AssignError) as caught:
        assign.give(layer, run, by="dashboard", dry_run=True)
    assert caught.value.reason == "limit"
    spawned = []
    result = assign.give(layer, run, by="dashboard", override=True, detach=True,
                         spawn=lambda argv, **kw: spawned.append(argv))
    assert result["status"] == "started" and result["kind"] == "blockers" and result["attempt"] == 4
    assert len(spawned) == 1 and record(layer)["attempts"][-1]["outcome"] == "started"


def _batch_run(layer: Path, run: str = RUN, stage: str = "") -> None:
    """Rewrite a stopped run into a run of the kit workflow verify-batch, as batch.batch_adw records it."""
    names = ("open", "checks", "record", "close")
    _set_state(layer, run, adw="verify-batch", prompt="verify the full suites on main",
               definition={"name": "verify-batch", "batch": {"branch": "main", "stage": stage, "base": "demo"},
                           "steps": [{"kind": "batch", "name": n, "action": n} for n in names]})


@pytest.mark.parametrize("builder, stage, status, words", [
    (nothing_changed_run, "", "failed", "`al batch LAYER run`"),
    (nothing_changed_run, "sprint", "failed", "`al batch LAYER run --stage sprint`"),
    (died_run, "", "died", "`al batch LAYER run`"),
])
def test_plan_of_a_batch_run_is_never_start_again(make_layer, app_repo, builder, stage, status, words):
    """A failed or died batch run is not a request launch.start can start: no start-again plan, and
    giving it is refused before an attempt is recorded."""
    layer, run = builder(make_layer, app_repo)
    _batch_run(layer, run, stage)
    found = _plan(layer, run)
    assert found["stop"]["status"] == status
    assert (found["kind"], found["blocked"]) == ("none", "not-stopped"), found["summary"]
    assert words in found["summary"] and words in found["blocked_why"]
    assert "verify-batch" in found["summary"] and "start workflow" not in " ".join(found["steps"])
    spawned = []
    for more in ({"by": "dashboard"}, {"by": "decider"}, {"by": "dashboard", "override": True},
                 {"by": "cli", "override": True}):
        with pytest.raises(assign.AssignError) as caught:
            assign.give(layer, run, detach=True, spawn=lambda *a, **k: spawned.append(a), **more)
        assert caught.value.reason == "not-stopped" and "al batch LAYER run" in caught.value.message, more
    assert not (layer / "agents" / run / "assign.json").exists(), "no attempt is recorded for a start that cannot work"
    assert spawned == []
    assert sorted(p.name for p in (layer / "agents").iterdir() if (p / "state.json").is_file()) == [run]


def test_a_batch_run_never_reaches_the_limit_message(make_layer, app_repo):
    layer, run = nothing_changed_run(make_layer, app_repo)
    clicks = [{"by": "dashboard", "kind": "start-again", "why": "it did not start: no workflow named verify-batch"}] * 3
    write_attempts(layer, clicks)
    ordinary = _plan(layer)                               # an ordinary run with three clicks: the count is worded
    assert (ordinary["kind"], ordinary["clicks"], ordinary["blocked"]) == ("start-again", 3, "limit")
    lines = assign.plan_lines(ordinary)
    assert lines[-1].startswith("limit:      2 of 2 used, and 1 more by --override; not started: limit: ")
    assert "3 of 2" not in "\n".join(lines)
    write_attempts(layer, clicks[:2])
    assert assign.plan_lines(_plan(layer))[-1].startswith("limit:      2 of 2 used; not started: limit: ")

    write_attempts(layer, clicks)
    _batch_run(layer, run)
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("none", "not-stopped")
    assert "twice already" not in found["blocked_why"] and "al batch LAYER run" in found["blocked_why"]
    assert run not in assign.stopped_runs(ir.load(layer))
    spawned = []
    assert assign.give_all(layer, by="dashboard", spawn=lambda *a, **k: spawned.append(a)) == []
    named = assign.give_all(layer, by="dashboard", run_ids=[run], spawn=lambda *a, **k: spawned.append(a))
    assert [(r["run"], r["status"]) for r in named] == [(run, "skipped")] and "al batch" in named[0]["why"]
    assert spawned == [] and len(record(layer)["attempts"]) == 3, "the counter does not grow"


def test_same_reason_twice_stays_with_the_person(make_layer, app_repo):
    layer, _run = rejected_run(make_layer, app_repo)
    write_attempts(layer, [{"reason": "review:blockers", "after": "review:blockers", "why": "its review rejected it again"},
                           {"reason": "review:blockers", "after": "review:blockers", "why": "its review rejected it again"}])
    found = _plan(layer)
    assert found["same_reason"] is True and found["blocked"] == "limit"
    assert "it stopped twice for the same reason" in found["blocked_why"] and "it stays with you" in found["blocked_why"]
    write_attempts(layer, [{"reason": "review:blockers", "after": "step:resolve"},
                           {"reason": "step:resolve", "after": "check:test"}])
    assert _plan(layer)["same_reason"] is False


def test_bulk_order(make_layer, app_repo):
    layer, _run = failed_check_run(make_layer, app_repo)           # f0000001: check
    runs = {"f0000002": "proof", "f0000003": "blockers", "f0000004": "integrate", "f0000005": "integrate",
            "f0000006": "person", "f0000007": "blockers"}
    handed_over_run(layer, app_repo, "f0000002", {"door.py": "DOOR = 1\n"}, reasons=("review-failed",), rejected=True,
                    title="Add a door")
    handed_over_run(layer, app_repo, "f0000003", {"till.py": "TILL = 1\n"}, reasons=("review-failed",), rejected=True,
                    title="Add a till")
    handed_over_run(layer, app_repo, "f0000005", {"stock.py": "def total(items, rebate=0):\n    return sum(items)\n"},
                    title="Give stock a rebate")
    handed_over_run(layer, app_repo, "f0000004", title="Give stock a discount")
    handed_over_run(layer, app_repo, "f0000006", {"lamp.py": "LAMP = 1\n"}, reasons=("class-not-allowed:feature",),
                    title="Add a lamp")
    handed_over_run(layer, app_repo, "f0000007", {"rack.py": "RACK = 1\n"}, reasons=("review-failed",), rejected=True,
                    title="Add a rack")
    move_main(app_repo)
    for run, started in (("f0000002", "09:02"), ("f0000003", "09:03"), ("f0000004", "09:05"), ("f0000005", "09:04"),
                         ("f0000006", "09:06"), ("f0000007", "09:01")):
        state = state_of(layer, run)
        state["started"] = f"2026-11-16T{started}:00+00:00"
        if run == "f0000002":
            state["review"]["verdict"]["issues"] = [{"severity": "blocker", **PROOF_BLOCKER}]
        (layer / "agents" / run / "state.json").write_text(json.dumps(state))
    write_attempts(layer, [{"by": "cli"}, {"by": "cli"}], "f0000007")       # the limit blocks it
    ordered = assign.order(ir.load(layer), ["f0000007", "f0000006", "f0000005", "f0000004", "f0000003", "f0000002",
                                           RUN])
    assert [(item["run"], item["kind"]) for item in ordered] == [
        (RUN, "check"), ("f0000005", "integrate"), ("f0000004", "integrate"), ("f0000002", "proof"),
        ("f0000003", "blockers"), ("f0000006", "person"), ("f0000007", "blockers")]
    assert [bool(item["skip"]) for item in ordered] == [False] * 5 + [True, True]
    assert ordered[-1]["skip"].startswith("limit") and ordered[-2]["skip"].startswith("needs-a-person")
    assert runs["f0000006"] == "person"


def test_note_is_checked(make_layer, app_repo):
    layer, run = rejected_run(make_layer, app_repo)
    assert _plan(layer, note="  keep the old\tsignature  ")["note"] == "keep the old signature"
    for bad in ("x" * 501, "one\x07two", "one\x1btwo"):
        with pytest.raises(assign.AssignError) as caught:
            _plan(layer, note=bad)
        assert caught.value.reason == "bad-note"
        with pytest.raises(assign.AssignError):
            assign.give(layer, run, note=bad, dry_run=True)
    assert not (layer / "agents" / run / "assign.json").exists()


def test_a_changed_protected_path_is_a_decision_a_person_may_approve(make_layer, app_repo):
    layer = assign_layer(make_layer, app_repo)
    handed_over_run(layer, app_repo, RUN, {"stock.py": "def total(items):\n    return 0\n",
                                           "deploy/release.cfg": "channel = beta\n"},
                    reasons=("review-failed",), rejected=True)
    found = _plan(layer)
    assert (found["kind"], found["blocked"]) == ("person", "needs-a-person")
    [item] = found["decisions"]
    assert (item["kind"], item["path"], item["approvable"]) == ("protected-path", "deploy/release.cfg", True)
    after = assign.approve(layer, RUN, item["id"], "Robin Example")
    assert (after["kind"], after["blocked"]) == ("blockers", "") and after["integrate"]["eligible"] is True
    # the same change with a passed review and a conflict is `al integrate`'s: a person merges it
    handed_over_run(layer, app_repo, "f0000002", {"stock.py": "def total(items, discount=0):\n    return 0\n",
                                                  "deploy/release.cfg": "channel = beta\n"}, title="Release it")
    move_main(app_repo)
    found = _plan(layer, "f0000002")
    assert (found["kind"], found["blocked"]) == ("person", "needs-a-person")
    assert found["decisions"][0]["approvable"] is False and "a protected path" in found["decisions"][0]["text"]
