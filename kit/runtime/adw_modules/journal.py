"""Follow a foreign run journal: the folder a hand-built workflow script writes its own record to
((script … #:journal ".work/adw/{adw_id}")).

While the script runs, a daemon thread reads the folder's `events.jsonl` from where it left off
(complete lines only) and mirrors each line that is a JSON object as a `script.event` of the step,
with the original object under `foreign`; anything else is counted as malformed. Each immediate
sub-folder is an inner step: its `prompt.md` and `output*` files (.md, .json, .txt, .log) are
copied into the run (agents/<id>/scripts/<step>/journal/<inner>/<file>) so they become documents
of the run. A final sweep runs after the script has ended.

Only what lies inside the worktree's real path is read: a journal folder that resolves elsewhere
(a symlink out of the worktree) is refused, and only regular files (never symlinks) up to
MAX_BYTES are copied. Nothing here ever raises into the run."""
from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

from . import events

MAX_BYTES = 1024 * 1024
DOC_SUFFIXES = (".md", ".json", ".txt", ".log")


def _is_document(name: str) -> bool:
    return name == "prompt.md" or (name.startswith("output") and name.endswith(DOC_SUFFIXES))


class Follower:
    def __init__(self, layer_dir: Path, worktree: Path, relative: str, step: str, dest: Path,
                 poll: float = 0.5) -> None:
        self.layer_dir = Path(layer_dir)
        self.worktree = Path(worktree)
        self.relative = relative
        self.step = step
        self.dest = Path(dest)
        self.poll = poll
        self.offset = 0
        self.line = 0
        self.summary: dict[str, Any] = {"path": relative, "found": False, "lines": 0, "malformed": 0,
                                        "refused": "", "documents": []}
        self.copied: dict[str, tuple[int, int]] = {}  # source → (size, mtime_ns) last copied
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"journal-{step}", daemon=True)

    def start(self) -> "Follower":
        self.thread.start()
        return self

    def stop(self) -> dict[str, Any]:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)
        self._sweep()
        with self.lock:
            out = dict(self.summary)
            out["documents"] = sorted(self.summary["documents"], key=lambda d: d["path"])
            return out

    def _loop(self) -> None:
        while not self.stopped.wait(self.poll):
            self._sweep()

    def _folder(self) -> Path | None:
        """The journal folder if it may be read; None (with `refused` set) otherwise."""
        folder = self.worktree / self.relative
        try:
            real = Path(os.path.realpath(folder))
            inside = Path(os.path.realpath(self.worktree))
        except OSError:
            return None
        if real != inside and inside not in real.parents:
            self.summary["refused"] = f"{self.relative} resolves to {real}, outside the worktree: not read"
            return None
        return real

    def _sweep(self) -> None:
        with self.lock:
            try:
                folder = self._folder()
                if folder is None or not folder.is_dir():
                    return
                self.summary["found"] = True
                self._read_events(folder)
                self._copy_documents(folder)
            except Exception as exc:  # noqa: BLE001 — following a journal is never worth failing a run for
                self.summary["error"] = f"{type(exc).__name__}: {exc}"[:300]

    def _read_events(self, folder: Path) -> None:
        path = folder / "events.jsonl"
        try:
            info = os.lstat(path)
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_size <= self.offset:
            return
        with path.open("rb") as handle:
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b"\n")
        if end < 0:
            return  # no complete line yet
        self.offset += end + 1
        for raw in data[:end].split(b"\n"):
            if not raw.strip():
                continue
            self.line += 1
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
            if not isinstance(value, dict):
                self.summary["malformed"] += 1
                continue
            self.summary["lines"] += 1
            events.emit("script.event", step=self.step, owner=self.step, line=self.line, foreign=value)

    def _copy_documents(self, folder: Path) -> None:
        for inner in sorted(folder.iterdir()):
            try:
                if not stat.S_ISDIR(os.lstat(inner).st_mode):
                    continue
                files = sorted(inner.iterdir())
            except OSError:
                continue
            for source in files:
                if not _is_document(source.name):
                    continue
                try:
                    info = os.lstat(source)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    continue
                key = str(source)
                if self.copied.get(key) == (info.st_size, info.st_mtime_ns):
                    continue
                target = self.dest / inner.name / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, MAX_BYTES)
                if key not in self.copied:
                    self.summary["documents"].append({"path": target.relative_to(self.layer_dir).as_posix(),
                                                      "inner": inner.name, "file": source.name})
                self.copied[key] = (info.st_size, info.st_mtime_ns)
