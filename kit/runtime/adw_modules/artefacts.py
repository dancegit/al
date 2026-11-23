from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from . import storage
from .errors import LayerError, StepFailed

GLOB_KINDS = {"junit", "coverage", "image", "video", "audio", "trace", "html", "file"}
FFMPEG_TIMEOUT = 120
GIT_TIMEOUT = 120


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_path(layer: Any, adw_id: str) -> Path:
    return storage.artefacts_root(layer) / adw_id / "manifest.json"


def _probe_duration(source: Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT, check=False,
    )
    if probe.returncode != 0:
        raise ValueError(probe.stderr.strip() or f"ffprobe exited {probe.returncode}")
    duration = float(probe.stdout.strip())
    if duration <= 0:
        raise ValueError(f"video duration must be positive, got {duration}")
    return duration


def extract_keyframes(source: Path, target_dir: Path, count: int) -> tuple[list[Path], str | None]:
    """Extract `count` evenly spaced PNG frames; returns (frames, warning). Never raises for tool problems."""
    if count <= 0:
        return [], None
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return [], "ffmpeg/ffprobe not found; keyframes were not extracted"
    try:
        duration = _probe_duration(source)
        target_dir.mkdir(parents=True, exist_ok=True)
        frames: list[Path] = []
        for index in range(count):
            timestamp = duration * (index + 0.5) / count
            output = target_dir / f"{source.stem}-{index + 1:02d}.png"
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(source),
                 "-frames:v", "1", str(output)],
                capture_output=True, text=True, timeout=FFMPEG_TIMEOUT, check=False,
            )
            if result.returncode != 0 or not output.is_file():
                raise ValueError(result.stderr.strip() or f"ffmpeg produced no frame at {timestamp:.3f}s")
            frames.append(output)
        return frames, None
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return [], f"keyframe extraction failed for {source.name}: {exc}"


def _changed_by_run(layer: Any, state: Any, worktree: Path) -> set[str]:
    """The paths this run changed in its worktree: new, modified and ignored files (build output)
    as `git status` lists them, plus what the run committed since it left the target branch. Read
    only; any git failure gives an empty set (the order of an artefact's files is then by name)."""
    del state

    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or f"git exited {result.returncode}")
        return result.stdout

    try:
        if Path(git("rev-parse", "--show-toplevel").strip()).resolve() != Path(worktree).resolve():
            return set()                        # a directory inside another repository says nothing
        changed: set[str] = set()
        fields = git("status", "--porcelain", "-z", "--untracked-files=all", "--ignored=matching").split("\0")
        index = 0
        while index < len(fields):
            entry = fields[index]
            index += 1
            if len(entry) < 4:
                continue
            changed.add(entry[3:])
            if entry[0] in "RC":                # a rename or copy: the next field is the old path
                index += 1
        base = git("merge-base", "HEAD", str(layer.default_branch)).strip()
        changed |= {name for name in git("diff", "--name-only", "-z", base).split("\0") if name}
        return changed
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError, KeyError):
        return set()


def _glob_matches(worktree: Path, option: dict[str, Any],
                  changed: set[str] | None = None,
                  wanted: Sequence[str] = ()) -> tuple[list[tuple[str, Path]], int]:
    """(the files of an artefact option to hand over, how many more matched and were left out).
    The files a proof plan row writes (`wanted` globs) come first, then the files this run changed,
    then the others, each group by name; an image artefact keeps the first `maxFiles` of them."""
    pattern = str(option.get("glob") or "")
    if not pattern:
        raise LayerError(f"artefact {option.get('id')!r} has an empty glob")
    changed = changed or set()
    matches = sorted(((path.relative_to(worktree).as_posix(), path) for path in worktree.glob(pattern)
                      if path.is_file() and path.resolve().is_relative_to(worktree)),
                     key=lambda item: (0 if any(fnmatch.fnmatchcase(item[0], g) for g in wanted)
                                        else 1 if item[0] in changed else 2, item[0]))
    total = len(matches)
    if option.get("kind") == "image":
        matches = matches[: max(0, int(option.get("maxFiles", 0)))]
    return matches, total - len(matches)


def _log_matches(layer: Any, state: Any) -> list[tuple[str, Path]]:
    checks_dir = Path(layer.layer_dir) / "agents" / state.adw_id / "checks"
    if not checks_dir.is_dir():
        return []
    return [(path.name, path) for path in sorted(checks_dir.glob("*.log")) if path.is_file()]


def _tail(path: Path, lines: int) -> str:
    if lines <= 0:
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _record(layer: Any, state: Any, store: storage.Storage, option: dict[str, Any], step: str,
            relative: str, source: Path) -> dict[str, Any]:
    run_root = storage.artefacts_root(layer) / state.adw_id
    artefact_id = str(option["id"])
    destination = run_root / artefact_id / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    record: dict[str, Any] = {
        "id": artefact_id,
        "type": option["kind"],
        "step": step,
        "path": destination.relative_to(layer.layer_dir).as_posix(),
        "bytes": destination.stat().st_size,
        "sha256": sha256_of(destination),
        "url": store.put(destination, _key(layer, state, run_root, destination)),
    }
    if option["kind"] == "log":
        record["tail"] = _tail(destination, int(option.get("tailLines", 0)))
    if option["kind"] == "video":
        frames_dir = destination.parent / f"{destination.stem}.keyframes"
        frames, warning = extract_keyframes(destination, frames_dir, int(option.get("keyframes", 0)))
        record["keyframes"] = [
            {"path": frame.relative_to(layer.layer_dir).as_posix(), "bytes": frame.stat().st_size,
             "sha256": sha256_of(frame), "url": store.put(frame, _key(layer, state, run_root, frame))}
            for frame in frames
        ]
        if warning:
            record["warning"] = warning
            print(f"[adw {state.adw_id}] warning: {warning}", file=sys.stderr)
    return record


def _key(layer: Any, state: Any, run_root: Path, path: Path) -> str:
    return f"{layer.name}/{state.adw_id}/{path.relative_to(run_root).as_posix()}"


def skip_entries(state: Any) -> list[dict[str, Any]]:
    """One `skips` entry per check with a skip record: how many tests did not run and why. They go
    to the reviewers' manifest only; state.artefacts (PR links, the proof comment) is unchanged."""
    entries = []
    for name, record in (getattr(state, "checks", None) or {}).items():
        found = record.get("skips") if isinstance(record, dict) else None
        if not isinstance(found, dict):
            continue
        entries.append({"id": "skips", "type": "skips", "step": name, "path": record.get("log", ""),
                        "skipped": found.get("skipped", 0), "reasons": found.get("reasons", []),
                        "unexplained": found.get("unexplained", 0), "rule": list(record.get("skip_rule", []))})
    return entries


def left_out_entries(state: Any) -> list[dict[str, Any]]:
    """One `left-out` entry per artefact id whose limit (`#:max-files`) left matching files out:
    how many, of how many. Like the `skips` entries they go to the reviewers' manifest only."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in getattr(state, "artefacts", None) or []:
        artefact_id = str(record.get("id"))
        left = int(record.get("left_out") or 0) if isinstance(record, dict) else 0
        if left <= 0 or artefact_id in seen:
            continue
        seen.add(artefact_id)
        matched = int(record.get("matched") or 0)
        entries.append({"id": artefact_id, "type": "left-out", "step": record.get("step", ""), "path": "",
                        "left_out": left, "matched": matched, "limit": matched - left})
    return entries


def write_manifest(layer: Any, state: Any) -> Path:
    target = manifest_path(layer, state.adw_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    entries = state.artefacts + skip_entries(state) + left_out_entries(state)
    if getattr(state, "proof_plan", None) or getattr(state, "approvals", None):
        from . import convergence  # the evidence pack, the approvals and the round scope for the reviewers

        entries = entries + convergence.manifest_entries(layer, state)
    temporary.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def _clear(run_root: Path, artefact_id: str) -> None:
    """Remove the files of an earlier collection of `artefact_id` (keyframes included)."""
    directory = (run_root / artefact_id).resolve()
    if directory.parent != run_root.resolve():
        raise LayerError(f"artefact id {artefact_id!r} is not a plain name")
    if directory.is_dir() and not directory.is_symlink():
        shutil.rmtree(directory)


def collect(layer: Any, state: Any, artefact_ids: list[str], producing_step: str) -> list[dict[str, Any]]:
    """Copy each artefact option's matches into artefacts/<adw_id>/<id>/, store them, update the manifest.

    An image artefact hands over at most its `maxFiles` files, those this run changed first; when
    more matched, its records say so (`left_out`, `matched`) and the manifest gets a `left-out` entry.

    A collection REPLACES the previous one of the same id: its manifest entries and copied files
    (keyframes too) go, so a screenshot a later e2e run no longer produces is not shown to a
    reviewer again. Logs lose nothing by this: every attempt's log stays in agents/<id>/checks/
    and each log collection takes all of them. Remote copies of dropped files are left alone,
    just no longer referenced."""
    from . import proofplan  # the files a proof plan row writes; the run-scoped proof it declares (.al-proof/)

    worktree = Path(state.worktree).resolve() if state.worktree else None
    store = storage.get_storage(layer)
    run_root = storage.artefacts_root(layer) / state.adw_id
    collected: list[dict[str, Any]] = []
    replaced: set[str] = set()
    changed: set[str] | None = None      # what this run changed: asked of git once per collection
    wanted = proofplan.write_globs(state)
    for artefact_id in artefact_ids:
        option = layer.option("artefacts", artefact_id)
        kind = option["kind"]
        left_out = 0
        if kind == "log":
            matches = _log_matches(layer, state)
        elif kind in GLOB_KINDS:
            if worktree is None or not worktree.is_dir():
                raise StepFailed(producing_step, f"artefact {artefact_id!r} needs a worktree, none exists")
            if changed is None:
                changed = _changed_by_run(layer, state, worktree)
            matches, left_out = _glob_matches(worktree, option, changed, wanted)
        else:
            raise LayerError(f"Unknown artefact kind: {kind}")
        _clear(run_root, str(option["id"]))
        replaced.add(str(option["id"]))
        records = [_record(layer, state, store, option, producing_step, rel, src) for rel, src in matches]
        if left_out > 0:
            total = len(records) + left_out
            for record in records:
                record.update(left_out=left_out, matched=total)
            print(f"[adw {state.adw_id}] artefact {option['id']}: {len(records)} of {total} files handed over, "
                  f"{left_out} left out (limit {len(records)}); files the proof plan names and of this change first")
        collected.extend(records)
    state.artefacts[:] = [item for item in state.artefacts if item.get("id") not in replaced] + collected
    write_manifest(layer, state)
    # the run-scoped proof the plan declares (.al-proof/), refreshed with every collection
    if proofplan.proof_globs(state):
        collected += proofplan.collect_proof(layer, state, producing_step)
    return collected


def add_files(layer: Any, state: Any, artefact_id: str, kind: str, step: str,
              matches: list[tuple[str, Path]]) -> list[dict[str, Any]]:
    """Record arbitrary files as the artefacts of `artefact_id` (e.g. the kept work of a run, see
    keep.py): `matches` are (relative path under artefacts/<adw_id>/<id>/, source file). Like
    `collect`, it REPLACES an earlier collection of the same id, files and manifest entries."""
    store = storage.get_storage(layer)
    run_root = storage.artefacts_root(layer) / state.adw_id
    _clear(run_root, artefact_id)
    option = {"id": artefact_id, "kind": kind}
    collected = [_record(layer, state, store, option, step, relative, source) for relative, source in matches]
    state.artefacts[:] = [item for item in state.artefacts if item.get("id") != artefact_id] + collected
    write_manifest(layer, state)
    return collected
