"""Decisions only a person may take, given for one run: pre-approvals and approvals after a stop.

A run may need something a person decides: a layer word (`vocabulary`), an edit of a protected
path (`edit:PATH`), a new dependency (`dependency:NAME`). A plan says so in its Proof plan table
(`approval:KIND[:ARG]`, see proofplan.py) and the run stops before the build unless the approval
was given. They come in, each recorded in `state.approvals` with who, when and how:

- at the start: `al run --approve WHAT`, the dashboard's start form, the runner's `--approve`
  (`via` "start");
- after a stop: `al approve LAYER RUN WHAT` ("cli"), the dashboard's Approve button
  ("dashboard"), the stand-in's `go` answer by mail or webhook ("answer:mail" …).

An approval never lowers the gate: a pre-approved protected-path edit still goes to a person
there. It only lets the run go on to the build and the review instead of stopping first.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Iterable

from . import events, state as state_module
from .errors import LayerError

TOKEN = re.compile(r"[a-z][a-z0-9-]*(:[^\s]{1,200})?")
GRAMMAR = ("an approval is a word of lower-case letters, digits and -, optionally followed by :ARG "
           "(1 to 200 characters, no spaces), e.g. vocabulary, edit:docs/DESIGN.md, dependency:requests")
APPROVER = "approver"


def parse(text: str) -> str:
    """The approval token `text` names; ValueError with the grammar when it is not one."""
    token = str(text or "").strip()
    if not TOKEN.fullmatch(token):
        raise ValueError(f"bad approval {token!r}: {GRAMMAR}")
    return token


def matches(approval: str, need: str) -> bool:
    """True when `approval` covers `need`: the same token, or `edit:GLOB` for `edit:PATH` when the
    glob matches the path (fnmatch) or the path lies under it (review.is_protected's rule)."""
    approval, need = str(approval), str(need)
    if approval == need:
        return True
    if approval.startswith("edit:") and need.startswith("edit:"):
        from . import review  # read only: its protected-path rule

        glob, path = approval[len("edit:"):], need[len("edit:"):]
        return fnmatch.fnmatchcase(path, glob) or review.is_protected(path, [glob])
    return False


def covered(state: Any, need: str) -> dict[str, Any] | None:
    """The recorded approval that covers `need`, or None."""
    for item in getattr(state, "approvals", None) or []:
        if isinstance(item, dict) and matches(str(item.get("what") or ""), need):
            return item
    return None


def record(state: Any, whats: Iterable[str], who: str, via: str) -> list[dict[str, Any]]:
    """Add each approval (validated; one already recorded is not added again) with a UTC `at`."""
    added = []
    known = {str(item.get("what")) for item in state.approvals if isinstance(item, dict)}
    for what in whats:
        token = parse(what)
        if token in known:
            continue
        known.add(token)
        item = {"what": token, "who": str(who or "unknown"), "at": state_module.now(), "via": str(via)}
        state.approvals.append(item)
        added.append(item)
    return added


def _refused(why: str, message: str) -> dict[str, Any]:
    return {"status": "refused", "reason": why, "message": message}


def _layer(layer: Any) -> Any:
    if isinstance(layer, (str, Path)):
        from . import ir

        return ir.load(Path(layer))
    return layer


def grant(layer: Any, adw_id: str, whats: Iterable[str], who: str, via: str) -> dict[str, Any]:
    """Approve `whats` for a run that stopped for an approver. {status: granted | refused, reason,
    message, state}; a refusal changes nothing."""
    layer = _layer(layer)
    adw_id = str(adw_id or "")
    if not re.fullmatch(r"[0-9a-fA-F]{8}", adw_id):
        return _refused("unknown-run", f"no run {adw_id!r} (a run id is 8 hexadecimal characters)")
    try:
        tokens = [parse(what) for what in whats]
    except ValueError as exc:
        return _refused("bad-approval", str(exc))
    if not tokens:
        return _refused("bad-approval", "name at least one approval")
    try:
        state = state_module.load(layer.layer_dir, adw_id)
    except LayerError:
        return _refused("unknown-run", f"no run {adw_id} in this layer")
    stopped = (state.proof_plan or {}).get("stopped") or {}
    if state.status != "failed" or stopped.get("party") != APPROVER:
        return _refused("not-stopped-for-an-approver",
                        f"run {adw_id} is {state.status}"
                        + (f", stopped for the {stopped.get('party')}" if stopped.get("party") else "")
                        + "; only a run stopped for an approver takes an approval")
    added = record(state, tokens, who, via)
    state.save(layer.layer_dir)
    with events.recording(layer.layer_dir, adw_id):
        events.emit("approval.granted", what=tokens, who=str(who or ""), via=str(via))
    return {"status": "granted", "reason": "", "state": state, "added": added,
            "message": f"approved {', '.join(tokens)} for run {adw_id}"}


def grant_and_resume(layer: Any, adw_id: str, whats: Iterable[str], who: str, via: str) -> dict[str, Any]:
    """`grant`, then make the stopped step pending again and start the run's resume (detached).
    {status: resumed | refused, reason, message}. A run held by a live process is refused
    (`running`) and nothing is recorded."""
    from . import decide

    layer = _layer(layer)
    held = state_module.run_holder(layer.layer_dir, adw_id)
    if held:
        return _refused("running", f"run {adw_id} is held by process {held.get('pid')}: it is left to that process")
    result = grant(layer, adw_id, whats, who, via)
    if result["status"] != "granted":
        return {key: result[key] for key in ("status", "reason", "message")}
    state = result["state"]
    at = str(((state.proof_plan or {}).get("stopped") or {}).get("at") or "")
    for item in state.steps:
        if item.get("name") == at:
            item["status"] = "pending"
    if (state.decision or {}).get("status") in ("suggested", "explained"):
        state.decision["status"] = "resuming"
    state.save(layer.layer_dir)
    try:
        decide.spawn_resume(layer, state)
    except (OSError, LayerError) as exc:
        return {"status": "refused", "reason": "resume-failed",
                "message": f"{result['message']}, but the resume did not start: {exc}"}
    return {"status": "resumed", "reason": "", "message": f"{result['message']}; resuming from step {at or '?'}"}
