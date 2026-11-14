#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PITER · T — file-watch trigger (polling: mtime + size, no inotify dependency).

Watches the tasklist file(s) and spec inboxes of the ADWs that list this
trigger. When the snapshot changed and then stayed quiet for
`debounceSeconds`, it behaves like one cron poll (claim + launch).

    trigger_watch.py [--layer DIR] [--trigger ID] [--once] [--dry-run]

--once: one poll right away (as if a change had been seen), wait for the
children, exit 1 if any failed.
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import inputs  # noqa: E402
from adw_modules.forge import as_layer  # noqa: E402
from trigger_cron import Scheduler, find_trigger, say  # noqa: E402

DEFAULT_LAYER = Path(__file__).resolve().parent.parent.parent

Snapshot = dict[str, tuple[int, int]]


def watched(layer, trigger_id: str) -> tuple[list[Path], list[tuple[Path, str]]]:
    """(files, [(dir, pattern)]) watched for this trigger."""
    files, dirs = [], []
    for a in inputs.adws_for_trigger(layer.ir, trigger_id):
        for input_id in a.get("input") or []:
            o = inputs.option_by_id(layer.ir, input_id) or {}
            if o.get("kind") == "tasklist":
                p = Path(layer.layer_dir) / o.get("path", "tasks.md")
                if p not in files:
                    files.append(p)
            elif o.get("kind") == "spec-file":
                d = (Path(layer.layer_dir) / o.get("dir", "specs/inbox"), o.get("pattern") or "*.md")
                if d not in dirs:
                    dirs.append(d)
    return files, dirs


def snapshot(files: list[Path], dirs: list[tuple[Path, str]]) -> Snapshot:
    snap: Snapshot = {}
    paths = list(files)
    for d, pattern in dirs:
        if d.is_dir():
            paths += [p for p in d.iterdir() if fnmatch.fnmatch(p.name, pattern)]
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        snap[str(p)] = (st.st_mtime_ns, st.st_size)
    return snap


class Debouncer:
    """Pure state machine: feed (now, snapshot); returns True when a debounced change is due."""

    def __init__(self, debounce: float, initial: Snapshot):
        self.debounce = debounce
        self.acted = initial
        self.last = initial
        self.changed_at: float | None = None

    def feed(self, now: float, snap: Snapshot) -> bool:
        if snap != self.last:
            self.last = snap
            self.changed_at = now
            return False
        if self.changed_at is not None and snap != self.acted and now - self.changed_at >= self.debounce:
            self.acted = snap
            self.changed_at = None
            return True
        if snap == self.acted:
            self.changed_at = None
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="File-watch trigger for the agentic layer")
    ap.add_argument("--layer", default=str(DEFAULT_LAYER))
    ap.add_argument("--trigger", help="file-watch trigger id (default: the first one)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    layer = as_layer(args.layer)
    trig = find_trigger(layer.ir, "file-watch", args.trigger)
    cron = next((t for t in layer.ir.get("triggers") or [] if t.get("kind") == "cron"), {})
    sched = Scheduler(layer, trig["id"], cron.get("maxConcurrent") or 3, dry_run=args.dry_run)
    files, dirs = watched(layer, trig["id"])
    debounce = float(trig.get("debounceSeconds") or 3)
    say(f"watch {trig['id']}: {', '.join(str(p) for p in files + [d for d, _ in dirs]) or '(nothing)'} "
        f"debounce {debounce:g}s{' [dry-run]' if args.dry_run else ''}")

    if args.once:
        if sched.poll_once() == 0:
            say("nothing to do")
        sched.wait_all()
        return 1 if sched.failures else 0

    deb = Debouncer(debounce, snapshot(files, dirs))
    try:
        while True:
            time.sleep(min(1.0, max(0.2, debounce / 3)))
            sched.reap()
            if deb.feed(time.monotonic(), snapshot(files, dirs)):
                say("change detected")
                sched.poll_once()
                # our own claims rewrite tasks.md; absorb them so they don't count as a change
                deb.acted = deb.last = snapshot(files, dirs)
    except KeyboardInterrupt:
        say(f"stopping; {len(sched.running)} run(s) continue in the background")
        return 130


if __name__ == "__main__":
    sys.exit(main())
