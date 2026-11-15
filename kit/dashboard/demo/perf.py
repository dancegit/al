#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Measure the dashboard's own CPU use (from /proc), idle and while runs are busy.

    demo/perf.py [--seconds 30] [--busy] [--layer DIR …] [--scan DIR …]

Idle: the dashboard watches the given layers with two live clients attached (the global event
stream and one transcript tail). --busy: it also sets up the live demo's layer (see live_demo.py)
and starts its three runs, then measures while they run.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TICK = os.sysconf("SC_CLK_TCK")


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / TICK


def drain(port: int, path: str, stop: threading.Event, counter: list[int]) -> None:
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            data = sock.recv(65536)
            if not data:
                break
            counter[0] += data.count(b"\n\n")
        except socket.timeout:
            continue
    sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--busy", action="store_true")
    parser.add_argument("--layer", action="append", default=[])
    parser.add_argument("--scan", action="append", default=[])
    args = parser.parse_args()
    layers = list(args.layer)
    runs: list[subprocess.Popen] = []
    if args.busy:
        sys.path.insert(0, str(HERE))
        import live_demo
        layer = live_demo.setup(Path(tempfile.mkdtemp(prefix="al-dash-perf.")))
        layers.append(str(layer))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    argv = [sys.executable, str(HERE.parent / "dashboard.py"), "--port", str(port)]
    argv += [x for d in layers for x in ("--layer", d)] + [x for d in args.scan for x in ("--scan", d)]
    server = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop, counter = threading.Event(), [0]
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        threads = [threading.Thread(target=drain, args=(port, "/api/events", stop, counter), daemon=True)]
        threads[0].start()
        time.sleep(2)   # startup (first scan and parse) is not part of the measurement
        if args.busy:
            for prompt in live_demo.PROMPTS:
                runs.append(subprocess.Popen(["uv", "run", "--quiet", "adws/adw_change.py", prompt], cwd=layers[-1],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
                time.sleep(1)
        before, started = cpu_seconds(server.pid), time.monotonic()
        time.sleep(args.seconds)
        used, elapsed = cpu_seconds(server.pid) - before, time.monotonic() - started
        rss = int(next(line for line in Path(f"/proc/{server.pid}/status").read_text().splitlines() if line.startswith("VmRSS")).split()[1])
        fds = len(os.listdir(f"/proc/{server.pid}/fd"))
        print(f"{'busy' if args.busy else 'idle'}: {used:.2f} s CPU in {elapsed:.0f} s = {100 * used / elapsed:.2f}% of one core; "
              f"RSS {rss // 1024} MiB; open fds {fds}; SSE events received {counter[0]}")
    finally:
        stop.set()
        for proc in runs:
            proc.wait(timeout=180)
        server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
