#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PITER · T — polling trigger.

Every `intervalSeconds`, poll the prompt inputs of every ADW that lists this
trigger (tasklist: eligible tasks; issue: open labelled issues without a bot
comment; spec-file: inbox files not yet in agents/trigger-seen.json), claim
each item, and launch at most `maxConcurrent` ADW subprocesses.

    trigger_cron.py [--layer DIR] [--trigger ID] [--once] [--dry-run]

--once: one poll, wait for the children, exit 1 if any child failed.
--dry-run: claim nothing, pass --dry-run to the ADWs.

Every poll also reads replies to the stand-in decider's suggestions over IMAP when
AL_DECIDE_IMAP_URL is set (local.env or the environment; see adw_modules/decide.py).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import inputs  # noqa: E402
from adw_modules.forge import as_layer  # noqa: E402

DEFAULT_LAYER = Path(__file__).resolve().parent.parent.parent


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_trigger(ir: dict, kind: str, trigger_id: str | None) -> dict:
    for t in ir.get("triggers") or []:
        if (trigger_id and t.get("id") == trigger_id) or (not trigger_id and t.get("kind") == kind):
            return t
    raise SystemExit(f"no {kind} trigger{' ' + trigger_id if trigger_id else ''} declared in the layer")


def spawn(argv: list[str], log_path: Path, env: dict) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab")
    try:
        # own session: Ctrl-C on the trigger must not kill running ADWs
        return subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, start_new_session=True)
    finally:
        fh.close()


class Scheduler:
    """Poll → claim → launch, bounded by max_concurrent. Shared by cron and file-watch."""

    def __init__(self, layer, trigger_id: str, max_concurrent: int = 3, dry_run: bool = False,
                 forge=None, launch=spawn, out=say):
        self.layer = as_layer(layer)
        self.trigger_id = trigger_id
        self.max_concurrent = max(1, int(max_concurrent))
        self.dry_run = dry_run
        self.forge = forge
        self.launch = launch
        self.out = out
        self.running: list[tuple[object, inputs.Candidate]] = []
        self.failures = 0
        self.inflight: set[str] = set()
        self.seq = 0

    def day_limit_reached(self) -> str | None:
        """The layer's day limit (#:day-usd) or its subscription usage threshold is reached: no run
        would start, so nothing is claimed."""
        from adw_modules import budget, models
        try:
            limits = budget.limits(self.layer, models.overrides(None, None, None))
        except (ValueError, AttributeError, TypeError):
            return None
        return budget.refuse_start(self.layer, limits)

    def poll_once(self) -> int:
        """Launch what fits; return the number of runs started."""
        self.reap()
        started = 0
        self.poll_answers()
        refused = self.day_limit_reached()
        if refused:
            if refused != getattr(self, "_refused", None):
                self.out(f"not starting runs: {refused}")
            self._refused = refused
            return 0
        self._refused = None
        for cand in inputs.poll(self.layer, self.trigger_id, forge=self.forge, log=self.out):
            if len(self.running) >= self.max_concurrent:
                self.out(f"at capacity ({self.max_concurrent}); remaining items wait for the next poll")
                break
            if cand.source in self.inflight:
                continue
            if not self.dry_run and not inputs.claim(self.layer, cand, forge=self.forge):
                self.out(f"skip {cand.source}: claimed elsewhere")
                continue
            env = dict(os.environ)
            if cand.claim_id:
                env[inputs.CLAIM_ENV] = cand.claim_id
            argv = inputs.adw_argv(self.layer.layer_dir, cand.adw, cand.source, dry_run=self.dry_run)
            log = (Path(self.layer.layer_dir) / "agents" / "trigger-logs"
                   / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{self.seq}-{cand.adw}.log")
            self.seq += 1
            proc = self.launch(argv, log, env)
            self.running.append((proc, cand))
            self.inflight.add(cand.source)
            started += 1
            self.out(f"start {cand.adw} <- {cand.source} ({cand.label}) log {log}")
        return started

    def poll_answers(self) -> None:
        """Replies to the stand-in decider's suggestions (does nothing without AL_DECIDE_IMAP_URL)."""
        from adw_modules import decide
        try:
            decide.poll_mail(self.layer, out=self.out)
        except Exception as exc:  # noqa: BLE001 — poll_mail never raises; a mailbox never stops the poller
            self.out(f"decide mail: {type(exc).__name__}")

    def reap(self) -> None:
        still = []
        for proc, cand in self.running:
            rc = proc.poll()
            if rc is None:
                still.append((proc, cand))
                continue
            self.inflight.discard(cand.source)
            if rc != 0:
                self.failures += 1
            if not self.dry_run:
                inputs.release(self.layer, cand, rc)
            self.out(f"{'done' if rc == 0 else 'FAIL'} {cand.adw} <- {cand.source} exit {rc}")
        self.running = still

    def wait_all(self, tick: float = 0.5) -> None:
        while self.running:
            time.sleep(tick)
            self.reap()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Polling trigger for the agentic layer")
    ap.add_argument("--layer", default=str(DEFAULT_LAYER))
    ap.add_argument("--trigger", help="cron trigger id (default: the first cron trigger)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    layer = as_layer(args.layer)
    trig = find_trigger(layer.ir, "cron", args.trigger)
    interval = int(trig.get("intervalSeconds") or 20)
    sched = Scheduler(layer, trig["id"], trig.get("maxConcurrent") or 3, dry_run=args.dry_run)
    names = [a["name"] for a in inputs.adws_for_trigger(layer.ir, trig["id"])]
    say(f"cron {trig['id']}: layer {layer.layer_dir}, adws {', '.join(names) or '(none)'}, "
        f"every {interval}s, max {sched.max_concurrent}{' [dry-run]' if args.dry_run else ''}")

    if args.once:
        n = sched.poll_once()
        if n == 0:
            say("nothing to do")
        sched.wait_all()
        return 1 if sched.failures else 0
    try:
        while True:
            sched.poll_once()
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                time.sleep(min(1.0, interval))
                sched.reap()
    except KeyboardInterrupt:
        say(f"stopping; {len(sched.running)} run(s) continue in the background")
        return 130


if __name__ == "__main__":
    sys.exit(main())
