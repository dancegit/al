"""(fan-out …) steps: candidates as real child runs, selection, merge into the parent, cleanup.

Real git, mock agent, no LLM, no network. Candidates differ deterministically through the
codebase `test` command (check.sh below): the parent exports ADW_FANOUT_INDEX=1..N to each
child, and check.sh reads `<cfg>/<index>` = "FAILS LINES PAUSE FILE":
  FAILS  the first FAILS check attempts fail (each failure triggers the mock resolve step),
  LINES  lines written to FILE in the worktree on the first attempt (diff size; "-" = no file),
  PAUSE  seconds slept on the first attempt (completion order).
The child's last step is a mock `commit` with "commit": true, so its work ends up on its branch.

Most tests launch children with `python -c 'runner.main(…)'` (the injected `fanout.launcher`);
one test uses the real default `uv run <layer>/adws/adw_<child>.py` path.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, fanout
from adw_modules.runner import main

RUNTIME = Path(__file__).resolve().parents[1]
PARENT_ID = "fa000001"

CHECK_SH = """#!/bin/bash
set -u
idx="${ADW_FANOUT_INDEX:-0}"
read -r fails lines pause file < "CFG/$idx"
counter="CFG/count-$(basename "$PWD")"
n=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$counter"
if [ "$n" -eq 1 ]; then
  sleep "$pause"
  if [ "$file" != "-" ]; then
    : > "$file"
    for i in $(seq 1 "$lines"); do echo "candidate $idx line $i" >> "$file"; done
  fi
fi
[ "$n" -gt "$fails" ]
"""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _layer(make_layer, tmp_path: Path, configs: dict[int, str], *, count=None, pick="best", max_parallel=0,
           max_runs=8, parent_env=None, extra_envs=None, retries=2) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    for index, line in configs.items():
        (cfg / str(index)).write_text(line + "\n", encoding="utf-8")
    script = tmp_path / "check.sh"
    script.write_text(CHECK_SH.replace("CFG", str(cfg)), encoding="utf-8")
    command = lambda name, output="text": {"name": name, "template": name, "modelSet": "base", "output": output,  # noqa: E731
                                            "description": "", "file": "", "focus": [], "fabric": [], "relevant": [],
                                            "validation": []}
    layer = make_layer({
        "concurrency": {"id": "slots", "kind": "slots", "status": "stable", "maxRuns": max_runs, "maxAgents": 8,
                        "maxChecks": 8, "waitSeconds": 60},
        "codebase": {"commands": [{"name": "test", "run": f"bash {script}", "timeout": 30},
                                  {"name": "verify", "run": "ls cand-*.txt shared.txt 2>/dev/null | grep -q .",
                                   "timeout": 30}]},
        "commands": [command("plan", "path"), command("build"), command("resolve-test"), command("commit"),
                     command("review", "json")],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False}],
        "environments": [
            {"id": "wt-child", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27000,
             "portsPerRun": 0, "keep": "on-failure", "status": "stable"},
            {"id": "wt-parent", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27500,
             "portsPerRun": 0, "keep": "always", "status": "stable"},
            *(extra_envs or [])],
        "adws": [
            {"name": "candidate", "agent": "mock-agent", "review": "human", "environment": ["wt-child"],
             "input": [], "classes": [], "steps": [
                 {"name": "plan", "kind": "run", "command": "plan"},
                 {"name": "build", "kind": "run", "command": "build"},
                 {"name": "test", "kind": "check", "cmd": "test", "resolve": "resolve-test", "retries": retries},
                 {"name": "commit", "kind": "run", "command": "commit"}]},
            {"name": "parent", "agent": "mock-agent", "review": "human", "environment": parent_env or ["wt-parent"],
             "input": [], "classes": [], "steps": [
                 {"name": "race", "kind": "fan-out", "adw": "candidate", "count": count or len(configs),
                  "pick": pick, "maxParallel": max_parallel},
                 *([] if parent_env else [{"name": "verify", "kind": "check", "cmd": "verify", "retries": 0}])]},
        ],
    })
    mock = layer / "mock" / "candidate"
    mock.mkdir(parents=True, exist_ok=True)
    (mock / "commit.json").write_text(json.dumps({"commit": True}), encoding="utf-8")
    return layer


@pytest.fixture
def launches(monkeypatch):
    """Launch candidates as `python -c runner.main(…)` subprocesses and record their argv."""
    calls: list[list[str]] = []
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
            "from adw_modules.runner import main; sys.exit(main(sys.argv[2], Path(sys.argv[3]), sys.argv[4:]))")

    def launch(layer, child, args, env, log):
        calls.append(list(args))
        return subprocess.Popen([sys.executable, "-c", code, str(RUNTIME), child, str(layer.layer_dir), *args],
                                cwd=layer.layer_dir, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    monkeypatch.setattr(fanout, "launcher", launch)
    return calls


def _state(layer: Path, adw_id: str = PARENT_ID) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _by_index(parent: dict) -> dict[int, dict]:
    return {item["index"]: item for item in parent["fanout"]["race"]["candidates"]}


def _branches(app: Path) -> set[str]:
    return set(_git(app, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def _assert_app_untouched(app: Path, head: str) -> None:
    assert _git(app, "status", "--porcelain") == ""
    assert _git(app, "rev-parse", "HEAD") == head
    assert _git(app, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _git(app, "rev-parse", "refs/heads/main") == head


def _assert_candidates_cleaned(layer: Path, app: Path, parent: dict) -> None:
    for item in parent["fanout"]["race"]["candidates"]:
        assert not (layer / "trees" / item["id"]).exists()
        assert item["branch"] not in _branches(app)
    assert _git(app, "worktree", "list", "--porcelain").count("worktree ") == 2   # app + parent


def test_best_picks_fewest_resolves_then_smallest_diff(make_layer, app_repo, tmp_path, launches):
    head = _git(app_repo, "rev-parse", "HEAD")
    layer = _layer(make_layer, tmp_path, {1: "1 2 0 cand-1.txt", 2: "0 5 0 cand-2.txt", 3: "0 2 0 cand-3.txt"})
    assert main("parent", layer, ["Race them", "--adw-id", PARENT_ID]) == 0
    parent = _state(layer)
    assert parent["status"] == "passed"
    race = parent["fanout"]["race"]
    by_index = _by_index(parent)
    assert [by_index[i]["resolve_attempts"] for i in (1, 2, 3)] == [1, 0, 0]
    assert [by_index[i]["diff_lines"] for i in (1, 2, 3)] == [2, 5, 2]
    assert all(by_index[i]["status"] == "passed" for i in (1, 2, 3))
    assert race["pick"] == "best" and race["picked"] == [by_index[3]["id"]]
    assert parent["outputs"]["race"] == by_index[3]["id"]
    worktree = Path(parent["worktree"])
    assert (worktree / "cand-3.txt").read_text().splitlines() == ["candidate 3 line 1", "candidate 3 line 2"]
    assert not (worktree / "cand-1.txt").exists() and not (worktree / "cand-2.txt").exists()
    assert _git(worktree, "merge-base", "--is-ancestor", by_index[3]["head"], "HEAD", check=False) == ""
    assert parent["checks"]["verify"]["exit"] == 0                      # the later check ran on the winner
    _assert_candidates_cleaned(layer, app_repo, parent)
    assert _branches(app_repo) == {"main", parent["branch"]}
    for loser in (1, 2):                                                # losers recorded, not silently lost
        assert len(by_index[loser]["head"]) == 40
        assert _git(app_repo, "cat-file", "-t", by_index[loser]["head"]) == "commit"
    _assert_app_untouched(app_repo, head)
    log = (layer / "agents" / PARENT_ID / "run.log").read_text()
    assert log.count("fan-out race: candidate ") == 3 and "fan-out race: pick best ->" in log
    assert all(call[-2:] == ["--", "Race them"] and "--class" in call for call in launches)
    assert "--dry-run" not in launches[0]


def test_first_picks_by_completion_and_runs_in_parallel(make_layer, app_repo, tmp_path, launches):
    layer = _layer(make_layer, tmp_path, {1: "0 1 3 cand-1.txt", 2: "0 1 0 cand-2.txt", 3: "0 1 1.5 cand-3.txt"},
                   pick="first")
    assert main("parent", layer, ["First wins", "--adw-id", PARENT_ID]) == 0
    parent = _state(layer)
    by_index = _by_index(parent)
    assert parent["fanout"]["race"]["picked"] == [by_index[2]["id"]]
    worktree = Path(parent["worktree"])
    assert (worktree / "cand-2.txt").exists() and not (worktree / "cand-1.txt").exists()
    children = {i: _state(layer, by_index[i]["id"]) for i in (1, 2, 3)}
    assert children[2]["ended"] < children[1]["ended"]                 # ran concurrently: 2 overtook 1 (launched after it, finished before it)
    _assert_candidates_cleaned(layer, app_repo, parent)


def test_max_parallel_one_runs_candidates_sequentially(make_layer, app_repo, tmp_path, launches):
    layer = _layer(make_layer, tmp_path, {1: "0 1 1 cand-1.txt", 2: "0 1 0 cand-2.txt", 3: "0 1 0 cand-3.txt"},
                   pick="first", max_parallel=1)
    assert main("parent", layer, ["One at a time", "--adw-id", PARENT_ID]) == 0
    parent = _state(layer)
    by_index = _by_index(parent)
    children = [_state(layer, by_index[i]["id"]) for i in (1, 2, 3)]
    for earlier, later in zip(children, children[1:]):
        assert earlier["ended"] <= later["started"]
    assert parent["fanout"]["race"]["picked"] == [by_index[1]["id"]]   # sequential: launch order = completion order


def test_all_merges_every_candidate_in_order(make_layer, app_repo, tmp_path, launches):
    head = _git(app_repo, "rev-parse", "HEAD")
    layer = _layer(make_layer, tmp_path, {1: "0 1 0 cand-1.txt", 2: "1 2 0 cand-2.txt", 3: "0 3 0 cand-3.txt"},
                   pick="all")
    assert main("parent", layer, ["All of them", "--adw-id", PARENT_ID]) == 0
    parent = _state(layer)
    by_index = _by_index(parent)
    assert parent["fanout"]["race"]["picked"] == [by_index[i]["id"] for i in (1, 2, 3)]
    assert parent["outputs"]["race"] == " ".join(by_index[i]["id"] for i in (1, 2, 3))
    worktree = Path(parent["worktree"])
    for i in (1, 2, 3):
        assert (worktree / f"cand-{i}.txt").exists()
        assert _git(worktree, "merge-base", "--is-ancestor", by_index[i]["head"], "HEAD", check=False) == ""
    _assert_candidates_cleaned(layer, app_repo, parent)
    _assert_app_untouched(app_repo, head)


def test_all_fails_when_one_candidate_fails(make_layer, app_repo, tmp_path, launches):
    layer = _layer(make_layer, tmp_path, {1: "0 1 0 cand-1.txt", 2: "99 1 0 cand-2.txt"}, pick="all", retries=1)
    assert main("parent", layer, ["All or nothing", "--adw-id", PARENT_ID]) == 1
    parent = _state(layer)
    by_index = _by_index(parent)
    race_step = next(step for step in parent["steps"] if step["name"] == "race")
    assert race_step["status"] == "failed"
    assert by_index[2]["id"] in race_step["detail"] and "check" in race_step["detail"]
    assert by_index[1]["status"] == "passed" and by_index[2]["status"] == "failed"
    assert not (Path(parent["worktree"]) / "cand-1.txt").exists()       # nothing merged
    for item in by_index.values():
        assert not (layer / "trees" / item["id"]).exists()              # kept (failed) worktrees removed too
    assert by_index[2]["branch"] not in _branches(app_repo)


def test_no_candidate_passing_fails_with_every_reason(make_layer, app_repo, tmp_path, launches):
    layer = _layer(make_layer, tmp_path, {1: "99 1 0 cand-1.txt", 2: "99 1 0 cand-2.txt", 3: "0 0 0 -"}, retries=1)
    assert main("parent", layer, ["Nobody passes", "--adw-id", PARENT_ID]) == 1
    parent = _state(layer)
    by_index = _by_index(parent)
    detail = next(step for step in parent["steps"] if step["name"] == "race")["detail"]
    assert "no candidate passed" in detail
    for i in (1, 2, 3):
        assert by_index[i]["id"] in detail and by_index[i]["status"] == "failed"
    assert "committed nothing" in by_index[3]["reason"]                # a passing run without commits loses
    assert _state(layer, by_index[3]["id"])["status"] == "passed"
    assert parent["checks"] == {}                                       # the parent stopped at the fan-out
    assert _branches(app_repo) == {"main", parent["branch"]}
    for item in by_index.values():
        assert not (layer / "trees" / item["id"]).exists()


def test_conflicting_all_candidates_fail_cleanly(make_layer, app_repo, tmp_path, launches):
    head = _git(app_repo, "rev-parse", "HEAD")
    layer = _layer(make_layer, tmp_path, {1: "0 2 0 shared.txt", 2: "0 3 0 shared.txt"}, pick="all")
    assert main("parent", layer, ["Collide", "--adw-id", PARENT_ID]) == 1
    parent = _state(layer)
    detail = next(step for step in parent["steps"] if step["name"] == "race")["detail"]
    assert "merge" in detail and "conflict" in detail.lower()
    worktree = Path(parent["worktree"])
    assert _git(worktree, "status", "--porcelain") == ""                # merge aborted, no half state
    assert _git(worktree, "rev-parse", "HEAD") == head                  # first merge rolled back too
    assert not (worktree / ".git").is_dir() and not (worktree / "shared.txt").exists()
    by_index = _by_index(parent)
    for item in by_index.values():                                      # passing work is kept for a human
        assert item["branch"] in _branches(app_repo)
        assert not (layer / "trees" / item["id"]).exists()
    _assert_app_untouched(app_repo, head)


def test_dry_run_parent_passes_dry_run_to_candidates(make_layer, app_repo, tmp_path, launches):
    layer = _layer(make_layer, tmp_path, {1: "0 1 0 cand-1.txt", 2: "0 1 0 cand-2.txt"})
    assert main("parent", layer, ["Dry", "--adw-id", PARENT_ID, "--dry-run"]) == 0
    assert len(launches) == 2 and all("--dry-run" in call for call in launches)
    parent = _state(layer)
    for item in parent["fanout"]["race"]["candidates"]:
        child = _state(layer, item["id"])
        assert child["dry_run"] is True and child["prompt"] == "Dry" and child["klass"] == parent["klass"]


def test_max_runs_below_two_fails_fast(make_layer, app_repo, tmp_path, launches):
    layer = _layer(make_layer, tmp_path, {1: "0 1 0 cand-1.txt", 2: "0 1 0 cand-2.txt"}, max_runs=1)
    assert main("parent", layer, ["No room", "--adw-id", PARENT_ID]) == 1
    detail = next(step for step in _state(layer)["steps"] if step["name"] == "race")["detail"]
    assert "maxRuns" in detail and "at least 2" in detail
    assert launches == []


def test_sandboxed_parent_merges_through_gitsafe(make_layer, app_repo, tmp_path, launches, monkeypatch):
    """Parent with a podman option (podman faked; the parent has no check, so no container is
    started): the merge runs through gitsafe and the merged HEAD is published host-side."""
    real_run = envs.subprocess.run
    monkeypatch.setattr(envs.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stderr="", stdout="")
                        if argv[0] == "podman" else real_run(argv, **kw))
    head = _git(app_repo, "rev-parse", "HEAD")
    box = {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "2", "memory": "4g",
           "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
           "credentials": []}
    layer = _layer(make_layer, tmp_path, {1: "0 4 0 cand-1.txt", 2: "0 1 0 cand-2.txt"},
                   parent_env=["wt-parent", "box"], extra_envs=[box])
    assert main("parent", layer, ["Sandboxed race", "--adw-id", PARENT_ID]) == 0
    parent = _state(layer)
    by_index = _by_index(parent)
    assert parent["fanout"]["race"]["picked"] == [by_index[2]["id"]]
    published = _git(app_repo, "rev-parse", f"refs/heads/{parent['branch']}")
    assert _git(app_repo, "show", f"{published}:cand-2.txt") == "candidate 2 line 1"
    assert _git(app_repo, "merge-base", "--is-ancestor", by_index[2]["head"], published, check=False) == ""
    assert _branches(app_repo) == {"main", parent["branch"]}
    assert (Path(parent["worktree"]) / "cand-2.txt").exists()
    _assert_app_untouched(app_repo, head)


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_default_launcher_runs_generated_entry_script(make_layer, app_repo, tmp_path):
    """The real path: `uv run <layer>/adws/adw_candidate.py …`, with an entry script exactly as
    `al gen` writes it and the runtime copied into the layer."""
    layer = _layer(make_layer, tmp_path, {1: "0 2 0 cand-1.txt", 2: "0 1 0 cand-2.txt"})
    shutil.copytree(RUNTIME / "adw_modules", layer / "adws" / "adw_modules",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (layer / "adws" / "adw_candidate.py").write_text(
        "#!/usr/bin/env -S uv run --quiet --script\n# /// script\n# requires-python = \">=3.11\"\n"
        "# dependencies = []\n# ///\nimport sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        "from adw_modules.runner import main  # noqa: E402\n\n"
        "if __name__ == \"__main__\":\n"
        "    sys.exit(main(adw=\"candidate\", layer_dir=Path(__file__).resolve().parent.parent))\n",
        encoding="utf-8")
    assert main("parent", layer, ["Real launch", "--adw-id", PARENT_ID]) == 0
    parent = _state(layer)
    by_index = _by_index(parent)
    assert parent["fanout"]["race"]["picked"] == [by_index[2]["id"]]
    assert (Path(parent["worktree"]) / "cand-2.txt").exists()
    log = layer / "agents" / PARENT_ID / "fanout" / "race" / f"{by_index[1]['id']}.log"
    assert "=== ADW summary ===" in log.read_text()


def test_missing_entry_script_names_the_fix(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, {1: "0 1 0 cand-1.txt", 2: "0 1 0 cand-2.txt"})
    assert main("parent", layer, ["No script", "--adw-id", PARENT_ID]) == 1
    detail = next(step for step in _state(layer)["steps"] if step["name"] == "race")["detail"]
    assert "adw_candidate.py" in detail and "al gen" in detail
