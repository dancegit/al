"""The coverage map (adw_modules/coverage_map.py): the three recipes, the one map format, a lost
map, and the rule that a file changed since the map's commit is unknown to it."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from adw_modules import affected, coverage_map, selection, testing
from adw_modules.ir import load
from adw_modules.state import ADWState

DATA = Path(__file__).resolve().parent / "data" / "selection"
ADW_ID = "c0000001"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def _python_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(DATA / "python", repo, ignore=shutil.ignore_patterns("conftest.py"))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _cmd(**over) -> dict:
    return {"name": "unit", "run": "pytest -q", "covers": ["pkg", "tests"], "always": False, "reuse": True,
            "targeted": "pytest -q {tests}", "select": ["coverage"], "recordCoverage": "al-json",
            "coverageRun": "pytest -q --al-coverage", "coverageFile": "coverage.json", **over}


def _layer(make_layer, repo: Path, **testing_over):
    return load(make_layer({
        "codebase": {"root": str(repo), "commands": [_cmd()]},
        "adws": [{"name": "demo", "agent": "mock-agent", "review": "auto", "environment": ["worktree"],
                  "steps": [{"name": "unit", "kind": "check", "cmd": "unit"}]}],
        "testing": {"depth": "targeted", "escalateOn": ["unmapped"], "escalateDiffLines": 0, "escalateFiles": 0,
                    **testing_over}}))


# ---------------------------------------------------------------- recipes

def test_coverage_py_contexts_to_test_files(tmp_path):
    report = {"meta": {"show_contexts": True}, "files": {
        "pkg/core.py": {"contexts": {"1": ["tests/test_core.py::test_total|run", ""],
                                     "2": ["tests/test_util.py::test_average|run"]}},
        str(tmp_path / "pkg" / "util.py"): {"contexts": {"3": ["tests/test_util.py::test_average|setup"]}}}}
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert coverage_map.read_recipe("coverage-py", path, tmp_path) == {
        "tests/test_core.py": ["pkg/core.py"], "tests/test_util.py": ["pkg/core.py", "pkg/util.py"]}


def test_profile_dir_lcov_and_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example.org/shop\n\ngo 1.22\n", encoding="utf-8")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "tests__test_core.py.info").write_text(
        f"TN:\nSF:{tmp_path}/pkg/core.py\nDA:1,1\nend_of_record\nSF:pkg/util.py\nDA:1,0\nend_of_record\n",
        encoding="utf-8")
    (profiles / "cart__TestTotal.out").write_text(
        "mode: set\nexample.org/shop/cart/cart.go:4.33,6.2 1 1\nexample.org/shop/price/price.go:1.1,2.2 1 0\n",
        encoding="utf-8")
    (profiles / "notes.txt").write_text("not a profile\n", encoding="utf-8")
    assert coverage_map.read_recipe("profile-dir", profiles, tmp_path) == {
        "tests/test_core.py": ["pkg/core.py"], "cart/TestTotal": ["cart/cart.go"]}


def test_al_json(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"tests": {"tests/test_core.py": ["pkg/core.py"]}}), encoding="utf-8")
    assert coverage_map.read_recipe("al-json", path, tmp_path) == {"tests/test_core.py": ["pkg/core.py"]}
    notes: list[str] = []
    assert coverage_map.read_recipe("al-json", tmp_path / "missing.json", tmp_path, notes=notes) == {}
    assert notes and "missing.json" in notes[0]
    path.write_text("{not json", encoding="utf-8")
    assert coverage_map.read_recipe("coverage-py", path, tmp_path) == {}


def test_save_and_load_round_trip(tmp_path, make_layer):
    layer = _layer(make_layer, _python_repo(tmp_path))
    coverage_map.save(layer, "unit", "abc123", "al-json", {"tests/test_core.py": ["pkg/core.py"]})
    data = coverage_map.load(layer, "unit")
    assert data["version"] == 1 and data["cmd"] == "unit" and data["commit"] == "abc123"
    assert data["recipe"] == "al-json" and data["tests"] == {"tests/test_core.py": ["pkg/core.py"]}
    assert (Path(layer.layer_dir) / "agents" / "testing" / "coverage" / "unit.json").is_file()


# ---------------------------------------------------------------- a lost or stale map

def test_lost_map_is_none_and_notes(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo)
    assert coverage_map.load(layer, "unit") is None
    path = Path(layer.layer_dir) / "agents" / "testing" / "coverage" / "unit.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert coverage_map.load(layer, "unit") is None
    result = selection.select(layer, _cmd(), ["pkg/core.py"], root=repo)
    assert result.tests == [] and result.unmapped == ["pkg/core.py"]
    assert any("no coverage map for unit yet" in note for note in result.notes)


def test_stale_paths_since_recorded_commit_are_unknown(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    recorded = _git(repo, "rev-parse", "HEAD")
    layer = _layer(make_layer, repo)
    coverage_map.save(layer, "unit", recorded, "al-json",
                      {"tests/test_core.py": ["pkg/core.py"], "tests/test_util.py": ["pkg/util.py", "pkg/core.py"]})
    (repo / "pkg" / "core.py").write_text("def total(prices):\n    return sum(prices) + 0\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "core changed after the map was recorded")
    # the run's own change, not committed yet
    (repo / "pkg" / "core.py").write_text("def total(prices):\n    return sum(list(prices))\n", encoding="utf-8")
    (repo / "pkg" / "util.py").write_text("from pkg.core import total\n\n\ndef average(p):\n    return 0\n",
                                          encoding="utf-8")
    state, env = ADWState(adw_id=ADW_ID, adw="demo", worktree=str(repo)), SimpleNamespace(sandbox=None)
    data = coverage_map.load(layer, "unit")
    assert coverage_map.stale_paths(layer, state, env, data, ["pkg/core.py", "pkg/util.py"]) == {"pkg/core.py"}

    found = selection.coverage(["pkg/core.py", "pkg/util.py"], _cmd(), layer, repo, state=state, env_stack=env)
    assert list(found.tests) == ["tests/test_util.py"] and found.tests["tests/test_util.py"] == {"pkg/util.py"}

    tree = affected.tree(layer, state, env)
    plan = testing.plan_check(layer, state, env, {"name": "unit", "kind": "check", "cmd": "unit"}, "check",
                              affected.base(layer, state, env), tree)
    assert plan.depth == "full" and plan.asked == "targeted"
    assert "pkg/core.py" in plan.why and plan.command["run"] == "pytest -q"


def test_unknown_commit_makes_every_path_stale(tmp_path, make_layer):
    repo = _python_repo(tmp_path)
    layer = _layer(make_layer, repo)
    state, env = ADWState(adw_id=ADW_ID, adw="demo", worktree=str(repo)), SimpleNamespace(sandbox=None)
    data = {"version": 1, "cmd": "unit", "commit": "0" * 40, "recipe": "al-json", "tests": {}}
    assert coverage_map.stale_paths(layer, state, env, data, ["pkg/core.py", "pkg/util.py"]) == {
        "pkg/core.py", "pkg/util.py"}
    assert coverage_map.stale_paths(layer, state, env, {**data, "commit": ""}, ["pkg/core.py"]) == {"pkg/core.py"}
