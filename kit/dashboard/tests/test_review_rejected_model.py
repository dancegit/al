"""A review whose final verdict failed is shown `rejected`, and its run `handed-over`, in the new
record format (the runtime says so) and in the old one (review `passed`, run `passed`, verdict
failed: worked out when the files are read). Layer counts keep handed-over runs out of "passed"."""
from __future__ import annotations

import json
from pathlib import Path

import dashboard as d
import fixtures
from fixtures import rec, state, write

REJECTED = {"success": False, "summary": "proof missing", "issues": [
    {"severity": "blocker", "description": "no proof", "resolution": "add proof"}]}
APPROVED = {"success": True, "summary": "looks right", "issues": []}


def _rounds(verdicts: list[dict]) -> list[dict]:
    return [{"round": n, "reviewers": [v, v], "verdict": {**v, "issues": v["issues"] * 2}}
            for n, v in enumerate(verdicts, start=1)]


def _review_run(agents: Path, rid: str, base: float, *, review_status: str, run_status: str, verdicts: list[dict],
                handed_over: list[str] | None = None, events: bool = False) -> None:
    run = agents / rid
    records, t = fixtures.plan_records(base)
    final = verdicts[-1]
    detail = (f"{'pass' if final['success'] else 'fail'} after {len(verdicts)} round(s): {final['summary']}"
              if review_status != "passed" or final["success"] else "ok")
    records += [rec("lint", "check", "passed", t, t + 4, "exit 0"), rec("test", "check", "passed", t + 4, t + 10, "exit 0"),
                rec("commit", "run", "passed", t + 10, t + 20, "Committed."),
                rec("review", "review", review_status, t + 20, t + 80, detail),
                rec("ship", "ship", "passed", t + 80, t + 90, "ok")]
    for n in range(1, len(verdicts) + 1):
        for i in (1, 2):
            name = f"review-{i}" if n == 1 else f"review-{n}-{i}"
            start = t + 20 + (n - 1) * 20 + (i - 1) * 10
            fixtures.agent(run, name, start, start + 10, fixtures.mock_raw(json.dumps(verdicts[n - 1])))
    reasons = [] if final["success"] else ["review-failed"]
    ship = {"action": "pr", "shipped": False, "target": f"agents/{rid}/PR.md", "commit": "3" * 40, "pr": None,
            "reasons": reasons, "gate": {"action": "pr", "ship": False if reasons else True, "reasons": reasons,
                                         "diff_lines": 5, "changed_files": ["shop/cart.py"]}}
    if handed_over is not None:
        ship["handed_over"] = handed_over
    review = {"mode": "auto", "verdict": {**final, "issues": final["issues"] * 2, "mode": "auto"},
              "rounds": _rounds(verdicts)}
    write(run / "state.json", json.dumps(state(rid, "zte", f"Review case {rid}", run_status, base, t + 90, records,
                                               review=review, ship=ship)), t + 90)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n", t + 90)
    if events:
        lines = [{"ts": fixtures.iso(t + 20 + n * 20), "seq": n, "type": "review.round", "step": "review", "round": n,
                  "success": v["success"], "reviewers": 2, "summary": v["summary"]} for n, v in enumerate(verdicts, start=1)]
        write(run / "events.jsonl", "".join(json.dumps(line) + "\n" for line in lines), t + 90)


def add_review_runs(layer: Path) -> None:
    """a1000101: new format, rejected and handed over; a1000102: the same in the old format;
    a1000103: approved on round 2."""
    agents = layer / "agents"
    _review_run(agents, "a1000101", 20000, review_status="rejected", run_status="handed-over",
                verdicts=[REJECTED] * 3, handed_over=["review-rejected", "review-failed"], events=True)
    _review_run(agents, "a1000102", 21000, review_status="passed", run_status="passed", verdicts=[REJECTED] * 3)
    _review_run(agents, "a1000103", 22000, review_status="passed", run_status="passed", verdicts=[REJECTED, APPROVED])


def _model(layer: Path) -> d.Model:
    model = d.Model([layer], [])
    model.discover()
    model.tick()
    return model


def _review_step(summary: dict) -> dict:
    return next(step for step in summary["steps"] if step["name"] == "review")


def test_rejected_review_and_handed_over_run_new_and_old_format(fx):
    add_review_runs(fx["layer"])
    model = _model(fx["layer"])
    for rid in ("a1000101", "a1000102"):
        summary = model.summaries[f"demo-shop/{rid}"]
        assert summary["status"] == "handed-over", rid
        assert _review_step(summary)["status"] == "rejected", rid
        assert summary["outcome"]["handover"][0] == "review-rejected", rid
        assert summary["outcome"]["blockers"] == 2, rid
        detail = model.run_detail("demo-shop", rid)
        node = next(n for n in detail["graph"]["nodes"] if n["step"] == "review")
        assert node["status"] == "rejected", rid
        assert detail["steps_detail"]["review"]["status"] == "rejected", rid
        assert next(b for b in detail["timeline"]["bars"] if b["id"] == "step:review")["status"] == "rejected"
    approved = model.summaries["demo-shop/a1000103"]
    assert approved["status"] == "passed" and _review_step(approved)["status"] == "passed"
    assert approved["outcome"]["handover"] == []


def test_review_rejected_from_events_without_a_recorded_review():
    events = [{"type": "review.round", "success": False}, {"type": "review.round", "success": False}]
    assert d.review_rejected({"review": {}}, events) is True
    assert d.review_rejected({"review": {}}, [*events, {"type": "review.round", "success": True}]) is False
    assert d.review_rejected({"review": {"mode": "human", "verdict": {"success": True}, "rounds": []}}, events) is False


def test_layer_counts_keep_handed_over_runs_out_of_passed(fx):
    add_review_runs(fx["layer"])
    model = _model(fx["layer"])
    runs = [s for key, s in model.summaries.items() if key.startswith("demo-shop/")]
    agg = d.aggregate(runs)
    handed = [r for r in runs if r["status"] == "handed-over"]
    assert agg["handed_over"] == 2 and len(handed) == 2
    assert agg["passed"] == sum(1 for r in runs if r["status"] == "passed")
    assert agg["finished"] == sum(1 for r in runs if r["status"] in {"passed", "failed", "handed-over"})
    assert agg["pass_rate"] == agg["passed"] / agg["finished"]
