"""The sandbox image of a toolchain: build it from env/flake.nix, pin nixpkgs in env/flake.lock.

A podman environment with `#:toolchain ID` runs in the layer's recipe image (`al-<layer>:<recipe hash>`,
see sandbox.py), made in two steps:

  1. the Nix-built image `localhost/al-<layer>-toolchain-<id>:latest` (flake output `image-<id>`, a
     dockerTools.streamLayeredImage), streamed into `podman load` and tagged;
  2. `podman build` of env/Containerfile on top of it (the #:setup lines as a final layer), labelled
     with `al.toolchain.key`: a sha256 over everything that decides the image.

`sandbox.build_image` calls `ensure` for a recipe option with a toolchain and passes the recipe ref
and the layer's own labels. `ensure` does nothing when the existing image carries the current key. Nix runs in one of two ways
(`choose_way`): on the host when `nix` is on PATH, else inside a builder container pinned by digest
(NIX_BUILDER) with a named store volume whose name contains that digest, so a new builder never runs
on an old store. The builder only ever sees the env directory, read-only except for `lock` and
`update`, and its store volume: never the worktree or a credential. AL_SANDBOX_BUILD=nix|container
forces a way.

Standard library only. Every command goes through `run` (default subprocess.run) and the stream into
`podman load` through `popen` (default subprocess.Popen), so tests inject both.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

NIX_BUILDER = "docker.io/nixos/nix@sha256:b49cb33f3a7e5cf3933ad8774529de0e3807ec5cf54987aa7ccdfb49f2fc55ed"  # 2.35.6
KEY_LABEL = "al.toolchain.key"
BUILD_TIMEOUT = 3600
LOCK_TIMEOUT = 600
INSPECT_TIMEOUT = 120
TAIL_LINES = 40
NIX_FLAGS = ["--extra-experimental-features", "nix-command flakes"]
WAYS = ("nix", "container")
KEY_FILES = ("flake.nix", "flake.lock", "toolchain-{id}.nix", "features.nix", "agent-cli.json")

Run = Callable[..., subprocess.CompletedProcess]


class BuildError(Exception):
    """Building, loading or locking failed; the message ends with the tail of the tool's stderr."""


def _tail(text: str) -> str:
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[-TAIL_LINES:])


def _failed(what: str, cp: subprocess.CompletedProcess) -> BuildError:
    tail = _tail(cp.stderr or cp.stdout or "")
    return BuildError(f"{what} failed (exit {cp.returncode})" + (f":\n{tail}" if tail else ""))


# --- identity -----------------------------------------------------------------------------------------

def builder_digest(builder: str = NIX_BUILDER) -> str:
    return builder.split("@sha256:", 1)[1]


def store_volume(builder: str = NIX_BUILDER) -> str:
    """The builder's Nix store: named after the builder's digest."""
    return f"al-nix-store-{builder_digest(builder)[:12]}"


def toolchain_image(layer_name: str, toolchain_id: str) -> str:
    return f"localhost/al-{layer_name}-toolchain-{toolchain_id}:latest".lower()


def key(env_dir: Path, toolchain_id: str, setup: list[str], builder: str = NIX_BUILDER) -> str:
    """sha256 over the flake, its lock, the toolchain, the kit files, the #:setup lines and the builder."""
    h = hashlib.sha256()
    for pattern in KEY_FILES:
        name = pattern.format(id=toolchain_id)
        path = Path(env_dir) / name
        h.update(name.encode() + b"\0")
        h.update(path.read_bytes() if path.is_file() else b"<missing>")
        h.update(b"\0")
    for line in setup:
        h.update(b"setup\0" + line.encode() + b"\0")
    h.update(b"builder\0" + builder_digest(builder).encode())
    return h.hexdigest()


# --- the two ways ---------------------------------------------------------------------------------------

def choose_way(environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    forced = environ.get("AL_SANDBOX_BUILD", "").strip()
    if forced:
        if forced not in WAYS:
            raise BuildError(f"AL_SANDBOX_BUILD={forced!r}: expected `nix` (on this host) or `container` (in the builder container)")
        return forced
    if shutil.which("nix"):
        return "nix"
    if shutil.which("podman"):
        return "container"
    raise BuildError("neither nix nor podman is on PATH: install Nix to build on this host, or podman to build in "
                     f"the builder container {NIX_BUILDER}")


def way_sentence(way: str, builder: str = NIX_BUILDER) -> str:
    if way == "nix":
        return "built with Nix on this host"
    return f"no Nix on this host: built in the builder container {builder}, store volume {store_volume(builder)}"


def flake_ref(way: str, env_dir: Path) -> str:
    return f"path:{Path(env_dir)}" if way == "nix" else "path:/src"


def container_argv(env_dir: Path, writable: bool = False, builder: str = NIX_BUILDER) -> list[str]:
    """podman run of the builder: its store volume and the env directory, nothing else."""
    return ["podman", "run", "--rm", "--security-opt=no-new-privileges",
            "-v", f"{store_volume(builder)}:/nix", "-v", f"{Path(env_dir)}:/src{'' if writable else ':ro'}",
            "-w", "/src", builder]


def nix_argv(way: str, env_dir: Path, args: list[str], writable: bool = False, builder: str = NIX_BUILDER) -> list[str]:
    if way == "nix":
        return ["nix", *NIX_FLAGS, *args]
    return [*container_argv(env_dir, writable, builder), "nix", *NIX_FLAGS,
            "--option", "sandbox", "false", "--option", "build-users-group", "", *args]


def _run(run: Run, argv: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise BuildError(f"{argv[0]} is not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"`{' '.join(argv[:3])} …` took longer than {timeout} s") from exc


def build_outputs(env_dir: Path, outputs: list[str], way: str, run: Run = subprocess.run) -> list[str]:
    """Build flake outputs; their store paths, in order."""
    ref = flake_ref(way, env_dir)
    argv = nix_argv(way, env_dir, ["build", "--no-link", "--print-out-paths", "--no-write-lock-file",
                                   *(f"{ref}#{o}" for o in outputs)])
    cp = _run(run, argv, BUILD_TIMEOUT)
    if cp.returncode != 0:
        raise _failed(f"nix build {' '.join('#' + o for o in outputs)}", cp)
    return [line.strip() for line in (cp.stdout or "").splitlines() if line.strip()]


# --- the lock -------------------------------------------------------------------------------------------

def read_lock(env_dir: Path) -> dict | None:
    path = Path(env_dir) / "flake.lock"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def lock(env_dir: Path, way: str | None = None, run: Run = subprocess.run) -> str:
    """Write env/flake.lock when there is none: "written", or "kept" when it exists."""
    if (Path(env_dir) / "flake.lock").is_file():
        return "kept"
    way = way or choose_way()
    cp = _run(run, nix_argv(way, env_dir, ["flake", "lock", flake_ref(way, env_dir)], writable=True), LOCK_TIMEOUT)
    if cp.returncode != 0:
        raise _failed("nix flake lock", cp)
    return "written"


def update(env_dir: Path, way: str | None = None, run: Run = subprocess.run) -> list[dict]:
    """Re-pin every input of env/flake.nix; the per-input change (see lock_diff)."""
    way = way or choose_way()
    old = read_lock(env_dir)
    cp = _run(run, nix_argv(way, env_dir, ["flake", "update", "--flake", flake_ref(way, env_dir)], writable=True),
              LOCK_TIMEOUT)
    if cp.returncode != 0:
        raise _failed("nix flake update", cp)
    new = read_lock(env_dir)
    if new is None:
        raise BuildError(f"nix flake update wrote no readable {Path(env_dir) / 'flake.lock'}")
    return lock_diff(old, new)


def _locked_inputs(data: dict | None) -> dict[str, dict]:
    if not data:
        return {}
    nodes = data.get("nodes") or {}
    root = nodes.get(data.get("root", "root")) or {}
    found = {}
    for name, node_name in sorted((root.get("inputs") or {}).items()):
        node = nodes.get(node_name if isinstance(node_name, str) else name) or {}
        found[name] = node.get("locked") or {}
    return found


def _date(locked: dict) -> str | None:
    stamp = locked.get("lastModified")
    return datetime.fromtimestamp(int(stamp), timezone.utc).strftime("%Y-%m-%d") if stamp else None


def _rev(locked: dict) -> str | None:
    rev = locked.get("rev") or locked.get("narHash")
    return str(rev)[:7] if rev else None


def lock_diff(old: dict | None, new: dict) -> list[dict]:
    """Per input of `new`: revision (7 characters) and date before and after."""
    before, after = _locked_inputs(old), _locked_inputs(new)
    rows = []
    for name, locked in after.items():
        prev = before.get(name)
        row = {"input": name, "old": _rev(prev) if prev else None, "new": _rev(locked),
               "oldDate": _date(prev) if prev else None, "newDate": _date(locked)}
        row["changed"] = (row["old"], row["oldDate"]) != (row["new"], row["newDate"])
        rows.append(row)
    return rows


def describe_lock_diff(rows: list[dict]) -> list[str]:
    lines = []
    for r in rows:
        new = f"{r['new']} ({r['newDate']})"
        if not r["changed"]:
            lines.append(f"{r['input']}: {new}, unchanged")
        elif r["old"] is None:
            lines.append(f"{r['input']}: (not locked before) → {new}")
        else:
            lines.append(f"{r['input']}: {r['old']} ({r['oldDate']}) → {new}")
    return lines


# --- sizes ----------------------------------------------------------------------------------------------

def parse_path_info(text: str) -> dict[str, int]:
    """`nix path-info -S --json`: path → closure size (an object keyed by path, or a list of objects)."""
    data = json.loads(text)
    items = data.items() if isinstance(data, dict) else ((d.get("path"), d) for d in data)
    return {path: int(info.get("closureSize") or 0) for path, info in items if path and info}


def feature_sizes(env_dir: Path, toolchain_id: str, features: list[str], way: str,
                  run: Run = subprocess.run) -> dict[str, int]:
    """Closure size of the base (declared packages and what every image needs) and of each feature."""
    names = ["base", *features]
    paths = build_outputs(env_dir, [f"toolchain-{toolchain_id}-{n}" for n in names], way, run)
    if len(paths) != len(names):
        raise BuildError(f"nix build printed {len(paths)} paths for {len(names)} outputs")
    cp = _run(run, nix_argv(way, env_dir, ["path-info", "-S", "--json", *paths]), INSPECT_TIMEOUT)
    if cp.returncode != 0:
        raise _failed("nix path-info", cp)
    try:
        by_path = parse_path_info(cp.stdout)
    except (ValueError, AttributeError, TypeError) as exc:
        raise BuildError(f"nix path-info printed no JSON: {exc}") from exc
    return {n: by_path.get(p, 0) for n, p in zip(names, paths)}


def image_size(image: str, run: Run = subprocess.run) -> int | None:
    cp = _run(run, ["podman", "image", "inspect", "--format", "{{.Size}}", image], INSPECT_TIMEOUT)
    try:
        return int((cp.stdout or "").strip()) if cp.returncode == 0 else None
    except ValueError:
        return None


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


# --- the image ------------------------------------------------------------------------------------------

def image_key(image: str, run: Run = subprocess.run) -> str | None:
    """The al.toolchain.key label of an existing image; None when there is none (or no image)."""
    cp = _run(run, ["podman", "image", "inspect", "--format", "{{ index .Config.Labels \"" + KEY_LABEL + "\" }}", image],
              INSPECT_TIMEOUT)
    value = (cp.stdout or "").strip() if cp.returncode == 0 else ""
    return value if value and value != "<no value>" else None


def _stream_into_load(stream: list[str], popen: Callable[..., Any]) -> str:
    """Pipe the image stream script into `podman load`; the name it loaded."""
    try:
        producer = popen(stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        loader = popen(["podman", "load"], stdin=producer.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise BuildError(f"cannot stream the image into podman load: {exc}") from exc
    if producer.stdout is not None:
        producer.stdout.close()   # podman load owns the pipe now
    try:
        out, err = loader.communicate(timeout=BUILD_TIMEOUT)
        _p_out, p_err = producer.communicate(timeout=60)
    except subprocess.TimeoutExpired as exc:
        loader.kill()
        producer.kill()
        raise BuildError(f"streaming the image into podman load took longer than {BUILD_TIMEOUT} s") from exc
    if producer.returncode:
        raise BuildError("the image stream failed" + (f":\n{_tail(p_err if isinstance(p_err, str) else (p_err or b'').decode(errors='replace'))}" if p_err else ""))
    if loader.returncode:
        raise BuildError("podman load failed" + (f":\n{_tail(err)}" if err else ""))
    for line in (out or "").splitlines():
        if line.startswith("Loaded image"):
            return line.split(":", 1)[1].strip().split(",")[0].strip()
    return ""


def report_path(layer_dir: Path, toolchain_id: str) -> Path:
    return Path(layer_dir) / "agents" / "sandbox" / f"{toolchain_id}.json"


def ensure(layer_dir: Path, layer_name: str, option: dict, image: str, say: Callable[[str], None] = print,
           run: Run = subprocess.run, popen: Callable[..., Any] = subprocess.Popen, way: str | None = None,
           labels: list[str] | None = None) -> dict:
    """Make `image` (the layer's recipe image, al-<layer>:<recipe hash>) from the toolchain of the
    podman `option`, unless the existing image already carries the current key. `labels` are extra
    `name=value` labels of the image, after al.toolchain.key. Returns the report that is also
    written to agents/sandbox/<toolchain>.json."""
    layer_dir = Path(layer_dir)
    env_dir = layer_dir / "env"
    tid = option["toolchain"]
    setup = list(option.get("setup") or [])
    current = key(env_dir, tid, setup)
    base = toolchain_image(layer_name, tid)
    report: dict[str, Any] = {"toolchain": tid, "image": image, "base": base, "key": current}
    if image_key(image, run) == current:
        report["status"] = "up-to-date"
        return report
    way = way or choose_way()
    say(f"sandbox: building toolchain `{tid}` ({way_sentence(way)})")
    (stream,) = build_outputs(env_dir, [f"image-{tid}"], way, run) or [""]
    if not stream:
        raise BuildError(f"nix build #image-{tid} printed no store path")
    loaded = _stream_into_load([stream] if way == "nix" else [*container_argv(env_dir), stream], popen)
    cp = _run(run, ["podman", "tag", loaded or base, base], INSPECT_TIMEOUT)
    if cp.returncode != 0:
        raise _failed(f"podman tag {loaded or base} {base}", cp)
    extra = [word for label in labels or [] for word in ("--label", label)]
    cp = _run(run, ["podman", "build", "--label", f"{KEY_LABEL}={current}", *extra, "-t", image,
                    "-f", "env/Containerfile", "env/"], BUILD_TIMEOUT, cwd=layer_dir)
    if cp.returncode != 0:
        raise _failed(f"podman build of {image} (env/Containerfile: the #:setup lines)", cp)
    try:
        sizes = feature_sizes(env_dir, tid, list(option.get("features") or _features(env_dir, tid)), way, run)
    except BuildError as exc:
        sizes, report["sizeNote"] = {}, str(exc).splitlines()[0]
    report.update(status="built", way=way, sizes=sizes, imageSize=image_size(image, run),
                  at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if way == "container":
        report.update(builder=NIX_BUILDER, storeVolume=store_volume())
    path = report_path(layer_dir, tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1) + "\n")
    return report


def _features(env_dir: Path, toolchain_id: str) -> list[str]:
    """The features a generated toolchain-<id>.nix names (its `names = [ "…" … ];` line)."""
    path = Path(env_dir) / f"toolchain-{toolchain_id}.nix"
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("names = ["):
                return [w.strip('"') for w in line[len("names = ["):].rstrip("];").split() if w.startswith('"')]
    except OSError:
        pass
    return []
