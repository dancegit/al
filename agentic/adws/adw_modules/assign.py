"""Give a stopped run to an agent: one plan for every run that waits for a person.

A run under "Needs you" stopped for one of a handful of reasons. `plan(layer, run)` is the one
function that says what an agent would do about it; `al assign LAYER RUN` (the layer's
`adws/assign.py`), the dashboard's "Give to an agent" and the stand-in (`after_run`) all use it.

    why the run stopped                         kind         what starts
    review passed, the merge conflicts          integrate    an integration run (integrate.py, mode conflict)
    review rejected, with blockers              blockers     an integration run in mode blockers: the branch onto
                                                             the target, the review's patch round for the blockers,
                                                             the checks, ONE review round, the unchanged gate
    review rejected, only proof is missing      proof        an integration run in mode proof: the proof that exists
                                                             is collected (or the plan's proof commands are run),
                                                             then the review; no code changes
    something only a person may decide          person       nothing; the plan names the decision, and
                                                             `approve` records the person's approval of that item
    a check still failing after its retries     check        --resume from that check, on the heavy tier
    a login failure, a time limit, a process    resume       --resume from the step that stopped, unchanged
    that died, a waiting step
    it failed before changing anything          start-again  the same request again; the new run replaces it
    settled, passed, under way, or a batch run  none         nothing

No second engine: an integration run (a rejected run has no worktree any more, so it continues in
a new run), `--resume` of the run itself (a failed run keeps its worktree), or launch.start. A
"needs a person" item is never given to an agent; its approval is quoted in the agent's request
and in the review brief. Nothing here merges: every plan ends in the checks, the review and the
unchanged gate, or with a person.

Records:
    agents/<run>/assign.json   {version: 1,
                                attempts: [{n, at, by: cli | dashboard | decider, who, kind, note, run: the run
                                            it started or resumed, reason: the stop key when given, outcome:
                                            started | landed | passed | stopped, after: the stop key afterwards,
                                            why}],
                                approvals: [{id, kind, text, path, by, at, via}]}
    events of the run          assign.attempt, assign.outcome, assign.approval
    (kit/dashboard/dashboard.py read_assign reads the same file; keep the two alike)

Limits: a person gives a run to an agent MAX_CLICKS times (`--override` once more); the stand-in
MAX_STAND_IN time, never counted as a click, and counted over a run and every run it replaces (a
request that was started again is a new run; the stand-in does not start it a second time). A run
that stopped twice for the same reason stays with the person.

Refusals are classified (REASONS) and printed as `assign:<reason>: <words>`.

Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from . import affected, artefacts, batch, decide, events, integrate, ir, models, state as state_module
from .errors import LayerError, StepFailed
from .integrate import IntegrateError, first_line

KINDS = ("integrate", "blockers", "proof", "person", "check", "resume", "start-again", "none")
INTEGRATION_KINDS = ("integrate", "proof", "blockers")       # they start an integration run, in the bulk order
RESUME_KINDS = ("check", "resume")
RECORD_FILE = "assign.json"
MAX_CLICKS = 2
MAX_STAND_IN = 1
NOTE_MAX = 500
MAX_WHO = 80
SCRIPT = Path("adws") / "assign.py"
REFUSED_EXIT = 3
RESUME_LOG = "resume.log"
PERSON_MARKERS = re.compile(r"REQUIRES HUMAN|needs? a (person|human)|a person (must|decides|approves)|"
                            r"protected (path|file)|credential|secret|password|api key|vocabular|layer change", re.I)
PROOF_ONLY = re.compile(r"proof|screenshot|recording|video|artefact|evidence|not collected", re.I)
PROOF_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".webm", ".mp4", ".mov", ".wav", ".mp3", ".ogg", ".har",
                  ".trace.zip")
REASONS = ("unknown-run", "not-stopped", "settled", "running", "needs-a-person", "limit", "no-script", "bad-item",
           "bad-note")
BYS = ("cli", "dashboard", "decider")
ASSUMED = {"by": "(assumed)", "at": ""}      # an approval a caller of plan(…, assume=…) only supposes
_CHECK_EXIT = re.compile(r"exited \d+ after")
_LOGIN = re.compile(r"login was refused|authentication error|not logged in|log ?in (has )?expired", re.I)
_TIME_LIMIT = re.compile(r"timed out|time limit", re.I)
_PATH = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_TICKS = re.compile(r"`([^`]+)`")
_COMMAND_HEADINGS = ("commands", "validation commands")


class AssignError(LayerError):
    """A run cannot be given to an agent, for a classified reason (see REASONS)."""

    def __init__(self, reason: str, message: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown assign reason {reason!r}")
        self.reason = reason
        self.message = message
        super().__init__(f"assign:{reason}: {message}")


class _Usage(Exception):
    """A wrong use of the command line (exit 2)."""


def _spawn(argv: list[str], **popen: Any) -> subprocess.Popen:
    return subprocess.Popen(argv, **popen)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _state(layer: Any, run_id: str) -> dict[str, Any]:
    return integrate._state(layer, run_id)


def _event(layer: Any, run_id: str, type_: str, **fields: Any) -> None:
    with events.recording(layer.layer_dir, run_id):
        events.emit(type_, **fields)


def check_note(note: Any) -> str:
    """The person's sentence for the agent, on one line; AssignError("bad-note") when it is longer
    than NOTE_MAX or holds a control character."""
    text = " ".join(str(note or "").split())
    if len(text) > NOTE_MAX:
        raise AssignError("bad-note", f"the note has {len(text)} characters, at most {NOTE_MAX}")
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise AssignError("bad-note", "the note holds a control character: plain text on one line")
    return text


# ---------------------------------------------------------------- the record

def _record_path(layer: Any, run_id: str) -> Path:
    return state_module.run_dir(layer.layer_dir, run_id) / RECORD_FILE


def read_record(layer: Any, run_id: str) -> dict[str, Any]:
    """agents/<run>/assign.json; an empty record when there is none."""
    data = integrate._read_json(_record_path(layer, run_id))
    data = data if isinstance(data, dict) else {}
    return {"version": 1,
            "attempts": [dict(item) for item in data.get("attempts") or [] if isinstance(item, dict)],
            "approvals": [dict(item) for item in data.get("approvals") or [] if isinstance(item, dict)]}


def _write_record(layer: Any, run_id: str, record: Mapping[str, Any]) -> None:
    integrate._write_private(_record_path(layer, run_id), record)


# ---------------------------------------------------------------- why the run stopped

def _ship(state: Mapping[str, Any]) -> dict[str, Any]:
    return state.get("ship") if isinstance(state.get("ship"), dict) else {}


def _steps(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [step for step in state.get("steps") or [] if isinstance(step, dict)]


def _detail(step: Mapping[str, Any]) -> str:
    return str(step.get("detail") or "").removeprefix(f"{step.get('name')}: ")


def _stopped_step(state: Mapping[str, Any], status: str) -> dict[str, Any]:
    steps = _steps(state)
    wanted = {"failed": "failed", "waiting": "waiting", "died": "running"}.get(status)
    if wanted:
        found = next((step for step in reversed(steps) if step.get("status") == wanted), None)
        return found or (steps[-1] if steps else {})
    if status == "handed-over":
        kind = "review" if integrate._rejected(state) else "ship"
        return next((step for step in steps if step.get("kind") == kind), {})
    return {}


def _status(layer: Any, state: Mapping[str, Any]) -> str:
    status = str(state.get("status") or "")
    if status == "running" and not integrate.is_running(layer, str(state.get("adw_id") or "")):
        return "died"
    return status


def _proof_only(blockers: list[dict[str, Any]]) -> bool:
    return bool(blockers) and all(PROOF_ONLY.search(f"{b.get('description')} {b.get('resolution')}") for b in blockers)


def stop(layer: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """Why the run stopped: {status, step, kind: the step's kind, key, why}. `key` is the coarse
    reason two stops are compared by; `why` is one line for a person."""
    status = _status(layer, state)
    ship = _ship(state)
    step = _stopped_step(state, status)
    name = str(step.get("name") or "")
    detail = _detail(step)
    key, why = "", ""
    if status == "handed-over":
        reasons = [first_line(r)[:160] for r in ship.get("handed_over") or ship.get("reasons") or []]
        why = ", ".join(reasons) or "handed to a person"
        if integrate._rejected(state):
            blockers = integrate.blockers_of(state)
            verdict = (state.get("review") or {}).get("verdict") or {}
            why = f"the review rejected it: {first_line(verdict.get('summary')) or _plural(len(blockers), 'blocker')}"
            if any(not item["approved"] for item in decisions(layer, state)):
                key = "person"
            else:
                key = "review:proof" if _proof_only(blockers) else "review:blockers"
        elif any(r.startswith(("merge-conflict", "ship-failed:")) for r in reasons):
            key = "conflict"
        else:
            key = "person"
    elif status in ("failed", "died", "waiting"):
        why = f"{name or 'the run'}: {first_line(detail) or ('its process died' if status == 'died' else 'no reason recorded')}"
        if status == "waiting":
            key = f"waiting:{name}"
        elif status == "died":
            key = "died"
        elif _LOGIN.search(detail):
            key = "login"
        elif _TIME_LIMIT.search(detail):
            key = "time-limit"
        elif str(ship.get("handoff_skipped") or "").startswith("nothing changed"):
            key = "nothing-changed"
        elif step.get("kind") == "check" and _CHECK_EXIT.search(detail):
            key = f"check:{name}"
        else:
            key = f"step:{name}"
    return {"status": status, "step": name, "kind": str(step.get("kind") or ""), "key": key, "why": why[:400]}


# ---------------------------------------------------------------- what only a person may decide

def _decision_id(text: str) -> str:
    return hashlib.sha256(" ".join(str(text).split()).encode("utf-8")).hexdigest()[:10]


def _decision_kind(text: str) -> tuple[str, str]:
    """(kind, path) of a "needs a person" text."""
    if re.search(r"protected (path|file)", text, re.I):
        found = _PATH.search(text)
        return "protected-path", found.group(0) if found else ""
    if re.search(r"credential|secret|password|api key", text, re.I):
        return "credential", ""
    if re.search(r"vocabular|layer change", text, re.I):
        return "layer-change", ""
    return "requires-human", ""


def _base_adw(layer: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    return integrate._base_adw(layer, state) or {}


def decisions(layer: Any, state: Mapping[str, Any], work: Mapping[str, Any] | None = None,
              assume: Any = ()) -> list[dict[str, Any]]:
    """The "needs a person" items of a stopped run: [{id, kind, text, path, approvable, approved}].
    `work` is the run whose review is read (an earlier attempt; default the run itself). Approvals
    come from agents/<run>/assign.json; an approvable item whose id is in `assume` reads as approved
    (by "(assumed)") without anything being written, so a caller can ask what an approval would start."""
    from . import ship as ship_module

    run_id = str(state.get("adw_id") or "")
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(text: str, kind: str = "", path: str = "", approvable: bool = True) -> None:
        text = " ".join(str(text).split())[:500]
        if not text or _decision_id(text) in seen:
            return
        seen.add(_decision_id(text))
        guessed, guessed_path = _decision_kind(text)
        found.append({"id": _decision_id(text), "kind": kind or guessed, "text": text,
                      "path": path or (guessed_path if (kind or guessed) == "protected-path" else ""),
                      "approvable": approvable, "approved": None})

    adw = _base_adw(layer, state)
    changes_layer = str(adw.get("changes") or "codebase") == "layer"
    ship = _ship(state)
    # 1. the blockers of the final review that only a person may decide
    for blocker in integrate.blockers_of(work if work is not None else state):
        text = f"{blocker['description']}\n\n{blocker['resolution']}"
        if not PERSON_MARKERS.search(text):
            continue
        add(blocker["description"])
        for request in ship_module.human_requests(text):
            if " ".join(request.split()) not in " ".join(blocker["description"].split()):
                add(request)
    # 2. what the gate said
    gate = ship.get("gate") if isinstance(ship.get("gate"), dict) else {}
    for reason in [*(gate.get("reasons") or []), *(ship.get("reasons") or [])]:
        reason = str(reason)
        if reason.startswith("protected-path:"):
            path = reason.split(":", 1)[1]
            add(f"{path} is a protected path, and this run changes it", "protected-path", path)
        elif reason.startswith(("layer-changed", "layer-change")) and not changes_layer:
            add(first_line(reason), "layer-change")
    # 3. a failed step that asks for a person
    status = str(state.get("status") or "")
    failed = _stopped_step(state, "failed") if status == "failed" else {}
    detail = _detail(failed) if failed.get("status") == "failed" else ""
    integrity = "git-integrity" in detail or "git-integrity" in str(ship.get("handoff_skipped") or "")
    budget = (state.get("budget") or {}).get("stopped") if isinstance(state.get("budget"), dict) else None
    if detail and not integrity and not budget and (PERSON_MARKERS.search(detail) or ship_module.MARKER in detail):
        requests = ship_module.human_requests(detail)
        for request in requests or [first_line(detail)]:
            add(request)
    # 4. never an agent's, whatever a person approves here (the words of decide.eligible)
    if changes_layer:
        add("the workflow changes the layer (a person always approves)", "layer-change", approvable=False)
    if budget:
        add("a spending limit stopped the run (money is a person's decision)", "spending-limit", approvable=False)
    if integrity:
        add("a git-integrity failure: the worktree is left for inspection, never continued by an agent", "integrity",
            approvable=False)
    if state.get("promotion") or isinstance((state.get("definition") or {}).get("promotion"), dict):
        add("a promotion is decided with `al promote`", "promotion", approvable=False)
    record = integrate.read_record(layer, run_id) if run_id else None
    if record and record.get("status") == "waiting" and _state(layer, str(record["run"])).get("status") == "waiting":
        add(f"integration run {record['run']} waits for a person's approval: al integrate LAYER {run_id} --approve",
            "promotion", approvable=False)
    approvals = {str(item.get("id")): item for item in read_record(layer, run_id)["approvals"]} if run_id else {}
    for item in found:
        given = approvals.get(item["id"])
        if given and item["approvable"]:
            item["approved"] = {"by": str(given.get("by") or ""), "at": str(given.get("at") or "")}
        elif item["approvable"] and item["id"] in assume:
            item["approved"] = dict(ASSUMED)
    return found


# ---------------------------------------------------------------- proof that was not collected

def _git_names(root: Path, args: list[str]) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [name for name in out.stdout.split("\0") if name] if out.returncode == 0 else []


def _worktree(state: Mapping[str, Any]) -> Path | None:
    text = str(state.get("worktree") or "")
    return Path(text) if text and Path(text).is_dir() else None


def _is_proof(name: str, texts: str) -> bool:
    return name.lower().endswith(PROOF_SUFFIXES) or name in texts


def uncollected(layer: Any, state: Mapping[str, Any]) -> list[str]:
    """Proof files of the run that no artefact handed to its review: files of the change with a
    proof suffix (or named in a blocker) that match no artefact glob, and, while the worktree
    exists, the files an image artefact's limit left out."""
    app = Path(layer.app_root)
    globs = [str(o.get("glob")) for o in layer.ir.get("artefacts") or [] if isinstance(o, dict) and o.get("glob")]
    texts = " ".join(f"{b['description']} {b['resolution']}" for b in integrate.blockers_of(state))
    tip = str(_ship(state).get("commit") or "")
    target_head = integrate._ref(app, f"refs/heads/{integrate._target_branch(layer, state)}")
    names: list[str] = []
    if tip and target_head:
        base = integrate._app_git(app, ["merge-base", target_head, tip]).stdout.strip()
        if base:
            names += _git_names(app, ["diff", "--name-only", "-z", "--no-renames", "--diff-filter=AM", f"{base}..{tip}"])
    worktree = _worktree(state)
    if worktree is not None:
        for entry in _git_names(worktree, ["status", "--porcelain", "-z", "--untracked-files=all", "--ignored=matching"]):
            if entry[:2] in ("??", "!!") and len(entry) > 3:
                names.append(entry[3:])
    found = [name for name in dict.fromkeys(names) if _is_proof(name, texts) and not affected.matches(name, globs)]
    if worktree is not None:
        root = worktree.resolve()
        changed = artefacts._changed_by_run(layer, SimpleNamespace(adw_id=str(state.get("adw_id"))), root)
        for option in layer.ir.get("artefacts") or []:
            if not isinstance(option, dict) or option.get("kind") != "image" or not option.get("glob"):
                continue
            try:
                kept, left = artefacts._glob_matches(root, option, changed)
                if left:
                    every, _ = artefacts._glob_matches(root, {**option, "maxFiles": left + len(kept)}, changed)
                    found += [name for name, _path in every[len(kept):] if name not in found]
            except (LayerError, OSError, ValueError):
                continue
    return found


def proof_commands(text: str) -> list[str]:
    """The first backticked span of each bullet under a heading `Commands` or `Validation Commands`
    of a plan."""
    commands: list[str] = []
    inside = False
    for line in str(text or "").splitlines():
        heading = _HEADING.match(line)
        if heading:
            inside = heading.group(1).strip().lower() in _COMMAND_HEADINGS
            continue
        bullet = _BULLET.match(line) if inside else None
        span = _TICKS.search(bullet.group(1)) if bullet else None
        if span and span.group(1).strip() not in commands:
            commands.append(span.group(1).strip())
    return commands


def _plan_text(layer: Any, state: Mapping[str, Any]) -> str:
    plan_file = str(state.get("plan") or "")
    if not plan_file:
        return ""
    path = Path(plan_file)
    path = path if path.is_absolute() else Path(layer.layer_dir) / path
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- attempts

def _attempt_outcome(layer: Any, run_id: str, attempt: Mapping[str, Any]) -> dict[str, str] | None:
    """{outcome, after, why} of a started attempt once it ended; None while it is under way."""
    kind, started = str(attempt.get("kind") or ""), str(attempt.get("run") or "")
    if not started:
        return {"outcome": "stopped", "after": "", "why": "it did not start"}
    state = _state(layer, started)
    if not state:
        return {"outcome": "stopped", "after": "", "why": f"run {started} left no state"}
    if state.get("status") == "running" and integrate.is_running(layer, started):
        return None
    found = stop(layer, state)
    if kind in INTEGRATION_KINDS:
        record = integrate.read_record(layer, run_id) or {}
        mine = next((item for item in [record, *(record.get("previous") or [])]
                     if isinstance(item, dict) and item.get("run") == started), {})
        if mine.get("status") in ("queued", "running") and state.get("status") == "running":
            mine = {}                                    # its process died before it wrote its result
        if mine.get("status") == "landed":
            return {"outcome": "landed", "after": "", "why": f"landed in {str(mine.get('commit'))[:7]}"}
        why = str(mine.get("why") or found["why"] or "the run did not land")
        return {"outcome": "stopped", "after": found["key"] or f"status:{found['status']}", "why": why[:400]}
    if found["status"] == "passed":
        return {"outcome": "passed", "after": "", "why": ""}
    return {"outcome": "stopped", "after": found["key"] or f"status:{found['status']}", "why": found["why"]}


def attempts(layer: Any, run_id: str, settle: bool = True) -> list[dict[str, Any]]:
    """The attempts recorded for a run, each `started` one settled once its run ended: the outcome
    is written back to assign.json and emitted, once, as `assign.outcome` into the run's events.
    With `settle=False` the outcome is only returned: nothing is written or emitted (a reader)."""
    record = read_record(layer, run_id)
    changed = []
    for attempt in record["attempts"]:
        if attempt.get("outcome") != "started":
            continue
        outcome = _attempt_outcome(layer, run_id, attempt)
        if outcome is not None:
            attempt.update(outcome)
            changed.append(attempt)
    if changed and settle:
        try:
            _write_record(layer, run_id, record)
        except OSError:
            return record["attempts"]             # a reader without write access still sees the outcome
        for attempt in changed:
            _event(layer, run_id, "assign.outcome", n=attempt.get("n"), kind=attempt.get("kind"),
                   run=attempt.get("run"), outcome=attempt.get("outcome"), after=attempt.get("after"),
                   why=str(attempt.get("why") or "")[:500])
    return record["attempts"]


def _same_reason(found: list[dict[str, Any]]) -> bool:
    stopped = [item for item in found if item.get("outcome") == "stopped"]
    if len(stopped) < 2:
        return False
    last, before = stopped[-1], stopped[-2]
    return bool(last.get("after")) and last.get("after") in (last.get("reason"), before.get("after"))


# ---------------------------------------------------------------- the plan

def _declared(layer: Any, name: str) -> bool:
    return any(isinstance(a, dict) and a.get("name") == name and not a.get("integration")
               for a in layer.ir.get("adws") or [])


def _source(layer: Any, run_id: str, found: list[dict[str, Any]]) -> str:
    """The newest earlier attempt run that ended handed over with a rejected review ("" = none)."""
    for attempt in reversed(found):
        started = str(attempt.get("run") or "")
        if attempt.get("kind") in INTEGRATION_KINDS and started and started != run_id:
            state = _state(layer, started)
            if state.get("status") == "handed-over" and integrate._rejected(state):
                return started
    return ""


def _blocker_lines(blockers: list[dict[str, Any]]) -> list[str]:
    return [f"   - {first_line(item.get('description'))[:200]}" for item in blockers]


def plan(layer: Any, run: str, *, note: str = "", assume: Any = (), settle: bool = True) -> dict[str, Any]:
    """What giving `run` (an id or a branch) to an agent would do; nothing is started or written
    beyond settling the outcome of an attempt that ended. See the module docstring for the table.
    The decision ids in `assume` are treated as approved, also for a decision found only on the way
    (a protected path the integration would refuse); nothing is written for them (approvable.py asks
    this way what starts after an approval). With `settle=False` the outcome of an attempt that ended
    is not written either: the call only reads.

    Keys: run, asked, title, status, stop, kind, summary, steps, moved, decisions, blockers, found,
    commands, integrate, source, from_step, tier, attempts, clicks, left, same_reason, blocked
    ("" or a reason of REASONS), blocked_why, note."""
    note = check_note(note)
    assume = {str(item) for item in assume or ()}
    try:
        asked = integrate.resolve_run(layer, run)
    except IntegrateError as exc:
        raise AssignError("unknown-run", exc.message) from exc
    state = _state(layer, asked)
    if not state:
        raise AssignError("unknown-run", f"run {asked} has no readable state.json")
    run_id = asked
    planned = (state.get("definition") or {}).get("integration") if isinstance(state.get("definition"), dict) else None
    info = planned if isinstance(planned, dict) else state.get("integration")
    if (state.get("adw") == integrate.ADW_NAME or info) and isinstance(info, dict):
        original = str(info.get("run") or "")
        if original and _state(layer, original):      # an integration run: the plan is its original's
            run_id, state = original, _state(layer, original)
    found = attempts(layer, run_id, settle)
    clicks = sum(1 for item in found if item.get("by") in ("cli", "dashboard"))
    stopped = stop(layer, state)
    adw = _base_adw(layer, state)
    out: dict[str, Any] = {
        "run": run_id, "asked": asked, "title": first_line(state.get("prompt")), "status": stopped["status"],
        "stop": stopped, "kind": "none", "summary": "", "steps": [], "moved": False, "decisions": [], "blockers": [],
        "found": [], "commands": [], "integrate": None, "source": run_id, "from_step": "", "tier": "",
        "attempts": found, "clicks": clicks, "left": max(0, MAX_CLICKS - clicks),
        "same_reason": _same_reason(found), "blocked": "", "blocked_why": "", "note": note}

    def done(kind: str, summary: str, steps: list[str] | None = None, blocked: str = "", why: str = "") -> dict[str, Any]:
        out.update(kind=kind, summary=summary, steps=list(steps or [summary]), blocked=blocked, blocked_why=why)
        if not out["blocked"] and kind not in ("none", "person") and out["left"] == 0:
            words = "given to an agent twice already; `--override` to give it once more"
            if out["same_reason"]:
                last = next((item for item in reversed(found) if item.get("outcome") == "stopped"), {})
                words = (f"it stopped twice for the same reason ({first_line(last.get('why')) or last.get('after')}): "
                         f"it stays with you; {words}")
            out.update(blocked="limit", blocked_why=words)
        return out

    # 1. nothing to give
    if (state_module.run_dir(layer.layer_dir, run_id) / integrate.SETTLEMENT_FILE).is_file():
        return done("none", "settled already", blocked="settled", why=f"run {run_id} is settled already")
    if stopped["status"] == "passed":
        return done("none", "it passed", blocked="not-stopped", why=f"run {run_id} passed: nothing stopped it")
    under_way = next((item for item in found if item.get("outcome") == "started"), None)
    record = integrate.read_record(layer, run_id)
    if stopped["status"] == "running":
        return done("none", "it is running", blocked="running", why=f"run {run_id} is running")
    held = state_module.run_holder(layer.layer_dir, run_id)
    if held:
        return done("none", "its process is alive", blocked="running",
                    why=f"run {run_id} is held by process {held.get('pid')}")
    if under_way is not None:
        return done("none", f"an agent is on it: run {under_way.get('run')}", blocked="running",
                    why=f"attempt {under_way.get('n')} is under way: run {under_way.get('run')}")
    if record and record.get("status") in ("queued", "running") and integrate.is_running(layer, str(record["run"])):
        return done("none", f"an agent is on it: run {record['run']}", blocked="running",
                    why=f"run {run_id} is being integrated by {record['run']}")
    if state.get("fanout_parent"):
        return done("none", "a fan-out candidate: its parent decides", blocked="not-stopped",
                    why="a fan-out candidate: its parent decides")
    if stopped["status"] not in ("handed-over", "failed", "died", "waiting"):
        return done("none", f"it is {stopped['status'] or 'unknown'}", blocked="not-stopped",
                    why=f"run {run_id} is {stopped['status'] or 'unknown'}: not a stopped run")
    if batch.is_batch_run(state):       # never a request launch.start can start, and never resumed as one
        again = batch.rerun_words(state)
        return done("none", f"a batch run (kit workflow {batch.ADW_NAME}): run the full suites again with "
                            f"`{again}`, or settle it", blocked="not-stopped",
                    why=f"run {run_id} is a batch run (kit workflow {batch.ADW_NAME}), not a request an agent "
                        f"takes: `{again}` runs the full suites again")

    source = _source(layer, run_id, found)
    work = _state(layer, source) if source else state
    out["source"] = source or run_id
    rejected = stopped["status"] == "handed-over" and integrate._rejected(work)
    out["blockers"] = integrate.blockers_of(work) if rejected else []
    out["decisions"] = decisions(layer, state, work, assume)
    # a protected path the integration would refuse is named only when it is asked (see `protected` below):
    # its approval is listed here, and then lets the path through
    known = {item["id"] for item in out["decisions"]}
    for given in read_record(layer, run_id)["approvals"]:
        if given.get("kind") == "protected-path" and given.get("path") and given.get("id") not in known:
            out["decisions"].append({"id": str(given["id"]), "kind": "protected-path", "text": str(given.get("text")),
                                     "path": str(given["path"]), "approvable": True,
                                     "approved": {"by": str(given.get("by") or ""), "at": str(given.get("at") or "")}})
    app = Path(layer.app_root)
    target = integrate._target_branch(layer, state)
    target_head = integrate._ref(app, f"refs/heads/{target}")
    tip = str(_ship(work).get("commit") or "")
    out["moved"] = bool(target_head and tip) and not integrate._is_ancestor(app, target_head, tip)

    # 2. a person decides first
    waiting = [item for item in out["decisions"] if not item["approved"]]
    if waiting:
        lines = []
        for item in out["decisions"]:
            if item["approved"]:
                lines.append(f"approved by {item['approved']['by']}: {item['text']}")
            elif item["approvable"]:
                lines.append(f"a person decides: {item['text']}; to approve exactly this and let the rest start: "
                             f"al assign LAYER {run_id} --approve {item['id']} --who NAME")
            else:
                lines.append(f"a person decides: {item['text']}")
        return done("person", f"a person decides: {waiting[0]['text']}", lines, "needs-a-person",
                    f"{_plural(len(waiting), 'item')} of run {run_id} only a person may decide: {waiting[0]['text']}")
    approved_paths = [item["path"] for item in out["decisions"] if item["approved"] and item["path"]]

    def person(text: str, approvable: bool = False, kind: str = "requires-human", path: str = "") -> dict[str, Any]:
        """The plan `person` for one more decision, found only now."""
        item = {"id": _decision_id(text), "kind": kind, "text": text, "path": path, "approvable": approvable,
                "approved": None}
        out["decisions"].append(item)
        line = f"a person decides: {text}" + (
            f"; to approve exactly this and let the rest start: al assign LAYER {run_id} --approve {item['id']} "
            "--who NAME" if approvable else "")
        return done("person", f"a person decides: {text}", [line], "needs-a-person", f"a person decides: {text}")

    def integration(mode: str, **extra: Any) -> tuple[str, str]:
        """Fill out["integrate"]; (blocked, why) when the integration run could not start. `decide`
        means a changed protected path a person may approve (the gate still judges it)."""
        while True:
            seen = integrate.preview(layer, run_id, mode=mode, source=source, allow_protected=approved_paths,
                                     extra=extra)
            out["integrate"] = seen
            if seen["eligible"]:
                return "", ""
            if seen["reason"] == "already-integrating":
                return "running", seen["why"]
            if seen["reason"] == "protected-path" and mode != "conflict":
                text, path = protected_item(seen["why"])
                if path and path not in approved_paths and _decision_id(text) in assume:
                    out["decisions"].append({"id": _decision_id(text), "kind": "protected-path", "text": text,
                                             "path": path, "approvable": True, "approved": dict(ASSUMED)})
                    approved_paths.append(path)       # assumed approved: ask again with the path let through
                    continue
                return "decide", seen["why"]
            return "needs-a-person", f"integrate:{seen['reason']}: {seen['why']}"

    def protected_item(why: str) -> tuple[str, str]:
        found_path = re.search(r"changes (\S+), a protected path", why)
        path = found_path.group(1) if found_path else ""
        return f"{path or 'a file'} is a protected path, and this run changes it", path

    def protected(why: str) -> dict[str, Any]:
        text, path = protected_item(why)
        return person(text, True, "protected-path", path)

    workflow = str(adw.get("name") or state.get("adw") or "?")
    if stopped["status"] == "handed-over":
        # 3. the review passed
        if not rejected:
            blocked, why = integration("conflict")
            if not blocked:
                seen = out["integrate"]
                files = seen.get("conflicts")
                return done("integrate", f"bring it onto {target}: "
                            + ("the branch applies cleanly" if files == [] else
                               f"an agent resolves {_plural(len(files), 'conflicted file')}" if files else
                               "an agent resolves what conflicts")
                            + ", then the checks, the review and the gate",
                            [f"apply branch {seen['branch']} onto {target} in a fresh worktree",
                             "resolve the conflicts with an agent on the heavy tier" if files != [] else
                             "no conflict to resolve: the branch applies cleanly",
                             f"run the checks and the review of workflow {workflow}",
                             f"ship through the unchanged gate; run {run_id} is then settled as landed"])
            if blocked == "running":
                return done("none", "an agent is on it", blocked=blocked, why=why)
            if out["integrate"]["reason"] == "not-a-conflict":
                reasons = ", ".join(first_line(r)[:120] for r in _ship(state).get("handed_over")
                                    or _ship(state).get("reasons") or []) or "no reason recorded"
                return person(f"review and merge the pull request: {reasons}")
            return person(str(out["integrate"]["why"]))      # e.g. a protected path, a branch a person changed
        # 4. only proof is missing
        if _proof_only(out["blockers"]):
            out["found"] = uncollected(layer, work)
            blocked, why = integration("proof", produce=not out["found"])
            if blocked == "decide":
                return protected(why)
            if out["found"]:
                n = len(out["found"])
                summary = (f"the proof exists but was not collected: collect {_plural(n, 'file')} for this run and "
                           "ask the review again; no code changes")
                steps = [f"apply branch {out['integrate'].get('branch') or state.get('branch')} onto {target} in a "
                         "fresh worktree", f"hand the review {_plural(n, 'file')} as proof: {', '.join(out['found'])}",
                         "run the checks", "review once, with no patch round",
                         "ship through the unchanged gate, or back to you with the history"]
            else:
                out["commands"] = proof_commands(_plan_text(layer, state))
                n = len(out["commands"])
                summary = f"run the {_plural(n, 'command')} the plan lists for proof, then review"
                steps = [f"apply branch {out['integrate'].get('branch') or state.get('branch')} onto {target} in a "
                         "fresh worktree", "an agent runs the plan's proof commands and changes no code: "
                         + (", ".join(f"`{c}`" for c in out["commands"]) or "(the plan lists none: it finds them)"),
                         "collect the layer's artefacts", "run the checks", "review once, with no patch round",
                         "ship through the unchanged gate, or back to you with the history"]
            return done("proof", summary, steps, blocked, why)
        # 5. blockers
        blocked, why = integration("blockers")
        if blocked == "decide":
            return protected(why)
        n = len(out["blockers"])
        summary = ((f"bring it onto {target} first, then " if out["moved"] else "")
                   + f"address {_plural(n, 'blocker')} (the review's patch round) and review once more")
        steps = [f"apply branch {out['integrate'].get('branch') or work.get('branch')} onto {target} in a fresh "
                 "worktree" + (" and resolve what conflicts" if out["moved"] else ""),
                 "address the blockers with the review's patch round:", *_blocker_lines(out["blockers"]),
                 "run the checks again", "review once, with no further patch round",
                 "ship through the unchanged gate, or back to you with the history"]
        return done("blockers", summary, steps, blocked, why)

    # a failed, died or waiting run
    step = stopped["step"]
    worktree = _worktree(state)
    branch = str(state.get("branch") or "")
    declared = _declared(layer, str(state.get("adw") or ""))
    can_resume = worktree is not None and declared and bool(step)
    key = stopped["key"]
    if can_resume and key.startswith("check:"):            # 6
        agent = {}
        with contextlib.suppress(LayerError, KeyError):
            agent = layer.option("agents", str(adw.get("agent")))
        out["from_step"] = step
        out["tier"] = "heavy" if agent and models.has_tier(agent, "heavy") else ""
        return done("check", f"resume from check {step}: it runs again, and when it still fails a fresh fixing agent "
                             "gets its log" + (", on the heavy tier" if out["tier"] else ""),
                    [f"resume run {run_id} in its own worktree from check {step}",
                     "the check runs again; when it still fails a fresh fixing agent gets its log"
                     + (", on the heavy tier" if out["tier"] else ""),
                     "then the remaining steps: the review and the unchanged gate"])
    usage = (state.get("usage") or {}).get("stopped") if isinstance(state.get("usage"), dict) else None
    if can_resume and (key in ("login", "time-limit", "died") or key.startswith("waiting:") or usage):   # 7
        out["from_step"] = step
        return done("resume", f"resume from {step}, unchanged",
                    [f"resume run {run_id} in its own worktree from step {step}, unchanged",
                     "then the remaining steps: the checks, the review and the unchanged gate"])
    again = "start the same request again; the new run replaces it"
    has_branch = bool(branch) and bool(integrate._ref(app, f"refs/heads/{branch}"))
    if key == "nothing-changed" or not branch or (worktree is None and not has_branch):      # 8
        return done("start-again", f"it failed before changing anything: {again}",
                    [f"start workflow {workflow} again with the same request", f"the new run replaces run {run_id}"])
    if can_resume:                                         # 9
        out["from_step"] = step
        return done("resume", f"resume from {step}, unchanged",
                    [f"resume run {run_id} in its own worktree from step {step}, unchanged",
                     "then the remaining steps: the checks, the review and the unchanged gate"])
    gone = ("its worktree is gone, so it cannot be resumed" if worktree is None else
            "it is not a run of a declared workflow, so it cannot be resumed")
    return done("start-again", f"{gone}; branch {branch} is kept: {again}",
                [f"start workflow {workflow} again with the same request", f"the new run replaces run {run_id}",
                 f"branch {branch} is kept"])


def plan_lines(found: Mapping[str, Any]) -> list[str]:
    """The text of `al assign LAYER RUN --preview`."""
    stopped = found.get("stop") or {}
    lines = [f"run:        {found['run']}" + (f" ({found['title']})" if found.get("title") else ""),
             f"stopped:    {stopped.get('status')}" + (f" at {stopped['step']}" if stopped.get("step") else "")
             + (f": {stopped['why']}" if stopped.get("why") else ""),
             f"plan:       {found.get('summary')}"]
    numbered = 0
    for step in found.get("steps") or []:
        if str(step).startswith("   "):
            lines.append(f"  {step}")
        else:
            numbered += 1
            lines.append(f"  {numbered}. {step}")
    if found.get("decisions"):
        lines.append("decisions:")
        for item in found["decisions"]:
            state = (f"approved by {item['approved']['by']}" if item.get("approved") else
                     "open" if item.get("approvable") else "open, a person's only")
            lines.append(f"  {item['id']}  {state}: {item['text']}")
    if found.get("attempts"):
        lines.append("attempts:")
        for item in found["attempts"]:
            lines.append(f"  {item.get('n')}. {item.get('at')} by {item.get('by')}: {item.get('kind')}"
                         + (f", run {item['run']}" if item.get("run") else "") + f": {item.get('outcome')}"
                         + (f" ({item['why']})" if item.get("why") else ""))
    clicks = int(found.get("clicks", 0) or 0)
    limit = (f"limit:      {clicks} of {MAX_CLICKS} used" if clicks <= MAX_CLICKS else
             f"limit:      {MAX_CLICKS} of {MAX_CLICKS} used, and {clicks - MAX_CLICKS} more by --override")
    if found.get("blocked"):
        limit += f"; not started: {found['blocked']}: {found.get('blocked_why')}"
    lines.append(limit)
    return lines


# ---------------------------------------------------------------- approve

def check_who(who: Any) -> str:
    text = " ".join(str(who or "").split())
    if not text or len(text) > MAX_WHO or not text.isprintable():
        raise _Usage(f"--who: 1 to {MAX_WHO} printable characters (who approves)")
    return text


def approve(layer_dir: Path, run: str, item: str, who: str, *, via: str = "cli") -> dict[str, Any]:
    """A person approves exactly one "needs a person" item of a stopped run: recorded in
    agents/<run>/assign.json and in the run's events. Starts nothing; returns the new plan."""
    layer = ir.load(Path(layer_dir))
    who = check_who(who)
    found = plan(layer, run)
    chosen = next((d for d in found["decisions"] if d["id"] == str(item).strip()), None)
    if chosen is None:
        known = ", ".join(d["id"] for d in found["decisions"] if d["approvable"] and not d["approved"]) or "none"
        raise AssignError("bad-item", f"run {found['run']} has no item {item!r} to approve (open: {known})")
    if chosen["approved"]:
        raise AssignError("bad-item", f"item {chosen['id']} is approved already, by {chosen['approved']['by']}")
    if not chosen["approvable"]:
        raise AssignError("bad-item", f"item {chosen['id']} is a person's only and is never given to an agent: "
                                      f"{chosen['text']}")
    record = read_record(layer, found["run"])
    record["approvals"].append({"id": chosen["id"], "kind": chosen["kind"], "text": chosen["text"],
                                "path": chosen["path"], "by": who, "at": _stamp(), "via": via})
    _write_record(layer, found["run"], record)
    _event(layer, found["run"], "assign.approval", item=chosen["id"], kind=chosen["kind"], text=chosen["text"][:500],
           by=who, via=via)
    return plan(layer, found["run"])


# ---------------------------------------------------------------- give

def _instruction(found: Mapping[str, Any], layer: Any) -> str:
    """The request of the `address` step (integrate._address): the blockers in the format of the
    review's patch round, then what a person approved and added."""
    blockers = "\n".join(f"- {b['description']}\n  Resolution: {b['resolution']}" for b in found["blockers"])
    if found["kind"] == "proof":
        globs = ", ".join(str(o.get("glob")) for o in layer.ir.get("artefacts") or []
                          if isinstance(o, dict) and o.get("glob")) or "the layer's artefact globs"
        commands = "\n".join(f"- {command}" for command in found["commands"]) or "- (the plan lists none: find them)"
        text = ("The review rejected this change only because proof is missing:\n"
                f"{blockers}\n\nChange no application code. Run these commands the plan lists for proof and leave "
                f"their output where the layer collects artefacts ({globs}):\n{commands}")
    else:
        text = blockers
    for item in found["decisions"]:
        if item.get("approved"):
            text += f"\nApproved by a person: {item['text']} ({item['approved']['by']}, {item['approved']['at']})"
    if found.get("note"):
        text += f"\nThe person adds: {found['note']}"
    return text


def _new_id(layer_dir: Path) -> str:
    new_id = state_module.new_adw_id()
    while state_module.run_dir(layer_dir, new_id).exists():
        new_id = state_module.new_adw_id()
    return new_id


def _resume(layer: Any, found: Mapping[str, Any], n: int, *, detach: bool, dry_run: bool, model_set: str,
            spawn: Any) -> int | None:
    """Resume the run in place from `from_step` (the loop of decide.prepare_resume): every step
    from there on is pending again, the person's note goes into the prompt, and the layer's own
    script continues it. Returns the exit code of an attached resume."""
    from . import inputs

    layer_dir = Path(layer.layer_dir)
    state = state_module.load(layer_dir, found["run"])
    adw = layer.adw(state.adw)
    names = decide.step_names(adw)
    start = str(found.get("from_step") or "")
    reset = set(names[names.index(start):]) if start in names else set()
    for record in state.steps:
        if record.get("name") in reset or record.get("status") == "running":
            record["status"] = "pending"
    if found.get("note"):
        state.prompt = f"{state.prompt.rstrip()}\n\n## Given to an agent (attempt {n})\n{found['note']}"
    # what a reader sees until the resumed process writes its own state: running, as launch.start writes it
    state.status, state.ended, state.pid, state.host, state.heartbeat = "running", "", 0, "", state_module.now()
    state.save(layer_dir)
    tier = model_set or (str(found.get("tier") or "") if found["kind"] == "check" else "")
    argv = [sys.executable, str(inputs.adw_script(layer_dir, state.adw)), "--resume", "--adw-id", state.adw_id,
            "--decider", "off", *(["--model-set", tier] if tier else []),
            *(["--dry-run"] if dry_run or state.dry_run else [])]
    if not detach:
        process = spawn(argv, cwd=str(layer_dir))
        return int(process.wait())
    with (state_module.run_dir(layer_dir, state.adw_id) / RESUME_LOG).open("ab") as log:
        spawn(argv, cwd=str(layer_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
              start_new_session=True)
    return None


def _start_again(layer: Any, found: Mapping[str, Any], n: int, *, detach: bool, dry_run: bool, model_set: str,
                 spawn: Any) -> dict[str, Any]:
    from . import launch

    run_id = found["run"]
    state = _state(layer, run_id)
    try:
        prompt = (state_module.run_dir(layer.layer_dir, run_id) / launch.REQUEST_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        prompt = ""
    prompt = prompt or str(state.get("prompt") or "").strip()
    name = str(state.get("adw") or "")
    target = (launch.Target("command", name[len(launch.COMMAND_PREFIX):]) if name.startswith(launch.COMMAND_PREFIX)
              else launch.Target("workflow", name))
    result = launch.start(layer, target, prompt, request_key=f"assign-{run_id}-{n}", replaces=run_id,
                          confirm_duplicate=True, confirm_ships=True,
                          klass=str(state.get("klass")) if state.get("class_fixed") and state.get("klass") else None,
                          model_set=model_set or None, dry_run=dry_run or bool(state.get("dry_run")), detach=detach,
                          spawn=spawn)
    if result.get("status") != "started":
        raise LayerError(f"the request was not started again ({result.get('status')}): "
                         f"{result.get('message') or result.get('error') or 'no reason given'}")
    return result


def _stand_in_attempts(layer: Any, run_id: str) -> tuple[int, str]:
    """The stand-in's attempts over `run_id` and every run it replaces: (how many, the first run
    of the chain that has one, "" when none). A request that is started again is a new run with
    its own empty record, so the limit of the stand-in is counted over the whole chain."""
    count, where, seen, current = 0, "", set(), str(run_id or "")
    for _ in range(50):
        if not current or current in seen:
            break
        seen.add(current)
        mine = sum(1 for item in read_record(layer, current)["attempts"] if item.get("by") == "decider")
        if mine and not where:
            where = current
        count += mine
        state = _state(layer, current)
        if not isinstance(state, dict) or not state:
            break
        current = str(state.get("replaces") or "")
    return count, where


def give(layer_dir: Path, run: str, *, note: str = "", by: str = "cli", who: str = "", override: bool = False,
         detach: bool = True, dry_run: bool = False, model_set: str = "", spawn: Any = None,
         quiet: bool = False) -> dict[str, Any]:
    """Give `run` to an agent: start what plan() says. AssignError when the plan is blocked (the
    limit passes with `override`; the stand-in, by="decider", has its own limit and is never a
    click). The attempt is recorded and emitted before anything starts. Returns {status: started,
    kind, run, run_id: the run to watch, attempt, exit: an attached run's exit code}."""
    if by not in BYS:
        raise _Usage(f"--by: one of {', '.join(BYS)}")
    layer_dir = Path(layer_dir)
    layer = ir.load(layer_dir)
    spawn = spawn or _spawn
    found = plan(layer, run, note=note)
    run_id, kind = found["run"], found["kind"]
    if by == "decider":
        count, where = _stand_in_attempts(layer, run_id)
        if count >= MAX_STAND_IN:
            raise AssignError("limit", f"the stand-in gave run {run_id} to an agent already; it stays with a person"
                              if where == run_id else
                              f"the stand-in gave run {where}, which run {run_id} replaces, to an agent already; "
                              "it stays with a person")
        refused = found["blocked"] not in ("", "limit")
    else:
        refused = bool(found["blocked"]) and not (found["blocked"] == "limit" and override)
    if refused or kind in ("none", "person"):
        raise AssignError(found["blocked"] or "not-stopped", found["blocked_why"] or found["summary"])
    if kind in RESUME_KINDS + ("start-again",) and not (layer_dir / "adws").is_dir():
        raise AssignError("no-script", "the layer has no adws/ directory: run `al gen` on it")
    record = read_record(layer, run_id)
    n = len(record["attempts"]) + 1
    new_id = _new_id(layer_dir) if kind in INTEGRATION_KINDS else run_id if kind in RESUME_KINDS else ""
    attempt = {"n": n, "at": _stamp(), "by": by, "who": " ".join(str(who or "").split())[:MAX_WHO], "kind": kind,
               "note": found["note"], "run": new_id, "reason": found["stop"]["key"], "outcome": "started",
               "after": "", "why": ""}
    record["attempts"].append(attempt)
    _write_record(layer, run_id, record)
    _event(layer, run_id, "assign.attempt", n=n, by=by, kind=kind, run=new_id, reason=attempt["reason"],
           note=found["note"], override=bool(override and found["blocked"] == "limit"))
    result: dict[str, Any] = {"status": "started", "kind": kind, "run": run_id, "run_id": new_id, "attempt": n}
    was_dry = bool(_state(layer, run_id).get("dry_run"))
    try:
        if kind in INTEGRATION_KINDS:
            more: dict[str, Any] = {}
            if kind != "integrate":
                approved = [{"id": d["id"], "text": d["text"], "by": d["approved"]["by"], "at": d["approved"]["at"]}
                            for d in found["decisions"] if d.get("approved")]
                more = {"mode": kind, "extra": {
                    "instruction": _instruction(found, layer), "approved": approved, "note": found["note"],
                    "found": list(found["found"]), "produce": kind == "proof" and not found["found"],
                    "source": found["source"] if found["source"] != run_id else "",
                    "allow_protected": [d["path"] for d in found["decisions"] if d.get("approved") and d.get("path")]}}
            with contextlib.redirect_stdout(io.StringIO()) if quiet or detach else contextlib.nullcontext():
                code = integrate.run(layer_dir, run_id, model_set=model_set, dry_run=dry_run or was_dry, detach=detach,
                                     adw_id=new_id, by=by, spawn=spawn, **more)
            if not detach:
                result["exit"] = code
        elif kind in RESUME_KINDS:
            code = _resume(layer, found, n, detach=detach, dry_run=dry_run, model_set=model_set, spawn=spawn)
            if code is not None:
                result["exit"] = code
        else:
            started = _start_again(layer, found, n, detach=detach, dry_run=dry_run, model_set=model_set, spawn=spawn)
            result["run_id"] = attempt["run"] = str(started.get("run_id") or "")
            if "exit" in started:
                result["exit"] = started["exit"]
            record = read_record(layer, run_id)
            for item in record["attempts"]:
                if item.get("n") == n:
                    item["run"] = attempt["run"]
            _write_record(layer, run_id, record)
    except Exception as exc:
        record = read_record(layer, run_id)
        for item in record["attempts"]:
            if item.get("n") == n and item.get("outcome") == "started":
                item.update(outcome="stopped", why=f"it did not start: {first_line(exc)}"[:400])
                _write_record(layer, run_id, record)
                _event(layer, run_id, "assign.outcome", n=n, kind=kind, run=item.get("run"), outcome="stopped",
                       after="", why=item["why"])
        raise
    return result


# ---------------------------------------------------------------- all of them

def stopped_runs(layer: Any) -> list[str]:
    """Ids of the layer's runs that wait for a person: ended handed over, failed or waiting, or
    died; not settled, not started again by another run, not an integration or promotion run, and
    not a batch run (kit workflow verify-batch: `al batch` runs the full suites again)."""
    states: dict[str, dict[str, Any]] = {}
    for path in sorted((Path(layer.layer_dir) / "agents").glob("*/state.json")):
        data = integrate._read_json(path)
        if isinstance(data, dict) and data.get("adw_id"):
            states[path.parent.name] = data
    replaced = {str(data.get("replaces")) for data in states.values() if data.get("replaces")}
    found = []
    for run_id, data in states.items():
        if (data.get("adw") == integrate.ADW_NAME or data.get("integration") or data.get("promotion")
                or data.get("fanout_parent") or run_id in replaced or batch.is_batch_run(data)):
            continue
        if (state_module.run_dir(layer.layer_dir, run_id) / integrate.SETTLEMENT_FILE).is_file():
            continue
        if _status(layer, data) in ("handed-over", "failed", "died", "waiting"):
            found.append((str(data.get("started") or ""), run_id))
    return [run_id for _, run_id in sorted(found)]


def order(layer: Any, run_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """The plans of `run_ids` (default: every stopped run) in the order a bulk start gives them:
    first the ones that touch only their own worktree (check, resume, start-again), then the
    integration runs (integrate, proof, blockers; oldest run first), which the integrate lock runs
    one at a time. A plan that cannot start comes last, with `skip` saying why."""
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run_id in stopped_runs(layer) if run_ids is None else run_ids:
        try:
            found = plan(layer, run_id)
        except AssignError as exc:
            plans.append({"run": str(run_id), "kind": "none", "summary": exc.message, "title": "", "started": "",
                          "skip": f"{exc.reason}: {exc.message}"})
            continue
        if found["run"] in seen:
            continue
        seen.add(found["run"])
        found["started"] = str(_state(layer, found["run"]).get("started") or "")
        found["skip"] = (f"{found['blocked']}: {found['blocked_why']}" if found["blocked"] else
                         "nothing to give" if found["kind"] in ("none", "person") else "")
        plans.append(found)

    def rank(found: Mapping[str, Any]) -> tuple[int, int, str]:
        kind = found["kind"]
        if found["skip"]:
            return 2, 0 if kind == "person" else 1 if kind == "none" else 2, found["started"]
        if kind in INTEGRATION_KINDS:
            return 1, INTEGRATION_KINDS.index(kind), found["started"]
        return 0, 0, found["started"]

    return sorted(plans, key=rank)


def give_all(layer_dir: Path, *, by: str = "cli", who: str = "", dry_run: bool = False, spawn: Any = None,
             run_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Give every startable stopped run to an agent, detached, in order(); one result per run:
    {run, kind, status: started | skipped | error, run_id, why}."""
    layer = ir.load(Path(layer_dir))
    results = []
    for found in order(layer, run_ids):
        base = {"run": found["run"], "kind": found["kind"], "run_id": "", "why": ""}
        if found["skip"]:
            results.append({**base, "status": "skipped", "why": found["skip"]})
            continue
        try:
            started = give(layer_dir, found["run"], by=by, who=who, detach=True, dry_run=dry_run, spawn=spawn, quiet=True)
            results.append({**base, "status": "started", "run_id": started["run_id"]})
        except AssignError as exc:
            results.append({**base, "status": "skipped", "why": f"{exc.reason}: {exc.message}"})
        except (LayerError, StepFailed, OSError) as exc:
            results.append({**base, "status": "error", "why": first_line(exc)[:400]})
    return results


# ---------------------------------------------------------------- the stand-in

def after_run(layer: Any, state: Any, args: Any = None, spawn: Any = None) -> None:
    """Called by the runner after a run wrote its summary (beside decide.after_run). For a run that
    ended handed over, when the layer's templates/integrate.json says "decider": true and the
    decider's mode is `auto`, the stand-in starts what plan() says: the integration as before
    (integrate.after_run), or, once per run, the `blockers` or `proof` plan. No agent call decides
    this: the plan is rules. Never raises, never changes the ended run; an integration run starts
    nothing further."""
    try:
        if (getattr(state, "integration", None) or getattr(state, "adw", "") == integrate.ADW_NAME
                or getattr(state, "status", "") != "handed-over" or getattr(state, "fanout_parent", None)):
            return
        if not integrate.settings(layer.layer_dir)["decider"]:
            return
        if decide.mode_for(layer, getattr(args, "decider", None))[0] != "auto":
            return
        plain = ir.load(Path(layer.layer_dir))
        found = plan(plain, state.adw_id)
        if found["kind"] in ("blockers", "proof") and found["blocked"] in ("", "limit"):
            if any(item.get("by") == "decider" for item in found["attempts"]):
                print(f"[adw {state.adw_id}] decider: given to an agent once already; the run stays with a person",
                      flush=True)
                return
            result = give(Path(plain.layer_dir), state.adw_id, by="decider", detach=True,
                          dry_run=bool(getattr(state, "dry_run", False)), spawn=spawn, quiet=True)
            print(f"[adw {state.adw_id}] decider: given to an agent ({result['kind']}): run {result['run_id']}",
                  flush=True)
            return
        if found["kind"] in ("blockers", "proof", "person") and found["blocked"]:
            print(f"[adw {state.adw_id}] decider: not given to an agent ({found['blocked']}: {found['blocked_why']}); "
                  "the run stays with a person", flush=True)
            return
        integrate.after_run(layer, state, args, **({"spawn": spawn} if spawn is not None else {}))
    except Exception as exc:  # noqa: BLE001 — the run has ended; an attempt that does not start is not its failure
        print(f"[adw {getattr(state, 'adw_id', '?')}] decider: not given to an agent: {type(exc).__name__}: {exc}; "
              "the run stays with a person", flush=True)


# ---------------------------------------------------------------- the command line

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assign", description="Give a stopped run to an agent: the plan follows "
                                     "from why the run stopped; nothing ships without the checks, the review and "
                                     "the gate.")
    sub = parser.add_subparsers(dest="command", required=True)
    look = sub.add_parser("preview", help="what giving RUN to an agent would do; nothing is started")
    look.add_argument("run", metavar="RUN")
    look.add_argument("--note", default="")
    look.add_argument("--json", action="store_true")
    start = sub.add_parser("give", help="start the plan of RUN (a run id or its branch)")
    start.add_argument("run", metavar="RUN")
    start.add_argument("--note", default="")
    start.add_argument("--by", default="cli", choices=BYS)
    start.add_argument("--who", default="")
    start.add_argument("--override", action="store_true")
    start.add_argument("--detach", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--model-set", default="", metavar="TIER")
    start.add_argument("--json", action="store_true")
    ok = sub.add_parser("approve", help="a person approves one item of RUN; nothing starts")
    ok.add_argument("run", metavar="RUN")
    ok.add_argument("item", metavar="ITEM")
    ok.add_argument("--who", default="")
    ok.add_argument("--via", default="cli", choices=("cli", "dashboard"))
    ok.add_argument("--json", action="store_true")
    every = sub.add_parser("all", help="every stopped run of the layer (or the RUNs named), in the order they "
                           "are started")
    every.add_argument("runs", nargs="*", metavar="RUN")
    every.add_argument("--preview", action="store_true")
    every.add_argument("--by", default="cli", choices=BYS)
    every.add_argument("--who", default="")
    every.add_argument("--dry-run", action="store_true")
    every.add_argument("--json", action="store_true")
    return parser


def _order_line(found: Mapping[str, Any]) -> str:
    return (f"{found['run']}  {found['kind']:<11}  "
            + (f"skipped: {found['skip']}" if found.get("skip") else str(found.get("summary") or "")))


def main(layer_dir: Path, argv: list[str] | None = None, prog: str = "") -> int:
    """`adws/assign.py preview RUN | give RUN | approve RUN ITEM | all [RUN …]` (and `al assign`, which passes prog="al ").
    0 started or shown (an attached run: its exit code), 2 wrong use, 3 refused with the reason."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    name = f"{prog}assign"
    as_json = bool(getattr(args, "json", False))
    layer_dir = Path(layer_dir)
    try:
        if args.command == "preview":
            found = plan(ir.load(layer_dir), args.run, note=args.note)
            print(json.dumps(found) if as_json else "\n".join(plan_lines(found)))
            return 0
        if args.command == "approve":
            found = approve(layer_dir, args.run, args.item, args.who, via=args.via)
            if as_json:
                print(json.dumps(found))
            else:
                print(f"approved by {check_who(args.who)}: item {args.item} of run {found['run']}; nothing started")
                print("\n".join(plan_lines(found)))
            return 0
        if args.command == "all":
            if args.preview:
                plans = order(ir.load(layer_dir), list(args.runs) or None)
                print(json.dumps({"plans": plans}) if as_json else
                      "\n".join(_order_line(found) for found in plans) or "no stopped run")
                return 0
            results = give_all(layer_dir, by=args.by, who=args.who, dry_run=args.dry_run,
                               run_ids=list(args.runs) or None)
            print(json.dumps({"results": results}) if as_json else "\n".join(
                f"{r['run']}  {r['kind']:<11}  {r['status']}" + (f": run {r['run_id']}" if r["run_id"] else "")
                + (f": {r['why']}" if r["why"] else "") for r in results) or "no stopped run")
            return 0
        result = give(layer_dir, args.run, note=args.note, by=args.by, who=args.who, override=args.override,
                      detach=args.detach, dry_run=args.dry_run, model_set=args.model_set, quiet=as_json)
        if as_json:
            print(json.dumps(result))
        else:
            print(f"{name}: run {result['run']} given to an agent ({result['kind']}): "
                  + (f"run {result['run_id']}" if result["run_id"] != result["run"] else "resumed in place")
                  + f"; attempt {result['attempt']}", flush=True)
        return int(result.get("exit", 0))
    except (AssignError, IntegrateError) as exc:
        if as_json:
            print(json.dumps({"status": "error", "reason": exc.reason, "error": exc.message}))
        print(f"{name}: {exc.reason}: {exc.message}", file=sys.stderr)
        return REFUSED_EXIT
    except _Usage as exc:
        if as_json:
            print(json.dumps({"status": "error", "reason": "usage", "error": str(exc)}))
        print(f"{name}: error: {exc}", file=sys.stderr)
        return 2
    except (LayerError, StepFailed) as exc:
        if as_json:
            print(json.dumps({"status": "error", "reason": "error", "error": str(exc)}))
        print(f"{name}: error: {exc}", file=sys.stderr)
        return 1
