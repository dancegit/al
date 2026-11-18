#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""al work dashboard: a live view of what the agents of one or more layers are doing.

  dashboard.py --layer path/to/app.al [--layer …] [--scan DIR …] [--port 8765] [--bind 127.0.0.1]
               [--token T] [--open] [--read-only] [--no-start] [--workspace DIR …] [--workspace-depth N]
               [--no-workspace] [--no-actions] [--jobs-dir DIR] [--al PATH] [--no-stand-in | --stand-in-only]

It watches each layer's `agents/` (run state, run.log, step transcripts, check logs, slot locks,
port leases) and `artefacts/` by polling mtimes and sizes, and serves one page that updates over
Server-Sent Events. Standard library only. It changes three things in a layer, all guarded: a layer's
local model setting (agents/settings/models.json), from the Models panel; it starts work (a workflow
or one command, or a failed run again) through the layer's own `adws/dispatch.py --start`; and it
records that a finished run is settled (agents/<id>/settlement.json). Whether a handed-over run's work
landed on its target branch is worked out read-only from git (see Landing), never by fetching.

It also lists the git repositories of a workspace (the folder holding each layer's repository, or
--workspace DIR) with their al state (layer / setup / none), read-only, and runs allow-listed `al`
commands on one of them from the page (scout, create, adopt, gen, check, verify, explain) as recorded
jobs in --jobs-dir.

It is also the control room of the stand-in (kit/runtime/adw_modules/standin.py), always through the
layer's own `adws/standin.py`, never by importing a layer's runtime: the Stand-in panel switches the
mode, the scope and the pause (POST /api/decider, the file `al decider` writes), a suggestion is
answered on the page (POST /api/decision, the entry of `al answer`), "Ask the stand-in" files a
suggestion for an old hand-over or failure (POST /api/standin), and a loop hands each new item of
"Needs you" to the stand-in, one at a time (Model.tick_standin). --no-stand-in keeps the loop out of
this process; --stand-in-only runs the loop without the page (the generated systemd unit).

--read-only turns every write off; --no-start and --no-actions each turn off their own.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import difflib
import fcntl
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import mimetypes
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

VERSION = "1"
HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
RUN_ID = re.compile(r"^[0-9a-fA-F]{8}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LAYER_FILE = "agentic-layer.json"
PRUNE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", "trees",
              "agents", "artefacts", ".tox", ".cache", ".claude", ".mypy_cache", ".pytest_cache", "testbeds",
              ".direnv", ".nox", "site-packages"}
EXPLAIN_FILES = ("EXPLAIN.md", "explain.md", "docs/EXPLAIN.md", "docs/explain.md", "docs/explain.txt")
DEFINITION_FILES = (LAYER_FILE, "agentic.rkt")
SERVED_TREES = ("agents", "artefacts", "specs", "promotions")
DOC_SUFFIXES = (".md", ".txt", ".json", ".log", ".diff")   # what counts as a run's document (viewable as text)
DOC_VIEW_CAP = 1_000_000    # bytes shown inline; larger documents are download only
DOC_ZIP_CAP = 50_000_000    # bytes put into one "download all" zip
DOC_TYPES = {".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
             ".log": "text/plain; charset=utf-8", ".json": "application/json"}
PREVIEW_CHARS = 1600
TRANSCRIPT_SETTLE = 20.0   # a JSON line without its newline is held this long before it counts as truncated
TEXT_CAP = 400_000
POOLS = ("runs", "agents", "checks")
POOL_DEFAULTS = {"runs": ("maxRuns", 4), "agents": ("maxAgents", 2), "checks": ("maxChecks", 2)}
EVENTS_FILE = "events.jsonl"
USAGE_FILE = "subscription-usage.json"   # agents/subscription-usage.json: what the agent CLI last reported
# mirror of kit/runtime/adw_modules/models.py; tests/tools/test_al_models.py holds them equal
SETTINGS_FILE = "agents/settings/models.json"   # the layer's local model setting, never committed
KNOWN_MODELS = ("claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5", "claude-haiku-5-5", "gpt-5.4",
                "gpt-5.4-mini")
BUILTIN_TIERS = {"claude-code": {"base": "claude-opus-5-5", "heavy": "claude-opus-5-5"},
                 "codex": {"base": "gpt-5.4-mini", "heavy": "gpt-5.4"}}
TIER_RX = re.compile(r"^[a-z][a-z0-9-]*$")
MODEL_ID_RX = re.compile(r"^[^\s\x00-\x1f\x7f]{1,200}$")   # non-empty, no whitespace, no control characters
WRITE_HEADER = "X-AL-Dashboard"   # a plain HTML form cannot send it: every POST requires it
MAX_WRITE_BODY = 4096
MAX_START_BODY = 64 * 1024        # POST /api/start: the prompt travels in the body
START_TIMEOUT = 60.0              # seconds the layer's launcher may take to reserve and detach a run
MAX_PROMOTION_BODY = 8 * 1024     # POST /api/promotion: a decision with its comment, or a start
PROMOTION_KEYS = {"layer", "run", "action", "from", "to", "decision", "changes", "by", "comment", "then", "dry_run"}
PROMOTION_ACTIONS = ("decide", "start", "continue")
PROMOTION_DECISIONS = ("accept", "refuse", "approve")
PROMOTION_THEN = ("revert", "patch")
PROMOTE_SCRIPT = "adws/promote.py"        # generated by `al gen` for a layer with stages
OPEN_PROMOTIONS = "agents/promotions"     # one <from>-<to>.json per open promotion (mirror of delivery.py)
INTEGRATE_SCRIPT = "adws/integrate.py"     # generated by `al gen` for every layer (the kit workflow `integrate`)
INTEGRATION_FILE = "integration.json"      # agents/<original>/integration.json, written by adw_modules/integrate.py
INTEGRATE_SETTINGS = "templates/integrate.json"
INTEGRATE_REASONS = ("off", "unknown-run", "not-handed-over", "not-a-conflict", "review-rejected", "protected-path",
                     "branch-changed", "branch-missing", "settled", "landed", "already-integrating", "layer-change",
                     "no-ship-step", "name-taken", "undecidable", "markers-remain", "head-moved", "not-waiting")
INTEGRATE_KEYS = {"layer", "run", "action", "confirm", "model_set", "who"}
INTEGRATE_ACTIONS = ("start", "approve")
INTEGRATE_WHO_CHARS = 80
EVIDENCE_KINDS = ("landed", "replaced")
EVIDENCE_CANDIDATES = 5
CHANGE_ID_RX = re.compile(r"^[0-9a-f]{8}$")
STAGE_NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
START_CLASSES = ("chore", "bug", "feature", "patch")   # mirror of kit/runtime/adw_modules/agents.py
START_ISSUE = ("none", "start", "trigger")
START_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")     # mirror of launch.KEY_RE
COMMAND_PREFIX = "command:"       # a single-command run's ADW name (launch.single_command_adw)
LAUNCHER = "adws/dispatch.py"
# The stand-in (kit/runtime/adw_modules/standin.py), always asked through the layer's own script:
STANDIN_SCRIPT = "adws/standin.py"
STANDIN_EVERY = 5.0               # seconds between looks at the Needs you list for the stand-in
STANDIN_TIMEOUT = 1800.0          # seconds one decision of the stand-in may take (an agent call)
STANDIN_STATUS_TIMEOUT = 30.0     # seconds the layer's script may take to answer status, set, answer or note
STANDIN_KINDS = ("resume", "restart", "answer", "integrate", "settle", "land", "promote", "retry-job")  # mirror of standin.KINDS
DECIDER_MODES = ("off", "suggest", "auto")   # mirror of decide.MODES
DECIDER_FILE = "agents/settings/decider.json"   # the stand-in's local setting (decide.SETTINGS_FILE)
STANDIN_LOG = "agents/stand-in/log.jsonl"       # the layer's log of decisions (standin.LOG_FILE)
DECISIONS_FILE = "decisions.jsonl"              # agents/<id>/decisions.jsonl: the decisions of one run
STANDIN_KEYS = {"decider": {"layer", "mode", "reset", "may", "paused"},
                "decision": {"layer", "run", "answer", "text"},
                "standin": {"layer", "run", "runs", "apply"}}
DECISION_ANSWERS = ("go", "alter", "reject", "explain")
ASK_STATUSES = ("failed", "died", "handed-over", "waiting")   # what "Ask the stand-in" is offered for
MAX_ASK_RUNS = 200
MAX_DECISIONS = 200               # entries of a run's decisions.jsonl the run page carries
LAUNCH_MODULE = "adws/adw_modules/launch.py"
WINDOW_LABELS = {"five_hour": "5-hour", "seven_day": "weekly"}
WINDOW_ORDER = ("five_hour", "seven_day")
API_KEY_SOURCES = ("ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key")
# Liveness (state.json pid / host / heartbeat, refreshed every ~5 s by a running run):
HEARTBEAT_STALE = 20.0      # older than this and its process is gone: the run died
HEARTBEAT_UNSEEN = 300.0    # its process cannot be checked (other host, container): died after this long
HEARTBEAT_REUSED = 3600.0   # a live pid with a heartbeat this old is another process that got the same pid
# The workspace (folders of git repositories side by side) and the actions run on them:
WORKSPACE_DEPTH = 1            # how deep below a workspace folder repositories are looked for
WORKSPACE_RESCAN = 60          # seconds between rescans (a repository whose signature is unchanged is not reread)
WORKSPACE_REFRESH_MIN = 5      # at most one rescan asked for by the page per this many seconds
WORKSPACE_FILE_CAP = 20_000    # tracked paths read per repository
WORKSPACE_REPO_CAP = 500       # repositories listed
WORKSPACE_GIT_TIMEOUT = 5      # seconds per git call of the scan
JOB_ID = re.compile(r"^[0-9a-f]{8}$")
JOB_HEARTBEAT = 5              # seconds between a job runner's heartbeats
ACTION_KEYS = {"repo", "action", "confirm", "choices", "placement"}
PLACEMENTS = ("beside", "inside")   # where create and adopt put the layer: <name>.al beside the repository, or in it
IN_REPO_DIR = "agentic"             # the folder of an inside layer: IN_REPO_DIR of tools/al_create.py (a test keeps them equal)
RETRY_KEYS = {"job", "from"}
PREFLIGHT_TIMEOUT = 10      # seconds `al doctor --for …` may take before a job's first pending step
TOOL_ROWS_TTL = 300         # seconds the Repos view's tools table (with versions) is kept
PREVIEW_TTL = 600              # seconds a preview's confirmation stays valid
SETTLEMENT_FILE = "settlement.json"   # agents/<id>/settlement.json: a person settled the run (al settle, the page)
SETTLE_AS = ("landed", "superseded", "abandoned")
SETTLE_NOTE_CHARS = 500
SETTLE_WHO_CHARS = 80
FAILURE_GROUP_SECONDS = 1800   # failures of one step and reason that end within this of each other are one group
LANDING_CHECKS_PER_TICK = 8    # uncached landed checks (git) per watcher tick; the others wait as "checking"
PR_STATE_SECONDS = 600         # a forge answer about a pull request is kept this long
LANDING_GIT_TIMEOUT = 10       # seconds per git call of a landed check
PATCH_ID_COMMITS = 500         # commits of the target compared by patch-id
LANDING_PATHS = 2000           # a branch that changed more paths than this is not compared path by path
COMMIT_RX = re.compile(r"^[0-9a-f]{7,40}$")
BUCKETS = ("needs-you", "running", "landed", "settled", "done")
ATTENTION_EVERY = 20           # ticks between recomputations of a layer's attention without a change
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")   # run.log line prefix since run events


def liveness(state: dict[str, Any], now: float, can_see_pids: bool = True) -> dict[str, Any] | None:
    """For a run whose state says `running` and that records a heartbeat: did its process die?
    → {"died": bool, "last_seen": epoch}; None when there is nothing to judge (finished, or an old
    run without a heartbeat)."""
    if not isinstance(state, dict) or state.get("status") != "running":
        return None
    seen = parse_time(state.get("heartbeat"))
    if seen is None:
        return None
    age = now - seen
    if age <= HEARTBEAT_STALE:
        return {"died": False, "last_seen": seen}
    pid = state.get("pid") if isinstance(state.get("pid"), int) else 0
    if can_see_pids and pid > 0 and state.get("host") == socket.gethostname():
        alive = pid_alive(pid)
        if alive is False:
            return {"died": True, "last_seen": seen}
        if alive is True:
            return {"died": age > HEARTBEAT_REUSED, "last_seen": seen}
    return {"died": age > HEARTBEAT_UNSEEN, "last_seen": seen}


_EVENT_CACHE: dict[str, tuple[tuple[int, int, int] | None, list[dict[str, Any]]]] = {}
_EVENT_LOCK = threading.Lock()


def load_events(path: Path) -> list[dict[str, Any]]:
    """Every complete, parseable event of a run's events.jsonl (cached by size/mtime; a torn last
    line is left for the next poll). [] when there is none (an older run)."""
    sig = stat_sig(path)
    if sig is None:
        return []
    key = str(path)
    with _EVENT_LOCK:
        cached = _EVENT_CACHE.get(key)
        if cached and cached[0] == sig:
            return cached[1]
    try:
        with path.open("rb") as handle:
            data = handle.read(32 * 1024 * 1024)
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in data.split(b"\n")[:-1]:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            value["_t"] = parse_time(value.get("ts"))
            if value["_t"] is not None:
                events.append(value)
    with _EVENT_LOCK:
        if len(_EVENT_CACHE) > 512:
            _EVENT_CACHE.clear()
        _EVENT_CACHE[key] = (sig, events)
    return events


# ============================================================================ small helpers

def parse_time(value: Any) -> float | None:
    """ISO-8601 (as written by the runtime) → epoch seconds; None for empty or invalid."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def json_bytes(value: Any) -> bytes:
    """JSON safe to embed anywhere: `<`, `>` and `&` are escaped so no consumer can mistake it for markup."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode("utf-8")


def stat_sig(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read_text(path: Path, cap: int = TEXT_CAP) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(cap + 1)
    except OSError:
        return ""
    text = data[:cap].decode("utf-8", errors="replace")
    return text + ("\n… (truncated)" if len(data) > cap else "")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def billing_of(source: Any) -> str:
    """The billing mode an init event's `apiKeySource` names: subscription / api-key / "" (not said).
    The same mapping as the runtime's usage.billing_of; the dashboard is a separate standard-library
    script and imports nothing from the runtime."""
    if source == "none":
        return "subscription"
    if isinstance(source, str) and source in API_KEY_SOURCES:
        return "api-key"
    return ""


def window_label(window: str) -> str:
    return WINDOW_LABELS.get(window) or window.replace("_", " ")


def _share(value: Any) -> float | None:
    """A share used, 0..1, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return float(value)


def collapse_billing(values: Iterable[str]) -> str:
    known = sorted({v for v in values if v})
    return "" if not known else (known[0] if len(known) == 1 else "mixed")


def is_loopback(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class Shortener:
    """Turns absolute paths in agent output into short, readable (and less personal) ones."""

    def __init__(self, pairs: Iterable[tuple[str, str]] = ()) -> None:
        self.pairs: list[tuple[str, str]] = []
        for prefix, replacement in pairs:
            self.add(prefix, replacement)
        home = os.path.expanduser("~")
        if home and home != "/":
            self.add(home, "~")

    def add(self, prefix: str, replacement: str) -> None:
        prefix = str(prefix or "").rstrip("/")
        if len(prefix) < 2 or any(p == prefix for p, _ in self.pairs):
            return
        self.pairs.append((prefix, replacement))
        self.pairs.sort(key=lambda pair: -len(pair[0]))

    def __call__(self, text: Any) -> str:
        text = "" if text is None else str(text)
        for prefix, replacement in self.pairs:
            if prefix in text:
                if replacement.endswith(":"):      # "layer:" + "specs/x.md", not "layer:/specs/x.md"
                    text = text.replace(prefix + "/", replacement).replace(prefix, replacement)
                elif replacement:
                    text = text.replace(prefix, replacement)
                else:
                    text = text.replace(prefix + "/", "").replace(prefix, ".")
        return text


# ============================================================================ ANSI → segments

_ANSI = re.compile(r"\x1b\[([0-9;:?]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BASIC = ["#4b5563", "#f87171", "#4ade80", "#facc15", "#60a5fa", "#e879f9", "#22d3ee", "#e5e7eb"]
_BRIGHT = ["#9ca3af", "#fca5a5", "#86efac", "#fde047", "#93c5fd", "#f0abfc", "#67e8f9", "#ffffff"]


def _xterm(index: int) -> str:
    if index < 8:
        return _BASIC[index]
    if index < 16:
        return _BRIGHT[index - 8]
    if index < 232:
        index -= 16
        levels = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (levels[index // 36], levels[(index // 6) % 6], levels[index % 6])
    grey = 8 + (index - 232) * 10
    return "#%02x%02x%02x" % (grey, grey, grey)


@dataclass
class AnsiState:
    fg: str = ""
    bg: str = ""
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False

    def style(self) -> list[Any]:
        flags = "".join(flag for flag, on in (("b", self.bold), ("d", self.dim), ("i", self.italic),
                                               ("u", self.underline)) if on)
        return [self.fg, self.bg, flags]

    def apply(self, params: str) -> None:
        codes = [int(part) if part.isdigit() else 0 for part in re.split(r"[;:]", params)] if params else [0]
        index = 0
        while index < len(codes):
            code = codes[index]
            if code == 0:
                self.__init__()  # type: ignore[misc]
            elif code == 1:
                self.bold = True
            elif code == 2:
                self.dim = True
            elif code == 3:
                self.italic = True
            elif code == 4:
                self.underline = True
            elif code == 22:
                self.bold = self.dim = False
            elif code == 23:
                self.italic = False
            elif code == 24:
                self.underline = False
            elif 30 <= code <= 37:
                self.fg = _BASIC[code - 30]
            elif 90 <= code <= 97:
                self.fg = _BRIGHT[code - 90]
            elif code == 39:
                self.fg = ""
            elif 40 <= code <= 47:
                self.bg = _BASIC[code - 40]
            elif 100 <= code <= 107:
                self.bg = _BRIGHT[code - 100]
            elif code == 49:
                self.bg = ""
            elif code in (38, 48) and index + 1 < len(codes):
                colour = ""
                if codes[index + 1] == 5 and index + 2 < len(codes):
                    colour = _xterm(max(0, min(255, codes[index + 2])))
                    index += 2
                elif codes[index + 1] == 2 and index + 4 < len(codes):
                    colour = "#%02x%02x%02x" % tuple(max(0, min(255, c)) for c in codes[index + 2:index + 5])
                    index += 4
                if code == 38:
                    self.fg = colour
                else:
                    self.bg = colour
            index += 1


def ansi_segments(text: str, state: AnsiState | None = None) -> tuple[list[list[Any]], AnsiState]:
    """Split text with ANSI escapes into [text, fg, bg, flags] segments. Only SGR colours survive;
    every other escape and control character is dropped; a carriage return keeps what follows it
    (progress bars), line by line. The state carries over between calls (incremental tailing)."""
    state = state or AnsiState()
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        if "\r" in line:
            parts = [part for part in line.split("\r")]
            line = next((part for part in reversed(parts) if part.strip()), parts[-1])
        cleaned.append(line)
    text = "\n".join(cleaned)
    segments: list[list[Any]] = []
    position = 0

    def emit(chunk: str) -> None:
        chunk = _CONTROL.sub("", chunk)
        if not chunk:
            return
        style = state.style()
        if segments and segments[-1][1:] == style:
            segments[-1][0] += chunk
        else:
            segments.append([chunk, *style])

    for match in _ANSI.finditer(text):
        emit(text[position:match.start()])
        if match.group(2) == "m":
            state.apply(match.group(1) or "")
        position = match.end()
    emit(text[position:])
    return segments, state


def strip_ansi(text: str) -> str:
    return "".join(segment[0] for segment in ansi_segments(text)[0])


# ============================================================================ incremental file tail

class Tail:
    """Reads a growing file incrementally: complete lines only, a partial last line is held back
    until it is finished (or the file has been quiet for `settle` seconds; `open` is then true until
    the rest of that line arrives, which comes as the next item). A file that shrinks or
    is replaced (new inode) restarts from the beginning and reports `reset`. Opens and closes the
    file on every read, so no handle is ever held between polls."""

    def __init__(self, path: Path, offset: int = 0, line_no: int = 0, settle: float = 2.0) -> None:
        self.path = path
        self.offset = offset
        self.line_no = line_no
        self.inode: int | None = None
        self.pending = b""
        self.settle = settle
        self.open = False   # the last item returned was a partial line shown early: the next one continues it

    def read(self, max_bytes: int = 4 * 1024 * 1024) -> tuple[bool, list[tuple[int, int, str]]]:
        """→ (reset, [(line_no, start_offset, text)]). line_no starts at 1."""
        try:
            info = self.path.stat()
        except OSError:
            return False, []
        reset = False
        replaced = self.inode is not None and info.st_ino != self.inode
        if replaced or info.st_size < self.offset + len(self.pending):
            reset = replaced or self.offset > 0 or bool(self.pending)
            self.offset, self.line_no, self.pending, self.open = 0, 0, b"", False
        self.inode = info.st_ino
        start = self.offset + len(self.pending)
        data = b""
        if info.st_size > start:
            try:
                with self.path.open("rb") as handle:
                    handle.seek(start)
                    data = handle.read(max_bytes)
            except OSError:
                return reset, []
        buffer = self.pending + data
        lines: list[tuple[int, int, str]] = []
        cursor = 0
        while True:
            newline = buffer.find(b"\n", cursor)
            if newline < 0:
                break
            raw = buffer[cursor:newline]
            self.line_no += 1
            lines.append((self.line_no, self.offset + cursor, raw.decode("utf-8", errors="replace").rstrip("\r")))
            cursor = newline + 1
        self.offset += cursor
        self.pending = buffer[cursor:]
        if lines:
            self.open = False
        if self.pending and (time.time() - info.st_mtime) >= self.settle and start + len(data) >= info.st_size:
            text = self.pending.decode("utf-8", errors="replace")
            self.line_no += 1
            lines.append((self.line_no, self.offset, text))
            self.offset += len(self.pending)
            self.pending = b""
            self.open = True
        return reset, lines


# ============================================================================ transcript normalisation

_SKIP_FIRST = {"cd", "export", "set", "source", ".", "true", "echo", "printf", "mkdir", "unset"}
_WRAPPERS = {"sudo", "env", "time", "nice", "exec", "command", "nohup", "stdbuf", "xargs"}
_RUNNERS = {"uv", "uvx", "poetry", "pipenv", "npx", "bunx", "pnpx", "hatch", "pdm", "rye", "nix", "devbox"}
_VALUE_FLAGS = {"--group", "--with", "--python", "-p", "--from", "--project", "--directory", "--env-file", "--extra",
                "--package", "-e", "--only-group", "--index", "--with-requirements", "-w", "--config", "-c"}
_SUBCOMMAND_TOOLS = {"git", "npm", "pnpm", "yarn", "bun", "cargo", "go", "make", "podman", "docker", "tox",
                     "gh", "deno", "dotnet", "mvn", "gradle", "kubectl", "pip", "pip3", "just"}


def command_verb(command: str) -> str:
    """'uv run --locked … pre-commit run --all-files' → 'pre-commit run'; 'python -m pytest -q' → 'pytest'."""
    for segment in re.split(r"&&|\|\||;|\||\n", command or ""):
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            tokens = segment.split()
        tokens = [t for t in tokens if t]
        while tokens and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]) or tokens[0] in _WRAPPERS):
            tokens = tokens[1:]
            if tokens and tokens[0] in {"-n", "-u", "-i"}:
                tokens = tokens[1:]
        if not tokens:
            continue
        head = os.path.basename(tokens[0])
        if head in _SKIP_FIRST:
            continue
        if head == "timeout" and len(tokens) > 2:
            tokens, head = tokens[2:], os.path.basename(tokens[2])
        if head in _RUNNERS:
            rest = tokens[1:]
            if rest and rest[0] in {"run", "exec", "tool", "x", "develop", "shell"}:
                rest = rest[1:]
            positional, skip = [], False
            for token in rest:
                if skip:
                    skip = False
                elif token.startswith("-"):
                    skip = "=" not in token and token in _VALUE_FLAGS
                else:
                    positional.append(token)
            rest = positional
            if rest:
                tokens, head = rest, os.path.basename(rest[0])
        if re.match(r"^python[0-9.]*$", head) and len(tokens) > 2 and tokens[1] == "-m":
            return tokens[2]
        if re.match(r"^python[0-9.]*$", head) and len(tokens) > 1 and not tokens[1].startswith("-"):
            return os.path.basename(tokens[1])
        if head in _SUBCOMMAND_TOOLS and len(tokens) > 1 and not tokens[1].startswith("-"):
            return f"{head} {tokens[1]}"
        if len(tokens) > 1 and not tokens[1].startswith("-") and head in {"pre-commit", "pytest", "ruff", "mypy"}:
            return f"{head} {tokens[1]}" if head == "pre-commit" else head
        return head
    return "a command"


def _diff_lines(old: str, new: str, limit: int = 80) -> list[str]:
    lines = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        lines.append(line)
        if len(lines) >= limit:
            lines.append("… (diff truncated)")
            break
    return lines


def _patch_lines(patch: Any, limit: int = 80) -> list[str]:
    lines: list[str] = []
    if not isinstance(patch, list):
        return lines
    for hunk in patch:
        if not isinstance(hunk, dict):
            continue
        lines.append(f"@@ -{hunk.get('oldStart', '?')},{hunk.get('oldLines', '?')} "
                     f"+{hunk.get('newStart', '?')},{hunk.get('newLines', '?')} @@")
        for line in hunk.get("lines") or []:
            lines.append(str(line))
            if len(lines) >= limit:
                lines.append("… (diff truncated)")
                return lines
    return lines


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(block)[:400])
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)[:4000]


def summarise_tool(name: str, data: Any, short: Shortener) -> dict[str, Any]:
    """One tool call → {title, action, detail…} for people."""
    data = data if isinstance(data, dict) else {}
    path = short(data.get("file_path") or data.get("path") or data.get("notebook_path") or "")
    event: dict[str, Any] = {"tool": name}
    if name == "Read":
        extra = ""
        if data.get("offset") or data.get("limit"):
            extra = f" (lines {data.get('offset', 1)}–{int(data.get('offset') or 1) + int(data.get('limit') or 0)})"
        event.update(title=f"read {path}{extra}", action=f"reading {path}", path=path)
    elif name in {"Edit", "MultiEdit"}:
        edits = data.get("edits") if name == "MultiEdit" else [data]
        diff: list[str] = []
        for edit in edits or []:
            if isinstance(edit, dict):
                diff += _diff_lines(short(edit.get("old_string", "")), short(edit.get("new_string", "")))
        event.update(title=f"edit {path}" + (f" ({len(edits)} edits)" if name == "MultiEdit" else ""),
                     action=f"editing {path}", path=path, diff=diff[:120])
    elif name == "Write":
        content = short(data.get("content", ""))
        lines = content.splitlines()
        event.update(title=f"write {path} ({len(lines)} lines)", action=f"writing {path}", path=path,
                     diff=["+" + line for line in lines[:40]] + (["… (more lines)"] if len(lines) > 40 else []))
    elif name in {"Bash", "BashOutput", "Shell"}:
        command = short(data.get("command", ""))
        verb = command_verb(data.get("command", ""))
        event.update(title=f"$ {clip(command.splitlines()[0] if command else '', 240)}", action=f"running {verb}",
                     cmd=command, note=short(data.get("description", "")))
    elif name == "Grep":
        pattern = data.get("pattern", "")
        where = short(data.get("path") or data.get("glob") or "")
        event.update(title=f"search /{clip(str(pattern), 120)}/" + (f" in {where}" if where else ""),
                     action=f"searching for {clip(str(pattern), 40)}", pattern=str(pattern))
    elif name == "Glob":
        pattern = data.get("pattern", "")
        event.update(title=f"find files {pattern}" + (f" in {short(data.get('path'))}" if data.get("path") else ""),
                     action=f"listing files {clip(str(pattern), 40)}", pattern=str(pattern))
    elif name in {"Task", "Agent"}:
        description = short(data.get("description") or data.get("subagent_type") or "subagent")
        event.update(title=f"delegate: {clip(description, 160)}", action=f"delegating: {clip(description, 50)}",
                     note=clip(short(data.get("prompt", "")), 600))
    elif name == "WebFetch":
        event.update(title=f"fetch {data.get('url', '')}", action="fetching a web page")
    elif name == "WebSearch":
        event.update(title=f"web search: {data.get('query', '')}", action="searching the web")
    elif name in {"TodoWrite", "TaskCreate", "TaskUpdate"}:
        todos = data.get("todos") or []
        event.update(title=f"update todo list ({len(todos)} items)" if todos else name,
                     action="updating its todo list",
                     note="\n".join(f"[{t.get('status', '')[:1] or ' '}] {t.get('content', '')}"
                                    for t in todos if isinstance(t, dict))[:1500])
    elif name == "NotebookEdit":
        event.update(title=f"edit notebook {path}", action=f"editing {path}", path=path)
    else:
        compact = short(json.dumps(data, ensure_ascii=False))
        event.update(title=f"{name} {clip(compact, 200)}", action=f"using {name}")
    return event


@dataclass
class TranscriptAgg:
    """Running totals over a transcript; cheap to keep per file in the model."""
    kind: str = "empty"          # claude | plain | mock | empty
    model: str = ""
    cost: float | None = None
    turns: int | None = None
    duration_ms: int | None = None
    tokens: dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
    final: bool = False
    is_error: bool = False
    events: int = 0
    tools: int = 0
    errors: int = 0
    action: str = ""
    first_ts: float | None = None
    last_ts: float | None = None
    thinking_tokens: int = 0
    billing: str = ""            # subscription | api-key | "" (what the init event's apiKeySource says)
    _message_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "model": self.model, "cost": self.cost, "turns": self.turns,
                "duration_ms": self.duration_ms, "tokens": dict(self.tokens), "final": self.final,
                "is_error": self.is_error, "events": self.events, "tools": self.tools, "errors": self.errors,
                "action": self.action, "first_ts": self.first_ts, "last_ts": self.last_ts,
                "thinking_tokens": self.thinking_tokens, "billing": self.billing}

    def usage_from_messages(self) -> None:
        if self.final:
            return
        totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        for usage in self._message_usage.values():
            for key in totals:
                totals[key] += usage.get(key, 0)
        self.tokens = totals


def _usage(usage: Any) -> dict[str, int]:
    usage = usage if isinstance(usage, dict) else {}
    def num(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0
    return {"in": num("input_tokens"), "out": num("output_tokens"),
            "cache_read": num("cache_read_input_tokens"), "cache_write": num("cache_creation_input_tokens")}


class Transcript:
    """Normalises the raw.jsonl of one agent step into human rows, incrementally.

    Claude Code stream-json events: system/init, assistant (text | thinking | tool_use blocks),
    user (tool_result blocks, sometimes a plain string), result, system/task_started,
    system/task_notification, system/thinking_tokens, system/ui_invalidate, rate_limit_event,
    anything else is kept as a generic row. Mock agents write one JSON object without `type`;
    codex and custom CLIs write plain text, shown as text."""

    def __init__(self, short: Shortener | None = None) -> None:
        self.short = short or Shortener()
        self.agg = TranscriptAgg()
        self.tool_names: dict[str, str] = {}
        self.plain_lines = 0

    def reset(self) -> None:
        self.__init__(self.short)

    def feed(self, lines: list[tuple[int, int, str]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        plain: list[str] = []
        plain_start = 0

        def flush_plain() -> None:
            nonlocal plain
            if plain:
                rows.append({"id": f"{plain_start}", "n": plain_start, "k": "plain",
                             "text": self.short("\n".join(plain))})
                plain = []

        for line_no, _offset, text in lines:
            stripped = text.strip()
            if not stripped:
                continue
            if stripped.startswith("{"):
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    flush_plain()
                    self.agg.errors += 1
                    rows.append({"id": f"{line_no}", "n": line_no, "k": "corrupt", "err": True,
                                 "text": clip(self.short(stripped), 600),
                                 "title": "unreadable line (partial or corrupt JSON)"})
                    continue
                if isinstance(obj, dict):
                    flush_plain()
                    rows.extend(self._event(obj, line_no))
                    continue
            if self.agg.kind in {"empty", "plain"}:
                self.agg.kind = "plain"
            if not plain:
                plain_start = line_no
            plain.append(text)
            self.plain_lines += 1
            if len(plain) >= 200:
                flush_plain()
        flush_plain()
        self.agg.events += len(rows)
        if self.agg.kind == "plain" and rows:
            self.agg.action = "writing output"
        self.agg.usage_from_messages()
        return rows

    # ------------------------------------------------------------------ one JSON event
    def _event(self, obj: dict[str, Any], n: int) -> list[dict[str, Any]]:
        short, agg = self.short, self.agg
        kind = obj.get("type")
        ts = parse_time(obj.get("timestamp"))
        if ts:
            agg.first_ts = agg.first_ts or ts
            agg.last_ts = ts
        base = {"n": n, "ts": ts}
        if kind is None and ("synthesised" in obj or "script" in obj or "output" in obj):
            agg.kind = "mock"
            agg.action = "mock agent"
            files = sorted((obj.get("files") or {}).keys()) if isinstance(obj.get("files"), dict) else []
            title = "mock agent (synthesised output)" if obj.get("synthesised") else \
                f"mock agent script {short(obj.get('script', ''))}"
            text = short(obj.get("output", ""))
            if files:
                text += "\nfiles written: " + ", ".join(files)
            return [{**base, "id": f"{n}", "k": "mock", "title": title, "text": text}]
        if agg.kind in {"empty", "plain"}:
            agg.kind = "claude"
        if kind == "system":
            subtype = obj.get("subtype")
            if subtype == "init":
                agg.model = str(obj.get("model") or "")
                agg.billing = billing_of(obj.get("apiKeySource"))
                if obj.get("cwd"):
                    short.add(str(obj["cwd"]), "")
                agg.action = "starting"
                tools = obj.get("tools") or []
                return [{**base, "id": f"{n}", "k": "init",
                         "title": f"session started · {obj.get('model', '?')} · {obj.get('permissionMode', '')}",
                         "text": f"cwd {short(obj.get('cwd', ''))} · {len(tools)} tools available"}]
            if subtype == "thinking_tokens":
                agg.thinking_tokens = max(agg.thinking_tokens, int(obj.get("estimated_tokens") or 0))
                agg.action = "thinking"
                return []
            if subtype == "ui_invalidate":
                return []
            if subtype == "task_started":
                return [{**base, "id": f"{n}", "k": "task", "tid": obj.get("tool_use_id"),
                         "title": f"task started: {clip(short(obj.get('description', '')), 200)}"}]
            if subtype == "task_notification":
                status = obj.get("status", "")
                return [{**base, "id": f"{n}", "k": "task", "tid": obj.get("tool_use_id"),
                         "err": status not in {"completed", ""},
                         "title": f"task {status}: {clip(short(obj.get('summary', '')), 200)}"}]
            return [{**base, "id": f"{n}", "k": "meta", "title": f"system {subtype or ''}".strip()}]
        if kind == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            windows = info.get("unifiedWindows") or {}
            used = ", ".join(f"{name} {round(float(w.get('utilization', 0)) * 100)}%"
                             for name, w in windows.items() if isinstance(w, dict))
            return [{**base, "id": f"{n}", "k": "meta", "err": info.get("status") not in {None, "allowed"},
                     "title": f"rate limit: {info.get('status', '?')}" + (f" ({used})" if used else "")}]
        if kind == "result":
            agg.final = True
            agg.is_error = bool(obj.get("is_error")) or obj.get("subtype") not in {None, "success"}
            cost = obj.get("total_cost_usd")
            agg.cost = float(cost) if isinstance(cost, (int, float)) else agg.cost
            agg.turns = obj.get("num_turns") if isinstance(obj.get("num_turns"), int) else agg.turns
            agg.duration_ms = obj.get("duration_ms") if isinstance(obj.get("duration_ms"), int) else agg.duration_ms
            agg.tokens = _usage(obj.get("usage"))
            agg.action = "finished" if not agg.is_error else "failed"
            if agg.is_error:
                agg.errors += 1
            return [{**base, "id": f"{n}", "k": "final", "err": agg.is_error,
                     "title": f"result: {obj.get('subtype', '?')}", "cost": agg.cost, "turns": agg.turns,
                     "duration_ms": agg.duration_ms, "tokens": dict(agg.tokens),
                     "text": clip(short(obj.get("result", "")), 4000),
                     "stop": obj.get("stop_reason") or obj.get("terminal_reason") or ""}]
        if kind in {"assistant", "user"}:
            message = obj.get("message") or {}
            content = message.get("content")
            sub = bool(obj.get("parent_tool_use_id"))
            if kind == "assistant":
                if message.get("model") and not agg.model:
                    agg.model = str(message["model"])
                if message.get("id") and isinstance(message.get("usage"), dict):
                    agg._message_usage[str(message["id"])] = _usage(message["usage"])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            rows = []
            for index, block in enumerate(content or []):
                if not isinstance(block, dict):
                    continue
                row = {**base, "id": f"{n}.{index}", "sub": sub}
                block_type = block.get("type")
                if block_type == "text":
                    text = short(block.get("text", ""))
                    if kind == "assistant":
                        agg.action = "writing: " + clip(text.strip().splitlines()[0] if text.strip() else "", 60)
                        rows.append({**row, "k": "text", "text": text})
                    else:
                        rows.append({**row, "k": "user", "text": clip(text, 4000)})
                elif block_type in {"thinking", "redacted_thinking"}:
                    agg.action = "thinking"
                    thought = str(block.get("thinking") or "")
                    rows.append({**row, "k": "thinking", "chars": len(thought),
                                 "ms": obj.get("thinking_duration_ms")})
                elif block_type == "tool_use":
                    agg.tools += 1
                    name = str(block.get("name", "tool"))
                    self.tool_names[str(block.get("id", ""))] = name
                    summary = summarise_tool(name, block.get("input"), short)
                    agg.action = summary.get("action", f"using {name}")
                    rows.append({**row, "k": "tool", "tid": block.get("id"), **summary})
                elif block_type == "tool_result":
                    text = _result_text(block.get("content"))
                    error = bool(block.get("is_error"))
                    meta = obj.get("tool_use_result") if isinstance(obj.get("tool_use_result"), dict) else {}
                    if error:
                        agg.errors += 1
                    tool = self.tool_names.get(str(block.get("tool_use_id", "")), "")
                    result = {**row, "k": "result", "tid": block.get("tool_use_id"), "tool": tool, "err": error,
                              "size": len(text), "lines": text.count("\n") + (1 if text else 0),
                              "text": clip(short(text), PREVIEW_CHARS), "more": len(text) > PREVIEW_CHARS}
                    if meta.get("structuredPatch"):
                        result["diff"] = [short(line) for line in _patch_lines(meta.get("structuredPatch"))]
                    if meta.get("stderr"):
                        result["stderr"] = clip(short(str(meta.get("stderr"))), 800)
                    if meta.get("interrupted"):
                        result["err"] = True
                    agg.action = "thinking"
                    rows.append(result)
                else:
                    rows.append({**row, "k": "meta", "title": f"{kind} {block_type}"})
            return rows
        return [{**base, "id": f"{n}", "k": "meta", "title": f"event {kind}",
                 "text": clip(short(json.dumps(obj, ensure_ascii=False)), 600)}]


def transcript_rows(path: Path, short: Shortener | None = None) -> tuple[list[dict[str, Any]], TranscriptAgg]:
    transcript = Transcript(short)
    _reset, lines = Tail(path, settle=0).read(max_bytes=64 * 1024 * 1024)
    return transcript.feed(lines), transcript.agg


# ============================================================================ job journal
#
# A job's agent step (`al scout --into … --raw`, `al author … --raw`) prints Claude Code's raw event stream. The job runner keeps it
# whole in steps/<name>.log and condenses it once, line by line as it arrives, into
# steps/<name>.journal.jsonl: one small event per line, each with `t` (the runner's clock), `kind` and
# `text`. The page reads the journal and never parses the stream; `al author` prints the same events.
#
#   message    Markdown the agent wrote (plain and mock output too)
#   tool       id, tool, target, text (summarise_tool's title), doing (what it is doing right now)
#   tool.done  id, tool, bytes, lines, error, ms (since its `tool` event)
#   error      a tool error's first line, an unreadable line, an `al: …` stderr line, an error result
#   report     the final report's Markdown, with decisions: [str] and left_out: [str]
#   result     cost, turns, ms, tokens, error
#   hidden     what: hook | init | system | rate | task | thinking | meta (shown only with "show everything")

JOURNAL_MESSAGE_CAP = 8000
JOURNAL_TARGET_CAP = 240
JOURNAL_HIDDEN_CAP = 300
JOURNAL_TAIL = 64 * 1024
CI_MARKERS = (".github/workflows/", ".gitlab-ci.yml", ".circleci/", "Jenkinsfile", ".forgejo/workflows/", ".woodpecker")
COMPILE_RX = re.compile(r"(?:^|[\s/;&|])(?:al compile|raco make)\b")
GENERIC_REASON = re.compile(r"^(?:\S+ exited -?\d+; see the output|the agent exited -?\d+)\.?$")
REPORT_ITEM_CAP = 600
_REPORT_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+(.*)$")


def first_line(text: Any) -> str:
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def short_duration(ms: Any) -> str:
    """Milliseconds as the page shows durations: 4.2s, 42s, 5m 01s, 1h 05m; "?" when unknown."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return "?"
    sec = max(0.0, ms / 1000)
    if sec < 10:
        return f"{sec:.1f}s"
    if sec < 60:
        return f"{round(sec)}s"
    minutes, rest = int(sec // 60), int(sec % 60)
    return f"{minutes}m {rest:02d}s" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"


class AgentJournal:
    """Condenses an agent's raw output, one line at a time, into journal events (see above). It wraps the
    Transcript normaliser of the run Step view, so both read Claude Code's stream the same way."""

    def __init__(self, short: Shortener | None = None, clock: Callable[[], float] = time.time) -> None:
        self.short = short or Shortener()
        self.clock = clock
        self.transcript = Transcript(self.short)
        self.open: dict[str, tuple[float, str]] = {}   # tool id → (t, tool) of a call without its result yet
        self.compiles = 0
        self.line_no = 0
        self.reported = False
        self.ended = False      # a `result` arrived: the agent's own end, with or without a report
        self.last_message = ""
        self.last_tool_error = ""

    def feed_line(self, line: str) -> list[dict[str, Any]]:
        """One line of the step's stdout → its journal events (none for an empty line)."""
        self.line_no += 1
        text = line.rstrip("\r\n")
        if not text.strip():
            return []
        t = self.clock()
        obj: Any = None
        if text.lstrip().startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
        if isinstance(obj, dict) and obj.get("type") == "system" and str(obj.get("subtype") or "").startswith("hook"):
            name = obj.get("hook_name") or obj.get("hook_event") or "hook"
            body = first_line(obj.get("output") or obj.get("stdout") or obj.get("stderr") or "")
            return [{"t": t, "kind": "hidden", "what": "hook",
                     "text": clip(self.short(f"{obj.get('subtype')} {name}" + (f": {body}" if body else "")), JOURNAL_HIDDEN_CAP)}]
        events: list[dict[str, Any]] = []
        for row in self.transcript.feed([(self.line_no, 0, text)]):
            events += self._event(row, obj if isinstance(obj, dict) else {}, t)
        return events

    def error(self, text: str) -> dict[str, Any]:
        """An `error` event of the step from outside the stream (the runner's `al: …` stderr lines)."""
        return {"t": self.clock(), "kind": "error", "text": clip(self.short(text.rstrip()), 1000)}

    def finish(self) -> list[dict[str, Any]]:
        """At the step's end: a stream that sent no result (the mock agent, a plain-text CLI) gets its
        last message as the report."""
        if self.reported or self.ended or not self.last_message.strip():
            return []
        self.reported = True
        return [{"t": self.clock(), "kind": "report", "text": self.last_message, **parse_report(self.last_message)}]

    def _event(self, row: dict[str, Any], obj: dict[str, Any], t: float) -> list[dict[str, Any]]:
        k = row.get("k")
        if k in ("text", "plain", "mock"):
            text = clip(str(row.get("text") or ""), JOURNAL_MESSAGE_CAP)
            if not text.strip():
                return []
            self.last_message = text
            return [{"t": t, "kind": "message", "text": text}]
        if k == "tool":
            tid, tool = str(row.get("tid") or ""), str(row.get("tool") or "tool")
            self.open[tid] = (t, tool)
            target = row.get("path") or first_line(row.get("cmd")) or row.get("pattern") or ""
            return [{"t": t, "kind": "tool", "id": tid, "tool": tool, "target": clip(str(target), JOURNAL_TARGET_CAP),
                     "text": clip(str(row.get("title") or tool), JOURNAL_TARGET_CAP), "doing": doing_of(row, self)}]
        if k == "result":
            tid = str(row.get("tid") or "")
            began, tool = self.open.pop(tid, (t, str(row.get("tool") or "")))
            error = bool(row.get("err"))
            events = [{"t": t, "kind": "tool.done", "id": tid, "tool": tool, "bytes": int(row.get("size") or 0),
                       "lines": int(row.get("lines") or 0), "error": error, "ms": max(0, int(round((t - began) * 1000))),
                       "text": ""}]
            if error:
                why = first_line(row.get("text")) or "(no output)"
                self.last_tool_error = clip(why, 400)
                events.append({"t": t, "kind": "error", "text": self.last_tool_error})
            return events
        if k == "final":
            self.ended = True
            error = bool(row.get("err"))
            events = []
            report = clip(self.short(str(obj.get("result") or row.get("text") or "")), JOURNAL_MESSAGE_CAP)
            if report.strip():
                self.reported = True
                events.append({"t": t, "kind": "report", "text": report, **parse_report(report)})
            events.append({"t": t, "kind": "result", "cost": row.get("cost"), "turns": row.get("turns"),
                           "ms": row.get("duration_ms"), "tokens": row.get("tokens") or {}, "error": error,
                           "text": f"{row.get('turns') or '?'} turns"})
            if error:
                said = first_line(report)
                text = said or f"the agent ended with {obj.get('subtype') or 'an error'}"
                if not said and self.last_tool_error:
                    text += f"; its last error: {self.last_tool_error}"
                events.append({"t": t, "kind": "error", "text": clip(text, 600)})
            return events
        if k == "corrupt":
            return [{"t": t, "kind": "error", "text": f"{row.get('title')}: {clip(str(row.get('text') or ''), 200)}"}]
        what = {"init": "init", "task": "task", "thinking": "thinking", "user": "meta"}.get(str(k), "meta")
        if obj.get("type") == "rate_limit_event":
            what = "rate"
        elif obj.get("type") == "system" and k == "meta":
            what = "system"
        text = row.get("title") or ("thinking" if k == "thinking" else str(k))
        if row.get("text") and k != "thinking":
            text = f"{text}: {row.get('text')}"
        return [{"t": t, "kind": "hidden", "what": what, "text": clip(str(text), JOURNAL_HIDDEN_CAP)}]


def doing_of(summary: dict[str, Any], journal: AgentJournal) -> str:
    """What a tool call says the agent is doing right now, in words a person reads at a glance."""
    tool = str(summary.get("tool") or "")
    if tool in ("Bash", "Shell") and COMPILE_RX.search(str(summary.get("cmd") or "")):
        journal.compiles += 1
        return f"compiling, attempt {journal.compiles}"
    target = str(summary.get("path") or summary.get("pattern") or "")
    if tool in ("Read", "Grep", "Glob") and any(m in f"{target} {summary.get('title') or ''}" for m in CI_MARKERS):
        return "reading the CI files"
    if tool == "Read" and target.endswith("LLM.txt"):
        return "reading the codebase facts (LLM.txt)"
    if tool in ("Write", "Edit", "MultiEdit") and target.endswith("agentic.rkt"):
        return "writing agentic.rkt"
    return str(summary.get("action") or f"using {tool}")


def _report_heading(line: str) -> str | None:
    """The title of a report line that opens a section (`### Title`, `**Title:**`, `Title:`), else None."""
    text = line.strip()
    if not text or _REPORT_BULLET.match(line):
        return None
    if text.startswith("#"):
        return text.lstrip("#").strip()
    bold = re.fullmatch(r"(\*\*|__)(.+?)\1\s*:?", text)
    if bold:
        return bold.group(2).strip().rstrip(":")
    if text.endswith(":") and len(text) <= 80:
        return text[:-1].strip()
    return None


def parse_report(text: str) -> dict[str, list[str]]:
    """The Decisions and Left out sections of an agent's report: the bullets under a heading, a bold
    line or a `…:` line that says "decision" or "left out", up to the next such line."""
    out: dict[str, list[str]] = {"decisions": [], "left_out": []}
    section: str | None = None
    items: list[str] | None = None
    for line in str(text or "").splitlines():
        heading = _report_heading(line)
        if heading is not None:
            lower = heading.lower()
            section = "decisions" if "decision" in lower else "left_out" if "left out" in lower else None
            items = out[section] if section else None
            continue
        if items is None:
            continue
        bullet = _REPORT_BULLET.match(line)
        if bullet:
            items.append(bullet.group(1).strip())
        elif items and line.strip() and line[:1].isspace():
            items[-1] = f"{items[-1]} {line.strip()}"
    return {key: [clip(item, REPORT_ITEM_CAP) for item in values if item] for key, values in out.items()}


def journal_line(event: dict[str, Any]) -> str | None:
    """One terminal (and output.log) line of a journal event, or None for what is not shown there."""
    kind = event.get("kind")
    if kind == "message":
        line = first_line(event.get("text"))
        return f"  {line}" if line else None
    if kind == "tool":
        return f"  · {event.get('text') or event.get('tool')}"
    if kind == "tool.done":
        return f"  ! {event.get('tool') or 'a tool'} failed" if event.get("error") else None
    if kind == "error":
        return f"  ! {event.get('text')}"
    if kind == "result":
        cost = event.get("cost")
        money = f"${cost:.2f}" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "$?"
        return f"  = {event.get('turns') if event.get('turns') is not None else '?'} turns, {short_duration(event.get('ms'))}, {money}"
    return None


def step_files(directory: Path, step: Any) -> dict[str, Path]:
    """A job step's files: {"log": steps/<step>.log, "journal": steps/<step>.journal.jsonl}; {} for a name
    that is not SAFE_NAME (nothing outside steps/ can be named)."""
    if not isinstance(step, str) or not SAFE_NAME.match(step):
        return {}
    base = Path(directory) / "steps"
    return {"log": base / f"{step}.log", "journal": base / f"{step}.journal.jsonl"}


def confined_file(directory: Path, path: Path) -> bool:
    """A regular file, not a symlink, whose real path stays inside the job directory."""
    try:
        return (not path.is_symlink() and path.is_file()
                and os.path.realpath(path).startswith(os.path.realpath(directory) + os.sep))
    except OSError:
        return False


# ============================================================================ layer and graph

def build_graph(adw: dict[str, Any], layer_ir: dict[str, Any] | None = None) -> dict[str, Any]:
    """Nodes and edges of one ADW, laid out left → right. Consecutive `when`-conditioned steps are
    alternative branches stacked in one column; a check with a resolve command gets a resolve
    loop; a review with patch + implement gets a back-edge to the first check (re-check); a fan-out
    becomes one lane per candidate followed by a pick node."""
    layer_ir = layer_ir or {}
    reviews = {r.get("id"): r for r in layer_ir.get("reviews") or [] if isinstance(r, dict)}
    review_option = reviews.get(adw.get("review"), {})
    steps = [s for s in adw.get("steps") or [] if isinstance(s, dict)]
    node_w, node_h, gap_x, gap_y, pad = 132, 54, 46, 18, 24
    columns: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        when = str(step.get("when") or "always")
        if step.get("kind") == "run" and when != "always":
            group = []
            while index < len(steps) and steps[index].get("kind") == "run" and str(steps[index].get("when") or "always") != "always":
                group.append(steps[index])
                index += 1
            columns.append([_node(s, "branch") for s in group])
            continue
        if step.get("kind") == "fan-out":
            count = max(1, int(step.get("count") or 1))
            lanes = [{"id": f"{step['name']}#{i}", "step": step["name"], "type": "candidate", "lane": i,
                      "label": f"candidate {i}", "sub": str(step.get("adw", "")), "when": "always"}
                     for i in range(1, count + 1)]
            columns.append(lanes)
            columns.append([_node(step, "flow")])
            index += 1
            continue
        columns.append([_node(step, "flow")])
        index += 1
    setup = _setup_node(adw, layer_ir)
    if setup:  # the run's prepare step (#:prepare, or a dev container's postCreateCommand) comes first
        columns.insert(0, [setup])
    has_back = any(s.get("kind") == "review" and s.get("patch") and s.get("implement") for s in steps)
    looped = {s["name"] for s in steps if s.get("kind") == "check" and s.get("resolve")}
    nodes: list[dict[str, Any]] = []
    band, widest, col = 0.0, 1, 0
    for band_columns in _graph_rows(columns):
        # a long workflow wraps onto further rows, so that it fits a laptop window without scrolling
        tallest = max(len(column) for column in band_columns)
        top_margin = 40 if any(node["id"] in looped for column in band_columns for node in column) else 16
        content = tallest * node_h + (tallest - 1) * gap_y
        middle = band + top_margin + content / 2
        for x_index, column in enumerate(band_columns):
            span = len(column) * node_h + (len(column) - 1) * gap_y
            top = middle - span / 2
            for row, node in enumerate(column):
                node.update(col=col, row=row, x=pad + x_index * (node_w + gap_x),
                            y=round(top + row * (node_h + gap_y), 1), w=node_w, h=node_h, band=band)
                nodes.append(node)
            col += 1
        widest = max(widest, len(band_columns))
        band += top_margin + content + GRAPH_ROW_GAP
    height = max(band - GRAPH_ROW_GAP, 16 + node_h) + (58 if has_back else 16)
    edges: list[dict[str, Any]] = []
    for col in range(len(columns) - 1):
        for source in columns[col]:
            for target in columns[col + 1]:
                kind = "branch" if target.get("when", "always") != "always" else \
                    ("fan" if target["type"] == "candidate" else "flow")
                label = target["when"].removeprefix("class-") if kind == "branch" else ""
                if source.get("when", "always") != "always":
                    kind = "join"
                if source["type"] == "candidate":
                    kind = "fan"
                edges.append({"from": source["id"], "to": target["id"], "kind": kind, "label": label})
    by_name = {node["id"]: node for node in nodes}
    first_check = next((s["name"] for s in steps if s.get("kind") == "check"), None)
    for step in steps:
        if step.get("kind") == "check" and step.get("resolve"):
            edges.append({"from": step["name"], "to": step["name"], "kind": "resolve",
                          "label": f"resolve ×{int(step.get('retries') or 0)} · {step['resolve']}"})
        if step.get("kind") == "review" and step.get("patch") and step.get("implement"):
            target = first_check if first_check and by_name.get(first_check, {}).get("col", 0) < by_name[step["name"]]["col"] else step["name"]
            retries = int(review_option.get("maxRetries", 0) or 0)
            edges.append({"from": step["name"], "to": target, "kind": "patch",
                          "label": f"blocker → {step['patch']} → {step['implement']} → re-check"
                                   + (f" (≤{retries})" if retries else "")})
    width = pad * 2 + widest * (node_w + gap_x) - gap_x
    return {"nodes": nodes, "edges": edges, "width": width, "height": height,
            "review": {"kind": review_option.get("kind", ""), "reviewers": review_option.get("reviewers", 0),
                       "ship": review_option.get("ship", ""), "maxRetries": review_option.get("maxRetries", 0)}}


GRAPH_MAX_COLUMNS = 7   # 7 columns are 1248 graph units: a 1280-pixel window shows them without scrolling
GRAPH_ROW_GAP = 30


def _graph_rows(columns: list[list[dict[str, Any]]]) -> list[list[list[dict[str, Any]]]]:
    """Columns split into rows of at most GRAPH_MAX_COLUMNS, balanced (12 columns: 6 + 6). A row
    does not end on a column of alternatives (branches) when it can avoid it: the edge into the
    next row then leaves from one node."""
    if len(columns) <= GRAPH_MAX_COLUMNS:
        return [columns] if columns else []
    count = -(-len(columns) // GRAPH_MAX_COLUMNS)
    per_row = -(-len(columns) // count)
    rows: list[list[list[dict[str, Any]]]] = []
    rest = list(columns)
    while rest:
        take = min(per_row, len(rest))
        if take < len(rest) and take > 1 and len(rest[take - 1]) > 1:
            take -= 1
        rows.append(rest[:take])
        rest = rest[take:]
    return rows


PROMOTE_ACTIONS = {"open": "pin the stage", "checks": "stage checks", "pack": "presentation pack",
                   "request": "pull request / approval item", "approval": "people decide", "version": "version + changelog",
                   "merge": "merge into the next stage", "tag": "tag", "deploy": "deploy", "back-merge": "merge back",
                   "close": "sign-off + close"}


INTEGRATE_STEPS = {"prepare": "apply the branch", "resolve": "resolve conflicts", "approve": "a person approves"}


def _node(step: dict[str, Any], role: str) -> dict[str, Any]:
    kind = str(step.get("kind", "run"))
    types = {"run": "agent", "check": "check", "script": "script", "review": "review", "ship": "ship",
             "fan-out": "fan-out", "promote": "promote", "integrate": "integrate"}
    sub = {"run": step.get("command", ""), "check": step.get("cmd", ""), "script": f"$ {step.get('cmd', '')}",
           "review": "review", "ship": "gate + ship", "fan-out": f"pick {step.get('pick', 'best')}",
           "promote": PROMOTE_ACTIONS.get(str(step.get("action") or step.get("name") or ""), ""),
           "integrate": INTEGRATE_STEPS.get(str(step.get("action") or step.get("name") or ""), "")}.get(kind, "")
    if kind == "run" and step.get("agent"):
        sub = f"{sub} @{step['agent']}"
    return {"id": str(step.get("name", "")), "step": str(step.get("name", "")), "type": types.get(kind, kind),
            "label": str(step.get("name", "")), "sub": str(sub), "when": str(step.get("when") or "always"),
            "role": role}


def _setup_node(adw: dict[str, Any], layer_ir: dict[str, Any]) -> dict[str, Any] | None:
    """The `prepare` node (type `setup`) of an ADW whose worktree option has #:prepare, or whose podman
    option reuses a dev container (its postCreateCommand is then the prepare step), else None."""
    options = {o.get("id"): o for o in layer_ir.get("environments") or [] if isinstance(o, dict)}
    stack = [options[i] for i in adw.get("environment") or [] if i in options]
    prepare = next((list(o.get("prepare") or []) for o in stack if o.get("kind") == "worktree" and o.get("prepare")), [])
    devcontainer = any(o.get("kind") == "podman" and o.get("devcontainer") for o in stack)
    if not prepare and not devcontainer:
        return None
    return {"id": "prepare", "step": "prepare", "type": "setup", "label": "prepare",
            "sub": " ".join(prepare) if prepare else "postCreateCommand", "when": "always", "role": "flow"}


@dataclass
class Layer:
    key: str
    path: Path          # resolved
    ir: dict[str, Any]
    sig: tuple[int, int, int] | None

    @property
    def name(self) -> str:
        return str(self.ir.get("name") or self.path.name)

    def adw(self, name: str) -> dict[str, Any] | None:
        return next((a for a in self.ir.get("adws") or [] if isinstance(a, dict) and a.get("name") == name), None)

    def limits(self) -> dict[str, int]:
        option = self.ir.get("concurrency") or {}
        return {pool: int(option.get(key, default) or default) for pool, (key, default) in POOL_DEFAULTS.items()}

    def budget(self) -> dict[str, float | None] | None:
        """The declared spending limits (budget limits …), or None."""
        option = self.ir.get("budget")
        if not isinstance(option, dict):
            return None
        return {name: (float(option[key]) if isinstance(option.get(key), (int, float)) else None)
                for name, key in (("run", "runUsd"), ("step", "stepUsd"), ("day", "dayUsd"))}

    def usage_threshold(self) -> dict[str, Any] | None:
        """The declared subscription usage threshold (#:limit-stop-percent …), or None when it is off."""
        option = self.ir.get("budget")
        if not isinstance(option, dict):
            return None
        percent = option.get("limitStopPercent")
        if isinstance(percent, bool) or not isinstance(percent, int) or percent <= 0:
            return None
        minutes = option.get("limitWaitMinutes")
        return {"percent": percent, "on_limit": "wait" if option.get("onLimit") == "wait" else "stop",
                "wait_minutes": minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0 else 300}

    def shortener(self, state: Any = None) -> Shortener:
        """Absolute paths a run recorded, made relative for reading: the run's worktree is the
        repository; the layer is `layer:` beside the repository, or its directory in the repository
        when it is checked in with it (`agentic/specs/…`)."""
        short = Shortener()
        if isinstance(state, dict) and state.get("worktree"):
            short.add(str(state["worktree"]), "")
        placement = self.ir.get("layer") if isinstance(self.ir.get("layer"), dict) else {}
        root = str((self.ir.get("codebase") or {}).get("root") or "")
        if placement.get("kind") == "in-repo" and root:
            short.add(str((self.path / root).resolve()), "")
        else:
            short.add(str(self.path), "layer:")
        return short

    def explain_file(self) -> str | None:
        for name in EXPLAIN_FILES:
            if (self.path / name).is_file():
                return name
        return None

    def models_view(self, read_only: bool) -> dict[str, Any]:
        """Which model each declared tier means and why, without a run's flags or environment:
        the local setting, else the first non-mock agent that declares the tier."""
        error = ""
        try:
            local = read_settings(self.path)
        except ValueError as exc:
            local, error = {}, str(exc)
        agents = [a for a in self.ir.get("agents") or [] if isinstance(a, dict) and a.get("kind") != "mock"]
        tiers = []
        for tier in declared_tiers(self.ir):
            declaring = [a for a in agents if tier in (agent_tiers(a) or {})]
            if tier in local:
                model, source = local[tier], "local setting"
            elif declaring:
                agent = declaring[0]
                model = (agent_tiers(agent) or {})[tier]["model"]
                default = BUILTIN_TIERS.get(str(agent.get("kind") or ""), {}).get(tier)
                source = "built-in default" if model == default else "layer"
            else:   # no agent states a model: the built-in default of the first agent's kind, if any
                model = next((BUILTIN_TIERS[str(a.get("kind"))][tier] for a in agents
                              if tier in BUILTIN_TIERS.get(str(a.get("kind") or ""), {})), "")
                source = "built-in default" if model else ""
            tiers.append({"tier": tier, "model": model, "source": source, "local": local.get(tier),
                          "agents": [str(a.get("id") or "") for a in declaring]})
        writable, why = settings_writable(self, read_only)
        return {"tiers": tiers, "known": list(KNOWN_MODELS), "writable": writable, "why": why, "error": error}


def find_layers(roots: Iterable[Path], depth: int = 4) -> list[Path]:
    """Directories containing agentic-layer.json below each root (the root itself included)."""
    found: list[Path] = []
    seen: set[str] = set()

    def walk(directory: Path, level: int) -> None:
        if (directory / LAYER_FILE).is_file():
            real = os.path.realpath(directory)
            if real not in seen:
                seen.add(real)
                found.append(Path(real))
            return
        if level >= depth:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and entry.name not in PRUNE_DIRS:
                    walk(Path(entry.path), level + 1)
            except OSError:
                continue

    for root in roots:
        walk(Path(root), 0)
    return found


def load_layer(path: Path, taken: set[str]) -> Layer | None:
    ir = read_json(path / LAYER_FILE)
    if not isinstance(ir, dict):
        return None
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(ir.get("name") or path.name)).strip("-") or "layer"
    key, suffix = base, 2
    while key in taken:
        key, suffix = f"{base}-{suffix}", suffix + 1
    return Layer(key=key, path=path, ir=ir, sig=stat_sig(path / LAYER_FILE))


# ============================================================================ local model setting

def check_model_id(model: Any) -> str | None:
    """mirror of kit/runtime/adw_modules/models.py; tests/tools/test_al_models.py holds them equal"""
    if not isinstance(model, str) or not MODEL_ID_RX.fullmatch(model):
        raise ValueError(f"model id must be a non-empty string without whitespace, got {model!r}")
    if model not in KNOWN_MODELS:
        return f"{model} is not a known model id (known: {', '.join(KNOWN_MODELS)}); it is passed to the agent as is"
    return None


def check_setting(tier: Any, model: Any, known_tiers: list[str]) -> str | None:
    """mirror of kit/runtime/adw_modules/models.py; tests/tools/test_al_models.py holds them equal"""
    if not isinstance(tier, str) or not TIER_RX.fullmatch(tier) or tier not in known_tiers:
        raise ValueError(f"no agent of this layer has a tier `{tier}`; declared: {', '.join(known_tiers)}")
    return check_model_id(model)


def agent_tiers(agent: Any) -> dict[str, dict[str, str]] | None:
    """mirror of kit/runtime/adw_modules/models.py; tests/tools/test_al_models.py holds them equal"""
    if not isinstance(agent, dict):
        return None
    models = agent.get("models")
    if isinstance(models, list):
        return {str(m.get("tier")): {"model": str(m.get("model") or ""), "effort": str(m.get("effort") or "default")}
                for m in models if isinstance(m, dict) and m.get("tier")}
    if "modelBase" in agent or "modelHeavy" in agent:
        return {"base": {"model": str(agent.get("modelBase") or ""), "effort": "default"},
                "heavy": {"model": str(agent.get("modelHeavy") or ""), "effort": "default"}}
    return None


def declared_tiers(ir: dict[str, Any]) -> list[str]:
    """mirror of kit/runtime/adw_modules/models.py; tests/tools/test_al_models.py holds them equal"""
    seen: list[str] = []
    for agent in ir.get("agents") or []:
        for tier in (agent_tiers(agent) or {"base": {}, "heavy": {}}):
            if tier not in seen:
                seen.append(tier)
    return seen or ["base", "heavy"]


def render_settings(mapping: dict[str, str]) -> str:
    """mirror of kit/runtime/adw_modules/models.py; tests/tools/test_al_models.py holds them equal"""
    return json.dumps({"version": 1, "models": dict(sorted(mapping.items()))}, indent=2, sort_keys=True) + "\n"


def read_settings(layer_dir: Path) -> dict[str, str]:
    """mirror of kit/runtime/adw_modules/models.py (ValueError instead of LayerError)"""
    path = Path(layer_dir) / SETTINGS_FILE
    hint = "fix or remove it (al models LAYER --reset)"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"{SETTINGS_FILE}: cannot read it ({exc.strerror}); {hint}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{SETTINGS_FILE}: not JSON; {hint}") from exc
    mapping = data.get("models") if isinstance(data, dict) else None
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise ValueError(f'{SETTINGS_FILE}: expected {{"version": 1, "models": {{TIER: MODEL_ID}}}}; {hint}')
    for tier, model in mapping.items():
        if not TIER_RX.fullmatch(tier):
            raise ValueError(f"{SETTINGS_FILE}: {tier!r} is not a tier name; {hint}")
        try:
            check_model_id(model)
        except ValueError as exc:
            raise ValueError(f"{SETTINGS_FILE}: {exc}; {hint}") from exc
    return dict(mapping)


def write_settings(layer_dir: Path, mapping: dict[str, str]) -> Path:
    """mirror of kit/runtime/adw_modules/models.py; tests/tools/test_al_models.py holds them equal"""
    path = Path(layer_dir) / SETTINGS_FILE
    if not mapping:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix=".models.", suffix=".tmp")
    try:
        with handle:
            handle.write(render_settings(mapping))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def settings_writable(layer: "Layer", read_only: bool) -> tuple[bool, str]:
    """Can the Models panel write this layer's local setting? → (writable, why not)."""
    if read_only:
        return False, "the dashboard was started with --read-only"
    why = ("agents/settings is not writable here (in the podman dashboard only layers given directly are "
           "mounted writable); use `al models LAYER --set TIER=ID` on the host")
    root = Path(os.path.realpath(layer.path))
    target = layer.path / SETTINGS_FILE
    for directory in (target.parent, target.parent.parent):   # agents/settings, agents/
        real = Path(os.path.realpath(directory))
        if real != root and root not in real.parents:
            return False, why   # a symlink that leaves the layer
    for directory in (target.parent, target.parent.parent, layer.path):
        if directory.exists():
            return (True, "") if directory.is_dir() and os.access(directory, os.W_OK | os.X_OK) else (False, why)
    return False, why


# ============================================================================ starting work

_ARG_HINT = re.compile(r"^argument-hint:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_COUNT_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def argument_hint(layer: Layer, kind: str, name: str) -> str:
    """The `argument-hint` of the generated commands/<name>.md or hops/<name>.md front matter
    ("[prompt]" when there is none). Only a regular file inside that directory of the layer is read."""
    directory = "hops" if kind == "hop" else "commands"
    if not SAFE_NAME.match(name):
        return "[prompt]"
    root = Path(os.path.realpath(layer.path / directory))
    target = Path(os.path.realpath(root / f"{name}.md"))
    if target.parent != root or not target.is_file():
        return "[prompt]"
    text = read_text(target, cap=4096)
    head = text.split("\n---", 1)[0] if text.startswith("---") else ""
    match = _ARG_HINT.search(head)
    return clip(match.group(1), 120) if match and match.group(1) else "[prompt]"


def _count(n: int, noun: str) -> str:
    word = _COUNT_WORDS[n] if 0 <= n < len(_COUNT_WORDS) else str(n)
    return f"{word} {noun}{'' if n == 1 else 's'}"


def _review(ir: dict[str, Any], review_id: Any) -> dict[str, Any]:
    return next((r for r in ir.get("reviews") or [] if isinstance(r, dict) and r.get("id") == review_id), {})


def ships_unattended(ir: dict[str, Any], adw: dict[str, Any]) -> str:
    """mirror of kit/runtime/adw_modules/launch.py: "merge" / "deploy" when the workflow ships
    without a person (an auto review and a ship step), else ""."""
    if not any(isinstance(s, dict) and s.get("kind") == "ship" for s in adw.get("steps") or []):
        return ""
    review = _review(ir, adw.get("review"))
    return str(review["ship"]) if review.get("kind") == "auto" and review.get("ship") in ("merge", "deploy") else ""


def _command_entry(ir: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    for key, kind in (("commands", "command"), ("hops", "hop")):
        for entry in ir.get(key) or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                return kind, entry
    return "", {}


_TEMPLATE_VERBS = {"classify": "classifies", "plan-chore": "plans", "plan-bug": "plans", "plan-feature": "plans",
                   "plan-patch": "plans", "build": "builds", "run-plan": "builds", "commit": "commits",
                   "review": "reviews", "document": "documents", "test": "tests", "resolve-test": "fixes tests",
                   "prime": "reads the codebase"}


def workflow_summary(ir: dict[str, Any], adw: dict[str, Any]) -> str:
    """What a workflow does, from its steps: "plans, builds, checks test, one reviewer, merges without a person"."""
    phrases: list[str] = []
    review = _review(ir, adw.get("review"))
    for step in adw.get("steps") or []:
        if not isinstance(step, dict):
            continue
        kind = step.get("kind")
        if kind == "run":
            _kind, entry = _command_entry(ir, str(step.get("command") or ""))
            phrase = _TEMPLATE_VERBS.get(str(entry.get("template") or ""), f"runs {step.get('command')}")
        elif kind == "check":
            phrase = f"checks {step.get('cmd')}"
        elif kind == "script":
            phrase = f"runs the script {step.get('cmd')}"
        elif kind == "review":
            if review.get("kind") in ("auto", "agent"):
                n = review.get("reviewers") if isinstance(review.get("reviewers"), int) else 1
                phrase = _count(int(n), "reviewer")
            else:
                phrase = "a person reviews"
        elif kind == "ship":
            unattended = ships_unattended(ir, adw)
            phrase = (f"{'merges' if unattended == 'merge' else 'deploys'} without a person" if unattended
                      else "hands a pull request to a person")
        elif kind == "fan-out":
            phrase = f"races {_count(int(step.get('count') or 0), 'candidate')}"
        elif kind == "compose":
            phrase = f"runs {step.get('adw')}"
        else:
            phrase = str(kind)
        if not phrases or phrases[-1] != phrase:
            phrases.append(phrase)
    return ", ".join(phrases) or "no steps"


_OUTPUT_WORDS = {"path": "writes a plan", "json": "answers in JSON", "text": "answers in text"}
_PRODUCES_WORDS = {"changes": "changes the code", "command": "writes a new command", "adw": "writes a new workflow",
                   "expert": "writes a new expert"}


def command_summary(kind: str, entry: dict[str, Any]) -> str:
    name = str(entry.get("name"))
    tier = str(entry.get("modelSet") or ("heavy" if kind == "hop" else "base"))
    what = (_PRODUCES_WORDS.get(str(entry.get("produces") or ""), "makes changes") if kind == "hop"
            else _OUTPUT_WORDS.get(str(entry.get("output") or ""), "answers"))
    return f"one step: {name} ({tier} tier), {what}; never ships"


def _trigger_starts(ir: dict[str, Any], adw: dict[str, Any]) -> bool:
    kinds = {str(t.get("id")): str(t.get("kind")) for t in ir.get("triggers") or [] if isinstance(t, dict)}
    return any(kinds.get(str(t)) in ("cron", "webhook") for t in adw.get("trigger") or [])


def _issue_input(ir: dict[str, Any], adw: dict[str, Any]) -> str:
    kinds = {str(o.get("id")): str(o.get("kind")) for o in ir.get("promptInputs") or [] if isinstance(o, dict)}
    return next((str(i) for i in adw.get("input") or [] if kinds.get(str(i)) == "issue"), "")


def startables(layer: Layer, decider: str = "off") -> dict[str, Any]:
    """Everything a person may start in this layer, from the compiled IR (adopted workflows and
    prompts included, nothing else is consulted): its workflows, and its commands and hops (each one
    a single-command run that never ships). `decider` is the layer's stand-in mode now (what a start
    without its own `decider` runs with)."""
    ir = layer.ir
    forge_kind = str((ir.get("forge") or {}).get("kind") or "none") if isinstance(ir.get("forge"), dict) else "none"
    workflows = []
    for adw in ir.get("adws") or []:
        if not isinstance(adw, dict) or not isinstance(adw.get("name"), str):
            continue
        review = _review(ir, adw.get("review"))
        by_trigger = forge_kind != "none" and bool(_issue_input(ir, adw)) and _trigger_starts(ir, adw)
        workflows.append({
            "name": adw["name"], "description": str(adw.get("description") or ""),
            "classes": list(adw.get("classes") or []), "input": list(adw.get("input") or []),
            "trigger": list(adw.get("trigger") or []), "environment": list(adw.get("environment") or []),
            "review": {"id": str(review.get("id") or adw.get("review") or ""), "kind": str(review.get("kind") or ""),
                       "ship": str(review.get("ship") or ""), "allowedClasses": list(review.get("allowedClasses") or []),
                       "reviewers": review.get("reviewers") if isinstance(review.get("reviewers"), (int, list)) else None},
            "steps": [str(s.get("name")) for s in adw.get("steps") or [] if isinstance(s, dict)],
            "ships_unattended": ships_unattended(ir, adw), "issue_trigger": by_trigger,
            "summary": workflow_summary(ir, adw)})
    commands = []
    seen: set[str] = set()
    for key, kind in (("commands", "command"), ("hops", "hop")):
        for entry in ir.get(key) or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or entry["name"] in seen:
                continue
            seen.add(entry["name"])
            commands.append({"name": entry["name"], "kind": kind, "description": str(entry.get("description") or ""),
                             "argument_hint": argument_hint(layer, kind, entry["name"]),
                             "tier": str(entry.get("modelSet") or ("heavy" if kind == "hop" else "base")),
                             "output": str(entry.get("output") or entry.get("produces") or ""),
                             "summary": command_summary(kind, entry)})
    names = {w["name"] for w in workflows}
    return {"workflows": workflows, "commands": commands, "forge": forge_kind,
            "issue": {"available": forge_kind != "none",
                      "trigger": [w["name"] for w in workflows if w["issue_trigger"]]},
            "tiers": declared_tiers(ir), "classes": list(START_CLASSES),
            "decider": {"modes": list(DECIDER_MODES), "layer": decider if decider in DECIDER_MODES else "off"},
            "collisions": sorted(c["name"] for c in commands if c["name"] in names)}


def split_slash(text: str, catalog: dict[str, Any]) -> tuple[dict[str, str], str] | None:
    """`/NAME rest…` → ({kind, name}, rest) by the same rule as launch.resolve_target: a workflow
    wins over a command of the same name, `/command:NAME` is always the command. None when NAME is
    neither."""
    match = re.match(r"^\s*/(\S+)(?:\s+(.*))?$", text or "", re.DOTALL)
    if not match:
        return None
    head, rest = match.group(1), (match.group(2) or "").strip()
    forced = head.startswith(COMMAND_PREFIX)
    if forced:
        head = head[len(COMMAND_PREFIX):]
    if not forced and any(w["name"] == head for w in catalog["workflows"]):
        return {"kind": "workflow", "name": head}, rest
    if any(c["name"] == head for c in catalog["commands"]):
        return {"kind": "command", "name": head}, rest
    return None


def start_status(layer: Layer, model: "Model") -> dict[str, Any]:
    """Can work be started in this layer from the page? → {enabled, why}."""
    if model.read_only:
        return {"enabled": False, "why": "the dashboard was started with --read-only"}
    if model.no_start:
        return {"enabled": False, "why": "the dashboard was started with --no-start"}
    if model.in_container and not model.allow_start:
        return {"enabled": False, "why": "the dashboard runs in a container, which cannot start runs "
                                         "(--allow-start-in-container only for an image built for it)"}
    if not (layer.path / LAUNCH_MODULE).is_file() or not (layer.path / LAUNCHER).is_file():
        return {"enabled": False, "why": f"generated before starting from the dashboard: run `al gen {layer.name}`"}
    return {"enabled": True, "why": ""}


def promotion_via_layer(layer: Layer, argv: list[str], timeout: float = START_TIMEOUT) -> tuple[int, dict[str, Any], str]:
    """Run the layer's own `adws/promote.py ARGV --json` (decide, or start / continue a promotion run,
    which it detaches): argv list, no shell, the dashboard's token not passed on. → (exit code, its
    JSON result, stderr tail)."""
    command = [sys.executable, str(layer.path / PROMOTE_SCRIPT), *argv, "--json"]
    env = {k: v for k, v in os.environ.items() if k != "AL_DASHBOARD_TOKEN"}
    try:
        done = subprocess.run(command, capture_output=True, cwd=str(layer.path), env=env, timeout=timeout,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return -1, {}, f"the layer's promote.py took longer than {int(timeout)} s"
    except OSError as exc:
        return -1, {}, f"cannot run the layer's promote.py: {exc}"
    tail = done.stderr.decode("utf-8", "replace")[-2000:]
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    return done.returncode, result if isinstance(result, dict) else {}, tail


def integrate_via_layer(layer: Layer, argv: list[str], timeout: float = START_TIMEOUT) -> tuple[int, dict[str, Any], str]:
    """Run the layer's own `adws/integrate.py ARGV --json` (preview, or start / approve an integration
    run, which it detaches): argv list, no shell, the dashboard's token not passed on. → (exit code,
    its JSON result, stderr tail)."""
    command = [sys.executable, str(layer.path / INTEGRATE_SCRIPT), *argv, "--json"]
    env = {k: v for k, v in os.environ.items() if k != "AL_DASHBOARD_TOKEN"}
    try:
        done = subprocess.run(command, capture_output=True, cwd=str(layer.path), env=env, timeout=timeout,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return -1, {}, f"the layer's integrate.py took longer than {int(timeout)} s"
    except OSError as exc:
        return -1, {}, f"cannot run the layer's integrate.py: {exc}"
    tail = done.stderr.decode("utf-8", "replace")[-2000:]
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    return done.returncode, result if isinstance(result, dict) else {}, tail


def read_integration(layer_dir: Path, run_id: str) -> dict[str, Any] | None:
    """agents/<id>/integration.json: the integration run that brings (or brought, or could not bring)
    this handed-over run onto its target; None when there is none."""
    data = read_json(Path(layer_dir) / "agents" / run_id / INTEGRATION_FILE)
    if not isinstance(data, dict) or not RUN_ID.match(str(data.get("run") or "")):
        return None
    return {key: str(data.get(key) or "") for key in ("run", "status", "why", "commit", "target", "by", "at")}


def integrate_settings(layer_dir: Path) -> tuple[dict[str, Any], str]:
    """(what the page needs of templates/integrate.json: enabled, approval; why it cannot be read).
    The runtime (adw_modules/integrate.py settings()) is the authority; this mirrors its rules."""
    path = Path(layer_dir) / INTEGRATE_SETTINGS
    out = {"enabled": True, "approval": "none"}
    if not path.is_file():
        return out, ""
    data = read_json(path)
    known = {"version", "enabled", "approval", "decider", "generated"}
    if not isinstance(data, dict):
        return out, f"{INTEGRATE_SETTINGS} is not a JSON object"
    if set(data) - known:
        return out, f"{INTEGRATE_SETTINGS}: unknown key {', '.join(sorted(set(data) - known))}"
    if not isinstance(data.get("enabled", True), bool) or data.get("approval", "none") not in ("none", "person"):
        return out, f"{INTEGRATE_SETTINGS}: enabled is true or false, approval is none or person"
    return {"enabled": data.get("enabled", True), "approval": data.get("approval", "none")}, ""


def integrate_offer(layer: Layer, summary: dict[str, Any]) -> dict[str, Any]:
    """Does the page offer "Resolve with an agent" for this run? → {offered, why, conflict, script}:
    the layer has adws/integrate.py, its settings are readable and say enabled, the run is handed over
    and not settled, a handover reason is a merge conflict or a failed ship, and its review did not
    reject it. `conflict` says its reasons name one (the page then shows why it is not offered).
    Everything else (a protected path, a changed branch, …) the runtime's preview decides."""
    reasons = [str(r) for r in (summary.get("outcome") or {}).get("handover") or []]
    conflict = any(r.startswith("merge-conflict") or r.startswith("ship-failed:") for r in reasons)
    script = (layer.path / INTEGRATE_SCRIPT).is_file()
    settings, problem = integrate_settings(layer.path)

    def answer(why: str) -> dict[str, Any]:
        return {"offered": not why, "why": why, "conflict": conflict, "script": script}

    if summary.get("integrates"):
        return answer(f"an integration run: integrate the run it names, {summary['integrates']}")
    if summary.get("status") != "handed-over":
        return answer("only a run that ended handed over is integrated")
    if summary.get("settlement"):
        return answer("the run is settled")
    if "review-rejected" in reasons or (summary.get("outcome") or {}).get("review") is False:
        return answer("its review rejected it")
    if not conflict:
        return answer("it was not handed over because of a merge conflict or a moved target")
    if not script:
        return answer(f"run `al gen` to add {INTEGRATE_SCRIPT}")
    if problem:
        return answer(problem)
    if not settings["enabled"]:
        return answer(f"switched off in {INTEGRATE_SETTINGS}")
    return answer("")


def integrate_status(layer: Layer, model: "Model") -> dict[str, Any]:
    """Can an integration be started in this layer from the page? → {enabled, why}."""
    if model.read_only:
        return {"enabled": False, "why": "the dashboard was started with --read-only"}
    if model.no_start:
        return {"enabled": False, "why": "the dashboard was started with --no-start"}
    if model.in_container and not model.allow_start:
        return {"enabled": False, "why": "the dashboard runs in a container, which cannot start runs "
                                         "(--allow-start-in-container only for an image built for it)"}
    if not (layer.path / INTEGRATE_SCRIPT).is_file():
        return {"enabled": False, "why": f"generated before `al integrate`: run `al gen {layer.name}`"}
    return {"enabled": True, "why": ""}


def promotion_decisions(layer: Layer, promotion_id: str) -> list[dict[str, Any]]:
    """promotions/<id>/acceptance.jsonl: one decision per line (who, when, what, comment)."""
    try:
        path = safe_layer_file(layer, f"promotions/{promotion_id}/acceptance.jsonl")
    except Refused:
        return []
    out = []
    for line in read_text(path, cap=2_000_000).splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("decision") in PROMOTION_DECISIONS:
            out.append({k: item.get(k) for k in ("at", "by", "kind", "scope", "decision", "then", "comment", "source")})
    return out


def open_promotions(layer: Layer) -> list[dict[str, Any]]:
    """agents/promotions/*.json: the promotions that are open (waiting for people, or stopped)."""
    directory = layer.path / OPEN_PROMOTIONS
    out = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        data = read_json(path)
        if isinstance(data, dict) and data.get("id"):
            out.append({k: data.get(k) for k in ("id", "from", "to", "from_branch", "to_branch", "opened_at")})
    return out


def decided(change_id: str, decisions: list[dict[str, Any]]) -> dict[str, str] | None:
    """A change's status under the recorded decisions (mirror of promotion.change_status): per person and
    scope the latest counts; a refusal of it (or of all) wins, else an acceptance or an approval of all."""
    latest: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for d in decisions:
        key = (str(d.get("by")), tuple(d.get("scope") or ["all"]))
        latest.pop(key, None)
        latest[key] = d
    covering = [d for d in latest.values() if change_id in (d.get("scope") or []) or (d.get("scope") or []) == ["all"]]
    for wanted, status in ((("refuse",), "refused"), (("accept", "approve"), "accepted")):
        found = [d for d in covering if d.get("decision") in wanted]
        if found:
            d = found[-1]
            return {"status": status, "by": str(d.get("by") or ""), "at": str(d.get("at") or ""),
                    "comment": str(d.get("comment") or "")}
    return None


def promotion_view(layer: Layer, run_id: str, state: dict[str, Any], short: Callable[[str], str]) -> dict[str, Any] | None:
    """The promotion card of a promotion run: its changes with their criteria and proof, the decisions
    recorded so far, the approval status, the pack and what the run did."""
    p = state.get("promotion")
    if not isinstance(p, dict) or not p:
        return None
    proofs = p.get("proofs") if isinstance(p.get("proofs"), dict) else {}
    recorded = promotion_decisions(layer, run_id)
    changes = []
    for change in p.get("changes") or []:
        if not isinstance(change, dict):
            continue
        cid = str(change.get("id") or "")
        if not change.get("excluded"):   # decided since the run last looked: shown as recorded
            change = {**change, **(decided(cid, recorded) or {})}
        changes.append({
            "id": cid, "title": short(str(change.get("prompt") or "").strip().splitlines()[0] if change.get("prompt") else ""),
            "klass": change.get("klass", ""), "adw": change.get("adw", ""), "status": change.get("status", "open"),
            "excluded": change.get("excluded", ""), "by": change.get("by", ""), "at": change.get("at", ""),
            "comment": short(str(change.get("comment") or "")), "files": list(change.get("files") or []),
            "plan": short(str(change.get("plan") or "")), "patch_run": change.get("patch_run") or {},
            "criteria": [{"criterion": short(str(c.get("criterion", ""))), "uncovered": bool(c.get("uncovered")),
                          "proof": [{k: (short(v) if isinstance(v, str) else v) for k, v in item.items()}
                                    for item in c.get("proof") or [] if isinstance(item, dict)]}
                         for c in proofs.get(cid) or [] if isinstance(c, dict)]})
    is_open = any(o.get("id") == run_id for o in open_promotions(layer))
    keep = ("from", "to", "from_branch", "to_branch", "method", "approval", "approvers", "requires", "pinned",
            "promoted", "result", "tag", "version", "deploy", "back_merges", "notes", "waiting_runs", "stopped",
            "pr", "opened_at", "closed_at", "staged", "cycle", "approval_status", "dry_run")
    view = {k: p.get(k) for k in keep if k in p}
    view.update({"id": run_id, "changes": changes, "decisions": recorded, "open": is_open,
                 "pack": str(p.get("pack") or ""),
                 "records": {name: f"promotions/{run_id}/{name}" for name in ("sign-off.md", "approval.md", "promotion.json")
                             if (layer.path / "promotions" / run_id / name).is_file()},
                 "can_decide": is_open and (layer.path / PROMOTE_SCRIPT).is_file()})
    return view


def standin_via_layer(layer: Layer, argv: list[str], stdin: Any = None,
                      timeout: float = STANDIN_STATUS_TIMEOUT) -> tuple[int, dict[str, Any], str]:
    """Run the layer's own `adws/standin.py ARGV` (status, set, consider, apply, answer, note; the
    layer's runtime version): argv list, no shell, `stdin` (text, or a value sent as JSON) on its
    standard input, the dashboard's token not passed on. → (exit code, the JSON of its last output
    line, stderr tail)."""
    command = [sys.executable, str(layer.path / STANDIN_SCRIPT), *argv]
    env = {k: v for k, v in os.environ.items() if k != "AL_DASHBOARD_TOKEN"}
    data = b"" if stdin is None else (stdin if isinstance(stdin, str) else json.dumps(stdin)).encode("utf-8")
    try:
        done = subprocess.run(command, input=data, capture_output=True, cwd=str(layer.path), env=env,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return -1, {}, f"the layer's standin.py took longer than {int(timeout)} s"
    except OSError as exc:
        return -1, {}, f"cannot run the layer's standin.py: {exc}"
    tail = done.stderr.decode("utf-8", "replace")[-2000:]
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    return done.returncode, result if isinstance(result, dict) else {}, tail


def read_decisions(path: Path, limit: int | None = MAX_DECISIONS) -> list[dict[str, Any]]:
    """The entries of a decisions.jsonl or of the layer's log (standin.record), oldest first, each with
    the notes that name it folded in (its outcome, its stop); bad lines are skipped."""
    out: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for line in read_text(path, cap=4_000_000).splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or not item.get("id") or not isinstance(item.get("type"), str):
            continue
        if item["type"] == "note" and item.get("ref"):
            target = by_id.get(str(item["ref"]))
            if target is not None:
                target.update({key: item[key] for key in ("outcome", "stop", "reason") if key in item})
            continue
        out.append(item)
        by_id[str(item["id"])] = item
    return out[-limit:] if limit else out


def run_fingerprint(state: dict[str, Any]) -> str:
    """What identifies one stop of a run (mirror of standin.fingerprint): status, end, failed step."""
    failed = next((str(r.get("name")) for r in state.get("steps") or []
                   if isinstance(r, dict) and r.get("status") == "failed"), "")
    return f"{state.get('status')}:{state.get('ended') or ''}:{failed}"


_ENTRY_TEXT = ("id", "at", "type", "who", "via", "mode", "situation", "kind", "action", "answer", "summary", "why",
               "status", "reason", "outcome", "from_step", "instructions", "settle_as", "promote", "run", "job")


def entry_view(entry: dict[str, Any], short: Callable[[str], str], limit: int = 2000) -> dict[str, Any]:
    """One entry of the stand-in's record as the page gets it: known keys only, every string shortened and clipped."""
    view: dict[str, Any] = {key: clip(short(str(entry[key])), limit) for key in _ENTRY_TEXT
                            if isinstance(entry.get(key), (str, int, float)) and not isinstance(entry.get(key), bool)}
    view["auto"] = entry.get("auto") is True
    confidence = entry.get("confidence")
    view["confidence"] = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None
    view["read"] = [clip(short(str(x)), 200) for x in entry.get("read") or [] if isinstance(x, str)][:20] \
        if isinstance(entry.get("read"), list) else []
    cost = entry.get("cost")
    view["cost"] = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    return view


def start_via_layer(layer: Layer, request: dict[str, Any], timeout: float = START_TIMEOUT) -> tuple[int, dict[str, Any], str]:
    """Run the layer's own launcher (`adws/dispatch.py --start --json`, the layer's runtime version):
    argv list, prompt on stdin, no shell. It reserves the run and launches it detached in its own
    session, so the run outlives this dashboard. → (exit code, its JSON result, stderr tail)."""
    target = request["target"]
    argv = [sys.executable, str(layer.path / LAUNCHER), "--start", "--json",
            "--adw" if target["kind"] == "workflow" else "--command", target["name"],
            "--request-key", request["request_key"], "--issue", request["issue"]]
    argv += ["--class", request["class"]] if request.get("class") else []
    argv += ["--model-set", request["model_set"]] if request.get("model_set") else []
    for tier, model_id in sorted((request.get("models") or {}).items()):
        argv += ["--model", f"{tier}={model_id}"]
    argv += ["--confirm-duplicate"] if request.get("confirm_duplicate") else []
    argv += ["--confirm-ships"] if request.get("confirm_ships") else []
    argv += ["--replaces", request["replaces"]] if request.get("replaces") else []
    argv += ["--decider", request["decider"]] if request.get("decider") else []
    env = {k: v for k, v in os.environ.items() if k != "AL_DASHBOARD_TOKEN"}
    try:
        done = subprocess.run(argv, input=request["prompt"].encode("utf-8"), capture_output=True, cwd=str(layer.path),
                              env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return -1, {}, f"the layer's launcher took longer than {int(timeout)} s"
    except OSError as exc:
        return -1, {}, f"cannot run the layer's launcher: {exc}"
    tail = done.stderr.decode("utf-8", "replace")[-2000:]
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    return done.returncode, result if isinstance(result, dict) else {}, tail


# ============================================================================ landing and settlement
#
# A handed-over run's work may already be on its target branch: a person merged it, squash-merged it,
# or another run integrated it. Landing works that out read-only (git through safe_git, never a
# fetch), cached per (repository, the run's tip, the target's heads), so a new commit on the target
# recomputes. A settlement is a person's record that a finished run needs nothing more
# (agents/<id>/settlement.json). attention() puts every run of a layer in one bucket of BUCKETS with
# a one-line reason and next action; replacement and supersession are derived from the later run's
# records, so detection never writes.


class SettleError(ValueError):
    """A settlement that cannot be written: kind invalid (bad as / by / note / who), running (the run
    is not finished) or by (by names neither a run nor a commit of the application repository)."""

    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


def run_target(layer_ir: dict[str, Any], state: dict[str, Any] | None) -> str:
    """The branch a run's work goes to: its stage's branch when the layer has stages, else the
    codebase's default branch, else main."""
    delivery = (state or {}).get("delivery") if isinstance(state, dict) else None
    if isinstance(delivery, dict) and delivery.get("branch") and not delivery.get("refused"):
        return str(delivery["branch"])
    codebase = layer_ir.get("codebase") if isinstance(layer_ir.get("codebase"), dict) else {}
    return str(codebase.get("defaultBranch") or "main")


def app_repo(layer_dir: Path, layer_ir: dict[str, Any]) -> Path | None:
    """The application repository: AL_APP_ROOT when set, else codebase.root resolved against the layer
    (the rule of tools/al app_root_of and the runtime's ir.load)."""
    override = os.environ.get("AL_APP_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = (layer_ir.get("codebase") or {}).get("root") if isinstance(layer_ir.get("codebase"), dict) else None
    return (Path(layer_dir) / str(root)).resolve() if root else None


def prompt_mentions(prompt: str, cap: int = 100) -> list[str]:
    """Run ids and branch-like names (al/1a2b3c4d, feature/x) a prompt names: how a later run says
    which runs it integrates, without sending whole prompts to the page."""
    found = re.findall(r"(?<![\w/.-])(?:[0-9a-f]{8}|[\w.-]+/[\w./-]*[\w-])(?![\w/-])", prompt or "")
    return list(dict.fromkeys(found))[:cap]


def read_settlement(layer_dir: Path, run_id: str) -> dict[str, Any] | None:
    """agents/<id>/settlement.json, or None when there is none (or it is not a settlement)."""
    data = read_json(Path(layer_dir) / "agents" / run_id / SETTLEMENT_FILE)
    if not isinstance(data, dict) or data.get("as") not in SETTLE_AS:
        return None
    keep = ("as", "by", "note", "who", "at", "via", "previous", "pruned")
    return {k: data[k] for k in keep if k in data}


def _write_record(path: Path, data: dict[str, Any]) -> None:
    """Atomically: a temporary file beside it (mode 0o600), then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settlement.", suffix=".json")
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


def check_settlement(as_: Any, by: Any, note: Any, who: Any) -> tuple[str, str, str, str]:
    """The fields of a settlement, checked → (as, by, note, who); SettleError (invalid) otherwise."""
    if as_ not in SETTLE_AS:
        raise SettleError(f"as is one of {', '.join(SETTLE_AS)}")
    by = "" if by is None else by
    note = "" if note is None else note
    who = "" if who is None else who
    if not isinstance(by, str) or not isinstance(note, str) or not isinstance(who, str):
        raise SettleError("by, note and who are text")
    by = by.strip()
    if by and not COMMIT_RX.fullmatch(by):
        raise SettleError("by names a run (8 hexadecimal characters) or a commit (7 to 40)")
    if len(note) > SETTLE_NOTE_CHARS:
        raise SettleError(f"a note has at most {SETTLE_NOTE_CHARS} characters")
    note = " ".join(note.split())   # one paragraph
    if len(who) > SETTLE_WHO_CHARS or not who.isprintable():
        raise SettleError(f"who is at most {SETTLE_WHO_CHARS} printable characters on one line")
    return as_, by, note, who.strip()


def write_settlement(layer_dir: Path, run_id: str, *, as_: str, by: str = "", note: str = "", who: str = "",
                     via: str, status: str, repo: Path | None = None) -> dict[str, Any]:
    """Write agents/<id>/settlement.json and return the record (the runtime writes the same format when
    an integration lands a run: adw_modules/integrate.py write_settlement; keep the two alike). Writing
    again replaces the record and keeps the earlier one under "previous" (one level). Nothing else of the run is touched. Refused
    (SettleError) for a run that is running or unknown, and when `by` is neither a run of the layer
    nor a commit `git cat-file -e` finds in `repo`."""
    if not RUN_ID.match(run_id or "") or not (Path(layer_dir) / "agents" / run_id).is_dir():
        raise SettleError(f"no run {run_id}", "missing")
    if status in ("running", "unknown"):
        raise SettleError(f"run {run_id} is {status}: only a finished run can be settled", "running")
    as_, by, note, who = check_settlement(as_, by, note, who)
    if by:
        is_run = bool(RUN_ID.match(by)) and (Path(layer_dir) / "agents" / by / "state.json").is_file()
        if not is_run and not (repo is not None and safe_git(repo, "cat-file", "-e", f"{by}^{{commit}}",
                                                              timeout=LANDING_GIT_TIMEOUT) is not None):
            raise SettleError(f"by {by}: neither a run of this layer nor a commit of the application repository", "by")
    record: dict[str, Any] = {"as": as_, "by": by, "note": note, "who": who, "at": utc_stamp(), "via": via}
    previous = read_settlement(layer_dir, run_id)
    if previous:
        record["previous"] = {k: v for k, v in previous.items() if k not in ("previous", "pruned")}
        if previous.get("pruned"):
            record["pruned"] = previous["pruned"]
    _write_record(Path(layer_dir) / "agents" / run_id / SETTLEMENT_FILE, record)
    return record


def remove_settlement(layer_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Undo the last settlement of a run: the record kept under `previous` comes back when there is
    one, else the file goes. Returns what is left (None: the run is not settled any more). SettleError
    (missing) for a run without a settlement."""
    path = Path(layer_dir) / "agents" / run_id / SETTLEMENT_FILE
    if not RUN_ID.match(run_id or "") or read_settlement(layer_dir, run_id) is None:
        raise SettleError(f"run {run_id} has no settlement", "missing")
    current = read_settlement(layer_dir, run_id) or {}
    previous = current.get("previous")
    if isinstance(previous, dict) and previous.get("as") in SETTLE_AS:
        record = {k: v for k, v in previous.items() if k not in ("previous", "pruned")}
        if current.get("pruned"):
            record["pruned"] = current["pruned"]
        _write_record(path, record)
        return record
    path.unlink()
    return None


def add_pruned(layer_dir: Path, run_id: str, pruned: dict[str, Any], landing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Note in the settlement what `al runs … prune` removed; a run that landed by detection (no
    settlement yet) gets one with as = landed."""
    record = read_settlement(layer_dir, run_id)
    if record is None:
        how = (landing or {}).get("how")
        record = {"as": "landed", "by": str((landing or {}).get("commit") or ""),
                  "note": f"landed ({how}) on {(landing or {}).get('target', '')}" if how else "",
                  "who": "al runs prune", "at": utc_stamp(), "via": "cli"}
    record["pruned"] = pruned
    _write_record(Path(layer_dir) / "agents" / run_id / SETTLEMENT_FILE, record)
    return record


def normalise_reason(text: Any) -> str:
    """The first line of an error with what differs between runs of the same fault dropped: times,
    paths, hexadecimal ids and numbers."""
    line = first_line(text)
    line = re.sub(r"\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)?", " ", line)
    line = re.sub(r"\b\d\d:\d\d(?::\d\d(?:\.\d+)?)?\b", " ", line)
    line = re.sub(r"(?:~|\.{1,2})?(?:/[^\s'\"`,;:()\[\]]+)+/?", " ", line)
    line = re.sub(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,64}\b", " ", line)
    line = re.sub(r"\d+(?:\.\d+)?", " ", line)
    return " ".join(line.split())


def _git_common_dir(repo: Path) -> Path:
    directory = git_dir(repo)
    common = directory / "commondir"
    if common.is_file():
        text = read_text(common, cap=4096).strip()
        if text:
            target = Path(text)
            return target if target.is_absolute() else (directory / target).resolve()
    return directory


class Landing:
    """Did a handed-over run's work land on its target branch? Read-only: git rev-parse, merge-base,
    rev-list, log, diff, patch-id and ls-tree through safe_git with a timeout; refs are read from the
    repository's files (for-each-ref when it keeps them in a reftable). Never fetches. A result is
    cached per (repository, the run's tip, the target heads), so asking again costs no git call and a
    new commit on the target recomputes."""

    def __init__(self, git: Callable[..., str | None] | None = None) -> None:
        self._git = git
        self.cache: dict[tuple, dict[str, Any]] = {}
        self.contained: dict[tuple, bool] = {}
        self.lock = threading.Lock()

    def git(self, repo: Path, *args: str, input: bytes | None = None) -> str | None:  # noqa: A002
        run = self._git or safe_git   # looked up at call time, so a test can count the calls
        return run(repo, *args, timeout=LANDING_GIT_TIMEOUT, input=input) if input is not None else \
            run(repo, *args, timeout=LANDING_GIT_TIMEOUT)

    # ---------------------------------------------------------------- refs, without git
    def _refs(self, repo: Path, names: Callable[[str], bool], loose: list[str]) -> dict[str, str] | None:
        common = _git_common_dir(repo)
        if not common.is_dir():
            return None
        if (common / "reftable").exists():
            out = self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes")
            pairs = [line.split(" ", 1) for line in (out or "").splitlines() if " " in line]
            return {name: sha for name, sha in pairs if names(name)}
        found: dict[str, str] = {}
        for line in read_text(common / "packed-refs", cap=50_000_000).splitlines():
            if not line or line[0] in "#^":
                continue
            sha, _, name = line.partition(" ")
            if names(name.strip()):
                found[name.strip()] = sha.strip()
        for name in loose:
            text = read_text(common / name, cap=200).strip()
            if re.fullmatch(r"[0-9a-f]{40,64}", text):
                found[name] = text
        return found

    def heads(self, repo: Path, target: str) -> list[tuple[str, str]] | None:
        """[(ref, sha)] of refs/heads/<target> and refs/remotes/*/<target> that exist; None without a repository."""
        common = _git_common_dir(repo)
        remotes = []
        with contextlib.suppress(OSError):
            remotes = [f"refs/remotes/{e.name}/{target}" for e in os.scandir(common / "refs" / "remotes") if e.is_dir()]
        pattern = re.compile(rf"^refs/(?:heads|remotes/[^/]+)/{re.escape(target)}$")
        refs = self._refs(repo, lambda name: bool(pattern.match(name)), [f"refs/heads/{target}", *remotes])
        return None if refs is None else sorted(refs.items())

    def tip(self, repo: Path, branch: str, commit: str) -> str:
        """The run's tip: refs/heads/<branch> when it exists, else the shipped commit, else ""."""
        if branch:
            refs = self._refs(repo, lambda name: name == f"refs/heads/{branch}", [f"refs/heads/{branch}"])
            if refs and refs.get(f"refs/heads/{branch}"):
                return refs[f"refs/heads/{branch}"]
        return commit if COMMIT_RX.fullmatch(commit or "") else ""

    def _key(self, repo: Path | None, branch: str, commit: str, target: str) -> tuple | dict[str, Any]:
        if repo is None or not Path(repo).is_dir():
            return {"state": "unknown", "why": "no application repository", "target": target}
        heads = self.heads(repo, target)
        if heads is None:
            return {"state": "unknown", "why": "not a git repository", "target": target}
        if not heads:
            return {"state": "unknown", "why": f"no branch {target}", "target": target}
        tip = self.tip(repo, branch, commit)
        if not tip:
            return {"state": "unknown", "why": "branch and commit are gone", "target": target}
        return (str(repo), tip, tuple(heads), target)

    # ---------------------------------------------------------------- the checks
    def peek(self, repo: Path | None, branch: str, commit: str, target: str) -> dict[str, Any] | None:
        """The cached result (or one that needs no git), else None: a check would run git."""
        key = self._key(repo, branch, commit, target)
        if isinstance(key, dict):
            return key
        with self.lock:
            return self.cache.get(key)

    def check(self, repo: Path | None, branch: str, commit: str, target: str, pr: Any = None,
              pr_state: Callable[[Any], dict[str, Any]] | None = None) -> dict[str, Any]:
        """{"state": landed|partly|open|unknown, "how": ancestor|forge|patch-id|paths, "commit": short sha,
        "at": epoch, "target", "differs": [paths]}. The first check that holds wins: the tip is an
        ancestor of a target head; the forge says the pull request `pr` is merged (`pr_state`, when
        given); the patch-id of the branch's change equals a target commit's; every changed path is
        equal on the target (some only: partly)."""
        key = self._key(repo, branch, commit, target)
        if isinstance(key, dict):
            return key
        with self.lock:
            local = self.cache.get(key)
        if local is None:
            local = self._compute(Path(key[0]), key[1], list(key[2]), target)
            with self.lock:
                if len(self.cache) > 4096:
                    self.cache.clear()
                self.cache[key] = local
        if local.get("how") == "ancestor" or local.get("state") == "unknown" or not pr or pr_state is None:
            return local
        try:
            answer = pr_state(pr) or {}
        except Exception:  # noqa: BLE001 — the forge is optional: the local result stands
            answer = {}
        if answer.get("state") == "merged":
            sha = str(answer.get("commit") or "")
            return {"state": "landed", "how": "forge", "commit": sha[:7], "at": parse_time(answer.get("at")),
                    "target": target, "pr": pr}
        return local

    def _compute(self, repo: Path, tip: str, heads: list[tuple[str, str]], target: str) -> dict[str, Any]:
        g = lambda *args, **kw: self.git(repo, *args, **kw)   # noqa: E731
        if g("cat-file", "-e", f"{tip}^{{commit}}") is None:
            return {"state": "unknown", "why": "branch and commit are gone", "target": target}

        def landed(how: str, sha: str, **extra: Any) -> dict[str, Any]:
            when = (g("log", "-1", "--format=%ct", sha) or "").strip()
            return {"state": "landed", "how": how, "commit": sha[:7], "at": int(when) if when.isdigit() else None,
                    "target": target, **extra}

        for _ref, head in heads:   # 1. merged: the tip is in the target's history
            if g("merge-base", "--is-ancestor", tip, head) is not None:
                path = (g("rev-list", "--ancestry-path", "--reverse", f"{tip}..{head}") or "").split()
                return landed("ancestor", path[0] if path else tip)
        bases: list[tuple[str, str]] = []
        for _ref, head in heads:   # 2. (the forge, in check) 3. squash-merged: the same patch on the target
            base = (g("merge-base", tip, head) or "").strip()
            if not base:
                continue
            bases.append((head, base))
            diff = g("diff", "--no-color", "--no-ext-diff", base, tip)
            if not diff or not diff.strip():
                continue
            mine = (g("patch-id", "--stable", input=diff.encode("utf-8", "surrogateescape")) or "").split()
            if not mine:
                continue
            log = g("log", "-p", "--no-color", "--no-ext-diff", f"-{PATCH_ID_COMMITS}", f"{base}..{head}")
            theirs = g("patch-id", "--stable", input=(log or "").encode("utf-8", "surrogateescape")) if log else ""
            for line in (theirs or "").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] == mine[0]:
                    return landed("patch-id", parts[1])
        partly: dict[str, Any] | None = None
        for head, base in bases:   # 4. integrated: every path the branch changed is the same on the target
            names = [n for n in (g("diff", "--name-only", "--no-renames", "-z", base, tip) or "").split("\0") if n]
            if not names or len(names) > LANDING_PATHS:
                continue
            mine, theirs = self._blobs(g, tip, names), self._blobs(g, head, names)
            if mine is None or theirs is None:
                continue
            equal = [n for n in names if mine.get(n) == theirs.get(n)]
            if len(equal) == len(names):
                sha = (g("log", "-1", "--format=%H", head, "--", *names) or "").strip() or head
                return landed("paths", sha)
            if equal and (partly is None or len(equal) > partly["equal"]):
                differs = [n for n in names if n not in equal]
                partly = {"state": "partly", "differs": differs[:20], "differ": len(differs), "equal": len(equal),
                          "target": target}
        return partly or {"state": "open", "target": target}

    @staticmethod
    def _blobs(g: Callable[..., str | None], rev: str, names: list[str]) -> dict[str, str] | None:
        out = g("ls-tree", "-r", "-z", "--full-tree", rev, "--", *names)
        if out is None:
            return None
        blobs = {}
        for entry in out.split("\0"):
            meta, _, name = entry.partition("\t")
            if name:
                blobs[name] = meta.split()[-1] if meta.split() else ""
        return blobs

    def contains(self, repo: Path | None, older: dict[str, Any], newer: dict[str, Any]) -> bool:
        """Is the older run's tip in the newer run's tip (the newer run integrated it)? Cached per pair."""
        if repo is None or not Path(repo).is_dir():
            return False
        a = self.tip(repo, str(older.get("branch") or ""), str(older.get("commit") or ""))
        b = self.tip(repo, str(newer.get("branch") or ""), str(newer.get("commit") or ""))
        if not a or not b or a == b:
            return False
        key = (str(repo), a, b)
        with self.lock:
            if key in self.contained:
                return self.contained[key]
        found = self.git(repo, "merge-base", "--is-ancestor", a, b) is not None
        with self.lock:
            if len(self.contained) > 4096:
                self.contained.clear()
            self.contained[key] = found
        return found


def _day(epoch: Any) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%d") if isinstance(epoch, (int, float)) else ""


def settled_reason(record: dict[str, Any]) -> str:
    """"abandoned by Ada: the spec changed", "landed (c0ffee1) by al settle"."""
    text = str(record.get("as") or "settled") + (f" ({record['by']})" if record.get("by") else "")
    text += f" by {record['who']}" if record.get("who") else ""
    return text + (f": {record['note']}" if record.get("note") else "")


def landed_reason(landing: dict[str, Any]) -> str:
    when = _day(landing.get("at"))
    return f"landed in {landing.get('commit') or '?'} on {landing.get('target') or '?'}" + (f", {when}" if when else "")


def failure_groups(rows: list[dict[str, Any]], layer_key: str = "") -> dict[str, dict[str, Any]]:
    """Failures (summaries) of one layer that share failed_step and normalise_reason(error) and whose
    ends each fall within FAILURE_GROUP_SECONDS of the previous one's → {run key: group} for groups of
    at least two: {"id": LAYER/STEP/first-id, "count", "step", "reason", "runs": [ids]}."""
    by_cause: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_cause.setdefault((str(row.get("failed_step") or row.get("status") or ""),
                             normalise_reason(row.get("error"))), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for (step, reason), members in by_cause.items():
        members.sort(key=lambda r: (r.get("ended") or r.get("started") or 0, r["id"]))
        chains: list[list[dict[str, Any]]] = []
        last: float | None = None
        for row in members:
            end = row.get("ended") or row.get("started") or 0
            if chains and last is not None and end - last <= FAILURE_GROUP_SECONDS:
                chains[-1].append(row)
            else:
                chains.append([row])
            last = end
        for chain in chains:
            if len(chain) < 2:
                continue
            group = {"id": f"{layer_key or chain[0].get('layer', '')}/{step}/{chain[0]['id']}", "count": len(chain),
                     "step": step, "reason": first_line(chain[0].get("error")), "runs": [r["id"] for r in chain]}
            for row in chain:
                out[row["key"]] = group
    return out


def attention(summaries: list[dict[str, Any]], landings: dict[str, dict[str, Any]], layer_ir: dict[str, Any],
              contains: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None) -> dict[str, dict[str, Any]]:
    """For each run of one layer: {bucket, reason, next, landing, settled, replaced_by, superseded_by,
    group}. `landings` are Landing results by run key (handed-over runs); `contains(older, newer)` says
    whether the newer run's tip holds the older one's (optional). See the module's runs list."""
    ordered = sorted(summaries, key=lambda r: (r.get("started") or 0, r["id"]))
    by_id = {r["id"]: r for r in ordered}
    replaced_by: dict[str, str] = {}
    for row in ordered:   # the newest run that names it wins
        if row.get("replaces") and row["replaces"] != row["id"]:
            replaced_by[row["replaces"]] = row["id"]

    def landed_run(row: dict[str, Any]) -> bool:
        record = row.get("settlement") or {}
        if record.get("as") == "landed":
            return True
        if row.get("status") == "handed-over" and (landings.get(row["key"]) or {}).get("state") == "landed":
            return True
        return row.get("status") == "passed" and bool((row.get("outcome") or {}).get("shipped"))

    landed_runs = [r for r in ordered if landed_run(r)]

    def superseded(row: dict[str, Any]) -> str:
        names = {row["id"], *( [row["branch"]] if row.get("branch") else [])}
        for later in reversed(landed_runs):
            if later["id"] == row["id"] or (later.get("started") or 0) < (row.get("started") or 0):
                continue
            text = str(later.get("prompt") or "")
            if any(name in text for name in names) or names & set(later.get("mentions") or []):
                return later["id"]
        if contains is not None:
            for later in reversed(landed_runs):
                if later["id"] != row["id"] and (later.get("started") or 0) >= (row.get("started") or 0) \
                        and contains(row, later):
                    return later["id"]
        return ""

    out: dict[str, dict[str, Any]] = {}
    for row in ordered:
        status = str(row.get("status") or "unknown")
        record = row.get("settlement") if isinstance(row.get("settlement"), dict) else None
        landing = landings.get(row["key"])
        integ = row.get("integration") if isinstance(row.get("integration"), dict) else None
        item: dict[str, Any] = {"bucket": "done", "reason": "", "next": "", "landing": landing, "settled": record,
                                "replaced_by": replaced_by.get(row["id"], ""), "superseded_by": "", "group": None,
                                "landed_by": "", "integration": integ}
        # the run this one integrates (an integration run), when a person or another run settled it since
        original = by_id.get(str(row.get("integrates") or "")) if row.get("integrates") else None
        settled_original = (original or {}).get("settlement") if original else None
        target = str(row.get("target_branch") or run_target(layer_ir, None))
        if status in ("running", "unknown"):
            current = row.get("current") or {}
            item.update(bucket="running", reason=(f"at {current['step']}: {current.get('action', '')}"
                                                   if current.get("step") else str(current.get("action") or status)))
        elif status == "waiting":
            step = (row.get("waiting_step") or {}).get("name") or "a step"
            item.update(bucket="needs-you", reason=f"waiting at {step}", next=str(row.get("resume") or ""))
        elif status == "handed-over":
            state = (landing or {}).get("state")
            merge_or_settle = (f"merge {row.get('branch') or 'its branch'} into {target}, or "
                               + ("`al integrate`, or " if (row.get("integrate") or {}).get("offered") else "")
                               + "`al settle`")
            other = by_id.get(integ["run"]) if integ else None       # the integration run, when the layer has it
            other_status = str((other or {}).get("status") or "")
            if record and record.get("via") == "integrate" and record.get("as") == "landed":
                by = str(record.get("by") or "")
                item.update(bucket="landed", reason=f"landed by {by} (integration)", landed_by=by)
            elif record:
                item.update(bucket="settled", reason=settled_reason(record))
            elif settled_original:
                item.update(bucket="settled", reason=f"run {original['id']} is settled ({settled_original.get('as')})")
            elif state == "landed":
                item.update(bucket="landed", reason=landed_reason(landing or {}))
            elif (sb := superseded(row)):
                item.update(bucket="settled", reason=f"superseded by {sb}", superseded_by=sb)
            elif integ and (other_status in ("running", "unknown") or (integ["status"] == "queued" and other is None)):
                item.update(bucket="running", reason=f"being integrated by {integ['run']}")
            elif integ and integ["status"] == "waiting" and other_status in ("waiting", ""):
                item.update(bucket="needs-you", reason=f"integration {integ['run']} waits for approval",
                            next=f"approve integration {integ['run']}")
            elif integ and (integ["status"] == "failed" or other_status in ("failed", "died")):
                why = integ["why"] or (f"its run {other_status}" if other_status else "no reason recorded")
                item.update(bucket="needs-you", reason=f"integration {integ['run']} did not land: {why}",
                            next=merge_or_settle)
            else:
                pr = row.get("pr") or {}
                why = ", ".join((row.get("outcome") or {}).get("handover") or []) or "handed over"
                if state == "partly":
                    shown = ", ".join((landing or {}).get("differs", [])[:3])
                    more = "…" if (landing or {}).get("differ", 0) > 3 else ""
                    why = f"partly landed: {(landing or {}).get('differ', 0)} paths differ ({shown}{more})"
                elif state == "checking":
                    why += "; checking whether it landed"
                nxt = f"review PR #{pr['number']}" if pr.get("number") else merge_or_settle
                item.update(bucket="needs-you", reason=why, next=nxt)
        elif status in ("failed", "died"):
            new = item["replaced_by"]
            if record:
                item.update(bucket="settled", reason=settled_reason(record))
            elif new:
                item.update(bucket="settled",
                            reason=f"replaced by {new} ({(by_id.get(new) or {}).get('status', 'unknown')})")
            elif settled_original:
                item.update(bucket="settled", reason=f"run {original['id']} is settled ({settled_original.get('as')})")
            else:
                cause = first_line(row.get("error")) or ("its process died" if status == "died" else "failed")
                step = str(row.get("failed_step") or status)
                # a failed integration is not started again by itself: the run it integrates is
                again = f"integrate {row['integrates']} again, or `al settle`" if row.get("integrates") else "Start again"
                item.update(bucket="needs-you", reason=cause if cause.startswith(f"{step}:") else f"{step}: {cause}",
                            next=again)
        elif record:
            item.update(bucket="settled", reason=settled_reason(record))
        out[row["key"]] = item
    failures = [r for r in ordered if out[r["key"]]["bucket"] == "needs-you" and r.get("status") in ("failed", "died")]
    for key, group in failure_groups(failures).items():
        out[key]["group"] = group
    return out


LANDED_HOW = {"ancestor": "its tip is in the history of", "forge": "the forge says its pull request was merged into",
              "patch-id": "the same change (equal patch-id) is a commit of",
              "paths": "every path it changed has the same content on"}


def _landed_or_shipped(row: dict[str, Any], landing: Landing, repo: Path | None, layer_ir: dict[str, Any]) -> bool:
    if (row.get("settlement") or {}).get("as") == "landed":
        return True
    if row.get("status") == "passed" and bool((row.get("outcome") or {}).get("shipped")):
        return True
    if row.get("status") != "handed-over":
        return False
    found = landing.check(repo, str(row.get("branch") or ""), str(row.get("commit") or ""),
                          str(row.get("target_branch") or run_target(layer_ir, None)))
    return found.get("state") == "landed"


def settle_evidence(layer: "Layer", summaries: list[dict[str, Any]], landing: Landing, run_id: str, kind: str,
                    pr_state: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    """What a person would work out before settling run `run_id`, read only: {found, as, by, note,
    evidence: [lines], why}. kind `landed`: is its work on its target (the landed check, now, and
    the commit of its integration run)? kind `replaced`: which later run names it, starts it again
    or has the same source, and has that one landed? Nothing is written: the page pre-fills the
    Settle form, and the person's click writes through /api/settle."""
    rows = {r["id"]: r for r in summaries}
    row = rows.get(run_id)
    if row is None:
        return {"found": False, "as": "", "by": "", "note": "", "evidence": [], "why": f"no run {run_id}"}
    repo = app_repo(layer.path, layer.ir)
    branch, commit = str(row.get("branch") or ""), str(row.get("commit") or "")
    target = str(row.get("target_branch") or run_target(layer.ir, None))
    if kind == "landed":
        number = (row.get("pr") or {}).get("number")
        ask = (lambda n: pr_state(row, n)) if pr_state is not None and number else None
        result = landing.check(repo, branch, commit, target, pr=number, pr_state=ask)
        tip = landing.tip(repo, branch, commit) if repo is not None and Path(repo).is_dir() else ""
        if result.get("state") == "landed":
            how = str(result.get("how") or "")
            line = (f"tip {tip[:7] or '?'} of {branch or 'the run'}: {LANDED_HOW.get(how, 'it is on')} {target} "
                    f"(commit {result.get('commit') or '?'}" + (f", {_day(result.get('at'))}" if result.get("at") else "") + ")")
            return {"found": True, "as": "landed", "by": str(result.get("commit") or ""), "how": how,
                    "note": f"landed ({how}) on {target}", "evidence": [line], "why": ""}
        integ = row.get("integration") or {}
        if integ.get("commit") and repo is not None:
            heads = landing.heads(repo, target) or []
            if any(landing.git(repo, "merge-base", "--is-ancestor", integ["commit"], head) is not None for _r, head in heads):
                line = (f"integration run {integ['run']} brought it onto {target} in {integ['commit'][:7]}, which is "
                        f"in the history of {target}")
                return {"found": True, "as": "landed", "by": integ["run"], "how": "integration",
                        "note": f"landed (integration run {integ['run']} in {integ['commit'][:7]}) on {target}",
                        "evidence": [line], "why": ""}
        evidence = []
        state = result.get("state")
        if state == "unknown":
            evidence.append(f"cannot tell: {result.get('why') or 'unknown'}")
        else:
            evidence.append(f"tip {tip[:7] or '?'} is not on {target}")
            evidence.append(f"no commit of {target} has the same change (patch-id)")
            if state == "partly":
                names = list(result.get("differs") or [])
                count = int(result.get("differ") or len(names))
                evidence.append(f"{count} path{'' if count == 1 else 's'} differ{'s' if count == 1 else ''}: "
                                + ", ".join(names[:3]) + ("…" if count > 3 else ""))
            else:
                evidence.append(f"no path it changed has the same content on {target}")
        if integ.get("run"):
            evidence.append(f"integration run {integ['run']}: {integ.get('status') or '?'}"
                            + (f" ({integ['why']})" if integ.get("why") else ""))
        return {"found": False, "as": "landed", "by": "", "note": "", "evidence": evidence, "why": evidence[0]}
    names = [name for name in (run_id, branch) if name]
    source = str(row.get("source") or "")
    candidates = []
    for later in sorted(summaries, key=lambda r: (-(r.get("started") or 0), r["id"])):
        if later["id"] == run_id or (later.get("started") or 0) < (row.get("started") or 0):
            continue
        text = str(later.get("prompt") or "")
        mentions = set(later.get("mentions") or [])
        if later.get("replaces") == run_id:
            how = "starts it again"
        elif any(name in text for name in names) or mentions & set(names):
            named = next((name for name in names if name in text or name in mentions), run_id)
            how = f"names {'branch ' + named if named == branch else 'run ' + named} in its request"
        elif source and source not in ("cli", "manual") and later.get("source") == source:
            how = f"has the same source ({source})"
        else:
            continue
        candidates.append((later, how, _landed_or_shipped(later, landing, repo, layer.ir)))
    evidence = [f"run {c['id']} ({c.get('status')}, {'landed' if done else 'not landed'}) {how}"
                for c, how, done in candidates[:EVIDENCE_CANDIDATES]]
    chosen = next((c for c, _how, done in candidates if done), None)
    if chosen is not None:
        return {"found": True, "as": "superseded", "by": chosen["id"],
                "note": f"superseded by {chosen['id']}: {clip(str(chosen.get('title') or ''), 120)}".rstrip(": "),
                "evidence": evidence, "why": ""}
    if candidates:
        return {"found": False, "as": "superseded", "by": "", "note": "", "evidence": evidence,
                "why": f"{candidates[0][0]['id']} has not landed yet"}
    return {"found": False, "as": "superseded", "by": "", "note": "",
            "evidence": ["no later run names it, starts it again or has the same source"],
            "why": "no later run names it, starts it again or has the same source"}


def layer_attention(layer: "Layer", summaries: list[dict[str, Any]], landing: Landing, *, limit: int | None = None,
                    pr_state: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None
                    ) -> tuple[dict[str, dict[str, Any]], int]:
    """attention() for one layer with landed checks from `landing`: at most `limit` checks that are not
    cached yet (the others are "checking"); `pr_state(summary, number)` asks the forge. → (attention by
    run key, how many checks wait)."""
    repo = app_repo(layer.path, layer.ir)
    landings: dict[str, dict[str, Any]] = {}
    pending = checks = 0
    for row in sorted(summaries, key=lambda r: -(r.get("started") or 0)):
        if row.get("status") != "handed-over" or row.get("settlement"):
            continue
        args = (repo, str(row.get("branch") or ""), str(row.get("commit") or ""),
                str(row.get("target_branch") or run_target(layer.ir, None)))
        if landing.peek(*args) is None:
            if limit is not None and checks >= limit:
                landings[row["key"]] = {"state": "checking", "target": args[3]}
                pending += 1
                continue
            checks += 1
        number = (row.get("pr") or {}).get("number")
        ask = (lambda n, row=row: pr_state(row, n)) if pr_state is not None and number else None
        landings[row["key"]] = landing.check(*args, pr=number, pr_state=ask)
    return attention(summaries, landings, layer.ir, lambda older, newer: landing.contains(repo, older, newer)), pending


def pr_state_via_layer(layer: "Layer", number: int, timeout: float = 15.0) -> dict[str, Any]:
    """Ask the layer's own runtime whether pull request `number` is merged (`adws/dispatch.py
    --pr-state N --json`): argv list, no shell, the forge's credentials stay with the layer. →
    {"state": open|merged|closed|unknown, "commit", "at"}."""
    argv = [sys.executable, str(layer.path / LAUNCHER), "--pr-state", str(int(number)), "--json"]
    env = {k: v for k, v in os.environ.items() if k != "AL_DASHBOARD_TOKEN"}
    try:
        done = subprocess.run(argv, capture_output=True, cwd=str(layer.path), env=env, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"state": "unknown", "error": str(exc)[:200]}
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    return result if isinstance(result, dict) and result.get("state") else {"state": "unknown"}


# ============================================================================ runs

def step_status(data: dict[str, Any], name: str) -> str:
    """A step record's status; the step a died run was in is shown as died, not running. A review
    step an older run recorded as passed although its final verdict failed is shown as rejected."""
    status = str(data["records"].get(name, {}).get("status", "pending"))
    if status == "passed" and data["kinds"].get(name) == "review" and data.get("review_rejected"):
        return "rejected"
    return "died" if status == "running" and data["status"] == "died" else status


def review_rejected(state: dict[str, Any] | None, events: list[dict[str, Any]]) -> bool:
    """True when the run's agent or auto review ended with a failed verdict (state.review); without
    a recorded review, when the last `review.round` event says it failed."""
    review = (state or {}).get("review")
    if isinstance(review, dict) and review:
        verdict = review.get("verdict") if isinstance(review.get("verdict"), dict) else {}
        return bool(review.get("rounds")) and verdict.get("success") is False
    rounds = [e for e in events if e.get("type") == "review.round"]
    return bool(rounds) and rounds[-1].get("success") is False


def handover_reasons(layer_ir: dict[str, Any], state: dict[str, Any] | None, rejected: bool) -> list[str]:
    """Why a run that reached its end was handed to a person (the runtime's runner.handover_reasons):
    recorded in state.ship.handed_over, else worked out for older records."""
    state = state or {}
    ship = state.get("ship") if isinstance(state.get("ship"), dict) else {}
    if isinstance(ship.get("handed_over"), list):
        return [str(r) for r in ship["handed_over"]]
    reasons = ["review-rejected"] if rejected else []
    adw = next((a for a in layer_ir.get("adws") or [] if isinstance(a, dict) and a.get("name") == state.get("adw")), None)
    if adw is None and isinstance(state.get("definition"), dict):
        adw = state["definition"]
    option = _review(layer_ir, (adw or {}).get("review")) if (adw or {}).get("review") else {}
    gate = ship.get("gate") if isinstance(ship.get("gate"), dict) else {}
    if (option.get("kind") == "auto" and gate.get("ship") is False) or ship.get("fallback"):
        reasons += [str(r) for r in ship.get("reasons") or [] if str(r) not in reasons]
    return reasons


def agent_group(dirname: str, step_names: list[str], kinds: dict[str, str]) -> tuple[str, str]:
    """Which ADW step an agent directory belongs to → (step name, role)."""
    if dirname in kinds:
        return dirname, "main"
    match = re.match(r"^(.+)-resolve-(\d+)$", dirname)
    if match and match.group(1) in kinds:
        return match.group(1), "resolve"
    review = next((name for name in step_names if kinds.get(name) == "review"), None)
    if review and (re.match(r"^review-\d+(-\d+)?$", dirname) or re.match(r"^(patch|implement)-\d+$", dirname)):
        return review, "patch" if dirname.startswith(("patch", "implement")) else "reviewer"
    return "", "other"


_LOG_ATTEMPT = re.compile(r"^(?P<name>.+)\.(?P<n>\d+)\.log$")
_GATE_EXIT = re.compile(r"gate: (\S+) exit (-?\d+)")
_CONTAINER_STARTED = re.compile(r"container started: (\S+)")
_CONTAINER_REMOVED = re.compile(r"container removed: (\S+)")
_WAITING = re.compile(r"^\[slots\] (.*): waiting for a free (runs|agents|checks) slot")
_RUN_START = re.compile(r"^\[adw ([0-9a-fA-F]{8})\] (\S+) start: class=(\S+) dry_run=(\S+)")


class RunFiles:
    """Everything on disk for one run: agents/<id>/ (+ artefacts/<id>/manifest.json)."""

    def __init__(self, layer: Layer, run_id: str) -> None:
        self.layer = layer
        self.id = run_id
        self.dir = layer.path / "agents" / run_id

    def agent_dirs(self) -> list[Path]:
        out = []
        try:
            for entry in os.scandir(self.dir):
                if entry.is_dir(follow_symlinks=False) and entry.name not in {"checks", "objects", "fanout"}:
                    path = Path(entry.path)
                    if (path / "prompt.md").exists() or (path / "raw.jsonl").exists():
                        out.append(path)
        except OSError:
            pass
        return sorted(out, key=lambda p: (mtime(p / "prompt.md") or mtime(p / "raw.jsonl") or 0, p.name))

    def check_logs(self) -> list[Path]:
        try:
            return sorted((Path(e.path) for e in os.scandir(self.dir / "checks") if e.name.endswith(".log")),
                          key=lambda p: p.name)
        except OSError:
            return []

    def signature(self, deep: bool) -> tuple:
        items: list[Any] = [stat_sig(self.dir / "state.json"), stat_sig(self.dir / "run.log"), mtime(self.dir),
                            stat_sig(self.dir / EVENTS_FILE), stat_sig(self.dir / SETTLEMENT_FILE),
                            stat_sig(self.dir / INTEGRATION_FILE), stat_sig(self.dir / DECISIONS_FILE)]
        if deep:
            items.append(stat_sig(self.dir / "PR.md"))
            items.append(stat_sig(self.layer.path / "artefacts" / self.id / "manifest.json"))
            items.extend((p.name, stat_sig(p)) for p in self.check_logs())
            for directory in self.agent_dirs():
                items.extend((directory.name, stat_sig(directory / name)) for name in ("raw.jsonl", "output.txt"))
        return tuple(items)


class AggCache:
    """Transcript aggregates per raw.jsonl, fed incrementally (a growing file is only read once)."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[Tail, Transcript]] = {}
        self.lock = threading.Lock()

    def get(self, path: Path, short: Shortener | None = None) -> TranscriptAgg:
        key = str(path)
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                entry = (Tail(path, settle=TRANSCRIPT_SETTLE), Transcript(short))
                self.entries[key] = entry
            tail, transcript = entry
            reset, lines = tail.read(max_bytes=64 * 1024 * 1024)
            if reset:
                transcript.reset()
            if lines:
                transcript.feed(lines)
            return transcript.agg

    def forget(self, prefix: str) -> None:
        with self.lock:
            for key in [k for k in self.entries if k.startswith(prefix)]:
                del self.entries[key]


def load_state(run: RunFiles) -> tuple[dict[str, Any] | None, str]:
    path = run.dir / "state.json"
    if not path.exists():
        return None, "no state.json"
    data = read_json(path)
    if not isinstance(data, dict):
        return None, "state.json unreadable"
    return data, ""


def runlog_facts(path: Path) -> dict[str, Any]:
    """What run.log says that state.json does not: container, slot waits, gate exits, start line."""
    facts: dict[str, Any] = {"container": "", "container_removed": False, "waiting": "", "gate": {}, "start": None,
                             "last_line": ""}
    text = read_text(path, cap=2_000_000)
    for line in text.splitlines():
        if not line.strip():
            continue
        line = _STAMP.sub("", line, count=1)   # newer runtimes prefix each line with its UTC time
        facts["last_line"] = line
        if (match := _CONTAINER_STARTED.search(line)):
            facts["container"], facts["container_removed"] = match.group(1), False
        elif (match := _CONTAINER_REMOVED.search(line)):
            facts["container_removed"] = True
        if (match := _WAITING.match(line)):
            facts["waiting"] = match.group(2)
            facts.setdefault("waited", []).append(match.group(2))
        elif line.startswith("[adw "):
            facts["waiting"] = ""
        if (match := _GATE_EXIT.search(line)):
            facts["gate"][match.group(1)] = int(match.group(2))
        if facts["start"] is None and (match := _RUN_START.match(line)):
            facts["start"] = {"adw": match.group(2), "klass": match.group(3), "dry_run": match.group(4) == "True"}
    return facts


@dataclass
class Activity:
    key: str            # agent dir name or check log file name
    kind: str           # agent | check | gate
    step: str
    role: str
    label: str
    start: float | None
    end: float | None
    status: str
    extra: dict[str, Any] = field(default_factory=dict)


def tier_fields(record: dict[str, Any]) -> dict[str, str]:
    """The model tier of an agent call and why it was chosen (runs from before tiers: model_set only)."""
    return {"tier": str(record.get("tier") or record.get("model_set") or ""),
            "tier_from": str(record.get("tier_from") or ""), "model_from": str(record.get("model_from") or "")}


KEPT_FILES_SHOWN = 500   # the kept files listed on a run page; the rest is a count


def kept_view(state: dict[str, Any], short: Any) -> dict[str, Any] | None:
    """What the runtime kept of a run whose workflow has no ship step (kit/runtime keep.py): the
    branch, the files with their artefact copies, the diff; None for every other run."""
    kept = state.get("kept") if isinstance(state.get("kept"), dict) else {}
    if not kept:
        return None
    copies: dict[str, str] = {}
    prefix = f"artefacts/{state.get('adw_id', '')}/kept-files/"
    for item in state.get("artefacts") or []:
        if isinstance(item, dict) and item.get("id") == "kept-files" and isinstance(item.get("path"), str):
            path = item["path"]
            copies[path.split("/kept-files/", 1)[1] if "/kept-files/" in path else path.removeprefix(prefix)] = path
    names = [str(f) for f in kept.get("files") or [] if isinstance(f, (str, int))]
    files = [{"path": short(name), "artefact": copies.get(name, "")} for name in names[:KEPT_FILES_SHOWN]]
    return {"changed": kept.get("changed"), "error": short(str(kept.get("error") or "")),
            "branch": str(kept.get("branch") or ""), "commit": str(kept.get("commit") or "")[:12],
            "count": int(kept.get("count") or len(names)), "files": files,
            "more": max(0, len(names) - KEPT_FILES_SHOWN), "diff": str(kept.get("diff") or ""),
            "too_large": [{"path": short(str(i.get("path", ""))), "bytes": i.get("bytes")}
                          for i in kept.get("too_large") or [] if isinstance(i, dict)],
            "skipped": [short(str(i)) for i in kept.get("skipped") or []],
            "left_uncommitted": [short(str(i)) for i in kept.get("left_uncommitted") or []]}


def budget_view(state: dict[str, Any]) -> dict[str, Any] | None:
    """Limits, what the run spent and whether a limit stopped it; None for a run without limits or cost."""
    record = state.get("budget") if isinstance(state.get("budget"), dict) else {}
    limits = record.get("limits") if isinstance(record.get("limits"), dict) else {}
    cost = state.get("cost") if isinstance(state.get("cost"), dict) else {}
    stopped = record.get("stopped") if isinstance(record.get("stopped"), dict) else None
    if not any(limits.get(k) is not None for k in ("run", "step", "day")) and not stopped and not cost:
        return None
    return {"run": limits.get("run"), "step": limits.get("step"), "day": limits.get("day"),
            "run_from": limits.get("run_from") or "", "spent": cost.get("total"),
            "unreported": [str(x) for x in cost.get("unreported") or []],
            "stopped": {k: stopped.get(k) for k in ("limit", "spent", "of", "step", "reason")} if stopped else None}


def discipline_view(record: dict[str, Any]) -> dict[str, Any] | None:
    """The step page's step-discipline line from a step record's `discipline` (written by the
    runtime around every run step that may not commit): {ok, text}; None for a record without it
    (another kind of step, a commit step, or a run from before the check)."""
    facts = record.get("discipline")
    if not isinstance(facts, dict):
        return None
    if facts.get("ok"):
        return {"ok": True, "text": "Stayed in its step: HEAD unchanged, no refs changed, nothing outside the worktree"}
    said = [str(t) for t in facts.get("texts") or []] or [str(v) for v in facts.get("violations") or []]
    text = "Left its step: " + "; ".join(said or ["(no details recorded)"])
    restored = [str(r) for r in facts.get("restored") or []]
    not_restored = [str(r) for r in facts.get("not_restored") or []]
    if restored:
        text += ". Restored: " + "; ".join(restored)
    if not_restored:
        text += ". Not restored: " + "; ".join(not_restored)
    return {"ok": False, "text": text}


def usage_view(state: dict[str, Any]) -> dict[str, Any] | None:
    """What a run recorded about subscription usage: its billing mode, the limit that stopped it and
    how often it waited for a reset; None for a run that recorded nothing."""
    record = state.get("usage")
    if not isinstance(record, dict) or not record:
        return None
    stopped = record.get("stopped") if isinstance(record.get("stopped"), dict) else None
    waits = record.get("waits") if isinstance(record.get("waits"), list) else []
    return {"billing": str(record.get("billing") or ""),
            "stopped": {"window": str(stopped.get("window") or ""), "cause": str(stopped.get("cause") or ""),
                        "status": str(stopped.get("status") or ""), "used": _share(stopped.get("used")),
                        "percent": stopped.get("percent") if isinstance(stopped.get("percent"), int) else None,
                        "resets_at": parse_time(stopped.get("resets_at")), "step": str(stopped.get("step") or ""),
                        "reason": str(stopped.get("reason") or "")} if stopped else None,
            "waits": len(waits)}


def local_day(stamp: float | None) -> str:
    return datetime.fromtimestamp(stamp).date().isoformat() if stamp else ""


def _skip_view(event: dict[str, Any]) -> dict[str, Any]:
    """What a check.end says about skipped tests and where the check ran (#:in); events written
    before skips were counted have none of it: skipped None, no reasons, no env."""
    skipped = event.get("skipped")
    reasons = []
    raw = event.get("skip_reasons")
    for pair in raw if isinstance(raw, list) else []:
        if (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str)
                and isinstance(pair[1], int) and not isinstance(pair[1], bool)):
            reasons.append([pair[0][:200], pair[1]])
    return {"skipped": skipped if isinstance(skipped, int) and not isinstance(skipped, bool) else None,
            "skip_reasons": reasons[:5], "env": str(event.get("in") or "")}


def _decided_fields(item: dict[str, Any]) -> dict[str, Any]:
    """outcome, why, compared paths, tree and source step of an attempt that is a decision; {} for a real run."""
    if item.get("outcome") not in CHECK_DECISIONS:
        return {}
    return {key: item.get(key) for key in ("outcome", "why", "paths", "paths_total", "tree", "from_step")}


def _state_skipped(state: dict[str, Any] | None, name: str) -> int | None:
    """Skipped tests of check `name` from state.json (its latest attempt), None when not counted."""
    record = ((state or {}).get("checks") or {}).get(name)
    skips = record.get("skips") if isinstance(record, dict) else None
    value = skips.get("skipped") if isinstance(skips, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


CHECK_DECISIONS = ("not-affected", "reused")   # a check that was not run, and why (check.decision)
CHECK_OUTCOMES = ("passed", "failed", *CHECK_DECISIONS)
PROOF_PATHS = 50


def _decision_view(source: dict[str, Any]) -> dict[str, Any]:
    """What a `check.decision` event (or a ledger entry) says about a check that was not run:
    validated, every string capped, at most PROOF_PATHS compared paths."""
    paths = [str(p)[:300] for p in (source.get("paths") if isinstance(source.get("paths"), list) else [])
             if isinstance(p, str)][:PROOF_PATHS]
    total = source.get("paths_total")
    origin = source.get("from") if isinstance(source.get("from"), dict) else {}
    return {"outcome": str(source.get("outcome") or "") if source.get("outcome") in CHECK_OUTCOMES else "",
            "why": str(source.get("why") or "")[:500], "paths": paths,
            "paths_total": total if isinstance(total, int) and not isinstance(total, bool) else len(paths),
            "tree": str(source.get("tree") or "")[:64],
            "from_step": str(source.get("from_step") or origin.get("step") or "")[:200]}


def _state_outcome(state: dict[str, Any] | None, name: str) -> tuple[str, str]:
    """(outcome, why) of check `name` from state.json: `not-affected` or `reused` when its step did
    not run the command, else what the record says ("" for a run from before outcomes were recorded)."""
    record = ((state or {}).get("checks") or {}).get(name)
    if not isinstance(record, dict) or record.get("outcome") not in CHECK_OUTCOMES:
        return "", ""
    return str(record["outcome"]), str(record.get("why") or "")[:500]


def proof_view(state: dict[str, Any] | None, short: Any) -> list[dict[str, Any]]:
    """What the checks proved, from the run's ledger (state.json `check_results`), as the runtime's
    `affected.rows` builds it: checks in order of first appearance; per check its latest entry of a
    check step or a review round, then its latest gate entry. [] for a run without a ledger."""
    ledger = (state or {}).get("check_results")
    entries = [e for e in ledger if isinstance(e, dict) and isinstance(e.get("check"), str) and e["check"]
               and e.get("outcome") in CHECK_OUTCOMES] if isinstance(ledger, list) else []
    order: list[str] = []
    for entry in entries:
        if entry["check"] not in order:
            order.append(entry["check"])
    rows = []
    for check in order:
        mine = [e for e in entries if e["check"] == check]
        step_entry = next((e for e in reversed(mine) if e.get("phase") in ("check", "recheck")), None)
        gate_entry = next((e for e in reversed(mine) if e.get("phase") == "gate"), None)
        for entry in (step_entry, gate_entry):
            if entry is None:
                continue
            step = str(entry.get("step") or "")[:200]
            where = "gate" if entry.get("phase") == "gate" else (f"{step} (review round)" if entry.get("phase") == "recheck" else step)
            view = _decision_view(entry)
            origin = entry.get("from") if isinstance(entry.get("from"), dict) else None
            exit_code = entry.get("exit")
            rows.append({"check": check[:200], "where": where, "outcome": view["outcome"], "why": short(view["why"]),
                         "tree": view["tree"],
                         "exit": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
                         "log": short(str(entry.get("log") or ""))[:500], "paths": [short(p) for p in view["paths"]],
                         "paths_total": view["paths_total"],
                         "from": {key: short(str(origin.get(key) or ""))[:500] for key in ("step", "phase", "log")}
                         if origin else None})
    return rows


def event_spans(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Exact spans from a run's events (see docs/DESIGN.md, "Run events"): agent calls, check
    attempts (with re-checks), gate / deploy commands, slot waits, the container, the check that is
    running now, what was recorded about subscription usage (limit.status / limit.stop / limit.wait)
    and the run's setup: sandbox image builds and the preflight (env.build.* / env.preflight). Later
    events of the same agent directory or log file win (a resumed run)."""
    agents: dict[str, dict[str, Any]] = {}
    attempts: dict[str, list[dict[str, Any]]] = {}
    gates: dict[str, dict[str, Any]] = {}
    waits: list[dict[str, Any]] = []
    open_waits: dict[tuple[str, str], dict[str, Any]] = {}
    container: dict[str, Any] | None = None
    limits: list[dict[str, Any]] = []
    inner: dict[str, list[dict[str, Any]]] = {}
    setup: list[dict[str, Any]] = []
    current = ""
    for event in events:
        kind, at = event["type"], event["_t"]
        if kind == "step.start":
            current = str(event.get("step") or "")
        elif kind == "step.end":
            if event.get("step") == current:
                current = ""
        elif kind == "agent.start":
            agents[str(event.get("step") or "")] = {
                "start": at, "end": None, "status": "running", "owner": str(event.get("owner") or ""),
                "agent": str(event.get("agent") or ""), "agent_kind": str(event.get("agent_kind") or ""),
                "model": str(event.get("model") or ""), **tier_fields(event)}
        elif kind == "agent.end":
            span = agents.setdefault(str(event.get("step") or ""), {
                "start": at, "owner": str(event.get("owner") or ""), "agent": str(event.get("agent") or ""),
                "agent_kind": str(event.get("agent_kind") or ""), "model": str(event.get("model") or ""),
                **tier_fields(event)})
            span.update(end=at, status="failed" if event.get("status") == "failed" else "passed",
                        cost=event.get("cost") if isinstance(event.get("cost"), (int, float)) else None,
                        turns=event.get("turns") if isinstance(event.get("turns"), int) else None,
                        error=str(event.get("error") or ""), billing=str(event.get("billing") or ""))
        elif kind in {"limit.status", "limit.stop", "limit.wait"}:
            seconds = event.get("seconds")
            limits.append({"type": kind, "at": at,
                           "step": str(event.get("owner") or current or event.get("step") or ""),
                           "call": str(event.get("step") or ""), "window": str(event.get("window") or ""),
                           "status": str(event.get("status") or ""), "used": _share(event.get("used")),
                           "resets_at": parse_time(event.get("resets_at")), "cause": str(event.get("cause") or ""),
                           "reason": str(event.get("reason") or ""),
                           "seconds": float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                           else None})
        elif kind in {"check.start", "check.end"}:
            file = os.path.basename(str(event.get("log") or ""))
            if not file:
                continue
            phase = str(event.get("phase") or "check")
            step, owner = str(event.get("step") or ""), str(event.get("owner") or "")
            exit_code = event.get("exit") if isinstance(event.get("exit"), int) else None
            if phase in {"gate", "deploy"}:
                if kind == "check.start":
                    gates[file] = {"file": file, "phase": phase, "cmd": str(event.get("cmd") or ""),
                                   "owner": owner, "start": at, "end": None, "exit": None}
                elif file in gates:
                    gates[file].update(end=at, exit=exit_code)
                continue
            items = attempts.setdefault(step, [])
            if kind == "check.start":
                items[:] = [item for item in items if item["file"] != file]
                items.append({"file": file, "attempt": int(event.get("attempt") or len(items) + 1), "start": at,
                              "end": None, "exit": None, "phase": owner if phase == "recheck" else "",
                              "owner": owner or step, "running": True})
            else:
                item = next((i for i in items if i["file"] == file), None)
                if item is None:
                    item = {"file": file, "attempt": int(event.get("attempt") or len(items) + 1), "start": at,
                            "phase": owner if phase == "recheck" else "", "owner": owner or step}
                    items.append(item)
                item.update(end=at, exit=exit_code, running=False, **_skip_view(event))
        elif kind == "check.decision":
            # a check that was not run: not affected by the change, or its pass on the same tree reused
            phase = str(event.get("phase") or "check")
            step, owner = str(event.get("step") or ""), str(event.get("owner") or "")
            view = _decision_view(event)
            if view["outcome"] not in CHECK_DECISIONS:
                continue
            file = os.path.basename(str(event.get("log") or ""))
            if phase == "gate":
                key = file or f"gate-{event.get('cmd') or ''}.log"
                gates[key] = {"file": file, "phase": phase, "cmd": str(event.get("cmd") or ""), "owner": owner,
                              "start": at, "end": at, "exit": None, **view}
                continue
            items = attempts.setdefault(step, [])
            items.append({"file": file, "attempt": len(items) + 1, "start": at, "end": at, "exit": None,
                          "phase": owner if phase == "recheck" else "", "owner": owner or step, "running": False,
                          **view})
        elif kind == "slot.wait":
            key = (str(event.get("pool") or ""), str(event.get("who") or ""))
            open_waits[key] = {"pool": key[0], "who": key[1], "start": at, "end": None, "step": current}
        elif kind == "slot.acquire":
            key = (str(event.get("pool") or ""), str(event.get("who") or ""))
            if key in open_waits:
                wait = open_waits.pop(key)
                wait["end"] = at
                waits.append(wait)
        elif kind == "script.event":
            entry = inner_entry(event)
            if entry is not None:
                inner.setdefault(entry["step"], []).append(entry)
        elif kind == "env.build.start":
            setup.append({"kind": "build", "start": at, "end": None, "status": "running",
                          "image": str(event.get("image") or ""), "recipe": str(event.get("recipe") or ""),
                          "seconds": None, "size": None})
        elif kind == "env.build.end":
            build = next((s for s in reversed(setup) if s["kind"] == "build" and s["end"] is None
                          and s["image"] == str(event.get("image") or "")), None)
            if build is None:
                seconds = event.get("seconds") if isinstance(event.get("seconds"), (int, float)) else 0.0
                build = {"kind": "build", "start": at - seconds, "image": str(event.get("image") or ""),
                         "recipe": str(event.get("recipe") or "")}
                setup.append(build)
            build.update(end=at, status="failed" if event.get("status") == "failed" else "passed",
                         seconds=event.get("seconds") if isinstance(event.get("seconds"), (int, float)) else None,
                         size=event.get("size") if isinstance(event.get("size"), (int, float)) else None)
        elif kind == "env.preflight":
            seconds = event.get("seconds") if isinstance(event.get("seconds"), (int, float)) else 0.0
            checks = [c for c in event.get("checks") or [] if isinstance(c, dict)]
            setup.append({"kind": "preflight", "start": at - seconds, "end": at,
                          "status": "failed" if event.get("status") == "failed" else "passed",
                          "missed": [str(c.get("name") or "") for c in checks if c.get("status") == "miss"]})
        elif kind == "env.container.start":
            container = {"name": str(event.get("name") or ""), "removed": False}
        elif kind == "env.container.stop":
            if container and container["name"] == event.get("name"):
                container["removed"] = True
    waits.extend(open_waits.values())
    for items in attempts.values():
        items.sort(key=lambda item: (item["start"] or 0, item["attempt"]))
    live = None
    for step, items in attempts.items():
        for item in items:
            if item.get("running") and (live is None or item["start"] > live["start"]):
                live = {"check": step, "attempt": item["attempt"], "start": item["start"], "file": item["file"]}
    return {"agents": agents, "attempts": attempts, "gates": list(gates.values()), "waits": waits,
            "container": container, "live_check": live, "limits": limits, "inner": inner, "setup": setup}


_TOKEN_KEYS = (("in", "input_tokens", "in"), ("out", "output_tokens", "out"),
               ("cache_read", "cache_read_input_tokens", None), ("cache_write", "cache_creation_input_tokens", None))


def foreign_usage(obj: Any) -> dict[str, Any] | None:
    """Tokens and cost a hand-built suite's journal line reports: `usage.{input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens}` or `tokens.{in, out}`, and `cost_usd` / `cost`
    when it is a number. None when the line reports neither."""
    if not isinstance(obj, dict):
        return None

    def count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    usage, tokens = obj.get("usage"), obj.get("tokens")
    found = False
    out: dict[str, Any] = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    for key, usage_key, tokens_key in _TOKEN_KEYS:
        if isinstance(usage, dict) and usage_key in usage:
            out[key], found = count(usage.get(usage_key)), True
        elif isinstance(tokens, dict) and tokens_key and tokens_key in tokens:
            out[key], found = count(tokens.get(tokens_key)), True
    cost = next((obj[k] for k in ("cost_usd", "cost") if isinstance(obj.get(k), (int, float))
                 and not isinstance(obj.get(k), bool)), None)
    if not found and cost is None:
        return None
    out["cost"] = cost
    return out


def _foreign_field(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return clip(value, 300)
    try:
        return clip(json.dumps(value, ensure_ascii=False), 300)
    except (TypeError, ValueError):
        return clip(str(value), 300)


def inner_entry(event: dict[str, Any]) -> dict[str, Any] | None:
    """One `script.event` (a line of a hand-built suite's own journal, mirrored by the runtime) → an
    inner step of its script step, or None when it names no step or carries no object."""
    step, foreign = str(event.get("step") or ""), event.get("foreign")
    if not step or not isinstance(foreign, dict):
        return None
    line = event.get("line") if isinstance(event.get("line"), int) else 0
    kind = str(foreign.get("kind") or foreign.get("type") or "")
    subject_key = "subject" if foreign.get("subject") else ("step" if foreign.get("step") else "")
    used = {"time", "ts", "kind", "type", "prev", "hash"} | ({subject_key} if subject_key else set())
    at = parse_time(foreign.get("time")) or parse_time(foreign.get("ts")) or event["_t"]
    return {"step": step, "line": line, "at": at, "kind": clip(kind, 120),
            "subject": clip(str(foreign.get(subject_key) or ""), 200) if subject_key else "",
            "fields": {clip(str(k), 80): _foreign_field(v) for k, v in list(foreign.items())[:40] if k not in used},
            "usage": foreign_usage(foreign)}


def inner_usage(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The totals of the inner steps' usage, or None when none reported any."""
    reported = [e["usage"] for e in entries if e.get("usage")]
    if not reported:
        return None
    total: dict[str, Any] = {key: sum(u[key] for u in reported) for key, _, _ in _TOKEN_KEYS}
    costs = [u["cost"] for u in reported if u.get("cost") is not None]
    total["cost"] = round(sum(costs), 6) if costs else None
    return total


def inner_bars(step: str, entries: list[dict[str, Any]], cutoff: float | None) -> list[dict[str, Any]]:
    """Timeline bars (level 1) of a script step's inner steps: a `*.start` … `*.end` pair with the same
    subject is one bar, any other line a point."""
    bars: list[dict[str, Any]] = []
    open_: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        kind, subject = entry["kind"], entry["subject"]
        label = " ".join(x for x in (kind, subject) if x) or f"line {entry['line']}"
        if kind.endswith(".start"):
            bar = {"id": f"inner:{step}:{entry['line']}", "step": step, "level": 1, "kind": "inner", "role": "inner",
                   "label": " ".join(x for x in (kind[:-len(".start")], subject) if x), "start": entry["at"],
                   "end": None, "status": "running", "cost": None, "ref": str(entry["line"])}
            open_[(kind[:-len(".start")], subject)] = bar
            bars.append(bar)
            continue
        if kind.endswith(".end") and (kind[:-len(".end")], subject) in open_:
            bar = open_.pop((kind[:-len(".end")], subject))
            failed = str(entry["fields"].get("status") or "") == "failed"
            bar.update(end=entry["at"], status="failed" if failed else "done",
                       cost=(entry["usage"] or {}).get("cost"))
            continue
        bars.append({"id": f"inner:{step}:{entry['line']}", "step": step, "level": 1, "kind": "inner", "role": "inner",
                     "label": label, "start": entry["at"], "end": entry["at"], "status": "done",
                     "cost": (entry["usage"] or {}).get("cost"), "ref": str(entry["line"])})
    for bar in open_.values():
        if cutoff is not None:
            bar.update(end=max(cutoff, bar["start"] or cutoff), status="done")
    return bars


class RunModel:
    """Builds the summary and the detail of one run from its files."""

    def __init__(self, layer: Layer, run_id: str, aggs: AggCache, now: Callable[[], float] = time.time,
                 can_see_pids: bool = True) -> None:
        self.layer, self.id, self.aggs, self.now = layer, run_id, aggs, now
        self.can_see_pids = can_see_pids
        self.files = RunFiles(layer, run_id)

    def _short(self, state: dict[str, Any] | None) -> Shortener:
        return self.layer.shortener(state)

    def collect(self) -> dict[str, Any]:
        state, problem = load_state(self.files)
        facts = runlog_facts(self.files.dir / "run.log")
        events = load_events(self.files.dir / EVENTS_FILE)
        spans = event_spans(events) if any(e["type"] == "run.start" for e in events) else None
        adw_name = (state or {}).get("adw") or (facts["start"] or {}).get("adw") or "?"
        adw = self.layer.adw(adw_name)
        if adw is None:   # a single-command run records its synthetic ADW; anything else shows its records only
            recorded = (state or {}).get("definition")
            adw = recorded if isinstance(recorded, dict) and recorded.get("name") == adw_name else None
        adw = adw or {"name": adw_name, "steps": []}
        ir_steps = [s for s in adw.get("steps") or [] if isinstance(s, dict)]
        step_names = [str(s.get("name")) for s in ir_steps]
        kinds = {str(s.get("name")): str(s.get("kind")) for s in ir_steps}
        records = {str(r.get("name")): r for r in (state or {}).get("steps") or [] if isinstance(r, dict)}
        # the last agent call made from each agent directory (state.json `agent_calls`)
        calls = {str(c.get("step")): c for c in (state or {}).get("agent_calls") or [] if isinstance(c, dict)}
        setups = 0
        for name, record in records.items():
            kinds.setdefault(name, str(record.get("kind", "run")))
            if name not in step_names:
                if record.get("kind") == "setup":  # the prepare step runs before every ADW step
                    step_names.insert(setups, name)
                    setups += 1
                else:
                    step_names.append(name)
        status = str((state or {}).get("status") or "unknown")
        now = self.now()
        alive = liveness(state or {}, now, self.can_see_pids)
        if alive and alive["died"]:
            status = "died"
        rejected = review_rejected(state, events)
        handover = handover_reasons(self.layer.ir, state, rejected) if status in {"passed", "handed-over"} else []
        if status == "passed" and handover:   # an older record: the runtime called this run passed
            status = "handed-over"
        last_seen = alive["last_seen"] if alive else None
        short = self._short(state)
        running = status == "running"
        unfinished = "died" if status == "died" else "failed"
        activities: list[Activity] = []
        agents: list[dict[str, Any]] = []
        for directory in self.files.agent_dirs():
            agg = self.aggs.get(directory / "raw.jsonl", short)
            step, role = agent_group(directory.name, step_names, kinds)
            record = records.get(step, {})
            span = (spans or {}).get("agents", {}).get(directory.name)
            if span:
                start, end = span["start"], span["end"]
                if end is not None:
                    astatus = span["status"]
                elif running:
                    astatus = "running"
                else:   # it never finished: the run failed or died around it
                    end, astatus = (mtime(directory / "raw.jsonl") or start), unfinished
            else:
                start = mtime(directory / "prompt.md") or mtime(directory / "raw.jsonl")
                out = mtime(directory / "output.txt")
                raw_m = mtime(directory / "raw.jsonl")
                if out is not None:
                    end, astatus = out, "passed"
                elif running and (record.get("status") == "running" or role != "main"):
                    end, astatus = None, "running"
                else:
                    end, astatus = (raw_m or start), ("failed" if record.get("status") == "failed" or agg.is_error else
                                                      (unfinished if status == "died" else "done"))
            if agg.final and agg.is_error:
                astatus = "failed"
            ident = span or ({"agent": record.get("agent"), "agent_kind": record.get("agent_kind"), "model": record.get("model")}
                             if role == "main" and record.get("agent_kind") else {})
            info = {"dir": directory.name, "step": step, "role": role, "start": start, "end": end,
                    "status": astatus, **agg.as_dict(), "agent_id": str(ident.get("agent") or ""),
                    "agent_kind": str(ident.get("agent_kind") or "")}
            info["model"] = str(ident.get("model") or "") or agg.model
            call = calls.get(directory.name) or {}
            info.update(tier_fields(span or call))
            if not info["tier"] and call:
                info.update(tier_fields(call))
            if info["cost"] is None and span and span.get("cost") is not None:
                info["cost"] = span["cost"]
            # how the call was billed: its transcript says so (also for runs from before this was recorded)
            info["billing"] = agg.billing or str((span or {}).get("billing") or "") or str(call.get("billing") or "")
            agents.append(info)
            activities.append(Activity(directory.name, "agent", step, role, directory.name, start, end, astatus,
                                       {"cost": info["cost"]}))
        if spans is None:
            # a running step's agent: the newest open one
            open_agents = [a for a in agents if a["status"] == "running"]
            for extra in open_agents[:-1]:
                extra["status"] = "passed" if extra["final"] else "done"
                extra["end"] = extra["end"] or mtime(self.files.dir / extra["dir"] / "raw.jsonl")
        run_start = parse_time((state or {}).get("started")) or min(
            (a.start for a in activities if a.start), default=None) or mtime(self.files.dir / "run.log")
        run_end = parse_time((state or {}).get("ended")) if not running else None
        if status == "died":
            run_end = last_seen
        current_record = next((r for r in reversed(list(records.values())) if r.get("status") == "running"), None)
        if spans is not None:
            attempts, live_check = self._event_checks(spans, activities, running, unfinished, run_end or now)
        else:
            attempts, live_check = self._inferred_checks(state, facts, records, kinds, step_names, activities, agents,
                                                         running, run_start, current_record, now)
        return {"state": state, "problem": problem, "facts": facts, "adw": adw, "steps": step_names, "kinds": kinds,
                "records": records, "status": status, "running": running, "agents": agents,
                "activities": activities, "attempts": attempts, "run_start": run_start, "run_end": run_end,
                "current": current_record if running else None, "live_check": live_check, "now": now,
                "spans": spans, "last_seen": last_seen, "review_rejected": rejected, "handover": handover}

    def _event_checks(self, spans: dict[str, Any], activities: list[Activity], running: bool, unfinished: str,
                      cutoff: float) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
        def outcome(item: dict[str, Any]) -> str:
            if item.get("outcome") in CHECK_DECISIONS:
                return str(item["outcome"])   # not run: never shown as passed
            if item.get("exit") is not None:
                return "passed" if item["exit"] == 0 else "failed"
            return "running" if running else unfinished

        attempts: dict[str, list[dict[str, Any]]] = {}
        for name, items in spans["attempts"].items():
            out = []
            for item in items:
                state_ = outcome(item)
                end = item.get("end") if item.get("end") is not None else (None if state_ == "running" else cutoff)
                entry = {"file": item["file"], "attempt": item["attempt"], "start": item["start"], "end": end,
                         "exit": item.get("exit"), "phase": item.get("phase", ""), "running": state_ == "running",
                         "size": (stat_sig(self.files.dir / "checks" / item["file"]) or (0, 0))[1],
                         "skipped": item.get("skipped"), "skip_reasons": list(item.get("skip_reasons") or []),
                         "env": str(item.get("env") or "")}
                decided = _decided_fields(item)
                entry.update(decided)
                out.append(entry)
                recheck = bool(entry["phase"])
                # a decision's log may be the log of the run it reuses: its bar has a key of its own
                key = f"{name}.decision.{item['attempt']}" if decided else item["file"]
                what = {"not-affected": "not affected", "reused": "reused"}.get(decided.get("outcome"), f"#{item['attempt']}")
                activities.append(Activity(key, "check", entry["phase"] or name, "recheck" if recheck else "attempt",
                                           f"{'re-check ' if recheck else ''}{name} {what}", item["start"], end,
                                           state_, {"check": name, "attempt": item["attempt"], "exit": item.get("exit"),
                                                    "phase": entry["phase"], "skipped": entry["skipped"],
                                                    "skip_reasons": entry["skip_reasons"], "env": entry["env"], **decided}))
            attempts[name] = out
        for gate in spans["gates"]:
            state_ = outcome(gate)
            end = gate["end"] if gate["end"] is not None else (None if state_ == "running" else cutoff)
            label = f"gate {gate['cmd']}" if gate["phase"] == "gate" else f"deploy ({gate['cmd']})"
            if state_ in CHECK_DECISIONS:
                label += " not affected" if state_ == "not-affected" else " reused"
            activities.append(Activity(gate["file"] or f"gate-{gate['cmd']}.log", "gate", gate["owner"], "gate", label,
                                       gate["start"], end, state_,
                                       {"check": gate["cmd"], "exit": gate["exit"], **_decided_fields(gate)}))
        live = spans["live_check"] if running else None
        return attempts, live

    def _inferred_checks(self, state: dict[str, Any] | None, facts: dict[str, Any], records: dict[str, Any],
                         kinds: dict[str, str], step_names: list[str], activities: list[Activity],
                         agents: list[dict[str, Any]], running: bool, run_start: float | None,
                         current_record: dict[str, Any] | None, now: float
                         ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
        """Older runs (no events.jsonl): check attempts from log file names and mtimes. A log is
        written when its command ends, so an attempt starts where whatever ran before it ended."""
        checks_state = (state or {}).get("checks") or {}
        logs = self.files.check_logs()
        attempts: dict[str, list[dict[str, Any]]] = {}
        for log in logs:
            end = mtime(log)
            if log.name.startswith("gate-"):
                name = log.name[len("gate-"):-len(".log")]
                activities.append(Activity(log.name, "gate", "", "gate", f"gate {name}", None, end,
                                           "passed" if facts["gate"].get(name, 0) == 0 else "failed",
                                           {"check": name, "exit": facts["gate"].get(name)}))
                continue
            match = _LOG_ATTEMPT.match(log.name)
            if not match:
                continue
            attempts.setdefault(match.group("name"), []).append(
                {"file": log.name, "attempt": int(match.group("n")), "end": end, "size": (stat_sig(log) or (0, 0))[1]})
        for name, items in attempts.items():
            items.sort(key=lambda item: item["attempt"])
            for item in items:
                activities.append(Activity(item["file"], "check", name, "attempt", f"{name} #{item['attempt']}",
                                           None, item["end"], "done", {"check": name, "attempt": item["attempt"]}))
        windows = []
        for name in step_names:
            record = records.get(name)
            if not record:
                continue
            windows.append((name, parse_time(record.get("started")), parse_time(record.get("ended")),
                            str(record.get("status", ""))))

        def window_of(at: float | None) -> str:
            if at is None:
                return ""
            for name, started, ended, _status in windows:
                if started and started - 0.001 <= at <= (ended or now) + 0.5:
                    return name
            return ""

        activities.sort(key=lambda a: (a.end if a.end is not None else now + 1e6))
        previous_end = run_start
        for activity in activities:
            if activity.kind in {"check", "gate"}:
                owner = window_of(activity.end)
                if activity.kind == "gate":
                    activity.step = owner or activity.step
                elif owner and owner != activity.step and kinds.get(owner) == "review":
                    activity.role, activity.label = "recheck", f"re-check {activity.label}"
                    activity.extra["phase"] = owner
                    activity.step = owner
                else:
                    owner = activity.step
                owner_start = next((w[1] for w in windows if w[0] == (owner or activity.step)), None)
                candidates = [x for x in (previous_end, owner_start) if x is not None and (activity.end is None or x <= activity.end)]
                activity.start = max(candidates) if candidates else activity.end
            if activity.end is not None:
                previous_end = max(previous_end or activity.end, activity.end)
        by_key = {a.key: a for a in activities if a.kind == "check"}
        for name, items in attempts.items():
            record = records.get(name, {})
            recorded = checks_state.get(name) or {}
            last_file = os.path.basename(str(recorded.get("log", "")))
            for item in items:
                found = by_key.get(item["file"])
                item["start"], item["phase"] = (found.start, found.extra.get("phase", "")) if found else (None, "")
            for item in items:
                same_phase = [i for i in items if i["phase"] == item["phase"]]
                if item["file"] == last_file and recorded.get("exit") is not None:
                    exit_code: Any = recorded.get("exit")
                elif item is not same_phase[-1]:
                    exit_code = "fail"   # a resolve agent followed it, so it did not pass
                elif not item["phase"] and record.get("status") in {"passed", "failed"}:
                    exit_code = 0 if record.get("status") == "passed" else "fail"
                else:
                    exit_code = None
                item["exit"] = exit_code
                if found := by_key.get(item["file"]):
                    found.status = "passed" if exit_code == 0 else ("failed" if exit_code is not None else "done")
                    found.extra["exit"] = exit_code
        # a running check with no new log yet
        live_check = None
        if running and current_record and current_record.get("kind") == "check":
            name = str(current_record.get("name"))
            done = attempts.get(name, [])
            open_resolve = any(a["status"] == "running" and a["step"] == name for a in agents)
            if not open_resolve:
                started = max([x for x in [parse_time(current_record.get("started"))] +
                               [i["end"] for i in done] +
                               [a["end"] for a in agents if a["step"] == name and a["end"]] if x], default=now)
                live_check = {"check": name, "attempt": (done[-1]["attempt"] + 1) if done else 1, "start": started}
                activities.append(Activity(f"{name}.{live_check['attempt']}.log", "check", name, "attempt",
                                           f"{name} #{live_check['attempt']}", started, None, "running",
                                           {"check": name, "attempt": live_check["attempt"]}))
        return attempts, live_check

    # ------------------------------------------------------------------ summary
    def summary(self, extras: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.collect()
        return self._summary(data, extras or {})

    def _summary(self, data: dict[str, Any], extras: dict[str, Any]) -> dict[str, Any]:
        state = data["state"] or {}
        facts = data["facts"]
        agents = data["agents"]
        costs = [a["cost"] for a in agents if a["cost"] is not None]
        tokens = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        turns = 0
        for agent in agents:
            for key in tokens:
                tokens[key] += agent["tokens"].get(key, 0)
            turns += agent["turns"] or 0
        status = data["status"]
        start, end = data["run_start"], data["run_end"]
        duration = (end - start) if start and end else None   # a running run's elapsed time is the page's job
        if status != "running" and not end:
            last = max([mtime(self.files.dir / "run.log") or 0, mtime(self.files.dir / "state.json") or 0])
            duration = (last - start) if start and last else None
        current = None
        record = data["current"]
        if data["running"] and record:
            name, kind = str(record.get("name")), str(record.get("kind"))
            action = ""
            open_agent = next((a for a in reversed(agents) if a["status"] == "running"), None)
            if open_agent:
                action = open_agent["action"] or "starting"
                if open_agent["dir"] != name:
                    action = f"{open_agent['dir']}: {action}"
            elif data["live_check"]:
                live = data["live_check"]
                action = f"running check {live['check']}" + (f" (attempt {live['attempt']})" if live["attempt"] > 1 else "")
            elif kind == "ship":
                action = "gate and ship"
            elif kind == "fan-out":
                action = "candidates running"
            current = {"step": name, "kind": kind, "action": action or "working",
                       "since": parse_time(record.get("started"))}
        elif data["running"]:
            building = next((s for s in (data["spans"] or {}).get("setup") or []
                             if s["kind"] == "build" and s["end"] is None), None)
            current = {"step": "", "kind": "", "action": "building the sandbox image" if building else "setting up",
                       "since": building["start"] if building else start}
        spans = data["spans"]
        waiting = facts["waiting"] if data["running"] else ""
        if spans is not None:
            waiting = next((w["pool"] for w in spans["waits"] if w["end"] is None), "") if data["running"] else ""
        ship = state.get("ship") or {}
        review = state.get("review") or {}
        verdict = review.get("verdict") or {}
        failed = next((r for r in state.get("steps") or [] if isinstance(r, dict) and r.get("status") == "failed"), None)
        fanout_children = [c.get("id") for record in (state.get("fanout") or {}).values() if isinstance(record, dict)
                           for c in record.get("candidates") or [] if isinstance(c, dict)]
        container = None
        if spans is not None and spans["container"]:
            container = {"name": spans["container"]["name"], "state": "removed" if spans["container"]["removed"] else
                         ("running" if data["running"] else "unknown"), "source": "events"}
        elif facts["container"]:
            container = {"name": facts["container"], "state": "removed" if facts["container_removed"] else
                         ("running" if data["running"] else "unknown"), "source": "run.log"}
        prompt = str(state.get("prompt") or "")
        cost_record = state.get("cost") if isinstance(state.get("cost"), dict) else {}
        parent = state.get("fanout_parent") if isinstance(state.get("fanout_parent"), dict) else {}
        pr = ship.get("pr") if isinstance(ship.get("pr"), dict) else {}
        integration = state.get("integration") if isinstance(state.get("integration"), dict) else {}
        definition = state.get("definition") if isinstance(state.get("definition"), dict) else {}
        planned = definition.get("integration") if isinstance(definition.get("integration"), dict) else {}
        integrates = str(integration.get("run") or planned.get("run") or "")
        summary = {
            **self._standin(state, agents),
            "key": f"{self.layer.key}/{self.id}", "layer": self.layer.key, "id": self.id, "adw": data["adw"].get("name"),
            "status": status, "problem": data["problem"],
            "klass": state.get("klass") or (facts["start"] or {}).get("klass") or "",
            "dry_run": bool(state.get("dry_run") if state else (facts["start"] or {}).get("dry_run")),
            "source": state.get("source", ""), "title": clip(prompt.strip().splitlines()[0] if prompt.strip() else "", 160),
            "prompt": clip(prompt, 600), "started": start, "ended": end, "duration": duration,
            "cost": round(sum(costs), 6) if costs else None,
            "cost_partial": any(a["status"] == "running" and a["kind"] == "claude" for a in agents),
            "tokens": tokens, "turns": turns,
            "steps": [{"name": name, "kind": data["kinds"].get(name, ""),
                       "status": step_status(data, name)} for name in data["steps"]],
            "current": current, "waiting": waiting, "container": container, "ports": state.get("ports") or [],
            "outcome": {"ship": ship.get("action", ""), "shipped": ship.get("shipped"), "target": ship.get("target", ""),
                        "url": str(ship.get("url") or (ship.get("pr").get("url", "") if isinstance(ship.get("pr"), dict) else "")),
                        "review": verdict.get("success") if verdict else None, "rounds": len(review.get("rounds") or []),
                        "stopped_at": ship.get("stopped_at", ""), "handover": data["handover"],
                        "blockers": sum(1 for i in verdict.get("issues") or []
                                        if isinstance(i, dict) and i.get("severity") == "blocker")},
            "error": clip(str(failed.get("detail", "")), 300) if failed else "",
            "failed_step": failed.get("name") if failed else "",
            "fanout_children": [c for c in fanout_children if c],
            **({"fanout_parent": str(parent["run"]),
                "fanout": {"run": str(parent["run"]), "step": str(parent.get("step") or ""), "index": parent.get("index")}}
               if parent.get("run") else {}),
            "artefacts": len(state.get("artefacts") or []),
            "documents": len(run_documents(self.layer, self.id, data["state"])),
            "agents": len(agents),
            "budget": budget_view(state),
            "billing": str(cost_record.get("billing") or "") or collapse_billing(a["billing"] for a in agents),
            "usage": usage_view(state),
            "timing": "events" if spans is not None else "files",
            "last_seen": data["last_seen"],
            "mtime": max([mtime(self.files.dir / "state.json") or 0, mtime(self.files.dir / "run.log") or 0]),
            **self._promotion_badge(state, status),
            **self._waiting(data),
            # what landing and settlement need (see attention): the run's branch and shipped commit, its
            # pull request, the branch its work goes to, a person's settlement, and the run it starts again
            "replaces": str(state.get("replaces") or "") if RUN_ID.match(str(state.get("replaces") or "")) else "",
            "branch": str(state.get("branch") or ""), "commit": str(ship.get("commit") or ""),
            "pr": {"number": pr.get("number"), "url": str(pr.get("url") or "")}
            if isinstance(pr.get("number"), int) and pr.get("number") > 0 else None,
            "target_branch": run_target(self.layer.ir, state),
            "settlement": read_settlement(self.layer.path, self.id),
            "ended_at": str(state.get("ended") or ""),
            "mentions": prompt_mentions(prompt),
            # the kit workflow `integrate`: the integration run of this (handed-over) run, and the run an
            # integration run brings onto its target
            "integration": read_integration(self.layer.path, self.id),
            "integrates": integrates if RUN_ID.match(integrates) else "",
            **extras,
        }
        summary["integrate"] = integrate_offer(self.layer, summary)
        return summary

    def _standin(self, state: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
        """What the stand-in did with the run: the suggestion waiting for an answer, its last entry, the
        stop it looked at last and what its calls cost. None / "" for a run it never saw."""
        short = self._short(state)
        recorded = state.get("decision") if isinstance(state.get("decision"), dict) else {}
        suggestion = None
        if recorded.get("status") in ("suggested", "explained") and recorded.get("action"):
            action = str(recorded.get("action"))
            suggestion = {key: clip(short(str(recorded.get(key) or "")), 600) for key in
                          ("kind", "action", "summary", "instructions", "from_step", "settle_as", "promote", "at",
                           "situation", "held")}
            suggestion["kind"] = suggestion["kind"] or {"resume-with": "resume"}.get(action, action)
            confidence = recorded.get("confidence")
            suggestion["confidence"] = (float(confidence) if isinstance(confidence, (int, float))
                                        and not isinstance(confidence, bool) else None)
            suggestion["read"] = [clip(short(str(x)), 200) for x in recorded.get("read") or [] if isinstance(x, str)][:20] \
                if isinstance(recorded.get("read"), list) else []
        mine = [e for e in read_decisions(self.files.dir / DECISIONS_FILE) if e.get("who") == "stand-in"
                and e.get("type") == "decision"]
        last = mine[-1] if mine else None
        costs = [a["cost"] for a in agents if a.get("dir") == "decide" and isinstance(a.get("cost"), (int, float))]
        return {"decider": str(state.get("decider") or "") if state.get("decider") in DECIDER_MODES else "",
                "suggestion": suggestion, "decision_status": clip(str(recorded.get("status") or ""), 40),
                "standin_last": ({**{key: clip(short(str(last.get(key) or "")), 600) for key in
                                     ("at", "action", "status", "outcome", "reason")}, "auto": last.get("auto") is True}
                                 if last else None),
                "standin_stop": str(last.get("stop") or "") if last else "",
                "fingerprint": run_fingerprint(state) if state else "",
                "cost_standin": round(sum(costs), 6) if costs else None}

    def _promotion_badge(self, state: dict[str, Any], status: str) -> dict[str, Any]:
        """A promotion run's badge for the run card and the runs list."""
        p = state.get("promotion")
        if not isinstance(p, dict) or not p:
            return {}
        changes = [c for c in p.get("changes") or [] if isinstance(c, dict)]
        return {"promotion": {"from": p.get("from", ""), "to": p.get("to", ""), "to_branch": p.get("to_branch", ""),
                              "from_branch": p.get("from_branch", ""), "changes": len(changes),
                              "refused": sum(1 for c in changes if c.get("status") == "refused"),
                              "state": "waiting for approval" if status == "waiting" else status,
                              "why": str((p.get("approval_status") or {}).get("why") or ""),
                              "tag": p.get("tag", ""), "dry_run": bool(p.get("dry_run"))}}

    def _waiting(self, data: dict[str, Any]) -> dict[str, Any]:
        """A run that ended `waiting` (a script step exited with a declared waiting code, or a promotion
        waiting for people): the step it waits at and the command that resumes it."""
        if data["status"] != "waiting":
            return {}
        record = next((r for r in reversed(list(data["records"].values())) if r.get("status") == "waiting"), None)
        promotion = (data["adw"] or {}).get("promotion") if isinstance(data["adw"], dict) else None
        integration = (data["adw"] or {}).get("integration") if isinstance(data["adw"], dict) else None
        if isinstance(promotion, dict) and promotion.get("from"):
            resume = f"uv run adws/promote.py run {promotion['from']} {promotion.get('to', '')} --resume --adw-id {self.id}"
        elif isinstance(integration, dict) and integration.get("run"):   # a person approves; that resumes it
            resume = f"uv run adws/integrate.py approve {integration['run']} --who NAME"
        else:
            resume = f"uv run adws/adw_{data['adw'].get('name')}.py --resume --adw-id {self.id}"
        return {"waiting_step": {k: v for k, v in record.items() if isinstance(v, (str, int, float, bool)) or v is None}
                if record else None, "resume": resume}

    # ------------------------------------------------------------------ detail
    def _declared_check_cmds(self, adw: dict[str, Any]) -> dict[str, str]:
        """check step name → the command line the layer declares for it (before any attempt ran)."""
        commands = {str(c.get("name")): str(c.get("run", "")) for c in (self.layer.ir.get("codebase") or {}).get("commands") or []
                    if isinstance(c, dict)}
        return {str(s.get("name")): commands.get(str(s.get("cmd")), "") for s in adw.get("steps") or []
                if isinstance(s, dict) and s.get("kind") == "check"}
    def detail(self, extras: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.collect()
        summary = self._summary(data, extras or {})
        state = data["state"] or {}
        short = self._short(state)
        graph = build_graph(data["adw"], self.layer.ir)
        records = data["records"]
        agents = data["agents"]
        now = data["now"]
        # live node status
        for node in graph["nodes"]:
            step = node["step"]
            record = records.get(step, {})
            group = [a for a in agents if a["step"] == step]
            node["status"] = step_status(data, step)
            started, ended = parse_time(record.get("started")), parse_time(record.get("ended"))
            if node["status"] == "died":
                ended = data["run_end"]
            node["duration"] = ((ended or now) - started) if started and (ended or node["status"] == "running") else None
            used = next((a for a in group if a["agent_kind"] and a["role"] == "main"), None) or \
                next((a for a in group if a["agent_kind"]), None)
            if used:
                node["agent"] = {"id": used["agent_id"], "kind": used["agent_kind"], "model": used["model"]}
            elif record.get("agent_kind"):
                node["agent"] = {"id": str(record.get("agent") or ""), "kind": str(record["agent_kind"]),
                                 "model": str(record.get("model") or "")}
            costs = [a["cost"] for a in group if a["cost"] is not None]
            node["cost"] = round(sum(costs), 4) if costs else None
            node["attempts"] = (len([a for a in data["attempts"].get(step, []) if not a.get("outcome")])
                                if node["type"] == "check" else None)
            node["skipped"] = _state_skipped(state, step) if node["type"] == "check" else None   # = steps_detail
            node["outcome"] = _state_outcome(state, step)[0] if node["type"] == "check" else ""
            node["resolves"] = sum(1 for a in group if a["role"] == "resolve")
            node["agents"] = [a["dir"] for a in group]
            node["detail"] = short(str(record.get("detail", "")))[:300]
            if node["type"] == "candidate":
                record = (state.get("fanout") or {}).get(step) or {}
                candidates = record.get("candidates") or []
                candidate = next((c for c in candidates if isinstance(c, dict) and c.get("index") == node["lane"]), None)
                if candidate:
                    node["status"] = {"passed": "passed", "failed": "failed"}.get(str(candidate.get("status")), node["status"])
                    if candidate.get("id"):
                        node["run"] = candidate["id"]
                        node["label"] = f"#{node['lane']} {candidate['id']}"
                    if candidate.get("id") in record.get("picked", []):
                        node["picked"] = True
                    node["duration"] = candidate.get("seconds")
                    node["detail"] = short(str(candidate.get("reason", "")))[:300]
                else:
                    node["status"] = str(records.get(step, {}).get("status", "pending"))
                    if node["status"] == "passed":
                        node["status"] = "done"
        # timeline
        bars = []
        order = {name: index for index, name in enumerate(data["steps"])}
        for name in data["steps"]:
            record = records.get(name)
            if not record:
                continue
            started, ended = parse_time(record.get("started")), parse_time(record.get("ended"))
            if started is None:
                continue
            died = step_status(data, name) == "died"
            bars.append({"id": f"step:{name}", "step": name, "level": 0, "kind": data["kinds"].get(name, ""),
                         "label": name, "start": started,
                         "end": data["run_end"] if died else (ended if record.get("status") != "running" else None),
                         "status": step_status(data, name),
                         "cost": next((n["cost"] for n in graph["nodes"] if n["id"] == name), None)})
        for activity in data["activities"]:
            if activity.start is None and activity.end is None:
                continue
            bars.append({"id": f"{activity.kind}:{activity.key}", "step": activity.step, "level": 1,
                         "kind": activity.kind, "role": activity.role, "label": activity.label,
                         "start": activity.start if activity.start is not None else activity.end,
                         "end": activity.end, "status": activity.status, "cost": activity.extra.get("cost"),
                         "ref": activity.key, "check": activity.extra.get("check"), "exit": activity.extra.get("exit"),
                         **({key: (short(activity.extra[key]) if isinstance(activity.extra[key], str) else activity.extra[key])
                             for key in ("outcome", "why", "paths", "paths_total", "tree", "from_step")}
                            if activity.extra.get("outcome") else {})})
        # the inner steps of a script step that follows its own journal (script.event)
        inner = (data["spans"] or {}).get("inner") or {}
        for name, entries in inner.items():
            bars.extend(inner_bars(name, entries, None if data["running"] else data["run_end"]))
        # waiting for a `runs` slot: from the run's start until its first step started (run.log says it waited)
        wait_time = 0.0
        if data["spans"] is not None:
            # exact: every slot.wait → slot.acquire; a run's own slot at the top, agent/check slots under their step
            for index, wait in enumerate(data["spans"]["waits"]):
                runs_pool = wait["pool"] == "runs"
                end = wait["end"] if wait["end"] is not None else (None if data["running"] else data["run_end"])
                first_runs = runs_pool and not any(b["id"] == "wait:runs" for b in bars)
                bars.append({"id": "wait:runs" if first_runs else f"wait:{wait['pool']}:{index}",
                             "step": "" if runs_pool else wait["step"], "level": 0 if runs_pool else 1, "kind": "wait",
                             "label": f"waiting for a {wait['pool']} slot", "start": wait["start"], "end": end,
                             "status": "waiting" if end is None else "done"})
                wait_time += (end or now) - wait["start"]
            # what the agent CLI said about subscription usage, a limit stop, a wait for a reset
            for index, entry in enumerate(data["spans"]["limits"]):
                name = window_label(entry["window"])
                at, end, status = entry["at"], entry["at"], "done"
                if entry["type"] == "limit.stop":
                    label, status = f"stopped: {name} limit", "failed"
                elif entry["type"] == "limit.wait":
                    label = f"waiting for the {name} limit to reset"
                    cutoff = data["run_end"] or now
                    end = min(at + (entry["seconds"] or 0.0), cutoff)
                    if data["running"] and at + (entry["seconds"] or 0.0) > now:
                        status = "waiting"
                elif entry["used"] is not None:
                    label = f"{name} limit: {round(entry['used'] * 100)}% used · {entry['status']}"
                else:
                    label = f"{name} limit: {entry['status']}"
                bars.append({"id": f"limit:{index}", "step": entry["step"], "level": 1, "kind": "limit", "label": label,
                             "start": at, "end": end, "status": status, "ref": entry["call"]})
            # the run's setup before its first step: sandbox image builds and the preflight
            for index, entry in enumerate(data["spans"].get("setup") or []):
                if entry["kind"] == "build":
                    size = f" ({entry['size'] / 1e6:.0f} MB)" if entry.get("size") else ""
                    label = f"building sandbox image {entry['recipe']}{size}"
                elif entry["missed"]:
                    label = f"preflight: missing {', '.join(entry['missed'])}"
                else:
                    label = "preflight: all checks passed"
                open_ = entry["end"] is None
                end = entry["end"] if not open_ else (None if data["running"] else data["run_end"])
                bars.append({"id": f"setup:{entry['kind']}:{index}", "step": "", "level": 0, "kind": "setup",
                             "label": label, "start": entry["start"], "end": end,
                             "status": "running" if open_ and data["running"] else
                             ("failed" if entry["status"] in ("failed", "running") else "done")})
        elif "runs" in (data["facts"].get("waited") or []) and data["run_start"]:
            first = min((b["start"] for b in bars if b["level"] == 0 and b["start"]), default=None)
            wait_end = first if first else (None if data["running"] else data["run_end"])
            bars.append({"id": "wait:runs", "step": "", "level": 0, "kind": "wait", "label": "waiting for a runs slot",
                         "start": data["run_start"], "end": wait_end, "status": "waiting" if wait_end is None else "done"})
            wait_time = (wait_end or now) - data["run_start"]
        order[""] = -1
        bars.sort(key=lambda b: (order.get(b["step"], 999), b["level"], b["start"] or 0))
        agent_time = sum(((b["end"] or now) - b["start"]) for b in bars if b["level"] == 1 and b["kind"] == "agent" and b["start"])
        check_time = sum(((b["end"] or now) - b["start"]) for b in bars if b["level"] == 1 and b["kind"] in {"check", "gate"} and b["start"])
        total = summary["duration"] or ((now - data["run_start"]) if data["run_start"] and data["running"] else 0)
        timeline = {"start": data["run_start"], "end": data["run_end"], "bars": bars,
                    "totals": {"run": total, "agent": agent_time, "check": check_time, "wait": wait_time,
                               "other": max(0.0, total - agent_time - check_time - wait_time)}}
        # outcome
        ship = dict(state.get("ship") or {})
        handoff = None
        if ship.get("action") == "handoff" or ship.get("handoff_error") or ship.get("handoff_skipped"):
            why = ""
            pr_md = self.files.dir / "PR.md"
            if pr_md.is_file():
                text = read_text(pr_md, cap=60_000)
                match = re.search(r"## Why this stopped\s*(.*?)(?=\n## |\Z)", text, re.S)
                asks = re.search(r"## What the agent asks a human to do\s*(.*?)(?=\n## |\Z)", text, re.S)
                why = short(match.group(1).strip()) if match else ""
                handoff = {"why": why, "asks": short(asks.group(1).strip()) if asks else "", "file": "PR.md"}
            handoff = {**(handoff or {}), "stopped_at": ship.get("stopped_at", ""),
                       "reasons": [short(r) for r in ship.get("reasons") or []],
                       "target": short(ship.get("target", "")), "error": short(ship.get("handoff_error", "")),
                       "skipped": short(ship.get("handoff_skipped", ""))}
        decision = None
        recorded = state.get("decision")
        if isinstance(recorded, dict) and recorded:
            # what the stand-in decider did (decide.py); the answer code stays in state.json
            answers = [a for a in recorded.get("answers") or [] if isinstance(a, dict)]
            last = answers[-1] if answers else None
            decision = {key: short(str(recorded.get(key) or "")) for key in
                        ("mode", "action", "from_step", "stopped_at", "summary", "instructions", "status", "skipped",
                         "reason")}
            decision.update({key: short(str(recorded.get(key) or "")) for key in ("kind", "situation", "settle_as",
                                                                                 "promote", "held", "outcome")})
            decision["explanation"] = clip(short(str(recorded.get("explanation") or "")), 4000)
            confidence = recorded.get("confidence")
            decision["confidence"] = (float(confidence) if isinstance(confidence, (int, float))
                                      and not isinstance(confidence, bool) else None)
            decision["read"] = [clip(short(str(x)), 200) for x in recorded.get("read") or [] if isinstance(x, str)][:20] \
                if isinstance(recorded.get("read"), list) else []
            decision["rounds"] = recorded.get("rounds") if isinstance(recorded.get("rounds"), int) else 0
            decision["answer"] = ({key: short(str(last.get(key) or "")) for key in ("kind", "via", "at", "text")}
                                  if last else None)
            decision["file"] = "decision.md" if (self.files.dir / "decision.md").is_file() else ""
        decisions = [entry_view(e, short) for e in read_decisions(self.files.dir / DECISIONS_FILE)]
        timeline["marks"] = [{"at": parse_time(e.get("at")), "who": e.get("who", ""),
                              "action": e.get("action") or e.get("answer") or "", "auto": e["auto"]}
                             for e in decisions if e.get("type") in ("decision", "answer") and parse_time(e.get("at"))]
        review = state.get("review") or {}
        rounds = []
        for item in review.get("rounds") or []:
            if not isinstance(item, dict):
                continue
            number = int(item.get("round") or len(rounds) + 1)
            reviewers = []
            for index, verdict in enumerate(item.get("reviewers") or [], start=1):
                if not isinstance(verdict, dict):
                    continue
                reviewers.append({"dir": f"review-{index}" if number == 1 else f"review-{number}-{index}",
                                  "success": verdict.get("success"), "summary": short(verdict.get("summary", "")),
                                  "issues": [{"severity": i.get("severity", ""), "description": short(i.get("description", "")),
                                              "resolution": short(i.get("resolution", ""))}
                                             for i in verdict.get("issues") or [] if isinstance(i, dict)]})
            patch = item.get("patch") or {}
            rounds.append({"round": number, "reviewers": reviewers,
                           "success": (item.get("verdict") or {}).get("success"),
                           "patch": {"plan": short(patch.get("patch_plan", "")), "rerun": patch.get("checks_rerun", []),
                                     "dirs": [f"patch-{number}", f"implement-{number}"]} if patch else None})
        artefacts = []
        for item in state.get("artefacts") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            entry = {"id": item.get("id"), "type": item.get("type"), "step": item.get("step"),
                     "path": str(item["path"]), "bytes": item.get("bytes"), "name": os.path.basename(str(item["path"])),
                     "tail": short(item.get("tail", ""))[-3000:] if item.get("tail") else "",
                     "keyframes": [str(k.get("path")) for k in item.get("keyframes") or [] if isinstance(k, dict)],
                     "warning": item.get("warning", "")}
            artefacts.append(entry)
        if not artefacts:
            manifest = read_json(self.layer.path / "artefacts" / self.id / "manifest.json")
            for item in manifest if isinstance(manifest, list) else []:
                if isinstance(item, dict) and item.get("path"):
                    artefacts.append({"id": item.get("id"), "type": item.get("type"), "step": item.get("step"),
                                      "path": str(item["path"]), "bytes": item.get("bytes"),
                                      "name": os.path.basename(str(item["path"])), "tail": "",
                                      "keyframes": [str(k.get("path")) for k in item.get("keyframes") or [] if isinstance(k, dict)],
                                      "warning": item.get("warning", "")})
        fanout = {}
        for name, record in (state.get("fanout") or {}).items():
            if isinstance(record, dict):
                fanout[name] = {"pick": record.get("pick"), "adw": record.get("adw"), "picked": record.get("picked", []),
                                "candidates": [{k: (short(v) if isinstance(v, str) else v) for k, v in c.items()}
                                               for c in record.get("candidates") or [] if isinstance(c, dict)]}
        gate = ship.get("gate") if isinstance(ship.get("gate"), dict) else None
        scripts = {name: {k: (short(v) if isinstance(v, str) else v) for k, v in rec.items()}
                   for name, rec in (state.get("scripts") or {}).items() if isinstance(rec, dict)}
        for name, entries in inner.items():
            script = scripts.setdefault(name, {})
            script["inner"] = [{**e, "subject": short(e["subject"]),
                                "fields": {k: (short(v) if isinstance(v, str) else v) for k, v in e["fields"].items()}}
                               for e in entries]
            script["usage"] = inner_usage(entries)
        runlog_tail =read_text(self.files.dir / "run.log", cap=2_000_000).splitlines()[-400:]
        return {
            "summary": summary, "graph": graph, "timeline": timeline,
            "agents": [{**a, "label": a["dir"]} for a in agents],
            "checks": {name: items for name, items in data["attempts"].items()},
            "check_cmds": {**self._declared_check_cmds(data["adw"]),
                           **{name: short(rec.get("cmd", "")) for name, rec in (state.get("checks") or {}).items()}},
            "state": {"branch": state.get("branch", ""), "plan": short(state.get("plan", "")),
                      "worktree": short(state.get("worktree", "")), "ports": state.get("ports") or [],
                      "model_set": state.get("model_set", ""), "class_fixed": state.get("class_fixed"),
                      "overrides": state.get("overrides") or {}, "models": state.get("models") or {},
                      "source": state.get("source", ""), "prompt": short(state.get("prompt", ""))},
            "review": {"mode": review.get("mode", ""), "success": (review.get("verdict") or {}).get("success"),
                       "summary": short((review.get("verdict") or {}).get("summary", "")), "rounds": rounds},
            "ship": {k: (short(v) if isinstance(v, str) else v) for k, v in ship.items() if k != "gate"},
            "gate": gate, "handoff": handoff, "decision": decision, "decisions": decisions,
            "kept": kept_view(state, short), "fanout": fanout, "artefacts": artefacts,
            "documents": run_documents(self.layer, self.id, state),
            "budget": budget_view(state),
            "usage": usage_view(state),
            "scripts": scripts,
            "runlog": [short(line) for line in runlog_tail],
            "steps_detail": {name: {"detail": short(str(r.get("detail", ""))), "attempts": r.get("attempts"),
                                    "status": step_status(data, name), "started": parse_time(r.get("started")),
                                    "ended": parse_time(r.get("ended")), "discipline": discipline_view(r),
                                    "skipped": _state_skipped(state, name),
                                    "outcome": _state_outcome(state, name)[0],
                                    "why": short(_state_outcome(state, name)[1])}
                             for name, r in records.items()},
            "proof": proof_view(state, short),
            "promotion": promotion_view(self.layer, self.id, state, short),
        }


# ============================================================================ slots, ports, containers

def proc_locks() -> set[tuple[int, int]] | None:
    """(device, inode) of every file currently flock()ed, from /proc/locks; None when unreadable.
    In a container /proc/locks only shows locks of processes in its own pid namespace."""
    try:
        text = Path("/proc/locks").read_text()
    except OSError:
        return None
    held = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[1] not in {"FLOCK", "POSIX", "OFDLCK"}:
            continue
        if parts[1] == "->":
            continue
        try:
            major, minor, inode = parts[5].split(":")
            held.add((os.makedev(int(major, 16), int(minor, 16)), int(inode)))
        except ValueError:
            continue
    return held


def pid_alive(pid: int) -> bool | None:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def layer_slots(layer: Layer, runs: list[dict[str, Any]], locks: set[tuple[int, int]] | None,
                in_container: bool) -> dict[str, Any]:
    limits = layer.limits()
    directory = layer.path / "agents" / ".slots"
    pools: dict[str, Any] = {}
    source = "locks" if locks is not None and not in_container else "inferred"
    running = [r for r in runs if r["status"] == "running"]
    for pool in POOLS:
        holders = []
        if source == "locks":
            for index in range(limits[pool]):
                path = directory / f"{pool}.{index}.lock"
                try:
                    info = path.stat()
                except OSError:
                    continue
                if (info.st_dev, info.st_ino) in locks:  # type: ignore[operator]
                    who = read_text(path, cap=300).strip()
                    match = re.search(r"adw ([0-9a-fA-F]{8})", who)
                    holders.append({"slot": index, "who": clip(re.sub(r"^\d+\s*", "", who), 120),
                                    "run": match.group(1) if match else ""})
        else:
            for run in running:
                current = run.get("current") or {}
                busy = {"runs": not run.get("waiting") == "runs",
                        "agents": current.get("kind") in {"run", "review"} and not run.get("waiting") and not run.get("dry_run"),
                        "checks": current.get("kind") == "check" and "running check" in str(current.get("action", ""))}[pool]
                if busy:
                    holders.append({"slot": len(holders), "who": f"adw {run['id']}", "run": run["id"]})
        pools[pool] = {"used": len(holders), "total": limits[pool], "holders": holders}
    ports = []
    table = read_json(layer.path / "agents" / ".ports.json")
    if isinstance(table, dict):
        for run_id, lease in sorted(table.items()):
            if isinstance(lease, list):
                lease = {"ports": lease, "pid": 0}
            if not isinstance(lease, dict):
                continue
            alive = None if in_container else pid_alive(int(lease.get("pid") or 0))
            ports.append({"run": run_id, "ports": lease.get("ports") or [], "alive": alive})
    waiting = [r["id"] for r in running if r.get("waiting")]
    return {"pools": pools, "ports": ports, "source": source, "waiting": waiting}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 5) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class ContainerProbe:
    """Container name/status and, when cheap, CPU/memory of the runs' podman containers.
    Uses the podman CLI when present, else an explicitly given (opt-in) podman API socket, else
    nothing: then the dashboard shows what run.log recorded."""

    def __init__(self, socket_path: str | None, enabled: bool = True) -> None:
        self.socket_path = socket_path
        self.cli = shutil.which("podman") if enabled else None
        self.enabled = enabled and bool(self.cli or socket_path)
        self.source = "podman" if self.cli else ("socket" if socket_path and enabled else "none")
        self.data: dict[tuple[str, str], dict[str, Any]] = {}
        self.error = ""
        self.last = 0.0

    def info(self) -> dict[str, Any]:
        note = {"podman": "podman CLI on this host", "socket": "podman API socket (opt-in)",
                "none": "podman not available here: container status comes from run.log"}[self.source]
        return {"available": self.enabled, "source": self.source, "note": note, "error": self.error}

    def _cli(self, args: list[str]) -> Any:
        result = subprocess.run([self.cli or "podman", *args], capture_output=True, text=True, timeout=8, check=False,
                                stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise OSError(result.stderr.strip()[:200] or f"podman exited {result.returncode}")
        return json.loads(result.stdout or "[]")

    def _api(self, path: str) -> Any:
        connection = UnixHTTPConnection(str(self.socket_path))
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise OSError(f"podman API {response.status}")
            return json.loads(body or b"[]")
        finally:
            connection.close()

    def poll(self, want_stats: bool) -> bool:
        if not self.enabled:
            return False
        try:
            if self.cli:
                listing = self._cli(["ps", "-a", "--filter", "label=al.run", "--format", "json"])
            else:
                filters = urllib.parse.quote(json.dumps({"label": ["al.run"]}))
                listing = self._api(f"/v4.0.0/libpod/containers/json?all=true&filters={filters}")
            data: dict[tuple[str, str], dict[str, Any]] = {}
            ids = []
            for item in listing or []:
                labels = item.get("Labels") or {}
                run, layer_dir = labels.get("al.run"), labels.get("al.layer-dir")
                if not run or not layer_dir:
                    continue
                names = item.get("Names") or []
                name = names[0] if isinstance(names, list) and names else str(names or item.get("Id", "")[:12])
                record = {"name": name.lstrip("/"), "state": str(item.get("State", "")).lower(),
                          "status": item.get("Status", ""), "id": str(item.get("Id", ""))[:12],
                          "image": item.get("Image", ""), "source": self.source}
                key = (os.path.realpath(layer_dir), run)
                previous = data.get(key)
                if previous is None or record["state"] == "running":
                    data[key] = record
                if record["state"] == "running":
                    ids.append(record["id"])
            if want_stats and ids:
                try:
                    if self.cli:
                        stats = self._cli(["stats", "--no-stream", "--format", "json", *ids])
                    else:
                        stats = self._api("/v4.0.0/libpod/containers/stats?stream=false&" +
                                          "&".join(f"containers={i}" for i in ids))
                        stats = stats.get("Stats", []) if isinstance(stats, dict) else stats
                    by_id = {}
                    for item in stats or []:
                        cid = str(item.get("ID") or item.get("ContainerID") or item.get("id") or "")[:12]
                        cpu = item.get("CPU") if "CPU" in item else item.get("cpu_percent")
                        mem = item.get("MemUsage") or item.get("mem_usage") or ""
                        if isinstance(cpu, (int, float)):
                            cpu = f"{cpu:.1f}%"
                        if isinstance(mem, (int, float)):
                            mem = f"{mem / 1048576:.0f} MiB"
                        by_id[cid] = {"cpu": cpu or "", "mem": mem}
                    for record in data.values():
                        record.update(by_id.get(record["id"], {}))
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.error = f"stats: {exc}"[:200]
            changed = data != self.data
            self.data, self.error = data, "" if not self.error.startswith("stats") else self.error
            self.last = time.time()
            return changed
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.error = str(exc)[:200]
            return False

    def lookup(self, layer: Layer, run_id: str) -> dict[str, Any] | None:
        return self.data.get((str(layer.path), run_id))


# ============================================================================ the model and its watcher

class Hub:
    """Fan-out of server events to SSE clients; a client that cannot keep up is dropped (it
    reconnects and gets a fresh snapshot)."""

    def __init__(self) -> None:
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.serial = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        payload = json_bytes(data)
        with self.lock:
            self.serial += 1
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait((kind, payload, self.serial))
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.clients.discard(q)
                q.dead = True  # type: ignore[attr-defined]

    def count(self) -> int:
        with self.lock:
            return len(self.clients)


def sse_frame(kind: str, payload: bytes | str, event_id: str | int | None = None) -> bytes:
    """One Server-Sent Event. `data` lines never contain a raw newline (JSON payloads are single
    line; any text is split over several data: lines as the spec requires)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    out = b""
    if event_id is not None:
        out += f"id: {event_id}\n".encode()
    out += f"event: {kind}\n".encode()
    for line in payload.split(b"\n"):
        out += b"data: " + line.rstrip(b"\r") + b"\n"
    return out + b"\n"


class Model:
    def __init__(self, layer_dirs: list[Path], scan_dirs: list[Path], *, poll: float = 0.75, scan_depth: int = 4,
                 probe: ContainerProbe | None = None, in_container: bool = False, read_only: bool = False,
                 no_start: bool = False, allow_start: bool = False,
                 workspace: Workspace | None = None, jobs: Jobs | None = None, al: Path | None = None,
                 al_looked: list[str] | None = None, no_actions: bool = False,
                 author_flags: list[str] | None = None, no_standin: bool = False) -> None:
        self.workspace = workspace
        self.jobs = jobs
        self.al = al
        self.al_looked = list(al_looked or [])
        self.no_actions = no_actions
        self.author_flags = list(author_flags or [])
        self.finder = load_tool_finder(al)
        self._tool_rows: tuple[float, list[dict[str, Any]]] | None = None
        # nonce → (repo, action, signature, expires, placement)
        self.previews: dict[str, tuple[str, str, tuple, float, str]] = {}
        self.action_lock = threading.Lock()
        self.workspace_version = workspace.version if workspace else 0
        self.job_sigs: dict[str, tuple | None] = {}
        self.job_views: dict[str, dict[str, Any]] = {}
        self.job_checked: dict[str, float] = {}
        self.layer_dirs = [Path(p) for p in layer_dirs]
        self.scan_dirs = [Path(p) for p in scan_dirs]
        self.poll = poll
        self.scan_depth = scan_depth
        self.layers: dict[str, Layer] = {}
        self.summaries: dict[str, dict[str, Any]] = {}
        self.signatures: dict[str, tuple] = {}
        self.checked: dict[str, float] = {}
        self.slots: dict[str, Any] = {}
        self.aggs = AggCache()
        self.hub = Hub()
        self.lock = threading.RLock()
        self.probe = probe or ContainerProbe(None, enabled=False)
        self.in_container = in_container
        self.last_scan = 0.0
        self.stop = threading.Event()
        self.detail_cache: dict[str, tuple[tuple, dict[str, Any]]] = {}
        self.ticks = 0
        self.parents: dict[str, str] = {}
        self._parents_size, self._parents_dirty = -1, True
        self.liveness_checked: dict[str, float] = {}
        self.usage_sigs: dict[str, tuple | None] = {}
        self.settings_sigs: dict[str, tuple | None] = {}
        self.read_only = read_only
        self.no_start = no_start
        self.allow_start = allow_start
        self.landing = Landing()
        self.attention: dict[str, dict[str, Any]] = {}       # run key → attention() of its layer
        self.attention_pending: dict[str, int] = {}          # layer key → landed checks still waiting
        self.pr_answers: dict[tuple, tuple[float, dict[str, Any]]] = {}   # (run key, target heads) → (at, answer)
        # a preview's confirmation: nonce → (expires, layer key, run id, the run's tip, the target's head)
        self.integrate_nonces: dict[str, tuple[float, str, str, str, str]] = {}
        # the stand-in: its loop runs here unless nothing may be written, and one worker asks one layer script at a time
        self.no_standin = no_standin
        self.standin_on = not read_only and not in_container and not no_standin
        self.started = time.time()
        self.standin_views: dict[str, tuple[tuple, dict[str, Any]]] = {}   # layer key → (signature, status answer)
        self.standin_sigs: dict[str, tuple] = {}
        self.standin_queue: queue.Queue = queue.Queue()
        self.standin_pending: set[str] = set()               # "layer/run" or "layer/job:ID" waiting for the worker
        self.standin_done: dict[str, str] = {}               # item key → the stop this process handed over already
        self.standin_tick = 0.0
        self.standin_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- discovery
    def discover(self) -> bool:
        paths: list[Path] = []
        for directory in self.layer_dirs:
            if (directory / LAYER_FILE).is_file():
                paths.append(Path(os.path.realpath(directory)))
        paths += find_layers(self.scan_dirs, self.scan_depth)
        if self.workspace is not None:   # the generated layers of workspace repositories are watched like any layer
            paths += self.workspace.layer_paths()
        changed = False
        with self.lock:
            known = {str(layer.path): layer for layer in self.layers.values()}
            fresh: dict[str, Layer] = {}
            taken: set[str] = set()
            for path in dict.fromkeys(paths):
                existing = known.get(str(path))
                if existing and existing.sig == stat_sig(path / LAYER_FILE):
                    fresh[existing.key] = existing
                    taken.add(existing.key)
            for path in dict.fromkeys(paths):
                if any(layer.path == path for layer in fresh.values()):
                    continue
                existing = known.get(str(path))
                layer = load_layer(path, taken)
                if layer is None:
                    continue
                if existing and existing.key not in taken:
                    layer.key = existing.key
                taken.add(layer.key)
                fresh[layer.key] = layer
                changed = True
            if set(fresh) != set(self.layers):
                changed = True
            for key in set(self.layers) - set(fresh):
                for run_key in [k for k in self.summaries if k.startswith(key + "/")]:
                    del self.summaries[run_key]
                    self.signatures.pop(run_key, None)
            self.layers = fresh
        return changed

    def display_path(self, path: Path) -> str:
        """A layer's path as shown: relative to the --scan root it was found under (prefixed with
        that root's name), else just its directory name. Readable, and it keeps the absolute
        location (often a home directory) off the screen."""
        for root in self.scan_dirs:
            real = Path(os.path.realpath(root))
            if path == real or real in path.parents:
                relative = path.relative_to(real).as_posix()
                return f"{real.name}/{relative}" if relative != "." else real.name
        return path.name

    def ws_display(self, path: Path) -> str:
        """A path in the workspace as shown: relative to its workspace folder, prefixed with that
        folder's name; anything else with the home directory as ~."""
        real = os.path.realpath(path)
        for root in (self.workspace.roots if self.workspace else []):
            if real == str(root):
                return root.name
            if real.startswith(str(root) + os.sep):
                return f"{root.name}/{os.path.relpath(real, root)}"
        return home_short(real)

    # ---------------------------------------------------------------- workspace and actions
    def actions_state(self) -> dict[str, Any]:
        why = ("the dashboard was started with --read-only" if self.read_only else
               "actions are off (--no-actions)" if self.no_actions else
               "no workspace" if self.workspace is None else
               f"needs the al tool; looked for: {', '.join(self.al_looked)}" if self.al is None else "")
        return {"enabled": not why, "why": why}

    def repo_view(self, entry: RepoEntry, layer_keys: dict[str, str] | None = None) -> dict[str, Any]:
        if layer_keys is None:
            with self.lock:
                layer_keys = {str(l.path): l.key for l in self.layers.values()}
        running = next((v["id"] for v in list(self.job_views.values())
                        if v.get("repo") == entry.key and v.get("status") == "running"), "")
        return {"key": entry.key, "name": entry.name, "dir": self.ws_display(entry.path), "state": entry.state,
                "branch": entry.branch, "dirty": entry.dirty, "last_activity": entry.last_activity,
                "layer": self.ws_display(entry.layer_dir) if entry.layer_dir else "", "layer_kind": entry.layer_kind,
                "layer_generated": entry.layer_generated,
                "layer_key": layer_keys.get(os.path.realpath(entry.layer_dir), "") if entry.layer_dir else "",
                "suite": entry.suite, "setup": entry.setup, "setup_checked": entry.setup_checked, "files": entry.files,
                "truncated": entry.truncated, "error": entry.error, "running_job": running}

    def workspace_view(self) -> dict[str, Any]:
        ws = self.workspace
        if ws is None:
            return {"enabled": False, "roots": [], "depth": 0, "scanned_at": None, "repos": []}
        with self.lock:
            layer_keys = {str(l.path): l.key for l in self.layers.values()}
        return {"enabled": True, "roots": [root.name for root in ws.roots], "depth": ws.depth, "scanned_at": ws.scanned_at,
                "repos": [self.repo_view(e, layer_keys) for e in ws.as_list()]}

    def action_views(self, entry: RepoEntry) -> list[dict[str, Any]]:
        cfg = ActionConfig(self.al or Path("al"), self.author_flags)
        docs = Path("/<job>/docs")
        out = []
        placements: list[dict[str, Any]] | None = None    # computed once, shared by create and adopt
        for name, action in ACTIONS.items():
            why, needs = "", ""
            if self.al is None:
                why, needs = f"needs the al tool; looked for: {', '.join(self.al_looked)}", "al"
            elif self.read_only:
                why = "the dashboard was started with --read-only"
            elif self.no_actions:
                why = "actions are off (--no-actions)"
            elif entry.state not in action.states:
                why = f"not for this state ({entry.state}); it is for: {', '.join(action.states)}"
            show = lambda steps: [display_argv(s.argv, self.ws_display, docs) for s in steps]  # noqa: E731
            paths = lambda c: [p if isinstance(p, str) else self.ws_display(p)  # noqa: E731
                               for p in action.writes_paths(entry, c)]
            view = {"name": name, "description": action.description, "writes": action.writes,
                    "available": not why, "why": why, "needs": needs, "states": list(action.states),
                    "commands": show(action.steps(entry, cfg, docs)),
                    "preview": show(action.preview(entry, cfg, docs)) if action.preview else [],
                    "paths": paths(cfg)}
            if action.definition and entry.state in action.states:
                if placements is None:
                    app = Path(os.path.realpath(entry.path))
                    placements = [placement_view(app, placement, self.ws_display) for placement in PLACEMENTS]
                view["placements"] = []
                for placed in placements:
                    c = ActionConfig(cfg.al, cfg.author_flags, placement=placed["name"])
                    view["placements"].append({**placed, "commands": show(action.steps(entry, c, docs)),
                                               "preview": show(action.preview(entry, c, docs)) if action.preview else [],
                                               "paths": paths(c)})
            out.append(view)
        return out

    def job_list(self, repo: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if self.jobs is None:
            return []
        out = []
        for job_id in self.jobs.ids():
            job = self.jobs.read(job_id)
            if job and (repo is None or job.get("repo") == repo):
                out.append(job_summary(job))
        out.sort(key=lambda j: -(j.get("created") or 0))
        return out[:limit]

    def repo_detail(self, key: str) -> dict[str, Any] | None:
        entry = self.workspace.entry(key) if self.workspace else None
        if entry is None:
            return None
        return {"repo": self.repo_view(entry), "summary": entry.summary, "actions": self.action_views(entry),
                "jobs": self.job_list(entry.key, limit=20),
                "al": {"found": self.al is not None, "looked": self.al_looked, "tools": self.tool_rows()}}

    def author_agent(self) -> str:
        """The agent kind `author` steps run with: --agent in the author flags, else claude-code."""
        flags = self.author_flags
        return flags[flags.index("--agent") + 1] if "--agent" in flags[:-1] else "claude-code"

    def job_tools(self) -> tuple[str, ...]:
        """The tools a job's steps run besides the al tool: uv (its shebang), git, racket and the agent CLI."""
        agent = self.finder.agent_bin(self.author_agent()) if self.finder else ()
        return ("uv", "git", "racket") + tuple(agent)

    def tool_rows(self) -> list[dict[str, Any]]:
        """al doctor's rows (tool, found where, version) for the Repos view; kept TOOL_ROWS_TTL seconds."""
        if self.finder is None:
            return []
        now = time.monotonic()
        if self._tool_rows is None or now - self._tool_rows[0] >= TOOL_ROWS_TTL:
            agent = self.finder.agent_bin(self.author_agent())
            try:
                rows = self.finder.doctor_rows(("racket", "raco", "uv", "git") + tuple(agent), None, versions=True)
            except Exception as exc:  # noqa: BLE001 — the table is a help; never a failed page
                print(f"[dashboard] cannot list the tools: {exc}", file=sys.stderr)
                rows = []
            self._tool_rows = (now, rows)
        return self._tool_rows[1]

    def start_action(self, entry: RepoEntry, name: str, nonce: str | None,
                     choices: dict[str, str] | None = None, placement: str | None = None) -> tuple[int, dict[str, Any]]:
        """Every guard, then a preview (a writing action without a confirmation) or a job. → (status, body).
        `choices` (create and adopt only, checked against CHOICE_RULES) go to the author step; `placement`
        (create and adopt only, one of PLACEMENTS, beside when absent) is where the new layer goes: it is
        checked by `placement_view`, as the page shows it, and a confirmation is bound to its preview's."""
        placement = placement or "beside"
        action = ACTIONS[name]
        if self.al is None:
            return 409, {"error": f"this action needs the al tool; looked for: {', '.join(self.al_looked)}"}
        if self.jobs is None or self.workspace is None:
            return 409, {"error": "actions are off"}
        if entry.state not in action.states:
            return 409, {"error": f"{name} is not for {entry.name}: its state is {entry.state} "
                                  f"({name} is for: {', '.join(action.states)})"}
        app = os.path.realpath(entry.path)
        if not self.workspace.inside(app) or not os.path.lexists(os.path.join(app, ".git")):
            return 409, {"error": f"{entry.name} is no longer a git repository inside the workspace"}
        layer = layer_of(entry, placement)
        if not self.workspace.inside(str(layer)) and not str(layer).startswith(app + os.sep):
            return 409, {"error": f"{entry.name}'s layer is outside the workspace"}
        if action.definition:
            problem = definition_problem(Path(app), layer, self.ws_display)
            if problem:
                return 409, {"error": problem}
            view = placement_view(Path(app), placement, self.ws_display)
            if not view["ok"]:
                return 409, {"error": view["problem"]}
        cfg = ActionConfig(self.al, self.author_flags, dict(choices or {}), placement)
        with self.action_lock:
            busy = self.jobs.running_for(app)
            if busy is not None or lock_held(lock_file(self.jobs.root, app)):
                return 409, {"error": f"an action is already running on {entry.name} (job {(busy or {}).get('id', '?')})"}
            now = time.time()
            for old in [n for n, v in self.previews.items() if v[3] < now]:
                del self.previews[old]
            if action.writes:
                sig = confirm_signature(Path(app), layer)
                if not nonce:
                    nonce = secrets.token_urlsafe(18)
                    self.previews[nonce] = (entry.key, name, sig, now + PREVIEW_TTL, placement)
                    docs = Path("/<job>/docs")
                    body: dict[str, Any] = {
                        "preview": True, "repo": entry.key, "action": name, "nonce": nonce,
                        "commands": [display_argv(s.argv, self.ws_display, docs) for s in action.steps(entry, cfg, docs)],
                        "writes": [p if isinstance(p, str) else self.ws_display(p)
                                   for p in action.writes_paths(entry, cfg)]}
                    if action.definition:
                        body["placement"] = {"name": placement, "path": self.ws_display(layer)}
                    if action.preview:
                        body["job"] = self._spawn(entry, name, action.preview, cfg, layer, preview=True)
                    return 200, body
                stored = self.previews.pop(nonce, None)
                if stored is None or stored[0] != entry.key or stored[1] != name or stored[3] < now:
                    return 409, {"error": "this confirmation is unknown, used or expired: preview the action again"}
                if stored[4] != placement:
                    return 409, {"error": "the placement changed since the preview: preview the action again"}
                if stored[2] != sig:
                    return 409, {"error": f"{entry.name} changed since the preview: preview the action again"}
            job_id = self._spawn(entry, name, action.steps, cfg, layer)
        return 202, {"job": job_id, "repo": entry.key, "action": name}

    def _spawn(self, entry: RepoEntry, name: str, build: Callable[[RepoEntry, ActionConfig, Path], list[Step]],
               cfg: ActionConfig, layer: Path, preview: bool = False) -> str:
        assert self.jobs is not None
        job_id, directory = self.jobs.allocate()
        docs = directory / "docs"
        steps = build(entry, cfg, docs)
        app = os.path.realpath(entry.path)
        now = utc_stamp()
        agent_flag = ["--agent", self.author_agent()] if "--agent" in self.author_flags[:-1] else []
        job = {"version": 1, "id": job_id, "repo": entry.key, "repo_name": entry.name, "repo_path": app,
               "repo_dir": self.ws_display(Path(app)), "action": name, "preview": preview, "status": "running",
               "pid": None, "host": socket.gethostname(), "created": now, "heartbeat": now, "started": None,
               "ended": None, "exit": None, "layer_dir": str(layer), "layer_display": self.ws_display(layer),
               "steps": [{"name": s.name, "argv": s.argv, "display": display_argv(s.argv, self.ws_display, docs),
                          "doc": s.doc, "timeout": s.timeout, "stream": s.stream, "status": "pending", "exit": None,
                          "started": None, "ended": None} for s in steps],
               "changed": [], "repo_changes": [], "repo_unchanged": None,
               "path": self.job_path(),
               "preflight": [str(cfg.al), "doctor", str(layer),
                             "--for", ",".join(dict.fromkeys(s.argv[1] for s in steps if len(s.argv) > 1)), *agent_flag],
               "retries": [], "choices": {} if preview else dict(cfg.choices),
               "placement": cfg.placement if ACTIONS[name].definition else ""}
        uv = self.finder.find("uv") if self.finder is not None else None
        if uv is not None and not uv.path and job["steps"]:
            # the al tool starts through uv (its shebang): without it no step can run; say so at once
            sentence = self.finder.missing_sentence(uv)
            first, *rest = job["steps"]
            first.update(status="failed", reason=sentence, ended=now)
            for step in rest:
                step["status"] = "skipped"
            job.update(status="failed", reason=f"{first['name']}: {sentence}", ended=now, exit=127)
            write_json_atomic(directory / "job.json", job)
            self.hub.publish("job", job_summary(job))
            return job_id
        write_json_atomic(directory / "job.json", job)
        self._launch(directory)
        self.hub.publish("job", job_summary(job))
        return job_id

    def job_path(self) -> str:
        """A job's PATH: the folders of the tools it runs, then the dashboard's own PATH."""
        base = os.environ.get("PATH", "")
        if self.finder is None:
            return base
        try:
            return self.finder.search_path(self.job_tools(), base)
        except Exception:  # noqa: BLE001 — a PATH without the extra folders is still a PATH
            return base

    def _launch(self, directory: Path) -> None:
        """The detached runner of the job in `directory` (dashboard.py --run-job)."""
        proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--run-job", str(directory)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True)
        threading.Thread(target=proc.wait, name=f"job-{directory.name}", daemon=True).start()   # no zombie runner

    def retry_job(self, job_id: str, step: str, by: str = "person") -> tuple[int, dict[str, Any]]:
        """Run a failed or interrupted job again from its first step that did not pass, in place: the
        passed steps, their times and the output log stay. A retried `author` gets --accept-existing.
        → (status, body)."""
        if self.al is None:
            return 409, {"error": f"retrying needs the al tool; looked for: {', '.join(self.al_looked)}"}
        if self.jobs is None or self.workspace is None:
            return 409, {"error": "actions are off"}
        directory = self.jobs.dir(job_id)
        job = self.jobs.read(job_id) if directory else None
        if job is None or directory is None:
            return 404, {"error": "unknown job"}
        job = self.jobs.settle(job)
        if job.get("preview") or job.get("status") not in ("failed", "interrupted"):
            return 409, {"error": "only a failed or interrupted job can be retried"}
        first = job_summary(job)["retry_from"]
        if step != first:
            return 409, {"error": f"retry starts at the first step that did not pass: {first}"}
        app = str(job.get("repo_path") or "")
        if not app or not self.workspace.inside(app) or not os.path.lexists(os.path.join(app, ".git")):
            return 409, {"error": f"{job.get('repo_name')} is no longer a git repository inside the workspace"}
        with self.action_lock:
            busy = self.jobs.running_for(app)
            if busy is not None or lock_held(lock_file(self.jobs.root, app)):
                return 409, {"error": f"an action is already running on {job.get('repo_name')} "
                                      f"(job {(busy or {}).get('id', '?')})"}
            names = [s.get("name") for s in job["steps"]]
            for s in job["steps"][names.index(step):]:
                for key in ("exit", "started", "ended", "reason", "pid"):
                    s.pop(key, None)
                s.update(status="pending", exit=None, started=None, ended=None)
                if s.get("name") == "author" and "--accept-existing" not in s["argv"]:
                    s["argv"] = [*s["argv"], "--accept-existing"]
                    s["display"] = display_argv(s["argv"], self.ws_display, directory / "docs")
            now = utc_stamp()
            job.update(status="running", reason="", exit=None, ended=None, pid=None, heartbeat=now,
                       retries=[*(job.get("retries") or []), {"from": step, "at": now}], path=self.job_path())
            write_json_atomic(directory / "job.json", job)
            self._launch(directory)
        self.hub.publish("job", job_summary(job))
        if by == "person":
            for layer in self.layers_of_repo(app):
                self.standin_note(layer, {"job": job_id, "kind": "retry-job", "action": "retry-job",
                                          "outcome": f"retried from {step}"})
        return 202, {"job": job_id, "from": step}

    @staticmethod
    def _job_sig(directory: Path, view: dict[str, Any] | None) -> tuple | None:
        """A job's signature: its job.json, and while it runs its running step's log and journal."""
        sig = stat_sig(directory / "job.json")
        if not view or view.get("status") != "running":
            return sig
        running = next((s.get("name") for s in view.get("steps") or [] if s.get("status") == "running"), None)
        files = step_files(directory, running)
        return (sig, *(stat_sig(path) for path in files.values()))

    def tick_jobs(self, now: float) -> bool:
        """Publish changed jobs; a running job whose runner died is rewritten as interrupted. → any ended?"""
        if self.jobs is None:
            return False
        ended = False
        for job_id in self.jobs.ids():
            directory = self.jobs.root / job_id
            last = self.job_views.get(job_id)
            sig = self._job_sig(directory, last)
            stale = last is not None and last.get("status") == "running" and now - self.job_checked.get(job_id, 0) >= 2
            if sig == self.job_sigs.get(job_id) and not stale:
                continue
            self.job_sigs[job_id] = sig
            self.job_checked[job_id] = now
            job = self.jobs.read(job_id)
            if job is None:
                continue
            job = self.jobs.settle(job, now)
            view = job_summary(job)
            if view["status"] == "running":     # what the running step is doing, live on the steps row
                for step in view["steps"]:
                    if step.get("status") == "running":
                        step["doing"] = view["doing"] = step_doing(directory, str(step.get("name")))
                        files = step_files(directory, step.get("name"))
                        step["log_bytes"] = (stat_sig(files["log"]) or (0, 0))[1] if files else 0
            if view != last:
                if last is not None and last.get("status") == "running" and view["status"] != "running":
                    ended = True
                self.job_views[job_id] = view
                self.job_sigs[job_id] = self._job_sig(directory, view)
                if last is not None or now - (view.get("created") or 0) < 3600:
                    self.hub.publish("job", view)
        return ended

    def layer_list(self) -> list[dict[str, Any]]:
        with self.lock:
            out = []
            for layer in sorted(self.layers.values(), key=lambda l: l.key):
                runs = [s for s in self.summaries.values() if s["layer"] == layer.key]
                out.append({"key": layer.key, "name": layer.name, "dir": self.display_path(layer.path),
                            "description": str(layer.ir.get("description", "")),
                            "adws": [a.get("name") for a in layer.ir.get("adws") or [] if isinstance(a, dict)],
                            "runs": len(runs), "running": sum(1 for r in runs if r["status"] == "running"),
                            "needs_you": self.needs_you(layer.key),
                            "explain": layer.explain_file(), "limits": layer.limits(),
                            "budget": self.layer_budget(layer, runs),
                            "usage": self.layer_usage(layer, time.time()),
                            "models": layer.models_view(self.read_only),
                            "standin": self.standin_view(layer),
                            "start": start_status(layer, self)})
            return out

    # ---------------------------------------------------------------- the stand-in
    def standin_loop(self) -> str:
        """Whether this process hands the Needs you list to the stand-in, and why not."""
        if self.read_only:
            return "off: --read-only"
        if self.in_container:
            return "off: in a container"
        if self.no_standin:
            return "off: --no-stand-in"
        return "on"

    def _standin_sig(self, layer: Layer) -> tuple:
        return (stat_sig(layer.path / DECIDER_FILE), stat_sig(layer.path / STANDIN_LOG), layer.sig,
                stat_sig(layer.path / STANDIN_SCRIPT), local_day(time.time()))

    def standin_view(self, layer: Layer) -> dict[str, Any]:
        """The Stand-in panel of one layer: the answer of its `adws/standin.py status` (mode, source,
        scope, limits, counts, the end of the log), kept until the setting, the log or the layer changes,
        plus whether the page may switch it and whether the loop runs here."""
        base = {"loop": self.standin_loop()}
        if not (layer.path / STANDIN_SCRIPT).is_file():
            return {"available": False, "why": f"generated before the stand-in: run `al gen {layer.name}`",
                    "mode": "off", "writable": False, **base}
        sig = self._standin_sig(layer)
        cached = self.standin_views.get(layer.key)
        if cached is not None and cached[0] == sig:
            view = cached[1]
        else:
            code, result, tail = standin_via_layer(layer, ["status", "--json"])
            if code == 0 and result.get("mode") in DECIDER_MODES:
                view = {"available": True, "why": "", **result}
            else:
                last = (tail.strip().splitlines() or ["no answer"])[-1]
                view = {"available": False, "mode": "off",
                        "why": f"the layer's stand-in did not answer: {clip(home_short(last), 300)}"}
            self.standin_views[layer.key] = (sig, view)
        writable, why = settings_writable(layer, self.read_only)
        return {**view, "writable": bool(view.get("available")) and writable, "why": view.get("why") or why, **base}

    def drop_standin_view(self, layer: Layer) -> None:
        self.standin_views.pop(layer.key, None)

    def standin_item(self, row: dict[str, Any]) -> dict[str, Any]:
        """What the stand-in is told about one run of the Needs you list."""
        landing = row.get("landing") if isinstance(row.get("landing"), dict) else {}
        return {"run": row["id"], "status": row.get("status"), "reason": str(row.get("reason") or ""),
                "next": str(row.get("next") or ""),
                "landing": {"state": landing.get("state"), "differ": landing.get("differ"), "target": landing.get("target")},
                "target_branch": str(row.get("target_branch") or ""), "replaced_by": str(row.get("replaced_by") or "")}

    def standin_items(self, layer: Layer, view: dict[str, Any]) -> list[dict[str, Any]]:
        """The items the loop hands to the stand-in, oldest first: runs of the layer that need a person,
        are not running, stopped after the stand-in was switched on and whose stop it has not looked at;
        and failed or interrupted jobs on the layer's repository it has not looked at."""
        since = parse_time(view.get("since")) or self.started
        with self.lock:
            rows = [self._decorated(s) for s in self.summaries.values() if s["layer"] == layer.key]
        items: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda r: (r.get("ended") or r.get("started") or 0, r["id"])):
            if row.get("bucket") != "needs-you" or row.get("status") in ("running", "unknown"):
                continue
            if row.get("decider") == "off":                      # the run's own --decider off
                continue
            if row.get("decision_status") == "resuming":         # a resume was started: not a new stop
                continue
            ended = parse_time(row.get("ended_at")) or row.get("ended") or row.get("last_seen") or row.get("mtime") or 0
            if ended <= since:
                continue
            stop = str(row.get("fingerprint") or "")
            if row.get("standin_stop") == stop or self.standin_done.get(row["key"]) == stop:
                continue
            items.append(self.standin_item(row))
        if self.jobs is None or self.workspace is None or self.no_actions:
            return items
        repo = app_repo(layer.path, layer.ir)
        seen = {str(e.get("stop") or "") for e in read_decisions(layer.path / STANDIN_LOG, limit=None) if e.get("job")}
        for job_id in self.jobs.ids():
            directory = self.jobs.dir(job_id)
            job = self.jobs.read(job_id) if directory else None
            if job is None or directory is None or repo is None:
                continue
            job = self.jobs.settle(job)
            if job.get("preview") or job.get("status") not in ("failed", "interrupted"):
                continue
            if os.path.realpath(str(job.get("repo_path") or "")) != os.path.realpath(str(repo)):
                continue
            ended = parse_time(job.get("ended")) or 0
            stop = f"job:{job_id}:{len(job.get('retries') or [])}"
            if ended <= since or stop in seen or self.standin_done.get(f"{layer.key}/job:{job_id}") == stop:
                continue
            step = retry_from(job)
            items.append({"job": {"id": job_id, "action": str(job.get("action") or ""), "failed_step": step,
                                  "reason": clip(str(job.get("reason") or ""), 600),
                                  "log": "\n".join(last_relevant_lines(directory, step)),
                                  "retries": len(job.get("retries") or [])}})
        return items

    @staticmethod
    def _standin_key(layer: Layer, item: dict[str, Any]) -> str:
        job = item.get("job") if isinstance(item.get("job"), dict) else None
        return f"{layer.key}/job:{job['id']}" if job else f"{layer.key}/{item.get('run')}"

    def standin_enqueue(self, layer: Layer, verb: str, item: dict[str, Any]) -> bool:
        """Queue one piece of work for the stand-in (consider, ask, apply); False when it is queued already."""
        key = self._standin_key(layer, item)
        with self.lock:
            if key in self.standin_pending:
                return False
            self.standin_pending.add(key)
            if self.standin_thread is None or not self.standin_thread.is_alive():
                self.standin_thread = threading.Thread(target=self.standin_worker, name="stand-in", daemon=True)
                self.standin_thread.start()
        self.standin_queue.put((layer.key, verb, item))
        return True

    def tick_standin(self, now: float) -> None:
        """Hand the first new item of each layer's Needs you list to the stand-in (one per layer and look)."""
        if not self.standin_on or now - self.standin_tick < STANDIN_EVERY:
            return
        self.standin_tick = now
        with self.lock:
            layers = list(self.layers.values())
        for layer in layers:
            view = self.standin_view(layer)
            if not view.get("available") or view.get("mode") not in ("suggest", "auto") or view.get("paused") \
                    or view.get("error"):
                continue
            for item in self.standin_items(layer, view):
                if self.standin_enqueue(layer, "consider", item):
                    break

    def standin_worker(self) -> None:
        """One piece of stand-in work at a time, for every layer, until the dashboard stops."""
        while not self.stop.is_set():
            try:
                layer_key, verb, item = self.standin_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            layer = self.layers.get(layer_key)
            try:
                if layer is not None:
                    self.standin_handle(layer, verb, item)
            except Exception as exc:  # noqa: BLE001 — one item must never stop the worker
                print(f"[dashboard] stand-in error: {type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                if layer is not None:
                    with self.lock:
                        self.standin_pending.discard(self._standin_key(layer, item))

    def standin_handle(self, layer: Layer, verb: str, item: dict[str, Any]) -> dict[str, Any]:
        """Do one piece of stand-in work through the layer's script and publish what changed. `verb`:
        consider (the loop), ask ("Ask the stand-in": a suggestion whatever the mode) or apply (a stored
        suggestion). A `retry-job` the stand-in decided is carried out here, where the jobs are."""
        job = item.get("job") if isinstance(item.get("job"), dict) else None
        if verb == "apply":
            argv, stdin = ["apply", str(item.get("run")), "--via", "dashboard", "--json"], None
        elif verb == "ask":
            argv, stdin = ["consider", "--suggest-only", "--via", "asked", "--json"], item
        else:
            argv, stdin = ["consider", "--via", "loop", "--json"], item
        _code, result, tail = standin_via_layer(layer, argv, stdin=stdin, timeout=STANDIN_TIMEOUT)
        if not result:
            result = {"status": "failed", "reason": clip(home_short((tail.strip().splitlines() or ["no answer"])[-1]), 300)}
        if verb == "consider":
            with self.lock:
                row = self.summaries.get(f"{layer.key}/{item.get('run')}") if job is None else None
            stop = f"job:{job['id']}:{int(job.get('retries') or 0)}" if job else str((row or {}).get("fingerprint") or "")
            self.standin_done[self._standin_key(layer, item)] = stop
        perform = result.get("perform") if isinstance(result.get("perform"), dict) else None
        if perform and perform.get("kind") == "retry-job":
            status, answer = self.retry_job(str(perform.get("job") or ""), str(perform.get("from") or ""), by="stand-in")
            text = f"retried from {perform.get('from')}" if status == 202 else f"not retried: {answer.get('error', status)}"
            entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
            if entry.get("id"):
                standin_via_layer(layer, ["note", "--json"], stdin={"ref": entry["id"], "outcome": text})
        self.drop_standin_view(layer)
        if job is None and RUN_ID.match(str(item.get("run") or "")):
            self.refresh_run(layer, str(item["run"]))
        self.hub.publish("layers", self.layer_list())
        return result

    def refresh_run(self, layer: Layer, run_id: str) -> None:
        """Read one run again now and publish it (after the stand-in or a person changed its files)."""
        key = f"{layer.key}/{run_id}"
        if not (layer.path / "agents" / run_id).is_dir():
            return
        try:
            fresh = RunModel(layer, run_id, self.aggs, can_see_pids=not self.in_container).summary(
                self._extras(layer, run_id))
        except Exception:  # noqa: BLE001 — the watcher reads it again on its next tick
            return
        with self.lock:
            self.summaries[key] = fresh
            self.signatures[key] = RunFiles(layer, run_id).signature(deep=fresh["status"] in ("running", "unknown"))
        self.hub.publish("run", self._decorated(fresh))
        self.refresh_attention(layer)

    def standin_note(self, layer: Layer, entry: dict[str, Any]) -> None:
        """Record what a person did on the page (settled, undid, started again, retried) in the layer's
        record of decisions, so the stand-in knows a person touched the item. Only for a layer that uses
        the stand-in (it is switched on, or has a log already): nothing changes for one that does not.
        Never fails the action."""
        if self.read_only or not (layer.path / STANDIN_SCRIPT).is_file():
            return
        if not (layer.path / STANDIN_LOG).is_file() and self.standin_view(layer).get("mode") not in ("suggest", "auto"):
            return
        try:
            standin_via_layer(layer, ["note", "--json"],
                              stdin={"type": "decision", "who": "person", "via": "dashboard", "status": "acted", **entry})
            self.drop_standin_view(layer)
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboard] stand-in note failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def layers_of_repo(self, repo_path: str) -> list[Layer]:
        real = os.path.realpath(repo_path) if repo_path else ""
        with self.lock:
            layers = list(self.layers.values())
        return [l for l in layers if real and (repo := app_repo(l.path, l.ir)) is not None
                and os.path.realpath(str(repo)) == real]

    def unsettle(self, layer_key: str, run_id: str) -> tuple[int, dict[str, Any]]:
        """Undo the last settlement of a run (the page's Undo) → (HTTP status, answer)."""
        layer = self.layers.get(layer_key)
        if layer is None:
            return 404, {"error": "unknown layer"}
        if not isinstance(run_id, str) or not RUN_ID.match(run_id) or not (layer.path / "agents" / run_id).is_dir():
            return 404, {"error": "unknown run"}
        try:
            record = remove_settlement(layer.path, run_id)
        except SettleError as exc:
            return 404, {"error": str(exc)}
        except OSError as exc:
            return 409, {"error": f"cannot change {SETTLEMENT_FILE}: {exc.strerror or exc}"}
        self.standin_note(layer, {"run": run_id, "kind": "settle", "action": "undo", "outcome": "settlement undone"})
        self.refresh_run(layer, run_id)
        return 200, {"status": "undone", "record": record}

    def needs_you(self, layer_key: str) -> int:
        """How many runs of the layer are in the needs-you bucket (what the Runs link counts)."""
        with self.lock:
            return sum(1 for key, item in self.attention.items()
                       if key.startswith(layer_key + "/") and item.get("bucket") == "needs-you")

    def _pr_state(self, layer: Layer, row: dict[str, Any], number: Any) -> dict[str, Any]:
        """The forge's answer for a run's pull request, kept PR_STATE_SECONDS per (run, target heads)."""
        repo = app_repo(layer.path, layer.ir)
        heads = tuple(self.landing.heads(repo, str(row.get("target_branch") or "")) or ()) if repo else ()
        key, now = (row["key"], heads), time.time()
        with self.lock:
            cached = self.pr_answers.get(key)
        if cached and now - cached[0] < PR_STATE_SECONDS:
            return cached[1]
        answer = pr_state_via_layer(layer, int(number))
        with self.lock:
            self.pr_answers[key] = (now, answer)
        return answer

    def refresh_attention(self, layer: Layer) -> None:
        """Recompute the buckets of one layer's runs (landed checks bounded per call) and publish the
        runs whose attention changed, and the layers when the needs-you count changed."""
        with self.lock:
            runs = [s for s in self.summaries.values() if s["layer"] == layer.key]
        forge = layer.ir.get("forge") if isinstance(layer.ir.get("forge"), dict) else {}
        ask = None
        if str(forge.get("kind") or "none") != "none" and not self.in_container and (layer.path / LAUNCHER).is_file():
            ask = lambda row, number: self._pr_state(layer, row, number)   # noqa: E731
        before = self.needs_you(layer.key)
        result, pending = layer_attention(layer, runs, self.landing, limit=LANDING_CHECKS_PER_TICK, pr_state=ask)
        changed = []
        with self.lock:
            for key, item in result.items():
                if self.attention.get(key) != item:
                    self.attention[key] = item
                    changed.append(key)
            for key in [k for k in self.attention if k.startswith(layer.key + "/") and k not in result]:
                del self.attention[key]
            self.attention_pending[layer.key] = pending
            published = [self._decorated(self.summaries[key]) for key in changed if key in self.summaries]
        for summary in published:
            self.hub.publish("run", summary)
        if self.needs_you(layer.key) != before:
            self.hub.publish("layers", self.layer_list())

    def settle(self, layer_key: str, run_id: str, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Write a person's settlement of a finished run (the page's form) → (HTTP status, answer)."""
        layer = self.layers.get(layer_key)
        if layer is None:
            return 404, {"error": "unknown layer"}
        if not isinstance(run_id, str) or not RUN_ID.match(run_id) or not (layer.path / "agents" / run_id).is_dir():
            return 404, {"error": "unknown run"}
        key = f"{layer_key}/{run_id}"
        model = RunModel(layer, run_id, self.aggs, can_see_pids=not self.in_container)
        with self.lock:
            summary = self.summaries.get(key)
        status = str((summary or model.summary()).get("status") or "unknown")
        try:
            record = write_settlement(layer.path, run_id, as_=request.get("as"), by=request.get("by") or "",
                                      note=request.get("note") or "", who=request.get("who") or "", via="dashboard",
                                      status=status, repo=app_repo(layer.path, layer.ir))
        except SettleError as exc:
            return (409 if exc.kind == "running" else 404 if exc.kind == "missing" else 400), {"error": str(exc)}
        except OSError as exc:
            return 409, {"error": f"cannot write {SETTLEMENT_FILE}: {exc.strerror or exc}"}
        fresh = model.summary(self._extras(layer, run_id))
        with self.lock:
            self.summaries[key] = fresh
            self.signatures[key] = RunFiles(layer, run_id).signature(deep=False)
        self.hub.publish("run", self._decorated(fresh))
        self.refresh_attention(layer)
        self.standin_note(layer, {"run": run_id, "kind": "settle", "action": "settle",
                                  "outcome": f"settled as {record['as']}"})
        return 200, {"status": "settled", "record": record}

    # ---------------------------------------------------------------- resolve with an agent
    def _run_of(self, layer_key: str, run_id: Any) -> tuple[Layer | None, int, str]:
        with self.lock:
            layer = self.layers.get(layer_key) if isinstance(layer_key, str) else None
        if layer is None:
            return None, 404, "unknown layer"
        if not isinstance(run_id, str) or not RUN_ID.match(run_id) or not (layer.path / "agents" / run_id).is_dir():
            return None, 404, "unknown run"
        return layer, 0, ""

    def _layer_summaries(self, layer: Layer) -> list[dict[str, Any]]:
        """The layer's run summaries: the watcher's, completed by a read for a run it has not seen yet."""
        with self.lock:
            known = {s["id"]: s for s in self.summaries.values() if s["layer"] == layer.key}
        for run_id in self.run_ids(layer):
            if run_id not in known:
                known[run_id] = RunModel(layer, run_id, self.aggs, can_see_pids=not self.in_container).summary()
        return list(known.values())

    def _reread(self, layer: Layer, run_ids: Iterable[str]) -> None:
        """Read these runs again now (a record of theirs was just written) and recompute the buckets."""
        for run_id in run_ids:
            if not RUN_ID.match(run_id or "") or not (layer.path / "agents" / run_id).is_dir():
                continue
            key = f"{layer.key}/{run_id}"
            try:
                fresh = RunModel(layer, run_id, self.aggs, can_see_pids=not self.in_container).summary(
                    self._extras(layer, run_id))
            except Exception:  # noqa: BLE001 — the watcher's next tick reads it
                continue
            with self.lock:
                self.summaries[key] = fresh
                self.signatures[key] = RunFiles(layer, run_id).signature(deep=fresh["status"] in ("running", "unknown"))
            self.hub.publish("run", self._decorated(fresh))
        self.refresh_attention(layer)

    def integrate_preview(self, layer_key: str, run_id: str) -> tuple[int, dict[str, Any]]:
        """What "Resolve with an agent" would do for a run, from the layer's own adws/integrate.py
        preview, plus whether this dashboard may start it and a confirmation for the start."""
        layer, code, why = self._run_of(layer_key, run_id)
        if layer is None:
            return code, {"error": why}
        start = integrate_status(layer, self)
        if not (layer.path / INTEGRATE_SCRIPT).is_file():
            return 409, {"error": start["why"] if not start["enabled"] and "al gen" in start["why"]
                         else f"generated before `al integrate`: run `al gen {layer.name}`"}
        exit_code, result, stderr = integrate_via_layer(layer, ["preview", run_id])
        if "eligible" not in result:
            if result.get("status") == "error":
                return 409, {"error": f"{result.get('reason')}: {result.get('error')}", "reason": result.get("reason")}
            tail = "\n".join(stderr.strip().splitlines()[-12:])
            return 502, {"error": f"the layer's integrate.py failed (exit {exit_code})", "stderr": tail}
        short = layer.shortener()
        answer = {**result, "why": short(result.get("why") or ""), "start": start, "tiers": declared_tiers(layer.ir)}
        if result.get("eligible") and start["enabled"]:
            nonce, now = secrets.token_urlsafe(16), time.time()
            with self.lock:
                for old in [n for n, entry in self.integrate_nonces.items() if entry[0] < now]:
                    del self.integrate_nonces[old]
                self.integrate_nonces[nonce] = (now + PREVIEW_TTL, layer.key, run_id, str(result.get("tip") or ""),
                                                str(result.get("target_head") or ""))
            answer["nonce"] = nonce
        return 200, answer

    def integrate_start(self, layer_key: str, run_id: str, nonce: Any, model_set: Any = "") -> tuple[int, dict[str, Any]]:
        """Start the integration a preview showed: the nonce is that preview's, for this layer and
        run, unexpired and not used before; the run starts detached through the layer's own script."""
        layer, code, why = self._run_of(layer_key, run_id)
        if layer is None:
            return code, {"error": why}
        if model_set not in ("", None) and model_set not in declared_tiers(layer.ir):
            return 400, {"error": f"model_set is one of the layer's tiers: {', '.join(declared_tiers(layer.ir)) or '(none)'}"}
        stale = {"error": "the preview is stale: open it again"}
        with self.lock:
            entry = self.integrate_nonces.pop(nonce, None) if isinstance(nonce, str) else None
        if entry is None or entry[0] < time.time() or entry[1:3] != (layer.key, run_id):
            return 409, stale
        if not (layer.path / INTEGRATE_SCRIPT).is_file():
            return 409, {"error": f"generated before `al integrate`: run `al gen {layer.name}`"}
        argv = ["run", run_id, "--detach", "--by", "dashboard", *(["--model-set", model_set] if model_set else [])]
        exit_code, result, stderr = integrate_via_layer(layer, argv)
        return self._integrate_answer(layer, run_id, exit_code, result, stderr)

    def integrate_approve(self, layer_key: str, run_id: str, who: str) -> tuple[int, dict[str, Any]]:
        """A person approves a waiting integration (the original's or the integration run's id)."""
        layer, code, why = self._run_of(layer_key, run_id)
        if layer is None:
            return code, {"error": why}
        if not (layer.path / INTEGRATE_SCRIPT).is_file():
            return 409, {"error": f"generated before `al integrate`: run `al gen {layer.name}`"}
        exit_code, result, stderr = integrate_via_layer(layer, ["approve", run_id, "--who", who, "--detach"])
        return self._integrate_answer(layer, run_id, exit_code, result, stderr)

    def _integrate_answer(self, layer: Layer, run_id: str, exit_code: int, result: dict[str, Any],
                          stderr: str) -> tuple[int, dict[str, Any]]:
        if result.get("status") == "started":
            new = str(result.get("run_id") or "")
            self._reread(layer, [str(result.get("original") or run_id), new])   # the row moves at once
            return 202, {"status": "started", "run_id": new, "original": str(result.get("original") or run_id),
                         "url": f"#/run/{layer.key}/{new}", "resumed": bool(result.get("resumed"))}
        if result.get("status") == "error":
            short = layer.shortener()
            status = 409 if result.get("reason") in INTEGRATE_REASONS else 400
            return status, {"error": f"{result.get('reason')}: {short(result.get('error') or '')}",
                            "reason": result.get("reason")}
        tail = "\n".join(stderr.strip().splitlines()[-12:])
        return 502, {"error": f"the layer's integrate.py failed (exit {exit_code})", "stderr": tail}

    def evidence(self, layer_key: str, run_id: str, kind: str) -> tuple[int, dict[str, Any]]:
        """Read-only: what "Check if it has landed" and "Find what replaced it" show (settle_evidence)."""
        layer, code, why = self._run_of(layer_key, run_id)
        if layer is None:
            return code, {"error": why}
        if kind not in EVIDENCE_KINDS:
            return 400, {"error": f"kind is one of {', '.join(EVIDENCE_KINDS)}"}
        forge = layer.ir.get("forge") if isinstance(layer.ir.get("forge"), dict) else {}
        ask = None
        if str(forge.get("kind") or "none") != "none" and not self.in_container and (layer.path / LAUNCHER).is_file():
            ask = lambda row, number: self._pr_state(layer, row, number)   # noqa: E731
        answer = settle_evidence(layer, self._layer_summaries(layer), self.landing, run_id, kind, ask)
        return 200, {"layer": layer.key, "run": run_id, "kind": kind, **answer}

    def again_request(self, layer: Layer, run_id: str, request_key: str = "") -> tuple[dict[str, Any] | None, int, str]:
        """The start request that begins a failed (or died) run again, rebuilt from its records: its
        workflow (or its command, for a single-command run), its full prompt (agents/<id>/request.md, else
        state.prompt), its class when it was fixed and its model set → (request, 0, "") or (None, code, why).
        A run already replaced is refused, unless `request_key` is the key that started its replacement
        (a double click): the launcher then answers with that run."""
        if not isinstance(run_id, str) or not RUN_ID.match(run_id) or not (layer.path / "agents" / run_id).is_dir():
            return None, 404, "unknown run"
        key = f"{layer.key}/{run_id}"
        with self.lock:
            summary = self.summaries.get(key)
        summary = summary or RunModel(layer, run_id, self.aggs, can_see_pids=not self.in_container).summary()
        if summary.get("status") not in ("failed", "died"):
            return None, 400, f"run {run_id} is {summary.get('status')}: only a failed or died run is started again"
        with self.lock:
            others = [s for s in self.summaries.values() if s["layer"] == layer.key and s.get("replaces") == run_id]
        ledger = read_json(layer.path / "agents" / "starts.json") if request_key else None   # {"version", "starts": {key: {run_id, …}}}
        starts = ledger.get("starts") if isinstance(ledger, dict) and isinstance(ledger.get("starts"), dict) else {}
        entry = starts.get(request_key) if isinstance(starts.get(request_key), dict) else {}
        mine = str(entry.get("run_id") or "")
        if others and not any(s["id"] == mine for s in others):
            newest = max(others, key=lambda s: s.get("started") or 0)
            return None, 409, f"run {run_id} is replaced by {newest['id']} already"
        state = read_json(layer.path / "agents" / run_id / "state.json")
        state = state if isinstance(state, dict) else {}
        adw = str(state.get("adw") or "")
        definition = state.get("definition") if isinstance(state.get("definition"), dict) else {}
        if layer.adw(adw) is not None:
            target = {"kind": "workflow", "name": adw}
        elif adw.startswith(COMMAND_PREFIX):
            steps = [s for s in definition.get("steps") or [] if isinstance(s, dict)]
            name = str(steps[0].get("command") or "") if steps else ""
            target = {"kind": "command", "name": name or adw[len(COMMAND_PREFIX):]}
        else:
            return None, 400, f"run {run_id} ran {adw or 'an unknown workflow'}, which the layer no longer declares"
        prompt = read_text(layer.path / "agents" / run_id / "request.md", cap=MAX_START_BODY).strip() \
            or str(state.get("prompt") or "").strip()
        if not prompt and target["kind"] == "workflow":
            return None, 400, f"run {run_id} recorded no prompt to start again"
        klass = str(state.get("klass") or "") if state.get("class_fixed") else ""
        overrides = state.get("overrides") if isinstance(state.get("overrides"), dict) else {}
        tier = str(overrides.get("tier") or "") if (overrides.get("from") or {}).get("tier") == "--model-set" else ""
        return {"target": target, "prompt": prompt, "class": klass or None,
                "model_set": tier or str(state.get("model_set") or "") or None,
                "models": {}, "issue": "none", "replaces": run_id}, 0, ""

    @staticmethod
    def layer_usage(layer: Layer, now: float) -> dict[str, Any]:
        """What the agent CLI last reported about the subscription's usage windows in this layer
        (agents/subscription-usage.json, written by the runtime). Read-only; every value is data."""
        data = read_json(layer.path / "agents" / USAGE_FILE)
        threshold = layer.usage_threshold()
        if not isinstance(data, dict):
            return {"state": "none", "billing": "", "windows": [], "threshold": threshold}
        listed = data.get("windows") if isinstance(data.get("windows"), dict) else {}
        windows = []
        for name, item in listed.items():
            if not isinstance(item, dict):
                continue
            resets = parse_time(item.get("resets_at"))
            run = item.get("run")
            windows.append({"window": str(name), "label": window_label(str(name)), "status": str(item.get("status") or ""),
                            "used": _share(item.get("used")), "resets_at": resets, "at": parse_time(item.get("at")),
                            "run": run if isinstance(run, str) and RUN_ID.match(run) else "",
                            "step": str(item.get("step") or ""), "expired": resets is not None and resets <= now})
        windows.sort(key=lambda w: (WINDOW_ORDER.index(w["window"]) if w["window"] in WINDOW_ORDER else len(WINDOW_ORDER),
                                    w["window"]))
        billing = str(data.get("billing") or "")
        return {"state": "api-key" if billing == "api-key" else ("known" if windows else "none"), "billing": billing,
                "api_key_source": str(data.get("api_key_source") or "")[:40], "windows": windows, "threshold": threshold}

    @staticmethod
    def layer_budget(layer: Layer, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The layer's limits and what its runs that started today (local date) cost so far."""
        limits = layer.budget()
        today = datetime.now().date().isoformat()
        spent = sum(float(r["cost"]) for r in runs if isinstance(r.get("cost"), (int, float))
                    and local_day(r.get("started")) == today)
        stops = sum(1 for r in runs if (r.get("budget") or {}).get("stopped") and local_day(r.get("started")) == today)
        if limits is None and not stops:
            return None
        return {**(limits or {"run": None, "step": None, "day": None}), "today": round(spent, 6), "stops_today": stops}

    # ---------------------------------------------------------------- polling
    def run_ids(self, layer: Layer) -> list[str]:
        ids = []
        try:
            for entry in os.scandir(layer.path / "agents"):
                if entry.is_dir(follow_symlinks=False) and RUN_ID.match(entry.name):
                    ids.append(entry.name)
        except OSError:
            pass
        return ids

    def tick(self) -> None:
        now = time.time()
        self.ticks += 1
        if self.tick_jobs(now) and self.workspace is not None:
            self.workspace.refresh()   # a job ended: its repository's state may have changed
        rediscover = False
        if self.workspace is not None and self.workspace.version != self.workspace_version:
            self.workspace_version = self.workspace.version
            rediscover = True
        if rediscover or now - self.last_scan > (10 if self.scan_dirs else 5) or not self.layers:
            self.last_scan = now
            if self.discover():
                self.hub.publish("layers", self.layer_list())
            if rediscover:
                self.hub.publish("workspace", self.workspace_view())
        locks = None
        with self.lock:
            layers = list(self.layers.values())
        usage_changed = False
        for layer in layers:   # the subscription usage file is layer-level: a change republishes the layers
            sig = stat_sig(layer.path / "agents" / USAGE_FILE)
            if layer.key not in self.usage_sigs:
                self.usage_sigs[layer.key] = sig
            elif self.usage_sigs[layer.key] != sig:
                self.usage_sigs[layer.key] = sig
                usage_changed = True
            sig = stat_sig(layer.path / SETTINGS_FILE)   # the local model setting: the same
            if layer.key not in self.settings_sigs:
                self.settings_sigs[layer.key] = sig
            elif self.settings_sigs[layer.key] != sig:
                self.settings_sigs[layer.key] = sig
                usage_changed = True
            both = self._standin_sig(layer)              # the stand-in's setting and its log: the same
            if layer.key not in self.standin_sigs:
                self.standin_sigs[layer.key] = both
                self.standin_view(layer)                 # asked here, outside the lock, so layer_list() finds it
            elif self.standin_sigs[layer.key] != both:
                self.standin_sigs[layer.key] = both
                self.standin_view(layer)
                usage_changed = True
        if usage_changed:
            self.hub.publish("layers", self.layer_list())
        any_running = False
        for layer in layers:
            seen = set()
            changed_layer = False
            for run_id in self.run_ids(layer):
                key = f"{layer.key}/{run_id}"
                seen.add(key)
                summary = self.summaries.get(key)
                running = summary is None or summary["status"] in {"running", "unknown"}
                if not running and now - self.checked.get(key, 0) < 4:
                    continue
                self.checked[key] = now
                files = RunFiles(layer, run_id)
                sig = files.signature(deep=running)
                if summary is not None and self.signatures.get(key) == sig and not running:
                    continue
                if summary is not None and self.signatures.get(key) == sig:
                    any_running = any_running or summary["status"] == "running"
                    seen_at = summary.get("last_seen")
                    stale = (summary["status"] == "running" and seen_at and now - seen_at > HEARTBEAT_STALE
                             and now - self.liveness_checked.get(key, 0) >= 2)
                    if not stale:
                        continue  # nothing on disk changed; elapsed time is computed by the page
                    self.liveness_checked[key] = now   # silent for a while: has its process died?
                try:
                    fresh = RunModel(layer, run_id, self.aggs, can_see_pids=not self.in_container).summary(
                        self._extras(layer, run_id))
                except Exception as exc:  # one broken run must never stop the watcher
                    fresh = {"key": key, "layer": layer.key, "id": run_id, "status": "unknown", "adw": "?",
                             "problem": f"{type(exc).__name__}: {exc}"[:300], "steps": [], "title": "", "prompt": "",
                             "started": None, "ended": None, "duration": None, "cost": None, "tokens": {},
                             "turns": 0, "current": None, "waiting": "", "container": None, "ports": [],
                             "outcome": {}, "error": "", "fanout_children": [], "artefacts": 0, "documents": 0, "mtime": now,
                             "klass": "", "dry_run": False, "source": "", "agents": 0, "cost_partial": False,
                             "failed_step": ""}
                if fresh["status"] == "running":
                    any_running = True
                if fresh["status"] not in {"running", "unknown"}:
                    sig = files.signature(deep=False)
                self.signatures[key] = sig
                if fresh != summary:
                    with self.lock:
                        self.summaries[key] = fresh
                        if fresh.get("fanout_children") != (summary or {}).get("fanout_children"):
                            self._parents_dirty = True
                    self.hub.publish("run", self._decorated(fresh))
                    changed_layer = True
            for key in [k for k in list(self.summaries) if k.startswith(layer.key + "/") and k not in seen]:
                with self.lock:
                    del self.summaries[key]
                self.signatures.pop(key, None)
                self.aggs.forget(str(layer.path / "agents" / key.split("/", 1)[1]))
                self.hub.publish("run-removed", {"key": key})
                changed_layer = True
            if (changed_layer or layer.key not in self.attention_pending or self.attention_pending[layer.key]
                    or self.ticks % ATTENTION_EVERY == 0):
                self.refresh_attention(layer)   # merged, settled or started again: a run changes bucket
            if changed_layer or layer.key not in self.slots or self.ticks % 4 == 0:
                if locks is None:
                    locks = proc_locks()
                runs = [s for s in self.summaries.values() if s["layer"] == layer.key]
                slots = layer_slots(layer, runs, locks, self.in_container)
                if slots != self.slots.get(layer.key):
                    self.slots[layer.key] = slots
                    self.hub.publish("slots", {"layer": layer.key, **slots})
        self.tick_standin(now)
        if self.probe.enabled and (any_running or self.hub.count()) and now - self.probe.last > (3 if any_running else 15):
            if self.probe.poll(want_stats=True):   # stats are only fetched for containers that are running
                self.hub.publish("containers", self.containers())
                with self.lock:
                    for key, summary in self.summaries.items():
                        layer = self.layers.get(summary["layer"])
                        if layer:
                            container = self.probe.lookup(layer, summary["id"])
                            if container:
                                self.hub.publish("run", self._decorated(summary))

    def _extras(self, layer: Layer, run_id: str) -> dict[str, Any]:
        return {}

    def _decorated(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Add what other runs and podman know: container status, fan-out parent and candidates' cost."""
        layer = self.layers.get(summary["layer"])
        extra: dict[str, Any] = {}
        container = self.probe.lookup(layer, summary["id"]) if layer else None
        if container:
            extra["container"] = {**(summary.get("container") or {}), **container}
        children = [self.summaries.get(f"{summary['layer']}/{cid}") for cid in summary.get("fanout_children") or []]
        costs = [c["cost"] for c in children if c and isinstance(c.get("cost"), (int, float))]
        if costs:
            extra["cost_children"] = round(sum(costs), 6)
        if self._parents_size != len(self.summaries) or self._parents_dirty:
            self.parents = {f"{s['layer']}/{cid}": s["id"] for s in self.summaries.values()
                            for cid in s.get("fanout_children") or []}
            self._parents_size, self._parents_dirty = len(self.summaries), False
        parent = self.parents.get(summary["key"])
        if parent:
            extra["fanout_parent"] = parent
        item = self.attention.get(summary["key"])
        if item:
            extra.update(item)
        return {**summary, **extra} if extra else summary

    def containers(self) -> dict[str, Any]:
        return self.probe.info()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            runs = [self._decorated(s) for s in self.summaries.values()]
            return {"version": VERSION, "now": time.time(), "layers": self.layer_list(),
                    "runs": sorted(runs, key=lambda r: -(r.get("started") or 0)),
                    "slots": dict(self.slots), "containers": self.containers(),
                    "in_container": self.in_container, "workspace": self.workspace_view(),
                    "al": {"found": self.al is not None, "looked": self.al_looked},
                    "actions": self.actions_state(), "jobs": self.job_list()}

    def run_detail(self, layer_key: str, run_id: str) -> dict[str, Any] | None:
        layer = self.layers.get(layer_key)
        if layer is None or not RUN_ID.match(run_id) or not (layer.path / "agents" / run_id).is_dir():
            return None
        files = RunFiles(layer, run_id)
        sig = files.signature(deep=True)
        key = f"{layer_key}/{run_id}"
        cached = self.detail_cache.get(key)
        if cached and cached[0] == sig and cached[1]["summary"]["status"] != "running":
            return {**cached[1], "summary": self._decorated(cached[1]["summary"]),
                    "standin": self.run_standin(layer, cached[1]["summary"])}
        detail = RunModel(layer, run_id, self.aggs, can_see_pids=not self.in_container).detail()
        if len(self.detail_cache) > 64:
            self.detail_cache.clear()
        self.detail_cache[key] = (sig, detail)
        # decorated on every read: the bucket and landing change without the run's files changing
        return {**detail, "summary": self._decorated(detail["summary"]), "standin": self.run_standin(layer, detail["summary"])}

    def run_standin(self, layer: Layer, summary: dict[str, Any]) -> dict[str, Any]:
        """The stand-in's mode for one run: its own --decider, else the layer's mode now."""
        if summary.get("decider"):
            return {"mode": summary["decider"], "from": "--decider"}
        view = self.standin_view(layer)
        return {"mode": str(view.get("mode") or "off"), "from": str(view.get("source") or "default")}

    def layer_detail(self, layer_key: str) -> dict[str, Any] | None:
        layer = self.layers.get(layer_key)
        if layer is None:
            return None
        with self.lock:
            runs = [{**s, **self.attention.get(s["key"], {})} for s in self.summaries.values() if s["layer"] == layer_key]
        adws = []
        for adw in layer.ir.get("adws") or []:
            if not isinstance(adw, dict):
                continue
            mine = [r for r in runs if r.get("adw") == adw.get("name")]
            adws.append({"name": adw.get("name"), "description": adw.get("description", ""),
                         "environment": adw.get("environment", []), "review": adw.get("review", ""),
                         "trigger": adw.get("trigger", []), "input": adw.get("input", []),
                         "graph": build_graph(adw, layer.ir), "stats": aggregate(mine),
                         "recent": sorted(mine, key=lambda r: -(r.get("started") or 0))[:8]})
        explain = layer.explain_file()
        opened = open_promotions(layer)
        promotions = [{"from": p.get("from"), "to": p.get("to"), "approval": p.get("approval"),
                       "approvers": p.get("approvers"), "method": p.get("method"), "tag": p.get("tag", ""),
                       "hotfix": bool(p.get("hotfix")), "origin": p.get("origin", ""),
                       "open": next((o for o in opened if o.get("from") == p.get("from") and o.get("to") == p.get("to")), None)}
                      for p in layer.ir.get("promotions") or [] if isinstance(p, dict)]
        return {"layer": next((l for l in self.layer_list() if l["key"] == layer_key), {}), "adws": adws,
                "promotions": promotions, "open_promotions": opened,
                "promote": {"available": (layer.path / PROMOTE_SCRIPT).is_file(),
                            "start": start_status(layer, self)},
                "stats": aggregate(runs), "explain": explain,
                "explain_text": read_text(layer.path / explain, cap=200_000) if explain else ""}

    def run_forever(self) -> None:
        if self.workspace is not None:
            threading.Thread(target=self.workspace.run_forever, args=(self.stop,), name="workspace", daemon=True).start()
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — keep watching whatever one tick hit
                print(f"[dashboard] watcher error: {type(exc).__name__}: {exc}", file=sys.stderr)
            self.stop.wait(max(0.1, self.poll - (time.monotonic() - started)))


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Count, pass rate, total / median / mean cost and duration over run summaries."""
    finished = [r for r in runs if r.get("status") in {"passed", "failed", "handed-over"}]
    passed = sum(1 for r in finished if r["status"] == "passed")   # a handed-over run finished, but did not pass
    costs = [float(r["cost"]) for r in runs if isinstance(r.get("cost"), (int, float))]
    durations = [float(r["duration"]) for r in finished if isinstance(r.get("duration"), (int, float))]
    return {"count": len(runs), "finished": len(finished), "passed": passed,
            "failed": sum(1 for r in finished if r["status"] == "failed"),
            "handed_over": sum(1 for r in finished if r["status"] == "handed-over"),
            "running": sum(1 for r in runs if r.get("status") == "running"),
            "needs_you": sum(1 for r in runs if r.get("bucket") == "needs-you"),
            "pass_rate": (passed / len(finished)) if finished else None,
            "cost_total": round(sum(costs), 6) if costs else 0.0,
            "cost_median": statistics.median(costs) if costs else None,
            "cost_mean": statistics.fmean(costs) if costs else None,
            "duration_median": statistics.median(durations) if durations else None,
            "duration_mean": statistics.fmean(durations) if durations else None,
            "duration_total": sum(durations) if durations else 0.0}


# ============================================================================ safe file access

class Refused(Exception):
    pass


def safe_layer_file(layer: Layer, relative: str) -> Path:
    """Resolve a path inside a layer for serving. Allowed: agents/, artefacts/, specs/, the layer
    definition and its explanation file. Rejects traversal, absolute paths, NUL, and anything whose
    real path (symlinks resolved) leaves the allowed tree."""
    if not relative or "\x00" in relative or relative.startswith(("/", "\\")) or "\\" in relative:
        raise Refused("bad path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise Refused("bad path")
    root = Path(os.path.realpath(layer.path))
    allowed_file = relative in DEFINITION_FILES or relative == layer.explain_file()
    if not allowed_file and parts[0] not in SERVED_TREES:
        raise Refused("not served")
    target = Path(os.path.realpath(root / relative))
    base = root / relative if allowed_file else root / parts[0]
    if allowed_file:
        if target != base:
            raise Refused("symlink escape")
    else:
        if os.path.realpath(base) != str(base) or not str(target).startswith(str(base) + os.sep):
            raise Refused("outside the layer")
    if not target.is_file():
        raise Refused("not found")
    return target


_PATCH_STEP = re.compile(r"^patch-\d+$")
_VERDICT_DIR = re.compile(r"^review-\d+(-\d+)?$")


def document_path(layer: Layer, recorded: Any) -> str | None:
    """A path the runtime recorded (absolute on the host, possibly under another mount such as a
    container's, or already layer-relative) → the layer-relative POSIX path, or None when it is
    not one of the layer's served trees."""
    if not isinstance(recorded, str) or not recorded or len(recorded) > 1024:
        return None
    if any(c in recorded for c in "\n\r\x00"):
        return None
    text = recorded.strip()
    if not text or "\\" in text:
        return None
    relative: str | None = None
    if not text.startswith("/"):
        relative = text
    else:
        for root in (os.path.realpath(layer.path), str(layer.path)):
            if text.startswith(root + os.sep):
                relative = text[len(root) + 1:]
                break
        if relative is None:
            parts = text.split("/")
            index = next((i for i in range(len(parts) - 1, 0, -1)
                          if parts[i] in SERVED_TREES and parts[i - 1] == layer.path.name), None)
            if index is None:
                return None
            relative = "/".join(parts[index:])
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts) or parts[0] not in SERVED_TREES or len(parts) < 2:
        return None
    return relative


def _doc_type(suffix: str) -> str:
    return {".md": "markdown", ".json": "json"}.get(suffix, "text")


def run_documents(layer: Layer, run_id: str, state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every document a run has produced so far, in a stable order and without duplicates: its plan,
    the text outputs its steps recorded, review patch plans, PR.md and other text in agents/<id>/,
    reviewer verdicts and text artefacts. Each must be a regular file inside specs/ or this run's own
    agents/<id>/ or artefacts/<id>/ (symlinks out of the layer are not listed)."""
    state = state if isinstance(state, dict) else {}
    outputs = state.get("outputs") if isinstance(state.get("outputs"), dict) else {}
    candidates: list[tuple[Any, str, str, str, str]] = []   # (recorded, step, kind, name, filename)
    plan = state.get("plan")
    if isinstance(plan, str) and plan.strip():
        step = next((str(k) for k, v in outputs.items() if isinstance(v, str) and v.strip() == plan.strip()), "plan")
        candidates.append((plan, step, "plan", "", ""))
    for step, value in outputs.items():
        relative = document_path(layer, value)
        if relative and relative.endswith(DOC_SUFFIXES):
            candidates.append((relative, str(step), "patch" if _PATCH_STEP.match(str(step)) else "output", "", ""))
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    for item in review.get("rounds") or []:
        patch = item.get("patch") if isinstance(item, dict) else None
        if isinstance(patch, dict) and patch.get("patch_plan"):
            candidates.append((patch["patch_plan"], "review", "patch", "", ""))
    ship = state.get("ship") if isinstance(state.get("ship"), dict) else {}
    candidates.append((f"agents/{run_id}/PR.md", str(ship.get("stopped_at") or "ship"), "pr", "", ""))
    run_dir = layer.path / "agents" / run_id
    try:
        entries = sorted(os.scandir(run_dir), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if entry.name in {"state.json", "run.log"} or not entry.name.endswith(DOC_SUFFIXES):
            continue
        if entry.is_file(follow_symlinks=False):
            candidates.append((f"agents/{run_id}/{entry.name}", "", "run", "", ""))
    for entry in entries:
        if _VERDICT_DIR.match(entry.name) and entry.is_dir(follow_symlinks=False):
            candidates.append((f"agents/{run_id}/{entry.name}/output.txt", "review", "verdict",
                               f"{entry.name} verdict", f"{entry.name}-output.txt"))
    for item in state.get("artefacts") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].endswith(DOC_SUFFIXES):
            candidates.append((item["path"], str(item.get("step") or ""), "artefact", "", ""))
    scripts = state.get("scripts") if isinstance(state.get("scripts"), dict) else {}
    for step, record in scripts.items():   # inner steps' prompts and outputs copied from a script's own journal
        for item in (record.get("documents") if isinstance(record, dict) else None) or []:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                name = "/".join(str(item.get(k) or "") for k in ("inner", "file") if item.get(k))
                candidates.append((item["path"], str(step), "inner", name, name.replace("/", "-")))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for recorded, step, kind, name, filename in candidates:
        relative = document_path(layer, recorded)
        if relative is None or relative in seen:
            continue
        parts = relative.split("/")
        if parts[0] != "specs" and parts[1] != run_id:
            continue
        try:
            target = safe_layer_file(layer, relative)
            st = target.stat()
        except (Refused, OSError):
            continue
        seen.add(relative)
        suffix = os.path.splitext(relative)[1].lower()
        out.append({"path": relative, "name": name or parts[-1], "filename": filename or parts[-1], "step": step,
                    "kind": kind, "bytes": st.st_size, "mtime": st.st_mtime,
                    "viewable": suffix in DOC_SUFFIXES and st.st_size <= DOC_VIEW_CAP, "type": _doc_type(suffix)})
    return out


def run_document(layer: Layer, run_id: str, relative: str) -> tuple[dict[str, Any], Path] | None:
    """The listed document `relative` of a run and its file, or None when the run does not list it."""
    if not RUN_ID.match(run_id or "") or not isinstance(relative, str):
        return None
    state = read_json(layer.path / "agents" / run_id / "state.json")
    entry = next((e for e in run_documents(layer, run_id, state if isinstance(state, dict) else None)
                  if e["path"] == relative), None)
    if entry is None:
        return None
    try:
        return entry, safe_layer_file(layer, relative)
    except Refused:
        return None


INLINE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                ".webp": "image/webp", ".webm": "video/webm", ".mp4": "video/mp4", ".ogg": "video/ogg",
                ".wav": "audio/wav", ".mp3": "audio/mpeg", ".json": "application/json; charset=utf-8",
                ".jsonl": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
                ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                ".xml": "text/plain; charset=utf-8", ".rkt": "text/plain; charset=utf-8",
                ".html": "text/html; charset=utf-8"}   # a promotion's presentation pack: sandboxed, no script
# what a served page (a promotion's pack) may load: its own pictures, recordings and inline style. Still
# sandboxed with no script at all; allow-same-origin only lets it fetch its own media from this dashboard.
HTML_CSP = ("sandbox allow-same-origin; default-src 'none'; img-src 'self'; media-src 'self'; "
            "style-src 'unsafe-inline'")


# ============================================================================ workspace: repositories beside each other

KIT_AL = HERE.parent.parent / "tools" / "al"   # the al tool of the checkout this kit belongs to
WS_KEY_BAD = re.compile(r"[\s\x00-\x1f\x7f]")


def home_short(text: str) -> str:
    """A path as shown: the home directory as `~`."""
    home = str(Path.home())
    if home and home != "/" and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def utc_stamp(when: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if when is None else when, timezone.utc).isoformat().replace("+00:00", "Z")


def find_al_tool(explicit: Path | None) -> tuple[Path | None, list[str]]:
    """The al tool: --al PATH, else $AL_TOOL, else tools/al of the checkout the kit belongs to, else
    tools/al in a folder above it (a layer generated inside al's own repository has its dashboard
    several folders below the checkout), else `al` on PATH.
    → (the first executable one, every place looked at, the home directory shown as ~)."""
    looked: list[str] = []
    named = [Path(explicit)] if explicit is not None else []
    if os.environ.get("AL_TOOL"):
        named.append(Path(os.environ["AL_TOOL"]).expanduser())
    for candidate in named + [KIT_AL]:
        looked.append(home_short(str(candidate)))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return Path(os.path.realpath(candidate)), looked
    looked.append("tools/al in a folder above it")
    for folder in KIT_AL.parent.parent.parents:
        candidate = folder / "tools" / "al"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return Path(os.path.realpath(candidate)), looked
    looked.append("PATH")
    found = shutil.which("al")
    return (Path(os.path.realpath(found)), looked) if found else (None, looked)


@dataclass
class Detectors:
    detect: Callable[..., Any]
    detect_suite: Callable[..., Any]
    render_section: Callable[..., str]
    render_suite: Callable[..., str]


def load_detectors(al: Path | None) -> Detectors | None:
    """`al scout`'s own detection (tools/al_adopt.py, tools/al_suite.py: standard library only, they
    read files and execute nothing), from beside the al tool or the kit's checkout; None without it."""
    import importlib.util
    for tools in dict.fromkeys(([al.parent] if al else []) + [KIT_AL.parent]):
        if not ((tools / "al_adopt.py").is_file() and (tools / "al_suite.py").is_file()):
            continue
        added = str(tools) not in sys.path
        if added:
            sys.path.insert(0, str(tools))
        try:
            modules = {}
            for name in ("al_suite", "al_adopt"):
                source = tools / f"{name}.py"
                loaded = sys.modules.get(name)
                if loaded is not None and os.path.realpath(getattr(loaded, "__file__", "") or "") == os.path.realpath(source):
                    modules[name] = loaded
                    continue
                spec = importlib.util.spec_from_file_location(name, source)
                if spec is None or spec.loader is None:
                    raise ImportError(name)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                modules[name] = module
            return Detectors(modules["al_adopt"].detect, modules["al_suite"].detect_suite,
                             modules["al_adopt"].render_section, modules["al_suite"].render_suite)
        except Exception as exc:  # noqa: BLE001 — without them the states are only layer / not checked
            print(f"[dashboard] cannot load al's detection from {home_short(str(tools))}: {exc}", file=sys.stderr)
        finally:
            if added and str(tools) in sys.path:
                sys.path.remove(str(tools))
    return None


def load_tool_finder(al: Path | None) -> Any:
    """tools/al_tools.py (where al finds racket, uv, git and the agent CLI; standard library only, it
    reads files and runs only `--version`), from beside the al tool or the kit's checkout; None without it."""
    import importlib.util
    for tools in dict.fromkeys(([al.parent] if al else []) + [KIT_AL.parent]):
        source = tools / "al_tools.py"
        if not source.is_file():
            continue
        loaded = sys.modules.get("al_tools")
        if loaded is not None and os.path.realpath(getattr(loaded, "__file__", "") or "") == os.path.realpath(source):
            return loaded
        try:
            spec = importlib.util.spec_from_file_location("al_tools", source)
            if spec is None or spec.loader is None:
                raise ImportError("al_tools")
            module = importlib.util.module_from_spec(spec)
            previous = sys.modules.get("al_tools")
            sys.modules["al_tools"] = module          # its dataclasses look their module up while it loads
            try:
                spec.loader.exec_module(module)
            except BaseException:
                if previous is not None:
                    sys.modules["al_tools"] = previous
                else:
                    sys.modules.pop("al_tools", None)
                raise
            return module
        except Exception as exc:  # noqa: BLE001 — without it jobs run under the dashboard's PATH as is
            print(f"[dashboard] cannot load al's tool finder from {home_short(str(tools))}: {exc}", file=sys.stderr)
    return None


def safe_git(repo: Path, *args: str, timeout: float | None = None, errors: list[str] | None = None,
             input: bytes | None = None) -> str | None:  # noqa: A002 — subprocess's own name
    """git in a repository that may be hostile: no fsmonitor, untracked cache or hooks from its config,
    no optional locks (the index is never rewritten), no prompt, its own process group killed after
    `timeout` seconds; `input` is its stdin (e.g. a diff for patch-id). → stdout, or None on an error
    or a timeout (recorded in `errors`)."""
    limit = WORKSPACE_GIT_TIMEOUT if timeout is None else timeout
    argv = ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null",
            "--no-optional-locks", "-C", str(repo), *args]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError:
        if errors is not None:
            errors.append("git not found")
        return None
    try:
        out, _ = proc.communicate(input=input, timeout=limit)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=2)
        if errors is not None:
            errors.append("git timed out")
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="surrogateescape")


def git_dir(repo: Path) -> Path:
    """The repository's git directory (.git, or where a worktree's .git file points)."""
    dot = repo / ".git"
    if dot.is_file() and not dot.is_symlink():
        text = read_text(dot, cap=4096).strip()
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:"):].strip())
            return target if target.is_absolute() else repo / target
    return dot


def repo_signature(repo: Path, layer_dir: Path | None = None) -> tuple:
    """Cheap: stats of what changes when a repository's al state can change (a commit, the index, a
    sibling definition, the layer's generated IR) and of the repository's top directory."""
    gd = git_dir(repo)
    paths = [repo, gd / "HEAD", gd / "index", gd / "logs" / "HEAD", repo.parent / f"{repo.name}.al" / "agentic.rkt"]
    if layer_dir is not None:
        paths += [layer_dir / LAYER_FILE, layer_dir / "agentic.rkt"]
    return tuple(stat_sig(p) for p in paths)


def confirm_signature(repo: Path, layer: Path) -> tuple:
    """What a confirmation is bound to: the commit, the branch, the working tree's status and the layer's
    definition. Read with safe_git (which never rewrites the index), so a preview cannot change it."""
    return (safe_git(repo, "rev-parse", "-q", "--verify", "HEAD"), safe_git(repo, "symbolic-ref", "-q", "HEAD"),
            hashlib.sha256((safe_git(repo, "status", "--porcelain", "-z", "--untracked-files=normal") or "").encode(
                "utf-8", errors="surrogateescape")).hexdigest(),
            stat_sig(layer / "agentic.rkt"), stat_sig(layer / LAYER_FILE))


def scan_root(root: Path, depth: int) -> list[Path]:
    """Git repositories below `root`, at most `depth` levels down; a repository is not looked into.
    Symlinked entries, PRUNE_DIRS, hidden directories and a sibling layer `X.al` beside `X` are
    skipped, and nothing whose real path leaves the root is listed."""
    base = os.path.realpath(root)
    found: list[Path] = []

    def walk(directory: str, level: int) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        names = {e.name for e in entries}
        for entry in entries:
            if len(found) >= WORKSPACE_REPO_CAP:
                return
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name in PRUNE_DIRS or entry.name.startswith("."):
                continue
            if entry.name.endswith(".al") and entry.name[:-3] in names:
                continue
            real = os.path.realpath(entry.path)
            if not real.startswith(base + os.sep):
                continue
            if os.path.lexists(os.path.join(entry.path, ".git")):
                found.append(Path(entry.path))
            elif level + 1 < depth:
                walk(entry.path, level + 1)

    walk(base, 0)
    return found


@dataclass
class RepoEntry:
    key: str
    name: str
    path: Path
    root: Path
    state: str = "none"                 # layer | setup | none
    branch: str = ""
    dirty: bool = False
    last_activity: float | None = None
    layer_dir: Path | None = None
    layer_kind: str = ""                # sibling | in-repo
    layer_generated: bool = False
    suite: str = ""                     # the hand-built suite's directory, when there is one
    setup: list[str] = field(default_factory=list)   # rules / prompts / workflows / suite found
    summary: str = ""                   # al scout's "Existing agent setup" section
    setup_checked: bool = True          # False: al's detection was not available
    files: int = 0
    truncated: bool = False
    error: str = ""


def classify_repo(repo: Path, root: Path, detectors: Detectors | None, key: str = "") -> RepoEntry:
    """A repository's al state, read-only and bounded: its tracked files (at most WORKSPACE_FILE_CAP,
    every one whose real path leaves the repository dropped) through al scout's detection."""
    entry = RepoEntry(key=key or repo.name, name=repo.name, path=repo, root=root)
    errors: list[str] = []
    real_repo = os.path.realpath(repo)
    listed = safe_git(repo, "ls-files", "-z", errors=errors)
    files: list[str] = []
    if listed is not None:
        names = [f for f in listed.split("\0") if f]
        entry.truncated = len(names) > WORKSPACE_FILE_CAP
        for rel in names[:WORKSPACE_FILE_CAP]:
            real = os.path.realpath(os.path.join(real_repo, rel))
            if real.startswith(real_repo + os.sep):
                files.append(rel)
    entry.files = len(files)
    sibling = repo.parent / f"{repo.name}.al"
    if not sibling.is_symlink() and (sibling / "agentic.rkt").is_file():
        entry.layer_dir, entry.layer_kind = sibling, "sibling"
    else:
        definitions = [f for f in files if f == "agentic.rkt" or f.endswith("/agentic.rkt")]
        default = os.path.join(real_repo, "agentic", "agentic.rkt")
        if "agentic/agentic.rkt" not in definitions and os.path.isfile(default) \
                and os.path.realpath(default).startswith(real_repo + os.sep):
            definitions.append("agentic/agentic.rkt")   # an in-repo layer not committed yet
        definitions.sort(key=lambda f: (f.count("/"), len(f), f))
        generated = [f for f in definitions if (repo / f).parent.joinpath(LAYER_FILE).is_file()]
        chosen = (generated or definitions)[:1]
        if chosen:
            entry.layer_dir, entry.layer_kind = (repo / chosen[0]).parent, "in-repo"
    if entry.layer_dir is not None:
        entry.layer_generated = (entry.layer_dir / LAYER_FILE).is_file()
    if detectors is None:
        entry.setup_checked = False
    elif listed is not None:
        try:
            items = detectors.detect(repo, files)
            suite = detectors.detect_suite(repo, files)
        except Exception as exc:  # noqa: BLE001 — one odd repository must not stop the scan
            items, suite = [], None
            errors.append(f"detection failed: {type(exc).__name__}: {exc}"[:200])
        entry.setup = sorted({str(it.get("category")) for it in items if it.get("category") in ("rules", "prompts", "workflows")})
        if suite:
            entry.setup.append("suite")
            entry.suite = str(suite.get("root") or "")
        entry.summary = detectors.render_section(items) + ("\n\n" + detectors.render_suite(suite) if suite else "")
    entry.state = "layer" if entry.layer_dir is not None else ("setup" if entry.setup else "none")
    if not errors:
        head = safe_git(repo, "symbolic-ref", "--short", "-q", "HEAD", errors=errors)
        entry.branch = head.strip() if head and head.strip() else "detached"
    if not errors:
        status = safe_git(repo, "status", "--porcelain", "-z", "--untracked-files=normal", errors=errors)
        entry.dirty = bool(status)
    times = [t for t in [mtime(git_dir(repo) / "index")] if t]
    if not errors:
        log = (safe_git(repo, "log", "-1", "--format=%ct", errors=errors) or "").strip()
        if log.isdigit():
            times.append(float(log))
    entry.last_activity = max(times) if times else None
    entry.error = errors[0] if errors else ""
    return entry


def repo_key(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "repo"
    key, suffix = base, 2
    while key in taken:
        key, suffix = f"{base}-{suffix}", suffix + 1
    return key


class Workspace:
    """The git repositories in one or more folders, each with its al state; cached per repository by
    `repo_signature`, rescanned every WORKSPACE_RESCAN seconds and on request."""

    def __init__(self, roots: Iterable[Path], depth: int = WORKSPACE_DEPTH, detectors: Detectors | None = None) -> None:
        self.roots = list(dict.fromkeys(Path(os.path.realpath(r)) for r in roots))
        self.depth = max(1, int(depth))
        self.detectors = detectors
        self.entries: dict[str, RepoEntry] = {}
        self.sigs: dict[str, tuple] = {}
        self.lock = threading.RLock()
        self.refreshing = threading.Lock()
        self.scanned_at: float | None = None
        self.scans = 0
        self.version = 0
        self.last_requested = -math.inf

    def refresh(self, force: bool = False) -> bool:
        """Rescan; a repository whose signature did not change keeps its entry unless `force`. → changed?"""
        with self.refreshing:
            found: list[tuple[Path, Path]] = []
            for root in self.roots:
                found += [(root, repo) for repo in scan_root(root, self.depth)]
            found = found[:WORKSPACE_REPO_CAP]
            with self.lock:
                old_entries = dict(self.entries)
            by_path = {str(e.path): e for e in old_entries.values()}
            taken: set[str] = set()
            keys: dict[str, str] = {}
            for _root, repo in found:   # a repository keeps its key while it exists
                old = by_path.get(str(repo))
                if old and old.key not in taken:
                    keys[str(repo)] = old.key
                    taken.add(old.key)
            for _root, repo in found:
                if str(repo) not in keys:
                    keys[str(repo)] = repo_key(repo.name, taken)
                    taken.add(keys[str(repo)])
            fresh: dict[str, RepoEntry] = {}
            sigs: dict[str, tuple] = {}
            changed = False
            for root, repo in found:
                old = by_path.get(str(repo))
                key = keys[str(repo)]
                sig = repo_signature(repo, old.layer_dir if old else None)
                if old is not None and not force and self.sigs.get(str(repo)) == sig and old.key == key:
                    entry = old
                else:
                    entry = classify_repo(repo, root, self.detectors, key)
                    sig = repo_signature(repo, entry.layer_dir)
                    changed = changed or entry != old
                fresh[key] = entry
                sigs[str(repo)] = sig
            changed = changed or set(fresh) != set(old_entries)
            with self.lock:
                self.entries, self.sigs = fresh, sigs
                self.scanned_at = time.time()
                self.scans += 1
                if changed:
                    self.version += 1
            return changed

    def request_refresh(self) -> bool:
        """A rescan asked for by the page: at most one per WORKSPACE_REFRESH_MIN seconds. → rescanned?"""
        now = time.monotonic()
        with self.lock:
            if now - self.last_requested < WORKSPACE_REFRESH_MIN:
                return False
            self.last_requested = now
        self.refresh(force=True)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.wait(WORKSPACE_RESCAN):
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 — keep scanning
                print(f"[dashboard] workspace scan error: {type(exc).__name__}: {exc}", file=sys.stderr)

    def layer_paths(self) -> list[Path]:
        with self.lock:
            return [Path(os.path.realpath(e.layer_dir)) for e in self.entries.values()
                    if e.layer_dir is not None and (e.layer_dir / LAYER_FILE).is_file()]

    def as_list(self) -> list[RepoEntry]:
        with self.lock:
            return sorted(self.entries.values(), key=lambda e: (e.name.lower(), e.key))

    def entry(self, key: str) -> RepoEntry | None:
        with self.lock:
            return self.entries.get(key)

    def inside(self, path: str) -> bool:
        return any(path.startswith(str(root) + os.sep) for root in self.roots)


# ============================================================================ actions and jobs

@dataclass
class Step:
    name: str
    argv: list[str]
    doc: str | None = None
    timeout: int = 1800
    stream: str = ""     # "agent": its stdout is an agent's event stream, condensed into a journal


@dataclass
class ActionConfig:
    al: Path
    author_flags: list[str]
    choices: dict[str, str] = field(default_factory=dict)    # --choice KEY=VALUE of the author step
    placement: str = "beside"                                # one of PLACEMENTS: where create and adopt put the layer


# What the start form may send as `choices` (KEY → allowed values, a comma-separated list of them, or a
# pattern): the same as `CHOICES` in tools/al_author.py (a test keeps the two equal; this file stays
# standard library only and needs no al tool to be imported).
CHOICE_RULES: dict[str, tuple[tuple[str, ...], bool, str]] = {
    "forge": (("none", "github", "forgejo"), False, ""),
    "host": ((), False, r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?"),
    "repo": ((), False, r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
    "input": (("tasklist", "issue"), False, ""),
    "trigger": (("manual", "cron", "webhook", "forge-actions"), True, ""),
    "environment": (("worktree", "podman", "nix-devshell"), True, ""),
    "review": (("human", "agent"), False, ""),
    "delivery": (("trunk", "sprint", "gitflow", "release-train", "phase-gate", "none"), False, ""),
}
CHOICE_ACTIONS = ("create", "adopt")


def choice_problem(key: Any, value: Any) -> str:
    """Why one choice of the start form is refused, or ""."""
    if not isinstance(key, str) or key not in CHOICE_RULES:
        return f"unknown choice {key!r}; the choices are: {', '.join(CHOICE_RULES)}"
    if not isinstance(value, str):
        return f"the choice {key} must be a string"
    values, multi, pattern = CHOICE_RULES[key]
    if pattern:
        return "" if re.fullmatch(pattern, value) else f"{key}: not a valid value"
    parts = value.split(",") if multi else [value]
    if not parts or any(v not in values for v in parts):
        return f"{key}: {'each of ' if multi else ''}one of {', '.join(values)}"
    return ""


def _agent_flag(c: ActionConfig) -> list[str]:
    flags = c.author_flags
    return ["--agent", flags[flags.index("--agent") + 1]] if "--agent" in flags[:-1] else []


def _choice_args(choices: dict[str, str]) -> list[str]:
    return [arg for key in sorted(choices) for arg in ("--choice", f"{key}={choices[key]}")]


def placement_layer(app: Path, placement: str) -> Path:
    """Where a new layer of the repository `app` goes: `app/agentic` inside it, else the sibling `<name>.al`."""
    return app / IN_REPO_DIR if placement == "inside" else app.parent / f"{app.name}.al"


def layer_of(entry: RepoEntry, placement: str = "beside") -> Path:
    """The layer an action works on: the repository's layer, else the one al creates for `placement`
    (the sibling `<name>.al`, or `<repository>/agentic`)."""
    if entry.layer_dir is not None:
        return Path(os.path.realpath(entry.layer_dir))
    return placement_layer(Path(os.path.realpath(entry.path)), placement)


PLACEMENT_TEXT = {
    "beside": ("beside the repository", "A folder next to the repository. Nothing is written into the repository."),
    "inside": ("inside the repository", "The folder agentic in the repository, committed with the code, so every clone "
                                        "has the layer. Run state stays in git-ignored folders of it."),
}


def placement_view(app: Path, placement: str, shown: Callable[[Path], str]) -> dict[str, Any]:
    """One placement of a new layer for the repository `app`, as the page shows it and as the server
    enforces it: the path, whether it exists, is writable, is already a layer, and the first reason it
    cannot be used (`problem`, "" when `ok`)."""
    layer = placement_layer(app, placement)
    path = shown(layer)
    exists = os.path.lexists(layer)
    folder = layer if exists else layer.parent
    writable = folder.is_dir() and os.access(folder, os.W_OK | os.X_OK)
    is_layer = (layer / "agentic.rkt").exists() or (layer / "agentic.rkt").is_symlink() or \
        os.path.lexists(layer / LAYER_FILE)
    problem = definition_problem(app, layer, shown) or ""
    if not problem and is_layer:
        problem = f"{path} is already a layer; it is never overwritten"
    if not problem and exists and (layer.is_symlink() or not layer.is_dir()):
        problem = f"{path} exists and is not a directory"
    if not problem and not writable:
        problem = f"{shown(folder)} is not writable"
    if not problem and placement == "inside":
        tracked = safe_git(app, "ls-files", "-z", "--", IN_REPO_DIR)
        if tracked is None:
            problem = f"cannot list the files of {app.name} (git failed or timed out)"
        elif tracked.strip("\0"):
            problem = f"{path} already has tracked files of the repository; choose beside"
    label, explain = PLACEMENT_TEXT[placement]
    return {"name": placement, "label": label, "explain": explain, "path": path, "exists": exists,
            "writable": writable, "layer": is_layer, "ok": not problem, "problem": problem}


def _steps_scout(e: RepoEntry, c: ActionConfig, docs: Path) -> list[Step]:
    return [Step("scout", [str(c.al), "scout", os.path.realpath(e.path)], "LLM.txt", 300)]


def _steps_create(e: RepoEntry, c: ActionConfig, docs: Path) -> list[Step]:
    """Create and adopt run the same sequence as `al create` / `al adopt-all` (tools/al_create.py; a test
    keeps the two equal): init, scout, adopt (a no-op without an agent setup), author, gen, check, result."""
    al, app, layer = str(c.al), os.path.realpath(e.path), str(layer_of(e, c.placement))
    return [Step("init", [al, "init", app, *(["--in-repo"] if c.placement == "inside" else [])], None, 300),
            Step("scout", [al, "scout", app, "--into", layer, *_agent_flag(c), "--raw"], None, 1800, "agent"),
            Step("adopt", [al, "adopt", layer, "--app", app], None, 600),
            Step("author", [al, "author", layer, *c.author_flags, "--raw", *_choice_args(c.choices)], None, 3600, "agent"),
            Step("gen", [al, "gen", layer], None, 600), Step("check", [al, "check", layer], None, 600),
            Step("result", [al, "result", layer, "--json"], "result.json", 120)]


def _steps_choices(e: RepoEntry, c: ActionConfig, docs: Path) -> list[Step]:
    """The start form's preview: the choices the facts lead to (value, options, the fact), read-only."""
    return [Step("choices", [str(c.al), "create", os.path.realpath(e.path), "--choices", "--json"], "choices.json", 300)]


def _steps_adopt_preview(e: RepoEntry, c: ActionConfig, docs: Path) -> list[Step]:
    return [Step("adopt-dry-run", [str(c.al), "adopt", str(layer_of(e, c.placement)), "--app", os.path.realpath(e.path), "--dry-run",
                                   "--diff"], "preview.diff.txt", 300), *_steps_choices(e, c, docs)]


_steps_adopt = _steps_create


def _one(name: str, *extra: str, doc: str | None = None, timeout: int = 600):
    def steps(e: RepoEntry, c: ActionConfig, docs: Path) -> list[Step]:
        return [Step(name, [str(c.al), name, str(layer_of(e)), *extra], doc, timeout)]
    return steps


def _steps_explain(e: RepoEntry, c: ActionConfig, docs: Path) -> list[Step]:
    return [Step("explain", [str(c.al), "explain", str(layer_of(e)), "--md", str(docs / "EXPLAIN.md")], None, 300)]


@dataclass
class Action:
    states: tuple[str, ...]
    writes: bool
    description: str
    steps: Callable[[RepoEntry, ActionConfig, Path], list[Step]]
    writes_paths: Callable[[RepoEntry, ActionConfig], list[Path | str]] = lambda e, c: []
    preview: Callable[[RepoEntry, ActionConfig, Path], list[Step]] | None = None
    definition: bool = False     # writes agentic.rkt: refused when any definition exists


# The allow-list: a fixed name → the fixed argv lists it runs, built from the scanned repository only.
ACTIONS: dict[str, Action] = {
    "scout": Action(("layer", "setup", "none"), False, "draft LLM.txt facts of the repository (read-only)", _steps_scout),
    "create": Action(("none", "setup"), True, "create a layer for the repository (beside or inside it) and have its "
                     "definition written",
                     _steps_create, lambda e, c: [layer_of(e, c.placement)], preview=_steps_choices, definition=True),
    "adopt": Action(("setup",), True, "create a layer that adopts the repository's existing agent setup",
                    _steps_adopt, lambda e, c: [layer_of(e, c.placement), layer_of(e, c.placement) / "adopted",
                                                layer_of(e, c.placement) / "templates" / "custom"],
                    preview=_steps_adopt_preview, definition=True),
    "gen": Action(("layer",), True, "compile the definition and generate the layer's files",
                  _one("gen"), lambda e, c: [layer_of(e)]),
    "check": Action(("layer",), False, "verify the generated layer (read-only)", _one("check")),
    "verify": Action(("layer",), True, "run the layer's codebase cmds on a throwaway worktree",
                     _one("verify", "--json", doc="verify.json", timeout=3600),
                     lambda e, c: [layer_of(e) / "agents" / "verify", "a throwaway git worktree (removed afterwards)"]),
    "explain": Action(("layer",), False, "explain the layer definition in plain language (read-only)", _steps_explain),
}


def display_argv(argv: list[str], shown: Callable[[Path], str], docs: Path | None = None) -> str:
    """A step's command line as shown: `al …`, paths relative to the workspace (the home directory as ~)."""
    out = ["al"]
    for arg in argv[1:]:
        if docs is not None and arg.startswith(str(docs) + os.sep):
            out.append("<job>/docs/" + arg[len(str(docs)) + 1:])
        elif arg.startswith("/"):
            out.append(shown(Path(arg)))
        else:
            out.append(arg)
    return " ".join(shlex.quote(a) if not a.startswith("<job>") else a for a in out)


def definition_problem(app: Path, layer: Path, shown: Callable[[Path], str]) -> str | None:
    """Why `create` / `adopt` may not write agentic.rkt here, or None: any definition, in the layer
    they would write or anywhere in the repository (committed or not), is never overwritten."""
    if (layer / "agentic.rkt").exists() or (layer / "agentic.rkt").is_symlink():
        return f"{shown(layer / 'agentic.rkt')} already exists; it is never overwritten"
    listed = safe_git(app, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is None:
        return f"cannot list the files of {app.name} (git failed or timed out)"
    found = sorted((f for f in listed.split("\0") if f == "agentic.rkt" or f.endswith("/agentic.rkt")),
                   key=lambda f: (f.count("/"), f))
    if not found:
        return None
    status = safe_git(app, "status", "--porcelain", "-z", "--untracked-files=all", "--", *found) or ""
    dirty = {item[3:] for item in status.split("\0") if len(item) > 3}
    for rel in found:
        if rel in dirty:
            return f"{app.name}/{rel} has uncommitted changes; a definition is never overwritten"
    return f"{app.name}/{found[0]} already exists; it is never overwritten"


def default_jobs_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "al" / "dashboard-jobs"


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def lock_file(jobs_root: Path, repo_path: str) -> Path:
    """The per-repository lock every job runner holds while it runs."""
    return jobs_root / "locks" / (hashlib.sha1(os.path.realpath(repo_path).encode()).hexdigest() + ".lock")


def lock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def job_liveness(job: dict[str, Any], now: float) -> dict[str, Any] | None:
    """For a job that says `running`: has its runner died? Like a run's `liveness`, except that a
    runner on this host whose pid is gone is dead at once."""
    if not isinstance(job, dict) or job.get("status") != "running":
        return None
    seen = parse_time(job.get("heartbeat"))
    pid = job.get("pid") if isinstance(job.get("pid"), int) else 0
    if pid > 0 and job.get("host") == socket.gethostname() and pid_alive(pid) is False:
        return {"died": True, "last_seen": seen}
    if seen is None:
        return {"died": True, "last_seen": None}
    if pid <= 0:
        return {"died": now - seen > HEARTBEAT_STALE, "last_seen": seen}
    return liveness(job, now)


def layer_files_snapshot(layer: Path) -> dict[str, tuple[int, int]]:
    """rel → (size, mtime_ns) of a layer's files, without agents/, trees/ and .git; at most 20 000."""
    out: dict[str, tuple[int, int]] = {}
    if not layer.is_dir():
        return out
    for directory, dirs, files in os.walk(layer):
        rel_dir = os.path.relpath(directory, layer)
        dirs[:] = sorted(d for d in dirs if d != ".git" and not (rel_dir == "." and d in ("agents", "trees"))
                         and not os.path.islink(os.path.join(directory, d)))
        for name in sorted(files):
            path = os.path.join(directory, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            out[os.path.normpath(os.path.join(rel_dir, name))] = (info.st_size, info.st_mtime_ns)
            if len(out) >= WORKSPACE_FILE_CAP:
                return out
    return out


def changed_files(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]], prefix: str) -> list[dict[str, str]]:
    out = []
    for rel in sorted(set(before) | set(after)):
        change = "added" if rel not in before else "removed" if rel not in after else \
            "modified" if before[rel] != after[rel] else ""
        if change:
            out.append({"path": f"{prefix}/{rel}" if prefix else rel, "change": change})
    return out


def repo_status(repo: Path) -> list[str]:
    return sorted(item for item in (safe_git(repo, "status", "--porcelain", "-z", "--untracked-files=all") or "").split("\0")
                  if item)


class Jobs:
    """Recorded actions: <root>/<id>/{job.json, output.log, docs/}; <root>/locks/ holds the per-repository locks."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def allocate(self) -> tuple[str, Path]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "locks").mkdir(exist_ok=True, mode=0o700)
        while True:
            job_id = secrets.token_hex(4)
            try:
                (self.root / job_id).mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        (self.root / job_id / "docs").mkdir()
        (self.root / job_id / "output.log").touch()
        return job_id, self.root / job_id

    def dir(self, job_id: str) -> Path | None:
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            return None
        path = self.root / job_id
        if path.is_symlink() or not path.is_dir():
            return None
        return path

    def read(self, job_id: str) -> dict[str, Any] | None:
        directory = self.dir(job_id)
        data = read_json(directory / "job.json") if directory else None
        return data if isinstance(data, dict) else None

    def ids(self) -> list[str]:
        try:
            return [e.name for e in os.scandir(self.root) if JOB_ID.match(e.name) and e.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    def settle(self, job: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        """A job that says running but whose runner is gone is rewritten as interrupted (never shown alive)."""
        now = time.time() if now is None else now
        verdict = job_liveness(job, now)
        if verdict and verdict["died"]:
            directory = self.dir(str(job.get("id") or ""))
            job = {**job, "status": "interrupted", "ended": utc_stamp(now),
                   "reason": "its runner is gone (the machine restarted or the process was killed)",
                   "steps": [{**s, "status": "interrupted" if s.get("status") == "running" else s.get("status")}
                             for s in job.get("steps") or []]}
            if directory:
                with contextlib.suppress(OSError):
                    write_json_atomic(directory / "job.json", job)
        return job

    def load(self) -> None:
        for job_id in self.ids():
            job = self.read(job_id)
            if job:
                self.settle(job)

    def docs(self, job_id: str) -> list[dict[str, Any]]:
        directory = self.dir(job_id)
        out = []
        try:
            for e in sorted(os.scandir(directory / "docs"), key=lambda e: e.name) if directory else []:
                if e.is_file(follow_symlinks=False) and SAFE_NAME.match(e.name):
                    out.append({"name": e.name, "bytes": e.stat(follow_symlinks=False).st_size})
        except OSError:
            pass
        return out

    def running_for(self, repo_path: str) -> dict[str, Any] | None:
        for job_id in self.ids():
            job = self.read(job_id)
            if job and job.get("repo_path") == repo_path and job.get("status") == "running":
                job = self.settle(job)
                if job.get("status") == "running":
                    return job
        return None


def job_summary(job: dict[str, Any]) -> dict[str, Any]:
    """What the page sees of a job: no argv and no absolute path."""
    return {"id": job.get("id"), "repo": job.get("repo"), "repo_name": job.get("repo_name"), "action": job.get("action"),
            "preview": bool(job.get("preview")), "status": job.get("status"), "exit": job.get("exit"),
            "reason": job.get("reason") or "", "created": parse_time(job.get("created")),
            "started": parse_time(job.get("started")), "ended": parse_time(job.get("ended")),
            "layer": job.get("layer_display") or "", "repo_dir": job.get("repo_dir") or "",
            "placement": job.get("placement") or "",
            "steps": [{"name": s.get("name"), "display": s.get("display"), "status": s.get("status"), "exit": s.get("exit"),
                       "doc": s.get("doc"), "started": parse_time(s.get("started")), "ended": parse_time(s.get("ended")),
                       "reason": s.get("reason") or ""}
                      for s in job.get("steps") or [] if isinstance(s, dict)],
            "changed": job.get("changed") or [], "repo_changes": job.get("repo_changes") or [],
            "repo_unchanged": job.get("repo_unchanged"), "retry_from": retry_from(job),
            "retries": len(job.get("retries") or [])}


def retry_from(job: dict[str, Any]) -> str:
    """The step a retry starts at (the first that did not pass), for a failed or interrupted job; else ""."""
    if job.get("preview") or job.get("status") not in ("failed", "interrupted"):
        return ""
    for step in job.get("steps") or []:
        if isinstance(step, dict) and step.get("status") != "passed":
            return str(step.get("name") or "")
    return ""


JOURNAL_PAGE = 5000              # journal events per /api/job/journal answer
LOG_PAGE_LINES = 20_000          # lines per /api/job/log answer
LOG_PAGE_BYTES = 2 * 1024 * 1024
JOB_NEXT = {"create": "open the layer's workflows on the Layers view",
            "adopt": "open the layer's workflows on the Layers view"}
_NEXT_LINE = re.compile(r"(?:^\s*|;\s+)next:\s*(.+?)\s*$", re.IGNORECASE)   # "next: …", "… compiles; next: …"
_RELEVANT = re.compile(r"^al: |error|failed|Traceback", re.IGNORECASE)


def tail_bytes(path: Path, cap: int = JOURNAL_TAIL) -> bytes:
    """The last `cap` bytes of a file, from its first whole line; b"" when it cannot be read."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - cap))
            data = handle.read(cap)
    except OSError:
        return b""
    if size > cap:
        data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
    return data


def journal_events(path: Path, tail: bool = False) -> list[dict[str, Any]]:
    """The complete events of a journal (only its last 64 KiB with `tail`); unreadable lines are skipped."""
    if tail:
        data = tail_bytes(path)
    else:
        try:
            data = path.read_bytes()
        except OSError:
            return []
    out = []
    for line in data.split(b"\n")[:-1]:     # a last line without its newline is still being written
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def log_lines(path: Path, tail: bool = True) -> list[str]:
    """A step log's non-empty lines, ANSI stripped and the home directory as ~ (its last 64 KiB with `tail`)."""
    try:
        data = tail_bytes(path) if tail else path.read_bytes()
    except OSError:
        return []
    short = Shortener()
    return [short(line).strip() for line in strip_ansi(data.decode("utf-8", "replace")).splitlines() if line.strip()]


def first_sentence(text: Any) -> str:
    line = first_line(text)
    return re.split(r"(?<=[.!?])\s+(?=[A-Z(`'\"])", line, maxsplit=1)[0] if line else ""


def step_doing(directory: Path, step: str) -> str:
    """What a running step is doing, in one line: an agent step's last tool call ("compiling, attempt 2"),
    "thinking" after a tool's result, "writing: …" after a message; a plain step's last line of output."""
    files = step_files(directory, step)
    if not files:
        return ""
    if confined_file(directory, files["journal"]):
        for event in reversed(journal_events(files["journal"], tail=True)):
            kind = event.get("kind")
            if kind == "tool":
                return clip(str(event.get("doing") or event.get("text") or ""), 120)
            if kind == "tool.done":
                return "thinking"
            if kind == "message":
                return clip("writing: " + " ".join(first_line(event.get("text")).split()[:8]), 120)
        return ""
    if confined_file(directory, files["log"]):
        lines = log_lines(files["log"])
        return clip(lines[-1], 120) if lines else ""
    return ""


def last_relevant_lines(directory: Path, step: str, n: int = 12) -> list[str]:
    """What a person reads first about a failed step: an agent step's last errors, then its last message's
    last lines; a plain step's last lines of output, with `al: …`, error, failed and Traceback lines first."""
    files = step_files(directory, step)
    if not files:
        return []
    if confined_file(directory, files["journal"]):
        events = journal_events(files["journal"], tail=True)
        errors = list(dict.fromkeys(str(e.get("text") or "") for e in events if e.get("kind") == "error"))
        message = next((str(e.get("text") or "") for e in reversed(events) if e.get("kind") == "message"), "")
        said = [line.strip() for line in message.splitlines() if line.strip()]
        out = errors[-n:]
        return out + said[-(n - len(out)):] if len(out) < n and said else out
    if not confined_file(directory, files["log"]):
        return []
    lines = log_lines(files["log"])[-200:]
    relevant = [line for line in lines if _RELEVANT.search(line)][-n:]
    rest = [line for line in lines if not _RELEVANT.search(line)]
    return [clip(line, 400) for line in relevant + (rest[-(n - len(relevant)):] if len(relevant) < n else [])]


def job_report(job: dict[str, Any], directory: Path) -> dict[str, Any] | None:
    """The author step's last `report`: {text, decisions, left_out}, or None. Only the step named
    `author`: the scout agent's final message is not the layer's report."""
    for step in reversed([s for s in job.get("steps") or [] if isinstance(s, dict) and s.get("name") == "author"]):
        files = step_files(directory, step.get("name"))
        if step.get("stream") != "agent" or not files or not confined_file(directory, files["journal"]):
            continue
        report = next((e for e in reversed(journal_events(files["journal"])) if e.get("kind") == "report"), None)
        if report is not None:
            return {"text": str(report.get("text") or ""), "decisions": list(report.get("decisions") or []),
                    "left_out": list(report.get("left_out") or [])}
    return None


def job_next(job: dict[str, Any], directory: Path) -> str:
    """The last `next: …` line the steps printed (an agent step's in its journal), else a fixed sentence."""
    found = ""
    for step in job.get("steps") or []:
        files = step_files(directory, step.get("name")) if isinstance(step, dict) else {}
        if not files:
            continue
        if confined_file(directory, files["journal"]):
            lines = [line for e in journal_events(files["journal"]) if e.get("kind") in ("message", "report")
                     for line in str(e.get("text") or "").splitlines()]
        elif confined_file(directory, files["log"]):
            short = Shortener([(str(job.get("layer_dir") or ""), str(job.get("layer_display") or "")),
                               (str(job.get("repo_path") or ""), str(job.get("repo_dir") or ""))])
            lines = [short(line) for line in log_lines(files["log"])]
        else:
            continue
        for line in lines:
            match = _NEXT_LINE.search(line)
            if match:
                found = match.group(1).strip("` ")
    return clip(found, 300) or JOB_NEXT.get(str(job.get("action") or ""), "")


def job_card(job: dict[str, Any], directory: Path) -> dict[str, Any] | None:
    """The result card of a passed `result` step (`al result LAYER --json`, kept as docs/<doc>), its layer
    and next steps shortened as job_next shortens a log line; None without one."""
    step = next((s for s in job.get("steps") or [] if isinstance(s, dict) and s.get("name") == "result"), None)
    doc = str((step or {}).get("doc") or "")
    if not step or step.get("status") != "passed" or not doc or not SAFE_NAME.match(doc):
        return None
    path = Path(directory) / "docs" / doc
    if not confined_file(directory, path):
        return None
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(card, dict):
        return None
    short = Shortener([(str(job.get("layer_dir") or ""), str(job.get("layer_display") or "")),
                       (str(job.get("repo_path") or ""), str(job.get("repo_dir") or ""))])
    if isinstance(card.get("layer"), str):
        card["layer"] = short(card["layer"])
    if isinstance(card.get("next"), list):
        card["next"] = [short(item) for item in card["next"] if isinstance(item, str)]
    return card


def job_outcome(job: dict[str, Any], directory: Path, report: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The outcome card of an ended job (None while it runs, or for a preview). Passed: what was created or
    adopted, what is unverified, the next action. Failed or interrupted: the step, the reason in one
    sentence, its last relevant lines, where a retry starts and the step's log. A passed create or adopt
    also holds the result card (job_card); its first next step is the next action."""
    status = job.get("status")
    if job.get("preview") or status in (None, "running"):
        return None
    if status == "passed":
        changed = [c for c in job.get("changed") or [] if isinstance(c, dict)]
        count = lambda kind: sum(1 for c in changed if c.get("change") == kind)  # noqa: E731
        left_out = (report or {}).get("left_out") or []
        card = job_card(job, directory)
        return {"status": "passed",
                "created": {"layer": str(job.get("layer_display") or ""), "added": count("added"),
                            "modified": count("modified"), "removed": count("removed")},
                "adopted": sum(1 for c in changed if "/adopted/" in "/" + str(c.get("path") or "")),
                "unverified": [item for item in left_out if "UNVERIFIED" in item],
                "next": card["next"][0] if card and card.get("next") else job_next(job, directory),
                "card": card}
    steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
    failed = next((s for s in steps if s.get("status") in ("failed", "interrupted")), None)
    name = str((failed or {}).get("name") or retry_from(job) or "")
    reason = first_sentence((failed or {}).get("reason") or "")
    files = step_files(directory, name)
    if (not reason or GENERIC_REASON.match(reason)) and files and confined_file(directory, files["journal"]):
        for event in reversed(journal_events(files["journal"], tail=True)):
            text = first_sentence(event.get("text")) if event.get("kind") == "error" else ""
            if text and not GENERIC_REASON.match(re.sub(r"^al: (?:\S+: )?", "", text)):
                reason = text
                break
    if not reason:
        reason = first_sentence(re.sub(r"^\S+: ", "", str(job.get("reason") or ""))) or f"{name} did not pass"
    return {"status": status, "step": name, "reason": clip(reason, 400),
            "lines": last_relevant_lines(directory, name) if name else [],
            "retry_from": retry_from(job),
            "log": f"{name}.log" if files and confined_file(directory, files["log"]) else ""}


def job_detail(job: dict[str, Any], directory: Path) -> dict[str, Any]:
    """The Job page's view: job_summary plus, per step, its stream, what it shows (activity: journal | log
    | ""), its log's size, whether its raw events can be downloaded and what it is doing; and the job's
    outcome, the author's report and how many items need a person. No absolute path."""
    view = job_summary(job)
    records = [s for s in job.get("steps") or [] if isinstance(s, dict)]
    for shown, record in zip(view["steps"], records):
        stream = str(record.get("stream") or "")
        files = step_files(directory, record.get("name"))
        has_log = bool(files) and confined_file(directory, files["log"])
        has_journal = bool(files) and stream == "agent" and confined_file(directory, files["journal"])
        shown.update(stream=stream, activity="journal" if has_journal else "log" if has_log else "",
                     log_bytes=(stat_sig(files["log"]) or (0, 0))[1] if has_log else 0, raw=stream == "agent" and has_log,
                     journal=has_journal,
                     doing=step_doing(directory, str(record.get("name"))) if record.get("status") == "running" else "")
    report = job_report(job, directory)
    output = directory / "output.log"
    view.update(outcome=job_outcome(job, directory, report), report=report,
                needs_person=len(report["left_out"]) if report else 0,
                output_bytes=(stat_sig(output) or (0, 0))[1] if confined_file(directory, output) else 0,
                has_steps_dir=(directory / "steps").is_dir() and not (directory / "steps").is_symlink())
    return view


def step_reason(step: dict[str, Any], al_lines: list[str]) -> str:
    """Why a step failed, from its `al: …` stderr lines (without `al: ` and the subcommand's own name)."""
    argv = step.get("argv") or []
    command = f"{argv[1]}: " if len(argv) > 1 else "\0"
    out = []
    for line in al_lines:
        line = line.removeprefix("al: ")
        out.append(line.removeprefix(command))
    return "\n".join(out)


def merge_changed(earlier: list[dict[str, str]], now: list[dict[str, str]]) -> list[dict[str, str]]:
    """The files a job changed over its attempts: a file added by an earlier attempt stays added."""
    merged = {c["path"]: c for c in earlier if isinstance(c, dict) and "path" in c}
    for change in now:
        was = merged.get(change["path"])
        if was is not None and was.get("change") == "added":
            if change["change"] == "removed":
                del merged[change["path"]]
            continue
        merged[change["path"]] = change
    return [merged[p] for p in sorted(merged)]


def run_job(directory: Path) -> int:
    """`dashboard.py --run-job DIR`: run a recorded job's steps in order, detached from the dashboard (so a
    restart does not stop it), holding the repository's lock; argv lists only, never a shell.

    Every step's stdout and stderr go, as they arrive, to steps/<name>.log (started afresh when a retry
    runs the step again) and to the job's output.log (every attempt). An agent step (stream "agent")
    is condensed instead: its stdout becomes journal events in steps/<name>.journal.jsonl and only
    their one-line form reaches output.log; the raw stream stays whole in its steps/<name>.log."""
    directory = Path(directory).resolve()
    path = directory / "job.json"
    job = read_json(path)
    if not isinstance(job, dict):
        print(f"run-job: no job at {directory}", file=sys.stderr)
        return 2
    io_lock = threading.RLock()

    def save(**changes: Any) -> None:
        with io_lock:
            job.update(changes)
            write_json_atomic(path, job)

    jobs_root = directory.parent
    (jobs_root / "locks").mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file(jobs_root, job["repo_path"]), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        save(status="refused", reason=f"an action is already running on {job.get('repo_name')}", ended=utc_stamp(),
             pid=os.getpid(), host=socket.gethostname())
        os.close(fd)
        return 1
    save(status="running", pid=os.getpid(), host=socket.gethostname(), heartbeat=utc_stamp(),
         started=job.get("started") or utc_stamp())
    stop = threading.Event()
    interrupted = threading.Event()
    current: dict[str, subprocess.Popen | None] = {"proc": None}

    def beat() -> None:
        while not stop.wait(JOB_HEARTBEAT):
            with contextlib.suppress(OSError):
                save(heartbeat=utc_stamp())

    def kill_group(sig: int = signal.SIGTERM) -> None:
        proc = current["proc"]
        if proc is not None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, sig)

    def on_term(*_args: Any) -> None:
        interrupted.set()
        kill_group()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    threading.Thread(target=beat, daemon=True).start()
    layer, repo = Path(job["layer_dir"]), Path(job["repo_path"])
    prefix = os.path.relpath(layer, repo.parent)
    before_layer, before_repo = layer_files_snapshot(layer), repo_status(repo)
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    env["PATH"] = job.get("path") or env.get("PATH", "")
    status, exit_code = "passed", 0
    log_lock = threading.Lock()
    (directory / "steps").mkdir(mode=0o700, exist_ok=True)
    short = Shortener([(str(job.get("layer_dir") or ""), str(job.get("layer_display") or "")),
                       (str(job.get("repo_path") or ""), str(job.get("repo_dir") or ""))])

    def fail_before(index: int, reason: str, code: int) -> None:
        """Step `index` failed with `reason` before it ran; every later pending step is skipped."""
        with io_lock:
            job["steps"][index].update(status="failed", exit=code, reason=reason, ended=utc_stamp())
            for rest in job["steps"][index + 1:]:
                if rest.get("status") != "passed":
                    rest["status"] = "skipped"
            job["reason"] = f"{job['steps'][index]['name']}: {reason.splitlines()[0] if reason else ''}"
        save()

    with (directory / "output.log").open("ab") as log:
        retries = job.get("retries") or []
        if retries:
            log.write(f"--- retry from {retries[-1].get('from')} at {utc_stamp()} ---\n".encode())
            log.flush()
        pending = [i for i, s in enumerate(job["steps"]) if s.get("status") != "passed"]
        preflight = job.get("preflight")
        if preflight and pending:
            shown = " ".join(["al"] + [os.path.basename(a) if str(a).startswith("/") else shlex.quote(str(a))
                                       for a in preflight[1:]])
            log.write(f"$ {shown}\n".encode())
            try:
                cp = subprocess.run(preflight, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    env=env, cwd=str(directory), timeout=PREFLIGHT_TIMEOUT)
                code, out, err = cp.returncode, cp.stdout, cp.stderr
            except subprocess.TimeoutExpired:
                code, out, err = 124, "", f"the prerequisite check did not answer within {PREFLIGHT_TIMEOUT} s"
            except OSError as exc:
                code, out, err = 127, "", str(exc)
            log.write((out + err).encode())
            log.flush()
            if code != 0:
                lines = [line.strip() for line in out.splitlines() if line.strip()]
                last = next((line.strip() for line in reversed(err.splitlines()) if line.strip()), "")
                if code in (126, 127) or not lines:
                    lines = [f"cannot start the al tool: {last}" if code in (126, 127)
                             else f"the prerequisite check failed (exit {code}): {last}".rstrip(": ")]
                fail_before(pending[0], "\n".join(lines), code)
                status, exit_code = "failed", code
                pending = []
        for index, step in enumerate(job["steps"]):
            if not pending:
                break
            if step.get("status") == "passed":
                continue
            if interrupted.is_set():
                break
            with log_lock:
                log.write(f"$ {step['display']}\n".encode())
                log.flush()
            with io_lock:
                step.update(status="running", started=utc_stamp())
            save()
            doc = (directory / "docs" / step["doc"]).open("wb") if step.get("doc") else None
            files = step_files(directory, step.get("name"))
            step_log = files["log"].open("wb") if files else None
            journal = AgentJournal(short) if files and step.get("stream") == "agent" else None
            journal_file = files["journal"].open("wb") if journal is not None else None

            def close_files() -> None:
                for handle in (doc, step_log, journal_file):
                    if handle is not None:
                        handle.close()

            try:
                proc = subprocess.Popen(step["argv"], shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True, env=env, cwd=str(directory))
            except OSError as exc:
                with log_lock:
                    log.write(f"cannot start: {exc}\n".encode())
                    if step_log is not None:
                        step_log.write(f"cannot start: {exc}\n".encode())
                fail_before(index, f"cannot start: {exc}", 127)
                status, exit_code = "failed", 127
                close_files()
                break
            current["proc"] = proc
            with io_lock:
                step["pid"] = proc.pid   # its process group: what a timeout or an interruption kills
            save()

            al_lines: list[str] = []      # the step's own `al: …` stderr lines: its failure reason

            def record(events: list[dict[str, Any]], condensed: bool) -> None:
                """Journal events of the agent step (log_lock held); `condensed`: their lines go to output.log."""
                for event in events:
                    journal_file.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
                    line = journal_line(event) if condensed else None
                    if line is not None:
                        log.write(line.encode("utf-8", "replace") + b"\n")
                journal_file.flush()
                log.flush()

            def pump(stream: Any, to_doc: bool) -> None:
                for chunk in iter(stream.readline, b""):
                    with log_lock:
                        if step_log is not None:
                            step_log.write(chunk)
                            step_log.flush()
                        if journal is not None and to_doc:     # the agent's stream: only its condensed lines
                            record(journal.feed_line(chunk.decode("utf-8", "replace")), True)
                        else:
                            log.write(chunk)
                            log.flush()
                            if journal is not None and chunk.startswith(b"al: "):
                                record([journal.error(chunk.decode("utf-8", "replace"))], False)
                    if to_doc and doc is not None:
                        doc.write(chunk)
                        doc.flush()
                    if not to_doc and chunk.startswith(b"al: "):
                        al_lines.append(chunk.decode("utf-8", "replace").rstrip("\n"))
                        del al_lines[:-5]

            readers = [threading.Thread(target=pump, args=(proc.stdout, True), daemon=True),
                       threading.Thread(target=pump, args=(proc.stderr, False), daemon=True)]
            for reader in readers:
                reader.start()
            timed_out = threading.Event()

            def expire() -> None:
                timed_out.set()
                kill_group(signal.SIGKILL)

            timer = threading.Timer(float(step.get("timeout") or 1800), expire)
            timer.daemon = True
            timer.start()
            code = proc.wait()
            timer.cancel()
            for reader in readers:
                reader.join(timeout=5)
            kill_group(signal.SIGKILL)   # whatever the step left behind in its own group
            current["proc"] = None
            if journal is not None:
                with log_lock:
                    record(journal.finish(), True)
            close_files()
            if timed_out.is_set():
                with log_lock:
                    log.write(f"timed out after {step.get('timeout')} s\n".encode())
            with io_lock:
                step.update(exit=code, ended=utc_stamp(),
                            status="passed" if code == 0 else "interrupted" if interrupted.is_set() else "failed")
                if code != 0 and not interrupted.is_set():
                    reason = (f"timed out after {step.get('timeout')} s" if timed_out.is_set() else
                              step_reason(step, al_lines) or f"{step['name']} exited {code}; see the output")
                    step["reason"] = reason
                    job["reason"] = f"{step['name']}: {reason.splitlines()[0]}"
            save()
            if code != 0:
                status, exit_code = ("interrupted" if interrupted.is_set() else "failed"), code
                for rest in job["steps"][index + 1:]:
                    rest["status"] = "skipped"
                break
    if interrupted.is_set():
        status = "interrupted"
    stop.set()
    after_repo = repo_status(repo)
    earlier_unchanged = job.get("repo_unchanged") if job.get("retries") else None
    save(status=status, exit=exit_code, ended=utc_stamp(), heartbeat=utc_stamp(),
         changed=merge_changed(job.get("changed") or [], changed_files(before_layer, layer_files_snapshot(layer), prefix)),
         repo_changes=sorted(set(job.get("repo_changes") or []) | (set(after_repo) - set(before_repo))),
         repo_unchanged=after_repo == before_repo and earlier_unchanged is not False)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return 0 if status == "passed" else 1


# ============================================================================ HTTP

class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], model: Model, token: str | None, static_dir: Path,
                 read_only: bool = False, no_start: bool = False, allow_start: bool = False) -> None:
        self.model = model
        self.token = token
        self.read_only = read_only
        self.no_start = no_start
        self.allow_start = allow_start
        self.static_dir = static_dir
        self.shutting_down = threading.Event()
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"
    server_version = "al-dashboard"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — quiet by default
        if os.environ.get("AL_DASHBOARD_LOG"):
            super().log_message(format, *args)

    # ---------------------------------------------------------------- plumbing
    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._send(status, json_bytes({"error": message}), "application/json; charset=utf-8")

    def _authorised(self, query: dict[str, list[str]]) -> tuple[bool, bool]:
        """→ (allowed, set_cookie)."""
        token = self.server.token
        if not token:
            return True, False
        offered = (query.get("token") or [""])[0]
        if offered and hmac.compare_digest(offered.encode(), token.encode()):
            return True, True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip().encode(), token.encode()):
            return True, False
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "al_dashboard_token" and hmac.compare_digest(value.encode(), token.encode()):
                return True, False
        return False, False

    def _host_ok(self) -> bool:
        """DNS-rebinding guard when there is no token: only loopback Host headers are accepted."""
        if self.server.token:
            return True
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host[1:host.index("]")]
        return is_loopback(name)

    def _origin_ok(self) -> bool:
        """The request comes from this dashboard's own page: Origin is present and names the Host."""
        origin, host = self.headers.get("Origin"), (self.headers.get("Host") or "").strip()
        return bool(origin and host) and origin == f"http://{host}"

    def do_POST(self) -> None:  # noqa: N802 — the writes: the local model setting, starting work, settling a run, the workspace actions
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/settle":
                return self._settle()
            if path == "/api/models":
                return self._set_models()
            if path == "/api/start":
                return self._start()
            if path == "/api/action":
                return self._action()
            if path == "/api/job/retry":
                return self._retry()
            if path == "/api/promotion":
                return self._promotion()
            if path == "/api/integrate":
                return self._integrate()
            if path == "/api/decider":
                return self._decider()
            if path == "/api/decision":
                return self._decision()
            if path == "/api/standin":
                return self._standin()
        except (BrokenPipeError, ConnectionResetError):
            return None
        self._refuse()

    def _refuse(self) -> None:
        self._drain()
        self._error(405, "read-only")

    do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815

    def _drain(self, limit: int = MAX_WRITE_BODY) -> bytes | None:
        """Read the request body (so the connection stays usable); None when it is over `limit`,
        which closes the connection once anything large is left unread."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return None
        if length < 0:
            self.close_connection = True
            return None
        if length > max(limit, 64 * 1024):
            self.close_connection = True
            return None
        data = self.rfile.read(length) if length else b""
        return data if length <= limit else None

    def _write_guard(self, max_body: int) -> bytes | None:
        """Every check a write must pass, in one place: a loopback Host (or a token server), the
        token from the header or cookie (never the URL), an Origin naming this Host, the
        X-AL-Dashboard header a plain form cannot send, a JSON body of at most `max_body` bytes, and
        no --read-only. Sends the refusal and returns None; else the body."""
        body = self._drain(max_body)
        if not self._host_ok():
            return self._error(403, "bad host")
        if not self._authorised({})[0]:   # header or cookie: a token in the URL is not accepted for a write
            return self._error(401, "token required")
        if not self._origin_ok():
            return self._error(403, "cross-origin request refused")
        if self.headers.get(WRITE_HEADER) != "1":
            return self._error(403, f"missing {WRITE_HEADER} header")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "application/json only")
        if body is None:
            return self._error(413, f"request body over {max_body} bytes")
        if self.server.read_only:
            return self._error(403, "the dashboard was started with --read-only")
        return body

    def _action(self) -> None:
        """POST /api/action {"repo": KEY, "action": NAME[, "confirm": NONCE][, "placement": P]}: an
        allow-listed al action."""
        body = self._write_guard(MAX_WRITE_BODY)
        if body is None:
            return None
        model = self.server.model
        if model.no_actions:
            return self._error(403, "actions are off (--no-actions)")
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        usage = ('expected {"repo": KEY, "action": NAME}, and "confirm": NONCE to confirm a preview, and for create '
                 'or adopt "choices": {KEY: VALUE} and "placement": "beside" | "inside"; nothing else')
        if not isinstance(request, dict) or not set(request) <= ACTION_KEYS or not {"repo", "action"} <= set(request) \
                or not all(isinstance(v, str) for k, v in request.items() if k != "choices"):
            return self._error(400, usage)
        if request["action"] not in ACTIONS:
            return self._error(400, "unknown action")
        choices = request.get("choices")
        if choices is not None:
            if request["action"] not in CHOICE_ACTIONS:
                return self._error(400, f"choices are for {' and '.join(CHOICE_ACTIONS)} only")
            if not isinstance(choices, dict):
                return self._error(400, usage)
            for key, value in choices.items():
                problem = choice_problem(key, value)
                if problem:
                    return self._error(400, problem)
        placement = request.get("placement")
        if placement is not None:
            if request["action"] not in CHOICE_ACTIONS:
                return self._error(400, f"placement is for {' and '.join(CHOICE_ACTIONS)} only")
            if placement not in PLACEMENTS:
                return self._error(400, f"placement: one of {', '.join(PLACEMENTS)}")
        if not request["repo"] or WS_KEY_BAD.search(request["repo"]) or WS_KEY_BAD.search(request.get("confirm", "")):
            return self._error(400, usage)
        if model.workspace is None:
            return self._error(404, "no workspace")
        entry = model.workspace.entry(request["repo"])
        if entry is None:
            return self._error(404, "unknown repository")
        status, answer = model.start_action(entry, request["action"], request.get("confirm") or None, choices,
                                             placement)
        return self._json(answer, status)

    def _retry(self) -> None:
        """POST /api/job/retry {"job": ID, "from": STEP}: run a failed job again from its first step that
        did not pass. The same write checks as /api/action."""
        body = self._write_guard(MAX_WRITE_BODY)
        if body is None:
            return None
        model = self.server.model
        if model.no_actions:
            return self._error(403, "actions are off (--no-actions)")
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        usage = 'expected {"job": ID, "from": STEP}; nothing else'
        if not isinstance(request, dict) or set(request) != RETRY_KEYS or not all(isinstance(v, str) for v in request.values()):
            return self._error(400, usage)
        if not request["job"] or not request["from"] or any(WS_KEY_BAD.search(v) for v in request.values()):
            return self._error(400, usage)
        status, answer = model.retry_job(request["job"], request["from"])
        return self._json(answer, status)

    def _set_models(self) -> None:
        """POST /api/models {"layer", "tier", "model"} or {"layer", "tier"|null, "reset": true}."""
        body = self._write_guard(MAX_WRITE_BODY)
        if body is None:
            return None
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        if not isinstance(request, dict) or not isinstance(request.get("layer"), str):
            return self._error(400, 'expected {"layer", "tier", "model"} or {"layer", "tier", "reset": true}')
        reset = request.get("reset") is True
        tier, model = request.get("tier"), request.get("model")
        if reset and not (tier is None or isinstance(tier, str)) or not reset and not (
                isinstance(tier, str) and isinstance(model, str)):
            return self._error(400, 'expected {"layer", "tier", "model"} or {"layer", "tier", "reset": true}')
        model_ = self.server.model
        with model_.lock:
            layer = model_.layers.get(request["layer"])
        if layer is None:
            return self._error(404, "unknown layer")
        note = None
        try:
            if reset:
                if tier is not None:
                    check_setting(tier, "x", declared_tiers(layer.ir))
            else:
                note = check_setting(tier, model, declared_tiers(layer.ir))
        except ValueError as exc:
            return self._error(400, str(exc))
        writable, why = settings_writable(layer, self.server.read_only)
        if not writable:
            return self._error(409, why)
        try:
            current = read_settings(layer.path)
        except ValueError as exc:
            return self._error(409, str(exc))
        if reset and tier is None:
            current = {}
        elif reset:
            current.pop(tier, None)
        else:
            current[tier] = model
        try:
            write_settings(layer.path, current)
        except OSError as exc:
            return self._error(409, f"cannot write {SETTINGS_FILE}: {exc.strerror or exc}")
        view = layer.models_view(self.server.read_only)
        with model_.lock:
            model_.settings_sigs[layer.key] = stat_sig(layer.path / SETTINGS_FILE)
        model_.hub.publish("layers", model_.layer_list())
        return self._json({"ok": True, "note": note, "models": view})

    def _settle(self) -> None:
        """POST /api/settle {layer, run, as: landed|superseded|abandoned, by, note, who}: a person's record
        that a finished run needs nothing more (agents/<id>/settlement.json), behind every write guard.
        {layer, run, undo: true} takes the last settlement back (the one before it returns, if any)."""
        body = self._write_guard(MAX_WRITE_BODY)
        if body is None:
            return None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        keys = {"layer", "run", "as", "by", "note", "who"}
        if isinstance(data, dict) and "undo" in data:   # {"layer", "run", "undo": true}: the settlement goes
            if set(data) != {"layer", "run", "undo"} or data["undo"] is not True or not isinstance(data.get("layer"), str) \
                    or not isinstance(data.get("run"), str):
                return self._error(400, 'expected {"layer", "run", "undo": true}')
            code, answer = self.server.model.unsettle(data["layer"], data["run"])
            return self._json(answer, code)
        if not isinstance(data, dict) or not set(data) <= keys or not isinstance(data.get("layer"), str) \
                or not isinstance(data.get("run"), str):
            return self._error(400, 'expected {"layer", "run", "as": landed|superseded|abandoned, "by", "note", "who"}')
        code, answer = self.server.model.settle(data["layer"], data["run"], data)
        return self._json(answer, code)

    def _start(self) -> None:
        """POST /api/start {layer, target: {kind, name} | text: "/NAME args", prompt, class, model_set,
        models: {tier: id}, issue: none|start|trigger, request_key, confirm_duplicate, confirm_ships}:
        checked here against what the page offers, then started by the layer's own launcher. With
        {layer, again: RUN, request_key, confirm_duplicate, confirm_ships} instead, the failed run RUN is
        started again from its own records (Model.again_request) and the new run records `replaces`."""
        body = self._write_guard(MAX_START_BODY)
        if body is None:
            return None
        if self.server.no_start:
            return self._error(403, "the dashboard was started with --no-start")
        if self.server.model.in_container and not self.server.allow_start:
            return self._error(403, "the dashboard runs in a container and cannot start runs "
                                    "(--allow-start-in-container only for an image built for it)")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        if not isinstance(data, dict) or not isinstance(data.get("layer"), str):
            return self._error(400, "expected a JSON object with a layer")
        model_ = self.server.model
        with model_.lock:
            layer = model_.layers.get(data["layer"])
        if layer is None:
            return self._error(404, "unknown layer")
        status = start_status(layer, model_)
        if not status["enabled"]:
            return self._error(409, status["why"])
        if "again" in data:
            request, problem, code_ = self._again_request(layer, data)
            if request is None:
                return self._error(code_, problem)
        else:
            request, problem = self._start_request(layer, data)
            if request is None:
                return self._error(400, problem)
        code, result, stderr = start_via_layer(layer, request)
        outcome = result.get("status")
        url = f"#/run/{layer.key}/{result.get('run_id')}" if result.get("run_id") else ""
        if outcome == "started":
            if "again" in data and not result.get("existing"):
                model_.standin_note(layer, {"run": str(data.get("again")), "kind": "restart", "action": "restart",
                                            "outcome": f"started {result['run_id']}"})
            return self._json({"status": "started", "run_id": result["run_id"], "url": url,
                               "queued": result.get("queued") or "", "existing": bool(result.get("existing")),
                               "issue": result.get("issue")}, 202)
        if outcome == "issue-created":
            return self._json({"status": "issue-created", "run_id": "", "url": "", "issue": result.get("issue"),
                               "existing": bool(result.get("existing")),
                               "message": result.get("message") or "the layer's trigger will start it"}, 202)
        if outcome in ("confirm-duplicate", "confirm-ships"):
            return self._json({"error": result.get("message") or outcome, "confirm": outcome.split("-", 1)[1],
                               "message": result.get("message") or "", "previous": result.get("previous") or ""}, 409)
        if outcome == "refused":
            return self._json({"error": result.get("message") or "refused", "refused": True}, 429)
        if outcome == "invalid":
            return self._error(400, str(result.get("error") or "invalid request"))
        tail = "\n".join(stderr.strip().splitlines()[-12:])
        return self._json({"error": str(result.get("error") or f"the layer's launcher failed (exit {code})"),
                           "stderr": tail}, 502)

    def _standin_request(self, which: str, usage: str) -> tuple[dict[str, Any], Layer] | None:
        """The checks the three stand-in writes share: every write guard, a JSON object with known keys,
        a known layer that has the stand-in's script. Sends the refusal and returns None; else (body, layer)."""
        body = self._write_guard(MAX_WRITE_BODY)
        if body is None:
            return None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        if not isinstance(data, dict) or not set(data) <= STANDIN_KEYS[which] or not isinstance(data.get("layer"), str):
            return self._error(400, usage)
        model_ = self.server.model
        with model_.lock:
            layer = model_.layers.get(data["layer"])
        if layer is None:
            return self._error(404, "unknown layer")
        if not (layer.path / STANDIN_SCRIPT).is_file():
            return self._error(409, f"generated before the stand-in: run `al gen {layer.name}`")
        return data, layer

    def _decider(self) -> None:
        """POST /api/decider {layer, mode: off|suggest|auto} | {layer, reset: true} | {layer, may: [KIND…]} |
        {layer, paused: bool}: the stand-in's local setting of a layer (agents/settings/decider.json, the
        file `al decider` writes), changed by the layer's own `adws/standin.py set`."""
        usage = ('expected {"layer", "mode": off|suggest|auto}, {"layer", "reset": true}, {"layer", "may": [KIND…]} '
                 'or {"layer", "paused": true|false}')
        checked = self._standin_request("decider", usage)
        if checked is None:
            return None
        data, layer = checked
        model_ = self.server.model
        given = [key for key in ("mode", "reset", "may", "paused") if key in data]
        if len(given) != 1:
            return self._error(400, usage)
        view = model_.standin_view(layer)
        if "mode" in data:
            if data["mode"] not in DECIDER_MODES:
                return self._error(400, f"mode: one of {', '.join(DECIDER_MODES)}")
            argv = ["--mode", data["mode"]]
        elif "reset" in data:
            if data["reset"] is not True:
                return self._error(400, usage)
            argv = ["--reset"]
        elif "may" in data:
            kinds = data["may"]
            if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds) or len(kinds) > len(STANDIN_KINDS):
                return self._error(400, usage)
            for kind in kinds:
                if kind not in STANDIN_KINDS:
                    return self._error(400, f"may: {clip(kind, 40)!r} is not a decision kind ({', '.join(STANDIN_KINDS)})")
                if kind not in (view.get("offered") or []):
                    return self._error(400, f"{kind} is not in the layer's (stand-in … #:may)")
            argv = ["--may", ",".join(kinds)]
        else:
            if not isinstance(data["paused"], bool):
                return self._error(400, usage)
            argv = ["--pause" if data["paused"] else "--unpause"]
        if not view.get("available"):
            return self._error(409, str(view.get("why") or "the layer's stand-in is not available"))
        writable, why = settings_writable(layer, self.server.read_only)
        if not writable:
            return self._error(409, why.replace("al models LAYER --set TIER=ID", "al decider LAYER"))
        code, result, tail = standin_via_layer(layer, ["set", *argv, "--via", "dashboard", "--json"])
        if code != 0:
            message = str(result.get("error") or (tail.strip().splitlines() or ["the layer's stand-in failed"])[-1])
            return self._error(400 if code == 2 else 409, clip(home_short(message), 400))
        model_.drop_standin_view(layer)
        with model_.lock:
            model_.standin_sigs[layer.key] = model_._standin_sig(layer)
        fresh = model_.standin_view(layer)
        model_.hub.publish("layers", model_.layer_list())
        return self._json({"ok": True, "standin": fresh})

    def _decision(self) -> None:
        """POST /api/decision {layer, run, answer: go|alter|reject|explain, text}: a person's answer to the
        stand-in's suggestion for a run, applied by the layer's own `adws/standin.py answer` (decide.answer,
        the entry of `al answer`, mail and the webhook too). A go or alter resumes or starts work, so it is
        off where starting work is."""
        usage = 'expected {"layer", "run", "answer": go|alter|reject|explain, "text"}'
        checked = self._standin_request("decision", usage)
        if checked is None:
            return None
        data, layer = checked
        run, answer, text = data.get("run"), data.get("answer"), data.get("text", "")
        if not isinstance(run, str) or answer not in DECISION_ANSWERS or not isinstance(text, str):
            return self._error(400, usage)
        if not RUN_ID.match(run) or not (layer.path / "agents" / run / "state.json").is_file():
            return self._error(404, "unknown run")
        if answer == "alter" and not text.strip():
            return self._error(400, "alter needs the change as text")
        if answer in ("go", "alter"):
            if self.server.no_start:
                return self._error(403, "the dashboard was started with --no-start")
            if self.server.model.in_container and not self.server.allow_start:
                return self._error(403, "the dashboard runs in a container and cannot start runs")
        code, result, tail = standin_via_layer(layer, ["answer", run, answer, "--via", "dashboard", "--json"],
                                               stdin=text, timeout=START_TIMEOUT * 2)
        model_ = self.server.model
        model_.drop_standin_view(layer)
        model_.refresh_run(layer, run)
        model_.hub.publish("layers", model_.layer_list())
        status = str(result.get("status") or "")
        if not status or status == "invalid":
            last = str(result.get("error") or (tail.strip().splitlines() or ["the layer's stand-in failed"])[-1])
            return self._error(502, clip(home_short(last), 400))
        if status == "refused":
            reason = str(result.get("reason") or "")
            return self._error(400 if reason == "bad-answer" else 404 if reason == "unknown-run" else 409,
                               str(result.get("message") or reason or "refused"))
        return self._json({"status": status, "message": str(result.get("message") or "")})

    def _standin(self) -> None:
        """POST /api/standin {layer, run} | {layer, runs: [ID…]}: ask the stand-in about failed, died,
        handed-over or waiting runs (a suggestion each, whatever the mode); {layer, apply: [ID…]}: let it
        take the suggestions stored for those runs (its scope and limits hold). Queued for the worker:
        202 {"queued": n}; what it does arrives over the `run` events."""
        usage = 'expected {"layer", "run": ID}, {"layer", "runs": [ID…]} or {"layer", "apply": [ID…]} (at most 200 runs)'
        checked = self._standin_request("standin", usage)
        if checked is None:
            return None
        data, layer = checked
        given = [key for key in ("run", "runs", "apply") if key in data]
        if len(given) != 1:
            return self._error(400, usage)
        ids = [data["run"]] if given[0] == "run" else data[given[0]]
        if not isinstance(ids, list) or not ids or len(ids) > MAX_ASK_RUNS \
                or not all(isinstance(i, str) and RUN_ID.match(i) for i in ids):
            return self._error(400, usage)
        model_ = self.server.model
        verb = "apply" if given[0] == "apply" else "ask"
        if verb == "apply":
            if self.server.no_start:
                return self._error(403, "the dashboard was started with --no-start")
            if model_.in_container and not self.server.allow_start:
                return self._error(403, "the dashboard runs in a container and cannot start runs")
        items = []
        for run_id in dict.fromkeys(ids):
            with model_.lock:
                row = model_.summaries.get(f"{layer.key}/{run_id}")
            if row is None:
                return self._error(404, f"unknown run {run_id}")
            row = model_._decorated(row)
            if verb == "ask" and row.get("status") not in ASK_STATUSES:
                return self._error(400, f"run {run_id} is {row.get('status')}: the stand-in is asked about a failed, "
                                        "died, handed-over or waiting run")
            items.append(model_.standin_item(row) if verb == "ask" else {"run": run_id})
        queued = sum(1 for item in items if model_.standin_enqueue(layer, verb, item))
        return self._json({"queued": queued}, 202)

    def _promotion(self) -> None:
        """POST /api/promotion {layer, action: decide|start|continue, from, to, decision, changes, by, comment,
        then, dry_run, run}: checked here, then done by the layer's own adws/promote.py (a decision is a line
        in promotions/<id>/acceptance.jsonl; start and continue detach a promotion run)."""
        body = self._write_guard(MAX_PROMOTION_BODY)
        if body is None:
            return None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        usage = ('expected {"layer", "action": decide|start|continue, "from", "to"} and, to decide, "decision", '
                 '"changes", "by", "comment", "then"; nothing else')
        if not isinstance(data, dict) or not set(data) <= PROMOTION_KEYS or not isinstance(data.get("layer"), str):
            return self._error(400, usage)
        action = data.get("action")
        if action not in PROMOTION_ACTIONS:
            return self._error(400, f"unknown action; one of {', '.join(PROMOTION_ACTIONS)}")
        stages = [data.get("from"), data.get("to")]
        if not all(isinstance(v, str) and STAGE_NAME_RX.match(v) for v in stages):
            return self._error(400, "from and to name the two stages, e.g. sprint and main")
        if action in ("start", "continue"):
            if self.server.no_start:
                return self._error(403, "the dashboard was started with --no-start")
            if self.server.model.in_container and not self.server.allow_start:
                return self._error(403, "the dashboard runs in a container and cannot start runs "
                                        "(--allow-start-in-container only for an image built for it)")
        argv: list[str]
        if action == "decide":
            decision, changes, by = data.get("decision"), data.get("changes", []), data.get("by")
            comment, then = data.get("comment", ""), data.get("then", "")
            if decision not in PROMOTION_DECISIONS:
                return self._error(400, f"decision is one of {', '.join(PROMOTION_DECISIONS)}")
            if not isinstance(changes, list) or not all(isinstance(c, str) and CHANGE_ID_RX.match(c) for c in changes):
                return self._error(400, "changes are change ids: 8 hexadecimal characters each")
            if not isinstance(by, str) or not by.strip() or len(by) > 80 or not by.isprintable():
                return self._error(400, "by names who decides: 1–80 printable characters on one line")
            if not isinstance(comment, str) or len(comment) > 2000:
                return self._error(400, "a comment has at most 2000 characters")
            if then not in ("", *PROMOTION_THEN):
                return self._error(400, "then is revert or patch")
            argv = ["decide", stages[0], stages[1], decision, *changes, "--by", by.strip()]
            argv += ["--comment", comment] if comment else []
            argv += ["--then", then] if then else []
        else:
            if not isinstance(data.get("dry_run", False), bool):
                return self._error(400, "dry_run is true or false")
            argv = ["run", stages[0], stages[1], "--detach", *(["--dry-run"] if data.get("dry_run") else [])]
        model_ = self.server.model
        with model_.lock:
            layer = model_.layers.get(data["layer"])
        if layer is None:
            return self._error(404, "unknown layer")
        if not (layer.path / PROMOTE_SCRIPT).is_file():
            return self._error(409, f"this layer has no adws/promote.py (no stages, or generated before promotions): "
                                    f"run `al gen {layer.name}`")
        code, result, stderr = promotion_via_layer(layer, argv)
        if result.get("status") == "recorded":
            return self._json({"status": "recorded", "promotion": result.get("promotion"),
                               "record": result.get("record"), "approval": result.get("approval")})
        if result.get("status") == "started":
            url = f"#/run/{layer.key}/{result.get('run_id')}"
            return self._json({"status": "started", "run_id": result.get("run_id"), "url": url,
                               "resumed": bool(result.get("resumed"))}, 202)
        if result.get("status") == "error":
            return self._json({"error": f"{result.get('reason')}: {result.get('error')}", "reason": result.get("reason")},
                              409 if result.get("reason") in ("not-open", "nothing-to-promote", "already-open") else 400)
        tail = "\n".join(stderr.strip().splitlines()[-12:])
        return self._json({"error": f"the layer's promote.py failed (exit {code})", "stderr": tail}, 502)

    def _integrate(self) -> None:
        """POST /api/integrate {layer, run, action: start|approve, confirm: NONCE, model_set, who} and
        nothing else: "Resolve with an agent". Checked here, then done by the layer's own
        adws/integrate.py, which detaches an integration run (start: the nonce of the preview the person
        saw; approve: who approves a waiting integration)."""
        body = self._write_guard(MAX_WRITE_BODY)
        if body is None:
            return None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "expected a JSON object")
        usage = ('expected {"layer", "run", "action": start|approve} and, to start, "confirm" (the preview\'s '
                 'nonce) and "model_set"; to approve, "who"; nothing else')
        if not isinstance(data, dict) or not set(data) <= INTEGRATE_KEYS or not isinstance(data.get("layer"), str) \
                or not isinstance(data.get("run"), str):
            return self._error(400, usage)
        action = data.get("action")
        if action not in INTEGRATE_ACTIONS:
            return self._error(400, f"unknown action; one of {', '.join(INTEGRATE_ACTIONS)}")
        if self.server.no_start:
            return self._error(403, "the dashboard was started with --no-start")
        if self.server.model.in_container and not self.server.allow_start:
            return self._error(403, "the dashboard runs in a container and cannot start runs "
                                    "(--allow-start-in-container only for an image built for it)")
        model_ = self.server.model
        if action == "start":
            model_set = data.get("model_set", "")
            if not isinstance(model_set, str) or (model_set and not TIER_RX.match(model_set)):
                return self._error(400, "model_set names one of the layer's tiers")
            if not isinstance(data.get("confirm"), str) or not data["confirm"]:
                return self._json({"error": "the preview is stale: open it again"}, 409)
            code, answer = model_.integrate_start(data["layer"], data["run"], data["confirm"], model_set)
        else:
            who = data.get("who")
            if not isinstance(who, str) or not who.strip() or len(who) > INTEGRATE_WHO_CHARS or not who.isprintable():
                return self._error(400, f"who names who approves: 1–{INTEGRATE_WHO_CHARS} printable characters on one line")
            code, answer = model_.integrate_approve(data["layer"], data["run"], who.strip())
        return self._json(answer, code)

    def _integrate_preview(self, query: dict[str, list[str]]) -> None:
        """GET /api/integrate/preview?layer=K&run=ID: what the integration would do (read-only)."""
        code, answer = self.server.model.integrate_preview(self._q(query, "layer"), self._q(query, "run"))
        return self._json(answer, code)

    def _evidence(self, query: dict[str, list[str]]) -> None:
        """GET /api/settle/evidence?layer=K&run=ID&kind=landed|replaced: evidence for a settlement (read-only)."""
        code, answer = self.server.model.evidence(self._q(query, "layer"), self._q(query, "run"), self._q(query, "kind"))
        return self._json(answer, code)

    def _again_request(self, layer: Layer, data: dict[str, Any]) -> tuple[dict[str, Any] | None, str, int]:
        """The body {layer, again: RUN, request_key, confirm_duplicate, confirm_ships} (no target, no prompt)
        → (the request rebuilt from RUN's records, "", 0) or (None, why, HTTP status)."""
        if not set(data) <= {"layer", "again", "request_key", "confirm_duplicate", "confirm_ships"}:
            return None, "start again takes layer, again, request_key, confirm_duplicate and confirm_ships only", 400
        key = data.get("request_key")
        if not isinstance(key, str) or not START_KEY.fullmatch(key):
            return None, "request_key: 8 to 64 letters, digits, - or _", 400
        request, code, why = self.server.model.again_request(layer, data.get("again"), key)
        if request is None:
            return None, why, code
        return {**request, "request_key": key, "confirm_duplicate": data.get("confirm_duplicate") is True,
                "confirm_ships": data.get("confirm_ships") is True}, "", 0

    def _start_request(self, layer: Layer, data: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """The body of POST /api/start, checked against the layer's startables → (request, "") or (None, why)."""
        catalog = startables(layer)
        decider = data.get("decider") or ""
        if decider not in ("", *DECIDER_MODES):
            return None, f"decider: one of {', '.join(DECIDER_MODES)}"
        prompt = data.get("prompt", "")
        if not isinstance(prompt, str):
            return None, "prompt must be a string"
        target = data.get("target")
        if target is None and isinstance(data.get("text"), str):
            split = split_slash(data["text"], catalog)
            if split is None:
                known = [w["name"] for w in catalog["workflows"]] + [c["name"] for c in catalog["commands"]]
                word = (data["text"].split() or [""])[0]
                return None, (f"no workflow or command named {clip(word, 80)!r}; known: "
                              + ", ".join(f"/{n}" for n in dict.fromkeys(known)))
            target, rest = split
            prompt = "\n\n".join(part for part in (rest, prompt.strip()) if part)
        if not isinstance(target, dict) or target.get("kind") not in ("workflow", "command") \
                or not isinstance(target.get("name"), str):
            return None, 'expected target {"kind": "workflow"|"command", "name"} or text "/NAME …"'
        names = [w["name"] for w in catalog["workflows"]] if target["kind"] == "workflow" \
            else [c["name"] for c in catalog["commands"]]
        if target["name"] not in names:
            return None, f"no {target['kind']} named {clip(target['name'], 80)!r}; known: {', '.join(names) or '(none)'}"
        if target["kind"] == "workflow" and not prompt.strip():
            return None, "a workflow needs a prompt"
        key = data.get("request_key")
        if not isinstance(key, str) or not START_KEY.fullmatch(key):
            return None, "request_key: 8 to 64 letters, digits, - or _"
        klass = data.get("class") or None
        if klass is not None and klass not in START_CLASSES:
            return None, f"class: one of {', '.join(START_CLASSES)}"
        model_set = data.get("model_set") or None
        if model_set is not None and (not isinstance(model_set, str) or model_set not in catalog["tiers"]):
            return None, f"model_set: one of {', '.join(catalog['tiers'])}"
        models = data.get("models") or {}
        if not isinstance(models, dict):
            return None, "models: {tier: model id}"
        models = {k: v for k, v in models.items() if v not in ("", None)}
        for tier, model_id in models.items():
            try:
                check_setting(tier, model_id, catalog["tiers"])
            except ValueError as exc:
                return None, str(exc)
        issue = data.get("issue") or "none"
        if issue not in START_ISSUE:
            return None, f"issue: one of {', '.join(START_ISSUE)}"
        if issue != "none" and not catalog["issue"]["available"]:
            return None, "the layer has no forge (forge none): the request cannot be logged as an issue"
        if issue == "trigger" and (target["kind"] != "workflow" or target["name"] not in catalog["issue"]["trigger"]):
            return None, f"no cron or webhook trigger starts {target['name']} from an issue"
        return {"target": {"kind": target["kind"], "name": target["name"]}, "prompt": prompt, "request_key": key,
                "class": klass, "model_set": model_set, "models": models, "issue": issue, "decider": decider,
                "confirm_duplicate": data.get("confirm_duplicate") is True,
                "confirm_ships": data.get("confirm_ships") is True}, ""

    def _startable(self, query: dict[str, list[str]]) -> None:
        layer = self.server.model.layers.get(self._q(query, "layer"))
        if layer is None:
            return self._error(404, "unknown layer")
        mode = str(self.server.model.standin_view(layer).get("mode") or "off")
        return self._json({"layer": layer.key, **startables(layer, mode), "start": start_status(layer, self.server.model)})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._host_ok():
            return self._error(403, "bad host")
        allowed, set_cookie = self._authorised(query)
        if not allowed:
            return self._error(401, "token required")
        path = parsed.path
        try:
            if path in {"/", "/index.html"}:
                return self._static("index.html", set_cookie)
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):], False)
            if path == "/api/snapshot":
                return self._json(self.server.model.snapshot())
            if path == "/api/events":
                return self._events()
            if path == "/api/run":
                return self._run(query)
            if path == "/api/step":
                return self._step(query)
            if path == "/api/rawline":
                return self._rawline(query)
            if path == "/api/tail":
                return self._tail(query)
            if path == "/api/layer":
                return self._layer(query)
            if path == "/api/startable":
                return self._startable(query)
            if path in ("/api/start", "/api/models", "/api/promotion", "/api/settle", "/api/integrate", "/api/decider",
                        "/api/decision", "/api/standin"):
                return self._error(405, "POST only")
            if path == "/api/integrate/preview":
                return self._integrate_preview(query)
            if path == "/api/settle/evidence":
                return self._evidence(query)
            if path == "/api/workspace":
                return self._workspace(query)
            if path == "/api/repo":
                return self._repo(query)
            if path == "/api/job":
                return self._job(query)
            if path == "/api/job/doc":
                return self._job_doc(query)
            if path == "/api/job/journal":
                return self._job_journal(query)
            if path == "/api/job/log":
                return self._job_log(query)
            if path == "/api/job/file":
                return self._job_file(query)
            if path == "/api/doc":
                return self._doc(query)
            if path == "/api/doc/download":
                return self._doc_download(query)
            if path == "/api/doc/zip":
                return self._doc_zip(query)
            if path.startswith("/files/"):
                return self._file(path[len("/files/"):])
            if path == "/healthz":
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _static(self, name: str, set_cookie: bool) -> None:
        if not SAFE_NAME.match(name) or "/" in name:
            return self._error(404, "not found")
        target = self.server.static_dir / name
        if not target.is_file():
            return self._error(404, "not found")
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
        extra = {}
        if name.endswith(".html"):
            extra["Content-Security-Policy"] = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                                "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
                                                "font-src 'self'; base-uri 'none'; form-action 'none'; "
                                                "frame-ancestors 'none'")
        if set_cookie and self.server.token:
            extra["Set-Cookie"] = f"al_dashboard_token={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
        self._send(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream"), extra)

    def _q(self, query: dict[str, list[str]], name: str) -> str:
        return (query.get(name) or [""])[0]

    def _resolve_run(self, query: dict[str, list[str]]) -> tuple[Layer, RunFiles] | None:
        layer = self.server.model.layers.get(self._q(query, "layer"))
        run_id = self._q(query, "run")
        if layer is None or not RUN_ID.match(run_id):
            self._error(404, "no such run")
            return None
        run = RunFiles(layer, run_id)
        if not run.dir.is_dir():
            self._error(404, "no such run")
            return None
        return layer, run

    def _agent_dir(self, run: RunFiles, name: str) -> Path | None:
        if not SAFE_NAME.match(name or ""):
            return None
        path = run.dir / name
        if path.is_symlink() or not path.is_dir():
            return None
        if not str(Path(os.path.realpath(path))).startswith(os.path.realpath(run.dir) + os.sep):
            return None
        return path

    def _run(self, query: dict[str, list[str]]) -> None:
        detail = self.server.model.run_detail(self._q(query, "layer"), self._q(query, "run"))
        if detail is None:
            return self._error(404, "no such run")
        self._json(detail)

    def _step(self, query: dict[str, list[str]]) -> None:
        resolved = self._resolve_run(query)
        if not resolved:
            return
        layer, run = resolved
        directory = self._agent_dir(run, self._q(query, "dir"))
        if directory is None:
            return self._error(404, "no such step")
        state = read_json(run.dir / "state.json") or {}
        short = layer.shortener(state)
        raw = directory / "raw.jsonl"
        self._json({"dir": directory.name, "prompt": short(read_text(directory / "prompt.md")),
                    "output": short(read_text(directory / "output.txt")),
                    "stderr": short(read_text(directory / "stderr.log", cap=20_000)),
                    "raw_bytes": (stat_sig(raw) or (0, 0))[1],
                    "raw_url": f"/files/{layer.key}/agents/{run.id}/{directory.name}/raw.jsonl"})

    def _rawline(self, query: dict[str, list[str]]) -> None:
        resolved = self._resolve_run(query)
        if not resolved:
            return
        _layer, run = resolved
        directory = self._agent_dir(run, self._q(query, "dir"))
        try:
            wanted = int(self._q(query, "line"))
        except ValueError:
            wanted = -1
        if directory is None or wanted < 1:
            return self._error(404, "no such line")
        try:
            with (directory / "raw.jsonl").open("rb") as handle:
                for number, line in enumerate(handle, start=1):
                    if number == wanted:
                        text = line.decode("utf-8", errors="replace").strip()
                        try:
                            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                        except ValueError:
                            pass
                        return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
        except OSError:
            pass
        self._error(404, "no such line")

    def _layer(self, query: dict[str, list[str]]) -> None:
        detail = self.server.model.layer_detail(self._q(query, "layer"))
        if detail is None:
            return self._error(404, "no such layer")
        self._json(detail)

    def _workspace(self, query: dict[str, list[str]]) -> None:
        model = self.server.model
        refreshed = False
        if self._q(query, "refresh") == "1" and model.workspace is not None:
            refreshed = model.workspace.request_refresh()
        self._json({**model.workspace_view(), "refreshed": refreshed, "al": {"found": model.al is not None,
                    "looked": model.al_looked}, "actions": model.actions_state()})

    def _repo(self, query: dict[str, list[str]]) -> None:
        detail = self.server.model.repo_detail(self._q(query, "repo"))
        if detail is None:
            return self._error(404, "unknown repository")
        self._json(detail)

    def _job(self, query: dict[str, list[str]]) -> None:
        jobs = self.server.model.jobs
        job = jobs.read(self._q(query, "id")) if jobs else None
        if job is None:
            return self._error(404, "no such job")
        job = jobs.settle(job)
        directory = jobs.dir(str(job.get("id") or ""))
        if directory is None:
            return self._error(404, "no such job")
        self._json({**job_detail(job, directory), "docs": jobs.docs(str(job.get("id")))})

    def _job_step(self, query: dict[str, list[str]], which: str) -> tuple[dict[str, Any], Path, Path] | None:
        """A file of one step of a job (`log` or `journal`): the id matches JOB_ID, the step is one of the
        job's, and the file is a regular file inside the job directory; else a 404 is sent and None returned."""
        jobs = self.server.model.jobs
        directory = jobs.dir(self._q(query, "id")) if jobs else None
        job = jobs.read(self._q(query, "id")) if jobs and directory else None
        step = self._q(query, "step")
        names = [s.get("name") for s in (job or {}).get("steps") or [] if isinstance(s, dict)]
        files = step_files(directory, step) if directory is not None and step in names else {}
        if job is None or directory is None or not files or not confined_file(directory, files[which]):
            self._error(404, "no such step file")
            return None
        return job, directory, files[which]

    def _from(self, query: dict[str, list[str]], name: str = "from") -> int | None:
        value = self._q(query, name)
        return int(value) if value.isdigit() else None

    def _job_journal(self, query: dict[str, list[str]]) -> None:
        """GET /api/job/journal?id&step&from=N (or &tail=N): a step's journal events from index N, at most
        JOURNAL_PAGE per answer; `next_from` is where the next answer starts."""
        found = self._job_step(query, "journal")
        if found is None:
            return None
        events = journal_events(found[2])
        tail = self._from(query, "tail")
        start = self._from(query) if self._from(query) is not None else max(0, len(events) - tail) if tail is not None else 0
        page = events[start:start + JOURNAL_PAGE]
        self._json({"events": page, "from": start, "next_from": start + len(page), "total": len(events)})

    def _job_log(self, query: dict[str, list[str]]) -> None:
        """GET /api/job/log?id&step&from=N (or &tail=N): a step's log as ANSI-stripped lines from line N,
        the home directory as ~, at most LOG_PAGE_LINES lines or LOG_PAGE_BYTES per answer."""
        found = self._job_step(query, "log")
        if found is None:
            return None
        job, _directory, path = found
        running = any(s.get("name") == self._q(query, "step") and s.get("status") == "running"
                      for s in job.get("steps") or [] if isinstance(s, dict))
        try:
            data = path.read_bytes()
        except OSError:
            return self._error(404, "no such step file")
        raw = data.split(b"\n")
        if raw and (raw[-1] == b"" or running):     # a running step's last line without its newline is not done
            raw = raw[:-1]
        total = len(raw)
        tail = self._from(query, "tail")
        start = self._from(query) if self._from(query) is not None else max(0, total - tail) if tail is not None else 0
        short, lines, size = Shortener(), [], 0
        for item in raw[start:]:
            if len(lines) >= LOG_PAGE_LINES or size >= LOG_PAGE_BYTES:
                break
            size += len(item) + 1
            lines.append(short(strip_ansi(item.decode("utf-8", "replace"))))
        self._json({"lines": lines, "from": start, "next_from": start + len(lines), "total": total})

    def _job_file(self, query: dict[str, list[str]]) -> None:
        """GET /api/job/file?id&name: a download of the job's output.log or of one of its steps' files
        (steps/<step>.log, steps/<step>.journal.jsonl); nothing else, never through a symlink."""
        jobs = self.server.model.jobs
        directory = jobs.dir(self._q(query, "id")) if jobs else None
        job = jobs.read(self._q(query, "id")) if jobs and directory else None
        name = self._q(query, "name")
        allowed = {"output.log"} | {f"steps/{s.get('name')}{suffix}" for s in (job or {}).get("steps") or []
                                    if isinstance(s, dict) and SAFE_NAME.match(str(s.get("name") or ""))
                                    for suffix in (".log", ".journal.jsonl")}
        if job is None or directory is None or name not in allowed or not confined_file(directory, directory / name):
            return self._error(404, "no such file")
        target = directory / name
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", f"job-{directory.name}-{target.name}")
        try:
            size = target.stat().st_size
            handle = target.open("rb")
        except OSError:
            return self._error(404, "no such file")
        with handle:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
            self.end_headers()
            if self.command == "HEAD":
                return None
            remaining = size
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return None

    def _job_doc(self, query: dict[str, list[str]]) -> None:
        """A file of a job's docs/ (and nothing else): a regular file whose real path stays there."""
        jobs = self.server.model.jobs
        directory = jobs.dir(self._q(query, "id")) if jobs else None
        name = self._q(query, "name")
        if directory is None or not SAFE_NAME.match(name) or not name.lower().endswith(DOC_SUFFIXES):
            return self._error(404, "no such document")
        docs = os.path.realpath(directory / "docs")
        target = directory / "docs" / name
        if target.is_symlink() or not target.is_file() or not os.path.realpath(target).startswith(docs + os.sep):
            return self._error(404, "no such document")
        try:
            size = target.stat().st_size
            text = target.read_bytes()[:DOC_VIEW_CAP].decode("utf-8", errors="replace")
        except OSError:
            return self._error(404, "no such document")
        self._json({"name": name, "bytes": size, "text": text, "too_large": size > DOC_VIEW_CAP, "viewable": True,
                    "type": _doc_type(target.suffix.lower())})

    def _doc_target(self, query: dict[str, list[str]]) -> tuple[Layer, RunFiles, dict[str, Any], Path] | None:
        """A document the run lists, or a 404 (never 403: what is not listed does not exist here)."""
        resolved = self._resolve_run(query)
        if not resolved:
            return None
        layer, run = resolved
        relative = self._q(query, "path")
        found = None
        if relative and "\x00" not in relative and "\\" not in relative and not relative.startswith("/"):
            found = run_document(layer, run.id, relative)
        if found is None:
            self._error(404, "no such document")
            return None
        entry, target = found
        return layer, run, entry, target

    def _doc(self, query: dict[str, list[str]]) -> None:
        resolved = self._doc_target(query)
        if not resolved:
            return
        _layer, _run, entry, target = resolved
        text = ""
        try:
            if entry["viewable"]:
                text = target.read_bytes()[:DOC_VIEW_CAP].decode("utf-8", errors="replace")
        except OSError:
            return self._error(404, "no such document")
        self._json({**entry, "text": text, "too_large": entry["bytes"] > DOC_VIEW_CAP,
                    "viewable": bool(entry["viewable"])})

    def _doc_download(self, query: dict[str, list[str]]) -> None:
        resolved = self._doc_target(query)
        if not resolved:
            return
        _layer, _run, entry, target = resolved
        try:
            body = target.read_bytes()
        except OSError:
            return self._error(404, "no such document")
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", entry["filename"])
        self._send(200, body, DOC_TYPES.get(target.suffix.lower(), "application/octet-stream"),
                   {"Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Security-Policy": "sandbox; default-src 'none'"})

    def _doc_zip(self, query: dict[str, list[str]]) -> None:
        resolved = self._resolve_run(query)
        if not resolved:
            return
        layer, run = resolved
        state = read_json(run.dir / "state.json")
        docs = run_documents(layer, run.id, state if isinstance(state, dict) else None)
        if not docs:
            return self._error(404, "no documents")
        buffer = io.BytesIO()
        total, skipped = 0, []
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for doc in docs:
                try:
                    target = safe_layer_file(layer, doc["path"])
                    if total + target.stat().st_size > DOC_ZIP_CAP:
                        skipped.append(doc["path"])
                        continue
                    data = target.read_bytes()
                except (Refused, OSError):
                    continue
                total += len(data)
                archive.writestr(doc["path"], data)
            if skipped:
                archive.writestr("SKIPPED.txt", "Not included (the zip is limited to "
                                 f"{DOC_ZIP_CAP} bytes); download them one by one:\n" + "\n".join(skipped) + "\n")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{layer.key}-{run.id}-documents.zip")
        self._send(200, buffer.getvalue(), "application/zip",
                   {"Content-Disposition": f'attachment; filename="{name}"',
                    "Content-Security-Policy": "sandbox; default-src 'none'"})

    def _file(self, rest: str) -> None:
        rest = urllib.parse.unquote(rest)
        key, _, relative = rest.partition("/")
        layer = self.server.model.layers.get(key)
        if layer is None:
            return self._error(404, "no such layer")
        try:
            target = safe_layer_file(layer, relative)
        except Refused as exc:
            return self._error(404 if str(exc) == "not found" else 403, str(exc))
        content_type = INLINE_TYPES.get(target.suffix.lower(), "application/octet-stream")
        size = target.stat().st_size
        extra = {"Content-Security-Policy": "sandbox; default-src 'none'", "Accept-Ranges": "bytes"}
        if target.suffix.lower() == ".html":
            extra["Content-Security-Policy"] = HTML_CSP
        if content_type == "application/octet-stream":
            extra["Content-Disposition"] = f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]", "_", target.name)}"'
        if "download" in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query):
            extra["Content-Disposition"] = f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]", "_", target.name)}"'
        start, end = 0, size - 1
        status = 200
        match = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if match and size:
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            elif match.group(2):
                start = max(0, size - int(match.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with target.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # ---------------------------------------------------------------- SSE
    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._security_headers()
        self.end_headers()
        self.close_connection = True

    def _write(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _events(self) -> None:
        model = self.server.model
        q = model.hub.subscribe()
        try:
            self._sse_start()
            if not self._write(b"retry: 2000\n\n" + sse_frame("snapshot", json_bytes(model.snapshot()))):
                return
            last_beat = time.monotonic()
            while not self.server.shutting_down.is_set() and not getattr(q, "dead", False):
                try:
                    kind, payload, serial = q.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last_beat > 15:
                        if not self._write(b": keepalive\n\n"):
                            return
                        last_beat = time.monotonic()
                    continue
                if not self._write(sse_frame(kind, payload, serial)):
                    return
        finally:
            model.hub.unsubscribe(q)

    def _tail(self, query: dict[str, list[str]]) -> None:
        """Stream one file as it grows. what=transcript (an agent's raw.jsonl, normalised),
        what=check (checks/<file>, ANSI → segments), what=runlog, or what=job (a workspace action's
        output.log, job=<id>). Resumes from Last-Event-ID (`offset:line`), so a reconnect only gets what
        it missed."""
        what = self._q(query, "what")
        if what == "job":
            jobs = self.server.model.jobs
            directory = jobs.dir(self._q(query, "job")) if jobs else None
            if directory is None:
                return self._error(404, "no such job")
            path = directory / "output.log"
            if path.is_symlink():
                return self._error(403, "symlink")
            return self._stream(path, what, Shortener(), query)    # the home directory as ~
        resolved = self._resolve_run(query)
        if not resolved:
            return
        layer, run = resolved
        state = read_json(run.dir / "state.json") or {}
        short = layer.shortener(state)
        if what == "transcript":
            directory = self._agent_dir(run, self._q(query, "dir"))
            if directory is None:
                return self._error(404, "no such step")
            path = directory / "raw.jsonl"
        elif what == "check":
            name = self._q(query, "file")
            if not SAFE_NAME.match(name) or not name.endswith(".log"):
                return self._error(404, "no such log")
            path = run.dir / "checks" / name
            if path.is_symlink():
                return self._error(403, "symlink")
        elif what == "runlog":
            path = run.dir / "run.log"
        else:
            return self._error(400, "what=transcript|check|runlog|job")
        return self._stream(path, what, short, query)

    def _stream(self, path: Path, what: str, short: Callable[[str], str], query: dict[str, list[str]]) -> None:
        offset, line_no = 0, 0
        last_id = self.headers.get("Last-Event-ID", "") or self._q(query, "from")
        if re.match(r"^\d+:\d+$", last_id):
            offset, line_no = (int(x) for x in last_id.split(":"))
        tail = Tail(path, offset=offset, line_no=line_no, settle=TRANSCRIPT_SETTLE if what == "transcript" else 1.5)
        transcript = Transcript(short)
        if offset and what == "transcript":
            # rebuild aggregates and tool names quietly from what the client already has
            head = Tail(path, settle=0)
            _reset, earlier = head.read(max_bytes=offset)
            transcript.feed([item for item in earlier if item[1] < offset])
        ansi = AnsiState()
        self._sse_start()
        if not self._write(b"retry: 1500\n\n"):
            return
        last_beat = time.monotonic()
        last_meta: Any = None
        while not self.server.shutting_down.is_set():
            reset, lines = tail.read()
            if reset:
                transcript.reset()
                ansi = AnsiState()
                if not self._write(sse_frame("reset", b"{}", f"0:0")):
                    return
            if lines:
                event_id = f"{tail.offset}:{tail.line_no}"
                if what == "transcript":
                    for start in range(0, len(lines), 400):
                        chunk = lines[start:start + 400]
                        rows = transcript.feed(chunk)
                        end_line = chunk[-1]
                        chunk_id = f"{end_line[1] + len(end_line[2].encode('utf-8')) + 1}:{end_line[0]}" \
                            if start + 400 < len(lines) else event_id
                        if rows and not self._write(sse_frame("rows", json_bytes(rows), chunk_id)):
                            return
                else:
                    # a partial line shown early ends without a newline, so its rest continues the same line
                    text = "\n".join(short(line[2]) for line in lines) + ("" if tail.open else "\n")
                    segments, ansi = ansi_segments(text, ansi)
                    first = lines[0][0]
                    if not self._write(sse_frame("text", json_bytes({"from": first, "segments": segments,
                                                                     "lines": len(lines)}), event_id)):
                        return
                last_beat = time.monotonic()
            meta = transcript.agg.as_dict() if what == "transcript" else {"lines": tail.line_no}
            meta["bytes"] = tail.offset
            if meta != last_meta:
                if not self._write(sse_frame("meta", json_bytes(meta))):
                    return
                last_meta = meta
            if time.monotonic() - last_beat > 15:
                if not self._write(b": keepalive\n\n"):
                    return
                last_beat = time.monotonic()
            self.server.shutting_down.wait(0.5)


# ============================================================================ entry point

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dashboard.py", description="Live work dashboard for al layers: "
                                     "watches runs; writes model settings, starts work and runs al "
                                     "actions (off with --read-only / --no-start / --no-actions).")
    parser.add_argument("--layer", action="append", default=[], type=Path, metavar="DIR",
                        help="a layer directory (contains agentic-layer.json); repeatable")
    parser.add_argument("--scan", action="append", default=[], type=Path, metavar="DIR",
                        help="find layers below DIR (also in-repo ones); repeatable; rescanned every 10 s")
    parser.add_argument("--scan-depth", type=int, default=4, help="how deep --scan looks (default 4)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--token", default=os.environ.get("AL_DASHBOARD_TOKEN") or None,
                        help="require this token on every request (needed for a non-loopback --bind); "
                             "also read from AL_DASHBOARD_TOKEN")
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    parser.add_argument("--poll", type=float, default=0.75, help="seconds between file polls (default 0.75)")
    parser.add_argument("--no-podman", action="store_true", help="never call podman")
    parser.add_argument("--podman-socket", default=None, metavar="PATH",
                        help="opt-in: read container status from this podman API socket (when no podman CLI)")
    parser.add_argument("--in-container", action="store_true",
                        help="running in a container: pids and locks of the host are not visible")
    parser.add_argument("--read-only", action="store_true",
                        help="refuse every change: no model settings, no starting work, no actions")
    parser.add_argument("--no-start", action="store_true",
                        help="never start work from the page (the Models panel still writes)")
    parser.add_argument("--allow-start-in-container", action="store_true",
                        help="with --in-container: allow starting work (only for an image that has uv, the "
                             "checkout and a writable layer)")
    parser.add_argument("--workspace", action="append", default=[], type=Path, metavar="DIR",
                        help="a folder of git repositories side by side: list them with their al state and act on them; "
                             "repeatable (default: the folder holding each --layer's repository)")
    parser.add_argument("--workspace-depth", type=int, default=WORKSPACE_DEPTH, metavar="N",
                        help=f"how deep below a workspace folder repositories are looked for (default {WORKSPACE_DEPTH})")
    parser.add_argument("--no-workspace", action="store_true", help="list no repositories, only layers")
    parser.add_argument("--no-actions", action="store_true", help="run no al action from the page")
    parser.add_argument("--no-stand-in", action="store_true",
                        help="the stand-in's loop does not run in this process; the panel still shows and switches it")
    parser.add_argument("--stand-in-only", action="store_true",
                        help="run the stand-in's loop without the page: no HTTP server (what the generated "
                             "systemd unit al-<name>-stand-in.service runs)")
    parser.add_argument("--jobs-dir", type=Path, default=None, metavar="DIR",
                        help="where actions are recorded (default $XDG_STATE_HOME/al/dashboard-jobs)")
    parser.add_argument("--al", type=Path, default=None, metavar="PATH",
                        help="the al tool the actions run (default: tools/al beside the kit, else al on PATH)")
    parser.add_argument("--author-agent", choices=("claude-code", "mock"), default="claude-code",
                        help="the agent `al author` uses in the create and adopt actions (default claude-code)")
    parser.add_argument("--author-mock", type=Path, default=None, metavar="FILE",
                        help="`al author --agent mock --mock FILE` in the create and adopt actions (tests)")
    args = parser.parse_args(argv)
    if not args.layer and not args.scan and not args.workspace:
        parser.error("give at least one --layer DIR, --scan DIR or --workspace DIR")
    if args.stand_in_only and (args.read_only or args.no_stand_in or args.in_container):
        parser.error("--stand-in-only runs the stand-in's loop: it cannot be combined with --read-only, "
                     "--no-stand-in or --in-container")
    if not args.stand_in_only and not is_loopback(args.bind) and not args.token:
        parser.error(f"--bind {args.bind} is not a loopback address: refusing without --token")
    return args


def start_banner(args: argparse.Namespace) -> str:
    if args.read_only:
        return "off (--read-only)"
    if args.no_start:
        return "off (--no-start)"
    if args.in_container and not args.allow_start_in_container:
        return "off (in a container)"
    return "work can be started from the Layers view"


def workspace_roots(args: argparse.Namespace) -> list[Path]:
    """--workspace DIR …, else the folder holding each --layer's repository (its codebase root's parent)."""
    if args.no_workspace or args.in_container:
        return []
    if args.workspace:
        return [Path(w) for w in args.workspace if Path(w).is_dir()]
    roots = []
    for layer in args.layer:
        ir = read_json(Path(layer) / LAYER_FILE)
        root = ((ir or {}).get("codebase") or {}).get("root") if isinstance(ir, dict) else None
        if isinstance(root, str) and root:
            app = (Path(layer) / root).resolve()
            if app.is_dir():
                roots.append(app.parent)
    return list(dict.fromkeys(roots))


def make_model(args: argparse.Namespace) -> Model:
    probe = ContainerProbe(args.podman_socket, enabled=not args.no_podman)
    roots = workspace_roots(args)
    al, looked = find_al_tool(args.al)
    workspace = jobs = None
    if roots:
        workspace = Workspace(roots, args.workspace_depth, load_detectors(al))
        workspace.refresh()
        jobs = Jobs(args.jobs_dir or default_jobs_dir())
        jobs.load()
    author_flags = (["--agent", "mock", "--mock", str(Path(args.author_mock).resolve())] if args.author_mock
                    else ["--agent", args.author_agent] if args.author_agent != "claude-code" else [])
    model = Model(args.layer, args.scan, poll=args.poll, scan_depth=args.scan_depth, probe=probe,
                  in_container=args.in_container, read_only=args.read_only, no_start=args.no_start,
                  allow_start=args.allow_start_in_container, workspace=workspace, jobs=jobs, al=al,
                  al_looked=looked, no_actions=args.no_actions or args.in_container, author_flags=author_flags,
                  no_standin=args.no_stand_in)
    model.discover()
    probe.poll(want_stats=True)   # so the first snapshot already knows the containers
    model.tick()
    return model


def make_server(args: argparse.Namespace, static_dir: Path = STATIC_DIR) -> DashboardServer:
    model = make_model(args)
    return DashboardServer((args.bind, args.port), model, args.token, static_dir, read_only=args.read_only,
                           no_start=args.no_start, allow_start=args.allow_start_in_container)


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw[:1] == ["--run-job"]:   # the detached runner of one workspace action (spawned by the server)
        if len(raw) != 2:
            print("usage: dashboard.py --run-job DIR", file=sys.stderr)
            return 2
        return run_job(Path(raw[1]))
    args = parse_args(argv)
    if args.stand_in_only:   # the stand-in's loop without the page: no HTTP server, until SIGTERM
        model = make_model(args)
        signal.signal(signal.SIGTERM, lambda *_args: model.stop.set())
        signal.signal(signal.SIGINT, lambda *_args: model.stop.set())
        print(f"stand-in loop for {len(model.layers)} layer(s)", flush=True)
        model.run_forever()
        return 0
    try:
        server = make_server(args)
    except OSError as exc:
        print(f"dashboard: cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1
    watcher = threading.Thread(target=server.model.run_forever, name="watcher", daemon=True)
    watcher.start()

    def stop(*_args: Any) -> None:
        server.shutting_down.set()
        server.model.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    host = args.bind if ":" not in args.bind else f"[{args.bind}]"
    url = f"http://{host if host not in {'0.0.0.0', '[::]'} else '127.0.0.1'}:{server.server_address[1]}/"
    if args.token:
        url += f"?token={urllib.parse.quote(args.token)}"
    layers = ", ".join(f"{l['key']} ({l['runs']} runs)" for l in server.model.layer_list()) or "none yet"
    where = f"listening inside the container on port {server.server_address[1]}" if args.in_container else f"on {url}"
    ws = server.model.workspace
    workspace = (f"{len(ws.entries)} repositories in {', '.join(home_short(str(r)) for r in ws.roots)} "
                 f"(depth {ws.depth})" if ws else "off")
    actions = server.model.actions_state()
    print(f"al dashboard {where}\n  watching: {layers}\n"
          f"  models: {'read-only' if args.read_only else 'switchable from the Now view'}\n"
          f"  start: {start_banner(args)}\n"
          f"  stand-in: loop {server.model.standin_loop()}\n"
          f"  workspace: {workspace}\n"
          f"  actions: {'on (al: ' + home_short(str(server.model.al)) + ')' if actions['enabled'] else 'off: ' + actions['why']}\n"
          f"  containers: {server.model.probe.info()['note']}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.shutting_down.set()
        server.model.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
