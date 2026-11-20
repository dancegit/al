"""A layer and mock scripts that reproduce the five causes of reviews that do not converge, and the
normal case, on runner.main with the mock agent and real git (test_convergence_runs.py).

The workflow: plan (plan-feature, its plan written by a mock `layer_files` script with a Proof plan
table) → build → check unit → commit → review (auto, 2 reviewers, 2 retries, patch/implement) →
ship (pr, forge none)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ADW = "converge"
MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
UNIT_OK = "test -f feature.py && echo 'tests/test_feature.py::test_feature PASSED' && echo '==== 1 passed in 0.01s ===='"
NOTIFIER = {"id": "hook", "kind": "command", "status": "stable", "events": ["needs-human"], "timeout": 5,
            "run": "cat > /dev/null"}
TASKS = {"id": "tasks", "kind": "tasklist", "status": "stable", "path": "tasks.md", "openMarker": "[ ]",
         "runningMarker": "[~]", "doneMarker": "[x]", "failedMarker": "[!]", "blockedMarker": "[>]",
         "tagSyntax": "braces"}
STEPS = ["plan", "build", "unit", "commit", "review", "ship"]


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                            capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def converge_layer(make_layer, *, artefacts: list[dict[str, Any]] | None = None,
                   review_artefacts: list[str] | None = None, steps: list[dict[str, Any]] | None = None,
                   unit: str = UNIT_OK, protected: list[str] | None = None, tasklist: bool = False,
                   review: dict[str, Any] | None = None, commands: list[dict[str, Any]] | None = None) -> Path:
    cmds = list(commands) if commands is not None else [{"name": "unit", "run": unit, "timeout": 30}]
    commands = [{"name": n, "template": t, "modelSet": "base", "output": o, "description": "", "file": "",
                 "focus": [], "fabric": [], "relevant": [], "validation": []} for n, t, o in (
        ("plan", "plan-feature", "path"), ("build", "build", "text"), ("commit", "commit", "text"),
        ("review", "review", "json"), ("patch", "plan-patch", "text"), ("implement", "implement", "text"),
        ("resolve-conflicts", "resolve-conflicts", "text"))]
    adw = {"name": ADW, "agent": "mock", "review": "policy", "environment": ["worktree"],
           "input": ["cli", *(["tasks"] if tasklist else [])], "classes": ["feature"], "changes": "codebase",
           "steps": steps or [
               {"name": "plan", "kind": "run", "command": "plan", "args": ["$prompt"]},
               {"name": "build", "kind": "run", "command": "build", "args": ["$plan"]},
               {"name": "unit", "kind": "check", "cmd": "unit"},
               {"name": "commit", "kind": "run", "command": "commit"},
               {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"},
               {"name": "ship", "kind": "ship"}]}
    return make_layer({
        "agents": [MOCK],
        "commands": commands,
        "codebase": {"commands": cmds,
                     "paths": {"protected": list(protected or [])}},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 2,
                     "maxRetries": 2, "artefacts": list(review_artefacts or []),
                     "allowedClasses": ["chore", "bug", "feature"], "blockProtectedPaths": True,
                     "maxDiffLines": 1000, "requireChecks": [], "ship": "pr", "deployCmd": "", **(review or {})}],
        "artefacts": list(artefacts or []),
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 28000,
                          "portsPerRun": 0, "keep": "always", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "notifiers": [NOTIFIER],
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False},
                         *([TASKS] if tasklist else [])],
        "adws": [adw],
    })


def script(layer: Path, step: str, data: dict[str, Any]) -> None:
    path = layer / "mock" / ADW / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def table(rows: list[tuple[str, str, str, str, str]]) -> str:
    lines = ["| # | Criterion | Proven by | Writes | Needs |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def plan_with(layer: Path, rows: list[tuple[str, str, str, str, str]] | None, name: str = "plan-converge.md") -> None:
    """The plan step writes specs/<name> (with the Proof plan table, or without one) and outputs its path."""
    body = "# Feature: converge\n\n## How it is proven\n\n### Checkable results\n- it works\n"
    if rows is not None:
        body += "\n### Proof plan\n" + table(rows) + "\n"
    script(layer, "plan", {"layer_files": {f"specs/{name}": body}, "output": f"specs/{name}"})


def build_writes(layer: Path, files: dict[str, str] | None = None) -> None:
    script(layer, "build", {"files": {"feature.py": "FEATURE = True\n", **(files or {})}})


def reviewers(layer: Path, rounds: int = 1, verdict: dict[str, Any] | None = None, count: int = 2) -> None:
    """Every reviewer of every round answers `verdict` (a pass without issues by default)."""
    verdict = verdict or {"success": True, "summary": "the evidence shows it", "issues": []}
    for number in range(1, rounds + 1):
        for index in range(1, count + 1):
            name = f"review-{index}" if number == 1 else f"review-{number}-{index}"
            script(layer, name, {"output": json.dumps(verdict)})


C1 = ("C1", "the feature flag exists", "`unit` `tests/test_feature.py::test_feature`", "-", "none")


# ---------------------------------------------------------------- the scenarios

def proof_not_reaching(make_layer, app_repo) -> tuple[Path, dict[str, Any]]:
    """Cause 1: a row writes 30 screenshots; the image artefact hands 12 to the reviewers."""
    layer = converge_layer(make_layer, artefacts=[{"id": "shots", "kind": "image", "glob": "shots/*.png",
                                                    "maxFiles": 12, "status": "stable"}],
                           review_artefacts=["shots"])
    plan_with(layer, [C1, ("C2", "every screen is shown", "`unit`", "shots/*.png (30)", "none")])
    build_writes(layer)
    reviewers(layer)
    return layer, {}


def proof_unprovable_here(make_layer, app_repo) -> tuple[Path, dict[str, Any]]:
    """Cause 2: a row needs a tool this machine does not have."""
    layer = converge_layer(make_layer)
    plan_with(layer, [C1, ("C2", "the tool says so", "`unit`", "-", "tool:al-no-such-tool")])
    build_writes(layer)
    reviewers(layer)
    return layer, {}


def decision_for_a_person(make_layer, app_repo) -> tuple[Path, dict[str, Any]]:
    """Cause 3: a row needs a decision only a person may take (a layer word)."""
    layer = converge_layer(make_layer)
    plan_with(layer, [C1, ("C2", "the new word is used", "`unit`", "-", "approval:vocabulary")])
    build_writes(layer)
    reviewers(layer)
    return layer, {}


def no_progress(make_layer, app_repo) -> tuple[Path, dict[str, Any]]:
    """Cause 4: the run-scoped proof the plan declares is never written, by the build or any patch."""
    layer = converge_layer(make_layer)
    plan_with(layer, [C1, ("C2", "the report is written", "`unit`", "proof:report.json", "none")])
    build_writes(layer)
    script(layer, "evidence-implement-1", {"files": {"attempt.py": "FIRST_ATTEMPT = 1\n"}})
    script(layer, "evidence-implement-2", {"files": {"attempt.py": "SECOND_ATTEMPT = 2\n"}})
    reviewers(layer)
    return layer, {}


def carried(make_layer, app_repo, *, touch: str) -> tuple[Path, dict[str, Any]]:
    """Two #:when-affected checks that together cover every path: `unit` (feature.py) and `docs`
    (README.md, tests, app.py, test.sh). The first evidence patch writes the missing proof and
    `touch`, which only `docs` covers, so `unit` is decided not affected and its pass is carried;
    the second patch would write `touch` again with other content."""
    unit = {"name": "unit", "run": UNIT_OK, "timeout": 30, "covers": ["feature.py"], "always": False}
    docs = {"name": "docs", "run": "true", "timeout": 30, "covers": ["README.md", "tests", "app.py", "test.sh"],
            "always": False}
    steps = [{"name": "plan", "kind": "run", "command": "plan", "args": ["$prompt"]},
             {"name": "build", "kind": "run", "command": "build", "args": ["$plan"]},
             {"name": "unit", "kind": "check", "cmd": "unit", "whenAffected": True},
             {"name": "docs", "kind": "check", "cmd": "docs", "whenAffected": True},
             {"name": "commit", "kind": "run", "command": "commit"},
             {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"},
             {"name": "ship", "kind": "ship"}]
    layer = converge_layer(make_layer, steps=steps, commands=[unit, docs])
    plan_with(layer, [C1, ("C2", "the report is written", "`unit`", "proof:report.json", "none")])
    build_writes(layer)
    script(layer, "evidence-implement-1", {"files": {".al-proof/report.json": "{}\n", touch: "# first\n"}})
    script(layer, "evidence-implement-2", {"files": {touch: "# second\n"}})
    reviewers(layer)
    return layer, {}


def _moves_main(app_repo: Path, content: str) -> str:
    """A unit cmd that, the first time it runs, commits `content` as feature.py on main (in a throwaway
    worktree of the app; the app's checkout is not touched), then checks the run's feature.py."""
    flag = app_repo.parent / "main-moved.flag"
    tree = app_repo.parent / "main-mover"
    payload = app_repo.parent / "main-feature.py"
    payload.write_text(content, encoding="utf-8")
    return (f"if [ ! -e '{flag}' ]; then "
            f"git -C '{app_repo}' worktree add -q '{tree}' main && cp '{payload}' '{tree}/feature.py' && "
            f"git -C '{tree}' add feature.py && "
            f"git -C '{tree}' -c user.name=t -c user.email=t@example.org commit -qm 'main moves' && "
            f"git -C '{app_repo}' worktree remove --force '{tree}' && touch '{flag}' || exit 9; fi; " + UNIT_OK)


def target_moved(make_layer, app_repo, *, clean: bool = False) -> tuple[Path, dict[str, Any]]:
    """Cause 5: while the run works, main gets its own feature.py (the same content when `clean`,
    a conflicting one otherwise); the freshen-resolve mock reports nothing resolved."""
    if app_repo.is_dir():  # the app's checkout must not be main, so a worktree of main can be added
        git(app_repo, "checkout", "-q", "-b", "side")
    content = "FEATURE = True\n" if clean else "FEATURE = 'from main'\n"
    layer = converge_layer(make_layer, unit=_moves_main(app_repo, content))
    plan_with(layer, [C1])
    build_writes(layer)
    reviewers(layer)
    return layer, {}


def normal(make_layer, app_repo, *, verdict: dict[str, Any] | None = None,
           tasklist: bool = False) -> tuple[Path, dict[str, Any]]:
    """Complete evidence: the build writes the feature and its run-scoped proof; reviewers approve."""
    layer = converge_layer(make_layer, tasklist=tasklist)
    plan_with(layer, [C1, ("C2", "the report is written", "`unit`", "proof:report.json, proof:shots/*.png (3)",
                           "none")])
    build_writes(layer, {".al-proof/report.json": "{\"ok\": true}\n", ".al-proof/shots/a.png": "png-a",
                         ".al-proof/shots/b.png": "png-b", ".al-proof/shots/c.png": "png-c"})
    reviewers(layer, verdict=verdict)
    return layer, {}


def state_of(layer: Path, adw_id: str) -> dict[str, Any]:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text(encoding="utf-8"))


def agent_steps(state: dict[str, Any]) -> list[str]:
    return [str(call.get("step")) for call in state.get("agent_calls") or []]


def step_names(state: dict[str, Any]) -> list[str]:
    return [str(record.get("name")) for record in state.get("steps") or []]
