"""Helpers for the tests of adw_modules/assign.py: on the pantry of integrate_fixtures (invented
names only), one stopped run per row of the table "why a run stopped → what Give to an agent does".

Every builder returns (layer directory, run id). Except `conflict_run` (the hand-written run of
integrate_fixtures) and `died_run` (a real failed run whose state.json is then left `running`),
each run is a real run of the workflow `demo` through runner.main with mock scripts.

The layer: the review option allows one patch round (`maxRetries` 1) and collects the image
artefact `shots` (`shots/*.png`, at most 2 files); the worktree is kept when a run fails; the check
`test` passes unless the file `broken.flag` exists, and has a resolve command."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from adw_modules import runner
from integrate_fixtures import (MAIN_FILES, RUN_STOCK, RUNTIME, conflict_layer, handed_over_run,
                                move_main, state_of)

RUN = "f0000001"
TITLE = "Give stock a discount"
PROMPT = f"{TITLE}\n\nThe total can take a discount."
BLOCKER = {"description": "total() ignores a negative discount", "resolution": "reject a negative discount"}
PROOF_BLOCKER = {"description": "no screenshot of the shelf: proof not collected",
                 "resolution": "collect the screenshot of the shelf"}
PERSON_BLOCKER = {"description": "REQUIRES HUMAN: deploy/release.cfg is a protected file",
                  "resolution": "a person changes the release channel"}
LOGIN = "the agent's login was refused 4 times (authentication error)"
FIXED_STOCK = ("def total(items, discount=0):\n    if discount < 0:\n        raise ValueError('negative discount')\n"
               "    return sum(items) - discount\n")
FIXED_BOTH = ("def total(items, discount=0, tax=0):\n    if discount < 0:\n        raise ValueError('negative discount')\n"
              "    return sum(items) - discount + tax\n")
BUILD_FILES = {"stock.py": RUN_STOCK,
               "tests/test_discount.py": "from stock import total\n\n\ndef test_discount():\n"
                                         "    assert total([5], discount=1) == 4\n"}
PLAN_COMMANDS = ["./shots.sh shelf", "./shots.sh till"]
PLAN_TEXT = (f"# Plan: {TITLE}\n\n## Acceptance Criteria\n\n- the shelf shows the discount\n\n## Commands\n\n"
             + "".join(f"- `{command}` — a screenshot for the review\n" for command in PLAN_COMMANDS))
SHOTS_OPTION = {"id": "shots", "kind": "image", "glob": "shots/*.png", "maxFiles": 2, "status": "stable"}


def _entry(module: str, call: str) -> str:
    """A script of adws/ as `al gen` writes it, importing this checkout's runtime."""
    return ("import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(RUNTIME)!r})\n"
            f"from adw_modules import {module}\n"
            f"sys.exit({module}.{call})\n")


ASSIGN_ENTRY = _entry("assign", "main(Path(__file__).resolve().parent.parent, sys.argv[1:])")
DEMO_ENTRY = _entry("runner", 'main("demo", Path(__file__).resolve().parent.parent, sys.argv[1:])')


def assign_layer(make_layer, app_repo: Path, *, keep: str = "on-failure", test_run: str = "test ! -e broken.flag",
                 timeout: int = 30, patch: bool = True, changes: str = "", **options: Any) -> Path:
    """The layer of the module docstring. `patch=False` gives a review step without a patch round;
    `changes="layer"` makes `demo` a layer-change workflow."""
    review_step: dict[str, Any] = {"name": "review", "kind": "review"}
    if patch:
        review_step.update(patch="patch", implement="implement")
    demo: dict[str, Any] = {
        "name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
        "classes": ["chore", "bug", "feature", "patch"], "steps": [
            {"name": "plan", "kind": "run", "command": "plan"},
            {"name": "build", "kind": "run", "command": "build"},
            {"name": "test", "kind": "check", "cmd": "test", "resolve": "resolve-test", "retries": 1},
            review_step,
            {"name": "ship", "kind": "ship"}]}
    if changes:
        demo["changes"] = changes
    extra = {
        "artefacts": [dict(SHOTS_OPTION)],
        "commands": [{"name": n, "template": t, "modelSet": "base", "output": o} for n, t, o in (
            ("plan", "plan", "path"), ("build", "build", "text"), ("review", "review", "json"),
            ("patch", "plan-patch", "text"), ("implement", "implement", "text"),
            ("resolve-test", "resolve-test", "text"))],
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 1, "artefacts": ["shots"], "allowedClasses": ["chore", "bug", "feature", "patch"],
                     "blockProtectedPaths": True, "maxDiffLines": 2000, "requireChecks": ["test"], "ship": "merge",
                     "deployCmd": ""}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 27700, "portsPerRun": 0, "keep": keep, "status": "stable"}],
    }
    layer = conflict_layer(make_layer, app_repo, test_run=test_run, extra=extra, adws=[demo], **options)
    if timeout != 30:
        path = layer / "agentic-layer.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["codebase"]["commands"][0]["timeout"] = timeout
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    (layer / "adws" / "assign.py").write_text(ASSIGN_ENTRY, encoding="utf-8")
    (layer / "adws" / "adw_demo.py").write_text(DEMO_ENTRY, encoding="utf-8")
    return layer


def script(layer: Path, step: str, adw: str = "demo", **data: Any) -> Path:
    """mock/<adw>/<step>.json: what the mock agent does at that step."""
    path = layer / "mock" / adw / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def verdict(*blockers: dict[str, str], summary: str = "") -> dict[str, Any]:
    return {"success": not blockers, "summary": summary or ("the change is not right yet" if blockers else "looks right"),
            "issues": [{"severity": "blocker", **blocker} for blocker in blockers]}


def reviewers(layer: Path, *blockers: dict[str, str], adw: str = "demo", rounds: tuple[str, ...] = ("review-1", "review-2-1")) -> None:
    """The reviewers of `adw` return these blockers (none: they pass) in every round."""
    for name in rounds:
        script(layer, name, adw, output=json.dumps(verdict(*blockers)))


def plan_script(layer: Path, text: str = PLAN_TEXT) -> None:
    script(layer, "plan", layer_files={"specs/plan-shelf.md": text}, output="specs/plan-shelf.md")


def run_demo(layer: Path, run_id: str = RUN, prompt: str = PROMPT, *more: str) -> tuple[int, str]:
    """One run of `demo` through runner.main: (exit code, its output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = runner.main("demo", layer, [prompt, "--adw-id", run_id, "--class", "chore", "--decider", "off", *more])
    return code, out.getvalue()


def assign_main(layer: Path, *argv: str) -> tuple[int, str]:
    """assign.main with its output captured: (exit code, stdout + stderr)."""
    from adw_modules import assign

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = assign.main(layer, list(argv))
    return code, out.getvalue() + err.getvalue()


def record(layer: Path, run_id: str = RUN) -> dict[str, Any]:
    return json.loads((layer / "agents" / run_id / "assign.json").read_text(encoding="utf-8"))


def write_attempts(layer: Path, attempts: list[dict[str, Any]], run_id: str = RUN) -> None:
    """agents/<run>/assign.json with these attempts (each completed with the fields give() writes)."""
    path = layer / "agents" / run_id / "assign.json"
    full = [{"n": index, "at": f"2026-11-17T10:0{index}:00Z", "by": "dashboard", "who": "", "kind": "blockers",
             "note": "", "run": "", "reason": "", "outcome": "stopped", "after": "", "why": "", **attempt}
            for index, attempt in enumerate(attempts, start=1)]
    earlier = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    path.write_text(json.dumps({"version": 1, "approvals": earlier.get("approvals", []), "attempts": full}),
                    encoding="utf-8")


# ---------------------------------------------------------------- one stopped run per row

def conflict_run(make_layer, app_repo: Path, **options: Any) -> tuple[Path, str]:
    """The review passed; `main` moved and conflicts with the run's branch."""
    layer = assign_layer(make_layer, app_repo, **options)
    handed_over_run(layer, app_repo, RUN)
    move_main(app_repo)
    return layer, RUN


def rejected_run(make_layer, app_repo: Path, moved: bool = False, blockers: tuple[dict[str, str], ...] = (BLOCKER,),
                 build: dict[str, str] | None = None, **options: Any) -> tuple[Path, str]:
    """The reviewers return a blocker in every round: the run ends handed over, its review rejected.
    With `moved`, `main` then gains a commit that conflicts in stock.py."""
    layer = assign_layer(make_layer, app_repo, **options)
    plan_script(layer)
    script(layer, "build", raw_files=dict(BUILD_FILES if build is None else build))
    reviewers(layer, *blockers)
    code, out = run_demo(layer)
    assert code == 0 and state_of(layer, RUN)["status"] == "handed-over", out
    if moved:
        move_main(app_repo, {name: content for name, content in MAIN_FILES.items() if name != "INDEX.md"})
    return layer, RUN


def proof_run(make_layer, app_repo: Path, found: bool = True, left_out: bool = False, **options: Any) -> tuple[Path, str]:
    """The only blocker is missing proof. `found`: the branch holds proof/shelf.png, which no
    artefact collects. `left_out`: the worktree is kept and holds more screenshots than the limit."""
    files = dict(BUILD_FILES)
    if found:
        files["proof/shelf.png"] = "a picture of the shelf\n"
    if left_out:
        options.setdefault("keep", "always")
        files.update({f"shots/{name}.png": f"{name}\n" for name in ("a-shelf", "b-till", "c-door")})
    return rejected_run(make_layer, app_repo, blockers=(PROOF_BLOCKER,), build=files, **options)


def person_run(make_layer, app_repo: Path, **options: Any) -> tuple[Path, str]:
    """Two blockers: an ordinary one and one only a person may decide."""
    return rejected_run(make_layer, app_repo, blockers=(BLOCKER, PERSON_BLOCKER), **options)


def failed_check_run(make_layer, app_repo: Path, **options: Any) -> tuple[Path, str]:
    """The check `test` exits 1 after its retries (the build left broken.flag)."""
    layer = assign_layer(make_layer, app_repo, **options)
    script(layer, "build", raw_files={**BUILD_FILES, "broken.flag": "the build left this\n"})
    code, out = run_demo(layer)
    assert code == 1 and state_of(layer, RUN)["status"] == "failed", out
    return layer, RUN


def login_run(make_layer, app_repo: Path, **options: Any) -> tuple[Path, str]:
    """The build step fails because the agent's login was refused."""
    layer = assign_layer(make_layer, app_repo, **options)
    script(layer, "build", raw_files=dict(BUILD_FILES), fail=LOGIN)
    code, out = run_demo(layer)
    assert code == 1, out
    return layer, RUN


def time_limit_run(make_layer, app_repo: Path, **options: Any) -> tuple[Path, str]:
    """The check `test` hits its time limit twice."""
    layer = assign_layer(make_layer, app_repo, test_run="sleep 20", timeout=1, **options)
    script(layer, "build", raw_files=dict(BUILD_FILES))
    code, out = run_demo(layer)
    assert code == 1 and "timed out twice" in out, out
    return layer, RUN


def died_run(make_layer, app_repo: Path, **options: Any) -> tuple[Path, str]:
    """A run whose process died at the build step: state.json still says running, the pid is dead,
    the heartbeat is old, the worktree is there."""
    layer, run_id = login_run(make_layer, app_repo, **options)
    child = subprocess.Popen(["true"])
    child.wait()
    path = layer / "agents" / run_id / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for step in data["steps"]:
        if step["status"] == "failed":
            step.update(status="running", detail="", ended="")
    data.update(status="running", ended="", pid=child.pid, host=os.uname().nodename,
                heartbeat="2026-11-16T09:00:00+00:00", ship={})
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    assert Path(data["worktree"]).is_dir()
    return layer, run_id


def nothing_changed_run(make_layer, app_repo: Path, **options: Any) -> tuple[Path, str]:
    """The first agent step fails before any file is written."""
    layer = assign_layer(make_layer, app_repo, **options)
    script(layer, "plan", fail="the agent returned nothing")
    code, out = run_demo(layer)
    state = state_of(layer, RUN)
    assert code == 1 and str(state["ship"].get("handoff_skipped", "")).startswith("nothing changed"), out
    return layer, RUN


def fix_scripts(layer: Path, stock: str = FIXED_STOCK, passes: bool = True) -> None:
    """The mock agent of an assigned `blockers` run: address-patch plans, address-implement writes
    the fix, the reviewer passes (or rejects again)."""
    script(layer, "address-patch", "integrate", output="plan: reject a negative discount")
    script(layer, "address-implement", "integrate", raw_files={"stock.py": stock}, output="implemented")
    if passes:
        reviewers(layer, adw="integrate")
    else:
        reviewers(layer, BLOCKER, adw="integrate")
