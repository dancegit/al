from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import envs, ir, state as state_module, steps
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


class _Tee:
    """Writes progress to the terminal and to agents/<id>/run.log."""

    def __init__(self, terminal: TextIO, log: TextIO) -> None:
        self.terminal, self.log = terminal, log

    def write(self, value: str) -> int:
        self.log.write(value)
        self.log.flush()
        return self.terminal.write(value)

    def flush(self) -> None:
        self.log.flush()
        self.terminal.flush()


def _parser(adw: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"adw_{adw}", description=f"Run the {adw} ADW.")
    parser.add_argument("prompt", nargs="?", help="high-level prompt")
    parser.add_argument("--prompt-file", type=Path, help="read the prompt from a file")
    parser.add_argument("--source", help="KIND:REF prompt source, e.g. issue:42")
    parser.add_argument("--class", dest="klass", choices=CLASSES, help="fix the work class (skips classify routing)")
    parser.add_argument("--adw-id", help="8 hex chars; default random")
    parser.add_argument("--resume", action="store_true", help="continue --adw-id, skipping passed steps")
    parser.add_argument("--dry-run", action="store_true", help="use the mock agent regardless of the ADW agent")
    parser.add_argument("--keep-worktree", action="store_true", help="never remove the worktree")
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
        model_set=request.model_set or "",
    )


def _resumed_state(layer: Any, adw_name: str, args: argparse.Namespace) -> state_module.ADWState:
    if not args.adw_id:
        raise UsageError("--resume needs --adw-id")
    state = state_module.load(layer.layer_dir, args.adw_id)
    if state.adw != adw_name:
        raise LayerError(f"run {args.adw_id} belongs to ADW {state.adw!r}, not {adw_name!r}")
    state.status, state.ended = "running", ""
    state.dry_run = state.dry_run or args.dry_run
    if args.klass:
        state.klass, state.class_fixed = args.klass, True
    return state


def summary_lines(layer: Any, state: state_module.ADWState) -> list[str]:
    checks = ", ".join(f"{name}={data['exit']}" for name, data in state.checks.items()) or "none"
    verdict = state.review.get("verdict")
    review = "none" if not verdict else f"{'pass' if verdict.get('success') else 'fail'} ({verdict.get('mode')})"
    ship = state.ship.get("action", "none")
    if state.ship.get("action"):
        ship += f" -> {state.ship.get('target', '')} (shipped={state.ship.get('shipped')})"
    return [
        "=== ADW summary ===",
        f"adw:       {state.adw} {state.adw_id}",
        f"status:    {state.status}",
        f"branch:    {state.branch or 'none'}",
        f"plan:      {state.plan or 'none'}",
        f"checks:    {checks}",
        f"review:    {review}",
        f"ship:      {ship}",
        f"artefacts: {len(state.artefacts)}",
        f"state:     {state_module.run_dir(layer.layer_dir, state.adw_id) / 'state.json'}",
    ]


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


def _run(layer: Any, state: state_module.ADWState, adw: dict[str, Any], args: argparse.Namespace,
         request: Request | None) -> int:
    exit_code = 1
    stack = envs.build(layer, state, list(adw.get("environment", [])))
    stack.keep_worktree = bool(args.keep_worktree)
    print(f"[adw {state.adw_id}] {state.adw} start: class={state.klass} dry_run={state.dry_run}")
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
    except Exception as exc:  # last-resort boundary: record the crash in state instead of leaving it "running"
        state.status = "failed"
        print(f"[adw {state.adw_id}] run crashed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
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
        state.ended = state_module.now()
        state.save(layer.layer_dir)
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
        request = None if args.resume else resolve_request(layer, definition, args)
        state = _resumed_state(layer, adw, args) if args.resume else _new_state(adw, definition, args, request)
        state.save(layer.layer_dir)
    except UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"adw_{adw}: error: {exc}", file=sys.stderr)
        return 2
    except LayerError as exc:
        print(f"adw_{adw}: error: {exc}", file=sys.stderr)
        return 1
    log_path = state_module.run_dir(layer.layer_dir, state.adw_id) / "run.log"
    with log_path.open("a", encoding="utf-8") as log, contextlib.redirect_stdout(_Tee(sys.stdout, log)):
        exit_code = _run(layer, state, definition, args, request)
        lines = summary_lines(layer, state)
        print("\n".join(lines))
    _speak(layer, f"ADW {adw} {state.adw_id} {state.status}. Ship: {state.ship.get('action', 'none')}.")
    return exit_code
