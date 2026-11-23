"""The local ship-without-review limit: `#:max-diff-lines` of an auto review option, per machine.

An auto review option ships a run with no person when every rule of the gate holds; one of them is
that the diff has at most `#:max-diff-lines` changed lines (`maxDiffLines` in the IR). The person
who operates a layer can set another limit for this machine without editing the layer definition:

    agents/settings/review.json   (under the layer directory, never committed)
    {"version": 1,
     "maxDiffLines": {OPTION-ID: N},
     "set": {OPTION-ID: {"who": NAME, "via": "cli"|"dashboard", "at": "2026-11-23T10:00:00Z"}}}

`set` is optional. N is a whole number from 1 to 50000.

Precedence: the override of the option's id if one is set, else the declared value. Only an auto
option has a limit; an override naming another kind of option is not applied. The setting changes
this one number: it never lowers any other rule of the gate (the checks, the review, the allowed
classes, the protected paths). A file that cannot be read never raises a limit and never stops the
gate: the declared value applies and the gate's line says the setting was ignored.

Written by `al review LAYER --set ID=N | --reset [ID]` and by the dashboard's "Ship without review"
card (kit/dashboard/dashboard.py mirrors `read_setting` and `write_setting` byte for byte).

Standard library only; imports `errors`, never `review`, `ship` or `steps` at module level.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import LayerError

SETTING_FILE = "agents/settings/review.json"
MIN_LINES = 1
MAX_LINES = 50000
WHO_MAX = 80
GATE_CAPABILITY = "READS_LOCAL_LIMIT"     # review.py sets it true once the gate applies the local limit
SOURCE_OVERRIDE = "local override"
SOURCE_DECLARED = "declared"
FIX = "fix or remove it (al review LAYER --reset)"


def check_limit(value: Any) -> int:
    """`value` as a limit; ValueError naming the range when it is not a whole number within it."""
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LINES <= value <= MAX_LINES:
        raise ValueError(f"max-diff-lines is a whole number from {MIN_LINES} to {MAX_LINES}, got {value!r}")
    return value


def check_who(who: Any) -> str:
    """The name of the person who sets a limit, stripped; ValueError when it is not a short name."""
    name = who.strip() if isinstance(who, str) else ""
    if not 1 <= len(name) <= WHO_MAX or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"who: a name of 1 to {WHO_MAX} characters")
    return name


def render_setting(limits: dict[str, int], set_by: dict[str, dict[str, str]]) -> str:
    """The file's text. This is the one format; the dashboard writes it byte for byte alike."""
    return json.dumps({"version": 1, "maxDiffLines": limits, "set": set_by}, indent=2, sort_keys=True) + "\n"


def read_setting(layer_dir: Path | str) -> dict[str, Any]:
    """{"maxDiffLines": {ID: N}, "set": {ID: {"who", "via", "at"}}}; both empty without the file.
    A file that is not the setting raises LayerError with how to fix it."""
    path = Path(layer_dir) / SETTING_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"maxDiffLines": {}, "set": {}}
    except UnicodeDecodeError as exc:
        raise LayerError(f"{path}: not JSON; {FIX}") from exc
    except OSError as exc:
        raise LayerError(f"{path}: cannot read it ({exc.strerror}); {FIX}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LayerError(f"{path}: not JSON; {FIX}") from exc
    expected = f'{path}: expected {{"version": 1, "maxDiffLines": {{ID: N}}}}'
    if not isinstance(data, dict):
        raise LayerError(f"{expected}; {FIX}")
    limits = data.get("maxDiffLines", {})
    if not isinstance(limits, dict):
        raise LayerError(f"{expected}; {FIX}")
    for option_id, value in limits.items():
        try:
            check_limit(value)
        except ValueError as exc:
            raise LayerError(f"{path}: {option_id}: {exc}; {FIX}") from exc
    set_by = data.get("set", {})
    if not isinstance(set_by, dict) or not all(
            isinstance(stamp, dict) and all(isinstance(value, str) for value in stamp.values())
            for stamp in set_by.values()):
        raise LayerError(f'{path}: "set" is {{ID: {{"who", "via", "at"}}}} with text values; {FIX}')
    return {"maxDiffLines": dict(limits), "set": {key: dict(stamp) for key, stamp in set_by.items()}}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_setting(layer_dir: Path | str, option_id: str | None, limit: int | None, *, who: str = "",
                  via: str = "cli", at: str | None = None) -> Path:
    """Set the limit of one option (with who, how and when), atomically. `limit=None` drops that
    option's override, `option_id=None` every override; with none left the file is removed."""
    path = Path(layer_dir) / SETTING_FILE
    if option_id is None:
        path.unlink(missing_ok=True)
        return path
    if limit is not None:   # checked before anything is read or written
        limit, who = check_limit(limit), check_who(who)
    try:
        current = read_setting(layer_dir)
    except LayerError:
        current = {"maxDiffLines": {}, "set": {}}   # a broken file is replaced
    limits, set_by = current["maxDiffLines"], current["set"]
    if limit is None:
        limits.pop(option_id, None)
        set_by.pop(option_id, None)
    else:
        limits[option_id] = limit
        set_by[option_id] = {"who": who, "via": str(via), "at": at or _now()}
    set_by = {key: stamp for key, stamp in set_by.items() if key in limits}
    if not limits:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix=".review.", suffix=".tmp")
    try:
        with handle:
            handle.write(render_setting(limits, set_by))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def effective(option: dict[str, Any], setting: dict[str, Any]) -> dict[str, Any]:
    """The limit that applies to `option` and where it comes from: the override when the option is
    an auto one and the setting names its id, else the declared value."""
    option_id = str(option.get("id", ""))
    declared = int(option.get("maxDiffLines", 0))
    limits = setting.get("maxDiffLines") or {}
    if option.get("kind") == "auto" and option_id in limits:
        return {"option": option_id, "declared": declared, "limit": int(limits[option_id]), "source": SOURCE_OVERRIDE}
    return {"option": option_id, "declared": declared, "limit": declared, "source": SOURCE_DECLARED}


def apply(layer_dir: Path | str, option: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the option the gate decides with, what limit that is). An option that is not auto comes
    back as it is with {}. A broken setting is ignored and noted; the option passed in is not mutated."""
    if option.get("kind") != "auto":
        return option, {}
    try:
        setting = read_setting(layer_dir)
    except LayerError as exc:
        info = {**effective(option, {"maxDiffLines": {}, "set": {}}), "note": f"the local setting is ignored: {exc}"}
        return option, info
    info = effective(option, setting)
    return {**option, "maxDiffLines": info["limit"]}, info


def gate_line(info: dict[str, Any]) -> str:
    """The gate's progress line: the limit it decides with and where that comes from."""
    if info.get("source") == SOURCE_OVERRIDE:
        line = f"gate: max-diff-lines {info['limit']} ({SOURCE_OVERRIDE}; declared {info['declared']})"
    else:
        line = f"gate: max-diff-lines {info['limit']} ({SOURCE_DECLARED})"
    return line + (f"; {info['note']}" if info.get("note") else "")


def gate_reads_limit() -> bool:
    """True once the gate (review.py) applies the local limit; until then no override is written."""
    from . import review  # read only: whether it declares the capability

    return bool(getattr(review, GATE_CAPABILITY, False))
