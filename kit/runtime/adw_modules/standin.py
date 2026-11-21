"""The stand-in beyond a resume: its scope, its limits, the record of every decision, and the
decisions it takes for a person outside a run.

decide.py holds the contract of a decision (validate), where it is stored (state.decision) and the
one entry for a person's answer (decide.answer). This module holds what the stand-in may do:

Decision kinds (KINDS). Each is a decision a person is asked for today:

- `resume`     a stopped run with a hand-off: run again from a step, unchanged or with an instruction;
- `restart`    a failed or died run: start the same request again (the new run records `replaces`);
- `answer`     a run waiting at a script or frozen step: continue it, optionally with an instruction;
- `integrate`  a handed-over run whose target moved: a new run of the workflow integrates its branch;
- `land`       a handed-over run whose automatic review passed: run its ship step again (the gate decides);
- `settle`     a finished run that needs nothing more: write settlement.json (a person can undo it);
- `promote`    a promotion waiting at approval: record approve or refuse as `stand-in` and continue it
               (the promotion's approval rule decides);
- `retry-job`  a failed dashboard job: retry it from its first step that did not pass (the dashboard does).

Scope (`scope`). The mode is decide.mode_for's. The kinds the stand-in takes alone (`may`) are the
declaration's #:may (without a declaration: `resume`), minus #:never, narrowed by the local
setting's optional `may`; every other kind it only suggests; a #:never kind is neither taken nor
suggested.

Limits. `limit_reached`: paused, #:max-actions-per-run (over a run and the runs it replaces),
#:max-per-hour (over the layer). `hard_limit`: reasons no setting lifts, checked after the model
answered and again before the action: a person touched the run since it stopped, the review rejected
the work, the change touches a protected path, a required check fails, its own work in a promotion,
a call that changed files, a `land` without the run's worktree. They hold for the stand-in only:
a person's go-ahead is the person's decision, and the gate still stands.

Record (`record`, `entries`). Every decision and answer is one JSON line in
agents/<id>/decisions.jsonl (with the run) and agents/stand-in/log.jsonl (the layer's log), and the
event `standin.decision` or `standin.action` of the run.

`consider` looks at one item of the dashboard's "Needs you" list (a run, or a failed job), one at a
time per layer (a lock on agents/stand-in/.lock); `apply` performs one action. `main` is the command
line of the generated adws/standin.py: the dashboard, `al decider` and the loop all go through it.
Never: a merge, a deploy or an approval in the gate's place. `land` and `promote` ask the gate again.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import decide, diskuse, events, state as state_module
from .errors import LayerError, StepFailed

KINDS = ("resume", "restart", "answer", "integrate", "settle", "land", "promote", "retry-job")
KIND_LINES = {
    "resume": "run a stopped run again from a step, unchanged or with an instruction",
    "restart": "start a failed or died run's request again",
    "answer": "continue a run that waits at a step",
    "integrate": "start a run that integrates a handed-over branch onto the moved target",
    "settle": "mark a finished run as landed, superseded or abandoned (a person can undo it)",
    "land": "ask the gate again for a handed-over run whose automatic review passed",
    "promote": "record approve or refuse on a waiting promotion; its approval rule decides",
    "retry-job": "retry a failed dashboard job from its first step that did not pass",
}
KIND_OF = {"resume": "resume", "resume-with": "resume", "restart": "restart", "answer": "answer",
           "integrate": "integrate", "land": "land", "settle": "settle", "promote": "promote", "retry-job": "retry-job"}
ACTION_LINES = {
    "resume": "run again from the step that stopped (or from `from_step`), unchanged; for a stop that looks "
              "accidental, such as a flaky check or a timeout",
    "resume-with": "run again from `from_step`, with `instructions` added to the run's request; for a stop that "
                   "one clear extra instruction fixes",
    "restart": "start the same request again as a new run (it records that it replaces this one); for a run "
               "that died or failed before it did useful work",
    "answer": "continue the waiting run (optionally with `instructions`); only when what it waited for is there",
    "integrate": "start a new run of the same workflow that integrates this run's branch onto the current "
                 "target; for work that is fine but whose target moved",
    "land": "run the ship step again: the gate decides on the current target; only when the automatic review "
            "passed and nothing else is open",
    "settle": "mark the run as finished with `settle_as`: `landed` (its work is on the target), `superseded` "
              "(`settle_by` names the run or commit that replaces it) or `abandoned` (nobody needs it); a person can undo it",
    "promote": "record `promote`: `approve` or `refuse` for the whole promotion, as the stand-in; the "
               "promotion's approval rule decides whether it merges",
    "retry-job": "retry the job from its first step that did not pass; for a failure that looks accidental",
    "ask-human": "you cannot decide, or the decision belongs to a person",
}
LOG_FILE = Path("agents") / "stand-in" / "log.jsonl"
LOCK_FILE = Path("agents") / "stand-in" / ".lock"
JOBS_DIR = Path("agents") / "stand-in" / "jobs"
RUN_FILE = "decisions.jsonl"
SETTLEMENT_FILE = "settlement.json"
PER_HOUR = 12
WHY_MAX = 2000
NOTE_MAX = 500
TAIL_LINES = 40
LOG_SHOWN = 30
GIT_TIMEOUT = 10
TOUCH_GRACE = 60          # a commit this many seconds after the run ended is the run's own (the hand-off)
RUN_ID = re.compile(r"^[0-9a-f]{8}$")
JOB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

TOUCHED = "a person touched the run since it stopped"
REJECTED = "the review rejected this work"
PROTECTED = "the change touches a protected path"
CHECK_FAILS = "a required check fails"
OWN_WORK = "the stand-in does not approve its own work"
CHANGED_FILES = "the stand-in changed files"
NO_WORKTREE = "the run's worktree is gone"
NEVER_ALL = "every decision here is in #:never"
NOT_IN_SCOPE = "not in scope"
RUNNING = "the run is running"
SAME_REASON = "stopped again for the same reason"
HARD_LIMITS = (
    "It never acts on a run a person touched since it stopped.",
    "It never overrides a rejected review.",
    "It never lands work that touches a protected path or has a failing required check.",
    "It never approves its own work.",
    "It never changes a file: a call that did is discarded.",
    "`land` and `promote` only ask the gate again; the gate and the promotion's approval rule decide.",
)

PROMPT = """\
# Stand in for a person: decide what happens to work that waits for one

You stand in for the person who would look at this item on the dashboard's "Needs you" list.
{situation}

Do not change any file: read only. Choose exactly one action:

{actions}

Prefer `ask-human` when you are unsure. Always choose `ask-human` for anything that needs a
credential, a secret, money or access you do not have, that touches a protected path, or that the
agents marked `REQUIRES HUMAN` as work only a person may do. You can never merge, deploy or approve
in the gate's place: every action here either sends the work round again, asks the gate again, or
leaves it with a person.

## The request

{request}

## Why it waits for a person

{reason}

## What the agents asked a person to do

{asks}

## The review and the gate

{review}

## The change

{diff}

## The end of the log

{log}

## Where the work stands

{landing}

## Earlier decisions for this item

{earlier}

## The workflow's steps, in order

{steps}

`from_step` is one of these names, or "" for the step that stopped.

## Your answer

End with exactly one fenced json block:

```json
{{"action": "{action_names}",
 "summary": "one line, at most 300 characters, for the person",
 "explanation": "why, in a few sentences",
 "confidence": 0.0,
 "instructions": "what the next round must do differently (required for resume-with, else empty)",
 "from_step": "a step name from the list above, or empty",
 "settle_as": "landed | superseded | abandoned (settle only)",
 "settle_by": "the run id or commit that replaces it (settle as superseded), else empty",
 "promote": "approve | refuse (promote only)"}}
```

`confidence` is how sure you are, from 0 to 1.
"""
SITUATIONS = {
    "stopped": "A run of the workflow `{adw}` (run `{run}`) stopped at step `{step}` and was handed to a person.",
    "failed": "A run of the workflow `{adw}` (run `{run}`) failed or died without a usable hand-off.",
    "waiting": "A run of the workflow `{adw}` (run `{run}`) waits at step `{step}` for something outside it.",
    "promotion": "A promotion (run `{run}`) waits for an approval.",
    "handed-over": "A run of the workflow `{adw}` (run `{run}`) ran to its end and was handed to a person "
                   "instead of shipping.",
    "job": "A dashboard job (`{run}`) failed or was interrupted.",
    "finished": "A run of the workflow `{adw}` (run `{run}`) has finished.",
}


class ApplyError(Exception):
    """An action that could not be performed; its text is the reason a person reads."""


class _ReadStack:
    """The environment of the stand-in's own call: on this machine, read only, no sandbox."""
    env: dict[str, str] = {}
    read_only = True
    sandbox = None
    agent_inside = False

    @staticmethod
    def wrap_agent(argv: list[str]) -> list[str]:
        return argv


# ---------------------------------------------------------------- small helpers

def _load(layer: Any) -> Any:
    if isinstance(layer, (str, Path)):
        from . import ir

        return ir.load(Path(layer))
    return layer


def _parse(stamp: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _adw_of(layer: Any, state: Any) -> dict[str, Any]:
    """The ADW of the run; a single-command or promotion run carries its own definition."""
    if state.definition:
        if not any(isinstance(a, dict) and a.get("name") == state.adw for a in layer.ir.get("adws") or []):
            layer.ir["adws"] = [*(layer.ir.get("adws") or []), dict(state.definition)]
        return dict(state.definition)
    return layer.adw(state.adw)


def _clean(layer: Any, state: Any, text: Any, limit: int) -> str:
    """Text for the record: no machine path, the home directory as `~`, clipped."""
    from . import ship

    text = str(text or "")
    try:
        text = ship._without_machine_paths(layer, state if state is not None else SimpleNamespace(worktree=""), text)
    except Exception:  # noqa: BLE001 — a record is written even when a path cannot be shortened
        pass
    return text.replace(str(Path.home()), "~")[:limit]


def fingerprint(state: Any) -> str:
    """What identifies one stop of a run: its status, when it ended and the step that failed."""
    failed = next((str(r.get("name")) for r in state.steps or [] if r.get("status") == "failed"), "")
    return f"{state.status}:{state.ended}:{failed}"


def job_fingerprint(job: dict[str, Any]) -> str:
    return f"job:{job.get('id')}:{int(job.get('retries') or 0)}"


# ---------------------------------------------------------------- the record

def _append(path: Path, obj: dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


def _read_lines(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        data = Path(path).read_bytes()
    except OSError:
        return out
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("id") and value.get("type"):
            out.append(value)
    return out


def _run_file(layer: Any, run: str) -> Path | None:
    if not RUN_ID.fullmatch(str(run or "")):
        return None
    directory = state_module.run_dir(layer.layer_dir, str(run))
    return directory / RUN_FILE if directory.is_dir() else None


def record(layer: Any, entry: dict[str, Any], state: Any = None) -> dict[str, Any]:
    """Append one entry to the layer's log and, when it names a run, to the run's decisions.jsonl,
    and emit the run's event. Returns the entry as written. Texts hold no machine path."""
    entry = {key: value for key, value in entry.items() if value is not None}
    entry.setdefault("id", secrets.token_hex(4))
    entry.setdefault("at", state_module.now())
    entry.setdefault("type", "decision")
    for key, limit in (("why", WHY_MAX), ("summary", decide.SUMMARY_MAX), ("outcome", NOTE_MAX), ("reason", NOTE_MAX)):
        if key in entry:
            entry[key] = _clean(layer, state, entry[key], limit)
    entry = json.loads(json.dumps(entry, default=str))            # exactly what a reader gets back
    _append(Path(layer.layer_dir) / LOG_FILE, entry)
    path = _run_file(layer, str(entry.get("run") or ""))
    if path is not None:
        _append(path, entry)
        if entry["type"] in ("decision", "answer"):
            _emit(layer, str(entry["run"]), entry)
    return entry


def _emit(layer: Any, run: str, entry: dict[str, Any]) -> None:
    type_ = "standin.action" if entry.get("status") == "acted" else "standin.decision"
    fields = {key: entry[key] for key in ("id", "who", "via", "kind", "action", "answer", "status", "auto", "outcome",
                                          "reason", "summary") if entry.get(key) not in (None, "")}
    fields["entry"] = fields.pop("id")
    target = events.path_for(layer.layer_dir, run)
    current = events._current
    if current is not None and Path(current.path) == target:      # inside the run: its own recorder
        events.emit(type_, **fields)
        return
    with events.recording(layer.layer_dir, run):
        events.emit(type_, **fields)


def entries(layer: Any, run: str | None = None, since: Any = None) -> list[dict[str, Any]]:
    """The entries of a run (its decisions.jsonl) or of the layer (the log), oldest first, each with
    the notes that name it folded in (its outcome, its stop). Bad lines are skipped."""
    if run:
        path = _run_file(layer, run)
        lines = _read_lines(path) if path is not None else []
    else:
        lines = _read_lines(Path(layer.layer_dir) / LOG_FILE)
    out: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line.get("type") == "note" and line.get("ref"):
            target = by_id.get(str(line["ref"]))
            if target is not None:
                target.update({key: line[key] for key in ("outcome", "stop", "reason") if key in line})
            continue
        out.append(line)
        by_id[str(line["id"])] = line
    if since:
        start = _parse(since)
        if start is not None:
            out = [e for e in out if (_parse(e.get("at")) or start) >= start]
    return out


def amend(layer: Any, entry_id: str, **fields: Any) -> None:
    """A note that changes an entry's outcome, stop or reason; readers fold it into the entry."""
    found = next((e for e in _read_lines(Path(layer.layer_dir) / LOG_FILE) if e.get("id") == entry_id), None)
    note = {"id": secrets.token_hex(4), "at": state_module.now(), "type": "note", "ref": entry_id, **fields}
    if "outcome" in note:
        note["outcome"] = _clean(layer, None, note["outcome"], NOTE_MAX)
    if found is not None and found.get("run"):
        note["run"] = found["run"]
    _append(Path(layer.layer_dir) / LOG_FILE, note)
    path = _run_file(layer, str(note.get("run") or ""))
    if path is not None:
        _append(path, note)


def outcome(layer: Any, entry_id: str, text: str) -> None:
    """What happened next to the action of entry `entry_id`."""
    amend(layer, entry_id, outcome=text)


# ---------------------------------------------------------------- scope

def _since(layer: Any) -> str:
    """When the stand-in was last switched on: the last `mode` entry of the log that is not `off`,
    else the settings file's time, else ""."""
    last = ""
    for line in _read_lines(Path(layer.layer_dir) / LOG_FILE):
        if line.get("type") == "mode" and line.get("mode") not in ("off", "", None) and not line.get("scope_only"):
            last = str(line.get("at") or "")
    if last:
        return last
    try:
        mtime = (Path(layer.layer_dir) / decide.SETTINGS_FILE).stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat()


def scope(layer: Any, flag: str | None = None) -> dict[str, Any]:
    """What the stand-in may do in this layer now; see the module docstring."""
    mode, source = decide.mode_for(layer, flag)
    setting = decide.read_setting(Path(layer.layer_dir))
    declared = (getattr(layer, "ir", None) or {}).get("standIn") or None
    never = [str(k) for k in (declared or {}).get("never") or [] if k in KINDS]
    offered = [str(k) for k in ((declared or {}).get("may") if declared else ("resume",)) or [] if k in KINDS and k not in never]
    may = [k for k in offered if setting["may"] is None or k in setting["may"]]
    return {
        "mode": mode, "source": source, "declared": dict(declared) if declared else None,
        "offered": offered, "never": never, "may": may,
        "suggests": [k for k in KINDS if k not in may and k not in never],
        "per_run": decide.per_run(layer),
        "per_hour": int((declared or {}).get("maxPerHour") or PER_HOUR),
        "escalate": [str(x) for x in (declared or {}).get("escalateTo") or ["dashboard"]],
        "model_set": str((declared or {}).get("modelSet") or "heavy"),
        "paused": bool(setting["paused"]), "since": _since(layer),
    }


# ---------------------------------------------------------------- situations, eligibility, limits

def _worktree_gone(state: Any) -> bool:
    """The run had a worktree and it is no longer there: nothing can continue in it."""
    return bool(state.worktree) and not Path(state.worktree).is_dir()


def situation(layer: Any, state: Any, item: dict[str, Any]) -> tuple[str, list[str]]:
    """(the name of the situation, the actions offered in it). An action that continues in the run's
    worktree (resume, resume-with, answer, land) is not offered once that worktree is gone."""
    name, actions = _situation(layer, state, item)
    if _worktree_gone(state):
        actions = [a for a in actions if a not in ("resume", "resume-with", "answer", "land")]
    return name, actions


def _situation(layer: Any, state: Any, item: dict[str, Any]) -> tuple[str, list[str]]:
    if isinstance(item.get("job"), dict):
        return "job", ["retry-job", "ask-human"]
    status = str(state.status)
    ship = state.ship or {}
    if status == "waiting":
        return ("promotion", ["promote", "ask-human"]) if state.promotion else ("waiting", ["answer", "ask-human"])
    if status == "handed-over":
        actions = ["integrate", "resume-with", "settle", "ask-human"]
        try:
            adw = _adw_of(layer, state)
            option = layer.option("reviews", adw["review"]) if adw.get("review") else {}
        except (LayerError, KeyError):
            option = {}
        passed = ((state.review or {}).get("verdict") or {}).get("success") is True
        return "handed-over", (["land", *actions] if option.get("kind") == "auto" and passed else actions)
    if status == "failed" or item.get("status") == "died" or status == "running":
        usable = (status == "failed" and ship.get("action") == "handoff" and not ship.get("handoff_error")
                  and item.get("status") != "died")
        if usable:
            return "stopped", ["resume", "resume-with", "restart", "settle", "ask-human"]
        return "failed", ["restart", "settle", "ask-human"]
    return "finished", ["settle", "ask-human"]


def offered_in_scope(offered: list[str], scope_: dict[str, Any]) -> list[str]:
    """The offered actions without those whose kind is in #:never."""
    return [action for action in offered if KIND_OF.get(action) not in scope_["never"]]


def _alive(state: Any) -> bool:
    beat = _parse(state.heartbeat)
    if beat is None:
        return False
    return (datetime.now(timezone.utc) - beat).total_seconds() < state_module.HEARTBEAT_SECONDS * 12


def not_eligible(layer: Any, state: Any, adw: dict[str, Any], item: dict[str, Any]) -> str:
    """Why the stand-in never looks at this item ("" when it may): as decide.eligible, for every situation."""
    if isinstance(item.get("job"), dict):
        return ""
    if state.fanout_parent:
        return "a fan-out candidate (its parent decides)"
    if str(adw.get("changes") or "codebase") == "layer":
        return "the workflow changes the layer (a person always approves)"
    if (state.budget or {}).get("stopped"):
        return "a spending limit stopped the run (money is a person's decision)"
    if (state.usage or {}).get("stopped"):
        return "a subscription usage limit stopped the run (a person's decision)"
    if state.status == "running" and _alive(state):
        return RUNNING
    return ""


def _reasons(state: Any) -> list[str]:
    ship = state.ship or {}
    return [str(r) for r in [*(ship.get("reasons") or []), *(ship.get("handed_over") or [])]]


def touched(layer: Any, state: Any) -> bool:
    """True when a person did something to the run after it ended: an answer or another recorded
    action, a settlement that is not the stand-in's, or a commit on its branch."""
    ended = _parse(state.ended)
    if ended is None:
        return False
    for entry in entries(layer, run=state.adw_id):
        at = _parse(entry.get("at"))
        if entry.get("who") == "person" and at is not None and at > ended:
            return True
    settlement = _read_json(state_module.run_dir(layer.layer_dir, state.adw_id) / SETTLEMENT_FILE)
    if isinstance(settlement, dict) and settlement.get("via") != "stand-in":
        return True
    if state.branch:
        try:
            done = subprocess.run(["git", "-C", str(layer.app_root), "log", "-1", "--format=%ct", state.branch, "--"],
                                  capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
            if done.returncode == 0 and done.stdout.strip().isdigit():
                return int(done.stdout.strip()) > ended.timestamp() + TOUCH_GRACE
        except (OSError, subprocess.SubprocessError):
            pass
    return False


def hard_limit(layer: Any, state: Any, action: str, fields: dict[str, Any], item: dict[str, Any] | None = None) -> str:
    """Why the stand-in may not take this action by itself; "" when it may. No setting lifts these."""
    from . import steps

    if KIND_OF.get(action) is None or isinstance((item or {}).get("job"), dict):
        return ""
    if touched(layer, state):
        return TOUCHED
    reasons = _reasons(state)
    landed = action == "settle" and fields.get("settle_as") == "landed"
    lands = action in ("land", "integrate") or landed
    rejected = (steps.review_outcome(state)[0] is False or "review-rejected" in reasons
                or any(r.startswith("review-failed") for r in reasons))
    if rejected:
        if lands or action == "promote":
            return REJECTED
        if action in ("resume", "resume-with"):
            try:
                names = decide.step_names(_adw_of(layer, state))
                kinds = {str(s.get("name")): str(s.get("kind")) for s in _adw_of(layer, state).get("steps", [])}
            except LayerError:
                names, kinds = [], {}
            start = str(fields.get("from_step") or fields.get("stopped_at") or "")
            review_at = next((i for i, name in enumerate(names) if kinds.get(name) == "review"), None)
            if review_at is not None and start in names and names.index(start) > review_at:
                return REJECTED
    if any(r.startswith("protected-path:") for r in reasons) and (lands or action == "promote"):
        return PROTECTED
    failing = (any(r.startswith(("check-failed:", "require-check-failed:")) for r in reasons)
               or any(int((check or {}).get("exit") or 0) != 0 for check in (state.checks or {}).values()))
    if failing and lands:
        return CHECK_FAILS
    if action == "promote" and fields.get("promote") == "approve":
        for change in (state.promotion or {}).get("changes") or []:
            run = str((change or {}).get("id") or "") if isinstance(change, dict) else ""
            if RUN_ID.fullmatch(run) and any(e.get("who") == "stand-in" and e.get("status") == "acted"
                                              for e in entries(layer, run=run)):
                return OWN_WORK
    if action == "land" and not (state.worktree and Path(state.worktree).is_dir()):
        return NO_WORKTREE
    if action in ("resume", "resume-with", "answer") and _worktree_gone(state):
        return NO_WORKTREE
    return ""


def _chain(layer: Any, state: Any) -> list[str]:
    """The run and every run it replaces, newest first."""
    ids, current = [state.adw_id], str(state.replaces or "")
    while RUN_ID.fullmatch(current) and current not in ids and len(ids) < 50:
        ids.append(current)
        data = _read_json(state_module.run_dir(layer.layer_dir, current) / "state.json")
        current = str((data or {}).get("replaces") or "") if isinstance(data, dict) else ""
    return ids


def _acted(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in found if e.get("who") == "stand-in" and e.get("status") == "acted"]


def history(layer: Any, state: Any, job: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The stand-in's and people's entries for the run and the runs it replaces (a job: its entries), oldest first."""
    if job is not None:
        return [e for e in entries(layer) if e.get("job") == job.get("id")]
    found: list[dict[str, Any]] = []
    for run in reversed(_chain(layer, state)):
        found += entries(layer, run=run)
    return found


def history_lines(found: list[dict[str, Any]]) -> list[str]:
    return [f"- {e.get('at', '')[:19]} {e.get('who', '?')} {e.get('action') or e.get('answer') or e.get('type')}"
            f" ({e.get('status', '')})" + (f": {e['outcome']}" if e.get("outcome") else "")
            + (f" — {e['reason']}" if e.get("reason") else "") for e in found]


def limit_reached(layer: Any, state: Any, scope_: dict[str, Any] | None = None, job: dict[str, Any] | None = None) -> str:
    """Why the stand-in takes no further action by itself now ("" when it may)."""
    scope_ = scope_ or scope(layer)
    if scope_["paused"]:
        return "paused"
    count = len(_acted(history(layer, state, job)))
    if count >= scope_["per_run"]:
        return f"the stand-in acted {count} times on this run (limit {scope_['per_run']})"
    hour = datetime.now(timezone.utc) - timedelta(seconds=3600)
    recent = len(_acted(entries(layer, since=hour.isoformat())))
    if recent >= scope_["per_hour"]:
        return f"the stand-in acted {recent} times this hour (limit {scope_['per_hour']})"
    return ""


# ---------------------------------------------------------------- the prompt

def _tail(path: Path, lines: int = TAIL_LINES) -> str:
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _diff_summary(layer: Any, state: Any) -> str:
    if not state.branch:
        return ""
    target = str((state.delivery or {}).get("branch") or layer.default_branch)
    try:
        done = subprocess.run(["git", "-C", str(layer.app_root), "diff", "--stat", f"{target}...{state.branch}", "--"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return "\n".join(done.stdout.splitlines()[-TAIL_LINES:]) if done.returncode == 0 else ""


def render_prompt(layer: Any, state: Any, adw: dict[str, Any], item: dict[str, Any], name: str,
                  offered: list[str]) -> tuple[str, list[str]]:
    """(the prompt, the labels of the sections that had content: what the stand-in read)."""
    read: list[str] = []
    job = item.get("job") if isinstance(item.get("job"), dict) else None
    ship = state.ship or {}
    waiting = next((str(r.get("name")) for r in state.steps or [] if r.get("status") == "waiting"), "")
    step = str(ship.get("stopped_at") or waiting or (job or {}).get("failed_step") or "?")
    reason = "\n".join(part for part in (
        (decide._stop_reason(state).strip() if job is None and state.status != "handed-over" else ""),
        str(item.get("reason") or (job or {}).get("reason") or "").strip(),
        str(getattr(state, "error", "") or "").strip() if state.status == "waiting" else "") if part)
    if reason:
        read.append("why it stopped")
    asks = "" if job is not None else decide._asks(layer, state)
    target = str(ship.get("target") or "")
    handoff = Path(layer.layer_dir) / target if target and not re.match(r"https?://", target) else None
    if handoff is not None and handoff.is_file():
        read.append("PR.md")
        asks += f"\n\nThe hand-off a person reads is at {handoff}."
    plan = Path(state.plan) if state.plan else None
    if plan is not None and not plan.is_absolute():
        plan = Path(layer.layer_dir) / plan
    if plan is not None and plan.is_file():
        read.append("the plan")
        asks += f"\n\nThe plan is at {plan}."
    verdict = (state.review or {}).get("verdict") or {}
    review = []
    if verdict:
        read.append("review verdict")
        blockers = sum(1 for issue in verdict.get("issues") or [] if issue.get("severity") == "blocker")
        review.append(f"Review: {'passed' if verdict.get('success') else 'rejected'}, {blockers} blocker(s). "
                      f"{str(verdict.get('summary') or '')[:600]}")
    reasons = _reasons(state)
    if reasons:
        read.append("gate reasons")
        review.append("Gate reasons: " + "; ".join(dict.fromkeys(reasons)))
    diff = "" if job is not None else _diff_summary(layer, state)
    if diff:
        read.append("diff summary")
    log = str((job or {}).get("log") or "") if job is not None else _tail(
        state_module.run_dir(layer.layer_dir, state.adw_id) / "run.log")
    if log:
        read.append("job log" if job is not None else f"run.log (last {TAIL_LINES} lines)")
    landing = item.get("landing") if isinstance(item.get("landing"), dict) else {}
    landing_text = ""
    if landing and landing.get("state"):
        read.append("landing")
        landing_text = (f"Landing: {landing.get('state')}" + (f", target {landing['target']}" if landing.get("target") else "")
                        + (f", differs: {landing['differ']}" if landing.get("differ") else ""))
    if item.get("replaced_by"):
        landing_text += f"\nReplaced by run {item['replaced_by']}."
    earlier = history_lines(history(layer, state, job))
    if earlier:
        read.append("earlier decisions")
    names = decide.step_names(adw) if job is None else []
    prompt = PROMPT.format(
        situation=SITUATIONS[name].format(adw=state.adw, run=(job or {}).get("id") or state.adw_id, step=step),
        actions="\n".join(f"- `{action}`: {ACTION_LINES[action]};" for action in offered),
        request=(state.prompt.strip() if job is None else f"Job `{job.get('action')}`") or "(empty)",
        reason=reason or "(none recorded)", asks=asks.strip() or "Nothing.",
        review="\n".join(review) or "No review verdict and no gate reason.",
        diff=diff or "(no diff to show)", log=log or "(no log)", landing=landing_text.strip() or "(unknown)",
        earlier="\n".join(earlier) or "None.",
        steps="\n".join(f"{index}. `{step_name}`" for index, step_name in enumerate(names, start=1)) or "(none)",
        action_names=" | ".join(offered))
    return prompt, read


# ---------------------------------------------------------------- consider

def _git_state(cwd: Path) -> tuple[str, str]:
    out = []
    for args in (["status", "--porcelain"], ["rev-parse", "HEAD"]):
        try:
            done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                                  timeout=GIT_TIMEOUT * 3, check=False)
            out.append(done.stdout if done.returncode == 0 else "")
        except (OSError, subprocess.SubprocessError):
            out.append("")
    return out[0], out[1]


def _job_state(layer: Any, item: dict[str, Any]) -> Any:
    adws = [a for a in layer.ir.get("adws") or [] if isinstance(a, dict) and a.get("name")]
    if not adws:
        raise LayerError("the layer declares no workflow, so the stand-in has no agent to ask")
    job = item["job"]
    if not JOB_ID.fullmatch(str(job.get("id") or "")):
        raise LayerError("a job id is 1 to 80 letters, digits, dots, dashes or underscores")
    run = hashlib.sha1(str(job["id"]).encode("utf-8")).hexdigest()[:8]
    return state_module.ADWState(adw_id=run, adw=str(adws[0]["name"]), source=f"job:{job['id']}",
                                 dry_run=bool(item.get("dry_run")))


def consider(layer: Any, item: dict[str, Any], *, force_suggest: bool = False, via: str = "loop") -> dict[str, Any]:
    """Let the stand-in look at one item (`{"run": ID, …}` or `{"job": {...}}`): at most one decision
    per stop, one item at a time per layer. `force_suggest` ("Ask the stand-in") runs it in suggest
    mode whatever the layer's mode. Returns {"status", "entry", "action", "kind", "summary", "perform"};
    status is acted, suggested, refused, escalated, failed, skipped, seen or off. Never raises."""
    layer = _load(layer)
    lock = Path(layer.layer_dir) / LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            return _consider(layer, dict(item), force_suggest, via)
        except Exception as exc:  # noqa: BLE001 — the stand-in never breaks what it looks at
            return _result("failed", reason=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _result(status: str, entry: dict[str, Any] | None = None, **more: Any) -> dict[str, Any]:
    entry = entry or {}
    return {"status": status, "entry": entry or None, "action": entry.get("action", ""), "kind": entry.get("kind", ""),
            "summary": entry.get("summary", ""), "perform": None, **more}


def _call_cost(state: Any) -> Any:
    calls = [c for c in state.agent_calls or [] if c.get("step") == decide.STEP]
    return calls[-1].get("cost") if calls else None


def _consider(layer: Any, item: dict[str, Any], force_suggest: bool, via: str) -> dict[str, Any]:
    from . import agents

    job = item.get("job") if isinstance(item.get("job"), dict) else None
    try:
        state = _job_state(layer, item) if job is not None else state_module.load(layer.layer_dir, str(item.get("run") or ""))
        adw = _adw_of(layer, state)
        scope_ = scope(layer, None if job is not None else (state.decider or None))
    except LayerError as exc:
        return _result("failed", reason=str(exc)[:500])
    if scope_["mode"] == "off" and not force_suggest:
        return _result("off")
    if scope_["paused"] and not force_suggest:
        return _result("skipped", reason="paused")
    mode = "suggest" if force_suggest and scope_["mode"] == "off" else scope_["mode"]
    stop = job_fingerprint(job) if job is not None else fingerprint(state)
    base: dict[str, Any] = {"type": "decision", "who": "stand-in", "via": via, "mode": mode, "stop": stop,
                            **({"job": str(job["id"])} if job is not None else {"run": state.adw_id})}
    earlier = history(layer, state, job)
    mine = [e for e in earlier if e.get("who") == "stand-in" and e.get("type") == "decision"]
    if not force_suggest and (any(e.get("stop") == stop for e in mine) or (state.decision or {}).get("entry_pending")):
        return _result("seen")
    if job is None and (state.decision or {}).get("status") == "resuming" and state.status != "running":
        return _result("skipped", reason="a resume of the run was started")     # not a new stop: nothing to record
    if job is None:
        held = state_module.run_holder(layer.layer_dir, state.adw_id)
        if held:   # the run's process lives (e.g. in its own decide step): it decides its own stop
            reason = f"the run's process {held.get('pid')} is alive: it decides its own stop"
            decide.note_skipped(layer.layer_dir, state.adw_id, "stand-in", reason)
            return _result("skipped", reason=reason)
    why = not_eligible(layer, state, adw, item)
    if why == RUNNING:
        return _result("skipped", reason=why)                     # it will stop again: that stop is looked at
    if why:
        return _result("skipped", record(layer, {**base, "status": "skipped", "reason": why}, state))
    name, offered = situation(layer, state, item)
    offered = offered_in_scope(offered, scope_)
    base["situation"] = name
    if offered in ([], ["ask-human"]):
        return _result("skipped", record(layer, {**base, "status": "skipped", "reason": NEVER_ALL}, state))
    if job is None and not force_suggest:   # one party decides each stop: the run's decide step or this loop
        lost = decide.claim_stop(layer.layer_dir, state.adw_id, "stand-in")
        if lost:
            reason = f"the stop was decided by {lost}"
            decide.note_skipped(layer.layer_dir, state.adw_id, "stand-in", reason)
            return _result("skipped", reason=reason)
    # numbers (an attempt, a log's name, a duration) do not make a reason a different one
    stopped_for = re.sub(r"\d+", "#", str(job.get("reason") or "") if job is not None else decide._stop_reason(state))[:300]
    base["stopped_for"] = stopped_for
    last = (_acted(earlier) or [None])[-1]
    if last is not None and last.get("stopped_for") and last["stopped_for"] == stopped_for and last.get("outcome") != SAME_REASON:
        outcome(layer, str(last["id"]), SAME_REASON)
        earlier = history(layer, state, job)

    # ---- the call: read only, in the run's worktree or the application root
    prompt, read = render_prompt(layer, state, adw, item, name, offered)
    names = decide.step_names(adw)
    previous = dict(state.decision or {})
    previous.pop("skipped", None)
    kept_worktree = state.worktree
    if state.worktree and not Path(state.worktree).is_dir():
        state.worktree = ""                                       # the call runs in the application root
    cwd = agents.work_dir(layer, state)
    before = _git_state(cwd)
    found: dict[str, Any] = {}
    failure = ""
    try:
        if job is not None:
            output = agents.run_prompt(layer, state, decide.STEP, decide.COMMAND, prompt, scope_["model_set"],
                                       _ReadStack(), tier_from="stand-in",
                                       directory=Path(layer.layer_dir) / JOBS_DIR / str(job["id"]) / decide.STEP)
        else:
            with events.recording(layer.layer_dir, state.adw_id):
                output = agents.run_prompt(layer, state, decide.STEP, decide.COMMAND, prompt, scope_["model_set"],
                                           _ReadStack(), tier_from="stand-in")
        found = decide.validate(json.loads(output), names, allowed=offered)
    except (StepFailed, LayerError, ValueError, OSError) as exc:
        failure = (str(exc.reason) if isinstance(exc, StepFailed) else f"{type(exc).__name__}: {exc}")[:500]
    finally:
        state.worktree = kept_worktree
        if job is not None:                                       # a job has no run: nothing stays under agents/<id>
            leftover = state_module.run_dir(layer.layer_dir, state.adw_id)
            for directory in (leftover / "scratch", leftover):
                with contextlib.suppress(OSError):
                    directory.rmdir()
    if not failure and _git_state(cwd) != before:
        failure = CHANGED_FILES
    base["cost"] = _call_cost(state)
    if failure:
        entry = record(layer, {**base, "status": "failed", "reason": failure, "read": read}, state)
        if job is None:
            state.decision = {**previous, "mode": mode, "status": "failed", "reason": failure}
            state.decision.pop("action", None)
            _escalate(layer, state, scope_, failure, earlier)
            decide._save(layer, state)
        return _result("failed", entry, reason=failure)

    # ---- what to do with the answer
    action = str(found["action"])
    kind = KIND_OF.get(action, "")
    stopped_at = str((state.ship or {}).get("stopped_at") or "")
    decision = {"mode": mode, "mode_from": scope_["source"], **found, "kind": kind, "situation": name, "read": read,
                "by": "stand-in", "from_step": found["from_step"] or (stopped_at if kind == "resume" else ""),
                "stopped_at": stopped_at, "code": secrets.token_urlsafe(9), "rounds": int(previous.get("rounds", 0) or 0),
                "answers": list(previous.get("answers") or []), "at": state_module.now(), "status": "suggested"}
    if job is not None:
        decision["from_step"] = str(job.get("failed_step") or "")
    shown = {**base, "kind": kind, "action": action, "summary": found["summary"], "why": found["explanation"],
             "confidence": found.get("confidence"), "read": read,
             **{key: decision[key] for key in ("from_step", "instructions", "settle_as", "settle_by", "promote") if decision.get(key)}}

    def finish(status: str, reason: str = "", escalate: bool = False) -> dict[str, Any]:
        # a suggestion a limit held back stays answerable; ask-human and abandon are decided; a refusal is final
        decision["status"] = "refused" if status == "refused" else "decided" if not kind else "suggested"
        if reason:
            decision["held"] = reason
        entry = record(layer, {**shown, "status": status, "auto": False, **({"reason": reason} if reason else {})}, state)
        if job is None:
            state.decision = decision
            decide.write_record(layer, state)
            if escalate:
                _escalate(layer, state, scope_, reason or found["summary"], earlier)
            decide._save(layer, state)
        return _result(status, entry, reason=reason)

    if not kind:                                                  # ask-human, abandon: a person decides
        return finish("escalated", "the stand-in asks a person", escalate=True)
    refused = hard_limit(layer, state, action, decision, item)
    if refused:
        return finish("refused", refused, escalate=True)
    if kind not in scope_["may"] or mode == "suggest" or force_suggest:
        return finish("suggested")
    limit = limit_reached(layer, state, scope_, job)
    if limit:
        return finish("escalated", limit, escalate=True)
    if job is None:
        state.decision = decision
    return apply(layer, state, decision, who="stand-in", via=via, item=item, shown=shown)


def _escalate(layer: Any, state: Any, scope_: dict[str, Any], reason: str, earlier: list[dict[str, Any]]) -> None:
    """An item the stand-in hands to a person: with `notify` declared, one message through the
    notifiers that list needs-human, with the reason and the history."""
    if "notify" not in scope_["escalate"]:
        return
    from . import notify

    lines = [f"The stand-in hands run {state.adw_id} to a person: {reason}"]
    if earlier:
        lines += ["", "What it did before:", *history_lines(earlier)]
    with contextlib.redirect_stdout(io.StringIO()):
        notify.send_decision(layer, state, _clean(layer, state, "\n".join(lines), decide.EXPLAIN_MAX))


# ---------------------------------------------------------------- apply: one action

def _target(state: Any) -> Any:
    from . import launch

    name = str(state.adw)
    if name.startswith(launch.COMMAND_PREFIX):
        return launch.Target("command", name[len(launch.COMMAND_PREFIX):])
    return launch.Target("workflow", name)


def _request_text(layer: Any, state: Any) -> str:
    from . import launch

    try:
        text = (state_module.run_dir(layer.layer_dir, state.adw_id) / launch.REQUEST_FILE).read_text(encoding="utf-8")
    except OSError:
        text = ""
    return text.strip() or re.split(r"\n\n## Decision \(", str(state.prompt), maxsplit=1)[0].strip()


def _with_decision(text: str, decision: dict[str, Any], extra: str, who: str) -> str:
    more = "\n\n".join(part for part in (str(decision.get("instructions") or "").strip(),
                                         f"The person adds: {extra.strip()}" if extra.strip() else "") if part)
    if not more:
        return text
    return f"{text.rstrip()}\n\n## Decision ({'answered by a person' if who == 'person' else 'the stand-in'})\n{more}"


def _launch(layer: Any, state: Any, prompt: str, key: str, replaces: str) -> str:
    from . import launch

    result = launch.start(layer, _target(state), prompt, request_key=key,
                          klass=state.klass if state.class_fixed else None, model_set=state.model_set or None,
                          confirm_duplicate=True, confirm_ships=True, dry_run=state.dry_run, replaces=replaces,
                          decider=state.decider, spawn=lambda argv, **kw: launch._spawn(argv, **kw))
    if result.get("status") != "started" or not result.get("run_id"):
        raise ApplyError(str(result.get("message") or result.get("error") or result.get("status")))
    return str(result["run_id"])


def _spawn_detached(argv: list[str], cwd: Path, log: Path) -> None:
    with Path(log).open("ab") as handle:
        subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                         start_new_session=True)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def settle(layer: Any, state: Any, as_: str, by: str = "", note: str = "", who: str = "stand-in",
           via: str = "stand-in") -> dict[str, Any]:
    """Write agents/<id>/settlement.json in the dashboard's format (dashboard.write_settlement); the
    earlier record stays under `previous`, so a person can undo it. Then the run's worktree goes
    (diskuse.release_worktree; its branch, agents/<id> and artefacts/<id> stay)."""
    if as_ not in decide.SETTLE_AS:
        raise ApplyError(f"as is one of {', '.join(decide.SETTLE_AS)}")
    by = str(by or "")
    if by and not (RUN_ID.fullmatch(by) and (state_module.run_dir(layer.layer_dir, by) / "state.json").is_file()):
        by = ""                                                   # only a run of this layer is kept
    record_ = {"as": as_, "by": by, "note": " ".join(str(note or "").split())[:NOTE_MAX], "who": who,
               "at": utc_stamp(), "via": via}
    path = state_module.run_dir(layer.layer_dir, state.adw_id) / SETTLEMENT_FILE
    previous = _read_json(path)
    if isinstance(previous, dict) and previous.get("as") in decide.SETTLE_AS:
        record_["previous"] = {k: v for k, v in previous.items() if k not in ("previous", "pruned")}
        if previous.get("pruned"):
            record_["pruned"] = previous["pruned"]
    temporary = path.with_name(f".settlement.{secrets.token_hex(4)}.json")
    try:
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(record_, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    with contextlib.suppress(OSError, subprocess.SubprocessError):   # settling never fails because of a worktree
        diskuse.release_worktree(layer.layer_dir, layer.app_root, state.adw_id, _tree_dir(layer))
    return record_


def _tree_dir(layer: Any) -> str:
    """The worktree environment's directory (the first `kind == "worktree"` one), default `trees`."""
    for env in (getattr(layer, "ir", None) or {}).get("environments") or []:
        if isinstance(env, dict) and env.get("kind") == "worktree" and env.get("dir"):
            return str(env["dir"])
    return "trees"


def _perform(layer: Any, state: Any, decision: dict[str, Any], who: str, via: str, extra: str,
             item: dict[str, Any] | None) -> tuple[str, Any, Any]:
    """Change what the action changes → (outcome, what to start once everything is saved, perform)."""
    from . import promotion, runner

    action = str(decision.get("action"))
    resume = lambda: decide._start(layer, state, say=False)  # noqa: E731
    count = len([e for e in entries(layer, run=state.adw_id) if e.get("status") == "acted"]) + 1
    if action in ("resume", "resume-with"):
        if who == "stand-in":
            decision["rounds"] = int(decision.get("rounds", 0) or 0) + 1
        decide.prepare_resume(state, _adw_of(layer, state), decision, extra=extra, layer_dir=layer.layer_dir)
        return f"resuming from {decision.get('from_step') or decision.get('stopped_at') or '?'}", resume, None
    if action == "restart":
        run = _launch(layer, state, _with_decision(_request_text(layer, state), decision, extra, who),
                      f"standin-{state.adw_id}-{count}", state.adw_id)
        decision["status"] = "applied"
        return f"started {run}", None, None
    if action == "answer":
        text = _with_decision("", decision, extra, who).strip()
        if text:
            state.prompt = f"{state.prompt.rstrip()}\n\n{text}"
        waiting = [r for r in state.steps if r.get("status") == "waiting"]
        for step in waiting:
            step["status"] = "pending"
        step_name = str(waiting[0].get("name")) if waiting else "?"
        decision.update(status="resuming", from_step=step_name)
        return f"resumed at {step_name}", resume, None
    if action == "integrate":
        target = str((state.delivery or {}).get("branch") or layer.default_branch)
        prompt = (f"Integrate the work of run {state.adw_id} (branch {state.branch or '?'}) onto the current {target} "
                  f"and make every check pass.\n\nThe original request:\n\n{_request_text(layer, state)}")
        run = _launch(layer, state, _with_decision(prompt, decision, extra, who), f"standin-int-{state.adw_id}-{count}", "")
        decision["status"] = "applied"
        return f"integration run {run} started", None, None
    if action == "land":
        state.ship = {"previous_handoff": dict(state.ship or {})}
        for step in state.steps:
            if step.get("kind") in ("ship", "deliver") or step.get("name") == "deliver":
                step["status"] = "pending"
        decision["status"] = "resuming"
        return "asked the gate again", resume, None
    if action == "settle":
        note = extra.strip() or str(decision.get("summary") or "")
        person = who == "person"
        settle(layer, state, str(decision.get("settle_as")), str(decision.get("settle_by") or ""), note,
               who="person" if person else "stand-in", via=via if person else "stand-in")
        decision["status"] = "applied"
        return f"settled as {decision.get('settle_as')}", None, None
    if action == "promote":
        p = state.promotion or {}
        if not p.get("from") or not p.get("to"):
            raise ApplyError("the run is not a promotion")
        by = "stand-in" if who == "stand-in" else f"person ({via})"
        with contextlib.redirect_stdout(io.StringIO()):
            promotion.decide(Path(layer.layer_dir), str(p["from"]), str(p["to"]), str(decision.get("promote")), [], by=by,
                             comment=str(decision.get("summary") or "")[:300], as_json=True)
        argv = shlex.split(runner.resume_command(state))
        if argv[:2] == ["uv", "run"]:
            argv = [sys.executable, *argv[2:]]
        log = state_module.run_dir(layer.layer_dir, state.adw_id) / "resume.log"
        decision["status"] = "applied"
        return (f"recorded {decision.get('promote')} as {by}; the promotion continues",
                lambda: _spawn_detached(argv, Path(layer.layer_dir), log), None)
    if action == "retry-job":
        job = (item or {}).get("job") or {}
        return "retry requested", None, {"kind": "retry-job", "job": str(job.get("id") or ""),
                                         "from": str(job.get("failed_step") or decision.get("from_step") or "")}
    raise ApplyError(f"{action} is not an action the stand-in performs")


def apply(layer: Any, state: Any, decision: dict[str, Any], who: str, via: str, extra: str = "",
          item: dict[str, Any] | None = None, shown: dict[str, Any] | None = None) -> dict[str, Any]:
    """Perform one action and record it (`acted`, with what happened next). For the stand-in the
    hard limits are checked again; a person's go-ahead is the person's decision. The state is saved
    and the entry written before anything is started, so a resumed run owns its state.json."""
    from . import promotion

    job = (item or {}).get("job") if isinstance((item or {}).get("job"), dict) else None
    action = str(decision.get("action"))
    kind = KIND_OF.get(action, "")
    base = {"type": "decision", "who": who, "via": via, "mode": decision.get("mode"), "kind": kind, "action": action,
            "summary": decision.get("summary"), "situation": decision.get("situation"),
            **({"job": str(job.get("id"))} if job is not None else {"run": state.adw_id, "stop": fingerprint(state)}),
            **(shown or {})}
    base.update(who=who, via=via)
    held = state_module.run_holder(layer.layer_dir, state.adw_id) if job is None else None
    if held:   # the run's process lives: it is left to that process (apply_stored, a go-ahead)
        reason = f"the run's process {held.get('pid')} is alive"
        entry = record(layer, {**base, "status": "refused", "auto": False, "reason": reason}, state)
        return _result("refused", entry, reason=reason)
    if who == "stand-in":
        refused = hard_limit(layer, state, action, decision, item)
        if refused:
            entry = record(layer, {**base, "status": "refused", "auto": False, "reason": refused}, state)
            if job is None:
                decision.update(status="refused", held=refused)
                state.decision = decision
                decide._save(layer, state)
            return _result("refused", entry, reason=refused)
    try:
        text, start, perform = _perform(layer, state, decision, who, via, extra, item)
    except (ApplyError, LayerError, StepFailed, OSError, ValueError, KeyError, promotion.PromotionError) as exc:
        reason = (str(exc.reason) if isinstance(exc, StepFailed) else str(getattr(exc, "message", "") or exc))[:500]
        entry = record(layer, {**base, "status": "failed", "auto": False, "reason": reason}, state)
        if job is None:
            decision.update(status="failed", reason=reason)
            state.decision = decision
            decide._save(layer, state)
        return _result("failed", entry, reason=reason)
    entry = record(layer, {**base, "status": "acted", "auto": who == "stand-in", "outcome": text}, state)
    if job is None:
        decision["outcome"] = text
        state.decision = decision
        state.save(layer.layer_dir)
        decide.write_record(layer, state)
    if start is not None:
        try:
            start()                                               # last: the started process owns the run from here
        except Exception as exc:  # noqa: BLE001 — say why nothing started; the run stays with a person
            outcome(layer, str(entry["id"]), f"did not start: {type(exc).__name__}: {exc}"[:300])
    return _result("acted", entry, outcome=text, perform=perform)


def apply_stored(layer: Any, run: str, via: str = "loop") -> dict[str, Any]:
    """Apply a run's stored suggestion as the stand-in ("Do these"): scope, limits and hard limits hold."""
    layer = _load(layer)
    try:
        state = state_module.load(layer.layer_dir, run)
        scope_ = scope(layer, state.decider or None)
    except LayerError as exc:
        return _result("failed", reason=str(exc)[:500])
    decision = dict(state.decision or {})
    if decision.get("status") not in decide.ANSWERABLE or not KIND_OF.get(str(decision.get("action"))):
        return _result("refused", reason="the run has no suggestion waiting")
    kind = KIND_OF[str(decision["action"])]
    base = {"type": "decision", "who": "stand-in", "via": via, "run": run, "kind": kind, "action": decision["action"],
            "summary": decision.get("summary"), "mode": scope_["mode"], "stop": fingerprint(state)}
    blocked = NOT_IN_SCOPE if kind not in scope_["may"] else limit_reached(layer, state, scope_)
    if blocked:
        return _result("refused", record(layer, {**base, "status": "refused", "auto": False, "reason": blocked}, state),
                       reason=blocked)
    lock = Path(layer.layer_dir) / LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            return apply(layer, state, decision, who="stand-in", via=via,
                         shown={"why": decision.get("explanation"), "confidence": decision.get("confidence"),
                                "read": decision.get("read")})
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------- status and the command line

def status(layer: Any) -> dict[str, Any]:
    """The panel's view: the scope, one line per kind, the counts and the end of the log."""
    layer = _load(layer)
    try:
        view: dict[str, Any] = dict(scope(layer))
    except LayerError as exc:
        return {"mode": "off", "source": "error", "error": str(exc), "kinds": dict(KIND_LINES), "log": [],
                "counts": {"today": 0, "hour": 0, "waiting": 0}, "settings": str(decide.SETTINGS_FILE),
                "offered": [], "may": [], "never": [], "suggests": [], "paused": False, "since": "",
                "per_run": decide.per_run(layer), "per_hour": PER_HOUR, "escalate": ["dashboard"], "declared": None}
    found = entries(layer)
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    hour = datetime.now(timezone.utc) - timedelta(seconds=3600)
    acted = _acted(found)
    last: dict[str, dict[str, Any]] = {}
    for entry in found:
        key = str(entry.get("run") or entry.get("job") or "")
        if key and entry.get("type") in ("decision", "answer"):
            last[key] = entry
    waiting = sum(1 for entry in last.values()
                  if (entry.get("type") == "decision" and entry.get("status") == "suggested")
                  or (entry.get("type") == "answer" and entry.get("answer") == "explain"))
    view.update(kinds=dict(KIND_LINES), settings=str(decide.SETTINGS_FILE), log=found[-LOG_SHOWN:], counts={
        "today": sum(1 for e in acted if (_parse(e.get("at")) or midnight) >= midnight),
        "hour": sum(1 for e in acted if (_parse(e.get("at")) or hour) >= hour),
        "waiting": waiting})
    return view


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="standin.py", description="The stand-in of this layer: its scope, its "
                                     "decisions and a person's answers. One JSON line with --json.")
    sub = parser.add_subparsers(dest="verb", required=True)

    def verb(name: str, text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=text)
        p.add_argument("--json", action="store_true", help="print one JSON line")
        return p
    verb("status", "the mode, the scope, the limits, the counts and the end of the log")
    p = verb("set", "change the local setting (agents/settings/decider.json)")
    p.add_argument("--mode", choices=decide.MODES)
    p.add_argument("--reset", action="store_true", help="remove the local mode: the layer's declaration applies")
    p.add_argument("--may", metavar="K,K", help="the kinds it may take alone on this machine (of those the layer offers)")
    p.add_argument("--may-reset", action="store_true", help="every kind the layer offers")
    p.add_argument("--pause", action="store_true")
    p.add_argument("--unpause", action="store_true")
    p.add_argument("--via", choices=("cli", "dashboard"), default="cli")
    p = verb("consider", "look at one item (JSON on stdin)")
    p.add_argument("--suggest-only", action="store_true", help="suggest, whatever the layer's mode")
    p.add_argument("--via", choices=("loop", "asked"), default="loop")
    p = verb("apply", "apply a run's stored suggestion as the stand-in")
    p.add_argument("run")
    p.add_argument("--via", choices=("loop", "asked", "dashboard", "cli"), default="loop")
    p = verb("answer", "a person's answer to a run's suggestion (the text on stdin)")
    p.add_argument("run")
    p.add_argument("answer", choices=decide.ANSWERS)
    p.add_argument("--via", choices=("cli", "dashboard"), default="cli")
    verb("note", "record an entry (JSON on stdin): a person's action, or an outcome")
    return parser


def set_local(layer: Any, via: str = "cli", drop: bool = False, **changes: Any) -> dict[str, Any]:
    """Change the local setting (`mode`, `may`, `paused`; `drop` removes the whole file), note it in the
    log and return the new status. ValueError for a kind that is unknown or that the layer does not offer."""
    if "may" in changes and changes["may"] is not None:
        offered = scope(layer)["offered"]
        for kind in changes["may"]:
            if kind not in KINDS:
                raise ValueError(f"unknown decision kind {kind!r}; use any of {', '.join(KINDS)}")
            if kind not in offered:
                raise ValueError(f"{kind} is not in the layer's (stand-in … #:may): it offers {', '.join(offered) or 'nothing'}")
    if drop:
        decide.write_mode(Path(layer.layer_dir), "")
    elif not changes:
        raise ValueError("nothing to set: give --mode, --reset, --may, --may-reset, --pause or --unpause")
    else:
        decide.write_setting(Path(layer.layer_dir), **changes)
    now = scope(layer)
    quiet = not drop and (set(changes) == {"may"} or bool(changes.get("paused")))   # not a switch-on: `since` stays
    record(layer, {"type": "mode", "who": "person", "via": via, "mode": now["mode"], "may": now["may"],
                   "paused": now["paused"], "set": ["reset"] if drop else sorted(changes),
                   **({"scope_only": True} if quiet else {})})
    return status(layer)


def _set(layer: Any, a: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if a.reset and a.mode:
        raise ValueError("give --mode or --reset, not both")
    if a.pause and a.unpause:
        raise ValueError("give --pause or --unpause, not both")
    if a.may is not None and a.may_reset:
        raise ValueError("give --may or --may-reset, not both")
    if a.mode:
        changes["mode"] = a.mode
    if a.reset:
        changes["mode"] = ""
    if a.may is not None:
        changes["may"] = [k.strip() for k in a.may.split(",") if k.strip()]
    if a.may_reset:
        changes["may"] = None
    if a.pause or a.unpause:
        changes["paused"] = bool(a.pause)
    return set_local(layer, via=a.via, **changes)


def main(layer_dir: Path, argv: list[str] | None = None) -> int:
    """`adws/standin.py VERB …`: exit 0 done, 1 refused or failed, 2 bad use."""
    parser = _parser()
    try:
        a = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    def out(result: dict[str, Any], code: int) -> int:
        if a.json:
            print(json.dumps(result, default=str), flush=True)
        else:
            text = result.get("message") or result.get("reason") or result.get("outcome") or result.get("error") or ""
            print(f"stand-in: {result.get('status') or result.get('mode')}" + (f": {text}" if text else ""), flush=True)
        return code

    def quiet(work: Any) -> Any:
        """Progress lines of the runtime go to stderr: stdout holds the one JSON line."""
        with contextlib.redirect_stdout(sys.stderr):
            return work()

    try:
        layer = _load(Path(layer_dir))
        if a.verb == "status":
            return out(status(layer), 0)
        if a.verb == "set":
            return out(_set(layer, a), 0)
        if a.verb in ("consider", "note"):
            try:
                data = json.loads(sys.stdin.read() or "null")
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return out({"status": "invalid", "error": "stdin: one JSON object"}, 2)
            if a.verb == "note":
                data.pop("id", None)
                if data.get("ref"):
                    amend(layer, str(data.pop("ref")), **{k: v for k, v in data.items() if k in ("outcome", "stop", "reason")})
                    return out({"status": "noted"}, 0)
                data.setdefault("who", "person")
                return out({"status": "noted", "entry": record(layer, data)}, 0)
            result = quiet(lambda: consider(layer, data, force_suggest=a.suggest_only, via=a.via))
            return out(result, 1 if result["status"] in ("failed", "refused") else 0)
        if a.verb == "apply":
            if not RUN_ID.fullmatch(a.run):
                return out({"status": "invalid", "error": "a run id is 8 hexadecimal characters"}, 2)
            result = quiet(lambda: apply_stored(layer, a.run, via=a.via))
            return out(result, 0 if result["status"] == "acted" else 1)
        if a.verb == "answer":
            text = "" if sys.stdin.isatty() else sys.stdin.read()
            result = quiet(lambda: decide.answer(layer, a.run, a.answer, text, via=a.via))
            return out(result, 1 if result["status"] == "refused" else 0)
    except (LayerError, ValueError) as exc:
        return out({"status": "invalid", "error": str(exc)}, 2)
    return 2
