"""Integration of a handed-over run: an agent brings it onto its target branch.

A run whose review passed but whose ship step could not merge (the target branch moved, or the
merge conflicts) ends `handed-over`. `al integrate LAYER RUN` (the layer's `adws/integrate.py run
RUN`, and the dashboard's "Resolve with an agent") starts an integration run: an ordinary run of a
synthetic ADW named `integrate` (`integration_adw`, like a promotion run) built from the original
run's workflow, so it has the run log, events, heartbeat, slots, summary, waiting/resume and
dashboard page of every run. Its steps:

    prepare   a fresh worktree at the head of the target branch; the run's branch applied with
              `git merge --no-commit --squash` and three-way (diff3) conflict markers; the conflicted
              files, which are generated, and who touched each one since the run's base
    resolve   the kit prompt `resolve-conflicts` on the heavy tier (no agent call when the branch
              applies cleanly); it reports every conflict and its resolution, regenerates generated
              files, and stops when a conflict is a real disagreement
    <checks>  the check steps of the original run's workflow, as affected checks
    review    that workflow's review step, on the integration brief (specs/integrate-<id>.md)
    approve   only with "approval": "person": waits (exit 75) for `al integrate LAYER RUN --approve`
    ship      that workflow's ship step: the unchanged gate and ship

Review and ship are used exactly as they are. Nothing is ever force-pushed and the original run's
branch is never changed: the work is applied in a worktree of its own.

Modes. The above is mode `conflict` (`al integrate`). "Give to an agent" (assign.py) also continues
a run whose review rejected it, the same way, in a mode with up to two more steps of kind `integrate`:

    blockers  prepare, resolve, address, <checks>, review, ship. `address` is the review's patch
              round: the review step's own `patch` and `implement` commands, with the blockers, a
              person's approvals and their note as the request.
    proof     prepare, resolve, (address, only when the proof must be produced), proof, <checks>,
              review, ship. `proof` hands the review the proof that was not collected (the files
              found on the branch as `found-*` artefacts, or the layer's artefacts collected again);
              no application code changes.

In both the review runs ONE round with no further patch round (`review_retries` 0 for this run,
see with_review_retries): the layer's rounds were used in the original run. The review runs again
and the unchanged gate decides, so a rejected review never ships.

Settings are a file of the layer, `templates/integrate.json` (committed layer source, never written
by `al gen`): {"version": 1, "enabled": true, "approval": "none" | "person", "decider": false,
"generated": [{"path": "<path or glob>", "regenerate": "<shell command>"}]}.

Records:
    agents/<integration>/state.json   "integration": {run, branch, tip, target, target_head, base,
                                      workflow, by, clean, conflicts, report, seen_before, brief,
                                      approval, result, why; in the modes blockers and proof also
                                      mode, source, blockers, approved, note, found, address, proof}
    agents/<original>/integration.json  {run, status: queued | running | waiting | landed | failed,
                                        why, commit, target, by, at, previous}
    agents/<original>/settlement.json   written when the integration landed, in the dashboard's
                                        format: {as: landed, by: <integration run>, via: integrate, …}

One integration of a layer runs at a time (agents/.integrate.lock, an flock that dies with the
process): a second request waits, and because its worktree is created only inside the runner it
starts at the target head that already holds the first one's result.

Refusals are classified (REASONS) and printed as `integrate:<reason>: <words>`.

Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import fcntl
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import affected, delivery, events, gitsafe, ir, slots, state as state_module
from .errors import LayerError, StepFailed, StepWaiting

ADW_NAME = "integrate"
SETTINGS_FILE = Path("templates") / "integrate.json"
SETTINGS_SHOWN = SETTINGS_FILE.as_posix()
DEFAULTS: dict[str, Any] = {"version": 1, "enabled": True, "approval": "none", "decider": False, "generated": []}
APPROVALS = ("none", "person")
SETTABLE = ("enabled", "approval", "decider")          # what `al integrate LAYER --set KEY=VALUE` may change
REASONS = ("off", "unknown-run", "not-handed-over", "not-a-conflict", "review-rejected", "protected-path",
           "branch-changed", "branch-missing", "settled", "landed", "already-integrating", "layer-change",
           "no-ship-step", "name-taken", "undecidable", "markers-remain", "head-moved", "not-waiting",
           "not-rejected", "no-patch-round")
MODES = ("conflict", "blockers", "proof")
# what assign.give hands an integration run of the modes blockers and proof (kept in the ADW's "integration")
EXTRA_KEYS = ("instruction", "approved", "note", "found", "produce", "source", "allow_protected")
FOUND_KINDS = (("found-image", "image", (".png", ".jpg", ".jpeg", ".gif", ".webp")),
               ("found-video", "video", (".webm", ".mp4", ".mov")))
FOUND_FILE = ("found-file", "file")
GENERATED_MARKERS = ("@generated", "do not edit", "generated by")   # compared in lower case
MARKER_LINES = 5
RECORD_FILE = "integration.json"
SETTLEMENT_FILE = "settlement.json"
APPROVAL_FILE = "approval.json"
REQUEST_FILE = "request.md"
LAUNCH_LOG = "launch.log"
LOCK_FILE = Path("agents") / ".integrate.lock"
SCRIPT = Path("adws") / "integrate.py"
COMMAND_NAME = "resolve-conflicts"
# the command record of the resolve step: the IR does not declare it (the runtime holds it, as
# decide.py holds the decider's); a layer that declares a command of this name wins
COMMAND = {"name": COMMAND_NAME, "template": COMMAND_NAME, "output": "text", "modelSet": "heavy", "isHop": False}
STEP_NAMES = ("prepare", "resolve", "approve", "address", "proof")
WAITING_EXIT = 75
REFUSED_EXIT = 3
MAX_TOUCHED = 10
MAX_PREVIOUS = 5
MAX_WHO = 80
GIT_TIMEOUT = 300
IDENTITY = ["-c", "user.name=adw", "-c", "user.email=adw@localhost"]   # git asks for one even when nothing is committed
QUEUE_POLL = 0.5
LAUNCH_SECONDS = 30.0       # a detached integration without a process yet counts as running this long
STALE_SECONDS = 120.0       # a process whose state.json was not refreshed for this long is not counted
CONFLICT_MARKER = re.compile(r"^<{7}(?: |$)", re.MULTILINE)    # a marker line, not the characters in a sentence
RUN_ID = re.compile(r"^[0-9a-f]{8}$")
RUN_TAG = re.compile(r"\[adw ([0-9a-f]{8})\]\s*$")
OLD_GIT_NOTE = "git older than 2.38: the conflicted files are known once it starts"
ADVICE = "a file that conflicts often: split it, or mark it generated in templates/integrate.json"
_STARTED_BY: dict[str, str] = {}    # integration run id -> who started it (cli | dashboard | decider), this process


class IntegrateError(LayerError):
    """An integration cannot start or go on, for a classified reason (see REASONS)."""

    def __init__(self, reason: str, message: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown integrate reason {reason!r}")
        self.reason = reason
        self.message = message
        super().__init__(f"integrate:{reason}: {message}")


class _Usage(Exception):
    """A wrong use of the command line (exit 2)."""


@dataclass
class Original:
    """The handed-over run an integration brings onto its target."""
    id: str
    state: dict[str, Any]
    adw: dict[str, Any]
    branch: str
    tip: str
    target: str
    klass: str = "chore"
    title: str = ""
    reasons: list[str] = field(default_factory=list)
    target_head: str = ""
    mode: str = "conflict"
    source: str = ""                 # the earlier attempt whose branch and review are used ("" = the run itself)
    blockers: list[dict[str, Any]] = field(default_factory=list)


def first_line(text: Any) -> str:
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- settings

def _settings_error(layer_dir: Path, what: str) -> LayerError:
    del layer_dir
    return LayerError(f"{SETTINGS_SHOWN}: {what}; fix it or remove it (al integrate LAYER --reset)")


def check_settings(layer_dir: Path, data: Any) -> dict[str, Any]:
    """The settings of a parsed file, defaults filled in; LayerError naming the file otherwise."""
    if not isinstance(data, dict):
        raise _settings_error(layer_dir, "expected a JSON object")
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise _settings_error(layer_dir, f"unknown key {', '.join(unknown)} (known: {', '.join(DEFAULTS)})")
    out = copy.deepcopy(DEFAULTS)
    out.update(copy.deepcopy(data))
    if out["version"] != 1 or isinstance(out["version"], bool):
        raise _settings_error(layer_dir, f'"version" must be 1, got {out["version"]!r}')
    for key in ("enabled", "decider"):
        if not isinstance(out[key], bool):
            raise _settings_error(layer_dir, f'"{key}" must be true or false, got {out[key]!r}')
    if out["approval"] not in APPROVALS:
        raise _settings_error(layer_dir, f'"approval" is one of {", ".join(APPROVALS)}, got {out["approval"]!r}')
    if not isinstance(out["generated"], list):
        raise _settings_error(layer_dir, '"generated" must be a list of {"path", "regenerate"}')
    for entry in out["generated"]:
        if (not isinstance(entry, dict) or set(entry) - {"path", "regenerate"} or not isinstance(entry.get("path"), str)
                or not entry["path"].strip() or not isinstance(entry.get("regenerate", ""), str)):
            raise _settings_error(layer_dir, f'"generated" entries are {{"path": "<path or glob>", "regenerate": '
                                             f'"<shell command>"}}, got {entry!r}')
        entry.setdefault("regenerate", "")
    return out


def settings(layer_dir: Path) -> dict[str, Any]:
    """templates/integrate.json of the layer; the defaults when there is none. A broken file, an
    unknown key or a wrong type raises LayerError naming the file."""
    path = Path(layer_dir) / SETTINGS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except OSError as exc:
        raise _settings_error(Path(layer_dir), f"cannot read it ({exc.strerror})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _settings_error(Path(layer_dir), f"not JSON ({exc.msg})") from exc
    return check_settings(Path(layer_dir), data)


def write_settings(layer_dir: Path, changes: Mapping[str, Any] | None) -> Path:
    """Change keys of templates/integrate.json (written atomically); None removes the file."""
    path = Path(layer_dir) / SETTINGS_FILE
    if changes is None:
        path.unlink(missing_ok=True)
        return path
    merged = check_settings(Path(layer_dir), {**settings(layer_dir), **dict(changes)})
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".integrate.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
            fh.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path


def settings_lines(data: Mapping[str, Any]) -> list[str]:
    generated = ", ".join(f"{entry['path']}" + (f" ({entry['regenerate']})" if entry.get("regenerate") else "")
                          for entry in data.get("generated") or []) or "none named"
    return [f"integrate: {'enabled' if data.get('enabled') else 'switched off'} ({SETTINGS_SHOWN})",
            f"approval:  {'a person approves before it ships' if data.get('approval') == 'person' else 'none'}",
            f"decider:   {'may start it (mode auto)' if data.get('decider') else 'may not start it'}",
            f"generated: {generated}"]


def is_generated(text: str) -> bool:
    """True when one of the first five lines carries a generated marker (@generated, DO NOT EDIT,
    Generated by; any case)."""
    head = "\n".join(str(text or "").splitlines()[:MARKER_LINES]).lower()
    return any(marker in head for marker in GENERATED_MARKERS)


def _generated_entry(data: Mapping[str, Any], path: str) -> dict[str, Any] | None:
    for entry in data.get("generated") or []:
        if affected.matches(path, [str(entry.get("path") or "")]):
            return dict(entry)
    return None


# ---------------------------------------------------------------- git in the application, records

def _app_git(app: Path, args: list[str], env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(app), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                              check=False, env=dict(env) if env is not None else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LayerError(f"git {' '.join(args)} failed: {exc}") from exc


def _ref(app: Path, ref: str) -> str:
    out = _app_git(app, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def _is_ancestor(app: Path, commit: str, ref: str) -> bool:
    return bool(commit and ref) and _app_git(app, ["merge-base", "--is-ancestor", commit, ref]).returncode == 0


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _state(layer: Any, run_id: str) -> dict[str, Any]:
    data = _read_json(state_module.run_dir(layer.layer_dir, run_id) / "state.json")
    return data if isinstance(data, dict) else {}


def _write_private(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically: a temporary file beside it (mode 0600), then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def read_record(layer: Any, run_id: str) -> dict[str, Any] | None:
    """agents/<original>/integration.json, or None."""
    data = _read_json(state_module.run_dir(layer.layer_dir, run_id) / RECORD_FILE)
    return data if isinstance(data, dict) and data.get("run") else None


def write_record(layer: Any, run_id: str, *, run: str, status: str, why: str = "", commit: str = "",
                 target: str = "", by: str = "") -> dict[str, Any]:
    """Write agents/<original>/integration.json. An update of the same integration replaces its
    record; another integration's record moves under "previous" (newest first, at most 5)."""
    earlier = read_record(layer, run_id) or {}
    previous = [dict(item) for item in earlier.get("previous") or [] if isinstance(item, dict)]
    if earlier and earlier.get("run") != run:
        previous.insert(0, {k: v for k, v in earlier.items() if k != "previous"})
    same = earlier if earlier.get("run") == run else {}
    record = {"run": run, "status": status, "why": first_line(why)[:500], "commit": commit,
              "target": target or str(same.get("target") or ""), "by": by or str(same.get("by") or ""),
              "at": _stamp(), "previous": previous[:MAX_PREVIOUS]}
    _write_private(state_module.run_dir(layer.layer_dir, run_id) / RECORD_FILE, record)
    return record


def write_settlement(layer: Any, run_id: str, integration_id: str, commit: str, target: str) -> dict[str, Any]:
    """agents/<original>/settlement.json for a run an integration landed. The format is the
    dashboard's (kit/dashboard/dashboard.py write_settlement; keep the two alike): {as, by, note,
    who, at, via}, the earlier record kept under "previous" (one level), "pruned" carried over."""
    path = state_module.run_dir(layer.layer_dir, run_id) / SETTLEMENT_FILE
    record: dict[str, Any] = {"as": "landed", "by": integration_id,
                              "note": f"landed by integration run {integration_id} in {commit[:7]} on {target}",
                              "who": "al integrate", "at": _stamp(), "via": "integrate"}
    earlier = _read_json(path)
    if isinstance(earlier, dict) and earlier.get("as"):
        record["previous"] = {k: v for k, v in earlier.items() if k not in ("previous", "pruned")}
        if earlier.get("pruned"):
            record["pruned"] = earlier["pruned"]
    _write_private(path, record)
    return record


# ---------------------------------------------------------------- which runs are being integrated

def _lock_holder(layer: Any) -> str:
    """The id of the integration run that holds the layer's lock now, "" when nobody does."""
    path = Path(layer.layer_dir) / LOCK_FILE
    try:
        handle = os.open(path, os.O_RDWR)
    except OSError:
        return ""
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            words = os.read(handle, 200).decode("utf-8", "replace").split()
            return words[1] if len(words) > 1 else "?"
        fcntl.flock(handle, fcntl.LOCK_UN)
        return ""
    finally:
        os.close(handle)


def _age(stamp: Any) -> float:
    try:
        return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")))
                   .total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running(layer: Any, run_id: str) -> bool:
    """True while integration run `run_id` is queued or running: its state.json says running and
    it holds the lock, or its process is alive and seen lately, or it was launched a moment ago."""
    data = _state(layer, run_id)
    if data.get("status") != "running":
        return False
    if _lock_holder(layer) == run_id:
        return True
    age = _age(data.get("heartbeat") or data.get("started"))
    pid = int(data.get("pid") or 0)
    if pid <= 0:
        return age < LAUNCH_SECONDS
    here = not data.get("host") or data.get("host") == socket.gethostname()
    return here and _pid_alive(pid) and age < STALE_SECONDS


def running_integrations(layer: Any, but: str = "") -> list[str]:
    """Ids of the layer's integration runs that are queued or running, oldest first."""
    found = []
    for path in sorted((Path(layer.layer_dir) / "agents").glob("*/state.json")):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("adw") == ADW_NAME and data.get("status") == "running":
            run_id = str(data.get("adw_id") or path.parent.name)
            if run_id != but and is_running(layer, run_id):
                found.append((str(data.get("started") or ""), run_id))
    return [run_id for _, run_id in sorted(found)]


# ---------------------------------------------------------------- eligibility

def resolve_run(layer: Any, text: str) -> str:
    """The run id for `text`: an id of the layer, or a branch name (the newest run on that branch)."""
    text = str(text or "").strip()
    agents_dir = Path(layer.layer_dir) / "agents"
    if RUN_ID.match(text) and (agents_dir / text / "state.json").is_file():
        return text
    found = []
    if text:
        for path in agents_dir.glob("*/state.json"):
            data = _read_json(path)
            if isinstance(data, dict) and data.get("branch") == text and data.get("adw_id"):
                found.append((str(data.get("started") or ""), str(data["adw_id"])))
    if not found:
        raise IntegrateError("unknown-run", f"the layer has no run {text or '(empty)'} and no run on a branch of "
                                            "that name")
    return max(found)[1]


def _base_adw(layer: Any, state: Mapping[str, Any]) -> dict[str, Any] | None:
    name = str(state.get("adw") or "")
    for entry in layer.ir.get("adws") or []:
        if isinstance(entry, dict) and entry.get("name") == name and not entry.get("integration"):
            return entry
    definition = state.get("definition")
    return dict(definition) if isinstance(definition, dict) and definition.get("steps") else None


def _target_branch(layer: Any, state: Mapping[str, Any]) -> str:
    recorded = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    return str(recorded.get("branch") or "") or str(layer.codebase_default_branch)


def _rejected(state: Mapping[str, Any]) -> bool:
    """True when the run's review rejected it."""
    ship = state.get("ship") if isinstance(state.get("ship"), dict) else {}
    review_ = state.get("review") if isinstance(state.get("review"), dict) else {}
    verdict = review_.get("verdict") if isinstance(review_.get("verdict"), dict) else {}
    return ("review-rejected" in [str(r) for r in ship.get("handed_over") or []]
            or (verdict.get("success") is False and bool(review_.get("rounds"))))


def blockers_of(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The blockers of the run's final review verdict: [{description, resolution}]."""
    review_ = state.get("review") if isinstance(state.get("review"), dict) else {}
    verdict = review_.get("verdict") if isinstance(review_.get("verdict"), dict) else {}
    return [{"description": str(issue.get("description") or ""), "resolution": str(issue.get("resolution") or "")}
            for issue in verdict.get("issues") or [] if isinstance(issue, dict) and issue.get("severity") == "blocker"]


def eligible(layer: Any, run: str, *, me: str = "", mode: str = "conflict", source: str = "",
             allow_protected: Any = ()) -> Original:
    """The run an integration may bring onto its target, or IntegrateError with the reason. `me` is
    the integration run asking (its own record does not count as "already integrating").

    `mode` conflict asks for a passed review and a conflict or a moved target; blockers and proof
    ask for a rejected review instead (and blockers for a review step that can patch). `source` is
    an earlier attempt (an integration run of `run` that ended handed over): the branch, tip and
    review are then its, while the records stay on `run`. `allow_protected` are changed paths a
    person approved: they are not refused here (the gate still judges them)."""
    from . import review

    if mode not in MODES:
        raise ValueError(f"unknown integrate mode {mode!r}")

    data = settings(layer.layer_dir)
    if not data["enabled"]:
        raise IntegrateError("off", f"integration is switched off for this layer ({SETTINGS_SHOWN})")
    run_id = resolve_run(layer, run)
    state = _state(layer, run_id)
    if not state:
        raise IntegrateError("unknown-run", f"run {run_id} has no readable state.json")
    if state.get("adw") == ADW_NAME or state.get("integration"):
        raise IntegrateError("not-handed-over", f"run {run_id} is itself an integration run: integrate the run it "
                                                f"names, {(state.get('integration') or {}).get('run') or '?'}")
    if state.get("status") != "handed-over":
        raise IntegrateError("not-handed-over", f"run {run_id} is {state.get('status') or 'unknown'}: only a run "
                                                "that ended handed over to a person is integrated")
    if (state_module.run_dir(layer.layer_dir, run_id) / SETTLEMENT_FILE).is_file():
        raise IntegrateError("settled", f"run {run_id} is settled already (agents/{run_id}/{SETTLEMENT_FILE})")
    work = state                      # the run whose branch and review are used
    source = str(source or "")
    if source and source != run_id:
        work = _state(layer, source)
        planned = (work.get("definition") or {}).get("integration") if isinstance(work.get("definition"), dict) else {}
        of = str((planned or {}).get("run") or (work.get("integration") or {}).get("run") or "")
        if not work or of != run_id or work.get("status") != "handed-over":
            raise IntegrateError("not-handed-over", f"run {source} is not an earlier attempt of run {run_id} that "
                                                    "ended handed over")
    else:
        source = ""
    ship = work.get("ship") if isinstance(work.get("ship"), dict) else {}
    handed = [str(r) for r in ship.get("handed_over") or []]
    rejected = _rejected(work)
    if mode == "conflict" and rejected:
        raise IntegrateError("review-rejected", f"the review of run {run_id} rejected it: a person decides what "
                                                "happens with it")
    if mode != "conflict" and not rejected:
        raise IntegrateError("not-rejected", f"the review of run {source or run_id} did not reject it: there are no "
                                             "blockers to address")
    reasons = handed or [str(r) for r in ship.get("reasons") or []]
    app = Path(layer.app_root)
    target = _target_branch(layer, state)
    target_head = _ref(app, f"refs/heads/{target}")
    tip = str(ship.get("commit") or "")
    conflict = any(r.startswith("merge-conflict") for r in reasons)
    moved = (any(r.startswith("ship-failed:") for r in reasons) and bool(target_head)
             and not _is_ancestor(app, target_head, tip))
    if mode == "conflict" and not conflict and not moved:
        raise IntegrateError("not-a-conflict", f"run {run_id} was not handed over because of a merge conflict or a "
                                               f"moved target ({', '.join(first_line(r)[:80] for r in reasons) or 'no reason recorded'})")
    adw = _base_adw(layer, state)
    if adw is None:
        raise IntegrateError("no-ship-step", f"the workflow {state.get('adw') or '?'} of run {run_id} is not known "
                                             "to the layer any more")
    if str(adw.get("changes") or "codebase") == "layer":
        raise IntegrateError("layer-change", f"workflow {adw.get('name')} changes the layer: a person always "
                                             "approves and merges that")
    if not any(step.get("kind") == "ship" for step in adw.get("steps") or []):
        raise IntegrateError("no-ship-step", f"workflow {adw.get('name')} has no ship step, so nothing would land")
    if any(isinstance(a, dict) and a.get("name") == ADW_NAME and not a.get("integration")
           for a in layer.ir.get("adws") or []):
        raise IntegrateError("name-taken", f"the layer declares a workflow named {ADW_NAME}: rename it to use "
                                           "`al integrate`")
    if mode == "blockers":
        patches = next((step for step in adw.get("steps") or [] if step.get("kind") == "review"), {})
        if not (patches.get("patch") and patches.get("implement")):
            raise IntegrateError("no-patch-round", f"the review step of workflow {adw.get('name')} has no patch and "
                                                   "implement command, so no agent can address the blockers")
    branch = str(work.get("branch") or "")
    head = _ref(app, f"refs/heads/{branch}") if branch else ""
    if not head:
        raise IntegrateError("branch-missing", f"branch {branch or '(none)'} of run {run_id} does not exist in the "
                                               "application repository")
    if not tip or head != tip:
        raise IntegrateError("branch-changed", f"a person changed {branch} since the run ended: {head[:7]} is not "
                                               f"the run's {tip[:7] or '(no commit recorded)'}")
    if not target_head:
        raise IntegrateError("branch-missing", f"the target branch {target} does not exist in the application "
                                               "repository")
    if _is_ancestor(app, tip, target_head):
        raise IntegrateError("landed", f"the work of run {run_id} is on {target} already ({tip[:7]})")
    base = _app_git(app, ["merge-base", target_head, tip]).stdout.strip()
    changed = _app_git(app, ["diff", "--name-only", "-z", "--no-renames", f"{base}..{tip}" if base else tip]).stdout
    protected = list((layer.ir["codebase"].get("paths") or {}).get("protected") or [])
    in_repo = str(getattr(layer, "in_repo_dir", "") or "")
    allowed = {str(path) for path in allow_protected or ()}
    for path in sorted(p for p in changed.split("\0") if p):
        if path in allowed:
            continue
        if review.is_protected(path, protected) or (in_repo and review.is_protected(path, [in_repo])):
            raise IntegrateError("protected-path", f"run {run_id} changes {path}, a protected path: a person "
                                                   "merges it")
    record = read_record(layer, run_id)
    if record and record.get("run") != me and is_running(layer, str(record["run"])):
        raise IntegrateError("already-integrating", f"run {run_id} is being integrated by {record['run']}")
    return Original(id=run_id, state=state, adw=adw, branch=branch, tip=tip, target=target,
                    klass=str(state.get("klass") or "chore"), title=first_line(state.get("prompt")),
                    reasons=reasons, target_head=target_head, mode=mode, source=source,
                    blockers=blockers_of(work) if mode != "conflict" else [])


# ---------------------------------------------------------------- the synthetic ADW

def integration_adw(layer: Any, original: Original, data: Mapping[str, Any], *, mode: str = "conflict",
                    extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The synthetic ADW of an integration run (see the module docstring): the agent, review option,
    environment and target of the original run's workflow; steps prepare and resolve, then that
    workflow's checks (affected), its review step, `approve` when the layer asks for a person, and
    its ship step. In the modes blockers and proof: `address` and `proof` after `resolve`, the
    review with no further patch round, and (mode proof) the checks as the workflow declares them,
    because they must run to produce the proof."""
    if mode not in MODES:
        raise ValueError(f"unknown integrate mode {mode!r}")
    extra = dict(extra or {})
    base = original.adw
    declared = {str(o.get("id")) for o in layer.ir.get("promptInputs") or [] if isinstance(o, dict)}

    def renamed(step: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(step)
        if str(out.get("name")) in STEP_NAMES:
            out["name"] = f"{out['name']}-check"
        return out

    steps_ = base.get("steps") or []
    info: dict[str, Any] = {"run": original.id, "branch": original.branch, "tip": original.tip,
                            "target": original.target, "workflow": str(base.get("name"))}
    more: list[dict[str, Any]] = []
    if mode != "conflict":
        info.update(mode=mode, review_retries=0, instruction=str(extra.get("instruction") or ""),
                    approved=[dict(item) for item in extra.get("approved") or [] if isinstance(item, Mapping)],
                    note=str(extra.get("note") or ""), found=[str(name) for name in extra.get("found") or []],
                    produce=bool(extra.get("produce")), source=str(extra.get("source") or original.source or ""),
                    allow_protected=[str(path) for path in extra.get("allow_protected") or []],
                    blockers=[dict(item) for item in original.blockers])
        if mode == "blockers" or info["produce"]:
            more.append({"kind": "integrate", "name": "address", "action": "address"})
        if mode == "proof":
            more.append({"kind": "integrate", "name": "proof", "action": "proof"})
    adw: dict[str, Any] = {
        "name": ADW_NAME, "description": f"integrate run {original.id} onto {original.target}",
        "integration": info,
        "agent": base.get("agent"), "review": base.get("review"),
        "environment": list(base.get("environment") or []),
        "input": [i for i in ["cli"] if i in declared], "classes": [], "trigger": [], "changes": "codebase",
        "steps": [{"kind": "integrate", "name": "prepare", "action": "prepare"},
                  {"kind": "integrate", "name": "resolve", "action": "resolve"},
                  *more,
                  *[renamed(step) if mode == "proof" else {**renamed(step), "whenAffected": True}
                    for step in steps_ if step.get("kind") == "check"],
                  *[dict(step) for step in steps_ if step.get("kind") == "review"][:1],
                  *([{"kind": "integrate", "name": "approve", "action": "approve"}]
                    if data.get("approval") == "person" else []),
                  *[dict(step) for step in steps_ if step.get("kind") == "ship"][:1]]}
    if base.get("deliverTo"):
        adw["deliverTo"] = base["deliverTo"]
    return adw


# ---------------------------------------------------------------- the preview

def _merge_tree(app: Path, target_head: str, tip: str) -> subprocess.CompletedProcess[str]:
    """A dry merge that writes nothing into the application: the objects git creates go to a
    temporary object directory that has the repository's own as its alternate."""
    objects = _app_git(app, ["rev-parse", "--git-path", "objects"]).stdout.strip()
    objects_dir = (Path(objects) if os.path.isabs(objects) else Path(app) / objects).resolve()
    with tempfile.TemporaryDirectory(prefix="al-integrate-") as scratch:
        env = {**os.environ, "GIT_OBJECT_DIRECTORY": scratch, "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects_dir)}
        return _app_git(app, ["merge-tree", "--write-tree", "--name-only", "-z", target_head, tip], env=env)


def _dry_merge(app: Path, target_head: str, tip: str) -> list[str] | None:
    """The files a merge of `tip` into `target_head` conflicts in ([] = it applies cleanly); None
    when this git has no `merge-tree --write-tree` (older than 2.38)."""
    result = _merge_tree(app, target_head, tip)
    if result.returncode not in (0, 1):
        return None
    fields = result.stdout.split("\0")
    files: list[str] = []
    for name in fields[1:]:
        if not name:
            break
        files.append(name)
    return sorted(set(files)) if result.returncode == 1 else []


def _show(app: Path, commit: str, path: str) -> str:
    out = _app_git(app, ["show", f"{commit}:{path}"])
    return out.stdout if out.returncode == 0 else ""


def _file_generated(data: Mapping[str, Any], path: str, texts: list[str]) -> tuple[bool, str]:
    """(generated, regenerate command) of a conflicted file: named in the settings, or a generated
    marker in the first lines of either side."""
    entry = _generated_entry(data, path)
    if entry is not None:
        return True, str(entry.get("regenerate") or "")
    return any(is_generated(text) for text in texts), ""


def preview(layer: Any, run: str, *, mode: str = "conflict", source: str = "", allow_protected: Any = (),
            extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What an integration of `run` would do, without starting it or writing anything into the
    application: {eligible, reason, why, run, branch, tip, target, target_head, clean, conflicts,
    generated, approval, ahead, steps}. `mode`, `source` and `allow_protected` as in eligible();
    `extra` as in integration_adw()."""
    out: dict[str, Any] = {"eligible": False, "reason": "", "why": "", "run": str(run), "branch": "", "tip": "",
                           "target": "", "target_head": "", "clean": None, "conflicts": None, "generated": [],
                           "approval": "none", "ahead": [], "steps": []}
    try:
        original = eligible(layer, run, mode=mode, source=source, allow_protected=allow_protected)
    except IntegrateError as exc:
        with contextlib.suppress(IntegrateError):
            out["run"] = resolve_run(layer, run)
        out.update(reason=exc.reason, why=exc.message)
        return out
    data = settings(layer.layer_dir)
    app = Path(layer.app_root)
    conflicts = _dry_merge(app, original.target_head, original.tip)
    out.update(eligible=True, run=original.id, branch=original.branch, tip=original.tip, target=original.target,
               target_head=original.target_head, approval=data["approval"], title=original.title,
               ahead=running_integrations(layer),
               steps=[str(step["name"]) for step in integration_adw(layer, original, data, mode=mode,
                                                                    extra=extra)["steps"]])
    if conflicts is None:
        out["note"] = OLD_GIT_NOTE
        return out
    out.update(conflicts=conflicts, clean=not conflicts,
               generated=[name for name in conflicts if _file_generated(
                   data, name, [_show(app, original.target_head, name), _show(app, original.tip, name)])[0]])
    return out


def preview_lines(found: Mapping[str, Any]) -> list[str]:
    conflicts = found.get("conflicts")
    if conflicts is None:
        files = str(found.get("note") or OLD_GIT_NOTE)
    elif not conflicts:
        files = "none: the branch applies cleanly"
    else:
        generated = set(found.get("generated") or [])
        files = f"{len(conflicts)}: " + ", ".join(f"{name} (generated)" if name in generated else name
                                                 for name in conflicts)
    lines = [f"run:        {found['run']}" + (f" ({found['title']})" if found.get("title") else ""),
             f"branch:     {found['branch']} at {str(found['tip'])[:7]}",
             f"target:     {found['target']} at {str(found['target_head'])[:7]}",
             f"conflicts:  {files}",
             f"steps:      {', '.join(found.get('steps') or [])}",
             f"approval:   {'a person approves before it ships' if found.get('approval') == 'person' else 'none'}"]
    if found.get("ahead"):
        lines.append(f"queue:      behind {', '.join(found['ahead'])}")
    return lines


# ---------------------------------------------------------------- lessons: what conflicts often

def conflict_report(layer: Any, but: str = "") -> dict[str, Any]:
    """Over all integration runs of the layer: how often each file conflicted, and each pair of
    files that conflicted in the same integration."""
    files: dict[str, int] = {}
    generated: dict[str, bool] = {}
    pairs: dict[tuple[str, str], int] = {}
    count = 0
    for path in sorted((Path(layer.layer_dir) / "agents").glob("*/state.json")):
        data = _read_json(path)
        info = data.get("integration") if isinstance(data, dict) else None
        if not isinstance(info, dict) or not isinstance(info.get("conflicts"), list) or path.parent.name == but:
            continue
        names = sorted({str(item.get("file")) for item in info["conflicts"] if isinstance(item, dict) and item.get("file")})
        if not names:
            continue
        count += 1
        for item in info["conflicts"]:
            if isinstance(item, dict) and item.get("file"):
                generated[str(item["file"])] = generated.get(str(item["file"]), False) or bool(item.get("generated"))
        for name in names:
            files[name] = files.get(name, 0) + 1
        for pair in itertools.combinations(names, 2):
            pairs[pair] = pairs.get(pair, 0) + 1
    return {"integrations": count,
            "files": [{"file": name, "times": times, "generated": generated.get(name, False)}
                      for name, times in sorted(files.items(), key=lambda item: (-item[1], item[0]))],
            "pairs": [{"files": list(pair), "times": times}
                      for pair, times in sorted(pairs.items(), key=lambda item: (-item[1], item[0]))]}


def report_lines(report: Mapping[str, Any]) -> list[str]:
    if not report.get("integrations"):
        return ["no integration had a conflict yet"]
    n = int(report["integrations"])
    lines = [f"{n} integration{'' if n == 1 else 's'} had conflicts", "files:"]
    lines += [f"  {item['file']}  {_plural(int(item['times']), 'time')}" + ("  (generated)" if item.get("generated") else "")
              for item in report["files"]]
    if report.get("pairs"):
        lines.append("together:")
        lines += [f"  {' + '.join(item['files'])}  {_plural(int(item['times']), 'time')}" for item in report["pairs"]]
    lines.append(ADVICE)
    return lines


# ---------------------------------------------------------------- the run's context

@dataclass
class _Run:
    layer: Any
    state: Any
    adw: dict[str, Any]
    env: Any

    @property
    def i(self) -> dict[str, Any]:
        return self.state.integration

    @property
    def app(self) -> Path:
        return Path(self.layer.app_root)

    @property
    def sandbox(self) -> Any:
        return getattr(self.env, "sandbox", None)

    @property
    def worktree(self) -> Path:
        return Path(self.state.worktree)

    def git(self, args: list[str], step: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """git in the run's worktree (sandboxed: through gitsafe)."""
        return delivery.tree_git(self.state, self.env, args, check=check, step=step)

    def app_git(self, args: list[str], step: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """git in the application repository: reading refs only."""
        if self.sandbox is not None:
            return gitsafe.run_app(self.sandbox, args, step, check=check)
        result = _app_git(self.app, args)
        if check and result.returncode != 0:
            raise StepFailed(step, f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
        return result

    def head(self, step: str) -> str:
        return self.git(["rev-parse", "HEAD"], step, check=True).stdout.strip()

    def names(self, args: list[str], step: str) -> list[str]:
        out = self.git([args[0], "-z", *args[1:]], step).stdout
        return sorted({name for name in out.split("\0") if name})


def _unmerged(run: _Run, step: str) -> list[str]:
    return run.names(["diff", "--name-only", "--diff-filter=U"], step)


def _markers(worktree: Path, files: Any) -> list[str]:
    """Files that still hold a conflict marker line (promotion.py's rule, on marker lines)."""
    out = []
    for name in sorted(set(files)):
        path = worktree / name
        if path.is_file() and not path.is_symlink() and CONFLICT_MARKER.search(
                path.read_text(encoding="utf-8", errors="replace")):
            out.append(name)
    return out


def _original_request(layer: Any, info: Mapping[str, Any]) -> str:
    run_dir = state_module.run_dir(layer.layer_dir, str(info.get("run")))
    try:
        text = (run_dir / REQUEST_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or str(_state(layer, str(info.get("run"))).get("prompt") or "").strip()


# ---------------------------------------------------------------- the brief

def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def write_brief(run: _Run) -> Path:
    """specs/integrate-<id>.md: what the reviewers of the integration read as its plan."""
    layer, info = run.layer, run.i
    original = _state(layer, str(info.get("run")))
    ship = original.get("ship") if isinstance(original.get("ship"), dict) else {}
    reasons = [first_line(r)[:200] for r in (ship.get("handed_over") or ship.get("reasons") or [])]
    conflicts = list(info.get("conflicts") or [])
    mode = str(info.get("mode") or "conflict")
    if mode == "blockers":
        lead = (f"Run {info.get('run')} was reviewed and rejected. This run addressed the blockers below (and "
                f"brought the change onto {info.get('target')}). Review the whole change against the plan; for each "
                "blocker say whether it is resolved.")
    elif mode == "proof":
        lead = (f"Run {info.get('run')} was reviewed and rejected only for missing proof. No code was changed. The "
                "proof is in the manifest (`found-*` entries / the collected artefacts). Review the whole change "
                "against the plan, on that proof.")
    else:
        lead = (f"This change was planned, built, checked and reviewed in run {info.get('run')}; its review passed. "
                f"It could not merge because {'; '.join(reasons) or 'the target branch moved'}. Review what the "
                "integration changed: concentrate on the conflict resolutions below.")
    lines = [f"# Integration of run {info.get('run')} onto {info.get('target')}", "", lead, "",
             "## The original request", "", _original_request(layer, info) or "(not recorded)", ""]
    if mode != "conflict":
        lines += ["## The blockers of the rejected review", ""]
        lines += [f"- {_cell(item.get('description'))}\n  Resolution: {_cell(item.get('resolution'))}"
                  for item in info.get("blockers") or []] or ["- none recorded"]
        if info.get("approved"):
            lines += ["", "## Approved by a person", ""]
            lines += [f"- {_cell(item.get('text'))} ({_cell(item.get('by'))}, {_cell(item.get('at'))})"
                      for item in info["approved"]]
        if info.get("note"):
            lines += ["", "## The person adds", "", _cell(info["note"])]
        if isinstance(info.get("address"), dict):
            lines += ["", "## What this run did about them", "", str(info["address"].get("patch") or "(no patch plan)")]
        if isinstance(info.get("proof"), dict):
            lines += ["", "## The proof handed over", ""]
            lines += [f"- `{_cell(name)}`" for name in info["proof"].get("found") or []] or [
                f"- {info['proof'].get('collected', 0)} collected artefacts of the layer"]
        lines.append("")
    plan = str(original.get("plan") or "")
    lines += ["## The original plan", "",
              (f"`{layer.display_path(plan)}`: its requirements and acceptance criteria still hold."
               if plan else "The original run recorded no plan; the request above is what must hold."), "",
              "## The two diffs to read", "",
              f"- `git diff {info.get('target_head')}`: what lands on {info.get('target')} (the whole change, "
              "as it is in the worktree)",
              f"- `git diff {info.get('tip')}`: what the integration changed in the reviewed work (the conflict "
              f"resolutions{', the answer to the blockers' if mode == 'blockers' else ''} and what "
              f"{info.get('target')} gained since)", "",
              "## Conflicts", ""]
    if conflicts:
        lines += ["| file | generated | touched on the target since the run's base |", "|---|---|---|"]
        for item in conflicts:
            touched = "; ".join(f"{t.get('commit')}" + (f" (run {t['run']})" if t.get("run") else "")
                                + f" {t.get('summary')}" for t in item.get("touched_by") or []) or "-"
            generated = ("yes" + (f": `{item['regenerate']}`" if item.get("regenerate") else "")
                         if item.get("generated") else "no")
            lines.append(f"| `{_cell(item.get('file'))}` | {_cell(generated)} | {_cell(touched)} |")
    else:
        lines.append(f"None: the branch applies cleanly onto {info.get('target')}.")
    report = info.get("report") if isinstance(info.get("report"), dict) else None
    if report is not None:
        lines += ["", "## Resolutions", ""]
        lines += [f"- `{item.get('file')}`: {_cell(item.get('sides') or '')} → {_cell(item.get('resolution') or '')}"
                  for item in report.get("resolved") or []]
        lines += [f"- `{item.get('file')}`: regenerated with `{_cell(item.get('command') or '')}`"
                  for item in report.get("regenerated") or []]
        lines += [f"- `{item.get('file')}`: not decided: {_cell(item.get('question') or '')}"
                  for item in report.get("undecidable") or []]
        if not any(report.get(key) for key in ("resolved", "regenerated", "undecidable")):
            lines.append("- none reported")
    seen = list(info.get("seen_before") or [])
    if seen:
        lines += ["", "## Conflicted before", ""]
        lines += [f"- `{item['file']}`: in {_plural(int(item['times']), 'earlier integration')}" for item in seen]
    path = Path(layer.layer_dir) / "specs" / f"integrate-{run.state.adw_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info["brief"] = path.relative_to(layer.layer_dir).as_posix()
    run.state.plan = str(path)
    return path


# ---------------------------------------------------------------- the steps

def _prepare(run: _Run) -> None:
    step = "prepare"
    definition = run.adw.get("integration") or {}
    data = settings(run.layer.layer_dir)
    tip, branch = str(definition.get("tip")), str(definition.get("branch"))
    target = str(definition.get("target"))
    head = run.head(step)
    base = run.git(["merge-base", "HEAD", tip], step, check=True).stdout.strip()
    run.i.update({"run": str(definition.get("run")), "branch": branch, "tip": tip, "target": target,
                  "target_head": head, "base": base, "workflow": str(definition.get("workflow")),
                  "by": _STARTED_BY.get(run.state.adw_id) or run.i.get("by") or "cli",
                  "approval": {"required": data["approval"] == "person", "by": "", "at": ""}})
    if definition.get("mode") in ("blockers", "proof"):
        run.i.update({key: copy.deepcopy(definition.get(key)) for key in ("mode", "source", "blockers", "approved",
                                                                          "note", "found")})
    now = run.app_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], step).stdout.strip()
    if now != tip:
        raise StepFailed(step, f"integrate:branch-changed: a person changed {branch} since the run ended: "
                               f"{now[:7] or '(gone)'} is not the run's {tip[:7]}")
    merged = run.git([*IDENTITY, "-c", "merge.conflictStyle=diff3", "merge", "--no-commit", "--squash", tip], step)
    files = _unmerged(run, step)
    if merged.returncode != 0 and not files:
        raise StepFailed(step, f"git merge --no-commit --squash {tip[:7]} failed: "
                               f"{(merged.stderr or merged.stdout).strip()[:500]}")
    seen = {item["file"]: item["times"] for item in conflict_report(run.layer, but=run.state.adw_id)["files"]}
    conflicts = []
    for name in files:
        generated, command = _file_generated(data, name, [
            run.git(["show", f"{head}:{name}"], step).stdout, run.git(["show", f"{tip}:{name}"], step).stdout])
        touched = []
        log = run.git(["log", f"-n{MAX_TOUCHED}", "--format=%h%x09%s", f"{base}..{head}", "--", name], step).stdout
        for line in log.splitlines():
            commit, _, summary = line.partition("\t")
            tag = RUN_TAG.search(summary)
            known = bool(tag) and state_module.run_dir(run.layer.layer_dir, tag.group(1)).is_dir()
            touched.append({"commit": commit, "run": tag.group(1) if known else "", "summary": summary[:200]})
        conflicts.append({"file": name, "generated": generated, "regenerate": command, "touched_by": touched})
    run.i["clean"] = not conflicts
    run.i["conflicts"] = conflicts
    run.i["seen_before"] = [{"file": name, "times": seen[name]} for name in files if name in seen]
    write_brief(run)
    run.state.save(run.layer.layer_dir)
    count = sum(1 for item in conflicts if item["generated"])
    line = (f"prepare: {_plural(len(conflicts), 'conflicted file')}"
            + (f" ({count} generated)" if conflicts else "") + f"; target {target} at {head[:7]}")
    run.state.outputs[step] = line
    events.say(f"[adw {run.state.adw_id}] {line}", "integrate.prepared", run=run.i["run"], target=target,
               target_head=head, base=base, conflicts=[item["file"] for item in conflicts],
               generated=[item["file"] for item in conflicts if item["generated"]])
    if run.i["seen_before"]:
        print(f"[adw {run.state.adw_id}] conflicted before: "
              + ", ".join(f"{item['file']} ({_plural(item['times'], 'time')})" for item in run.i["seen_before"]))


def _command(run: _Run) -> tuple[dict[str, Any], str]:
    """(the command record, its prompt text): the layer's own `resolve-conflicts` when it declares
    one, else the runtime's record and the file `al gen` wrote from the kit template."""
    try:
        command = run.layer.command(COMMAND_NAME)
    except LayerError:
        command = dict(COMMAND)
    folder = "hops" if command.get("isHop") else "commands"
    path = Path(run.layer.layer_dir) / folder / f"{COMMAND_NAME}.md"
    try:
        return command, path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StepFailed("resolve", f"cannot read the prompt {folder}/{COMMAND_NAME}.md ({exc.strerror}): run "
                                    "`al gen` on the layer") from exc


def _check_report(report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for key in ("resolved", "regenerated", "undecidable"):
        items = report.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) and isinstance(item.get("file"), str)
                                                  for item in items):
            raise StepFailed("resolve", f'the agent\'s report: "{key}" must be a list of objects with a string "file"')
        out[key] = [{k: (v if isinstance(v, str) else json.dumps(v)) for k, v in item.items()} for item in items]
    return out


def _dropped_tests(run: _Run, step: str) -> list[str]:
    """Test files of either side that are gone from the worktree, unless a side deleted them itself."""
    patterns = [str(p) for p in (run.layer.ir["codebase"].get("paths") or {}).get("tests") or [] if str(p).strip()]
    if not patterns:
        return []
    head, tip, base = str(run.i["target_head"]), str(run.i["tip"]), str(run.i["base"])
    deleted: set[str] = set()
    for side in (tip, head):
        rows = run.git(["diff", "--name-status", "--no-renames", "-z", base, side], step).stdout.split("\0")
        deleted |= {rows[i + 1] for i in range(0, len(rows) - 1, 2) if rows[i].startswith("D")}
    known: set[str] = set()
    for side in (head, tip):
        known |= set(run.names(["ls-tree", "-r", "--name-only", side], step))
    return sorted(path for path in known if affected.matches(path, patterns) and path not in deleted
                  and not (run.worktree / path).exists() and not (run.worktree / path).is_symlink())


def _resolve(run: _Run) -> None:
    from . import agents, templates

    step = "resolve"
    conflicts = list(run.i.get("conflicts") or [])
    if not conflicts:
        run.state.outputs[step] = "no conflicts: the branch applies cleanly"
        run.i["report"] = {"resolved": [], "regenerated": [], "undecidable": []}
        return
    command, text = _command(run)
    brief = str(Path(run.layer.layer_dir) / str(run.i.get("brief")))
    args = [brief, json.dumps([{"file": item["file"], "generated": item["generated"],
                                "regenerate": item["regenerate"]} for item in conflicts])]
    prompt = templates.render(text, agents.template_variables(run.layer, run.state), args)
    output = agents.run_prompt(run.layer, run.state, step, command, prompt, str(command.get("modelSet") or "heavy"),
                               run.env, tier_from="step", args=args)
    head = run.head(step)
    if head != run.i["target_head"]:
        raise StepFailed(step, f"integrate:head-moved: the agent committed ({head[:7]} is not the target head "
                               f"{str(run.i['target_head'])[:7]}); the resolve step never commits")
    report = _check_report(agents.extract_json(output, step))
    run.i["report"] = report
    run.state.outputs[step] = (f"{len(report['resolved'])} resolved, {len(report['regenerated'])} regenerated"
                               + (f", {len(report['undecidable'])} undecidable" if report["undecidable"] else "")
                               + f"\n\n{output}")
    write_brief(run)
    events.emit("integrate.resolved", resolved=[i["file"] for i in report["resolved"]],
                regenerated=[i["file"] for i in report["regenerated"]],
                undecidable=[i["file"] for i in report["undecidable"]])
    if report["undecidable"]:
        raise StepFailed(step, "integrate:undecidable: " + "; ".join(
            f"{item['file']}: {first_line(item.get('question')) or 'a person decides'}" for item in report["undecidable"]))
    run.git(["add", "-A"], step, check=True)
    names = [item["file"] for item in conflicts]
    reported = {item["file"] for item in report["resolved"] + report["regenerated"]}
    changed = run.names(["diff", "--cached", "--name-only", "--no-renames", str(run.i["target_head"])], step)
    # a changed file that is byte for byte the reviewed work is not the agent's: its text may hold marker lines
    differs = set(run.names(["diff", "--cached", "--name-only", "--no-renames", str(run.i["tip"])], step))
    candidates = set(names) | {name for name in changed if name in differs}
    remaining = sorted({name for name in names if name not in reported} | set(_unmerged(run, step))
                       | set(_markers(run.worktree, candidates)))
    if remaining:
        raise StepFailed(step, f"integrate:markers-remain: {', '.join(remaining)}: not reported as resolved or "
                               "regenerated, or still holding conflict markers; a person resolves them")
    dropped = _dropped_tests(run, step)
    if dropped:
        raise StepFailed(step, f"integrate:markers-remain: a test of one side was dropped: {', '.join(dropped)}")
    write_brief(run)


def _approve(run: _Run) -> None:
    step = "approve"
    info = run.i.setdefault("approval", {"required": True, "by": "", "at": ""})
    info["required"] = True
    given = _read_json(state_module.run_dir(run.layer.layer_dir, run.state.adw_id) / APPROVAL_FILE)
    if isinstance(given, dict) and given.get("by"):
        info.update(by=str(given["by"]), at=str(given.get("at") or ""))
        run.state.outputs[step] = f"approved by {info['by']}"
        return
    raise StepWaiting(step, WAITING_EXIT, f"waiting for a person: al integrate LAYER {run.i.get('run')} --approve")


def _artefact_globs(layer: Any) -> list[str]:
    return [str(option.get("glob")) for option in layer.ir.get("artefacts") or []
            if isinstance(option, dict) and option.get("glob")]


def _tree(run: _Run, step: str) -> str:
    """The tree of everything in the worktree now (staged with `add -A`; nothing is committed)."""
    run.git(["add", "-A"], step, check=True)
    return run.git(["write-tree"], step, check=True).stdout.strip()


def _address(run: _Run) -> None:
    """The review's patch round, for the blockers of the rejected review (mode blockers) or to
    produce the missing proof (mode proof): the review step's own `patch` and `implement` commands."""
    from . import agents

    step = "address"
    definition = run.adw.get("integration") or {}
    mode = str(definition.get("mode") or "")
    review_step = next((item for item in run.adw.get("steps") or [] if item.get("kind") == "review"), {})
    patch, implement = str(review_step.get("patch") or ""), str(review_step.get("implement") or "")
    if not patch or not implement:
        raise StepFailed(step, "integrate:no-patch-round: the review step has no patch and implement command")
    request = str(definition.get("instruction") or "").strip() or "Address the blockers of the rejected review."
    before = _tree(run, step) if mode == "proof" else ""

    def tier(name: str) -> str:
        return str(run.layer.command(name).get("modelSet") or "base")

    plan = agents.run_agent(run.layer, run.state, "address-patch", patch, [request, run.state.plan or ""],
                            tier(patch), run.env)
    run.state.outputs["address-patch"] = plan
    done = agents.run_agent(run.layer, run.state, "address-implement", implement, [plan], tier(implement), run.env)
    run.state.outputs["address-implement"] = done
    head = run.head(step)
    if head != run.i["target_head"]:
        raise StepFailed(step, f"integrate:head-moved: the agent committed ({head[:7]} is not the target head "
                               f"{str(run.i['target_head'])[:7]}); the address step never commits")
    blockers = list(definition.get("blockers") or [])
    run.i["address"] = {"blockers": len(blockers), "patch": plan[:2000]}
    if mode == "proof":
        changed = run.names(["diff", "--name-only", "--no-renames", before, _tree(run, step)], step)
        code = [name for name in changed if not affected.matches(name, _artefact_globs(run.layer))]
        if code:
            raise StepFailed(step, f"integrate:markers-remain: the proof round changed code: {', '.join(code)}")
    run.state.outputs[step] = (f"produced the proof the review missed ({_plural(len(blockers), 'blocker')})"
                               if mode == "proof" else f"addressed {_plural(len(blockers), 'blocker')}")
    write_brief(run)
    events.emit("integrate.resolved", resolved=[], regenerated=[], undecidable=[], addressed=len(blockers), mode=mode)


def _proof(run: _Run) -> None:
    """Hand the review the proof it missed: the files found for the original run (`found-image`,
    `found-video`, `found-file`), or, when the proof had to be produced, the layer's artefacts
    collected again. No application code changes here."""
    from . import artefacts

    step = "proof"
    definition = run.adw.get("integration") or {}
    found = [str(name) for name in definition.get("found") or []]
    missing: list[str] = []
    collected = 0
    if found:
        roots = []
        for run_id in (str(definition.get("source") or ""), str(definition.get("run") or "")):
            kept = str(_state(run.layer, run_id).get("worktree") or "") if run_id else ""
            if kept and Path(kept).is_dir():
                roots.append(Path(kept).resolve())
        roots.append(run.worktree.resolve())
        groups: dict[tuple[str, str], list[tuple[str, Path]]] = {}
        for name in found:
            path = next((root / name for root in roots if (root / name).is_file() and not (root / name).is_symlink()
                         and (root / name).resolve().is_relative_to(root)), None)
            if path is None:
                missing.append(name)
                continue
            suffix = Path(name).suffix.lower()
            group = next(((artefact_id, kind) for artefact_id, kind, suffixes in FOUND_KINDS if suffix in suffixes),
                         FOUND_FILE)
            groups.setdefault(group, []).append((name, path))
        for (artefact_id, kind), matches in groups.items():
            collected += len(artefacts.add_files(run.layer, run.state, artefact_id, kind, step, matches))
    else:
        ids: list[str] = []
        for item in run.adw.get("steps") or []:
            if item.get("kind") == "check":
                ids += [str(i) for i in item.get("collect") or []]
        with contextlib.suppress(LayerError):
            ids += [str(i) for i in run.layer.option("reviews", str(run.adw.get("review"))).get("artefacts") or []]
        collected = len(artefacts.collect(run.layer, run.state, list(dict.fromkeys(ids)), step))
    run.i["proof"] = {"found": [name for name in found if name not in missing], "collected": collected}
    if not collected:
        raise StepFailed(step, "no proof to hand to the review: "
                               + (f"not found any more: {', '.join(missing)}" if missing
                                  else "the layer's artefacts matched no file"))
    run.state.outputs[step] = (f"{_plural(collected, 'file')} of proof handed to the review"
                               + (f"; not found any more: {', '.join(missing)}" if missing else ""))
    write_brief(run)


ACTIONS = {"prepare": _prepare, "resolve": _resolve, "approve": _approve, "address": _address, "proof": _proof}


def with_review_retries(layer: Any, adw: Mapping[str, Any]) -> Any:
    """The layer a review step of `adw` runs with: `layer` itself, unless the ADW is an integration
    run that fixes its review's patch rounds (`integration.review_retries`, 0 for the modes blockers
    and proof). Then a copy whose IR has that many `maxRetries` on the ADW's review option, for
    this run only (the way delivery.scope copies a layer); review.py reads the option as always."""
    info = adw.get("integration") if isinstance(adw, Mapping) else None
    retries = info.get("review_retries") if isinstance(info, Mapping) else None
    if not isinstance(retries, int) or isinstance(retries, bool):
        return layer
    changed = copy.deepcopy(layer.ir)
    for option in changed.get("reviews") or []:
        if isinstance(option, dict) and option.get("id") == adw.get("review"):
            option["maxRetries"] = max(0, retries)
    return dataclasses.replace(layer, ir=changed)


def run_step(layer: Any, state: Any, adw: Mapping[str, Any], step: Mapping[str, Any], env_stack: Any) -> None:
    """One step of an integration run (steps._execute_step dispatches kind `integrate` here)."""
    action = str(step.get("action") or step.get("name"))
    if action not in ACTIONS:
        raise LayerError(f"unknown integration step {action!r}; steps are {', '.join(ACTIONS)}")
    run = _Run(layer, state, dict(adw), env_stack)
    try:
        ACTIONS[action](run)
        state.integration.pop("stopped", None)
    except StepFailed as exc:
        state.integration["stopped"] = {"step": action, "reason": exc.reason[:2000]}
        raise
    finally:
        state.save(layer.layer_dir)


# ---------------------------------------------------------------- the queue: one integration at a time

def _note(layer: Any, run_id: str, line: str) -> None:
    """A line of the integration itself (outside the runner): printed, and kept in the run's log."""
    print(line, flush=True)
    with contextlib.suppress(OSError):
        with (state_module.run_dir(layer.layer_dir, run_id) / "run.log").open("a", encoding="utf-8") as log:
            log.write(line + "\n")


def _event(layer: Any, run_id: str, type_: str, **fields: Any) -> None:
    with events.recording(layer.layer_dir, run_id):
        events.emit(type_, **fields)


@contextlib.contextmanager
def _queue(layer: Any, run_id: str, skeleton: Any = None) -> Iterator[float]:
    """Hold agents/.integrate.lock for the block; waits (up to the layer's waitSeconds) while
    another integration holds it, keeping the waiting run's state.json alive."""
    path = Path(layer.layer_dir) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    limit = slots.limits(layer)["wait"]
    started = time.monotonic()
    beat = started
    announced = False
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                pass
            if not announced:
                words = os.pread(handle, 200, 0).decode("utf-8", "replace").split()
                holder = words[1] if len(words) > 1 else "another one"
                _note(layer, run_id, f"[adw {run_id}] queued behind integration {holder}")
                _event(layer, run_id, "integrate.queued", behind=holder)
                announced = True
            if time.monotonic() - started >= limit:
                raise StepFailed("queue", f"another integration held the layer's lock for {limit}s")
            if skeleton is not None and time.monotonic() - beat >= state_module.HEARTBEAT_SECONDS:
                beat = time.monotonic()
                skeleton.mark_alive()
                skeleton.save(layer.layer_dir)
            time.sleep(QUEUE_POLL)
        waited = round(time.monotonic() - started, 3)
        os.ftruncate(handle, 0)
        os.pwrite(handle, f"{os.getpid()} {run_id}\n".encode(), 0)
        _event(layer, run_id, "integrate.lock", holder=run_id, waited=waited)
        try:
            yield waited
        finally:
            _event(layer, run_id, "integrate.unlock", holder=run_id, held=round(time.monotonic() - started - waited, 3))
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


# ---------------------------------------------------------------- entry points

def _spawn(argv: list[str], **popen: Any) -> subprocess.Popen:
    return subprocess.Popen(argv, **popen)


def _prompt(layer: Any, original: Original, mode: str = "conflict") -> str:
    request = _original_request(layer, {"run": original.id})
    verb = {"blockers": "Address the review of run", "proof": "Collect proof for run"}.get(mode, "Integrate run")
    return (f"{verb} {original.id} ({original.title or 'no title'}) onto {original.target}"
            + (f"\n\n{request}" if request else ""))


def _skeleton(layer: Any, adw_id: str, prompt: str, klass: str, definition: dict[str, Any], dry_run: bool,
              by: str) -> Any:
    """What a reader sees until the run writes its own state: running, as launch.start writes it."""
    info = definition.get("integration") or {}
    skeleton = state_module.ADWState(adw_id=adw_id, adw=ADW_NAME, prompt=prompt, source="cli", klass=klass,
                                     class_fixed=True, dry_run=dry_run, definition=definition,
                                     integration={"run": info.get("run"), "branch": info.get("branch"),
                                                  "tip": info.get("tip"), "target": info.get("target"),
                                                  "workflow": info.get("workflow"), "by": by,
                                                  **({"mode": info["mode"]} if info.get("mode") else {})})
    skeleton.host, skeleton.heartbeat = "", state_module.now()
    return skeleton


def _fail_skeleton(layer: Any, adw_id: str, why: str) -> None:
    """A run that never reached the runner: its skeleton says failed and why."""
    data = _state(layer, adw_id)
    if not data or data.get("steps"):
        return
    data.update(status="failed", ended=state_module.now())
    data.setdefault("integration", {}).update(result="failed", why=first_line(why)[:500])
    path = state_module.run_dir(layer.layer_dir, adw_id) / "state.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _check_tier(layer: Any, definition: dict[str, Any], model_set: str) -> None:
    from . import models

    if not model_set:
        return
    try:
        models.check_overrides(layer, definition, models.overrides(model_set, [], None))
    except (LayerError, ValueError) as exc:
        raise _Usage(str(exc)) from exc


def result_line(layer: Any, original_id: str, integration_id: str) -> str:
    """The last line of an attached integration: what happened to the original."""
    record = read_record(layer, original_id) or {}
    head = f"integrate:  run {original_id} onto {record.get('target') or '?'}: "
    if record.get("run") != integration_id:
        return head + f"not landed: see run {integration_id}; {original_id} stays under Needs you"
    if record.get("status") == "landed":
        return head + f"landed in {str(record.get('commit'))[:7]}; {original_id} settled as landed by {integration_id}"
    if record.get("status") == "waiting":
        return head + f"waiting for a person: al integrate LAYER {original_id} --approve"
    return head + f"not landed: {record.get('why') or 'no reason recorded'}; {original_id} stays under Needs you"


def finish(layer: Any, original_id: str, integration_id: str) -> dict[str, Any]:
    """After the integration run ended: write the original's record (and its settlement when the
    work landed), and the result into the integration run's own state."""
    data = _state(layer, integration_id)
    info = data.get("integration") if isinstance(data.get("integration"), dict) else {}
    ship = data.get("ship") if isinstance(data.get("ship"), dict) else {}
    target = str(info.get("target") or (data.get("definition") or {}).get("integration", {}).get("target") or "")
    status = str(data.get("status") or "")
    commit, why = "", ""
    reasons = ", ".join(first_line(r)[:120] for r in ship.get("handed_over") or ship.get("reasons") or [])
    if status == "waiting":
        result, record_status = "waiting", "waiting"
        why = "waits for a person's approval before it ships"
    elif status == "failed":
        result, record_status = "failed", "failed"
        stopped = next((s for s in data.get("steps") or [] if s.get("status") == "failed"), {})
        why = (f"stopped at {stopped.get('name') or '?'}: "
               f"{first_line(str(stopped.get('detail') or '').split(': ', 1)[-1]) or 'no reason recorded'}"
               if stopped else first_line(info.get("why")) or "the run failed before its first step")
    elif ship.get("shipped") is True and ship.get("action") in ("merge", "deploy"):
        result, record_status, commit = "landed", "landed", str(ship.get("commit") or "")
    elif ship.get("action") == "pr":
        result, record_status = "pull-request", "failed"
        why = f"the gate did not ship it: {reasons or 'no reason recorded'}; see {ship.get('target') or 'the run'}"
        mode = str(info.get("mode") or (data.get("definition") or {}).get("integration", {}).get("mode") or "")
        if mode in ("blockers", "proof") and _rejected(data):
            verdict = (data.get("review") or {}).get("verdict") or {}
            why = f"its review rejected it again: {first_line(verdict.get('summary')) or 'no summary recorded'}"
    elif ship.get("action") in ("merge", "deploy"):
        result, record_status = "staged", "failed"
        why = str(ship.get("note") or f"the result is staged on {ship.get('target')}, not on {target}")
    else:
        result, record_status = "failed", "failed"
        why = f"the run ended {status or 'unknown'} without shipping"
    by = str(info.get("by") or _STARTED_BY.get(integration_id) or "")
    record = write_record(layer, original_id, run=integration_id, status=record_status, why=why, commit=commit,
                          target=target, by=by)
    if record_status == "landed":
        write_settlement(layer, original_id, integration_id, commit, target)
    with contextlib.suppress(LayerError):
        state = state_module.load(layer.layer_dir, integration_id)
        state.integration.update(result=result, why=first_line(why)[:500])
        state.save(layer.layer_dir)
    _event(layer, integration_id, "integrate.result", run=original_id, result=result, why=first_line(why)[:500],
           commit=commit, target=target)
    return record


def run(layer_dir: Path, run: str, *, model_set: str = "", dry_run: bool = False, detach: bool = False,  # noqa: A002
        resume: bool = False, adw_id: str = "", by: str = "cli", as_json: bool = False, spawn: Any = _spawn,
        mode: str = "conflict", extra: Mapping[str, Any] | None = None) -> int:
    """Start (or, with resume, continue) the integration of `run`; returns the run's exit code
    (0 landed or handed over, 1 failed, 75 waiting). IntegrateError when it is refused.

    `mode` and `extra` (EXTRA_KEYS) are given by assign.give for a run whose review rejected it. A
    detached start keeps them in the skeleton's definition, where the child process reads them."""
    from . import runner

    layer_dir = Path(layer_dir)
    layer = ir.load(layer_dir)
    original: Original | None = None
    if resume:
        saved = _state(layer, adw_id) if adw_id else {}
        definition = saved.get("definition") if isinstance(saved.get("definition"), dict) else None
        if not definition or not isinstance(definition.get("integration"), dict):
            raise IntegrateError("not-waiting", f"run {adw_id or '(no --adw-id)'} is not an integration run to resume")
        original_id = str(definition["integration"]["run"])
        prompt, klass = str(saved.get("prompt") or ""), str(saved.get("klass") or "chore")
        dry_run = dry_run or bool(saved.get("dry_run"))
    else:
        if mode == "conflict" and extra is None and adw_id:      # the detached child of a start in another mode
            planned = (_state(layer, adw_id).get("definition") or {}).get("integration")
            if isinstance(planned, dict) and planned.get("mode") in ("blockers", "proof"):
                mode, extra = str(planned["mode"]), {key: planned.get(key) for key in EXTRA_KEYS}
        extra = dict(extra or {})
        # mode conflict asks eligible() exactly as before
        accepts = {} if mode == "conflict" else {
            "mode": mode, "source": str(extra.get("source") or ""),
            "allow_protected": tuple(str(path) for path in extra.get("allow_protected") or [])}
        shape = {} if mode == "conflict" else {"mode": mode, "extra": extra}
        try:
            original = eligible(layer, run, me=adw_id, **accepts)
        except IntegrateError as exc:
            # a detached start whose run was reserved already: its skeleton and the record say why it never ran
            if adw_id and (state_module.run_dir(layer_dir, adw_id) / "state.json").is_file():
                _fail_skeleton(layer, adw_id, exc.message)
                with contextlib.suppress(IntegrateError):
                    known = resolve_run(layer, run)
                    record = read_record(layer, known)
                    if record is not None and record.get("run") == adw_id:
                        write_record(layer, known, run=adw_id, status="failed", why=exc.message, by=by)
            raise
        definition = integration_adw(layer, original, settings(layer_dir), **shape)
        original_id, prompt, klass = original.id, _prompt(layer, original, mode), original.klass
        if not adw_id:
            adw_id = state_module.new_adw_id()
            while state_module.run_dir(layer_dir, adw_id).exists():
                adw_id = state_module.new_adw_id()
    _check_tier(layer, definition, model_set)
    flags = [*(["--model-set", model_set] if model_set else []), *(["--dry-run"] if dry_run else []),
             *(["--resume"] if resume else [])]
    run_dir = state_module.run_dir(layer_dir, adw_id)
    skeleton = None
    if not (run_dir / "state.json").is_file():
        run_dir.mkdir(parents=True, exist_ok=True)
        handle = os.open(run_dir / REQUEST_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(prompt + "\n")
        skeleton = _skeleton(layer, adw_id, prompt, klass, definition, dry_run, by)
        skeleton.save(layer_dir)
    target = str(definition["integration"].get("target") or "")
    if detach:
        argv = [sys.executable, str(layer_dir / SCRIPT), "run", original_id, "--adw-id", adw_id, *flags, "--by", by]
        if not resume:
            write_record(layer, original_id, run=adw_id, status="queued", target=target, by=by)
        with (run_dir / LAUNCH_LOG).open("ab") as log:
            process = spawn(argv, cwd=str(layer_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
        pid = getattr(process, "pid", 0)
        if skeleton is not None and isinstance(pid, int) and pid > 0:
            current = _state(layer, adw_id)
            if not current.get("pid"):   # the child has not written its own state yet
                skeleton.pid, skeleton.host = pid, socket.gethostname()
                skeleton.save(layer_dir)
        print(json.dumps({"status": "started", "run_id": adw_id, "original": original_id, "resumed": resume})
              if as_json else f"started integration run {adw_id} of {original_id}; log agents/{adw_id}/{LAUNCH_LOG}")
        return 0
    if skeleton is None and not resume:   # the detached parent wrote it: this process is the run now
        with contextlib.suppress(LayerError):
            skeleton = state_module.load(layer_dir, adw_id)
    if skeleton is not None:
        skeleton.mark_alive()
        skeleton.save(layer_dir)
    try:
        with _queue(layer, adw_id, skeleton):
            if not resume:
                original = eligible(layer, original_id, me=adw_id, **accepts)   # it may have landed or changed meanwhile
                definition = integration_adw(layer, original, settings(layer_dir), **shape)
            write_record(layer, original_id, run=adw_id, status="running", target=target, by=by)
            _STARTED_BY[adw_id] = by
            argv = (["--resume", "--adw-id", adw_id, "--decider", "off", *[f for f in flags if f != "--resume"]]
                    if resume else [prompt, "--class", klass, "--adw-id", adw_id, "--decider", "off", *flags])
            code = runner.main(ADW_NAME, layer_dir, argv, definition=definition)
            if code == 2 or not _state(layer, adw_id).get("steps") and code != 0:
                _fail_skeleton(layer, adw_id, "the run did not start: see its log")
            finish(layer, original_id, adw_id)
    except (IntegrateError, StepFailed) as exc:
        why = exc.message if isinstance(exc, IntegrateError) else exc.reason
        _fail_skeleton(layer, adw_id, why)
        record = read_record(layer, original_id)
        if record is None or record.get("run") == adw_id:
            write_record(layer, original_id, run=adw_id, status="failed", why=why, target=target, by=by)
        _note(layer, adw_id, f"[adw {adw_id}] integration not started: {exc}")
        if isinstance(exc, IntegrateError):
            raise
        code = 1
    if as_json:
        record = read_record(layer, original_id) or {}
        print(json.dumps({"status": record.get("status", ""), "run_id": adw_id, "original": original_id,
                          "exit": code, "why": record.get("why", ""), "commit": record.get("commit", "")}))
    else:
        _note(layer, adw_id, result_line(layer, original_id, adw_id))
    return code


def _integration_of(layer: Any, run_id: str) -> tuple[str, str]:
    """(original id, integration run id) for either of the two ids."""
    resolved = resolve_run(layer, run_id)
    data = _state(layer, resolved)
    if data.get("adw") == ADW_NAME:
        info = (data.get("definition") or {}).get("integration") or data.get("integration") or {}
        return str(info.get("run") or ""), resolved
    record = read_record(layer, resolved)
    if not record:
        raise IntegrateError("not-waiting", f"no integration of run {resolved} is waiting for approval")
    return resolved, str(record["run"])


def approve(layer_dir: Path, run_id: str, who: str, *, detach: bool = False, as_json: bool = False,
            spawn: Any = _spawn) -> int:
    """A person approves a waiting integration (the original's or the integration run's id): writes
    agents/<integration>/approval.json and resumes the run, which then ships."""
    layer_dir = Path(layer_dir)
    layer = ir.load(layer_dir)
    who = " ".join(str(who or "").split())
    if not who or len(who) > MAX_WHO or not who.isprintable():
        raise _Usage(f"--who: 1 to {MAX_WHO} printable characters (who approves)")
    original_id, integration_id = _integration_of(layer, run_id)
    data = _state(layer, integration_id)
    waiting = next((s for s in data.get("steps") or [] if s.get("status") == "waiting"), {})
    if data.get("status") != "waiting" or waiting.get("name") != "approve":
        raise IntegrateError("not-waiting", f"integration run {integration_id} is {data.get('status') or 'unknown'}, "
                                            "not waiting for approval")
    _write_private(state_module.run_dir(layer_dir, integration_id) / APPROVAL_FILE, {"by": who, "at": _stamp()})
    if not as_json:
        print(f"approved by {who}: integration run {integration_id} of {original_id} continues", flush=True)
    return run(layer_dir, original_id, resume=True, adw_id=integration_id, detach=detach, as_json=as_json,
               by=str((data.get("integration") or {}).get("by") or "cli"), spawn=spawn)


def after_run(layer: Any, state: Any, args: Any = None, spawn: Any = _spawn) -> None:
    """Called by the runner after a run wrote its summary (beside decide.after_run): the stand-in
    decider starts the integration, detached, when the run ended handed over and is eligible, the
    decider's mode is `auto` and the layer's settings say "decider": true. No agent call: whether
    a run can be integrated is decided by rules. Never raises, never changes the ended run."""
    from . import decide

    try:
        if (getattr(state, "status", "") != "handed-over" or getattr(state, "adw", "") == ADW_NAME
                or getattr(state, "integration", None) or getattr(state, "fanout_parent", None)):
            return
        if not settings(layer.layer_dir)["decider"]:
            return
        if decide.mode_for(layer, getattr(args, "decider", None))[0] != "auto":
            return
        plain = ir.load(Path(layer.layer_dir))
        eligible(plain, state.adw_id)
        new_id = state_module.new_adw_id()
        while state_module.run_dir(plain.layer_dir, new_id).exists():
            new_id = state_module.new_adw_id()
        with contextlib.redirect_stdout(None):
            run(Path(plain.layer_dir), state.adw_id, detach=True, by="decider", adw_id=new_id,
                dry_run=bool(getattr(state, "dry_run", False)), spawn=spawn)
        print(f"[adw {state.adw_id}] decider: integrating with an agent: run {new_id}", flush=True)
    except IntegrateError as exc:
        print(f"[adw {state.adw_id}] decider: not integrated ({exc.reason}: {exc.message}); the run stays with a "
              "person", flush=True)
    except Exception as exc:  # noqa: BLE001 — the run has ended; an integration that does not start is not its failure
        print(f"[adw {state.adw_id}] decider: the integration did not start: {type(exc).__name__}: {exc}; the run "
              "stays with a person", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="integrate", description="Bring a handed-over run onto its target branch "
                                     "with an agent: prepare, resolve, checks, review, ship.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("run", help="integrate RUN (a run id or its branch)")
    start.add_argument("run", metavar="RUN")
    start.add_argument("--model-set", default="", metavar="TIER")
    start.add_argument("--detach", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--resume", action="store_true")
    start.add_argument("--adw-id", default="")
    start.add_argument("--by", default="cli")
    start.add_argument("--json", action="store_true")
    look = sub.add_parser("preview", help="what integrating RUN would do; nothing is started or written")
    look.add_argument("run", metavar="RUN")
    look.add_argument("--json", action="store_true")
    ok = sub.add_parser("approve", help="approve a waiting integration; it then ships")
    ok.add_argument("run", metavar="RUN")
    ok.add_argument("--who", default="")
    ok.add_argument("--detach", action="store_true")
    ok.add_argument("--json", action="store_true")
    conflicts = sub.add_parser("conflicts", help="which files conflicted, and how often")
    conflicts.add_argument("--json", action="store_true")
    show = sub.add_parser("settings", help="the layer's templates/integrate.json in words")
    show.add_argument("--json", action="store_true")
    return parser


def main(layer_dir: Path, argv: list[str] | None = None, prog: str = "") -> int:
    """`adws/integrate.py run|preview|approve|conflicts|settings …` (and `al integrate`, which passes
    prog="al "). 0 landed or handed over, 1 failed, 2 wrong use, 3 refused, 75 waiting."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    name = f"{prog}integrate"
    as_json = bool(getattr(args, "json", False))
    layer_dir = Path(layer_dir)
    try:
        if args.command == "run":
            if not RUN_ID.match(args.adw_id or "00000000"):
                raise _Usage("--adw-id: 8 hexadecimal characters")
            return run(layer_dir, args.run, model_set=args.model_set, dry_run=args.dry_run, detach=args.detach,
                       resume=args.resume, adw_id=args.adw_id, by=args.by, as_json=as_json)
        if args.command == "approve":
            return approve(layer_dir, args.run, args.who, detach=args.detach, as_json=as_json)
        if args.command == "settings":
            data = settings(layer_dir)
            print(json.dumps(data) if as_json else "\n".join(settings_lines(data)))
            return 0
        layer = ir.load(layer_dir)
        if args.command == "conflicts":
            report = conflict_report(layer)
            print(json.dumps(report) if as_json else "\n".join(report_lines(report)))
            return 0
        found = preview(layer, args.run)
        if as_json:
            print(json.dumps(found))
            return 0 if found["eligible"] else REFUSED_EXIT
        if not found["eligible"]:
            print(f"{name}: {found['reason']}: {found['why']}", file=sys.stderr)
            return REFUSED_EXIT
        print("\n".join(preview_lines(found)))
        return 0
    except IntegrateError as exc:
        if as_json:
            print(json.dumps({"status": "error", "reason": exc.reason, "error": exc.message}))
        print(f"{name}: {exc.reason}: {exc.message}", file=sys.stderr)
        return REFUSED_EXIT
    except _Usage as exc:
        if as_json:
            print(json.dumps({"status": "error", "reason": "usage", "error": str(exc)}))
        print(f"{name}: error: {exc}", file=sys.stderr)
        return 2
    except (LayerError, StepFailed) as exc:
        if as_json:
            print(json.dumps({"status": "error", "reason": "error", "error": str(exc)}))
        print(f"{name}: error: {exc}", file=sys.stderr)
        return 1
