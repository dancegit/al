"""Rounds that must make progress (convergence.py): the reviewers' context entries, blocker
fingerprints, round comparison, the in-loop decision for review.py, the different-approach request,
round timing from events, and debt filed as tasks linked to the run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convergence_fixtures as cf  # noqa: E402

from adw_modules import approvals, artefacts, convergence, ir  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

RUN = "c0c0c0c0"


def _blocker(description: str, criterion: str = "", kind: str = "", resolution: str = "fix it") -> dict:
    issue = {"severity": "blocker", "description": description, "resolution": resolution, "artefact": ""}
    if criterion:
        issue["criterion"] = criterion
    if kind:
        issue["kind"] = kind
    return issue


def _round(number: int, *issues: dict, success: bool | None = None, **extra) -> dict:
    blocked = any(issue["severity"] == "blocker" for issue in issues)
    verdict = {"success": (not blocked) if success is None else success, "summary": f"round {number}",
               "issues": list(issues), "mode": "auto"}
    return {"round": number, "reviewers": [], "verdict": verdict, **extra}


def _state(**fields) -> ADWState:
    state = ADWState(adw_id=RUN, adw=cf.ADW, prompt="Add the feature", **fields)
    return state


def _layer(make_layer, **options):
    return ir.load(cf.converge_layer(make_layer, **options))


def _manifest(layer) -> list[dict]:
    return json.loads((Path(layer.layer_dir) / "artefacts" / RUN / "manifest.json").read_text())


# ---------------------------------------------------------------- the reviewers' context

def test_manifest_carries_evidence_approvals_and_round_scope(make_layer):
    layer = _layer(make_layer)
    state = _state()
    state.proof_plan = {"table": "parsed", "rows": [{"id": "C1", "criterion": "works", "command": "unit",
                                                     "state": "proven"}],
                        "pack": {"tree": "abc", "rows": [{"id": "C1", "criterion": "works", "command": "unit",
                                                          "state": "proven", "files": ["artefacts/x.png"]}]}}
    approvals.record(state, ["vocabulary"], "ana", "start")
    artefacts.write_manifest(layer, state)
    entries = {entry["type"]: entry for entry in _manifest(layer)}
    assert entries["evidence"]["states"] == {"C1": "proven"}
    assert entries["evidence"]["table"][0]["files"] == ["artefacts/x.png"]
    assert entries["evidence"]["path"] == f"artefacts/{RUN}/review-context/evidence.md"
    assert [a["what"] for a in entries["approvals"]["approvals"]] == ["vocabulary"]
    assert entries["round"]["round"] == 1 and entries["round"]["earlier_blockers"] == []
    for kind in ("evidence", "approvals", "round"):
        assert (Path(layer.layer_dir) / entries[kind]["path"]).is_file()


def test_approvals_manifest_shows_note_and_gate(make_layer):
    layer = _layer(make_layer)
    state = _state()
    approvals.record(state, ["vocabulary"], "ana", "start")
    approvals.record(state, ["edit:docs/DESIGN.md"], "Robin Example", "dashboard", note="agreed in review", gate=True)
    (entry,) = [e for e in convergence.manifest_entries(layer, state) if e["type"] == "approvals"]
    assert [(a["what"], a.get("gate"), a.get("note")) for a in entry["approvals"]] == [
        ("vocabulary", None, None), ("edit:docs/DESIGN.md", True, "agreed in review")]
    first, second = (Path(layer.layer_dir) / entry["path"]).read_text().splitlines()[2:4]
    assert first.startswith("- `vocabulary` by ana at ") and first.endswith("(start)")
    assert second.startswith("- `edit:docs/DESIGN.md` by Robin Example at ")
    assert second.endswith("(dashboard); accepted at the gate for this run; note: agreed in review")


def test_round_two_scope_lists_earlier_blockers_and_safety(make_layer):
    layer = _layer(make_layer)
    state = _state()
    state.proof_plan = {"table": "parsed", "rows": []}
    first = _blocker("feature.py:12 returns None", criterion="C1", kind="fixable", resolution="return the flag")
    state.review = {"rounds": [_round(1, first, patch={"patch_plan": "plan text\nspecs/patch-1.md"})]}
    (entry,) = [e for e in convergence.manifest_entries(layer, state) if e["type"] == "round"]
    assert entry["round"] == 2
    assert entry["earlier_blockers"] == [{"fingerprint": "C1|fixable", "description": "feature.py:12 returns None",
                                          "resolution": "return the flag"}]
    assert entry["patch_plan"] == "specs/patch-1.md"
    assert entry["safety"] == list(convergence.SAFETY) and "only that each earlier blocker" in entry["rule"]
    text = (Path(layer.layer_dir) / entry["path"]).read_text()
    assert "C1|fixable" in text and "- data loss" in text


def test_manifest_unchanged_without_proof_plan(make_layer):
    layer = _layer(make_layer)
    state = _state()
    state.artefacts = [{"id": "log", "type": "log", "path": "x.log"}]
    artefacts.write_manifest(layer, state)
    before = _manifest(layer)
    state.proof_plan = {"plan": "specs/p.md", "table": "absent"}
    state.review = {"rounds": [_round(1, _blocker("x"))]}
    artefacts.write_manifest(layer, state)
    assert _manifest(layer) == before == state.artefacts
    assert not (Path(layer.layer_dir) / "artefacts" / RUN / "review-context").exists()


# ---------------------------------------------------------------- fingerprints

def test_fingerprint_stable_across_wording_with_criterion():
    one = _blocker("The flag is missing in feature.py:3", criterion="C2", kind="fixable")
    two = _blocker("feature.py does not define FEATURE at all", criterion="C2", kind="fixable")
    assert convergence.fingerprint(one) == convergence.fingerprint(two) == "C2|fixable"
    person = _blocker("renaming the vocabulary needs approval", criterion="C2")
    assert convergence.fingerprint(person) == "C2|needs-person"


def test_fingerprint_by_file_without_criterion():
    one = _blocker("src/app/feature.py:12: the flag is never read")
    two = _blocker("src/app/feature.py:40 still has the old name; rename it")
    assert convergence.fingerprint(one) == convergence.fingerprint(two) == "src/app/feature.py|fixable"
    words = convergence.fingerprint(_blocker("The summary  is wrong, Really"))
    assert words.startswith("text:") and words == convergence.fingerprint(_blocker("the summary is wrong really"))


def test_compare_resolved_open_new():
    a, b, c = _blocker("a.py: x", "C1"), _blocker("b.py: y"), _blocker("c.py: z")
    assert convergence.compare([a, b], [b, c]) == {"resolved": ["C1|fixable"], "open": ["b.py|fixable"],
                                                   "new": ["c.py|fixable"]}


# ---------------------------------------------------------------- the in-loop decision

def test_after_round_same_blocker_twice_asks_different_approach_then_stops(make_layer):
    layer = _layer(make_layer)
    state = _state()
    same = _blocker("feature.py:3 the flag is wrong", criterion="C1", kind="fixable")
    rounds = [_round(1, same, patch={"patch_plan": "p", "diff": "-FEATURE = 0\n+FEATURE = 1"})]
    assert convergence.after_round(layer, state, rounds)["action"] == "continue"
    rounds.append(_round(2, same))
    decision = convergence.after_round(layer, state, rounds)
    assert decision["action"] == "different-approach" and decision["party"] == "agent"
    assert "take a different approach" in decision["request"] and "+FEATURE = 1" in decision["request"]
    rounds[-1]["patch"] = {"patch_plan": "q", "different_approach": True}
    rounds.append(_round(3, same))
    stop = convergence.after_round(layer, state, rounds)
    assert (stop["action"], stop["party"]) == ("stop", "person") and stop["reason"].startswith("no-progress: C1|fixable")
    moved = convergence.after_round(layer, state, [_round(1, same), _round(2, _blocker("other.py: new"))])
    assert moved["action"] == "continue"


def test_after_round_needs_person_stops_at_once(make_layer):
    layer = _layer(make_layer)
    for kind, party in (("needs-person", "approver"), ("needs-environment", "operator"),
                        ("needs-declaration", "layer author")):
        decision = convergence.after_round(layer, _state(), [_round(1, _blocker("x.py: y", kind=kind))])
        assert (decision["action"], decision["party"]) == ("stop", party)
        assert decision["reason"].startswith(f"{kind}: x.py|{kind}")


def test_after_round_approved_need_is_not_a_stop(make_layer):
    layer = _layer(make_layer)
    state = _state()
    approvals.record(state, ["edit:docs/*"], "ana", "start")
    issue = _blocker("docs/DESIGN.md is a protected path", kind="needs-person")
    assert convergence.after_round(layer, state, [_round(1, issue)])["action"] == "continue"
    state.proof_plan = {"rows": [{"id": "C3", "needs": ["approval:vocabulary"]}]}
    word = _blocker("a new layer word", criterion="C3", kind="needs-person")
    assert convergence.after_round(layer, state, [_round(1, word)])["action"] == "stop"
    approvals.record(state, ["vocabulary"], "ana", "cli")
    assert convergence.after_round(layer, state, [_round(1, word)])["action"] == "continue"


def test_patch_request_carries_first_diff():
    issues = [_blocker("C2: files-missing: the report", resolution="make `unit` prove it")]
    plain = convergence.patch_request(issues)
    assert plain == "- C2: files-missing: the report\n  Resolution: make `unit` prove it"
    again = convergence.patch_request(issues, first_diff="+FIRST = 1\n")
    assert again.startswith(plain)
    assert "A first attempt did not resolve this; take a different approach. Its diff:" in again
    assert "```diff\n+FIRST = 1\n```" in again


# ---------------------------------------------------------------- after the review

def _events(layer, records: list[tuple[str, str]], started: str) -> None:
    path = Path(layer.layer_dir) / "agents" / RUN / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": "2026-11-18T09:00:00.000+00:00", "seq": 1, "type": "review.round", "step": "review",
                         "round": 1})]  # an earlier review attempt: before the step started, not counted
    lines += [json.dumps({"ts": ts, "seq": index + 2, "type": kind, "step": "review"})
              for index, (kind, ts) in enumerate(records)]
    path.write_text("\n".join(lines) + "\n")


def test_after_review_records_idle_seconds_from_events(make_layer):
    layer = _layer(make_layer)
    state = _state()
    state.proof_plan = {"table": "parsed", "rows": [{"id": "C1", "state": "proven"}]}
    state.steps = [{"name": "review", "kind": "review", "status": "passed", "started": "2026-11-18T10:00:00+00:00"}]
    same = _blocker("feature.py:3 wrong", criterion="C1", kind="fixable")
    state.review = {"verdict": {"success": True, "issues": []},
                    "rounds": [_round(1, same), _round(2, same), _round(3)]}
    _events(layer, [("review.round", "2026-11-18T10:10:00+00:00"), ("review.round", "2026-11-18T10:40:00+00:00"),
                    ("review.round", "2026-11-18T11:00:00+00:00")], "2026-11-18T10:00:00+00:00")
    convergence.after_review(layer, state, {"name": "review", "patch": "patch", "implement": "implement"})
    rounds = state.proof_plan["rounds"]
    assert [r["seconds"] for r in rounds] == [600.0, 1800.0, 1200.0]
    assert [(r["resolved"], r["open"], r["new"]) for r in rounds] == [
        ([], [], ["C1|fixable"]), ([], ["C1|fixable"], []), (["C1|fixable"], [], [])]
    summary = state.proof_plan["summary"]
    assert summary["idle_seconds"] == 1800.0      # round 2 changed no verdict
    assert summary["rounds"] == 3 and summary["repeated"] == [{"fingerprint": "C1|fixable", "cause": "fixable"}]
    assert state.proof_plan["why_stopped"] == "passed"


def _passing_review(state, *issues):
    state.proof_plan = {"table": "parsed", "rows": []}
    state.steps = [{"name": "review", "kind": "review", "status": "passed", "started": "2026-11-18T10:00:00+00:00"}]
    state.review = {"verdict": {"success": True, "issues": list(issues)}, "rounds": [_round(1, *issues)]}


DEBT = {"severity": "tech_debt", "description": "feature.py: a module constant", "resolution": "read settings",
        "artefact": ""}


def test_debt_filed_to_task_list_linked_to_run(make_layer):
    layer = _layer(make_layer, tasklist=True)
    state = _state()
    _passing_review(state, DEBT)
    convergence.after_review(layer, state, {"name": "review"})
    (item,) = state.proof_plan["debt"]
    assert item["text"] == f"debt from run {RUN}: feature.py: a module constant — read settings"
    assert item["where"].startswith("tasklist:tasks.md:")
    assert f"- [ ] debt from run {RUN}: feature.py: a module constant — read settings" in \
        (Path(layer.layer_dir) / "tasks.md").read_text()
    convergence.after_review(layer, state, {"name": "review"})   # recorded once
    assert len(state.proof_plan["debt"]) == 1


def test_debt_request_names_the_check_that_covers_its_files(make_layer):
    layer = _layer(make_layer, tasklist=True)
    state = _state()
    _passing_review(state, DEBT)
    convergence.after_review(layer, state, {"name": "review"})
    (item,) = state.proof_plan["debt"]
    assert item["text"] == f"debt from run {RUN}: feature.py: a module constant — read settings"
    assert "Proof: prove it only with the check(s) `unit` of this layer" in \
        (Path(layer.layer_dir) / "tasks.md").read_text()


def test_debt_falls_back_to_debt_md(make_layer):
    layer = _layer(make_layer)
    state = _state()
    later = _blocker("other.py: a new finding in round 2")
    state.proof_plan = {"table": "parsed", "rows": []}
    state.review = {"verdict": {"success": True, "issues": []},
                    "rounds": [_round(1, _blocker("feature.py: x", "C1")), _round(2, later), _round(3)]}
    convergence.after_review(layer, state, {"name": "review"})
    texts = [item["text"] for item in state.proof_plan["debt"]]
    assert texts == [f"debt from run {RUN}: other.py: a new finding in round 2 — fix it"]
    assert state.proof_plan["debt"][0]["where"] == f"file:agents/{RUN}/debt.md"
    assert texts[0] in (Path(layer.layer_dir) / "agents" / RUN / "debt.md").read_text()


def test_debt_runs_file_no_debt(make_layer):
    layer = _layer(make_layer)
    state = ADWState(adw_id=RUN, adw=cf.ADW, prompt="debt from run 12345678: something — fix")
    _passing_review(state, DEBT)
    convergence.after_review(layer, state, {"name": "review"})
    assert not state.proof_plan.get("debt")
    assert not (Path(layer.layer_dir) / "agents" / RUN / "debt.md").exists()


def test_safety_finding_is_not_debt(make_layer):
    layer = _layer(make_layer)
    state = _state()
    secret = _blocker("config.py: a secret token is logged")
    state.proof_plan = {"table": "parsed", "rows": []}
    state.review = {"verdict": {"success": True, "issues": []},
                    "rounds": [_round(1, _blocker("feature.py: x", "C1")), _round(2, secret), _round(3)]}
    convergence.after_review(layer, state, {"name": "review"})
    assert not state.proof_plan.get("debt")
    assert convergence.is_debt(state, _blocker("x.py: style"), round_number=2)
    assert not convergence.is_debt(state, secret, round_number=2)
    assert not convergence.is_debt(state, _blocker("x.py: style"), round_number=1)


def test_after_review_never_raises_and_skips_runs_without_a_table(make_layer, monkeypatch):
    layer = _layer(make_layer)
    state = _state()
    state.review = {"verdict": {"success": True, "issues": [DEBT]}, "rounds": [_round(1, DEBT)]}
    convergence.after_review(layer, state, {"name": "review"})
    assert state.proof_plan == {}
    _passing_review(state, DEBT)
    monkeypatch.setattr(convergence, "_progress", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    convergence.after_review(layer, state, {"name": "review"})   # printed, not raised
