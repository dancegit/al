"""The kit workflow `verify-batch`: the full suites proven per batch, so each run may check less.

A layer that declares `(testing targeted …)` checks each change with the tests that concern it; the
whole is proven here, once per batch, on a stage head or the default branch. `al batch LAYER run`
(the layer's `adws/verify_batch.py run`), a landed run's ship step when a batch is due, a closed
cycle and the dashboard's "Run the full suites now" start a batch run: an ordinary run of a
synthetic ADW (`batch_adw`, like a promotion run) whose steps are kind `batch`, so it has the run
log, events, heartbeat, slots, summary and dashboard page of every run. Its steps:

    open     pin the branch head in the run's worktree; the last green batch's head (or the merge
             base with the default branch); the commits and the runs delivered since
    checks   every check cmd of the layer at #:on-stage depth on the head (the stage's #:checks,
             every review's #:require-checks, every cmd with #:covers; each once); a failure does
             not stop the run: the following steps need the result
    record   the ledger line (suite timings and coverage maps are recorded by the full runs)
    bisect   red only: per failing cmd only its failing tests, binary search over the first-parent
             commits since the last green batch; the first failing commit's run is the culprit; when
             the culprit's check of that cmd passed below full depth, the miss is counted and its
             changed files and the failing tests are learned
    fix      red only: open-fix starts a fix run (the failing tests, the culprit, its request and
             plan); revert prepares refs/heads/al/revert/<batch> (created, never moved)
    close    the final ledger line and `batch.result`; the run fails when the batch is red

`bisect` and `fix` are steps only when #:on-batch-failure lists them, and end as skipped on a green
batch. The ledger is agents/testing/batches.jsonl (one JSON line per batch result; a batch's later
line replaces its earlier one).

When a batch is due (`due`): #:batch-every changes landed on the branch since its last batch, or a
change landed and the last batch is older than #:batch-after-hours, or the cycle of that stage
closed since; never while a batch of the same branch runs.

Bisect moves only the batch run's own worktree (`checkout --detach`); application refs are only ever
created (al/revert/<id>), never moved. Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import delivery, events, gitsafe, ir, selection, skips, state as state_module, testing
from .errors import LayerError, StepFailed

ADW_NAME = "verify-batch"
STEPS = ("open", "checks", "record", "bisect", "fix", "close")
LEDGER = testing.TESTING_DIR / "batches.jsonl"
IDENTITY = ["-c", "user.name=adw", "-c", "user.email=adw@localhost"]
REVERT_PREFIX = "refs/heads/al/revert/"
LOG_TAIL = 60              # log lines of a failing check the fix run's request quotes
MAX_COMMITS = 200          # first-parent commits a batch without a known green head looks back over
RUN_ID = re.compile(r"^[0-9a-f]{8}$")
_MESSAGE_RUN = (re.compile(r"\[adw ([0-9a-f]{8})\]"), re.compile(r"\bMerge run ([0-9a-f]{8})\b"),
                re.compile(r"Merge branch '[^']*?\b([0-9a-f]{8})-[^']*'"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _when(stamp: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _layer_dir(layer: Any) -> Path:
    return Path(layer.layer_dir)


# ---------------------------------------------------------------- the ledger

def _ledger_path(layer: Any) -> Path:
    return _layer_dir(layer) / LEDGER


def ledger(layer: Any) -> list[dict[str, Any]]:
    """The batch results, oldest first; a batch's later line (its close) replaces its earlier one."""
    lines = testing._read_jsonl(_ledger_path(layer))
    latest: dict[str, dict[str, Any]] = {}
    for line in lines:
        if isinstance(line.get("id"), str):
            latest.pop(line["id"], None)
            latest[line["id"]] = line
    return list(latest.values())


def _append(layer: Any, line: dict[str, Any]) -> None:
    testing._append_jsonl(_ledger_path(layer), line)


def _of(line: Mapping[str, Any], branch: str) -> bool:
    return str(line.get("branch") or "") == branch


def last(layer: Any, branch: str) -> dict[str, Any] | None:
    """The newest batch result (green or red) of `branch`."""
    found = [line for line in ledger(layer) if _of(line, branch) and line.get("status") in ("green", "red")]
    return found[-1] if found else None


def last_green(layer: Any, branch: str) -> dict[str, Any] | None:
    found = [line for line in ledger(layer) if _of(line, branch) and line.get("status") == "green"]
    return found[-1] if found else None


def broken_by(layer: Any) -> dict[str, dict[str, Any]]:
    """run id → the red batch it broke (the dashboard's "broke the batch"); a later batch wins."""
    out: dict[str, dict[str, Any]] = {}
    for line in ledger(layer):
        culprit = str(line.get("culprit") or "")
        if line.get("status") == "red" and RUN_ID.match(culprit):
            out[culprit] = dict(line)
    return out


# ---------------------------------------------------------------- what a batch targets

def target(layer: Any, stage_name: str | None = None) -> tuple[str, str]:
    """(branch, stage): a stage's expanded branch (its open cycle), or the default branch ("" stage)."""
    if stage_name:
        stage = delivery.stage_named(layer, stage_name)
        return delivery.expand_branch(stage, delivery.load_cycle(layer)), stage.name
    return str(layer.codebase_default_branch), ""


def targets(layer: Any) -> list[tuple[str, str]]:
    """Every branch a batch proves: each stage's (its cycle's branch when it has one open), or the
    default branch of a layer without stages."""
    found: list[tuple[str, str]] = []
    for stage in delivery.stages(layer):
        try:
            branch = delivery.expand_branch(stage, delivery.load_cycle(layer))
        except (delivery.DeliveryError, LayerError):
            continue
        found.append((branch, stage.name))
    return found or [target(layer, None)]


def base_adw(layer: Any) -> dict[str, Any]:
    """The workflow whose environment, agent and review a batch run uses: #:fix-adw, else the first
    workflow with a worktree."""
    adws = [a for a in layer.ir.get("adws") or []
            if isinstance(a, dict) and not a.get("promotion") and not a.get("integration") and not a.get("batch")]
    if not adws:
        raise LayerError("the layer declares no workflow, so a batch has no environment to run in")
    wanted = str(testing.policy(layer).get("fixAdw") or "")
    if wanted:
        chosen = next((a for a in adws if a.get("name") == wanted), None)
        if chosen is not None:
            return chosen
    kinds = {str(e.get("id")): str(e.get("kind")) for e in layer.ir.get("environments") or [] if isinstance(e, dict)}
    return next((a for a in adws if any(kinds.get(str(e)) in ("worktree", "podman") for e in a.get("environment") or [])),
                adws[0])


def batch_steps(layer: Any) -> list[str]:
    failure = list(testing.policy(layer).get("onBatchFailure") or [])
    return [step for step in STEPS
            if step not in ("bisect", "fix") or (step == "bisect" and "bisect" in failure)
            or (step == "fix" and ("open-fix" in failure or "revert" in failure))]


def batch_adw(layer: Any, branch: str, stage: str) -> dict[str, Any]:
    """The synthetic ADW of a batch run (see the module docstring)."""
    base = base_adw(layer)
    declared = {str(o.get("id")) for o in layer.ir.get("promptInputs") or [] if isinstance(o, dict)}
    adw: dict[str, Any] = {
        "name": ADW_NAME, "description": f"verify the full suites on {branch}",
        "batch": {"branch": branch, "stage": stage, "base": str(base.get("name"))},
        "agent": base.get("agent"), "review": base.get("review"),
        "environment": list(base.get("environment") or []), "input": [i for i in ["cli"] if i in declared],
        "classes": [], "trigger": [], "changes": "codebase",
        "steps": [{"kind": "batch", "name": step, "action": step} for step in batch_steps(layer)]}
    if stage:
        adw["deliverTo"] = stage
    else:
        final = delivery.final_stage(delivery.stages(layer))
        if final is not None:
            adw["deliverTo"] = final.name
    return adw


def check_cmds(layer: Any, stage: str = "") -> list[str]:
    """Every check cmd a batch runs, each once: the stage's #:checks, every review's
    #:require-checks, every cmd with #:covers."""
    names: list[str] = []

    def add(name: Any) -> None:
        if name and str(name) not in names:
            names.append(str(name))

    for item in delivery.stages(layer):
        if not stage or item.name == stage:
            for name in item.checks:
                add(name)
    for review in layer.ir.get("reviews") or []:
        for name in (review or {}).get("requireChecks") or []:
            add(name)
    for cmd in (layer.ir.get("codebase") or {}).get("commands") or []:
        if isinstance(cmd, dict) and cmd.get("covers"):
            add(cmd.get("name"))
    known = {str(c.get("name")) for c in (layer.ir.get("codebase") or {}).get("commands") or [] if isinstance(c, dict)}
    return [name for name in names if name in known]


# ---------------------------------------------------------------- when a batch is due

def _states(layer: Any) -> list[dict[str, Any]]:
    out = []
    for path in sorted((_layer_dir(layer) / "agents").glob("*/state.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("adw_id", path.parent.name)
            out.append(data)
    return out


def _landed_branch(data: Mapping[str, Any]) -> str:
    ship = data.get("ship") or {}
    if not ship.get("shipped"):
        return ""
    return str((data.get("delivery") or {}).get("branch") or ship.get("target") or "")


def landed_since(layer: Any, branch: str, since: str = "", current: Any = None,
                 proven: Any = ()) -> list[dict[str, Any]]:
    """Runs that landed on `branch` after `since` (an ISO time; "" = ever) and are not among the
    `proven` run ids (what the last batch verified), oldest first. `current` is a run state in the
    middle of its ship step (not ended yet): it counts as landed now."""
    after = _when(since) if since else None
    proven = {str(item) for item in proven or ()}
    found: dict[str, dict[str, Any]] = {}
    for data in _states(layer):
        if data.get("adw") == ADW_NAME or _landed_branch(data) != branch or str(data.get("adw_id")) in proven:
            continue
        ended = _when(data.get("ended"))
        if after is not None and ended is not None and ended <= after:
            continue
        found[str(data.get("adw_id"))] = data
    if current is not None and _landed_branch({"ship": getattr(current, "ship", {}),
                                                 "delivery": getattr(current, "delivery", {})}) == branch:
        found.setdefault(str(current.adw_id), {"adw_id": current.adw_id, "ended": ""})
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(found.values(), key=lambda d: _when(d.get("ended")) or far)


def running(layer: Any, branch: str) -> str:
    """The id of a batch run of `branch` that is running now ("" when none)."""
    for data in _states(layer):
        definition = data.get("definition") if isinstance(data.get("definition"), dict) else {}
        if data.get("adw") != ADW_NAME or str((definition.get("batch") or {}).get("branch") or "") != branch:
            continue
        if data.get("status") != "running":
            continue
        beat = _when(data.get("heartbeat"))
        if beat is not None and (datetime.now(timezone.utc) - beat).total_seconds() > state_module.HEARTBEAT_SECONDS * 12:
            continue  # its process died long ago
        return str(data.get("adw_id"))
    return ""


def _cycle_closed_since(layer: Any, branch: str, since: str) -> bool:
    try:
        cycle = delivery.load_cycle(layer) or {}
    except LayerError:
        return False
    after = _when(since) if since else None
    for item in cycle.get("history") or []:
        closed = _when(item.get("closed_at"))
        if str(item.get("branch") or "") == branch and closed is not None and (after is None or closed > after):
            return True
    return False


def due(layer: Any, branch: str, current: Any = None) -> tuple[bool, str]:
    """(due, why) for a batch of `branch` (see the module docstring)."""
    pol = testing.policy(layer)
    other = running(layer, branch)
    if other:
        return False, f"batch {other} of {branch} is running"
    previous = last(layer, branch)
    since = str((previous or {}).get("at") or "")
    landed = landed_since(layer, branch, since, current, (previous or {}).get("delivered"))
    n = len(landed)
    every, hours = int(pol.get("batchEvery") or 0), int(pol.get("batchAfterHours") or 0)
    if every and n >= every:
        return True, f"{n} changes landed on {branch} since the last batch (every {every})"
    if hours and n:
        when = _when(since)
        if when is None:
            return True, f"{n} change{'' if n == 1 else 's'} landed on {branch} and it has had no batch yet"
        age = (datetime.now(timezone.utc) - when).total_seconds() / 3600
        if age >= hours:
            return True, f"a change landed on {branch} and the last batch is {age:.0f} h old (after {hours} h)"
    if _cycle_closed_since(layer, branch, since):
        return True, f"the cycle of {branch} closed since the last batch"
    if not pol.get("declared"):
        return False, "the layer declares no (testing …): every run is checked at full depth"
    parts = []
    if every:
        parts.append(f"{n} of {every} changes landed")
    if hours:
        parts.append(f"no batch older than {hours} h with a change landed" if n else "no change landed since the last batch")
    return False, "; ".join(parts) or "no batch rhythm declared (#:batch)"


# ---------------------------------------------------------------- starting a batch

def _spawn(argv: list[str], **popen: Any) -> subprocess.Popen:
    return subprocess.Popen(argv, **popen)


def run(layer_dir: Path, stage: str | None = None, *, if_due: bool = False, detach: bool = False,
        as_json: bool = False, adw_id: str = "", spawn: Any = None, current: Any = None) -> int:
    """Start a batch of `stage`'s branch (or the default branch); returns the run's exit code (0
    green, 1 red) or 0 for a detached start and a batch that is not due."""
    from . import runner

    spawn = spawn or _spawn
    layer_dir = Path(layer_dir)
    layer = ir.load(layer_dir)
    branch, stage_name = target(layer, stage or None)
    if if_due:
        ok, why = due(layer, branch, current)
        if not ok:
            print(json.dumps({"status": "not-due", "branch": branch, "why": why}) if as_json
                  else f"batch: not due ({why})")
            return 0
    other = running(layer, branch)
    if other and not adw_id:
        message = f"batch {other} of {branch} is running"
        print(json.dumps({"status": "error", "reason": "already-running", "error": message, "run_id": other})
              if as_json else f"batch: {message}")
        return 1
    definition = batch_adw(layer, branch, stage_name)
    prompt = f"verify the full suites on {branch}"
    if not adw_id:
        adw_id = state_module.new_adw_id()
        while state_module.run_dir(layer_dir, adw_id).exists():
            adw_id = state_module.new_adw_id()
    if detach:
        run_dir = state_module.run_dir(layer_dir, adw_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # what a reader (and `running`) sees until the run writes its own state
        skeleton = state_module.ADWState(adw_id=adw_id, adw=ADW_NAME, prompt=prompt, source="cli",
                                         definition=definition)
        skeleton.host, skeleton.heartbeat = "", state_module.now()
        skeleton.save(layer_dir)
        argv = [sys.executable, str(layer_dir / "adws" / "verify_batch.py"), "run", "--adw-id", adw_id,
                *(["--stage", stage_name] if stage_name else [])]
        with (run_dir / "launch.log").open("ab") as log:
            spawn(argv, cwd=str(layer_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                  start_new_session=True)
        result = {"status": "started", "run_id": adw_id, "branch": branch, "stage": stage_name}
        print(json.dumps(result) if as_json else f"started batch run {adw_id} on {branch}; log agents/{adw_id}/launch.log")
        return 0
    code = runner.main(ADW_NAME, layer_dir, [prompt, "--class", "chore", "--adw-id", adw_id], definition=definition)
    if as_json:
        saved = testing._run_state(layer, adw_id)
        print(json.dumps({"status": saved.get("status", ""), "run_id": adw_id, "exit": code,
                          "batch": (saved.get("batch") or {}).get("status", "")}))
    return code


def after_landing(layer: Any, state: Any) -> str:
    """After a run's ship step: start a batch of the branch it landed on when one is due. Never
    raises; returns the batch run's id ("" when none started)."""
    try:
        if not getattr(state, "ship", {}).get("shipped") or not testing.policy(layer).get("declared"):
            return ""
        stage = str((getattr(state, "delivery", None) or {}).get("stage") or "")
        branch = _landed_branch({"ship": state.ship, "delivery": getattr(state, "delivery", {})})
        ok, why = due(layer, branch, state)
        if not ok:
            print(f"[adw {state.adw_id}] batch: not due on {branch} ({why})")
            return ""
        adw_id = state_module.new_adw_id()
        run(Path(layer.layer_dir), stage or None, detach=True, adw_id=adw_id, spawn=_spawn)
        state.testing["batch_started"] = adw_id
        print(f"[adw {state.adw_id}] batch: started {adw_id} on {branch} ({why})")
        return adw_id
    except Exception as exc:  # noqa: BLE001 — a batch that cannot start never fails the shipped run
        print(f"[adw {getattr(state, 'adw_id', '?')}] batch: not started: {type(exc).__name__}: {exc}; "
              "run `al batch LAYER run --if-due` later")
        return ""


def after_cycle_closed(layer: Any, stage: str) -> str:
    """After `al cycle LAYER close`: a batch of the closed cycle's branch. Never raises."""
    try:
        if not testing.policy(layer).get("declared"):
            return ""
        cycle = delivery.load_cycle(layer) or {}
        closed = (cycle.get("history") or [{}])[-1]
        branch = str(closed.get("branch") or "")
        if not branch or running(layer, branch):
            return ""
        layer_dir = Path(layer.layer_dir)
        definition = batch_adw(layer, branch, stage)
        adw_id = state_module.new_adw_id()
        run_dir = state_module.run_dir(layer_dir, adw_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        skeleton = state_module.ADWState(adw_id=adw_id, adw=ADW_NAME, prompt=f"verify the full suites on {branch}",
                                         source="cli", definition=definition)
        skeleton.host, skeleton.heartbeat = "", state_module.now()
        skeleton.save(layer_dir)
        argv = [sys.executable, str(layer_dir / "adws" / "verify_batch.py"), "run", "--adw-id", adw_id,
                "--stage", stage]
        with (run_dir / "launch.log").open("ab") as log:
            _spawn(argv, cwd=str(layer_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                   start_new_session=True)
        print(f"batch: started {adw_id} on {branch} (the cycle closed)")
        return adw_id
    except Exception as exc:  # noqa: BLE001 — closing a cycle never fails because of a batch
        print(f"batch: not started after the cycle closed: {type(exc).__name__}: {exc}")
        return ""


def record_stage_result(layer: Any, state: Any, branch: str, head: str, results: Mapping[str, Any]) -> dict[str, Any]:
    """A promotion's checks on a stage head, as a batch ledger line (source promotion)."""
    depth = testing.on_stage(layer)
    stage = str((getattr(state, "promotion", None) or {}).get("from") or "")
    previous = last_green(layer, branch)
    checks = {str(cmd): {"exit": int(data.get("exit", 1)), "log": str(data.get("log") or ""),
                         **({"failing": list(data["failing"])} if data.get("failing") else {})}
              for cmd, data in results.items()}
    line = {"id": str(state.adw_id), "branch": branch, "stage": stage, "head": head,
            "since": str((previous or {}).get("head") or ""),
            "status": "green" if all(item["exit"] == 0 for item in checks.values()) else "red",
            "depth": depth, "checks": checks,
            "delivered": [str(c.get("id")) for c in (getattr(state, "promotion", None) or {}).get("changes") or []
                          if isinstance(c, dict) and c.get("id")],
            "at": _now(), "source": "promotion"}
    _append(layer, line)
    return line


# ---------------------------------------------------------------- the steps

class _Run:
    def __init__(self, layer: Any, state: Any, adw: Mapping[str, Any], env: Any) -> None:
        self.layer, self.state, self.adw, self.env = layer, state, dict(adw), env

    @property
    def b(self) -> dict[str, Any]:
        return self.state.batch

    @property
    def id(self) -> str:
        return str(self.state.adw_id)

    @property
    def sandbox(self) -> Any:
        return getattr(self.env, "sandbox", None)

    def say(self, line: str) -> None:
        print(f"[adw {self.id}] batch: {line}", flush=True)
        self.b.setdefault("lines", []).append(line)

    def git(self, args: list[str], step: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """git in the run's worktree (sandboxed: through gitsafe)."""
        return delivery.tree_git(self.state, self.env, args, check=check, step=step)

    def app_git(self, args: list[str], step: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """git in the application repository: only to create a ref. Never its working tree."""
        if self.sandbox is not None:
            return gitsafe.run_app(self.sandbox, args, step, check=check)
        result = delivery._git(Path(self.layer.app_root), args)
        if check and result.returncode != 0:
            raise StepFailed(step, f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
        return result

    def branch_head(self, branch: str, step: str) -> str:
        remote = delivery._remote(self.layer)
        if remote:
            fetched = self.git(["fetch", *(["--refmap="] if self.sandbox else []), remote, branch], step)
            if fetched.returncode == 0:
                return self.git(["rev-parse", "FETCH_HEAD"], step, check=True).stdout.strip()
        out = self.app_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], step)
        return out.stdout.strip() if out.returncode == 0 else ""

    def checkout(self, commit: str, step: str) -> None:
        self.git(["checkout", "-q", "--detach", commit], step, check=True)

    def skip(self, step: str, why: str) -> None:
        self.b.setdefault("skipped", {})[step] = why
        self.state.outputs[step] = f"skipped: {why}"


def _runs_of_commits(layer: Any, commits: list[str], messages: Mapping[str, str]) -> dict[str, str]:
    """commit → the run that delivered it: the run named in its message (or its merged branch's),
    else the run whose shipped commit or delivered head is this commit or a later one in the list."""
    known = {str(d.get("adw_id")) for d in _states(layer)}
    heads: dict[str, str] = {}
    for data in _states(layer):
        if data.get("adw") == ADW_NAME:
            continue
        for commit in ((data.get("ship") or {}).get("commit"), (data.get("delivery") or {}).get("head")):
            if commit:
                heads.setdefault(str(commit), str(data.get("adw_id")))
    out: dict[str, str] = {}
    pending: list[str] = []
    for commit in commits:
        named = ""
        for pattern in _MESSAGE_RUN:
            match = pattern.search(messages.get(commit, ""))
            if match and match.group(1) in known:
                named = match.group(1)
                break
        if named:
            out[commit] = named
        pending.append(commit)
        if commit in heads:
            for item in pending:
                out.setdefault(item, heads[commit])
            pending = []
    return out


def _messages(run: _Run, commits: list[str], step: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for commit in commits:
        text = run.git(["log", "-1", "--format=%B", commit], step).stdout
        parents = run.git(["rev-list", "--parents", "-n", "1", commit], step).stdout.split()
        if len(parents) > 2:  # a merge: the merged branch's tip names the run too
            text += "\n" + run.git(["log", "-1", "--format=%B", parents[2]], step).stdout
        out[commit] = text
    return out


def _open(run: _Run) -> None:
    layer, state = run.layer, run.state
    definition = run.adw.get("batch") or {}
    branch, stage = str(definition.get("branch") or ""), str(definition.get("stage") or "")
    head = run.branch_head(branch, "open")
    if not head:
        raise StepFailed("open", f"branch {branch} does not exist in the application repository")
    run.checkout(head, "open")
    green = last_green(layer, branch)
    since = str((green or {}).get("head") or "")
    if since and run.git(["merge-base", "--is-ancestor", since, head], "open").returncode != 0:
        since = ""  # the branch was rewritten since that batch
    default = str(layer.codebase_default_branch)
    if not since and branch != default:
        base_head = run.branch_head(default, "open")
        if base_head:
            since = run.git(["merge-base", head, base_head], "open").stdout.strip()
    span = [f"{since}..{head}"] if since else ["--max-count", str(MAX_COMMITS), head]
    commits = run.git(["rev-list", "--first-parent", "--reverse", *span], "open", check=True).stdout.split()
    owners = _runs_of_commits(layer, commits, _messages(run, commits, "open"))
    delivered = list(dict.fromkeys(owners[c] for c in commits if c in owners))
    state.batch = {"id": run.id, "branch": branch, "stage": stage, "head": head, "since": since,
                   "commits": commits, "owners": owners, "delivered": delivered,
                   "depth": testing.on_stage(layer), "opened_at": _now()}
    events.emit("batch.open", branch=branch, stage=stage, head=head, since=since, commits=len(commits),
                delivered=delivered)
    run.say(f"{branch} at {head[:7]}: {len(commits)} commit(s) since "
            + (since[:7] if since else "the start of the history looked at")
            + (f", delivered by {', '.join(delivered)}" if delivered else ""))
    state.outputs["open"] = f"{branch} at {head[:7]}, {len(delivered)} run(s) since {since[:7] or '-'}"


def _checks(run: _Run) -> None:
    from . import steps

    layer, state = run.layer, run.state
    depth = testing.on_stage(layer)
    results: dict[str, Any] = {}
    for cmd in check_cmds(layer, str(run.b.get("stage") or "")):
        name = f"batch-{cmd}"
        code = steps.run_check(layer, state, {"name": name, "kind": "check", "cmd": cmd, "depth": depth}, run.env)
        record = state.checks.get(name) or {}
        log_text = ""
        with contextlib.suppress(OSError):
            log_text = (Path(layer.layer_dir) / str(record.get("log") or "")).read_text(encoding="utf-8", errors="replace")
        acc = record.get("testing") or {}
        results[cmd] = {"exit": int(code), "log": str(record.get("log") or ""), "depth": str(acc.get("depth") or depth),
                        "seconds": acc.get("seconds"), "failing": skips.failing_tests(log_text) if code else []}
    run.b["checks"] = results
    run.b["status"] = "green" if all(item["exit"] == 0 for item in results.values()) else "red"
    shown = ", ".join(f"{cmd}={data['exit']}" for cmd, data in results.items()) or "none declared"
    run.say(f"checks at {depth} depth on {str(run.b.get('head'))[:7]}: {shown} — {run.b['status']}")
    state.outputs["checks"] = f"{run.b['status']}: {shown}"


def _line(run: _Run) -> dict[str, Any]:
    b = run.b
    checks = {cmd: {key: value for key, value in data.items() if value not in (None, [], "")}
              for cmd, data in (b.get("checks") or {}).items()}
    line = {"id": run.id, "branch": b.get("branch"), "stage": b.get("stage"), "head": b.get("head"),
            "since": b.get("since"), "status": b.get("status", "red"), "depth": b.get("depth"),
            "checks": checks, "delivered": list(b.get("delivered") or []), "at": _now(), "source": "batch"}
    for key in ("culprit", "fix_run", "revert"):
        if b.get(key):
            line[key] = b[key]
    return line


def _record(run: _Run) -> None:
    _append(run.layer, _line(run))
    run.state.outputs["record"] = f"{run.b.get('status')} batch recorded in {LEDGER.as_posix()}"


def _test_units(failing: list[str]) -> list[str]:
    """Test files (or packages, names) from failing test ids (`a/b.py::t` → `a/b.py`)."""
    return list(dict.fromkeys(item.split("::", 1)[0] for item in failing if item.split("::", 1)[0]))


def _narrow(layer: Any, cmd_name: str, units: list[str]) -> tuple[dict[str, Any], str]:
    """The cmd with only `units` to run: its #:targeted form filled with them, else its full form."""
    cmd = dict(layer.cmd(cmd_name))
    form = str(cmd.get("targeted") or "")
    if not form or not units:
        return cmd, ("its full form (no #:targeted form)" if not form else "its full form (no failing test was named)")
    chosen = selection.Selection(tests=list(units), packages=list(units), files=[])
    return {**cmd, "run": selection.fill(form, chosen)}, "only " + ", ".join(units)


def _fails(run: _Run, cmd_name: str, command: dict[str, Any], commit: str) -> bool:
    from . import steps

    run.checkout(commit, "bisect")
    plan = testing.CheckPlan("targeted", "full", command, "bisect: the failing tests only", None, "tests", "bisect")
    name = f"bisect-{cmd_name}-{commit[:7]}"
    return steps.run_check(run.layer, run.state, {"name": name, "kind": "check", "cmd": cmd_name}, run.env, plan=plan) != 0


def _culprit_check(layer: Any, run_id: str, cmd: str) -> dict[str, Any] | None:
    """The culprit run's last check of `cmd` when it passed below full depth (the miss), else None."""
    entries = testing._run_state(layer, run_id).get("check_results") or []
    for entry in reversed(entries):  # the newest result that ran (a reused or not-affected one ran nothing)
        if str(entry.get("check") or "") == cmd and entry.get("outcome") in ("passed", "failed"):
            return entry if testing._below_full_pass(entry) else None
    return None


def _changed_files(run: _Run, commit: str, step: str) -> list[str]:
    parents = run.git(["rev-list", "--parents", "-n", "1", commit], step).stdout.split()
    if len(parents) < 2:
        return run.git(["show", "--name-only", "--format=", commit], step).stdout.split()
    return run.git(["diff", "--name-only", parents[1], commit], step).stdout.split()


def _bisect(run: _Run) -> None:
    layer, b = run.layer, run.b
    if b.get("status") != "red":
        run.skip("bisect", "the batch is green")
        return
    commits = list(b.get("commits") or [])
    head = str(b.get("head"))
    culprits: dict[str, Any] = {}
    for cmd, data in (b.get("checks") or {}).items():
        if not data.get("exit"):
            continue
        units = _test_units(list(data.get("failing") or []))
        command, what = _narrow(layer, cmd, units)
        run.say(f"bisect {cmd}: {len(commits)} commit(s), running {what} per step")
        if not commits:
            culprits[cmd] = {"culprit": "", "why": "no commit since the last green batch"}
            continue
        if not _fails(run, cmd, command, head):
            culprits[cmd] = {"culprit": "", "why": "the failing tests pass when run alone at the head (flaky?)"}
            run.say(f"bisect {cmd}: the failing tests pass alone at {head[:7]}; no culprit, no miss counted")
            continue
        lo, hi = 0, len(commits) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if _fails(run, cmd, command, commits[mid]):
                hi = mid
            else:
                lo = mid + 1
        commit = commits[lo]
        owner = str((b.get("owners") or {}).get(commit) or "")
        files = _changed_files(run, commit, "bisect")
        found = {"commit": commit, "culprit": owner or f"commit {commit[:12]} (no run)", "run": owner,
                 "tests": list(data.get("failing") or []), "files": files}
        entry = _culprit_check(layer, owner, cmd) if owner else None
        if entry is not None:
            # the strategies the run relied on: those it tried (#:select, changed-tests first) and any
            # that found a test (learned); a miss counts against each of them
            tried = ["changed-tests", *[str(x) for x in layer.cmd(cmd).get("select") or selection.DEFAULT_SELECT]]
            strategies = list(dict.fromkeys([*tried, *((entry.get("testing") or {}).get("strategies") or {}).keys()]))
            root = str(layer.cmd(cmd).get("testRoot") or "").strip("/")
            tests = [f"{root}/{unit}" if root and unit != "." else unit for unit in units]
            testing.record_miss(layer, batch=run.id, run=owner, cmd=cmd, strategies=strategies,
                                tests=list(data.get("failing") or []), files=files)
            testing.learn(layer, cmd, files, tests, run.id)
            found["miss"] = {"depth": entry.get("depth") or (entry.get("testing") or {}).get("depth"),
                             "strategies": strategies}
            run.say(f"bisect {cmd}: run {owner} broke it at {commit[:7]}; its check passed at "
                    f"{found['miss']['depth']} depth: a miss, learned {', '.join(files)} → {', '.join(tests)}")
        else:
            run.say(f"bisect {cmd}: {found['culprit']} broke it at {commit[:7]}")
        culprits[cmd] = found
    run.checkout(head, "bisect")
    b["culprits"] = culprits
    first = next((item for item in culprits.values() if item.get("culprit")), None)
    if first:
        b["culprit"] = first["run"] or first["culprit"]
        b["culprit_commit"] = first["commit"]
    events.emit("batch.bisect", culprits={cmd: item.get("culprit", "") for cmd, item in culprits.items()})
    run.state.outputs["bisect"] = (f"culprit {b['culprit']}" if b.get("culprit")
                                   else "no culprit: " + "; ".join(i.get("why", "") for i in culprits.values()))


def _fix_adw(layer: Any) -> str:
    wanted = str(testing.policy(layer).get("fixAdw") or "")
    if wanted:
        return wanted
    for adw in layer.ir.get("adws") or []:
        if (isinstance(adw, dict) and not adw.get("promotion") and not adw.get("integration") and not adw.get("batch")
                and "bug" in (adw.get("classes") or [])):
            return str(adw["name"])
    return ""


def _agents_may_merge(layer: Any, stage: str) -> bool:
    if stage:
        with contextlib.suppress(LayerError):
            return delivery.stage_named(layer, stage).agents_may_merge
        return False
    return not delivery.stages(layer)


def _tail(layer: Any, log: str) -> str:
    with contextlib.suppress(OSError):
        lines = (Path(layer.layer_dir) / log).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-LOG_TAIL:])
    return "(the log could not be read)"


def _prepare_revert(run: _Run) -> str:
    """refs/heads/al/revert/<id>: the head with the culprit's commit reverted ("" when it cannot be)."""
    commit = str(run.b.get("culprit_commit") or "")
    if not commit:
        return ""
    run.checkout(str(run.b["head"]), "fix")
    parents = run.git(["rev-list", "--parents", "-n", "1", commit], "fix").stdout.split()
    reverted = run.git([*IDENTITY, "revert", "--no-edit", *(["-m", "1"] if len(parents) > 2 else []), commit], "fix")
    if reverted.returncode != 0:
        run.git(["revert", "--abort"], "fix")
        run.say(f"revert of {commit[:7]} does not apply cleanly: no revert branch prepared")
        return ""
    sha = run.git(["rev-parse", "HEAD"], "fix", check=True).stdout.strip()
    ref = f"{REVERT_PREFIX}{run.id}"
    if run.sandbox is not None:
        sha = gitsafe.import_objects(run.sandbox, sha, "fix")
    created = run.app_git(["update-ref", "-m", f"batch {run.id}: revert {commit[:12]}", ref, sha, ""], "fix")
    run.checkout(str(run.b["head"]), "fix")
    if created.returncode != 0:  # it exists: never moved
        run.say(f"{ref} exists already; it is left as it is")
        return ""
    return ref.removeprefix("refs/heads/")


def _fix_prompt(run: _Run, revert: str) -> str:
    layer, b = run.layer, run.b
    lines = [f"The full suites failed on {b.get('branch')} at {str(b.get('head'))[:12]} (batch {run.id}).", ""]
    for cmd, data in (b.get("checks") or {}).items():
        if not data.get("exit"):
            continue
        lines += [f"## {cmd} failed (exit {data['exit']})", "",
                  "Failing tests:", *[f"- {t}" for t in data.get("failing") or ["(not named in the log)"]], "",
                  f"Last {LOG_TAIL} lines of {data.get('log')}:", "", "```", _tail(layer, str(data.get("log") or "")),
                  "```", ""]
    culprit = str(b.get("culprit") or "")
    if culprit:
        lines.append(f"The change that broke it: {culprit} (commit {str(b.get('culprit_commit') or '')[:12]}).")
        if RUN_ID.match(culprit):
            data = testing._run_state(layer, culprit)
            lines += [f"Its request: {str(data.get('prompt') or '').strip()[:2000]}",
                      f"Its plan: {data.get('plan') or '(none)'}"]
    else:
        lines.append("Bisect named no culprit run: " + "; ".join(
            str(item.get("why") or item.get("culprit") or "") for item in (b.get("culprits") or {}).values()))
    lines.append("")
    if revert and _agents_may_merge(layer, str(b.get("stage") or "")):
        lines.append(f"A revert of that change is prepared on branch {revert}: revert it (take that branch) "
                     "unless the fix is small and certain, and keep the failing tests passing.")
    else:
        lines.append("Fix the cause so the failing tests pass again; do not weaken or skip a test.")
    return "\n".join(lines) + "\n"


def _fix(run: _Run) -> None:
    from . import launch

    layer, b = run.layer, run.b
    if b.get("status") != "red":
        run.skip("fix", "the batch is green")
        return
    failure = list(testing.policy(layer).get("onBatchFailure") or [])
    revert = _prepare_revert(run) if "revert" in failure else ""
    if revert:
        b["revert"] = revert
        if not _agents_may_merge(layer, str(b.get("stage") or "")):
            run.say(f"revert prepared on {revert}: a person decides whether to take it")
    if "open-fix" in failure:
        name = _fix_adw(layer)
        if not name:
            run.say("no workflow accepts class bug: no fix run started; a person fixes it")
        else:
            result = launch.start(layer, launch.Target("workflow", name), _fix_prompt(run, revert),
                                  request_key=f"batch-fix-{run.id}", klass="bug", confirm_ships=True,
                                  detach=True, spawn=lambda *a, **k: _spawn(*a, **k))
            if result.get("status") == "started":
                b["fix_run"] = str(result.get("run_id"))
                run.say(f"fix run {b['fix_run']} started ({name})")
            else:
                b["fix_error"] = str(result.get("error") or result.get("message") or result.get("status"))
                run.say(f"fix run not started: {b['fix_error']}")
    run.state.outputs["fix"] = (f"fix run {b.get('fix_run')}" if b.get("fix_run") else "no fix run") + (
        f"; revert on {revert}" if revert else "")


def _close(run: _Run) -> None:
    layer, b = run.layer, run.b
    line = _line(run)
    _append(layer, line)
    events.emit("batch.result", **{k: line[k] for k in ("id", "branch", "stage", "head", "status", "depth")},
                culprit=b.get("culprit", ""), fix_run=b.get("fix_run", ""), delivered=line["delivered"])
    if b.get("status") == "green":
        run.state.outputs["close"] = f"green: the full suites pass on {b.get('branch')} at {str(b.get('head'))[:7]}"
        return
    failing = [t for data in (b.get("checks") or {}).values() for t in data.get("failing") or []]
    failed = [cmd for cmd, data in (b.get("checks") or {}).items() if data.get("exit")]
    why = (f"red: {', '.join(failing[:5]) or ', '.join(failed)} failed"
           + (f"; culprit {b['culprit']}" if b.get("culprit") else "; no culprit found")
           + (f"; fix run {b['fix_run']}" if b.get("fix_run") else ""))
    raise StepFailed("close", why)


ACTIONS = {"open": _open, "checks": _checks, "record": _record, "bisect": _bisect, "fix": _fix, "close": _close}


def run_step(layer: Any, state: Any, adw: Mapping[str, Any], step: Mapping[str, Any], env_stack: Any) -> None:
    """One step of a batch run (steps._execute_step dispatches kind `batch` here)."""
    action = str(step.get("action") or step.get("name"))
    if action not in ACTIONS:
        raise LayerError(f"unknown batch step {action!r}; steps are {', '.join(STEPS)}")
    if not isinstance(getattr(state, "batch", None), dict):
        state.batch = {}
    try:
        ACTIONS[action](_Run(layer, state, adw, env_stack))
    finally:
        state.save(layer.layer_dir)


def skipped(state: Any, name: str) -> str:
    """Why a batch step was skipped ("" when it ran): bisect and fix on a green batch."""
    data = getattr(state, "batch", None)
    return str(((data or {}).get("skipped") or {}).get(name) or "") if isinstance(data, dict) else ""


# ---------------------------------------------------------------- status and entry point

def status(layer: Any) -> list[dict[str, Any]]:
    """Per branch: the last batch, the runs landed since, whether the next one is due and why."""
    out = []
    for branch, stage in targets(layer):
        previous = last(layer, branch)
        landed = landed_since(layer, branch, str((previous or {}).get("at") or ""), None,
                              (previous or {}).get("delivered"))
        ok, why = due(layer, branch)
        out.append({"branch": branch, "stage": stage, "last": previous, "running": running(layer, branch),
                    "delivered_since": [str(d.get("adw_id")) for d in landed], "due": ok, "why": why})
    return out


def _status_text(items: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in items:
        where = f"{item['branch']}" + (f" (stage {item['stage']})" if item["stage"] else "")
        previous = item["last"]
        if previous:
            text = (f"last batch {previous.get('id')} {previous.get('status')} at {str(previous.get('head') or '')[:7]} "
                    f"({str(previous.get('at') or '')[:16]})")
            if previous.get("culprit"):
                text += f"; broken by {previous['culprit']}"
            if previous.get("fix_run"):
                text += f"; fix run {previous['fix_run']}"
        else:
            text = "no batch yet"
        lines.append(f"{where}: {text}")
        lines.append(f"  delivered since: {', '.join(item['delivered_since']) or 'none'}")
        lines.append(f"  next: {'due now: ' if item['due'] else 'not due: '}{item['why']}"
                     + (f"; batch {item['running']} is running" if item["running"] and "running" not in item["why"] else ""))
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify_batch", description="The full suites per batch (kit workflow verify-batch).")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("run", help="run the full suites on a stage head or the default branch")
    start.add_argument("--stage", default="")
    start.add_argument("--if-due", action="store_true", help="only when a batch is due")
    start.add_argument("--detach", action="store_true")
    start.add_argument("--json", action="store_true")
    start.add_argument("--adw-id", default="")
    shown = sub.add_parser("status", help="per branch: the last batch, what landed since, when the next is due")
    shown.add_argument("--json", action="store_true")
    return parser


def main(layer_dir: Path, argv: list[str] | None = None) -> int:
    """`adws/verify_batch.py run|status …` (and `al batch LAYER …`)."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if args.command == "run":
            return run(Path(layer_dir), args.stage or None, if_due=args.if_due, detach=args.detach,
                       as_json=args.json, adw_id=args.adw_id)
        items = status(ir.load(Path(layer_dir)))
        print(json.dumps(items) if args.json else "\n".join(_status_text(items)))
        return 0
    except (LayerError, delivery.DeliveryError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "reason": "error", "error": str(exc)}))
        print(f"batch: error: {exc}", file=sys.stderr)
        return 1
