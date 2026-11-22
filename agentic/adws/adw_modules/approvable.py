"""Approve and resume: one model of what a stopped run waits for, one action that clears it.

A run that stopped for a decision only a person may take is approved in one step. `derive(layer,
run)` says whether the run waits only for a person's approval, what the items are, where it resumes
and what happens after; `approve_resume` records every approval where the runtime already reads it
and resumes the run with the mechanism that stop already has. `al approve LAYER RUN --all`, the
layer's `adws/dispatch.py --approve-resume` and the dashboard's Approve & resume all use the two.

The rules of `derive`, the first that applies:

    the run                                                    result
    no readable state; settled; passed; running or held by     not waiting, `why` names it
    a live process; a fan-out candidate; a promotion run
    its integration waits at the approve step (or it is that   kind integration, one item `integration`;
    integration run)                                           resumes from `approve`; the integration run
                                                               then ships through the gate
    failed, stopped for the layer author or the operator       not waiting: only that party clears it
    (e.g. needs-declaration)
    failed, stopped for an approver at `evidence`, the open    kind protected-paths, one item `protected-paths`
    approvals all `edit:PATH`                                  for all of them; resumes from `evidence`
    failed, stopped for an approver, other open approvals      kind proof, one item per approval; resumes from
                                                               the step that stopped. None open: not waiting
    the stand-in's proposal waits for an answer (not           kind proposal, one item `proposal`; `go` is
    abandon or ask-human, no approvals of its own)             answered: a resume, or the action is performed
    handed over, the review passed, the gate's only reasons    when the gate takes approvals
    are protected paths, the worktree is there                 (approvals.gate_takes_approvals): kind gate, one
                                                               item `gate`; the ship step runs again. Otherwise
                                                               not waiting: a person merges the pull request
    otherwise handed over, failed, died or waiting             the plan of assign.py: only a plan `person` whose
                                                               open decisions are all approvable can wait, and
                                                               only when the plan with them approved can start:
                                                               kind decision, the protected paths as one item,
                                                               every other decision one item

Nothing is offered for what only the layer author or the operator clears, for a decision that is a
person's only (a spending limit, a git-integrity failure, a workflow that changes the layer), or
for a stop that is not an approval (a failing check, a login failure, a time limit).

Where an approval is recorded, by kind: proof and protected-paths in `state.approvals`
(approvals.grant_and_resume; protected paths with the `gate` mark); gate in `state.approvals` with
the mark and in agents/<run>/assign.json; proposal as the `go` answer of the decision
(decide.answer); decision in agents/<run>/assign.json (assign.approve, then assign.give);
integration in the integration run's approval.json (integrate.approve). Every resume is detached
and refuses a run held by a live process. Nothing here ships: a resumed run goes on through its
checks, its review and the unchanged gate.

Records:
    agents/<run>/approvable.json   {version: 1, run, basis, waiting, kind, items: [{id, kind, text, what:
                                    [approval tokens], paths, decisions: [assign decision ids]}], from_step,
                                    after, why, token, at}
    (kit/dashboard/dashboard.py read_approvable reads the same file and approval_basis builds the same
    string as `basis`; keep the two alike)

The token is a digest of what was derived (run, basis, resume step, items): a caller that shows the
items hands it back, and an approval of something else than what was shown is refused (`stale`).

Standard library only.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from . import approvals, assign, decide, events, integrate, standin, state as state_module
from .errors import LayerError, StepFailed
from .integrate import IntegrateError

RECORD_FILE = "approvable.json"
KINDS = ("proof", "protected-paths", "gate", "proposal", "decision", "integration")
REASONS = ("unknown-run", "not-waiting", "running", "stale", "bad-item", "bad-who", "bad-note", "resume-failed")
VIAS = ("cli", "dashboard")
MAX_WHO = 80
NOTE_MAX = 500
TEXT_MAX = 300
MAX_ASSUMED_ROUNDS = 3
_RUN_ID = re.compile(r"[0-9a-fA-F]{8}")
_NO_APPROVAL = ("abandon", "ask-human")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _raw(layer_dir: Any, run_id: str, name: str) -> dict[str, Any]:
    return _dict(integrate._read_json(state_module.run_dir(Path(layer_dir), run_id) / name))


def basis(layer_dir: Any, run_id: str) -> str:
    """What a derivation rests on, as one string: it changes when the run's status, its approvals,
    its attempts, its decision or its integration change. (dashboard.py approval_basis builds the
    same string from the same files; keep the two alike.)"""
    layer_dir = Path(getattr(layer_dir, "layer_dir", layer_dir))
    state = _raw(layer_dir, run_id, "state.json")
    given = _raw(layer_dir, run_id, assign.RECORD_FILE)
    return "|".join([str(state.get("status") or ""), str(state.get("ended") or ""),
                     str(len(_list(state.get("approvals")))), str(len(_list(given.get("approvals")))),
                     str(len(_list(given.get("attempts")))), str(_dict(state.get("decision")).get("status") or ""),
                     str(_raw(layer_dir, run_id, integrate.RECORD_FILE).get("status") or "")])


# ---------------------------------------------------------------- the model

def _waiting_step(state: Mapping[str, Any]) -> str:
    return next((str(step.get("name") or "") for step in _list(state.get("steps"))
                 if isinstance(step, dict) and step.get("status") == "waiting"), "")


def _waiting_integration(layer: Any, run_id: str, state: Mapping[str, Any]) -> str:
    """The id of the integration run that waits at its approve step for `run_id` (the original or
    the integration run itself); "" when none does."""
    if state.get("adw") == integrate.ADW_NAME:
        found, data = run_id, state
    else:
        record = integrate.read_record(layer, run_id)
        if not record or record.get("status") != "waiting":
            return ""
        found = str(record["run"])
        data = integrate._state(layer, found)
    return found if data.get("status") == "waiting" and _waiting_step(data) == "approve" else ""


def _paths_text(paths: list[str]) -> str:
    return f"protected paths: {_plural(len(paths), 'file')}: {', '.join(paths)}"


def _gate_reasons(state: Mapping[str, Any]) -> list[str]:
    ship = _dict(state.get("ship"))
    return [str(reason) for reason in [*_list(_dict(ship.get("gate")).get("reasons")), *_list(ship.get("reasons"))]]


def _ship_step(layer: Any, state: Mapping[str, Any]) -> str:
    adw = integrate._base_adw(layer, state) or {}
    return next((str(step.get("name")) for step in adw.get("steps") or []
                 if isinstance(step, dict) and step.get("kind") == "ship"), "ship")


def _token(record: Mapping[str, Any]) -> str:
    shown = [record["run"], record["basis"], record["from_step"],
             [[item["id"], item["text"]] for item in record["items"]]]
    return hashlib.sha256(json.dumps(shown).encode("utf-8")).hexdigest()[:16]


def derive(layer: Any, run_id: str) -> dict[str, Any]:
    """What `run_id` waits for (see the module docstring): {version, run, basis, waiting, kind, items,
    from_step, after, why, token, at}. Nothing is started and nothing is written."""
    from . import ship as ship_module

    layer = approvals._layer(layer)
    run_id = str(run_id or "")
    known = bool(_RUN_ID.fullmatch(run_id))
    state = integrate._state(layer, run_id) if known else {}
    record: dict[str, Any] = {"version": 1, "run": run_id, "basis": basis(layer.layer_dir, run_id) if state else "",
                              "waiting": False, "kind": "", "items": [], "from_step": "", "after": "", "why": "",
                              "token": "", "at": state_module.now()}
    shown_from = SimpleNamespace(worktree=str(state.get("worktree") or ""))

    def clean(text: Any) -> str:
        text = ship_module._without_machine_paths(layer, shown_from, " ".join(str(text or "").split()))
        return text.replace(str(Path.home()), "~")[:TEXT_MAX]

    def item(id_: str, kind: str, text: str, what: Any = (), paths: Any = (), decisions: Any = ()) -> dict[str, Any]:
        return {"id": str(id_), "kind": str(kind), "text": clean(text), "what": [str(w) for w in what],
                "paths": [str(p) for p in paths], "decisions": [str(d) for d in decisions]}

    def no(why: str) -> dict[str, Any]:
        record["why"] = clean(why)
        return record

    def yes(kind: str, items: list[dict[str, Any]], from_step: str, after: str) -> dict[str, Any]:
        record.update(waiting=True, kind=kind, items=items, from_step=str(from_step or ""), after=clean(after))
        record["token"] = _token(record)
        return record

    # 1. nothing waits
    if not state:
        return no(f"run {run_id or '(empty)'} has no readable state")
    if (state_module.run_dir(layer.layer_dir, run_id) / integrate.SETTLEMENT_FILE).is_file():
        return no(f"run {run_id} is settled already")
    status = assign._status(layer, state)
    if status == "passed":
        return no(f"run {run_id} passed: nothing stopped it")
    if status == "running":
        return no(f"run {run_id} is running")
    held = state_module.run_holder(layer.layer_dir, run_id)
    if held:
        return no(f"run {run_id} is held by process {held.get('pid')}: it is left to that process")
    if state.get("fanout_parent"):
        return no("a fan-out candidate: its parent decides")
    if state.get("promotion") or isinstance(_dict(state.get("definition")).get("promotion"), dict):
        return no("a promotion is decided with `al promote`")

    # 2. an integration's approve step
    waiting = _waiting_integration(layer, run_id, state)
    if waiting:
        return yes("integration", [item("integration", "integration", f"integration run {waiting}: checks and review "
                                        "passed, its approve step waits for a person")],
                   "approve", f"integration run {waiting} ships through the gate")
    if state.get("adw") == integrate.ADW_NAME:
        return no(f"integration run {run_id} is {status or 'unknown'}, not waiting at its approve step")

    # 3. a stop of the proof plan or the evidence step
    plan = _dict(state.get("proof_plan"))
    stopped = _dict(plan.get("stopped"))
    party = str(stopped.get("party") or "")
    if status == "failed" and party in ("layer author", "operator"):
        return no(f"stopped for the {party}: only the {party} clears it")
    if status == "failed" and party == approvals.APPROVER:
        at = str(stopped.get("at") or "")
        given = SimpleNamespace(approvals=_list(state.get("approvals")))
        open_ = [str(token) for token in _list(stopped.get("approvals")) if approvals.covered(given, str(token)) is None]
        if not open_:
            return no(f"run {run_id} stopped for an approver and no approval it asked for is open")
        if at == "evidence" and all(token.startswith("edit:") for token in open_):
            paths = [token[len("edit:"):] for token in open_]
            return yes("protected-paths", [item("protected-paths", "protected-path", _paths_text(paths), open_, paths)],
                       "evidence", "resume from evidence")
        rows = [row for row in _list(plan.get("rows")) if isinstance(row, dict)]
        items = []
        for token in open_:
            naming = [row for row in rows if token in _list(row.get("approvals"))]
            text = token
            if naming:
                text += (f": needed by {', '.join(str(row.get('id')) for row in naming)}: "
                         f"{naming[0].get('criterion') or ''}")
            items.append(item(token, token.split(":", 1)[0], text, [token],
                              [token[len("edit:"):]] if token.startswith("edit:") else ()))
        return yes("proof", items, at, f"resume from {at or '?'}")

    # 4. the stand-in's proposal
    decision = _dict(state.get("decision"))
    action = str(decision.get("action") or "")
    if (str(decision.get("status") or "") in decide.ANSWERABLE and not decision.get("approvals")
            and action and action not in _NO_APPROVAL):
        step = str(decision.get("from_step") or "")
        resumes = standin.KIND_OF.get(action) == "resume"
        return yes("proposal", [item("proposal", "proposal", f"the stand-in proposes {action}"
                                     + (f" from {step}" if step else "") + f": {decision.get('summary') or ''}")],
                   step, f"resume from {step or decision.get('stopped_at') or '?'}" if resumes else
                   f"{action} is performed")

    # 5. the gate
    reasons = _gate_reasons(state)
    tree = str(state.get("worktree") or "")
    if (status == "handed-over" and not integrate._rejected(state) and reasons
            and all(reason.startswith("protected-path:") for reason in reasons) and tree and Path(tree).is_dir()):
        paths = list(dict.fromkeys(reason.split(":", 1)[1] for reason in reasons))
        if not approvals.gate_takes_approvals():
            return no(f"{_plural(len(paths), 'protected path')}: a person merges the pull request (the gate does "
                      "not take approvals)")
        ids = [found["id"] for found in assign.decisions(layer, state)
               if found["kind"] == "protected-path" and found["path"] in paths and found["approvable"]]
        return yes("gate", [item("gate", "gate", f"the gate: review passed, a person ships: {_paths_text(paths)}",
                                 [f"edit:{path}" for path in paths], paths, ids)],
                   _ship_step(layer, state), "the ship step runs again: the gate ships it")

    # 6. a decision of give-to-an-agent
    if status not in ("handed-over", "failed", "died", "waiting"):
        return no(f"run {run_id} is {status or 'unknown'}: not a stopped run")
    try:
        found = assign.plan(layer, run_id, settle=False)      # a derivation only reads
        if found["kind"] != "person":
            return no(f"no approval is open: {found['blocked_why'] or found['summary']}")
        chosen = [entry for entry in found["decisions"] if not entry["approved"]]
        after_plan = found
        for round_ in range(MAX_ASSUMED_ROUNDS + 1):
            only = next((entry for entry in chosen if not entry["approvable"]), None)
            if only is not None:
                return no(f"a person's only: {only['text']}")
            if not chosen:
                return no(found["blocked_why"] or found["summary"])
            ids = {entry["id"] for entry in chosen}
            after_plan = assign.plan(layer, run_id, assume=tuple(sorted(ids)), settle=False)
            more = [entry for entry in after_plan["decisions"] if not entry["approved"] and entry["id"] not in ids]
            if after_plan["kind"] != "person" or not more or round_ == MAX_ASSUMED_ROUNDS:
                break
            chosen += more
    except assign.AssignError as exc:
        return no(exc.message)
    if after_plan["kind"] in ("none", "person") or after_plan["blocked"]:
        return no(after_plan["blocked_why"] or after_plan["summary"])
    protected = [entry for entry in chosen if entry["kind"] == "protected-path"]
    items = []
    if protected:
        paths = list(dict.fromkeys(entry["path"] for entry in protected if entry["path"]))
        items.append(item("protected-paths", "protected-path",
                          _paths_text(paths) if paths else "; ".join(entry["text"] for entry in protected),
                          (), paths, [entry["id"] for entry in protected]))
    items += [item(entry["id"], entry["kind"], entry["text"], (), (), [entry["id"]])
              for entry in chosen if entry["kind"] != "protected-path"]
    return yes("decision", items, after_plan["from_step"], after_plan["summary"])


def write(layer: Any, run_id: str) -> dict[str, Any]:
    """`derive`, stored as agents/<run>/approvable.json (where the dashboard reads it); returns the
    record. A run without a readable state writes nothing."""
    layer = approvals._layer(layer)
    record = derive(layer, run_id)
    if record["basis"]:                               # "" only for a run without a readable state
        integrate._write_private(state_module.run_dir(layer.layer_dir, str(run_id)) / RECORD_FILE, record)
    return record


def lines(record: Mapping[str, Any]) -> list[str]:
    """The text `al approve LAYER RUN --all` prints before it approves."""
    if not record.get("waiting"):
        return [f"run {record.get('run')} does not wait for an approval: {record.get('why')}"]
    items = list(record.get("items") or [])
    return [f"run {record.get('run')} waits for your approval of {len(items)} item(s):",
            *[f"  {item.get('text')}" for item in items], f"after:     {record.get('after')}"]


# ---------------------------------------------------------------- approve & resume

def _refused(reason: str, message: str, run_id: str) -> dict[str, Any]:
    return {"status": "refused", "reason": reason, "message": message, "run_id": run_id}


def _resumed(message: str, run_id: str) -> dict[str, Any]:
    return {"status": "resumed", "reason": "", "message": message, "run_id": run_id}


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in text)


def _gate(layer: Any, run_id: str, record: Mapping[str, Any], who: str, via: str, note: str) -> dict[str, Any]:
    """The gate's protected paths: recorded with the `gate` mark and in assign.json, then the reset of
    the stand-in's `land` (standin._perform): the ship steps are pending again and the run resumes."""
    whats = [what for item in record["items"] for what in item["what"]]
    ids = [found for item in record["items"] for found in item["decisions"]]
    state = state_module.load(layer.layer_dir, run_id)
    known = {entry["id"]: entry for entry in assign.decisions(layer, integrate._state(layer, run_id))}
    approvals.record(state, whats, who, via, note=note, gate=True)
    given = assign.read_record(layer, run_id)
    approved = []
    for found in ids:
        entry = known.get(found)
        if entry is None or entry["approved"]:
            continue
        approved.append({"id": entry["id"], "kind": entry["kind"], "text": entry["text"], "path": entry["path"],
                         "by": who, "at": assign._stamp(), "via": via})
    if approved:
        given["approvals"] += approved
        assign._write_record(layer, run_id, given)
    with events.recording(layer.layer_dir, run_id):
        events.emit("approval.granted", what=whats, who=who, via=via, gate=True, **({"note": note} if note else {}))
        for entry in approved:
            events.emit("assign.approval", item=entry["id"], kind=entry["kind"], text=entry["text"][:500], by=who,
                        via=via)
    state.ship = {"previous_handoff": dict(state.ship or {})}
    for step in state.steps:
        if step.get("kind") in ("ship", "deliver") or step.get("name") == "deliver":
            step["status"] = "pending"
    state.save(layer.layer_dir)
    message = f"approved {len(whats)} protected path(s) for run {run_id}"
    try:
        decide.spawn_resume(layer, state)
    except (OSError, LayerError) as exc:
        return _refused("resume-failed", f"{message}: the approval is recorded, but the resume did not start: {exc}",
                        run_id)
    return _resumed(f"{message}; the ship step runs again", run_id)


def _decision(layer: Any, run_id: str, record: Mapping[str, Any], who: str, via: str, note: str,
              spawn: Any) -> dict[str, Any]:
    """The "needs a person" items of give-to-an-agent: each approved in assign.json, then the plan
    that follows is started (assign.give)."""
    layer_dir = Path(layer.layer_dir)
    ids = [found for item in record["items"] for found in item["decisions"]]
    done = 0
    try:
        for found in ids:
            assign.approve(layer_dir, run_id, found, who, via=via)
            done += 1
    except assign.AssignError as exc:
        return _refused(exc.reason, exc.message if not done else
                        f"approved {_plural(done, 'item')}, then nothing started: {exc.message}", run_id)
    said = f"approved {_plural(done, 'item')} for run {run_id}"
    try:
        started = assign.give(layer_dir, run_id, note=note, by=via, who=who, detach=True, spawn=spawn, quiet=True)
    except assign.AssignError as exc:
        return _refused(exc.reason, f"approved, but nothing started: {exc.message}", run_id)
    except (LayerError, StepFailed, OSError) as exc:
        return _refused("resume-failed", f"approved, but nothing started: {integrate.first_line(exc)[:400]}", run_id)
    new = str(started.get("run_id") or "") or run_id
    if started["kind"] in assign.RESUME_KINDS:
        return _resumed(f"{said}; resuming from step {record.get('from_step') or '?'}", new)
    if started["kind"] in assign.INTEGRATION_KINDS:
        return _resumed(f"{said}; given to an agent ({started['kind']}): run {new} continues it", new)
    return _resumed(f"{said}; run {new} starts the request again", new)


def _integration(layer: Any, run_id: str, who: str, spawn: Any) -> dict[str, Any]:
    waiting = _waiting_integration(layer, run_id, integrate._state(layer, run_id))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            integrate.approve(Path(layer.layer_dir), run_id, who, detach=True, as_json=True,
                              spawn=spawn or integrate._spawn)
    except IntegrateError as exc:
        return _refused(exc.reason, exc.message, run_id)
    except (LayerError, StepFailed, OSError) as exc:
        return _refused("resume-failed", f"approved, but integration run {waiting} did not continue: "
                                         f"{integrate.first_line(exc)[:400]}", run_id)
    return _resumed(f"approved by {who}: integration run {waiting} continues; resuming from step approve",
                    waiting or run_id)


def approve_resume(layer: Any, run_id: str, items: Any, who: str, via: str, *, note: str = "", token: str = "",
                   spawn: Any = None) -> dict[str, Any]:
    """A person approves what `run_id` waits for and the run resumes: {status: resumed | refused,
    reason, message, run_id: the run to watch}. `items` are the ids of the items approved, or None
    for all of them; `token` is the one of the derivation that was shown ("" skips that check). A
    refusal records nothing; `spawn` replaces the process start of a "decision" or "integration"
    resume (tests)."""
    if via not in VIAS:
        raise ValueError(f"via: one of {', '.join(VIAS)}")
    run_id = str(run_id or "")
    who = " ".join(str(who or "").split())
    if not who or len(who) > MAX_WHO or not who.isprintable():
        return _refused("bad-who", f"who approves: 1 to {MAX_WHO} printable characters", run_id)
    if _has_control(str(note or "")):
        return _refused("bad-note", "the note holds a line break or a control character: plain text on one line",
                        run_id)
    try:
        note = assign.check_note(note)
    except assign.AssignError as exc:
        return _refused("bad-note", exc.message, run_id)
    if not _RUN_ID.fullmatch(run_id):
        return _refused("unknown-run", f"no run {run_id!r} (a run id is 8 hexadecimal characters)", run_id)
    try:
        layer = approvals._layer(layer)
    except LayerError as exc:
        return _refused("resume-failed", f"the layer cannot be read: {exc}", run_id)
    if not integrate._state(layer, run_id):
        return _refused("unknown-run", f"no run {run_id} in this layer", run_id)
    held = state_module.run_holder(layer.layer_dir, run_id)
    if held:
        return _refused("running", f"run {run_id} is held by process {held.get('pid')}: it is left to that process",
                        run_id)
    record = derive(layer, run_id)
    try:
        if not record["waiting"]:
            return _refused("not-waiting", record["why"], run_id)
        if token and str(token) != record["token"]:
            return _refused("stale", "what was shown changed: look again", run_id)
        open_ids = [item["id"] for item in record["items"]]
        if items is not None:
            asked = [str(item) for item in items]
            unknown = [item for item in asked if item not in open_ids]
            if unknown:
                return _refused("bad-item", f"run {run_id} has no item {unknown[0]!r} to approve "
                                            f"(open: {', '.join(open_ids)})", run_id)
            if set(asked) != set(open_ids):
                return _refused("stale", f"run {run_id} waits for {_plural(len(open_ids), 'item')} "
                                         f"({', '.join(open_ids)}): look again", run_id)
        kind = record["kind"]
        if kind in ("proof", "protected-paths"):
            whats = [what for item in record["items"] for what in item["what"]]
            result = {**approvals.grant_and_resume(layer, run_id, whats, who, via, note=note,
                                                   gate=kind == "protected-paths"), "run_id": run_id}
        elif kind == "gate":
            result = _gate(layer, run_id, record, who, via, note)
        elif kind == "proposal":
            answered = decide.answer(layer, run_id, "go", note, via, who=who)
            if answered["status"] == "resuming":
                result = _resumed(f"approved the stand-in's proposal for run {run_id}; resuming from step "
                                  f"{record['from_step'] or '?'}", run_id)
            elif answered["status"] == "applied":
                result = _resumed(f"approved the stand-in's proposal for run {run_id}: {answered['message']}", run_id)
            else:
                result = _refused(str(answered.get("reason") or "not-waiting"), answered["message"], run_id)
        elif kind == "decision":
            result = _decision(layer, run_id, record, who, via, note, spawn)
        else:
            result = _integration(layer, run_id, who, spawn)
        return result
    finally:                                          # the stored record follows the new state
        with contextlib.suppress(Exception):
            write(layer, run_id)
