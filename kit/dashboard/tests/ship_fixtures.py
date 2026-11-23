"""A handed-over run whose gate only asked for a person's yes (`diff-too-large:3148>400` and the like),
written as files and git branches: what "Approve & ship" / "Approve & merge" is offered for.

`add_ship_run` gives a layer such a run: its branch `chore/<id>-ship-fixture` with one commit cut from
the default branch, the stage branch `integration` (way `merge`), and agents/<id>/state.json as the
runtime leaves it (green checks, a passed review, the gate's reasons, the diff size and the files).
The workflow, its check steps and the checks its gate runs again are read from the layer's own
agentic-layer.json, so the same run fits every test layer. `record` writes the matching
agents/<id>/approvable.json (kind `ship`, as adw_modules/approvable.py derives it; JSON only here) for
the tests that do not start the layer's launcher. Every name is invented."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DIFF = "diff-too-large:3148>400"
PR_URL = "https://forge.example/o/r/pull/7"
STAGE = "integration"
LABELS = {"merge": "Approve & merge", "pr": "Approve & ship", "forge": "Approve & ship", "branch": "Approve & ship"}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _ir(layer: Path) -> dict[str, Any]:
    return json.loads((layer / "agentic-layer.json").read_text(encoding="utf-8"))


def workflow(layer: Path, name: str = "") -> dict[str, Any]:
    """The workflow the run is of: `name`, else the first one with a ship step, else the first."""
    adws = [adw for adw in _ir(layer).get("adws") or [] if isinstance(adw, dict)]
    if name:
        return next(adw for adw in adws if adw["name"] == name)
    with_ship = [adw for adw in adws if any(step.get("kind") == "ship" for step in adw.get("steps") or [])]
    return (with_ship or adws)[0]


def check_names(layer: Path, adw: dict[str, Any], klass: str = "chore") -> tuple[list[str], list[str]]:
    """(the check steps of the workflow that apply to the class, the checks its gate runs again)."""
    steps = [str(step["name"]) for step in adw.get("steps") or []
             if step.get("kind") == "check" and str(step.get("when") or "always") in ("always", f"class-{klass}")]
    option = next((item for item in _ir(layer).get("reviews") or [] if item.get("id") == adw.get("review")), {})
    required = [str(name) for name in option.get("requireChecks") or []] if option.get("kind") == "auto" else []
    return steps, required


def add_ship_run(layer: Path, repo: Path, run_id: str, *, reasons: tuple[str, ...] | list[str] = (DIFF,),
                 way: str = "merge", checks_exit: int = 0, pr_url: str = "", adw: str = "",
                 title: str = "Give the shelf a label") -> dict[str, Any]:
    """The run of the module docstring → its state. `way`: merge (a stage branch agents may merge
    into), pr (a pull request at `pr_url`, default PR_URL) or branch (the default branch: a person
    merges). `checks_exit` is the exit code of its check records."""
    flow = workflow(layer, adw)
    default = str(_ir(layer)["codebase"].get("defaultBranch") or "main")
    branch = f"chore/{run_id}-ship-fixture"
    tree = Path(tempfile.mkdtemp(prefix="ship-", dir=repo.parent))
    tree.rmdir()
    git(repo, "worktree", "add", "-q", "-b", branch, str(tree), default)
    name = f"shelf_{run_id}.py"
    (tree / name).write_text("SHELF = 1\n", encoding="utf-8")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", f"chore: {title} [adw {run_id}]")
    commit = git(tree, "rev-parse", "HEAD")
    git(repo, "worktree", "remove", "--force", str(tree))
    if way == "merge" and subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                                          f"refs/heads/{STAGE}"], capture_output=True, timeout=60).returncode != 0:
        git(repo, "branch", STAGE, default)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started, ended = (now - timedelta(minutes=12)).isoformat(), (now - timedelta(minutes=2)).isoformat()
    steps, required = check_names(layer, flow)
    reasons = [str(reason) for reason in reasons]
    verdict = {"success": True, "summary": "fine", "issues": []}
    pr = {"number": 7, "url": pr_url or PR_URL} if way == "pr" else None
    state: dict[str, Any] = {
        "adw_id": run_id, "adw": flow["name"], "prompt": f"{title}\n\nThe shelf gets a label.", "source": "cli",
        "klass": "chore", "class_fixed": True, "status": "handed-over", "branch": branch, "worktree": "",
        "plan": "", "started": started, "ended": ended,
        "steps": [{"name": str(step["name"]), "kind": str(step.get("kind") or "run"), "status": "passed",
                   "attempts": 1, "started": started, "ended": ended, "detail": ""} for step in flow["steps"]],
        "checks": {step: {"cmd": "true", "exit": checks_exit, "outcome": "passed" if checks_exit == 0 else "failed",
                          "tree": "", "log": ""} for step in steps},
        "check_results": [{"check": check, "step": "ship", "phase": "gate",
                           "outcome": "passed" if checks_exit == 0 else "failed", "exit": checks_exit, "tree": ""}
                          for check in required],
        "review": {"verdict": verdict, "rounds": [{"round": 1}], "mode": "auto"},
        "ship": {"action": "pr", "shipped": False, "commit": commit, "reasons": reasons, "handed_over": reasons,
                 "target": pr["url"] if pr else f"agents/{run_id}/PR.md", "pr": pr,
                 "gate": {"action": "pr", "ship": False, "reasons": reasons, "diff_lines": 3148,
                          "changed_files": [name]}},
    }
    if way == "merge":
        state["delivery"] = {"stage": STAGE, "branch": STAGE, "agents_may_merge": True}
    run_dir = layer / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    (run_dir / "request.md").write_text(state["prompt"] + "\n", encoding="utf-8")
    return state


def set_checks(layer: Path, run_id: str, exit_code: int) -> None:
    """Turn every check record of the run to `exit_code` (a check that went red since)."""
    path = layer / "agents" / run_id / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for record in state["checks"].values():
        record.update(exit=exit_code, outcome="passed" if exit_code == 0 else "failed")
    for record in state["check_results"]:
        record.update(exit=exit_code, outcome="passed" if exit_code == 0 else "failed")
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def basis(layer: Path, run_id: str) -> str:
    """The same string as dashboard.approval_basis, from the run's files."""
    def read(name: str) -> dict[str, Any]:
        try:
            data = json.loads((layer / "agents" / run_id / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    state, given, integration = read("state.json"), read("assign.json"), read("integration.json")
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    count = lambda value: str(len(value)) if isinstance(value, list) else "0"  # noqa: E731
    return "|".join([str(state.get("status") or ""), str(state.get("ended") or ""), count(state.get("approvals")),
                     count(given.get("approvals")), count(given.get("attempts")), str(decision.get("status") or ""),
                     str(integration.get("status") or "")])


def record(layer: Path, run_id: str, *, ship: dict[str, Any] | None = None, with_ship: bool = True) -> dict[str, Any]:
    """Write agents/<run>/approvable.json of kind `ship` for a run of add_ship_run, as approvable.write
    does; returns the record. `ship` replaces fields of its `ship` object; `with_ship=False` leaves the
    object out (a record the page must not show)."""
    state = json.loads((layer / "agents" / run_id / "state.json").read_text(encoding="utf-8"))
    gate = state["ship"]["gate"]
    steps, required = check_names(layer, workflow(layer, state["adw"]))
    delivery = state.get("delivery") or {}
    pr = state["ship"].get("pr")
    way = "pr" if pr else "merge" if delivery else "branch"
    target = str(delivery.get("branch") or _ir(layer)["codebase"].get("defaultBranch") or "main")
    facts: dict[str, Any] = {
        "reasons": list(gate["reasons"]), "way": way, "label": LABELS[way], "target": target, "pr": pr,
        "commit": state["ship"]["commit"], "tree": "", "diff_lines": gate["diff_lines"],
        "files": list(gate["changed_files"]), "files_total": len(gate["changed_files"]),
        "checks": [*({"name": name, "phase": "check", "state": "passed"} for name in steps),
                   *({"name": name, "phase": "gate", "state": "passed"} for name in required)]}
    facts.update(ship or {})
    url = (facts["pr"] or {}).get("url") or ""
    text = (f"{', '.join(str(r) for r in facts['reasons'])}: {facts['diff_lines']} lines in {facts['files_total']} file(s) → "
            f"{url or facts['target']}; checks: {len(facts['checks'])} passed")
    items = [{"id": "ship", "kind": "ship", "text": text, "what": [f"ship:{r}" for r in facts["reasons"]],
              "paths": list(facts["files"]), "decisions": []}]
    after = {"merge": f"the run's branch is merged into {facts['target']} (fast-forward)",
             "pr": f"the run is settled as shipped to {url}",
             "forge": "the pull request is opened and the run is settled as shipped",
             "branch": f"the run is settled as shipped; a person merges {state['branch']} into {facts['target']}"}.get(
                 facts["way"], "")
    found = basis(layer, run_id)
    shown: list[Any] = [run_id, found, "", [[item["id"], item["text"]] for item in items]]
    if with_ship:
        shown.append([facts["way"], facts["target"], facts["commit"], facts["reasons"],
                      [[check["name"], check["state"]] for check in facts["checks"] if isinstance(check, dict)], url])
    data = {"version": 1, "run": run_id, "basis": found, "waiting": True, "kind": "ship", "items": items,
            "from_step": "", "after": after, "why": "",
            "token": hashlib.sha256(json.dumps(shown).encode("utf-8")).hexdigest()[:16],
            "at": "2026-11-18T10:00:00+00:00"}
    if with_ship:
        data["ship"] = facts
    (layer / "agents" / run_id / "approvable.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                                                               encoding="utf-8")
    return data
