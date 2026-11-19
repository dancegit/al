"""`al runs LAYER` (what needs you; --view, --json), `al settle` and `al runs LAYER prune` on the scenario
of kit/dashboard/tests/settle_fixtures.py: an invented orchard with real git history and a layer whose
runs were merged, squash-merged, integrated by another run, left open, or failed and started again."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import KIT, al

sys.path.insert(0, str(KIT / "dashboard" / "tests"))
import settle_fixtures as sf  # noqa: E402

LINE = re.compile(r"^([0-9a-f]{8})  (\S+)\s")


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("al-runs") / "s"
    sf.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path) -> dict[str, Path]:
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return {"root": root, "repo": root / "orchard", "layer": root / "orchard.al"}


def ids_in(text: str) -> list[str]:
    return [m.group(1) for m in (LINE.match(line) for line in text.splitlines()) if m]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_al_runs_default_lists_what_needs_you(scenario):
    cp = al("runs", scenario["layer"])
    lines = cp.stdout.splitlines()
    assert lines[0] == "6 need you (5 running, 8 landed, 7 settled; --view all for every run)"
    assert sorted(ids_in(cp.stdout)) == sorted(sf.OPEN + [sf.WAITING, sf.OLD_FAILURE])
    runs = [line for line in lines[1:] if LINE.match(line)]
    nexts = [line for line in lines[1:] if line.lstrip().startswith("→ ")]
    assert len(runs) == len(nexts) == 6   # each run with its next action
    for run, line in zip(runs, nexts):    # and each can be given to an agent
        assert line.endswith(f"; or: al assign {scenario['layer']} {run.split()[0]}"), line
    text = cp.stdout
    assert "review PR #7" in text and "waiting at approve" in text and "review-rejected" in text
    assert f"--replaces {sf.OLD_FAILURE}" in text and f"al settle {scenario['layer']} {sf.OPEN[0]} --as" in text
    assert "build: the map renderer needs a page size" in text


def test_al_runs_views_and_json(scenario):
    landed = al("runs", scenario["layer"], "--view", "landed").stdout
    assert landed.splitlines()[0] == "8 landed (6 need you, 5 running, 8 landed, 7 settled)"
    assert sorted(ids_in(landed)) == sorted(sf.MERGED + sf.SQUASHED)
    for line in landed.splitlines()[1:]:
        assert re.search(r"landed in [0-9a-f]{7} \((ancestor|patch-id)\) on main, \d{4}-\d\d-\d\d$", line), line
    settled = al("runs", scenario["layer"], "--view", "settled").stdout
    assert sorted(ids_in(settled)) == sorted(sf.INTEGRATED + sf.FAILED)
    assert settled.count(f"superseded by {sf.INTEGRATION}") == 2
    for old, new in zip(sf.FAILED, sf.RESTARTS):
        assert re.search(rf"^{old} .*replaced by {new} \(running\)$", settled, re.MULTILINE)
    rows = json.loads(al("runs", scenario["layer"], "--view", "all", "--json").stdout)
    assert len(rows) == 27
    assert all({"bucket", "landing", "settled"} <= set(row) for row in rows)
    by_id = {row["id"]: row for row in rows}
    assert by_id[sf.MERGED[0]]["bucket"] == "landed" and by_id[sf.MERGED[0]]["landing"]["how"] == "ancestor"
    assert by_id[sf.FAILED[0]]["replaced_by"] == sf.RESTARTS[0]
    assert by_id[sf.INTEGRATED[0]]["superseded_by"] == sf.INTEGRATION
    assert json.loads(al("runs", scenario["layer"], "--json").stdout).__len__() == 6


def test_al_runs_groups_failures(tmp_path):
    sc = sf.build_group_scenario(tmp_path / "g")
    out = al("runs", sc["layer"]).stdout
    lines = out.splitlines()
    assert lines[0].startswith("5 need you (")
    at = lines.index("5 runs: preflight, same reason")
    assert lines[at + 1].split() == sf.FAILED   # in the order they failed
    assert ids_in(out) == []   # the five show once, as their group
    assert "preflight failed: the sandbox image" in lines[at + 2]


def test_al_settle_writes_record_and_list_moves_it(scenario):
    run = sf.OPEN[0]
    cp = al("settle", scenario["layer"], run, "--as", "abandoned", "--note", "the orchard map replaces it", "--who", "Ada")
    assert cp.stdout.strip() == f"settled {run} as abandoned by Ada: agents/{run}/settlement.json"
    record = json.loads((scenario["layer"] / "agents" / run / "settlement.json").read_text())
    assert (record["as"], record["note"], record["who"], record["via"]) == (
        "abandoned", "the orchard map replaces it", "Ada", "cli")
    listed = al("runs", scenario["layer"]).stdout
    assert listed.splitlines()[0] == "5 need you (5 running, 8 landed, 8 settled; --view all for every run)"
    assert run not in ids_in(listed)
    settled = al("runs", scenario["layer"], "--view", "settled").stdout
    assert re.search(rf"^{run} .*abandoned by Ada: the orchard map replaces it$", settled, re.MULTILINE)
    tip = git(scenario["repo"], "rev-parse", "main")
    cp = al("settle", scenario["layer"], sf.OPEN[1], "--as", "landed", "--by", tip[:9])
    assert f"settled {sf.OPEN[1]} as landed ({tip[:9]})" in cp.stdout


def test_al_settle_refuses_running_unknown_by_and_bad_as(scenario):
    running = al("settle", scenario["layer"], sf.RESTARTS[0], "--as", "abandoned", ok=False)
    assert running.returncode == 3 and "running" in running.stderr
    unknown = al("settle", scenario["layer"], sf.OPEN[0], "--as", "landed", "--by", "deadbeefdeadbeef", ok=False)
    assert unknown.returncode == 3 and "neither a run" in unknown.stderr
    bad = al("settle", scenario["layer"], sf.OPEN[0], "--as", "merged", ok=False)
    assert bad.returncode == 2
    missing = al("settle", scenario["layer"], "0badc0de", "--as", "abandoned", ok=False)
    assert missing.returncode == 4
    long_note = al("settle", scenario["layer"], sf.OPEN[0], "--as", "abandoned", "--note", "x" * 501, ok=False)
    assert long_note.returncode == 2
    assert not list((scenario["layer"] / "agents").glob("*/settlement.json"))


def _leftovers(scenario: dict[str, Path]) -> dict[str, str]:
    """A worktree and artefacts for a landed run; another landed run's branch checked out elsewhere."""
    layer, repo = scenario["layer"], scenario["repo"]
    git(repo, "worktree", "add", "-q", str(layer / "trees" / sf.MERGED[0]), f"al/{sf.MERGED[0]}")
    (layer / "artefacts" / sf.MERGED[0]).mkdir(parents=True)
    (layer / "artefacts" / sf.MERGED[0] / "proof.txt").write_text("the apple trees are tracked\n")
    other = scenario["root"] / "elsewhere"
    git(repo, "worktree", "add", "-q", str(other), f"al/{sf.MERGED[1]}")
    return {"other": str(other)}


def _snapshot(scenario: dict[str, Path]) -> tuple:
    layer, repo = scenario["layer"], scenario["repo"]
    return (git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
            git(repo, "worktree", "list", "--porcelain"),
            sorted(str(p.relative_to(layer)) for p in layer.rglob("*") if "settlement" in p.name or p.parent.name == "trees"))


def test_al_runs_prune_dry_run_changes_nothing(scenario):
    _leftovers(scenario)
    before = _snapshot(scenario)
    cp = al("runs", scenario["layer"], "prune", "--landed", "--older-than", "1", "--dry-run")
    assert _snapshot(scenario) == before
    assert f"would remove worktree trees/{sf.MERGED[0]}" in cp.stdout
    assert f"would remove branch al/{sf.MERGED[0]} (was " in cp.stdout
    assert f"{sf.MERGED[1]}: kept branch al/{sf.MERGED[1]} (checked out in " in cp.stdout
    assert cp.stdout.strip().splitlines()[-1].startswith("would prune ")
    assert not re.search(r"^removed ", cp.stdout, re.MULTILINE)


def test_al_runs_prune_removes_leftovers_keeps_records(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    where = _leftovers(scenario)
    tip = git(repo, "rev-parse", f"al/{sf.MERGED[0]}")
    cp = al("runs", layer, "prune", "--landed", "--older-than", "1")
    assert f"removed worktree trees/{sf.MERGED[0]}" in cp.stdout
    assert f"removed branch al/{sf.MERGED[0]} (was {tip[:7]})" in cp.stdout
    assert not (layer / "trees" / sf.MERGED[0]).exists()
    heads = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert f"al/{sf.MERGED[0]}" not in heads
    assert "main" in heads and f"al/{sf.MERGED[1]}" in heads   # checked out in another worktree: kept
    assert all(f"al/{run}" in heads for run in sf.OPEN)          # open runs are untouched
    assert (layer / "agents" / sf.MERGED[0] / "state.json").is_file()
    assert (layer / "artefacts" / sf.MERGED[0] / "proof.txt").is_file()
    record = json.loads((layer / "agents" / sf.MERGED[0] / "settlement.json").read_text())
    assert record["as"] == "landed" and record["pruned"]["branch"] == f"al/{sf.MERGED[0]}"
    assert record["pruned"]["tip"] == tip and record["pruned"]["worktree"] == f"trees/{sf.MERGED[0]}"
    assert Path(where["other"]).is_dir()
    assert not any((layer / "agents" / run / "settlement.json").exists() for run in sf.OPEN)
    again = al("runs", layer, "prune", "--landed", "--older-than", "1")   # pruned runs are not pruned twice
    assert f"branch al/{sf.MERGED[0]}" not in again.stdout
    assert al("runs", layer).stdout.splitlines()[0].startswith("6 need you (")


def test_al_runs_prune_needs_landed_flag(scenario):
    cp = al("runs", scenario["layer"], "prune", "--older-than", "1", ok=False)
    assert cp.returncode == 2 and "--landed" in cp.stderr
    cp = al("runs", scenario["layer"], "prune", "--landed", ok=False)
    assert cp.returncode == 2
