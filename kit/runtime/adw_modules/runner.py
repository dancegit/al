from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import affected, approvals, assign, budget, checkenv, decide, delivery, delivery_request, envs, envsource
from . import events, ir, models
from . import nested_engine, notify, placement, prepare, sandbox, selfrun, skips, testing
from . import state as state_module, steps
from . import slots
from .agents import CLASSES
from .errors import LayerError, RunStopped, StepFailed, StepWaiting

EXIT_WAITING = 75  # a (script …) step is waiting for something outside: run again with --resume
HANDED_OVER = "handed-over"  # the workflow ran to its end, but a person decides (rejected review, gate fallback)
STOPPED = "stopped by a person"  # the reason of a run ended by SIGTERM (launch.stop, the dashboard's Stop)


class UsageError(Exception):
    """Bad command line; exit 2."""


@dataclass
class Request:
    prompt: str
    source: str
    klass: str | None
    model_set: str | None
    hooks: Any | None  # inputs.resolve() result with on_start/on_finish, or None


def log_stamp() -> str:
    """The UTC time that starts every run.log line: 2026-11-15T10:04:12.345Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _Tee:
    """Writes progress to the terminal unchanged and to agents/<id>/run.log with each line
    prefixed by its UTC time (`2026-11-15T10:04:12.345Z [adw 1a2b3c4d] step plan (run) start`).
    Readers that look for the usual `[adw …]` / `[slots]` prefixes strip the stamp first."""

    def __init__(self, terminal: TextIO, log: TextIO) -> None:
        self.terminal, self.log = terminal, log
        self.partial = ""

    def write(self, value: str) -> int:
        lines = (self.partial + value).split("\n")
        self.partial = lines.pop()
        if lines:
            self.log.write("".join(f"{log_stamp()} {line}\n" for line in lines))
            self.log.flush()
        return self.terminal.write(value)

    def flush(self) -> None:
        self.log.flush()
        self.terminal.flush()

    def close(self) -> None:
        if self.partial:
            self.log.write(f"{log_stamp()} {self.partial}\n")
            self.partial = ""
        self.log.flush()


OVERRIDES_HELP = """\
models and spending, for this run only (the layer file is not changed):
  --model-set TIER       every agent step of this run uses TIER (a tier the agents declare: base,
                         heavy, or one from #:models). Environment: AL_MODEL_SET=TIER
  --model TIER=MODEL_ID  this run uses MODEL_ID wherever tier TIER is asked for (repeatable).
                         Environment: AL_MODELS="heavy=claude-fable-5-2,base=claude-opus-5-5"
  --max-cost USD         stop the run (and hand it to a person) once its agent calls cost more than
                         USD; replaces the layer's #:run-usd. Environment: AL_MAX_COST=USD
  Flags win over the environment variables. Triggers and `al improve` pass the variables on.

which tier an agent step runs on (first match wins):
  1. --model-set / AL_MODEL_SET
  2. a run step's own #:model-set
  3. the request's tier: a task-file tag ({heavy}) or an issue keyword (adw: sdlc heavy)
  4. the command's or hop's #:model-set

which model a tier means (first match wins):
  1. --model TIER=MODEL_ID
  2. AL_MODELS
  3. the layer's local setting (agents/settings/models.json, never committed)
  4. the layer definition (#:models / #:model-heavy)
  5. the built-in default
  A local setting is changed with `al models LAYER --set TIER=ID` or the dashboard; it applies to
  runs started afterwards. Runs in flight (and resumed runs) keep the models they started with.
  state.json (agent_calls) and the run's events record the tier, model and reason of every call.

the stand-in decider (when a run stops and is handed to a person):
  --decider MODE         off, suggest or auto for this run. Environment: AL_DECIDER=MODE; else the layer's
                         local setting (`al decider LAYER --set MODE`, the dashboard's Stand-in panel,
                         agents/settings/decider.json), else the layer's (stand-in KIND …) declaration, else off.
                         suggest: a strong model proposes resume, resume-with, abandon or ask-human in the
                         needs-human message; a person answers with `al answer LAYER RUN go|alter|explain|reject`,
                         on the dashboard, with a reply to the message, or the webhook. auto: a resume starts at
                         once when `resume` is in its scope (the declaration's #:may; without a declaration:
                         resume only), at most #:max-actions-per-run times per run (without a declaration:
                         twice). It never merges or deploys: a resumed run passes every check and the gate again.

where the run executes (only for a layer that declares (placement …)):
  --on HOST              run on this host of #:hosts (`home` is this machine) instead of letting the
                         scheduler pick one; refused when the host cannot take the run
  --follow               with --adw-id: follow a run on another host again after its proxy here died,
                         and finish it here
  --restart              with --adw-id: start a lost run again on another host (or --on HOST) from the
                         last commit it pushed

notifications: a --dry-run sends none of the layer's (notify …) messages unless --notify is given.

exit codes: 0 passed or handed-over (the workflow ran to its end; handed-over: a rejected review or a
              gate that fell back, so a person decides; see the summary's handoff line),
            1 failed (handed to a person where the workflow ships),
            2 usage error, 3 refused: the layer's day limit (#:day-usd) or its subscription usage threshold (#:limit-stop-percent) is reached,
              or no host can take the run,
            75 waiting: a script step is waiting for something outside; run again with --resume
"""


TESTING_CHOICES = (*testing.LEVELS, *testing.DEPTHS)


def _parser(adw: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"adw_{adw}", description=f"Run the {adw} ADW.", epilog=OVERRIDES_HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="?", help="high-level prompt")
    parser.add_argument("--prompt-file", type=Path, help="read the prompt from a file")
    parser.add_argument("--source", help="KIND:REF prompt source, e.g. issue:42")
    parser.add_argument("--class", dest="klass", choices=CLASSES,
                        help="fix the work class (skips classify routing; wins over a class label in the request)")
    parser.add_argument("--adw-id", help="8 hex chars; default random")
    parser.add_argument("--resume", action="store_true", help="continue --adw-id, skipping passed and rejected steps")
    parser.add_argument("--dry-run", action="store_true", help="use the mock agent regardless of the ADW agent")
    parser.add_argument("--keep-worktree", action="store_true", help="never remove the worktree")
    parser.add_argument("--model-set", metavar="TIER", help="every agent step of this run uses TIER (see below)")
    parser.add_argument("--model", action="append", default=[], metavar="TIER=MODEL_ID",
                        help="use MODEL_ID for tier TIER in this run (repeatable)")
    parser.add_argument("--max-cost", metavar="USD", help="run spending limit in US dollars for this run")
    parser.add_argument("--notify", action="store_true", help="send the layer's notifications even in a --dry-run")
    parser.add_argument("--replaces", metavar="OLD", help="this run starts run OLD (8 hex chars) again; OLD then "
                        "reads as replaced by this run")
    parser.add_argument("--decider", choices=decide.MODES, help="the stand-in decider for this run (see below)")
    parser.add_argument("--testing", choices=TESTING_CHOICES, metavar="LEVEL",
                        help="how deep this run's checks go: light|standard|heavy (also smoke|targeted|full); "
                             "default: the local setting, else the layer's (testing …)")
    parser.add_argument("--approve", action="append", default=[], metavar="WHAT",
                        help="pre-approve a decision only a person may take, for this run (repeatable): "
                             "vocabulary, edit:PATH, dependency:NAME")
    parser.add_argument("--approved-by", metavar="WHO", default=os.environ.get("AL_APPROVED_BY") or _user(),
                        help="who gave the --approve approvals (default: AL_APPROVED_BY, else the login name)")
    parser.add_argument("--on", metavar="HOST", help="run on this host of the layer's (placement …)")
    parser.add_argument("--follow", action="store_true", help="follow --adw-id on its host again and finish it here")
    parser.add_argument("--restart", action="store_true", help="start the lost run --adw-id again on another host")
    return parser


def _user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def _record_approvals(state: state_module.ADWState, args: argparse.Namespace) -> None:
    """The run's --approve approvals, each validated (a bad one is a usage error), recorded with who and when."""
    whats = list(getattr(args, "approve", None) or [])
    try:
        tokens = [approvals.parse(what) for what in whats]
    except ValueError as exc:
        raise UsageError(f"--approve: {exc}") from exc
    if tokens:
        approvals.record(state, tokens, str(getattr(args, "approved_by", "") or _user()), "start")


def _stdin_allowed(layer: Any, adw: dict[str, Any]) -> bool:
    for input_id in adw.get("input", []):
        option = layer.option("promptInputs", input_id)
        if option["kind"] == "cli" and option.get("allowStdin"):
            return True
    return False


def resolve_request(layer: Any, adw: dict[str, Any], args: argparse.Namespace) -> Request:
    prompt = args.prompt or ""
    if args.prompt_file:
        try:
            prompt = args.prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --prompt-file: {exc}") from exc
    if args.source:
        if ":" not in args.source:
            raise UsageError("--source must be KIND:REF")
        from . import inputs  # written by the inputs integration; imported only when a source is given

        resolved = inputs.resolve(layer, args.source, prompt)
        return Request(str(resolved.prompt), args.source, resolved.klass, resolved.model_set, resolved)
    if not prompt and _stdin_allowed(layer, adw) and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise UsageError("a prompt is required (argument, --prompt-file, --source or stdin)")
    return Request(prompt.strip(), "cli", None, None, None)


# An explicit class label at the start of a request: (class, what the first line starts with once
# its heading and emphasis marks are gone). The colon follows the word directly: "Fix the login"
# and "Feature flags are broken" carry no label.
_LABEL_MARKS = re.compile(r"(?:#+[ \t]+)?[*_]*")
_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bug", re.compile(r"(?:(?:zte|urgent)[ \t]+)?bugfix\b|bug:|\[bug\]|fix:", re.IGNORECASE)),
    ("chore", re.compile(r"chore:|\[chore\]", re.IGNORECASE)),
    ("feature", re.compile(r"feature:|\[feature\]", re.IGNORECASE)),
)
CLASS_FROM_LABEL = "request-label"


def request_label(prompt: str) -> tuple[str, str] | None:
    """The class a request names itself at its start: (class, its first line clipped to 80
    characters), or None. Only the first non-blank line is read ("# BUGFIX run for …", "Bug: …",
    "[chore] …", "Feature: …"); a label on any later line does not count."""
    for line in str(prompt or "").splitlines():
        if not line.strip():
            continue
        text = line.strip()
        rest = text[_LABEL_MARKS.match(text).end():]
        for klass, pattern in _LABELS:
            if pattern.match(rest):
                return klass, text[:80]
        return None
    return None


def _new_state(adw_name: str, adw: dict[str, Any], args: argparse.Namespace, request: Request) -> state_module.ADWState:
    fixed = args.klass or request.klass
    if fixed is not None and fixed not in CLASSES:
        raise LayerError(f"prompt source returned unknown class {fixed!r}")
    class_from = ""
    if fixed is None:   # the request says what it is; a class the workflow does not take stays with classify
        label = request_label(request.prompt)
        if label is not None and (not adw.get("classes") or label[0] in adw["classes"]):
            fixed, class_from = label[0], CLASS_FROM_LABEL
    initial = fixed or (adw.get("classes") or ["chore"])[0]
    requested = getattr(args, "testing", None)
    state = state_module.ADWState(
        adw_id=args.adw_id or state_module.new_adw_id(), adw=adw_name, prompt=request.prompt,
        source=request.source, klass=initial, class_fixed=fixed is not None, class_from=class_from,
        dry_run=args.dry_run,
        model_set=request.model_set or "", fanout_parent=fanout_parent(),
        replaces=getattr(args, "replaces", None) or "", decider=getattr(args, "decider", None) or "",
        testing={"requested": testing.normalise(requested)} if requested else {},
    )
    _record_approvals(state, args)
    return state


def _check_replaces(layer: Any, old: str) -> str:
    """The run `--replaces OLD` names: 8 hex characters and a run of this layer, else UsageError."""
    if not re.fullmatch(r"[0-9a-f]{8}", old or ""):
        raise UsageError("--replaces needs a run id of 8 hexadecimal characters")
    if not (state_module.run_dir(layer.layer_dir, old) / "state.json").is_file():
        raise UsageError(f"--replaces {old}: the layer has no such run")
    return old


def _age_seconds(stamp: str) -> float | None:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def previous_attempt(layer: Any, source: str, adw_id: str) -> str:
    """The run this one retries: the newest earlier run of the same prompt source (issue:N, a
    task-list line; never cli or manual) that failed, or that still says running but whose
    heartbeat stopped long ago, and that no other run replaces already. "" when there is none."""
    kind = (source or "").partition(":")[0]
    if ":" not in (source or "") or kind in ("cli", "manual"):
        return ""
    found: list[tuple[str, str]] = []
    replaced: set[str] = set()
    for path in sorted((Path(layer.layer_dir) / "agents").glob("*/state.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or path.parent.name == adw_id:
            continue
        if data.get("replaces"):
            replaced.add(str(data["replaces"]))
        if data.get("source") != source:
            continue
        status = data.get("status")
        stale = status == "running" and (_age_seconds(data.get("heartbeat") or "") or 0) > state_module.HEARTBEAT_SECONDS * 12
        if status == "failed" or stale:
            found.append((str(data.get("started") or ""), path.parent.name))
    open_ = [item for item in found if item[1] not in replaced]
    return max(open_)[1] if open_ else ""


def fanout_parent() -> dict[str, Any]:
    """{run, step, index} when this run is a fan-out candidate (the parent exports ADW_FANOUT_*)."""
    parent = os.environ.get("ADW_FANOUT_PARENT", "").strip()
    if not parent:
        return {}
    index = os.environ.get("ADW_FANOUT_INDEX", "").strip()
    return {"run": parent, "step": os.environ.get("ADW_FANOUT_STEP", ""),
            "index": int(index) if index.isdigit() else None}


def _resumed_state(layer: Any, adw_name: str, args: argparse.Namespace) -> state_module.ADWState:
    if not args.adw_id:
        raise UsageError("--resume needs --adw-id")
    state = state_module.load(layer.layer_dir, args.adw_id)
    if state.adw != adw_name:
        raise LayerError(f"run {args.adw_id} belongs to ADW {state.adw!r}, not {adw_name!r}")
    state.status, state.ended = "running", ""
    if state.ship.get("action") == "handoff" or "handoff_skipped" in state.ship or "handoff_error" in state.ship:
        state.ship = {"previous_handoff": dict(state.ship)}  # the resumed run decides afresh
    state.dry_run = state.dry_run or args.dry_run
    if args.klass:
        state.klass, state.class_fixed = args.klass, True
    state.budget.pop("stopped", None)  # the resumed run is judged afresh against its limits
    state.usage.pop("stopped", None)
    if state.decision.get("status") == "resuming":  # the decider (or a person's answer) started this resume
        state.decision["status"] = "resumed"
    if getattr(args, "testing", None):  # a resume may ask for another depth; later checks use it
        state.testing["requested"] = testing.normalise(args.testing)
    _record_approvals(state, args)
    return state


def summary_lines(layer: Any, state: state_module.ADWState) -> list[str]:
    def shown(data: dict[str, Any]) -> str:
        # a check that was not run is never shown as its exit code: not affected or reused, with why;
        # a result that carries its depth (testing …) says it: passed (targeted: ran 2 of 43 tests …)
        if data.get("outcome") in (affected.NOT_AFFECTED, affected.REUSED) or isinstance(data.get("testing"), dict):
            return affected.summary_text(data)
        return str(data["exit"])

    checks = ", ".join(f"{name}={shown(data)}" + (f" ({skips.summary_text(data['skips'])})"
                                                  if skips.summary_text(data.get("skips") or {}) else "")
                       for name, data in state.checks.items()) or "none"
    gate = ", ".join(f"{row['check']}={affected.summary_text(row)}"
                     for row in affected.rows(layer, state) if row["where"] == "gate")
    review = _review_line(state)
    ship = state.ship.get("action", "none")
    if state.ship.get("action"):
        ship += f" -> {state.ship.get('target', '')} (shipped={state.ship.get('shipped')})"
    handoff = _handoff_line(state)
    return [
        "=== ADW summary ===",
        f"adw:       {state.adw} {state.adw_id}",
        f"status:    {state.status}",
        f"branch:    {state.branch or 'none'}",
        *([f"tiers:     {_tiers_line(state)}"] if state.models else []),
        *[f"testing:   {line}" for line in [_testing_line(layer, state)] if line],
        f"plan:      {state.plan or 'none'}",
        f"checks:    {checks}",
        *([f"gate:      {gate}"] if gate else []),
        f"review:    {review}",
        *([f"proof:     {_proof_line(state)}"] if _proof_line(state) else []),
        f"cost:      {budget.summary(state)}",
        *([f"models:    {_models_line(state)}"] if state.agent_calls else []),
        f"ship:      {ship}",
        *([f"request:   {delivery_request.summary_line(state)}"] if state.delivery_request else []),
        *([f"kept:      {_kept_line(state)}"] if state.kept.get("changed") or state.kept.get("error") else []),
        *([f"delivery:  {_delivery_line(state)}"] if state.delivery or state.kept else []),
        *([f"promotion: {_promotion_line(state)}"] if state.promotion else []),
        *([f"integrate: {_integration_line(state)}"] if state.integration else []),
        *([f"replaces:  {state.replaces}"] if state.replaces else []),
        *([handoff] if handoff else []),
        *([f"decision:  {_decision_line(state)}"] if state.decision else []),
        *([f"host:      {_host_line(state)}"] if state.remote else []),
        f"artefacts: {len(state.artefacts)}",
        *([f"resume:    {resume_command(state)}"] if state.status == "waiting" else []),
        f"state:     {state_module.run_dir(layer.layer_dir, state.adw_id) / 'state.json'}",
    ]


def _kept_line(state: state_module.ADWState) -> str:
    """What keep.py kept of a run without a ship step, e.g. `branch chore/1a2b3c4d-report (2 files)`."""
    kept = state.kept
    if kept.get("error"):
        return f"FAILED ({str(kept['error'])[:160]}); the worktree is kept"
    count = kept.get("count", 0)
    if kept.get("branch"):
        line = f"branch {kept['branch']} ({count} files)"
    else:
        line = f"no branch; {kept.get('artefacts', 0)} files as artefacts"
    too_large = len(kept.get("too_large") or [])
    return line + (f", {too_large} too large for the artefacts" if too_large else "")
def _host_line(state: state_module.ADWState) -> str:
    """Where the run executed, e.g. `build-2 (delivered chore/1a2b3c4d-tidy at 0123456789ab; gate on home)`."""
    remote = state.remote
    delivered = remote.get("delivered") or {}
    parts = []
    if delivered.get("sha"):
        parts.append(f"delivered {delivered.get('branch')} at {str(delivered['sha'])[:12]}")
    if remote.get("lost"):
        parts.append(f"lost: {remote['lost'].get('reason')}")
    elif remote.get("phase") == "gate" and not selfrun.active():
        parts.append("gate on home")
    if remote.get("restart_of"):
        parts.append(f"started again after {remote['restart_of']}")
    return str(remote.get("host") or "?") + (f" ({'; '.join(parts)})" if parts else "")


def _delivery_line(state: state_module.ADWState) -> str:
    """Where the run delivers, e.g. `sprint -> sprint/2026-s47 (merge; delivered=True)`; for a run
    without a ship step, what happened to its work (see keep.py)."""
    d = state.delivery
    if state.kept:
        stage = f" (stage {d.get('stage')} -> {d.get('branch')})" if d else ""
        if state.kept.get("error"):
            return "none (no ship step); keeping failed, the worktree is kept" + stage
        if state.kept.get("changed"):
            where = f"on branch {state.kept['branch']}" if state.kept.get("branch") else "as artefacts"
            return f"none (no ship step); work kept {where}" + stage
        return "nothing changed" + stage
    if d.get("refused"):
        return f"{d.get('stage') or '?'} -> {d.get('branch') or '?'} (refused: {d['refused']})"
    where = f" @ {d['environment_name']}" if d.get("environment_name") else ""
    return (f"{d.get('stage')} -> {d.get('branch')} ({d.get('entry', 'merge')}; "
            f"delivered={d.get('delivered')}){where}")


def _proof_line(state: state_module.ADWState) -> str:
    """What the plan's Proof plan came to, e.g. `3 criteria, 3 proven; 1 round; 2 debt tasks filed` or
    `stopped at prove: needs-declaration (layer author)`; "" for a plan without the table."""
    plan = state.proof_plan or {}
    if plan.get("table") not in ("parsed", "invalid"):
        return ""
    stopped = plan.get("stopped") or {}
    if stopped:
        return f"stopped at {stopped.get('at')}: {stopped.get('outcome')} ({stopped.get('party')})"
    rows = list(plan.get("rows") or [])
    proven = sum(1 for row in rows if row.get("state") == "proven")
    rounds = len((state.review or {}).get("rounds") or [])
    debt = len(plan.get("debt") or [])
    parts = [f"{len(rows)} criteri{'on' if len(rows) == 1 else 'a'}, {proven} proven"]
    if rounds:
        parts.append(f"{rounds} round{'' if rounds == 1 else 's'}")
    if debt:
        parts.append(f"{debt} debt task{'' if debt == 1 else 's'} filed")
    return "; ".join(parts)


def _review_line(state: state_module.ADWState) -> str:
    """`pass (human)`, `pass (agent) after 2 round(s)` or `rejected (auto) after 3 round(s), 2 blocker(s)`."""
    verdict = state.review.get("verdict")
    if not verdict:
        return "none"
    rounds = state.review.get("rounds") or []
    mode = verdict.get("mode") or state.review.get("mode")
    if not rounds:
        return f"{'pass' if verdict.get('success') else 'fail'} ({mode})"
    if verdict.get("success"):
        return f"pass ({mode}) after {len(rounds)} round(s)"
    blockers = sum(1 for issue in verdict.get("issues", []) if issue.get("severity") == "blocker")
    return f"rejected ({mode}) after {len(rounds)} round(s), {blockers} blocker(s)"


def handover_reasons(layer: Any, state: state_module.ADWState, adw: dict[str, Any]) -> list[str]:
    """Why a run that reached its end is handed to a person instead of having passed: a rejected
    review, an auto gate that decided not to ship, or a ship fallback. Agent and human reviews
    always open a PR by design, so they are never a fallback by themselves."""
    reasons: list[str] = []
    if steps.review_outcome(state)[0] is False:
        reasons.append("review-rejected")
    option = layer.option("reviews", adw["review"]) if adw.get("review") else {}
    auto_refused = option.get("kind") == "auto" and state.ship.get("gate", {}).get("ship") is False
    if auto_refused or state.ship.get("fallback"):
        reasons += [str(reason) for reason in state.ship.get("reasons") or [] if str(reason) not in reasons]
    return reasons
def _promotion_line(state: state_module.ADWState) -> str:
    """What a promotion run did, e.g. `sprint/2026-s47 -> main (merge; 3 changes, 1 refused; tag v0.2.0;
    waiting at approval)`."""
    p = state.promotion
    changes = list(p.get("changes") or [])
    refused = sum(1 for change in changes if change.get("status") == "refused")
    parts = [str(p.get("method") or "merge"),
             f"{len(changes)} change{'' if len(changes) == 1 else 's'}" + (f", {refused} refused" if refused else "")]
    if p.get("tag"):
        parts.append(f"tag {p['tag']}")
    if state.status == "waiting":
        waiting = next((str(r.get("name")) for r in state.steps if r.get("status") == "waiting"), "")
        parts.append(f"waiting at {waiting or 'a step'}")
    elif p.get("result"):
        parts.append(f"at {str(p['result'])[:7]}")
    notes = [str(note) for note in p.get("notes") or []]
    line = f"{p.get('from_branch') or p.get('from')} -> {p.get('to_branch') or p.get('to')} ({'; '.join(parts)})"
    return line + "".join(f"\n           note: {note}" for note in notes)


def _integration_line(state: state_module.ADWState) -> str:
    """What an integration run did, e.g. `run f0000001 onto main at 1a2b3c4 (2 conflicted files: 1 resolved,
    1 regenerated; conflicted before: stock.py); waiting at approve`."""
    i = state.integration
    conflicts = list(i.get("conflicts") or [])
    report = i.get("report") or {}
    if i.get("clean"):
        what = "applies cleanly"
    elif not conflicts:
        what = "not applied yet"
    else:
        what = (f"{len(conflicts)} conflicted file{'' if len(conflicts) == 1 else 's'}: "
                f"{len(report.get('resolved') or [])} resolved, {len(report.get('regenerated') or [])} regenerated")
        if report.get("undecidable"):
            what += f", {len(report['undecidable'])} undecidable"
    seen = ", ".join(str(item.get("file")) for item in i.get("seen_before") or [])
    mode = str(i.get("mode") or "conflict")
    line = ((f"{mode} of " if mode != "conflict" else "") + f"run {i.get('run')} onto {i.get('target')}"
            + (f" at {str(i['target_head'])[:7]}" if i.get("target_head") else "")
            + f" ({what}{f'; conflicted before: {seen}' if seen else ''})")
    if state.status == "waiting":
        waiting = next((str(r.get("name")) for r in state.steps if r.get("status") == "waiting"), "")
        line += f"; waiting at {waiting or 'a step'}"
    elif (i.get("stopped") or {}).get("reason"):
        line += f"; stopped at {i['stopped'].get('step')}: {str(i['stopped']['reason']).splitlines()[0][:200]}"
    return line


def resume_command(state: state_module.ADWState) -> str:
    """How a person (or a timer) continues a waiting run."""
    integration = (state.definition or {}).get("integration")
    if isinstance(integration, dict) and integration.get("run"):   # a person approves; that resumes it
        return f"uv run adws/integrate.py approve {integration['run']} --who NAME"
    promotion = (state.definition or {}).get("promotion")
    if isinstance(promotion, dict) and promotion.get("from"):
        return (f"uv run adws/promote.py run {promotion['from']} {promotion.get('to', '')} "
                f"--resume --adw-id {state.adw_id}")
    return f"uv run adws/adw_{state.adw}.py --resume --adw-id {state.adw_id}"


def _testing_line(layer: Any, state: state_module.ADWState) -> str:
    """`targeted (layer policy); escalated: core path a/core/x.py`: the depth this run's checks
    asked for, its source, and why a check ran deeper. "" for a layer without (testing …) and a run
    that asked for no depth, so their summary reads as before."""
    data = state.testing if isinstance(state.testing, dict) else {}
    try:
        declared = bool(testing.policy(layer)["declared"])
    except (LayerError, KeyError, TypeError):
        declared = False
    if not declared and not data:
        return ""
    depth, source = data.get("depth"), data.get("source")
    if not depth:
        try:
            depth, source = testing.run_depth(layer, state, "check")
        except (LayerError, OSError):
            return ""
    line = f"{depth} ({source})"
    reasons: list[str] = []
    for found in (data.get("escalated") or {}).values():
        reasons += [reason for reason in found if reason not in reasons]
    if reasons:
        line += "; escalated: " + "; ".join(reasons)
    if testing.requested_full(layer.layer_dir, state.adw_id):
        line += f"; gate: full ({testing.SOURCE_REQUEST})"
    if data.get("batch_started"):
        line += f"; started batch {data['batch_started']}"
    return line


def _tiers_line(state: state_module.ADWState) -> str:
    """Which model each tier of the ADW's agent meant and why, e.g. `heavy=claude-fable-5-2 (local setting)`."""
    return "; ".join(f"{tier}={spec.get('model') or '?'} ({spec.get('source', '?')})"
                     for tier, spec in sorted(state.models.items()))


def _models_line(state: state_module.ADWState) -> str:
    """Which tier and model the run's agent calls used, e.g. `heavy=claude-fable-5-2 ×3 (command); …`."""
    groups: dict[tuple[str, str, str], int] = {}
    for call in state.agent_calls:
        key = (str(call.get("tier", "")), str(call.get("model") or call.get("agent_kind") or ""),
               str(call.get("tier_from", "")) + (", model overridden" if call.get("model_from") == "override" else ""))
        groups[key] = groups.get(key, 0) + 1
    return "; ".join(f"{tier}={model or '?'} x{count} ({why})" for (tier, model, why), count in groups.items())


def _speak(layer: Any, line: str) -> None:
    voice = layer.ir.get("voice") or {}
    script = Path(layer.layer_dir) / "adws" / "voice" / "say.py"
    if not voice.get("speakSummaries") or not script.is_file():
        return
    try:
        subprocess.Popen([sys.executable, str(script), "--no-fetch", line], cwd=layer.layer_dir, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        print(f"[adw] warning: spoken summary failed: {exc}", file=sys.stderr)


def _execute(layer: Any, state: state_module.ADWState, adw: dict[str, Any], stack: envs.EnvStack,
             take_over: bool = False) -> None:
    stack.setup()
    state.save(layer.layer_dir)
    contained = selfrun.active()
    if contained:
        selfrun.after_setup(layer, state, stack)   # a run that starts again continues from its pushed commit
    elif state.remote.get("phase") == "gate" and (take_over or not state.remote.get("taken_over")):
        from . import remoterun

        if not state.remote.get("taken_over"):   # once: a resumed run keeps what it did here since
            remoterun.place_result(layer, state, stack)   # the commit the run delivered on its host, fetched here
        failure = remoterun.remote_failure(state) if take_over else None
        if failure is not None:
            raise failure   # the run failed on its host: the handoff happens here
    envsource.ensure_ready(layer, state, stack)  # the repository's own base image or dev shell, as of now
    sandbox.preflight(layer, state, adw, stack)  # podman stacks: current image, and what the workflow needs is there
    checkenv.preflight(layer, state, adw, stack)  # the podman environments its checks run #:in
    state.save(layer.layer_dir)
    prepare.run(layer, state, adw, stack)        # #:prepare (or postCreateCommand): once per run
    for step in adw.get("steps", []):
        if contained and step.get("kind") == "ship":
            selfrun.deliver(layer, state, adw, step, stack)   # the home machine ships, through its gate
            continue
        steps.run_step(layer, state, adw, step, stack)
        if contained:
            selfrun.checkpoint(layer, state, stack, step)
    if contained and "delivered" not in state.remote:
        selfrun.deliver(layer, state, adw, None, stack)   # a workflow without a (ship) step: its work still goes home


def _hand_off(layer: Any, state: state_module.ADWState, adw: dict[str, Any], stack: envs.EnvStack,
              failure: BaseException) -> None:
    """A stopped run hands its work to a human (see ship.handoff) while its worktree and
    container still exist, i.e. before teardown. The run stays failed whatever happens here."""
    from . import ship  # imported only when a run fails

    try:
        ship.handoff(delivery.ship_layer(layer), state, adw, stack, failure)
        affected.publish(layer, state)  # what each check proved, next to the handoff's text
    except Exception as exc:  # ship.handoff never raises by contract; this keeps the failure intact if it does
        state.ship["handoff_error"] = f"{type(exc).__name__}: {exc}"[:2000]
        print(f"[adw {state.adw_id}] warning: handoff failed: {type(exc).__name__}: {exc}")
    if state.ship.get("action") == "handoff" and state.ship.get("target") and not state.ship.get("handoff_error"):
        # read by the task-list source's on_finish as the failed task's reason
        state.error = f"needs a human: {state.ship['target']} (stopped at {state.ship.get('stopped_at')})"
    state.save(layer.layer_dir)


def _decision_line(state: state_module.ADWState) -> str:
    """What the stand-in decider did, e.g. `auto resume-with from build (resuming)`."""
    d = state.decision
    if d.get("skipped"):
        return f"not used ({d['skipped']})"
    if d.get("status") == "failed":
        return f"{d.get('mode', '?')} failed ({str(d.get('reason', ''))[:160]}); the run stays with a person"
    answered = (d.get("answers") or [{}])[-1].get("kind") if d.get("answers") else ""
    return (f"{d.get('mode', '?')} {d.get('action', '?')} from {d.get('from_step') or '?'} ({d.get('status', '?')})"
            + (f"; answered {answered}" if answered else ""))


def _stopped(layer: Any, state: state_module.ADWState, adw: dict[str, Any], stack: envs.EnvStack,
             failure: BaseException, args: argparse.Namespace) -> None:
    """A run stopped: here it is handed to a person and reported; on another host its work is
    pushed, and the home machine hands it over."""
    if selfrun.active():
        selfrun.deliver_stopped(layer, state, adw, stack, failure)
        return
    _hand_off(layer, state, adw, stack, failure)
    if isinstance(failure, RunStopped):  # a person stopped it: the stand-in decider does not resume it
        state.error = STOPPED + (f"; {state.error}" if getattr(state, "error", "") else "")
    else:
        decide.consider(layer, state, adw, stack, args)
        if decide.keeps_worktree(state):  # the resume (now, or after a person's `go`) continues in it
            stack.keep_worktree = True
    if notify.report(layer, state, force=bool(getattr(args, "notify", False)), at="handover"):
        state.save(layer.layer_dir)


def _overrides_text(over: dict[str, Any]) -> str:
    parts = []
    if over.get("tier"):
        parts.append(f"tier {over['tier']}")
    if over.get("models"):
        parts.append(",".join(f"{k}={v}" for k, v in over["models"].items()))
    if over.get("max_cost"):
        parts.append(f"max cost ${float(over['max_cost']):.2f}")
    return "; ".join(parts)


def _tiers_text(tiers: dict[str, Any]) -> str:
    return ", ".join(f"{tier}={spec.get('model') or '?'} ({spec.get('source', '?')})" for tier, spec in tiers.items())


def _handoff_line(state: state_module.ADWState) -> str | None:
    ship = state.ship
    if state.status == HANDED_OVER:
        target = ship.get("target") or f"branch {state.branch or 'none'}"
        return (f"handoff:   finished and handed to a person: {target} "
                f"({', '.join(ship.get('handed_over') or [])})")
    if ship.get("handoff_error"):
        return (f"handoff:   FAILED ({ship['handoff_error'][:160]}); the work stays on branch "
                f"{state.branch or 'none'}")
    if ship.get("action") == "handoff":
        return (f"handoff:   stopped at {ship.get('stopped_at')}; the work needs a human: {ship.get('target')} "
                f"(branch {ship.get('branch') or state.branch})")
    if ship.get("handoff_skipped"):
        return f"handoff:   none ({ship['handoff_skipped']})"
    return None


def _on_term(state: state_module.ADWState) -> Any:
    """The SIGTERM handler of a run (launch.stop signals the run's process group): while the run is
    working it raises RunStopped once, which ends the run `failed` through the usual failure path
    (hand-off, teardown, notification). A second signal (uv forwards the group's to its child) and a
    signal after the run's outcome is decided change nothing. `state.stopped_by` also makes the next
    step raise, in case the step that was interrupted swallowed the failure."""
    def handler(signum: int, frame: Any) -> None:
        if getattr(state, "stopped_by", "") or state.status != "running":
            return
        state.stopped_by = STOPPED
        raise RunStopped(steps.running_step(state) or "run", STOPPED)
    return handler


@contextlib.contextmanager
def _stoppable(state: state_module.ADWState):
    """Install _on_term for the duration of the run (only the main thread can install a handler)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, _on_term(state))
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run(layer: Any, state: state_module.ADWState, adw: dict[str, Any], args: argparse.Namespace,
         request: Request | None, take_over: bool = False) -> int:
    """`take_over`: the run executed on another host and continues here (remoterun.take_over); a
    run that failed there fails here right after setup, so the failure branch below hands it over.
    In a self-contained run (selfrun.active()) the run ends `delivered`, a failure pushes its work
    instead of handing it over, and nothing is notified: the home machine does all of that."""
    exit_code = 1
    contained = selfrun.active()
    stack = envs.build(layer, state, list(adw.get("environment", [])))
    envsource.apply_publish(layer, stack)  # a dev container's forwardPorts, published like #:publish
    stack.keep_worktree = bool(args.keep_worktree)
    beat = state_module.Heartbeat(state, layer.layer_dir).start()
    state.save(layer.layer_dir)
    events.say(f"[adw {state.adw_id}] {state.adw} start: class={state.klass} dry_run={state.dry_run}"
               + (f" overrides={_overrides_text(state.overrides)}" if _overrides_text(state.overrides) else "")
               + (f" models={_tiers_text(state.models)}" if state.models else ""), "run.start",
               adw=state.adw, adw_id=state.adw_id, klass=state.klass, dry_run=state.dry_run,
               resumed=bool(args.resume), pid=state.pid, host=state.host, fanout_parent=state.fanout_parent or None,
               overrides=state.overrides or None, models=state.models or None,
               limits=state.budget.get("limits") or None, replaces=state.replaces or None,
               class_from=state.class_from or None)
    if state.class_from == CLASS_FROM_LABEL and not args.resume:
        label = request_label(state.prompt)
        print(f"[adw {state.adw_id}] class fixed from the request label: {state.klass}"
              + (f" ({label[1]})" if label else ""))
    try:
        if request is not None and request.hooks is not None and not take_over:
            request.hooks.on_start(state)   # a taken-over run was announced when it was placed
        with slots.slot(layer, "runs", f"adw {state.adw_id}"):
            if stack.worktree_option:
                for removed in envs.collect_garbage(layer, stack.worktree_option):
                    print(f"[adw {state.adw_id}] gc: removed stale worktree {removed}")
            if any(nested_engine.enabled(option) for option in sandbox.podman_options(layer)):
                for removed in nested_engine.collect_stale_volumes(layer):
                    print(f"[adw {state.adw_id}] gc: removed stale nested-engine volume {removed}")
            _execute(layer, state, adw, stack, take_over)
        reasons = [] if contained else handover_reasons(layer, state, adw)
        if contained:
            state.status = "delivered"
        elif reasons:
            state.ship["handed_over"] = reasons
            state.status = HANDED_OVER
            print(f"[adw {state.adw_id}] run handed over to a person: {', '.join(reasons)}")
        else:
            state.ship.pop("handed_over", None)
            state.status = "passed"
        exit_code = 0
    except StepWaiting as exc:  # neither failed nor passed: no hand-off, the worktree stays for the resume
        state.status, exit_code = "waiting", EXIT_WAITING
        stack.keep_worktree = True
        state.error = f"waiting at step {exc.step} (exit {exc.code}); resume: {resume_command(state)}"
        print(f"[adw {state.adw_id}] run {state.error}")
    except (StepFailed, LayerError) as exc:
        state.status = "failed"
        print(f"[adw {state.adw_id}] run failed: {exc}")
        _stopped(layer, state, adw, stack, exc, args)
    except Exception as exc:  # last-resort boundary: record the crash in state instead of leaving it "running"
        state.status = "failed"
        print(f"[adw {state.adw_id}] run crashed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        _stopped(layer, state, adw, stack, exc, args)
    finally:
        # a waiting run keeps its worktree for --resume; a run on a host pushed its work, and the home
        # machine keeps it after it took the run over
        if state.status != "waiting" and not contained:
            from . import keep

            try:
                keep.keep_work(layer, state, adw, stack)
            except Exception as exc:  # keep.keep_work never raises by contract; this keeps the run result intact
                print(f"[adw {state.adw_id}] warning: keeping the work failed: {type(exc).__name__}: {exc}")
                stack.keep_worktree = True
            state.save(layer.layer_dir)
        try:
            stack.teardown(state.status in ("passed", HANDED_OVER, "delivered"))
        except (StepFailed, LayerError, OSError) as exc:
            print(f"[adw {state.adw_id}] teardown failed: {exc}")
            state.status, exit_code = "failed", 1
        try:
            checkenv.close_all(state)
        except (StepFailed, LayerError, OSError) as exc:
            print(f"[adw {state.adw_id}] check environments: cleanup failed: {exc}")
        if any(nested_engine.enabled(option) for option in sandbox.podman_options(layer)):
            try:
                nested_engine.remove_volume(layer, state)
            except (StepFailed, LayerError, OSError) as exc:
                print(f"[adw {state.adw_id}] nested engine: cleanup failed: {exc}")
        if request is not None and request.hooks is not None:
            try:
                request.hooks.on_finish(state)
            except Exception as exc:  # the source hook is foreign code; report it without masking the run result
                print(f"[adw {state.adw_id}] source on_finish failed: {type(exc).__name__}: {exc}")
                state.status, exit_code = "failed", 1
        beat.stop()
        state.ended = state_module.now()
        state.save(layer.layer_dir)
        if not contained and state.status != "waiting":
            try:  # after_run never raises by contract; this keeps the run result intact
                delivery_request.after_run(layer, state, adw)
            except Exception as exc:
                print(f"[adw {state.adw_id}] warning: the delivery request failed: {type(exc).__name__}: {exc}")
        if not contained and notify.report(layer, state, force=bool(getattr(args, "notify", False))):
            state.save(layer.layer_dir)
        events.emit("run.end", status=state.status, exit=exit_code)
    return exit_code


def main(adw: str, layer_dir: Path, argv: list[str] | None = None, definition: dict[str, Any] | None = None) -> int:
    """Entry point of adws/adw_<name>.py. Returns 0 when the run passed or was handed over (it ran to
    its end, but a rejected review or a gate fallback leaves the decision to a person), 1 on a failed
    run, 2 on usage errors, 3 when refused by a limit or when another live process holds the run
    (agents/<id>/run.lock), and 75 when a step is waiting.

    `definition` is an ADW the compiled layer does not declare (a single-command run, see
    launch.single_command_adw): it is added to the loaded IR for this run only and recorded in
    state.json, so the run goes through the same runner as every other one."""
    held: list[state_module.RunLock] = []
    try:
        return _main(adw, layer_dir, argv, definition, held)
    finally:   # on every return path, so a later resume in this process can take the run
        for lock in held:
            lock.release()


def _main(adw: str, layer_dir: Path, argv: list[str] | None, definition: dict[str, Any] | None,
          held: list[state_module.RunLock]) -> int:
    parser = _parser(adw)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse --help / usage error
        return int(exc.code or 0)
    try:
        layer = ir.load(Path(layer_dir))
        synthetic = definition
        if synthetic is not None:
            layer.ir["adws"] = [*layer.ir.get("adws", []), synthetic]
        definition = layer.validate_adw(adw)
        recompute = not args.resume or bool(args.model_set or args.model or args.max_cost or any(
            os.environ.get(k) for k in (models.ENV_TIER, models.ENV_MODELS, models.ENV_MAX_COST)))
        settings: dict[str, str] = {}
        if recompute:  # a plain resume keeps the models its run started with
            try:
                settings, notes = models.local_settings(layer)
            except LayerError as exc:
                raise UsageError(str(exc)) from exc
            for note in notes:
                print(f"adw_{adw}: note: {note}", file=sys.stderr)
        try:
            over = models.overrides(args.model_set, args.model, args.max_cost, settings=settings)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        try:
            models.check_overrides(layer, definition, over)
        except LayerError as exc:
            raise UsageError(str(exc)) from exc
        contained = selfrun.active()
        placed = placement.declared(layer) is not None and not contained
        if (args.on or args.follow or args.restart) and not placed:
            raise UsageError("--on, --follow and --restart need a layer that declares (placement auto #:hosts (…))")
        if args.follow or args.restart:
            if not args.adw_id or args.resume or (args.follow and args.restart):
                raise UsageError("--follow and --restart take --adw-id RUN (and not each other or --resume)")
            return _remote_again(layer, adw, definition, args)
        if args.replaces:
            _check_replaces(layer, args.replaces)
        if contained:
            request, state = _contained_start(layer, adw, definition, args)
        else:
            request = None if args.resume else resolve_request(layer, definition, args)
            state = _resumed_state(layer, adw, args) if args.resume else _new_state(adw, definition, args, request)
            if not args.resume and not state.replaces:   # a reopened task or issue started again after a failure
                state.replaces = previous_attempt(layer, state.source, state.adw_id)
        if synthetic is not None:
            state.definition = dict(synthetic)
        if recompute and not contained:
            state.overrides = over
            state.models = models.resolve_tiers(layer, str(definition.get("agent") or ""), over)
        if not contained:   # a run on another host keeps the limits its home machine decided
            state.budget["limits"] = budget.limits(layer, state.overrides)
            refused = budget.refuse_start(layer, state.budget["limits"], state.adw_id)
            if refused:
                print(f"adw_{adw}: refused: {refused}", file=sys.stderr)
                return budget.EXIT_REFUSED
        # one process owns a run while it lives; a second one refuses before anything is saved or built
        # (taken after the limits: a start they refuse leaves no run directory)
        try:
            held.append(state_module.hold_run(layer.layer_dir, state.adw_id))
        except state_module.RunHeld as exc:
            print(f"adw_{adw}: refused: run {state.adw_id} is held by process {exc.holder.get('pid')} "
                  f"on {exc.holder.get('host')}", file=sys.stderr)
            return budget.EXIT_REFUSED
        try:  # where the run delivers (a layer with stages); refused before any worktree exists
            target = delivery.for_run(layer, definition, state)
        except delivery.DeliveryError as exc:
            print(f"adw_{adw}: refused: delivery:{exc.reason}: {exc.message}", file=sys.stderr)
            state.status, state.ended = "failed", state_module.now()
            state.save(layer.layer_dir)
            return 1
        if target is not None:
            layer = delivery.scope(layer, target)
            state.delivery = {**state.delivery, **layer.delivery}
            state.delivery.pop("refused", None)
            state.delivery.pop("reason", None)
        state.save(layer.layer_dir)
        decision = None
        if placed and not args.resume:   # where the run executes; refused before anything is created
            try:
                decision = placement.decide(layer, definition, state, on=args.on)
            except placement.Refused as exc:
                print(f"adw_{adw}: refused: {exc}", file=sys.stderr)
                state.status, state.ended = "failed", state_module.now()
                state.save(layer.layer_dir)
                return budget.EXIT_REFUSED
            if decision["host"] != placement.HOME:
                from . import remoterun

                return remoterun.run_remote(layer, state, definition, args, request, decision)
    except UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"adw_{adw}: error: {exc}", file=sys.stderr)
        return 2
    except LayerError as exc:
        print(f"adw_{adw}: error: {exc}", file=sys.stderr)
        return 1
    log_path = state_module.run_dir(layer.layer_dir, state.adw_id) / "run.log"
    with log_path.open("a", encoding="utf-8") as log, events.recording(layer.layer_dir, state.adw_id):
        tee = _Tee(sys.stdout, log)
        if layer.delivery:
            events.emit("delivery.target", **layer.delivery)
        if decision is not None:
            placement.emit_decision(decision)
        try:
            with contextlib.redirect_stdout(tee), _stoppable(state):
                exit_code = _run(layer, state, definition, args, request)
                lines = summary_lines(layer, state)
                print("\n".join(lines), flush=True)  # out before integrate.finish() reports the run as ended
        finally:
            tee.close()
    held[0].release()   # the resume decide.after_run may spawn takes the run over
    if not selfrun.active():   # on a host the home machine does these, after it took the run over
        decide.after_run(layer, state)  # a resume the decider chose starts once this run has ended
        assign.after_run(layer, state, args)  # a handed-over run the decider may give to an agent (assign.py)
        _speak(layer, f"ADW {adw} {state.adw_id} {state.status}. Ship: {state.ship.get('action', 'none')}.")
    return exit_code


def _contained_start(layer: Any, adw: str, definition: dict[str, Any],
                     args: argparse.Namespace) -> tuple[Request, state_module.ADWState]:
    """The request and the state of a self-contained run: what the home machine resolved (class,
    tiers, limits, delivery target, dry run) comes in the seed; no prompt source is asked here."""
    seed = selfrun.seed()
    if seed.get("run") != args.adw_id or seed.get("adw") != adw:
        raise LayerError("the run's seed names another run or workflow")
    try:
        prompt = args.prompt_file.read_text(encoding="utf-8").strip() if args.prompt_file else str(seed.get("prompt") or "")
    except OSError as exc:
        raise UsageError(f"cannot read --prompt-file: {exc}") from exc
    fixed = str(seed.get("klass") or "") if seed.get("class_fixed") else None
    request = Request(prompt, str(seed.get("source") or "cli"), fixed, str(seed.get("model_set") or "") or None, None)
    args.klass, args.dry_run = fixed, bool(seed.get("dry_run"))
    state = selfrun.restored_state(layer, seed) or _new_state(adw, definition, args, request)
    if not state.class_from:
        state.class_from = str(seed.get("class_from") or "")
    state.dry_run = bool(seed.get("dry_run"))
    state.overrides = dict(seed.get("overrides") or {})
    state.models = dict(seed.get("models") or {})
    state.budget["limits"] = dict(seed.get("limits") or {})
    if seed.get("delivery"):
        state.delivery = dict(seed["delivery"])
    state.remote.setdefault("host", str(seed.get("host") or ""))
    state.remote["base"] = _base_commit(layer)
    return request, state


def _base_commit(layer: Any) -> str:
    result = subprocess.run(["git", "-C", str(layer.app_root), "rev-parse", "--verify", "--quiet",
                             f"refs/heads/{layer.default_branch}"], capture_output=True, text=True, timeout=120, check=False)
    return result.stdout.strip()


def _remote_again(layer: Any, adw: str, definition: dict[str, Any], args: argparse.Namespace) -> int:
    """--follow / --restart of a run that executes (or executed) on another host."""
    from . import remoterun

    state = state_module.load(layer.layer_dir, args.adw_id)
    if state.adw != adw:
        raise LayerError(f"run {args.adw_id} belongs to ADW {state.adw!r}, not {adw!r}")
    target = delivery.for_run(layer, definition, state)   # the target the run started with
    if target is not None:
        layer = delivery.scope(layer, target)
    work = remoterun.restart if args.restart else remoterun.reattach
    return work(layer, definition, args)
