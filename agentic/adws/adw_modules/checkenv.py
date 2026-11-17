"""`(check NAME #:cmd C #:in ENV)`: a check that runs in another environment option.

The check's command runs in the run's worktree inside ENV (a podman option, e.g. one with
#:nested-engine #t, or a nix-devshell option) instead of the ADW's own stack, which stays the
default. The same environment is used by the review's re-checks (they go through
steps.run_check) and by the gate's re-run of the same cmd (steps.run_logged, phase "gate").

A CheckStack is an envs.EnvStack that shares the run's worktree, cwd, exported variables and git
Sandbox with the run's own stack (the protected envs.py is not changed). It differs only in its
name (`<run>-<env>`), an extra `al.check-env=<env>` label and scope `command`: one
`podman run --rm` per invocation, so nothing of it is running while the host runs git. It relaxes
nothing: hardening, mounts and limits are EnvStack's own.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any

from . import envs, events, nested_engine
from .errors import LayerError, StepFailed


def target(adw: dict[str, Any] | None, step: dict[str, Any]) -> str | None:
    """The environment id a check step runs in, or None for the ADW's own stack."""
    env_id = str(step.get("in") or "")
    if not env_id or env_id in list((adw or {}).get("environment") or []):
        return None
    return env_id


class CheckStack(envs.EnvStack):
    def __init__(self, layer: Any, state: Any, primary: envs.EnvStack, env_id: str) -> None:
        option = dict(layer.option("environments", env_id))
        kind = option.get("kind")
        if kind not in ("podman", "nix-devshell"):
            raise LayerError(f"check environment {env_id!r} is a {kind!r} environment; a check runs in the run's "
                             f"worktree inside a podman or nix-devshell environment")
        if primary.worktree_option is None:
            raise LayerError(f"check environment {env_id!r} needs the run's worktree environment")
        if kind == "podman":
            option["scope"] = "command"  # nothing of it runs while the host runs git
        super().__init__(layer, state, [primary.worktree_option, option])
        self.primary = primary
        self.env_id = env_id
        self.cwd = primary.cwd
        self.env = primary.env            # the same dict: ports, ADW_* exports
        self._sandbox = primary.sandbox   # the run's git Sandbox (None: one of its own, see EnvStack.sandbox)
        if kind == "podman":
            self._validate_podman(option)

    @property
    def run_name(self) -> str:
        return f"{super().run_name}-{self.env_id}"

    def _labels(self) -> list[str]:
        return super()._labels() + ["--label", f"al.check-env={self.env_id}"]

    def process_env(self) -> dict[str, str]:
        env = super().process_env()
        sandbox = self.primary.sandbox
        if self.podman_option is None and sandbox is not None:
            # a check on the host of a sandboxed run: git in the worktree resolves the run's commits
            # exactly as it does inside the run's container (envs._podman_env)
            env["GIT_OBJECT_DIRECTORY"] = str(sandbox.quarantine)
            env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(sandbox.app_git / "objects")
        return env

    def setup(self) -> None:  # the worktree already exists: it is the run's
        return None

    def teardown(self, success: bool) -> None:  # never the run's worktree: only what this stack started
        self.close()

    def close(self) -> None:
        """Remove every container of this check environment (one that outlived a timed-out command
        too) and its pod. Never raises; failures are printed."""
        if self.podman_option is None:
            return
        try:
            listed = subprocess.run(
                ["podman", "ps", "-aq", "--filter", f"label=al.run={self.state.adw_id}",
                 "--filter", f"label=al.check-env={self.env_id}"],
                capture_output=True, text=True, timeout=120, check=False)
            for ident in (listed.stdout or "").split() if listed.returncode == 0 else []:
                if not envs._podman_ok(["rm", "-f", "-t", "0", ident]):
                    print(f"[adw {self.state.adw_id}] check environment {self.env_id}: could not remove container {ident}")
            if self._pod:
                self._remove_pod(self._pod)
                self._pod = None
        except Exception as exc:  # noqa: BLE001 — cleanup reports, it never masks the run's result
            print(f"[adw {self.state.adw_id}] check environment {self.env_id}: cleanup failed: {exc}")


_open: dict[tuple[str, str], CheckStack] = {}


def stack_for(layer: Any, state: Any, step: dict[str, Any], primary: envs.EnvStack) -> envs.EnvStack:
    """The stack a check step's command runs in: the run's own, or the cached CheckStack of its #:in."""
    env_id = target(layer.adw(state.adw), step)
    if env_id is None:
        return primary
    key = (str(state.adw_id), env_id)
    if key not in _open:
        stack = CheckStack(layer, state, primary, env_id)
        nested_engine.prepare(layer, state, stack.podman_option)
        _open[key] = stack
    return _open[key]


def check_for_cmd(layer: Any, state: Any, cmd_name: str) -> dict[str, Any] | None:
    """The first check step of the run's ADW that runs `cmd_name`."""
    for step in layer.adw(state.adw).get("steps") or []:
        if step.get("kind") == "check" and str(step.get("cmd")) == str(cmd_name):
            return step
    return None


def preflight(layer: Any, state: Any, adw: dict[str, Any], primary: envs.EnvStack) -> None:
    """Check every podman environment a check of this ADW runs #:in, with that environment's own
    commands, exactly as sandbox.preflight checks the run's own. Raises StepFailed("preflight")."""
    from . import sandbox  # sandbox imports this module

    done: set[str] = set()
    for step in adw.get("steps") or []:
        if step.get("kind") != "check":
            continue
        env_id = target(adw, step)
        if env_id is None or env_id in done:
            continue
        done.add(env_id)
        if layer.option("environments", env_id).get("kind") != "podman":
            continue
        started = time.monotonic()
        sandbox.ensure_image(layer)
        stack = stack_for(layer, state, step, primary)
        checks = sandbox.doctor(layer, stack, adw, fast=True, cmds=sandbox.workflow_commands(layer, adw, env_id),
                                agent=None, browser=False, services=True)
        for check in checks:
            # git inside the container is the agent's tool; no agent runs in a check environment
            if check.name == "git" and check.status == "miss":
                check.status, check.detail = "skip", "not in the image; only an agent inside the container needs it"
        missed = [check for check in checks if check.status == "miss"]
        events.say(f"[adw {state.adw_id}] preflight {env_id}: {len(checks)} checks, {len(missed)} missed",
                   "env.preflight", env=env_id, status="failed" if missed else "passed",
                   seconds=round(time.monotonic() - started, 3),
                   checks=[{"name": c.name, "status": c.status, "detail": c.detail} for c in checks])
        if missed:
            raise StepFailed("preflight", f"check environment {env_id} is missing what its checks need:\n"
                             + sandbox.format_table(checks))


def close_all(state: Any) -> None:
    """Close every check environment of this run (containers, pods) and forget it."""
    for key in [key for key in _open if key[0] == str(state.adw_id)]:
        stack = _open.pop(key)
        stack.close()
