"""The record of a run on another host, streamed to the home machine.

The record is pulled, never pushed: the home proxy (remoterun) sends its cursor, the host answers
with a delta (`delta`), the home machine applies it (`apply`) and stores the new cursor. A
connection that drops is caught up from the cursor, so nothing is duplicated and nothing is lost.

What travels (paths as the frame names them, relative to the layer directory):

    agents/<id>/…       the run folder, without objects/ (the git quarantine), credentials/ (the
                        login copy), incoming.git/ and .remote/ (the home machine's own notes)
    artefacts/<id>/…    the run's artefacts (the storage option's local directory on each side)
    specs/<name>        plan files whose name contains the run id

Append-only files (events.jsonl, run.log, launch.log, raw.jsonl, stderr.log and *.log under
checks/, scripts/ and fanout/) travel as byte ranges; a range is written only when the home file
has exactly the size the range starts at. Every other file travels whole and replaces the home
file atomically. Symbolic links and special files never travel.

Cursor: {path: {size, mtime_ns, sha, sha_len, shift}}, kept at home in
agents/<id>/.remote/cursor.json. `sha` is the hash of the first `sha_len` bytes of an append-only
file, so a file that was rewritten on the host is sent whole again; `shift` is how many bytes the
home file holds in front of the host's first byte (a run restarted on another host keeps the
record of its first attempt).

Frame: one JSON header line
    {protocol, run, layer_dir, app_root, status, alive, more, files: [{path, mode: append | whole, offset, bytes, mtime_ns, …}]}
followed by the bytes of the files, in order. A reply is capped at MAX_REPLY; the rest comes with
the next pull.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import state as state_module, storage
from .errors import LayerError

PROTOCOL = 1
MAX_REPLY = 8 * 1024 * 1024
SHA_LEN = 4096
EXCLUDED = ("objects", "credentials", "incoming.git", ".remote")
APPEND_NAMES = ("events.jsonl", "run.log", "launch.log", "raw.jsonl", "stderr.log")
APPEND_DIRS = ("checks", "scripts", "fanout")
CURSOR = Path(".remote") / "cursor.json"
HOME_NOTES = ".remote"
# what the host's run ended as; at home such a run is still running: the gate (or the handoff) is to come
REMOTE_ENDED = ("delivered", "failed", "passed", "handed-over", "waiting")
# the keys of the remote block the host writes and that only ever move forward: its latest value wins
FORWARD = ("pushed", "branch", "delivered")


class Refused(Exception):
    """A delta the home machine does not apply: a path outside the run, another run's state."""


# ---------------------------------------------------------------- paths

def append_only(path: str) -> bool:
    parts = path.split("/")
    if parts[-1] in APPEND_NAMES:
        return True
    return parts[-1].endswith(".log") and any(part in APPEND_DIRS for part in parts[2:-1])


def check_path(path: Any, run_id: str) -> str:
    """`path` when it names a file of run `run_id` that may travel; Refused otherwise."""
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise Refused(f"not a path: {path!r}")
    parts = path.split("/")
    if path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise Refused(f"path outside the run: {path!r}")
    if parts[0] == "agents" and len(parts) >= 3 and parts[1] == run_id:
        if parts[2] in EXCLUDED:
            raise Refused(f"{parts[2]}/ never travels: {path!r}")
        return path
    if parts[0] == "artefacts" and len(parts) >= 3 and parts[1] == run_id:
        return path
    if parts[0] == "specs" and len(parts) == 2 and run_id in parts[1]:
        return path
    raise Refused(f"path outside the run: {path!r}")


def run_roots(layer: Any, run_id: str) -> list[tuple[str, Path]]:
    """(name in a frame, directory or file on this machine) of everything run `run_id` records."""
    layer_dir = Path(layer.layer_dir)
    found = [(f"agents/{run_id}", layer_dir / "agents" / run_id),
             (f"artefacts/{run_id}", Path(storage.artefacts_root(layer)) / run_id)]
    specs = layer_dir / "specs"
    if specs.is_dir():
        found += [(f"specs/{entry.name}", entry) for entry in sorted(specs.iterdir()) if run_id in entry.name]
    return found


def local_path(layer: Any, run_id: str, path: str) -> Path:
    """Where the frame path `path` is on this machine."""
    for name, root in run_roots(layer, run_id)[:2]:
        if path.startswith(name + "/"):
            return root / path[len(name) + 1:]
    return Path(layer.layer_dir) / path


def _files(roots: list[tuple[str, Path]]) -> list[tuple[str, Path, os.stat_result]]:
    """Every regular file below the roots; links and special files are left out, never followed."""
    out: list[tuple[str, Path, os.stat_result]] = []

    def visit(name: str, path: Path, top: bool) -> None:
        try:
            info = os.lstat(path)
        except OSError:
            return
        if stat.S_ISREG(info.st_mode):
            out.append((name, path, info))
        elif stat.S_ISDIR(info.st_mode):
            try:
                entries = sorted(os.listdir(path))
            except OSError:
                return
            for entry in entries:
                if top and name.startswith("agents/") and entry in EXCLUDED:
                    continue
                visit(f"{name}/{entry}", path / entry, False)

    for name, root in roots:
        visit(name, root, True)
    return out


def _head_sha(path: Path, length: int) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read(length)).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------- the host's side

def delta(roots: list[tuple[str, Path]], cursor: dict[str, Any], header: dict[str, Any] | None = None,
          limit: int = MAX_REPLY) -> bytes:
    """The frame of everything below `roots` the cursor has not seen. state.json goes first, so a
    capped reply never holds back what the run is doing now."""
    entries: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    total, more = 0, False
    files = sorted(_files(roots), key=lambda item: (not item[0].endswith("/state.json"), item[0]))
    for name, path, info in files:
        seen = cursor.get(name) if isinstance(cursor.get(name), dict) else None
        appendable = append_only(name)
        offset = 0
        if appendable and seen is not None:
            known = int(seen.get("size") or 0)
            same = known <= info.st_size and (not seen.get("sha") or
                                              _head_sha(path, int(seen.get("sha_len") or 0)) == seen["sha"])
            if same and known == info.st_size:
                continue
            offset = known if same else 0
            appendable = same
        elif seen is not None and int(seen.get("size", -1)) == info.st_size and seen.get("mtime_ns") == info.st_mtime_ns:
            continue
        room = limit - total
        if room <= 0 or (not appendable and info.st_size > room and entries):
            more = True
            continue
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(room if appendable else -1)
        except OSError:
            continue
        if appendable and not data and seen is not None:
            continue   # nothing new; an empty file the home machine has not seen still travels
        entry = {"path": name, "mode": "append" if appendable else "whole", "offset": offset, "bytes": len(data),
                 "mtime_ns": info.st_mtime_ns}
        if appendable:
            length = min(SHA_LEN, offset + len(data))
            entry.update(sha=_head_sha(path, length), sha_len=length)
            more = more or offset + len(data) < info.st_size
        entries.append(entry)
        chunks.append(data)
        total += len(data)
    head = {"protocol": PROTOCOL, **(header or {}), "more": more, "files": entries}
    return json.dumps(head, separators=(",", ":")).encode("utf-8") + b"\n" + b"".join(chunks)


def parse(data: bytes) -> tuple[dict[str, Any], list[tuple[dict[str, Any], bytes]]]:
    """(header, [(file entry, its bytes)]) of a frame; Refused when it is not one."""
    line, separator, body = data.partition(b"\n")
    try:
        header = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Refused("the answer is not a record frame") from exc
    if not separator or not isinstance(header, dict) or not isinstance(header.get("files"), list):
        raise Refused("the answer is not a record frame")
    if header.get("protocol") != PROTOCOL:
        raise Refused("the host speaks another record protocol: al deploy it again")
    files, position = [], 0
    for entry in header["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("bytes"), int) or entry["bytes"] < 0:
            raise Refused("bad file entry in the record frame")
        chunk = body[position:position + entry["bytes"]]
        if len(chunk) != entry["bytes"]:
            raise Refused("the record frame is shorter than its header says")
        files.append((entry, chunk))
        position += entry["bytes"]
    return header, files


# ---------------------------------------------------------------- the home machine's side

def cursor_path(layer: Any, run_id: str) -> Path:
    return state_module.run_dir(layer.layer_dir, run_id) / CURSOR


def load_cursor(layer: Any, run_id: str) -> dict[str, Any]:
    try:
        data = json.loads(cursor_path(layer, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, prefix=".sync.") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(handle.name, path)


def save_cursor(layer: Any, run_id: str, cursor: dict[str, Any]) -> None:
    write_json(cursor_path(layer, run_id), cursor)


def restart_cursor(layer: Any, run_id: str) -> dict[str, Any]:
    """The cursor for a run that starts again on another host: nothing of the new host is known,
    and what the home machine already holds of the append-only files stays in front."""
    cursor: dict[str, Any] = {}
    for name, path, info in _files(run_roots(layer, run_id)):
        if append_only(name) and info.st_size:
            cursor[name] = {"size": 0, "shift": info.st_size}
    save_cursor(layer, run_id, cursor)
    return cursor


def shift_to_end(layer: Any, run_id: str, names: tuple[str, ...] = ("events.jsonl", "run.log")) -> None:
    """The home machine is about to write its own lines to the run's append-only files: what the
    host sends later (a host that comes back) goes behind them."""
    cursor = load_cursor(layer, run_id)
    for name in names:
        path = f"agents/{run_id}/{name}"
        try:
            size = os.path.getsize(local_path(layer, run_id, path))
        except OSError:
            continue
        entry = cursor.setdefault(path, {"size": 0})
        entry["shift"] = size - int(entry.get("size") or 0)
    save_cursor(layer, run_id, cursor)


def _prepare(root: Path, target: Path) -> bool:
    """Create the directories between `root` and `target`, one by one, and say whether `target` can
    be written without passing through a link: no directory on the way, and not `target` itself."""
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        return False
    if root.is_symlink():
        return False
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            return False
        if not current.exists():
            current.mkdir()
        elif not current.is_dir():
            return False
    return not target.is_symlink() and not target.is_dir()


def _replace(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False, prefix=".sync.") as handle:
        handle.write(data)
    os.replace(handle.name, target)


def _rewrite(value: Any, pairs: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for old, new in pairs:
            if old and old in value:
                value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_rewrite(item, pairs) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, pairs) for key, item in value.items()}
    return value


def home_state(layer: Any, run_id: str, data: bytes, header: dict[str, Any]) -> dict[str, Any]:
    """The host's state.json as the home machine keeps it: the host's paths become this machine's,
    the home `remote` block stays, except for the keys in FORWARD (the last push, the run branch,
    the delivery), where the host's latest value wins, so a lost run restarts from its latest push;
    and a run whose remote part ended is still running here (the gate is to come). Refused when it
    is the state of another run or workflow."""
    try:
        incoming = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Refused("the host's state.json is not JSON") from exc
    if not isinstance(incoming, dict):
        raise Refused("the host's state.json is not an object")
    target = state_module.run_dir(layer.layer_dir, run_id) / "state.json"
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if incoming.get("adw_id") != run_id:
        raise Refused(f"the host sent the state of run {incoming.get('adw_id')!r}, not {run_id}")
    if current.get("adw") and incoming.get("adw") != current.get("adw"):
        raise Refused(f"the host sent the state of workflow {incoming.get('adw')!r}, not {current.get('adw')!r}")
    pairs: list[tuple[str, str]] = []
    for theirs, ours in ((str(header.get("layer_dir") or ""), str(Path(layer.layer_dir))),
                         (str(header.get("app_root") or ""), str(Path(layer.app_root)))):
        if theirs and theirs != ours:
            pairs += [(theirs, ours)] + ([(quote(theirs), quote(ours))] if quote(theirs) != theirs else [])
    pairs.sort(key=lambda pair: -len(pair[0]))
    incoming = _rewrite(incoming, pairs)
    incoming_remote = incoming.get("remote") if isinstance(incoming.get("remote"), dict) else {}
    current_remote = current.get("remote") if isinstance(current.get("remote"), dict) else {}
    remote = {**incoming_remote, **current_remote}
    for key in FORWARD:   # only ever moves forward on the host: the latest push is where a restart begins
        if key in incoming_remote:
            remote[key] = incoming_remote[key]
    theirs = str(incoming.get("status") or "")
    remote["status"] = theirs
    remote["last_seen"] = state_module.now()
    if theirs in REMOTE_ENDED:
        remote["phase"] = "gate"
    incoming.update(remote=remote, status="running", ended="", pid=os.getpid(), host=socket.gethostname(),
                    heartbeat=state_module.now())
    try:
        state_module.ADWState(**incoming)
    except (TypeError, ValueError, LayerError) as exc:
        raise Refused(f"the host's state.json is not a run state: {exc}") from exc
    return incoming


def apply(layer: Any, run_id: str, data: bytes) -> dict[str, Any]:
    """Apply one frame to this machine's record of run `run_id` and store the new cursor. Every
    path is checked before anything is written. Returns {status, alive, more, changed, bytes}."""
    header, files = parse(data)
    if header.get("run") != run_id:
        raise Refused(f"the host answered for run {header.get('run')!r}, not {run_id}")
    layer_dir = Path(layer.layer_dir)
    for entry, _chunk in files:   # every path is checked before a single byte or directory is written
        path = check_path(entry.get("path"), run_id)
        if entry.get("mode") not in ("append", "whole") or not isinstance(entry.get("offset"), int) or entry["offset"] < 0:
            raise Refused(f"bad file entry for {path!r}")
    plan: list[tuple[dict[str, Any], bytes, Path]] = []
    roots = run_roots(layer, run_id)[:2]
    for entry, chunk in files:
        path = str(entry["path"])
        target = local_path(layer, run_id, path)
        root = next((r for name, r in roots if path.startswith(name + "/")), layer_dir / "specs")
        if not _prepare(root, target):
            raise Refused(f"{path!r} would be written through a link")
        if path == f"agents/{run_id}/state.json":
            chunk = (json.dumps(home_state(layer, run_id, chunk, header), indent=2, sort_keys=True) + "\n").encode("utf-8")
        plan.append((entry, chunk, target))
    cursor = load_cursor(layer, run_id)
    changed, count = [], 0
    for entry, chunk, target in plan:
        path = str(entry["path"])
        seen = cursor.get(path) if isinstance(cursor.get(path), dict) else {}
        shift = int(seen.get("shift") or 0)
        if entry["mode"] == "append":
            try:
                size = os.path.getsize(target)
            except OSError:
                size = 0
            if size == entry["offset"] + shift:
                handle = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(handle, chunk)
                finally:
                    os.close(handle)
                cursor[path] = {"size": entry["offset"] + len(chunk), "mtime_ns": entry.get("mtime_ns"),
                                "sha": entry.get("sha", ""), "sha_len": entry.get("sha_len", 0), "shift": shift}
                changed.append(path)
                count += len(chunk)
            else:  # not where this range starts: ask again from what is here
                cursor[path] = {**seen, "size": max(0, size - shift), "shift": shift, "sha": "", "sha_len": 0}
        else:
            _replace(target, chunk)
            cursor[path] = {"size": entry["bytes"], "mtime_ns": entry.get("mtime_ns")}
            changed.append(path)
            count += len(chunk)
    save_cursor(layer, run_id, cursor)
    return {"status": str(header.get("status") or ""), "alive": bool(header.get("alive")),
            "more": bool(header.get("more")), "changed": changed, "bytes": count}
