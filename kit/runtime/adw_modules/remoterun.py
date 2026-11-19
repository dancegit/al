"""The home machine's side of a run that executes on another host: its proxy.

The process that would have run the workflow here (`adws/adw_<name>.py`, however it was started)
becomes the run's proxy when the scheduler picks another host:

  run_remote   starts the run on the host (`adws/host.py start`, the seed on standard input);
  follow       pulls the run's record every POLL seconds until the run's remote part ended; a host
               that does not answer for #:lost-after seconds gives a `lost` run (mark_lost);
  take_over    builds the normal environment stack here, fetches the pushed branch from the git
               source into the run's quarantine through `git index-pack --strict` (fetch_result),
               moves the fresh worktree to that commit (place_result) and runs what is left of the
               workflow, i.e. its (ship) step, or the handoff of a failed run, through the same
               runner._run as a resumed local run. The gate, the merge and the handoff are the
               unchanged ship.py, review.py and gitsafe.py.
  restart      starts a lost run again on another host, from the last commit it pushed.

While the run is on the host this process writes nothing to the run's events.jsonl and run.log:
they are the host's bytes, applied range by range (recordsync). Its own events of the start travel
in the seed and open the host's events; everything after the remote part is appended here.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import events, gitsafe, hostlink, placement, recordsync, selfrun, state as state_module
from .errors import LayerError, StepFailed

POLL = 2.0
START_TIMEOUT = 1800          # the host clones before it answers
PULL_TIMEOUT = 120
LOST_ENV = "AL_REMOTE_LOST_SECONDS"
GONE_SECONDS = 15.0           # a run whose process on the host is gone without a final state
TAKEOVER = "take-over"
INCOMING = "incoming.git"


def _run_dir(layer: Any, run_id: str) -> Path:
    return state_module.run_dir(layer.layer_dir, run_id)


def lost_after(layer: Any) -> float:
    override = os.environ.get(LOST_ENV, "").strip()
    if override:
        with contextlib.suppress(ValueError):
            return max(0.5, float(override))
    return float((placement.declared(layer) or {}).get("lost_after") or 120)


def _say(state: Any, text: str) -> None:
    print(f"[adw {state.adw_id}] {text}", flush=True)


def _last_seq(layer: Any, run_id: str) -> int:
    return events._last_seq(events.path_for(layer.layer_dir, run_id))


# ---------------------------------------------------------------- start

def build_seed(layer: Any, state: Any, adw: dict[str, Any], decision: dict[str, Any],
               restart: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the host needs to run this run: no path, no address, no secret of this machine."""
    host = str(decision["host"])
    opening = [{"type": "placement.decision", "fields": {"host": host, "reasons": decision.get("reasons") or [],
                                                         "skipped": decision.get("skipped") or []}},
               {"type": "remote.start", "fields": {"host": host, "adw": state.adw, "adw_id": state.adw_id,
                                                   "base_branch": layer.default_branch,
                                                   "restart": bool(restart)}}]
    return {"protocol": selfrun.PROTOCOL, "run": state.adw_id, "adw": state.adw, "host": host,
            "base_branch": layer.default_branch, "layer_rev": placement.layer_revision(layer),
            "prompt": state.prompt, "klass": state.klass, "class_fixed": bool(state.class_fixed),
            "model_set": state.model_set, "overrides": state.overrides, "models": state.models,
            "max_cost": (state.overrides or {}).get("max_cost"), "limits": state.budget.get("limits") or {},
            "dry_run": bool(state.dry_run), "source": state.source, "delivery": state.delivery or {},
            "seq": _last_seq(layer, state.adw_id), "events": opening, "restart": restart or {}}


def _finish_hooks(state: Any, request: Any) -> None:
    if request is not None and getattr(request, "hooks", None) is not None:
        try:
            request.hooks.on_finish(state)
        except Exception as exc:  # the source hook is foreign code
            _say(state, f"source on_finish failed: {type(exc).__name__}: {exc}")


def _failed_before_start(layer: Any, state: Any, request: Any, reason: str) -> int:
    state.status, state.error, state.ended = "failed", reason, state_module.now()
    state.remote["phase"] = "gate"
    state.save(layer.layer_dir)
    _say(state, f"run failed: {reason}")
    _finish_hooks(state, request)
    return 1


def run_remote(layer: Any, state: Any, adw: dict[str, Any], args: argparse.Namespace, request: Any,
               decision: dict[str, Any], restart: dict[str, Any] | None = None) -> int:
    """Start the run on the decided host, follow it, then take it over here. Returns the exit code."""
    host = str(decision["host"])
    if request is not None and getattr(request, "hooks", None) is not None:
        request.hooks.on_start(state)
    previous = state.remote.get("host") if restart is not None else None
    state.remote = {"host": host, "phase": "remote", "status": "starting", "last_seen": state_module.now(),
                    **({"restart_of": previous} if previous else {})}
    state.status, state.ended = "running", ""
    state.mark_alive()
    state.save(layer.layer_dir)
    _say(state, f"placed on host {host}: {'; '.join(decision.get('reasons') or [])}")
    for item in decision.get("skipped") or []:
        _say(state, f"host {item['host']} skipped: {item['reason']}")
    try:
        link = hostlink.link_for(layer, host)
        link.tool(layer, host, "start", stdin=json.dumps(build_seed(layer, state, adw, decision, restart)),
                  timeout=START_TIMEOUT)
    except (hostlink.HostSilent, LayerError) as exc:
        reason = exc.reason if isinstance(exc, hostlink.HostSilent) else str(exc)
        return _failed_before_start(layer, state, request, f"host {host} did not start the run: {reason}")
    return attend(layer, state, adw, args, request, link)


def attend(layer: Any, state: Any, adw: dict[str, Any], args: argparse.Namespace, request: Any, link: Any) -> int:
    """Follow the run until its remote part ended, then take over; or mark it lost."""
    outcome = follow(layer, state, link)
    if outcome != "ended":
        mark_lost(layer, state, outcome)
        _finish_hooks(state_module.load(layer.layer_dir, state.adw_id), request)
        return 1
    return take_over(layer, state, adw, args, request, link)


# ---------------------------------------------------------------- follow

def _touch(layer: Any, run_id: str, **remote: Any) -> None:
    """Refresh this machine's liveness of a remote run (and its `remote` block) in state.json."""
    path = _run_dir(layer, run_id) / "state.json"
    data = selfrun._read_json(path)
    if not data:
        return
    block = data.get("remote") if isinstance(data.get("remote"), dict) else {}
    block.update(remote, last_seen=state_module.now())
    data.update(remote=block, heartbeat=state_module.now(), pid=os.getpid())
    recordsync.write_json(path, data)


class _LogTail:
    """Prints what arrives in the run's run.log, so an attached terminal shows the run's progress."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.offset = path.stat().st_size
        except OSError:
            self.offset = 0

    def show(self) -> None:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return
        cut = data.rfind(b"\n") + 1
        if cut:
            self.offset += cut
            for line in data[:cut].decode("utf-8", "replace").splitlines():
                print(line.split(" ", 1)[1] if line[:2] == "20" and " " in line else line, flush=True)


def pull(layer: Any, state: Any, link: Any) -> dict[str, Any]:
    """One pull: ask the host for what the cursor has not seen and apply it."""
    host = str(state.remote.get("host") or "")
    cursor = recordsync.load_cursor(layer, state.adw_id)
    frame = link.tool(layer, host, "pull", state.adw_id, stdin=json.dumps(cursor), timeout=PULL_TIMEOUT, raw=True)
    return recordsync.apply(layer, state.adw_id, frame)


def follow(layer: Any, state: Any, link: Any, poll: float | None = None) -> str:
    """Pull the record until the run's remote part ended ("ended"). A host that gives no usable
    answer for #:lost-after seconds, or whose run process is gone without a final state, ends the
    loop with the reason the run is lost."""
    poll = POLL if poll is None else poll
    limit = lost_after(layer)
    tail = _LogTail(_run_dir(layer, state.adw_id) / "run.log")
    last_ok = time.monotonic()
    gone_since: float | None = None
    said = ""
    while True:
        try:
            result = pull(layer, state, link)
        except (hostlink.HostSilent, recordsync.Refused, OSError) as exc:
            reason = exc.reason if isinstance(exc, hostlink.HostSilent) else str(exc)
            if reason != said:
                said = reason
                _say(state, f"host {state.remote.get('host')}: {reason}; trying again")
            if time.monotonic() - last_ok >= limit:
                return f"host {state.remote.get('host')} gave no answer for {int(limit)} s ({reason})"
            time.sleep(min(poll, max(0.2, limit / 4)))
            continue
        last_ok, said = time.monotonic(), ""
        tail.show()
        _touch(layer, state.adw_id)
        status = result["status"]
        if status in recordsync.REMOTE_ENDED and not result["alive"] and not result["more"] and not result["changed"]:
            return "ended"
        if status not in recordsync.REMOTE_ENDED and not result["alive"]:
            gone_since = gone_since or time.monotonic()
            if time.monotonic() - gone_since >= min(GONE_SECONDS, limit):
                return f"the run's process on host {state.remote.get('host')} is gone (its state there: {status})"
        else:
            gone_since = None
        if not (result["more"] or (status in recordsync.REMOTE_ENDED and result["changed"])):
            time.sleep(poll)


def mark_lost(layer: Any, state: Any, reason: str) -> None:
    """The run is `lost`: what was last received stays, the reason and the time are recorded."""
    current = state_module.load(layer.layer_dir, state.adw_id)
    current.status, current.ended, current.error = "lost", state_module.now(), reason
    current.remote["lost"] = {"at": state_module.now(), "reason": reason, "host": current.remote.get("host")}
    current.remote["phase"] = "lost"
    current.save(layer.layer_dir)
    state.status, state.remote = current.status, current.remote
    recordsync.shift_to_end(layer, state.adw_id)
    with events.recording(layer.layer_dir, state.adw_id):
        events.emit("remote.lost", host=current.remote.get("host"), reason=reason,
                    pushed=(current.remote.get("pushed") or {}).get("sha", ""))
    line = (f"[adw {state.adw_id}] run lost: {reason}; start it again on another host: "
            f"uv run adws/adw_{state.adw}.py --restart --adw-id {state.adw_id}")
    print(line, flush=True)
    with contextlib.suppress(OSError):
        from . import runner

        with (_run_dir(layer, state.adw_id) / "run.log").open("a", encoding="utf-8") as log:
            log.write(f"{runner.log_stamp()} {line}\n")


# ---------------------------------------------------------------- take-over

def fetch_result(layer: Any, state: Any, stack: Any) -> str:
    """Bring the commit the run delivered from the git source onto this machine: fetched into a
    temporary bare repository, checked to be exactly the delivered commit, then repacked and fed to
    `git index-pack --strict` in the run's quarantine (a sandboxed stack) or the application's
    object store. No ref of the application moves. Returns the commit."""
    delivered = state.remote.get("delivered") or {}
    sha, branch = str(delivered.get("sha") or ""), str(delivered.get("branch") or "")
    if not gitsafe._OID.fullmatch(sha):
        raise StepFailed(TAKEOVER, "the run recorded no delivered commit")
    selfrun.check_branch(layer, branch, state.adw_id, TAKEOVER)
    url = selfrun.source_url(layer)
    app_git = Path(layer.app_root).resolve() / ".git"
    incoming = _run_dir(layer, state.adw_id) / INCOMING
    shutil.rmtree(incoming, ignore_errors=True)
    sandbox = getattr(stack, "sandbox", None)
    try:
        selfrun._git(None, ["init", "--quiet", "--bare", str(incoming)], TAKEOVER)
        (incoming / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (incoming / "objects" / "info" / "alternates").write_text(f"{app_git / 'objects'}\n", encoding="utf-8")
        got = selfrun._fetch(incoming, url, f"refs/heads/{branch}", TAKEOVER)
        if got != sha:
            raise StepFailed(TAKEOVER, "the pushed branch is not the commit the run delivered")
        env = {**gitsafe._base_env(), "GIT_DIR": str(incoming)}
        # what the application does not have yet: reachable from the commit, not from its own refs
        listing = gitsafe._exec(["git", *gitsafe.HARDENING, "rev-list", "--single-worktree", "--objects", sha, "--not", "--all"],
                                {**gitsafe._base_env(), "GIT_DIR": str(app_git),
                                 "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(incoming / "objects")},
                                app_git.parent, TAKEOVER, True, gitsafe.GIT_TIMEOUT).stdout
        if listing.strip():
            if sandbox is not None:
                target = {**gitsafe._base_env(), "GIT_DIR": str(app_git), "GIT_OBJECT_DIRECTORY": str(sandbox.quarantine),
                          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(app_git / "objects")}
                sandbox.quarantine.mkdir(parents=True, exist_ok=True)
            else:
                target = {**gitsafe._base_env(), "GIT_DIR": str(app_git)}
            with tempfile.TemporaryFile() as objects, tempfile.TemporaryFile() as pack:
                objects.write(listing.encode())
                objects.seek(0)
                gitsafe._exec(["git", *gitsafe.HARDENING, "pack-objects", "--stdout"], env, incoming, TAKEOVER, True,
                              gitsafe.GIT_TIMEOUT, stdin=objects, stdout=pack, text=False)
                pack.seek(0)
                gitsafe._exec(["git", *gitsafe.HARDENING, "index-pack", "--strict", "--stdin"], target, app_git.parent,
                              TAKEOVER, True, gitsafe.GIT_TIMEOUT, stdin=pack, text=False)
        return sha
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


def place_result(layer: Any, state: Any, stack: Any) -> None:
    """After the environment is set up here: the fresh worktree moves to the delivered commit."""
    delivered = state.remote.get("delivered") or {}
    if not delivered.get("sha") or not state.worktree:
        return   # the run changed nothing (or has no worktree): nothing to bring over
    sha = fetch_result(layer, state, stack)
    selfrun.move_worktree(layer, state, stack, sha, str(delivered["branch"]), TAKEOVER)
    state.remote["taken_over"] = {"sha": sha, "at": state_module.now()}
    state.save(layer.layer_dir)
    events.say(f"[adw {state.adw_id}] take-over: {state.branch} at {sha[:12]} fetched from the git source "
               f"(host {state.remote.get('host')})", "remote.takeover", host=state.remote.get("host"), sha=sha,
               branch=state.branch)


def remote_failure(state: Any) -> StepFailed | None:
    """The failure a run recorded on its host, to be raised here so the handoff happens here."""
    if str(state.remote.get("status") or "") != "failed":
        return None
    record = next((r for r in state.steps if r.get("status") == "failed"), None)
    if record is not None:
        name = str(record.get("name"))
        return StepFailed(name, str(record.get("detail") or "failed").removeprefix(f"{name}: "))
    return StepFailed("run", str(getattr(state, "error", "") or f"the run failed on host {state.remote.get('host')}"))


def take_over(layer: Any, state: Any, adw: dict[str, Any], args: argparse.Namespace, request: Any, link: Any) -> int:
    """The remote part ended: run what is left of the workflow here, through the home gate."""
    from . import runner

    run_id = state.adw_id
    with contextlib.suppress(hostlink.HostSilent, recordsync.Refused, OSError):
        pull(layer, state, link)   # one last pull: nothing of the record stays behind
    state = state_module.load(layer.layer_dir, run_id)
    remote_status = str(state.remote.get("status") or "")
    for record in state.steps:   # a step the host was in when it stopped never ended there
        if record.get("status") == "running":
            record.update(status="failed", ended=state_module.now(), detail=record.get("detail") or "stopped on the host")
    state.remote["phase"] = "gate"
    state.worktree, state.ports, state.status, state.ended = "", [], "running", ""
    state.save(layer.layer_dir)
    recordsync.shift_to_end(layer, run_id)
    log_path = _run_dir(layer, run_id) / "run.log"
    with log_path.open("a", encoding="utf-8") as log, events.recording(layer.layer_dir, run_id):
        tee = runner._Tee(sys.stdout, log)
        try:
            with contextlib.redirect_stdout(tee):
                events.say(f"[adw {run_id}] host {state.remote.get('host')}: the run's remote part ended "
                           f"{remote_status}; continuing here", "remote.sync", host=state.remote.get("host"),
                           status=remote_status)
                exit_code = runner._run(layer, state, adw, args, request, take_over=True)
                print("\n".join(runner.summary_lines(layer, state)))
        finally:
            tee.close()
    try:
        link.tool(layer, str(state.remote.get("host")), "clean", run_id, timeout=120)
    except (hostlink.HostSilent, LayerError) as exc:
        print(f"[adw {run_id}] note: the run's storage on host {state.remote.get('host')} was not removed: {exc}")
    runner._speak(layer, f"ADW {state.adw} {run_id} {state.status}. Ship: {state.ship.get('action', 'none')}.")
    return exit_code


# ---------------------------------------------------------------- follow again, restart

def reattach(layer: Any, adw: dict[str, Any], args: argparse.Namespace) -> int:
    """`--follow --adw-id RUN`: the proxy of a remote run died; follow the run again and finish it."""
    state = state_module.load(layer.layer_dir, args.adw_id)
    if not state.remote.get("host"):
        raise LayerError(f"run {state.adw_id} did not execute on another host: nothing to follow")
    if state.status not in ("running", "lost"):
        raise LayerError(f"run {state.adw_id} is {state.status}: nothing to follow")
    if state.remote.get("taken_over") or state.remote.get("phase") == "gate" and state.worktree:
        raise LayerError(f"run {state.adw_id} was already taken over here; continue it with --resume")
    host = str(state.remote["host"])
    state.status, state.ended, state.error = "running", "", ""
    state.remote.pop("lost", None)
    state.remote["phase"] = "remote" if state.remote.get("status") not in recordsync.REMOTE_ENDED else "gate"
    state.mark_alive()
    state.save(layer.layer_dir)
    _say(state, f"following the run on host {host} again")
    try:
        link = hostlink.link_for(layer, host)
    except hostlink.HostSilent as exc:
        raise LayerError(str(exc)) from exc
    return attend(layer, state, adw, args, None, link)


def _pushed(layer: Any, state: Any) -> dict[str, Any]:
    """What a restart starts from: {sha, branch, state, files} of the last push, or {}."""
    pushed = state.remote.get("pushed") or {}
    branch = str(state.remote.get("branch") or state.branch or "")
    if not pushed.get("sha") or not branch:
        return {}
    run_dir = _run_dir(layer, state.adw_id)
    files: dict[str, str] = {}
    specs = Path(layer.layer_dir) / "specs"
    if specs.is_dir():
        for entry in sorted(specs.iterdir()):
            if state.adw_id in entry.name and entry.is_file() and not entry.is_symlink():
                with contextlib.suppress(OSError, UnicodeDecodeError):
                    files[entry.name] = entry.read_text(encoding="utf-8")
    return {"sha": str(pushed["sha"]), "branch": branch, "state": selfrun._read_json(run_dir / selfrun.PUSHED_STATE),
            "files": files}


def restart(layer: Any, adw: dict[str, Any], args: argparse.Namespace) -> int:
    """`--restart --adw-id RUN [--on HOST]`: start a lost run again on another host, from the last
    commit it pushed: the steps that finished before that push are skipped, later ones run again."""
    state = state_module.load(layer.layer_dir, args.adw_id)
    lost = state.status == "lost" or (state.status == "failed" and state.remote.get("lost"))
    if not lost:
        raise LayerError(f"run {state.adw_id} is {state.status}, not lost: only a lost run is started again")
    old = str(state.remote.get("host") or "")
    on = getattr(args, "on", None)
    try:
        decision = placement.decide(layer, adw, state, on=on, exclude=() if on else (placement.HOME, old))
    except placement.Refused as exc:
        raise LayerError(f"refused: {exc}") from exc
    if decision["host"] == placement.HOME:
        raise LayerError("a lost run is started again on another host, not on home; name one with --on HOST")
    seed_restart = _pushed(layer, state) or {"sha": "", "branch": "", "state": {}, "files": {}}
    recordsync.restart_cursor(layer, state.adw_id)
    state.error = ""
    return run_remote(layer, state, adw, args, None, decision, restart=seed_restart)
