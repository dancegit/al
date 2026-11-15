"""(fan-out NAME #:adw CHILD #:count N #:pick best|first|all #:max-parallel M)

N independent runs of the child ADW, each launched as its own process
(`uv run <layer>/adws/adw_<child>.py --class <class> --adw-id <id> [--dry-run] -- <prompt>`), so every
candidate gets its own worktree, branch, container, port lease and slots exactly like a manual run.
The parent exports ADW_FANOUT_PARENT / ADW_FANOUT_STEP / ADW_FANOUT_INDEX (1..N) to each candidate.

A candidate's work survives its run because the child commits it (the compiler requires a child
without ship; its last step is expected to commit) and teardown keeps a branch that holds commits
the default branch does not (sandboxed runs publish their HEAD to the branch on teardown).

After all candidates finish the winner(s) are merged into the PARENT's worktree, so the parent's
later check/review/ship steps work on them. Then candidate worktrees are removed, and candidate
branches are deleted when they are merged into the parent or lost; their ids and head commits
stay in state.fanout[NAME], so nothing disappears unrecorded. When the step fails, branches of
candidates that passed are kept for a human (a conflict, or `all` with a failing sibling).

Slots: the parent holds one `runs` slot for its whole run and every candidate takes another, so
(concurrency slots #:max-runs …) must exceed concurrent parents plus candidates; with fewer slots
candidates wait for one (up to waitSeconds). maxRuns < 2 can never work and fails immediately.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import IO, Any, Callable

from . import envs, events, gitsafe, slots
from . import state as state_module
from .errors import LayerError, StepFailed

GIT_TIMEOUT = 300
POLL_SECONDS = 0.2
IDENTITY = ["-c", "user.name=adw", "-c", "user.email=adw@localhost"]
_LOG_ATTEMPT = re.compile(r"\.(\d+)\.log$")


def default_launcher(layer: Any, child: str, args: list[str], env: dict[str, str], log: IO[str]) -> subprocess.Popen:
    """Start one candidate exactly like a manual run of the generated entry script."""
    script = Path(layer.layer_dir) / "adws" / f"adw_{child}.py"
    if not script.is_file():
        raise StepFailed("fan-out", f"{script} does not exist; run `al gen` so the child ADW has its entry script")
    return subprocess.Popen(["uv", "run", "--quiet", str(script), *args], cwd=layer.layer_dir, env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


# Replaceable (tests inject a launcher that runs the runtime without generated scripts).
launcher: Callable[[Any, str, list[str], dict[str, str], IO[str]], subprocess.Popen] = default_launcher


# ---------------------------------------------------------------- git helpers

def _git(argv: list[str], step: str, check: bool, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed(step, f"git … {' '.join(argv[-3:])} failed: {exc}") from exc
    if check and result.returncode != 0:
        raise StepFailed(step, f"git … {' '.join(argv[-3:])} failed: {(result.stderr or result.stdout).strip()}")
    return result


def _app_git(layer: Any, sandbox: gitsafe.Sandbox | None, args: list[str], step: str,
             check: bool = True) -> subprocess.CompletedProcess[str]:
    """git in the application repository (refs only; its working tree is never involved)."""
    if sandbox is not None:
        return gitsafe.run_app(sandbox, args, step, check=check)
    return _git(["git", "-C", str(layer.app_root), *gitsafe.HARDENING, *args], step, check)


def _tree_git(state: Any, sandbox: gitsafe.Sandbox | None, args: list[str], step: str,
              check: bool = True) -> subprocess.CompletedProcess[str]:
    """git in the parent run's worktree; sandboxed: only through gitsafe (frozen, verified, pinned)."""
    if sandbox is not None:
        return gitsafe.run(sandbox, args, step, check=check)
    return _git(["git", "-C", str(state.worktree), *args], step, check)


# ---------------------------------------------------------------- launching

def _fresh_ids(layer_dir: Path, count: int) -> list[str]:
    ids: list[str] = []
    while len(ids) < count:
        candidate = state_module.new_adw_id()
        if candidate not in ids and not state_module.run_dir(layer_dir, candidate).exists():
            ids.append(candidate)
    return ids


def _child_args(state: Any, adw_id: str) -> list[str]:
    return ["--class", state.klass, "--adw-id", adw_id, *(["--dry-run"] if state.dry_run else []), "--", state.prompt]


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


def _launch_all(layer: Any, state: Any, name: str, child: str, candidates: list[dict[str, Any]],
                parallel: int, log_dir: Path) -> None:
    """Run every candidate, at most `parallel` at once; fills exit, seconds and completion order."""
    pending = list(candidates)
    running: dict[str, tuple[dict[str, Any], subprocess.Popen, IO[str], float]] = {}
    completed = 0
    try:
        while pending or running:
            while pending and len(running) < parallel:
                candidate = pending.pop(0)
                log = (log_dir / f"{candidate['id']}.log").open("w", encoding="utf-8")
                env = {**os.environ, "ADW_FANOUT_PARENT": state.adw_id, "ADW_FANOUT_STEP": name,
                       "ADW_FANOUT_INDEX": str(candidate["index"])}
                try:
                    process = launcher(layer, child, _child_args(state, candidate["id"]), env, log)
                except StepFailed as exc:
                    log.close()
                    raise StepFailed(name, exc.reason) from exc
                except OSError as exc:
                    log.close()
                    raise StepFailed(name, f"could not launch candidate {candidate['id']}: {exc}") from exc
                running[candidate["id"]] = (candidate, process, log, time.monotonic())
                events.say(f"[adw {state.adw_id}] fan-out {name}: launched {candidate['id']} "
                           f"({candidate['index']}/{len(candidates)}), log {candidate['log']}", "fanout.candidate",
                           step=name, id=candidate["id"], index=candidate["index"], status="running")
            for adw_id, (candidate, process, log, started) in list(running.items()):
                code = process.poll()
                if code is None:
                    continue
                log.close()
                completed += 1
                candidate.update(exit=code, seconds=round(time.monotonic() - started, 2), completed=completed)
                del running[adw_id]
            if running:
                time.sleep(POLL_SECONDS)
    finally:
        for candidate, process, log, _started in running.values():  # interrupted: no orphan candidates
            _stop(process)
            log.close()
            candidate.update(exit=process.returncode, reason="stopped: the fan-out step was interrupted")
            events.emit("fanout.candidate", step=name, id=candidate["id"], index=candidate["index"], status="stopped",
                        exit=process.returncode, reason=candidate["reason"])


# ---------------------------------------------------------------- judging

def _resolve_attempts(data: dict[str, Any]) -> int:
    """Failed-then-resolved check attempts: a check's log number is its attempt count."""
    total = 0
    for check in (data.get("checks") or {}).values():
        match = _LOG_ATTEMPT.search(str(check.get("log", "")))
        total += max(0, int(match.group(1)) - 1) if match else 0
    return total


def _failure(data: dict[str, Any], code: int | None) -> str:
    for step in data.get("steps") or []:
        if step.get("status") == "failed":
            return f"step {step.get('name')} failed: {step.get('detail', '')}"[:400]
    return f"run {data.get('status', 'unknown')} (exit {code})"


def _judge(layer: Any, sandbox: gitsafe.Sandbox | None, candidate: dict[str, Any], step: str) -> None:
    """Read the candidate's state.json and its branch; sets status, branch, head, diff, reason."""
    candidate.update(status="failed", branch="", head="", diff_lines=0, resolve_attempts=0)
    path = state_module.run_dir(layer.layer_dir, candidate["id"]) / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        candidate.setdefault("reason", f"no readable state.json ({exc}); see {candidate['log']}")
        return
    candidate["resolve_attempts"] = _resolve_attempts(data)
    branch = str(data.get("branch") or "")
    try:  # state.json of a sandboxed child is container-writable: only ever touch the child's own branch
        envs.check_run_branch(branch, candidate["id"], step)
    except StepFailed:
        candidate.setdefault("reason", f"state names branch {branch!r}, which is not this candidate's own")
        return
    candidate["branch"] = branch
    head = _app_git(layer, sandbox, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
                    step, check=False).stdout.strip()
    candidate["head"] = head
    default = f"refs/heads/{layer.default_branch}"
    ahead = 0
    if head:
        ahead = int(_app_git(layer, sandbox, ["rev-list", "--count", f"{default}..{head}"], step).stdout.strip() or 0)
        numstat = _app_git(layer, sandbox, ["diff", "--numstat", f"{default}...{head}"], step).stdout
        candidate["diff_lines"] = sum(
            (int(added) if added.isdigit() else 1) + (int(deleted) if deleted.isdigit() else 0)
            for added, deleted, *_rest in (line.split("\t") for line in numstat.splitlines() if line.strip()))
    if data.get("status") != "passed" or candidate.get("exit") != 0:
        candidate.setdefault("reason", _failure(data, candidate.get("exit")))
    elif ahead == 0:
        candidate.setdefault("reason", "committed nothing: no commits beyond the default branch")
    else:
        candidate["status"] = "passed"
        candidate.pop("reason", None)


def _select(candidates: list[dict[str, Any]], mode: str, step: str) -> list[dict[str, Any]]:
    passed = [item for item in candidates if item["status"] == "passed"]
    failures = "; ".join(f"{item['id']}: {item.get('reason', '')}" for item in candidates if item["status"] != "passed")
    if not passed:
        raise StepFailed(step, f"no candidate passed — {failures}")
    if mode == "all":
        if len(passed) != len(candidates):
            raise StepFailed(step, f"pick all needs every candidate to pass — {failures}")
        return sorted(passed, key=lambda item: item["index"])
    if mode == "first":
        return [min(passed, key=lambda item: item["completed"])]
    if mode == "best":
        return [min(passed, key=lambda item: (item["resolve_attempts"], item["diff_lines"], item["completed"]))]
    raise LayerError(f"fan-out {step}: unknown pick {mode!r}")


# ---------------------------------------------------------------- merging and cleanup

def _merge(state: Any, sandbox: gitsafe.Sandbox | None, picked: list[dict[str, Any]], step: str) -> None:
    """Merge the winners into the parent worktree; on conflict, roll back to where it started."""
    before = _tree_git(state, sandbox, ["rev-parse", "HEAD"], step).stdout.strip()
    for candidate in picked:
        message = f"adw {state.adw_id}: fan-out {step} picks {candidate['id']}"
        result = _tree_git(state, sandbox, [*IDENTITY, "merge", "--no-edit", "-m", message,
                                            f"refs/heads/{candidate['branch']}"], step, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            _tree_git(state, sandbox, ["merge", "--abort"], step, check=False)
            _tree_git(state, sandbox, ["reset", "--hard", "-q", before], step, check=False)
            raise StepFailed(step, f"merge conflict merging candidate {candidate['id']} ({candidate['branch']}) "
                                   f"into the run's worktree: {' | '.join(detail[-3:])}; worktree reset to "
                                   f"{before[:12]}; branches of passing candidates are kept")


def _child_tree_dir(layer: Any, child: dict[str, Any]) -> Path:
    for env_id in child.get("environment", []):
        option = layer.option("environments", env_id)
        if option["kind"] == "worktree":
            return Path(layer.layer_dir) / str(option.get("dir") or "trees")
    raise LayerError(f"fan-out child ADW {child.get('name')!r} has no worktree environment")


def _cleanup(layer: Any, sandbox: gitsafe.Sandbox | None, candidates: list[dict[str, Any]], tree_dir: Path,
             keep: set[str], step: str) -> None:
    """Remove every candidate worktree (and quarantine); delete each branch not in `keep`, only
    if it still points where we recorded it (update-ref -d with old value)."""
    removed = False
    for candidate in candidates:
        if candidate["status"] == "pending":
            continue  # never judged (interrupted): its worktree/quarantine may hold its only copy of the work
        worktree = tree_dir / candidate["id"]
        if worktree.exists():
            _git(["git", "-C", str(layer.app_root), *gitsafe.HARDENING, "worktree", "remove", "--force",
                  str(worktree)], step, check=False)
            removed = True
        gitsafe.remove_quarantine(gitsafe.Sandbox.for_run(layer.app_root, tree_dir, layer.layer_dir, candidate["id"]))
        candidate["branch_deleted"] = False
        if candidate.get("branch") and candidate.get("head") and candidate["id"] not in keep:
            deleted = _app_git(layer, sandbox, ["update-ref", "-d", f"refs/heads/{candidate['branch']}",
                                                candidate["head"]], step, check=False)
            candidate["branch_deleted"] = deleted.returncode == 0
    if removed:
        _git(["git", "-C", str(layer.app_root), "worktree", "prune"], step, check=False)


def _report(state: Any, name: str, candidates: list[dict[str, Any]]) -> None:
    total = len(candidates)
    for item in sorted(candidates, key=lambda c: c["index"]):
        prefix = f"[adw {state.adw_id}] fan-out {name}: candidate {item['id']} ({item['index']}/{total})"
        seconds = item.get("seconds", "?")
        fields = {"step": name, "id": item["id"], "index": item["index"], "status": item["status"],
                  "exit": item.get("exit"), "seconds": item.get("seconds"), "reason": item.get("reason", "")}
        if item["status"] == "passed":
            events.say(f"{prefix} passed in {seconds}s: branch {item['branch']} head {item['head'][:12]}, "
                       f"diff {item['diff_lines']} lines, {item['resolve_attempts']} resolve attempt(s)",
                       "fanout.candidate", **fields)
        else:
            events.say(f"{prefix} failed in {seconds}s: {item.get('reason', '')}", "fanout.candidate", **fields)


# ---------------------------------------------------------------- the step

def run_fan_out(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> None:
    name = str(step["name"])
    if env_stack.read_only or not state.worktree:
        raise StepFailed(name, "fan-out needs a worktree environment: the winner is merged into the run's worktree")
    runs = slots.limits(layer)["runs"]
    if runs < 2:
        raise StepFailed(name, f"concurrency maxRuns is {runs}, but fan-out needs at least 2 `runs` slots: the parent "
                               f"holds one for its whole run and every candidate needs another; raise #:max-runs")
    child_name = str(step["adw"])
    child = layer.adw(child_name)
    if child_name == state.adw or any(s.get("kind") in ("ship", "fan-out") for s in child.get("steps", [])):
        raise LayerError(f"fan-out {name}: child ADW {child_name!r} must be another ADW without ship/fan-out steps")
    tree_dir = _child_tree_dir(layer, child)
    count = int(step.get("count", 3))
    mode = str(step.get("pick", "best"))
    parallel = min(count, int(step.get("maxParallel") or count))
    if parallel > runs - 1:
        print(f"[adw {state.adw_id}] fan-out {name}: max-parallel {parallel} but only {runs - 1} free `runs` slot(s) "
              f"besides this run; candidates will queue for slots")
    log_dir = state_module.run_dir(layer.layer_dir, state.adw_id) / "fanout" / name
    log_dir.mkdir(parents=True, exist_ok=True)
    candidates = [{"id": adw_id, "index": index, "status": "pending", "branch": "", "head": "", "diff_lines": 0,
                   "resolve_attempts": 0, "seconds": 0.0,
                   "log": (log_dir / f"{adw_id}.log").relative_to(layer.layer_dir).as_posix()}
                  for index, adw_id in enumerate(_fresh_ids(layer.layer_dir, count), start=1)]
    record: dict[str, Any] = {"candidates": candidates, "picked": [], "pick": mode, "adw": child_name}
    state.fanout[name] = record
    state.save(layer.layer_dir)
    sandbox = getattr(env_stack, "sandbox", None)
    keep: set[str] = set()
    try:
        _launch_all(layer, state, name, child_name, candidates, parallel, log_dir)
        for candidate in candidates:
            _judge(layer, sandbox, candidate, name)
        _report(state, name, candidates)
        keep = {item["id"] for item in candidates if item["status"] == "passed"}  # kept if anything below fails
        try:
            picked = _select(candidates, mode, name)
        except StepFailed:
            print(f"[adw {state.adw_id}] fan-out {name}: pick {mode} -> none")
            raise
        with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
            _merge(state, sandbox, picked, name)
            if sandbox is not None:  # host-side: import the merged HEAD's objects and publish the run branch
                parent_head = envs.publish_run_branch(sandbox, state.branch, state.adw_id, name)
            else:
                parent_head = _tree_git(state, None, ["rev-parse", "HEAD"], name).stdout.strip()
        keep = {item["id"] for item in picked
                if _app_git(layer, sandbox, ["merge-base", "--is-ancestor", item["head"], parent_head], name,
                            check=False).returncode != 0}
        record["picked"] = [item["id"] for item in picked]
        state.outputs[name] = " ".join(record["picked"])
        print(f"[adw {state.adw_id}] fan-out {name}: pick {mode} -> {state.outputs[name]} "
              f"(merged into {state.branch}, now {parent_head[:12]})")
    finally:
        try:
            _cleanup(layer, sandbox, candidates, tree_dir, keep, name)
        finally:
            state.save(layer.layer_dir)
