from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import budget, envs, events, ir, models, notify, state as state_module, steps
from . import slots
from .agents import CLASSES
from .errors import LayerError, StepFailed


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
  then --model / AL_MODELS may give that tier another model id for this run.
  state.json (agent_calls) and the run's events record the tier, model and reason of every call.

notifications: a --dry-run sends none of the layer's (notify …) messages unless --notify is given.

exit codes: 0 passed, 1 failed (handed to a person where the workflow ships),
            2 usage error, 3 refused: the layer's day limit (#:day-usd) or its subscription usage threshold (#:limit-stop-percent) is reached
"""


def _parser(adw: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"adw_{adw}", description=f"Run the {adw} ADW.", epilog=OVERRIDES_HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="?", help="high-level prompt")
    parser.add_argument("--prompt-file", type=Path, help="read the prompt from a file")
    parser.add_argument("--source", help="KIND:REF prompt source, e.g. issue:42")
    parser.add_argument("--class", dest="klass", choices=CLASSES, help="fix the work class (skips classify routing)")
    parser.add_argument("--adw-id", help="8 hex chars; default random")
    parser.add_argument("--resume", action="store_true", help="continue --adw-id, skipping passed steps")
    parser.add_argument("--dry-run", action="store_true", help="use the mock agent regardless of the ADW agent")
    parser.add_argument("--keep-worktree", action="store_true", help="never remove the worktree")
    parser.add_argument("--model-set", metavar="TIER", help="every agent step of this run uses TIER (see below)")
    parser.add_argument("--model", action="append", default=[], metavar="TIER=MODEL_ID",
                        help="use MODEL_ID for tier TIER in this run (repeatable)")
    parser.add_argument("--max-cost", metavar="USD", help="run spending limit in US dollars for this run")
    parser.add_argument("--notify", action="store_true", help="send the layer's notifications even in a --dry-run")
    return parser


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


def _new_state(adw_name: str, adw: dict[str, Any], args: argparse.Namespace, request: Request) -> state_module.ADWState:
    fixed = args.klass or request.klass
    if fixed is not None and fixed not in CLASSES:
        raise LayerError(f"prompt source returned unknown class {fixed!r}")
    initial = fixed or (adw.get("classes") or ["chore"])[0]
    return state_module.ADWState(
        adw_id=args.adw_id or state_module.new_adw_id(), adw=adw_name, prompt=request.prompt,
        source=request.source, klass=initial, class_fixed=fixed is not None, dry_run=args.dry_run,
        model_set=request.model_set or "", fanout_parent=fanout_parent(),
    )


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
    return state


def summary_lines(layer: Any, state: state_module.ADWState) -> list[str]:
    checks = ", ".join(f"{name}={data['exit']}" for name, data in state.checks.items()) or "none"
    verdict = state.review.get("verdict")
    review = "none" if not verdict else f"{'pass' if verdict.get('success') else 'fail'} ({verdict.get('mode')})"
    ship = state.ship.get("action", "none")
    if state.ship.get("action"):
        ship += f" -> {state.ship.get('target', '')} (shipped={state.ship.get('shipped')})"
    handoff = _handoff_line(state)
    return [
        "=== ADW summary ===",
        f"adw:       {state.adw} {state.adw_id}",
        f"status:    {state.status}",
        f"branch:    {state.branch or 'none'}",
        f"plan:      {state.plan or 'none'}",
        f"checks:    {checks}",
        f"review:    {review}",
        f"cost:      {budget.summary(state)}",
        *([f"models:    {_models_line(state)}"] if state.agent_calls else []),
        f"ship:      {ship}",
        *([handoff] if handoff else []),
        f"artefacts: {len(state.artefacts)}",
        f"state:     {state_module.run_dir(layer.layer_dir, state.adw_id) / 'state.json'}",
    ]


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
        subprocess.Popen([sys.executable, str(script), line], cwd=layer.layer_dir, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        print(f"[adw] warning: spoken summary failed: {exc}", file=sys.stderr)


def _execute(layer: Any, state: state_module.ADWState, adw: dict[str, Any], stack: envs.EnvStack) -> None:
    stack.setup()
    state.save(layer.layer_dir)
    for step in adw.get("steps", []):
        steps.run_step(layer, state, adw, step, stack)


def _hand_off(layer: Any, state: state_module.ADWState, adw: dict[str, Any], stack: envs.EnvStack,
              failure: BaseException) -> None:
    """A stopped run hands its work to a human (see ship.handoff) while its worktree and
    container still exist, i.e. before teardown. The run stays failed whatever happens here."""
    from . import ship  # imported only when a run fails

    try:
        ship.handoff(layer, state, adw, stack, failure)
    except Exception as exc:  # ship.handoff never raises by contract; this keeps the failure intact if it does
        state.ship["handoff_error"] = f"{type(exc).__name__}: {exc}"[:2000]
        print(f"[adw {state.adw_id}] warning: handoff failed: {type(exc).__name__}: {exc}")
    if state.ship.get("action") == "handoff" and state.ship.get("target") and not state.ship.get("handoff_error"):
        # read by the task-list source's on_finish as the failed task's reason
        state.error = f"needs a human: {state.ship['target']} (stopped at {state.ship.get('stopped_at')})"
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


def _handoff_line(state: state_module.ADWState) -> str | None:
    ship = state.ship
    if ship.get("handoff_error"):
        return (f"handoff:   FAILED ({ship['handoff_error'][:160]}); the work stays on branch "
                f"{state.branch or 'none'}")
    if ship.get("action") == "handoff":
        return (f"handoff:   stopped at {ship.get('stopped_at')}; the work needs a human: {ship.get('target')} "
                f"(branch {ship.get('branch') or state.branch})")
    if ship.get("handoff_skipped"):
        return f"handoff:   none ({ship['handoff_skipped']})"
    return None


def _run(layer: Any, state: state_module.ADWState, adw: dict[str, Any], args: argparse.Namespace,
         request: Request | None) -> int:
    exit_code = 1
    stack = envs.build(layer, state, list(adw.get("environment", [])))
    stack.keep_worktree = bool(args.keep_worktree)
    beat = state_module.Heartbeat(state, layer.layer_dir).start()
    state.save(layer.layer_dir)
    events.say(f"[adw {state.adw_id}] {state.adw} start: class={state.klass} dry_run={state.dry_run}"
               + (f" overrides={_overrides_text(state.overrides)}" if _overrides_text(state.overrides) else ""), "run.start",
               adw=state.adw, adw_id=state.adw_id, klass=state.klass, dry_run=state.dry_run,
               resumed=bool(args.resume), pid=state.pid, host=state.host, fanout_parent=state.fanout_parent or None,
               overrides=state.overrides or None, limits=state.budget.get("limits") or None)
    try:
        if request is not None and request.hooks is not None:
            request.hooks.on_start(state)
        with slots.slot(layer, "runs", f"adw {state.adw_id}"):
            if stack.worktree_option:
                for removed in envs.collect_garbage(layer, stack.worktree_option):
                    print(f"[adw {state.adw_id}] gc: removed stale worktree {removed}")
            _execute(layer, state, adw, stack)
        state.status, exit_code = "passed", 0
    except (StepFailed, LayerError) as exc:
        state.status = "failed"
        print(f"[adw {state.adw_id}] run failed: {exc}")
        _hand_off(layer, state, adw, stack, exc)
        if notify.report(layer, state, force=bool(getattr(args, "notify", False)), at="handover"):
            state.save(layer.layer_dir)
    except Exception as exc:  # last-resort boundary: record the crash in state instead of leaving it "running"
        state.status = "failed"
        print(f"[adw {state.adw_id}] run crashed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        _hand_off(layer, state, adw, stack, exc)
        if notify.report(layer, state, force=bool(getattr(args, "notify", False)), at="handover"):
            state.save(layer.layer_dir)
    finally:
        try:
            stack.teardown(state.status == "passed")
        except (StepFailed, LayerError, OSError) as exc:
            print(f"[adw {state.adw_id}] teardown failed: {exc}")
            state.status, exit_code = "failed", 1
        if request is not None and request.hooks is not None:
            try:
                request.hooks.on_finish(state)
            except Exception as exc:  # the source hook is foreign code; report it without masking the run result
                print(f"[adw {state.adw_id}] source on_finish failed: {type(exc).__name__}: {exc}")
                state.status, exit_code = "failed", 1
        beat.stop()
        state.ended = state_module.now()
        state.save(layer.layer_dir)
        if notify.report(layer, state, force=bool(getattr(args, "notify", False))):
            state.save(layer.layer_dir)
        events.emit("run.end", status=state.status, exit=exit_code)
    return exit_code


def main(adw: str, layer_dir: Path, argv: list[str] | None = None) -> int:
    """Entry point of adws/adw_<name>.py. Returns 0 on success, 1 on a failed run, 2 on usage errors."""
    parser = _parser(adw)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse --help / usage error
        return int(exc.code or 0)
    try:
        layer = ir.load(Path(layer_dir))
        definition = layer.validate_adw(adw)
        try:
            over = models.overrides(args.model_set, args.model, args.max_cost)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        try:
            models.check_overrides(layer, definition, over)
        except LayerError as exc:
            raise UsageError(str(exc)) from exc
        request = None if args.resume else resolve_request(layer, definition, args)
        state = _resumed_state(layer, adw, args) if args.resume else _new_state(adw, definition, args, request)
        if not args.resume or args.model_set or args.model or args.max_cost or any(
                os.environ.get(k) for k in (models.ENV_TIER, models.ENV_MODELS, models.ENV_MAX_COST)):
            state.overrides = over
        state.budget["limits"] = budget.limits(layer, state.overrides)
        refused = budget.refuse_start(layer, state.budget["limits"], state.adw_id)
        if refused:
            print(f"adw_{adw}: refused: {refused}", file=sys.stderr)
            return budget.EXIT_REFUSED
        state.save(layer.layer_dir)
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
        try:
            with contextlib.redirect_stdout(tee):
                exit_code = _run(layer, state, definition, args, request)
                lines = summary_lines(layer, state)
                print("\n".join(lines))
        finally:
            tee.close()
    _speak(layer, f"ADW {adw} {state.adw_id} {state.status}. Ship: {state.ship.get('action', 'none')}.")
    return exit_code
