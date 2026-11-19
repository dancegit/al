"""Everything that runs on a host of a (placement …): the self-contained run.

A self-contained run needs nothing from the machine it runs on except git, uv, a container engine
(when the workflow's environment has one) and network access to a git source. `adws/host.py start`
(tool_main) creates the run's own storage, `$XDG_STATE_HOME/al/<layer name>/runs/<id>/`:

    repo/         a clone of the base branch tip from the git source: a real .git directory with no
                  remote configured (the URL is never written to a file there)
    layer/        only for a layer that is not in the application repository: a clone of the layer's
                  own git source at the exact revision the home machine runs
    seed.json     what the home machine sent (0600)
    request.md    the prompt (0600)
    where.json    where the run's layer directory is, and the process that runs it
    tool.log      the output of the run's process

and starts that clone's own `adws/adw_<name>.py` with AL_SELF_CONTAINED=1. From there the unchanged
environment stack, sandbox, steps and review run exactly as on the home machine. Three things
differ, all in runner.py behind `active()`:

  * a (ship) step is not run. `deliver` commits pending work, imports the commit, checks the
    branch name and pushes `<sha>:refs/heads/<run branch>` to the git source: never forced, never
    another branch. The run ends `delivered`; the home machine runs the ship step through its gate.
  * a failed run makes no handoff; `deliver_stopped` pushes the work the same way.
  * request hooks, notifications and the delivery routing are the home machine's; what it decided
    comes in the seed.

After every step that moved HEAD the branch is pushed too (`checkpoint`), with a copy of the state
at that moment: a run whose host vanishes starts again elsewhere from there.

All runs of a host share one slot directory (`…/al/<name>/slots`, AL_SLOTS_DIR), so the limits of
(concurrency …) and #:max-runs-per-host hold per host.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import events, gitsafe, ir, localenv, recordsync, slots, state as state_module
from .errors import LayerError, StepFailed

PROTOCOL = recordsync.PROTOCOL
ENV_ACTIVE = "AL_SELF_CONTAINED"
ENV_SEED = "AL_RUN_SEED"
ENV_DEPLOYED = "AL_HOST_LAYER_DIR"       # the deployed layer directory (its local.env holds the host's values)
ENV_NO_SHALLOW = "AL_SELFRUN_NO_SHALLOW"  # the git source refuses shallow fetches: fetch the full history
SOURCE = "the git source"
FRESH_SECONDS = 4 * state_module.HEARTBEAT_SECONDS
STARTING_SECONDS = 120.0
GIT_TIMEOUT = 1800
PUSHED_STATE = "pushed-state.json"
DELIVER = "deliver"
AGENT_CREDENTIAL = {"claude-code": "claude", "codex": "codex"}
_seed_cache: dict[str, Any] | None = None


def active() -> bool:
    """True inside a self-contained run (the process `host.py start` launched)."""
    return os.environ.get(ENV_ACTIVE, "") == "1"


def seed() -> dict[str, Any]:
    """What the home machine sent for this run (the JSON file named by AL_RUN_SEED)."""
    global _seed_cache
    if _seed_cache is None:
        try:
            data = json.loads(Path(os.environ[ENV_SEED]).read_text(encoding="utf-8"))
        except (KeyError, OSError, ValueError) as exc:
            raise LayerError(f"a self-contained run needs its seed ({ENV_SEED}): {exc}") from exc
        if not isinstance(data, dict):
            raise LayerError("the run's seed is not a JSON object")
        _seed_cache = data
    return _seed_cache


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- storage and values

def storage_root(name: str) -> Path:
    """`$XDG_STATE_HOME/al/<layer name>` (default ~/.local/state/al/<layer name>)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "al" / name


def run_storage(name: str, run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F]{8}", str(run_id)):
        raise LayerError("a run id is 8 hexadecimal characters")
    return storage_root(name) / "runs" / run_id


def slots_dir(name: str) -> Path:
    return storage_root(name) / "slots"


def host_values(layer: Any) -> dict[str, str]:
    """The host's private values: the layer's env files, and local.env of the deployed layer when
    this process runs in a run's clone (which has none)."""
    values = localenv.load(layer)
    deployed = os.environ.get(ENV_DEPLOYED, "").strip()
    if deployed:
        with contextlib.suppress(OSError, ValueError):
            found, _bad = localenv.parse((Path(deployed) / localenv.FILE_NAME).read_text(encoding="utf-8", errors="replace"))
            values = {**values, **found}
    return values


def placement_of(layer: Any) -> dict[str, Any]:
    option = layer.ir.get("placement")
    return option if isinstance(option, dict) else {}


def source_url(layer: Any, values: dict[str, str] | None = None, which: str = "sourceEnv") -> str:
    """The git source as this machine reaches it; LayerError naming the variable when it is unset."""
    default = "AL_GIT_SOURCE" if which == "sourceEnv" else "AL_LAYER_SOURCE"
    name = str(placement_of(layer).get(which) or default)
    value = localenv.lookup(name, host_values(layer) if values is None else values).strip()
    if not value:
        raise LayerError(f"{name} is not set on this machine: put {name}=<the git URL as seen from here> in "
                         f"~/.config/al/{layer.name}.env (chmod 600) or in local.env in the layer directory")
    return value


def scrub(text: str, *secrets: str) -> str:
    """`text` without the given values (a git URL may hold a token): they never reach a log."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, f"<{SOURCE}>")
    return text


# ---------------------------------------------------------------- git

def _git(cwd: Path | None, args: list[str], step: str, *, check: bool = True, secrets: tuple[str, ...] = (),
         timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    argv = ["git", *gitsafe.HARDENING, *args]
    env = {**gitsafe._base_env(), "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed(step, scrub(f"git {args[0]} failed: {exc}", *secrets)) from exc
    done.stdout, done.stderr = scrub(done.stdout, *secrets), scrub(done.stderr, *secrets)
    if check and done.returncode != 0:
        raise StepFailed(step, f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-1500:]}")
    return done


def _fetch(repo: Path, url: str, ref: str, step: str, depth: int = 0) -> str:
    """Fetch `ref` of `url` into `repo` without touching a ref or recording the URL; returns the commit."""
    args = ["fetch", "--no-tags", "--quiet", *(["--depth", str(depth)] if depth else []), url, ref]
    try:
        _git(repo, args, step, secrets=(url,))
        return _git(repo, ["rev-parse", "--verify", "FETCH_HEAD^{commit}"], step).stdout.strip()
    finally:
        for name in ("FETCH_HEAD",):   # it names where the objects came from
            with contextlib.suppress(OSError):
                (_git_dir(repo) / name).unlink()


def _git_dir(repo: Path) -> Path:
    return repo if (repo / "HEAD").is_file() and not (repo / ".git").exists() else repo / ".git"


def mirror_path(url: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "al" / "mirrors" / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".git")


@contextlib.contextmanager
def _try_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive lock on `path` when nobody else does; yields whether it is held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _through_mirror(repo: Path, url: str, branch: str, depth: int) -> str | None:
    """Fetch the branch through this machine's mirror of the source: the mirror is brought up to
    date, then the clone copies from it (objects are copied, never shared: no alternates). None
    when the mirror cannot be used for any reason; the caller then fetches directly."""
    mirror = mirror_path(url)
    try:
        with _try_lock(mirror.with_suffix(".lock")) as held:
            if not held:
                return None
            if not (mirror / "HEAD").is_file():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                _git(None, ["init", "--quiet", "--bare", str(mirror)], "clone")
            tip = _fetch(mirror, url, f"refs/heads/{branch}", "clone")
            _git(mirror, ["update-ref", f"refs/heads/{branch}", tip], "clone")
            got = _fetch(repo, str(mirror), f"refs/heads/{branch}", "clone", depth)
            return got if got == tip else None
    except (StepFailed, OSError):
        return None


def clone(repo: Path, url: str, branch: str, *, shallow: bool = True, also: str = "") -> str:
    """`repo` becomes a repository whose `branch` is the tip of that branch at `url`; returns the
    commit. `also` is a second branch whose commits are fetched too (a run that starts again)."""
    _git(None, ["init", "--quiet", "-b", branch, str(repo)], "clone")
    depth = 1 if shallow and os.environ.get(ENV_NO_SHALLOW, "") != "1" and not also else 0
    tip = _through_mirror(repo, url, branch, depth)
    if tip is None:
        try:
            tip = _fetch(repo, url, f"refs/heads/{branch}", "clone", depth)
        except StepFailed:
            if not depth:
                raise
            tip = _fetch(repo, url, f"refs/heads/{branch}", "clone")   # a source that refuses shallow fetches
    _git(repo, ["checkout", "--quiet", "-B", branch, tip], "clone")
    if also:
        _fetch(repo, url, f"refs/heads/{also}", "clone")
    with contextlib.suppress(OSError):
        (repo / ".git" / "objects" / "info" / "alternates").unlink()
    return tip


def prepare(layer: Any, storage: Path, seed_data: dict[str, Any], values: dict[str, str]) -> dict[str, str]:
    """The run's clone(s) under `storage`. `layer` is the deployed layer (it says where the layer
    lives). Returns {layer_dir, app_root, base}."""
    url = source_url(layer, values)
    branch = str(seed_data.get("base_branch") or layer.codebase_default_branch)
    restart = seed_data.get("restart") if isinstance(seed_data.get("restart"), dict) else {}
    repo = storage / "repo"
    base = clone(repo, url, branch, also=str(restart.get("branch") or "") if restart.get("sha") else "")
    in_repo = layer.in_repo_dir
    if in_repo:
        layer_dir = repo / in_repo
        if not (layer_dir / ir.IR_FILE).is_file():
            raise LayerError(f"the base branch {branch} at {SOURCE} holds no layer in {in_repo}/: commit and push it")
        return {"layer_dir": str(layer_dir), "app_root": str(repo), "base": base}
    layer_url = source_url(layer, values, "layerSourceEnv")
    revision = str(seed_data.get("layer_rev") or "")
    if not revision:
        raise LayerError("the seed names no layer revision")
    layer_dir = storage / "layer"
    _git(None, ["init", "--quiet", str(layer_dir)], "clone")
    try:
        _git(layer_dir, ["fetch", "--no-tags", "--quiet", layer_url, "+refs/heads/*:refs/al/source/*"], "clone",
             secrets=(layer_url,))
    finally:
        with contextlib.suppress(OSError):
            (layer_dir / ".git" / "FETCH_HEAD").unlink()
    found = _git(layer_dir, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"], "clone", check=False)
    if found.returncode != 0:
        raise LayerError(f"the layer's git source does not hold revision {revision[:12]}: push the layer")
    _git(layer_dir, ["checkout", "--quiet", "--detach", found.stdout.strip()], "clone")
    return {"layer_dir": str(layer_dir), "app_root": str(repo), "base": base}


# ---------------------------------------------------------------- image

def pull_image(layer: Any) -> bool:
    """Get the sandbox image of the current recipe from the declared registry by its recipe tag,
    check its al.recipe label and tag it as this layer's image. False (with `pull_image.reason`)
    when the registry has no address here, the pull fails or the label differs."""
    from . import sandbox

    registry = layer.ir.get("registry")
    if not isinstance(registry, dict):
        pull_image.reason = "no (registry …) is declared"
        return False
    values = host_values(layer)
    address = localenv.lookup(str(registry.get("addressEnv") or "AL_REGISTRY"), values).strip().rstrip("/")
    if not address:
        pull_image.reason = f"{registry.get('addressEnv') or 'AL_REGISTRY'} is not set on this machine"
        return False
    recipe = sandbox.recipe_hash(layer)
    remote = f"{address}/{layer.name}:recipe-{recipe}"
    local = f"{sandbox.image_name(layer)}:{recipe}"
    options = [] if registry.get("tlsVerify", True) else ["--tls-verify=false"]
    directory = tempfile.mkdtemp(prefix="al-auth-")
    try:
        user = localenv.lookup(str(registry.get("usernameEnv") or ""), values) if registry.get("usernameEnv") else ""
        password = localenv.lookup(str(registry.get("passwordEnv") or ""), values) if registry.get("passwordEnv") else ""
        if user and password:
            authfile = os.path.join(directory, "auth.json")
            try:
                login = subprocess.run(["podman", "login", "--authfile", authfile, *options, "--username", user,
                                        "--password-stdin", address.split("/")[0]], input=password, text=True,
                                       capture_output=True, timeout=120, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pull_image.reason = "the registry login did not answer"
                return False
            if login.returncode != 0:
                pull_image.reason = "the registry refused the login"
                return False
            options = ["--authfile", authfile, *options]
        pulled = sandbox._podman(["pull", *options, remote], timeout=1800)
        if pulled is None or pulled.returncode != 0:
            pull_image.reason = f"no image with tag recipe-{recipe}"
            return False
        label = sandbox._podman(["image", "inspect", "--format", '{{index .Labels "al.recipe"}}', remote])
        if label is None or label.returncode != 0 or label.stdout.strip() != recipe:
            sandbox._podman(["untag", remote])
            pull_image.reason = "the pulled image was built from another recipe"
            return False
        sandbox._podman(["tag", remote, local])
        sandbox._tag_latest(layer, local)
        pull_image.reason = ""
        return True
    finally:
        shutil.rmtree(directory, ignore_errors=True)


pull_image.reason = ""


# ---------------------------------------------------------------- facts about this machine

def _alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a process that ended but was not collected yet is not running
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
        return fields[0] != "Z"
    except (OSError, IndexError):
        return True


def _age(stamp: str) -> float:
    try:
        return max(0.0, time.time() - datetime.fromisoformat(stamp).timestamp())
    except (TypeError, ValueError):
        return float("inf")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_facts(name: str, run_id: str) -> dict[str, Any]:
    """{run, status, heartbeat, alive, layer_dir, app_root} of a run in this machine's storage;
    status `starting` until the run wrote its state, `unknown` without one."""
    storage = run_storage(name, run_id)
    where = _read_json(storage / "where.json")
    data = _read_json(Path(where.get("layer_dir", "")) / "agents" / run_id / "state.json") if where else {}
    pid = data.get("pid") or where.get("pid")
    alive = _alive(pid) or (_alive(where.get("pid")) if where else False)
    if data:
        status = str(data.get("status") or "unknown")
    elif where:
        status = "starting" if alive or time.time() - float(where.get("at") or 0) < STARTING_SECONDS else "unknown"
    else:
        status = "unknown"
    return {"run": run_id, "status": status, "heartbeat": str(data.get("heartbeat") or ""), "alive": bool(alive),
            "layer_dir": str(where.get("layer_dir") or ""), "app_root": str(where.get("app_root") or "")}


def all_runs(name: str) -> list[dict[str, Any]]:
    root = storage_root(name) / "runs"
    try:
        ids = sorted(entry for entry in os.listdir(root) if re.fullmatch(r"[0-9a-fA-F]{8}", entry))
    except OSError:
        return []
    return [run_facts(name, run_id) for run_id in ids]


def running(facts: dict[str, Any]) -> bool:
    if facts["status"] == "starting":
        return True
    return facts["status"] == "running" and (_age(facts["heartbeat"]) <= FRESH_SECONDS or facts["alive"])


def _meminfo() -> tuple[int, int]:
    total = available = 0
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key == "MemTotal":
                total = int(rest.split()[0])
            elif key == "MemAvailable":
                available = int(rest.split()[0])
    return total, available


def _load1() -> float:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        try:
            return float(os.getloadavg()[0])
        except OSError:
            return 0.0


def logins(layer: Any, values: dict[str, str]) -> dict[str, str]:
    """{agent kind: `own login` | `token VAR in its env file` | `none` | `not needed`} for the
    agents the layer declares. The file and the variables are those of envs.CREDENTIALS."""
    from . import envs

    out: dict[str, str] = {}
    for agent in layer.ir.get("agents") or []:
        kind = str(agent.get("kind") or "")
        credential = AGENT_CREDENTIAL.get(kind)
        if credential is None:
            out[kind] = "not needed"
            continue
        relative, variables = envs.CREDENTIALS[credential]
        if (Path.home() / relative).is_file():
            out[kind] = "own login"
            continue
        token = next((name for name in variables if localenv.lookup(name, values)), "")
        out[kind] = f"token {token} in its env file" if token else "none"
    return out


def machine_facts(layer: Any, runs: int, disk: Path, values: dict[str, str]) -> dict[str, Any]:
    """The load of this machine as the scheduler reads it: numbers and words only, no path, user
    or host name."""
    from . import sandbox

    total, available = _meminfo()
    try:
        free = shutil.disk_usage(disk if disk.exists() else Path.home()).free // 1024
    except OSError:
        free = 0
    image: bool | None = None
    if any(option.get("kind") == "podman" for option in layer.ir.get("environments") or []):
        try:
            image = sandbox.image_exists(sandbox.image_ref(layer))
        except (LayerError, StepFailed, OSError):
            image = False
    return {"protocol": PROTOCOL, "runs": runs, "max_runs": max(1, slots.limits(layer)["runs"]),
            "cpus": os.cpu_count() or 1, "load1": round(_load1(), 2), "mem_total_kb": total,
            "mem_available_kb": available, "disk_free_kb": free, "image": image, "logins": logins(layer, values)}


# ---------------------------------------------------------------- the host tool

def _held_slots(layer: Any) -> int:
    limit = max(1, slots.limits(layer)["runs"])
    return limit - slots.free_slots(layer, "runs")


def _tool_load(layer: Any) -> dict[str, Any]:
    count = sum(1 for facts in all_runs(layer.name) if running(facts))
    return machine_facts(layer, max(count, _held_slots(layer)), storage_root(layer.name), localenv.load(layer))


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _tool_doctor(layer: Any) -> dict[str, Any]:
    values = localenv.load(layer)
    rows: list[dict[str, Any]] = []

    def row(name: str, ok: bool, detail: str, fix: str = "") -> None:
        rows.append({"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix})

    row("git", _which("git"), "found" if _which("git") else "not found", "install git")
    row("uv", _which("uv"), "found" if _which("uv") else "not found", "install uv (al deploy --provision does)")
    podman = any(option.get("kind") == "podman" for option in layer.ir.get("environments") or [])
    if podman:
        rootless = ""
        if _which("podman"):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                rootless = subprocess.run(["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
                                          capture_output=True, text=True, timeout=60, check=False).stdout.strip()
        row("podman", rootless == "true", "rootless" if rootless == "true" else ("not rootless" if _which("podman") else "not found"),
            "install podman and set up rootless containers for this user")
    found = logins(layer, values)
    binaries = {"claude-code": "claude", "codex": "codex"}
    for kind, text in found.items():
        if kind in binaries:
            row(f"agent {kind}", _which(binaries[kind]), "found" if _which(binaries[kind]) else f"{binaries[kind]} not found",
                f"install the {binaries[kind]} CLI on the host")
            from . import envs

            variables = envs.CREDENTIALS[AGENT_CREDENTIAL[kind]][1]
            row(f"login {kind}", text != "none", text,
                f"log in on it, or name a token ({' or '.join(variables)}) in its env file")
    placement = placement_of(layer)
    name = str(placement.get("sourceEnv") or "AL_GIT_SOURCE")
    url = localenv.lookup(name, values).strip()
    if not url:
        row("git source", False, f"{name} is not set", f"put {name}=<the git URL as seen from the host> in its env file")
    else:
        answered = False
        with contextlib.suppress(StepFailed):
            answered = _git(None, ["ls-remote", "--heads", url], "doctor", check=False, secrets=(url,),
                            timeout=60).returncode == 0
        row("git source", answered, "answers" if answered else "does not answer",
            f"check {name} in its env file and that the host may fetch from and push to it")
    registry = layer.ir.get("registry")
    if podman and isinstance(registry, dict):
        variable = str(registry.get("addressEnv") or "AL_REGISTRY")
        present = bool(localenv.lookup(variable, values))
        row("registry", present, f"{variable} is set" if present else f"{variable} is not set",
            f"put {variable}=… in its env file, or the host builds the sandbox image itself")
    return {"protocol": PROTOCOL, "rows": rows, "logins": found}


def _opening_events(path: Path, seed_data: dict[str, Any]) -> None:
    """The home machine's own events of the start (placement.decision, remote.start) open the run's
    events on the host, so the record that travels back holds them in order, once."""
    recorder = events.Recorder(path)
    recorder.seq = max(recorder.seq, int(seed_data.get("seq") or 0))
    for item in seed_data.get("events") or []:
        if isinstance(item, dict) and item.get("type") in events.TYPES:
            recorder.emit(str(item["type"]), dict(item.get("fields") or {}))


def _token_variables(layer: Any, adw_name: str, values: dict[str, str]) -> dict[str, str]:
    """The agent token variables this run may see: those of the credentials its podman option
    lists and of the agents the workflow runs on. Nothing else of the env file reaches the run."""
    from . import envs

    names: set[str] = set()
    try:
        adw = layer.adw(adw_name)
    except LayerError:
        adw = {}
    for option_id in adw.get("environment") or []:
        option = next((o for o in layer.ir.get("environments") or [] if o.get("id") == option_id), {})
        if option.get("kind") == "podman":
            names.update(str(name) for name in option.get("credentials") or [])
    agent_ids = {str(adw.get("agent") or "")} | {str(step.get("agent")) for step in adw.get("steps") or [] if step.get("agent")}
    for agent in layer.ir.get("agents") or []:
        if agent.get("id") in agent_ids and agent.get("kind") in AGENT_CREDENTIAL:
            names.add(AGENT_CREDENTIAL[str(agent["kind"])])
    found: dict[str, str] = {}
    for name in names:
        for variable in envs.CREDENTIALS.get(name, ("", []))[1]:
            value = localenv.lookup(variable, values)
            if value:
                found[variable] = value
    return found


def _tool_start(layer: Any, seed_data: dict[str, Any]) -> dict[str, Any]:
    if seed_data.get("protocol") != PROTOCOL:
        raise LayerError("the home machine speaks another protocol: al deploy this layer again")
    run_id, adw_name = str(seed_data.get("run") or ""), str(seed_data.get("adw") or "")
    storage = run_storage(layer.name, run_id)
    if (storage / "where.json").is_file() or (storage / "seed.json").is_file():
        return {"protocol": PROTOCOL, "run": run_id, "started": False, "existing": True}
    layer.adw(adw_name)   # LayerError when this host's layer does not declare the workflow
    values = localenv.load(layer)
    storage.mkdir(parents=True, exist_ok=True)
    os.chmod(storage, 0o700)

    def private(name: str, text: str) -> Path:
        path = storage / name
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        return path

    seed_path = private("seed.json", json.dumps(seed_data, indent=2) + "\n")
    try:
        where = prepare(layer, storage, seed_data, values)
    except (LayerError, StepFailed, OSError):
        shutil.rmtree(storage, ignore_errors=True)   # nothing started: the same id may be started again
        raise
    request = private("request.md", str(seed_data.get("prompt") or "") + "\n")
    layer_dir = Path(where["layer_dir"])
    run_dir = layer_dir / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    restart = seed_data.get("restart") if isinstance(seed_data.get("restart"), dict) else {}
    for name, text in (restart.get("files") or {}).items():
        if isinstance(name, str) and isinstance(text, str) and "/" not in name and run_id in name:
            (layer_dir / "specs").mkdir(exist_ok=True)
            (layer_dir / "specs" / name).write_text(text, encoding="utf-8")
    _opening_events(run_dir / events.FILE_NAME, seed_data)
    slots_dir(layer.name).mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in (ir.APP_ROOT_ENV,)}
    env.update(_token_variables(layer, adw_name, values))
    env.update({ENV_ACTIVE: "1", ENV_SEED: str(seed_path), slots.SLOTS_DIR_ENV: str(slots_dir(layer.name)),
                ENV_DEPLOYED: str(Path(layer.layer_dir))})
    if not layer.in_repo_dir:
        env[ir.APP_ROOT_ENV] = where["app_root"]
    argv = ["uv", "run", "--quiet", str(layer_dir / "adws" / f"adw_{adw_name}.py"), "--adw-id", run_id,
            "--prompt-file", str(request)]
    with (storage / "tool.log").open("ab") as log:
        process = subprocess.Popen(argv, cwd=str(layer_dir), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    recordsync.write_json(storage / "where.json", {**where, "pid": process.pid, "at": time.time(), "adw": adw_name})
    return {"protocol": PROTOCOL, "run": run_id, "started": True, "existing": False, "base": where["base"]}


def _layer_of_run(layer: Any, facts: dict[str, Any]) -> Any:
    """The layer of a run's clone (the deployed layer's IR is the same definition)."""
    return ir.Layer(layer.ir, Path(facts["layer_dir"]), Path(facts["app_root"]))


def _tool_pull(layer: Any, run_id: str, cursor: dict[str, Any]) -> bytes:
    facts = run_facts(layer.name, run_id)
    if not facts["layer_dir"]:
        raise LayerError(f"no run {run_id} on this host")
    run_layer = _layer_of_run(layer, facts)
    header = {"run": run_id, "layer_dir": facts["layer_dir"], "app_root": facts["app_root"],
              "status": facts["status"], "alive": facts["alive"]}
    return recordsync.delta(recordsync.run_roots(run_layer, run_id), cursor, header)


def _tool_clean(layer: Any, run_id: str) -> dict[str, Any]:
    storage = run_storage(layer.name, run_id)
    facts = run_facts(layer.name, run_id)
    if not storage.is_dir():
        return {"protocol": PROTOCOL, "run": run_id, "removed": False, "reason": "no such run"}
    if facts["alive"] or facts["status"] == "starting":
        return {"protocol": PROTOCOL, "run": run_id, "removed": False, "reason": "the run is still running"}
    shutil.rmtree(storage, ignore_errors=True)
    return {"protocol": PROTOCOL, "run": run_id, "removed": not storage.exists(), "reason": ""}


TOOL_USAGE = "usage: host.py load | doctor | runs | start < seed.json | pull RUN < cursor.json | clean RUN"


def tool_main(argv: list[str], layer_dir: Path) -> int:
    """`adws/host.py VERB …` on a host: prints one JSON document (or the record frames of `pull`)
    and nothing else on standard output. Exit 0, or 1 with one line on standard error."""
    verb, rest = (argv[0] if argv else ""), list(argv[1:])
    out = sys.stdout.buffer
    try:
        layer = ir.load(Path(layer_dir))
        os.environ[slots.SLOTS_DIR_ENV] = str(slots_dir(layer.name))
        if verb == "load" and not rest:
            answer: Any = _tool_load(layer)
        elif verb == "doctor" and not rest:
            answer = _tool_doctor(layer)
        elif verb == "runs" and not rest:
            answer = {"protocol": PROTOCOL, "runs": [{"run": f["run"], "status": f["status"], "heartbeat": f["heartbeat"]}
                                                    for f in all_runs(layer.name)]}
        elif verb == "start" and not rest:
            answer = _tool_start(layer, json.loads(sys.stdin.buffer.read().decode("utf-8")))
        elif verb == "pull" and len(rest) == 1:
            text = sys.stdin.buffer.read().decode("utf-8").strip()
            cursor = json.loads(text) if text else {}
            out.write(_tool_pull(layer, rest[0], cursor if isinstance(cursor, dict) else {}))
            out.flush()
            return 0
        elif verb == "clean" and len(rest) == 1:
            answer = _tool_clean(layer, rest[0])
        else:
            print(TOOL_USAGE, file=sys.stderr)
            return 2
    except (LayerError, StepFailed, ValueError, OSError) as exc:
        print(f"host.py {verb}: {exc}", file=sys.stderr)
        return 1
    out.write((json.dumps(answer, sort_keys=True) + "\n").encode("utf-8"))
    out.flush()
    return 0


# ---------------------------------------------------------------- inside the run: restart, delivery

def restored_state(layer: Any, seed_data: dict[str, Any]) -> state_module.ADWState | None:
    """The state a run that starts again continues from: what was recorded when its branch was
    last pushed, with the first host's paths rewritten to this clone. None for a new run or a run
    that never pushed."""
    restart = seed_data.get("restart") if isinstance(seed_data.get("restart"), dict) else {}
    pushed = restart.get("state") if isinstance(restart.get("state"), dict) else None
    if not pushed or not isinstance(pushed.get("state"), dict):
        return None
    pairs = sorted(((str(pushed.get(key) or ""), str(here)) for key, here in
                    (("layer_dir", Path(layer.layer_dir)), ("app_root", Path(layer.app_root)))),
                   key=lambda pair: -len(pair[0]))
    data = recordsync._rewrite(pushed["state"], [pair for pair in pairs if pair[0] and pair[0] != pair[1]])
    try:
        state = state_module.ADWState(**data)
    except (TypeError, ValueError, LayerError) as exc:
        raise LayerError(f"the pushed state of the run cannot be read: {exc}") from exc
    state.steps = [record for record in state.steps if record.get("status") in ("passed", "rejected", "skipped")]
    state.status, state.ended, state.error, state.worktree, state.ports = "running", "", "", "", []
    state.remote = {key: value for key, value in state.remote.items() if key in ("base",)}
    state.remote["restarted"] = {"sha": str(restart.get("sha") or ""), "branch": str(restart.get("branch") or "")}
    return state


def move_worktree(layer: Any, state: Any, stack: Any, sha: str, branch: str, step: str) -> None:
    """Put the run's fresh worktree on commit `sha`, as the run's branch `branch`."""
    sandbox = getattr(stack, "sandbox", None)
    if sandbox is not None:   # detached; the branch ref is created by the host side when it publishes
        gitsafe.run(sandbox, ["checkout", "--quiet", "--detach", sha], step)
        state.branch = branch
        return
    worktree = str(state.worktree)
    if state.branch == branch:
        _git(Path(worktree), ["reset", "--quiet", "--hard", sha], step)
        return
    previous = state.branch
    _git(Path(worktree), ["checkout", "--quiet", "-B", branch, sha], step)
    if previous:
        _git(Path(layer.app_root), ["branch", "--quiet", "-D", previous], step, check=False)
    state.branch = branch


def after_setup(layer: Any, state: Any, stack: Any) -> None:
    """Right after the environment exists: a run that starts again moves to the commit it pushed."""
    restarted = state.remote.pop("restarted", None)
    if restarted and restarted.get("sha") and state.worktree:
        from . import envs

        envs.check_run_branch(str(restarted["branch"]), state.adw_id, "restart")
        _git(Path(layer.app_root), ["cat-file", "-e", f"{restarted['sha']}^{{commit}}"], "restart")
        move_worktree(layer, state, stack, str(restarted["sha"]), str(restarted["branch"]), "restart")
        state.remote["pushed"] = {"sha": restarted["sha"], "step": "restart", "at": now()}
        print(f"[adw {state.adw_id}] restart: continuing from {str(restarted['sha'])[:12]} on {state.branch}")
    state.remote["branch"] = state.branch
    state.save(layer.layer_dir)


def protected_branches(layer: Any) -> set[str]:
    """Branches a host never pushes: the default branch, the run's target, every stage branch."""
    names = {layer.codebase_default_branch, layer.default_branch}
    for stage in layer.ir.get("stages") or []:
        if isinstance(stage, dict) and stage.get("branch"):
            names.add(str(stage["branch"]))
    return names


def check_branch(layer: Any, branch: str, adw_id: str, step: str = DELIVER) -> None:
    """Refuse every branch that is not this run's own, before anything is pushed."""
    from . import envs

    if branch in protected_branches(layer) or branch.startswith("al/ship/"):
        raise StepFailed(step, f"refusing to push {branch!r}: a host only pushes the run's own branch, never a "
                               "default or stage branch")
    envs.check_run_branch(branch, adw_id, step)


def _write_pushed_state(layer: Any, state: Any) -> None:
    state.save(layer.layer_dir)
    run_dir = state_module.run_dir(layer.layer_dir, state.adw_id)
    data = _read_json(run_dir / "state.json")
    recordsync.write_json(run_dir / PUSHED_STATE, {"layer_dir": str(Path(layer.layer_dir)),
                                                   "app_root": str(Path(layer.app_root)), "state": data})


def push(layer: Any, state: Any, sha: str, step: str, kind: str) -> None:
    """Push `sha` as the run's branch to the git source: `<sha>:refs/heads/<branch>`, no `+`, no
    --force. A source that already has the branch at a commit this one does not contain is left
    as it is and the step fails."""
    branch = str(state.branch)
    check_branch(layer, branch, state.adw_id, step)
    url = source_url(layer)
    repo = Path(layer.app_root)
    different = StepFailed(step, f"{SOURCE} already has a different {branch}; nothing was pushed")
    listed = _git(repo, ["ls-remote", url, f"refs/heads/{branch}"], step, secrets=(url,), timeout=300)
    there = listed.stdout.split()[0] if listed.stdout.strip() else ""
    if there and there != sha:
        known = _git(repo, ["merge-base", "--is-ancestor", there, sha], step, check=False)
        if known.returncode != 0:
            raise different
    if there != sha:
        done = _git(repo, ["push", "--quiet", url, f"{sha}:refs/heads/{branch}"], step, check=False, secrets=(url,))
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip()
            if any(word in detail for word in ("non-fast-forward", "fetch first", "rejected")):
                raise different
            raise StepFailed(step, f"pushing {branch} to {SOURCE} failed: {detail[-800:]}")
    state.remote["pushed"] = {"sha": sha, "step": step, "at": now()}
    state.remote["branch"] = branch
    _write_pushed_state(layer, state)
    events.say(f"[adw {state.adw_id}] pushed {branch} at {sha[:12]} to {SOURCE} ({kind})", "remote.push",
               sha=sha, branch=branch, step=step, kind=kind)


def _head(state: Any, stack: Any, step: str) -> str:
    sandbox = getattr(stack, "sandbox", None)
    if sandbox is not None:
        return gitsafe.run(sandbox, ["rev-parse", "HEAD"], step).stdout.strip()
    return _git(Path(state.worktree), ["rev-parse", "HEAD"], step).stdout.strip()


def _publish(layer: Any, state: Any, stack: Any, step: str, message: str = "", commit: bool = True) -> str:
    """Commit what is pending (when `commit`), make the commit part of the clone and name it as
    the run's branch there. Returns the commit."""
    from . import envs, ship

    worktree = Path(state.worktree)
    sandbox = getattr(stack, "sandbox", None)
    with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
        if commit:
            sha = ship.commit_pending(worktree, state, ship._never_commit(layer, state), sandbox, message=message)
        else:
            sha = _head(state, stack, step)
        if sandbox is not None:
            sha = gitsafe.import_objects(sandbox, "HEAD" if commit else sha, step)
    check_branch(layer, str(state.branch), state.adw_id, step)
    if sandbox is not None:
        envs.check_run_branch(state.branch, state.adw_id, step)
        gitsafe.update_ref(sandbox, f"refs/heads/{state.branch}", sha, step, f"adw {state.adw_id}: run branch")
    return sha


def checkpoint(layer: Any, state: Any, stack: Any, step: dict[str, Any]) -> None:
    """After a step: when it committed, push the run's branch, so a run that is lost starts again
    from here. Uncommitted work is not touched. A source that cannot be reached is a warning; a
    source that holds a different branch of this name fails the run."""
    if not state.worktree or getattr(stack, "read_only", False):
        return
    name = str(step.get("name") or "")
    try:
        head = _head(state, stack, name)
        if head in (state.remote.get("base"), (state.remote.get("pushed") or {}).get("sha")):
            return
        sha = _publish(layer, state, stack, name, commit=False)
        push(layer, state, sha, name, "checkpoint")
    except StepFailed as exc:
        if "already has a different" in str(exc) or exc.step == gitsafe.INTEGRITY:
            raise
        print(f"[adw {state.adw_id}] warning: the work after step {name} was not pushed: {exc.reason}")


def deliver(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any] | None, stack: Any) -> None:
    """In place of a (ship) step (or at the end of a workflow without one): commit what is pending
    and push the run's own branch. The home machine ships."""
    del adw
    name = str((step or {}).get("name") or DELIVER)
    if getattr(stack, "read_only", False) or not state.worktree:
        if step is not None:
            raise StepFailed(name, "delivering needs a worktree environment")
        return
    sha = _publish(layer, state, stack, name)
    if sha == state.remote.get("base") and step is None:
        state.remote["delivered"] = {}   # nothing was changed: nothing to push
        return
    push(layer, state, sha, name, "delivered")
    state.remote["delivered"] = {"sha": sha, "branch": state.branch, "at": now(), "step": name}
    _write_pushed_state(layer, state)


def deliver_stopped(layer: Any, state: Any, adw: dict[str, Any], stack: Any, failure: BaseException) -> None:
    """A run that failed on a host: its work is pushed like a delivery, so the home machine can
    hand it to a person. Never raises; the failure that stopped the run stands."""
    from . import ship

    try:
        step, _kind, reason = ship.stopped_at(state, adw, failure)
        if (isinstance(failure, StepFailed) and failure.step == gitsafe.INTEGRITY) or reason.startswith(f"{gitsafe.INTEGRITY}:"):
            state.remote["deliver_skipped"] = f"{gitsafe.INTEGRITY} failure: the worktree is left on the host for inspection"
            return
        if getattr(stack, "read_only", False) or not state.worktree or not Path(state.worktree).is_dir():
            state.remote["deliver_skipped"] = "the run has no worktree: nothing to push"
            return
        sha = _publish(layer, state, stack, DELIVER,
                       message=f"{ship._title(state)} (stopped at {step}; needs a human) [adw {state.adw_id}]")
        if sha == state.remote.get("base"):
            state.remote["deliver_skipped"] = "nothing changed: nothing to push"
            return
        push(layer, state, sha, DELIVER, "stopped")
        state.remote["delivered"] = {"sha": sha, "branch": state.branch, "at": now(), "step": step, "stopped": True}
    except Exception as exc:  # noqa: BLE001 — the push must never mask the failure that stopped the run
        state.remote["deliver_error"] = f"{type(exc).__name__}: {exc}"[:1000]
        print(f"[adw {state.adw_id}] warning: the stopped run's work was not pushed: {exc}")
    finally:
        with contextlib.suppress(OSError):
            state.save(layer.layer_dir)
