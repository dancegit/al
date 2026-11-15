"""Private values of whoever runs the layer, kept in `KEY=VALUE` files outside git.

A layer file names environment variables (`#:to-env "AL_NOTIFY_TO"`); it never holds an address,
a server or a password. The value of such a variable comes from, highest precedence first:

1. the process environment (a non-empty value);
2. `<layer>/local.env` (git-ignored by the generator; `al check` fails when it is tracked);
3. `$XDG_CONFIG_HOME/al/<layer name>.env` (default `~/.config/al/<layer name>.env`).

Format: one `KEY=VALUE` per line. Empty lines and lines starting with `#` are skipped, a leading
`export ` is dropped, the key is a variable name, the value is stripped and loses one pair of
matching surrounding quotes. Nothing else is interpreted: no `$` expansion, no escapes, and a `#`
inside a value belongs to it. A later line wins.

Values read from the files stay in the dictionary `load()` returns. They are never copied into
`os.environ` (so agents, checks and containers do not inherit them) and never written to state,
events or logs: a warning about a bad line names the file and the line number only.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

FILE_NAME = "local.env"
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LAYER_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")


def parse(text: str) -> tuple[dict[str, str], list[int]]:
    """(values, numbers of the lines that are not KEY=VALUE); line numbers start at 1."""
    values: dict[str, str] = {}
    bad: list[int] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            bad.append(number)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values, bad


def config_file(name: str) -> Path | None:
    """`$XDG_CONFIG_HOME/al/<name>.env`; None when `name` is not a layer name."""
    if not _LAYER_NAME.fullmatch(name):
        return None
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "al" / f"{name}.env"


def files(layer: Any) -> list[Path]:
    """The files of a layer, lowest precedence first."""
    config = config_file(str(layer.name))
    return ([config] if config is not None else []) + [Path(layer.layer_dir) / FILE_NAME]


def load(layer: Any) -> dict[str, str]:
    """The merged values of the layer's files. A missing or unreadable file is skipped. Never raises."""
    values: dict[str, str] = {}
    try:
        paths = files(layer)
    except Exception:  # noqa: BLE001 — e.g. no home directory: no file values, never a failed run
        return values
    for path in paths:
        try:
            found, bad = parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        for number in bad:
            print(f"[local.env] warning: {path.name} line {number} is not KEY=VALUE; ignored", file=sys.stderr)
        values.update(found)
    return values


def lookup(name: str, values: dict[str, str]) -> str:
    """The value of variable `name`: the environment's when it is non-empty, else the files'."""
    return os.environ.get(name) or values.get(name, "")
