"""The text task list (prompt-input kind `tasklist`).

    # Tasks

    ## feat-dark-mode
    - [ ] Add a dark mode toggle to the settings page {sdlc, heavy}
    - [>] Document the toggle in docs/settings.md
    ## fix-login
    - [x] Fix the off-by-one in pagination <!-- adw:1a2b3c4d commit:9f8e7d6 -->

`## <section>` names a unit of work (worktree/branch slug); tasks before any
heading belong to section `default`. Markers and tag syntax come from the IR
option. Every write locates exactly one line, rewrites only that line, and
replaces the file atomically (temp file + os.replace) under an flock on a
sidecar `<file>.lock`; every other byte is preserved.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

DEFAULT_OPTION = {
    "path": "tasks.md",
    "openMarker": "[ ]",
    "runningMarker": "[~]",
    "doneMarker": "[x]",
    "failedMarker": "[!]",
    "blockedMarker": "[>]",
    "tagSyntax": "braces",
}
DEFAULT_SECTION = "default"
CLASSES = ("chore", "bug", "feature", "patch")
MODEL_SETS = ("base", "heavy")

_HEADING = re.compile(r"^##(?!#)\s+(?P<name>.+?)\s*$")
_ITEM = re.compile(r"^(?P<lead>\s*[-*+]\s+)(?P<rest>.*)$")
_COMMENT = re.compile(r"\s*<!--\s*adw:(?P<id>[\w.\-]+)(?P<extra>[^>]*?)\s*-->\s*$")
_BRACES = re.compile(r"\s*\{(?P<tags>[^{}]*)\}\s*$")
_HASHES = re.compile(r"(?:\s+#[\w][\w:.\-/]*)+\s*$")


class TaskError(RuntimeError):
    """The task line could not be found or is not in the expected state."""


@dataclass
class Task:
    section: str
    line_no: int  # 1-based
    status: str  # open | running | done | failed | blocked
    text: str  # prompt text: marker, tags and adw comment stripped
    tags: list[str] = field(default_factory=list)  # normalised: "sdlc", "heavy", "class:bug"
    raw: str = ""  # the line as in the file, without its line ending
    adw_id: str | None = None

    @property
    def model_set(self) -> str | None:
        return self.tier(MODEL_SETS)

    def tier(self, tiers: Iterable[str]) -> str | None:
        """The first tag that names one of `tiers` (the model tiers the layer's agents declare)."""
        known = set(tiers)
        return next((t for t in self.tags if t in known), None)

    @property
    def klass(self) -> str | None:
        for t in self.tags:
            if t.startswith("class:") and t[6:] in CLASSES:
                return t[6:]
            if t in CLASSES:
                return t
        return None

    def workflow(self, adw_names: Iterable[str]) -> str | None:
        """First tag that names an ADW (workflow:<name> also accepted)."""
        names = set(adw_names)
        for t in self.tags:
            name = t[9:] if t.startswith("workflow:") else t
            if name in names:
                return name
        return None


def _opt(option: Mapping[str, Any] | None) -> dict:
    return {**DEFAULT_OPTION, **(option or {})}


def _markers(option: Mapping[str, Any]) -> list[tuple[str, str]]:
    o = _opt(option)
    pairs = [(o["openMarker"], "open"), (o["runningMarker"], "running"), (o["doneMarker"], "done"),
             (o["failedMarker"], "failed"), (o["blockedMarker"], "blocked")]
    return sorted(pairs, key=lambda p: -len(p[0]))  # longest first so prefixes never shadow


def _norm_tag(tag: str) -> str:
    tag = tag.strip()
    if ":" in tag:
        k, v = tag.split(":", 1)
        return f"{k.strip().lower()}:{v.strip()}"
    return tag.lower() if tag.lower() in (*CLASSES, *MODEL_SETS) else tag


def split_tags(text: str, syntax: str = "braces") -> tuple[str, list[str]]:
    """Strip trailing tags from `text`; return (text, normalised tags)."""
    if syntax == "hash":
        m = _HASHES.search(text)
        if not m:
            return text.strip(), []
        tags = [_norm_tag(t[1:]) for t in m.group(0).split()]
        return text[: m.start()].strip(), tags
    m = _BRACES.search(text)
    if not m:
        return text.strip(), []
    tags = [_norm_tag(t) for t in m.group("tags").split(",") if t.strip()]
    return text[: m.start()].strip(), tags


def format_tags(tags: Iterable[str], syntax: str = "braces") -> str:
    tags = [t for t in tags if t]
    if not tags:
        return ""
    if syntax == "hash":
        return " " + " ".join(f"#{t.replace(' ', '')}" for t in tags)
    return " {" + ", ".join(t.replace(":", ": ", 1) if ":" in t else t for t in tags) + "}"


def _parse_line(line: str, option: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """Return (lead, marker, status, rest) for a task line, else None."""
    m = _ITEM.match(line)
    if not m:
        return None
    rest = m.group("rest")
    for marker, status in _markers(option):
        if rest.startswith(marker) and (len(rest) == len(marker) or rest[len(marker)].isspace()):
            return m.group("lead"), marker, status, rest[len(marker):].strip()
    return None


def parse(text: str, option: Mapping[str, Any] | None = None) -> list[Task]:
    o = _opt(option)
    tasks: list[Task] = []
    section = DEFAULT_SECTION
    for i, line in enumerate(text.splitlines(), start=1):
        h = _HEADING.match(line)
        if h:
            section = h.group("name")
            continue
        p = _parse_line(line, o)
        if not p:
            continue
        _lead, _marker, status, rest = p
        adw_id = None
        c = _COMMENT.search(rest)
        if c:
            adw_id = c.group("id")
            rest = rest[: c.start()]
        body, tags = split_tags(rest, o["tagSyntax"])
        tasks.append(Task(section, i, status, body, tags, line, adw_id))
    return tasks


def eligible(tasks: Iterable[Task], serial_sections: bool = False) -> list[Task]:
    """Tasks ready to run: open tasks, and blocked tasks whose predecessors in the section are all done.

    With `serial_sections`, a section that already has a running task yields nothing and
    at most one task per section is returned (one worktree/branch per section).
    """
    by_section: dict[str, list[Task]] = {}
    for t in tasks:
        by_section.setdefault(t.section, []).append(t)
    out: list[Task] = []
    for items in by_section.values():
        if serial_sections and any(t.status == "running" for t in items):
            continue
        for idx, t in enumerate(items):
            ok = t.status == "open" or (
                t.status == "blocked" and all(p.status == "done" for p in items[:idx]))
            if ok:
                out.append(t)
                if serial_sections:
                    break
    return sorted(out, key=lambda t: t.line_no)


# --------------------------------------------------------------------------- file writes


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_lines(path: Path) -> list[str]:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read().splitlines(keepends=True)


def _write_atomic(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _eol(line: str) -> tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("-->", "->")).strip()[:200]


def _rewrite(line: str, new_marker: str, comment: str, option: Mapping[str, Any]) -> str:
    body, eol = _eol(line)
    p = _parse_line(body, option)
    if not p:
        raise TaskError(f"not a task line: {body!r}")
    lead, _marker, _status, rest = p
    c = _COMMENT.search(rest)
    if c:
        rest = rest[: c.start()]
    return f"{lead}{new_marker} {rest.rstrip()} {comment}{eol}"


def _status_of(line: str, option) -> str | None:
    p = _parse_line(_eol(line)[0], option)
    return p[2] if p else None


def claim(path: str | os.PathLike, task: Task, adw_id: str, option: Mapping[str, Any] | None = None) -> Task:
    """Mark `task` running with `<!-- adw:<id> -->`. Raises TaskError if it changed or was claimed."""
    o, path = _opt(option), Path(path)
    with locked(path):
        lines = _read_lines(path)
        idx = task.line_no - 1
        if not (0 <= idx < len(lines) and _eol(lines[idx])[0] == task.raw):
            matches = [i for i, ln in enumerate(lines) if _eol(ln)[0] == task.raw]
            if len(matches) != 1:
                raise TaskError(f"task line {task.line_no} changed or was already claimed: {task.text!r}")
            idx = matches[0]
        if _status_of(lines[idx], o) not in ("open", "blocked"):
            raise TaskError(f"task line {idx + 1} is not open: {task.text!r}")
        lines[idx] = _rewrite(lines[idx], o["runningMarker"], f"<!-- adw:{adw_id} -->", o)
        _write_atomic(path, "".join(lines))
    return next(t for t in parse("".join(lines), o) if t.line_no == idx + 1)


def _finish(path, adw_id, marker_key, extra, option) -> int:
    o, path = _opt(option), Path(path)
    needle = re.compile(r"<!--\s*adw:" + re.escape(adw_id) + r"(?![\w.\-])")
    with locked(path):
        lines = _read_lines(path)
        hits = [i for i, ln in enumerate(lines) if needle.search(ln) and _status_of(ln, o)]
        if not hits:
            raise TaskError(f"no task carries adw id {adw_id}")
        idx = hits[0]
        lines[idx] = _rewrite(lines[idx], o[marker_key], f"<!-- adw:{adw_id}{extra} -->", o)
        _write_atomic(path, "".join(lines))
    return idx + 1


def complete(path, adw_id: str, commit: str | None, option: Mapping[str, Any] | None = None) -> int:
    """Mark the task claimed by `adw_id` done (with the commit hash). Returns its line number."""
    extra = f" commit:{_clean(commit)}" if commit else ""
    return _finish(path, adw_id, "doneMarker", extra, option)


def fail(path, adw_id: str, reason: str, option: Mapping[str, Any] | None = None) -> int:
    """Mark the task claimed by `adw_id` failed (with a one-line reason). Returns its line number."""
    extra = f" reason:{_clean(reason)}" if reason else ""
    return _finish(path, adw_id, "failedMarker", extra, option)


def retag(path, old_id: str, new_id: str, option: Mapping[str, Any] | None = None) -> int:
    """Hand a running claim over from `old_id` (a trigger's claim) to `new_id` (the ADW's id)."""
    o, path = _opt(option), Path(path)
    needle = re.compile(r"<!--\s*adw:" + re.escape(old_id) + r"(?![\w.\-])")
    with locked(path):
        lines = _read_lines(path)
        hits = [i for i, ln in enumerate(lines) if needle.search(ln) and _status_of(ln, o) == "running"]
        if not hits:
            raise TaskError(f"no running task carries claim {old_id}")
        idx = hits[0]
        lines[idx] = _rewrite(lines[idx], o["runningMarker"], f"<!-- adw:{new_id} -->", o)
        _write_atomic(path, "".join(lines))
    return idx + 1


def append(path, section: str, text: str, tags: Iterable[str] = (),
           option: Mapping[str, Any] | None = None) -> int:
    """Append an open task to `section` (created at the end if missing). Returns its line number."""
    o, path = _opt(option), Path(path)
    text = _clean(text).replace("{", "(").replace("}", ")")
    new = f"- {o['openMarker']} {text}{format_tags(tags, o['tagSyntax'])}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        lines = _read_lines(path) if path.exists() else ["# Tasks\n", "\n"]
        eol = next((_eol(ln)[1] for ln in lines if _eol(ln)[1]), "\n")
        if lines and not _eol(lines[-1])[1]:
            lines[-1] += eol
        # find the section's span
        start, end, cur = None, None, DEFAULT_SECTION
        first_heading = None
        for i, ln in enumerate(lines):
            h = _HEADING.match(_eol(ln)[0])
            if h:
                if first_heading is None:
                    first_heading = i
                if start is not None and end is None:
                    end = i
                cur = h.group("name")
                if cur == section and start is None:
                    start = i + 1
        if section == DEFAULT_SECTION and start is None:
            start, end = 0, first_heading if first_heading is not None else len(lines)
        if start is None:
            if lines and _eol(lines[-1])[0].strip():
                lines.append(eol)
            lines += [f"## {section}{eol}", f"{new}{eol}"]
            pos = len(lines) - 1
        else:
            end = len(lines) if end is None else end
            task_rows = [i for i in range(start, end) if _parse_line(_eol(lines[i])[0], o)]
            # after the section's last task; else right under its heading (or at the end of the preamble)
            pos = task_rows[-1] + 1 if task_rows else (end if section == DEFAULT_SECTION else start)
            lines.insert(pos, f"{new}{eol}")
        _write_atomic(path, "".join(lines))
    return pos + 1


def path_for(layer_dir: str | os.PathLike, option: Mapping[str, Any] | None = None) -> Path:
    return Path(layer_dir) / _opt(option)["path"]
