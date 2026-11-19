"""Bring the target branch's new commits into the run before its review and before ship
(steps `freshen-review` and `freshen-ship`, recorded only when the target moved).

A review of a tree that no longer applies to the target ends in a hand-over however it went. So
before the review, and again before ship, the run looks at the target branch
(`layer.default_branch`; fetched first when a forge remote exists and the run is not a dry run):

- the target did not move: nothing happens (no step is recorded);
- it moved, but none of its new commits touch a file the run changed: recorded, nothing merged;
- it touches the run's files and merges cleanly: merged (`merge <branch> into <run branch>`), and
  the checks that ran run again;
- it conflicts, before the review: the conflicts are resolved with the integrate machinery's
  `resolve-conflicts` command (agent `freshen-resolve`); an undecidable file or a marker left
  stops the run (`target-moved: …; party: person`) with the merge aborted;
- it conflicts, before ship: the run stops the same way without resolving (a resolution would need
  a new review): `al integrate LAYER RUN` brings it in with a review of its own.

Sandboxed runs use git only through delivery.tree_git / gitsafe, as delivery does; nothing is pushed.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any

from . import agents, delivery, events, gitsafe, state as state_module
from .errors import LayerError, StepFailed

IDENTITY = delivery.IDENTITY


def _step(when: str) -> str:
    return f"freshen-{when}"


def _target_head(layer: Any, state: Any, stack: Any, step: str) -> str:
    """The commit the target branch is at now ("" when it cannot be read)."""
    branch = layer.default_branch
    sandbox = getattr(stack, "sandbox", None)
    remote = delivery._remote(layer) if not getattr(state, "dry_run", False) else ""
    if remote:
        fetched = delivery.tree_git(state, stack, ["fetch", *(["--refmap="] if sandbox else []), remote, branch],
                                    step=step)
        if fetched.returncode == 0:
            result = delivery.tree_git(state, stack, ["rev-parse", "FETCH_HEAD"], step=step)
            if result.returncode == 0:
                return result.stdout.strip()
    if sandbox is not None:
        result = gitsafe.run_app(sandbox, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], step,
                                 check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    result = delivery._git(Path(layer.app_root), ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    return result.stdout.strip() if result.returncode == 0 else ""


def _count(state: Any, stack: Any, spec: str, step: str) -> int:
    out = delivery.tree_git(state, stack, ["rev-list", "--count", spec], step=step)
    text = out.stdout.strip()
    return int(text) if out.returncode == 0 and text.isdigit() else 0


def behind(layer: Any, state: Any, stack: Any) -> int:
    """How many commits of the target branch the run's HEAD does not have; 0 when unknown. Never raises."""
    if not getattr(state, "worktree", ""):
        return 0
    try:
        head = _target_head(layer, state, stack, "freshen")
        return _count(state, stack, f"HEAD..{head}", "freshen") if head else 0
    except (LayerError, StepFailed, OSError, subprocess.SubprocessError) as exc:
        print(f"[adw {state.adw_id}] freshen: the target branch cannot be read: {exc}")
        return 0


def _names(state: Any, stack: Any, args: list[str], step: str) -> set[str]:
    result = delivery.tree_git(state, stack, args, step=step)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()} if result.returncode == 0 else set()


def _brief(layer: Any, state: Any, stack: Any, branch: str, base: str, head: str, files: list[str], step: str) -> Path:
    lines = [f"# Bring {branch} into run {state.adw_id}", "", "## The request", "", state.prompt.strip() or "(empty)",
             "", "## The plan", "", state.plan or "none", "",
             f"## What {branch} changed since the run started, per conflicted file", ""]
    for name in files:
        log = delivery.tree_git(state, stack, ["log", "--format=%h %s", f"{base}..{head}", "--", name], step=step)
        lines += [f"- `{name}`:"] + [f"  - {line}" for line in log.stdout.splitlines()[:20] if line.strip()]
    lines += ["", "## Sides", "",
              f"Here `<<<<<<<` is the run's side (its HEAD) and `>>>>>>>` is {branch}'s. Keep both intents; "
              "change nothing else. Do not commit: the runtime commits the merge."]
    path = state_module.run_dir(layer.layer_dir, state.adw_id) / "freshen-brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _rerun_checks(layer: Any, state: Any, adw: dict[str, Any], stack: Any) -> list[str]:
    from . import steps

    rerun = []
    for check in delivery._checks_that_ran(state, adw):
        steps.run_check_with_resolve(layer, state, check, stack)
        rerun.append(str(check["name"]))
    return rerun


def _stop(layer: Any, state: Any, step: str, files: list[str], branch: str, record: dict[str, Any]) -> None:
    text = (f"target-moved: {', '.join(files)} conflict with {branch}; party: person: "
            f"al integrate LAYER {state.adw_id}")
    record.update(stopped=text)
    state.proof_plan["stopped"] = {"at": step, "outcome": "target-moved", "party": "person", "reason": text,
                                   "approvals": [], "question": ""}
    from . import convergence

    convergence.record_summary(state)
    state.save(layer.layer_dir)
    events.emit("freshen.result", step=step, merged=False, conflicts=files, stopped=True)
    raise StepFailed(step, text)


def _resolve(layer: Any, state: Any, stack: Any, step: str, branch: str, base: str, head: str,
             files: list[str]) -> dict[str, Any]:
    """Let the resolve-conflicts command resolve a merge in progress; StepFailed-free: returns
    {report, remaining} and leaves the merge for the caller to commit or abort."""
    from . import integrate, templates

    command, text = integrate.resolve_command(layer)
    brief = _brief(layer, state, stack, branch, base, head, files, step)
    args = [str(brief), json.dumps([{"file": name, "generated": False, "regenerate": ""} for name in files])]
    prompt = templates.render(text, agents.template_variables(layer, state), args)
    output = agents.run_prompt(layer, state, "freshen-resolve", command, prompt,
                               str(command.get("modelSet") or "heavy"), stack, tier_from="step", args=args)
    state.outputs["freshen-resolve"] = output
    try:
        report = integrate._check_report(agents.extract_json(output, "freshen-resolve"))
    except StepFailed:
        report = {"resolved": [], "regenerated": [], "undecidable": []}
    reported = {item["file"] for item in report["resolved"] + report["regenerated"]}
    if not report["undecidable"]:
        delivery.tree_git(state, stack, ["add", "--", *[n for n in files if n in reported]], step=step)
    remaining = sorted({name for name in files if name not in reported} | set(delivery._unmerged(state, stack))
                       | set(integrate._markers(Path(state.worktree), files)))
    return {"report": report, "remaining": remaining}


def run(layer: Any, state: Any, adw: dict[str, Any], stack: Any, when: str) -> dict[str, Any]:
    """The step `freshen-review` or `freshen-ship` (see the module docstring)."""
    from . import ship

    step = _step(when)
    branch = layer.default_branch
    record: dict[str, Any] = {"branch": branch, "behind": 0, "touched": [], "merged": False, "conflicts": []}
    state.proof_plan.setdefault("freshen", {})[when] = record
    head = _target_head(layer, state, stack, step)
    if not head:
        record["note"] = f"{branch} cannot be read"
        return record
    record["head"] = head
    record["behind"] = _count(state, stack, f"HEAD..{head}", step)
    if record["behind"] == 0:
        return record
    base_result = delivery.tree_git(state, stack, ["merge-base", "HEAD", head], step=step)
    base = base_result.stdout.strip() if base_result.returncode == 0 else ""
    if not base:
        record["note"] = f"no merge base with {branch}"
        return record
    target_files = _names(state, stack, ["diff", "--name-only", "--no-renames", base, head], step)
    run_files = (_names(state, stack, ["diff", "--name-only", "--no-renames", base, "HEAD"], step)
                 | _names(state, stack, ["diff", "--name-only", "--no-renames", "HEAD"], step)
                 | _names(state, stack, ["ls-files", "--others", "--exclude-standard"], step))
    record["touched"] = sorted(target_files & run_files)
    if not record["touched"]:
        events.say(f"[adw {state.adw_id}] {step}: {branch} moved {record['behind']} commit(s); none touches the "
                   "run's files: nothing merged", "freshen.result", step=step, merged=False, touched=[])
        state.save(layer.layer_dir)
        return record
    sandbox = getattr(stack, "sandbox", None)
    with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
        ship.commit_pending(Path(state.worktree), state, delivery._never_commit(layer, state), sandbox)
    files = delivery._conflicts(state, stack, head)
    record["conflicts"] = files
    message = f"merge {branch} into {state.branch or 'the run'} [adw {state.adw_id}]"
    if not files:
        delivery.tree_git(state, stack, [*IDENTITY, "merge", "--no-ff", "-m", message, head], check=True, step=step)
        record["merged"] = True
        record["checks_rerun"] = _rerun_checks(layer, state, adw, stack)
        events.say(f"[adw {state.adw_id}] {step}: merged {branch} ({record['behind']} commit(s), touching "
                   f"{', '.join(record['touched'])})", "freshen.result", step=step, merged=True,
                   touched=record["touched"])
        state.save(layer.layer_dir)
        return record
    if when != "review":
        _stop(layer, state, step, files, branch, record)
    delivery.tree_git(state, stack, [*IDENTITY, "merge", "--no-ff", "--no-commit", head], step=step)
    resolved = _resolve(layer, state, stack, step, branch, base, head, files)
    record["report"] = resolved["report"]
    if resolved["report"]["undecidable"] or resolved["remaining"]:
        delivery.tree_git(state, stack, ["merge", "--abort"], step=step)
        _stop(layer, state, step, resolved["remaining"] or files, branch, record)
    with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
        ship.commit_pending(Path(state.worktree), state, delivery._never_commit(layer, state), sandbox,
                            message=message)
    record["merged"] = True
    record["checks_rerun"] = _rerun_checks(layer, state, adw, stack)
    events.say(f"[adw {state.adw_id}] {step}: merged {branch}, {len(files)} conflict(s) resolved", "freshen.result",
               step=step, merged=True, conflicts=files)
    state.save(layer.layer_dir)
    return record
