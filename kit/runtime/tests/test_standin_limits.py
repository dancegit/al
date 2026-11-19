"""The limits no setting lifts (standin.hard_limit): whatever the mode, the declared scope and the
local setting say, the stand-in never acts on a run a person touched since it stopped, never
overrides a rejected review, never lands a protected-path change or work with a failing required
check, never approves its own work, and never keeps a decision from a call that changed files.
Each test gives it the widest scope there is (mode auto, every kind in #:may and in the local
setting) and the mock agent proposing exactly the forbidden action."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_decide import ADW_ID, _state  # noqa: E402
from test_standin import (ALL, declared, decider, handed_over, item_for, no_settings_of_the_person, set_ir,  # noqa: E402,F401
                          spawned, stopped_run, zero_touch)

from adw_modules import decide, ir, standin, state as state_module  # noqa: E402

WIDEST = declared("auto", may=ALL, maxActionsPerRun=20, maxPerHour=1000)


def widest(layer_dir: Path) -> None:
    set_ir(layer_dir, standIn=WIDEST)
    decide.write_setting(layer_dir, mode="auto", may=ALL)


def change(layer_dir: Path, **fields) -> None:
    """Edit the stored state of the run (what the run itself would have recorded)."""
    path = layer_dir / "agents" / ADW_ID / "state.json"
    data = json.loads(path.read_text())
    for key, value in fields.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    path.write_text(json.dumps(data))


def proposes(layer_dir: Path, script: str, **item) -> dict:
    decider(layer_dir, script)
    return standin.consider(ir.load(layer_dir), item_for(**item), force_suggest=False)


def again(layer_dir: Path) -> None:
    """Forget that the stand-in looked at this stop, so the next proposal is judged afresh."""
    for path in (layer_dir / "agents" / "stand-in" / "log.jsonl", layer_dir / "agents" / ADW_ID / "decisions.jsonl",
                 layer_dir / "agents" / ADW_ID / decide.CLAIM_FILE):   # the stop's claim (one decision per stop)
        path.unlink(missing_ok=True)


REJECTED = {"verdict": {"success": False, "summary": "the totals are wrong", "issues": [
    {"severity": "blocker", "description": "wrong totals", "resolution": "fix them"}]}, "rounds": [{"n": 1}]}


def test_a_rejected_review_is_never_overridden(make_layer, tmp_path, app_repo, spawned):
    layer_dir = zero_touch(make_layer, tmp_path, app_repo, WIDEST)
    widest(layer_dir)
    handed_over(layer_dir)
    passed = _state(layer_dir)["review"]
    change(layer_dir, review=REJECTED, ship={"handed_over": ["review-rejected"], "reasons": ["review-failed"]})
    for script in ("integrate", "settle-landed", "resume-from-ship"):
        result = proposes(layer_dir, script)
        assert (result["status"], result["entry"]["reason"]) == ("refused", standin.REJECTED), script
        assert _state(layer_dir)["decision"]["status"] == "refused"
        again(layer_dir)
    # land is not even offered for a rejected review; were it proposed, the limit refuses it
    state = state_module.load(layer_dir, ADW_ID)
    layer = ir.load(layer_dir)
    assert "land" not in standin.situation(layer, state, item_for())[1]
    for action, fields in (("land", {}), ("promote", {"promote": "approve"}), ("settle", {"settle_as": "landed"})):
        assert standin.hard_limit(layer, state, action, fields) == standin.REJECTED
    assert standin.hard_limit(layer, state, "settle", {"settle_as": "abandoned"}) == ""
    assert standin.hard_limit(layer, state, "resume-with", {"from_step": "review"}) == ""
    assert standin.hard_limit(layer, state, "resume", {"from_step": "ship"}) == standin.REJECTED
    assert spawned == [] and not (layer_dir / "agents" / ADW_ID / "settlement.json").exists()
    # a resume from before the review runs the review again: allowed
    result = proposes(layer_dir, "resume-from-build")
    assert result["status"] == "acted" and result["entry"]["outcome"] == "resuming from build"
    assert len(spawned) == 1
    # the same proposals with the review passed are not refused for this reason
    change(layer_dir, review=passed, ship={"handed_over": ["class-not-allowed:bug"], "reasons": ["class-not-allowed:bug"]})
    assert standin.hard_limit(layer, state_module.load(layer_dir, ADW_ID), "integrate", {}) != standin.REJECTED


def test_a_protected_path_change_is_never_landed(make_layer, tmp_path, app_repo, spawned):
    layer_dir = zero_touch(make_layer, tmp_path, app_repo, WIDEST)
    widest(layer_dir)
    handed_over(layer_dir)
    change(layer_dir, ship={"handed_over": ["protected-path:deploy/release.sh"], "reasons": ["protected-path:deploy/release.sh"]})
    for script in ("land", "integrate", "settle-landed"):
        result = proposes(layer_dir, script)
        assert (result["status"], result["entry"]["reason"]) == ("refused", standin.PROTECTED), script
        again(layer_dir)
    layer, state = ir.load(layer_dir), state_module.load(layer_dir, ADW_ID)
    assert standin.hard_limit(layer, state, "promote", {"promote": "approve"}) == standin.PROTECTED
    assert spawned == [] and _state(layer_dir)["status"] == "handed-over"
    assert not (layer_dir / "agents" / ADW_ID / "settlement.json").exists()
    assert proposes(layer_dir, "settle")["status"] == "acted"      # dropping the work is not landing it


def test_a_failing_required_check_is_never_landed(make_layer, tmp_path, app_repo, spawned):
    layer_dir = zero_touch(make_layer, tmp_path, app_repo, WIDEST)
    widest(layer_dir)
    handed_over(layer_dir)
    for fields in ({"ship": {"handed_over": ["require-check-failed:test"], "reasons": ["require-check-failed:test"]}},
                   {"ship": {"handed_over": ["check-failed:lint"], "reasons": ["check-failed:lint"]}},
                   {"checks": {"test": {"exit": 1, "log": "x"}}}):
        original = (layer_dir / "agents" / ADW_ID / "state.json").read_text()
        change(layer_dir, **fields)
        for script in ("land", "integrate", "settle-landed"):
            result = proposes(layer_dir, script)
            assert (result["status"], result["entry"]["reason"]) == ("refused", standin.CHECK_FAILS), (fields, script)
            again(layer_dir)
        (layer_dir / "agents" / ADW_ID / "state.json").write_text(original)
    assert spawned == []


def test_a_person_s_later_action_stops_the_stand_in(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=WIDEST)
    widest(layer_dir)
    layer = ir.load(layer_dir)
    state = state_module.load(layer_dir, ADW_ID)
    assert standin.touched(layer, state) is False
    # an answer (or any recorded action) by a person after the run ended
    standin.record(layer, {"type": "answer", "who": "person", "via": "dashboard", "run": ADW_ID, "answer": "reject",
                           "status": "refused"})
    assert standin.touched(layer, state) is True
    result = proposes(layer_dir, "resume")
    assert (result["status"], result["entry"]["reason"]) == ("refused", standin.TOUCHED)
    again(layer_dir)
    assert standin.touched(layer, state) is False
    # a settlement that is not the stand-in's
    settlement = layer_dir / "agents" / ADW_ID / "settlement.json"
    settlement.write_text(json.dumps({"as": "abandoned", "by": "", "note": "", "who": "Ada", "at": "2026-11-17T10:00:00Z",
                                      "via": "dashboard"}))
    assert proposes(layer_dir, "restart")["entry"]["reason"] == standin.TOUCHED
    settlement.write_text(json.dumps({"as": "abandoned", "by": "", "note": "", "who": "stand-in",
                                      "at": "2026-11-17T10:00:00Z", "via": "stand-in"}))
    assert standin.touched(layer, state) is False
    settlement.unlink()
    again(layer_dir)
    # a commit on the run's branch after it ended
    app = Path(layer.app_root)
    tree = tmp_path / "person-tree"
    env = {**os.environ, "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00"}
    subprocess.run(["git", "-C", str(app), "worktree", "add", "--force", str(tree), state.branch], check=True, capture_output=True)
    (tree / "by-hand.txt").write_text("a person fixed it\n")
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tree), "-c", "user.name=Ada", "-c", "user.email=ada@example.org", "commit", "-m",
                    "by hand"], check=True, capture_output=True, env=env)
    assert standin.touched(layer, state) is True
    assert proposes(layer_dir, "resume")["entry"]["reason"] == standin.TOUCHED
    assert spawned == [] and _state(layer_dir)["status"] == "failed"


def test_it_never_approves_its_own_work(make_layer, app_repo, monkeypatch):
    import promotion_fixtures as pf

    layer_dir = pf.sprint_layer(make_layer, app_repo)
    widest(layer_dir)
    pf.deliver(layer_dir, app_repo, "9a000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert pf.promote(layer_dir, "sprint", "main")[0] == 75
    pid = pf.open_id(layer_dir)
    layer = ir.load(layer_dir)
    promotion = state_module.load(layer_dir, pid).promotion
    assert [c["id"] for c in promotion["changes"]] == ["9a000001"]
    # the stand-in acted on the run that is now a change of the promotion
    standin.record(layer, {"type": "decision", "who": "stand-in", "via": "loop", "run": "9a000001", "kind": "resume",
                           "action": "resume", "status": "acted", "auto": True})
    monkeypatch.setattr(standin, "_spawn_detached", lambda *a, **k: pytest.fail("the promotion must not continue"))
    decider(layer_dir, "promote")
    result = standin.consider(layer, item_for(pid))
    assert (result["status"], result["entry"]["reason"]) == ("refused", standin.OWN_WORK)
    assert not (layer_dir / "promotions" / pid / "acceptance.jsonl").exists()
    assert pf.state_of(layer_dir, pid)["status"] == "waiting"
    # and an answer in its own name is never accepted
    state = state_module.load(layer_dir, pid)
    state.decision["status"] = "suggested"
    state.save(layer_dir)
    for kind in ("go", "alter"):
        refused = decide.answer(layer, pid, kind, "x", via="stand-in")
        assert (refused["status"], refused["reason"], refused["message"]) == ("refused", "own-work", standin.OWN_WORK)


def test_a_call_that_changes_files_is_discarded(make_layer, tmp_path, spawned):
    layer_dir = stopped_run(make_layer, tmp_path, stand_in=WIDEST)
    widest(layer_dir)
    before = _state(layer_dir)
    result = proposes(layer_dir, "changes-files")                 # answers `resume` and writes sneaked.py
    assert (result["status"], result["entry"]["reason"]) == ("failed", standin.CHANGED_FILES)
    assert (Path(before["worktree"]) / "sneaked.py").is_file()
    after = _state(layer_dir)
    assert after["decision"] == {"mode": "auto", "status": "failed", "reason": standin.CHANGED_FILES}
    assert after["steps"] == before["steps"] and after["prompt"] == before["prompt"] and spawned == []


def test_land_needs_the_worktree(make_layer, tmp_path, app_repo, spawned):
    layer_dir = zero_touch(make_layer, tmp_path, app_repo, WIDEST, keep="never")
    widest(layer_dir)
    state = handed_over(layer_dir)
    assert not Path(state["worktree"]).exists()
    layer = ir.load(layer_dir)
    loaded = state_module.load(layer_dir, ADW_ID)
    assert standin.hard_limit(layer, loaded, "land", {}) == standin.NO_WORKTREE
    for action in ("resume", "resume-with", "answer"):
        assert standin.hard_limit(layer, loaded, action, {}) == standin.NO_WORKTREE
    assert standin.situation(layer, loaded, item_for())[1] == ["integrate", "settle", "ask-human"]
    # the call itself runs in the application root, and what it can propose is integrate
    result = proposes(layer_dir, "integrate")
    assert result["status"] == "acted" and result["entry"]["outcome"].startswith("integration run ")
    assert _state(layer_dir)["worktree"] == state["worktree"]      # the record of where it was is kept


def test_no_local_setting_lifts_a_hard_limit(make_layer, tmp_path, app_repo, spawned):
    layer_dir = zero_touch(make_layer, tmp_path, app_repo, WIDEST)
    widest(layer_dir)
    handed_over(layer_dir)
    scope = standin.scope(ir.load(layer_dir))
    assert (scope["mode"], scope["may"], scope["never"], scope["per_run"], scope["per_hour"]) == ("auto", ALL, [], 20, 1000)
    change(layer_dir, review=REJECTED, ship={"handed_over": ["review-rejected", "protected-path:x"],
                                             "reasons": ["review-failed", "protected-path:x"]})
    for flag in (None, "auto"):
        state = state_module.load(layer_dir, ADW_ID)
        state.decider = flag or ""
        state.save(layer_dir)
        result = proposes(layer_dir, "integrate")
        assert result["status"] == "refused" and result["entry"]["reason"] == standin.REJECTED
        again(layer_dir)
    # the stored suggestion cannot be applied in its name either
    state = state_module.load(layer_dir, ADW_ID)
    state.decision.update(status="suggested")
    state.save(layer_dir)
    result = standin.apply_stored(ir.load(layer_dir), ADW_ID)
    assert (result["status"], result["reason"]) == ("refused", standin.REJECTED)
    assert spawned == []
