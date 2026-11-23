"""Bounded disk use: what settled runs and the test suites leave behind, and where the space went.

- `release_worktree`: a settled run gives back its worktree, `<layer>/<worktree dir>/<run id>`, and
  nothing else (its branch, agents/<id> and artefacts/<id> stay). standin.settle and
  integrate.write_settlement call it; the dashboard has a twin (dashboard.release_worktree) for its
  settle and `al settle`.
- The test labels. Every image and container a test session builds, runs or creates carries
  `al.test=1` and `al.test.session=<id>`: `labelled_session` (a session-scoped autouse fixture of
  each suite) puts a tiny `podman` wrapper first on PATH (`write_label_shim`) that inserts both
  labels into `podman build|run|create` (also `image build`, `container run|create`) and then runs
  the real podman. When the session ends, pass or fail, its labelled objects are removed; when one
  starts, labelled objects of any session older than six hours are.
- `labelled_objects` and `remove_labelled`: the labelled objects, and their removal (containers
  first, then images by name). An image is never removed with --force, and an unlabelled object or
  a kept reference is never touched. `al sandbox LAYER prune --tests` uses it.
- `report` and `lines`: the `disk:` lines of `al doctor` (worktrees, run records, al's images, test
  leftovers, pytest's temp dirs, free space, a warning below 10% free).
- `room_problem`: the free-space guard before a large checkout is made (`al improve`'s copy of the
  application and its dry-run worktrees): "" when there is room, else one sentence saying why not.

Standard library only, no sibling imports: tools/al and the test suites load it by path.
"""
from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping

TEST_LABEL = "al.test"
SESSION_LABEL = "al.test.session"
STALE_SECONDS = 6 * 3600
LOW_FREE = 0.10
SMALL_CHECKOUT = 1 << 30
GIT_TIMEOUT = 120
PODMAN_TIMEOUT = 120
RUN_ID = re.compile(r"[0-9a-fA-F]{8}")
SETTLED = ("landed", "superseded", "abandoned", "shipped")

Runner = Callable[[list[str]], subprocess.CompletedProcess]


# ---------------------------------------------------------------- a settled run's worktree

def release_worktree(layer_dir: Path | str, app_root: Path | str, run_id: str,
                     tree_dir: str = "trees") -> tuple[str, str]:
    """Remove `<layer_dir>/<tree_dir>/<run_id>` (git worktree remove --force; when git refuses, the
    directory itself and then `git worktree prune`) → (the removed path relative to the layer, "")
    , ("", "") when there is no worktree, or ("", why) when it is still there."""
    if not RUN_ID.fullmatch(str(run_id or "")):
        return "", "not a run id"
    trees = Path(layer_dir) / tree_dir
    tree = trees / run_id
    if not tree.exists() and not tree.is_symlink():
        return "", ""
    relative = f"{tree_dir}/{run_id}"
    reason = ""
    try:
        done = subprocess.run(["git", "-C", str(app_root), "worktree", "remove", "--force", str(tree)],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
        if done.returncode:
            said = (done.stderr or done.stdout or "").strip().splitlines()
            reason = said[-1] if said else f"git worktree remove exited {done.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        reason = str(exc)
    if tree.is_dir() and not tree.is_symlink() and tree.parent.resolve() == trees.resolve():
        shutil.rmtree(tree, ignore_errors=True)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["git", "-C", str(app_root), "worktree", "prune"], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, check=False)
    if tree.exists() or tree.is_symlink():
        return "", reason or f"{relative} could not be removed"
    return relative, ""


# ---------------------------------------------------------------- the label shim

def _quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


SHIM = """#!/bin/sh
# al's test label shim (adw_modules/diskuse.py): every image and container this test session builds,
# runs or creates gets the labels {test}=1 and {session_label}={session}; then the real podman runs.
real={real}
session_label={session_value}
state=0
group=''
count=$#
for arg in "$@"; do
  set -- "$@" "$arg"
  case $state in
    0)
      case $arg in
        -*) ;;
        build|run|create) set -- "$@" --label {test}=1 --label "$session_label"; state=2 ;;
        image|container) group=$arg; state=1 ;;
        *) state=2 ;;
      esac ;;
    1)
      case $arg in
        -*) ;;
        *)
          case "$group $arg" in
            'image build'|'container run'|'container create') set -- "$@" --label {test}=1 --label "$session_label" ;;
          esac
          state=2 ;;
      esac ;;
  esac
done
shift "$count"
exec "$real" "$@"
"""


def write_label_shim(bin_dir: Path | str, real_podman: str, session: str) -> Path:
    """`bin_dir/podman`: a POSIX sh script that inserts `--label al.test=1 --label
    al.test.session=<session>` right after `build`, `run` or `create` (also `image build`,
    `container run|create`) and execs the absolute `real_podman`; every other call passes unchanged."""
    path = Path(bin_dir) / "podman"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SHIM.format(test=TEST_LABEL, session_label=SESSION_LABEL, session=session,
                                real=_quote(str(Path(real_podman).absolute())),
                                session_value=_quote(f"{SESSION_LABEL}={session}")), encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------- labelled objects

def _call(run: Runner, argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return run(argv)
    except (OSError, subprocess.SubprocessError):
        return None


def _rows(run: Runner, argv: list[str]) -> list[dict[str, Any]]:
    done = _call(run, argv)
    if done is None or done.returncode != 0:
        return []
    try:
        data = json.loads(done.stdout or "[]")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [row for row in data or [] if isinstance(row, dict)] if isinstance(data, list) else []


def _epoch(value: Any) -> float | None:
    """A creation time as podman gives it (epoch seconds, or an ISO string) → epoch seconds; None
    when unknown (an unknown time is never old)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        with contextlib.suppress(ValueError):
            return float(text)
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    return None


def _labels(row: dict[str, Any]) -> dict[str, str]:
    labels = row.get("Labels")
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


def _names(row: dict[str, Any]) -> list[str]:
    names = row.get("Names")
    if isinstance(names, str):
        names = [names]
    return [str(n) for n in names or [] if n and "<none>" not in str(n)] if isinstance(names, list) else []


def _size(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def labelled_objects(run: Runner, *, session: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """The containers and images labelled al.test=1 (and al.test.session=<session> when given) →
    {"containers": [{id, names, running, created, session}], "images": [{id, names, created, size,
    session}]}. A podman that fails or answers no JSON gives empty lists."""
    filters = ["--filter", f"label={TEST_LABEL}=1"]
    if session:
        filters += ["--filter", f"label={SESSION_LABEL}={session}"]

    def ours(row: dict[str, Any]) -> bool:   # the filter again: an unlabelled object is never listed
        labels = _labels(row)
        return labels.get(TEST_LABEL) == "1" and (not session or labels.get(SESSION_LABEL) == session)

    containers = [{"id": str(row.get("Id") or row.get("ID") or ""), "names": _names(row),
                   "running": str(row.get("State") or "").lower() == "running", "created": _epoch(row.get("Created")),
                   "session": _labels(row).get(SESSION_LABEL, "")}
                  for row in _rows(run, ["ps", "-a", *filters, "--format", "json"]) if ours(row)]
    images = [{"id": str(row.get("Id") or row.get("ID") or ""), "names": _names(row), "created": _epoch(row.get("Created")),
               "size": _size(row.get("Size")), "session": _labels(row).get(SESSION_LABEL, "")}
              for row in _rows(run, ["images", *filters, "--format", "json"]) if ours(row)]
    return {"containers": [c for c in containers if c["id"]], "images": [i for i in images if i["id"]]}


def _short(ref: str) -> str:
    return ref[len("localhost/"):] if ref.startswith("localhost/") else ref


def remove_labelled(run: Runner, *, session: str | None = None, older_than: float | None = None,
                    now: Callable[[], float] = time.time, keep: Iterable[str] = ()) -> list[str]:
    """Remove the labelled objects of `session` (of every session when None), with `older_than` only
    those created more than that many seconds before now(): containers first (`rm -f -t 0 ID`), then
    images (`rmi NAME` per name, `rmi ID` for an image without one; never --force). An image any of
    whose names is in `keep` is left. → the names (or ids) removed."""
    kept = {_short(str(ref)) for ref in keep}
    objects = labelled_objects(run, session=session)
    moment = now()

    def due(item: dict[str, Any]) -> bool:
        if older_than is None:
            return True
        return item["created"] is not None and moment - item["created"] > older_than

    removed: list[str] = []
    for container in (c for c in objects["containers"] if due(c)):
        done = _call(run, ["rm", "-f", "-t", "0", container["id"]])
        if done is not None and done.returncode == 0:
            removed.append(container["names"][0] if container["names"] else container["id"])
    for image in (i for i in objects["images"] if due(i)):
        if any(_short(name) in kept for name in image["names"]):
            continue
        for ref in image["names"] or [image["id"]]:
            done = _call(run, ["rmi", ref])
            if done is not None and done.returncode == 0:
                removed.append(ref)
    return removed


@contextlib.contextmanager
def labelled_session(*, which: Callable[[str], str | None] = shutil.which, run: Runner | None = None,
                     environ: MutableMapping[str, str] = os.environ,
                     now: Callable[[], float] = time.time) -> Iterator[str]:
    """A test session whose podman objects are labelled and removed: leftovers of any session older
    than STALE_SECONDS go first, the label shim is put first on PATH, and when the body ends (also by
    an exception) PATH is restored and this session's labelled objects are removed. Yields the
    session id, or "" (and changes nothing) when there is no podman."""
    real = which("podman")
    if not real:
        yield ""
        return
    session = f"{os.getpid()}-{secrets.token_hex(3)}"
    if run is None:
        def run(argv: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run([real, *argv], capture_output=True, text=True, timeout=PODMAN_TIMEOUT, check=False)
    remove_labelled(run, older_than=STALE_SECONDS, now=now)
    shim_dir = tempfile.mkdtemp(prefix="al-podman-label-")
    write_label_shim(shim_dir, real, session)
    had_path = "PATH" in environ
    old_path = environ.get("PATH", "")
    environ["PATH"] = shim_dir + (os.pathsep + old_path if old_path else "")
    try:
        yield session
    finally:
        if had_path:
            environ["PATH"] = old_path
        else:
            environ.pop("PATH", None)
        try:
            remove_labelled(run, session=session, now=now)
        finally:
            shutil.rmtree(shim_dir, ignore_errors=True)


# ---------------------------------------------------------------- al doctor's report

def human_size(n: Any) -> str:
    """Bytes as 512B, 3.4K, 50.0G, 100G (binary units; one decimal below 100)."""
    value = float(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 else 0.0
    if value < 1024:
        return f"{int(value)}B"
    for unit in ("K", "M", "G", "T"):
        value /= 1024
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if value < 100 else f"{value:.0f}{unit}"
    return f"{value:.0f}T"


def room_problem(path: Path | str, needed: int, *, disk_usage: Callable[[str], Any] = shutil.disk_usage) -> str:
    """"" when the filesystem that holds `path` (asked at its nearest existing ancestor) has room for
    `needed` bytes, else one sentence. No room: more is needed than is free; or, for SMALL_CHECKOUT
    bytes or more, it would leave less than LOW_FREE of the filesystem free (a small checkout is only
    refused when it does not fit at all). A filesystem that cannot be asked is never a reason to refuse."""
    where = Path(path).absolute()
    while not where.exists() and where.parent != where:
        where = where.parent
    try:
        usage = disk_usage(str(where))
        total, free = int(usage.total), int(usage.free)
    except (OSError, ValueError, AttributeError):
        return ""
    needed = max(int(needed), 0)
    if needed <= free and (needed < SMALL_CHECKOUT or free - needed >= LOW_FREE * total):
        return ""
    return (f"not enough free disk space on {where}: this needs about {human_size(needed)}, {human_size(free)} of "
            f"{human_size(total)} is free (al keeps {LOW_FREE:.0%} free); free space (al doctor shows where it went) "
            "or narrow the layer's worktree with #:sparse")


def _bytes(path: Path) -> int:
    """The disk blocks under `path` (symlinks not followed; vanishing files ignored)."""
    total = 0
    try:
        total += os.lstat(path).st_blocks * 512
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return total
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
    return total


def _tree_dir(ir: Mapping[str, Any] | None) -> str:
    for env in (ir or {}).get("environments") or []:
        if isinstance(env, dict) and env.get("kind") == "worktree" and env.get("dir"):
            return str(env["dir"])
    return "trees"


def _settled(layer_dir: Path, run_id: str) -> bool:
    try:
        record = json.loads((layer_dir / "agents" / run_id / "settlement.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("as") in SETTLED


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []


def _image_kind(row: dict[str, Any], layer_repos: set[str]) -> str:
    if _labels(row).get(TEST_LABEL) == "1":
        return "test"
    refs = []
    for name in _names(row):
        repository, _, tag = _short(name).rpartition(":")
        if not repository or "/" in tag:
            repository, tag = _short(name), "latest"
        refs.append((repository, repository.rsplit("/", 1)[-1], tag))
    if any(repo in layer_repos and tag == "latest" for repo, _, tag in refs):
        return "layer"
    if any(repo in layer_repos for repo, _, _ in refs):
        return "old"
    if any(last.startswith("al-") for _, last, _ in refs):
        return "other"
    return ""


def report(layer_dir: Path | str | None, ir: Mapping[str, Any] | None, *, run: Runner | None = None,
           disk_usage: Callable[[str], Any] = shutil.disk_usage, tmp_dir: Path | str | None = None,
           user: str | None = None) -> dict[str, Any]:
    """Where the space went: the layer's worktrees and run records (with a layer), al's images and
    the test leftovers (with podman: `run`), pytest's temp dirs of `user`, and the free space."""
    out: dict[str, Any] = {}
    layer = Path(layer_dir) if layer_dir else None
    if layer is not None:
        trees = _subdirs(layer / _tree_dir(ir))
        out["worktrees"] = {"count": len(trees), "bytes": sum(_bytes(t) for t in trees),
                            "settled": sum(1 for t in trees if _settled(layer, t.name))}
        records = [p for p in _subdirs(layer / "agents") if (p / "state.json").is_file()]
        out["records"] = {"count": len(records), "bytes": sum(_bytes(p) for p in records)}
    if run is None:
        out["images"] = None
        out["leftovers"] = None
    else:
        name = str((ir or {}).get("name") or "") or (layer.name if layer is not None and ir else "")
        repos = {f"al-{name}", f"al-{name}-base"} if name else set()
        images = {kind: {"count": 0, "bytes": 0} for kind in ("layer", "old", "test", "other")}
        for row in _rows(run, ["images", "--format", "json"]):
            kind = _image_kind(row, repos)
            if kind:
                images[kind]["count"] += 1
                images[kind]["bytes"] += _size(row.get("Size"))
        out["images"] = images
        objects = labelled_objects(run)
        out["leftovers"] = {"containers": len(objects["containers"]),
                            "running": sum(1 for c in objects["containers"] if c["running"]),
                            "images": len(objects["images"]), "bytes": sum(i["size"] for i in objects["images"])}
    if user is None:
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            user = str(os.getuid())
    pytest_dir = Path(tmp_dir or tempfile.gettempdir()) / f"pytest-of-{user}"
    out["pytest"] = {"path": str(pytest_dir), "bytes": _bytes(pytest_dir) if pytest_dir.exists() else 0}
    where = str(layer) if layer is not None else os.getcwd()
    usage = disk_usage(where)
    total, free = int(usage.total), int(usage.free)
    percent = 100.0 * free / total if total > 0 else 0.0
    out["free"] = {"path": where, "free": free, "total": total, "percent": round(percent, 1)}
    out["warning"] = (f"only {percent:.0f}% free on {where}; settled worktrees (al runs LAYER prune), test "
                      f"leftovers (al sandbox LAYER prune --tests) and pytest temp dirs can go"
                      if total > 0 and free / total < LOW_FREE else "")
    return out


def lines(report: Mapping[str, Any]) -> list[str]:
    """The report as `disk: …` lines (no blank line)."""
    out: list[str] = []
    if report.get("worktrees") is not None:
        w = report["worktrees"]
        out.append(f"disk: worktrees {w['count']} ({human_size(w['bytes'])}), {w['settled']} of settled runs")
    if report.get("records") is not None:
        r = report["records"]
        out.append(f"disk: run records {r['count']} ({human_size(r['bytes'])})")
    images = report.get("images")
    if images is None:
        out.append("disk: images: podman not found")
    else:
        parts = [f"{label} {images[kind]['count']} ({human_size(images[kind]['bytes'])})"
                 for kind, label in (("layer", "layer"), ("old", "old"), ("test", "test"), ("other", "other al"))]
        out.append("disk: images " + ", ".join(parts))
    left = report.get("leftovers")
    if left is not None:
        line = (f"disk: test leftovers {left['containers']} containers ({left['running']} running), "
                f"{left['images']} images ({human_size(left['bytes'])})")
        if left["containers"] or left["images"]:
            line += "; al sandbox LAYER prune --tests removes them"
        out.append(line)
    if report.get("pytest") is not None:
        out.append(f"disk: pytest temp {report['pytest']['path']} ({human_size(report['pytest']['bytes'])})")
    free = report.get("free")
    if free is not None:
        out.append(f"disk: free {human_size(free['free'])} of {human_size(free['total'])} "
                   f"({free['percent']:.0f}%) on {free['path']}")
    if report.get("warning"):
        out.append(f"disk: warning: {report['warning']}")
    return out
