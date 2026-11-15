from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import artefacts
from . import events
from . import slots
from .agents import run_agent
from .errors import LayerError, StepFailed
from .templates import render_step_arg


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def step_variables(state: Any) -> dict[str, str]:
    """Variables for IR step args ($prompt, $plan, $adw_id, $class, $branch)."""
    return {"prompt": state.prompt, "plan": state.plan, "adw_id": state.adw_id,
            "class": state.klass, "branch": state.branch}


def _log_number(check_dir: Path, name: str) -> int:
    prefix = f"{name}."
    numbers = []
    for path in check_dir.glob(f"{name}.*.log"):
        try:
            numbers.append(int(path.name[len(prefix):-4]))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # the group exited between the timeout and the kill; nothing left to stop


def _as_text(data: bytes) -> str:
    """Decode command output the way text-mode pipes did: UTF-8, universal newlines."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def run_command(argv: list[str], cwd: Path, env: dict[str, str], timeout: int,
                log_path: Path | None = None) -> tuple[int, str]:
    """Run argv in its own process group; returns (exit, combined output). Timeout kills the group, exit 124.

    With `log_path` the output goes straight to that file while the command runs, so it can be
    followed live (as fast as the command itself flushes); when the command ends the file is
    rewritten once with the same normalised text that is returned, exactly as before."""
    if log_path is None:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except OSError as exc:
            return 127, f"could not start {argv[0]!r}: {exc}\n"
        try:
            output, _ = process.communicate(timeout=max(1, timeout))
            return int(process.returncode or 0), output
        except subprocess.TimeoutExpired:
            _kill_group(process)
            output, _ = process.communicate(timeout=30)
            return 124, output + f"\ncommand timed out after {timeout}s\n"
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb", buffering=0) as log:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        except OSError as exc:
            output = f"could not start {argv[0]!r}: {exc}\n"
            log.write(output.encode("utf-8"))
            return 127, output
        try:
            code = int(process.wait(timeout=max(1, timeout)) or 0)
            note = ""
        except subprocess.TimeoutExpired:
            _kill_group(process)
            process.wait(timeout=30)
            code, note = 124, f"\ncommand timed out after {timeout}s\n"
    output = _as_text(log_path.read_bytes()) + note
    if log_path.read_bytes() != output.encode("utf-8"):
        log_path.write_text(output, encoding="utf-8")
    return code, output


def run_check(layer: Any, state: Any, step: dict[str, Any], env_stack: Any,
              attempt_offset: int = 0) -> int:
    name = str(step["name"])
    if env_stack.read_only:
        raise StepFailed(name, "check refused in read-only host environment")
    command = layer.cmd(step["cmd"])
    cmd = str(command["run"])
    check_dir = Path(layer.layer_dir) / "agents" / state.adw_id / "checks"
    check_dir.mkdir(parents=True, exist_ok=True)
    attempt = max(_log_number(check_dir, name), attempt_offset + 1)
    log_path = check_dir / f"{name}.{attempt}.log"
    relative_log = log_path.relative_to(layer.layer_dir).as_posix()
    owner = running_step(state)
    span = {"step": name, "owner": owner, "phase": "check" if owner in ("", name) else "recheck",
            "cmd": str(step["cmd"]), "attempt": attempt, "log": relative_log}
    with slots.slot(layer, "checks", f"adw {state.adw_id} check {name}"):
        argv = env_stack.wrap(cmd)
        events.emit("check.start", **span)
        started = time.monotonic()
        exit_code, _output = run_command(argv, env_stack.cwd, env_stack.process_env(),
                                          int(command.get("timeout", 900)), log_path=log_path)
        events.emit("check.end", **span, exit=exit_code, seconds=round(time.monotonic() - started, 3))
    state.checks[name] = {"cmd": cmd, "exit": exit_code, "log": relative_log}
    state.save(layer.layer_dir)
    return exit_code


def running_step(state: Any) -> str:
    """The ADW step running now ("" before the first): it owns every agent call and check."""
    return next((str(r.get("name")) for r in reversed(state.steps) if r.get("status") == "running"), "")


def run_logged(layer: Any, state: Any, env_stack: Any, command: dict[str, Any], log_path: Path,
               step: str, phase: str) -> int:
    """A codebase command outside a check step (the gate's re-runs, the deploy command), with its
    log streamed to log_path and a check.start / check.end pair."""
    span = {"step": step, "owner": running_step(state), "phase": phase, "cmd": str(command["name"]),
            "attempt": 1, "log": Path(log_path).relative_to(layer.layer_dir).as_posix()}
    argv = env_stack.wrap(str(command["run"]))
    events.emit("check.start", **span)
    started = time.monotonic()
    code, _output = run_command(argv, env_stack.cwd, env_stack.process_env(), int(command.get("timeout", 900)),
                                log_path=log_path)
    events.emit("check.end", **span, exit=code, seconds=round(time.monotonic() - started, 3))
    return code


def run_run(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> None:
    name = str(step["name"])
    command_name = str(step["command"])
    command = layer.command(command_name)
    model_set = str(step.get("modelSet", "inherit"))
    if model_set == "inherit":
        # A request-level model set (e.g. a tasklist `{heavy}` tag) beats the command's default.
        model_set = state.model_set or str(command.get("modelSet", "base"))
    if model_set not in {"base", "heavy"}:
        raise LayerError(f"Unknown model set {model_set!r} for step {name}")
    args = [render_step_arg(str(value), step_variables(state), state.outputs) for value in step.get("args", [])]
    retries = max(0, int(step.get("retries", 0)))
    for attempt in range(retries + 1):
        try:
            output = run_agent(layer, state, name, command_name, args, model_set, env_stack,
                               str(step.get("agent") or ""))
            break
        except StepFailed as exc:
            if attempt == retries:
                raise
            print(f"[adw {state.adw_id}] step {name} attempt {attempt + 1} failed, retrying: {exc.reason}")
    state.outputs[name] = output
    if command.get("template") == "classify" and not state.class_fixed:
        state.klass = str(json.loads(output)["class"])
    if command.get("output") == "path":
        state.plan = output


def run_check_with_resolve(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> None:
    """Run a check step's cmd; on failure let the resolve command fix it and re-run, up to `retries`."""
    name = str(step["name"])
    retries = max(0, int(step.get("retries", 0)))
    for attempt in range(retries + 1):
        exit_code = run_check(layer, state, step, env_stack)
        final = exit_code == 0 or not step.get("resolve") or attempt == retries
        if final:
            # Collected on failure too: screenshots of a failing run are the proof a human needs.
            artefacts.collect(layer, state, list(step.get("collect", [])), name)
            if exit_code == 0:
                return
            raise StepFailed(name, f"check {step['cmd']} exited {exit_code} after {attempt + 1} attempt(s); "
                                   f"log {state.checks[name]['log']}")
        log_path = Path(layer.layer_dir) / state.checks[name]["log"]
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-150:]
        resolve_args = [str(layer.cmd(step["cmd"])["run"]), "\n".join(lines)]
        resolve_command = layer.command(step["resolve"])
        model_set = str(resolve_command.get("modelSet", "base"))
        output = run_agent(layer, state, f"{name}-resolve-{attempt + 1}", step["resolve"], resolve_args,
                           model_set, env_stack)
        state.outputs[f"{name}-resolve-{attempt + 1}"] = output


def _execute_step(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], env_stack: Any) -> None:
    kind = step.get("kind")
    if kind == "run":
        run_run(layer, state, step, env_stack)
    elif kind == "check":
        if env_stack.read_only:
            raise StepFailed(str(step["name"]), "check refused in read-only host environment")
        run_check_with_resolve(layer, state, step, env_stack)
    elif kind == "review":
        from . import review

        review.run_review(layer, state, step, env_stack)
    elif kind == "ship":
        if env_stack.read_only:
            raise StepFailed(str(step["name"]), "ship refused in read-only host environment")
        from . import ship

        ship.ship(layer, state, adw, step, env_stack)
    elif kind == "fan-out":
        from . import fanout

        fanout.run_fan_out(layer, state, step, env_stack)
    else:
        raise LayerError(f"Unknown ADW step kind: {kind!r}")


def run_step(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], env_stack: Any) -> None:
    name, kind = str(step.get("name", "")), str(step.get("kind", ""))
    record = state.step_record(name, kind)
    if record.get("status") == "passed":
        return
    when = str(step.get("when", "always"))
    if when.startswith("class-") and state.klass != when.removeprefix("class-"):
        stamp = _timestamp()
        record.update(status="skipped", started=stamp, ended=stamp, detail=f"when={when}, class={state.klass}")
        state.save(layer.layer_dir)
        events.say(f"[adw {state.adw_id}] step {name} ({kind}) skipped: {record['detail']}", "step.end",
                   step=name, kind=kind, status="skipped", detail=record["detail"], seconds=0.0)
        return
    attempts = int(record.get("attempts", 0)) + 1
    events.say(f"[adw {state.adw_id}] step {name} ({kind}) start", "step.start", step=name, kind=kind, attempt=attempts)
    started = time.monotonic()
    record.update(status="running", attempts=attempts, started=_timestamp(), ended="", detail="")
    state.save(layer.layer_dir)

    def ended(status: str, detail: str, line: str) -> None:
        record.update(status=status, ended=_timestamp(), detail=detail)
        state.save(layer.layer_dir)
        events.say(line, "step.end", step=name, kind=kind, status=status, detail=detail[:2000],
                   seconds=round(time.monotonic() - started, 3))

    try:
        _execute_step(layer, state, adw, step, env_stack)
    except (StepFailed, LayerError) as exc:
        ended("failed", str(exc), f"[adw {state.adw_id}] step {name} ({kind}) failed: {exc}")
        if isinstance(exc, StepFailed):
            raise
        raise StepFailed(name, str(exc)) from exc
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        ended("failed", detail, f"[adw {state.adw_id}] step {name} ({kind}) failed: {detail}")
        raise StepFailed(name, detail) from exc
    detail = _pass_detail(state, step)
    ended("passed", detail, f"[adw {state.adw_id}] step {name} ({kind}) passed: {detail}")


def _pass_detail(state: Any, step: dict[str, Any]) -> str:
    name = str(step.get("name"))
    if step.get("kind") == "check" and name in state.checks:
        return f"exit {state.checks[name]['exit']}, log {state.checks[name]['log']}"
    output = state.outputs.get(name, "")
    first_line = output.strip().splitlines()[0] if output.strip() else "ok"
    return first_line[:160]
