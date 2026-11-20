"""The stand-in decider: when a run stops and is handed to a person, a strong model stands in for them.

Where. Right after runner._hand_off (a `[needs human]` pull request or PR.md, state.ship["action"]
== "handoff") and before the hand-over notification, `consider` makes one agent call on the heavy
tier of the workflow's agent, recorded as step `decide` (kind `decide`), like `deliver`. The agent
reads why the run stopped and what the agents asked a person to do, and answers with one action:

- `resume`       run again from the step that stopped (or `from_step`), unchanged;
- `resume-with`  run again from `from_step`, with `instructions` added to the run's prompt;
- `abandon`      stop here; the hand-off stays as it is;
- `ask-human`    it cannot decide.

Modes (`mode_for`; first match wins: `--decider MODE`, AL_DECIDER, the layer's local setting
agents/settings/decider.json written by `al decider LAYER --set MODE` or the dashboard, the kind of
the layer's `(stand-in KIND …)` declaration, else `off`):

- `off`      nothing happens; state.json has no `decision` key (the runs of before);
- `auto`     a resume starts at once (`after_run` spawns `--resume` once the run has written its
             summary), at most MAX_AUTO_ROUNDS per run; abandon and ask-human, and every stop after
             the last round, go to a person as before. A round that resumes sends no needs-human
             message (notify.event_of);
- `suggest`  nothing is applied: the decision goes into agents/<id>/decision.md and the run's
             needs-human message, whose subject carries `[decide RUN-CODE]`. A person answers
             `go`, `alter TEXT`, `explain` or `reject` (`answer`) with `al answer`, by replying to
             the message (`poll_mail`, AL_DECIDE_IMAP_URL) or with a webhook payload
             (trigger_webhook). An answer by e-mail or webhook must repeat CODE.

Never: a merge, a deploy, an approval or a way around the gate. Every action ends either in a
resume, which goes through every check, the review and ship.py again, or with a person. Not for
workflows with `#:changes layer`, runs a spending or usage limit stopped, waiting runs or fan-out
candidates (`eligible`). A decider call that fails leaves the run and its hand-off as they were.

The scope (standin.py). A layer may declare which decisions the stand-in takes alone
(`(stand-in auto #:may (…))`); without a declaration that is `resume` only, the behaviour of before.
In `auto` mode a resume starts only when `resume` is in that scope, the stand-in is not paused and
no limit is reached; otherwise the decision is a suggestion. The decisions beyond a resume (restart,
answer, integrate, land, settle, promote, retry-job: ALL_ACTIONS) are made outside the run by
standin.consider and stored here in the same `state.decision`; `answer` is the one entry for a
person's answer from every channel (cli, dashboard, mail, webhook) and hands a suggestion that is
not a resume to standin.apply. Every decision and answer is one line in agents/<id>/decisions.jsonl
and agents/stand-in/log.jsonl (standin.record).

E-mail reasons are a fixed vocabulary that never holds a value (an address, a host, a password).
"""
from __future__ import annotations

import contextlib
import email
import email.policy
import email.utils
import fcntl
import hmac
import imaplib
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import events, localenv, notify, state as state_module
from .errors import LayerError, StepFailed

MODES = ("off", "suggest", "auto")
ACTIONS = ("resume", "resume-with", "abandon", "ask-human")   # what the in-run prompt offers
STOP_ACTIONS = ACTIONS
# every action of the contract; the ones beyond STOP_ACTIONS are offered by standin.consider only
ALL_ACTIONS = ("resume", "resume-with", "restart", "answer", "integrate", "land", "settle", "promote", "retry-job",
               "abandon", "ask-human")
SETTLE_AS = ("landed", "superseded", "abandoned")
PROMOTE = ("approve", "refuse")
KEEP: Any = object()   # write_setting: leave this key as it is
ANSWERS = ("go", "alter", "explain", "reject")
SETTINGS_FILE = Path("agents") / "settings" / "decider.json"   # the layer's local mode, never committed
ENV_MODE = "AL_DECIDER"
ENV_IMAP = "AL_DECIDE_IMAP_URL"
CLAIM_FILE = "decide.claim"   # agents/<id>/decide.claim: who decides the run's current stop (claim_stop)
MAX_AUTO_ROUNDS = 2
STEP = "decide"
SUMMARY_MAX = 300
EXPLAIN_MAX = 4000
ANSWER_MAX = 4000
MAIL_TIMEOUT = 30
RECORD_FILE = "decision.md"
COMMAND = {"name": STEP, "template": STEP, "output": "json", "modelSet": "heavy", "isHop": False}
TOKEN = re.compile(r"\[decide ([0-9a-fA-F]{8})-([A-Za-z0-9_-]{8,64})\]")
ANSWERABLE = ("suggested", "explained")
_IMAP_PORTS = {"imaps": 993, "imap": 143}

PROMPT = """\
# Stand in for a person: decide what happens to a stopped run

You stand in for the person who would read this hand-off. A run of the workflow `{adw}` (run
`{adw_id}`) stopped at step `{stopped_at}` and was handed to a person. Decide what happens next.

Do not change any file: read only. Choose exactly one action:

- `resume`: run again from the step that stopped (or from `from_step`), unchanged; for a stop that
  looks accidental, such as a flaky check or a timeout;
- `resume-with`: run again from `from_step`, with `instructions` added to the run's request; for a
  stop that one clear extra instruction fixes;
- `abandon`: stop here; the hand-off stays as it is; for a request that cannot or should not be done;
- `ask-human`: you cannot decide.

Prefer `ask-human` when you are unsure. Always choose `ask-human` for anything that touches a
protected path, needs a credential, a secret, money or access you do not have, or that the agents
marked `REQUIRES HUMAN` as work only a person may do. A resume goes through every check, the review
and the shipping gate again: you can never merge or deploy, only send the work round again.

## The request

{request}

## Why the run stopped

{reason}

## What the agents asked a person to do

{asks}

## Where to read more

- plan: {plan}
- hand-off: {handoff}

## The workflow's steps, in order

{steps}

`from_step` is one of these names, or "" for the step that stopped (`{stopped_at}`).

## Earlier decisions and answers for this run

{earlier}

## Your answer

End with exactly one fenced json block:

```json
{{"action": "resume | resume-with | abandon | ask-human",
 "summary": "one line, at most 300 characters, for the person",
 "explanation": "why, in a few sentences",
 "instructions": "what the next round must do differently (required for resume-with, else empty)",
 "from_step": "a step name from the list above, or empty"}}
```
"""


class MailError(Exception):
    """A reason from the fixed vocabulary of the module docstring; never holds a value."""


# ---------------------------------------------------------------- settings and mode

def _checked_may(may: Any) -> list[str]:
    from . import standin

    kinds = [str(kind) for kind in may]
    unknown = [kind for kind in kinds if kind not in standin.KINDS]
    if unknown:
        raise ValueError(f"unknown decision kind {unknown[0]!r}; use any of {', '.join(standin.KINDS)}")
    return sorted(set(kinds))


def render_setting(mode: str, may: Any = None, paused: bool = False) -> str:
    """The one format of agents/settings/decider.json. `may` (the kinds the stand-in may take alone
    on this machine) and `paused` are written only when set, so a plain mode is the file of before."""
    data: dict[str, Any] = {"version": 1}
    if mode:
        data["mode"] = mode
    if may is not None:
        data["may"] = _checked_may(may)
    if paused:
        data["paused"] = True
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def read_setting(layer_dir: Path) -> dict[str, Any]:
    """The layer's local setting: {"mode": "" | off | suggest | auto, "may": [kind…] | None, "paused": bool};
    mode "" and no keys when there is no file. A broken file raises LayerError with its path."""
    path = Path(layer_dir) / SETTINGS_FILE
    fix = "fix or remove it (al decider LAYER --reset)"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"mode": "", "may": None, "paused": False}
    except OSError as exc:
        raise LayerError(f"{path}: cannot read it ({exc.strerror}); {fix}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LayerError(f"{path}: not JSON ({exc.msg}); {fix}") from exc
    mode = data.get("mode") if isinstance(data, dict) else None
    more = isinstance(data, dict) and ("may" in data or "paused" in data)
    if mode not in MODES and not (more and mode in (None, "")):
        raise LayerError(f'{path}: expected {{"version": 1, "mode": "{"|".join(MODES)}"}}, got mode {mode!r}; {fix}')
    may = data.get("may")
    if may is not None:
        try:
            if not isinstance(may, list) or not all(isinstance(kind, str) for kind in may):
                raise ValueError("not a list of names")
            may = _checked_may(may)
        except ValueError as exc:
            raise LayerError(f'{path}: "may" must be a list of decision kinds ({exc}); {fix}') from exc
    if not isinstance(data.get("paused", False), bool):
        raise LayerError(f'{path}: "paused" must be true or false; {fix}')
    return {"mode": str(mode or ""), "may": may, "paused": bool(data.get("paused", False))}


def write_setting(layer_dir: Path, *, mode: Any = KEEP, may: Any = KEEP, paused: Any = KEEP) -> Path:
    """Change the given keys of the local setting and write it atomically. An empty mode removes the
    mode, `may=None` the local scope; a setting with no mode, no scope and not paused removes the file."""
    path = Path(layer_dir) / SETTINGS_FILE
    try:
        current = read_setting(layer_dir)
    except LayerError:
        current = {"mode": "", "may": None, "paused": False}   # a broken file is replaced
    if mode is not KEEP:
        if mode and mode not in MODES:
            raise ValueError(f"unknown decider mode {mode!r}; use one of {', '.join(MODES)}")
        current["mode"] = str(mode or "")
    if may is not KEEP:
        current["may"] = None if may is None else _checked_may(may)
    if paused is not KEEP:
        current["paused"] = bool(paused)
    if not current["mode"] and current["may"] is None and not current["paused"]:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix=".decider.", suffix=".tmp")
    try:
        with handle:
            handle.write(render_setting(current["mode"], current["may"], current["paused"]))
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def read_mode(layer_dir: Path) -> str:
    """The layer's local mode; "" when there is none. A broken file raises LayerError with its path."""
    return read_setting(layer_dir)["mode"]


def write_mode(layer_dir: Path, mode: str | None) -> Path:
    """Write the local mode atomically; an empty mode removes the file (the reset of `al decider`)."""
    if not mode:
        path = Path(layer_dir) / SETTINGS_FILE
        path.unlink(missing_ok=True)
        return path
    return write_setting(layer_dir, mode=mode)


def mode_for(layer: Any, flag: str | None) -> tuple[str, str]:
    """(mode, where it comes from): `--decider` > AL_DECIDER > local setting > the layer's
    (stand-in KIND …) declaration > `off` (default)."""
    if flag:
        if flag not in MODES:
            raise LayerError(f"--decider: unknown mode {flag!r}; use one of {', '.join(MODES)}")
        return flag, "--decider"
    env = os.environ.get(ENV_MODE, "").strip()
    if env:
        if env not in MODES:
            raise LayerError(f"{ENV_MODE}: unknown mode {env!r}; use one of {', '.join(MODES)}")
        return env, ENV_MODE
    local = read_mode(Path(layer.layer_dir))
    if local:
        return local, "local setting"
    kind = ((getattr(layer, "ir", None) or {}).get("standIn") or {}).get("kind")
    if kind in ("suggest", "auto"):
        return str(kind), "layer"
    return "off", "default"


def per_run(layer: Any) -> int:
    """Most actions the stand-in takes on one run: the declaration's #:max-actions-per-run, else MAX_AUTO_ROUNDS."""
    declared = (getattr(layer, "ir", None) or {}).get("standIn") or {}
    try:
        return int(declared.get("maxActionsPerRun") or MAX_AUTO_ROUNDS)
    except (TypeError, ValueError):
        return MAX_AUTO_ROUNDS


# ---------------------------------------------------------------- eligibility, prompt, contract

def eligible(layer: Any, state: Any, adw: dict[str, Any], mode: str | None = None) -> str:
    """"" when the decider may decide this stopped run, else why not."""
    if mode is None:
        mode = mode_for(layer, None)[0]
    ship = state.ship or {}
    if mode == "off":
        return "the decider is off"
    stopped = (getattr(state, "proof_plan", None) or {}).get("stopped") or {}
    if stopped.get("party") in ("layer author", "operator"):
        first = (str(stopped.get("reason") or "").strip().splitlines() or [""])[0]
        return f"stopped for the {stopped['party']}: {first}"
    if state.fanout_parent:
        return "a fan-out candidate (its parent decides)"
    if str(adw.get("changes") or "codebase") == "layer":
        return "the workflow changes the layer (a person always approves)"
    if (state.budget or {}).get("stopped"):
        return "a spending limit stopped the run (money is a person's decision)"
    if (state.usage or {}).get("stopped"):
        return "a subscription usage limit stopped the run (a person's decision)"
    if state.status == "waiting":
        return "the run is waiting, not stopped"
    if ship.get("handoff_error"):
        return "the hand-off failed"
    if ship.get("action") != "handoff":
        return "the run was not handed to a person"
    if mode == "auto" and int((state.decision or {}).get("rounds", 0) or 0) >= per_run(layer):
        return "auto rounds used up"
    return ""


def step_names(adw: dict[str, Any]) -> list[str]:
    return [str(step.get("name")) for step in adw.get("steps", []) if step.get("name")]


def _stop_reason(state: Any) -> str:
    reasons = (state.ship or {}).get("reasons") or []
    if reasons:
        return re.sub(r"^run-stopped:[^:]*: ", "", str(reasons[0]))
    for record in state.steps or []:
        if record.get("status") == "failed":
            return str(record.get("detail") or "")
    return str(getattr(state, "error", "") or "no step recorded a reason")


def _asks(layer: Any, state: Any) -> str:
    """The REQUIRES HUMAN paragraphs of the plan and the agents' outputs (ship.human_requests)."""
    from . import ship

    texts: list[tuple[str, str]] = []
    if state.plan:
        plan = Path(state.plan)
        plan = plan if plan.is_absolute() else Path(layer.layer_dir) / plan
        if plan.is_file():
            texts.append(("the plan", plan.read_text(encoding="utf-8", errors="replace")))
    for name, output in (state.outputs or {}).items():
        if isinstance(output, str) and output.strip() != str(state.plan).strip():
            texts.append((f"step `{name}`", output))
    found, seen = [], set()
    for label, text in texts:
        for item in ship.human_requests(text):
            key = " ".join(item.split())
            if key not in seen:
                seen.add(key)
                found.append(f"From {label}:\n\n{item}")
    return "\n\n".join(found[:30]) or f"No `{ship.MARKER}` line in the plan or in the agents' reports."


def _earlier(state: Any) -> str:
    decision = state.decision or {}
    lines = []
    if decision.get("action"):
        lines.append(f"- round {decision.get('rounds', 0)}: {decision.get('mode')} {decision.get('action')} "
                     f"from {decision.get('from_step') or '?'}: {decision.get('summary', '')}")
    for item in decision.get("answers") or []:
        lines.append(f"- a person answered `{item.get('kind')}`" + (f": {item.get('text')}" if item.get("text") else ""))
    return "\n".join(lines) or "None."


def render_prompt(layer: Any, state: Any, adw: dict[str, Any]) -> str:
    ship_record = state.ship or {}
    target = str(ship_record.get("target") or "")
    handoff = target
    if target and not re.match(r"https?://", target):
        handoff = str(Path(layer.layer_dir) / target)
    return PROMPT.format(
        adw=state.adw, adw_id=state.adw_id, stopped_at=ship_record.get("stopped_at") or "?",
        request=state.prompt.strip() or "(empty)", reason=_stop_reason(state).strip() or "(none recorded)",
        asks=_asks(layer, state), plan=state.plan or "none", handoff=handoff or "none",
        steps="\n".join(f"{index}. `{name}`" for index, name in enumerate(step_names(adw), start=1)),
        earlier=_earlier(state))


def validate(obj: Any, names: list[str], allowed: Any = STOP_ACTIONS) -> dict[str, Any]:
    """The decision contract; anything else raises StepFailed("decide", …). `allowed` is what the
    prompt offered: the in-run decider's four actions, or the actions of a situation (standin.py)."""
    def bad(why: str) -> StepFailed:
        return StepFailed(STEP, f"invalid decision: {why}")

    if not isinstance(obj, dict):
        raise bad("not a JSON object")
    action = obj.get("action")
    if action not in tuple(allowed):
        raise bad(f"action must be one of {', '.join(allowed)}, got {action!r}")
    fields: dict[str, Any] = {"action": str(action)}
    for key in ("summary", "explanation", "instructions", "from_step"):
        value = obj.get(key, "")
        if not isinstance(value, str):
            raise bad(f"{key} must be a string")
        fields[key] = value.strip()
    if not fields["summary"]:
        raise bad("summary must not be empty")
    if len(fields["summary"]) > SUMMARY_MAX:
        raise bad(f"summary has {len(fields['summary'])} characters, at most {SUMMARY_MAX}")
    if action == "resume-with" and not fields["instructions"]:
        raise bad("resume-with needs instructions")
    if fields["from_step"] and fields["from_step"] not in names:
        raise bad(f"from_step {fields['from_step']!r} is not a step of the workflow ({', '.join(names)})")
    confidence = obj.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise bad("confidence must be a number from 0 to 1")
        confidence = float(confidence)
    fields["confidence"] = confidence
    if action == "settle":
        if obj.get("settle_as") not in SETTLE_AS:
            raise bad(f"settle needs settle_as, one of {', '.join(SETTLE_AS)}")
        by = obj.get("settle_by") or ""
        if not isinstance(by, str) or not re.fullmatch(r"(?:[0-9a-f]{8}|[0-9a-f]{7,40})?", by.strip()):
            raise bad("settle_by is a run id (8 hexadecimal characters), a commit (7 to 40) or empty")
        fields["settle_as"], fields["settle_by"] = str(obj["settle_as"]), by.strip()
    if action == "promote":
        if obj.get("promote") not in PROMOTE:
            raise bad(f"promote needs promote, one of {', '.join(PROMOTE)}")
        fields["promote"] = str(obj["promote"])
    return fields


# ---------------------------------------------------------------- the decision in a run

def consider(layer: Any, state: Any, adw: dict[str, Any], stack: Any, args: Any) -> str:
    """Let the decider look at a stopped run. "resume" when the run is to be resumed at once (the
    caller keeps the worktree; after_run starts the resume), else "". Never raises, and never
    changes the run's result or its hand-off."""
    try:
        mode, source = mode_for(layer, getattr(args, "decider", None) or getattr(state, "decider", "") or None)
    except LayerError as exc:
        print(f"[adw {state.adw_id}] decider: not used: {exc}")
        return ""
    if mode == "off":
        return ""
    try:
        return _consider(layer, state, adw, stack, mode, source)
    except Exception as exc:  # noqa: BLE001 — the decider never fails a run or its hand-off
        reason = f"{type(exc).__name__}: {exc}"[:500]
        with contextlib.suppress(OSError):
            release_stop(layer.layer_dir, state.adw_id, "run")   # no decision: the stop is the loop's to decide
        state.decision = {**(state.decision or {}), "status": "failed", "reason": reason}
        print(f"[adw {state.adw_id}] decider failed: {reason}; the run stays with a person")
        _save(layer, state)
        return ""


# ---------------------------------------------------------------- one claim per stop

def stop_seq(layer_dir: Path, run_id: str) -> int:
    """The seq of the run's current stop: its last `handoff` event after the last `run.start`, 0 when none."""
    path = events.path_for(layer_dir, run_id)
    found = 0
    try:
        with path.open("rb") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("seq"), int):
                    continue
                if event.get("type") == "run.start":
                    found = 0
                elif event.get("type") == "handoff":
                    found = event["seq"]
    except OSError:
        return 0
    return found


def _claimed(layer_dir: Path, run_id: str, change: Callable[[dict[str, Any], int], dict[str, Any] | None]) -> Any:
    """Read agents/<id>/decide.claim under its flock, let `change(claim, stop)` return what to write
    (None writes nothing) and return `change`'s claim afterwards."""
    path = state_module.run_dir(layer_dir, run_id) / CLAIM_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        try:
            claim = json.loads(b"".join(chunks) or b"{}")
        except ValueError:
            claim = {}
        if not isinstance(claim, dict):
            claim = {}
        written = change(claim, stop_seq(layer_dir, run_id))
        if written is not None:
            data = (json.dumps(written, sort_keys=True) + "\n").encode("utf-8")
            os.ftruncate(fd, 0)
            os.pwrite(fd, data, 0)
        return written if written is not None else claim
    finally:
        os.close(fd)   # releases the flock


def claim_stop(layer_dir: Path, run_id: str, who: str) -> str:
    """Claim the run's current stop for `who` (`run` or `stand-in`): "" when won, else who holds it.
    The run's own decide step and the stand-in's loop decide each stop once, whoever comes first."""
    lost: list[str] = []

    def change(claim: dict[str, Any], stop: int) -> dict[str, Any] | None:
        if stop <= 0:
            return None
        if claim.get("seq") == stop and claim.get("by"):
            lost.append(str(claim["by"]))
            return None
        skipped = list(claim.get("skipped") or []) if claim.get("seq") == stop else []
        return {"seq": stop, "by": who, "pid": os.getpid(), "at": state_module.now(), "skipped": skipped}

    _claimed(Path(layer_dir), run_id, change)
    return lost[0] if lost else ""


def release_stop(layer_dir: Path, run_id: str, who: str) -> None:
    """Give back `who`'s claim on the run's current stop when it took no decision (its decide step
    failed): the stop is open again, so the stand-in's loop decides it."""

    def change(claim: dict[str, Any], stop: int) -> dict[str, Any] | None:
        if stop <= 0 or claim.get("seq") != stop or claim.get("by") != who:
            return None
        return {**claim, "by": ""}

    _claimed(Path(layer_dir), run_id, change)


def note_skipped(layer_dir: Path, run_id: str, who: str, reason: str) -> None:
    """Record that `who` left the run's current stop alone: once per stop and party a `decision.skipped` event."""
    first: list[int] = []

    def change(claim: dict[str, Any], stop: int) -> dict[str, Any] | None:
        if claim.get("seq") != stop:
            claim = {"seq": stop, "by": ""}
        skipped = list(claim.get("skipped") or [])
        if who in skipped:
            return None
        first.append(stop)
        return {**claim, "skipped": [*skipped, who]}

    _claimed(Path(layer_dir), run_id, change)
    if not first:
        return
    fields = {"who": who, "reason": reason, "stop_seq": first[0]}
    current = events._current
    if current is not None and Path(current.path) == events.path_for(layer_dir, run_id):   # inside the run
        events.emit("decision.skipped", **fields)
        return
    with events.recording(Path(layer_dir), run_id):
        events.emit("decision.skipped", **fields)


def _save(layer: Any, state: Any) -> None:
    try:
        state.save(layer.layer_dir)
    except OSError as exc:
        print(f"[adw {state.adw_id}] warning: cannot save the decision: {exc}")


def _consider(layer: Any, state: Any, adw: dict[str, Any], stack: Any, mode: str, source: str) -> str:
    from . import agents, standin, steps

    previous = dict(state.decision or {})
    previous.pop("skipped", None)
    stopped = (getattr(state, "proof_plan", None) or {}).get("stopped") or {}
    if stopped.get("party") == "approver":
        return _ask_approver(layer, state, mode, source, previous, stopped)
    scope = standin.scope(layer, mode)
    why = eligible(layer, state, adw, mode) or ("the stand-in is paused" if scope["paused"] else "")
    if why:
        state.decision = {**previous, "skipped": why}
        events.say(f"[adw {state.adw_id}] decider: not used: {why}", "decision", mode=mode, status="skipped", reason=why)
        _save(layer, state)
        return ""
    lost = claim_stop(layer.layer_dir, state.adw_id, "run")
    if lost:   # the stand-in's loop took this stop first: it is decided once
        note_skipped(layer.layer_dir, state.adw_id, "run", "the stand-in's loop decided this stop")
        print(f"[adw {state.adw_id}] decider: not used: the stand-in's loop decided this stop", flush=True)
        return ""
    names = step_names(adw)
    prompt = render_prompt(layer, state, adw)
    found: dict[str, Any] = {}

    def work() -> None:
        output = agents.run_prompt(layer, state, STEP, COMMAND, prompt, scope["model_set"], stack)
        found.update(validate(json.loads(output), names))

    try:
        steps._run_recorded(layer, state, STEP, STEP, work)
    except StepFailed as exc:
        release_stop(layer.layer_dir, state.adw_id, "run")   # no decision: the stop is the loop's to decide
        state.decision = {**previous, "mode": mode, "status": "failed", "reason": str(exc.reason)[:500]}
        state.decision.pop("action", None)
        print(f"[adw {state.adw_id}] decider: no decision ({exc.reason[:300]}); the run stays with a person")
        _save(layer, state)
        return ""
    stopped = str((state.ship or {}).get("stopped_at") or "")
    rounds = int(previous.get("rounds", 0) or 0) + (1 if mode == "auto" else 0)
    # what the prompt gave it to read, as the dashboard's suggestion card names it
    target = str((state.ship or {}).get("target") or "")
    read = ["why it stopped", *(["PR.md"] if target and not re.match(r"https?://", target) else []),
            *(["the hand-off pull request"] if re.match(r"https?://", target) else []), *(["the plan"] if state.plan else [])]
    decision = {"mode": mode, "mode_from": source, **found, "from_step": found["from_step"] or stopped,
                "kind": standin.KIND_OF.get(found["action"], ""), "situation": "stopped", "read": read, "by": "stand-in",
                "stopped_at": stopped, "code": secrets.token_urlsafe(9), "rounds": rounds,
                "answers": list(previous.get("answers") or []), "at": state_module.now(), "status": "suggested"}
    state.decision = decision
    held = ""
    if mode == "auto" and decision["action"] in ("resume", "resume-with"):
        # alone only inside its scope, under its limits, and never past a hard limit: else a suggestion
        held = ("resume is not in the stand-in's scope" if "resume" not in scope["may"] else
                standin.hard_limit(layer, state, decision["action"], decision) or standin.limit_reached(layer, state, scope))
        if held:
            decision["held"] = held
        else:
            prepare_resume(state, adw, decision, layer_dir=layer.layer_dir)
    elif mode == "auto":
        decision["status"] = "decided"
    write_record(layer, state)
    status = {"resuming": "acted", "decided": "escalated"}.get(decision["status"], "suggested")
    entry = standin.record(layer, {
        "type": "decision", "who": "stand-in", "via": "run", "run": state.adw_id, "mode": mode, "situation": "stopped",
        "kind": standin.KIND_OF.get(decision["action"], ""), "action": decision["action"], "summary": decision["summary"],
        "why": decision["explanation"], "confidence": decision.get("confidence"), "read": read, "status": status,
        "auto": status == "acted", "stop": standin.fingerprint(state), "stopped_for": re.sub(r"\d+", "#", _stop_reason(state))[:300],
        "from_step": decision["from_step"],
        **({"instructions": decision["instructions"]} if decision.get("instructions") else {}),
        **({"outcome": f"resuming from {decision['from_step'] or '?'}"} if status == "acted" else {}),
        **({"reason": held} if held else {})}, state)
    decision["entry_pending"] = entry["id"]   # its stop is known once the run has ended (after_run)
    events.say(f"[adw {state.adw_id}] decider ({mode}): {decision['action']} from {decision['from_step'] or '?'} "
               f"({decision['status']}): {decision['summary']}", "decision", mode=mode,
               action=decision["action"], from_step=decision["from_step"], status=decision["status"])
    _save(layer, state)
    return "resume" if decision["status"] == "resuming" else ""


def _ask_approver(layer: Any, state: Any, mode: str, source: str, previous: dict[str, Any],
                  stopped: dict[str, Any]) -> str:
    """A run stopped for a decision only a person may take (proofplan.py, evidence.py): the
    suggestion is written without a model call, always as `suggest`, whatever the mode: a stand-in
    never approves for a person. `go` grants the listed approvals and resumes (see `answer`)."""
    wanted = [str(item) for item in stopped.get("approvals") or []]
    question = str(stopped.get("question") or "").strip() or (
        f"may this run {', '.join(wanted) or 'go on'}? answer: al approve LAYER {state.adw_id} {' '.join(wanted)}")
    at = str(stopped.get("at") or "")
    decision = {"mode": "suggest", "mode_from": source, "action": "resume", "from_step": at, "stopped_at": at,
                "summary": question[:SUMMARY_MAX], "explanation": str(stopped.get("reason") or "")[:EXPLAIN_MAX],
                "instructions": "", "approvals": wanted, "code": secrets.token_urlsafe(9),
                "rounds": int(previous.get("rounds", 0) or 0), "answers": list(previous.get("answers") or []),
                "at": state_module.now(), "status": "suggested"}
    state.decision = decision
    write_record(layer, state)
    events.say(f"[adw {state.adw_id}] decider ({mode}): a person decides: {decision['summary']}", "decision",
               mode="suggest", action="resume", from_step=at, status="suggested")
    _save(layer, state)
    return ""


def keeps_worktree(state: Any) -> bool:
    """A run the decider resumes, or whose suggestion a person may still accept, keeps its worktree:
    the resume continues in it."""
    return (state.decision or {}).get("status") in ("resuming", "suggested")


def prepare_resume(state: Any, adw: dict[str, Any], decision: dict[str, Any], extra: str = "",
                   layer_dir: Path | None = None) -> None:
    """Make the run ready for `--resume`: the decision's instructions (and a person's `extra`) go
    into the prompt under a `## Decision` heading, so every later $prompt sees them, and every step
    from `from_step` on runs again. Records of steps outside the workflow (deliver, decide) stay."""
    names = step_names(adw)
    start = str(decision.get("from_step") or decision.get("stopped_at") or "")
    reset = set(names[names.index(start):]) if start in names else set()
    for record in state.steps:
        if record.get("name") in reset:
            record["status"] = "pending"
    number = state.prompt.count("\n## Decision (round ") + 1
    by_person = decision.get("mode") == "suggest"
    text = str(decision.get("instructions") or "").strip() or f"Run again from step `{start}`."
    state.prompt = (f"{state.prompt.rstrip()}\n\n## Decision (round {number}, {decision.get('mode')}"
                    f"{', answered by a person' if by_person else ''})\n{text}"
                    + (f"\n\nThe person adds: {extra.strip()}" if extra.strip() else ""))
    decision["status"] = "resuming"
    state.decision = decision
    if layer_dir is not None:
        state.save(Path(layer_dir))


def write_record(layer: Any, state: Any) -> Path:
    """agents/<id>/decision.md: the decision as a person reads it (no machine path)."""
    from . import ship

    d = state.decision or {}
    clean = lambda text: ship._without_machine_paths(layer, state, str(text or "")).replace(str(Path.home()), "~")  # noqa: E731
    lines = [f"# Decision for run {state.adw_id}", "",
             f"- mode: {d.get('mode')}", f"- action: {d.get('action')}",
             f"- from step: {d.get('from_step') or '-'}", f"- stopped at: {d.get('stopped_at') or '-'}",
             f"- status: {d.get('status')}", f"- round: {d.get('rounds', 0)}", "",
             "## Summary", "", clean(d.get("summary")), "", "## Explanation", "", clean(d.get("explanation")) or "-"]
    if d.get("instructions"):
        lines += ["", "## Instructions for the next round", "", clean(d.get("instructions"))]
    if d.get("answers"):
        lines += ["", "## Answers", ""] + [f"- {a.get('at', '')} {a.get('kind')} via {a.get('via')}"
                                           + (f": {clean(a.get('text'))}" if a.get("text") else "")
                                           for a in d["answers"]]
    if d.get("mode") == "suggest":
        lines += ["", "## How to answer", "", f"al answer LAYER {state.adw_id} go | alter TEXT | explain | reject"]
    path = state_module.run_dir(layer.layer_dir, state.adw_id) / RECORD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def resume_argv(layer: Any, state: Any) -> list[str] | None:
    """The command line of the resume: the layer's own adws/ script on this interpreter (the one the
    registry names for this run: a workflow's script, dispatch.py for a single command, promote.py or
    integrate.py for a kit run), in the same decider mode, a dry run again when this one was. None
    when no script of the layer continues the run."""
    from . import registry

    argv = registry.resume_argv(Path(layer.layer_dir), str(state.adw), getattr(state, "definition", None),
                                state.adw_id, decider=str((state.decision or {}).get("mode") or "off"),
                                dry_run=bool(state.dry_run))
    return None if argv is None else [sys.executable, *argv]


def _no_script(state: Any) -> str:
    return f"no script of the layer continues a run of {state.adw}: it is a kit run that recorded no definition"


def spawn_resume(layer: Any, state: Any) -> None:
    """Start the resume detached (its own session), its early output in agents/<id>/resume.log.
    Refused (LayerError) while a live process holds the run: it is left to that process."""
    held = state_module.run_holder(layer.layer_dir, state.adw_id)
    if held:
        raise LayerError(f"run {state.adw_id} is held by process {held.get('pid')} on {held.get('host')}; "
                         "it is left to that process")
    argv = resume_argv(layer, state)
    if argv is None:
        raise LayerError(_no_script(state))
    log = state_module.run_dir(layer.layer_dir, state.adw_id) / "resume.log"
    with log.open("ab") as handle:
        subprocess.Popen(argv, cwd=layer.layer_dir, stdin=subprocess.DEVNULL, stdout=handle,
                         stderr=subprocess.STDOUT, start_new_session=True)


def _start(layer: Any, state: Any, say: bool = True) -> None:
    if say:
        print(f"[adw {state.adw_id}] decider: resuming from {state.decision.get('from_step') or '?'}: "
              f"{' '.join((resume_argv(layer, state) or [])[2:])}", flush=True)
    with events.recording(layer.layer_dir, state.adw_id):
        events.emit("decision.resume", from_step=state.decision.get("from_step", ""),
                    mode=state.decision.get("mode", ""))
    spawn_resume(layer, state)


def after_run(layer: Any, state: Any) -> None:
    """Called by the runner after the run wrote its summary: start the resume the decider chose."""
    pending = (state.decision or {}).pop("entry_pending", None)
    if pending:
        try:
            from . import standin

            standin.amend(layer, str(pending), stop=standin.fingerprint(state))
        except Exception as exc:  # noqa: BLE001 — the record is a courtesy; the run has ended
            print(f"[adw {state.adw_id}] decider: the record was not completed: {type(exc).__name__}: {exc}")
        _save(layer, state)
    if (state.decision or {}).get("status") != "resuming":
        return
    from . import registry

    if resume_argv(layer, state) is None:   # nothing is started: a script that does not exist would only fail
        print(f"[adw {state.adw_id}] decider: the run is not resumed by the decider: {_no_script(state)}; it "
              "stays with a person", flush=True)
        return
    try:
        _start(layer, state)
    except Exception as exc:  # noqa: BLE001 — the run has ended; say why the resume did not start
        by_hand = registry.resume_words(Path(layer.layer_dir), str(state.adw), getattr(state, "definition", None),
                                        state.adw_id) or "no script of the layer continues it"
        print(f"[adw {state.adw_id}] decider: the resume did not start: {type(exc).__name__}: {exc}; "
              f"resume by hand: {by_hand}")


# ---------------------------------------------------------------- a person answers

_ALTER = re.compile(r"^(?:alter|yes,?\s+but(?:\s+with\s+these\s+(?:alterations|changes))?)\b\s*:?\s*(.*)$", re.I)
_WROTE = re.compile(r"^on .+ wrote:\s*$", re.I)


def parse_answer(text: str) -> tuple[str, str]:
    """A person's free text as (kind, text): go, alter (with the changes), explain, reject, or ("", "")."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    index = next((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith(">")), None)
    if index is None:
        return "", ""
    first = lines[index].strip()
    word = first.lower().rstrip(".!")
    if word in ("go", "go ahead", "yes", "ok", "okay"):
        return "go", ""
    if word in ("explain", "explain more"):
        return "explain", ""
    if word in ("reject", "no"):
        return "reject", ""
    match = _ALTER.match(first)
    if not match:
        return "", ""
    rest = [match.group(1)]
    for line in lines[index + 1:]:
        if line.lstrip().startswith(">") or line.rstrip() == "--" or line.startswith("-- "):
            break
        rest.append(line)
    while rest and (not rest[-1].strip() or _WROTE.match(rest[-1].strip())):
        rest.pop()
    return "alter", "\n".join(rest).strip()[:ANSWER_MAX]


def _refused(why: str, message: str) -> dict[str, str]:
    return {"status": "refused", "reason": why, "message": message}


def _recipients(layer: Any) -> set[str]:
    values = localenv.load(layer)
    found: set[str] = set()
    for notifier in notify.notifiers(layer):
        if notifier.get("kind") == "email" and notifier.get("toEnv"):
            text = localenv.lookup(str(notifier["toEnv"]), values)
            found |= {address.lower() for _name, address in email.utils.getaddresses([text]) if "@" in address}
    return found


def answer(layer: Any, adw_id: str, kind: str, text: str, via: str, code: str | None = None,
           sender: str | None = None) -> dict[str, str]:
    """Apply a person's answer to a suggestion, from any channel (cli, dashboard, mail, webhook).
    {status, message} (+ reason when refused); a refusal changes nothing. A suggestion that is not a
    resume (restart, answer, integrate, land, settle, promote) is performed by standin.apply."""
    from . import standin

    if isinstance(layer, (str, Path)):
        from . import ir

        layer = ir.load(Path(layer))
    adw_id = str(adw_id or "")
    if not re.fullmatch(r"[0-9a-fA-F]{8}", adw_id):
        return _refused("unknown-run", f"no run {adw_id!r} (a run id is 8 hexadecimal characters)")
    try:
        state = state_module.load(layer.layer_dir, adw_id)
    except LayerError:
        return _refused("unknown-run", f"no run {adw_id} in this layer")
    held = state_module.run_holder(layer.layer_dir, adw_id)
    if held:
        return _refused("running", f"run {adw_id} is held by process {held.get('pid')}: it is left to that process")
    if kind not in ANSWERS:
        return _refused("bad-answer", f"unknown answer {kind!r}; use one of {', '.join(ANSWERS)}")
    if via == "stand-in" and kind in ("go", "alter"):
        return _refused("own-work", standin.OWN_WORK)
    decision = state.decision or {}
    status = str(decision.get("status") or "")
    if status not in ANSWERABLE:
        return _refused("not-waiting", f"run {adw_id} has no suggestion waiting for an answer "
                                       f"(decision status: {status or 'none'})")
    if via not in ("cli", "dashboard") and not hmac.compare_digest(str(code or ""), str(decision.get("code") or "")):
        return _refused("bad-code", "the answer's code does not match the run's suggestion")
    if via == "mail" and (sender or "").strip().lower() not in _recipients(layer):
        return _refused("bad-sender", "the sender is not a recipient of the layer's notifications")
    text = str(text or "").strip()[:ANSWER_MAX]
    if kind == "alter" and not text:
        return _refused("bad-answer", "alter needs the changes as text")
    adw = state.definition or layer.adw(state.adw)
    action = str(decision.get("action") or "")
    beyond = standin.KIND_OF.get(action) not in (None, "resume")   # not a resume: standin.apply performs it
    if beyond and kind == "alter" and action in ("land", "promote"):
        return _refused("bad-answer", f"alter does not apply to {action}; answer go or reject")
    decision.setdefault("answers", []).append({"kind": kind, "text": text, "via": via, "at": state_module.now()})
    state.decision = decision
    stop = standin.fingerprint(state)   # the stop the answer is about, before a resume resets its steps

    def answered(status: str, **more: Any) -> None:
        standin.record(layer, {"type": "answer", "who": "person", "via": via, "run": adw_id, "answer": kind,
                               "kind": standin.KIND_OF.get(action, ""), "action": action, "status": status,
                               "stop": stop, **more}, state)

    if beyond and kind in ("go", "alter"):
        answered("acted")
        with events.recording(layer.layer_dir, adw_id):
            events.emit("decision.answer", kind=kind, via=via, status="applying")
        result = standin.apply(layer, state, decision, who="person", via=via, extra=text if kind == "alter" else "")
        if result["status"] != "acted":
            return _refused("not-applied", f"{action} was not applied: {result.get('reason') or result['status']}")
        return {"status": str(decision.get("status") or "applied"), "message": str(result.get("outcome") or action)}
    if kind == "go" and decision.get("approvals"):
        from . import approvals

        approvals.record(state, [str(item) for item in decision["approvals"]], who=sender or via, via=f"answer:{via}")
    if kind in ("go", "alter"):
        extra = ""
        if kind == "alter" and decision.get("action") in ("abandon", "ask-human"):
            decision.update(action="resume-with", from_step=decision.get("stopped_at") or decision.get("from_step"),
                            instructions=text)
        elif kind == "alter":
            extra = text
        prepare_resume(state, adw, decision, extra=extra, layer_dir=layer.layer_dir)
        message = f"resuming (from step {decision.get('from_step') or '?'})"
    elif kind == "explain":
        from . import ship

        explanation = ship._without_machine_paths(layer, state, str(decision.get("explanation") or ""))
        explanation = explanation.replace(str(Path.home()), "~")[:EXPLAIN_MAX] or "(the decider gave no explanation)"
        sent = notify.send_decision(layer, state, explanation)
        decision["status"] = "explained"
        message = f"explained (sent through {sent} notifier(s))" if sent else "explained (no notifier accepted it)"
    else:
        decision["status"] = "rejected"
        message = "rejected; the run stays with a person"
    state.save(layer.layer_dir)
    write_record(layer, state)
    with events.recording(layer.layer_dir, adw_id):
        events.emit("decision.answer", kind=kind, via=via, status=decision["status"])
    answered({"resuming": "acted", "rejected": "refused"}.get(decision["status"], "suggested"),
             **({"outcome": message} if decision["status"] == "resuming" else {}))
    if decision["status"] == "resuming":
        _start(layer, state, say=False)   # the caller reports the answer's result
    return {"status": decision["status"], "message": message}


# ---------------------------------------------------------------- answers by e-mail

@dataclass(frozen=True)
class ImapUrl:
    scheme: str
    host: str
    port: int
    user: str
    password: str
    mailbox: str


def parse_imap_url(text: str, name: str = ENV_IMAP) -> ImapUrl:
    """`imaps://user:pass@host[:port][/MAILBOX]`; plain `imap://` only to this machine. An error
    names the variable, never the URL's text."""
    def unusable(why: str) -> MailError:
        return MailError(f"{name} is not a usable IMAP URL: {why}")

    try:
        parts = urllib.parse.urlsplit(str(text).strip())
        host = parts.hostname or ""
    except ValueError:
        raise unusable("no host") from None
    scheme = parts.scheme.lower()
    if scheme not in _IMAP_PORTS:
        raise unusable("unknown scheme (use imaps://, or imap:// to this machine)")
    if not host:
        raise unusable("no host")
    try:
        port = parts.port or _IMAP_PORTS[scheme]
    except ValueError:
        raise unusable("bad port") from None
    if not 1 <= port <= 65535:
        raise unusable("bad port")
    if not parts.username:
        raise unusable("no user")
    if parts.query or parts.fragment:
        raise unusable("unknown option")
    if scheme == "imap" and not notify.loopback(host):
        raise MailError("refused: plain imap:// to a host that is not this machine would send the password "
                        "unencrypted; use imaps://")
    mailbox = urllib.parse.unquote(parts.path.strip("/")) or "INBOX"
    return ImapUrl(scheme, host, port, urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""),
                   mailbox)


def _mail_failure(exc: BaseException) -> str:
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return f"timed out after {MAIL_TIMEOUT} s"
    if isinstance(exc, ssl.SSLError):
        return f"TLS failed ({type(exc).__name__})"
    if isinstance(exc, imaplib.IMAP4.error):
        return f"the server refused a command ({type(exc).__name__})"
    if isinstance(exc, OSError):
        return f"could not connect ({type(exc).__name__})"
    return type(exc).__name__


def _body_text(message: Any) -> str:
    part = message.get_body(preferencelist=("plain",)) if hasattr(message, "get_body") else None
    try:
        return str((part or message).get_content())
    except (KeyError, LookupError, AttributeError):
        return ""


def _poll(layer: Any, url: ImapUrl, out: Callable[[str], Any]) -> int:
    if url.scheme == "imaps":
        connection = imaplib.IMAP4_SSL(url.host, url.port, ssl_context=notify.tls_context(url.host), timeout=MAIL_TIMEOUT)
    else:
        connection = imaplib.IMAP4(url.host, url.port, timeout=MAIL_TIMEOUT)
    applied = 0
    try:
        connection.login(url.user, url.password)
        connection.select(url.mailbox)
        _typ, data = connection.search(None, "UNSEEN")
        for number in (data[0] or b"").split() if data else []:
            _typ, parts = connection.fetch(number, "(BODY.PEEK[])")
            raw = next((item[1] for item in parts or [] if isinstance(item, tuple) and len(item) > 1), b"")
            message = email.message_from_bytes(raw, policy=email.policy.default)
            match = TOKEN.search(str(message.get("Subject") or ""))
            if not match:
                continue                                # not an answer: left unseen for its reader
            run, code = match.group(1).lower(), match.group(2)
            kind, text = parse_answer(_body_text(message))
            sender = email.utils.parseaddr(str(message.get("From") or ""))[1]
            if not kind:
                out(f"decide mail: run {run}: the reply holds no answer (go, alter …, explain, reject)")
                result = {"status": "refused"}
            else:
                result = answer(layer, run, kind, text, via="mail", code=code, sender=sender)
                out(f"decide mail: run {run}: {kind}: " + (result["message"] if result["status"] != "refused"
                                                            else f"refused: {result['message']}"))
            connection.store(number, "+FLAGS", "\\Seen")
            if result["status"] != "refused":
                applied += 1
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001 — the answers are applied; a failed goodbye changes nothing
            pass
    return applied


def poll_mail(layer: Any, out: Callable[[str], Any] = print) -> int:
    """Read unseen replies over IMAP and apply each that carries a `[decide RUN-CODE]` token from a
    recipient of the layer's e-mail notifier. Returns how many were applied. Does nothing without
    AL_DECIDE_IMAP_URL; bounded by MAIL_TIMEOUT; never raises."""
    try:
        values = localenv.load(layer)
        text = localenv.lookup(ENV_IMAP, values).strip()
        if not text:
            return 0
        url = parse_imap_url(text)
        finished, outcome = notify._bounded(lambda: _poll(layer, url, out), MAIL_TIMEOUT)
        if not finished:
            out(f"decide mail: timed out after {MAIL_TIMEOUT} s")
            return 0
        if isinstance(outcome, BaseException):
            out(f"decide mail: {_mail_failure(outcome)}")
            return 0
        return int(outcome or 0)
    except Exception as exc:  # noqa: BLE001 — a mailbox never stops a trigger
        out(f"decide mail: {_mail_failure(exc)}")
        return 0
