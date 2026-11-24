"""Skipped tests: the parsers on recorded logs, the skip rules, and the rules through a real run."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import artefacts, runner, skips

DATA = Path(__file__).resolve().parent / "data" / "skips"
REASONS = [["no podman", 2], ["needs network", 1]]


def _log(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def test_pytest_counts_and_reasons_from_rs():
    report = skips.parse_pytest(_log("pytest-rs.log"))
    assert (report.skipped, report.passed, report.failed, report.unexplained) == (3, 1, 0, 0)
    assert report.top() == REASONS  # `test_engine.py:3:` is not part of the reason
    assert report.as_dict()["source"] == "pytest"


def test_pytest_without_rs_counts_unexplained():
    report = skips.parse_pytest(_log("pytest-plain.log"))
    assert (report.skipped, report.passed, report.unexplained) == (3, 1, 3)
    assert report.top() == [[skips.PYTEST_NO_RS, 3]]


def test_pytest_quiet_summary_is_recognised():
    report = skips.parse_pytest("SKIPPED [2] t.py:3: no podman\n1 passed, 2 skipped in 0.01s\n")
    assert (report.skipped, report.passed, report.top()) == (2, 1, [["no podman", 2]])


def test_go_text_and_json_give_the_same_report():
    text, as_json = skips.parse_go(_log("go-v.log")), skips.parse_go(_log("go-json.log"))
    for report in (text, as_json):
        assert (report.skipped, report.passed, report.failed, report.unexplained) == (3, 1, 0, 0)
        assert report.top() == REASONS
    assert text.as_dict() == as_json.as_dict()


def test_go_reason_printed_after_the_skip_line_counts_too():
    report = skips.parse_go("=== RUN   TestX\n--- SKIP: TestX (0.00s)\n    x_test.go:4: no podman\nok  \tp\t0.1s\n")
    assert report.top() == [["no podman", 1]]


def test_go_without_v_is_recognised_but_blind():
    report = skips.parse_go("ok  \texample.com/proj\t0.002s\nok  \texample.com/proj/sub\t(cached)\n")
    assert report is not None and report.skipped == 0 and report.sources == [skips.GO_NO_V]
    assert skips.violations({"name": "t"}, report) == []
    problems = skips.violations({"name": "t", "maxSkipped": 0, "forbidSkipReason": "podman"}, report)
    assert len(problems) == 2 and all("go test" in p and "-v" in p for p in problems)


def test_cargo_ignored_with_and_without_reason():
    report = skips.parse_cargo(_log("cargo.log"))
    assert (report.skipped, report.passed, report.unexplained) == (3, 1, 0)
    assert report.top() == REASONS
    bare = skips.parse_cargo("test a ... ignored\ntest b ... ignored, no podman\n"
                             "test result: ok. 0 passed; 0 failed; 2 ignored; 0 measured; 0 filtered out\n")
    assert (bare.skipped, bare.unexplained) == (2, 1)
    assert bare.top() == [[skips.NO_REASON, 1], ["no podman", 1]]


def test_mocha_pending_counts_without_reasons():
    report = skips.parse_mocha(_log("mocha.log"))
    assert (report.skipped, report.passed, report.unexplained) == (3, 1, 3)
    assert report.top() == [[skips.MOCHA_PENDING, 3]]


def test_bun_counts_pass_skip_and_fail():
    text = (DATA.parent / "bun.log").read_text(encoding="utf-8")
    report = skips.parse_bun(text)
    assert (report.passed, report.skipped, report.failed, report.unexplained) == (2, 1, 0, 1)
    assert report.top() == [[skips.BUN_SKIP, 1]]
    assert report.sources == ["bun"]
    assert skips.from_log(text).as_dict() == report.as_dict()
    assert skips.parse_bun(_log("mocha.log")) is None
    assert skips.parse_bun(_log("pytest-rs.log")) is None
    assert skips.parse_mocha(text) is None and skips.parse_pytest(text) is None


def test_junit_skipped_messages(tmp_path):
    report = skips.parse_junit([DATA / "junit.xml"])
    assert (report.skipped, report.passed, report.failed, report.unexplained) == (3, 1, 0, 0)
    assert report.top() == REASONS
    bare = tmp_path / "bare.xml"
    bare.write_text('<testsuite><testcase name="a"><skipped/></testcase><testcase name="b"><failure/></testcase>'
                    '<testcase name="c"><skipped>t.py:1: needs network</skipped></testcase></testsuite>')
    report = skips.parse_junit([bare])
    assert (report.skipped, report.failed, report.unexplained) == (2, 1, 1)
    assert report.top() == [[skips.NO_REASON, 1], ["needs network", 1]]


def test_mixed_log_sums_runners():
    report = skips.from_log(_log("make-mixed.log"))
    assert (report.skipped, report.passed, report.unexplained) == (6, 2, 0)
    assert report.top() == [["no podman", 4], ["needs network", 2]]
    assert report.as_dict()["source"] == "pytest+go"


def test_unrecognised_output_is_none():
    assert skips.from_log(_log("none.log")) is None
    assert skips.from_log("") is None


def _junit_layer(worktree: Path):
    option = {"id": "junit", "kind": "junit", "glob": "reports/*.xml"}
    return (SimpleNamespace(option=lambda family, ident: option),
            SimpleNamespace(worktree=str(worktree)), {"name": "test", "collect": ["junit"]})


def test_junit_older_than_the_attempt_is_ignored(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / "reports").mkdir(parents=True)
    report_file = worktree / "reports" / "unit.xml"
    report_file.write_text(_log("junit.xml"))
    log = tmp_path / "test.1.log"
    log.write_text("1 passed in 0.01s\n")
    layer, state, step = _junit_layer(worktree)
    started = time.time()
    os.utime(report_file, (started - 60, started - 60))
    stale = skips.account(layer, state, step, log, started)
    assert (stale.skipped, stale.sources) == (0, ["pytest"])  # fell back to the log
    os.utime(report_file, (started + 1, started + 1))
    fresh = skips.account(layer, state, step, log, started)
    assert (fresh.skipped, fresh.sources) == (3, ["junit:unit.xml"])


@pytest.mark.parametrize("limit, broken", [(-1, False), (0, True), (2, True), (3, False), (10, False)])
def test_max_skipped_violation_and_limit_respected(limit, broken):
    report = skips.parse_pytest(_log("pytest-rs.log"))
    problems = skips.violations({"name": "test", "maxSkipped": limit}, report)
    assert bool(problems) is broken
    if broken:
        assert problems == [f"al: check test fails: 3 tests skipped, more than #:max-skipped {limit} "
                            "(top reasons: no podman ×2, needs network ×1)"]


def test_forbid_reason_matches_case_insensitively():
    report = skips.parse_pytest(_log("pytest-rs.log"))
    problems = skips.violations({"name": "test", "forbidSkipReason": "NO PODMAN"}, report)
    assert problems == ['al: check test fails: 2 tests skipped because "no podman", '
                        'forbidden by #:forbid-skip-reason "NO PODMAN"']
    assert skips.violations({"name": "test", "forbidSkipReason": "gpu"}, report) == []


def test_rules_fail_closed_without_a_report():
    for step, flag in (({"name": "t", "maxSkipped": 0}, "#:max-skipped 0"),
                       ({"name": "t", "forbidSkipReason": "podman"}, '#:forbid-skip-reason "podman"')):
        (problem,) = skips.violations(step, None)
        assert problem.startswith("al: check t fails: no test report recognised in the output")
        assert flag in problem and "#:collect a junit report" in problem


def test_forbid_with_unexplained_skips_fails_closed():
    report = skips.parse_pytest(_log("pytest-plain.log"))
    (problem,) = skips.violations({"name": "t", "forbidSkipReason": "podman"}, report)
    assert problem.startswith("al: check t fails: 3 skipped tests show no reason")
    assert "pytest with -rs" in problem
    assert skips.violations({"name": "t", "maxSkipped": 3}, report) == []  # the count is still known


def test_no_rules_never_violate():
    assert skips.violations({"name": "t"}, None) == []
    assert skips.violations({"name": "t", "maxSkipped": -1, "forbidSkipReason": ""}, skips.from_log(_log("make-mixed.log"))) == []


def test_summary_text():
    record = skips.parse_pytest(_log("pytest-rs.log")).as_dict()
    assert skips.summary_text(record) == "3 skipped: no podman ×2, needs network ×1"
    assert skips.summary_text({"skipped": 0, "reasons": []}) == ""
    assert skips.summary_text(None) == ""
    assert skips.summary_text({"skipped": 2, "reasons": []}) == "2 skipped"


def test_normalise_reason():
    assert skips.normalise_reason("pkg/x_test.go:12:   no\n  podman ") == "no podman"
    assert skips.normalise_reason("Skipped: needs network") == "needs network"
    assert skips.normalise_reason("") == skips.NO_REASON
    assert len(skips.normalise_reason("x" * 500)) == 120


# ------------------------------------------------------------------------------------- through a run

def _layer(make_layer, *, run: str, check: dict | None = None, review: dict | None = None, steps=None,
           cmd: dict | None = None) -> Path:
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "build", "template": "build", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "test", "run": run, "timeout": 10, **(cmd or {})}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore", "bug"],
                     "blockProtectedPaths": True, "maxDiffLines": 100, "requireChecks": ["test"], "ship": "merge",
                     **(review or {})}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 26400, "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
                  "classes": ["chore", "bug"], "steps": steps or [
                      {"name": "build", "kind": "run", "command": "build"},
                      {"name": "test", "kind": "check", "cmd": "test", "resolve": "", "retries": 0,
                       "collect": [], "in": "", "maxSkipped": -1, "forbidSkipReason": "", **(check or {})}]}],
    })


def _state(layer: Path, adw_id: str) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _events(layer: Path, adw_id: str) -> list[dict]:
    path = layer / "agents" / adw_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_forbid_skip_reason_fails_the_check_and_the_gate(make_layer):
    layer = _layer(make_layer, run=f"cat {DATA / 'pytest-rs.log'}; exit 0", check={"forbidSkipReason": "no podman"})
    assert runner.main("demo", layer, ["Run the tests", "--adw-id", "5c1b0001"]) == 1
    state = _state(layer, "5c1b0001")
    assert state["status"] == "failed"
    assert next(s for s in state["steps"] if s["name"] == "test")["status"] == "failed"
    record = state["checks"]["test"]
    assert record["exit"] == 1
    assert record["skips"]["skipped"] == 3 and record["skips"]["reasons"] == REASONS
    assert any('"no podman"' in line for line in record["skip_rule"])
    log = (layer / record["log"]).read_text().rstrip().splitlines()
    assert log[-1].startswith("al: check test fails: 2 tests skipped because \"no podman\"")
    (end,) = [e for e in _events(layer, "5c1b0001") if e.get("type") == "check.end"]
    assert end["skipped"] == 3 and end["skip_reasons"] == REASONS and end["exit"] == 1
    assert end["skip_source"] == "pytest" and "in" not in end


def test_skip_rule_applies_to_the_gate_rerun(make_layer, tmp_path):
    clean = tmp_path / "clean.log"
    clean.write_text("SKIPPED [1] t.py:3: needs network\n1 passed, 1 skipped in 0.01s\n")
    marker = tmp_path / "ran-once"
    # the check step sees a clean report; every later run (the gate's re-run) a forbidden one
    run = (f"if [ -e {marker} ]; then cat {DATA / 'pytest-rs.log'}; else touch {marker}; cat {clean}; fi")
    steps = [{"name": "build", "kind": "run", "command": "build"},
             {"name": "test", "kind": "check", "cmd": "test", "resolve": "", "retries": 0, "collect": [],
              "in": "", "maxSkipped": -1, "forbidSkipReason": "no podman"},
             {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]
    # the command answers differently each time it runs, so its pass must not be reused: #:reuse #f
    layer = _layer(make_layer, run=run, steps=steps, cmd={"reuse": False})
    runner.main("demo", layer, ["Ship it", "--adw-id", "5c1b0002"])
    state = _state(layer, "5c1b0002")
    assert state["checks"]["test"]["exit"] == 0
    gate = state["ship"]["gate"]
    assert gate["ship"] is False and "require-check-failed:test" in gate["reasons"]
    gate_log = (layer / "agents" / "5c1b0002" / "checks" / "gate-test.log").read_text()
    assert "forbidden by #:forbid-skip-reason \"no podman\"" in gate_log
    gate_end = [e for e in _events(layer, "5c1b0002") if e.get("type") == "check.end" and e.get("phase") == "gate"]
    assert gate_end and gate_end[-1]["exit"] == 1 and gate_end[-1]["skipped"] == 3


def test_reviewers_manifest_lists_skips(make_layer):
    layer = _layer(make_layer, run=f"cat {DATA / 'pytest-rs.log'}")
    runner.main("demo", layer, ["Run the tests", "--adw-id", "5c1b0003"])
    state = _state(layer, "5c1b0003")
    manifest = json.loads(artefacts.manifest_path(runner_layer(layer), "5c1b0003").read_text())
    (entry,) = [item for item in manifest if item.get("type") == "skips"]
    assert entry == {"id": "skips", "type": "skips", "step": "test", "path": state["checks"]["test"]["log"],
                     "skipped": 3, "reasons": REASONS, "unexplained": 0, "rule": []}
    assert not any(item.get("type") == "skips" for item in state["artefacts"])


def runner_layer(layer_dir: Path):
    from adw_modules import ir
    return ir.load(layer_dir)


def test_summary_shows_skips_next_to_the_exit(make_layer):
    layer = _layer(make_layer, run=f"cat {DATA / 'pytest-rs.log'}")
    runner.main("demo", layer, ["Run the tests", "--adw-id", "5c1b0004"])
    from adw_modules import state as state_module
    lines = runner.summary_lines(runner_layer(layer), state_module.load(layer, "5c1b0004"))
    assert any("test=0 (3 skipped: no podman ×2, needs network ×1)" in line for line in lines)
