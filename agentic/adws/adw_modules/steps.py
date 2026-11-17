from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import artefacts
from . import discipline
from . import checkenv
from . import events
from . import journal as journal_module
from . import models
from . import skips
from . import slots
from .agents import run_agent
from .budget import BudgetExceeded
from .errors import LayerError, StepFailed, StepWaiting
from .templates import render_step_arg
from .usage import LimitReached

REJECTED = "rejected"


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
              attempt_offset: int = 0, timeout: int | None = None) -> int:
    """Run a check step's cmd once; `timeout` overrides the cmd's own time limit."""
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
    stack = checkenv.stack_for(layer, state, step, env_stack)
    env_id = checkenv.target(layer.adw(state.adw), step)
    with slots.slot(layer, "checks", f"adw {state.adw_id} check {name}"):
        argv = stack.wrap(cmd)
        events.emit("check.start", **span)
        started = time.monotonic()
        started_wall = time.time()
        exit_code, _output = run_command(argv, stack.cwd, stack.process_env(),
                                          int(timeout or command.get("timeout", 1800)), log_path=log_path)
        exit_code, report, problems = _skip_rules(layer, state, step, log_path, started_wall, exit_code)
        events.emit("check.end", **span, exit=exit_code, seconds=round(time.monotonic() - started, 3),
                    **_skip_fields(report, env_id))
    record: dict[str, Any] = {"cmd": cmd, "exit": exit_code, "log": relative_log}
    if report is not None:
        record["skips"] = report.as_dict()
    if problems:
        record["skip_rule"] = problems
    if env_id:
        record["in"] = env_id
    state.checks[name] = record
    state.save(layer.layer_dir)
    if report is not None:
        artefacts.write_manifest(layer, state)  # reviewers see the skips without a #:collect
    return exit_code


def _skip_rules(layer: Any, state: Any, step: dict[str, Any], log_path: Path, started_wall: float,
                exit_code: int) -> tuple[int, Any, list[str]]:
    """Count the attempt's skipped tests and apply the check's skip rules: a broken rule appends its
    `al: …` lines to the log (the resolve agent, the gate and a person all read why) and turns
    exit 0 into exit 1."""
    report = skips.account(layer, state, step, log_path, started_wall)
    problems = skips.violations(step, report)
    if problems:
        with Path(log_path).open("a", encoding="utf-8") as log:
            log.write("\n" + "\n".join(problems) + "\n")
        if exit_code == 0:
            exit_code = 1
    return exit_code, report, problems


def _skip_fields(report: Any, env_id: str | None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if report is not None:
        record = report.as_dict()
        fields.update(skipped=record["skipped"], skip_reasons=record["reasons"], skip_source=record["source"])
    if env_id:
        fields["in"] = env_id
    return fields


def running_step(state: Any) -> str:
    """The ADW step running now ("" before the first): it owns every agent call and check."""
    return next((str(r.get("name")) for r in reversed(state.steps) if r.get("status") == "running"), "")


def run_logged(layer: Any, state: Any, env_stack: Any, command: dict[str, Any], log_path: Path,
               step: str, phase: str) -> int:
    """A codebase command outside a check step (the gate's re-runs, the deploy command), with its
    log streamed to log_path and a check.start / check.end pair."""
    span = {"step": step, "owner": running_step(state), "phase": phase, "cmd": str(command["name"]),
            "attempt": 1, "log": Path(log_path).relative_to(layer.layer_dir).as_posix()}
    # the gate re-runs a cmd where its check step runs it (#:in) and under the same skip rules
    check = checkenv.check_for_cmd(layer, state, str(command["name"])) if phase == "gate" else None
    stack = checkenv.stack_for(layer, state, check, env_stack) if check is not None else env_stack
    env_id = checkenv.target(layer.adw(state.adw), check) if check is not None else None
    argv = stack.wrap(str(command["run"]))
    events.emit("check.start", **span)
    started = time.monotonic()
    started_wall = time.time()
    code, _output = run_command(argv, stack.cwd, stack.process_env(), int(command.get("timeout", 1800)),
                                log_path=log_path)
    fields: dict[str, Any] = {}
    if check is not None:
        code, report, _problems = _skip_rules(layer, state, check, log_path, started_wall, code)
        fields = _skip_fields(report, env_id)
    events.emit("check.end", **span, exit=code, seconds=round(time.monotonic() - started, 3), **fields)
    return code


def step_tier(layer: Any, state: Any, step: dict[str, Any], command: dict[str, Any]) -> tuple[str, str]:
    """(tier, why) a run step asks for: its own #:model-set, else the request's tier (a task tag or
    issue keyword) when the step's agent declares it, else its command's. A run-wide override
    (--model-set) is applied on top of this by models.choose."""
    own = str(step.get("modelSet") or "inherit")
    if own != "inherit":
        return own, "step"
    requested = str(state.model_set or "")
    if requested:
        agent = layer.option("agents", str(step.get("agent") or "") or layer.adw(state.adw)["agent"])
        if models.has_tier(agent, requested):
            return requested, "request"
        print(f"[adw {state.adw_id}] step {step.get('name')}: the request asks for tier {requested}, which agent "
              f"{agent.get('id')} does not declare; using its command's tier")
    return str(command.get("modelSet") or "base"), "command"


def run_run(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> None:
    name = str(step["name"])
    command_name = str(step["command"])
    command = layer.command(command_name)
    model_set, tier_from = step_tier(layer, state, step, command)
    args = [render_step_arg(str(value), step_variables(state), state.outputs) for value in step.get("args", [])]
    retries = max(0, int(step.get("retries", 0)))
    for attempt in range(retries + 1):
        try:
            output = run_agent(layer, state, name, command_name, args, model_set, env_stack,
                               str(step.get("agent") or ""), tier_from)
            break
        except (BudgetExceeded, LimitReached):
            raise  # a spending or usage limit is never retried
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
        if exit_code == 124:
            exit_code = _retry_time_limit(layer, state, step, env_stack)
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


def _retry_time_limit(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> int:
    """A check that hit its time limit runs once more with twice the limit, without the resolve
    agent: a slow (shared) machine is not broken code. Timing out again fails the step."""
    name = str(step["name"])
    limit = int(layer.cmd(step["cmd"]).get("timeout", 1800))
    first_log = state.checks[name]["log"]
    print(f"[adw {state.adw_id}] check {name} timed out after {limit}s; running it once more with {2 * limit}s "
          "(a time limit is not a test failure; no resolve agent)")
    entry: dict[str, Any] = {"limit": limit, "retry_limit": 2 * limit, "log": first_log}
    state.step_record(name, "check").setdefault("time_limit_retries", []).append(entry)
    state.save(layer.layer_dir)
    exit_code = run_check(layer, state, step, env_stack, timeout=2 * limit)
    entry["exit"] = exit_code
    state.save(layer.layer_dir)
    if exit_code == 124:
        artefacts.collect(layer, state, list(step.get("collect", [])), name)
        log = state.checks[name]["log"]
        line = _last_progress_line(Path(layer.layer_dir) / log)
        raise StepFailed(name, f"check {step['cmd']} timed out twice ({limit} s, then {2 * limit} s); "
                               f"last progress: {line or '(none)'}; log {log}")
    return exit_code


def _last_progress_line(log_path: Path) -> str:
    """The last non-empty line of a check's log that is not the runtime's own timeout note."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        text = line.strip()
        if text and not text.startswith("command timed out after"):
            return text[:300]
    return ""


SCRIPT_TAIL = 4000  # characters of a script's output kept as the step's output ($out.NAME)


def run_script(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> None:
    """(script NAME #:cmd C #:args (…)): run a declared codebase cmd with substituted, shell-quoted
    arguments in the run's environment, with the run context in ADW_* variables. Its output is the
    step's output; a non-zero exit fails the run, unless it is one of the step's #:waiting-exit
    codes (StepWaiting: the run ends `waiting`). With #:journal the script's own run journal is
    followed while it runs (see journal.Follower). Not a check: no resolve loop, not gate evidence."""
    name = str(step["name"])
    if env_stack.read_only:
        raise StepFailed(name, "script refused in read-only host environment")
    command = layer.cmd(step["cmd"])
    args = [render_step_arg(str(value), step_variables(state), state.outputs) for value in step.get("args", [])]
    resuming = bool((state.scripts.get(name) or {}).get("waiting"))
    context = {"ADW_ID": state.adw_id, "ADW_PROMPT": state.prompt, "ADW_CLASS": state.klass,
               "ADW_PLAN": state.plan, "ADW_BRANCH": state.branch, "ADW_WORKTREE": str(env_stack.cwd),
               "ADW_LAYER_DIR": str(layer.layer_dir), "ADW_STEP": name, "ADW_RESUME": "1" if resuming else ""}
    # exported in the shell line itself, so they reach the command in a container too
    exports = " ".join(f"{key}={shlex.quote(str(value))}" for key, value in context.items())
    line = f"export {exports}; {command['run']}" + "".join(f" {shlex.quote(arg)}" for arg in args)
    log_dir = Path(layer.layer_dir) / "agents" / state.adw_id / "scripts"
    log_path = log_dir / f"{name}.{_log_number(log_dir, name) if log_dir.is_dir() else 1}.log"
    relative = log_path.relative_to(layer.layer_dir).as_posix()
    timeout = int(step.get("timeout") or 0) or int(command.get("timeout", 1800))
    waiting = [int(code) for code in step.get("waitingExit") or []]
    span = {"step": name, "owner": running_step(state), "cmd": str(step["cmd"]), "log": relative}
    follower = None
    if step.get("journal"):
        follower = journal_module.Follower(
            layer.layer_dir, worktree=env_stack.cwd, relative=str(step["journal"]).replace("{adw_id}", state.adw_id),
            step=name, dest=log_dir / name / "journal")
    with slots.slot(layer, "checks", f"adw {state.adw_id} script {name}"):
        argv = env_stack.wrap(line)
        events.emit("script.start", **span)
        started = time.monotonic()
        if follower is not None:
            follower.start()
        try:
            code, output = run_command(argv, env_stack.cwd, env_stack.process_env(), timeout, log_path=log_path)
        finally:
            summary = follower.stop() if follower is not None else None
        events.emit("script.end", **span, exit=code, seconds=round(time.monotonic() - started, 3))
    record: dict[str, Any] = {"cmd": str(command["run"]), "args": args, "exit": code, "log": relative}
    if waiting:
        record["waiting"] = code in waiting
    if summary is not None:
        record["journal"] = {key: value for key, value in summary.items() if key != "documents"}
        record["documents"] = summary["documents"]
    state.scripts[name] = record
    state.outputs[name] = output[-SCRIPT_TAIL:]
    state.save(layer.layer_dir)
    if code in waiting:
        raise StepWaiting(name, code, f"script {step['cmd']} exited {code} (declared as waiting); log {relative}")
    if code != 0:
        tail = "\n".join(output.rstrip().splitlines()[-5:])
        raise StepFailed(name, f"script {step['cmd']} exited {code}; log {relative}" + (f"\n{tail}" if tail else ""))


def _execute_step(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], env_stack: Any) -> None:
    kind = step.get("kind")
    if kind == "run":
        with discipline.watched(layer, state, step, env_stack):
            run_run(layer, state, step, env_stack)
    elif kind == "script":
        run_script(layer, state, step, env_stack)
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
        if getattr(layer, "delivery", None):  # the run delivers into a stage (see delivery.py)
            from . import delivery

            delivery.after_ship(layer, state)
    elif kind == "fan-out":
        from . import fanout

        fanout.run_fan_out(layer, state, step, env_stack)
    elif kind == "promote":  # a step of a promotion run (promotion.promotion_adw)
        from . import promotion

        promotion.run_step(layer, state, adw, step, env_stack)
    else:
        raise LayerError(f"Unknown ADW step kind: {kind!r}")


DELIVER_STEP = "deliver"


def run_step(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], env_stack: Any) -> None:
    name, kind = str(step.get("name", "")), str(step.get("kind", ""))
    record = state.step_record(name, kind)
    # a rejected review is finished: re-running it on --resume would repeat its agent calls and commits
    if record.get("status") in ("passed", REJECTED):
        return
    when = str(step.get("when", "always"))
    if when.startswith("class-") and state.klass != when.removeprefix("class-"):
        stamp = _timestamp()
        record.update(status="skipped", started=stamp, ended=stamp, detail=f"when={when}, class={state.klass}")
        state.save(layer.layer_dir)
        events.say(f"[adw {state.adw_id}] step {name} ({kind}) skipped: {record['detail']}", "step.end",
                   step=name, kind=kind, status="skipped", detail=record["detail"], seconds=0.0)
        return
    if kind == "ship" and getattr(layer, "delivery", None):
        # The delivery checks (delivery.before_ship) are a step of their own, recorded as `deliver`
        # before the ship step, only for a ship step that runs (a ship step skipped by `#:when` has none):
        # a run they stop is handed to a person like any other stopped run.
        from . import delivery

        _run_recorded(layer, state, DELIVER_STEP, DELIVER_STEP,
                      lambda: delivery.before_ship(layer, state, adw, step, env_stack))
    _run_recorded(layer, state, name, kind, lambda: _execute_step(layer, state, adw, step, env_stack), step)


def _run_recorded(layer: Any, state: Any, name: str, kind: str, work: Any, step: dict[str, Any] | None = None) -> None:
    """Run `work` as step `name`: its record in state.json, its step.start/step.end events."""
    record = state.step_record(name, kind)
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
        work()
    except StepWaiting as exc:
        ended("waiting", str(exc), f"[adw {state.adw_id}] step {name} ({kind}) waiting: {exc.reason}")
        raise
    except (StepFailed, LayerError) as exc:
        ended("failed", str(exc), f"[adw {state.adw_id}] step {name} ({kind}) failed: {exc}")
        if isinstance(exc, StepFailed):
            raise
        raise StepFailed(name, str(exc)) from exc
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        ended("failed", detail, f"[adw {state.adw_id}] step {name} ({kind}) failed: {detail}")
        raise StepFailed(name, detail) from exc
    if kind == "review":
        success, detail = review_outcome(state)
        if success is False:
            # the review returned so a later ship step can route the work to a person: not a pass
            ended(REJECTED, detail, f"[adw {state.adw_id}] step {name} ({kind}) rejected: {detail}")
            return
    detail = _pass_detail(state, step) if step is not None else "ok"
    ended("passed", detail, f"[adw {state.adw_id}] step {name} ({kind}) passed: {detail}")


def review_outcome(state: Any) -> tuple[bool | None, str]:
    """(success, detail) of the agent or auto review recorded in `state.review`; success is None
    for a human review or when no review ran."""
    review = getattr(state, "review", None) or {}
    rounds = review.get("rounds") or []
    if not rounds:
        return None, ""
    verdict = review.get("verdict") or {}
    success = bool(verdict.get("success"))
    summary = str(verdict.get("summary", ""))
    return success, f"{'pass' if success else 'fail'} after {len(rounds)} round(s): {summary[:200]}"


def _pass_detail(state: Any, step: dict[str, Any]) -> str:
    name = str(step.get("name"))
    if step.get("kind") == "review":
        success, detail = review_outcome(state)
        if success is not None:
            return detail
    if step.get("kind") == "check" and name in state.checks:
        detail = f"exit {state.checks[name]['exit']}, log {state.checks[name]['log']}"
        record = next((r for r in state.steps if r.get("name") == name), {})
        retried = [r for r in record.get("time_limit_retries") or [] if r.get("exit") == 0]
        if retried:
            detail += (f" (time limit {retried[-1]['limit']} s was too low: passed on the retry with "
                       f"{retried[-1]['retry_limit']} s)")
        return detail
    if step.get("kind") == "script" and name in state.scripts:
        return f"exit {state.scripts[name]['exit']}, log {state.scripts[name]['log']}"
    output = state.outputs.get(name, "")
    first_line = output.strip().splitlines()[0] if output.strip() else "ok"
    return first_line[:160]
