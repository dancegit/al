"""Run events: the append-only record of what a run did, in `agents/<id>/events.jsonl`.

One JSON object per line, appended with a single `write(2)` on a file opened with O_APPEND (so a
reader never sees half a line from us, and concurrent writers never interleave inside a line):

    {"ts": "2026-11-15T10:04:12.345+00:00", "seq": 7, "type": "check.start", "step": "test", ...}

`ts` is ISO-8601 UTC, `seq` increases by one per event within the file (a resumed run continues
after the last seq on disk), `type` is one of TYPES. The fields of each type are documented in
docs/DESIGN.md ("Run events"); readers must ignore types and fields they do not know.

This module owns the format. Call sites use `say()` where the run already prints a progress line
(the line and the event are produced together, so they cannot drift) and `emit()` where it does
not. Nothing here ever raises into a run: a failed write is reported once on stderr and dropped.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

FILE_NAME = "events.jsonl"
TYPES = (
    "run.start", "run.end",
    "slot.wait", "slot.acquire", "slot.release",
    "env.container.start", "env.container.stop",
    "env.build.start", "env.build.end", "env.preflight",
    "step.start", "step.end",
    "agent.start", "agent.end",
    "check.start", "check.end",
    "script.start", "script.end", "script.event",
    "budget.stop",
    "limit.status", "limit.stop", "limit.wait",
    "review.round", "gate.decision", "ship.result", "handoff",
    "notify.sent", "notify.failed",
    "fanout.candidate",
    "delivery.frozen",
    "promotion.opened", "promotion.decision", "promotion.excluded", "promotion.waiting", "promotion.approved",
    "promotion.merged", "promotion.tagged", "promotion.deployed", "promotion.back-merge", "promotion.closed",
    "step.discipline",
)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _last_seq(path: Path) -> int:
    """The seq of the last complete line (0 for a new or unreadable file); reads only the tail."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            lines = handle.read().splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("seq"), int):
            return value["seq"]
    return 0


class Recorder:
    """Appends events of one run to its events.jsonl."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self.seq = _last_seq(self.path)
        self.failed = False

    def emit(self, type_: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            event = {"ts": timestamp(), "seq": self.seq + 1, "type": type_}
            event.update({key: value for key, value in fields.items() if key not in event})
            try:
                data = (json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(handle, data)
                finally:
                    os.close(handle)
            except (OSError, TypeError, ValueError) as exc:
                if not self.failed:
                    self.failed = True
                    print(f"[events] warning: cannot write {self.path}: {exc}; continuing without events",
                          file=sys.stderr, flush=True)
                return None
            self.seq += 1
            return event


_current: Recorder | None = None


def path_for(layer_dir: Path, adw_id: str) -> Path:
    return Path(layer_dir) / "agents" / adw_id / FILE_NAME


@contextlib.contextmanager
def recording(layer_dir: Path, adw_id: str) -> Iterator[Recorder]:
    """Every emit() inside the block goes to this run's events.jsonl (one run per process)."""
    global _current
    previous = _current
    _current = Recorder(path_for(layer_dir, adw_id))
    try:
        yield _current
    finally:
        _current = previous


def emit(type_: str, **fields: Any) -> dict[str, Any] | None:
    """Record one event of the current run; a no-op outside `recording()`. Never raises."""
    recorder = _current
    if recorder is None:
        return None
    try:
        return recorder.emit(type_, fields)
    except Exception as exc:  # noqa: BLE001 — an event is never worth failing a run for
        print(f"[events] warning: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


def say(line: str, type_: str, **fields: Any) -> None:
    """Print a progress line and record the matching event."""
    print(line, flush=True)
    emit(type_, **fields)


def read(path: Path) -> list[dict[str, Any]]:
    """Every complete, parseable event of a file (a torn or foreign line is skipped)."""
    out: list[dict[str, Any]] = []
    try:
        data = Path(path).read_bytes()
    except OSError:
        return out
    for line in data.split(b"\n")[:-1] if not data.endswith(b"\n") else data.split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "type" in value:
            out.append(value)
    return out
