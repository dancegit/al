"""A fixture layer whose runs carry the runtime's newer records: agents/<id>/events.jsonl,
liveness in state.json (pid, host, heartbeat), the agent on each step record and a fan-out
candidate's parent. Written as code, like fixtures.py; the event shapes follow docs/DESIGN.md
("Run events") and kit/runtime/adw_modules/events.py.

File mtimes are deliberately all the same (the build time), so a test can only get the right
timings by reading the events, not by guessing from mtimes.

Runs in events-shop.al:
  e1000001 zte        passed: waited 12 s for a runs slot, lint red then resolved, gate, merge
  e1000002 zte        running: check `test` attempt 1 is running and its log is growing
  e1000003 zte        died: state says running, heartbeat 5 min old, its pid is gone
  e1000004 candidate  passed; its own state names its fan-out parent (e1000099 / race / 2)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import fixtures
from fixtures import Claude, LAYER_DIR, iso, rec, simple_claude, state

NAME = "events-shop"
MODEL = "claude-opus-5-5"


def dead_pid() -> int:
    """A pid that belonged to a process which has exited (and was reaped)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class Events:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, at: float | str, type_: str, **fields: Any) -> None:
        stamp = at if isinstance(at, str) else iso(at).replace("+00:00", ".000+00:00")
        self.lines.append(json.dumps({"ts": stamp, "seq": len(self.lines) + 1, "type": type_, **fields}))

    def agent(self, start: float, end: float | None, step: str, owner: str, cost: float | None = None,
              status: str = "passed") -> None:
        ident = {"step": step, "owner": owner, "agent": "claude", "agent_kind": "claude-code", "model": MODEL,
                 "model_set": "heavy"}
        self.add(start, "agent.start", **ident)
        if end is not None:
            self.add(end, "agent.end", **ident, status=status, seconds=end - start,
                     **({"cost": cost, "turns": 3, "duration_ms": int((end - start) * 1000)} if cost is not None else {}))

    def check(self, start: float, end: float | None, step: str, attempt: int, exit_: int | None, run: str,
              phase: str = "check", owner: str | None = None, log: str | None = None, cmd: str | None = None) -> None:
        span = {"step": step, "owner": owner or step, "phase": phase, "cmd": cmd or step,
                "attempt": attempt, "log": log or f"agents/{run}/checks/{step}.{attempt}.log"}
        self.add(start, "check.start", **span)
        if end is not None:
            self.add(end, "check.end", **span, exit=exit_, seconds=end - start)

    def step(self, start: float, end: float | None, name: str, kind: str, status: str = "passed") -> None:
        self.add(start, "step.start", step=name, kind=kind, attempt=1)
        if end is not None:
            self.add(end, "step.end", step=name, kind=kind, status=status, detail="", seconds=end - start)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def with_agent(record: dict) -> dict:
    return {**record, "agent": "claude", "agent_kind": "claude-code", "model": MODEL}


def write_all(path: Path, content: str, when: float) -> None:
    fixtures.write(path, content, when)


def build(root: Path, now: float | None = None) -> dict[str, Path]:
    """Write events-shop.al below root. `now` (epoch) anchors the live runs' heartbeats."""
    now = time.time() if now is None else now
    layer = Path(root) / f"{NAME}.al"
    ir = dict(fixtures.IR, name=NAME, description="Fixture layer with run events")
    fixtures.write(layer / "agentic-layer.json", json.dumps(ir, indent=2))
    agents = layer / "agents"
    same = 100_000.0     # every file gets this mtime: timings must come from events
    host = socket.gethostname()
    cwd = lambda rid: f"{LAYER_DIR}/trees/{rid}"  # noqa: E731

    def put(path: Path, content: str) -> None:
        fixtures.write(path, content, same)

    # ---- e1000001: passed, exact spans
    rid, run = "e1000001", agents / "e1000001"
    ev = Events()
    ev.add(0, "run.start", adw="zte", adw_id=rid, klass="chore", dry_run=False, resumed=False, pid=1, host=host,
           fanout_parent=None)
    ev.add(0, "slot.wait", pool="runs", limit=2, who=f"adw {rid}")
    ev.add(12, "slot.acquire", pool="runs", slot=1, waited=12.0, who=f"adw {rid}")
    ev.add(13, "env.container.start", name=f"al-events-shop-{rid}")
    spans = [("classify", "run", 13, 20), ("plan-chore", "run", 20, 41), ("build", "run", 41, 90)]
    for name, kind, a, b in spans:
        ev.step(a, None, name, kind)
        ev.agent(a + 0.5, b - 0.5, name, name, cost=0.05)
        ev.add(b, "step.end", step=name, kind=kind, status="passed", detail="", seconds=b - a)
        put(run / name / "prompt.md", f"# {name}\n")
        put(run / name / "raw.jsonl", simple_claude(a, b, cwd(rid), f"{name} done", 0.05))
        put(run / name / "output.txt", "ok")
    ev.add(41, "step.end", step="plan-bug", kind="run", status="skipped", detail="when=class-bug, class=chore", seconds=0)
    ev.step(90, None, "lint", "check")
    ev.check(91, 95, "lint", 1, 1, rid)
    ev.agent(95.5, 110, "lint-resolve-1", "lint", cost=0.03)
    ev.add(110.2, "slot.wait", pool="checks", limit=1, who=f"adw {rid} check lint")
    ev.add(113, "slot.acquire", pool="checks", slot=0, waited=2.8, who=f"adw {rid} check lint")
    ev.check(113, 116, "lint", 2, 0, rid)
    ev.add(116.1, "step.end", step="lint", kind="check", status="passed", detail="exit 0", seconds=26.1)
    ev.step(116.2, None, "test", "check")
    ev.check(116.5, 124, "test", 1, 0, rid)
    ev.add(124.1, "step.end", step="test", kind="check", status="passed", detail="exit 0", seconds=7.9)
    ev.step(124.2, 130, "commit", "run")
    ev.agent(124.5, 129.5, "commit", "commit", cost=0.02)
    ev.step(130, None, "review", "review")
    ev.agent(130.5, 140, "review-1", "review", cost=0.1)
    ev.agent(140.2, 150, "review-2", "review", cost=0.1)
    ev.add(150.1, "review.round", step="review", round=1, success=True, reviewers=2, summary="ok | ok",
           issues={"blocker": 0, "tech_debt": 0, "skippable": 0})
    ev.add(150.2, "step.end", step="review", kind="review", status="passed", detail="", seconds=20.2)
    ev.step(150.3, None, "ship", "ship")
    ev.check(151, 153, "gate", 1, 0, rid, phase="gate", owner="ship", log=f"agents/{rid}/checks/gate-lint.log", cmd="lint")
    ev.check(153.5, 157, "gate", 1, 0, rid, phase="gate", owner="ship", log=f"agents/{rid}/checks/gate-test.log", cmd="test")
    ev.add(157.2, "gate.decision", action="merge", ship=True, reasons=[])
    ev.add(158, "ship.result", action="merge", shipped=True, target="al/ship/main", pr_url="", reasons=[])
    ev.add(158.1, "step.end", step="ship", kind="ship", status="passed", detail="", seconds=7.8)
    ev.add(159, "env.container.stop", name=f"al-events-shop-{rid}")
    ev.add(159.5, "slot.release", pool="runs", slot=1, held=147.5, who=f"adw {rid}")
    ev.add(160, "run.end", status="passed", exit=0)
    put(run / "events.jsonl", ev.text())
    for name, a, b in (("lint-resolve-1", 95.5, 110), ("commit", 124.5, 129.5), ("review-1", 130.5, 140), ("review-2", 140.2, 150)):
        put(run / name / "prompt.md", f"# {name}\n")
        put(run / name / "raw.jsonl", simple_claude(a, b, cwd(rid), f"{name} done", 0.05))
        put(run / name / "output.txt", "ok")
    put(run / "checks" / "lint.1.log", "F401 unused import\n")
    put(run / "checks" / "lint.2.log", "All checks passed!\n")
    put(run / "checks" / "test.1.log", "3 passed\n")
    put(run / "checks" / "gate-lint.log", "All checks passed!\n")
    put(run / "checks" / "gate-test.log", "3 passed\n")
    records = [with_agent(rec("classify", "run", "passed", 13, 20)), with_agent(rec("plan-chore", "run", "passed", 20, 41)),
               rec("plan-bug", "run", "skipped", 41, 41, "when=class-bug, class=chore"),
               rec("plan-feature", "run", "skipped", 41, 41, "when=class-feature, class=chore"),
               with_agent(rec("build", "run", "passed", 41, 90)),
               with_agent(rec("lint", "check", "passed", 90, 116.1, "exit 0")), rec("test", "check", "passed", 116.2, 124.1),
               with_agent(rec("commit", "run", "passed", 124.2, 130)), with_agent(rec("review", "review", "passed", 130, 150.2)),
               rec("ship", "ship", "passed", 150.3, 158.1)]
    put(run / "state.json", json.dumps(state(
        rid, "zte", "Remove the unused import", "passed", 0, 160, records, pid=1, host=host, heartbeat=iso(159.9),
        checks={"lint": {"cmd": "ruff check .", "exit": 0, "log": f"agents/{rid}/checks/lint.2.log"},
                "test": {"cmd": "pytest -q", "exit": 0, "log": f"agents/{rid}/checks/test.1.log"}},
        review={"mode": "auto", "verdict": {"success": True, "summary": "ok | ok", "issues": []},
                "rounds": [{"round": 1, "reviewers": [{"success": True, "summary": "ok", "issues": []}] * 2,
                            "verdict": {"success": True}}]},
        ship={"action": "merge", "shipped": True, "target": "al/ship/main", "reasons": [],
              "gate": {"action": "merge", "ship": True, "reasons": []}}), indent=2))
    put(run / "run.log", "".join(f"2026-11-01T10:00:00.000Z {line}\n" for line in (
        f"[adw {rid}] zte start: class=chore dry_run=False",
        f"[slots] adw {rid}: waiting for a free runs slot (2 in use)",
        f"[adw {rid}] container started: al-events-shop-{rid}",
        f"[adw {rid}] gate: lint exit 0", f"[adw {rid}] gate: test exit 0", f"[adw {rid}] gate: action merge",
        f"[adw {rid}] container removed: al-events-shop-{rid}", "=== ADW summary ===")))

    # ---- e1000002: running, check `test` attempt 1 running with a growing log
    rid, run = "e1000002", agents / "e1000002"
    start = now - 120
    stamp = lambda offset: fixtures.datetime.fromtimestamp(start + offset, fixtures.timezone.utc).isoformat(timespec="milliseconds")  # noqa: E731
    ev = Events()
    ev.add(stamp(0), "run.start", adw="zte", adw_id=rid, klass="chore", dry_run=False, resumed=False, pid=os.getpid(),
           host=host, fanout_parent=None)
    ev.add(stamp(0.1), "slot.acquire", pool="runs", slot=0, waited=0.0, who=f"adw {rid}")
    ev.add(stamp(1), "step.start", step="build", kind="run", attempt=1)
    ev.add(stamp(1.2), "agent.start", step="build", owner="build", agent="claude", agent_kind="claude-code", model=MODEL,
           model_set="heavy")
    ev.add(stamp(60), "agent.end", step="build", owner="build", agent="claude", agent_kind="claude-code", model=MODEL,
           model_set="heavy", status="passed", seconds=58.8, cost=0.31, turns=9, duration_ms=58000)
    ev.add(stamp(60.1), "step.end", step="build", kind="run", status="passed", detail="built", seconds=59.1)
    ev.add(stamp(60.2), "step.start", step="test", kind="check", attempt=1)
    ev.add(stamp(61), "check.start", step="test", owner="test", phase="check", cmd="test", attempt=1,
           log=f"agents/{rid}/checks/test.1.log")
    put(run / "events.jsonl", ev.text())
    put(run / "build" / "prompt.md", "# build\n")
    put(run / "build" / "raw.jsonl", simple_claude(1, 60, cwd(rid), "built", 0.31))
    put(run / "build" / "output.txt", "built")
    put(run / "checks" / "test.1.log", "collecting ...\ntests/test_cart.py ..")
    records = [{**with_agent(rec("build", "run", "passed", 0, 0)), "started": stamp(1), "ended": stamp(60.1)},
               {**rec("test", "check", "running", 0, None), "started": stamp(60.2)}]
    put(run / "state.json", json.dumps(dict(state(rid, "zte", "Speed up the cart tests", "running", 0, None, records),
                                            started=stamp(0), pid=os.getpid(), host=host, heartbeat=stamp(119)), indent=2))
    put(run / "run.log", f"2026-11-01T10:00:00.000Z [adw {rid}] zte start: class=chore dry_run=False\n")

    # ---- e1000003: died
    rid, run = "e1000003", agents / "e1000003"
    start = now - 900
    stamp3 = lambda offset: fixtures.datetime.fromtimestamp(start + offset, fixtures.timezone.utc).isoformat(timespec="milliseconds")  # noqa: E731
    ev = Events()
    gone = dead_pid()
    ev.add(stamp3(0), "run.start", adw="zte", adw_id=rid, klass="chore", dry_run=False, resumed=False, pid=gone,
           host=host, fanout_parent=None)
    ev.add(stamp3(1), "step.start", step="build", kind="run", attempt=1)
    ev.add(stamp3(1.5), "agent.start", step="build", owner="build", agent="claude", agent_kind="claude-code", model=MODEL,
           model_set="heavy")
    put(run / "events.jsonl", ev.text())
    put(run / "build" / "prompt.md", "# build\n")
    put(run / "build" / "raw.jsonl", Claude(start + 2, cwd(rid)).body())
    records = [{**with_agent(rec("build", "run", "running", 0, None)), "started": stamp3(1)}]
    put(run / "state.json", json.dumps(dict(state(rid, "zte", "Rewrite the checkout", "running", 0, None, records),
                                            started=stamp3(0), pid=gone, host=host, heartbeat=stamp3(600)), indent=2))
    put(run / "run.log", f"2026-11-01T10:00:00.000Z [adw {rid}] zte start: class=chore dry_run=False\n")

    # ---- e1000004: a fan-out candidate that names its parent
    rid, run = "e1000004", agents / "e1000004"
    put(run / "plan" / "prompt.md", "# plan\n")
    put(run / "plan" / "raw.jsonl", simple_claude(5000, 5010, cwd(rid), "planned", 0.02))
    put(run / "plan" / "output.txt", "ok")
    put(run / "state.json", json.dumps(state(rid, "candidate", "Race candidate", "passed", 5000, 5011,
                                             [with_agent(rec("plan", "run", "passed", 5000, 5010))],
                                             fanout_parent={"run": "e1000099", "step": "race", "index": 2}), indent=2))
    put(run / "run.log", f"2026-11-01T10:00:00.000Z [adw {rid}] candidate start: class=chore dry_run=False\n")
    return {"layer": layer, "agents": agents}


if __name__ == "__main__":
    print(build(Path(sys.argv[1])))


SETUP_RECIPE = "3f9c2a7b1d04"


def add_setup_runs(layer: Path, now: float | None = None) -> None:
    """Two runs whose setup shows on the timeline (env.build.* / env.preflight):
      e1000005 failed: built the sandbox image (1–40 s, 312 MB), then the preflight missed `command:test`
      e1000006 running: the sandbox image is being built right now (no step has started)"""
    now = time.time() if now is None else now
    agents, host = Path(layer) / "agents", socket.gethostname()
    image = f"al-{NAME}:{SETUP_RECIPE}"

    rid, run = "e1000005", agents / "e1000005"
    ev = Events()
    ev.add(0, "run.start", adw="zte", adw_id=rid, klass="chore", dry_run=False, resumed=False, pid=1, host=host,
           fanout_parent=None)
    ev.add(0.5, "slot.acquire", pool="runs", slot=0, waited=0.0, who=f"adw {rid}")
    ev.add(1, "env.build.start", image=image, recipe=SETUP_RECIPE)
    ev.add(40, "env.build.end", image=image, recipe=SETUP_RECIPE, status="passed", seconds=39.0, size=312_000_000,
           error=None)
    ev.add(43, "env.preflight", status="failed", seconds=3.0, checks=[
        {"name": "command:test", "status": "miss", "detail": "pytest not found on PATH"},
        {"name": "git", "status": "ok", "detail": "rev-parse and status work in the worktree"}])
    ev.add(44, "run.end", status="failed", exit=1)
    fixtures.write(run / "events.jsonl", ev.text(), 100_000.0)
    fixtures.write(run / "state.json", json.dumps(state(rid, "zte", "Tidy the cart", "failed", 0, 44, [], pid=1, host=host,
                                                        heartbeat=iso(43.9), image=image), indent=2), 100_000.0)
    fixtures.write(run / "run.log", f"2026-11-01T10:00:00.000Z [adw {rid}] zte start: class=chore dry_run=False\n"
                   f"2026-11-01T10:00:43.000Z [adw {rid}] preflight: 2 checks, 1 missed\n", 100_000.0)

    rid, run = "e1000006", agents / "e1000006"
    start = now - 30
    stamp = lambda offset: fixtures.datetime.fromtimestamp(start + offset, fixtures.timezone.utc).isoformat(timespec="milliseconds")  # noqa: E731
    ev = Events()
    ev.add(stamp(0), "run.start", adw="zte", adw_id=rid, klass="chore", dry_run=False, resumed=False, pid=os.getpid(),
           host=host, fanout_parent=None)
    ev.add(stamp(2), "env.build.start", image=image, recipe=SETUP_RECIPE)
    fixtures.write(run / "events.jsonl", ev.text(), 100_000.0)
    fixtures.write(run / "state.json", json.dumps(dict(state(rid, "zte", "Speed up checkout", "running", 0, None, []),
                                                       started=stamp(0), pid=os.getpid(), host=host,
                                                       heartbeat=stamp(29)), indent=2), 100_000.0)
    fixtures.write(run / "run.log", f"2026-11-01T10:00:00.000Z [adw {rid}] zte start: class=chore dry_run=False\n",
                   100_000.0)


PROOF_TREE = "9f2c1e7ab4d05566778899aabbccddeeff001122"


def add_proof_run(layer: Path) -> None:
    """e1000007 passed: a README-only change. Check `lint` ran and passed; check `test` was not
    affected (`check.decision`, step status `not-affected`); at the gate `lint` was reused from its
    step on the same tree and `test` was again not affected. state.json carries the ledger
    (`check_results`) the run page's "What the checks proved" block is built from."""
    agents, host = Path(layer) / "agents", socket.gethostname()
    rid, run = "e1000007", agents / "e1000007"
    same = 100_000.0
    checks = f"agents/{rid}/checks"
    not_affected = {"outcome": "not-affected", "why": "no covered path changed", "paths": ["README.md"],
                    "paths_total": 1, "tree": PROOF_TREE, "from_step": "", "from_phase": ""}
    ev = Events()
    ev.add(0, "run.start", adw="zte", adw_id=rid, klass="chore", dry_run=False, resumed=False, pid=1, host=host,
           fanout_parent=None)
    ev.step(1, None, "build", "run")
    ev.agent(1.5, 29.5, "build", "build", cost=0.05)
    ev.add(30, "step.end", step="build", kind="run", status="passed", detail="", seconds=29)
    ev.step(30.2, None, "lint", "check")
    ev.check(31, 35, "lint", 1, 0, rid)
    ev.add(35.1, "step.end", step="lint", kind="check", status="passed", detail="exit 0", seconds=4.9)
    ev.step(35.2, None, "test", "check")
    ev.add(35.5, "check.decision", step="test", owner="test", phase="check", cmd="test",
           log=f"{checks}/test.not-affected.log", **not_affected)
    ev.add(35.6, "step.end", step="test", kind="check", status="not-affected",
           detail="not affected: no covered path changed (1 paths compared)", seconds=0.4)
    ev.step(40, None, "ship", "ship")
    ev.add(41, "check.decision", step="gate", owner="ship", phase="gate", cmd="lint", outcome="reused", why="",
           paths=[], paths_total=0, tree=PROOF_TREE, from_step="lint", from_phase="check", log=f"{checks}/gate-lint.log")
    ev.add(41.5, "check.decision", step="gate", owner="ship", phase="gate", cmd="test", log=f"{checks}/gate-test.log",
           **not_affected)
    ev.add(42, "gate.decision", action="merge", ship=True, reasons=[])
    ev.add(43, "step.end", step="ship", kind="ship", status="passed", detail="", seconds=3)
    ev.add(44, "run.end", status="passed", exit=0)
    fixtures.write(run / "events.jsonl", ev.text(), same)
    fixtures.write(run / "build" / "prompt.md", "# build\n", same)
    fixtures.write(run / "build" / "raw.jsonl", simple_claude(1.5, 29.5, f"{LAYER_DIR}/trees/{rid}", "build done", 0.05), same)
    fixtures.write(run / "build" / "output.txt", "ok", same)
    fixtures.write(run / "checks" / "lint.1.log", "All checks passed!\n", same)
    fixtures.write(run / "checks" / "test.not-affected.log",
                   "al: check test not run: not affected: no covered path changed\nREADME.md\n", same)
    fixtures.write(run / "checks" / "gate-lint.log", "al: gate: lint not run: reused from lint, same tree 9f2c1e7a\n", same)
    fixtures.write(run / "checks" / "gate-test.log", "al: gate: test not run: not affected: no covered path changed\n", same)
    entry = {"definition": "0123456789abcdef", "why": "", "paths": [], "paths_total": 0, "from": None, "at": iso(35)}
    ledger = [
        {**entry, "check": "lint", "step": "lint", "phase": "check", "outcome": "passed", "tree": PROOF_TREE, "exit": 0,
         "log": f"{checks}/lint.1.log", "why": "runs for every change"},
        {**entry, "check": "test", "step": "test", "phase": "check", "outcome": "not-affected", "tree": PROOF_TREE,
         "exit": None, "log": f"{checks}/test.not-affected.log", "why": "no covered path changed",
         "paths": ["README.md"], "paths_total": 1},
        {**entry, "check": "lint", "step": "gate", "phase": "gate", "outcome": "reused", "tree": PROOF_TREE, "exit": 0,
         "log": f"{checks}/gate-lint.log", "from": {"step": "lint", "phase": "check", "log": f"{checks}/lint.1.log"}},
        {**entry, "check": "test", "step": "gate", "phase": "gate", "outcome": "not-affected", "tree": PROOF_TREE,
         "exit": None, "log": f"{checks}/gate-test.log", "why": "no covered path changed",
         "paths": ["README.md"], "paths_total": 1}]
    records = [with_agent(rec("build", "run", "passed", 1, 30)), rec("lint", "check", "passed", 30.2, 35.1, "exit 0"),
               rec("test", "check", "not-affected", 35.2, 35.6, "not affected: no covered path changed (1 paths compared)"),
               rec("ship", "ship", "passed", 40, 43)]
    fixtures.write(run / "state.json", json.dumps(state(
        rid, "zte", "Reword the README", "passed", 0, 44, records, pid=1, host=host, heartbeat=iso(43.9),
        checks={"lint": {"cmd": "ruff check .", "exit": 0, "log": f"{checks}/lint.1.log", "outcome": "passed",
                         "tree": PROOF_TREE},
                "test": {"cmd": "pytest -q", "exit": 0, "outcome": "not-affected", "why": "no covered path changed",
                         "paths": ["README.md"], "paths_total": 1, "tree": PROOF_TREE,
                         "log": f"{checks}/test.not-affected.log"}},
        check_results=ledger,
        ship={"action": "merge", "shipped": True, "target": "al/ship/main", "reasons": [],
              "gate": {"action": "merge", "ship": True, "reasons": []}}), indent=2), same)
    fixtures.write(run / "run.log", f"2026-11-01T10:00:00.000Z [adw {rid}] zte start: class=chore dry_run=False\n", same)
