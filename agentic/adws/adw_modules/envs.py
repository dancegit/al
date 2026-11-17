from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from . import events, gitsafe, slots
from .errors import LayerError, StepFailed

PODMAN_BUILD_TIMEOUT = 3600
CREDENTIAL_FOLLOW_SECONDS = 5.0   # how often a run's credential copy is compared with the host's file
IMPROVE_DIR = ".al-improve"  # run state of `al improve` inside a layer: never visible to an ordinary run


def _run_git(args: list[str], step: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed(step, str(exc)) from exc
    if result.returncode:
        raise StepFailed(step, result.stderr.strip() or result.stdout.strip() or f"command exited {result.returncode}")
    return result


def slugify(prompt: str) -> str:
    words = []
    for word in prompt.lower().split():
        cleaned = "".join(char for char in word if char.isalnum())
        if cleaned:
            words.append(cleaned)
        if len(words) == 6:
            break
    return "-".join(words) or "task"


def check_run_branch(branch: str, adw_id: str, step: str = "ship") -> None:
    """A sandboxed run may only ever publish its own branch `<class>/<adw_id>-<slug>`. The name
    is re-checked before every host ref update because state.json can be container-writable."""
    if not re.fullmatch(rf"[a-z]+/{re.escape(adw_id)}-[a-z0-9]+(-[a-z0-9]+)*", branch or ""):
        raise StepFailed(step, f"refusing to publish branch {branch!r}: not this run's branch")


def _bindable(ports: list[int]) -> bool:
    sockets: list[socket.socket] = []
    try:
        for port in ports:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(probe)
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        for probe in sockets:
            probe.close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed process whose parent has not collected it yet (a zombie) still answers a signal. It
    # runs nothing, so it holds nothing: this matters in containers, where no init collects it.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _live_leases(layer_dir: Path, table: Path, keep: str) -> dict[str, dict[str, Any]]:
    """Leases whose run still exists and whose owning process is alive. A lease is tied to a live
    process exactly like a slot is, so a killed run cannot hold its port block forever."""
    raw = json.loads(table.read_text()) if table.exists() else {}
    leases: dict[str, dict[str, Any]] = {}
    for run, lease in raw.items():
        if isinstance(lease, list):                  # table written by an older runtime
            lease = {"ports": lease, "pid": 0}
        exists = (Path(layer_dir) / "agents" / run / "state.json").exists()
        if run == keep or (exists and lease.get("pid") and _alive(int(lease["pid"]))):
            leases[run] = lease
    return leases


def lease_ports(layer_dir: Path, adw_id: str, base: int, count: int) -> list[int]:
    """Reserve a port block for a run. The lease table (agents/.ports.json) is read and written
    under a file lock, so two runs starting at the same moment can never get the same block.
    A run without ports still gets an (empty) lease: it records the owning pid, which is how
    `collect_garbage` tells a live run's containers from a crashed run's."""
    count = max(0, count)
    table = Path(layer_dir) / "agents" / ".ports.json"
    table.parent.mkdir(parents=True, exist_ok=True)
    with slots.file_lock(table.with_suffix(".lock")):
        leases = _live_leases(layer_dir, table, adw_id)
        if adw_id in leases and len(leases[adw_id]["ports"]) == count:
            leases[adw_id]["pid"] = os.getpid()      # --resume: same block, new owner process
            table.write_text(json.dumps(leases, indent=1))
            return leases[adw_id]["ports"]
        taken = {port for run, lease in leases.items() if run != adw_id for port in lease["ports"]}
        for slot_index in range(1000):
            ports = [base + slot_index * 10 + offset for offset in range(count)]
            if not taken.intersection(ports) and _bindable(ports):
                leases[adw_id] = {"ports": ports, "pid": os.getpid()}
                table.write_text(json.dumps(leases, indent=1))
                return ports
    raise LayerError(f"Could not allocate {count} ports starting at {base}")


def release_ports(layer_dir: Path, adw_id: str) -> None:
    table = Path(layer_dir) / "agents" / ".ports.json"
    if not table.exists():
        return
    with slots.file_lock(table.with_suffix(".lock")):
        leases = json.loads(table.read_text())
        if leases.pop(adw_id, None) is not None:
            table.write_text(json.dumps(leases, indent=1))


CACHE_ENV = {"uv": ["UV_CACHE_DIR"], "pip": ["PIP_CACHE_DIR"], "cargo": ["CARGO_HOME"],
             "go": ["GOMODCACHE"], "npm": ["npm_config_cache"], "pre-commit": ["PRE_COMMIT_HOME"]}
CREDENTIALS = {"claude": (".claude/.credentials.json", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]),
               "codex": (".codex/auth.json", ["OPENAI_API_KEY"])}
CONTAINER_HOME = "/tmp/home"
DEFAULT_PIDS_LIMIT = 2048
READY_POLL_SECONDS = 1.0


def _env_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", str(text)).upper()


def hardening_flags(option: dict[str, Any]) -> list[str]:
    """Flags every container the runtime starts gets (the run's container and its sidecars).

    Verified against real rootless podman (tests/test_isolation_podman.py): with
    --userns=keep-id the container user IS the host uid, so the worktree, the quarantine and the
    `:U` cache volumes are owned by it and nothing needs a capability: git add/commit, Python and
    cache writes all work with every capability dropped and no-new-privileges set. --read-only
    keeps the image immutable; /tmp is a private tmpfs (and HOME=/tmp/home lives in it, created by
    podman for the HOME variable); bind mounts and named volumes stay writable."""
    flags = ["--cap-drop=ALL", "--security-opt=no-new-privileges",
             "--pids-limit", str(int(option.get("pidsLimit") or DEFAULT_PIDS_LIMIT))]
    if option.get("readOnlyRoot"):
        flags += ["--read-only", "--tmpfs", "/tmp:rw,mode=1777"]
    return flags


SIDECAR_CAPABILITIES = "CHOWN,DAC_OVERRIDE,FOWNER,SETGID,SETUID"


def sidecar_flags(option: dict[str, Any]) -> list[str]:
    """Hardening for sidecar services. Service images (postgres, redis, …) start as root, chown
    their data directory and then drop to their own user, which the run container's flags forbid:
    under the pod's keep-id namespace they would start as the host uid and fail to initialise
    (verified with postgres:16-alpine). So a sidecar starts as root INSIDE the user namespace —
    an unprivileged sub-uid on the host — with only the five capabilities that hand-over needs.
    Everything else stays dropped, and a sidecar has no mounts at all."""
    flags = hardening_flags(option)
    flags.insert(1, f"--cap-add={SIDECAR_CAPABILITIES}")
    return ["--user", "0:0", *flags]


class EnvStack:
    def __init__(self, layer: Any, state: Any, definitions: list[dict[str, Any]]) -> None:
        self.layer = layer
        self.state = state
        self.definitions = definitions
        self.worktree_option = next((item for item in definitions if item["kind"] == "worktree"), None)
        self.host_option = next((item for item in definitions if item["kind"] == "host"), None)
        self.nix_option = next((item for item in definitions if item["kind"] == "nix-devshell"), None)
        self.podman_option = next((item for item in definitions if item["kind"] == "podman"), None)
        self.cwd: Path = layer.app_root
        self.env: dict[str, str] = {}
        self.keep_worktree = False
        self._image_ready = False
        self._container: str | None = None
        self._pod: str | None = None
        self._sandbox: gitsafe.Sandbox | None = None

    @property
    def image(self) -> str:
        return f"al-{self.layer.name}:latest"

    @property
    def sandboxed(self) -> bool:
        """Commands run in a podman container on a worktree: the container is untrusted, so the
        app's .git is mounted read-only and the host does all ref updates (see gitsafe)."""
        return bool(self.podman_option and self.worktree_option)

    def _tree_dir(self) -> Path:
        return self.layer.layer_dir / str((self.worktree_option or {}).get("dir") or "trees")

    @property
    def sandbox(self) -> gitsafe.Sandbox | None:
        """Paths + container freeze of a sandboxed run, for host-side git (None when not sandboxed)."""
        if not self.sandboxed or not self.state.worktree:
            return None
        if self._sandbox is None:
            self._sandbox = gitsafe.Sandbox.for_run(self.layer.app_root, self._tree_dir(), self.layer.layer_dir,
                                                    self.state.adw_id, self._frozen)
        return self._sandbox

    @property
    def read_only(self) -> bool:
        """True when commands run directly in the app checkout (host env), so writes are refused."""
        return self.worktree_option is None and self.host_option is not None

    def process_env(self) -> dict[str, str]:
        """os.environ plus the stack's exported variables (ADW_PORT_*)."""
        return {**os.environ, **self.env}

    @property
    def run_name(self) -> str:
        """Name of the run's container and pod; sidecars are `<run_name>-<service id>`."""
        return f"al-{self.layer.name}-{self.state.adw_id}"

    def setup(self) -> None:
        if self.podman_option:
            self._validate_podman(self.podman_option)
        if self.worktree_option:
            self._setup_worktree(self.worktree_option)
        elif self.host_option:
            self.cwd = self.layer.app_root
        if self.podman_option:
            self._export_podman(self.podman_option)

    def services(self) -> list[dict[str, Any]]:
        """The `services[]` entries the podman option lists, in its order."""
        declared = {entry.get("id"): entry for entry in self.layer.ir.get("services") or [] if isinstance(entry, dict)}
        wanted = list((self.podman_option or {}).get("services") or [])
        missing = [name for name in wanted if name not in declared]
        if missing:
            raise LayerError(f"podman option {self.podman_option.get('id')!r} lists undeclared services "
                             f"{missing}; declared: {sorted(str(key) for key in declared)}")
        for name in wanted:
            if declared[name].get("kind") != "container":
                raise LayerError(f"service {name!r}: unknown kind {declared[name].get('kind')!r}")
            if declared[name].get("status") == "planned":
                raise LayerError(f"service {name!r} has status 'planned' and is not implemented by this runtime")
        return [declared[name] for name in wanted]

    def _validate_podman(self, option: dict[str, Any]) -> None:
        """Refuse option combinations that cannot isolate a run, before anything is created."""
        publish, services = list(option.get("publish") or []), self.services()
        network = option.get("network")
        if network in ("none", "host") and (publish or services):
            what = " and ".join(item for item, present in (("publish", publish), ("services", services)) if present)
            reason = ("has no network at all, so nothing can be published and sidecars cannot be reached"
                      if network == "none" else
                      "shares the host's network namespace, so published ports and sidecar ports would collide "
                      "between runs")
            raise LayerError(f"podman option {option.get('id')!r}: network {network!r} conflicts with {what}: "
                             f"{network!r} {reason}; use network slirp")
        if publish:
            per_run = int((self.worktree_option or {}).get("portsPerRun", 0))
            if len(publish) > per_run:
                raise LayerError(f"podman option {option.get('id')!r} publishes {len(publish)} ports {publish} but the "
                                 f"worktree option leases only portsPerRun={per_run} host ports per run; raise "
                                 f"portsPerRun to at least {len(publish)}")
            if any(not isinstance(port, int) or not 0 < port < 65536 for port in publish) or len(set(publish)) != len(publish):
                raise LayerError(f"podman option {option.get('id')!r}: publish must list distinct ports 1-65535: {publish}")

    def _export_podman(self, option: dict[str, Any]) -> None:
        """ADW_PUBLISHED_<container port>=<host port> and ADW_SERVICE_<ID>=1, inside and on the host."""
        for container_port, host_port in zip(option.get("publish") or [], self.state.ports):
            self.env[f"ADW_PUBLISHED_{container_port}"] = str(host_port)
        for service in self.services():
            self.env[f"ADW_SERVICE_{_env_name(service['id'])}"] = "1"

    def _setup_worktree(self, option: dict[str, Any]) -> None:
        if self.sandboxed and option.get("sparse"):
            raise LayerError("a sparse worktree cannot be combined with a podman environment: git sparse-checkout "
                             "writes config.worktree into the worktree's admin dir, which a sandboxed run refuses")
        tree_dir = self._tree_dir()
        tree_dir.mkdir(parents=True, exist_ok=True)
        worktree = (tree_dir / self.state.adw_id).resolve()
        if self.state.worktree and Path(self.state.worktree).resolve() == worktree and worktree.is_dir():
            self.cwd = worktree  # --resume: the run's worktree and branch already exist
        else:
            branch = f"{self.state.klass}/{self.state.adw_id}-{slugify(self.state.prompt)}"
            if self.sandboxed:
                self._add_detached_worktree(worktree)
            else:
                _run_git(["git", "-C", str(self.layer.app_root), "worktree", "add", str(worktree), "-b", branch,
                          self.layer.default_branch], "environment setup")
            self.state.worktree = str(worktree)
            self.state.branch = branch
            self.cwd = worktree
            if option.get("sparse"):
                _run_git(["git", "-C", str(worktree), "sparse-checkout", "set", "--cone", *option["sparse"]],
                         "environment setup")
            self._copy_files(option, worktree)
        if self.sandboxed:
            self.sandbox.quarantine.mkdir(parents=True, exist_ok=True)
        ports = lease_ports(self.layer.layer_dir, self.state.adw_id, int(option.get("portBase", 9100)),
                            int(option.get("portsPerRun", 0)))
        self.state.ports = ports
        for index, port in enumerate(ports):
            self.env[f"ADW_PORT_{index}"] = str(port)

    def _add_detached_worktree(self, worktree: Path) -> None:
        """Sandboxed: branch refs live in the read-only common dir, so the worktree starts detached;
        refs/heads/<branch> is created by the host when it ships (gitsafe.update_ref)."""
        app_git = Path(self.layer.app_root).resolve() / ".git"
        if app_git.is_symlink() or not app_git.is_dir():
            raise LayerError(f"a podman environment needs the app's .git to be a directory: {app_git}")
        _run_git(["git", "-C", str(self.layer.app_root), *gitsafe.HARDENING, "-c", "worktree.useRelativePaths=false",
                  "worktree", "add", "--detach", str(worktree), self.layer.default_branch], "environment setup")
        admin = app_git / "worktrees" / worktree.name
        if (worktree / ".git").read_text(encoding="utf-8") != f"gitdir: {admin}\n":
            raise StepFailed("environment setup", f"git did not register the worktree as {admin} "
                                                  f"(stale admin dir? run `git worktree prune` in the app)")
        (app_git / "logs").mkdir(exist_ok=True)  # overlaid in the container so reflogs (git stash) work

    def _copy_files(self, option: dict[str, Any], worktree: Path) -> None:
        for relative in option.get("copyFiles", []):
            source = self.layer.app_root / relative
            if source.is_file():
                destination = worktree / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            elif source.is_dir():
                shutil.copytree(source, worktree / relative, dirs_exist_ok=True)
            else:
                print(f"[adw {self.state.adw_id}] copyFiles: {relative} not found in app, skipped")

    def _ensure_podman_image(self) -> None:
        if self._image_ready:
            return
        image = self.image
        try:
            exists = subprocess.run(["podman", "image", "exists", image], capture_output=True,
                                    text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepFailed("environment setup", f"podman image check failed: {exc}") from exc
        if exists.returncode == 0:
            self._image_ready = True
            return
        try:
            result = subprocess.run(["podman", "build", "-t", image, "-f", "env/Containerfile", "env/"],
                                    cwd=self.layer.layer_dir, capture_output=True, text=True,
                                    timeout=PODMAN_BUILD_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepFailed("environment setup", f"podman image build failed: {exc}") from exc
        if result.returncode:
            raise StepFailed("environment setup", result.stderr.strip() or "podman image build failed")
        self._image_ready = True

    # ------------------------------------------------------------------ podman

    def _podman_env(self) -> dict[str, str]:
        """Variables set inside the container: ports, cache locations, forwarded agent credentials."""
        option = self.podman_option or {}
        env = {**self.env, "HOME": CONTAINER_HOME}  # per container (podman creates it), never the image's or host's
        for name in option.get("caches", []):
            for variable in CACHE_ENV.get(name, [f"AL_CACHE_{name.upper().replace('-', '_')}"]):
                env[variable] = f"/caches/{name}"
        if "go" in option.get("caches", []):
            env["GOCACHE"] = "/caches/go/build"
        if option.get("agentInside"):
            for name in option.get("credentials", []):
                for variable in CREDENTIALS[name][1]:
                    if os.environ.get(variable):
                        env[variable] = os.environ[variable]
        sandbox = self.sandbox
        if sandbox is not None:  # new objects go to the run's quarantine, never into the app's store
            env["GIT_OBJECT_DIRECTORY"] = str(sandbox.quarantine)
            env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(sandbox.app_git / "objects")
        return env

    def _podman_mounts(self) -> list[str]:
        """Bind mounts, at their host paths so git works inside the container (a worktree's .git
        file points into <app>/.git/worktrees/<id>). The app's .git is read-only; only the run's
        own admin dir and object quarantine are writable, and refs/logs are overlays whose writes
        (git stash, git branch …) stay inside the container. The application's working tree
        itself is never mounted, and neither is anything of another run: with agentInside the
        layer is mounted entry by entry, leaving out agents/, artefacts/ and the worktree dir, so
        only this run's own agents/<id>, artefacts/<id> and worktree exist in the container."""
        option = self.podman_option or {}
        worktree = str(self.state.worktree)
        sandbox = self.sandbox
        if sandbox is None:
            raise LayerError("podman environment requires a worktree environment")
        git_dir = sandbox.app_git
        mounts: list[str] = []
        if option.get("agentInside"):
            layer_dir = Path(self.layer.layer_dir)
            run_dirs = [layer_dir / "specs", layer_dir / "docs", layer_dir / "experts",
                        layer_dir / "agents" / self.state.adw_id, layer_dir / "artefacts" / self.state.adw_id]
            # writable run dirs are left out of the read-only set: one destination, one mount
            # .al-improve: the layer-change workflow of `al improve` (its own runs and worktrees)
            private = [layer_dir / "agents", layer_dir / "artefacts", self._tree_dir(), layer_dir / IMPROVE_DIR,
                       *run_dirs]
            for entry in _shared_layer_entries(layer_dir, private):
                mounts += ["-v", f"{entry}:{entry}:ro"]
            for directory in run_dirs:
                directory.mkdir(parents=True, exist_ok=True)
                mounts += ["-v", f"{directory}:{directory}"]
            for name in option.get("credentials", []):
                copy = self._sync_credential(name)
                if copy is not None:
                    mounts += ["-v", f"{copy}:{CONTAINER_HOME}/{CREDENTIALS[name][0]}:ro"]
        mounts += ["-v", f"{worktree}:{worktree}", "-v", f"{git_dir}:{git_dir}:ro"]
        for name in ("refs", "logs", "reftable"):
            if (git_dir / name).is_dir() and not (git_dir / name).is_symlink():
                mounts += ["-v", f"{git_dir / name}:{git_dir / name}:O"]
        sandbox.quarantine.mkdir(parents=True, exist_ok=True)
        mounts += ["-v", f"{sandbox.admin}:{sandbox.admin}", "-v", f"{sandbox.quarantine}:{sandbox.quarantine}"]
        for name in option.get("caches", []):
            mounts += ["-v", f"al-cache-{name}:/caches/{name}:U"]
        return mounts

    def _labels(self) -> list[str]:
        """Ownership labels: garbage collection only ever touches resources of this layer directory."""
        return ["--label", f"al.layer={self.layer.name}", "--label", f"al.layer-dir={Path(self.layer.layer_dir).resolve()}",
                "--label", f"al.run={self.state.adw_id}"]

    def _network_flags(self) -> list[str]:
        """Network namespace + published ports. Each run (scope run: its container, or its pod
        when it has sidecars) has its own network namespace, so the app binds its usual ports
        and runs never collide; only `publish`ed ports reach the host, on 127.0.0.1 at the run's
        leased host ports."""
        option = self.podman_option or {}
        if option.get("network") not in ("none", "host", "slirp"):
            raise LayerError(f"Unknown podman network: {option.get('network')}")
        # "slirp" = podman's default rootless user-mode network (pasta or slirp4netns), so no flag.
        network = [] if option["network"] == "slirp" else ["--network", option["network"]]
        publish = [flag for container_port, host_port in zip(option.get("publish") or [], self.state.ports)
                   for flag in ("-p", f"127.0.0.1:{host_port}:{container_port}")]
        return ["--userns=keep-id", *network, *publish]

    def _podman_base(self) -> list[str]:
        option = self.podman_option or {}
        # In a pod the user namespace, network and published ports belong to the pod.
        namespace = ["--pod", self._pod] if self._pod else self._network_flags()
        return [*namespace, "--security-opt", "label=disable", *hardening_flags(option), *self._labels(),
                "--memory", str(option["memory"]), "--cpus", str(option["cpus"]),
                "-w", str(self.state.worktree), *self._podman_mounts()]

    def _ensure_pod(self) -> str | None:
        """With sidecar services: one pod per run (`al-<layer>-<adw_id>`) holding the sidecars and
        the run's container, so they share localhost and nothing else. Sidecars get no mounts at
        all (no worktree, no .git, no layer, no caches, no credentials), only their declared env.
        Each sidecar must pass its readyCmd before the run's container starts. On any failure the
        whole pod is removed before StepFailed propagates."""
        services = self.services()
        if not services or self._pod:
            return self._pod
        option = self.podman_option or {}
        pod = self.run_name
        self._remove_pod(pod)  # a crashed earlier attempt of this run (--resume)
        self._pod = pod
        try:
            _podman_checked(["pod", "create", "--name", pod, "--infra-name", f"{pod}-infra", *self._labels(),
                             *self._network_flags()], "podman pod create failed")
            print(f"[adw {self.state.adw_id}] pod created: {pod}")
            for service in services:
                name = f"{pod}-{service['id']}"
                env = [flag for item in service.get("env") or [] for flag in ("-e", str(item))]
                command = shlex.split(str(service.get("command") or ""))
                _podman_checked(["run", "-d", "--pod", pod, "--name", name, "--security-opt", "label=disable",
                                 *sidecar_flags(option), *self._labels(), "--memory", str(option["memory"]),
                                 "--cpus", str(option["cpus"]), *env, str(service["image"]), *command],
                                f"sidecar {service['id']} failed to start")
                self._wait_ready(service, name)
                print(f"[adw {self.state.adw_id}] sidecar ready: {name}")
        except BaseException:
            self._remove_pod(pod)
            self._pod = None
            raise
        return pod

    def _wait_ready(self, service: dict[str, Any], name: str) -> None:
        ready = str(service.get("readyCmd") or "")
        if not ready:
            return
        timeout = float(service.get("readyTimeout") or 60)
        deadline = time.monotonic() + timeout
        while True:
            try:
                remaining = max(1.0, deadline - time.monotonic())
                probe = subprocess.run(["podman", "exec", name, "sh", "-c", ready], capture_output=True, text=True,
                                       timeout=min(30.0, remaining), check=False)
                if probe.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(READY_POLL_SECONDS)
        logs = ""
        try:
            result = subprocess.run(["podman", "logs", "--tail", "20", name], capture_output=True, text=True,
                                    timeout=60, check=False)
            logs = "\n".join(line for line in (result.stdout + result.stderr).splitlines()[-20:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            logs = f"(podman logs failed: {exc})"
        raise StepFailed("environment setup", f"sidecar {service['id']} not ready after {timeout:g}s "
                                              f"(readyCmd {ready!r}); last log lines:\n{logs or '(none)'}")

    def _remove_pod(self, pod: str) -> None:
        """Sidecars first with -v (their anonymous volumes, e.g. a database's data dir, die with
        them), then the pod with everything left in it."""
        for service in (self.podman_option or {}).get("services") or []:
            _podman_ok(["rm", "-f", "-v", "-t", "0", f"{pod}-{service}"])
        _podman_ok(["pod", "rm", "-f", "-t", "0", pod])

    def _ensure_container(self) -> str:
        """scope = run: one long-lived container per ADW run (in the run's pod when it has
        sidecars); steps exec into it."""
        if self._container:
            return self._container
        self._ensure_pod()
        name = self.run_name
        subprocess.run(["podman", "rm", "-f", "-t", "0", name], capture_output=True, text=True, timeout=120, check=False)
        env_flags = ["-e", f"HOME={CONTAINER_HOME}"]
        try:
            # --init: a small init is process 1 and collects orphaned processes. Without it every
            # process whose parent exited stays a zombie for the whole run, and a long run with test
            # suites reaches the process limit ("cannot spawn thread") hours in.
            result = subprocess.run(["podman", "run", "-d", "--init", "--name", name, *self._podman_base(), *env_flags,
                                     self.image, "sleep", "infinity"], capture_output=True, text=True, timeout=300,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepFailed("environment setup", f"podman container start failed: {exc}") from exc
        if result.returncode:
            raise StepFailed("environment setup", result.stderr.strip() or "podman container start failed")
        self._container = name
        events.say(f"[adw {self.state.adw_id}] container started: {name}", "env.container.start", name=name)
        return name

    def _podman_argv(self, argv: list[str], interactive: bool = False) -> list[str]:
        option = self.podman_option or {}
        if not self.state.worktree:
            raise LayerError("podman environment requires a worktree environment")
        self._ensure_podman_image()
        env_flags = [flag for key, value in self._podman_env().items() for flag in ("-e", f"{key}={value}")]
        stdin = ["-i"] if interactive else []
        if option.get("scope", "run") == "run":
            return ["podman", "exec", *stdin, "-w", str(self.state.worktree), *env_flags,
                    self._ensure_container(), *argv]
        self._ensure_pod()
        return ["podman", "run", "--rm", *stdin, *self._podman_base(), *env_flags, self.image, *argv]

    def _nix_argv(self) -> list[str]:
        argv = ["nix", "develop", f"path:{self.layer.layer_dir / 'env'}"]
        if (self.nix_option or {}).get("pure"):
            argv.extend(["--ignore-environment", "--keep", "HOME"])
        return [*argv, "--command"]

    def wrap(self, cmd: str) -> list[str]:
        """argv that runs a codebase command in the innermost environment of the stack."""
        if self.podman_option:
            return self._podman_argv(["bash", "-c", cmd])
        if self.nix_option:
            return [*self._nix_argv(), "bash", "-c", cmd]
        return ["bash", "-c", cmd]

    @property
    def agent_inside(self) -> bool:
        return bool(self.podman_option and self.podman_option.get("agentInside"))

    def _sync_credential(self, name: str) -> Path | None:
        """The run's own copy of a credential file, brought up to date with the host's. The container
        mounts this copy, not the host's file: a file mount pins the file it was given, and the agent
        CLI on the host replaces its credential file when it renews the login, which revokes the old
        one. A run of several hours would then keep a revoked login. The copy is rewritten in place
        (same file, so the mount sees it) before every agent call."""
        source = Path.home() / CREDENTIALS[name][0]
        if not source.is_file():
            return None
        copy = Path(self.layer.layer_dir) / "agents" / str(self.state.adw_id) / "credentials" / f"{name}.json"
        try:
            data = source.read_bytes()
            if not copy.is_file():
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.touch(mode=0o600)
            if copy.read_bytes() != data:
                with open(copy, "r+b") as handle:   # in place: never a new file
                    handle.seek(0)
                    handle.write(data)
                    handle.truncate()
        except OSError as exc:
            raise StepFailed("environment setup", f"credential {name}: {exc}") from exc
        return copy

    def wrap_agent(self, argv: list[str]) -> list[str]:
        """argv that runs an agent CLI: inside the container when the podman option says so
        (the agent itself is then sandboxed), else under the nix dev shell, else on the host."""
        if self.agent_inside:
            names = list((self.podman_option or {}).get("credentials", []))
            for name in names:
                self._sync_credential(name)
            if names and getattr(self, "_credential_follower", None) is None:
                # an agent call can last hours and the host's login can be renewed in the middle of it:
                # the CLI in the container reads the file again when its login is refused, so the copy
                # has to follow the host's file during the call too, not only before it
                def follow() -> None:
                    while not self._credential_stop.wait(CREDENTIAL_FOLLOW_SECONDS):
                        for name in names:
                            try:
                                self._sync_credential(name)
                            except StepFailed:
                                pass
                self._credential_stop = threading.Event()
                self._credential_follower = threading.Thread(target=follow, name="al-credentials", daemon=True)
                self._credential_follower.start()
            return self._podman_argv(argv, interactive=True)
        if self.nix_option:
            return [*self._nix_argv(), *argv]
        return argv

    @contextlib.contextmanager
    def _frozen(self) -> Iterator[None]:
        """No process of the run may touch the worktree's git metadata while the host runs git on
        it: pause the run-scoped container (a check can leave background processes behind). If it
        cannot be paused it is removed instead (recreated on next use). scope=command containers
        never outlive their command, so there is nothing to freeze then.

        Sidecars are deliberately NOT paused: they have no mount of the worktree, its admin dir,
        the quarantine or the app's .git (see _ensure_pod), so they cannot touch what the host
        verifies; pausing the run's own container is sufficient. Unpause always runs in the
        finally below; if it fails the container is removed so it can never stay frozen."""
        name = self._container
        if not name:
            yield
            return
        paused = _podman_ok(["pause", name])
        if not paused:
            if not _podman_ok(["rm", "-f", name]):
                raise StepFailed(gitsafe.INTEGRITY, f"cannot pause or remove container {name} before host git")
            events.say(f"[adw {self.state.adw_id}] container {name} could not be paused; removed it instead",
                       "env.container.stop", name=name)
            self._container = None
        try:
            yield
        finally:
            if paused and not _podman_ok(["unpause", name]):
                _podman_ok(["rm", "-f", name])
                self._container = None

    # ------------------------------------------------------------------ teardown / cleanup

    def teardown(self, success: bool) -> None:
        if getattr(self, "_credential_follower", None) is not None:
            self._credential_stop.set()
            self._credential_follower = None
        if self._container:
            subprocess.run(["podman", "rm", "-f", "-t", "0", self._container], capture_output=True, text=True,
                           timeout=120, check=False)
            events.say(f"[adw {self.state.adw_id}] container removed: {self._container}", "env.container.stop",
                       name=self._container)
            self._container = None
        if self._pod:  # always, also on failure and when the worktree is kept: sidecars hold no work
            self._remove_pod(self._pod)
            print(f"[adw {self.state.adw_id}] pod removed: {self._pod}")
            self._pod = None
        if not self.worktree_option or not self.state.worktree:
            return
        release_ports(self.layer.layer_dir, self.state.adw_id)
        keep = self.worktree_option.get("keep")
        keeping = self.keep_worktree or keep == "always" or (keep == "on-failure" and not success)
        if self.sandboxed:
            self._teardown_sandboxed(keeping)
            return
        if keeping:
            return
        _run_git(["git", "-C", str(self.layer.app_root), "worktree", "remove", "--force", self.state.worktree], "environment teardown")
        print(f"[adw {self.state.adw_id}] worktree removed: {self.state.worktree}")
        if self.worktree_option.get("deleteMergedBranches", True) and self.state.branch:
            if delete_branch_if_merged(self.layer, self.state.branch):
                print(f"[adw {self.state.adw_id}] branch deleted (nothing unshipped on it): {self.state.branch}")

    def _teardown_sandboxed(self, keeping: bool) -> None:
        """The container is gone, so nothing can race us. The run's work (its HEAD) is published
        to its branch first, so removing the worktree and the quarantine never loses commits; a
        tampered worktree raises git-integrity and is left in place for inspection."""
        sandbox = self.sandbox
        if not sandbox.worktree.is_dir():
            gitsafe.remove_quarantine(sandbox)
            return
        if self.state.branch:
            publish_run_branch(sandbox, self.state.branch, self.state.adw_id, "environment teardown")
        # else: a branch-less run (`al verify` on a detached worktree) has nothing to publish
        if keeping:
            return  # the kept worktree's commits live in the quarantine: keep it too
        _run_git(["git", "-C", str(self.layer.app_root), *gitsafe.HARDENING, "worktree", "remove", "--force",
                  str(sandbox.worktree)], "environment teardown")
        gitsafe.remove_quarantine(sandbox)
        print(f"[adw {self.state.adw_id}] worktree removed: {self.state.worktree}")
        if self.worktree_option.get("deleteMergedBranches", True) and self.state.branch:
            if delete_branch_if_merged(self.layer, self.state.branch):
                print(f"[adw {self.state.adw_id}] branch deleted (nothing unshipped on it): {self.state.branch}")


def _podman_ok(args: list[str]) -> bool:
    try:
        return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=120,
                              check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _podman_checked(args: list[str], what: str) -> str:
    try:
        result = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed("environment setup", f"{what}: {exc}") from exc
    if result.returncode:
        raise StepFailed("environment setup", f"{what}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def _shared_layer_entries(layer_dir: Path, private: list[Path]) -> list[Path]:
    """The layer's entries an agent inside a container may read: everything except the private
    paths (other runs' state, artefacts and worktrees) and symlinks (a bind mount would follow
    them out of the layer). Directories that merely contain a private path are descended into."""
    private = [Path(path).resolve() for path in private]
    entries: list[Path] = []

    def visit(directory: Path) -> None:
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink() or entry.resolve() in private:
                continue
            if entry.is_dir() and any(entry.resolve() in path.parents for path in private):
                visit(entry)
            elif entry.is_dir() or entry.is_file():
                entries.append(entry)

    visit(Path(layer_dir).resolve())
    return entries


_RUN_RESOURCE = re.compile(r"(?P<run>[0-9a-fA-F]{8})(?:-.+)?")


def _run_alive(layer_dir: Path, adw_id: str, leases: dict[str, Any]) -> bool:
    """A run owns live resources only while its state says running AND the process holding its
    lease is alive (a crashed or killed run leaves status=running behind)."""
    state_file = Path(layer_dir) / "agents" / adw_id / "state.json"
    try:
        status = json.loads(state_file.read_text()).get("status")
    except (OSError, ValueError, AttributeError):
        return False
    lease = leases.get(adw_id)
    if isinstance(lease, list):
        lease = {"ports": lease, "pid": 0}
    return status == "running" and isinstance(lease, dict) and bool(lease.get("pid")) and _alive(int(lease["pid"]))


def _podman_json(args: list[str]) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return [item for item in data or [] if isinstance(item, dict)]


def collect_stale_containers(layer: Any) -> list[str]:
    """Remove containers and pods `al-<layer>-<adw_id>[-…]` of THIS layer whose run is dead
    (state.json missing, status not running, or the pid of its port lease is dead).

    Ownership is decided by the `al.layer-dir` label the runtime puts on everything it starts,
    so two layers with the same name (or `al-foo` vs `al-foo-bar`) never touch each other's
    resources. An unlabelled resource (started by an older runtime) is only taken when its run
    directory exists in this layer."""
    if not any(isinstance(entry, dict) and entry.get("kind") == "podman" for entry in layer.ir.get("environments") or []):
        return []
    layer_dir = Path(layer.layer_dir).resolve()
    prefix = f"al-{layer.name}-"
    table = layer_dir / "agents" / ".ports.json"
    leases: dict[str, Any] = {}
    if table.exists():
        with slots.file_lock(table.with_suffix(".lock")):
            try:
                leases = json.loads(table.read_text() or "{}")
            except ValueError:
                leases = {}

    def stale(name: str, labels: dict[str, Any] | None) -> bool:
        if not name.startswith(prefix):
            return False
        match = _RUN_RESOURCE.fullmatch(name[len(prefix):])
        if not match:
            return False
        run = match.group("run")
        owner = (labels or {}).get("al.layer-dir")
        if owner is not None:
            if owner != str(layer_dir) or (labels or {}).get("al.run") != run:
                return False
        elif not (layer_dir / "agents" / run).is_dir():
            return False
        return not _run_alive(layer_dir, run, leases)

    removed: list[str] = []
    for pod in _podman_json(["pod", "ps", "--format", "json", "--filter", f"name=^{re.escape(prefix)}"]):
        name = str(pod.get("Name") or "")
        if stale(name, pod.get("Labels")) and _podman_ok(["pod", "rm", "-f", "-t", "0", name]):
            removed.append(name)
    for container in _podman_json(["ps", "-a", "--format", "json", "--filter", f"name=^{re.escape(prefix)}"]):
        names = container.get("Names") or []
        name = str(names[0] if isinstance(names, list) and names else names or "")
        if name and stale(name, container.get("Labels")) and _podman_ok(["rm", "-f", "-v", "-t", "0", name]):
            removed.append(name)
    return removed


def publish_run_branch(sandbox: gitsafe.Sandbox, branch: str, adw_id: str, step: str) -> str:
    """Host side: import the sandboxed worktree's HEAD into the app and create / fast-forward
    refs/heads/<branch> to it. Returns the commit."""
    check_run_branch(branch, adw_id, step)
    commit = gitsafe.import_objects(sandbox, "HEAD", step)
    gitsafe.update_ref(sandbox, f"refs/heads/{branch}", commit, step, f"adw {adw_id}: run branch")
    return commit


def delete_branch_if_merged(layer: Any, branch: str) -> bool:
    """Delete a run's branch when every commit on it is already on the default or al/ship branch.
    Only refs change; the application's working tree is not involved."""
    app = str(layer.app_root)
    for target in (layer.default_branch, f"al/ship/{layer.default_branch}"):
        exists = subprocess.run(["git", "-C", app, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}"],
                                capture_output=True, text=True, timeout=120, check=False)
        if exists.returncode != 0:
            continue
        merged = subprocess.run(["git", "-C", app, "merge-base", "--is-ancestor", f"refs/heads/{branch}",
                                 f"refs/heads/{target}"], capture_output=True, text=True, timeout=120, check=False)
        if merged.returncode == 0:
            deleted = subprocess.run(["git", "-C", app, "branch", "-D", branch], capture_output=True, text=True,
                                     timeout=120, check=False)
            return deleted.returncode == 0
    return False


def collect_garbage(layer: Any, option: dict[str, Any], now: float | None = None) -> list[str]:
    """Remove containers / pods of dead runs of this layer (always), and kept worktrees of
    finished runs older than gcDays (0 = never; running runs are skipped). Returns the removed
    worktrees; removed containers and pods are reported on stdout."""
    for name in collect_stale_containers(layer):
        print(f"[adw] gc: removed stale container/pod {name}")
    days = int(option.get("gcDays", 0))
    tree_dir = Path(layer.layer_dir) / str(option.get("dir") or "trees")
    if days <= 0 or not tree_dir.is_dir():
        return []
    cutoff = (now or time.time()) - days * 86400
    removed: list[str] = []
    for worktree in sorted(tree_dir.iterdir()):
        state_file = Path(layer.layer_dir) / "agents" / worktree.name / "state.json"
        if not worktree.is_dir() or worktree.stat().st_mtime > cutoff:
            continue
        status, branch = "unknown", ""
        if state_file.exists():
            data = json.loads(state_file.read_text())
            status, branch = str(data.get("status")), str(data.get("branch") or "")
        if status == "running" and state_file.stat().st_mtime > cutoff:
            continue
        sandbox = gitsafe.Sandbox.for_run(layer.app_root, tree_dir, layer.layer_dir, worktree.name)
        if sandbox.quarantine.is_dir():  # a sandboxed run: its commits live only in the quarantine
            _podman_ok(["rm", "-f", f"al-{layer.name}-{worktree.name}"])  # a crashed run's container
            try:
                publish_run_branch(sandbox, branch, worktree.name, "gc")
            except StepFailed as exc:
                print(f"[adw] gc: kept {worktree.name}: {exc}")
                continue
        result = subprocess.run(["git", "-C", str(layer.app_root), "worktree", "remove", "--force", str(worktree)],
                                capture_output=True, text=True, timeout=120, check=False)
        if result.returncode == 0:
            gitsafe.remove_quarantine(sandbox)
            removed.append(worktree.name)
            if branch and option.get("deleteMergedBranches", True):
                delete_branch_if_merged(layer, branch)
    if removed:
        subprocess.run(["git", "-C", str(layer.app_root), "worktree", "prune"], capture_output=True, text=True,
                       timeout=120, check=False)
    return removed


def build(layer: Any, state: Any, environment_ids: list[str]) -> EnvStack:
    definitions = []
    for option_id in environment_ids:
        option = layer.option("environments", option_id)
        kind = option.get("kind")
        if kind not in {"worktree", "host", "nix-devshell", "podman", "home-manager"}:
            raise LayerError(f"Unknown environment kind: {kind}")
        if kind != "home-manager":
            definitions.append(option)
    if sum(item["kind"] == "worktree" for item in definitions) > 1:
        raise LayerError("Environment stack may contain only one worktree")
    return EnvStack(layer, state, definitions)
