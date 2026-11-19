"""Approvals (approvals.py): decisions only a person may take, given before a run (--approve) or after
it stopped for one (al approve, the dashboard, the stand-in's `go`), each recorded with who, when and
how; and what the stand-in decider does with a run stopped for an approver, a layer author or an
operator."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convergence_fixtures as cf  # noqa: E402

from adw_modules import approvals, decide, ir, state as state_module  # noqa: E402
from adw_modules.runner import main  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

RUN = "a9970001"


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


def _run(layer: Path, *extra: str) -> tuple[int, dict]:
    code = main(cf.ADW, layer, ["Add the feature", "--class", "feature", "--adw-id", RUN, *extra])
    return code, cf.state_of(layer, RUN)


# ---------------------------------------------------------------- state

def test_state_without_new_fields_saves_as_before(tmp_path):
    state = ADWState(adw_id=RUN, adw="demo", prompt="p")
    data = json.loads(state.save(tmp_path).read_text())
    assert "proof_plan" not in data and "approvals" not in data


def test_state_round_trips_proof_plan_and_approvals(tmp_path):
    state = ADWState(adw_id=RUN, adw="demo", prompt="p")
    state.proof_plan = {"plan": "specs/p.md", "table": "parsed", "rows": [{"id": "C1", "state": "planned"}]}
    approvals.record(state, ["vocabulary"], "ana", "start")
    state.save(tmp_path)
    loaded = state_module.load(tmp_path, RUN)
    assert loaded.proof_plan == state.proof_plan
    assert loaded.approvals == state.approvals and loaded.approvals[0]["who"] == "ana"


# ---------------------------------------------------------------- the grammar

@pytest.mark.parametrize("token", ["vocabulary", "edit:docs/DESIGN.md", "dependency:requests", "edit:kit/**",
                                   "layer-change", "a"])
def test_tokens_valid_and_invalid(token):
    assert approvals.parse(f"  {token} ") == token
    for bad in ("", "Vocabulary", "edit:", "edit:a b", "9lives", "-x", "edit:" + "x" * 201):
        with pytest.raises(ValueError, match="an approval is a word"):
            approvals.parse(bad)
    assert approvals.parse("edit:" + "x" * 200)


def test_edit_glob_covers_protected_path():
    assert approvals.matches("edit:kit/runtime/adw_modules/review.py", "edit:kit/runtime/adw_modules/review.py")
    assert approvals.matches("edit:kit/runtime/*", "edit:kit/runtime/adw_modules/review.py")
    assert approvals.matches("edit:kit/runtime", "edit:kit/runtime/adw_modules/review.py")   # under the entry
    assert approvals.matches("edit:docs/*.md", "edit:docs/DESIGN.md")
    assert not approvals.matches("edit:docs/*.md", "edit:kit/x.py")
    assert not approvals.matches("vocabulary", "dependency:requests")
    state = ADWState(adw_id=RUN, adw="demo")
    approvals.record(state, ["edit:tests/authored"], "ana", "cli")
    assert approvals.covered(state, "edit:tests/authored/case.py")["who"] == "ana"
    assert approvals.covered(state, "edit:tests/other.py") is None


# ---------------------------------------------------------------- how approvals come in

def test_runner_records_pre_approvals_with_who_and_when(make_layer, app_repo):
    layer = cf.converge_layer(make_layer)
    cf.plan_with(layer, None)
    cf.build_writes(layer)
    cf.reviewers(layer)
    code, state = _run(layer, "--approve", "vocabulary", "--approve", "edit:docs/*.md", "--approve", "vocabulary",
                       "--approved-by", "ana")
    assert code == 0
    assert [(a["what"], a["who"], a["via"]) for a in state["approvals"]] == [
        ("vocabulary", "ana", "start"), ("edit:docs/*.md", "ana", "start")]
    assert all(a["at"].endswith("+00:00") for a in state["approvals"])
    bad = main(cf.ADW, layer, ["again", "--adw-id", "a9970002", "--approve", "Not A Token"])
    assert bad == 2


def test_grant_refuses_a_run_not_stopped_for_an_approver(make_layer, app_repo, spawned):
    layer, _ = cf.proof_unprovable_here(make_layer, app_repo)
    assert _run(layer)[0] == 1
    result = approvals.grant_and_resume(layer, RUN, ["vocabulary"], "ana", "cli")
    assert result["status"] == "refused" and result["reason"] == "not-stopped-for-an-approver"
    assert "stopped for the operator" in result["message"]
    assert spawned == [] and "approvals" not in cf.state_of(layer, RUN)
    assert approvals.grant(layer, "nothere1", ["x"], "ana", "cli")["reason"] == "unknown-run"
    assert approvals.grant(layer, RUN, ["Bad Token"], "ana", "cli")["reason"] == "bad-approval"


def test_grant_and_resume_starts_the_resume(make_layer, app_repo, spawned):
    layer, _ = cf.decision_for_a_person(make_layer, app_repo)
    assert _run(layer)[0] == 1
    result = approvals.grant_and_resume(layer, RUN, ["vocabulary"], "ana", "dashboard")
    assert result["status"] == "resumed" and "resuming from step prove" in result["message"]
    assert spawned == [RUN]
    state = cf.state_of(layer, RUN)
    assert [(a["what"], a["who"], a["via"]) for a in state["approvals"]] == [("vocabulary", "ana", "dashboard")]
    assert next(r for r in state["steps"] if r["name"] == "prove")["status"] == "pending"
    events = (layer / "agents" / RUN / "events.jsonl").read_text()
    assert '"type":"approval.granted"' in events


def test_go_answer_grants_the_listed_approvals(make_layer, app_repo, spawned):
    layer, _ = cf.decision_for_a_person(make_layer, app_repo)
    assert _run(layer, "--decider", "suggest")[0] == 1
    assert cf.state_of(layer, RUN)["decision"]["approvals"] == ["vocabulary"]
    result = decide.answer(layer, RUN, "go", "", "cli")
    assert result["status"] == "resuming"
    state = cf.state_of(layer, RUN)
    assert [(a["what"], a["via"]) for a in state["approvals"]] == [("vocabulary", "answer:cli")]
    assert spawned == [RUN]


def test_decider_never_auto_resumes_a_needs_person_stop(make_layer, app_repo, spawned):
    layer, _ = cf.decision_for_a_person(make_layer, app_repo)
    assert _run(layer, "--decider", "auto")[0] == 1
    state = cf.state_of(layer, RUN)
    assert (state["decision"]["mode"], state["decision"]["status"]) == ("suggest", "suggested")
    assert "decide" not in cf.agent_steps(state)   # no model call
    assert spawned == []
    record = (layer / "agents" / RUN / "decision.md").read_text()
    assert "may this run vocabulary?" in record


def test_layer_author_stop_is_not_eligible(make_layer):
    layer = ir.load(cf.converge_layer(make_layer))
    state = ADWState(adw_id=RUN, adw=cf.ADW, status="failed")
    state.ship = {"action": "handoff", "target": "PR.md"}
    state.proof_plan = {"table": "parsed", "stopped": {"at": "prove", "party": "layer author",
                                                       "reason": "proof:needs-declaration: 1 criterion(s)\nC1 | …"}}
    adw = layer.adw(cf.ADW)
    assert decide.eligible(layer, state, adw, "auto") == ("stopped for the layer author: proof:needs-declaration: "
                                                          "1 criterion(s)")
    state.proof_plan["stopped"]["party"] = "operator"
    assert decide.eligible(layer, state, adw, "suggest").startswith("stopped for the operator: ")
    state.proof_plan["stopped"]["party"] = "person"
    assert decide.eligible(layer, state, adw, "suggest") == ""
    assert decide.eligible(layer, SimpleNamespace(**{**state.__dict__, "proof_plan": {}}), adw, "off") == \
        "the decider is off"
