"""The Testing panel's model and a run's depth, from the files the runtime writes (tests/testing_fixtures.py):
the local setting agents/settings/testing.json, the batch ledger, misses and suite timings under
agents/testing/, and each run's state.testing, per-check accounting and ledger depth. The dashboard reads
them directly; it never imports the runtime."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import dashboard as d
import testing_fixtures as tf


def model_for(layer: Path, **kwargs) -> d.Model:
    model = d.Model([layer], [], **kwargs)
    model.discover()
    model.tick()
    return model


def view(model: d.Model) -> dict:
    return model.layer_list()[0]["testing"]


@pytest.fixture()
def site(tmp_path: Path) -> dict:
    return tf.build_testing_site(tmp_path / "site")


def test_testing_view_undeclared_note(tmp_path):
    site = tf.build_testing_site(tmp_path / "old", declared=False, runs=False)
    testing = view(model_for(site["layer"]))
    assert testing["declared"] is False
    assert (testing["level"], testing["source"]) == ("heavy", "layer file")
    assert testing["note"] == ("this layer has no (testing …) clause, so every check runs its full form; "
                               "add (testing targeted …) and #:targeted forms to test what a change touches")
    assert testing["writable"] is True and testing["batches"][0]["last"] is None


def test_testing_view_levels_minutes_and_miss_rates(site):
    testing = view(model_for(site["layer"]))
    assert testing["declared"] is True and testing["note"] == ""
    assert (testing["level"], testing["source"]) == ("standard", "layer file")
    assert testing["levels"] == [{"level": "light", "depth": "smoke", "minutes": None},
                                 {"level": "standard", "depth": "targeted", "minutes": 0.6},
                                 {"level": "heavy", "depth": "full", "minutes": 8.9}]
    test, lint = testing["commands"]
    assert {k: test[k] for k in ("cmd", "forms", "targeted", "misses", "rate", "fallback")} == {
        "cmd": "test", "forms": ["full", "targeted", "smoke"], "targeted": 2, "misses": 1, "rate": 50.0, "fallback": True}
    assert test["by_strategy"] == {"changed-tests": {"targeted": 2, "misses": 1, "rate": 50.0},
                                   "imports": {"targeted": 2, "misses": 1, "rate": 50.0},
                                   "coverage": {"targeted": 1, "misses": 0, "rate": 0.0}}
    assert {k: lint[k] for k in ("cmd", "forms", "targeted", "misses", "rate", "fallback")} == {
        "cmd": "lint", "forms": ["full"], "targeted": 0, "misses": 0, "rate": None, "fallback": False}
    # the local setting wins and says so
    d.write_testing_setting(site["layer"], "heavy")
    assert json.loads((site["layer"] / d.TESTING_FILE).read_text()) == {"version": 1, "level": "heavy"}
    assert d.read_testing_setting(site["layer"]) == "heavy"
    testing = view(model_for(site["layer"]))
    assert (testing["level"], testing["source"]) == ("heavy", "local setting")
    d.write_testing_setting(site["layer"], None)
    assert not (site["layer"] / d.TESTING_FILE).exists() and d.read_testing_setting(site["layer"]) is None
    # a broken file is reported and not used
    (site["layer"] / d.TESTING_FILE).write_text("{oops")
    testing = view(model_for(site["layer"]))
    assert testing["error"] and "al testing LAYER --reset" in testing["error"] and testing["source"] == "layer file"


def test_setting_mirror_refuses_trunk_zero_touch_without_batch(tmp_path):
    site = tf.build_testing_site(tmp_path / "trunk", stages=False, batch=False, runs=False)
    ir = json.loads((site["layer"] / "agentic-layer.json").read_text())
    ok, why = d.testing_allowed(ir, "standard")
    assert not ok and why == ("(adw ship-it) ships to main without a person at testing depth targeted, but no batch "
                              "verification is declared: add #:batch ((every N)) or ((after-hours H)) to (testing …), "
                              "or #:per-run full")
    assert d.testing_allowed(ir, "light")[0] is False and d.testing_allowed(ir, "heavy") == (True, "")
    ir["testing"]["batchAfterHours"] = 24          # a batch rhythm: allowed
    assert d.testing_allowed(ir, "standard") == (True, "")
    ir["testing"]["batchAfterHours"] = 0
    ir["reviews"][1]["ship"] = "pr"              # nothing lands without a person
    assert d.testing_allowed(ir, "standard") == (True, "")
    with pytest.raises(ValueError):
        d.write_testing_setting(site["layer"], "medium")
    view_ = view(model_for(site["layer"]))
    assert view_["allowed"]["standard"]["ok"] is False and "#:batch ((every N))" in view_["allowed"]["standard"]["why"]
    assert view_["allowed"]["heavy"] == {"ok": True, "why": ""}


def test_sprint_delivery_is_allowed_without_batch(tmp_path):
    """ship-it delivers chores and bugs into the sprint, which is not the final stage."""
    site = tf.build_testing_site(tmp_path / "sprint", stages=True, batch=False, runs=False)
    ir = json.loads((site["layer"] / "agentic-layer.json").read_text())
    assert d.testing_allowed(ir, "standard") == (True, "")
    ir["adws"][1]["classes"] = ["chore", "feature"]
    ir["stages"][0]["accepts"] = ["chore"]         # a feature goes to the final stage, main
    assert d.testing_allowed(ir, "light")[0] is False


def test_batches_per_stage_with_culprit_and_fix(site):
    testing = view(model_for(site["layer"]))
    sprint, main = testing["batches"]
    assert (sprint["stage"], sprint["branch"], main["stage"], main["branch"]) == ("sprint", tf.SPRINT, "main", "main")
    last = sprint["last"]
    assert {k: last[k] for k in ("id", "status", "depth", "culprit", "culprit_run", "fix_run", "source", "run")} == {
        "id": tf.BATCH, "status": "red", "depth": "full", "culprit": tf.CULPRIT, "culprit_run": tf.CULPRIT,
        "fix_run": tf.FIX, "source": "rhythm", "run": tf.BATCH}
    assert last["delivered"] == [tf.TARGETED, tf.CULPRIT] and last["failing"] == ["tests/test_plugins.py::test_discount"]
    assert last["head"].startswith("c0ffee1") and last["at"] == tf.iso(307)
    assert sprint["delivered_since"] == [tf.FIX]
    assert sprint["next"] == "after 4 more landings (1 of 5), or once the last batch is 6 h old"
    assert main["last"]["status"] == "green" and main["last"]["run"] == "" and main["delivered_since"] == []
    assert main["last"]["culprit"] == "" and main["last"]["fix_run"] == ""
    assert testing["batch"] == {"enabled": True, "why": ""}
    # the start is refused where starting is
    assert view(model_for(site["layer"], no_start=True))["batch"] == {
        "enabled": False, "why": "the dashboard was started with --no-start"}
    (site["layer"] / "adws" / "verify_batch.py").unlink()
    assert "run `al gen" in view(model_for(site["layer"]))["batch"]["why"]


def test_trunk_batches_on_the_default_branch(tmp_path):
    site = tf.build_testing_site(tmp_path / "trunk", stages=False, runs=False)
    [only] = view(model_for(site["layer"]))["batches"]
    assert (only["stage"], only["branch"], only["last"], only["delivered_since"]) == ("", "main", None, [])


def test_run_detail_depth_and_accounting(site):
    model = model_for(site["layer"])
    testing = model.run_detail(tf.NAME, tf.TARGETED)["testing"]
    assert {k: testing[k] for k in ("depth", "source", "escalation", "requested_full", "broke_batch", "declared")} == {
        "depth": "targeted", "source": "layer policy", "escalation": [], "requested_full": None, "broke_batch": None,
        "declared": True}
    assert testing["checks"] == [{"name": "test", "depth": "targeted", "text": tf.ACCOUNTING},
                                 {"name": "lint", "depth": "full", "text": "full (no narrower form)"}]
    full = model.run_detail(tf.NAME, tf.FULL)["testing"]
    assert full["escalation"] == ["core path shop/core/price.py (shop/core/**)"]
    assert full["checks"][0]["text"] == "full (core path shop/core/price.py (shop/core/**))"
    # a person asked for full before it ships: the request file of the run
    running = model.run_detail(tf.NAME, tf.RUNNING)
    assert running["summary"]["status"] == "running" and running["testing"]["requested_full"] is None
    (site["layer"] / "agents" / tf.RUNNING / "testing-request.json").write_text(
        json.dumps({"depth": "full", "by": "dashboard", "at": tf.iso(401)}))
    asked = model.run_detail(tf.NAME, tf.RUNNING)["testing"]["requested_full"]
    assert asked == {"by": "dashboard", "at": tf.iso(401)}


@pytest.mark.parametrize("acc, text", [
    (tf.accounting("targeted"), tf.ACCOUNTING),
    (tf.accounting("targeted", total=None, full_seconds=None),
     "targeted: ran 14 tests (full suite size not known yet; changed-tests 3, imports 9, coverage 2): 41 s"),
    (tf.accounting("smoke", strategies={}, ran=20, total=812), "smoke: ran 20 tests: 41 s instead of about 9 min"),
    (tf.accounting("full", asked="targeted", why="12 changed files > 10"), "full (12 changed files > 10)"),
    (tf.accounting("full", why=""), "full"),
    ({"depth": "targeted", "ran": 3, "ran_from": "selection", "units": "test files", "total": None},
     "targeted: ran the 3 selected test files (full suite size not known yet)"),
    ({"depth": "targeted", "ran": 2, "total": 812, "seconds": 5300, "full_seconds": 9000, "strategies": {}},
     "targeted: ran 2 of 812 tests: 88 min instead of about 2.5 h"),
])
def test_accounting_text(acc, text):
    assert d.accounting_text(acc) == text
    assert "passed" not in d.accounting_text(acc)
    assert d.accounting_text(None) == ""        # a ledger entry from before depths


def test_run_broke_the_batch_badge(site):
    model = model_for(site["layer"])
    culprit = model.run_detail(tf.NAME, tf.CULPRIT)
    expected = {"batch": tf.BATCH, "fix_run": tf.FIX, "branch": tf.SPRINT, "stage": "sprint", "at": tf.iso(307),
                "failing": ["tests/test_plugins.py::test_discount"]}
    assert culprit["summary"]["broke_batch"] == expected and culprit["testing"]["broke_batch"] == expected
    assert model.summaries[f"{tf.NAME}/{tf.CULPRIT}"]["broke_batch"] == expected
    assert model.summaries[f"{tf.NAME}/{tf.TARGETED}"]["broke_batch"] is None
    # a later ledger line naming another culprit reaches that run's summary on the next tick
    with (site["layer"] / "agents" / "testing" / "batches.jsonl").open("a") as fh:
        fh.write(json.dumps({**tf.ledger_lines()[1], "id": "7e5700c1", "culprit": tf.TARGETED, "fix_run": "",
                             "at": tf.iso(500)}) + "\n")
    model.checked.clear()
    model.tick()
    assert model.summaries[f"{tf.NAME}/{tf.TARGETED}"]["broke_batch"]["batch"] == "7e5700c1"
    # a culprit that is a commit without a run names no run
    assert d.broken_by([{"status": "red", "culprit": "commit 1234567 (no run)", "id": "7e5700c2"}]) == {}


def test_proof_rows_carry_depth(site):
    rows = model_for(site["layer"]).run_detail(tf.NAME, tf.TARGETED)["proof"]
    assert [(r["check"], r["where"], r["outcome"], r["depth"]) for r in rows] == [
        ("test", "test", "passed", "targeted"), ("test", "gate", "reused", "targeted"), ("lint", "lint", "passed", "full")]
    assert rows[0]["accounting"] == tf.ACCOUNTING and rows[2]["accounting"] == "full (no narrower form)" and rows[1]["accounting"] == ""
    # a ledger from before depths: no depth, no accounting
    assert all(r["depth"] == "" for r in d.proof_view({"check_results": [
        {"check": "x", "step": "x", "phase": "check", "outcome": "passed", "exit": 0}]}, str))


def test_watcher_republishes_layers_when_the_setting_changes(site):
    model = model_for(site["layer"])
    seen: list[str] = []
    model.hub.publish = lambda kind, data: seen.append(kind)   # type: ignore[method-assign]
    model.tick()
    assert "layers" not in seen
    d.write_testing_setting(site["layer"], "heavy")
    model.tick()
    assert "layers" in seen
