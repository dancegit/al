from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import storage
from .errors import LayerError, StepFailed

GLOB_KINDS = {"junit", "coverage", "image", "video", "audio", "trace", "html", "file"}
FFMPEG_TIMEOUT = 120


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


def _glob_matches(worktree: Path, option: dict[str, Any]) -> list[tuple[str, Path]]:
    pattern = str(option.get("glob") or "")
    if not pattern:
        raise LayerError(f"artefact {option.get('id')!r} has an empty glob")
    matches = [
        (path.relative_to(worktree).as_posix(), path)
        for path in sorted(worktree.glob(pattern))
        if path.is_file() and path.resolve().is_relative_to(worktree)
    ]
    if option.get("kind") == "image":
        matches = matches[: max(0, int(option.get("maxFiles", 0)))]
    return matches


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


def write_manifest(layer: Any, state: Any) -> Path:
    target = manifest_path(layer, state.adw_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state.artefacts, indent=2) + "\n", encoding="utf-8")
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

    A collection REPLACES the previous one of the same id: its manifest entries and copied files
    (keyframes too) go, so a screenshot a later e2e run no longer produces is not shown to a
    reviewer again. Logs lose nothing by this: every attempt's log stays in agents/<id>/checks/
    and each log collection takes all of them. Remote copies of dropped files are left alone,
    just no longer referenced."""
    worktree = Path(state.worktree).resolve() if state.worktree else None
    store = storage.get_storage(layer)
    run_root = storage.artefacts_root(layer) / state.adw_id
    collected: list[dict[str, Any]] = []
    replaced: set[str] = set()
    for artefact_id in artefact_ids:
        option = layer.option("artefacts", artefact_id)
        kind = option["kind"]
        if kind == "log":
            matches = _log_matches(layer, state)
        elif kind in GLOB_KINDS:
            if worktree is None or not worktree.is_dir():
                raise StepFailed(producing_step, f"artefact {artefact_id!r} needs a worktree, none exists")
            matches = _glob_matches(worktree, option)
        else:
            raise LayerError(f"Unknown artefact kind: {kind}")
        _clear(run_root, str(option["id"]))
        replaced.add(str(option["id"]))
        collected.extend(_record(layer, state, store, option, producing_step, rel, src) for rel, src in matches)
    state.artefacts[:] = [item for item in state.artefacts if item.get("id") not in replaced] + collected
    write_manifest(layer, state)
    return collected
