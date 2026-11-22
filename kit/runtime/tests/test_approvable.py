"""Approve & resume (approvable.py): for a stopped run, whether it waits only for a person's approval,
what the items are, where it resumes and what happens after; and the one action that records every
approval where the runtime reads it and resumes the run with the mechanism its stop already has."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assign_fixtures as af  # noqa: E402
import convergence_fixtures as cf  # noqa: E402
import integrate_fixtures as itf  # noqa: E402

from adw_modules import (approvable, approvals, assign, decide, evidence, integrate, ir, review,  # noqa: E402
                         state as state_module)
from adw_modules.runner import main  # noqa: E402

RUN = "a9980001"
OTHER = "a9980002"
PATHS = ["docs/DESIGN.md", "docs/GUIDE.md"]


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_DECIDER", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_HOST_ID", "AL_APPROVED_BY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def spawned(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(decide, "spawn_resume", lambda layer, state: calls.append(state.adw_id))
    return calls


def _spawn_double(calls: list[list[str]]):
    def spawn(argv, **popen):
        calls.append(list(argv))
        return SimpleNamespace(pid=0)
    return spawn


def _run(layer: Path, run: str = RUN, *extra: str) -> tuple[int, dict]:
    code = main(cf.ADW, layer, ["Add the feature", "--class", "feature", "--adw-id", run, *extra])
    return code, cf.state_of(layer, run)


def _patch_state(layer: Path, run: str, **changes) -> dict:
    path = layer / "agents" / run / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def _events(layer: Path, run: str) -> str:
    path = layer / "agents" / run / "events.jsonl"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _proof_stop(make_layer, app_repo) -> Path:
    layer, _ = cf.decision_for_a_person(make_layer, app_repo)
    assert _run(layer)[0] == 1
    return layer


def _evidence_stop(make_layer, app_repo, *runs: str) -> Path:
    """Runs whose build changed two protected paths: each stops evidence:needs-person."""
    layer = cf.converge_layer(make_layer, protected=["docs"])
    cf.plan_with(layer, [cf.C1])
    cf.build_writes(layer, {path: "changed\n" for path in PATHS})
    cf.reviewers(layer)
    for run in runs or (RUN,):
        code, state = _run(layer, run)
        stopped = state["proof_plan"]["stopped"]
        assert code == 1 and (stopped["at"], stopped["party"]) == ("evidence", "approver"), state["steps"]
    return layer


def _gate_stop(make_layer, app_repo, tmp_path) -> Path:
    """Handed over, the review passed, the gate's only reasons are protected paths, the worktree is there."""
    layer = itf.conflict_layer(make_layer, app_repo)
    itf.handed_over_run(layer, app_repo, RUN, reasons=("protected-path:deploy/release.cfg",))
    tree = tmp_path / "kept-tree"
    tree.mkdir()
    _patch_state(layer, RUN, worktree=str(tree))
    return layer


def _proposal(make_layer, app_repo) -> tuple[Path, str]:
    layer, run = af.failed_check_run(make_layer, app_repo)
    _patch_state(layer, run, decision={
        "mode": "suggest", "action": "resume-with", "from_step": "test", "stopped_at": "test", "status": "suggested",
        "summary": "the flag file is left over: remove it and run the check again",
        "explanation": "the build left broken.flag", "instructions": "remove broken.flag", "rounds": 0})
    return layer, run


def _waiting_integration(make_layer, app_repo) -> tuple[Path, str]:
    layer = itf.conflict_layer(make_layer, app_repo, approval="person")
    itf.handed_over_run(layer, app_repo, af.RUN)
    itf.move_main(app_repo)
    itf.resolve_script(layer)
    itf.review_script(layer)
    code, out = itf.integrate_main(layer, "run", af.RUN)
    assert code == 75, out
    [new] = itf.integration_runs(layer)
    return layer, new


# ---------------------------------------------------------------- the model

def test_proof_stop_lists_each_approval(make_layer, app_repo):
    layer = _proof_stop(make_layer, app_repo)
    found = approvable.derive(layer, RUN)
    assert (found["version"], found["run"], found["waiting"], found["kind"]) == (1, RUN, True, "proof")
    [item] = found["items"]
    assert (item["id"], item["kind"], item["what"]) == ("vocabulary", "vocabulary", ["vocabulary"])
    assert item["text"] == "vocabulary: needed by C2: the new word is used"   # the row that names the approval
    assert found["from_step"] == "prove" and found["after"] == "resume from prove"
    assert len(found["token"]) == 16 and int(found["token"], 16) >= 0 and found["why"] == ""
    assert approvable.derive(layer, RUN)["token"] == found["token"], "the token follows from what is derived"
    assert approvable.lines(found) == [f"run {RUN} waits for your approval of 1 item(s):", f"  {item['text']}",
                                       "after:     resume from prove"]


def test_evidence_stop_lists_the_protected_paths_as_one_item(make_layer, app_repo):
    layer = _evidence_stop(make_layer, app_repo)
    found = approvable.derive(layer, RUN)
    assert (found["waiting"], found["kind"], found["from_step"]) == (True, "protected-paths", "evidence")
    [item] = found["items"]
    assert item["id"] == "protected-paths" and item["text"].startswith("protected paths: 2 files: docs/DESIGN.md, ")
    assert item["what"] == [f"edit:{path}" for path in PATHS] and item["paths"] == PATHS
    assert found["after"] == "resume from evidence"


def test_gate_item_only_when_the_gate_takes_approvals(make_layer, app_repo, tmp_path, monkeypatch):
    layer = _gate_stop(make_layer, app_repo, tmp_path)
    monkeypatch.setattr(review, "ACCEPTS_APPROVED_EDITS", False, raising=False)
    found = approvable.derive(layer, RUN)
    assert found["waiting"] is False and found["items"] == [] and found["token"] == ""
    assert found["why"] == ("1 protected path: a person merges the pull request (the gate does not take approvals)")
    monkeypatch.setattr(review, "ACCEPTS_APPROVED_EDITS", True, raising=False)
    found = approvable.derive(layer, RUN)
    assert (found["waiting"], found["kind"], found["from_step"]) == (True, "gate", "ship")
    [item] = found["items"]
    assert item["id"] == "gate" and item["what"] == ["edit:deploy/release.cfg"] and item["paths"] == ["deploy/release.cfg"]
    assert item["text"] == "the gate: review passed, a person ships: protected paths: 1 file: deploy/release.cfg"
    assert len(item["decisions"]) == 1
    assert found["after"] == "the ship step runs again: the gate ships it"


def test_stand_in_proposal_is_one_item(make_layer, app_repo):
    layer, run = _proposal(make_layer, app_repo)
    found = approvable.derive(layer, run)
    assert (found["waiting"], found["kind"], found["from_step"]) == (True, "proposal", "test")
    [item] = found["items"]
    assert item["id"] == "proposal"
    assert item["text"] == ("the stand-in proposes resume-with from test: the flag file is left over: remove it and "
                            "run the check again")
    assert found["after"] == "resume from test"
    # a proposal that only gives the run back to a person is not an approval
    _patch_state(layer, run, decision={"mode": "suggest", "action": "ask-human", "status": "suggested",
                                       "summary": "a person looks"})
    assert approvable.derive(layer, run)["kind"] != "proposal"


def test_assign_decision_items_and_what_starts_after(make_layer, app_repo):
    human = {"description": "REQUIRES HUMAN: choose the release channel for the discount",
             "resolution": "a person says which channel"}
    layer, run = af.rejected_run(make_layer, app_repo, blockers=(af.BLOCKER, human))
    loaded = ir.load(layer)
    [decision] = assign.plan(loaded, run)["decisions"]
    assert decision["kind"] == "requires-human"
    found = approvable.derive(layer, run)
    assert (found["waiting"], found["kind"]) == (True, "decision")
    [item] = found["items"]
    assert (item["id"], item["kind"], item["text"]) == (decision["id"], "requires-human", human["description"])
    assert item["decisions"] == [decision["id"]]
    assumed = assign.plan(loaded, run, assume=(decision["id"],))
    assert assumed["kind"] == "blockers" and assumed["blocked"] == ""
    assert found["after"] == assumed["summary"] and found["from_step"] == assumed["from_step"]
    assert not (layer / "agents" / run / "assign.json").exists(), "an assumed approval writes nothing"


def test_protected_path_decisions_are_one_item(make_layer, app_repo):
    layer, run = af.person_run(make_layer, app_repo)
    [decision] = assign.plan(ir.load(layer), run)["decisions"]
    found = approvable.derive(layer, run)
    [item] = found["items"]
    assert (found["kind"], item["id"], item["kind"]) == ("decision", "protected-paths", "protected-path")
    assert item["text"] == "protected paths: 1 file: deploy/release.cfg" and item["decisions"] == [decision["id"]]


def test_derive_only_reads(make_layer, app_repo):
    layer, run = af.person_run(make_layer, app_repo)
    af.write_attempts(layer, [{"outcome": "started", "run": "0badc0de", "by": "decider"}], run)
    before = (layer / "agents" / run / "assign.json").read_text(encoding="utf-8")
    approvable.derive(layer, run)
    assert (layer / "agents" / run / "assign.json").read_text(encoding="utf-8") == before
    assert '"assign.outcome"' not in _events(layer, run)
    assert not (layer / "agents" / run / approvable.RECORD_FILE).exists()


def test_integration_approve_step_is_one_item(make_layer, app_repo):
    layer, new = _waiting_integration(make_layer, app_repo)
    for asked in (af.RUN, new):
        found = approvable.derive(layer, asked)
        assert (found["run"], found["waiting"], found["kind"], found["from_step"]) == (asked, True, "integration",
                                                                                      "approve")
        [item] = found["items"]
        assert item["id"] == "integration"
        assert item["text"] == f"integration run {new}: checks and review passed, its approve step waits for a person"
        assert found["after"] == f"integration run {new} ships through the gate"


def test_needs_declaration_offers_nothing(make_layer, app_repo):
    layer, _ = cf.proof_unprovable_here(make_layer, app_repo)
    code, state = _run(layer)
    assert code == 1
    plan = state["proof_plan"]
    plan["stopped"].update(party="layer author", outcome="needs-declaration")
    _patch_state(layer, RUN, proof_plan=plan)
    found = approvable.derive(layer, RUN)
    assert found["waiting"] is False and found["items"] == [] and found["token"] == "" and found["kind"] == ""
    assert found["why"] == "stopped for the layer author: only the layer author clears it"
    assert approvable.lines(found) == [f"run {RUN} does not wait for an approval: {found['why']}"]
    plan["stopped"]["party"] = "operator"
    _patch_state(layer, RUN, proof_plan=plan)
    assert approvable.derive(layer, RUN)["why"] == "stopped for the operator: only the operator clears it"


def test_a_persons_only_decision_offers_nothing(make_layer, app_repo):
    layer, run = af.failed_check_run(make_layer, app_repo)
    _patch_state(layer, run, budget={"stopped": {"limit": "run", "spent": 3.0, "of": 2.0, "step": "build",
                                                 "reason": "the run's spending limit"}})
    found = approvable.derive(layer, run)
    assert found["waiting"] is False and found["items"] == []
    assert found["why"].startswith("a person's only: a spending limit stopped the run")


def test_a_check_failure_is_not_an_approval(make_layer, app_repo):
    layer, run = af.failed_check_run(make_layer, app_repo)
    found = approvable.derive(layer, run)
    assert found["waiting"] is False and found["items"] == [] and found["token"] == ""
    assert "resume from check test" in found["why"]


def test_unknown_passed_and_running_runs_offer_nothing(make_layer, app_repo):
    layer = _proof_stop(make_layer, app_repo)
    assert approvable.derive(layer, "0badc0de")["why"] == "run 0badc0de has no readable state"
    assert approvable.write(layer, "0badc0de")["waiting"] is False
    assert not (layer / "agents" / "0badc0de").exists(), "an unknown run writes nothing"
    _patch_state(layer, RUN, status="passed")
    assert approvable.derive(layer, RUN)["why"] == f"run {RUN} passed: nothing stopped it"
    _patch_state(layer, RUN, status="failed", fanout_parent={"run": OTHER, "step": "pick", "index": 0})
    assert approvable.derive(layer, RUN)["why"] == "a fan-out candidate: its parent decides"
    _patch_state(layer, RUN, fanout_parent={}, promotion={"from": "dev", "to": "main"})
    assert approvable.derive(layer, RUN)["why"] == "a promotion is decided with `al promote`"


def test_write_stores_the_record_with_basis_and_token(make_layer, app_repo):
    layer = _proof_stop(make_layer, app_repo)
    record = approvable.write(layer, RUN)
    path = layer / "agents" / RUN / approvable.RECORD_FILE
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == record and stored["waiting"] is True and stored["token"]
    assert stored["basis"] == approvable.basis(layer, RUN)
    status, ended, approved, *_rest = stored["basis"].split("|")
    assert (status, approved) == ("failed", "0") and ended
    state = state_module.load(layer, RUN)
    approvals.record(state, ["dependency:requests"], "ana", "cli")
    state.save(layer)
    assert approvable.basis(layer, RUN) != stored["basis"]
    assert approvable.derive(layer, RUN)["token"] != stored["token"]


# ---------------------------------------------------------------- approve & resume

def test_approve_resume_records_and_resumes_from_the_stopped_step(make_layer, app_repo, spawned):
    layer = _proof_stop(make_layer, app_repo)
    token = approvable.derive(layer, RUN)["token"]
    result = approvable.approve_resume(layer, RUN, ["vocabulary"], "  ana  ", "cli", note="agreed in review",
                                       token=token)
    assert result == {"status": "resumed", "reason": "", "run_id": RUN,
                      "message": f"approved vocabulary for run {RUN}; resuming from step prove"}
    state = cf.state_of(layer, RUN)
    [given] = state["approvals"]
    assert (given["what"], given["who"], given["via"], given["note"]) == ("vocabulary", "ana", "cli",
                                                                         "agreed in review")
    assert given["at"].endswith("+00:00") and "gate" not in given
    assert next(r for r in state["steps"] if r["name"] == "prove")["status"] == "pending"
    assert spawned == [RUN]
    events = _events(layer, RUN)
    assert '"type":"approval.granted"' in events and '"note":"agreed in review"' in events
    stored = json.loads((layer / "agents" / RUN / approvable.RECORD_FILE).read_text(encoding="utf-8"))
    assert stored["waiting"] is False, "the record follows the new state"


def test_approve_resume_of_protected_paths_marks_them_for_the_gate(make_layer, app_repo, spawned):
    layer = _evidence_stop(make_layer, app_repo)
    result = approvable.approve_resume(layer, RUN, None, "ana", "dashboard")
    assert result["status"] == "resumed" and result["message"].endswith("resuming from step evidence")
    state = cf.state_of(layer, RUN)
    assert [(a["what"], a["who"], a["via"], a["gate"]) for a in state["approvals"]] == [
        (f"edit:{path}", "ana", "dashboard", True) for path in PATHS]
    assert next(r for r in state["steps"] if r["name"] == "evidence")["status"] == "pending"
    assert spawned == [RUN] and '"gate":true' in _events(layer, RUN)


def test_approve_resume_gate_runs_the_ship_step_again(make_layer, app_repo, tmp_path, monkeypatch, spawned):
    layer = _gate_stop(make_layer, app_repo, tmp_path)
    monkeypatch.setattr(review, "ACCEPTS_APPROVED_EDITS", True, raising=False)
    before = cf.state_of(layer, RUN)["ship"]
    found = approvable.derive(layer, RUN)
    result = approvable.approve_resume(layer, RUN, ["gate"], "ana", "cli", note="release agreed", token=found["token"])
    assert result["status"] == "resumed" and result["run_id"] == RUN
    assert result["message"] == f"approved 1 protected path(s) for run {RUN}; the ship step runs again"
    state = cf.state_of(layer, RUN)
    [given] = state["approvals"]
    assert (given["what"], given["who"], given["via"], given["gate"], given["note"]) == (
        "edit:deploy/release.cfg", "ana", "cli", True, "release agreed")
    [approved] = af.record(layer, RUN)["approvals"]
    assert approved["id"] == found["items"][0]["decisions"][0]
    assert (approved["kind"], approved["path"], approved["by"], approved["via"]) == (
        "protected-path", "deploy/release.cfg", "ana", "cli")
    assert next(r for r in state["steps"] if r["name"] == "ship")["status"] == "pending"
    assert state["ship"] == {"previous_handoff": before}
    assert spawned == [RUN]
    events = _events(layer, RUN)
    assert '"type":"approval.granted"' in events and '"type":"assign.approval"' in events
    assert approvals.gate_approved(state_module.load(layer, RUN), ["deploy/release.cfg", "stock.py"]) == [
        "deploy/release.cfg"]


def test_approve_resume_proposal_answers_go_with_who(make_layer, app_repo, spawned):
    layer, run = _proposal(make_layer, app_repo)
    result = approvable.approve_resume(layer, run, ["proposal"], "Robin Example", "dashboard", note="go ahead")
    assert result["status"] == "resumed" and result["run_id"] == run, result
    assert result["message"].endswith("resuming from step test")
    decision = cf.state_of(layer, run)["decision"]
    last = decision["answers"][-1]
    assert (last["kind"], last["who"], last["via"], last["text"]) == ("go", "Robin Example", "dashboard", "go ahead")
    assert decision["status"] == "resuming" and spawned == [run]
    assert "by Robin Example" in (layer / "agents" / run / "decision.md").read_text(encoding="utf-8")


def test_approve_resume_decision_approves_then_gives_to_an_agent(make_layer, app_repo):
    layer, run = af.person_run(make_layer, app_repo)
    found = approvable.derive(layer, run)
    calls: list[list[str]] = []
    result = approvable.approve_resume(layer, run, None, "ana", "dashboard", note="the channel stays",
                                       token=found["token"], spawn=_spawn_double(calls))
    assert result["status"] == "resumed", result
    record = af.record(layer, run)
    [approved] = record["approvals"]
    assert (approved["id"], approved["by"], approved["via"]) == (found["items"][0]["decisions"][0], "ana", "dashboard")
    [attempt] = record["attempts"]
    assert (attempt["outcome"], attempt["kind"], attempt["by"], attempt["who"], attempt["note"]) == (
        "started", "blockers", "dashboard", "ana", "the channel stays")
    assert result["run_id"] == attempt["run"] != run and len(calls) == 1
    assert result["run_id"] in calls[0] and result["run_id"] in result["message"]


def test_approve_resume_integration_approves_its_step(make_layer, app_repo):
    layer, new = _waiting_integration(make_layer, app_repo)
    calls: list[list[str]] = []
    result = approvable.approve_resume(layer, af.RUN, ["integration"], "ana", "cli", spawn=_spawn_double(calls))
    assert result["status"] == "resumed" and result["run_id"] == new, result
    assert itf.record_of(layer, new, "approval.json")["by"] == "ana"
    assert len(calls) == 1 and "--resume" in calls[0] and new in calls[0]


def test_a_live_run_is_refused_and_nothing_is_recorded(make_layer, app_repo, spawned):
    layer = _proof_stop(make_layer, app_repo)
    lock = state_module.hold_run(layer, RUN)
    try:
        result = approvable.approve_resume(layer, RUN, None, "ana", "cli")
    finally:
        lock.release()
    assert (result["status"], result["reason"], result["run_id"]) == ("refused", "running", RUN)
    assert result["message"].startswith(f"run {RUN} is held by process ")
    assert "approvals" not in cf.state_of(layer, RUN) and spawned == []


def test_stale_token_and_unknown_item_are_refused(make_layer, app_repo, spawned):
    layer = _proof_stop(make_layer, app_repo)
    token = approvable.derive(layer, RUN)["token"]
    stale = approvable.approve_resume(layer, RUN, ["vocabulary"], "ana", "cli", token="0" * 16)
    assert (stale["status"], stale["reason"], stale["message"]) == ("refused", "stale",
                                                                    "what was shown changed: look again")
    unknown = approvable.approve_resume(layer, RUN, ["vocabulary", "edit:docs/x.md"], "ana", "cli", token=token)
    assert (unknown["status"], unknown["reason"]) == ("refused", "bad-item") and "open: vocabulary" in unknown["message"]
    left_out = approvable.approve_resume(layer, RUN, [], "ana", "cli", token=token)
    assert (left_out["status"], left_out["reason"]) == ("refused", "stale")
    assert "approvals" not in cf.state_of(layer, RUN) and spawned == []
    assert approvable.approve_resume(layer, "0badc0de", None, "ana", "cli")["reason"] == "unknown-run"
    assert approvable.approve_resume(layer, "not-a-run", None, "ana", "cli")["reason"] == "unknown-run"


def test_nothing_waiting_is_refused(make_layer, app_repo, spawned):
    layer, _ = cf.proof_unprovable_here(make_layer, app_repo)
    assert _run(layer)[0] == 1
    result = approvable.approve_resume(layer, RUN, None, "ana", "cli")
    assert (result["status"], result["reason"]) == ("refused", "not-waiting")
    assert result["message"] == "stopped for the operator: only the operator clears it"
    assert "approvals" not in cf.state_of(layer, RUN) and spawned == []


def test_bad_who_and_bad_note_are_refused(make_layer, app_repo, spawned):
    layer = _proof_stop(make_layer, app_repo)
    for who in ("", "   ", "x" * 81, "ana\x07"):
        assert approvable.approve_resume(layer, RUN, None, who, "cli")["reason"] == "bad-who", repr(who)
    for note in ("first line\nsecond line", "x" * 501, "bell\x07"):
        result = approvable.approve_resume(layer, RUN, None, "ana", "cli", note=note)
        assert (result["status"], result["reason"]) == ("refused", "bad-note"), repr(note)
    assert "approvals" not in cf.state_of(layer, RUN) and spawned == []
    with pytest.raises(ValueError):
        approvable.approve_resume(layer, RUN, None, "ana", "stand-in")


# ---------------------------------------------------------------- the gate mark

def _stack(cwd: Path):
    return SimpleNamespace(podman_option=None, cwd=cwd, sandbox=None, read_only=False,
                           wrap=lambda cmd: ["bash", "-c", cmd], process_env=lambda: {"PATH": "/usr/bin:/bin"})


def test_protected_path_approval_holds_for_that_run_only(make_layer, app_repo, spawned):
    layer = _evidence_stop(make_layer, app_repo, RUN, OTHER)
    assert approvable.approve_resume(layer, RUN, None, "ana", "cli")["status"] == "resumed"
    loaded = ir.load(layer)
    adw = loaded.adw(cf.ADW)
    first, second = state_module.load(layer, RUN), state_module.load(layer, OTHER)
    assert evidence.protected_unapproved(loaded, first, adw, _stack(Path(first.worktree))) == []
    assert evidence.protected_unapproved(loaded, second, adw, _stack(Path(second.worktree))) == PATHS
    assert approvals.gate_approved(first, PATHS) == PATHS
    assert approvals.gate_approved(second, PATHS) == []
    assert approvable.derive(layer, OTHER)["waiting"] is True, "the other run still waits for its own approval"


def test_a_start_time_pre_approval_does_not_lower_the_gate():
    state = state_module.ADWState(adw_id=RUN, adw="demo")
    approvals.record(state, ["edit:docs/*"], "ana", "start")
    assert approvals.gate_approved(state, PATHS) == []
    assert approvals.record(state, ["edit:docs/*"], "ana", "cli") == [], "the same approval is not recorded twice"
    [added] = approvals.record(state, ["edit:docs/*"], "Robin Example", "dashboard", note=" agreed\n in review ",
                               gate=True)
    assert (added["who"], added["gate"], added["note"]) == ("Robin Example", True, "agreed in review")
    assert len(state.approvals) == 2 and "gate" not in state.approvals[0] and "note" not in state.approvals[0]
    assert approvals.gate_approved(state, [*PATHS, "kit/x.py"]) == PATHS
    assert approvals.record(state, ["edit:docs/*"], "ana", "cli", gate=True) == [], "marked once"


def test_gate_takes_approvals_reads_the_review_module(monkeypatch):
    assert approvals.gate_takes_approvals() is bool(getattr(review, approvals.GATE_CAPABILITY, False))
    monkeypatch.delattr(review, approvals.GATE_CAPABILITY, raising=False)
    assert approvals.gate_takes_approvals() is False
    monkeypatch.setattr(review, approvals.GATE_CAPABILITY, True, raising=False)
    assert approvals.gate_takes_approvals() is True
