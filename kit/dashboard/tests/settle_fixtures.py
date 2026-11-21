"""The runs-list scenario of "landed, superseded and replaced runs", as code: an invented application
repository `orchard/` with real git history and a layer `orchard.al/` beside it (laid out by
start_layer.build_layer, so its own adws/dispatch.py can start runs again), whose agents/ holds:

  b0000001-b0000005  handed over; merged into main with a merge commit        → landed (ancestor)
  b0000006-b0000008  handed over; squash-merged into main                      → landed (patch-id)
  b0000009-b000000a  handed over; integrated by c0000001, which changed them
                     further, so neither patch-id nor every path matches        → settled (superseded)
  b000000b-b000000e  handed over and open; b000000e has pull request #7        → needs you
  c0000001           passed and shipped: "Integrate al/b0000009 and al/b000000a" (squash-merged)
  d0000001-d0000005  failed at preflight with the same error up to ids and times, ending within
                     ten minutes; each replaced by e0000001-e0000005 (running) → settled (replaced)
  a0000001           waiting at its approval step                              → needs you
  a0000002           an older failure at build that nothing replaces           → needs you

So: needs you 6, running 5, landed 8, settled 7, done 1, all 27. build_group_scenario() is the same
five failures without their replacements (one failure group). build_two_groups_scenario() has two
failure groups of two runs each and nothing else: f0000001-f0000002 failed at preflight, f0000003-f0000004
at build, the second of each the newer. Git runs with a fixed author,
committer and dates, so the commits are the same on every build. Every name is invented.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import start_layer

OPEN = ["b000000b", "b000000c", "b000000d", "b000000e"]
MERGED = ["b0000001", "b0000002", "b0000003", "b0000004", "b0000005"]
SQUASHED = ["b0000006", "b0000007", "b0000008"]
INTEGRATED = ["b0000009", "b000000a"]
INTEGRATION = "c0000001"
FAILED = ["d0000001", "d0000002", "d0000003", "d0000004", "d0000005"]
RESTARTS = ["e0000001", "e0000002", "e0000003", "e0000004", "e0000005"]
GROUP_A = ["f0000001", "f0000002"]
GROUP_B = ["f0000003", "f0000004"]
WAITING = "a0000001"
OLD_FAILURE = "a0000002"
PR_RUN = "b000000e"
ADW = "plan-build"
GIT_START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class Git:
    """git in one repository with a fixed identity and a clock that moves a minute per commit."""

    def __init__(self, repo: Path) -> None:
        self.repo, self.clock = repo, GIT_START

    def __call__(self, *args: str) -> str:
        stamp = self.clock.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_AUTHOR_NAME="Orchard Dev", GIT_AUTHOR_EMAIL="dev@orchard.example",
                   GIT_COMMITTER_NAME="Orchard Dev", GIT_COMMITTER_EMAIL="dev@orchard.example",
                   GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp, GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull)
        done = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, env=env,
                              timeout=30)
        if done.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {done.stderr}")
        return done.stdout.strip()

    def commit(self, message: str) -> str:
        self.clock += timedelta(minutes=1)
        self("commit", "-q", "-m", message)
        return self("rev-parse", "HEAD")


def _write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _branch(git: Git, repo: Path, run_id: str, files: dict[str, str], base: str = "main") -> str:
    git("checkout", "-q", "-b", f"al/{run_id}", base)
    for name, text in files.items():
        _write(repo, name, text)
    git("add", "-A")
    tip = git.commit(f"{run_id}: {', '.join(files)}")
    git("checkout", "-q", "main")
    return tip


def build_repo(repo: Path, *, merge: bool = True) -> dict[str, str]:
    """orchard/ with main and one branch al/<id> per handed-over run → {run id: branch tip}."""
    repo.mkdir(parents=True)
    git = Git(repo)
    git("init", "-q", "-b", "main")
    _write(repo, "README.md", "# orchard\n\nKeeps track of the trees in an orchard.\n")
    _write(repo, "src/trees.py", "TREES = []\n")
    git("add", "-A")
    git.commit("orchard: first commit")
    tips: dict[str, str] = {}
    for run_id in MERGED + SQUASHED + INTEGRATED + OPEN:
        tips[run_id] = _branch(git, repo, run_id, {f"src/{run_id}.py": f"# work of run {run_id}\nDONE = True\n"})
    if not merge:
        return tips
    for run_id in MERGED:
        git.clock += timedelta(minutes=1)
        git("merge", "-q", "--no-ff", "-m", f"Merge al/{run_id}", f"al/{run_id}")
    for run_id in SQUASHED:
        git("merge", "-q", "--squash", f"al/{run_id}")
        git.commit(f"{run_id} (squashed)")
    # the integration run merges two branches and changes their files further
    git("checkout", "-q", "-b", f"al/{INTEGRATION}", "main")
    for run_id in INTEGRATED:
        git("merge", "-q", "--no-ff", "-m", f"Merge al/{run_id}", f"al/{run_id}")
    for run_id in INTEGRATED:
        _write(repo, f"src/{run_id}.py", f"# work of run {run_id}, integrated\nDONE = True\nCHECKED = True\n")
    git("add", "-A")
    tips[INTEGRATION] = git.commit("integrate: both changes, checked together")
    git("checkout", "-q", "main")
    git("merge", "-q", "--squash", f"al/{INTEGRATION}")
    tips["main"] = git.commit(f"{INTEGRATION}: integrate al/{INTEGRATED[0]} and al/{INTEGRATED[1]} (squashed)")
    return tips


def _iso(when: datetime) -> str:
    return when.isoformat()


def write_run(layer: Path, run_id: str, **state) -> Path:
    """agents/<id>/state.json (and request.md with the prompt) as the runtime writes them."""
    run = layer / "agents" / run_id
    run.mkdir(parents=True, exist_ok=True)
    data = {"adw_id": run_id, "adw": ADW, "prompt": "", "source": "cli", "klass": "chore", "class_fixed": False,
            "dry_run": True, "model_set": "", "branch": "", "worktree": "", "ports": [], "plan": "", "outputs": {},
            "steps": [], "checks": {}, "scripts": {}, "artefacts": [], "review": {}, "ship": {}, "fanout": {},
            "status": "passed", "started": "", "ended": "", "pid": 0, "host": "", "heartbeat": "",
            "fanout_parent": {}, "overrides": {}, "models": {}, "agent_calls": [], "cost": {}, "budget": {},
            "usage": {}, "notified": {}, "definition": {}, "image": "", **state}
    (run / "state.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    if data["prompt"]:
        (run / "request.md").write_text(data["prompt"] + "\n")
    return run


def _step(name: str, status: str, start: datetime, end: datetime | None, detail: str = "", kind: str = "run") -> dict:
    return {"name": name, "kind": kind, "status": status, "attempts": 1, "started": _iso(start),
            "ended": _iso(end) if end else "", "detail": detail}


def preflight_error(run_id: str, minute: int) -> str:
    return (f"preflight failed: the sandbox image al-orchard:3f2a{minute}c0d is missing for run {run_id} "
            f"(looked in /srv/orchard/images at 2026-11-1{minute % 2}T10:0{minute}:12Z); build it with `al sandbox`")


def _failures(layer: Path, base: datetime, replaced: bool) -> None:
    now = datetime.now(timezone.utc)
    for index, run_id in enumerate(FAILED):
        start = base + timedelta(hours=2, minutes=2 * index)
        end = start + timedelta(seconds=40)
        write_run(layer, run_id, prompt=f"Add the pruning calendar, part {index + 1}", status="failed",
                  started=_iso(start), ended=_iso(end), branch=f"al/{run_id}",
                  steps=[_step("preflight", "failed", start, end, preflight_error(run_id, index + 1), kind="setup")])
        if replaced:
            write_run(layer, RESTARTS[index], prompt=f"Add the pruning calendar, part {index + 1}", status="running",
                      started=_iso(now - timedelta(minutes=5 - index)), heartbeat=_iso(now), replaces=run_id,
                      branch=f"al/{RESTARTS[index]}",
                      steps=[_step("plan", "running", now - timedelta(minutes=1), None)])


def _layer(root: Path, repo: Path) -> Path:
    layer = start_layer.build_layer(root, "orchard", repo, None, port_base=28800)
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["description"] = "the orchard's agentic layer"
    data["codebase"]["root"] = "../orchard"
    (layer / "agentic-layer.json").write_text(json.dumps(data, indent=1))
    return layer


def build_scenario(root: Path) -> dict:
    """root/orchard (the application) and root/orchard.al (the layer) with the 27 runs above."""
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "orchard"
    tips = build_repo(repo)
    layer = _layer(root, repo)
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=5)
    for index, run_id in enumerate(MERGED + SQUASHED + INTEGRATED + OPEN):
        start = base + timedelta(minutes=10 * index)
        end = start + timedelta(minutes=6)
        state = {"prompt": f"Track the {['apple', 'pear', 'plum', 'cherry', 'quince', 'medlar', 'fig'][index % 7]} "
                           f"trees, part {index + 1}",
                 "status": "handed-over", "started": _iso(start), "ended": _iso(end), "branch": f"al/{run_id}",
                 "steps": [_step("plan", "passed", start, start + timedelta(minutes=2)),
                           _step("build", "passed", start + timedelta(minutes=2), end)],
                 "ship": {"action": "pr", "shipped": False, "handed_over": ["review-rejected"],
                          "commit": tips[run_id], "target": ""}}
        if run_id == PR_RUN:
            state["ship"].update(pr={"number": 7, "url": "https://forge.orchard.example/orchard/pulls/7"},
                                 target="https://forge.orchard.example/orchard/pulls/7")
        write_run(layer, run_id, **state)
    start = base + timedelta(hours=3)
    write_run(layer, INTEGRATION, prompt=f"Integrate al/{INTEGRATED[0]} and al/{INTEGRATED[1]} and check them together",
              status="passed", started=_iso(start), ended=_iso(start + timedelta(minutes=9)),
              branch=f"al/{INTEGRATION}",
              steps=[_step("plan", "passed", start, start + timedelta(minutes=3)),
                     _step("build", "passed", start + timedelta(minutes=3), start + timedelta(minutes=9))],
              ship={"action": "merge", "shipped": True, "commit": tips["main"], "target": "main"})
    _failures(layer, base, replaced=True)
    start = base + timedelta(hours=4)
    write_run(layer, WAITING, prompt="Order new rootstock once the budget is approved", status="waiting",
              started=_iso(start), ended=_iso(start + timedelta(minutes=2)), branch=f"al/{WAITING}",
              steps=[_step("plan", "passed", start, start + timedelta(minutes=1)),
                     _step("approve", "waiting", start + timedelta(minutes=1), start + timedelta(minutes=2),
                           "waiting for the budget approval", kind="script")])
    start = base - timedelta(days=2)
    write_run(layer, OLD_FAILURE, prompt="Draw the orchard map", status="failed", started=_iso(start),
              ended=_iso(start + timedelta(minutes=4)), branch=f"al/{OLD_FAILURE}",
              steps=[_step("plan", "passed", start, start + timedelta(minutes=1)),
                     _step("build", "failed", start + timedelta(minutes=1), start + timedelta(minutes=4),
                           "build: the map renderer needs a page size")])
    return {"root": root, "repo": repo, "layer": layer, "tips": tips, "base": base}


def build_group_scenario(root: Path) -> dict:
    """root/orchard and root/orchard.al with only the five preflight failures, none replaced."""
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "orchard"
    tips = build_repo(repo, merge=False)
    layer = _layer(root, repo)
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    _failures(layer, base, replaced=False)
    return {"root": root, "repo": repo, "layer": layer, "tips": tips, "base": base}


def build_two_groups_scenario(root: Path) -> dict:
    """root/orchard and root/orchard.al with two failure groups of two runs each: GROUP_A at preflight,
    GROUP_B at build; in each the runs end two minutes apart and the second is the newer."""
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "orchard"
    tips = build_repo(repo, merge=False)
    layer = _layer(root, repo)
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    for index, run_id in enumerate(GROUP_A):
        start = base + timedelta(hours=2, minutes=2 * index)
        end = start + timedelta(seconds=40)
        write_run(layer, run_id, prompt=f"Add the pruning calendar, part {index + 1}", status="failed",
                  started=_iso(start), ended=_iso(end), branch=f"al/{run_id}",
                  steps=[_step("preflight", "failed", start, end, preflight_error(run_id, index + 1), kind="setup")])
    for index, run_id in enumerate(GROUP_B):
        start = base + timedelta(hours=3, minutes=2 * index)
        end = start + timedelta(minutes=1)
        write_run(layer, run_id, prompt=f"Draw the orchard map, part {index + 1}", status="failed",
                  started=_iso(start), ended=_iso(end), branch=f"al/{run_id}",
                  steps=[_step("plan", "passed", start, start + timedelta(seconds=20)),
                         _step("build", "failed", start + timedelta(seconds=20), end,
                               "build: the map renderer needs a page size")])
    return {"root": root, "repo": repo, "layer": layer, "tips": tips, "base": base}
