"""The environment the application describes itself, made ready before a run's first step.

`al gen` writes env/source.json when a layer reuses the repository's own Containerfile, dev
container or flake dev shell (see `(environment podman #:containerfile …)`, `#:devcontainer …` and
`(environment nix-devshell #:flake …)`). This module reads it and, without changing envs.py:

- builds the repository's Containerfile (from the run's worktree, under a file lock) as
  `localhost/al-<layer>-base`, labelled `al.env-fingerprint` with a fingerprint of the repository's
  Containerfile, context and target on the default branch; when the label already matches the base
  is not rebuilt. Then `sandbox.ensure_image` builds the layer's thin env/Containerfile on top of it
  as `al-<layer>:<recipe hash>` and re-points `al-<layer>:latest`; the recipe hash includes
  `source_identity`, so a change to the repository's source gives a new image. envs.py then finds
  `:latest` and uses it unchanged;
- refreshes the wrapper flake's `app` input, so each run uses the default branch as it is then;
- merges the dev container's forwardPorts into the stack's podman option (envs.py publishes them
  and checks them against the worktree's port block like any declared port);
- names the prepare commands of a run: the worktree's #:prepare cmds, else the dev container's
  postCreateCommand.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from . import envs, events, slots
from .errors import LayerError, StepFailed

LOCK = Path("agents") / ".env-source.lock"
FLAKE_TIMEOUT = 600
TAIL_LINES = 20
STEP = "environment setup"


def load(layer: Any) -> dict[str, Any]:
    """env/source.json of the layer, {} when the layer reuses nothing of the repository."""
    path = Path(layer.layer_dir) / "env" / "source.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LayerError(f"env/source.json is not readable ({exc}); run `al gen` again") from exc
    return data if isinstance(data, dict) else {}


def _podman_source(layer: Any, stack: Any, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """The podman entry of source.json when it belongs to this stack's podman option, else {}."""
    entry = (source if source is not None else load(layer)).get("podman") or {}
    option = stack.podman_option or {}
    return entry if option and entry.get("option") == option.get("id") else {}


def apply_publish(layer: Any, stack: Any) -> None:
    """Add the dev container's forwarded ports after the option's own #:publish ports (in memory)."""
    entry = _podman_source(layer, stack)
    if not entry.get("publish"):
        return
    declared = list(stack.podman_option.get("publish") or [])
    extra = [port for port in entry["publish"] if port not in declared]
    if extra:
        stack.podman_option = {**stack.podman_option, "publish": [*declared, *extra]}


def _tail(text: str) -> str:
    return "\n".join(text.rstrip().splitlines()[-TAIL_LINES:])


def _call(argv: list[str], what: str, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed(STEP, f"{what}: {exc}") from exc
    if result.returncode:
        raise StepFailed(STEP, f"{what} (exit {result.returncode}):\n{_tail(result.stderr or result.stdout)}")
    return result


def _rev(layer: Any, path: str) -> str:
    result = subprocess.run(["git", "-C", str(layer.app_root), "rev-parse", f"{layer.default_branch}:{path}"],
                            capture_output=True, text=True, timeout=120, check=False)
    return result.stdout.strip() if result.returncode == 0 else f"missing:{path}"


def _source_part(layer: Any, entry: dict[str, Any]) -> str:
    """What the repository contributes to the image: for a build source, its Containerfile and
    context on the default branch and the target stage; otherwise the dev container's image."""
    build = entry.get("build") or {}
    if build:
        context = str(build.get("context") or ".")
        parts = (_rev(layer, str(build["containerfile"])), _rev(layer, context), str(build.get("target") or ""))
        return "\0".join(parts)
    return str(entry.get("image") or "")


def source_identity(layer: Any) -> str:
    """The repository's part of the layer's image, for sandbox.recipe_hash: "" when the layer
    reuses no image of the repository."""
    entry = load(layer).get("podman") or {}
    if not entry or not hasattr(layer, "app_root"):  # a recipe-only view (al deploy's committed env/) has no repository
        return ""
    return _source_part(layer, entry)


def fingerprint(layer: Any, entry: dict[str, Any]) -> str:
    """The base image's label: what the repository contributes to it (env/Containerfile on top is
    covered by the recipe hash)."""
    return hashlib.sha256(_source_part(layer, entry).encode()).hexdigest()[:32]


def base_image(layer: Any) -> str:
    return f"localhost/al-{layer.name}-base:latest"


def _ensure_image(layer: Any, state: Any, stack: Any, entry: dict[str, Any]) -> None:
    """The repository's base image (for a build source) is current, then the layer's recipe image
    on top of it (sandbox.ensure_image)."""
    from . import sandbox  # imported here: sandbox imports this module

    build = entry.get("build") or {}
    if build:
        base, fp = base_image(layer), fingerprint(layer, entry)
        try:
            inspect = subprocess.run(["podman", "image", "inspect", base, "--format",
                                      '{{ index .Labels "al.env-fingerprint" }}'],
                                     capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepFailed(STEP, f"podman image inspect failed: {exc}") from exc
        if not (inspect.returncode == 0 and inspect.stdout.strip() == fp):
            started = time.monotonic()
            worktree = str(state.worktree)
            argv = ["podman", "build", "-f", os.path.join(worktree, str(build["containerfile"]))]
            if build.get("target"):
                argv += ["--target", str(build["target"])]
            argv += ["--label", f"al.env-fingerprint={fp}", "-t", base,
                     os.path.join(worktree, str(build.get("context") or "."))]
            events.say(f"[adw {state.adw_id}] building the repository's {build['containerfile']} as {base}",
                       "env.image.build", image=base, phase="base")
            _call(argv, f"podman build of the repository's {build['containerfile']} failed",
                  envs.PODMAN_BUILD_TIMEOUT)
            seconds = round(time.monotonic() - started, 1)
            events.say(f"[adw {state.adw_id}] base image built: {base} ({seconds}s)", "env.image.build",
                       image=base, seconds=seconds, phase="base")
    sandbox.ensure_image(layer, say=lambda line: print(line, flush=True))


def _refresh_flake(layer: Any, state: Any) -> None:
    flake = Path(layer.layer_dir) / "env"
    _call(["nix", "flake", "lock", "--update-input", "app", f"path:{flake}"],
          "refreshing the application's flake (nix flake lock --update-input app) failed", FLAKE_TIMEOUT)
    events.say(f"[adw {state.adw_id}] the application's dev shell is the default branch as of now", "env.flake.refresh")


def ensure_ready(layer: Any, state: Any, stack: Any) -> None:
    """Build the image and refresh the flake this run reuses; nothing when the layer reuses neither.
    Runs after stack.setup() (the base image is built from the run's worktree)."""
    source = load(layer)
    if not source:
        return
    entry = _podman_source(layer, stack, source)
    image = bool(entry) and bool(stack.state.worktree)
    flake = bool(source.get("nix")) and bool((stack.nix_option or {}).get("flake"))
    if not (image or flake):
        return
    with slots.file_lock(Path(layer.layer_dir) / LOCK):
        if image:
            _ensure_image(layer, state, stack, entry)
        if flake:
            _refresh_flake(layer, state)


def prepare_commands(layer: Any, stack: Any) -> list[tuple[str, str]]:
    """(label, shell line) of each command the run prepares with, in order: the worktree option's
    #:prepare cmds, else the dev container's postCreateCommand, else none."""
    names = list((stack.worktree_option or {}).get("prepare") or [])
    if names:
        return [(str(name), str(layer.cmd(name)["run"])) for name in names]
    entry = _podman_source(layer, stack)
    if entry.get("prepare"):
        return [("postCreateCommand", str(entry["prepare"]))]
    return []
