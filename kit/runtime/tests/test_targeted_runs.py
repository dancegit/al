"""Targeted testing through whole runs (runner.main, mock agent, a real pytest suite): a one-file
change runs the tests that concern it, escalation and a person's choice make a run full, a review
round re-checks what its patch touched, a layer without (testing …) is unchanged, and a full run
records its coverage map. The app and IR are targeted_fixtures.py (the shape of
racket/al/tests/fixtures/targeted-testing.rkt)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adw_modules import batch, coverage_map, ir, state as state_module, testing
from adw_modules.runner import summary_lines

from targeted_fixtures import change, git, make_shop_layer, ran, reset, script, state_of, unit_cmd

CHECK = "unit"


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_DASHBOARD_URL", "ADW_FANOUT_PARENT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST"):
        monkeypatch.delenv(key, raising=False)


def _summary(layer: Path, adw_id: str) -> list[str]:
    return summary_lines(ir.load(layer), state_module.load(layer, adw_id))


def _line(lines: list[str], prefix: str) -> str:
    return next(line for line in lines if line.startswith(prefix))


def _full_first(layer: Path, counter: Path) -> None:
    """A full run that landed: the suite's size and time are known afterwards (suites.json)."""
    change(layer, "f0000001", {"shop/cart.py": "def total(prices):\n    return sum(prices)  # full\n"},
           argv=("--testing", "full"))
    assert ran(counter) == ["full"]
    reset(counter)


def test_one_file_change_runs_few_tests_under_a_minute(make_layer, tmp_path):
    layer, _app, counter = make_shop_layer(make_layer, tmp_path)
    _full_first(layer, counter)
    suites = json.loads((layer / "agents" / "testing" / "suites.json").read_text())
    assert suites["unit"]["tests"] == 43
    change(layer, "a0000001", {"shop/cart.py": "def total(prices):\n    return sum(prices) + 0\n",
                               "tests/test_cart.py": ("from shop.cart import total\n\n\ndef test_total():\n"
                                                      "    assert total([1, 2]) == 3\n\n\ndef test_empty():\n"
                                                      "    assert total([]) == 0\n")})
    # the check ran the changed test and the test that imports the changed module; never the 40 fillers;
    # the gate reused that pass (same tree, same definition)
    assert ran(counter) == ["targeted tests/test_cart.py tests/test_price.py"]
    state = state_of(layer, "a0000001")
    acc = state["checks"][CHECK]["testing"]
    assert acc["depth"] == "targeted" and acc["strategies"] == {"changed-tests": 1, "imports": 1}
    assert acc["ran"] == 3 and acc["total"] == 43 and acc["seconds"] < 60
    assert [e["outcome"] for e in state["check_results"] if e["phase"] == "gate"] == ["reused"]
    lines = _summary(layer, "a0000001")
    checks = _line(lines, "checks:")
    assert checks.startswith("checks:    unit=passed (targeted: ran 3 of 43 tests (changed-tests 1, imports 1): ")
    assert "instead of about" in checks
    assert _line(lines, "testing:") == "testing:   targeted (layer policy)"
    assert state["status"] == "passed" and state["ship"]["shipped"] is True


def test_core_path_escalates_to_full_with_reason(make_layer, tmp_path):
    layer, _app, counter = make_shop_layer(make_layer, tmp_path)
    change(layer, "a0000002", {"shop/core/rules.py": "LIMIT = 3\n", "tests/test_rules.py":
                               "from shop.core import rules\n\n\ndef test_limit():\n    assert rules.LIMIT == 3\n"})
    assert ran(counter) == ["full"]
    state = state_of(layer, "a0000002")
    assert state["checks"][CHECK]["testing"]["depth"] == "full"
    assert state["checks"][CHECK]["testing"]["why"] == "escalated: core path shop/core/rules.py (shop/core/**)"
    assert _line(_summary(layer, "a0000002"), "testing:") == (
        "testing:   targeted (layer policy); escalated: core path shop/core/rules.py (shop/core/**)")


def test_testing_flag_and_heavy_setting_make_the_next_run_full(make_layer, tmp_path):
    layer, _app, counter = make_shop_layer(make_layer, tmp_path)
    cart = "def total(prices):\n    return sum(prices)  # {}\n"
    change(layer, "a0000003", {"shop/cart.py": cart.format(1)}, argv=("--testing", "full"))
    assert ran(counter) == ["full"]
    assert _line(_summary(layer, "a0000003"), "testing:") == "testing:   full (--testing)"
    reset(counter)
    testing.write_setting(layer, "heavy")
    change(layer, "a0000004", {"shop/cart.py": cart.format(2)})
    assert ran(counter) == ["full"]
    assert _line(_summary(layer, "a0000004"), "testing:") == "testing:   full (local setting)"
    reset(counter)
    testing.write_setting(layer, "light")
    change(layer, "a0000005", {"shop/cart.py": cart.format(3)})
    assert ran(counter) == ["smoke"]
    assert state_of(layer, "a0000005")["checks"][CHECK]["testing"]["depth"] == "smoke"


def test_full_before_it_ships_request_makes_the_gate_full(make_layer, tmp_path):
    layer, _app, counter = make_shop_layer(make_layer, tmp_path)
    # the build step asks for full before it ships, as the dashboard's button does while the run runs
    script(layer, "zte", "build", {"files": {"shop/cart.py": "def total(prices):\n    return sum(prices)  # x\n"},
                                   "layer_files": {"agents/a0000006/testing-request.json":
                                                   json.dumps({"depth": "full", "by": "dashboard", "at": "now"})}})
    script(layer, "zte", "commit", {"commit": True})
    from adw_modules.runner import main

    assert main("zte", layer, ["Change the cart", "--class", "chore", "--adw-id", "a0000006"]) == 0
    assert ran(counter) == ["targeted tests/test_cart.py tests/test_price.py", "full"]
    state = state_of(layer, "a0000006")
    check, gate = ([e for e in state["check_results"] if e["phase"] == phase][-1] for phase in ("check", "gate"))
    assert (check["depth"], check["outcome"]) == ("targeted", "passed")
    assert (gate["depth"], gate["outcome"]) == ("full", "passed")
    assert gate["definition"] != check["definition"]
    assert "gate: full (a person asked for full before it ships)" in _line(_summary(layer, "a0000006"), "testing:")


def test_review_round_selects_from_the_patch(make_layer, tmp_path):
    layer, _app, counter = make_shop_layer(make_layer, tmp_path)
    script(layer, "zte", "review-1", {"output": json.dumps({"success": False, "summary": "the tax is wrong", "issues": [
        {"severity": "blocker", "description": "with_tax rounds", "resolution": "fix price.py"}]})})
    script(layer, "zte", "patch-1", {"layer_files": {"specs/patch-1.md": "# Patch\nfix price.py\n"},
                                     "output": "specs/patch-1.md"})
    script(layer, "zte", "implement-1", {"files": {"shop/price.py": (
        "from shop import cart\n\n\ndef with_tax(prices, rate=0.25):\n    return cart.total(prices) * (1 + rate)  # r\n")},
        "commit": True})
    change(layer, "a0000007", {"tests/test_cart.py": ("from shop.cart import total\n\n\ndef test_total():\n"
                                                      "    assert total([1, 2]) == 3\n\n\ndef test_one():\n"
                                                      "    assert total([1]) == 1\n")})
    lines = ran(counter)
    assert lines[0] == "targeted tests/test_cart.py"
    # the round's re-check is selected from what the patch touched: price.py and the tests importing it
    assert "targeted tests/test_price.py" in lines[1:]
    assert not [line for line in lines if line == "full" or "test_filler" in line]


def test_layer_without_testing_is_unchanged(make_layer, tmp_path):
    layer, _app, counter = make_shop_layer(make_layer, tmp_path, declared=False)
    change(layer, "a0000008", {"shop/cart.py": "def total(prices):\n    return sum(prices)  # plain\n"})
    assert ran(counter) == ["full"]
    state = state_of(layer, "a0000008")
    check = state["checks"][CHECK]
    assert "testing" not in check and check["cmd"] == unit_cmd(tmp_path / "ran.log")["run"]
    assert "testing" not in state and not (layer / "agents" / "testing").exists()
    lines = _summary(layer, "a0000008")
    assert _line(lines, "checks:") == "checks:    unit=0"
    assert not [line for line in lines if line.startswith("testing:")]
    assert all("depth" not in entry and "testing" not in entry for entry in state["check_results"])


def test_full_run_records_coverage_map(make_layer, tmp_path):
    counter = tmp_path / "ran.log"
    record = ("import json, pathlib; pathlib.Path('cov.json').write_text(json.dumps({'tests': "
              "{'tests/test_cart.py': ['shop/cart.py'], 'tests/test_price.py': ['shop/price.py', 'shop/cart.py']}}))")
    cmd = unit_cmd(counter, recordCoverage="al-json", coverageFile="cov.json",
                   select=["changed-tests", "imports", "coverage"],
                   coverageRun=f"echo coverage >> {counter}; python3 -c \"{record}\"")
    layer, app, _ = make_shop_layer(make_layer, tmp_path, cmd=cmd)
    head = git(app, "rev-parse", "main")
    # a full run on a committed tree (a batch on the default branch): the coverage form runs instead
    # of #:run, and its output becomes the map, keyed by that commit
    assert batch.run(layer, None, adw_id="c0000001") == 0
    assert ran(counter) == ["coverage"]
    data = coverage_map.load(ir.load(layer), "unit")
    assert data is not None and data["commit"] == head and data["recipe"] == "al-json"
    assert sorted(data["tests"]["tests/test_price.py"]) == ["shop/cart.py", "shop/price.py"]
    assert (layer / "agents" / "testing" / "coverage" / "unit.json").is_file()
