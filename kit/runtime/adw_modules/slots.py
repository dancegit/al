"""Layer-wide concurrency limits shared by every trigger and manual run.

A pool (`runs`, `agents`, `checks`) has N slot files under agents/.slots; holding a slot means
holding an exclusive flock on one of them. Locks die with the process, so a crashed run can
never leak a slot.

On a host that runs self-contained runs every run has its own copy of the layer, so AL_SLOTS_DIR
names the one directory all runs of that host share: the pools are per host.
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import events
from .errors import StepFailed

POOL_FIELD = {"runs": "maxRuns", "agents": "maxAgents", "checks": "maxChecks"}


SLOTS_DIR_ENV = "AL_SLOTS_DIR"


def limits(layer: Any) -> dict[str, Any]:
    option = layer.ir.get("concurrency") or {}
    runs = int(option.get("maxRuns", 4))
    placement = layer.ir.get("placement")
    if isinstance(placement, dict) and int(placement.get("maxRunsPerHost") or 0) > 0:
        runs = int(placement["maxRunsPerHost"])  # (placement #:max-runs-per-host N): the same on every host
    return {"runs": runs, "agents": int(option.get("maxAgents", 2)),
            "checks": int(option.get("maxChecks", 2)), "wait": int(option.get("waitSeconds", 3600))}


def _directory(layer: Any) -> Path:
    """Where the slot files are: AL_SLOTS_DIR when it is set (one directory for every run of a
    host), else agents/.slots of the layer."""
    named = os.environ.get(SLOTS_DIR_ENV, "").strip()
    return Path(named).expanduser() if named else Path(layer.layer_dir) / "agents" / ".slots"


@contextmanager
def slot(layer: Any, pool: str, who: str = "", poll: float = 0.5) -> Iterator[int]:
    """Hold one slot of `pool` for the duration of the block; waits up to waitSeconds."""
    config = limits(layer)
    limit = max(1, config[pool])
    directory = _directory(layer)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + config["wait"]
    announced = False
    while True:
        for index in range(limit):
            handle = os.open(directory / f"{pool}.{index}.lock", os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(handle)
                continue
            acquired = time.monotonic()
            events.emit("slot.acquire", pool=pool, slot=index, waited=round(acquired - started, 3), who=who)
            try:
                os.ftruncate(handle, 0)
                os.write(handle, f"{os.getpid()} {who}\n".encode())
                yield index
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                os.close(handle)
                events.emit("slot.release", pool=pool, slot=index, held=round(time.monotonic() - acquired, 3), who=who)
            return
        if time.monotonic() >= deadline:
            raise StepFailed(who or pool, f"no free {pool} slot ({limit}) after {config['wait']}s")
        if not announced:
            events.say(f"[slots] {who or pool}: waiting for a free {pool} slot ({limit} in use)", "slot.wait",
                       pool=pool, limit=limit, who=who)
            announced = True
        time.sleep(poll)


def free_slots(layer: Any, pool: str) -> int:
    """How many slots of `pool` nobody holds right now: each slot file is probed with a
    non-blocking flock that is released at once. Informational only (a start says it will wait);
    the run's real wait for a slot stays in slot()."""
    limit = max(1, limits(layer)[pool])
    directory = _directory(layer)
    free = 0
    for index in range(limit):
        path = directory / f"{pool}.{index}.lock"
        if not path.exists():
            free += 1
            continue
        try:
            handle = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
            free += 1
        finally:
            os.close(handle)
    return free


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
