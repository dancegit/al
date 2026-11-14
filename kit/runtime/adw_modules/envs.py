from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

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


def _free_port_block(base: int, count: int) -> list[int]:
    for slot in range(1000):
        ports = [base + slot * 10 + offset for offset in range(count)]
        sockets: list[socket.socket] = []
        try:
            for port in ports:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(probe)
                probe.bind(("127.0.0.1", port))
            return ports
        except OSError:
            continue
        finally:
            for probe in sockets:
                probe.close()
    raise LayerError(f"Could not allocate {count} ports starting at {base}")


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
            self._copy_files(option, worktree)
        ports = _free_port_block(int(option.get("portBase", 9100)), int(option.get("portsPerRun", 0)))
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

    def wrap(self, cmd: str) -> list[str]:
        if self.podman_option:
            option = self.podman_option
            if not self.state.worktree:
                raise LayerError("podman environment requires a worktree environment")
            self._ensure_podman_image()
            network = {"none": "none", "host": "host", "slirp": "slirp4netns"}.get(option.get("network"))
            if network is None:
                raise LayerError(f"Unknown podman network: {option.get('network')}")
            argv = ["podman", "run", "--rm", "--userns=keep-id", "-v", f"{self.state.worktree}:/work:Z",
                    "-w", "/work", "--network", network, "--memory", str(option["memory"]),
                    "--cpus", str(option["cpus"])]
            for key, value in self.env.items():
                argv.extend(["-e", f"{key}={value}"])
            return [*argv, self.image, "bash", "-c", cmd]
        if self.nix_option:
            argv = ["nix", "develop", str(self.layer.layer_dir / "env")]
            if self.nix_option.get("pure"):
                argv.extend(["--ignore-environment", "--keep", "HOME"])
            return [*argv, "--command", "bash", "-c", cmd]
        return ["bash", "-c", cmd]

    def wrap_agent(self, argv: list[str]) -> list[str]:
        """Wrap an agent CLI in nix only when it is configured to run there."""
        if self.nix_option and not (self.podman_option and self.podman_option.get("agentInside")):
            result = ["nix", "develop", str(self.layer.layer_dir / "env")]
            if self.nix_option.get("pure"):
                result.extend(["--ignore-environment", "--keep", "HOME"])
            return [*result, "--command", *argv]
        return argv

    def teardown(self, success: bool) -> None:
        if not self.worktree_option or not self.state.worktree:
            return
        keep = self.worktree_option.get("keep")
        if self.keep_worktree or keep == "always" or (keep == "on-failure" and not success):
            return
        _run_git(["git", "-C", str(self.layer.app_root), "worktree", "remove", "--force", self.state.worktree], "environment teardown")
        print(f"[adw {self.state.adw_id}] worktree removed: {self.state.worktree}")


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
