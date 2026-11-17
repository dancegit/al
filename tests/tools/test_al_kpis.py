"""`al kpis LAYER [--since DATE] [--json|--md]` on the scenario of kit/dashboard/tests/settle_fixtures.py
(an invented orchard with real git history and 27 runs) and on a small layer written for the purpose:
runs by outcome and why, attempts per step, review rounds, how often a person acted, time and cost per
workflow and class, the changes on the default branch; and that the output names nothing private.
docs/KPIS.md is the marked sample of that scenario until a person replaces it with real numbers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import KIT, ROOT, al

sys.path.insert(0, str(KIT / "dashboard" / "tests"))
import settle_fixtures as sf  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import al_kpis  # noqa: E402

HANDED_OVER = sf.MERGED + sf.SQUASHED + sf.INTEGRATED + sf.OPEN
ALL_RUNS = HANDED_OVER + [sf.INTEGRATION, sf.WAITING, sf.OLD_FAILURE] + sf.FAILED + sf.RESTARTS
A, B, C = "f0000001", "f0000002", "f0000003"


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("al-kpis") / "s"
    sf.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path) -> dict[str, Path]:
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return {"root": root, "repo": root / "orchard", "layer": root / "orchard.al"}


def kpis(layer: Path, *args: str) -> dict:
    return json.loads(al("kpis", layer, "--json", *args).stdout)


def _step(name: str, status: str, attempts: int, kind: str = "run", detail: str = "") -> dict:
    return {"name": name, "kind": kind, "status": status, "attempts": attempts, "started": "", "ended": "",
            "detail": detail}


@pytest.fixture()
def small(scenario) -> Path:
    """The orchard layer with three runs of its own: A passed and shipped by the gate after three
    attempts at its check and two review rounds; B (a feature) handed over for two reasons and settled
    by a person; C failed on step discipline, has no recorded cost and starts B again."""
    layer = scenario["layer"]
    shutil.rmtree(layer / "agents")
    tip = subprocess.run(["git", "-C", str(scenario["repo"]), "rev-parse", "main"], capture_output=True, text=True,
                         check=True).stdout.strip()

    def at(day: int, minutes: int = 0) -> str:
        return (datetime(2026, 11, day, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()

    sf.write_run(layer, A, prompt="Prune the old pear rows", dry_run=False, started=at(5), ended=at(5, 10),
                 branch=f"al/{A}", cost={"total": 0.5},
                 steps=[_step("plan", "passed", 1), _step("test", "passed", 3, kind="check")],
                 review={"rounds": [{"round": 1}, {"round": 2}], "verdict": {"success": True, "issues": []}},
                 ship={"action": "merge", "shipped": True, "commit": tip, "target": "main"})
    sf.write_run(layer, B, prompt="Add a harvest planner", dry_run=False, klass="feature", status="handed-over",
                 started=at(12), ended=at(12, 20), branch=f"al/{B}", cost={"total": 1.5},
                 steps=[_step("plan", "passed", 1), _step("test", "passed", 2, kind="check")],
                 review={"rounds": [{"round": 1}], "verdict": {"success": True, "issues": []}},
                 ship={"action": "pr", "shipped": False, "target": "",
                       "handed_over": ["class-not-allowed:feature", "diff-too-large:500>400"]})
    sf.write_run(layer, C, prompt="Add a harvest planner", dry_run=False, status="failed", started=at(13),
                 ended=at(13, 5), branch=f"al/{C}", replaces=B,
                 steps=[_step("build", "failed", 1, detail="step-discipline: the step committed; see the guide")])
    al("settle", layer, B, "--as", "abandoned", "--note", "the planner waits for next season", "--who", "Ada")
    return layer


def test_kpis_counts_runs_by_outcome(scenario):
    runs = kpis(scenario["layer"])["runs"]
    assert runs["total"] == len(ALL_RUNS) == 27
    assert runs["passed"] == 1                      # the integration run
    assert runs["handed_over"] == len(HANDED_OVER)
    assert runs["why"]["handed_over"] == {"review-rejected": len(HANDED_OVER)}
    assert runs["failed"] == len(sf.FAILED) + 1
    assert runs["why"]["failed"] == {"setup": len(sf.FAILED), "run": 1}
    assert runs["landed_afterwards"] == len(sf.MERGED + sf.SQUASHED)
    assert runs["waiting"] == 1 and runs["running"] == len(sf.RESTARTS)
    assert sum(runs[name] for name in al_kpis.OUTCOMES) == runs["total"]
    assert set(runs["why"]["handed_over"]) <= set(al_kpis.HANDOVER_WHY)
    assert set(runs["why"]["failed"]) <= set(al_kpis.FAILED_WHY)


def test_kpis_attempts_rounds_and_person_actions(small):
    data = kpis(small)
    assert data["runs"]["why"] == {"handed_over": {"class-not-allowed": 1, "change-too-large": 1},
                                   "failed": {"step-discipline": 1}}
    assert data["attempts"] == {
        "build": {"records": 1, "mean": 1.0, "max": 1, "first_attempt_share": 0.0},
        "plan": {"records": 2, "mean": 1.0, "max": 1, "first_attempt_share": 1.0},
        "test": {"records": 2, "mean": 2.5, "max": 3, "first_attempt_share": 0.0}}
    assert data["review"] == {"runs_reviewed": 2, "mean": 1.5, "max": 2, "runs_with_more_than_one": 1}
    assert data["person"] == {
        "runs_needing_a_person": 2, "actions": 3, "zero_touch_runs": 1,
        "by_action": {"handed_over": 1, "waited_at_approval": 0, "settled": 1, "started_again": 1}}


def test_kpis_duration_and_cost_by_workflow_and_class(small):
    groups = {(g["workflow"], g["class"], g["dry_run"]): g for g in kpis(small)["time_and_cost"]}
    assert set(groups) == {(sf.ADW, "chore", False), (sf.ADW, "feature", False)}
    chore, feature = groups[(sf.ADW, "chore", False)], groups[(sf.ADW, "feature", False)]
    assert chore["runs"] == 2
    assert chore["duration_seconds"] == {"runs": 2, "median": 450.0, "total": 900.0}
    assert chore["cost_usd"] == {"runs": 1, "median": 0.5, "total": 0.5, "without_cost": 1}   # C has no cost
    assert feature["duration_seconds"] == {"runs": 1, "median": 1200.0, "total": 1200.0}
    assert feature["cost_usd"] == {"runs": 1, "median": 1.5, "total": 1.5, "without_cost": 0}


def test_kpis_dry_runs_are_their_own_rows_without_cost(scenario):
    groups = kpis(scenario["layer"])["time_and_cost"]
    assert [(g["workflow"], g["class"], g["dry_run"], g["runs"]) for g in groups] == [(sf.ADW, "chore", True, 27)]
    assert groups[0]["cost_usd"] is None
    assert groups[0]["duration_seconds"]["runs"] == 27 - len(sf.RESTARTS)   # a running run has no duration yet


def test_kpis_since_filters_by_start_date(small):
    assert kpis(small)["runs"]["total"] == 3
    later = kpis(small, "--since", "2026-11-10")
    assert later["since"] == "2026-11-10" and later["runs"]["total"] == 2
    assert later["runs"]["passed"] == 0 and later["runs"]["handed_over"] == 1 and later["runs"]["failed"] == 1
    assert kpis(small, "--since", "2026-11-12")["runs"]["total"] == 2   # the day itself counts
    assert kpis(small, "--since", "2027-01-01")["runs"]["total"] == 0
    bad = al("kpis", small, "--since", "last week", ok=False)
    assert bad.returncode == 2 and "usage: al kpis" in bad.stderr and "YYYY-MM-DD" in bad.stderr
    assert bad.stdout == ""


def test_kpis_changes_on_the_default_branch(scenario, small):
    del small   # the same layer, now with three runs: only A is on main
    assert kpis(scenario["layer"])["default_branch"] == {"changes": 1, "shipped_by_the_gate": 1, "landed_afterwards": 0}


def test_kpis_changes_on_the_default_branch_are_read_from_git(scenario):
    repo = scenario["repo"]
    branch = kpis(scenario["layer"])["default_branch"]
    assert branch == {"changes": len(sf.MERGED + sf.SQUASHED) + 1, "shipped_by_the_gate": 1,
                      "landed_afterwards": len(sf.MERGED + sf.SQUASHED)}
    # git agrees: each landed run's file is on main, the open ones' files are not
    on_main = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "main"], capture_output=True,
                             text=True, check=True).stdout.split()
    assert all(f"src/{run}.py" in on_main for run in sf.MERGED + sf.SQUASHED)
    assert not any(f"src/{run}.py" in on_main for run in sf.OPEN)
    # nothing is fetched: the application has no remote at all
    assert subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True).stdout.strip() == ""


@pytest.mark.parametrize("which", ["scenario", "small"])
def test_kpis_output_has_no_names_paths_or_prompts(which, scenario, request):
    layer = request.getfixturevalue(which) if which == "small" else scenario["layer"]
    outputs = [al("kpis", layer, *flags).stdout for flags in ((), ("--json",), ("--md",))]
    for text in outputs:
        for private in ("Track the", "pruning calendar", "rootstock", "orchard map", "map renderer", "harvest",
                        "pear rows", "pear trees", "sandbox image", "see the guide", "next season", "Ada", "orchard", "Orchard",
                        str(scenario["root"]), "al/", "feature:", "500>400", "forge.", "http", "@"):
            assert private not in text, private
        assert not re.search(r"\b[0-9a-f]{8}\b", text)            # no run id
        assert not re.search(r"\b[0-9a-f]{7,40}\b", text)         # no commit
        assert not re.search(r"\bmain\b", text)                   # the default branch is named in words only
        assert not re.search(r"(^|[\s\"'(])(/|~/|\.\./)\w", text, re.MULTILINE)   # no path
    assert "default branch" in outputs[0] and "default_branch" in outputs[1]


def test_kpis_md_is_a_document_and_text_is_the_default(scenario):
    md = al("kpis", scenario["layer"], "--md").stdout
    assert md.startswith("# Numbers\n") and "SAMPLE" not in md
    for heading in ("## Runs by outcome", "## Attempts per step", "## Review rounds", "## How often a person acted",
                    "## Time and cost by workflow and class", "## Changes on the default branch", "## What is counted"):
        assert heading in md
    assert "| handed over | 14 | 52% | review-rejected 14 |" in md
    assert "|---|---|" in md
    text = al("kpis", scenario["layer"]).stdout
    lines = text.splitlines()
    assert lines[0] == "Numbers from the run records: 27 runs"
    assert "|" not in text and "#" not in text
    table = lines[lines.index("Runs by outcome") + 1:lines.index("Attempts per step") - 1]
    assert table[0].split() == ["outcome", "runs", "share", "why"]
    column = table[0].index("runs") + len("runs")        # the numbers end under the end of their heading
    assert all(row[:column].rstrip()[-1].isdigit() and row[column] == " " for row in table[1:])
    both = al("kpis", scenario["layer"], "--json", "--md", ok=False)
    assert both.returncode == 2 and "usage: al kpis" in both.stderr and both.stdout == ""


def test_kpis_on_a_layer_without_runs(scenario):
    shutil.rmtree(scenario["layer"] / "agents")
    data = kpis(scenario["layer"])
    assert data["runs"]["total"] == 0 and data["attempts"] == {} and data["time_and_cost"] == []
    assert data["review"]["mean"] is None and data["person"]["actions"] == 0
    assert data["default_branch"]["changes"] == 0
    text = al("kpis", scenario["layer"])
    assert text.returncode == 0 and "0 runs" in text.stdout
    assert al("kpis", scenario["layer"], "--md").stdout.startswith("# Numbers\n")


def test_committed_kpis_file_is_the_marked_sample_of_the_fixture(scenario):
    committed = (ROOT / "docs" / "KPIS.md").read_text()
    if "SAMPLE" not in committed:
        return   # a person replaced the sample with the numbers of real runs
    lines = committed.splitlines()
    assert lines[0] == "# Numbers" and lines[1] == al_kpis.SAMPLE_LINE
    generated = al("kpis", scenario["layer"], "--md", "--sample").stdout

    def undated(text: str) -> list[str]:
        return [line for line in text.splitlines() if not line.startswith("Generated by `tools/al kpis` on ")]

    assert undated(committed) == undated(generated)
    assert len(undated(committed)) == len(lines) - 1   # exactly one dated line
