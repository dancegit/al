"""Approve & ship (shipapproval.py, approvable.py kind `ship`): a handed-over run whose gate only asked
for a person's yes is one approvable item; the approval is recorded, then the stage branch is
fast-forwarded or the run is settled as shipped to its pull request or branch."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convergence_fixtures as cf  # noqa: E402
import integrate_fixtures as itf  # noqa: E402

from adw_modules import approvable, shipapproval, state as state_module  # noqa: E402
from adw_modules.errors import StepFailed  # noqa: E402

RUNTIME = Path(__file__).resolve().parents[1]
RUN = "a9970001"
DIFF = "diff-too-large:3148>400"
PR = "https://forge.example/o/r/pull/7"


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_DECIDER", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_HOST_ID", "AL_APPROVED_BY"):
        monkeypatch.delenv(key, raising=False)


def _patch_state(layer: Path, run: str, **changes) -> dict:
    path = layer / "agents" / run / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def _patch_ship(layer: Path, run: str, **changes) -> dict:
    ship = dict(cf.state_of(layer, run)["ship"])
    ship.update(changes)
    return _patch_state(layer, run, ship=ship)


def _patch_layer(layer: Path, change) -> None:
    path = layer / "agentic-layer.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _events(layer: Path, run: str) -> str:
    path = layer / "agents" / run / "events.jsonl"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _settlement(layer: Path, run: str = RUN) -> dict | None:
    path = layer / "agents" / run / "settlement.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _ship_stop(make_layer, app_repo, reasons, *, stage="integration", **state) -> Path:
    """Handed over, the review passed, the checks are green, the gate's only reasons are `reasons`."""
    layer = itf.conflict_layer(make_layer, app_repo)
    written = itf.handed_over_run(layer, app_repo, RUN, files={"shelf.py": "SHELF = 1\n"}, reasons=tuple(reasons))
    if stage:
        itf.git(app_repo, "branch", stage, "main")
    changes = {
        "checks": {"test": {"cmd": "./test.sh", "exit": 0, "outcome": "passed", "tree": "", "log": ""}},
        "check_results": [{"check": "test", "step": "ship", "phase": "gate", "outcome": "passed", "exit": 0,
                           "tree": ""}],
        "review": {"verdict": {"success": True, "summary": "fine", "issues": []}, "rounds": [{"round": 1}],
                   "mode": "auto"},
        "ship": {**written["ship"], "gate": {"action": "pr", "ship": False, "reasons": list(reasons),
                                             "diff_lines": 3148, "changed_files": ["shelf.py"]}},
    }
    if stage:
        changes["delivery"] = {"stage": stage, "branch": stage, "agents_may_merge": True}
    changes.update(state)
    _patch_state(layer, RUN, **changes)
    return layer


def _commit(layer: Path) -> str:
    return cf.state_of(layer, RUN)["ship"]["commit"]


def _branch(layer: Path) -> str:
    return cf.state_of(layer, RUN)["branch"]


# ---------------------------------------------------------------- the model

@pytest.mark.parametrize("reason", [DIFF, "class-not-allowed:feature", "needs-person",
                                    "ship-failed:git push refused"])
def test_each_ship_reason_is_one_ship_item(make_layer, app_repo, reason):
    layer = _ship_stop(make_layer, app_repo, [reason])
    found = approvable.derive(layer, RUN)
    assert (found["waiting"], found["kind"], found["why"]) == (True, "ship", ""), found
    [item] = found["items"]
    token = "ship:ship-failed" if reason.startswith("ship-failed") else f"ship:{reason}"
    assert (item["id"], item["kind"], item["what"]) == ("ship", "ship", [token])
    assert reason in item["text"] and "3148 lines in 1 file(s)" in item["text"] and "integration" in item["text"]
    assert item["paths"] == ["shelf.py"]
    ship = found["ship"]
    assert ship["reasons"] == [reason] and ship["diff_lines"] == 3148
    assert (ship["files"], ship["files_total"], ship["target"]) == (["shelf.py"], 1, "integration")
    assert ship["commit"] == _commit(layer) and len(ship["tree"]) == 40
    assert ship["checks"] == [{"name": "test", "phase": "check", "state": "passed"},
                              {"name": "test", "phase": "gate", "state": "passed"}]
    assert "approvable" not in ship and "why" not in ship
    assert len(found["token"]) == 16 and approvable.derive(layer, RUN)["token"] == found["token"]
    lines = approvable.lines(found)
    assert lines[0] == f"run {RUN} waits for your approval of 1 item(s):"
    assert "  target:    integration" in lines and "  diff:      3148 lines, 1 file(s)" in lines
    assert "  checks:    test passed, test (gate) passed" in lines and lines[-1].startswith("after:     ")
    assert not (layer / "agents" / RUN / "approvable.json").exists(), "a derivation only reads"


def test_way_follows_the_target_and_the_pull_request(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    found = approvable.derive(layer, RUN)
    assert (found["ship"]["way"], found["ship"]["label"]) == ("merge", "Approve & merge")
    assert found["after"] == "the run's branch is merged into integration (fast-forward)"
    merge_token = found["token"]

    _patch_ship(layer, RUN, pr={"number": 7, "url": PR})
    found = approvable.derive(layer, RUN)
    assert (found["ship"]["way"], found["ship"]["label"]) == ("pr", "Approve & ship")
    assert found["ship"]["pr"] == {"number": 7, "url": PR} and PR in found["items"][0]["text"]
    assert found["after"] == f"the run is settled as shipped to {PR}"
    assert found["token"] != merge_token, "the token follows the way"

    # a ship script that opened the pull request: its recorded output names it
    _patch_ship(layer, RUN, pr=None)
    _patch_layer(layer, lambda data: data["adws"][0]["steps"].append(
        {"name": "open-pr", "kind": "script", "cmd": "open-pr", "args": [], "timeout": 0}))
    _patch_state(layer, RUN, outputs={"open-pr": f"pushed\nopened {PR}.\n", "build": "see https://x.example/o/r/pull/1"})
    found = approvable.derive(layer, RUN)
    assert found["ship"]["way"] == "pr" and found["ship"]["pr"] == {"number": None, "url": PR}
    _patch_state(layer, RUN, outputs={})
    assert approvable.derive(layer, RUN)["ship"]["way"] == "merge"

    _patch_state(layer, RUN, delivery={"stage": "integration", "branch": "integration", "agents_may_merge": False})
    found = approvable.derive(layer, RUN)
    assert (found["ship"]["way"], found["ship"]["label"]) == ("branch", "Approve & ship")
    assert found["after"] == f"the run is settled as shipped; a person merges {_branch(layer)} into integration"

    _patch_state(layer, RUN, delivery={})
    found = approvable.derive(layer, RUN)
    assert (found["ship"]["way"], found["ship"]["target"]) == ("branch", "main")
    # the default branch is never a merge target, whatever a state says
    _patch_state(layer, RUN, delivery={"stage": "main", "branch": "main", "agents_may_merge": True})
    assert approvable.derive(layer, RUN)["ship"]["way"] == "branch"


def test_red_missing_or_stale_checks_offer_nothing(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    green = cf.state_of(layer, RUN)

    def why(**changes) -> str:
        _patch_state(layer, RUN, **{"checks": green["checks"], "check_results": green["check_results"], **changes})
        found = approvable.derive(layer, RUN)
        assert found["waiting"] is False and found["items"] == [] and found["token"] == "", found
        assert found["ship_refused"] == "checks-not-green" and "ship" not in found
        return found["why"]

    assert why(checks={"test": {**green["checks"]["test"], "exit": 1}}) == "checks are not all green: test failed"
    assert why(checks={}) == "checks are not all green: test not-run"
    assert why(check_results=[]) == "checks are not all green: test (gate) not-run"
    assert why(check_results=[{**green["check_results"][0], "outcome": "failed", "exit": 1}]) == (
        "checks are not all green: test (gate) failed")
    assert why(check_results=[{**green["check_results"][0], "tree": "0" * 40}]) == (
        "checks are not all green: test (gate) stale")
    assert why(proof_plan={"pack": {"rows": [{"id": "C1", "check": "test", "state": "stale"}]}}) == (
        "checks are not all green: test stale, test (gate) stale")
    _patch_state(layer, RUN, checks=green["checks"], check_results=green["check_results"], proof_plan={})
    assert approvable.derive(layer, RUN)["waiting"] is True


def test_protected_paths_keep_approve_and_resume(make_layer, app_repo, tmp_path):
    layer = _ship_stop(make_layer, app_repo, [DIFF, "protected-path:deploy/release.cfg"])
    assert approvable.derive(layer, RUN)["kind"] != "ship"
    tree = tmp_path / "kept-tree"
    tree.mkdir()
    reasons = ["protected-path:deploy/release.cfg"]
    _patch_state(layer, RUN, worktree=str(tree))
    _patch_ship(layer, RUN, reasons=reasons, handed_over=reasons,
                gate={"action": "pr", "ship": False, "reasons": reasons, "diff_lines": 3, "changed_files": []})
    found = approvable.derive(layer, RUN)
    assert found["kind"] != "ship" and "ship" not in found
    assert shipapproval.claims(reasons) is False and shipapproval.claims([]) is False
    assert shipapproval.claims([DIFF, "needs-person", "class-not-allowed:feature", "ship-failed:x y"]) is True
    assert shipapproval.claims([DIFF, "review-failed"]) is False


def test_review_failed_budget_and_person_only_offer_nothing(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    passed = cf.state_of(layer, RUN)["review"]
    rejected = {"verdict": {"success": False, "summary": "wrong", "issues": []}, "rounds": [{"round": 1}],
                "mode": "auto"}
    _patch_state(layer, RUN, review=rejected)
    found = approvable.derive(layer, RUN)
    assert (found["waiting"], found["why"]) == (False, "its review rejected it")
    _patch_state(layer, RUN, review=passed)

    _patch_ship(layer, RUN, reasons=[DIFF, "review-failed"])
    found = approvable.derive(layer, RUN)
    assert found["kind"] != "ship" and "ship" not in found
    _patch_ship(layer, RUN, reasons=[DIFF])

    _patch_state(layer, RUN, budget={"stopped": {"limit": "run", "spent": 3.0, "of": 2.0, "step": "build",
                                                 "reason": "the run's spending limit"}})
    found = approvable.derive(layer, RUN)
    assert found["waiting"] is False and found["why"].startswith("a person's only: a spending limit stopped the run")
    _patch_state(layer, RUN, budget={})

    _patch_layer(layer, lambda data: data["adws"][0].update(changes="layer"))
    found = approvable.derive(layer, RUN)
    assert found["waiting"] is False and found["why"].startswith("a person's only: the workflow changes the layer")
    result = approvable.approve_ship(layer, RUN, "ana", "cli")
    assert (result["status"], result["reason"]) == ("refused", "not-waiting")
    assert not cf.state_of(layer, RUN).get("approvals") and _settlement(layer) is None


def test_a_changed_branch_or_a_moved_target_offers_nothing(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    before = itf.git(app_repo, "rev-parse", "integration")
    itf.commit_on(app_repo, "integration", {"shop.py": "SHOP = 1\n"}, "another run landed")
    found = approvable.derive(layer, RUN)
    assert found["waiting"] is False and found["ship_refused"] == "target-moved"
    assert found["why"] == ("integration moved since the gate judged this run: give it to an agent, or merge it "
                            "by hand")
    result = approvable.approve_ship(layer, RUN, "ana", "cli")
    assert (result["status"], result["reason"], result["message"]) == ("refused", "target-moved", found["why"])
    itf.git(app_repo, "branch", "-f", "integration", before)
    assert approvable.derive(layer, RUN)["waiting"] is True

    commit = _commit(layer)
    itf.commit_on(app_repo, _branch(layer), {"shelf.py": "SHELF = 2\n"}, "a person's change")
    found = approvable.derive(layer, RUN)
    assert found["waiting"] is False and found["ship_refused"] == "branch-changed"
    assert found["why"].startswith(f"a person changed {_branch(layer)} since the gate judged it (")
    assert f"is not {commit[:7]})" in found["why"]
    itf.git(app_repo, "branch", "-D", _branch(layer))
    assert approvable.derive(layer, RUN)["why"] == f"branch {_branch(layer)} does not exist"


# ---------------------------------------------------------------- the action

def test_approve_ship_fast_forwards_the_stage_and_settles_landed(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    main = itf.git(app_repo, "rev-parse", "main")
    commit = _commit(layer)
    token = approvable.derive(layer, RUN)["token"]
    result = approvable.approve_ship(layer, RUN, "  ana  ", "cli", note="agreed in review", token=token)
    assert result == {"status": "merged", "reason": "", "run_id": RUN, "settled": "landed",
                      "message": f"approved by ana: {_branch(layer)} is merged into integration ({commit[:7]})"}
    assert itf.git(app_repo, "rev-parse", "integration") == commit
    assert itf.git(app_repo, "rev-parse", "main") == main
    state = cf.state_of(layer, RUN)
    [given] = state["approvals"]
    assert (given["what"], given["who"], given["via"], given["note"]) == (f"ship:{DIFF}", "ana", "cli",
                                                                         "agreed in review")
    assert "gate" not in given
    assert state["ship"]["shipped"] is True and state["ship"]["approved_by"] == "ana"
    settled = _settlement(layer)
    assert (settled["as"], settled["by"], settled["who"], settled["via"], settled["note"]) == (
        "landed", commit, "ana", "cli", "agreed in review")
    events = _events(layer, RUN)
    assert '"type":"approval.granted"' in events and '"type":"ship.approved"' in events
    assert '"result":"merged"' in events and '"way":"merge"' in events
    stored = json.loads((layer / "agents" / RUN / approvable.RECORD_FILE).read_text(encoding="utf-8"))
    assert stored["waiting"] is False, "the record follows the new state"
    again = approvable.approve_ship(layer, RUN, "ana", "cli")
    assert (again["status"], again["reason"]) == ("refused", "not-waiting")


def test_approve_ship_with_an_open_pull_request_settles_shipped(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, ["class-not-allowed:feature"])
    _patch_ship(layer, RUN, pr={"number": 7, "url": PR})
    before = itf.refs(app_repo)
    result = approvable.approve_ship(layer, RUN, "Robin Example", "dashboard")
    assert (result["status"], result["reason"], result["settled"]) == ("shipped", "", "shipped")
    assert result["message"] == f"approved by Robin Example: shipped to {PR}; it is listed as landed once git shows it"
    assert itf.refs(app_repo) == before, "no ref moves"
    settled = _settlement(layer)
    assert (settled["as"], settled["by"], settled["who"], settled["via"]) == ("shipped", PR, "Robin Example",
                                                                             "dashboard")
    [given] = cf.state_of(layer, RUN)["approvals"]
    assert given["what"] == "ship:class-not-allowed:feature" and "note" not in given
    assert '"result":"shipped"' in _events(layer, RUN)


def test_approve_ship_never_moves_the_default_branch(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF], stage="")
    before = itf.refs(app_repo)
    commit = _commit(layer)
    assert approvable.derive(layer, RUN)["ship"]["way"] == "branch"
    result = approvable.approve_ship(layer, RUN, "ana", "cli")
    assert result["status"] == "shipped"
    assert result["message"] == f"approved by ana: {_branch(layer)} ({commit[:7]}) is ready for main; a person merges it"
    assert itf.refs(app_repo) == before
    settled = _settlement(layer)
    assert (settled["as"], settled["by"]) == ("shipped", commit)


def test_stage_checked_out_goes_to_the_staging_branch(make_layer, app_repo, tmp_path):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    itf.git(app_repo, "worktree", "add", "-q", str(tmp_path / "stage-tree"), "integration")
    before = itf.git(app_repo, "rev-parse", "integration")
    commit = _commit(layer)
    result = approvable.approve_ship(layer, RUN, "ana", "cli")
    assert (result["status"], result["settled"]) == ("shipped", "shipped")
    assert "git merge --ff-only al/ship/integration" in result["message"]
    assert itf.git(app_repo, "rev-parse", "al/ship/integration") == commit
    assert itf.git(app_repo, "rev-parse", "integration") == before
    settled = _settlement(layer)
    assert (settled["as"], settled["by"]) == ("shipped", commit)
    assert '"result":"staged"' in _events(layer, RUN)


def test_stale_token_bad_who_and_a_live_run_are_refused_and_record_nothing(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    before = itf.refs(app_repo)
    stale = approvable.approve_ship(layer, RUN, "ana", "cli", token="0" * 16)
    assert (stale["status"], stale["reason"], stale["message"]) == ("refused", "stale",
                                                                    "what was shown changed: look again")
    for who in ("", "x" * 81, "ana\x07"):
        assert approvable.approve_ship(layer, RUN, who, "cli")["reason"] == "bad-who", repr(who)
    assert approvable.approve_ship(layer, RUN, "ana", "cli", note="two\nlines")["reason"] == "bad-note"
    assert approvable.approve_ship(layer, "0badc0de", "ana", "cli")["reason"] == "unknown-run"
    lock = state_module.hold_run(layer, RUN)
    try:
        held = approvable.approve_ship(layer, RUN, "ana", "cli")
    finally:
        lock.release()
    assert (held["status"], held["reason"]) == ("refused", "running")
    with pytest.raises(ValueError):
        approvable.approve_ship(layer, RUN, "ana", "stand-in")
    assert not cf.state_of(layer, RUN).get("approvals") and _settlement(layer) is None
    assert itf.refs(app_repo) == before and '"ship.approved"' not in _events(layer, RUN)


def test_a_failing_action_keeps_the_approval_and_says_so(make_layer, app_repo, monkeypatch):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    before = itf.refs(app_repo)

    def refuse(layer_, target, commit):
        raise StepFailed("ship", "git push refused: non-fast-forward\nhint: more")

    monkeypatch.setattr(shipapproval, "_fast_forward", refuse)
    result = approvable.approve_ship(layer, RUN, "ana", "cli", note="go")
    assert (result["status"], result["reason"], result["settled"]) == ("refused", "ship-failed", "")
    assert result["message"] == "the approval is recorded, but ship: git push refused: non-fast-forward"
    [given] = cf.state_of(layer, RUN)["approvals"]
    assert (given["what"], given["who"]) == (f"ship:{DIFF}", "ana")
    assert _settlement(layer) is None and itf.refs(app_repo) == before
    events = _events(layer, RUN)
    assert '"type":"ship.approved"' in events and '"result":"failed"' in events


def test_approve_resume_all_takes_a_ship_approval(make_layer, app_repo):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    wrong = approvable.approve_resume(layer, RUN, ["gate"], "ana", "cli")
    assert (wrong["status"], wrong["reason"]) == ("refused", "bad-item")
    result = approvable.approve_resume(layer, RUN, None, "ana", "cli", note="all of them")
    assert (result["status"], result["settled"]) == ("merged", "landed")
    assert itf.git(app_repo, "rev-parse", "integration") == _commit(layer)


def test_dispatch_approve_ship_prints_one_json_line(make_layer, app_repo, monkeypatch):
    layer = _ship_stop(make_layer, app_repo, [DIFF])
    spec = importlib.util.spec_from_file_location("dispatch_under_test", RUNTIME / "dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    monkeypatch.setattr(dispatch, "LAYER", layer)

    def run(*argv: str) -> tuple[int, list[str]]:
        monkeypatch.setattr(sys, "argv", ["dispatch.py", *argv])
        out = io.StringIO()
        with redirect_stdout(out):
            code = dispatch.main()
        return code, out.getvalue().splitlines()

    code, lines = run("--approve-ship", RUN, "--who", "ana", "--token", "0" * 16, "--json")
    assert code == 1 and len(lines) == 1 and json.loads(lines[0])["reason"] == "stale"
    code, lines = run("--approve-ship", RUN, "--who", "ana", "--note", "agreed", "--json")
    assert code == 0 and len(lines) == 1, lines
    answer = json.loads(lines[0])
    assert (answer["status"], answer["run_id"], answer["settled"]) == ("merged", RUN, "landed")
    [given] = cf.state_of(layer, RUN)["approvals"]
    assert (given["who"], given["via"], given["note"]) == ("ana", "dashboard", "agreed")
