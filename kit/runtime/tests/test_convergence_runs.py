"""Reviews that converge, end to end (runner.main, mock agent, real git): each of the five causes of
review rounds that change nothing ends before a second review round, with its reason and the party
who can clear it; the normal case passes in one round; a plan without a Proof plan table runs exactly
as before."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convergence_fixtures as cf  # noqa: E402

from adw_modules import approvals, decide  # noqa: E402
from adw_modules.runner import main  # noqa: E402

RUN = "c0de0001"


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_DECIDER", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_HOST_ID", "ADW_FANOUT_PARENT",
                "AL_APPROVED_BY"):
        monkeypatch.delenv(key, raising=False)


def _run(layer: Path, *extra: str, adw_id: str = RUN) -> tuple[int, dict]:
    code = main(cf.ADW, layer, ["Add the feature", "--class", "feature", "--adw-id", adw_id, *extra])
    return code, cf.state_of(layer, adw_id)


def _no_second_round(state: dict) -> None:
    assert not [step for step in cf.agent_steps(state) if step.startswith("review-2-")]


def _stopped(state: dict) -> dict:
    return (state.get("proof_plan") or {}).get("stopped") or {}


def _failed_step(state: dict) -> dict:
    return next(record for record in state["steps"] if record.get("status") == "failed")


def _reason(state: dict) -> str:
    """The failed step's reason, without the `STEP: ` its record starts with."""
    record = _failed_step(state)
    return str(record["detail"]).removeprefix(f"{record['name']}: ")


def test_cause1_proof_not_reaching_stops_at_prove_needs_declaration(make_layer, app_repo):
    layer, _ = cf.proof_not_reaching(make_layer, app_repo)
    code, state = _run(layer)
    assert code == 1 and state["status"] == "failed"
    stopped = _stopped(state)
    assert (stopped["at"], stopped["outcome"], stopped["party"]) == ("prove", "needs-declaration", "layer author")
    assert _failed_step(state)["name"] == "prove"
    reason = _reason(state)
    assert reason.startswith("proof:needs-declaration: 1 criterion(s) cannot be proven here; party: layer author")
    assert "#:max-files 30" in reason and "or write them under proof:" in reason
    assert "build" not in cf.agent_steps(state)
    _no_second_round(state)


def test_cause2_unprovable_stops_at_prove_before_build(make_layer, app_repo):
    layer, _ = cf.proof_unprovable_here(make_layer, app_repo)
    code, state = _run(layer)
    assert code == 1
    stopped = _stopped(state)
    assert (stopped["at"], stopped["outcome"], stopped["party"]) == ("prove", "unprovable", "operator")
    assert _reason(state).startswith("proof:unprovable:")
    assert "al-no-such-tool" in stopped["reason"]
    assert "build" not in cf.agent_steps(state)
    _no_second_round(state)
    assert "proof:     stopped at prove: unprovable (operator)" in (layer / "agents" / RUN / "run.log").read_text()


def test_cause3_needs_person_stops_after_plan_with_one_question(make_layer, app_repo):
    layer, _ = cf.decision_for_a_person(make_layer, app_repo)
    code, state = _run(layer, "--decider", "auto")
    assert code == 1
    stopped = _stopped(state)
    assert (stopped["at"], stopped["outcome"], stopped["party"]) == ("prove", "needs-person", "approver")
    assert stopped["approvals"] == ["vocabulary"]
    assert stopped["question"] == f"may this run vocabulary? answer: al approve LAYER {RUN} vocabulary"
    assert "build" not in cf.agent_steps(state) and "decide" not in cf.agent_steps(state)
    # the stand-in writes one question for a person and never resumes by itself, even in auto
    decision = state["decision"]
    assert (decision["mode"], decision["status"], decision["approvals"]) == ("suggest", "suggested", ["vocabulary"])
    _no_second_round(state)


def test_cause3_pre_approved_runs_through_in_one_round(make_layer, app_repo):
    layer, _ = cf.decision_for_a_person(make_layer, app_repo)
    code, state = _run(layer, "--approve", "vocabulary", "--approved-by", "ana")
    assert code == 0 and state["status"] == "passed", state.get("steps")
    (given,) = state["approvals"]
    assert (given["what"], given["who"], given["via"]) == ("vocabulary", "ana", "start")
    assert len(state["review"]["rounds"]) == 1
    assert cf.step_names(state) == ["plan", "prove", "build", "unit", "commit", "review", "evidence", "ship"]


def test_cause3_al_approve_after_stop_resumes_and_passes(make_layer, app_repo):
    layer, _ = cf.decision_for_a_person(make_layer, app_repo)
    code, _state = _run(layer)
    assert code == 1
    granted = approvals.grant(layer, RUN, ["vocabulary"], "ana", "cli")
    assert granted["status"] == "granted"
    code, state = _run(layer, "--resume")
    assert code == 0 and state["status"] == "passed", state.get("steps")
    assert [item["via"] for item in state["approvals"]] == ["cli"]
    assert len(state["review"]["rounds"]) == 1
    assert not _stopped(state)
    assert cf.agent_steps(state).count("plan") == 1   # the plan is not made again


def test_cause4_no_progress_stops_in_evidence_without_a_reviewer(make_layer, app_repo):
    layer, _ = cf.no_progress(make_layer, app_repo)
    code, state = _run(layer)
    assert code == 1
    stopped = _stopped(state)
    assert (stopped["at"], stopped["outcome"], stopped["party"]) == ("evidence", "no-progress", "person")
    assert _reason(state).startswith("evidence:no-progress: C2 still files-missing after 2 patches")
    calls = cf.agent_steps(state)
    assert [step for step in calls if step.startswith("evidence-")] == [
        "evidence-patch-1", "evidence-implement-1", "evidence-patch-2", "evidence-implement-2"]
    assert not [step for step in calls if step.startswith("review")]
    prompt = (layer / "agents" / RUN / "evidence-patch-2" / "prompt.md").read_text()
    assert "take a different approach" in prompt and "FIRST_ATTEMPT = 1" in prompt
    first = (layer / "agents" / RUN / "evidence-patch-1" / "prompt.md").read_text()
    assert "- C2: files-missing: the report is written" in first
    assert "Resolution: make `unit` prove it for this tree" in first


def test_cause5_target_moved_conflict_stops_before_review(make_layer, app_repo):
    layer, _ = cf.target_moved(make_layer, app_repo)
    code, state = _run(layer)
    assert code == 1
    stopped = _stopped(state)
    assert (stopped["at"], stopped["outcome"], stopped["party"]) == ("freshen-review", "target-moved", "person")
    assert _reason(state).startswith("target-moved: feature.py conflict with main; party: person")
    assert "freshen-resolve" in cf.agent_steps(state)
    assert not [step for step in cf.agent_steps(state) if step.startswith("review")]
    worktree = Path(state["worktree"])
    assert cf.git(worktree, "status", "--porcelain") == ""   # the merge was aborted


def test_cause5_target_moved_clean_is_merged_and_reviewed_once(make_layer, app_repo):
    layer, _ = cf.target_moved(make_layer, app_repo, clean=True)
    code, state = _run(layer)
    assert code == 0 and state["status"] == "passed", state.get("steps")
    freshen = state["proof_plan"]["freshen"]["review"]
    assert freshen["behind"] == 1 and freshen["touched"] == ["feature.py"] and freshen["merged"] is True
    assert freshen["checks_rerun"] == ["unit"]
    assert len(state["review"]["rounds"]) == 1
    assert "freshen-review" in cf.step_names(state)
    log = cf.git(Path(state["worktree"]), "log", "--format=%s", "-3")
    assert "merge main into" in log


def test_normal_change_with_complete_evidence_passes_in_one_round(make_layer, app_repo):
    layer, _ = cf.normal(make_layer, app_repo)
    code, state = _run(layer)
    assert code == 0 and state["status"] == "passed", state.get("steps")
    assert len(state["review"]["rounds"]) == 1
    assert cf.step_names(state) == ["plan", "prove", "build", "unit", "commit", "review", "evidence", "ship"]
    assert [row["state"] for row in state["proof_plan"]["rows"]] == ["proven", "proven"]
    manifest = json.loads((layer / "artefacts" / RUN / "manifest.json").read_text())
    (evidence,) = [entry for entry in manifest if entry.get("type") == "evidence"]
    assert set(evidence["states"].values()) == {"proven"}
    assert {entry["type"] for entry in manifest} >= {"evidence", "approvals", "round"}
    proof = sorted(Path(entry["path"]).name for entry in manifest if str(entry.get("id")).startswith("proof-"))
    assert proof == ["a.png", "b.png", "c.png", "report.json"]
    assert state["proof_plan"]["summary"]["rounds"] == 1
    assert "proof:     2 criteria, 2 proven; 1 round" in (layer / "agents" / RUN / "run.log").read_text()
    # the run-scoped proof never reaches the branch
    assert ".al-proof" not in cf.git(Path(state["worktree"]), "ls-files")


def test_plan_without_proof_table_runs_as_before(make_layer, app_repo):
    layer = cf.converge_layer(make_layer)
    cf.plan_with(layer, None)
    cf.build_writes(layer)
    cf.reviewers(layer)
    code, state = _run(layer)
    assert code == 0 and state["status"] == "passed"
    assert cf.step_names(state) == cf.STEPS
    assert state["proof_plan"] == {"plan": state["plan"], "table": "absent"}
    assert "approvals" not in state
    manifest = json.loads((layer / "artefacts" / RUN / "manifest.json").read_text())
    assert not [entry for entry in manifest if entry.get("type") in ("evidence", "approvals", "round")]
    assert "proof:" not in (layer / "agents" / RUN / "run.log").read_text().split("=== ADW summary ===")[-1]


def test_debt_from_a_passing_review_is_filed_as_tasks(make_layer, app_repo):
    debt = {"severity": "tech_debt", "description": "feature.py: FEATURE is a module constant",
            "resolution": "read it from the settings", "artefact": ""}
    layer, _ = cf.normal(make_layer, app_repo, tasklist=True,
                         verdict={"success": True, "summary": "ok", "issues": [debt]})
    code, state = _run(layer)
    assert code == 0 and state["status"] == "passed"
    (item,) = state["proof_plan"]["debt"]
    assert item["text"] == (f"debt from run {RUN}: feature.py: FEATURE is a module constant — "
                            "read it from the settings")
    assert item["where"].startswith("tasklist:tasks.md:")
    tasks = (layer / "tasks.md").read_text()
    assert "## Debt" in tasks and f"- [ ] debt from run {RUN}: feature.py" in tasks
    assert "1 debt task filed" in (layer / "agents" / RUN / "run.log").read_text()


def _events(layer: Path, adw_id: str = RUN) -> list[dict]:
    path = layer / "agents" / adw_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_pass_carried_over_an_unrelated_patch_proves_the_rows(make_layer, app_repo):
    layer, _ = cf.carried(make_layer, app_repo, touch="README.md")
    code, state = _run(layer)
    assert code == 0 and state["status"] == "passed", (state.get("steps"), _stopped(state))
    assert [step for step in cf.agent_steps(state) if step.startswith("evidence-")] == [
        "evidence-patch-1", "evidence-implement-1"]
    assert [row["state"] for row in state["proof_plan"]["rows"]] == ["proven", "proven"]
    first = next(entry for entry in state["check_results"] if entry["check"] == "unit" and entry["phase"] == "check")
    c1 = next(row for row in state["proof_plan"]["pack"]["rows"] if row["id"] == "C1")
    assert c1["tree"] == "carried"
    assert c1["carried"]["changed"] == ["README.md"]
    assert c1["carried"]["from_tree"] == first["tree"]
    assert state["checks"]["unit"]["carried"]["from_tree"] == first["tree"]
    carried = [e for e in _events(layer) if e["type"] == "check.carried" and e.get("cmd") == "unit"]
    assert len(carried) == 1
    assert carried[0]["from_tree"] == first["tree"] and carried[0]["changed"] == ["README.md"]


def test_stale_row_whose_check_is_skipped_is_run_once_not_patched(make_layer, app_repo):
    layer, _ = cf.carried(make_layer, app_repo, touch="tests/test_feature.py")
    code, state = _run(layer)
    assert code == 0 and state["status"] == "passed", (state.get("steps"), _stopped(state))
    assert "evidence-patch-2" not in cf.agent_steps(state)
    rechecks = [entry["outcome"] for entry in state["check_results"]
                if entry["check"] == "unit" and entry["phase"] == "recheck"]
    assert rechecks.count("passed") == 1
    assert "not-affected" in rechecks and rechecks.index("not-affected") < rechecks.index("passed")
    assert [r["forced"] for r in state["proof_plan"]["gaps"] if r.get("forced")] == [["unit"]]
    c1 = next(row for row in state["proof_plan"]["pack"]["rows"] if row["id"] == "C1")
    assert (c1["state"], c1["tree"]) == ("proven", "current")
