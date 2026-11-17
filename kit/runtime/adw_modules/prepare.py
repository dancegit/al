"""The run's prepare step: dependency installs and the like, once per run, before the first step.

`(environment worktree #:prepare (install …))` names codebase cmds; with a podman #:devcontainer and
no #:prepare, the dev container's postCreateCommand takes their place (see envsource). They run in
the run's environment exactly like a check (stack.wrap: the container and its caches, or the Nix
shell), each holding a `checks` slot, with the run context in ADW_* variables. The step is recorded
as `prepare` of kind `setup` (state.steps, state.scripts["prepare"], step.start / script.start …
events), so the dashboard shows it first. A failure ends the run at once with the command's output;
a resumed run whose prepare passed does not prepare again.
"""
from __future__ import annotations

import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import envsource, events, slots, steps
from .errors import LayerError, StepFailed

NAME, KIND = "prepare", "setup"
DEFAULT_TIMEOUT = 900


def _timeout(layer: Any, label: str) -> int:
    try:
        return int(layer.cmd(label).get("timeout", DEFAULT_TIMEOUT))
    except LayerError:
        return DEFAULT_TIMEOUT  # postCreateCommand is not a declared cmd


def _line(state: Any, stack: Any, layer: Any, command: str) -> str:
    context = {"ADW_ID": state.adw_id, "ADW_PROMPT": state.prompt, "ADW_CLASS": state.klass, "ADW_PLAN": state.plan,
               "ADW_BRANCH": state.branch, "ADW_WORKTREE": str(stack.cwd), "ADW_LAYER_DIR": str(layer.layer_dir),
               "ADW_STEP": NAME, "ADW_RESUME": ""}
    exports = " ".join(f"{key}={shlex.quote(str(value))}" for key, value in context.items())
    return f"export {exports}; {command}"


def run(layer: Any, state: Any, adw: dict[str, Any], stack: Any) -> None:
    """Run the prepare commands of this run, unless there are none or they already passed."""
    commands = envsource.prepare_commands(layer, stack)
    if not commands:
        return
    record = state.step_record(NAME, KIND)
    if record.get("status") == "passed":
        return
    attempt = int(record.get("attempts", 0)) + 1
    events.say(f"[adw {state.adw_id}] step {NAME} ({KIND}) start: {', '.join(label for label, _ in commands)}",
               "step.start", step=NAME, kind=KIND, attempt=attempt)
    started = time.monotonic()
    record.update(status="running", attempts=attempt, started=datetime.now(timezone.utc).isoformat(), ended="", detail="")
    state.save(layer.layer_dir)

    def ended(status: str, detail: str) -> None:
        record.update(status=status, ended=datetime.now(timezone.utc).isoformat(), detail=detail)
        state.save(layer.layer_dir)
        events.say(f"[adw {state.adw_id}] step {NAME} ({KIND}) {status}: {detail}", "step.end", step=NAME, kind=KIND,
                   status=status, detail=detail[:2000], seconds=round(time.monotonic() - started, 3))

    log_dir = Path(layer.layer_dir) / "agents" / state.adw_id / "scripts"
    done: list[dict[str, Any]] = []
    try:
        for number, (label, command) in enumerate(commands, 1):
            log_path = log_dir / f"{NAME}.{number}.log"
            relative = log_path.relative_to(layer.layer_dir).as_posix()
            span = {"step": NAME, "owner": NAME, "cmd": label, "log": relative}
            with slots.slot(layer, "checks", f"adw {state.adw_id} prepare {label}"):
                argv = stack.wrap(_line(state, stack, layer, command))
                events.emit("script.start", **span)
                begun = time.monotonic()
                code, output = steps.run_command(argv, stack.cwd, stack.process_env(), _timeout(layer, label),
                                                 log_path=log_path)
                events.emit("script.end", **span, exit=code, seconds=round(time.monotonic() - begun, 3))
            done.append({"cmd": label, "run": command, "exit": code, "log": relative})
            state.scripts[NAME] = {"cmd": label, "exit": code, "log": relative, "commands": list(done)}
            state.save(layer.layer_dir)
            if code != 0:
                tail = "\n".join(output.rstrip().splitlines()[-5:])
                raise StepFailed(NAME, f"prepare {label} exited {code}; log {relative}" + (f"\n{tail}" if tail else ""))
    except (StepFailed, LayerError) as exc:
        ended("failed", str(exc))
        if isinstance(exc, StepFailed):
            raise
        raise StepFailed(NAME, str(exc)) from exc
    ended("passed", f"{len(done)} command(s): {', '.join(item['cmd'] for item in done)}")


def run_for_verify(layer: Any, stack: Any, out_dir: Path) -> list[dict[str, Any]]:
    """`al verify`: the same commands before the cmds it verifies; one report row each
    (`prepare:<label>`). The first failing command stops here (its row is the last)."""
    rows: list[dict[str, Any]] = []
    for label, command in envsource.prepare_commands(layer, stack):
        log_path = Path(out_dir) / f"prepare-{label}.log"
        started = time.monotonic()
        try:
            argv = stack.wrap(command)
            code, _output = steps.run_command(argv, stack.cwd, stack.process_env(), _timeout(layer, label),
                                              log_path=log_path)
        except (StepFailed, LayerError) as exc:
            code = 125
            log_path.write_text(f"environment failed before the command ran: {exc}\n", encoding="utf-8")
        rows.append({"name": f"{NAME}:{label}", "cmd": command, "exit": code,
                     "seconds": round(time.monotonic() - started, 1),
                     "log": log_path.relative_to(layer.layer_dir).as_posix(),
                     "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        if code != 0:
            break
    return rows
