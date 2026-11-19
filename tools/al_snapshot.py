"""`al snapshot LAYER`: a backup of a layer folder that the layer can be restored from.

    al snapshot LAYER [--note TEXT] [--json]      take one
    al snapshot LAYER --list [--json]             the snapshots, newest first
    al snapshot LAYER --restore ID [--dry-run]    bring the layer back to one

A layer folder that is its own git repository is snapshotted as a commit on its current branch (by
the al identity, hooks off, `--allow-empty` so every snapshot is its own commit), plus an archive of
the git-ignored agents/ (the run history). Any other layer folder, such as a layer inside the
application repository, is snapshotted as a dated archive and is never committed into the application.
Each snapshot writes a record LAYER/.work/snapshots/ID.json; the dashboard reads the same records
(kit/dashboard/dashboard.py snapshot_list, kept equal by tests/tools/test_snapshot.py).

Never in a snapshot: .work/ (runtime scratch, and the snapshots themselves), trees/ (checkouts), .git/,
.al-improve/ (al improve's own run state) and agents/<run>/objects/ (a run's quarantined git objects).

A restore takes a snapshot of the current state first (its id undoes the restore), then writes the
files that differ, removes the files the snapshot does not have (never under agents/ or the excluded
folders, and in a git layer never a git-ignored file) and adds back the agents/<run> folders that are
missing. It never touches an existing agents/<run> and never removes a folder that is not empty. In a
git layer the restored state stays uncommitted. Archives are extracted by checking every member
(relative names, no `..`, links that stay inside): a tampered archive cannot write outside its folder.
Standard library only, plus git through subprocess (found through tools/al_tools.py).
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Callable

import al_tools   # tools/ is on sys.path: tools/al loads this module from there

SNAPSHOT_DIR = ".work/snapshots"
IDENTITY = ["-c", "user.name=al", "-c", "user.email=al@localhost"]   # the identity of tools/al_improve.py
ID_RX = re.compile(r"^\d{8}T\d{6}Z(-\d{1,3})?$")
EXCLUDED_TOP = (".work", "trees", ".git", ".al-improve")
QUARANTINE = "objects"
MESSAGE = "al snapshot: {note}"
UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


class SnapshotError(Exception):
    pass


def _git(layer: Path, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(al_tools.argv(["git", "-C", str(layer), *args], layer), capture_output=True, text=text)
    if check and cp.returncode != 0:
        err = cp.stderr if text else cp.stderr.decode("utf-8", "replace")
        raise SnapshotError(f"git {' '.join(args)}: {err.strip()}")
    return cp


def is_own_repo(layer: Path) -> bool:
    """True when the layer folder is the top of its own git repository (not a folder inside the
    application's repository)."""
    if not (layer / ".git").exists():
        return False
    cp = subprocess.run(al_tools.argv(["git", "-C", str(layer), "rev-parse", "--show-toplevel"], layer),
                        capture_output=True, text=True)
    if cp.returncode != 0:
        return False
    return Path(cp.stdout.strip()).resolve() == layer.resolve()


def snapshot_dir(layer: Path) -> Path:
    return layer / SNAPSHOT_DIR


def new_id(layer: Path) -> str:
    base = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = snapshot_dir(layer)
    if not (directory / f"{base}.json").exists():
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if not (directory / f"{candidate}.json").exists():
            return candidate
    raise SnapshotError(f"too many snapshots within one second in {layer}")


def _excluded(parts: tuple[str, ...], agents: bool) -> bool:
    if not parts:
        return False
    if parts[0] in EXCLUDED_TOP:
        return True
    if parts[0] == "agents":
        return not agents or (len(parts) >= 3 and parts[2] == QUARANTINE)
    return False


def layer_files(layer: Path, agents: bool) -> list[str]:
    """The relative paths of the regular files and symlinks of the layer folder, without the excluded
    folders and quarantined objects, and with agents/ only when `agents` is true. Symlinked folders are
    listed as links and not followed; sockets and FIFOs are skipped. Sorted."""
    out: list[str] = []
    for top, dirs, files in os.walk(layer, followlinks=False):
        here = Path(top).relative_to(layer)
        keep = []
        for d in dirs:
            parts = (here / d).parts
            if _excluded(parts, agents):
                continue
            if (Path(top) / d).is_symlink():
                out.append((here / d).as_posix())
                continue
            keep.append(d)
        dirs[:] = keep
        for f in files:
            rel = here / f
            if _excluded(rel.parts, agents):
                continue
            mode = os.lstat(Path(top) / f).st_mode
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                out.append(rel.as_posix())
    return sorted(out)


def _write_archive(layer: Path, path: Path, rels: list[str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        for rel in rels:
            tar.add(layer / rel, arcname=rel, recursive=False)
    os.replace(tmp, path)


def _ensure_work(layer: Path) -> None:
    """LAYER/.work/ with a .gitignore of `*`: snapshots stay out of the layer's own commits and out of an
    application repository's status."""
    work = layer / ".work"
    work.mkdir(exist_ok=True)
    ignore = work / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("*\n")
    snapshot_dir(layer).mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_record(layer: Path, record: dict[str, Any]) -> None:
    path = snapshot_dir(layer) / f"{record['id']}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(tmp, path)


def _merge_in_progress(layer: Path) -> bool:
    status = _git(layer, "status", "--porcelain").stdout
    if any(line[:2] in UNMERGED for line in status.splitlines()):
        return True
    gitdir = Path(_git(layer, "rev-parse", "--absolute-git-dir").stdout.strip())
    return any((gitdir / name).exists() for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply"))


def take(layer: Path, note: str) -> dict[str, Any]:
    layer = layer.resolve()
    if not ((layer / "agentic.rkt").is_file() or (layer / "LLM.txt").is_file()):
        raise SnapshotError(f"{layer} is not a layer folder (it has neither agentic.rkt nor LLM.txt)")
    _ensure_work(layer)
    snap_id = new_id(layer)
    directory = snapshot_dir(layer)
    record: dict[str, Any] = {"id": snap_id, "created": _now_iso(), "note": note}
    if is_own_repo(layer):
        if _merge_in_progress(layer):
            raise SnapshotError(f"a merge or rebase is in progress in {layer}: finish or abort it first")
        _git(layer, "add", "-A", "--", ".", ":!.work", ":!trees", ":!.al-improve")
        _git(layer, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *IDENTITY,
             "commit", "-q", "--allow-empty", "--no-verify", "-m", MESSAGE.format(note=note))
        sha = _git(layer, "rev-parse", "HEAD").stdout.strip()
        agents = [rel for rel in layer_files(layer, agents=True) if rel.startswith("agents/")]
        agents_archive = directory / f"{snap_id}.agents.tar.gz"
        _write_archive(layer, agents_archive, agents)
        tracked = _git(layer, "ls-tree", "-r", "--name-only", "-z", sha).stdout.split("\0")
        record |= {"kind": "git", "commit": sha, "archive": None,
                   "agents_archive": agents_archive.relative_to(layer).as_posix(),
                   "files": len([t for t in tracked if t]) + len(agents)}
    else:
        rels = layer_files(layer, agents=True)
        archive = directory / f"{snap_id}.tar.gz"
        _write_archive(layer, archive, rels)
        record |= {"kind": "archive", "archive": archive.relative_to(layer).as_posix(), "files": len(rels)}
    _write_record(layer, record)
    return record


def records(layer: Path) -> list[dict[str, Any]]:
    """Every .work/snapshots/ID.json that names its own id, newest first."""
    directory = snapshot_dir(layer)
    out = []
    if not directory.is_dir():
        return out
    for path in directory.glob("*.json"):
        if not ID_RX.match(path.stem) or not path.is_file() or path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id") == path.stem:
            out.append(data)
    return sorted(out, key=lambda r: _id_key(r["id"]), reverse=True)


def _id_key(snap_id: str) -> tuple[str, int]:
    base, _, n = snap_id.partition("-")
    return base, int(n or 1)


def _inside(root: Path, target: str) -> bool:
    root_s = os.path.realpath(root)
    target_s = os.path.realpath(target)
    return target_s == root_s or target_s.startswith(root_s + os.sep)


def _extract(tar: tarfile.TarFile, into: Path) -> None:
    """Extract every member after checking it: files, folders and symlinks only, relative names with
    no `..`, links that resolve inside `into`, and nothing written through a link."""
    into.mkdir(parents=True, exist_ok=True)
    for member in tar.getmembers():
        name = member.name
        parts = Path(name).parts
        if not name or os.path.isabs(name) or ".." in parts or name.startswith("/"):
            raise SnapshotError(f"unsafe archive member {name!r}")
        if not (member.isfile() or member.isdir() or member.issym()):
            raise SnapshotError(f"archive member {name!r} is not a file, folder or symlink")
        target = into / name
        if not _inside(into, str(target.parent)):
            raise SnapshotError(f"archive member {name!r} would be written outside the restore folder")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(into, str(target.parent)):
            raise SnapshotError(f"archive member {name!r} would be written outside the restore folder")
        if target.is_symlink() or target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if member.issym():
            link = member.linkname
            if os.path.isabs(link) or not _inside(into, os.path.join(str(target.parent), link)):
                raise SnapshotError(f"archive member {name!r} links outside the restore folder ({link})")
            os.symlink(link, target)
            continue
        source = tar.extractfile(member)
        if source is None:
            raise SnapshotError(f"archive member {name!r} cannot be read")
        with source, open(target, "wb") as out:
            shutil.copyfileobj(source, out)
        os.chmod(target, (member.mode & 0o777) or 0o644)


def _materialise(layer: Path, record: dict[str, Any], into: Path) -> None:
    if record.get("kind") == "git":
        commit = str(record.get("commit") or "")
        if not re.fullmatch(r"[0-9a-f]{7,64}", commit):
            raise SnapshotError(f"snapshot {record['id']} names no commit")
        cp = _git(layer, "archive", "--format=tar", commit, text=False)
        with tarfile.open(fileobj=io.BytesIO(cp.stdout), mode="r:") as tar:
            _extract(tar, into)
        archive = record.get("agents_archive")
    elif record.get("kind") == "archive":
        archive = record.get("archive")
    else:
        raise SnapshotError(f"snapshot {record['id']} has an unknown kind {record.get('kind')!r}")
    if archive:
        path = (layer / str(archive)).resolve()
        if not _inside(snapshot_dir(layer), str(path)) or not path.is_file():
            raise SnapshotError(f"the archive of snapshot {record['id']} is missing ({archive})")
        with tarfile.open(path, "r:gz") as tar:
            _extract(tar, into)


def _same(a: Path, b: Path) -> bool:
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if not b.is_file():
        return False
    return (a.read_bytes() == b.read_bytes()
            and (a.stat().st_mode & 0o111 != 0) == (b.stat().st_mode & 0o111 != 0))


def _not_ignored(layer: Path) -> set[str]:
    """The files of a git layer that git does not ignore (tracked, or untracked and not ignored)."""
    out = _git(layer, "ls-files", "-co", "--exclude-standard", "-z").stdout
    return {p for p in out.split("\0") if p}


def _copy(source: Path, target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        raise SnapshotError(f"{target} is a folder; the snapshot has a file there")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
    else:
        shutil.copy2(source, target)


def _prune_empty(layer: Path, path: Path) -> None:
    parent = path.parent
    while parent != layer and layer in parent.parents:
        try:
            parent.rmdir()          # only an empty folder
        except OSError:
            return
        parent = parent.parent


def restore(layer: Path, snap_id: str, dry_run: bool, say: Callable[[str], None] = print,
            shown: str | None = None) -> dict[str, Any]:
    layer = layer.resolve()
    shown = shown or str(layer)
    record = next((r for r in records(layer) if r["id"] == snap_id), None) if ID_RX.match(snap_id) else None
    if record is None:
        raise SnapshotError(f"no snapshot {snap_id} in {shown}; al snapshot {shown} --list shows them")
    into = layer / ".work" / f"restore-{snap_id}"
    if into.exists():
        shutil.rmtree(into)
    try:
        _materialise(layer, record, into)
        then = set(layer_files(into, agents=False))
        now = set(layer_files(layer, agents=False))
        to_restore = sorted(rel for rel in then if not _same(into / rel, layer / rel))
        removable = now - then
        if is_own_repo(layer):          # a git-ignored file (local.env, …) is never in the commit: keep it
            removable &= _not_ignored(layer)
        to_remove = sorted(removable)
        snap_agents = into / "agents"
        runs = sorted(d.name for d in snap_agents.iterdir()
                      if d.is_dir() and not d.is_symlink() and not (layer / "agents" / d.name).exists()
                      and not (layer / "agents" / d.name).is_symlink()) if snap_agents.is_dir() else []
        result = {"id": snap_id, "restore": to_restore, "remove": to_remove, "runs": runs, "dry_run": dry_run}
        if dry_run:
            say("would take a snapshot of the current state first")
            for rel in to_restore:
                say(f"  restore {rel}")
            for rel in to_remove:
                say(f"  remove {rel}")
            for run in runs:
                say(f"  add back run agents/{run}")
            say(f"{len(to_restore)} file(s) to restore, {len(to_remove)} to remove, "
                f"{len(runs)} run folder(s) to add back (al snapshot {shown} --restore {snap_id} does it)")
            return result
        before = take(layer, f"before restore of {snap_id}")
        result["before"] = before["id"]
        say(f"snapshot {before['id']} of the current state "
            f"(al snapshot {shown} --restore {before['id']} undoes this restore)")
        for rel in to_restore:
            _copy(into / rel, layer / rel)
        for rel in to_remove:
            path = layer / rel
            if path.is_symlink() or path.is_file():
                path.unlink()
                _prune_empty(layer, path)
        for run in runs:
            target = layer / "agents" / run
            if target.exists() or target.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(snap_agents / run, target, symlinks=True)
        tail = ("; the restored state is uncommitted in the layer's git repository"
                if is_own_repo(layer) else "")
        say(f"restored {snap_id}: {len(to_restore)} files written, {len(to_remove)} removed, "
            f"{len(runs)} run folders added back{tail}")
        return result
    finally:
        if into.exists():
            shutil.rmtree(into)


def main(a: Any, al: Any) -> int:
    shown = str(a.layer)
    if a.list and a.restore:
        raise al.AlError("snapshot: --list and --restore exclude each other")
    if (a.list or a.restore) and a.note is not None:
        raise al.AlError("snapshot: --note goes with taking a snapshot, not with --list or --restore")
    if a.dry_run and not a.restore:
        raise al.AlError("snapshot: --dry-run goes with --restore")
    layer = a.layer.resolve()
    try:
        if a.list:
            rows = records(layer)
            if a.json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print(f"no snapshots of {shown} yet")
            else:
                table = [("ID", "kind", "created", "note")]
                table += [(str(r.get("id")), str(r.get("kind", "")), str(r.get("created", "")), str(r.get("note", "")))
                          for r in rows]
                widths = [max(len(row[i]) for row in table) for i in range(3)]
                for row in table:
                    print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:3])) + "  " + row[3])
            return 0
        if a.restore:
            restore(layer, a.restore, a.dry_run, say=lambda line: print(line, flush=True), shown=shown)
            return 0
        record = take(layer, a.note if a.note is not None else "by hand")
    except SnapshotError as exc:
        raise al.AlError(f"snapshot: {exc}") from exc
    if a.json:
        print(json.dumps(record, indent=2))
    elif record["kind"] == "git":
        print(f"snapshot {record['id']} (commit {record['commit'][:12]}) of {shown}")
    else:
        print(f"snapshot {record['id']} (archive {record['archive']}) of {shown}")
    return 0
