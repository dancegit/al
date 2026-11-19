"""The scenario of "Give to an agent", as code: the pantry of integrate_fixtures (an invented
application with real git history and its layer `pantry.al/`) plus four more stopped runs, written
as state.json records on real branches, one per kind of plan that integrate_fixtures does not have:

  f0000011  handed over: its review rejected it after two rounds, one blocker     → blockers
            (its manifest also says that 3 of 5 screenshots were left out)
  f0000012  handed over: rejected; one blocker is `REQUIRES HUMAN: deploy/release.cfg is a
            protected file`                                                       → a person decides
  f0000013  failed: the check `test` exited 1; its worktree is kept               → resume from the check
  f0000014  failed before changing anything (no branch, no worktree)              → start again

and, from integrate_fixtures: f0000001, f0000002 (merge conflict or moved target → integrate),
f0000003 (rejected → blockers), f0000004 and f0000005 (a person decides).

The layer's workflow `ship-it` gets what a patch round needs (the command `patch`, and `patch` and
`implement` on its review step) and the image artefact `shots`; adws/assign.py is the text `al gen`
writes, importing the layer's copy of this checkout's runtime; mock/integrate/address-implement.json
is what the mock agent does when it addresses the blocker of f0000011. Every name is invented.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import integrate_fixtures as fx
import settle_fixtures as sf

ADW = fx.ADW
REJECTED, PERSON, FAILED_CHECK, NOTHING = (f"f00000{n}" for n in range(11, 15))
STOPPED = [REJECTED, PERSON, FAILED_CHECK, NOTHING]
# the kind of plan of every run under Needs you
KINDS = {fx.ACCEPTANCE: "integrate", fx.CLEAN: "integrate", fx.REJECTED: "blockers", fx.PROTECTED: "person",
         fx.CHANGED: "person", REJECTED: "blockers", PERSON: "person", FAILED_CHECK: "check", NOTHING: "start-again"}
BLOCKER = {"description": "total() accepts a negative discount", "resolution": "reject a negative discount"}
PERSON_BLOCKER = {"description": "REQUIRES HUMAN: deploy/release.cfg is a protected file",
                  "resolution": "a person moves the release to the new channel"}
LABEL_BLOCKER = {"description": "the labels are not sorted", "resolution": "sort the labels by shelf"}
DISCOUNT_STOCK = ("def total(items, tax=0, discount=0):\n    return sum(items) + tax - discount\n")
FIXED_STOCK = ("def total(items, tax=0, discount=0):\n    if discount < 0:\n        raise ValueError('negative discount')\n"
               "    return sum(items) + tax - discount\n")
TITLES = {REJECTED: "Take a discount off the total", PERSON: "Print the shelf labels",
          FAILED_CHECK: "Sort the stock list", NOTHING: "Count the empty jars"}
FILES = {REJECTED: {"stock.py": DISCOUNT_STOCK},
         PERSON: {"shelf_labels.py": "SHELF_LABELS = ['jam', 'flour']\n"},
         FAILED_CHECK: {"sorting.py": "def by_name(items):\n    return items\n"}}
LEFT_OUT = {"id": "shots", "left_out": 3, "matched": 5}
PLAN_COMMANDS = ["./index.sh", "python -m pytest tests -q"]


def branch_of(run_id: str) -> str:
    return f"chore/{run_id}-{fx._slug(TITLES[run_id])}"


def _patch_round(layer: Path) -> None:
    """The definition gains a patch round and an image artefact (see the module docstring)."""
    path = layer / "agentic-layer.json"
    data = json.loads(path.read_text())
    data["commands"].append({"name": "patch", "template": "plan-patch", "modelSet": "base", "output": "text",
                             "description": "", "file": "", "focus": [], "fabric": [], "relevant": [],
                             "validation": []})
    data["artefacts"] = [{"id": "shots", "kind": "image", "glob": "shots/*.png", "maxFiles": 2, "status": "stable"}]
    workflow = next(adw for adw in data["adws"] if adw["name"] == ADW)
    review = next(step for step in workflow["steps"] if step["kind"] == "review")
    review.update(patch="patch", implement="implement")
    path.write_text(json.dumps(data, indent=1))
    (layer / "commands" / "patch.md").write_text(
        "---\ndescription: patch\nargument-hint: [blockers] [plan]\n---\n\n# patch\n\nBLOCKERS: $1\nPLAN: $2\n")
    script = layer / "mock" / "integrate" / "address-implement.json"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(json.dumps({"raw_files": {"stock.py": FIXED_STOCK}, "output": "a negative discount is refused"}))


def _commit(repo: Path, run_id: str, base: str, keep: Path | None = None) -> str:
    """The run's files committed on its branch, cut from `base`, in a worktree (kept at `keep`)."""
    tree = keep or repo.parent / f"tree-{run_id}"
    git = sf.Git(repo)
    git("worktree", "add", "-q", "-b", branch_of(run_id), str(tree), base)
    fx._write(tree, FILES[run_id])
    inside = sf.Git(tree)
    inside("add", "-A")
    commit = inside.commit(f"chore: {TITLES[run_id]} [adw {run_id}]")
    if keep is None:
        git("worktree", "remove", "--force", str(tree))
    return commit


def _verdict(*blockers: dict) -> dict:
    return {"success": False, "mode": "auto", "summary": "the change is not right yet",
            "issues": [{"severity": "blocker", **blocker} for blocker in blockers]}


def _rejected(layer: Path, run_id: str, commit: str, start, blockers: list[dict], **more) -> None:
    end = start + timedelta(minutes=9)
    verdict = _verdict(*blockers)
    plan = layer / "specs" / f"plan-{run_id}.md"
    plan.parent.mkdir(exist_ok=True)
    plan.write_text(f"# Plan: {TITLES[run_id]}\n\n## Acceptance Criteria\n\n- the tests pass\n\n## Commands\n\n"
                    + "".join(f"- `{command}`\n" for command in PLAN_COMMANDS))
    reasons = ["review-rejected", "review-failed"]
    sf.write_run(layer, run_id, adw=ADW, prompt=f"{TITLES[run_id]}\n\nAsked for by the shop.", status="handed-over",
                 dry_run=False, class_fixed=True, started=fx._iso(start), ended=fx._iso(end),
                 branch=branch_of(run_id), plan=str(plan), steps=fx._steps(start, end, rejected=True),
                 checks={"test": {"exit": 0, "log": f"agents/{run_id}/checks/test.log"}},
                 review={"mode": "auto", "verdict": verdict,
                         "rounds": [{"round": number, "reviewers": [verdict], "verdict": verdict}
                                    for number in (1, 2)]},
                 ship={"action": "pr", "shipped": False, "handed_over": reasons, "reasons": ["review-failed"],
                       "gate": {"action": "pr", "ship": False, "reasons": ["review-failed"]},
                       "commit": commit, "target": f"agents/{run_id}/PR.md", "pr": None}, **more)
    (layer / "agents" / run_id / "PR.md").write_text(f"# chore: {TITLES[run_id]}\n")


def _left_out(layer: Path, run_id: str) -> list[dict]:
    """Two screenshots handed to the review of `run_id`, and the manifest entry for the three left out."""
    records = []
    for name in ("a-total.png", "b-discount.png"):
        path = layer / "artefacts" / run_id / "shots" / "shots" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        records.append({"id": "shots", "type": "image", "step": "review", "bytes": path.stat().st_size,
                        "path": path.relative_to(layer).as_posix(), "sha256": "", "url": "",
                        "left_out": LEFT_OUT["left_out"], "matched": LEFT_OUT["matched"]})
    entry = {"id": "shots", "type": "left-out", "step": "review", "path": "", "left_out": LEFT_OUT["left_out"],
             "matched": LEFT_OUT["matched"], "limit": LEFT_OUT["matched"] - LEFT_OUT["left_out"]}
    (layer / "artefacts" / run_id / "manifest.json").write_text(json.dumps([*records, entry], indent=2) + "\n")
    return records


def build_scenario(root: Path, *, with_assign: bool = True) -> dict:
    """root/pantry and root/pantry.al as integrate_fixtures builds them, plus the four runs above.
    `with_assign=False` gives a layer generated before `al assign` (no adws/assign.py)."""
    scenario = fx.build_scenario(root)
    layer, repo, tips = scenario["layer"], scenario["repo"], scenario["tips"]
    _patch_round(layer)
    if not with_assign:
        (layer / "adws" / "assign.py").unlink()
    start = scenario["base"] + timedelta(hours=5)
    _rejected(layer, REJECTED, _commit(repo, REJECTED, tips["main"]), start, [BLOCKER],
              artefacts=_left_out(layer, REJECTED))
    _rejected(layer, PERSON, _commit(repo, PERSON, tips["main"]), start + timedelta(minutes=20),
              [LABEL_BLOCKER, PERSON_BLOCKER])
    # a failed check: the work is on its branch and its worktree is kept
    tree = layer / "trees" / FAILED_CHECK
    tree.parent.mkdir(exist_ok=True)
    commit = _commit(repo, FAILED_CHECK, tips["main"], keep=tree)
    begun = start + timedelta(minutes=40)
    ended = begun + timedelta(minutes=4)
    log = f"agents/{FAILED_CHECK}/checks/test.1.log"
    why = f"check test exited 1 after 1 attempt(s); log {log}"
    sf.write_run(layer, FAILED_CHECK, adw=ADW, prompt=f"{TITLES[FAILED_CHECK]}\n\nAsked for by the shop.",
                 status="failed", dry_run=False, class_fixed=True, started=fx._iso(begun), ended=fx._iso(ended),
                 branch=branch_of(FAILED_CHECK), worktree=str(tree),
                 steps=[sf._step("plan", "passed", begun, ended), sf._step("test", "failed", begun, ended,
                                                                           f"test: {why}", kind="check")],
                 checks={"test": {"exit": 1, "log": log}},
                 ship={"action": "handoff", "shipped": False, "reasons": [f"run-stopped:test: {why}"], "pr": None,
                       "commit": commit, "branch": branch_of(FAILED_CHECK), "stopped_at": "test",
                       "target": f"agents/{FAILED_CHECK}/PR.md"})
    (layer / "agents" / FAILED_CHECK / "checks").mkdir(parents=True, exist_ok=True)
    (layer / log).write_text("FAILED tests/test_sorting.py::test_by_name - the list is not sorted\n")
    (layer / "agents" / FAILED_CHECK / "PR.md").write_text(f"# [needs human] chore: {TITLES[FAILED_CHECK]}\n")
    # a run that failed before it changed anything
    begun = start + timedelta(minutes=60)
    ended = begun + timedelta(seconds=30)
    sf.write_run(layer, NOTHING, adw=ADW, prompt=f"{TITLES[NOTHING]}\n\nAsked for by the shop.", status="failed",
                 dry_run=False, class_fixed=True, started=fx._iso(begun), ended=fx._iso(ended),
                 branch=branch_of(NOTHING),
                 steps=[sf._step("plan", "failed", begun, ended, "plan: the agent returned nothing")],
                 ship={"handoff_skipped": "nothing changed relative to main: nothing to hand over"})
    return scenario


def plan_of(layer: Path, run_id: str, *more: str) -> dict:
    """What the layer's own adws/assign.py says about a run: `preview RUN --json`."""
    import subprocess
    import sys

    done = subprocess.run([sys.executable, str(layer / "adws" / "assign.py"), "preview", run_id, "--json", *more],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def record_of(layer: Path, run_id: str) -> dict:
    return json.loads((layer / "agents" / run_id / "assign.json").read_text())
