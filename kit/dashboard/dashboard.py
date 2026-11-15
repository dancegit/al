#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""al work dashboard: a live, read-only view of what the agents of one or more layers are doing.

  dashboard.py --layer path/to/app.al [--layer …] [--scan DIR …] [--port 8765] [--bind 127.0.0.1]
               [--token T] [--open]

It watches each layer's `agents/` (run state, run.log, step transcripts, check logs, slot locks,
port leases) and `artefacts/` by polling mtimes and sizes, and serves one page that updates over
Server-Sent Events. Standard library only; nothing is ever written to a layer.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hmac
import http.client
import ipaddress
import json
import math
import mimetypes
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
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
SERVED_TREES = ("agents", "artefacts", "specs")
PREVIEW_CHARS = 1600
TRANSCRIPT_SETTLE = 20.0   # a JSON line without its newline is held this long before it counts as truncated
TEXT_CAP = 400_000
POOLS = ("runs", "agents", "checks")
POOL_DEFAULTS = {"runs": ("maxRuns", 4), "agents": ("maxAgents", 2), "checks": ("maxChecks", 2)}


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
                if replacement:
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
    until it is finished (or the file has been quiet for `settle` seconds). A file that shrinks or
    is replaced (new inode) restarts from the beginning and reports `reset`. Opens and closes the
    file on every read, so no handle is ever held between polls."""

    def __init__(self, path: Path, offset: int = 0, line_no: int = 0, settle: float = 2.0) -> None:
        self.path = path
        self.offset = offset
        self.line_no = line_no
        self.inode: int | None = None
        self.pending = b""
        self.settle = settle

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
            self.offset, self.line_no, self.pending = 0, 0, b""
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
        if self.pending and (time.time() - info.st_mtime) >= self.settle and start + len(data) >= info.st_size:
            text = self.pending.decode("utf-8", errors="replace")
            self.line_no += 1
            lines.append((self.line_no, self.offset, text))
            self.offset += len(self.pending)
            self.pending = b""
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
    _message_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "model": self.model, "cost": self.cost, "turns": self.turns,
                "duration_ms": self.duration_ms, "tokens": dict(self.tokens), "final": self.final,
                "is_error": self.is_error, "events": self.events, "tools": self.tools, "errors": self.errors,
                "action": self.action, "first_ts": self.first_ts, "last_ts": self.last_ts,
                "thinking_tokens": self.thinking_tokens}

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
    tallest = max((len(column) for column in columns), default=1)
    has_loop = any(s.get("kind") == "check" and s.get("resolve") for s in steps)
    has_back = any(s.get("kind") == "review" and s.get("patch") and s.get("implement") for s in steps)
    top_margin = 40 if has_loop else 16
    content = tallest * node_h + (tallest - 1) * gap_y
    height = top_margin + content + (58 if has_back else 16)
    middle = top_margin + content / 2
    nodes: list[dict[str, Any]] = []
    for col, column in enumerate(columns):
        span = len(column) * node_h + (len(column) - 1) * gap_y
        top = middle - span / 2
        for row, node in enumerate(column):
            node.update(col=col, row=row, x=pad + col * (node_w + gap_x), y=round(top + row * (node_h + gap_y), 1),
                        w=node_w, h=node_h)
            nodes.append(node)
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
    width = pad * 2 + max(1, len(columns)) * (node_w + gap_x) - gap_x
    return {"nodes": nodes, "edges": edges, "width": width, "height": height,
            "review": {"kind": review_option.get("kind", ""), "reviewers": review_option.get("reviewers", 0),
                       "ship": review_option.get("ship", ""), "maxRetries": review_option.get("maxRetries", 0)}}


def _node(step: dict[str, Any], role: str) -> dict[str, Any]:
    kind = str(step.get("kind", "run"))
    types = {"run": "agent", "check": "check", "review": "review", "ship": "ship", "fan-out": "fan-out"}
    sub = {"run": step.get("command", ""), "check": step.get("cmd", ""), "review": "review",
           "ship": "gate + ship", "fan-out": f"pick {step.get('pick', 'best')}"}.get(kind, "")
    if kind == "run" and step.get("agent"):
        sub = f"{sub} @{step['agent']}"
    return {"id": str(step.get("name", "")), "step": str(step.get("name", "")), "type": types.get(kind, kind),
            "label": str(step.get("name", "")), "sub": str(sub), "when": str(step.get("when") or "always"),
            "role": role}


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

    def explain_file(self) -> str | None:
        for name in EXPLAIN_FILES:
            if (self.path / name).is_file():
                return name
        return None


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


# ============================================================================ runs

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
        items: list[Any] = [stat_sig(self.dir / "state.json"), stat_sig(self.dir / "run.log"), mtime(self.dir)]
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


class RunModel:
    """Builds the summary and the detail of one run from its files."""

    def __init__(self, layer: Layer, run_id: str, aggs: AggCache, now: Callable[[], float] = time.time) -> None:
        self.layer, self.id, self.aggs, self.now = layer, run_id, aggs, now
        self.files = RunFiles(layer, run_id)

    def _short(self, state: dict[str, Any] | None) -> Shortener:
        short = Shortener()
        if state and state.get("worktree"):
            short.add(str(state["worktree"]), "")
        short.add(str(self.layer.path), "layer:")
        return short

    def collect(self) -> dict[str, Any]:
        state, problem = load_state(self.files)
        facts = runlog_facts(self.files.dir / "run.log")
        adw_name = (state or {}).get("adw") or (facts["start"] or {}).get("adw") or "?"
        adw = self.layer.adw(adw_name) or {"name": adw_name, "steps": []}
        ir_steps = [s for s in adw.get("steps") or [] if isinstance(s, dict)]
        step_names = [str(s.get("name")) for s in ir_steps]
        kinds = {str(s.get("name")): str(s.get("kind")) for s in ir_steps}
        records = {str(r.get("name")): r for r in (state or {}).get("steps") or [] if isinstance(r, dict)}
        for name, record in records.items():
            kinds.setdefault(name, str(record.get("kind", "run")))
            if name not in step_names:
                step_names.append(name)
        status = str((state or {}).get("status") or "unknown")
        short = self._short(state)
        running = status == "running"
        now = self.now()
        activities: list[Activity] = []
        agents: list[dict[str, Any]] = []
        for directory in self.files.agent_dirs():
            agg = self.aggs.get(directory / "raw.jsonl", short)
            step, role = agent_group(directory.name, step_names, kinds)
            start = mtime(directory / "prompt.md") or mtime(directory / "raw.jsonl")
            out = mtime(directory / "output.txt")
            raw_m = mtime(directory / "raw.jsonl")
            record = records.get(step, {})
            if out is not None:
                end, astatus = out, "passed"
            elif running and (record.get("status") == "running" or role != "main"):
                end, astatus = None, "running"
            else:
                end, astatus = (raw_m or start), ("failed" if record.get("status") == "failed" or agg.is_error else "done")
            if agg.final and agg.is_error:
                astatus = "failed"
            info = {"dir": directory.name, "step": step, "role": role, "start": start, "end": end,
                    "status": astatus, **agg.as_dict()}
            agents.append(info)
            activities.append(Activity(directory.name, "agent", step, role, directory.name, start, end, astatus,
                                       {"cost": agg.cost}))
        # a running step's agent: the newest open one
        open_agents = [a for a in agents if a["status"] == "running"]
        for extra in open_agents[:-1]:
            extra["status"] = "passed" if extra["final"] else "done"
            extra["end"] = extra["end"] or mtime(self.files.dir / extra["dir"] / "raw.jsonl")
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
        # step windows from state
        windows = []
        for name in step_names:
            record = records.get(name)
            if not record:
                continue
            windows.append((name, parse_time(record.get("started")), parse_time(record.get("ended")),
                            str(record.get("status", ""))))
        run_start = parse_time((state or {}).get("started")) or min(
            (a.start for a in activities if a.start), default=None) or mtime(self.files.dir / "run.log")
        run_end = parse_time((state or {}).get("ended")) if not running else None

        def window_of(at: float | None) -> str:
            if at is None:
                return ""
            for name, started, ended, _status in windows:
                if started and started - 0.001 <= at <= (ended or now) + 0.5:
                    return name
            return ""

        # reconstruct check/gate starts: the end of whatever finished just before (sequential runtime)
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
        current_record = next((r for r in reversed(list(records.values())) if r.get("status") == "running"), None)
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
        if running and current_record and current_record.get("kind") == "review":
            open_review = [a for a in activities if a.kind == "check" and a.end is None]
            if not open_review and not any(a["status"] == "running" for a in agents):
                pass
        return {"state": state, "problem": problem, "facts": facts, "adw": adw, "steps": step_names, "kinds": kinds,
                "records": records, "status": status, "running": running, "agents": agents,
                "activities": activities, "attempts": attempts, "run_start": run_start, "run_end": run_end,
                "current": current_record, "live_check": live_check, "now": now}

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
            current = {"step": "", "kind": "", "action": "setting up", "since": start}
        waiting = facts["waiting"] if data["running"] else ""
        ship = state.get("ship") or {}
        review = state.get("review") or {}
        verdict = review.get("verdict") or {}
        failed = next((r for r in state.get("steps") or [] if isinstance(r, dict) and r.get("status") == "failed"), None)
        fanout_children = [c.get("id") for record in (state.get("fanout") or {}).values() if isinstance(record, dict)
                           for c in record.get("candidates") or [] if isinstance(c, dict)]
        container = None
        if facts["container"]:
            container = {"name": facts["container"], "state": "removed" if facts["container_removed"] else
                         ("running" if data["running"] else "unknown"), "source": "run.log"}
        prompt = str(state.get("prompt") or "")
        return {
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
                       "status": str(data["records"].get(name, {}).get("status", "pending"))} for name in data["steps"]],
            "current": current, "waiting": waiting, "container": container, "ports": state.get("ports") or [],
            "outcome": {"ship": ship.get("action", ""), "shipped": ship.get("shipped"), "target": ship.get("target", ""),
                        "url": str(ship.get("url") or (ship.get("pr").get("url", "") if isinstance(ship.get("pr"), dict) else "")),
                        "review": verdict.get("success") if verdict else None, "rounds": len(review.get("rounds") or []),
                        "stopped_at": ship.get("stopped_at", "")},
            "error": clip(str(failed.get("detail", "")), 300) if failed else "",
            "failed_step": failed.get("name") if failed else "",
            "fanout_children": [c for c in fanout_children if c],
            "artefacts": len(state.get("artefacts") or []),
            "agents": len(agents),
            "mtime": max([mtime(self.files.dir / "state.json") or 0, mtime(self.files.dir / "run.log") or 0]),
            **extras,
        }

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
            node["status"] = str(record.get("status", "pending"))
            started, ended = parse_time(record.get("started")), parse_time(record.get("ended"))
            node["duration"] = ((ended or now) - started) if started and (ended or node["status"] == "running") else None
            costs = [a["cost"] for a in group if a["cost"] is not None]
            node["cost"] = round(sum(costs), 4) if costs else None
            node["attempts"] = len(data["attempts"].get(step, [])) if node["type"] == "check" else None
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
            bars.append({"id": f"step:{name}", "step": name, "level": 0, "kind": data["kinds"].get(name, ""),
                         "label": name, "start": started, "end": ended if record.get("status") != "running" else None,
                         "status": str(record.get("status", "")),
                         "cost": next((n["cost"] for n in graph["nodes"] if n["id"] == name), None)})
        for activity in data["activities"]:
            if activity.start is None and activity.end is None:
                continue
            bars.append({"id": f"{activity.kind}:{activity.key}", "step": activity.step, "level": 1,
                         "kind": activity.kind, "role": activity.role, "label": activity.label,
                         "start": activity.start if activity.start is not None else activity.end,
                         "end": activity.end, "status": activity.status, "cost": activity.extra.get("cost"),
                         "ref": activity.key, "check": activity.extra.get("check"), "exit": activity.extra.get("exit")})
        # waiting for a `runs` slot: from the run's start until its first step started (run.log says it waited)
        wait_time = 0.0
        if "runs" in (data["facts"].get("waited") or []) and data["run_start"]:
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
        runlog_tail = read_text(self.files.dir / "run.log", cap=2_000_000).splitlines()[-400:]
        return {
            "summary": summary, "graph": graph, "timeline": timeline,
            "agents": [{**a, "label": a["dir"]} for a in agents],
            "checks": {name: items for name, items in data["attempts"].items()},
            "check_cmds": {**self._declared_check_cmds(data["adw"]),
                           **{name: short(rec.get("cmd", "")) for name, rec in (state.get("checks") or {}).items()}},
            "state": {"branch": state.get("branch", ""), "plan": short(state.get("plan", "")),
                      "worktree": short(state.get("worktree", "")), "ports": state.get("ports") or [],
                      "model_set": state.get("model_set", ""), "class_fixed": state.get("class_fixed"),
                      "source": state.get("source", ""), "prompt": short(state.get("prompt", ""))},
            "review": {"mode": review.get("mode", ""), "success": (review.get("verdict") or {}).get("success"),
                       "summary": short((review.get("verdict") or {}).get("summary", "")), "rounds": rounds},
            "ship": {k: (short(v) if isinstance(v, str) else v) for k, v in ship.items() if k != "gate"},
            "gate": gate, "handoff": handoff, "fanout": fanout, "artefacts": artefacts,
            "runlog": [short(line) for line in runlog_tail],
            "steps_detail": {name: {"detail": short(str(r.get("detail", ""))), "attempts": r.get("attempts"),
                                    "status": r.get("status"), "started": parse_time(r.get("started")),
                                    "ended": parse_time(r.get("ended"))} for name, r in records.items()},
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
                 probe: ContainerProbe | None = None, in_container: bool = False) -> None:
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

    # ---------------------------------------------------------------- discovery
    def discover(self) -> bool:
        paths: list[Path] = []
        for directory in self.layer_dirs:
            if (directory / LAYER_FILE).is_file():
                paths.append(Path(os.path.realpath(directory)))
        paths += find_layers(self.scan_dirs, self.scan_depth)
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

    def layer_list(self) -> list[dict[str, Any]]:
        with self.lock:
            out = []
            for layer in sorted(self.layers.values(), key=lambda l: l.key):
                runs = [s for s in self.summaries.values() if s["layer"] == layer.key]
                out.append({"key": layer.key, "name": layer.name, "dir": self.display_path(layer.path),
                            "description": str(layer.ir.get("description", "")),
                            "adws": [a.get("name") for a in layer.ir.get("adws") or [] if isinstance(a, dict)],
                            "runs": len(runs), "running": sum(1 for r in runs if r["status"] == "running"),
                            "explain": layer.explain_file(), "limits": layer.limits()})
            return out

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
        if now - self.last_scan > (10 if self.scan_dirs else 5) or not self.layers:
            self.last_scan = now
            if self.discover():
                self.hub.publish("layers", self.layer_list())
        locks = None
        with self.lock:
            layers = list(self.layers.values())
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
                    continue  # nothing on disk changed; elapsed time is computed by the page
                try:
                    fresh = RunModel(layer, run_id, self.aggs).summary(self._extras(layer, run_id))
                except Exception as exc:  # one broken run must never stop the watcher
                    fresh = {"key": key, "layer": layer.key, "id": run_id, "status": "unknown", "adw": "?",
                             "problem": f"{type(exc).__name__}: {exc}"[:300], "steps": [], "title": "", "prompt": "",
                             "started": None, "ended": None, "duration": None, "cost": None, "tokens": {},
                             "turns": 0, "current": None, "waiting": "", "container": None, "ports": [],
                             "outcome": {}, "error": "", "fanout_children": [], "artefacts": 0, "mtime": now,
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
            if changed_layer or layer.key not in self.slots or self.ticks % 4 == 0:
                if locks is None:
                    locks = proc_locks()
                runs = [s for s in self.summaries.values() if s["layer"] == layer.key]
                slots = layer_slots(layer, runs, locks, self.in_container)
                if slots != self.slots.get(layer.key):
                    self.slots[layer.key] = slots
                    self.hub.publish("slots", {"layer": layer.key, **slots})
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
        return {**summary, **extra} if extra else summary

    def containers(self) -> dict[str, Any]:
        return self.probe.info()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            runs = [self._decorated(s) for s in self.summaries.values()]
            return {"version": VERSION, "now": time.time(), "layers": self.layer_list(),
                    "runs": sorted(runs, key=lambda r: -(r.get("started") or 0)),
                    "slots": dict(self.slots), "containers": self.containers(),
                    "in_container": self.in_container}

    def run_detail(self, layer_key: str, run_id: str) -> dict[str, Any] | None:
        layer = self.layers.get(layer_key)
        if layer is None or not RUN_ID.match(run_id) or not (layer.path / "agents" / run_id).is_dir():
            return None
        files = RunFiles(layer, run_id)
        sig = files.signature(deep=True)
        key = f"{layer_key}/{run_id}"
        cached = self.detail_cache.get(key)
        if cached and cached[0] == sig and cached[1]["summary"]["status"] != "running":
            return cached[1]
        detail = RunModel(layer, run_id, self.aggs).detail()
        detail["summary"] = self._decorated(detail["summary"])
        if len(self.detail_cache) > 64:
            self.detail_cache.clear()
        self.detail_cache[key] = (sig, detail)
        return detail

    def layer_detail(self, layer_key: str) -> dict[str, Any] | None:
        layer = self.layers.get(layer_key)
        if layer is None:
            return None
        with self.lock:
            runs = [s for s in self.summaries.values() if s["layer"] == layer_key]
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
        return {"layer": next((l for l in self.layer_list() if l["key"] == layer_key), {}), "adws": adws,
                "stats": aggregate(runs), "explain": explain,
                "explain_text": read_text(layer.path / explain, cap=200_000) if explain else ""}

    def run_forever(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — keep watching whatever one tick hit
                print(f"[dashboard] watcher error: {type(exc).__name__}: {exc}", file=sys.stderr)
            self.stop.wait(max(0.1, self.poll - (time.monotonic() - started)))


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Count, pass rate, total / median / mean cost and duration over run summaries."""
    finished = [r for r in runs if r.get("status") in {"passed", "failed"}]
    passed = sum(1 for r in finished if r["status"] == "passed")
    costs = [float(r["cost"]) for r in runs if isinstance(r.get("cost"), (int, float))]
    durations = [float(r["duration"]) for r in finished if isinstance(r.get("duration"), (int, float))]
    return {"count": len(runs), "finished": len(finished), "passed": passed,
            "failed": sum(1 for r in finished if r["status"] == "failed"),
            "running": sum(1 for r in runs if r.get("status") == "running"),
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


INLINE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                ".webp": "image/webp", ".webm": "video/webm", ".mp4": "video/mp4", ".ogg": "video/ogg",
                ".wav": "audio/wav", ".mp3": "audio/mpeg", ".json": "application/json; charset=utf-8",
                ".jsonl": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
                ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                ".xml": "text/plain; charset=utf-8", ".rkt": "text/plain; charset=utf-8"}


# ============================================================================ HTTP

class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], model: Model, token: str | None, static_dir: Path) -> None:
        self.model = model
        self.token = token
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

    def do_POST(self) -> None:  # noqa: N802 — nothing may change anything
        self._error(405, "read-only")

    do_PUT = do_DELETE = do_PATCH = do_POST  # noqa: N815

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
        short = Shortener()
        if isinstance(state, dict) and state.get("worktree"):
            short.add(str(state["worktree"]), "")
        short.add(str(layer.path), "layer:")
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
        what=check (checks/<file>, ANSI → segments) or what=runlog. Resumes from Last-Event-ID
        (`offset:line`), so a reconnect only gets what it missed."""
        resolved = self._resolve_run(query)
        if not resolved:
            return
        layer, run = resolved
        what = self._q(query, "what")
        state = read_json(run.dir / "state.json") or {}
        short = Shortener()
        if isinstance(state, dict) and state.get("worktree"):
            short.add(str(state["worktree"]), "")
        short.add(str(layer.path), "layer:")
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
            return self._error(400, "what=transcript|check|runlog")
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
                    text = "\n".join(short(line[2]) for line in lines) + "\n"
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
    parser = argparse.ArgumentParser(prog="dashboard.py", description="Live read-only work dashboard for al layers.")
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
    args = parser.parse_args(argv)
    if not args.layer and not args.scan:
        parser.error("give at least one --layer DIR or --scan DIR")
    if not is_loopback(args.bind) and not args.token:
        parser.error(f"--bind {args.bind} is not a loopback address: refusing without --token")
    return args


def make_server(args: argparse.Namespace, static_dir: Path = STATIC_DIR) -> DashboardServer:
    probe = ContainerProbe(args.podman_socket, enabled=not args.no_podman)
    model = Model(args.layer, args.scan, poll=args.poll, scan_depth=args.scan_depth, probe=probe,
                  in_container=args.in_container)
    model.discover()
    probe.poll(want_stats=True)   # so the first snapshot already knows the containers
    model.tick()
    return DashboardServer((args.bind, args.port), model, args.token, static_dir)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    print(f"al dashboard {where}\n  watching: {layers}\n  containers: {server.model.probe.info()['note']}", flush=True)
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
