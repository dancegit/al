"""The coverage map: which test executed which file, recorded by a passing full run.

A cmd may declare `#:record-coverage RECIPE` with `#:coverage-run` (its full form with the runner's
per-test coverage on) and `#:coverage-file` (where that writes its output). After a passing full
run on a committed tree the runtime reads the output with `read_recipe` and `save`s one small map:

    agents/testing/coverage/CMD.json
    {"version": 1, "cmd": CMD, "commit": SHA, "recorded_at": ISO, "recipe": RECIPE,
     "tests": {TEST_ID: [repository-relative files]}}

Recipes:
- `coverage-py`: coverage.py JSON with contexts (`coverage json --show-contexts`, contexts from
  pytest-cov `--cov-context=test`): per file, the contexts of its lines; a context
  `tests/test_core.py::test_x|run` is the test file `tests/test_core.py`;
- `profile-dir`: a directory with one profile per test, named after the test (`__` reads as `/`,
  e.g. `tests__test_core.py.info`): lcov `.info` (`SF:` blocks with a hit line) or a Go cover
  profile (`mode: …`, `pkg/file.go:` lines with a count above 0, mapped through `go.mod`);
- `al-json`: al's own format `{"tests": {TEST_ID: [files]}}`, taken as it is.

The map belongs to its commit: a file changed between that commit and the run's base is unknown
to it (`stale_paths`), and an unknown commit (garbage-collected, another history) makes every
path unknown. A lost or unreadable map is `None`; selection then notes it and maps nothing.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import delivery

VERSION = 1
RECIPES = ("coverage-py", "profile-dir", "al-json")
COVERAGE_DIR = Path("agents") / "testing" / "coverage"


def _layer_dir(layer: Any) -> Path:
    return Path(getattr(layer, "layer_dir", layer))


def _name(cmd: Any) -> str:
    return str(cmd.get("name", "")) if isinstance(cmd, dict) else str(cmd)


def path(layer: Any, cmd: Any) -> Path:
    return _layer_dir(layer) / COVERAGE_DIR / f"{_name(cmd)}.json"


# ---------------------------------------------------------------- recipes

def _relative(file: str, root: Path, prefix: str) -> str:
    """A recorded file name as a repository-relative path: absolute names under `root` are made
    relative to it; relative names are joined to `prefix` (the directory the runner ran in)."""
    text = str(file).strip()
    if os.path.isabs(text):
        try:
            return Path(text).resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            return Path(text).as_posix()
    joined = Path(prefix, text).as_posix() if prefix else Path(text).as_posix()
    return os.path.normpath(joined).replace(os.sep, "/")


def _test_file(context: str) -> str:
    """`tests/test_core.py::test_x|run` → `tests/test_core.py`; "" for the empty (no test) context."""
    name = str(context).split("|", 1)[0].strip()
    return name.split("::", 1)[0] if name else ""


def _add(tests: dict[str, list[str]], test: str, file: str) -> None:
    files = tests.setdefault(test, [])
    if file not in files:
        files.append(file)


def _coverage_py(path: Path, root: Path, prefix: str) -> dict[str, list[str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tests: dict[str, list[str]] = {}
    for file, record in sorted((data.get("files") or {}).items()):
        relative = _relative(file, root, prefix)
        for contexts in (record.get("contexts") or {}).values():
            for context in contexts or []:
                test = _test_file(context)
                if test:
                    _add(tests, _relative(test, root, prefix), relative)
    return {test: sorted(files) for test, files in tests.items()}


def _go_module(root: Path) -> str:
    try:
        match = re.search(r"^module\s+(\S+)", (Path(root) / "go.mod").read_text(encoding="utf-8"), re.M)
    except OSError:
        return ""
    return match.group(1).strip('"') if match else ""


def _profile_files(path: Path, root: Path, prefix: str, module: str) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    files: list[str] = []
    if text.startswith("mode:"):
        for line in text.splitlines()[1:]:
            match = re.match(r"^(.+?\.go):\S+ \d+ (\d+)$", line.strip())
            if not match or int(match.group(2)) <= 0:
                continue
            name = match.group(1)
            if module and (name == module or name.startswith(module + "/")):
                name = name[len(module):].lstrip("/")
            elif module:
                continue  # a package of another module (a dependency): not in this repository
            files.append(_relative(name, root, prefix))
        return sorted(set(files))
    current, hit = "", False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SF:"):
            current, hit = line[3:], False
        elif line.startswith("DA:") and current:
            parts = line[3:].split(",")
            if len(parts) >= 2 and parts[1].strip().isdigit() and int(parts[1]) > 0:
                hit = True
        elif line.startswith("LH:") and current and line[3:].strip().isdigit() and int(line[3:]) > 0:
            hit = True
        elif line == "end_of_record":
            if current and hit:
                files.append(_relative(current, root, prefix))
            current, hit = "", False
    return sorted(set(files))


def _profile_dir(path: Path, root: Path, prefix: str) -> dict[str, list[str]]:
    module = _go_module(root)
    tests: dict[str, list[str]] = {}
    for profile in sorted(Path(path).iterdir()):
        if not profile.is_file():
            continue
        head = profile.read_text(encoding="utf-8", errors="replace")[:4096]
        if not (head.startswith("mode:") or re.search(r"^SF:", head, re.M)):
            continue
        stem = profile.name[:-len(profile.suffix)] if profile.suffix else profile.name
        test = stem.replace("__", "/")
        files = _profile_files(profile, root, prefix, module)
        if files:
            tests[test] = files
    return tests


def _al_json(path: Path) -> dict[str, list[str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, dict):
        raise ValueError('expected {"tests": {TEST_ID: [files]}}')
    return {str(test): [str(file) for file in files or []] for test, files in tests.items()}


def read_recipe(recipe: str, path: Path | str, root: Path | str, *, prefix: str = "",
                notes: list[str] | None = None) -> dict[str, list[str]]:
    """{test id: [repository-relative files]} from what a `#:coverage-run` wrote at `path`
    (relative names are taken relative to `prefix`, the directory the runner ran in, e.g. the
    cmd's #:test-root). Unreadable or unknown → {} and a note in `notes`."""
    try:
        if recipe == "coverage-py":
            return _coverage_py(Path(path), Path(root), prefix)
        if recipe == "profile-dir":
            return _profile_dir(Path(path), Path(root), prefix)
        if recipe == "al-json":
            return _al_json(Path(path))
        raise ValueError(f"unknown recipe {recipe!r}; recipes are {', '.join(RECIPES)}")
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        if notes is not None:
            notes.append(f"coverage: {path} could not be read with recipe {recipe}: {exc}"[:300])
        return {}


# ---------------------------------------------------------------- the map

def save(layer: Any, cmd: Any, commit: str, recipe: str, tests: dict[str, list[str]]) -> Path:
    """Write the map of `cmd` for `commit` atomically; returns its path."""
    target = path(layer, cmd)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": VERSION, "cmd": _name(cmd), "commit": str(commit), "recipe": str(recipe),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "tests": {str(test): sorted({str(file) for file in files}) for test, files in sorted(tests.items())}}
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False,
                                         prefix=f".{_name(cmd)}.", suffix=".tmp")
    try:
        with handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return target


def load(layer: Any, cmd: Any) -> dict[str, Any] | None:
    """The recorded map of `cmd`; None when there is none or it cannot be read."""
    try:
        data = json.loads(path(layer, cmd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tests"), dict):
        return None
    return data


def stale_paths(layer: Any, state: Any, env_stack: Any, data: dict[str, Any], changed: list[str]) -> set[str]:
    """The changed paths the map cannot speak for: those that changed between the map's commit
    and the run's base (`git diff --name-only COMMIT BASE` in the run's worktree; HEAD when the
    base is unknown). Measured against the base, not HEAD, so the run's own committed change does
    not make its own paths stale. An unknown commit or a failing git makes every path stale."""
    changed_set = {str(item) for item in changed}
    commit = str((data or {}).get("commit") or "")
    if not commit:
        return changed_set
    try:
        upto = ""
        branch = str(getattr(layer, "default_branch", "") or "")
        if branch:
            base = delivery.tree_git(state, env_stack, ["merge-base", "HEAD", f"refs/heads/{branch}"], step="checks")
            upto = base.stdout.strip() if base.returncode == 0 else ""
        result = delivery.tree_git(state, env_stack, ["diff", "--name-only", "--no-renames", "-z", commit,
                                                      upto or "HEAD", "--"], step="checks")
    except Exception:  # noqa: BLE001 — a map that cannot be compared speaks for nothing
        return changed_set
    if result.returncode != 0:
        return changed_set
    since = {item for item in result.stdout.split("\0") if item}
    return changed_set & since

