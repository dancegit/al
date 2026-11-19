"""The kit workflow verify-batch (adw_modules/batch.py) through whole runs: changes delivered with
targeted checks are proven together by one full run; a regression the targeted checks missed is
found, bisected to its run, handed to a fix run, counted as a miss and learned; batches start when
due (count, hours, a closed cycle), a promotion's checks count as a stage batch, a revert is prepared
on a new branch and never moves one. The app and IR are targeted_fixtures.py; a detached start
(a batch, a fix run) is captured instead of spawned."""
from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adw_modules import batch, delivery, ir, promotion, testing
from adw_modules.runner import main as run_main

from targeted_fixtures import (BROKEN_DISCOUNT, DATA, SPRINT, SOURCES, change, commit_on, git, make_shop_layer,
                               merge_into, ran, reset, script, state_of)

DISCOUNT = "shop/plugins/discount.py"


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_DASHBOARD_URL", "ADW_FANOUT_PARENT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST"):
        monkeypatch.delenv(key, raising=False)


class Spawned:
    """batch._spawn: records each detached start (a batch run or a fix run) instead of starting it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **popen):
        self.calls.append([str(a) for a in argv])
        return None

    def batches(self) -> list[list[str]]:
        return [argv for argv in self.calls if any(a.endswith("verify_batch.py") for a in argv)]

    def others(self) -> list[list[str]]:
        return [argv for argv in self.calls if not any(a.endswith("verify_batch.py") for a in argv)]


@pytest.fixture
def spawned(monkeypatch) -> Spawned:
    recorder = Spawned()
    monkeypatch.setattr(batch, "_spawn", recorder)
    return recorder


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _ledger(layer: Path) -> list[dict]:
    return batch.ledger(ir.load(layer))


def _run_batch(layer: Path, stage: str | None, adw_id: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = batch.run(layer, stage, adw_id=adw_id)
    return code, out.getvalue()


def _new_test(n: int) -> dict[str, str]:
    return {f"tests/test_extra_{n:02d}.py": f"def test_extra_{n:02d}():\n    assert {n} + 1 == {n + 1}\n"}


# ---------------------------------------------------------------- a sprint proven per batch

def test_ten_changes_into_a_sprint_proven_by_one_full_run(make_layer, tmp_path, spawned):
    layer, _app, counter = make_shop_layer(make_layer, tmp_path, stages=True)
    ids = [f"a10000{n:02d}" for n in range(10)]
    for n, adw_id in enumerate(ids):
        change(layer, adw_id, _new_test(n))
        assert state_of(layer, adw_id)["delivery"]["branch"] == SPRINT
        assert len(spawned.batches()) == (1 if n == 9 else 0), n   # only the tenth landing starts a batch
    assert all(line.startswith("targeted tests/test_extra_") for line in ran(counter))   # each run: its own test
    (argv,) = spawned.batches()
    assert _arg(argv, "--stage") == "sprint"
    batch_id = _arg(argv, "--adw-id")
    assert state_of(layer, ids[-1])["testing"]["batch_started"] == batch_id
    assert batch.due(ir.load(layer), SPRINT)[0] is False          # a batch of the sprint is running
    reset(counter)
    code, out = _run_batch(layer, "sprint", batch_id)
    assert code == 0, out
    assert ran(counter) == ["full"]                                # the full form, once, on the stage head
    (line,) = _ledger(layer)
    assert (line["id"], line["branch"], line["stage"], line["status"], line["depth"]) == (
        batch_id, SPRINT, "sprint", "green", "full")
    assert line["delivered"] == ids
    assert line["head"] == git(_app, "rev-parse", f"refs/heads/{SPRINT}")
    state = state_of(layer, batch_id)
    assert state["status"] == "passed"
    assert {s["name"]: s["status"] for s in state["steps"]} == {
        "open": "passed", "checks": "passed", "record": "passed", "bisect": "skipped", "fix": "skipped",
        "close": "passed"}
    assert batch.due(ir.load(layer), SPRINT) == (False, "0 of 10 changes landed")


def test_planted_regression_is_caught_bisected_fixed_and_counted(make_layer, tmp_path, spawned):
    layer, app, counter = make_shop_layer(make_layer, tmp_path, stages=True,
                                          testing={"batchEvery": 5, "escalateOn": ["lockfile", "layer"],
                                                   "maxMissPercent": 50})
    ids = [f"a20000{n:02d}" for n in range(5)]
    for n, adw_id in enumerate(ids):
        files = {DISCOUNT: BROKEN_DISCOUNT} if n == 2 else _new_test(n)
        change(layer, adw_id, files, prompt=f"Change {n}")
    culprit = ids[2]
    acc = state_of(layer, culprit)["checks"]["unit"]["testing"]
    # only a dynamic import reaches the plugin: selection finds no test, the smoke form passes
    assert (acc["depth"], state_of(layer, culprit)["checks"]["unit"]["exit"]) == ("smoke", 0)
    (argv,) = spawned.batches()
    batch_id = _arg(argv, "--adw-id")
    reset(counter)
    code, out = _run_batch(layer, "sprint", batch_id)
    assert code == 1, out
    lines = ran(counter)
    assert lines[0] == "full" and len(lines) >= 3
    assert set(lines[1:]) == {"targeted tests/test_plugins.py"}    # bisect runs only the failing test
    state = state_of(layer, batch_id)
    assert state["status"] == "failed"
    assert state["batch"]["culprit"] == culprit
    assert state["batch"]["checks"]["unit"]["failing"] == ["tests/test_plugins.py::test_discount"]
    close = next(s for s in state["steps"] if s["name"] == "close")
    assert close["status"] == "failed" and f"culprit {culprit}" in close["detail"]
    (line,) = _ledger(layer)
    assert (line["status"], line["culprit"]) == ("red", culprit)
    # the fix run: started detached with the failing test, the culprit and its plan in its request
    assert len(spawned.others()) == 1, state["batch"].get("fix_error")
    (fix_argv,) = spawned.others()
    fix_id = _arg(fix_argv, "--adw-id")
    assert line["fix_run"] == fix_id == state["batch"]["fix_run"]
    request = Path(_arg(fix_argv, "--prompt-file")).read_text(encoding="utf-8")
    assert "tests/test_plugins.py::test_discount" in request and culprit in request
    assert state_of(layer, culprit)["plan"] in request
    assert batch.broken_by(ir.load(layer))[culprit]["id"] == batch_id
    # the miss is counted for unit and its strategies; 1 of the 5 runs the batch verified
    (miss,) = testing.misses(ir.load(layer))
    assert (miss["cmd"], miss["run"], miss["strategies"]) == ("unit", culprit, ["changed-tests", "imports"])
    rates = testing.miss_rates(ir.load(layer))["unit"]
    assert (rates["targeted"], rates["misses"], rates["rate"], rates["fallback"]) == (5, 1, 20.0, False)
    # what the batch taught: a change to the plugin selects its test from now on
    learned = json.loads((layer / "agents" / "testing" / "learned.json").read_text())
    assert [(e["files"], e["tests"]) for e in learned["unit"]] == [([DISCOUNT], ["tests/test_plugins.py"])]
    reset(counter)
    script(layer, "fix", "build", {"files": {DISCOUNT: SOURCES[DISCOUNT]}})
    script(layer, "fix", "commit", {"commit": True})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        run_main("fix", layer, ["--prompt-file", _arg(fix_argv, "--prompt-file"), "--class", "bug", "--adw-id", fix_id])
    fix = state_of(layer, fix_id)
    assert fix["checks"]["unit"]["testing"]["strategies"].get("learned") == 1, out.getvalue()
    assert ran(counter)[0] == "targeted tests/test_plugins.py"
    # a person merges the fix into the sprint; the next batch is green
    merge_into(app, SPRINT, fix["branch"])
    reset(counter)
    code, out = _run_batch(layer, "sprint", "b2000002")
    assert code == 0, out
    assert [(l["id"], l["status"]) for l in _ledger(layer)] == [(batch_id, "red"), ("b2000002", "green")]


# ---------------------------------------------------------------- when a batch is due

def test_after_hours_and_if_due(make_layer, tmp_path, spawned):
    layer, app, _counter = make_shop_layer(make_layer, tmp_path, testing={"batchEvery": 0, "batchAfterHours": 6})
    head = git(app, "rev-parse", "main")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert batch.main(layer, ["run", "--if-due"]) == 0
    assert out.getvalue().startswith("batch: not due (no change landed since the last batch)")
    ledger = layer / "agents" / "testing" / "batches.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    ledger.write_text(json.dumps({"id": "b3000001", "branch": "main", "stage": "", "head": head, "since": "",
                                  "status": "green", "depth": "full", "checks": {}, "delivered": [], "at": recent}) + "\n")
    output = change(layer, "a3000001", _new_test(1))
    assert not spawned.batches() and "batch: not due on main" in output
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds")
    ledger.write_text(ledger.read_text().replace(recent, old))
    ok, why = batch.due(ir.load(layer), "main")
    assert ok and why.startswith("a change landed on main and the last batch is 7 h old (after 6 h)")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert batch.main(layer, ["run", "--if-due", "--detach", "--json"]) == 0
    started = json.loads(out.getvalue().splitlines()[-1])
    assert started["status"] == "started" and len(spawned.batches()) == 1
    assert batch.due(ir.load(layer), "main") == (False, f"batch {started['run_id']} of main is running")


def test_cycle_close_starts_a_stage_batch(make_layer, tmp_path, spawned):
    layer, _app, _counter = make_shop_layer(make_layer, tmp_path, stages=True)
    delivery.cycle_close(ir.load(layer))
    (argv,) = spawned.batches()
    assert _arg(argv, "--stage") == "sprint"
    skeleton = state_of(layer, _arg(argv, "--adw-id"))
    assert skeleton["adw"] == "verify-batch" and skeleton["definition"]["batch"]["branch"] == SPRINT


def test_promotion_checks_run_on_stage_depth_and_record_a_batch(make_layer, tmp_path, spawned):
    sprint = json.loads((DATA / "delivery-sprint.json").read_text(encoding="utf-8"))
    promo = {**sprint["promotions"][0], "adw": "zte", "tag": "", "version": {}}
    layer, app, counter = make_shop_layer(make_layer, tmp_path, stages=True, promotions=[promo])
    change(layer, "a5000001", _new_test(1), klass="feature")
    reset(counter)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = promotion.main(layer, ["run", "sprint", "main"])
    assert code == 75, out.getvalue() + err.getvalue()   # waiting for the person's approval
    assert ran(counter) == ["full"]                       # the stage head at #:on-stage depth
    pid = json.loads((layer / "agents" / "promotions" / "sprint-main.json").read_text())["id"]
    page = (layer / "artefacts" / pid / "pack" / "index.html").read_text()
    assert "<th>check</th><th>depth</th><th>exit</th>" in page and "<td>unit</td><td>full</td>" in page
    assert "unit=0 (full)" in (layer / "artefacts" / pid / "pack" / "summary.md").read_text()
    (line,) = _ledger(layer)
    assert (line["source"], line["branch"], line["stage"], line["status"], line["depth"]) == (
        "promotion", SPRINT, "sprint", "green", "full")
    assert line["head"] == git(app, "rev-parse", f"refs/heads/{SPRINT}") and line["delivered"] == ["a5000001"]


# ---------------------------------------------------------------- failure handling

def test_revert_prepares_a_branch_and_never_moves_one(make_layer, tmp_path, spawned):
    layer, app, _counter = make_shop_layer(make_layer, tmp_path, testing={
        "batchEvery": 0, "escalateOn": [], "onBatchFailure": ["bisect", "open-fix", "revert"]})
    change(layer, "a6000001", {DISCOUNT: BROKEN_DISCOUNT})
    culprit_commit = state_of(layer, "a6000001")["ship"]["commit"]
    before = git(app, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    code, out = _run_batch(layer, None, "b6000001")
    assert code == 1, out
    state = state_of(layer, "b6000001")
    assert state["batch"]["culprit"] == "a6000001" and state["batch"]["revert"] == "al/revert/b6000001"
    reverted = git(app, "rev-parse", "refs/heads/al/revert/b6000001")
    assert git(app, "show", f"{reverted}:{DISCOUNT}") + "\n" == SOURCES[DISCOUNT]
    assert git(app, "rev-parse", f"{reverted}^") == git(app, "rev-parse", "main")
    # no existing ref moved: only the new revert branch was added
    after = git(app, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    assert set(after.splitlines()) - set(before.splitlines()) == {f"refs/heads/al/revert/b6000001 {reverted}"}
    assert git(app, "rev-parse", "main") == culprit_commit or culprit_commit in git(app, "rev-list", "main")
    # the default branch lets agents merge: the fix run is asked to take the revert
    (fix_argv,) = spawned.others()
    assert "A revert of that change is prepared on branch al/revert/b6000001" in \
        Path(_arg(fix_argv, "--prompt-file")).read_text()
    # a revert branch that exists already is never moved
    git(app, "update-ref", "refs/heads/al/revert/b6000002", git(app, "rev-parse", "main~1"))
    held = git(app, "rev-parse", "refs/heads/al/revert/b6000002")
    code, out = _run_batch(layer, None, "b6000002")
    assert code == 1
    assert git(app, "rev-parse", "refs/heads/al/revert/b6000002") == held
    assert "refs/heads/al/revert/b6000002 exists already; it is left as it is" in out
    assert "revert" not in state_of(layer, "b6000002")["batch"]


def test_no_culprit_commit_without_run(make_layer, tmp_path, spawned):
    layer, app, _counter = make_shop_layer(make_layer, tmp_path, testing={"batchEvery": 0})
    change(layer, "a7000001", _new_test(1))
    commit = commit_on(app, "main", {DISCOUNT: BROKEN_DISCOUNT}, "a person's direct commit")
    code, out = _run_batch(layer, None, "b7000001")
    assert code == 1, out
    state = state_of(layer, "b7000001")
    assert state["batch"]["culprit"] == f"commit {commit[:12]} (no run)"
    assert state["batch"]["culprit_commit"] == commit
    assert testing.misses(ir.load(layer)) == []                    # nobody's targeted check missed it
    assert not (layer / "agents" / "testing" / "learned.json").exists()
    assert batch.broken_by(ir.load(layer)) == {}
    (fix_argv,) = spawned.others()
    assert f"commit {commit[:12]} (no run)" in Path(_arg(fix_argv, "--prompt-file")).read_text()


def test_batch_on_trunk_default_branch(make_layer, tmp_path, spawned):
    layer, app, counter = make_shop_layer(make_layer, tmp_path, testing={"batchEvery": 2})
    change(layer, "a8000001", _new_test(1))
    assert not spawned.batches()
    change(layer, "a8000002", _new_test(2))
    (argv,) = spawned.batches()
    assert "--stage" not in argv
    batch_id = _arg(argv, "--adw-id")
    reset(counter)
    code, out = _run_batch(layer, None, batch_id)
    assert code == 0, out
    assert ran(counter) == ["full"]
    (line,) = _ledger(layer)
    assert (line["branch"], line["stage"], line["status"], line["delivered"]) == (
        "main", "", "green", ["a8000001", "a8000002"])
    assert line["head"] == git(app, "rev-parse", "main")
    (item,) = batch.status(ir.load(layer))
    assert (item["branch"], item["last"]["id"], item["delivered_since"], item["due"]) == ("main", batch_id, [], False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert batch.main(layer, ["status"]) == 0
    assert out.getvalue().splitlines()[0].startswith(f"main: last batch {batch_id} green at {line['head'][:7]}")
