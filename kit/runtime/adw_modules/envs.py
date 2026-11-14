from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from . import slots
from .errors import LayerError, StepFailed

PODMAN_BUILD_TIMEOUT = 1800


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


def lease_ports(layer_dir: Path, adw_id: str, base: int, count: int) -> list[int]:
    """Reserve a port block for a run. The lease table (agents/.ports.json) is read and written
    under a file lock, so two runs starting at the same moment can never get the same block."""
    if count <= 0:
        return []
    table = Path(layer_dir) / "agents" / ".ports.json"
    with slots.file_lock(table.with_suffix(".lock")):
        leases: dict[str, list[int]] = json.loads(table.read_text()) if table.exists() else {}
        leases = {run: ports for run, ports in leases.items()
                  if run == adw_id or (Path(layer_dir) / "agents" / run / "state.json").exists()}
        if adw_id in leases and len(leases[adw_id]) == count:
            return leases[adw_id]
        taken = {port for run, ports in leases.items() if run != adw_id for port in ports}
        for slot_index in range(1000):
            ports = [base + slot_index * 10 + offset for offset in range(count)]
            if not taken.intersection(ports) and _bindable(ports):
                leases[adw_id] = ports
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

    @property
    def image(self) -> str:
        return f"al-{self.layer.name}:latest"

    @property
    def read_only(self) -> bool:
        """True when commands run directly in the app checkout (host env), so writes are refused."""
        return self.worktree_option is None and self.host_option is not None

    def process_env(self) -> dict[str, str]:
        """os.environ plus the stack's exported variables (ADW_PORT_*)."""
        return {**os.environ, **self.env}

    def setup(self) -> None:
        if self.worktree_option:
            self._setup_worktree(self.worktree_option)
        elif self.host_option:
            self.cwd = self.layer.app_root

    def _setup_worktree(self, option: dict[str, Any]) -> None:
        tree_dir = self.layer.layer_dir / str(option.get("dir") or "trees")
        tree_dir.mkdir(parents=True, exist_ok=True)
        worktree = (tree_dir / self.state.adw_id).resolve()
        if self.state.worktree and Path(self.state.worktree).resolve() == worktree and worktree.is_dir():
            self.cwd = worktree  # --resume: the run's worktree and branch already exist
        else:
            branch = f"{self.state.klass}/{self.state.adw_id}-{slugify(self.state.prompt)}"
            _run_git(["git", "-C", str(self.layer.app_root), "worktree", "add", str(worktree), "-b", branch,
                      self.layer.default_branch], "environment setup")
            self.state.worktree = str(worktree)
            self.state.branch = branch
            self.cwd = worktree
            if option.get("sparse"):
                _run_git(["git", "-C", str(worktree), "sparse-checkout", "set", "--cone", *option["sparse"]],
                         "environment setup")
            self._copy_files(option, worktree)
        ports = lease_ports(self.layer.layer_dir, self.state.adw_id, int(option.get("portBase", 9100)),
                            int(option.get("portsPerRun", 0)))
        self.state.ports = ports
        for index, port in enumerate(ports):
            self.env[f"ADW_PORT_{index}"] = str(port)

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
        env = dict(self.env)
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
        return env

    def _podman_mounts(self) -> list[str]:
        """Bind mounts. The worktree and the app's .git are mounted at their host paths so git works
        inside the container (a worktree's .git file points into <app>/.git/worktrees/<id>). The
        application's working tree itself is never mounted."""
        option = self.podman_option or {}
        worktree = str(self.state.worktree)
        git_dir = str(Path(self.layer.app_root) / ".git")
        mounts: list[str] = []
        if option.get("agentInside"):
            layer_dir = Path(self.layer.layer_dir)
            mounts += ["-v", f"{layer_dir}:{layer_dir}:ro"]
            run_dirs = [layer_dir / "specs", layer_dir / "docs", layer_dir / "experts",
                        layer_dir / "agents" / self.state.adw_id, layer_dir / "artefacts" / self.state.adw_id]
            for directory in run_dirs:
                directory.mkdir(parents=True, exist_ok=True)
                mounts += ["-v", f"{directory}:{directory}"]
            for name in option.get("credentials", []):
                source = Path.home() / CREDENTIALS[name][0]
                if source.is_file():
                    mounts += ["-v", f"{source}:{CONTAINER_HOME}/{CREDENTIALS[name][0]}:ro"]
        mounts += ["-v", f"{worktree}:{worktree}", "-v", f"{git_dir}:{git_dir}"]
        for name in option.get("caches", []):
            mounts += ["-v", f"al-cache-{name}:/caches/{name}:U"]
        return mounts

    def _podman_base(self) -> list[str]:
        option = self.podman_option or {}
        if option.get("network") not in ("none", "host", "slirp"):
            raise LayerError(f"Unknown podman network: {option.get('network')}")
        # "slirp" = podman's default rootless user-mode network (pasta or slirp4netns), so no flag.
        network = [] if option["network"] == "slirp" else ["--network", option["network"]]
        return ["--userns=keep-id", "--security-opt", "label=disable", *network,
                "--memory", str(option["memory"]), "--cpus", str(option["cpus"]),
                "-w", str(self.state.worktree), *self._podman_mounts()]

    def _ensure_container(self) -> str:
        """scope = run: one long-lived container per ADW run; steps exec into it."""
        if self._container:
            return self._container
        name = f"al-{self.layer.name}-{self.state.adw_id}"
        subprocess.run(["podman", "rm", "-f", name], capture_output=True, text=True, timeout=120, check=False)
        try:
            result = subprocess.run(["podman", "run", "-d", "--name", name, *self._podman_base(), self.image,
                                     "sleep", "infinity"], capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepFailed("environment setup", f"podman container start failed: {exc}") from exc
        if result.returncode:
            raise StepFailed("environment setup", result.stderr.strip() or "podman container start failed")
        self._container = name
        print(f"[adw {self.state.adw_id}] container started: {name}")
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

    def wrap_agent(self, argv: list[str]) -> list[str]:
        """argv that runs an agent CLI: inside the container when the podman option says so
        (the agent itself is then sandboxed), else under the nix dev shell, else on the host."""
        if self.agent_inside:
            return self._podman_argv(argv, interactive=True)
        if self.nix_option:
            return [*self._nix_argv(), *argv]
        return argv

    # ------------------------------------------------------------------ teardown / cleanup

    def teardown(self, success: bool) -> None:
        if self._container:
            subprocess.run(["podman", "rm", "-f", self._container], capture_output=True, text=True,
                           timeout=120, check=False)
            print(f"[adw {self.state.adw_id}] container removed: {self._container}")
            self._container = None
        if not self.worktree_option or not self.state.worktree:
            return
        release_ports(self.layer.layer_dir, self.state.adw_id)
        keep = self.worktree_option.get("keep")
        if self.keep_worktree or keep == "always" or (keep == "on-failure" and not success):
            return
        _run_git(["git", "-C", str(self.layer.app_root), "worktree", "remove", "--force", self.state.worktree], "environment teardown")
        print(f"[adw {self.state.adw_id}] worktree removed: {self.state.worktree}")
        if self.worktree_option.get("deleteMergedBranches", True) and self.state.branch:
            if delete_branch_if_merged(self.layer, self.state.branch):
                print(f"[adw {self.state.adw_id}] branch deleted (nothing unshipped on it): {self.state.branch}")


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
    """Remove kept worktrees of finished runs older than gcDays (0 = never). Running runs are skipped."""
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
        result = subprocess.run(["git", "-C", str(layer.app_root), "worktree", "remove", "--force", str(worktree)],
                                capture_output=True, text=True, timeout=120, check=False)
        if result.returncode == 0:
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
