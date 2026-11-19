"""A layer with targeted testing and batch verification, for the dashboard's Testing tests: built the way
`al gen` lays one out (tests/start_layer.py: the kit's runtime copied to adws/adw_modules, adws/dispatch.py),
with a `testing` IR, a sprint and the files the runtime writes, as code so every byte is reviewable:

  agents/settings/testing.json   (only when a test writes it)
  agents/testing/batches.jsonl   a green batch on main, then a red batch on the sprint (culprit, fix run)
  agents/testing/misses.jsonl    the miss the red batch counted
  agents/testing/suites.json     the full suites' last known time and size

Runs (synthetic state.json, the shapes of kit/runtime/adw_modules/state.py and testing.py):
  7e570001 ship-it  passed, targeted: ran 14 of 812 tests (changed-tests 3, imports 9, coverage 2), delivered
  7e570002 ship-it  passed, full: escalated by a core path, delivered
  7e570003 ship-it  passed, targeted, delivered: it broke the sprint's batch (the culprit)
  7e570004 ship-it  passed: the fix run the red batch opened, delivered after it
  7e570005 verify-batch  failed: the red batch run
  7e570006 ship-it  still running (this process's pid, started three minutes ago), targeted so far
Nothing here is real: the mock agent, a temporary application, invented names.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from start_layer import build_app, build_layer

NAME = "tested"
SPRINT = "sprint/2026-s47"
T0 = datetime(2026, 11, 16, 9, 0, 0, tzinfo=timezone.utc)
TARGETED, FULL, CULPRIT, FIX, BATCH, RUNNING = ("7e570001", "7e570002", "7e570003", "7e570004", "7e570005",
                                                "7e570006")
GREEN = "7e5700a1"   # the green batch on main: its run is not kept (pruned), only its ledger line
TREE = "4d3c2b1a00112233445566778899aabbccddeeff"
ACCOUNTING = "targeted: ran 14 of 812 tests (changed-tests 3, imports 9, coverage 2): 41 s instead of about 9 min"
FIX_FILE = "batch-calls.jsonl"   # what the fake adws/verify_batch.py was called with, one argv per line

POLICY = {"depth": "targeted", "perRun": "inherit", "reviewRounds": "inherit", "onStage": "full", "batchEvery": 5,
          "batchAfterHours": 6, "escalatePaths": ["shop/core/**"], "escalateDiffLines": 1500, "escalateFiles": 40,
          "escalateOn": ["lockfile", "layer", "unmapped"], "escalateClasses": [], "onBatchFailure": ["bisect", "open-fix"],
          "fixAdw": "ship-it", "maxMissPercent": 10}

# a stand-in for the generated adws/verify_batch.py: records its argv and answers like a detached start
VERIFY_BATCH = '''import json, sys
from pathlib import Path
layer = Path(__file__).resolve().parent.parent
with open(layer / "%s", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if "--stage" in sys.argv and sys.argv[sys.argv.index("--stage") + 1] == "busy":
    print(json.dumps({"status": "error", "reason": "already-running", "error": "a batch of this branch is running"}))
    sys.exit(1)
run = layer / "agents" / "7e5700b1"
run.mkdir(parents=True, exist_ok=True)
(run / "state.json").write_text(json.dumps({"adw_id": "7e5700b1", "adw": "verify-batch", "status": "running",
                                            "prompt": "verify the full suites", "steps": []}))
print(json.dumps({"status": "started", "run_id": "7e5700b1", "branch": "main"}))
''' % FIX_FILE


def iso(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def change_ir(stages: bool, batch: bool, declared: bool):
    """The IR edit given to start_layer.build_layer: commands with narrower forms, a lint check, the
    testing policy and, optionally, the sprint preset's stages."""
    def change(ir: dict) -> None:
        ir["codebase"]["commands"] = [
            {"name": "test", "run": "pytest -q", "timeout": 30, "covers": ["shop", "tests"], "always": False,
             "targeted": "pytest -q {tests}", "smoke": "pytest -q -m smoke",
             "select": ["changed-tests", "imports", "coverage"], "pathMap": [], "maxTargeted": 200},
            {"name": "lint", "run": "ruff check .", "timeout": 30}]
        ship_it = next(a for a in ir["adws"] if a["name"] == "ship-it")
        ship_it["classes"] = ["chore", "bug"]
        ship_it["steps"] = [ship_it["steps"][0], {"name": "test", "kind": "check", "cmd": "test", "retries": 0, "collect": []},
                            {"name": "lint", "kind": "check", "cmd": "lint", "retries": 0, "collect": []},
                            {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]
        if declared:
            ir["testing"] = {**POLICY, **({} if batch else {"batchEvery": 0, "batchAfterHours": 0})}
        if stages:
            data = json.loads((Path(__file__).resolve().parents[2] / "runtime" / "tests" / "data"
                               / "delivery-sprint.json").read_text())
            ir.update({k: data[k] for k in ("stages", "cycle", "delivery")})
    return change


def _write(path: Path, data, at: float | None = None) -> None:
    """`at`: the file's time in minutes after T0 (a check's attempt ends at its log's mtime)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
    if at is not None:
        stamp = (T0 + timedelta(minutes=at)).timestamp()
        os.utime(path, (stamp, stamp))


def accounting(depth: str, *, asked: str = "", why: str = "", selected: int = 14, ran: int = 14, total: int | None = 812,
               seconds: float = 41.0, full_seconds: float | None = 540.0, strategies: dict | None = None) -> dict:
    return {"depth": depth, "asked": asked or depth, "why": why, "selected": selected, "units": "tests",
            "strategies": strategies if strategies is not None else {"changed-tests": 3, "imports": 9, "coverage": 2},
            "ran": ran, "total": total, "seconds": seconds, "full_seconds": full_seconds}


def _step(name: str, kind: str, status: str, start: float, end: float | None, detail: str = "") -> dict:
    return {"name": name, "kind": kind, "status": status, "attempts": 1, "started": iso(start),
            "ended": iso(end) if end is not None else "", "detail": detail}


def write_run(layer: Path, rid: str, *, prompt: str, start: float, status: str = "passed", depth: str = "targeted",
              source: str = "layer policy", test_acc: dict | None = None, lint_acc: dict | None = None,
              escalation: list[str] | None = None, delivered: bool = True, adw: str = "ship-it", klass: str = "chore",
              extra: dict | None = None) -> Path:
    """One run of ship-it (plan, test, lint, review, ship) as the runtime records it, with the testing
    decision in state.testing, the accounting per check and the ledger entries carrying `depth`."""
    run = layer / "agents" / rid
    end = start + 6 if status != "running" else None
    test_acc = test_acc if test_acc is not None else accounting(depth)
    lint_acc = lint_acc if lint_acc is not None else accounting("full", why="no narrower form", selected=0, ran=0,
                                                                 total=None, seconds=4.0, full_seconds=4.0, strategies={})
    checks_dir = f"agents/{rid}/checks"
    steps = [_step("plan", "run", "passed", start, start + 1, "specs/plan.md"),
             _step("test", "check", "passed", start + 1, start + 2, f"passed — {ACCOUNTING}" if depth == "targeted" else "passed — full"),
             _step("lint", "check", "passed", start + 2, start + 3, "passed — full")]
    if status != "running":
        steps += [_step("review", "review", "passed", start + 3, start + 4), _step("ship", "ship", "passed", start + 4, start + 6)]
    else:
        steps[-1]["status"], steps[-1]["ended"] = "running", ""
    entry = {"definition": "0123456789abcdef", "why": "", "paths": [], "paths_total": 0, "from": None, "tree": TREE}
    ledger = [{**entry, "check": "test", "step": "test", "phase": "check", "outcome": "passed", "exit": 0,
               "log": f"{checks_dir}/test.1.log", "depth": test_acc["depth"], "testing": test_acc, "at": iso(start + 2)}]
    if status != "running":
        ledger += [{**entry, "check": "lint", "step": "lint", "phase": "check", "outcome": "passed", "exit": 0,
                    "log": f"{checks_dir}/lint.1.log", "depth": "full", "testing": lint_acc, "at": iso(start + 3)},
                   {**entry, "check": "test", "step": "gate", "phase": "gate", "outcome": "reused", "exit": 0,
                    "log": f"{checks_dir}/gate-test.log", "depth": test_acc["depth"], "at": iso(start + 5),
                    "from": {"step": "test", "phase": "check", "log": f"{checks_dir}/test.1.log"}}]
    command = "pytest -q tests/test_cart.py" if test_acc["depth"] == "targeted" else "pytest -q"
    checks = {"test": {"cmd": command, "exit": 0,
                       "log": f"{checks_dir}/test.1.log", "outcome": "passed", "tree": TREE, "testing": test_acc}}
    if status != "running":
        checks["lint"] = {"cmd": "ruff check .", "exit": 0, "log": f"{checks_dir}/lint.1.log", "outcome": "passed",
                          "tree": TREE, "testing": lint_acc}
    state = {"adw_id": rid, "adw": adw, "prompt": prompt, "source": "cli", "klass": klass, "class_fixed": True,
             "dry_run": False, "model_set": "", "branch": f"chore/{rid}", "ports": [], "outputs": {}, "steps": steps,
             "checks": checks, "check_results": ledger, "artefacts": [], "review": {}, "fanout": {}, "status": status,
             "started": iso(start), "ended": iso(end) if end is not None else "",
             "testing": {"requested": "", "depth": depth, "source": source,
                         "escalation": {TREE: escalation} if escalation else {}}}
    if delivered and status != "running":
        state["ship"] = {"action": "merge", "shipped": True, "target": SPRINT, "commit": rid * 5,
                         "gate": {"action": "merge", "ship": True, "reasons": []}}
        state["delivery"] = {"stage": "sprint", "branch": SPRINT}
    if status == "running":
        state.update({"pid": os.getpid(), "host": socket.gethostname(),
                      "heartbeat": datetime.now(timezone.utc).isoformat()})
    state.update(extra or {})
    _write(run / "state.json", state)
    _write(run / "run.log", f"[adw {rid}] {adw}\n")
    _write(run / "checks" / "test.1.log", "14 passed in 41.0s\n", at=start + 2)
    if status != "running":
        _write(run / "checks" / "lint.1.log", "All checks passed!\n", at=start + 3)
    return run


def write_batch_run(layer: Path) -> Path:
    """7e570005: the red batch on the sprint (the kit workflow verify-batch, a synthetic ADW)."""
    run = layer / "agents" / BATCH
    names = ("open", "checks", "record", "bisect", "fix", "close")
    steps = [_step(n, "batch", "failed" if n == "close" else "passed", 300 + i, 301 + i) for i, n in enumerate(names)]
    steps[-1]["detail"] = f"red: tests/test_plugins.py failed; culprit {CULPRIT}; fix run {FIX}"
    state = {"adw_id": BATCH, "adw": "verify-batch", "prompt": f"verify the full suites on {SPRINT}", "source": "cli",
             "klass": "chore", "status": "failed", "started": iso(300), "ended": iso(307), "steps": steps,
             "checks": {"batch-test": {"cmd": "pytest -q", "exit": 1, "log": f"agents/{BATCH}/checks/batch-test.1.log",
                                       "outcome": "failed", "tree": TREE,
                                       "testing": accounting("full", ran=812, total=812, seconds=540.0, strategies={})}},
             "definition": {"name": "verify-batch", "steps": [{"name": n, "kind": "batch", "action": n} for n in names]},
             "batch": {"branch": SPRINT, "stage": "sprint", "head": "c0ffee1" + "0" * 33, "culprit": CULPRIT,
                       "fix_run": FIX}}
    _write(run / "state.json", state)
    _write(run / "run.log", f"[adw {BATCH}] verify-batch\n")
    _write(run / "checks" / "batch-test.1.log", "FAILED tests/test_plugins.py::test_discount\n1 failed, 811 passed\n", at=302)
    return run


def ledger_lines() -> list[dict]:
    green = {"id": GREEN, "branch": "main", "stage": "main", "head": "a1b2c3d" + "0" * 33, "since": "9" * 40,
             "status": "green", "depth": "full", "checks": {"test": {"exit": 0, "seconds": 520.0}, "lint": {"exit": 0}},
             "delivered": [FULL], "at": iso(120), "source": "promotion"}
    red = {"id": BATCH, "branch": SPRINT, "stage": "sprint", "head": "c0ffee1" + "0" * 33, "since": "a1b2c3d" + "0" * 33,
           "status": "red", "depth": "full",
           "checks": {"test": {"exit": 1, "failing": ["tests/test_plugins.py::test_discount"]}, "lint": {"exit": 0}},
           "delivered": [TARGETED, CULPRIT], "at": iso(307), "culprit": CULPRIT, "fix_run": FIX, "source": "rhythm"}
    return [green, red]


def build_testing_site(root: Path, *, stages: bool = True, batch: bool = True, declared: bool = True,
                       runs: bool = True, script: bool = True) -> dict:
    """root/app and root/tested.al: the layer, its runs and the files under agents/testing/."""
    app = build_app(root)
    subprocess.run(["git", "-C", str(app), "branch", SPRINT, "main"], check=True, capture_output=True, timeout=30)
    layer = build_layer(root, NAME, app, None, port_base=28900, change=change_ir(stages, batch, declared))
    if script:
        _write(layer / "adws" / "verify_batch.py", VERIFY_BATCH)
    if stages:
        _write(layer / "agents" / "cycle.json", {
            "version": 1, "kind": "sprint", "stage": "sprint", "history": [],
            "current": {"name": "2026-s47", "version": "", "opened_at": iso(0), "n": 1, "branch": SPRINT,
                        "from_commit": ""}})
    site = {"root": root, "app": app, "layer": layer, "key": NAME}
    if not runs:
        return site
    write_run(layer, TARGETED, prompt="Add a cart total", start=10)
    write_run(layer, FULL, prompt="Rework the core pricing", start=60, depth="full",
              test_acc=accounting("full", asked="targeted", why="core path shop/core/price.py (shop/core/**)", ran=812,
                                  total=812, seconds=530.0, strategies={}),
              escalation=["core path shop/core/price.py (shop/core/**)"])
    write_run(layer, CULPRIT, prompt="Change the discount plugin", start=200,
              test_acc=accounting("targeted", selected=2, ran=2, seconds=30.0,
                                  strategies={"changed-tests": 1, "imports": 1}))
    write_run(layer, FIX, prompt=f"Fix the batch {BATCH}: tests/test_plugins.py fails", start=320, klass="bug",
              test_acc=accounting("targeted", selected=1, ran=1, seconds=12.0, strategies={"changed-tests": 1}))
    write_batch_run(layer)
    started = (datetime.now(timezone.utc) - timedelta(minutes=3) - T0).total_seconds() / 60   # three minutes ago
    write_run(layer, RUNNING, prompt="Add a wish list", start=started, status="running")
    testing = layer / "agents" / "testing"
    _write(testing / "batches.jsonl", "".join(json.dumps(line) + "\n" for line in ledger_lines()))
    _write(testing / "misses.jsonl", json.dumps({
        "batch": BATCH, "run": CULPRIT, "cmd": "test", "strategies": ["changed-tests", "imports"],
        "tests": ["tests/test_plugins.py"], "files": ["shop/plugins/discount.py"], "at": iso(306)}) + "\n")
    _write(testing / "suites.json", {"test": {"seconds": 540.0, "tests": 812, "commit": "a1b2c3d" + "0" * 33, "at": iso(120)},
                                     "lint": {"seconds": 4.0, "tests": 0, "commit": "a1b2c3d" + "0" * 33, "at": iso(120)}})
    return site
