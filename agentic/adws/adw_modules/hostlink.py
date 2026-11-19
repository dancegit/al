"""Reaching a host of a (placement …): the only way the runtime touches another machine.

A host is reached the way `al deploy` reaches it: with the person's own `ssh` and their own ssh
configuration (no option here names a key, a user, a port or a host-key policy). Its address is
never in the layer: it is the value of AL_HOST_<ID> (the host id in capitals, `-` as `_`), read
from the environment, from `local.env` in the layer directory or from
`$XDG_CONFIG_HOME/al/<layer name>.env`, like every other private value (see localenv). The value
is what `ssh HOST` accepts.

Every call runs the host's deployed layer: `cd <layer directory> && uv run --quiet adws/host.py
VERB …` (see selfrun.tool_main). The layer directory on the host is AL_HOST_<ID>_LAYER when that
is set, else what `al deploy` creates by default: `al/<repository name>/<in-repo dir>` under the
host's home.

Test seam (not in any usage text): with AL_HOSTS_LOCAL=<directory>, `<directory>/<id>` plays the
home of host <id>; commands run there with sh, HOME set to it and PATH
`<directory>/<id>/.hostbin:<directory>/<id>/bin`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import localenv
from .errors import LayerError

HOME = "home"                     # the reserved id of the machine that holds the layer
LOCAL_ENV = "AL_HOSTS_LOCAL"
CONNECT_TIMEOUT = 10
TOOL_TIMEOUT = 20
OLDER_LAYER = "its layer is older than this one: al deploy it again"


class HostSilent(Exception):
    """The host did not give a usable answer; `reason` says why, in words a person can act on."""

    def __init__(self, host_id: str, reason: str) -> None:
        super().__init__(f"host {host_id}: {reason}")
        self.host_id, self.reason = host_id, reason


def env_name(host_id: str) -> str:
    """AL_HOST_<ID>: the variable that holds the address of host `host_id`."""
    return "AL_HOST_" + str(host_id).upper().replace("-", "_")


def address(layer: Any, host_id: str) -> str:
    """The ssh destination of a host, from the environment or the layer's env files; "" when unset."""
    return localenv.lookup(env_name(host_id), localenv.load(layer)).strip()


def host_layer_dir(layer: Any, host_id: str) -> str:
    """The layer directory on the host: absolute, or relative to the host's home."""
    named = localenv.lookup(env_name(host_id) + "_LAYER", localenv.load(layer)).strip()
    if named:
        return named
    in_repo = str(getattr(layer, "in_repo_dir", "") or "")
    if in_repo:
        return f"al/{repository_name(layer)}/{in_repo}"
    return f"al/{Path(layer.layer_dir).name}"


def repository_name(layer: Any) -> str:
    """What `al deploy` calls the checkout on a host: the repository name of the application's
    remote (the forge's #:remote, else origin), else the directory of its main checkout; the layer's
    name when that is not a plain name."""
    app = str(layer.app_root)
    remote = str((layer.ir.get("forge") or {}).get("remote") or "origin")

    def git(*args: str) -> str:
        try:
            done = subprocess.run(["git", "-C", app, *args], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    url = git("remote", "get-url", remote).rstrip("/")
    if url:
        name = url.rsplit("/", 1)[-1]
    else:
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
        name = Path(common).parent.name if common and Path(common).name == ".git" else Path(app).name
    name = re.sub(r"\.git$", "", name).rsplit(":", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in (".", ".."):
        name = str(layer.name)
    return name


class Link:
    """One host. `run` executes a shell script there; `tool` calls the host's adws/host.py."""

    host_id = ""

    def run(self, script: str, stdin: bytes | None = None, timeout: float = TOOL_TIMEOUT) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def _run(self, argv: list[str], stdin: bytes | None, timeout: float, **kw: Any) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, input=stdin, stdin=subprocess.DEVNULL if stdin is None else None,
                                  capture_output=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired as exc:
            raise HostSilent(self.host_id, f"no answer within {int(timeout)} s") from exc
        except OSError as exc:
            raise HostSilent(self.host_id, f"cannot run {argv[0]}: {exc}") from exc

    def tool(self, layer: Any, host_id: str, verb: str, *args: str, stdin: bytes | str | None = None,
             timeout: float = TOOL_TIMEOUT, raw: bool = False) -> Any:
        """The answer of `adws/host.py VERB ARGS…` on the host: the parsed JSON document, or with
        `raw` its bytes (the record frames of `pull`). HostSilent on a timeout, a non-zero exit,
        an answer that is not JSON, or a host whose layer speaks another protocol."""
        from . import selfrun  # the protocol number lives with the tool that answers

        directory = host_layer_dir(layer, host_id)
        script = (f"cd {shlex.quote(directory)} && uv run --quiet adws/host.py "
                  + " ".join(shlex.quote(str(a)) for a in (verb, *args)))
        data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        done = self.run(script, stdin=data, timeout=timeout)
        if done.returncode != 0:
            detail = _last_line(done.stderr) or _last_line(done.stdout) or f"exit {done.returncode}"
            raise HostSilent(host_id, f"adws/host.py {verb} failed: {detail}")
        if raw:
            return done.stdout
        try:
            answer = json.loads(done.stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            raise HostSilent(host_id, f"adws/host.py {verb} gave no JSON answer") from exc
        if not isinstance(answer, dict):
            raise HostSilent(host_id, f"adws/host.py {verb} gave no JSON object")
        if answer.get("protocol") != selfrun.PROTOCOL:
            raise HostSilent(host_id, OLDER_LAYER)
        return answer


def _last_line(data: bytes) -> str:
    lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


class SshLink(Link):
    """The person's own ssh. BatchMode: a host that would ask for a password is a silent host, not
    a prompt in the middle of a run. `sh -lc` so the host's login PATH (uv, the agent CLI) applies."""

    def __init__(self, destination: str, host_id: str = "", connect_timeout: int = CONNECT_TIMEOUT) -> None:
        destination = (destination or "").strip()
        if not destination or destination.startswith("-") or any(c.isspace() for c in destination):
            raise LayerError(f"{env_name(host_id) if host_id else 'the host address'} is not a host: give what "
                             "`ssh HOST` accepts (address, user, port and key go in ~/.ssh/config)")
        self.destination, self.host_id, self.connect_timeout = destination, host_id, int(connect_timeout)

    def argv(self, script: str) -> list[str]:
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
                self.destination, "sh -lc " + shlex.quote(script)]

    def run(self, script: str, stdin: bytes | None = None, timeout: float = TOOL_TIMEOUT) -> subprocess.CompletedProcess:
        return self._run(self.argv(script), stdin, timeout)


class LocalLink(Link):
    """A directory that plays the host's home (tests): the same scripts, run by sh with that HOME."""

    def __init__(self, root: str | Path, path: str, host_id: str = "") -> None:
        self.root, self.path, self.host_id = str(root), path, host_id

    def environment(self) -> dict[str, str]:
        return {"HOME": self.root, "PATH": self.path, "LANG": "C"}

    def run(self, script: str, stdin: bytes | None = None, timeout: float = TOOL_TIMEOUT) -> subprocess.CompletedProcess:
        if not Path(self.root).is_dir():
            raise HostSilent(self.host_id, "no such host directory")
        return self._run(["sh", "-c", script], stdin, timeout, cwd=self.root, env=self.environment())


def link_for(layer: Any, host_id: str) -> Link:
    """The link to a declared host. HostSilent (naming the variable, never a value) when the host
    has no address on this machine."""
    if host_id == HOME:
        raise LayerError("`home` is this machine: it is not reached over a link")
    local = os.environ.get(LOCAL_ENV, "").strip()
    if local:
        root = Path(local) / host_id
        return LocalLink(root, f"{root / '.hostbin'}:{root / 'bin'}", host_id)
    destination = address(layer, host_id)
    if not destination:
        raise HostSilent(host_id, f"no address: set {env_name(host_id)} in the environment, in local.env in the layer "
                                  f"directory or in ~/.config/al/{layer.name}.env (what `ssh HOST` accepts)")
    try:
        return SshLink(destination, host_id)
    except LayerError as exc:
        raise HostSilent(host_id, str(exc)) from exc
