"""`al deploy` and `al dashboard --remote`: run a layer on another machine.

    al deploy LAYER --to HOST [--path DIR] [--as ID] [--provision [--yes]] [--dry-run] [--no-enable]
    al deploy LAYER --to HOST [--path DIR] [--as ID] --status
    al dashboard [LAYER] --remote HOST [--port N] [--remote-port N] [--open]

What is generated where:

- generated where `al gen` ran, committed, travels with the repository: agentic-layer.json, the
  prompts (commands/, hops/), adws/ (the runtime), env/. An in-repo layer is therefore complete on
  the host as soon as the repository is there.
- generated on the host, for the host's paths, never committed: the systemd user units
  ~/.config/systemd/user/al-<name>-{cron,webhook,dashboard}.service. `al deploy` renders them from
  the committed agentic-layer.json with the host's home directory, deployment directory and login
  PATH (all read in preflight) and writes them only on the host, outside the checkout, so the
  checkout stays clean and nothing machine-specific can be committed.
- generated where `al gen` ran, committed, machine-neutral: env/hosts/<id>/{home-manager.nix,
  nixos.nix,provision.sh} of each (host …) declaration (tools/al_provision.py). With --provision,
  deploy shows the plan for the declared style, asks once (--yes skips the question) and applies
  it; the Nix styles get a host-local wrapper (~/.config/al/<name>/home.nix or nixos.nix) that
  holds the host's paths and user name, written only on the host.
- not on the host, not needed to run: bin/al, .al-manifest.local.json, systemd/ in the layer (the
  machine-specific files `al gen` writes locally).

The host never runs `al gen` for the deployment: Racket compiles a definition and the host runs the
committed result. Racket is needed on the host only for the dashboard's actions (create, adopt, gen
on a repository from the page), so it is required only when the dashboard unit is installed and the
host has an al tool. Deploying needs no Racket here either (it reads the committed agentic-layer.json).

The tools the units run (git, uv, the agent CLI, racket and al when present) are found on the host by
the search of tools/al_tools.py (a setting in ~/.config/al/tools.env or <layer name>.env, the login
PATH, the per-user install folders), as one sh function (`al_tools.shell_finder`). Their folders go
in front of the login PATH in the units' Environment=PATH=, and the paths are recorded in
~/.config/al/<name>-tools.json on the host; `--status` reports a tool that moved or vanished since.
When deploy provisions the host first, the units' PATH and the record come from the preflight that
runs after provisioning, so they hold the tools provisioning installed (~/.local/bin for the script
style); the Home Manager and NixOS modules set their own units' PATH from the Nix profile.

A (host …) declaration's provisioning (tools/al_provision.py, above) and its sandbox image
(#:images build|pull|load, tools/al_sandbox.py) use the runtime's recipe hash of the committed env/,
so the image a host pulls or loads is al-<name>:<hash>, the one its runs ask for.

Everything that touches the host goes through a Transport. SshTransport runs the person's own
`ssh HOST` with no option that overrides their configuration (no key, user, port or host-key
policy); nothing about the host is stored anywhere. Every remote command is tagged read or change;
--dry-run and --status run reads only. Secrets are never copied: only variable *names* cross the
transport.

Test seam (like AL_DASHBOARD_DRY_RUN; not in the usage text): with AL_DEPLOY_LOCAL_HOST=<directory>
that directory plays the host's home (LocalTransport; AL_DEPLOY_LOCAL_PATH is its PATH) and --to is
only the label. AL_DEPLOY_TRACE=<file> appends every change command that ran, one per line.

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent) not in sys.path:     # tools/: al_tools beside this file
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import al_tools  # noqa: E402


class AlError(Exception):
    """A failure the person can act on; main() hands it to tools/al as its own AlError."""


# The POSIX programs the remote scripts call (besides shell builtins and the tools preflight checks
# for). The scripts use nothing else; tests/tools/deploy_host.py builds a host's bin/ from this.
REMOTE_TOOLS = ("sh", "cat", "sed", "grep", "df", "id", "mkdir", "chmod", "rm", "ls", "head", "dirname",
                "test", "env", "tr", "git", "mktemp")
CHECKED_TOOLS = ("uv", "podman", "systemctl", "loginctl", "claude", "codex", "getsubids", "home-manager", "nixos-rebuild")
# what preflight resolves on the host with al_tools.shell_finder: fact key → (program, setting)
HOST_TOOLS = {"git": ("git", "AL_GIT"), "uv": ("uv", "AL_UV"), "racket": ("racket", "AL_RACKET"), "al": ("al", "AL_TOOL")}
NOT_FOUND = "not found (looked at the setting, the login PATH and the per-user folders)"

# agent kind -> (CLI, credentials file under the home directory, variables that replace a login).
# Mirrors kit/runtime/adw_modules/envs.py: CREDENTIALS (a test keeps the two equal).
AGENT_LOGIN = {"claude-code": ("claude", ".claude/.credentials.json", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]),
               "codex": ("codex", ".codex/auth.json", ["OPENAI_API_KEY"])}
MIN_FREE_GB = 5
UNIT_KINDS = ("cron", "webhook", "dashboard")
UNIT_HEADER = "# Generated by al deploy for this host's paths. Not committed anywhere; the next deploy replaces it."
IN_REPO_ONLY = ('al deploy needs a layer that is committed with its repository: (layer in-repo #:dir "agentic"); '
                "see al init --in-repo")
STATUS_HINT = "is the dashboard running on the host? al deploy LAYER --to HOST --status"
RECORD_SEPARATOR = "\x1e"


# ------------------------------------------------------------------------------------------
# transports: the only way al touches a host
# ------------------------------------------------------------------------------------------

class Transport:
    label: str = ""     # what the terminal shows for the host; never written to a file

    def run(self, script: str, *, stdin: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def git_url(self, path: str) -> str:
        """What a LOCAL `git push` uses to reach `path` on the host."""
        raise NotImplementedError

    def tunnel_argv(self, local_port: int, remote_port: int) -> list[str]:
        raise NotImplementedError

    def run_file(self, script: str, path: str, *, timeout: float = 3600) -> subprocess.CompletedProcess:
        """Run `script` with the bytes of the local file `path` on its standard input (podman load)."""
        raise NotImplementedError

    def _run_file(self, argv: list[str], path: str, timeout: float, **kw: Any) -> subprocess.CompletedProcess:
        try:
            with open(path, "rb") as handle:
                done = subprocess.run(argv, stdin=handle, capture_output=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired as exc:
            raise AlError(f"{self.label}: no answer within {int(timeout)} s") from exc
        except OSError as exc:
            raise AlError(f"cannot run {argv[0]}: {exc}") from exc
        return subprocess.CompletedProcess(argv, done.returncode, done.stdout.decode(errors="replace"),
                                           done.stderr.decode(errors="replace"))

    def _run(self, argv: list[str], stdin: str | None, timeout: float, **kw: Any) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, input=stdin, stdin=subprocess.DEVNULL if stdin is None else None,
                                  capture_output=True, text=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired as exc:
            raise AlError(f"{self.label}: no answer within {int(timeout)} s") from exc
        except OSError as exc:
            raise AlError(f"cannot run {argv[0]}: {exc}") from exc


class SshTransport(Transport):
    """The person's own ssh: HOST as given, and no option that names a key, user, port or host-key
    policy (their ssh configuration decides all of that). `sh -lc` so the host's login PATH, where uv
    and the agent CLI usually are, applies."""

    def __init__(self, host: str):
        host = (host or "").strip()
        if not host or host.startswith("-"):
            raise AlError(f"not a host: {host!r} (give what `ssh HOST` accepts; address, user, port and key go in ~/.ssh/config)")
        self.host = self.label = host

    def argv(self, script: str) -> list[str]:
        return ["ssh", "-T", self.host, "sh -lc " + shlex.quote(script)]

    def run(self, script: str, *, stdin: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
        return self._run(self.argv(script), stdin, timeout)

    def git_url(self, path: str) -> str:
        return f"{self.host}:{path}"

    def run_file(self, script: str, path: str, *, timeout: float = 3600) -> subprocess.CompletedProcess:
        return self._run_file(self.argv(script), path, timeout)

    def tunnel_argv(self, local_port: int, remote_port: int) -> list[str]:
        return ["ssh", "-N", "-T", "-o", "ExitOnForwardFailure=yes",
                "-L", f"127.0.0.1:{int(local_port)}:127.0.0.1:{int(remote_port)}", self.host]


class LocalTransport(Transport):
    """A directory that plays the host's home (tests): the same scripts, run by sh with that HOME."""

    def __init__(self, root: str, path: str, label: str = "local"):
        self.root, self.path, self.label = str(root), path, label

    def run(self, script: str, *, stdin: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
        return self._run(["sh", "-c", script], stdin, timeout, cwd=self.root,
                         env={"HOME": self.root, "PATH": self.path, "LANG": "C"})

    def git_url(self, path: str) -> str:
        return path

    def run_file(self, script: str, path: str, *, timeout: float = 3600) -> subprocess.CompletedProcess:
        return self._run_file(["sh", "-c", script], path, timeout, cwd=self.root,
                              env={"HOME": self.root, "PATH": self.path, "LANG": "C"})

    def tunnel_argv(self, local_port: int, remote_port: int) -> list[str]:
        raise AlError("no tunnel to a directory (AL_DEPLOY_LOCAL_HOST is set)")


def transport_for(host: str) -> Transport:
    local = os.environ.get("AL_DEPLOY_LOCAL_HOST", "").strip()
    if local:
        return LocalTransport(local, os.environ.get("AL_DEPLOY_LOCAL_PATH") or os.environ.get("PATH", ""), label=host)
    return SshTransport(host)


def _done(code: int = 0, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, "")


class Remote:
    """A transport plus the rule that every command is a read or a change. Under dry_run reads run
    and are shown, changes are only shown. With read_only (--status) a change is a programming error."""

    def __init__(self, transport: Transport, *, dry_run: bool = False, read_only: bool = False):
        self.transport, self.dry_run, self.read_only = transport, dry_run, read_only
        self.log: list[tuple[str, str, str]] = []          # (read | change | local, script, why)
        self.trace = os.environ.get("AL_DEPLOY_TRACE", "").strip()

    @property
    def label(self) -> str:
        return self.transport.label

    def _show(self, tag: str, where: str, script: str, why: str) -> None:
        if self.dry_run:
            print(f"# {why}\n[{tag}] {where}$ " + script.replace("\n", "\n      "), flush=True)

    def _traced(self, line: str) -> None:
        if self.trace:
            with open(self.trace, "a") as handle:
                handle.write(line + "\n")

    def read(self, script: str, why: str, *, after_change: bool = False, stdin: str | None = None) -> subprocess.CompletedProcess:
        """Run a command that changes nothing. after_change: it reads what a change produced, so
        under dry_run (where the change did not happen) it is shown and not run. stdin: e.g. a
        script fed to `sh -s`."""
        self.log.append(("read", script, why))
        if self.dry_run and after_change:
            self._show("would read", "host", script, why)
            return _done()
        self._show("read", "host", script, why)
        return self.transport.run(script, stdin=stdin)

    def change(self, script: str, why: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        if self.read_only:
            raise RuntimeError(f"a change in a read-only session: {script}")
        self.log.append(("change", script, why))
        self._show("would run", "host", script, why)
        if self.dry_run:
            return _done()
        self._traced(f"host$ {script}")
        return self.transport.run(script, stdin=stdin)

    def change_file(self, script: str, path: str, why: str) -> subprocess.CompletedProcess:
        """A change whose standard input is a local file (an image for `podman load`)."""
        if self.read_only:
            raise RuntimeError(f"a change in a read-only session: {script}")
        self.log.append(("change", script, why))
        self._show("would run", "host", f"{script} < (a local file: {why})", why)
        if self.dry_run:
            return _done()
        self._traced(f"host$ {script}")
        return self.transport.run_file(script, path)

    def local_change(self, argv: list[str], why: str) -> subprocess.CompletedProcess:
        """A command on this machine that changes the host (git push)."""
        if self.read_only:
            raise RuntimeError(f"a change in a read-only session: {argv}")
        script = shlex.join(argv)
        self.log.append(("local", script, why))
        self._show("would run", "local", script, why)
        if self.dry_run:
            return _done()
        self._traced(f"local$ {script}")
        return subprocess.run(al_tools.argv(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL)


def put(remote: Remote, path: str, text: str, mode: str = "0644") -> subprocess.CompletedProcess:
    """Write a file on the host; its content travels on stdin, never on a command line."""
    p = shlex.quote(path)
    return remote.change(f'umask 077; mkdir -p "$(dirname {p})" && cat > {p} && chmod {mode} {p}',
                         f"write {path}", stdin=text)


# ------------------------------------------------------------------------------------------
# pure helpers
# ------------------------------------------------------------------------------------------

def safe_remote_url(url: str) -> bool:
    """False for an http(s) URL that carries a user, password or token: such a URL is never sent
    to the host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() in ("http", "https"):
        return "@" not in parts.netloc and not re.search(r"(?i)(token|password|secret|key)=", parts.query)
    return True


@dataclass(frozen=True)
class EnvNeed:
    name: str                       # the variable (or, for an agent login, what is shown in its place)
    why: str
    required: bool
    any_of: tuple[str, ...] = ()    # agent login: any of these variables satisfies it …
    file: str = ""                  # … or this credentials file under the home directory
    bin: str = ""                   # the agent CLI


def used_agents(ir: dict) -> list[dict]:
    """The agents some adw uses (its own, a step's, its review's); an empty reference is the first agent."""
    agents = ir.get("agents") or []
    if not agents:
        return []
    first = agents[0]["id"]
    reviews = {r["id"]: r for r in ir.get("reviews") or []}
    used: set[str] = set()
    for adw in ir.get("adws") or []:
        used.add(adw.get("agent") or first)
        for step in adw.get("steps") or []:
            if "agent" in step:
                used.add(step["agent"] or first)
        review = reviews.get(adw.get("review") or "")
        if review and "agent" in review:
            used.add(review.get("agent") or first)
    return [a for a in agents if a["id"] in used]


def required_env(ir: dict, decl: dict | None = None) -> list[EnvNeed]:
    """What the layer reads from the environment when it runs, from the IR alone. Names only.
    The (registry …) variables are needed on a host only when its declaration `decl` pulls the
    sandbox image (#:images pull)."""
    needs: list[EnvNeed] = []
    forge = ir.get("forge") or {}
    if forge.get("kind", "none") != "none" and forge.get("tokenEnv"):
        github = forge["kind"] == "github"
        needs.append(EnvNeed(forge["tokenEnv"], f"forge token ({forge['kind']})" + ("; without it gh auth on the host is used" if github else ""),
                             not github))
    for trigger in ir.get("triggers") or []:
        if trigger.get("kind") == "webhook" and trigger.get("secretEnv"):
            needs.append(EnvNeed(trigger["secretEnv"], "webhook signature secret", True))
    dashboard = ir.get("dashboard") or {}
    if dashboard.get("kind", "none") != "none" and dashboard.get("tokenEnv"):
        needs.append(EnvNeed(dashboard["tokenEnv"], "dashboard access token; the deployed dashboard listens on loopback only", False))

    registry = ir.get("registry") if isinstance(ir.get("registry"), dict) else None
    if registry and decl and decl.get("images") == "pull":
        needs.append(EnvNeed(registry["addressEnv"], "registry address the host pulls the sandbox image from", True))
        for key, why in (("usernameEnv", "registry user"), ("passwordEnv", "registry password")):
            if registry.get(key):
                needs.append(EnvNeed(registry[key], why, True))

    handled = {("forge", "tokenEnv"), ("dashboard", "tokenEnv"), ("triggers", "secretEnv"),
               ("registry", "addressEnv"), ("registry", "usernameEnv"), ("registry", "passwordEnv")}

    def walk(family: str, node: Any, ident: str) -> None:
        if isinstance(node, dict):
            ident = str(node.get("id") or node.get("name") or ident)
            for key in sorted(node):
                value = node[key]
                if isinstance(value, str) and key.endswith("Env"):
                    if value and (family, key) not in handled:
                        needs.append(EnvNeed(value, f"{family} {ident}: {key}".replace("  ", " "), True))
                else:
                    walk(family, value, ident)
        elif isinstance(node, list):
            for item in node:
                walk(family, item, ident)

    for family in sorted(ir):
        walk(family, ir[family], "")

    for agent in used_agents(ir):
        kind = agent.get("kind")
        if kind in AGENT_LOGIN:
            cli, credentials, variables = AGENT_LOGIN[kind]
            needs.append(EnvNeed(f"{cli} login (~/{credentials}) or {' / '.join(variables)}", f"agent {agent['id']} ({kind})",
                                 True, tuple(variables), credentials, cli))
        elif kind == "custom":
            words = str(agent.get("command") or agent.get("cmd") or "").split()
            needs.append(EnvNeed(f"checked by hand: {words[0] if words else agent['id']}", f"agent {agent['id']} (custom)", False))
    seen: set[str] = set()
    return [n for n in needs if not (n.name in seen or seen.add(n.name))]


def free_port(preferred: int, explicit: bool) -> int:
    """A port on 127.0.0.1 nothing listens on: `preferred`, else (unless the person asked for exactly
    that one) one of the next 50, else whatever the system hands out."""
    def bindable(port: int) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return 0
            return probe.getsockname()[1]
    if bindable(preferred):
        return preferred
    if explicit:
        raise AlError(f"port {preferred} is in use on this machine")
    for port in range(preferred + 1, min(preferred + 51, 65536)):
        if bindable(port):
            return port
    port = bindable(0)
    if not port:
        raise AlError("no free port on 127.0.0.1")
    return port


def table(headers: list[str], rows: list[list[str]], indent: str = "  ") -> str:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    return "\n".join(indent + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in [headers, *rows])


# ------------------------------------------------------------------------------------------
# local facts
# ------------------------------------------------------------------------------------------

@dataclass
class Facts:
    layer: Path
    ir: dict
    app: Path
    name: str
    layer_rel: str
    branch: str
    commit: str
    origin: str
    repository: str
    notes: list[str] = field(default_factory=list)
    decl: dict | None = None        # the (host …) declaration this host is (--as), or None


def load_ir(layer: Path) -> dict:
    try:
        return json.loads((layer / "agentic-layer.json").read_text())
    except OSError as exc:
        raise AlError(f"{layer} has no agentic-layer.json: run al gen first") from exc
    except ValueError as exc:
        raise AlError(f"{layer / 'agentic-layer.json'} is not JSON: run al gen first") from exc


def local_facts(layer: Path, al: Any) -> Facts:
    """What this machine knows: the committed IR (no compile, so no Racket is needed to deploy),
    the repository, its default branch and that branch's commit."""
    layer = layer.resolve()
    ir = load_ir(layer)
    if (ir.get("layer") or {}).get("kind") != "in-repo":
        raise AlError(IN_REPO_ONLY)
    app = al.app_root_of(layer, ir)
    layer_rel = ir["layer"]["dir"]
    branch = ir["codebase"].get("defaultBranch") or "main"
    commit = al.git(app, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if not commit:
        raise AlError(f"{app} has no branch {branch} (the layer's default branch) to deploy")
    if not al.git(app, "ls-tree", "--name-only", branch, "--", f"{layer_rel}/agentic-layer.json"):
        raise AlError(f"{layer_rel}/agentic-layer.json is not committed on {branch}: commit the layer, then deploy")
    origin = al.git(app, "remote", "get-url", (ir.get("forge") or {}).get("remote") or "origin")
    repository = re.sub(r"\.git$", "", al.repository_name(app, origin.rstrip("/"))).rsplit(":", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repository) or repository in (".", ".."):
        repository = ir["name"]
    notes = []
    if al.git(app, "status", "--porcelain", "--", layer_rel):
        notes.append(f"uncommitted changes under {layer_rel}/ are not deployed")
    return Facts(layer, ir, app, ir["name"], layer_rel, branch, commit, origin, repository, notes)


def host_decl(ir: dict, as_id: str | None) -> dict | None:
    """The (host …) declaration a deploy is for: --as ID, else the only one; None when the layer
    declares none."""
    hosts = ir.get("hosts") or []
    ids = [h["id"] for h in hosts]
    if as_id:
        found = next((h for h in hosts if h["id"] == as_id), None)
        if not found:
            raise AlError(f"--as {as_id}: the layer declares no (host #:id {as_id} …); declared: {', '.join(ids) or 'none'}")
        return found
    if not hosts:
        return None
    if len(hosts) > 1:
        raise AlError(f"the layer declares several hosts ({', '.join(ids)}): say which one this is with --as ID")
    return hosts[0]


def unit_kinds(ir: dict, al: Any, decl: dict | None = None) -> list[str]:
    """One unit per kind the layer declares, whether or not #:systemd is set: deploying means keep
    these running. A host declaration's #:units narrows them."""
    kinds = [k for k in ("cron", "webhook") if any(t.get("kind") == k for t in ir.get("triggers") or [])]
    if (ir.get("dashboard") or al.DEFAULT_DASHBOARD)["kind"] != "none":
        kinds.append("dashboard")
    if decl and decl.get("units"):
        kinds = [k for k in kinds if k in decl["units"]]
    return kinds


def unit_names(facts: Facts, al: Any) -> list[str]:
    return [f"al-{facts.name}-{kind}.service" for kind in unit_kinds(facts.ir, al, facts.decl)]


def agent_logins(ir: dict) -> list[EnvNeed]:
    return [n for n in required_env(ir) if n.bin]


def needs_podman(ir: dict) -> bool:
    return (any(e.get("kind") == "podman" for e in ir.get("environments") or [])
            or (ir.get("dashboard") or {}).get("kind") == "podman")


# ------------------------------------------------------------------------------------------
# preflight
# ------------------------------------------------------------------------------------------

@dataclass
class HostFacts:
    raw: dict[str, str]

    def __getitem__(self, key: str) -> str:
        return self.raw.get(key, "")

    home = property(lambda self: self["home"])
    path = property(lambda self: self["path"])
    directory = property(lambda self: self["directory"])
    envnames = property(lambda self: {n for n in self["envnames"].split(",") if n})

    @property
    def tools(self) -> dict[str, str]:
        """tool → its path on the host ("" when it was not found), from the al_find lines of preflight."""
        out = {}
        for key, value in self.raw.items():
            if key in HOST_TOOLS or key.startswith("agent."):
                out[key.removeprefix("agent.")] = value.split(" ", 1)[0] if value else ""
        return out


def directory_expr(facts: Facts, path: str | None) -> str:
    """The deployment directory as a shell word: --path, else <home>/al/<repository name>."""
    if path:
        if not path.startswith("/"):
            raise AlError("--path must be an absolute path on the host")
        return shlex.quote(path.rstrip("/") or "/")
    return f'"$HOME"/al/{shlex.quote(facts.repository)}'


def deployment_script(facts: Facts, path: str | None) -> str:
    return f"""d={directory_expr(facts, path)}
echo "directory=$d"
if test -d "$d/.git"; then echo dir=git; echo "dirty=$(git -C "$d" status --porcelain 2>/dev/null | grep -c '')"; echo "head=$(git -C "$d" rev-parse HEAD 2>/dev/null)"; echo "dir_origin=$(git -C "$d" remote get-url origin 2>/dev/null)"
elif test -e "$d"; then echo dir=other
else echo dir=absent
fi"""


def tool_lines(facts: Facts) -> list[str]:
    """sh lines that find each tool the units run (al_tools.shell_finder must come first) and print
    `KEY=PATH VERSION`, or `KEY=` when it is nowhere."""
    found = [(key, program, setting) for key, (program, setting) in HOST_TOOLS.items()]
    found += [(f"agent.{need.bin}", need.bin, al_tools.TOOLS[need.bin].setting if need.bin in al_tools.TOOLS
               else "AL_" + need.bin.upper()) for need in agent_logins(facts.ir)]
    lines = [f"al_layer={shlex.quote(facts.name)}"]
    for key, program, setting in found:
        version = "" if program == "al" else ' $("$p" --version 2>/dev/null | head -n 1)'
        lines.append(f'if p=$(al_find {program} {setting}); then echo "{key}=$p{version}"; else echo "{key}="; fi')
    return lines


def host_fix(program: str) -> str:
    """How to make a missing tool found on the host: install it, or name it in tools.env there."""
    tool = al_tools.TOOLS.get(program)
    install, setting = (tool.install, tool.setting) if tool else (f"install {program}", "AL_" + program.upper())
    return f"{install} on the host, or set {setting}=/path/to/{program} in ~/.config/al/tools.env there"


def preflight_script(facts: Facts, path: str | None = None) -> str:
    """One read. It prints key=value lines whose values are versions, paths and yes/no only: never
    the content of a variable or of a credentials file (of the environment only the names leave the host)."""
    lines = [al_tools.shell_finder(), 'echo "home=$HOME"', 'echo "path=$PATH"', *tool_lines(facts)]
    lines.append('if command -v podman >/dev/null 2>&1; then echo "podman=$(command -v podman) $(podman --version 2>/dev/null | head -n 1)"; '
                 'else echo "podman="; fi')
    lines.append("echo \"rootless=$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)\"")
    for tool in ("home-manager", "nixos-rebuild"):
        lines.append(f'echo "{tool}=$(command -v {tool} 2>/dev/null)"')
    lines += ['echo "user=$(id -un)"',
              'if getsubids "$(id -un)" >/dev/null 2>&1 || grep -q "^$(id -un):" /etc/subuid 2>/dev/null; then echo subids=yes; else echo subids=no; fi']
    for tool in ("uv", *(n.bin for n in agent_logins(facts.ir))):
        lines.append(f'echo "local.{tool}=$("$HOME/.local/bin/{tool}" --version 2>/dev/null | head -n 1)"')
    for need in agent_logins(facts.ir):
        lines.append(f'if test -s "$HOME/{need.file}"; then echo "login.{need.bin}=yes"; else echo "login.{need.bin}=no"; fi')
    env_file = f'"$HOME/.config/al/{facts.name}.env"'
    lines += [
        'set -- $(df -Pk "$HOME" 2>/dev/null | sed -n 2p)',
        'echo "disk_kb=$4"',
        "if systemctl --user show-environment >/dev/null 2>&1; then echo systemd=yes; else echo systemd=no; fi",
        'echo "linger=$(loginctl show-user "$(id -un)" --property=Linger 2>/dev/null | sed \'s/^Linger=//\')"',
        f'echo "envnames=$({{ cat {env_file} 2>/dev/null; echo; systemctl --user show-environment 2>/dev/null; }} | '
        "sed -n 's/^\\([A-Za-z_][A-Za-z0-9_]*\\)=.*/\\1/p' | tr '\\n' ',')\"",
        deployment_script(facts, path),
    ]
    return "\n".join(lines)


def parse_facts(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_.A-Z0-9-]+", key):
            found[key] = value.strip()
    return found


@dataclass
class Row:
    check: str
    state: str          # ok | MISSING | will fix | not needed
    detail: str = ""
    fix: str = ""


def preflight_rows(facts: Facts, host: HostFacts) -> list[Row]:
    name, rows = facts.name, []

    def tool(check: str, key: str, fix: str) -> None:
        rows.append(Row(check, "ok", host[key]) if host[key] else Row(check, "MISSING", NOT_FOUND, fix))

    tool("git", "git", host_fix("git"))
    tool("uv", "uv", host_fix("uv"))
    if not needs_podman(facts.ir):
        rows.append(Row("podman", "not needed", "the layer declares no podman environment or dashboard"))
    elif host["podman"] and host["rootless"] == "true":
        rows.append(Row("podman", "ok", f"{host['podman']} (rootless)"))
    else:
        rows.append(Row("podman", "MISSING", f"{host['podman']} (rootless: {host['rootless'] or 'unknown'})" if host["podman"] else "not found on the login PATH",
                        "install podman and make rootless work: podman info must say rootless: true (subuid/subgid for the user)"))
    if needs_podman(facts.ir):
        rows.append(Row("subuid/subgid", "ok", f"ranges for {host['user'] or 'the user'}") if host["subids"] != "no" else
                    Row("subuid/subgid", "MISSING", f"no ranges for {host['user'] or 'the user'}",
                        "rootless podman needs subuid/subgid ranges: sudo usermod --add-subuids 100000-165535 "
                        '--add-subgids 100000-165535 "$USER" on the host'))
    for need in agent_logins(facts.ir):
        tool(f"agent CLI {need.bin}", f"agent.{need.bin}", host_fix(need.bin))
        named = sorted(set(need.any_of) & host.envnames)
        if host[f"login.{need.bin}"] == "yes":
            rows.append(Row(f"agent login {need.bin}", "ok", f"~/{need.file}"))
        elif named:
            rows.append(Row(f"agent login {need.bin}", "ok", f"{named[0]} is set"))
        else:
            rows.append(Row(f"agent login {need.bin}", "MISSING", f"no ~/{need.file}, none of {' / '.join(need.any_of)} set",
                            f"log in on the host (run {need.bin} once there) or put {need.any_of[0]}=… in ~/.config/al/{name}.env on the host (chmod 600)"))
    try:
        free = int(host["disk_kb"]) / (1024 * 1024)
    except ValueError:
        rows.append(Row("disk", "ok", "free space could not be read"))
    else:
        if free < MIN_FREE_GB:
            rows.append(Row("disk", "MISSING", f"{free:.1f} GB free",
                            f"free space under the home directory on the host: {free:.1f} GB free, {MIN_FREE_GB} GB needed"))
        else:
            rows.append(Row("disk", "ok", f"{free:.1f} GB free"))
    if host["systemd"] == "yes":
        rows.append(Row("systemd user session", "ok"))
    else:
        rows.append(Row("systemd user session", "MISSING", "systemctl --user does not answer",
                        "systemctl --user must work for this user over ssh (pam_systemd; XDG_RUNTIME_DIR set)"))
    rows.append(Row("lingering", "ok", "units keep running after logout") if host["linger"] == "yes"
                else Row("lingering", "will fix", "off: loginctl enable-linger, so the units survive logout"))
    if racket_needed(facts, host):
        tool("racket", "racket", "install Racket on the host or set AL_RACKET in ~/.config/al/tools.env")
    else:
        rows.append(Row("racket", "not needed", "the layer was compiled where you ran al gen; the host only runs it"))
    return rows


def racket_needed(facts: Facts, host: HostFacts) -> bool:
    """Racket on the host runs only the dashboard's actions (create, adopt, gen), and those need the
    dashboard unit and an al tool on the host."""
    dashboard = (facts.ir.get("dashboard") or {}).get("kind", "local") != "none"
    return dashboard and bool(host.tools.get("al"))


def secret_rows(facts: Facts, host: HostFacts) -> tuple[list[list[str]], list[str]]:
    """(the secrets table, the required plain variables that are missing). Names only."""
    rows, missing, names = [], [], host.envnames
    for need in required_env(facts.ir, facts.decl):
        if need.bin:
            present = "set" if host[f"login.{need.bin}"] == "yes" or set(need.any_of) & names else "missing"
        elif need.name.startswith("checked by hand"):
            present = "by hand"
        else:
            present = "set" if need.name in names else "missing"
            if present == "missing" and need.required:
                missing.append(need.name)
        rows.append([need.name, need.why, present, "required" if need.required else "optional"])
    return rows, missing


def preflight(remote: Remote, facts: Facts, path: str | None) -> tuple[list[Row], HostFacts]:
    done = remote.read(preflight_script(facts, path), "preflight: what the host has (versions, paths, yes/no; names of variables, never values)")
    host = HostFacts(parse_facts(done.stdout))
    if not host.home or not host.directory:
        tail = " ".join((done.stderr or "").strip().splitlines()[-3:])
        raise AlError(f"cannot run commands on {remote.label}" + (f": {tail}" if tail else f" (exit {done.returncode})"))
    return preflight_rows(facts, host), host


# ------------------------------------------------------------------------------------------
# the repository
# ------------------------------------------------------------------------------------------

@dataclass
class Action:
    kind: str                       # change | local
    command: Any                    # a script (change) or an argv (local)
    why: str
    failed: str = ""                # what to say when it fails


INCOMING = "al-deploy-incoming"


def update_script(directory: str, source: str, branch: str, remote_tracking: bool) -> str:
    """Fast-forward the checkout at `directory` to `branch` of `source` (a path or URL; "" = the
    checkout's origin) without fetching inside it. A sandboxed run's worktree has a detached HEAD
    whose commit lives only in that run's quarantine object store (agents/<id>/objects), and the
    connectivity check after a fetch walks every worktree's HEAD, so a fetch in the checkout fails
    while such a run is in flight. Instead: fetch into a temporary bare repository
    (<directory>/.git/al-deploy-incoming) that borrows the checkout's objects through
    objects/info/alternates and has no worktrees, move exactly the new objects into the checkout
    with pack-objects | index-pack --strict (as gitsafe.import_objects does), and fast-forward to
    the fetched commit. Nothing reads a run's quarantine; the temporary repository always goes."""
    d, b = shlex.quote(directory), shlex.quote(branch)
    inc = shlex.quote(f"{directory}/.git/{INCOMING}")
    src = shlex.quote(source) if source else f'"$(git -C {d} remote get-url origin)"'
    lines = [f"inc={inc};",
             "trap 'rm -rf \"$inc\"' EXIT;",
             'rm -rf "$inc" && git init -q --bare "$inc" &&',
             f'echo {shlex.quote(directory + "/.git/objects")} > "$inc/objects/info/alternates" &&',
             f'git -C "$inc" fetch -q {src} +refs/heads/{b}:refs/heads/{b} &&',
             f'new=$(git -C "$inc" rev-parse --verify refs/heads/{b}) && old=$(git -C {d} rev-parse HEAD) &&',
             f'if git -C {d} merge-base --is-ancestor "$new" "$old" 2>/dev/null; then :; else',
             f'{{ echo "$new"; echo "^$old"; }} | git -C "$inc" pack-objects -q --revs --stdout | git -C {d} index-pack --stdin --strict >/dev/null;',
             "fi &&",
             f'git -C {d} -c gc.auto=0 merge -q --ff-only "$new"' + (f' && git -C {d} update-ref refs/remotes/origin/{b} "$new"' if remote_tracking else "")]
    return " ".join(lines)        # one line: --dry-run shows it, and the trace records it, as one command


def plan_repository(facts: Facts, host: HostFacts, remote: Remote) -> tuple[list[Action], list[str]]:
    """(what puts the default branch's commit into the deployment directory, notes). Reads only."""
    directory, branch = host.directory, facts.branch
    d, bare, b = shlex.quote(directory), shlex.quote(directory + ".git"), shlex.quote(branch)
    diverged = f"the deployment has commits that are not on {branch}: resolve on the host"
    if host["dir"] == "other":
        raise AlError(f"{directory} on the host exists and is not a git checkout; choose another --path")
    existing = host["dir"] == "git"
    if existing and int(host["dirty"] or 0) > 0:
        listing = remote.read(f"git -C {d} status --porcelain | head -n 10", "the local changes of the deployment").stdout.rstrip()
        raise AlError(f"the deployment at {directory} has local changes ({host['dirty']} files): commit or discard them "
                      f"on the host, then deploy again\n{listing}")
    if existing:
        push = host["dir_origin"] == directory + ".git"
    else:
        push = True
        if facts.origin and safe_remote_url(facts.origin):
            reach = remote.read("GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes' "
                                f"git ls-remote --exit-code {shlex.quote(facts.origin)} HEAD",
                                "can the host reach the repository's remote?")
            push = reach.returncode != 0
    notes, actions = [], []
    if push:
        notes.append(f"the host cannot reach the repository's remote: shipped work lands in {directory}.git on the host")
        send = Action("local", ["git", "-C", str(facts.app), "push", remote.transport.git_url(directory + ".git"), f"{branch}:{branch}"],
                      f"push {branch} to the host over the ssh connection", diverged)
        if existing:
            actions += [send, Action("change", update_script(directory, directory + ".git", branch, False),
                                     f"fast-forward the deployment to {branch}", diverged)]
        else:
            actions += [Action("change", f"git init --bare {bare} && git --git-dir {bare} symbolic-ref HEAD refs/heads/{b}",
                               "a bare repository on the host that receives the push"),
                        send,
                        Action("change", f"git clone {bare} {d} && git -C {d} checkout {b}", "the checkout the layer runs in")]
    elif existing:
        actions.append(Action("change", update_script(directory, "", branch, True),
                              f"fast-forward the deployment to the remote's {branch}", diverged))
    else:
        actions.append(Action("change", f"git clone {shlex.quote(facts.origin)} {d} && git -C {d} checkout {b}",
                              "clone the repository from its own remote; the checkout the layer runs in"))
    return actions, notes


def apply_repository(actions: list[Action], facts: Facts, host: HostFacts, remote: Remote) -> None:
    for action in actions:
        done = remote.change(action.command, action.why) if action.kind == "change" else remote.local_change(action.command, action.why)
        if done.returncode != 0:
            tail = "\n".join((done.stderr or done.stdout or "").strip().splitlines()[-5:])
            raise AlError((action.failed or f"failed on the host: {action.why}") + (f"\n{tail}" if tail else ""))
    head = remote.read(f"git -C {shlex.quote(host.directory)} rev-parse HEAD", "the commit that is deployed now", after_change=True).stdout.strip()
    if remote.dry_run:
        return
    if head != facts.commit:
        print(f"deployed {head[:12] or '?'} from the repository's remote; local {facts.branch} is {facts.commit[:12]} "
              f"(push it, then deploy again)")
    else:
        print(f"repository: {host.directory} is at {head[:12]} ({facts.branch})")


# ------------------------------------------------------------------------------------------
# units
# ------------------------------------------------------------------------------------------

def host_units(facts: Facts, host: HostFacts, directory: str, al: Any) -> dict[str, str]:
    """The unit files for this host: the generator's unit text (al.systemd_unit) with the host's
    layer path and login PATH. They exist only on the host, under ~/.config/systemd/user/."""
    decl = facts.decl
    ir = provision_module().with_host_port(facts.ir, decl)
    dashboard = ir.get("dashboard") or al.DEFAULT_DASHBOARD
    units = {}
    for kind in unit_kinds(facts.ir, al, decl):
        extra = []
        if kind == "dashboard" and dashboard["kind"] == "local":
            extra.append("--bind 127.0.0.1")
        if kind == "dashboard" and decl and decl.get("dashboardPort"):
            extra.append(f"--port {int(decl['dashboardPort'])}")
        units[f"al-{facts.name}-{kind}.service"] = al.systemd_unit(
            kind, facts.name, f"{directory}/{facts.layer_rel}", ir, header=UNIT_HEADER, path_env=unit_path(host),
            env_file=f"%h/.config/al/{facts.name}.env", extra_args=" ".join(extra))
    return units


def unit_path(host: HostFacts) -> str:
    """The units' PATH: the folder of each tool found on the host, then the host's login PATH."""
    return al_tools.search_path_from([os.path.dirname(p) for p in host.tools.values() if p], host.path)


def tools_record(facts: Facts, host: HostFacts) -> str:
    return f"{host.home}/.config/al/{facts.name}-tools.json"


def units_status_script(units: list[str]) -> str:
    return (f"for u in {' '.join(shlex.quote(u) for u in units)}; do "
            'echo "unit $u $(systemctl --user is-enabled "$u" 2>/dev/null | head -n 1) $(systemctl --user is-active "$u" 2>/dev/null | head -n 1)"; done\n'
            'echo "linger=$(loginctl show-user "$(id -un)" --property=Linger 2>/dev/null | sed \'s/^Linger=//\')"')


def units_table(text: str) -> str:
    rows = [line.split()[1:] for line in text.splitlines() if line.startswith("unit ")]
    rows = [[r[0], r[1] if len(r) > 1 else "?", r[2] if len(r) > 2 else "?"] for r in rows if r]
    linger = parse_facts(text).get("linger") or "no"
    return table(["unit", "enabled", "active"], rows) + f"\n  linger: {linger}"


def install_units(remote: Remote, facts: Facts, host: HostFacts, units: dict[str, str], enable: bool, write: bool = True) -> None:
    """write: put the unit files and reload; False when the host's Home Manager module owns them
    (#:provision home-manager), so deploy only (re)starts them."""
    def must(done: subprocess.CompletedProcess, what: str) -> None:
        if done.returncode != 0:
            tail = " ".join((done.stderr or done.stdout or "").strip().splitlines()[-3:])
            raise AlError(f"failed on the host: {what}" + (f": {tail}" if tail else ""))

    names = " ".join(shlex.quote(u) for u in units)
    if write:
        for filename, text in units.items():
            must(put(remote, f"{host.home}/.config/systemd/user/{filename}", text), f"write {filename}")
        must(remote.change("systemctl --user daemon-reload", "make systemd read the new units"), "systemctl --user daemon-reload")
    if not write:
        must(remote.change(f"systemctl --user restart {names}", "restart the units the Home Manager module defines"), "start the units")
    elif enable:
        must(remote.change(f"systemctl --user enable {names}", "start the units with the user's session"), "enable the units")
        must(remote.change(f"systemctl --user restart {names}", "start them now (an update takes effect)"), "start the units")
    if host["linger"] != "yes":
        done = remote.change("loginctl enable-linger", "keep the user's units running after logout")
        if done.returncode != 0:
            print("lingering is off and could not be enabled as this user; run once on the host as a privileged user:\n"
                  '  sudo loginctl enable-linger "$USER"')
    status = remote.read(units_status_script(list(units)), "what the units are doing now", after_change=True)
    if not remote.dry_run:
        print("units:\n" + units_table(status.stdout))


# ------------------------------------------------------------------------------------------
# provisioning: a (host …) declaration made true on the host
# ------------------------------------------------------------------------------------------

WRAPPER_HEADER = "# Written by al deploy for this host's paths and user. Not committed anywhere; the next deploy replaces it."


def _tools_module(name: str) -> Any:
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    return __import__(name)


def provision_module() -> Any:
    return _tools_module("al_provision")


def sandbox_module() -> Any:
    return _tools_module("al_sandbox")


def committed(facts: Facts, rel: str) -> bytes:
    """A file of the layer as committed on the deployed branch (what the host gets), as bytes."""
    done = subprocess.run(al_tools.argv(["git", "-C", str(facts.app), "show", f"{facts.branch}:{facts.layer_rel}/{rel}"]),
                          capture_output=True, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise AlError(f"{facts.layer_rel}/{rel} is not committed on {facts.branch}: run al gen, commit the layer, then deploy")
    return done.stdout


@dataclass
class Plan:
    style: str                      # script | home-manager | nixos
    lines: list[str]                # the plan as printed
    privileged: list[str]           # every privileged command in it
    nothing: bool                   # nothing to do
    script: str = ""                # provision.sh (script style)


def wrapper_path(facts: Facts, host: HostFacts, style: str) -> str:
    return f"{host.home}/.config/al/{facts.name}/" + ("home.nix" if style == "home-manager" else "nixos.nix")


def wrapper_text(facts: Facts, host: HostFacts, style: str) -> str:
    """The host-local Nix file that imports the committed module and sets what only the host knows."""
    decl, name = facts.decl or {}, facts.name
    module = f"{host.directory}/{facts.layer_rel}/env/hosts/{decl['id']}/{'home-manager.nix' if style == 'home-manager' else 'nixos.nix'}"
    quoted = json.dumps(module)
    if style == "home-manager":
        body = (f'  services."al-{name}".enable = true;\n'
                f'  services."al-{name}".checkout = {json.dumps(host.directory)};\n')
        hint = f"your Home Manager configuration: imports = [ {wrapper_path(facts, host, style)} ];"
    else:
        body = (f'  services."al-{name}-host".enable = true;\n'
                f'  services."al-{name}-host".user = {json.dumps(host["user"])};\n')
        hint = f"your NixOS configuration: imports = [ {wrapper_path(facts, host, style)} ];"
    return f"{WRAPPER_HEADER}\n# Import it from {hint}\n{{ ... }}: {{\n  imports = [ (/. + {quoted}) ];\n{body}}}\n"


def plan_provision(remote: Remote, facts: Facts, host: HostFacts, al: Any) -> tuple[Plan, list[Row]]:
    """What --provision would do on this host, from a read only: (the plan, the drift rows)."""
    decl = facts.decl or {}
    style = decl.get("provision", "none")
    rows = provision_rows(facts, host, al)
    if style == "script":
        text = committed(facts, f"env/hosts/{decl['id']}/provision.sh").decode()
        done = remote.read("sh -s -- --check", "the provisioning plan (provision.sh --check changes nothing)", stdin=text)
        if done.returncode != 0:
            tail = "\n".join((done.stderr or done.stdout).strip().splitlines()[-5:])
            raise AlError("provision.sh --check failed on the host" + (f":\n{tail}" if tail else ""))
        lines = done.stdout.rstrip("\n").splitlines()
        privileged = [line.strip()[len("privileged: "):] for line in lines if line.strip().startswith("privileged: ")]
        return Plan(style, lines, privileged, "provisioning: nothing to do" in done.stdout, text), rows
    tool = "home-manager" if style == "home-manager" else "nixos-rebuild"
    if not host[tool]:
        raise AlError(f"#:provision {style}: {tool} is not on the host's login PATH; install Nix and "
                      + ("Home Manager" if style == "home-manager" else "use a NixOS host") + ", or declare #:provision script")
    path = wrapper_path(facts, host, style)
    if style == "home-manager":
        lines = [f"provisioning plan ({style}):",
                 f"  write {path} (imports the checkout's env/hosts/{decl['id']}/home-manager.nix, sets checkout = {host.directory})",
                 "  home-manager switch (it must import that file; the units are the module's)"]
        return Plan(style, lines, [], False), rows
    lines = [f"provisioning plan ({style}):",
             f"  write {path} (imports the checkout's env/hosts/{decl['id']}/nixos.nix, sets user = {host['user']})",
             "  nixos-rebuild switch (the system configuration must import that file)",
             "    privileged: sudo -n nixos-rebuild switch"]
    return Plan(style, lines, ["nixos-rebuild switch"], False), rows


def apply_provision(remote: Remote, facts: Facts, host: HostFacts, plan: Plan) -> None:
    """Make the plan true. script: provision.sh --yes. Nix styles: the host-local wrapper, then the switch."""
    if plan.style == "script":
        done = remote.change("sh -s -- --yes", "apply the provisioning plan (provision.sh --yes)", stdin=plan.script)
        if not remote.dry_run:
            out = (done.stdout or "").rstrip("\n")
            plan_lines = set(plan.lines)
            for line in out.splitlines():
                if line not in plan_lines:
                    print(line)
        if done.returncode == 3:
            raise AlError(f"privilege was refused on {remote.label}: nothing else was changed (no repository update, no unit); "
                          "run the privileged commands above as a privileged user on the host, then deploy again"
                          + ("".join(f"\n  sudo {c.removeprefix('sudo -n ')}" for c in plan.privileged)))
        if done.returncode != 0:
            tail = "\n".join((done.stderr or done.stdout).strip().splitlines()[-5:])
            raise AlError(f"provisioning failed on {remote.label}" + (f":\n{tail}" if tail else ""))
        return
    must = put(remote, wrapper_path(facts, host, plan.style), wrapper_text(facts, host, plan.style))
    if must.returncode != 0:
        raise AlError(f"failed on the host: write {wrapper_path(facts, host, plan.style)}")
    if plan.style == "home-manager":
        done = remote.change("home-manager switch", "build and activate the user's Home Manager configuration")
    else:
        if not remote.dry_run:
            print("+ sudo -n nixos-rebuild switch", flush=True)
        done = remote.change("sudo -n nixos-rebuild switch", "build and activate the system configuration (privileged)")
    if done.returncode != 0:
        tail = "\n".join((done.stderr or done.stdout).strip().splitlines()[-5:])
        what = "home-manager switch" if plan.style == "home-manager" else "sudo nixos-rebuild switch"
        raise AlError(f"{what} failed on {remote.label}" + (f":\n{tail}" if tail else "")
                      + ("" if plan.style == "home-manager" else "\nrun it as a privileged user on the host: sudo nixos-rebuild switch"))


def import_hint(facts: Facts, host: HostFacts, style: str) -> str:
    path = wrapper_path(facts, host, style)
    if style == "home-manager":
        return f"add to your Home Manager configuration on the host, then deploy again:\n  imports = [ {path} ];"
    return f"add to the NixOS configuration of the host (/etc/nixos/configuration.nix), then deploy again:\n  imports = [ {path} ];"


def provision_rows(facts: Facts, host: HostFacts, al: Any, units_text: str = "", recipe: str | None = None,
                   recipe_wanted: str = "") -> list[list[str]]:
    """Provisioning drift: [fact, wanted, on host, ok | drift] for what the declaration wants. Reads nothing itself."""
    prov = provision_module()
    decl = facts.decl or {}
    style = decl.get("provision", "none")
    pinned = committed_pins(facts) if style == "script" else {}
    rows: list[list[str]] = []

    def row(fact: str, wanted: str, actual: str, ok: bool) -> None:
        rows.append([fact, wanted, actual or "-", "ok" if ok else "drift"])

    def version(tool: str, key: str) -> None:
        found = host[f"local.{tool}"] or host[key].partition(" ")[2] or ("" if not host[key] else host[key])
        if style == "script":
            want = pinned.get(tool) or prov.load_pins()[tool]["version"]
            ok = want in host[f"local.{tool}"] or want in host[key]
            row(tool, want, (found or "missing")[:40], ok)
        else:
            row(tool, "installed", (found or "missing")[:40], bool(host[key] or host[f"local.{tool}"]))

    if prov.needs_podman(facts.ir):
        row("podman rootless", "yes", "yes" if host["podman"] and host["rootless"] == "true" else "no",
            bool(host["podman"]) and host["rootless"] == "true")
        row("subids", "yes", host["subids"] or "unknown", host["subids"] == "yes")
    version("uv", "uv")
    for kind in prov.agent_kinds(facts.ir):
        tool = prov.AGENT_PIN[kind]
        version(tool, f"agent.{tool}")
    if decl.get("linger", True):
        linger = host["linger"] or parse_facts(units_text).get("linger", "")
        row("linger", "yes", linger or "no", linger == "yes")
    wanted = unit_names(facts, al)
    if units_text:
        states = {line.split()[1]: line.split()[2:] for line in units_text.splitlines() if line.startswith("unit ") and len(line.split()) > 1}
        running = [u for u in wanted if states.get(u, []) == ["enabled", "active"] or states.get(u, [None, None])[-1:] == ["active"]]
        row("units", f"{len(wanted)} active", f"{len(running)} active", len(running) == len(wanted))
    if recipe is not None:
        row("image recipe", recipe_wanted[:12], recipe[:12] or "none", recipe == recipe_wanted)
    return rows


def committed_pins(facts: Facts) -> dict[str, str]:
    """{program: version} as the committed provision.sh of the declaration pins them (what was
    generated, so drift compares the host with the declaration, not with this machine's pins file)."""
    try:
        text = committed(facts, f"env/hosts/{(facts.decl or {})['id']}/provision.sh").decode()
    except (AlError, KeyError):
        return {}
    return dict(re.findall(r"bin='?([\w.-]+)'?; version='?([\w.+-]+)'?", text))


def confirm(question: str) -> bool | None:
    """One question on the terminal; None when there is no terminal to ask."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return None


# ------------------------------------------------------------------------------------------
# the sandbox image on the host
# ------------------------------------------------------------------------------------------

def plan_images(facts: Facts, al: Any) -> tuple[str, str] | None:
    """(mode, the layer's recipe hash) when the host should get or check the sandbox image, else None.
    The hash is the runtime's (al_sandbox.recipe_hash_in) over env/ as committed on the deployed
    branch: what the host gets, and so the image its runs ask for."""
    decl = facts.decl
    if not decl or not provision_module().needs_podman(facts.ir) or not any(e.get("kind") == "podman" for e in facts.ir.get("environments") or []):
        return None
    return decl.get("images", "build"), committed_recipe(facts, al)


def committed_recipe(facts: Facts, al: Any) -> str:
    """The runtime's recipe hash of the layer's env/ as committed on facts.branch."""
    committed(facts, "env/Containerfile")           # a clear error when it is not committed
    sandbox = sandbox_module()
    with tempfile.TemporaryDirectory(prefix="al-recipe-") as directory:
        archive = subprocess.run(al_tools.argv(["git", "-C", str(facts.app), "archive", "--format=tar", facts.branch, f"{facts.layer_rel}/env"]),
                                 capture_output=True, stdin=subprocess.DEVNULL)
        if archive.returncode != 0:
            raise AlError(f"cannot read {facts.layer_rel}/env on {facts.branch}: {archive.stderr.decode(errors='replace').strip()[-300:]}")
        unpacked = subprocess.run(["tar", "-x", "-C", directory], input=archive.stdout, capture_output=True)
        if unpacked.returncode != 0:
            raise AlError(f"cannot unpack {facts.layer_rel}/env: {unpacked.stderr.decode(errors='replace').strip()[-300:]}")
        try:
            return sandbox.recipe_hash_in(Path(directory) / facts.layer_rel, facts.ir, facts.layer, al)
        except sandbox.SandboxError as exc:
            raise AlError(str(exc)) from exc


def _host_recipe(remote: Remote, facts: Facts, h: str, after_change: bool) -> str | None:
    """The recipe label of al-<name>:<h> on the host, else of al-<name>:latest. None when neither
    exists, or when only :latest exists and it is this recipe (the runtime tags :<h> at its first run)."""
    done = remote.read(sandbox_module().host_verify_script(facts.name, h), "the recipe of the sandbox image on the host",
                       after_change=after_change)
    found = parse_facts(done.stdout)
    if found.get("image") == "absent" or remote.dry_run and after_change:
        return None
    label = found.get("recipe", "")
    label = "" if label == "<no value>" else label
    if found.get("image") == "latest" and label == h:
        return None
    return label


def _mismatch(remote: Remote, facts: Facts, found: str, h: str) -> None:
    sandbox = sandbox_module()
    message = f"image: recipe {sandbox.short(found)} on the host, {sandbox.short(h)} in the layer"
    if not (facts.decl or {}).get("fallbackBuild", True):
        raise AlError(message + "; the host declares #:fallback-build #f, so nothing is built there: "
                      f"push the image of this recipe (al sandbox {facts.layer_rel} push) and deploy again")
    remote.change(sandbox.host_untag_script(facts.name, h), "drop the image of another recipe so the host builds this one")
    print(message + ": it will be built on the host at the first run")


def apply_images(remote: Remote, facts: Facts, al: Any) -> None:
    planned = plan_images(facts, al)
    if not planned:
        return
    mode, h = planned
    sandbox = sandbox_module()
    image, short = sandbox.image_ref(facts.name, h), sandbox.short(h)
    tagged = sandbox.tagged(facts.name, h)
    if mode == "pull":
        done = remote.change(sandbox.host_pull_script(facts.ir, h),
                             f"pull {sandbox.tag_of(facts.ir, h)} from the registry (address and login from the host's env file), check its recipe, tag it {tagged}")
        if remote.dry_run:
            return
        if done.returncode == 0:
            print(f"image: pulled {sandbox.tag_of(facts.ir, h)}, recipe {short} checked, tagged {tagged}")
            return
        if done.returncode == 5:
            _mismatch(remote, facts, parse_facts(done.stdout).get("recipe", ""), h)
            return
        raise AlError(f"the host could not pull {sandbox.tag_of(facts.ir, h)} (exit {done.returncode}): is it pushed "
                      f"(al sandbox {facts.layer_rel} push) and are the registry variables set in ~/.config/al/{facts.name}.env on the host?")
    if mode == "load":
        if remote.dry_run:
            remote.change_file("podman load", "", f"stream {image} (recipe {short}) over the connection")
            remote.change(sandbox.host_load_tag_script(facts.name, h), f"check the loaded image's recipe, tag it {tagged}")
        else:
            try:
                local = sandbox.ensure_local_image(facts.layer, facts.ir, print, al)
            except sandbox.SandboxError as exc:
                raise AlError(str(exc)) from exc
            if local != h:
                raise AlError(f"env/ here (recipe {sandbox.short(local)}) is not the committed one ({short}): commit it, then deploy")
            with tempfile.TemporaryDirectory(prefix="al-image-") as directory:
                archive = os.path.join(directory, "image.tar")
                saved = subprocess.run(["podman", "image", "save", "-o", archive, image], capture_output=True, text=True, stdin=subprocess.DEVNULL)
                if saved.returncode != 0:
                    raise AlError(f"podman image save failed: {saved.stderr.strip()[-300:]}")
                done = remote.change_file("podman load", archive, f"stream {image} (recipe {short}) over the connection")
            if done.returncode != 0:
                raise AlError(f"podman load failed on {remote.label}: {(done.stderr or '').strip()[-300:]}")
            done = remote.change(sandbox.host_load_tag_script(facts.name, h), f"check the loaded image's recipe, tag it {tagged}")
            if done.returncode == 5:
                _mismatch(remote, facts, parse_facts(done.stdout).get("recipe", ""), h)
                return
            if done.returncode != 0:
                raise AlError(f"tagging the loaded image failed on {remote.label}: {(done.stderr or '').strip()[-300:]}")
    found = _host_recipe(remote, facts, h, after_change=mode == "load")
    if remote.dry_run:
        return
    if found is None:
        print(f"image: none on the host yet; it will be built there at the first run (recipe {short})")
    elif found == h:
        print(f"image: {tagged} on the host are built from this recipe ({short})" + (", loaded over the connection" if mode == "load" else ""))
    else:
        _mismatch(remote, facts, found, h)


# ------------------------------------------------------------------------------------------
# al deploy
# ------------------------------------------------------------------------------------------

def generated_where(facts: Facts, al: Any, host: HostFacts | None = None) -> str:
    units = ", ".join(f"~/.config/systemd/user/{u}" for u in unit_names(facts, al)) or "nothing (the layer declares no cron or webhook trigger and no dashboard)"
    racket = (host.tools.get("racket") if host is not None else "") or ""
    return ("generated here, committed, travels with the repository: agentic-layer.json, commands/, hops/, adws/ (the runtime), env/\n"
            f"generated on the host, for the host's paths, never committed: {units}\n"
            "not on the host, not needed to run: bin/al, .al-manifest.local.json, systemd/ in the layer\n"
            "racket on the host: needed only for the dashboard's actions (create, adopt, gen); "
            + (f"found at {racket}" if racket else "not found"))


def print_preflight(rows: list[Row]) -> None:
    width = max(len(r.check) for r in rows)
    for row in rows:
        print(f"  {row.check.ljust(width)}  {row.state.ljust(10)}  {row.detail}".rstrip())
        if row.state == "MISSING":
            print(f"  {''.ljust(width)}  fix: {row.fix}")


def deploy(a: Any, al: Any) -> int:
    facts = local_facts(a.layer, al)
    facts.decl = host_decl(facts.ir, getattr(a, "as_id", None))
    style = (facts.decl or {}).get("provision", "none")
    provisioning = bool(getattr(a, "provision", False))
    if provisioning and not facts.decl:
        raise AlError("--provision needs a (host #:id ID #:provision script|home-manager|nixos …) declaration in agentic.rkt")
    if provisioning and style == "none":
        raise AlError(f"(host #:id {facts.decl['id']}) declares #:provision none: there is nothing to provision; "
                      "declare #:provision script, home-manager or nixos")
    remote = Remote(transport_for(a.to), dry_run=a.dry_run)
    rows, host = preflight(remote, facts, a.path)
    directory = host.directory
    print(f"host: {remote.label}   directory: {directory}   layer: {facts.layer_rel}   commit: {facts.commit[:12]}"
          + (f"   as: {facts.decl['id']}" if facts.decl else ""))
    print(generated_where(facts, al, host))
    for note in facts.notes:
        print(f"note: {note}")
    print("preflight:")
    print_preflight(rows)

    plan = None
    if provisioning:
        plan, _ = plan_provision(remote, facts, host, al)
        print("\n".join(plan.lines))
        if plan.nothing:
            pass                    # provision.sh said so: "provisioning: nothing to do"
        elif not getattr(a, "yes", False) and not a.dry_run:
            answer = confirm(f"apply this plan on {remote.label}? [y/N] ")
            if answer is None:
                print("al deploy: provisioning needs --yes when no terminal can answer; nothing was changed", file=sys.stderr)
                return 4
            if not answer:
                raise AlError(f"nothing was changed on {remote.label}")
        if plan.style == "script" and not plan.nothing:
            apply_provision(remote, facts, host, plan)
            if not a.dry_run:
                rows, host = preflight(remote, facts, a.path)
                print("preflight after provisioning:")
                print_preflight(rows)

    secrets, missing = secret_rows(facts, host)
    if secrets:
        print("secrets (names only; al never reads or copies a value):")
        print(table(["variable", "for", "on host", ""], secrets))
    if missing:
        print(f"missing on the host (al never copies secrets): {', '.join(missing)}\n"
              f"put each as NAME=value in ~/.config/al/{facts.name}.env on the host (chmod 600); the units read that file")
    misses = sum(1 for r in rows if r.state == "MISSING") + len(missing)
    nix_later = provisioning and style in ("home-manager", "nixos")
    if misses and not a.dry_run and not (nix_later and not missing):
        if facts.decl and style != "none" and not provisioning and any(r.state == "MISSING" for r in rows):
            print(f"provision it: al deploy {a.layer} --to {remote.label} --as {facts.decl['id']} --provision")
        raise AlError(f"nothing was changed on {remote.label}: {misses} preflight miss(es)")

    actions, notes = plan_repository(facts, host, remote)
    for note in notes:
        print(f"note: {note}")
    apply_repository(actions, facts, host, remote)
    if nix_later and plan is not None:
        apply_provision(remote, facts, host, plan)
        if not a.dry_run:
            rows, host = preflight(remote, facts, a.path)
            print("preflight after provisioning:")
            print_preflight(rows)
            left = sum(1 for r in rows if r.state == "MISSING")
            if left:
                print(import_hint(facts, host, style))
                raise AlError(f"{left} preflight miss(es) after provisioning on {remote.label}")
    apply_images(remote, facts, al)
    units = host_units(facts, host, directory, al)
    for trigger in facts.ir.get("triggers") or []:
        if trigger.get("kind") == "webhook" and not is_loopback(str(trigger.get("bind") or "")):
            print(f"warning: the webhook listens on {trigger['bind']}:{trigger['port']} (the layer's #:bind); "
                  "keep its secret set and put it behind a reverse proxy or a private network")
    if units:
        install_units(remote, facts, host, units, enable=not a.no_enable, write=style != "home-manager")
    done = put(remote, tools_record(facts, host), json.dumps(host.tools, sort_keys=True) + "\n", "0600")
    if done.returncode != 0:
        print(f"warning: could not record the tools' paths in {tools_record(facts, host)}; --status cannot report drift")
    if a.dry_run:
        print(f"dry run: nothing was changed on {remote.label}")
        if misses:
            print(f"would stop: {misses} preflight miss(es)")
            return 1
        return 0
    print(f"watch it: al dashboard {a.layer} --remote {remote.label}")
    return 0


def is_loopback(bind: str) -> bool:
    return bind in ("localhost", "::1") or bind.startswith("127.")


# ------------------------------------------------------------------------------------------
# al deploy --status
# ------------------------------------------------------------------------------------------

def _timestamp(text: Any) -> float | None:
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def run_lines(states: list[dict], al: Any, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    lines = ["in flight:"]
    running = [s for s in states if s.get("status") == "running"]
    for s in sorted(running, key=lambda s: str(s.get("started") or "")):
        beat = _timestamp(s.get("heartbeat"))
        if beat is None:
            age = "no heartbeat recorded"
        elif now - beat >= al.DIED_ANYWHERE:
            age = f"no heartbeat for {int((now - beat) // 60)} min — died?"
        else:
            age = f"heartbeat {max(0, int(now - beat))} s ago"
        lines.append(f"  {s.get('adw_id', '?')}  {s.get('adw', '?')}  started {s.get('started', '?')}  {age}")
    if not running:
        lines.append("  nothing")
    lines.append("last finished:")
    finished = sorted((s for s in states if s.get("status") in ("passed", "handed-over", "failed") and s.get("ended")),
                      key=lambda s: _timestamp(s.get("ended")) or 0, reverse=True)[:5]
    for s in finished:
        action = (s.get("ship") or {}).get("action") if isinstance(s.get("ship"), dict) else ""
        prompt = " ".join(str(s.get("prompt") or "").split())[:60]
        lines.append(f"  {s.get('adw_id', '?')}  {s.get('adw', '?')}  {s['status']}" + (f"  {action}" if action else "")
                     + f"  ended {s['ended']}  {prompt}".rstrip())
    if not finished:
        lines.append("  nothing")
    return lines


def status(a: Any, al: Any) -> int:
    """Reads only; nothing on the host or here is changed."""
    facts = local_facts(a.layer, al)
    several = ""
    try:
        facts.decl = host_decl(facts.ir, getattr(a, "as_id", None))
    except AlError:
        if getattr(a, "as_id", None):
            raise
        several = ", ".join(h["id"] for h in facts.ir.get("hosts") or [])
    remote = Remote(transport_for(a.to), read_only=True)
    units = unit_names(facts, al)
    script = 'echo "home=$HOME"\n' + deployment_script(facts, a.path) + ("\n" + units_status_script(units) if units else "")
    script += ("\n" + al_tools.shell_finder() + "\n" + "\n".join(tool_lines(facts))
               + f'\necho "tools_json=$(cat "$HOME/.config/al/{facts.name}-tools.json" 2>/dev/null)"')
    done = remote.read(script, "what is deployed, and the units")
    host = HostFacts(parse_facts(done.stdout))
    if not host.home or not host.directory:
        tail = " ".join((done.stderr or "").strip().splitlines()[-3:])
        raise AlError(f"cannot run commands on {remote.label}" + (f": {tail}" if tail else f" (exit {done.returncode})"))
    if host["dir"] != "git":
        raise AlError(f"nothing is deployed at {host.directory} on {remote.label}")
    head = host["head"]
    if head == facts.commit:
        compare = "same"
    else:
        compare = "differs"
        if head and al.git(facts.app, "cat-file", "-t", head) == "commit":
            counts = al.git(facts.app, "rev-list", "--left-right", "--count", f"{head}...{facts.commit}").split()
            if len(counts) == 2:
                compare += f", local is {counts[1]} ahead / {counts[0]} behind"
    print(f"host: {remote.label}   directory: {host.directory}   layer: {facts.layer_rel}")
    print(f"deployed: {head[:12] or '?'}   local {facts.branch}: {facts.commit[:12]}   ({compare})")
    if int(host["dirty"] or 0):
        print(f"note: the deployment has local changes ({host['dirty']} files); the next deploy stops until they are committed or discarded")
    if units:
        print("units:\n" + units_table(done.stdout))
    print("\n".join(tool_drift(host)))
    agents = f'{directory_expr(facts, a.path)}/{shlex.quote(facts.layer_rel)}/agents'
    runs = remote.read(f"ls -t {agents}/*/state.json 2>/dev/null | head -n 30 | while read -r f; do cat \"$f\" 2>/dev/null; printf '\\n\\036\\n'; done",
                       "the newest run states")
    states = []
    for chunk in runs.stdout.split(RECORD_SEPARATOR):
        try:
            state = json.loads(chunk)
        except ValueError:
            continue                # half-written or unreadable: skipped
        if isinstance(state, dict):
            states.append(state)
    print("\n".join(run_lines(states, al)))
    if several:
        print(f"provisioning: the layer declares several hosts ({several}); add --as ID for the drift table")
    elif facts.decl:
        pre = HostFacts(parse_facts(remote.read(preflight_script(facts, a.path), "preflight: what the host has, for the drift table").stdout))
        planned = plan_images(facts, al)
        recipe = wanted = None
        if planned:
            wanted = planned[1]
            recipe = _host_recipe(remote, facts, wanted, after_change=False) or ""
        drift = provision_rows(facts, pre, al, done.stdout if units else "", recipe, wanted or "")
        print(f"provisioning (host {facts.decl['id']}, #:provision {facts.decl.get('provision', 'none')}):")
        print(table(["fact", "wanted", "on host", ""], drift))
        drifted = sum(1 for r in drift if r[3] != "ok")
        if drifted and facts.decl.get("provision", "none") != "none":
            print(f"{drifted} fact(s) drifted: al deploy {a.layer} --to {remote.label} --as {facts.decl['id']} --provision")
    return 0


def tool_drift(host: HostFacts) -> list[str]:
    """The tools table of --status (now, at deploy) and a `drift:` line per tool that moved or vanished."""
    try:
        recorded = json.loads(host["tools_json"]) if host["tools_json"] else None
    except ValueError:
        recorded = None
    now = host.tools
    if not isinstance(recorded, dict):
        return ["tools:", table(["tool", "now", "at deploy"], [[t, p or "not found", "?"] for t, p in sorted(now.items())]),
                "  (no record of the deploy's tools: deployed by an older al; the next deploy records them)"]
    names = sorted(set(now) | set(recorded))
    lines = ["tools:", table(["tool", "now", "at deploy"],
                             [[t, now.get(t) or "not found", str(recorded.get(t) or "not found")] for t in names])]
    for name in names:
        was, is_now = str(recorded.get(name) or ""), now.get(name) or ""
        if was and not is_now:
            lines.append(f"drift: {name} vanished: was {was}")
        elif was and is_now != was:
            lines.append(f"drift: {name} moved: was {was}, now {is_now}; run al deploy again so the units' PATH follows")
    return lines


def main(a: Any, al: Any) -> int:
    try:
        if a.status:
            if a.dry_run or a.no_enable or getattr(a, "provision", False) or getattr(a, "yes", False):
                raise AlError("--status only reads; it does not go with --dry-run, --no-enable, --provision or --yes")
            return status(a, al)
        return deploy(a, al)
    except AlError as exc:
        raise al.AlError(str(exc)) from exc


# ------------------------------------------------------------------------------------------
# al dashboard --remote
# ------------------------------------------------------------------------------------------

def _answers(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(url, timeout=2).close()
        return True
    except urllib.error.HTTPError:
        return True                 # 401/403 from a dashboard with a token: it answers
    except (OSError, ValueError):
        return False


def _dashboard_remote(a: Any, al: Any) -> int:
    if a.bind or a.token or a.podman:
        raise AlError("--bind, --token and --podman configure a dashboard on this machine; --remote only opens a tunnel "
                      "to the one running on the host (loopback on both ends)")
    transport = transport_for(a.remote)
    declared, host_port = None, 0
    for d in a.dirs or [Path(".")]:
        if (Path(d) / "agentic-layer.json").is_file():
            ir = load_ir(Path(d))
            declared = ir.get("dashboard") or al.DEFAULT_DASHBOARD
            hosts = ir.get("hosts") or []
            if len(hosts) == 1 and hosts[0].get("dashboardPort"):
                host_port = int(hosts[0]["dashboardPort"])       # the only host declaration moves the dashboard
            break
    remote_port = a.remote_port or host_port or int((declared or {}).get("port") or 8765)
    local_port = free_port(a.port or 8765, explicit=a.port is not None)
    argv = transport.tunnel_argv(local_port, remote_port)
    if os.environ.get("AL_DASHBOARD_DRY_RUN"):
        print(json.dumps({"argv": argv, "local_port": local_port, "remote_port": remote_port}))
        return 0
    url = f"http://127.0.0.1:{local_port}/"

    def interrupt(*_: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    process = None
    with tempfile.TemporaryFile(mode="w+") as errors:
        try:
            try:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stderr=errors)
            except OSError as exc:
                raise AlError(f"cannot run ssh: {exc}") from exc
            deadline = time.monotonic() + 30
            while not _answers(url):
                if process.poll() is not None or time.monotonic() > deadline:
                    errors.seek(0)
                    tail = " ".join(errors.read().strip().splitlines()[-3:])
                    what = (f"ssh to {transport.label} ended (exit {process.returncode})" if process.poll() is not None
                            else f"nothing answered on port {remote_port} of {transport.label} within 30 s")
                    raise AlError(what + (f": {tail}" if tail else "") + f"\n{STATUS_HINT}")
                time.sleep(0.2)
            print(f"dashboard: {url}   (tunnel to {transport.label}, Ctrl-C closes it)", flush=True)
            if declared and (declared.get("tokenEnv") or declared.get("kind") == "podman"):
                name = load_ir(next(Path(d) for d in a.dirs if (Path(d) / "agentic-layer.json").is_file()))["name"]
                where = f"the value of {declared['tokenEnv']} on the host" if declared.get("tokenEnv") and declared.get("kind") != "podman" else \
                    f"journalctl --user -u al-{name}-dashboard on the host"
                print(f"it asks for its access token: {where}", flush=True)
            if a.open:
                import webbrowser
                webbrowser.open(url)
            return process.wait()
        except KeyboardInterrupt:
            return 130
        finally:                    # the tunnel is always closed
            signal.signal(signal.SIGTERM, previous)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def dashboard_remote(a: Any, al: Any) -> int:
    try:
        return _dashboard_remote(a, al)
    except AlError as exc:
        raise al.AlError(str(exc)) from exc
