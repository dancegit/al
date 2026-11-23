"""The evidence pack (evidence.py): per Proof plan row, what proves it for the exact tree the
reviewers read; gap classification; the protected-path stop before a review; earlier review rounds
archived before a new review."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convergence_fixtures as cf  # noqa: E402

from adw_modules import affected, approvals, evidence, ir, proofplan  # noqa: E402
from adw_modules.errors import StepFailed  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

RUN = "e0001234"


def _stack(cwd: Path):
    return SimpleNamespace(podman_option=None, cwd=cwd, sandbox=None, read_only=False,
                           wrap=lambda cmd: ["bash", "-c", cmd], process_env=lambda: {"PATH": "/usr/bin:/bin"})


def _row(**fields):
    base = {"id": "C1", "criterion": "it works", "command": "unit", "tests": [], "writes": [], "proof": [],
            "needs": [], "state": "planned"}
    return {**base, **fields}


def _setup(make_layer, app_repo, rows, **options):
    layer = ir.load(cf.converge_layer(make_layer, **options))
    state = ADWState(adw_id=RUN, adw=cf.ADW, worktree=str(app_repo))
    state.proof_plan = {"table": "parsed", "rows": rows}
    return layer, state, _stack(app_repo)


def _passed(layer, state, stack, *, tree: str | None = None, passed: int = 1, skipped: int = 0,
            reasons=None, log_text: str = "tests/test_feature.py::test_feature PASSED\n"):
    log = Path(layer.layer_dir) / "agents" / RUN / "checks" / "unit.1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(log_text)
    state.checks["unit"] = {"cmd": "unit", "exit": 0, "outcome": "passed",
                            "log": log.relative_to(layer.layer_dir).as_posix(),
                            "tree": affected.tree(layer, state, stack) if tree is None else tree,
                            "skips": {"skipped": skipped, "passed": passed, "failed": 0, "reasons": reasons or [],
                                      "unexplained": 0, "source": "pytest"}}


def test_pack_row_proven_for_current_tree(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row(tests=["tests/test_feature.py::test_feature",
                                                                    "tests/test_other.py::test_gone"])])
    _passed(layer, state, stack)
    pack = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)
    (row,) = pack["rows"]
    assert (row["state"], row["tree"], row["exit"], row["check"], row["passed"]) == ("proven", "current", 0, "unit", 1)
    assert row["tests"] == {"tests/test_feature.py::test_feature": "named in log",
                            "tests/test_other.py::test_gone": "not shown"}
    assert state.proof_plan["rows"][0]["state"] == "proven"
    written = (Path(layer.layer_dir) / "artefacts" / RUN / "review-context" / "evidence.md").read_text()
    assert "| C1 | it works | proven | unit | 0 | current | 1 | 0 | 0 |" in written


def test_pack_row_stale_when_tree_changed(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row()])
    _passed(layer, state, stack, tree="0" * 40)
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert (row["state"], row["tree"]) == ("stale", "older")


def _carry(layer, state, stack, changed: list[str]) -> None:
    """A pass on tree 0…0 carried to the current tree over `changed`."""
    _passed(layer, state, stack, tree="0" * 40)
    state.checks["unit"]["carried"] = {"from_tree": "0" * 40, "tree": affected.tree(layer, state, stack),
                                       "changed": list(changed), "changed_total": len(changed),
                                       "log": state.checks["unit"]["log"]}


def test_carried_pass_proves_a_row_whose_files_did_not_change(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row(tests=["tests/test_feature.py::test_feature"])])
    _carry(layer, state, stack, ["README.md"])
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert (row["state"], row["tree"]) == ("proven", "carried")
    assert row["note"] == "carried from 00000000 (only README.md changed)"
    assert row["carried"]["from_tree"] == "0" * 40 and row["carried"]["changed"] == ["README.md"]
    assert row["log"] == state.checks["unit"]["log"]
    written = (Path(layer.layer_dir) / "artefacts" / RUN / "review-context" / "evidence.md").read_text()
    assert "carried from 00000000 (only README.md changed)" in written


def test_carried_pass_is_stale_when_a_file_the_row_names_changed(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row(tests=["tests/test_feature.py::test_feature"])])
    _carry(layer, state, stack, ["kit/x/tests/test_feature.py"])
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert (row["state"], row["tree"]) == ("stale", "older")
    assert row["note"] == "a file this criterion names changed: kit/x/tests/test_feature.py"
    layer, state, stack = _setup(make_layer, app_repo, [_row(writes=[{"glob": "out/*.txt", "count": 1}])])
    _carry(layer, state, stack, ["out/a.txt"])
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert row["state"] == "stale"
    assert row["note"] == "a file this criterion names changed: out/a.txt"


def test_carried_pass_ignores_a_changed_test_file_of_another_suite(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row(tests=["tests/test_feature.py::test_feature"])])
    _carry(layer, state, stack, ["tests/tools/test_own_layer.py", "kit/dashboard/tests/test_feature_view.py"])
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert (row["state"], row["tree"]) == ("proven", "carried")
    assert row["note"] == ("carried from 00000000 (only tests/tools/test_own_layer.py, "
                           "kit/dashboard/tests/test_feature_view.py changed)")


def test_force_stale_runs_each_check_once(make_layer, app_repo):
    unit = "echo 'tests/test_feature.py::test_feature PASSED' && echo '==== 1 passed in 0.01s ===='"
    layer, state, stack = _setup(make_layer, app_repo, [_row()], unit=unit)
    _passed(layer, state, stack, tree="0" * 40)
    adw = layer.adw(cf.ADW)
    pack = evidence.build_pack(layer, state, adw, stack)
    assert pack["rows"][0]["state"] == "stale"
    forced: set[str] = set()
    assert evidence._force_stale(layer, state, adw, stack, pack, forced) == ["unit"]
    record = state.checks["unit"]
    assert (record["outcome"], record["tree"]) == ("passed", affected.tree(layer, state, stack))
    assert evidence._force_stale(layer, state, adw, stack, pack, forced) == []
    assert evidence.build_pack(layer, state, adw, stack)["rows"][0]["state"] == "proven"


def test_pack_row_skipped_only(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row()])
    _passed(layer, state, stack, passed=0, skipped=3, reasons=[["chromium not installed", 3]])
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert row["state"] == "skipped-only"
    named = [_row(tests=["tests/test_page.py::test_shot"])]
    layer, state, stack = _setup(make_layer, app_repo, named)
    _passed(layer, state, stack, log_text="tests/test_page.py::test_shot SKIPPED (no chrome)\n")
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert row["state"] == "skipped-only" and row["tests"] == {"tests/test_page.py::test_shot": "skipped in log"}


def test_pack_row_files_missing(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row(proof=[{"glob": "report.json", "count": 1}])])
    _passed(layer, state, stack)
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert row["state"] == "files-missing" and row["note"] == "no file for proof:report.json"
    folder = proofplan.ensure_dir(state)   # the prove step made it, git-ignored
    (folder / "report.json").write_text("{}")
    evidence._collect(layer, state, layer.adw(cf.ADW))
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert row["state"] == "proven" and row["files"] == [f"artefacts/{RUN}/proof-file/report.json"]


def test_command_without_check_step_is_run_once_and_reused(make_layer, app_repo, tmp_path):
    counter = tmp_path / "runs.txt"
    layer_dir = cf.converge_layer(make_layer)
    data = json.loads((layer_dir / "agentic-layer.json").read_text())
    data["codebase"]["commands"].append({"name": "smoke", "run": f"echo run >> '{counter}' && echo '1 passed in 0.1s'",
                                         "timeout": 30})
    (layer_dir / "agentic-layer.json").write_text(json.dumps(data))
    layer = ir.load(layer_dir)
    state = ADWState(adw_id=RUN, adw=cf.ADW, worktree=str(app_repo))
    state.proof_plan = {"table": "parsed", "rows": [_row(command="smoke")]}
    stack = _stack(app_repo)
    first = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"][0]
    second = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"][0]
    assert counter.read_text() == "run\n"
    assert first["state"] == second["state"] == "proven" and first["check"] == "cmd smoke"
    assert [(e["phase"], e["outcome"]) for e in state.check_results] == [("evidence", "passed"), ("evidence", "reused")]
    assert second["log"] == first["log"] == f"agents/{RUN}/checks/evidence-smoke.log"


def test_two_rows_on_one_command_both_show_its_counts_and_its_log_keeps_them(make_layer, app_repo):
    layer_dir = cf.converge_layer(make_layer)
    data = json.loads((layer_dir / "agentic-layer.json").read_text())
    data["codebase"]["commands"].append({"name": "smoke",
                                         "run": "echo 'tests/t.py::test_a PASSED' && echo '1 passed in 0.1s'",
                                         "timeout": 30})
    (layer_dir / "agentic-layer.json").write_text(json.dumps(data))
    layer = ir.load(layer_dir)
    state = ADWState(adw_id=RUN, adw=cf.ADW, worktree=str(app_repo))
    state.proof_plan = {"table": "parsed", "rows": [_row(id="C1", command="smoke", tests=["tests/t.py::test_a"]),
                                                    _row(id="C2", command="smoke", tests=["tests/t.py::test_a"])]}
    rows = evidence.build_pack(layer, state, layer.adw(cf.ADW), _stack(app_repo))["rows"]
    assert [row["passed"] for row in rows] == [1, 1]
    assert all(row["tests"] == {"tests/t.py::test_a": "named in log"} for row in rows)
    assert [(e["phase"], e["outcome"]) for e in state.check_results] == [("evidence", "passed"), ("evidence", "reused")]
    log = (Path(layer.layer_dir) / "agents" / RUN / "checks" / "evidence-smoke.log").read_text()
    assert "1 passed in 0.1s" in log and "not run" not in log


def test_narrowed_row_with_skipped_or_missing_results_is_not_proven(make_layer, app_repo, tmp_path):
    test_id = "tests/test_feature.py::test_feature"
    fake = tmp_path / "bin" / "pytest"
    fake.parent.mkdir()
    fake.write_text('echo "SKIPPED [1] tests/test_feature.py:3: REQUIRES HUMAN: not yet"\n'
                    'echo "1 skipped in 0.01s"\nexit 0\n')
    layer, state, stack = _setup(make_layer, app_repo, [_row(command=f"sh {fake} -q -rA {test_id}", tests=[test_id])],
                                 unit=f"sh {fake} -q")
    log = Path(layer.layer_dir) / "agents" / RUN / "checks" / "evidence-unit-C1.log"
    for _ in range(2):   # the second pack reuses the first one's run on the same tree
        (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
        assert (row["state"], row["skipped"], row["passed"]) == ("skipped-only", 1, 0)
        assert "1 skipped" in log.read_text()

    silent = tmp_path / "silent" / "pytest"
    silent.parent.mkdir()
    silent.write_text('echo "no tests ran in 0.01s"\nexit 0\n')
    layer, state, stack = _setup(make_layer, app_repo, [_row(command=f"sh {silent} -q -rA {test_id}", tests=[test_id])],
                                 unit=f"sh {silent} -q")
    (row,) = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert row["state"] == "not-run"
    assert row["tests"] == {test_id: "not shown"}
    assert test_id in row["note"]


def test_narrowed_row_is_proven_by_its_own_run(make_layer, app_repo, tmp_path):
    fake = tmp_path / "bin" / "pytest"
    fake.parent.mkdir()
    fake.write_text('for arg in "$@"; do case "$arg" in -*) ;; *) echo "PASSED $arg" ;; esac; done\n'
                    'echo "1 passed in 0.01s"\nexit 0\n')
    test_id = "tests/test_feature.py::test_feature"
    rows = [_row(command=f"sh {fake} -q -rA {test_id}", tests=[test_id]),
            _row(id="C2", command="unit", tests=[test_id])]
    layer, state, stack = _setup(make_layer, app_repo, rows, unit=f"sh {fake} -q")
    _passed(layer, state, stack, log_text=".\n1 passed in 0.01s\n")
    narrowed, verbatim = evidence.build_pack(layer, state, layer.adw(cf.ADW), stack)["rows"]
    assert narrowed["tests"] == {test_id: "named in log"}
    assert narrowed["check"] == "narrowed unit"
    assert narrowed["log"] == f"agents/{RUN}/checks/evidence-unit-C1.log"
    assert narrowed["state"] == "proven" and narrowed["passed"] == 1
    assert verbatim["tests"] == {test_id: "not shown"} and verbatim["check"] == "unit"


def test_gap_classification_env_words_and_declarations(make_layer, app_repo):
    layer = ir.load(cf.converge_layer(make_layer))
    adw = layer.adw(cf.ADW)
    assert evidence.classify_gap({"state": "skipped-only", "reasons": [["chromium not installed", 2]]}, layer, adw) \
        == "needs-environment"
    assert evidence.classify_gap({"state": "skipped-only", "reasons": [["flaky, skipped for now", 2]]}, layer, adw) \
        == "fixable"
    assert evidence.classify_gap({"state": "skipped-only", "reasons": [["no podman on this host", 1]]}, layer, adw) \
        == "needs-environment"
    assert evidence.classify_gap({"state": "files-missing", "empty": ["out/report.html"]}, layer, adw) \
        == "needs-declaration"
    assert evidence.classify_gap({"state": "files-missing", "empty": ["proof:report.json"]}, layer, adw) == "fixable"
    for state in ("failed", "stale", "missing", "not-run"):
        assert evidence.classify_gap({"state": state}, layer, adw) == "fixable"


def test_protected_change_without_approval_stops_needs_person(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row()], protected=["docs"])
    _passed(layer, state, stack)
    (app_repo / "docs").mkdir()
    (app_repo / "docs" / "DESIGN.md").write_text("changed\n")
    step = {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"}
    with pytest.raises(StepFailed) as stop:
        evidence.run(layer, state, layer.adw(cf.ADW), step, stack)
    assert stop.value.reason.startswith("evidence:needs-person: the run changed 1 protected path(s) without an "
                                        "approval: docs/DESIGN.md; party: approver")
    stopped = state.proof_plan["stopped"]
    assert (stopped["at"], stopped["party"], stopped["approvals"]) == ("evidence", "approver", ["edit:docs/DESIGN.md"])
    approvals.record(state, ["edit:docs/*"], "ana", "start")
    _passed(layer, state, stack)
    pack = evidence.run(layer, state, layer.adw(cf.ADW), step, stack)
    assert [row["state"] for row in pack["rows"]] == ["proven"] and "stopped" not in state.proof_plan


def test_old_review_rounds_archived_before_a_new_review(make_layer, app_repo):
    layer, state, stack = _setup(make_layer, app_repo, [_row()])
    _passed(layer, state, stack)
    earlier = {"verdict": {"success": False, "summary": "no", "issues": []}, "rounds": [{"round": 1}], "mode": "auto"}
    state.review = dict(earlier)
    evidence.run(layer, state, layer.adw(cf.ADW), {"name": "review", "kind": "review"}, stack)
    assert state.review == {}
    assert state.proof_plan["earlier_review"] == [earlier]
    assert subprocess.run(["git", "-C", str(app_repo), "status", "--porcelain"], capture_output=True, text=True,
                          check=True).stdout == ""
