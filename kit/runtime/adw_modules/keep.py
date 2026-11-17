"""Keep the work of a run whose workflow has no ship step.

A workflow without a ship step (`plan-build`, a single-command run, any workflow that ends in
agent steps) never commits, so its worktree would be removed with whatever the agents wrote in it.
Before teardown, `keep_work` looks at the worktree. When anything is left (tracked changes,
untracked files that are not ignored, commits not on the base branch), it:

1. commits what is uncommitted on the run's branch (`work of run <id>, kept because the workflow
   has no ship step [adw <id>]`), so teardown's `delete_branch_if_merged` leaves the branch alone
   and a sandboxed teardown publishes the commit with the rest of the run's HEAD;
2. copies the changed files and a `changes.diff` into the run's artefacts (`kept-files`,
   `kept-diff`), size-limited, so they stay readable from the dashboard after the branch is gone;
3. records `state.kept` and prints a `kept:` line.

Never raises: when keeping fails, the failure is recorded in `state.kept["error"]` and the
worktree is kept (`env_stack.keep_worktree = True`), so nothing is discarded silently.

Promotion runs are never kept: they merge and tag through promotion.py, not through a ship step.

Files copied in from the app (`copyFiles`, e.g. `.env`) are never committed and never copied.
A symlink in the worktree is never followed (a sandboxed worktree is untrusted).

Standard library only. The protected modules are used, never changed: `gitsafe.session` /
`gitsafe.run` for a sandboxed run's git, `ship.IDENTITY` for the commit identity; plain
`git -C <worktree>` otherwise.
"""
from __future__ import annotations

import contextlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from . import artefacts, events, gitsafe
from .discipline import _porcelain_z, scratch_dir
from .errors import LayerError, StepFailed

MAX_FILE_BYTES = 1_000_000      # a larger file stays on the branch only, listed as too large
MAX_TOTAL_BYTES = 20_000_000    # the copied files of one run, together
MAX_DIFF_BYTES = 5_000_000      # changes.diff is cut here, with a line that says so
FILES_ID = "kept-files"
DIFF_ID = "kept-diff"
STEP = "keep"
GIT_TIMEOUT = 120
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _message(state: Any) -> str:
    return f"work of run {state.adw_id}, kept because the workflow has no ship step [adw {state.adw_id}]"


def _git(env_stack: Any, state: Any, args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    """git in the run's worktree: through gitsafe when sandboxed, else `git -C <worktree>`."""
    sb = getattr(env_stack, "sandbox", None)
    if sb is not None:
        return gitsafe.run(sb, args, STEP, check=check)
    try:
        result = subprocess.run(["git", "-C", str(state.worktree), *args], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        if check:
            raise StepFailed(STEP, f"git {' '.join(args)} failed: {exc}") from exc
        return subprocess.CompletedProcess(args, 1, "", str(exc))
    if check and result.returncode != 0:
        raise StepFailed(STEP, f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.strip("/")
    path = path.strip("/")
    return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))


def _never_kept(layer: Any, state: Any, adw: dict[str, Any]) -> tuple[list[str], str]:
    """(copyFiles of the adw's worktree environment, the in-repo layer directory or "")."""
    never_commit: list[str] = []
    for option_id in adw.get("environment", []):
        option = layer.option("environments", option_id)
        if option.get("kind") == "worktree":
            never_commit += [str(p).strip("/") for p in option.get("copyFiles", []) if str(p).strip("/")]
    in_repo = str(getattr(layer, "in_repo_dir", "") or "")
    try:
        changes_layer = bool(layer.changes_layer(state.adw))
    except (LayerError, AttributeError):
        changes_layer = False
    return never_commit, (in_repo if in_repo and not changes_layer else "")


def _leftovers(env_stack: Any, state: Any, never_commit: list[str], in_repo: str) -> tuple[list[str], list[str]]:
    """(uncommitted paths to keep, uncommitted paths under the in-repo layer directory)."""
    status = _git(env_stack, state, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], check=True)
    keep: list[str] = []
    layer_paths: list[str] = []
    for _xy, path, old in _porcelain_z(status.stdout):
        for item in (path, old):
            if not item or any(_under(item, p) for p in never_commit):
                continue
            if in_repo and _under(item, in_repo):
                layer_paths.append(item)
            elif item not in keep:
                keep.append(item)
    return keep, layer_paths


def _commits_ahead(env_stack: Any, state: Any, base: str) -> int:
    result = _git(env_stack, state, ["rev-list", "--count", f"refs/heads/{base}..HEAD"])
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        return 0


def _commit(env_stack: Any, state: Any, never_commit: list[str], in_repo: str) -> bool:
    """Commit everything uncommitted except copyFiles and the in-repo layer. True when it committed."""
    from .ship import IDENTITY  # protected module, used read-only

    _git(env_stack, state, ["add", "-A"], check=True)
    for path in [*never_commit, *([in_repo] if in_repo else [])]:
        _git(env_stack, state, ["reset", "-q", "--", path])
    staged = _git(env_stack, state, ["diff", "--cached", "--name-only"], check=True)
    if not staged.stdout.strip():
        return False
    _git(env_stack, state, [*IDENTITY, "commit", "-q", "-m", _message(state)], check=True)
    return True


def _base(env_stack: Any, state: Any, default: str) -> str:
    result = _git(env_stack, state, ["merge-base", f"refs/heads/{default}", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else EMPTY_TREE


def _changed_files(env_stack: Any, state: Any, base: str) -> list[tuple[str, str]]:
    """(status letter, path) of every file the run's commits change relative to `base`."""
    result = _git(env_stack, state, ["diff", "--name-status", "--no-renames", "-z", base, "HEAD"], check=True)
    parts = [part for part in result.stdout.split("\0")]
    changed: list[tuple[str, str]] = []
    for i in range(0, len(parts) - 1, 2):
        if parts[i]:
            changed.append((parts[i][:1], parts[i + 1]))
    return changed


def _write_diff(layer: Any, env_stack: Any, state: Any, base: str) -> Path:
    result = _git(env_stack, state, ["diff", "--no-ext-diff", "--binary", base, "HEAD"], check=True)
    text = result.stdout
    data = text.encode("utf-8", "replace")
    if len(data) > MAX_DIFF_BYTES:
        text = data[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
        text += (f"\n\n[the diff is cut at {MAX_DIFF_BYTES} bytes of {len(data)}; "
                 f"the whole change is on branch {state.branch or '(none)'}]\n")
    target = scratch_dir(layer, state) / "keep" / "changes.diff"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _copyable(worktree: Path, relative: str) -> Path | None:
    """The file to copy: a regular file inside the worktree, never through a symlink; else None."""
    root = worktree.resolve()
    candidate = root / relative
    try:
        if not candidate.resolve().is_relative_to(root):
            return None
        current = root
        for part in Path(relative).parts:  # no symlink anywhere on the way
            current = current / part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return None
        return candidate if stat.S_ISREG(os.lstat(candidate).st_mode) else None
    except (OSError, ValueError):
        return None


def _copy_artefacts(layer: Any, state: Any, paths: list[str],
                    diff: Path) -> tuple[int, list[dict[str, Any]], list[str]]:
    """(files copied, too large, skipped)."""
    worktree = Path(state.worktree)
    matches: list[tuple[str, Path]] = []
    too_large: list[dict[str, Any]] = []
    skipped: list[str] = []
    total = 0
    for relative in paths:
        source = _copyable(worktree, relative)
        if source is None:
            if os.path.lexists(worktree / relative):
                skipped.append(relative)
            continue
        size = source.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            too_large.append({"path": relative, "bytes": size})
            continue
        total += size
        matches.append((relative, source))
    artefacts.add_files(layer, state, FILES_ID, "file", STEP, matches)
    artefacts.add_files(layer, state, DIFF_ID, "file", STEP, [("changes.diff", diff)])
    return len(matches), too_large, skipped


def _skip(layer: Any, state: Any, adw: dict[str, Any], env_stack: Any) -> bool:
    if any(item.get("kind") == "ship" for item in adw.get("steps", [])):
        return True
    if getattr(state, "promotion", None) or any(item.get("kind") == "promote" for item in adw.get("steps", [])):
        return True
    if state.dry_run or state.status == "waiting" or getattr(env_stack, "read_only", False):
        return True
    return not state.worktree or not Path(state.worktree).is_dir()


def keep_work(layer: Any, state: Any, adw: dict[str, Any], env_stack: Any) -> dict[str, Any] | None:
    """Keep what a run of a workflow without a ship step left in its worktree (see the module
    docstring). Returns `state.kept`, or None when this run is not one to keep. Never raises."""
    try:
        if _skip(layer, state, adw, env_stack):
            return None
    except Exception:  # noqa: BLE001 — a missing attribute must not stop the teardown
        return None
    try:
        never_commit, in_repo = _never_kept(layer, state, adw)
        sandbox = getattr(env_stack, "sandbox", None)
        default = layer.default_branch
        with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
            leftovers, layer_paths = _leftovers(env_stack, state, never_commit, in_repo)
            ahead = _commits_ahead(env_stack, state, default)
            if not leftovers and not ahead and not layer_paths:
                state.kept = {"changed": False, "workflow_ships": False}
                state.save(layer.layer_dir)
                return state.kept
            committed = _commit(env_stack, state, never_commit, in_repo) if leftovers else False
            commit = _git(env_stack, state, ["rev-parse", "HEAD"], check=True).stdout.strip()
            base = _base(env_stack, state, default)
            changed = _changed_files(env_stack, state, base)
            if not committed and not changed and not layer_paths:
                state.kept = {"changed": False, "workflow_ships": False}
                state.save(layer.layer_dir)
                return state.kept
            diff = _write_diff(layer, env_stack, state, base)
        files = [path for letter, path in changed]
        to_copy = [path for letter, path in changed if letter != "D"] + layer_paths
        to_copy = [path for path in dict.fromkeys(to_copy) if not any(_under(path, p) for p in never_commit)]
        copied, too_large, skipped = _copy_artefacts(layer, state, to_copy, diff)
        diff_path = f"{Path(diff).name}"
        diff_record = next((item for item in state.artefacts if item.get("id") == DIFF_ID), None)
        state.kept = {
            "changed": True, "workflow_ships": False, "branch": state.branch, "commit": commit,
            "committed": committed, "files": files, "count": len(files), "left_uncommitted": layer_paths,
            "artefacts": copied, "too_large": too_large, "skipped": skipped,
            "diff": str(diff_record["path"]) if diff_record else diff_path,
        }
    except Exception as exc:  # noqa: BLE001 — keeping must never mask the run's result or lose the worktree
        text = (str(exc) or type(exc).__name__)[:500]
        state.kept = {"changed": None, "error": text}
        env_stack.keep_worktree = True
        print(f"[adw {state.adw_id}] keep failed: {text}; the worktree is kept: {state.worktree}")
        with contextlib.suppress(Exception):
            state.save(layer.layer_dir)
        events.emit("run.kept", status="failed", error=text)
        return state.kept
    where = f"branch {state.branch}" if state.branch else "no branch"
    events.say(f"[adw {state.adw_id}] kept: {where} ({len(files)} files); the workflow has no ship step",
               "run.kept", branch=state.branch, files=len(files), commit=commit)
    for item in too_large:
        print(f"[adw {state.adw_id}] kept: {item['path']} ({item['bytes']} bytes) is too large for the artefacts; "
              f"it is on {where}")
    state.save(layer.layer_dir)
    return state.kept
