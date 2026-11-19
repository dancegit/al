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

from . import affected
from . import artefacts
from . import discipline
from . import checkenv
from . import coverage_map
from . import delivery
from . import events
from . import journal as journal_module
from . import models
from . import skips
from . import slots
from . import testing
from .agents import run_agent
from .budget import BudgetExceeded
from .errors import LayerError, RunStopped, StepFailed, StepWaiting
from .templates import render_step_arg
from .usage import LimitReached

REJECTED = "rejected"
NOT_AFFECTED = "not-affected"  # a check step that was not run: no changed path is under what its cmd covers


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


def _reap(process: subprocess.Popen) -> None:
    """Collect a killed child without letting a second signal or a slow exit mask the first error."""
    try:
        process.wait(timeout=10)
    except BaseException:  # noqa: BLE001 — best effort; the caller re-raises what stopped it
        pass


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
        except BaseException:  # the run was stopped (RunStopped) or interrupted: never leave the command running
            _kill_group(process)
            _reap(process)
            raise
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
        except BaseException:
            _kill_group(process)
            _reap(process)
            raise
    output = _as_text(log_path.read_bytes()) + note
    if log_path.read_bytes() != output.encode("utf-8"):
        log_path.write_text(output, encoding="utf-8")
    return code, output


def _phase(state: Any, name: str) -> str:
    """`check` in the check step itself, `recheck` when another step (a review round) runs it."""
    return "check" if running_step(state) in ("", name) else "recheck"


def plan_for(layer: Any, state: Any, env_stack: Any, step: dict[str, Any], phase: str, tree: str | None = None,
             reference: str | None = None) -> Any:
    """What one check runs now (testing.plan_check). The tree and what the change is measured
    against are computed only when a depth below full is asked; the gate measures against the
    run's base, a check step against the cmd's last pass (affected.reference). Never raises: a
    depth that cannot be decided runs the full form and says why."""
    try:
        asked = testing.normalise(step["depth"]) if step.get("depth") else testing.run_depth(layer, state, phase)[0]
        if asked != "full":
            if tree is None:
                tree = affected.tree(layer, state, env_stack)
            if reference is None:
                reference = (affected.base(layer, state, env_stack) if phase == "gate"
                             else affected.reference(layer, state, env_stack, step, tree))
        return testing.plan_check(layer, state, env_stack, step, phase, reference or "", tree or "")
    except Exception as exc:  # noqa: BLE001 — a depth that cannot be decided means "run it all"
        why = f"the depth could not be decided ({type(exc).__name__}: {exc})"[:300]
        print(f"[adw {state.adw_id}] check {step.get('name')}: {why}; running the full form")
        return testing.CheckPlan("full", "full", dict(layer.cmd(step["cmd"])), why, None, "tests", "")


def _tracked(layer: Any, plan: Any) -> bool:
    """Whether a result carries its depth: the layer declares (testing …), or a depth below full
    was asked (--testing, the local setting). A layer without the clause records what it did before."""
    return bool(testing.policy(layer)["declared"] or plan.depth != "full" or plan.asked != "full")


def _depth_fields(acc: dict[str, Any]) -> dict[str, Any]:
    return {"depth": acc.get("depth"), "selected": acc.get("selected"), "total": acc.get("total"),
            "strategies": dict(acc.get("strategies") or {})}


def _work_root(layer: Any, state: Any, env_stack: Any) -> Path:
    worktree = str(getattr(state, "worktree", "") or "")
    if worktree and Path(worktree).is_dir():
        return Path(worktree)
    sandbox = getattr(env_stack, "sandbox", None)
    if sandbox is not None and getattr(sandbox, "worktree", None):
        return Path(sandbox.worktree)
    return Path(layer.app_root)


def _head(state: Any, env_stack: Any, step: str) -> str:
    try:
        result = delivery.tree_git(state, env_stack, ["rev-parse", "HEAD"], step=step)
    except Exception:  # noqa: BLE001 — an unknown commit only means the timing is not keyed by one
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _coverage_form(layer: Any, state: Any, env_stack: Any, plan: Any, tree: str) -> str:
    """The cmd's #:coverage-run when this run records coverage: a full run of a cmd that declares
    #:record-coverage, on a tree that is exactly HEAD's (the map is keyed by that commit)."""
    command = plan.command
    recipe = str(command.get("recordCoverage") or "none")
    if plan.depth != "full" or recipe == "none" or not command.get("coverageRun") or not command.get("coverageFile"):
        return ""
    if not tree or tree != affected.head_tree(layer, state, env_stack):
        return ""
    return str(command["coverageRun"])


def _after_full_pass(layer: Any, state: Any, env_stack: Any, plan: Any, name: str, seconds: float,
                     log_text: str, coverage: bool) -> None:
    """A passing full run: the suite's timing and size, and the coverage map its coverage form wrote."""
    command = plan.command
    commit = _head(state, env_stack, name)
    try:
        testing.note_full_run(layer, command, seconds, log_text, commit)
    except OSError as exc:
        print(f"[adw {state.adw_id}] check {name}: the suite timing was not recorded: {exc}")
    if not coverage or not commit:
        return
    notes: list[str] = []
    root = _work_root(layer, state, env_stack)
    recipe = str(command.get("recordCoverage"))
    tests = coverage_map.read_recipe(recipe, root / str(command["coverageFile"]), root,
                                     prefix=str(command.get("testRoot") or ""), notes=notes)
    for note in notes:
        print(f"[adw {state.adw_id}] check {name}: {note}")
    if tests:
        path = coverage_map.save(layer, command, commit, recipe, tests)
        print(f"[adw {state.adw_id}] check {name}: coverage map of {len(tests)} tests recorded at {commit[:7]} "
              f"({Path(path).relative_to(layer.layer_dir).as_posix()})")


def run_check(layer: Any, state: Any, step: dict[str, Any], env_stack: Any,
              attempt_offset: int = 0, timeout: int | None = None, decision: Any = None, plan: Any = None) -> int:
    """Run a check step's cmd once; `timeout` overrides the cmd's own time limit. `decision` is
    what affected.decide said about this attempt (its tree and definition are not computed again);
    `plan` what testing.plan_check chose (the form that runs; computed here when not given)."""
    name = str(step["name"])
    if env_stack.read_only:
        raise StepFailed(name, "check refused in read-only host environment")
    tree_before = decision.tree if decision is not None else affected.tree(layer, state, env_stack)
    if plan is None:
        plan = plan_for(layer, state, env_stack, step, _phase(state, name), tree=tree_before)
    command = plan.command
    tracked = _tracked(layer, plan)
    coverage_run = _coverage_form(layer, state, env_stack, plan, tree_before) if tracked else ""
    cmd = coverage_run or str(command["run"])
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
        seconds = time.monotonic() - started
        acc = None
        if tracked:
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            acc = testing.accounting(plan, seconds, log_text, str(step["cmd"]), layer)
        events.emit("check.end", **span, exit=exit_code, seconds=round(seconds, 3),
                    **_skip_fields(report, env_id), **(_depth_fields(acc) if acc is not None else {}))
    # a result belongs to a tree only when the command left the worktree as it found it
    tree = tree_before if tree_before and tree_before == affected.tree(layer, state, env_stack) else ""
    outcome = affected.PASSED if exit_code == 0 else affected.FAILED
    record: dict[str, Any] = {"cmd": cmd, "exit": exit_code, "log": relative_log, "outcome": outcome, "tree": tree}
    if report is not None:
        record["skips"] = report.as_dict()
    if problems:
        record["skip_rule"] = problems
    if env_id:
        record["in"] = env_id
    if acc is not None:
        record["testing"] = acc
    state.checks[name] = record
    affected.record(state, check=str(step["cmd"]), step=name, phase=span["phase"], outcome=outcome, tree=tree,
                    definition=(decision.definition if decision is not None
                                else affected.definition(layer, state, command, step)),
                    exit=exit_code, log=relative_log, why=decision.why if decision is not None else "",
                    **({"depth": plan.depth, "testing": acc} if acc is not None else {}))
    state.save(layer.layer_dir)
    if acc is not None:
        print(f"[adw {state.adw_id}] check {name}: {testing.accounting_text(acc)}")
        if exit_code == 0 and plan.depth == "full":
            _after_full_pass(layer, state, env_stack, plan, name, seconds, log_text, bool(coverage_run))
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


def _decision_text(name: str, decision: Any, entry: dict[str, Any]) -> str:
    """What a log file says in place of a command's output when the command was not run."""
    lines = [f"al: check {name} not run: {affected.summary_text(entry)}"]
    if decision.action == affected.REUSE:
        source = decision.source or {}
        lines.append(f"al: it passed in {source.get('phase')} step {source.get('step')} on the same tree; "
                     f"log {source.get('log')}")
    else:
        lines.append(f"al: compared with {decision.reference or '(unknown)'}")
    lines.append(f"al: tree {decision.tree or '(unknown)'}")
    if decision.action != affected.REUSE:
        lines.append(f"al: {decision.paths_total} changed path(s) compared with what the check covers:")
        lines += [f"al:   {path}" for path in decision.paths]
        if decision.paths_total > len(decision.paths):
            lines.append(f"al:   … and {decision.paths_total - len(decision.paths)} more")
    return "\n".join(lines) + "\n"


def _decided(layer: Any, state: Any, step: dict[str, Any], decision: Any, phase: str,
             log_path: Path | None = None) -> dict[str, Any]:
    """Record a check that was not run (`reuse` or `not-affected`): its log, a check.decision
    event, the ledger entry and, for a check step itself, its `state.checks` record. With
    `log_path` (the gate) the decision is that log's content and `state.checks` is left alone."""
    name, cmd_name = str(step["name"]), str(step["cmd"])
    reused = decision.action == affected.REUSE
    source = decision.source or {}
    outcome = affected.REUSED if reused else affected.NOT_AFFECTED
    entry: dict[str, Any] = {"outcome": outcome, "why": decision.why, "tree": decision.tree,
                             "paths_total": decision.paths_total, "from": source if reused else None}
    if log_path is None and not reused:
        log_path = Path(layer.layer_dir) / "agents" / state.adw_id / "checks" / f"{name}.not-affected.log"
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(_decision_text(cmd_name if phase in ("gate", "evidence") else name, decision, entry),
                                  encoding="utf-8")
        log = Path(log_path).relative_to(layer.layer_dir).as_posix()
    else:
        log = str(source.get("log") or "")
    events.emit("check.decision", step=name, owner=running_step(state), phase=phase, cmd=cmd_name,
                outcome=outcome, why=decision.why, paths=list(decision.paths), paths_total=decision.paths_total,
                tree=decision.tree, from_step=str(source.get("step") or "") if reused else "",
                from_phase=str(source.get("phase") or "") if reused else "", log=log)
    # a reused result shows the depth of the pass it reuses
    depth = {"depth": source.get("depth"), "testing": source.get("testing")} if reused and source.get("testing") else {}
    recorded = affected.record(state, check=cmd_name, step=name, phase=phase, outcome=outcome, tree=decision.tree,
                               definition=decision.definition, exit=0 if reused else None, log=log, why=decision.why,
                               paths=decision.paths, paths_total=decision.paths_total,
                               **({"from": source} if reused else {}), **depth)
    if phase == "check":
        # exit 0 because the gate counts a check step without a record as failed; `outcome` is
        # what every reader shows, so a check that was not run is never displayed as passed
        cmd = str(layer.cmd(cmd_name)["run"])
        if reused:
            record: dict[str, Any] = {"cmd": cmd, "exit": 0, "log": log, "outcome": outcome,
                                      "from": recorded["from"], "tree": decision.tree}
            earlier = state.checks.get(str(source.get("step"))) or {}
            if earlier.get("log") == log and earlier.get("skips"):
                record["skips"] = earlier["skips"]
            if depth.get("testing"):
                record["testing"] = dict(depth["testing"])
        else:
            record = {"cmd": cmd, "exit": 0, "outcome": outcome, "why": decision.why,
                      "paths": list(decision.paths), "paths_total": decision.paths_total,
                      "tree": decision.tree, "log": log}
        state.checks[name] = record
    state.save(layer.layer_dir)
    return recorded


def _review_option(layer: Any, state: Any) -> dict[str, Any]:
    try:
        return layer.option("reviews", layer.adw(state.adw)["review"])
    except (LayerError, KeyError):
        return {}


def run_logged(layer: Any, state: Any, env_stack: Any, command: dict[str, Any], log_path: Path,
               step: str, phase: str) -> int:
    """A codebase command outside a check step (the gate's re-runs, the deploy command), with its
    log streamed to log_path and a check.start / check.end pair. At the gate the command is not
    run when it already passed on exactly the tree that ships (the pass is reused) or, with
    `#:require affected`, when no changed path is under what it covers; both are recorded."""
    span = {"step": step, "owner": running_step(state), "phase": phase, "cmd": str(command["name"]),
            "attempt": 1, "log": Path(log_path).relative_to(layer.layer_dir).as_posix()}
    # the gate re-runs a cmd where its check step runs it (#:in) and under the same skip rules; the
    # evidence step (evidence.py) runs a cmd no check step runs, and decides and records it the same way
    decided = phase in ("gate", "evidence")
    check = checkenv.check_for_cmd(layer, state, str(command["name"])) if decided else None
    decision = plan = None
    run_line = str(command["run"])
    if decided:
        option = _review_option(layer, state) if phase == "gate" else {}
        if phase == "gate":
            # the gate's depth: the run's per-run depth, full when a person asked for full before it ships;
            # a different form is a different definition, so a targeted pass is never reused for full
            plan = plan_for(layer, state, env_stack, {**(check or {}), "name": step, "cmd": str(command["name"])}, "gate")
        decision = affected.decide_gate(layer, state, env_stack, command, check, option,
                                        command=plan.command if plan is not None else None)
        if decision.action != affected.RUN:
            entry = _decided(layer, state, {"name": step, "cmd": str(command["name"])}, decision, phase, log_path)
            print(f"[adw {state.adw_id}] {phase}: {command['name']} not run: {affected.summary_text(entry)}")
            return 0
        if plan is not None:
            run_line = str(plan.command["run"])
    tracked = plan is not None and _tracked(layer, plan)
    stack = checkenv.stack_for(layer, state, check, env_stack) if check is not None else env_stack
    env_id = checkenv.target(layer.adw(state.adw), check) if check is not None else None
    argv = stack.wrap(run_line)
    events.emit("check.start", **span)
    started = time.monotonic()
    started_wall = time.time()
    code, _output = run_command(argv, stack.cwd, stack.process_env(), int(command.get("timeout", 1800)),
                                log_path=log_path)
    fields: dict[str, Any] = {}
    if check is not None:
        code, report, _problems = _skip_rules(layer, state, check, log_path, started_wall, code)
        fields = _skip_fields(report, env_id)
    seconds = time.monotonic() - started
    acc = None
    if tracked:
        log_text = Path(log_path).read_text(encoding="utf-8", errors="replace") if Path(log_path).is_file() else ""
        acc = testing.accounting(plan, seconds, log_text, str(command["name"]), layer)
        fields.update(_depth_fields(acc))
    events.emit("check.end", **span, exit=code, seconds=round(seconds, 3), **fields)
    if decision is not None:
        # the result belongs to the shipping tree only when that is what the command ran on and left
        tree = decision.tree if decision.tree and decision.tree == affected.tree(layer, state, env_stack) else ""
        affected.record(state, check=str(command["name"]), step=step, phase=phase,
                        outcome=affected.PASSED if code == 0 else affected.FAILED, tree=tree,
                        definition=decision.definition, exit=code, log=span["log"], why=decision.why,
                        paths=decision.paths, paths_total=decision.paths_total,
                        **({"depth": plan.depth, "testing": acc} if acc is not None else {}))
        state.save(layer.layer_dir)
        if acc is not None:
            print(f"[adw {state.adw_id}] gate: {command['name']}: {testing.accounting_text(acc)}")
            if code == 0 and plan.depth == "full":
                _after_full_pass(layer, state, env_stack, plan, step, seconds, log_text, False)
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
        except (BudgetExceeded, LimitReached, RunStopped):
            raise  # a spending or usage limit, or a person's stop, is never retried
        except StepFailed as exc:
            if attempt == retries:
                raise
            print(f"[adw {state.adw_id}] step {name} attempt {attempt + 1} failed, retrying: {exc.reason}")
    state.outputs[name] = output
    if command.get("template") == "classify" and not state.class_fixed:
        state.klass = str(json.loads(output)["class"])
    if command.get("output") == "path":
        state.plan = output


def run_check_with_resolve(layer: Any, state: Any, step: dict[str, Any], env_stack: Any,
                           redecide: bool = True) -> bool:
    """Run a check step's cmd; on failure let the resolve command fix it and re-run, up to `retries`.

    Before each attempt affected.decide says whether the cmd runs at all: a pass on the same tree
    is reused, and a `#:when-affected` step whose cmd covers no changed path is recorded as not
    affected. Returns True when the cmd ran (and passed), False when it was not run. In a review
    round (the owner is another step) the check steps recorded as not affected are decided again."""
    name = str(step["name"])
    retries = max(0, int(step.get("retries", 0)))
    owner = running_step(state)
    in_round = redecide and owner not in ("", name)
    phase = "check" if owner in ("", name) else "recheck"
    for attempt in range(retries + 1):
        # the depth first: the form it chooses is part of the definition affected.decide compares
        plan = plan_for(layer, state, env_stack, step, phase)
        decision = affected.decide(layer, state, env_stack, step, command=plan.command)
        if decision.action != affected.RUN:
            entry = _decided(layer, state, step, decision, phase)
            print(f"[adw {state.adw_id}] check {name} not run: {affected.summary_text(entry)}")
            if in_round:
                _redecide_not_affected(layer, state, env_stack)
            return False
        exit_code = run_check(layer, state, step, env_stack, decision=decision, plan=plan)
        if exit_code == 124:
            exit_code = _retry_time_limit(layer, state, step, env_stack, decision, plan)
        final = exit_code == 0 or not step.get("resolve") or attempt == retries
        if final:
            # Collected on failure too: screenshots of a failing run are the proof a human needs.
            artefacts.collect(layer, state, list(step.get("collect", [])), name)
            if exit_code == 0:
                if in_round:
                    _redecide_not_affected(layer, state, env_stack)
                return True
            raise StepFailed(name, f"check {step['cmd']} exited {exit_code} after {attempt + 1} attempt(s); "
                                   f"log {state.checks[name]['log']}")
        log_path = Path(layer.layer_dir) / state.checks[name]["log"]
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-150:]
        resolve_args = [str(plan.command["run"]), "\n".join(lines)]
        resolve_command = layer.command(step["resolve"])
        model_set = str(resolve_command.get("modelSet", "base"))
        output = run_agent(layer, state, f"{name}-resolve-{attempt + 1}", step["resolve"], resolve_args,
                           model_set, env_stack)
        state.outputs[f"{name}-resolve-{attempt + 1}"] = output
    return True


def _redecide_not_affected(layer: Any, state: Any, env_stack: Any) -> None:
    """A review round changed the tree: decide again for every check step that was recorded as not
    affected and has no decision for the tree as it is now. The round re-runs only the check steps
    that ran (review._checks_that_ran), so a patch that touches what such a step covers would
    otherwise go unchecked until the gate. A step that runs now becomes `passed`; a failure
    raises as any re-check does."""
    current = affected.tree(layer, state, env_stack)
    for step in layer.adw(state.adw).get("steps", []):
        if step.get("kind") != "check":
            continue
        record = next((r for r in state.steps if r.get("name") == step.get("name")), None)
        if record is None or record.get("status") != NOT_AFFECTED:
            continue
        if current and any(entry.get("check") == step.get("cmd") and entry.get("tree") == current
                           for entry in getattr(state, "check_results", [])):
            continue
        print(f"[adw {state.adw_id}] review round: deciding again for check {step['name']} (it was not affected before)")
        if run_check_with_resolve(layer, state, step, env_stack, redecide=False):
            record.update(status="passed", detail=_pass_detail(state, step))
            state.save(layer.layer_dir)


def _retry_time_limit(layer: Any, state: Any, step: dict[str, Any], env_stack: Any, decision: Any = None,
                      plan: Any = None) -> int:
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
    exit_code = run_check(layer, state, step, env_stack, timeout=2 * limit, decision=decision, plan=plan)
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
ANSWER_FILE = "answer.md"  # a person's answer to a waiting run (launch.resume), exported as ADW_ANSWER_FILE


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
    answer = Path(layer.layer_dir) / "agents" / state.adw_id / ANSWER_FILE   # written by launch.resume(answer=…)
    context = {"ADW_ID": state.adw_id, "ADW_PROMPT": state.prompt, "ADW_CLASS": state.klass,
               "ADW_PLAN": state.plan, "ADW_BRANCH": state.branch, "ADW_WORKTREE": str(env_stack.cwd),
               "ADW_LAYER_DIR": str(layer.layer_dir), "ADW_STEP": name, "ADW_RESUME": "1" if resuming else "",
               "ADW_ANSWER_FILE": str(answer) if answer.is_file() else ""}
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
        from . import convergence, integrate, review

        # an assigned run (integrate.py, modes blockers and proof) reviews once, with no patch round
        try:
            review.run_review(integrate.with_review_retries(layer, adw), state, step, env_stack)
        finally:
            convergence.after_review(layer, state, step)
    elif kind == "ship":
        if env_stack.read_only:
            raise StepFailed(str(step["name"]), "ship refused in read-only host environment")
        from . import ship

        ship.ship(layer, state, adw, step, env_stack)
        if getattr(layer, "delivery", None):  # the run delivers into a stage (see delivery.py)
            from . import delivery

            delivery.after_ship(layer, state)
        affected.publish(layer, state)  # what each check proved, next to the pull request text
        from . import batch

        batch.after_landing(layer, state)  # the full suites per batch: started when one is due; never raises
    elif kind == "fan-out":
        from . import fanout

        fanout.run_fan_out(layer, state, step, env_stack)
    elif kind == "promote":  # a step of a promotion run (promotion.promotion_adw)
        from . import promotion

        promotion.run_step(layer, state, adw, step, env_stack)
    elif kind == "integrate":  # a step of an integration run (integrate.integration_adw)
        from . import integrate

        integrate.run_step(layer, state, adw, step, env_stack)
    elif kind == "batch":  # a step of a batch run (batch.batch_adw, the kit workflow verify-batch)
        from . import batch

        batch.run_step(layer, state, adw, step, env_stack)
    else:
        raise LayerError(f"Unknown ADW step kind: {kind!r}")


DELIVER_STEP = "deliver"
PROVE_STEP = "prove"
EVIDENCE_STEP = "evidence"
FRESHEN_REVIEW_STEP = "freshen-review"
FRESHEN_SHIP_STEP = "freshen-ship"


def _plans(layer: Any, step: dict[str, Any]) -> bool:
    """A run step whose command writes the plan (#:output path), not a patch plan."""
    if step.get("kind") != "run":
        return False
    try:
        command = layer.command(str(step.get("command")))
    except (LayerError, KeyError):
        return False
    return command.get("output") == "path" and command.get("template") != "plan-patch"


def _prove(layer: Any, state: Any, adw: dict[str, Any], env_stack: Any) -> None:
    """The provability check of the plan's Proof plan (proofplan.py), recorded as step `prove` only
    when the plan has the table; a plan without one is recorded as such and changes nothing."""
    from . import proofplan

    parsed = proofplan.parse(proofplan.plan_text(state, layer))
    if parsed["table"] == "absent":
        state.proof_plan = {"plan": state.plan, "table": "absent"}
        state.save(layer.layer_dir)
        return
    _run_recorded(layer, state, PROVE_STEP, PROVE_STEP, lambda: proofplan.check(layer, state, adw, env_stack))


def _review_kind(layer: Any, adw: dict[str, Any]) -> str:
    try:
        return str(layer.option("reviews", adw["review"]).get("kind") or "")
    except (LayerError, KeyError):
        return ""


def _freshen(layer: Any, state: Any, adw: dict[str, Any], env_stack: Any, when: str) -> None:
    """Bring the target branch's new commits in before the review or ship (freshen.py), recorded
    as `freshen-<when>` only when the target moved."""
    from . import freshen

    if not getattr(state, "worktree", ""):
        return
    if any(item.get("kind") in ("integrate", "promote") for item in adw.get("steps") or []):
        return  # an integration or promotion run brings its target in itself
    if freshen.behind(layer, state, env_stack) > 0:
        _run_recorded(layer, state, f"freshen-{when}", "freshen",
                      lambda: freshen.run(layer, state, adw, env_stack, when))


def _before_review(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], env_stack: Any) -> None:
    """Before an agent or auto review: the target branch, then the evidence pack (evidence.py),
    recorded as `evidence` when there is a Proof plan table or an approval."""
    from . import evidence, proofplan

    if _review_kind(layer, adw) not in ("agent", "auto"):
        return
    _freshen(layer, state, adw, env_stack, "review")
    if proofplan.has_table(state) or state.approvals:
        _run_recorded(layer, state, EVIDENCE_STEP, EVIDENCE_STEP,
                      lambda: evidence.run(layer, state, adw, step, env_stack))
    else:
        evidence.prepare_context(layer, state, adw, step, env_stack)


def run_step(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], env_stack: Any) -> None:
    name, kind = str(step.get("name", "")), str(step.get("kind", ""))
    record = state.step_record(name, kind)
    # a rejected review is finished: re-running it on --resume would repeat its agent calls and commits
    if record.get("status") in ("passed", REJECTED):
        prove = next((r for r in state.steps if r.get("name") == PROVE_STEP), None)
        if _plans(layer, step) and prove is not None and prove.get("status") != "passed":
            _prove(layer, state, adw, env_stack)  # a resume after a stop before the build (al approve)
        return
    stopped = getattr(state, "stopped_by", "")
    if stopped:  # a stop that a step swallowed while it ran still ends the run before the next step
        raise RunStopped(name, stopped)
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
    elif kind == "ship" and review_outcome(state)[0] is not False and not env_stack.read_only:
        _freshen(layer, state, adw, env_stack, "ship")
    if kind == "review":
        _before_review(layer, state, adw, step, env_stack)
    _run_recorded(layer, state, name, kind, lambda: _execute_step(layer, state, adw, step, env_stack), step)
    if _plans(layer, step):
        _prove(layer, state, adw, env_stack)


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
    if kind == "batch":
        from . import batch

        why = batch.skipped(state, name)
        if why:  # bisect and fix of a green batch
            ended("skipped", why, f"[adw {state.adw_id}] step {name} ({kind}) skipped: {why}")
            return
    if kind == "check" and (state.checks.get(name) or {}).get("outcome") == affected.NOT_AFFECTED:
        check = state.checks[name]
        detail = f"not affected: {check.get('why')} ({int(check.get('paths_total') or 0)} paths compared)"
        ended(NOT_AFFECTED, detail, f"[adw {state.adw_id}] step {name} ({kind}) not affected: {detail}")
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
    if step.get("kind") == "check" and (state.checks.get(name) or {}).get("outcome") == affected.REUSED:
        check = state.checks[name]
        return (f"reused from step {(check.get('from') or {}).get('step')}, same tree "
                f"{str(check.get('tree') or '')[:8]}, log {check.get('log')}")
    if step.get("kind") == "check" and name in state.checks:
        detail = f"exit {state.checks[name]['exit']}, log {state.checks[name]['log']}"
        if isinstance(state.checks[name].get("testing"), dict):  # passed — targeted: ran 14 of 812 tests …
            detail = f"passed — {testing.accounting_text(state.checks[name]['testing'])}; log {state.checks[name]['log']}"
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
