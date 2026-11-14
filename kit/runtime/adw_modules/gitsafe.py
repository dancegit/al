"""Host-side git for sandboxed (podman) runs.

In a sandboxed run the container can write the run's worktree, its admin dir
`<app>/.git/worktrees/<name>` and the per-run object quarantine. Everything the HOST does with
git against such a worktree goes through this module, which

1. freezes the container (so nothing can change between check and use) and verifies that the
   container-writable git metadata has not been turned into a weapon: the worktree's `.git`
   file, the admin dir (no config.worktree / hooks / symlinks / special files, detached HEAD)
   and the quarantine (only object storage, no alternates, no symlinks);
2. runs git with GIT_DIR / GIT_COMMON_DIR / GIT_WORK_TREE pinned explicitly (the `.git` file is
   never trusted), the quarantine object variables and hardening config.

Objects created in the sandbox stay in the quarantine until the host decides to update a ref;
`import_objects` then repacks exactly the objects reachable from the commit and feeds them to
`git index-pack --strict` in the app repository, which re-hashes and fsck-checks every object.
Ref updates (`update_ref`, `run_app`) run against the app repository without the quarantine.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, ContextManager, Iterator

from .errors import StepFailed

GIT_TIMEOUT = 300
INTEGRITY = "git-integrity"
HARDENING = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.file.allow=user",
             "-c", "diff.ignoreSubmodules=dirty", "-c", "gc.auto=0", "-c", "maintenance.auto=false")
# Variables that would make git look somewhere other than where we point it.
_LOCATION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                       "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                       "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_QUARANTINE_PATH", "GIT_REPLACE_REF_BASE",
                       "GIT_NO_REPLACE_OBJECTS", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE")
_OID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_FANOUT = re.compile(r"[0-9a-f]{2}")


class Sandbox:
    """Paths of one sandboxed run, plus `freeze`: a context manager that stops every process
    that could write them (the run's container) for as long as the host runs git."""

    def __init__(self, app_git: Path, worktree: Path, admin: Path, quarantine: Path,
                 freeze: Callable[[], ContextManager[None]] | None = None) -> None:
        self.app_git, self.worktree, self.admin, self.quarantine = app_git, worktree, admin, quarantine
        self.freeze = freeze or contextlib.nullcontext
        self._depth = 0

    @classmethod
    def for_run(cls, app_root: Path, tree_dir: Path, layer_dir: Path, adw_id: str,
                freeze: Callable[[], ContextManager[None]] | None = None) -> "Sandbox":
        app_git = Path(app_root).resolve() / ".git"
        return cls(app_git, Path(tree_dir).resolve() / adw_id, app_git / "worktrees" / adw_id,
                   Path(layer_dir).resolve() / "agents" / adw_id / "objects", freeze)


def _refuse(reason: str) -> None:
    raise StepFailed(INTEGRITY, f"sandboxed worktree refused: {reason}")


def _read_exact(path: Path, what: str) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        _refuse(f"{what} is missing ({path})")
    if not stat.S_ISREG(info.st_mode):
        _refuse(f"{what} is not a regular file ({path})")
    if info.st_size > 4096:
        _refuse(f"{what} is suspiciously large ({path})")
    return path.read_text(encoding="utf-8", errors="replace")


def _expect_dir(path: Path, what: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        _refuse(f"{what} is missing ({path})")
    if not stat.S_ISDIR(info.st_mode):
        _refuse(f"{what} is not a real directory ({path})")


def _walk_plain(root: Path, what: str) -> Iterator[tuple[Path, os.stat_result]]:
    """Every entry below root; refuses symlinks and anything that is not a file or directory."""
    for directory, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(directory) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                _refuse(f"symlink in {what}: {path}")
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                _refuse(f"special file in {what}: {path}")
            yield path, info


def verify(sb: Sandbox) -> None:
    """Raise StepFailed("git-integrity", …) unless the container-writable git metadata is benign."""
    _expect_dir(sb.worktree, "worktree")
    content = _read_exact(sb.worktree / ".git", "the worktree's .git file")
    if content not in (f"gitdir: {sb.admin}\n", f"gitdir: {sb.admin}"):
        _refuse(f"the worktree's .git file was rewritten: {content.strip()[:200]!r}")
    _expect_dir(sb.admin, "worktree admin dir")
    if _read_exact(sb.admin / "commondir", "commondir").rstrip("\n") not in ("../..", str(sb.app_git)):
        _refuse("commondir was rewritten")
    if _read_exact(sb.admin / "gitdir", "gitdir").rstrip("\n") != str(sb.worktree / ".git"):
        _refuse("gitdir was rewritten")
    for forbidden in ("config.worktree", "hooks", "config"):
        if os.path.lexists(sb.admin / forbidden):
            _refuse(f"{forbidden} present in the admin dir")
    for _path, _info in _walk_plain(sb.admin, "the admin dir"):
        pass
    head = _read_exact(sb.admin / "HEAD", "HEAD").strip()
    if not _OID.fullmatch(head):
        _refuse(f"HEAD must stay detached in a sandboxed run, found {head[:100]!r}")
    _expect_dir(sb.quarantine, "object quarantine")
    for entry in os.listdir(sb.quarantine):
        path = sb.quarantine / entry
        if not (_FANOUT.fullmatch(entry) or entry in ("pack", "info")):
            _refuse(f"unexpected entry in the object quarantine: {entry!r}")
        _expect_dir(path, f"quarantine entry {entry}")
    for path, _info in _walk_plain(sb.quarantine, "the object quarantine"):
        if path.parent == sb.quarantine / "info" and path.name in ("alternates", "http-alternates", "grafts"):
            _refuse(f"{path.name} in the object quarantine")


@contextlib.contextmanager
def session(sb: Sandbox) -> Iterator[None]:
    """Freeze the sandbox and verify it once; nested sessions reuse the outer one."""
    if sb._depth:
        sb._depth += 1
        try:
            yield
        finally:
            sb._depth -= 1
        return
    with sb.freeze():
        verify(sb)
        sb._depth = 1
        try:
            yield
        finally:
            sb._depth = 0


def _base_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _LOCATION_VARIABLES}


def worktree_env(sb: Sandbox, quarantine: bool = True) -> dict[str, str]:
    env = {**_base_env(), "GIT_DIR": str(sb.admin), "GIT_COMMON_DIR": str(sb.app_git),
           "GIT_WORK_TREE": str(sb.worktree)}
    if quarantine:
        env["GIT_OBJECT_DIRECTORY"] = str(sb.quarantine)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(sb.app_git / "objects")
    return env


def app_env(sb: Sandbox) -> dict[str, str]:
    """The app repository itself (trusted: read-only to the container), no worktree, no quarantine."""
    return {**_base_env(), "GIT_DIR": str(sb.app_git)}


def _exec(argv: list[str], env: dict[str, str], cwd: Path, step: str, check: bool, timeout: int,
          stdin: object = None, stdout: object = subprocess.PIPE, text: bool = True) -> subprocess.CompletedProcess:
    shown = " ".join(argv[len(HARDENING) + 1:])
    try:
        result = subprocess.run(argv, env=env, cwd=cwd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE,
                                text=text, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed(step, f"git {shown} failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr or result.stdout or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise StepFailed(step, f"git {shown} failed: {detail.strip()}")
    return result


def run(sb: Sandbox, args: list[str], step: str, check: bool = True,
        timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """git <args> in the sandboxed worktree (objects written go to the quarantine)."""
    with session(sb):
        return _exec(["git", *HARDENING, *args], worktree_env(sb), sb.worktree, step, check, timeout)


def run_app(sb: Sandbox, args: list[str], step: str, check: bool = True,
            timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """git <args> in the app repository (ref updates, pushes). Never sees the quarantine."""
    return _exec(["git", *HARDENING, *args], app_env(sb), sb.app_git.parent, step, check, timeout)


def import_objects(sb: Sandbox, commit: str, step: str) -> str:
    """Move the objects `commit` needs from the quarantine into the app's object store and prove
    the app now has the complete commit. Returns the full commit id.

    Only objects reachable from `commit` and not already reachable from the app's own refs are
    taken; they are repacked and indexed by `git index-pack --strict`, which recomputes every
    object id and runs fsck checks, so a forged or malformed object cannot enter the app."""
    # The app's refs (never this worktree's HEAD or per-worktree refs: --single-worktree in the
    # app's GIT_DIR) with the quarantine visible as the primary object store.
    staging = {**app_env(sb), "GIT_OBJECT_DIRECTORY": str(sb.quarantine),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(sb.app_git / "objects")}
    root = sb.app_git.parent
    with session(sb):
        full = run(sb, ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"], step).stdout.strip()
        listing = _exec(["git", *HARDENING, "rev-list", "--single-worktree", "--objects", full, "--not", "--all"],
                        staging, root, step, True, GIT_TIMEOUT).stdout
        if listing.strip():
            with tempfile.TemporaryFile() as objects, tempfile.TemporaryFile() as pack:
                objects.write(listing.encode())
                objects.seek(0)
                _exec(["git", *HARDENING, "pack-objects", "--stdout"], staging, root, step, True, GIT_TIMEOUT,
                      stdin=objects, stdout=pack, text=False)
                pack.seek(0)
                _exec(["git", *HARDENING, "index-pack", "--strict", "--stdin"], app_env(sb), root, step, True,
                      GIT_TIMEOUT, stdin=pack, text=False)
    run_app(sb, ["cat-file", "-e", f"{full}^{{tree}}"], step)
    run_app(sb, ["rev-list", "--single-worktree", "--quiet", "--objects", full, "--not", "--all"], step)
    return full


def update_ref(sb: Sandbox, ref: str, commit: str, step: str, message: str = "adw") -> None:
    """Create or fast-forward `ref` in the app repository to `commit` (already imported)."""
    old = run_app(sb, ["rev-parse", "--verify", "--quiet", ref], step, check=False).stdout.strip()
    if old == commit:
        return
    if old and run_app(sb, ["merge-base", "--is-ancestor", old, commit], step, check=False).returncode != 0:
        raise StepFailed(step, f"refusing to move {ref}: {commit[:12]} is not a fast-forward of {old[:12]}")
    run_app(sb, ["update-ref", "-m", message, ref, commit, old or "0" * len(commit)], step)


def remove_quarantine(sb: Sandbox) -> None:
    shutil.rmtree(sb.quarantine, ignore_errors=True)
