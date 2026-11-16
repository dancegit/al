"""The layer's podman sandbox: its image follows its recipe, and it is checked before work starts.

Recipe identity. The image of a layer is `al-<layer>:<hash>`, where the hash covers the generated
`env/Containerfile`, every file it COPY/ADDs from `env/` (content and executable bit) and the
podman option's `baseImage`. A changed recipe is therefore always built and an unchanged one never
is. `al-<layer>:latest` is re-pointed at the current hash, so a runtime that still asks for
`:latest` gets it too. Old hashes stay while a run or a container uses them and are pruned by
count and age (KEEP_OLD, MAX_AGE_DAYS).

Doctor and preflight. One generated POSIX sh script runs in a container started exactly as a run
starts it (`sh -c` in the run's container, never bash), prints
`AL-DOCTOR<TAB>check<TAB>ok|miss|skip<TAB>detail` lines, and each miss carries the declaration to
change. `preflight` runs the fast checks before a run's first step and fails the run at once with
that table.

Used by the runner (preflight), by `al sandbox` and by `al verify` (through the layer's own
runtime), so they share one implementation.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import envs, events, gitsafe, slots
from .errors import LayerError, StepFailed

LEGACY_TAG = "latest"
KEEP_OLD = 2            # old images kept by the automatic prune (besides any in use)
MAX_AGE_DAYS = 14       # old images older than this are removed by the automatic prune
MIN_FREE_MB = 1024      # the disk check misses below this much free space
BUILD_TAIL = 40         # lines of build output kept for the error message
DOCTOR_TIMEOUT = 180
DEFAULT_PREFLIGHT = ("services", "browser")
INIT_NAMES = ("catatonit", "tini", "dumb-init", "podman-init", "docker-init")
BROWSER_ARTEFACTS = {"image", "video", "trace"}
AGENT_BINARIES = {"claude-code": ("claude", "claude"), "codex": ("codex", "codex")}  # kind -> (binary, credential)
NOOP_BUILTINS = {"cd", "export", "set", "source", ".", "true", ":", "unset", "umask", "shopt", "alias"}
WRAPPERS = {"env", "exec", "command", "nohup", "time", "builtin"}
MARK = "AL-DOCTOR"


# ------------------------------------------------------------------ recipe identity

def recipe_option(layer: Any) -> dict[str, Any] | None:
    """The podman option the generator writes env/Containerfile from: the first one declared."""
    return next((entry for entry in layer.ir.get("environments") or []
                 if isinstance(entry, dict) and entry.get("kind") == "podman"), None)


def podman_options(layer: Any) -> list[dict[str, Any]]:
    return [entry for entry in layer.ir.get("environments") or []
            if isinstance(entry, dict) and entry.get("kind") == "podman"]


def podman_option(layer: Any, env_id: str | None = None) -> dict[str, Any]:
    """The podman option `env_id`, or the recipe option; LayerError naming the podman ids."""
    options = podman_options(layer)
    if not options:
        raise LayerError(f"layer {layer.name} declares no (environment podman …)")
    if not env_id:
        return options[0]
    for option in options:
        if option.get("id") == env_id:
            return option
    raise LayerError(f"no podman environment `{env_id}`; podman environments: "
                     + ", ".join(str(option.get("id")) for option in options))


def _containerfile(layer: Any) -> Path:
    path = Path(layer.layer_dir) / "env" / "Containerfile"
    if not path.is_file():
        raise LayerError(f"{path} not found: run `al gen {layer.layer_dir}` (the sandbox image is built from it)")
    return path


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        lines.append(current + stripped)
        current = ""
    if current.strip():
        lines.append(current)
    return lines


def copied_sources(containerfile_text: str) -> list[str]:
    """Sources of COPY/ADD lines, as written (relative to the build context `env/`). Flags such as
    --chown/--chmod are skipped; a line with --from= (another stage or image) or a URL source is
    ignored; the last argument is the destination; the JSON-array form is understood."""
    sources: list[str] = []
    for line in _logical_lines(containerfile_text):
        parts = line.split(None, 1)
        if len(parts) < 2 or parts[0].upper() not in ("COPY", "ADD"):
            continue
        rest = parts[1].strip()
        words: list[str] = []
        flags: list[str] = []
        while rest.startswith("--"):
            flag, _, rest = rest.partition(" ")
            flags.append(flag)
            rest = rest.strip()
        if any(flag.startswith("--from") for flag in flags):
            continue
        if rest.startswith("["):
            try:
                value = json.loads(rest)
                words = [str(item) for item in value] if isinstance(value, list) else []
            except ValueError:
                words = []
        else:
            try:
                words = shlex.split(rest)
            except ValueError:
                words = rest.split()
        if len(words) < 2:
            continue
        for source in words[:-1]:
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", source) or source.startswith("git@"):
                continue
            sources.append(source)
    return sources


def _hash_entries(context: Path, source: str) -> list[bytes]:
    root = context.resolve()
    target = (root / source.lstrip("/")).resolve() if not any(c in source for c in "*?[") else None
    if target is None:  # a glob: every match, sorted
        matches = sorted(root.glob(source.lstrip("/")))
        out: list[bytes] = []
        for match in matches:
            out += _hash_entries(context, match.relative_to(root).as_posix())
        return out or [b"missing\0" + source.encode()]
    if target != root and root not in target.parents:
        raise LayerError(f"env/Containerfile copies {source!r}, which is outside env/: refusing to build from it")
    if not target.exists():
        return [b"missing\0" + source.encode()]
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    out = []
    for path in files:
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise LayerError(f"env/{path.relative_to(root)} points outside env/: refusing to build from it")
        relative = path.relative_to(root).as_posix()
        executable = b"1" if os.access(path, os.X_OK) else b"0"
        out.append(b"file\0" + relative.encode() + b"\0" + executable + b"\0" + path.read_bytes())
    return out


def recipe_hash(layer: Any) -> str:
    """12 hex digits over the Containerfile, the files it copies from env/ and the base image."""
    path = _containerfile(layer)
    text = path.read_bytes()
    digest = hashlib.sha256(b"containerfile\0" + text)
    for source in copied_sources(text.decode("utf-8", errors="replace")):
        for entry in _hash_entries(path.parent, source):
            digest.update(entry)
    option = recipe_option(layer) or {}
    digest.update(b"base\0" + str(option.get("baseImage") or "").encode())
    return digest.hexdigest()[:12]


def image_name(layer: Any) -> str:
    return f"al-{layer.name}"


def image_ref(layer: Any) -> str:
    return f"{image_name(layer)}:{recipe_hash(layer)}"


# ------------------------------------------------------------------ podman helpers

def _podman(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _podman_json(args: list[str]) -> list[dict[str, Any]]:
    result = _podman(args)
    if result is None or result.returncode:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [item for item in data or [] if isinstance(item, dict)]


def _short(ref: str) -> str:
    return ref[len("localhost/"):] if ref.startswith("localhost/") else ref


def image_exists(ref: str) -> bool:
    result = _podman(["image", "exists", ref])
    return result is not None and result.returncode == 0


def image_info(ref: str) -> dict[str, Any]:
    """{Id, Size, Created} of an image ({} when it does not exist)."""
    found = _podman_json(["image", "inspect", ref])
    if not found:
        return {}
    item = found[0]
    return {"Id": str(item.get("Id") or ""), "Size": int(item.get("Size") or 0), "Created": item.get("Created")}


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
        text = re.sub(r" ([+-]\d{4}) \w+$", r"\1", text)
        for parse in (datetime.fromisoformat, lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f%z")):
            try:
                stamp = parse(text)
                return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
            except ValueError:
                continue
    return None


def human_size(size: Any) -> str:
    if not isinstance(size, (int, float)) or size <= 0:
        return "-"
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "kB", "MB") else f"{size:.1f} {unit}"
        size /= 1000
    return "-"


def human_age(created: float | None, now: float | None = None) -> str:
    if created is None:
        return "-"
    seconds = max(0.0, (now or time.time()) - created)
    for unit, length in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= length:
            return f"{int(seconds // length)}{unit}"
    return f"{int(seconds)}s"


# ------------------------------------------------------------------ build / ensure

def _labels(layer: Any, recipe: str) -> list[str]:
    return ["--label", f"al.layer={layer.name}", "--label", f"al.layer-dir={Path(layer.layer_dir).resolve()}",
            "--label", f"al.recipe={recipe}"]


def _tag_latest(layer: Any, ref: str) -> None:
    latest = f"{image_name(layer)}:{LEGACY_TAG}"
    current = image_info(ref).get("Id")
    if current and image_info(latest).get("Id") == current:
        return
    _podman(["tag", ref, latest])


def build_image(layer: Any, *, no_cache: bool = False, say: Callable[[str], Any] = print) -> dict[str, Any]:
    """Build the current recipe with live output (each line to `say`), labelled with its layer,
    layer directory and recipe; tag :latest to it and prune old images. StepFailed on failure."""
    recipe = recipe_hash(layer)
    ref = f"{image_name(layer)}:{recipe}"
    argv = ["podman", "build", "-t", ref, *_labels(layer, recipe), *(["--no-cache"] if no_cache else []),
            "-f", "env/Containerfile", "env/"]
    events.say(f"[sandbox] building {ref} (recipe {recipe})", "env.build.start", image=ref, recipe=recipe)
    started = time.monotonic()
    tail: list[str] = []
    code: int | None = None
    error = ""
    try:
        process = subprocess.Popen(argv, cwd=layer.layer_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace")
    except OSError as exc:
        error = f"could not start podman: {exc}"
    else:
        timer = threading.Timer(envs.PODMAN_BUILD_TIMEOUT, process.kill)
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout or []:
                line = line.rstrip("\n")
                say(line)
                tail = (tail + [line])[-BUILD_TAIL:]
            code = process.wait()
        finally:
            expired = not timer.is_alive() and code not in (0, None)
            timer.cancel()
        if expired:
            error = f"timed out after {envs.PODMAN_BUILD_TIMEOUT}s"
        elif code:
            error = f"exit {code}"
    seconds = round(time.monotonic() - started, 3)
    size = image_info(ref).get("Size") if not error else None
    events.emit("env.build.end", image=ref, recipe=recipe, status="failed" if error else "passed",
                seconds=seconds, size=size, error=error or None)
    if error:
        raise StepFailed("environment setup", f"podman build of {ref} failed ({error}); last lines:\n"
                         + "\n".join(tail[-BUILD_TAIL:]))
    _tag_latest(layer, ref)
    prune_images(layer, keep_old=KEEP_OLD, max_age_days=MAX_AGE_DAYS)
    return {"image": ref, "recipe": recipe, "seconds": seconds, "size": size, "built": True}


def ensure_image(layer: Any, *, say: Callable[[str], Any] = print) -> dict[str, Any]:
    """The image of the current recipe exists afterwards (built at most once, also when several
    runs start together), and :latest points at it."""
    recipe = recipe_hash(layer)
    ref = f"{image_name(layer)}:{recipe}"
    if image_exists(ref):
        _tag_latest(layer, ref)
        return {"image": ref, "recipe": recipe, "built": False}
    with slots.file_lock(Path(layer.layer_dir) / "agents" / f".image-{recipe}.lock"):
        if image_exists(ref):
            _tag_latest(layer, ref)
            return {"image": ref, "recipe": recipe, "built": False}
        return build_image(layer, say=say)


# ------------------------------------------------------------------ status / prune

def _leases(layer_dir: Path) -> dict[str, Any]:
    table = layer_dir / "agents" / ".ports.json"
    if not table.exists():
        return {}
    with slots.file_lock(table.with_suffix(".lock")):
        try:
            return json.loads(table.read_text() or "{}")
        except (OSError, ValueError):
            return {}


def in_flight_images(layer: Any) -> set[str]:
    """Image ids and refs used by a container of this layer directory, or recorded by a run that is alive."""
    layer_dir = Path(layer.layer_dir).resolve()
    used: set[str] = set()
    for container in _podman_json(["ps", "-a", "--format", "json", "--filter", f"label=al.layer-dir={layer_dir}"]):
        for key in ("Image", "ImageID"):
            if container.get(key):
                used.add(_short(str(container[key])))
    leases = _leases(layer_dir)
    for state_file in sorted((layer_dir / "agents").glob("*/state.json")):
        try:
            image = json.loads(state_file.read_text()).get("image")
        except (OSError, ValueError, AttributeError):
            continue
        if image and envs._run_alive(layer_dir, state_file.parent.name, leases):
            used.add(_short(str(image)))
    return used


def layer_images(layer: Any) -> list[dict[str, Any]]:
    """Images built for this layer directory, newest first: {ref, refs, recipe, id, size, created}."""
    layer_dir = Path(layer.layer_dir).resolve()
    rows = []
    for item in _podman_json(["images", "--filter", f"label=al.layer-dir={layer_dir}", "--format", "json"]):
        labels = item.get("Labels") or {}
        recipe = str(labels.get("al.recipe") or "")
        names = [_short(str(name)) for name in item.get("Names") or []]
        ref = next((name for name in names if name.endswith(f":{recipe}")), names[0] if names else "")
        rows.append({"ref": ref, "refs": names, "recipe": recipe, "id": str(item.get("Id") or ""),
                     "size": item.get("Size"), "created": _epoch(item.get("Created") or item.get("CreatedAt"))})
    rows.sort(key=lambda row: row["created"] or 0, reverse=True)
    return rows


def _used(row: dict[str, Any], used: set[str]) -> bool:
    if any(ref in used for ref in [row["ref"], *row["refs"]]):
        return True
    ident = row["id"]
    return bool(ident) and any(len(item) >= 12 and (ident.startswith(item) or item.startswith(ident))
                               for item in used)


def prune_images(layer: Any, *, keep_old: int | None = None, max_age_days: float | None = None,
                 now: float | None = None) -> list[str]:
    """Remove (untag, never -f) old images of this layer that nothing uses. With keep_old /
    max_age_days (the automatic prune after a build) only those beyond the keep_old newest unused
    old images or older than max_age_days go; without (`al sandbox prune`) every unused old one."""
    try:
        current = recipe_hash(layer)
    except LayerError:
        return []
    used = in_flight_images(layer)
    candidates = [row for row in layer_images(layer) if row["recipe"] != current and not _used(row, used)]
    automatic = keep_old is not None or max_age_days is not None
    moment = now if now is not None else time.time()
    removed: list[str] = []
    for index, row in enumerate(candidates):
        if automatic:
            too_many = keep_old is not None and index >= keep_old
            too_old = (max_age_days is not None and row["created"] is not None
                       and moment - row["created"] > max_age_days * 86400)
            if not (too_many or too_old):
                continue
        refs = [ref for ref in row["refs"] if ref] or [row["ref"]]
        results = [_podman(["rmi", ref]) for ref in refs]
        if all(result is not None and result.returncode == 0 for result in results):
            removed.append(row["ref"])
    return removed


def image_status(layer: Any) -> list[dict[str, Any]]:
    """One row per podman option (all share the recipe's image), then the old images."""
    recipe_from = str((recipe_option(layer) or {}).get("id") or "")
    recipe = recipe_hash(layer)
    ref = f"{image_name(layer)}:{recipe}"
    info = image_info(ref)
    created = _epoch(info.get("Created"))
    rows = [{"env": str(option.get("id")), "recipe": recipe, "image": ref, "built": bool(info),
             "size": info.get("Size"), "age": human_age(created) if info else "-", "recipe_from": recipe_from,
             "old": False, "in_use": False} for option in podman_options(layer)]
    used = in_flight_images(layer)
    for row in layer_images(layer):
        if row["recipe"] == recipe:
            continue
        rows.append({"env": "", "recipe": row["recipe"], "image": row["ref"], "built": True, "size": row["size"],
                     "age": human_age(row["created"]), "recipe_from": recipe_from, "old": True,
                     "in_use": _used(row, used)})
    return rows


# ------------------------------------------------------------------ throwaway stack and shell

class ThrowawayState:
    """Stand-in for the runtime's ADWState: what envs.EnvStack reads and writes."""

    def __init__(self, adw_id: str, adw: str, worktree: Path, purpose: str) -> None:
        self.adw_id, self.adw = adw_id, adw
        self.klass, self.prompt, self.source = "chore", f"al sandbox {purpose}", purpose
        self.worktree, self.branch = str(worktree), ""
        self.ports: list[int] = []
        self.dry_run, self.status = True, "running"
        self.image = ""


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=120, check=False)


@contextlib.contextmanager
def throwaway(layer: Any, env_ids: list[str], purpose: str,
              report: Callable[[str], Any] = print) -> Iterator[envs.EnvStack]:
    """An EnvStack set up exactly as a run's, on a detached worktree of the default branch under
    the stack's worktree dir, named by an 8-hex id (a crashed doctor's container is collected by
    collect_stale_containers like a crashed run's). Always torn down; the worktree is removed."""
    adw_id = secrets.token_hex(4)
    worktree_option = next((layer.option("environments", env_id) for env_id in env_ids
                            if layer.option("environments", env_id).get("kind") == "worktree"), None)
    if worktree_option is None:
        raise LayerError(f"the environment stack ({' → '.join(env_ids)}) has no worktree environment; "
                         "a podman sandbox needs one")
    app = Path(layer.app_root).resolve()
    tree_dir = Path(layer.layer_dir) / str(worktree_option.get("dir") or "trees")
    worktree = (tree_dir / adw_id).resolve()
    inside_ignored = bool(layer.in_repo_dir) and Path(layer.layer_dir).resolve() in worktree.parents
    if (worktree == app or app in worktree.parents) and not inside_ignored:
        raise LayerError(f"refusing to create {worktree}: it is inside the application tree")
    created = [path for path in (Path(layer.layer_dir) / "agents" / adw_id, Path(layer.layer_dir) / "artefacts" / adw_id)
               if not path.exists()]
    tree_dir.mkdir(parents=True, exist_ok=True)
    added = _git(["-C", str(app), *gitsafe.HARDENING, "-c", "worktree.useRelativePaths=false", "worktree", "add",
                  "--detach", str(worktree), layer.default_branch])
    if added.returncode:
        raise LayerError(f"cannot create the {purpose} worktree: {added.stderr.strip()}")
    state = ThrowawayState(adw_id, purpose, worktree, purpose)
    stack: envs.EnvStack | None = None
    try:
        stack = envs.build(layer, state, env_ids)
        stack.setup()
        stack.cwd = worktree
        yield stack
    finally:
        problems: list[str] = []
        if stack is not None:
            try:
                stack.teardown(True)
            except Exception as exc:  # noqa: BLE001 — reported, and the cleanup below still runs
                problems.append(f"teardown failed: {exc}")
        if worktree.exists():
            _git(["-C", str(app), "worktree", "remove", "--force", str(worktree)])
            shutil.rmtree(worktree, ignore_errors=True)
        _git(["-C", str(app), "worktree", "prune"])
        with contextlib.suppress(Exception):
            envs.release_ports(layer.layer_dir, adw_id)
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
        for problem in problems:
            report(f"[sandbox] {purpose}: {problem}")


def shell_argv(stack: envs.EnvStack) -> list[str]:
    """argv of an interactive login shell in the stack's container (bash when the image has it)."""
    argv = stack._podman_argv(["sh", "-c", "command -v bash >/dev/null && exec bash -l || exec sh -l"],
                              interactive=True)
    index = argv.index("-i")
    return [*argv[:index + 1], "-t", *argv[index + 1:]]


# ------------------------------------------------------------------ what a workflow needs

def _segments(run: str) -> list[list[str]]:
    lexer = shlex.shlex(run, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= set("&|;()<>"):
            if token not in ("<", ">", ">>", "<<", "&>", "2>", ">&", "<&") and segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment]


def first_executable(run: str) -> str | None:
    """The first program a shell line runs (`FOO=1 uv run x` → uv, `cd web && npm test` → npm),
    or None for a line that cannot be parsed or runs only builtins."""
    try:
        segments = _segments(str(run))
    except ValueError:
        return None
    for words in segments:
        index = 0
        while index < len(words):
            word = words[index]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
                index += 1
            elif word in WRAPPERS:
                index += 1
                while index < len(words) and words[index].startswith("-"):
                    index += 1
            else:
                break
        if index >= len(words):
            continue
        if words[index] in NOOP_BUILTINS:
            continue
        return words[index]
    return None


def workflow_commands(layer: Any, adw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The codebase cmds a workflow runs: its check and script steps' cmds and, when it has a ship
    step (whose gate re-runs them), its review option's requireChecks; in declaration order, each once."""
    if not adw:
        return []
    names: list[str] = []
    for step in adw.get("steps") or []:
        if step.get("kind") in ("check", "script") and step.get("cmd"):
            names.append(str(step["cmd"]))
    review_id = adw.get("review")
    if review_id and any(step.get("kind") == "ship" for step in adw.get("steps") or []):
        with contextlib.suppress(LayerError):
            names += [str(name) for name in layer.option("reviews", review_id).get("requireChecks") or []]
    out, seen = [], set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        with contextlib.suppress(LayerError):
            out.append(layer.cmd(name))
    return out


def agent_requirement(layer: Any, adw: dict[str, Any] | None, stack: Any = None) -> tuple[str, str] | None:
    """(binary, agent kind) the workflow's agent needs inside the container; None for the mock
    agent and when the agent runs on the host (see host_agent_requirement)."""
    found = _agent(layer, adw)
    if found is None:
        return None
    option = (stack.podman_option if stack is not None else None) or _stack_podman(layer, adw) or {}
    return found if option.get("agentInside") else None


def host_agent_requirement(layer: Any, adw: dict[str, Any] | None, stack: Any = None) -> tuple[str, str] | None:
    found = _agent(layer, adw)
    if found is None:
        return None
    option = (stack.podman_option if stack is not None else None) or _stack_podman(layer, adw) or {}
    return None if option.get("agentInside") else found


def _agent(layer: Any, adw: dict[str, Any] | None) -> tuple[str, str] | None:
    if not adw or not adw.get("agent"):
        return None
    try:
        agent = layer.option("agents", adw["agent"])
    except LayerError:
        return None
    kind = str(agent.get("kind") or "")
    if kind not in AGENT_BINARIES:
        return None
    return AGENT_BINARIES[kind][0], kind


def _agent_kind(layer: Any, adw: dict[str, Any] | None) -> str:
    try:
        return str(layer.option("agents", (adw or {}).get("agent") or "").get("kind") or "")
    except LayerError:
        return ""


def _stack_podman(layer: Any, adw: dict[str, Any] | None) -> dict[str, Any] | None:
    for env_id in (adw or {}).get("environment") or []:
        with contextlib.suppress(LayerError):
            option = layer.option("environments", env_id)
            if option.get("kind") == "podman":
                return option
    return None


def needs_browser(layer: Any, adw: dict[str, Any] | None) -> bool:
    """The workflow's review option or steps collect an artefact of kind image, video or trace."""
    if not adw:
        return False
    kinds = {str(entry.get("id")): str(entry.get("kind")) for entry in layer.ir.get("artefacts") or []
             if isinstance(entry, dict)}
    wanted: list[str] = []
    for step in adw.get("steps") or []:
        wanted += [str(item) for item in step.get("collect") or []]
    with contextlib.suppress(LayerError):
        if adw.get("review"):
            wanted += [str(item) for item in layer.option("reviews", adw["review"]).get("artefacts") or []]
    return any(kinds.get(item) in BROWSER_ARTEFACTS for item in wanted)


# ------------------------------------------------------------------ doctor

@dataclass
class Check:
    name: str
    status: str          # ok | miss | skip
    detail: str = ""
    fix: str = ""


def _decl(option: dict[str, Any]) -> str:
    return f"(environment podman #:id {option.get('id')}"


def doctor_script(stack: Any, cmds: list[dict[str, Any]], *, agent: tuple[str, str] | None,
                  browser: bool, services: bool = True) -> str:
    """One POSIX sh script that prints an AL-DOCTOR line per check. It names variables, never
    prints their values."""
    option = stack.podman_option or {}
    worktree = str(getattr(stack.state, "worktree", "") or stack.cwd)
    q = shlex.quote
    lines = ["#!/bin/sh", f"r() {{ printf '{MARK}\\t%s\\t%s\\t%s\\n' \"$1\" \"$2\" \"$3\"; }}",
             f"cd {q(worktree)} 2>/dev/null || true"]
    for cmd in cmds:
        name = f"command:{cmd.get('name')}"
        exe = first_executable(str(cmd.get("run") or ""))
        if exe is None:
            lines.append(f"r {q(name)} skip {q('runs only shell builtins')}")
        elif "$" in exe or "`" in exe:
            lines.append(f"r {q(name)} skip {q(f'starts with {exe}, which is expanded at run time')}")
        elif "/" in exe:
            lines.append(f"if [ -x {q(exe)} ]; then r {q(name)} ok {q(exe)}; "
                         f"else r {q(name)} miss {q(exe + ' is not an executable file')}; fi")
        else:
            lines.append(f"if command -v {q(exe)} >/dev/null 2>&1; then r {q(name)} ok \"$(command -v {q(exe)})\"; "
                         f"else r {q(name)} miss {q(exe + ' not found on PATH')}; fi")
    if agent is not None:
        binary, kind = agent
        credential = AGENT_BINARIES.get(kind, (binary, kind))[1]
        auth_file, variables = envs.CREDENTIALS.get(credential, ("", []))
        tests = [f"[ -n \"${{{variable}:-}}\" ]" for variable in variables]
        if auth_file:
            tests.append(f"[ -r \"$HOME/{auth_file}\" ]")
        present = " || ".join(tests) or "false"
        names = ", ".join([*variables, *([f"$HOME/{auth_file}"] if auth_file else [])])
        lines.append(f"if ! {q(binary)} --version >/dev/null 2>&1; then r agent miss {q(f'`{binary} --version` failed')}; "
                     f"elif {present}; then r agent ok {q(f'{binary} starts; a credential is present')}; "
                     f"else r agent miss {q(f'{binary} starts, but no credential ({names})')}; fi")
    lines.append(f"if ! command -v git >/dev/null 2>&1; then r git miss 'git not found on PATH'; "
                 f"elif git -C {q(worktree)} rev-parse HEAD >/dev/null 2>&1 && git -C {q(worktree)} status --porcelain "
                 f">/dev/null 2>&1; then r git ok 'rev-parse and status work in the worktree'; "
                 f"else r git miss 'git rev-parse/status fail in the worktree'; fi")
    if option.get("scope", "run") == "command":
        lines.append("r init skip 'scope command: every command gets a fresh container'")
    else:
        names = " ".join(INIT_NAMES)
        lines.append("init=$(cat /proc/1/comm 2>/dev/null); case \" " + names + " \" in "
                     "*\" $init \"*) r init ok \"$init\";; *) r init miss \"process 1 is ${init:-unknown}\";; esac")
    for cache in option.get("caches") or []:
        path = f"/caches/{cache}"
        lines.append(f"f={q(path)}/.al-doctor-$$; if : > \"$f\" 2>/dev/null && rm -f \"$f\"; then r {q('cache:' + str(cache))} ok "
                     f"{q(path + ' is writable')}; else r {q('cache:' + str(cache))} miss {q(path + ' is not writable')}; fi")
    if browser:
        lines.append(
            "b=''; for c in chromium chromium-browser google-chrome google-chrome-stable; do "
            "if command -v \"$c\" >/dev/null 2>&1; then b=$(command -v \"$c\"); break; fi; done; "
            "if [ -z \"$b\" ]; then for c in \"${PLAYWRIGHT_BROWSERS_PATH:-/nonexistent}\"/chromium*/chrome-linux*/chrome "
            "\"$HOME\"/.cache/ms-playwright/chromium*/chrome-linux*/chrome; do if [ -x \"$c\" ]; then b=$c; break; fi; done; fi; "
            "if [ -z \"$b\" ]; then r browser miss 'no chromium, chromium-browser, google-chrome or Playwright chrome'; "
            "else t=''; command -v timeout >/dev/null 2>&1 && t='timeout 30'; "
            "if $t \"$b\" --headless --no-sandbox --disable-gpu --dump-dom about:blank >/dev/null 2>&1; "
            "then r browser ok \"$b\"; else r browser miss \"$b does not start headless\"; fi; fi")
    minimum = MIN_FREE_MB * 1024
    lines.append(f"free=$(df -Pk {q(worktree)} /tmp 2>/dev/null | awk 'NR>1 {{print $4}}' | sort -n | head -1); "
                 f"if [ -z \"$free\" ]; then r disk skip 'df is not available'; "
                 f"elif [ \"$free\" -lt {minimum} ]; then r disk miss \"$((free / 1024)) MB free\"; "
                 f"else r disk ok \"$((free / 1024)) MB free\"; fi")
    return "\n".join(lines) + "\n"


def parse_doctor_output(text: str) -> list[Check]:
    checks: list[Check] = []
    for line in str(text).splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) >= 3 and parts[0] == MARK and parts[2] in ("ok", "miss", "skip"):
            checks.append(Check(parts[1], parts[2], "\t".join(parts[3:])))
    return checks


def _fix(check: Check, option: dict[str, Any], cmds: dict[str, dict[str, Any]], layer_dir: str) -> str:
    decl = _decl(option)
    name = check.name
    if name.startswith("command:"):
        cmd = name.split(":", 1)[1]
        exe = first_executable(str((cmds.get(cmd) or {}).get("run") or "")) or cmd
        return (f"add the package that provides `{exe}` to {decl} #:packages …) or #:setup, "
                f"or change (cmd {cmd} …)")
    if name == "agent":
        if "credential" in check.detail:
            kind = "codex" if "codex" in check.detail else "claude"
            return f"add #:credentials ({kind}) to {decl} …) and log in (or set the API key) on this machine"
        if option.get("agentInside"):
            return f"add the agent CLI to the image with {decl} #:setup …), or set #:agent-inside #f"
        return "install the agent CLI on this machine (it runs outside the container)"
    if name == "git":
        return f"add git to #:packages of {decl} …)"
    if name == "init":
        return "the container was not started with --init: run `al gen` so the layer's runtime starts one"
    if name.startswith("cache:"):
        cache = name.split(":", 1)[1]
        return (f"check the volume al-cache-{cache} (podman volume inspect al-cache-{cache}), or drop "
                f"\"{cache}\" from #:caches of {decl} …)")
    if name == "browser":
        return f"add chromium to #:packages of {decl} …), or turn the browser check off for this environment"
    if name == "disk":
        return "free disk space on the host (podman's storage)"
    if name.startswith("service:"):
        service = name.split(":", 1)[1]
        return f"check (service container #:id {service} …): its image, #:ready-cmd and #:ready-timeout"
    if name == "image":
        return f"build it: al sandbox {layer_dir} build"
    if name == "container":
        return f"see why the container does not start: al sandbox {layer_dir} build, then al sandbox {layer_dir} shell"
    return ""


def doctor(layer: Any, stack: Any, adw: dict[str, Any] | None, *, fast: bool = False,
           cmds: list[dict[str, Any]] | None = None, agent: tuple[str, str] | None | str = "auto",
           browser: bool | None = None, services: bool | None = None) -> list[Check]:
    """Check a container started exactly as a run starts it. Returns one Check per probe; each
    miss names the declaration to change."""
    option = stack.podman_option or {}
    cmds = workflow_commands(layer, adw) if cmds is None else cmds
    agent = agent_requirement(layer, adw, stack) if agent == "auto" else agent
    browser = needs_browser(layer, adw) if browser is None else browser
    services = True if services is None else services
    script = doctor_script(stack, cmds, agent=agent, browser=browser, services=services)
    expected = ([f"command:{cmd.get('name')}" for cmd in cmds] + (["agent"] if agent else []) + ["git", "init"]
                + [f"cache:{cache}" for cache in option.get("caches") or []] + (["browser"] if browser else []) + ["disk"])
    checks: list[Check] = []
    started_ok = True
    try:
        argv = stack._podman_argv(["sh", "-c", script]) if stack.podman_option else stack.wrap(script)
    except (StepFailed, LayerError) as exc:
        started_ok = False
        message = str(exc)
        sidecar = re.search(r"sidecar (\S+) ", message)
        name = f"service:{sidecar.group(1)}" if sidecar else "container"
        checks.append(Check(name, "miss", message.splitlines()[0][:300]))
        checks += [Check(item, "skip", "the container did not start") for item in expected]
    if started_ok:
        try:
            result = subprocess.run(argv, cwd=stack.cwd, env=stack.process_env(), capture_output=True, text=True,
                                    timeout=DOCTOR_TIMEOUT, check=False)
            found = parse_doctor_output(result.stdout)
            output_tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            found, output_tail = [], [str(exc)]
        seen = {check.name for check in found}
        if not found:
            checks.append(Check("container", "miss", "the doctor script printed nothing: "
                                + (" | ".join(output_tail) or "no output")[:300]))
            checks += [Check(item, "skip", "the doctor script did not run") for item in expected]
        else:
            checks += found
            checks += [Check(item, "miss", "not reported by the doctor script") for item in expected if item not in seen]
        checks += _service_checks(stack, services)
    host = host_agent_requirement(layer, adw, stack)
    if host is not None:
        where = shutil.which(host[0])
        checks.append(Check("agent", "ok" if where else "miss",
                            f"{where} (on this machine)" if where else f"{host[0]} not found on this machine's PATH"))
    if not fast:
        ref = str(getattr(stack.state, "image", "") or "") or image_ref(layer)
        info = image_info(ref)
        checks.append(Check("image", "ok" if info else "miss",
                            f"{ref}, {human_size(info.get('Size'))}" if info else f"{ref} is not built"))
    by_name = {str(cmd.get("name")): cmd for cmd in cmds}
    for check in checks:
        if check.status == "miss" and not check.fix:
            check.fix = _fix(check, option, by_name, str(layer.layer_dir))
    return checks


def _service_checks(stack: Any, enabled: bool) -> list[Check]:
    try:
        declared = stack.services()
    except LayerError as exc:
        return [Check("services", "miss", str(exc))]
    rows = []
    for service in declared:
        name = f"service:{service.get('id')}"
        if not enabled:
            rows.append(Check(name, "skip", "the services check is off for this environment"))
            continue
        if not getattr(stack, "_pod", None):
            rows.append(Check(name, "skip", "no pod was started"))
            continue
        ready = str(service.get("readyCmd") or "")
        if not ready:
            rows.append(Check(name, "ok", "running (no #:ready-cmd)"))
            continue
        result = _podman(["exec", f"{stack.run_name}-{service.get('id')}", "sh", "-c", ready], timeout=60)
        ok = result is not None and result.returncode == 0
        rows.append(Check(name, "ok" if ok else "miss", f"ready-cmd {ready!r} " + ("exits 0" if ok else "fails")))
    return rows


def format_table(checks: list[Check]) -> str:
    """Fixed columns check | result | detail | fix, misses first."""
    order = {"miss": 0, "ok": 1, "skip": 2}
    rows = sorted(checks, key=lambda check: order.get(check.status, 3))
    table = [("check", "result", "detail", "fix")] + [
        (check.name, check.status, check.detail.replace("\n", " "), check.fix) for check in rows]
    widths = [max(len(row[index]) for row in table) for index in range(3)]
    return "\n".join(f"{row[0]:<{widths[0]}} | {row[1]:<{widths[1]}} | {row[2]:<{widths[2]}} | {row[3]}".rstrip()
                     for row in table)


def summary(checks: list[Check]) -> str:
    count = {status: sum(1 for check in checks if check.status == status) for status in ("ok", "miss", "skip")}
    return f"{count['ok']} ok, {count['miss']} missed, {count['skip']} skipped"


# ------------------------------------------------------------------ preflight

def preflight(layer: Any, state: Any, adw: dict[str, Any], stack: Any) -> list[Check]:
    """Before a run's first step: the image of the current recipe exists, and the container has
    what the workflow needs. Raises StepFailed("preflight", table) on any miss."""
    option = stack.podman_option
    if not option:
        return []
    cmds = workflow_commands(layer, adw)
    dry_run = bool(getattr(state, "dry_run", False))   # a dry run's agent is the mock, which runs on the host
    agent = None if dry_run else agent_requirement(layer, adw, stack)
    agent_inside = bool(option.get("agentInside")) and not dry_run and _agent_kind(layer, adw) != "mock"
    if not cmds and not agent_inside:
        print(f"[adw {state.adw_id}] preflight: skipped, this workflow runs nothing in the container", flush=True)
        return []
    started = time.monotonic()
    ensure_image(layer)
    if not getattr(state, "image", ""):
        state.image = image_ref(layer)
    on = set(option.get("preflight", list(DEFAULT_PREFLIGHT)))
    checks = doctor(layer, stack, adw, fast=True, cmds=cmds, agent=agent,
                    browser=needs_browser(layer, adw) and "browser" in on, services="services" in on)
    missed = [check for check in checks if check.status == "miss"]
    events.say(f"[adw {state.adw_id}] preflight: {len(checks)} checks, {len(missed)} missed", "env.preflight",
               status="failed" if missed else "passed", seconds=round(time.monotonic() - started, 3),
               checks=[{"name": check.name, "status": check.status, "detail": check.detail} for check in checks])
    if missed:
        raise StepFailed("preflight", "the sandbox is missing what this workflow needs:\n" + format_table(checks))
    return checks
