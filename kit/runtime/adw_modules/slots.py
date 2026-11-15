"""Layer-wide concurrency limits shared by every trigger and manual run.

A pool (`runs`, `agents`, `checks`) has N slot files under agents/.slots; holding a slot means
holding an exclusive flock on one of them. Locks die with the process, so a crashed run can
never leak a slot.
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


def limits(layer: Any) -> dict[str, Any]:
    option = layer.ir.get("concurrency") or {}
    return {"runs": int(option.get("maxRuns", 4)), "agents": int(option.get("maxAgents", 2)),
            "checks": int(option.get("maxChecks", 2)), "wait": int(option.get("waitSeconds", 3600))}


@contextmanager
def slot(layer: Any, pool: str, who: str = "", poll: float = 0.5) -> Iterator[int]:
    """Hold one slot of `pool` for the duration of the block; waits up to waitSeconds."""
    config = limits(layer)
    limit = max(1, config[pool])
    directory = Path(layer.layer_dir) / "agents" / ".slots"
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
