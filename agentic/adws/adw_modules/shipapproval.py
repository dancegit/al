"""Approve & ship: the facts of a ship approval and the action a person's yes performs.

A run whose checks and review passed may still be handed to a person by the gate, for a reason that
needs nothing but that person's yes: the diff is larger than the review option allows
(`diff-too-large:N>M`), its class is not one the gate ships (`class-not-allowed:CLASS`), the gate
asked for a person (`needs-person`), or the ship itself failed and fell back to a pull request
(`ship-failed:…`). `facts(layer, state)` says whether such a stop can be approved and what exactly a
person approves: the reasons, the commit the gate judged, the diff size and the changed files, the
target and the state of every check. `perform` records the approval and acts.

Nothing is offered (the first that applies) when: the review rejected the run; a decision is open
that is a person's only or needs its own approval (a spending limit, a workflow that changes the
layer, a `REQUIRES HUMAN` blocker, an integration that waits); the run's branch is gone or is no
longer the commit the gate judged; a check is failed, not run or stale; or, for a merge, the target
moved since the gate judged the run or holds the work already.

The ways, the first that applies:

    pr       the run has a pull request (state.ship.pr, or a URL a ship script printed): the run is
             settled as `shipped` to it; a person or the forge merges it
    merge    the target is a stage branch agents may merge into, and neither the codebase's default
             branch nor the final stage: the stage branch is fast-forwarded to the judged commit and
             the run is settled as `landed`
    forge    the forge takes pull requests: one is reused or opened, then as `pr`
    branch   everything else (the default branch, a stage only a person merges into): the run is
             settled as `shipped` by its commit; a person merges the branch

The approval is recorded in `state.approvals` as `ship:<reason>` with who, via and the note
(approvals.record, without the `gate` mark), the settlement in agents/<run>/settlement.json, and the
events `approval.granted` and `ship.approved` in the run's events. Nothing here asks the gate again:
it would answer the same, and the approval is the decision it asked for. A merge is fast-forward
only (`git push . COMMIT:refs/heads/TARGET`, the command ship.py uses), so what lands is exactly the
commit the gate judged, and the default branch is never moved.

Standard library only.
"""
from __future__ import annotations

import contextlib
import io
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from . import approvals, assign, delivery, diskuse, events, integrate, state as state_module
from .errors import LayerError, StepFailed

PREFIXES = ("diff-too-large:", "class-not-allowed:", "ship-failed:", "needs-person:")
EXACT = ("needs-person",)
WAYS = ("merge", "pr", "forge", "branch")
LABELS = {"merge": "Approve & merge", "pr": "Approve & ship", "forge": "Approve & ship", "branch": "Approve & ship"}
MAX_FILES = 20
PR_URL = re.compile(r"https?://[^\s<>\"')\]]+/(?:pull|pulls|merge_requests)/\d+")
REASONS = ("checks-not-green", "branch-changed", "target-moved", "ship-failed")
STATES = ("passed", "failed", "not-run", "stale")
_ROW_STATES = {"stale": "stale", "failed": "failed", "not-run": "not-run", "missing": "not-run",
               "files-missing": "not-run"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------- the facts

def claims(reasons: Any) -> bool:
    """True when the gate's reasons are all ones a person's yes answers (and there is one)."""
    found = [str(reason) for reason in reasons or []]
    return bool(found) and all(reason in EXACT or reason.startswith(PREFIXES) for reason in found)


def token_of(reason: str) -> str:
    """The approval token of a gate reason: `ship:<reason>`, or `ship:<its word>` when the reason
    is not a token (a `ship-failed:` reason holds spaces)."""
    reason = str(reason)
    token = "ship:" + reason
    return token if approvals.TOKEN.fullmatch(token) else "ship:" + reason.split(":", 1)[0]


def gate_reasons(state: Mapping[str, Any]) -> list[str]:
    """The reasons the gate and the ship step gave, each once, in their order."""
    ship = _dict(state.get("ship"))
    return list(dict.fromkeys(str(reason) for reason in [*_list(_dict(ship.get("gate")).get("reasons")),
                                                         *_list(ship.get("reasons"))]))


def _steps(layer: Any, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    adw = integrate._base_adw(layer, state) or {}
    return [step for step in adw.get("steps") or [] if isinstance(step, dict)]


def _required(layer: Any, state: Mapping[str, Any]) -> list[str]:
    """The checks the run's review option has the gate run again (none for a review that is not auto)."""
    adw = integrate._base_adw(layer, state) or {}
    try:
        option = layer.option("reviews", str(adw.get("review") or ""))
    except LayerError:
        return []
    if option.get("kind") != "auto":
        return []
    return [str(name) for name in option.get("requireChecks") or []]


def checks_of(layer: Any, state: Mapping[str, Any], commit_tree: str, branch_ok: bool) -> list[dict[str, str]]:
    """One {name, phase: check | gate, state: passed | failed | not-run | stale} per check of the run:
    the check steps of its workflow that apply to its class, then the checks its gate runs again."""
    klass = str(state.get("klass") or "")
    recorded = _dict(state.get("checks"))
    found: list[dict[str, str]] = []
    names: list[set[str]] = []
    for step in _steps(layer, state):
        if step.get("kind") != "check" or str(step.get("when") or "always") not in ("always", f"class-{klass}"):
            continue
        name = str(step.get("name") or "")
        entry = recorded.get(name)
        if not isinstance(entry, dict):
            said = "not-run"
        elif entry.get("exit") != 0:
            said = "failed"
        else:
            said = "passed"
        found.append({"name": name, "phase": "check", "state": said})
        names.append({name, str(step.get("cmd") or name)})
    results = [entry for entry in _list(state.get("check_results"))
               if isinstance(entry, dict) and entry.get("phase") == "gate"]
    for name in _required(layer, state):
        last = next((entry for entry in reversed(results) if str(entry.get("check") or "") == name), None)
        if last is None:
            said = "not-run"
        elif last.get("outcome") == "failed" or last.get("exit") not in (0, None):
            said = "failed"
        elif last.get("tree") and str(last.get("tree")) != commit_tree:
            said = "stale"
        else:
            said = "passed"
        found.append({"name": name, "phase": "gate", "state": said})
        names.append({name})
    for row in _list(_dict(_dict(state.get("proof_plan")).get("pack")).get("rows")):
        turned = _ROW_STATES.get(str(row.get("state") or "")) if isinstance(row, dict) else None
        if not turned:
            continue
        for check, known in zip(found, names):
            if str(row.get("check") or "") in known and check["state"] != "failed":
                check["state"] = turned
    if not branch_ok:
        for check in found:
            if check["state"] == "passed":
                check["state"] = "stale"
    return found


def check_text(check: Mapping[str, Any]) -> str:
    """`NAME STATE`, a gate check as `NAME (gate) STATE`."""
    return f"{check.get('name')}{' (gate)' if check.get('phase') == 'gate' else ''} {check.get('state')}"


def pull_request(layer: Any, state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run's pull request, {number, url}: the one its ship step recorded, else the last pull
    request URL a script step after the ship step printed (a ship script that opened it); or None."""
    recorded = _dict(state.get("ship")).get("pr")
    if isinstance(recorded, dict) and (recorded.get("url") or recorded.get("number")):
        number = recorded.get("number")
        return {"number": number if isinstance(number, int) and not isinstance(number, bool) else None,
                "url": str(recorded.get("url") or "")}
    outputs = _dict(state.get("outputs"))
    shipped, url = False, ""
    for step in _steps(layer, state):
        if step.get("kind") == "ship":
            shipped = True
        elif shipped and step.get("kind") == "script":
            hits = PR_URL.findall(str(outputs.get(str(step.get("name") or "")) or ""))
            url = hits[-1] if hits else url
    return {"number": None, "url": url} if url else None


def _way(layer: Any, state: Mapping[str, Any], found_pr: Mapping[str, Any] | None) -> str:
    from . import ship as ship_module

    if found_pr:
        return "pr"
    recorded = _dict(state.get("delivery"))
    branch = str(recorded.get("branch") or "")
    if branch and recorded.get("agents_may_merge") is not False and branch != layer.codebase_default_branch:
        declared = delivery.stages(layer)
        if not declared:
            return "merge"
        final = delivery.final_stage(declared)
        stage = next((item for item in declared if item.name == str(recorded.get("stage") or "")), None)
        if (stage is not None and stage.agents_may_merge and final is not None and stage.name != final.name
                and branch != final.branch):
            return "merge"
    try:
        if delivery.pull_requests(layer) and ship_module._forge_client(delivery.ship_layer(layer)) is not None:
            return "forge"
    except LayerError:
        pass
    return "branch"


def facts(layer: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """What a person approves for this handed-over run, and whether it can be approved: {approvable,
    why, reason, reasons, way, label, target, pr, commit, tree, diff_lines, files, files_total,
    checks}. `why` says what refuses it and `reason` is its word (one of REASONS, or "not-waiting").
    Only reads; `state` is the mapping of integrate._state."""
    ship = _dict(state.get("ship"))
    gate = _dict(ship.get("gate"))
    app = Path(layer.app_root)
    branch = str(state.get("branch") or "")
    commit = str(ship.get("commit") or "")
    target = integrate._target_branch(layer, state)
    found_pr = pull_request(layer, state)
    way = _way(layer, state, found_pr)
    changed = [str(path) for path in _list(gate.get("changed_files"))]
    lines = gate.get("diff_lines")
    found: dict[str, Any] = {
        "approvable": True, "why": "", "reason": "", "reasons": gate_reasons(state), "way": way,
        "label": LABELS[way], "target": target, "pr": found_pr, "commit": commit, "tree": "",
        "diff_lines": lines if isinstance(lines, int) and not isinstance(lines, bool) else 0,
        "files": changed[:MAX_FILES], "files_total": len(changed), "checks": []}

    def no(why: str, reason: str = "not-waiting") -> dict[str, Any]:
        if found["approvable"]:
            found.update(approvable=False, why=why, reason=reason)
        return found

    if integrate._rejected(state):
        return no("its review rejected it")
    for decision in assign.decisions(layer, state):
        if not decision["approved"]:
            return no(f"a person's only: {decision['text']}")
    head = integrate._ref(app, f"refs/heads/{branch}") if branch and commit else ""
    if not head:
        return no(f"branch {branch or '(none)'} does not exist")
    judged = integrate._ref(app, commit)
    branch_ok = bool(judged) and head == judged
    out = integrate._app_git(app, ["rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}"])
    found["tree"] = out.stdout.strip() if out.returncode == 0 else ""
    found["checks"] = checks_of(layer, state, found["tree"], branch_ok)
    if not branch_ok:
        return no(f"a person changed {branch} since the gate judged it ({head[:7]} is not {commit[:7]})",
                  "branch-changed")
    red = [check for check in found["checks"] if check["state"] != "passed"]
    if red:
        return no("checks are not all green: " + ", ".join(check_text(check) for check in red), "checks-not-green")
    if way == "merge":
        target_head = integrate._ref(app, f"refs/heads/{target}")
        if target_head and target_head == judged:
            return no(f"its work is on {target} already")
        if not target_head or not integrate._is_ancestor(app, target_head, judged):
            return no(f"{target} moved since the gate judged this run: give it to an agent, or merge it by hand",
                      "target-moved")
    return found


# ---------------------------------------------------------------- the action

def write_settlement(layer: Any, run_id: str, as_: str, by: str, who: str, via: str, note: str) -> dict[str, Any]:
    """agents/<run>/settlement.json in the dashboard's format (kit/dashboard/dashboard.py
    write_settlement; keep the two alike): {as, by, note, who, at, via}, the earlier record kept
    under "previous" (one level), "pruned" carried over. Then the run's worktree goes
    (diskuse.release_worktree; its branch and record stay)."""
    path = state_module.run_dir(layer.layer_dir, run_id) / integrate.SETTLEMENT_FILE
    record: dict[str, Any] = {"as": as_, "by": by, "note": note, "who": who, "at": integrate._stamp(), "via": via}
    earlier = integrate._read_json(path)
    if isinstance(earlier, dict) and earlier.get("as"):
        record["previous"] = {k: v for k, v in earlier.items() if k not in ("previous", "pruned")}
        if earlier.get("pruned"):
            record["pruned"] = earlier["pruned"]
    integrate._write_private(path, record)
    with contextlib.suppress(OSError, subprocess.SubprocessError):   # settling never fails because of a worktree
        diskuse.release_worktree(layer.layer_dir, layer.app_root, run_id, integrate._tree_dir(layer))
    return record


def _fast_forward(layer: Any, target: str, commit: str) -> tuple[str, str]:
    """Move `target` to `commit`, fast-forward only (git refuses anything else, and the StepFailed
    goes to the caller): ("merged", target), or ("staged", "al/ship/<target>") when the target is
    checked out in a working tree, which git does not let a push move."""
    from . import ship as ship_module

    app = Path(layer.app_root)
    if ship_module._default_checked_out(app, target):
        ship_module._git(app, ["push", ".", f"{commit}:refs/heads/al/ship/{target}"])
        return "staged", f"al/ship/{target}"
    ship_module._git(app, ["push", ".", f"{commit}:refs/heads/{target}"])
    return "merged", target


def _open_pr(layer: Any, state: Any, target: str) -> dict[str, Any]:
    """The run's branch pushed and its pull request into `target`: an open one is reused, else one
    is opened. {number, url}."""
    from . import ship as ship_module

    client = ship_module._forge_client(delivery.ship_layer(layer))
    if client is None:
        raise LayerError("the layer has no forge that takes pull requests")
    branch = str(state.branch)
    ship_module._git(Path(layer.app_root), ["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"])
    found = next((item for item in client.find_prs(branch, target)
                  if isinstance(item, dict) and item.get("state") == "open"), None)
    if found is None:
        found = client.create_pr(branch, target, ship_module._title(state, layer),
                                 ship_module.pr_body(layer, state, ""))
    if not isinstance(found, dict) or not (found.get("url") or found.get("number")):
        raise StepFailed("ship", f"the forge answered an unexpected value for the pull request: {found!r}")
    return {"number": found.get("number"), "url": str(found.get("url") or "")}


def perform(layer: Any, run_id: str, found: Mapping[str, Any], who: str, via: str, note: str) -> dict[str, Any]:
    """Record the person's approval of what `found` shows (the `ship` object of the derivation that
    was just made), then act by its way: {status: merged | shipped | refused, reason, message,
    run_id, settled: landed | shipped | ""}. A failing action keeps the approval and settles nothing."""
    way = str(found.get("way") or "")
    target = str(found.get("target") or "")
    commit = str(found.get("commit") or "")
    found_pr = _dict(found.get("pr"))
    url = str(found_pr.get("url") or "")
    tokens = list(dict.fromkeys(token_of(reason) for reason in found.get("reasons") or []))
    status, reason, settled, result = "shipped", "", "shipped", "shipped"
    with events.recording(layer.layer_dir, run_id):
        state = state_module.load(layer.layer_dir, run_id)
        branch = str(state.branch)
        approvals.record(state, tokens, who, via, note=note)
        state.save(layer.layer_dir)
        events.emit("approval.granted", what=tokens, who=who, via=via, ship=True, **({"note": note} if note else {}))
        try:
            if way == "merge":
                result, moved = _fast_forward(layer, target, commit)
                if result == "merged":
                    from . import batch

                    state.ship.update(shipped=True, target=target, approved_by=who)
                    state.save(layer.layer_dir)
                    with contextlib.redirect_stdout(io.StringIO()):     # their progress lines are a run's
                        if state.delivery:
                            delivery.after_ship(layer, state)
                        batch.after_landing(layer, state)
                    state.save(layer.layer_dir)
                    write_settlement(layer, run_id, "landed", commit, who, via, note)
                    status, settled = "merged", "landed"
                    message = f"approved by {who}: {branch} is merged into {target} ({commit[:7]})"
                else:
                    write_settlement(layer, run_id, "shipped", commit, who, via, note)
                    message = (f"approved by {who}: {target} is checked out in a working tree, so the result is on "
                               f"{moved}; run `git merge --ff-only {moved}` in the application")
            elif way in ("pr", "forge"):
                if way == "forge":
                    opened = _open_pr(layer, state, target)
                    url = opened["url"]
                    state.ship["pr"] = opened
                    state.save(layer.layer_dir)
                write_settlement(layer, run_id, "shipped", url or commit, who, via, note)
                message = (f"approved by {who}: shipped to {url or 'its pull request'}; it is listed as landed once "
                           "git shows it")
            elif way == "branch":
                write_settlement(layer, run_id, "shipped", commit, who, via, note)
                message = f"approved by {who}: {branch} ({commit[:7]}) is ready for {target}; a person merges it"
            else:
                raise LayerError(f"unknown way to ship {way!r}")
        except Exception as exc:  # noqa: BLE001 — StepFailed, LayerError, OSError, a forge error: the approval stays
            why = integrate.first_line(exc)[:400]
            status, reason, settled, result = "refused", "ship-failed", "", "failed"
            message = f"the approval is recorded, but {why}"
        events.emit("ship.approved", way=way, result=result, target=target, commit=commit, pr_url=url, who=who,
                    via=via, why="" if status != "refused" else message)
    return {"status": status, "reason": reason, "message": message, "run_id": run_id, "settled": settled}
