"""A container engine for the sandbox: `(environment podman … #:nested-engine #t)`.

Tests that build or start containers need an engine. The run's container gets a rootless podman
of its own, which:

- stores everything (images, layers, containers) in one volume per run, mounted at /al-engine and
  removed with the run;
- cannot see the host's containers, images or socket: it is a separate rootless engine whose
  storage is that volume, and no host socket is mounted (the `engine:isolation` row checks it);
- starts every inner container in the run container's own namespaces and cgroup
  (containers.conf below), so the run's network mode and its memory, CPU and process limits cover
  everything it starts.

Compared with an ordinary run container exactly two flags are added (FLAG_REASONS): /dev/fuse and
the engine volume. Every capability stays dropped, no-new-privileges, keep-id, the default seccomp
and AppArmor profiles and the masked /proc paths stay as they are. The inner engine runs with a
single uid mapping (the keep-id user has no /etc/subuid range inside), so `newuidmap`, a setuid
binary, is never used: inner images whose processes switch to another user do not work.

The protected envs.py passes `podman_flags(...)` in EnvStack._podman_base; until a person applies
that one line (docs/DESIGN.md "An engine for the sandbox"), the preflight's `engine:fuse` row
misses with a fix naming it, so a nested-engine run never starts half-configured.

The generator (`tools/al`) loads this file by path and takes CONTAINERS_CONF, STORAGE_CONF and
PACKAGES from it, so the image recipe and the runtime share one source. Standard library only, and
no package-relative import at module level (the generator loads it outside its package).
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

ENGINE_DIR = "/al-engine"
PACKAGES = ("podman", "fuse-overlayfs")
ROWS = ("engine", "engine:fuse", "engine:storage", "engine:isolation", "engine:userns")
NO_FUSE = "the run's container has no /dev/fuse"

CONTAINERS_CONF = """\
# Written by al for (environment podman … #:nested-engine #t): the configuration of the run
# container's own rootless podman. Every inner container stays inside the run's container.

[containers]
# Inner containers use the run container's network namespace. It is already private to the run and
# follows its #:network, so no /dev/net/tun (and no new network) is needed.
netns = "host"
# crun bind-mounts the outer /proc instead of mounting a new procfs, which podman's masked /proc
# paths forbid; so the run container needs no `unmask`.
pidns = "host"
# The run container's single-uid user namespace: no newuidmap (setuid), no capability.
userns = "host"
# No new IPC, UTS or cgroup namespaces: creating them needs nothing the run container may not do,
# and none of them would add isolation from the run itself.
ipcns = "host"
utsns = "host"
cgroupns = "host"
# Inner containers create no cgroup: every process stays in the run container's cgroup, so
# #:memory, #:cpus and #:pids-limit apply to everything the engine starts.
cgroups = "disabled"
# Logs are plain files in the engine's storage, no journald inside a container.
log_driver = "k8s-file"

[engine]
# No systemd inside the container: cgroups are managed (here: not created) by podman itself.
cgroup_manager = "cgroupfs"
# Events go to a file in the engine's storage, no journald.
events_logger = "file"
# crun runs rootless without a setuid helper.
runtime = "crun"
"""

STORAGE_CONF = """\
# Written by al for (environment podman … #:nested-engine #t): where the run container's own podman
# stores images and containers.

[storage]
# Overlay layers, mounted by fuse-overlayfs (below).
driver = "overlay"
# The per-run engine volume al-engine-<layer>-<run>, mounted at /al-engine and removed with the run.
rootless_storage_path = "/al-engine/storage"

[storage.options.overlay]
# The kernel's overlay cannot stack on the container's own overlay root, and the vfs driver copies
# every layer in full: fuse-overlayfs through /dev/fuse.
mount_program = "/usr/bin/fuse-overlayfs"
# One uid inside: files an image owns as another user become the engine user's instead of failing.
ignore_chown_errors = "true"
"""

ADDED = ("--device /dev/fuse", f"-v al-engine-<layer>-<run>:{ENGINE_DIR}:U")
KEPT = ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--userns=keep-id", "seccomp", "apparmor",
        "masked /proc", "--memory/--cpus/--pids-limit")

FLAG_REASONS: dict[str, str] = {
    # added
    "--device /dev/fuse": "fuse-overlayfs mounts the inner engine's image layers. The kernel's overlay cannot "
                          "stack on the container's own overlay root, and the vfs driver copies every layer in full.",
    f"-v al-engine-<layer>-<run>:{ENGINE_DIR}:U": "the inner engine's own storage, one volume per run, owned by the "
                                                  "run's user, removed with the run.",
    # kept
    "--cap-drop=ALL": "kept: the inner engine runs with a single uid mapping, so it never runs newuidmap or "
                      "another setuid binary and needs no capability.",
    "--security-opt=no-new-privileges": "kept: single uid mapping, no newuidmap and no setuid exec, so nothing "
                                        "inside needs to gain privileges.",
    "--userns=keep-id": "kept: the inner engine's user is the run's user, which owns the worktree and the "
                        "engine volume (:U).",
    "seccomp": "kept: podman's default seccomp profile allows the unprivileged unshare(CLONE_NEWUSER) rootless "
               "podman needs; nothing else is opened.",
    "apparmor": "kept: the default AppArmor profile stays; a host whose profile forbids user namespaces gets an "
                "engine:userns preflight miss instead of a weaker profile.",
    "masked /proc": "kept: the inner engine uses pidns=\"host\", so crun bind-mounts the outer /proc instead of "
                    "mounting a new procfs, and no masked path has to be unmasked.",
    "--memory/--cpus/--pids-limit": "kept: inner containers use cgroups=\"disabled\" and stay in the run "
                                    "container's cgroup, so these limits cover them.",
}

# host paths, module level so tests can point them elsewhere
HOST_FUSE = Path("/dev/fuse")
MAX_USER_NAMESPACES = Path("/proc/sys/user/max_user_namespaces")
APPARMOR_USERNS = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def enabled(option: dict[str, Any] | None) -> bool:
    return bool(option and option.get("nestedEngine"))


def volume_name(layer: Any, state: Any) -> str:
    return f"al-engine-{layer.name}-{state.adw_id}"


def podman_flags(option: dict[str, Any] | None, layer: Any, state: Any) -> list[str]:
    """What the protected envs.EnvStack._podman_base adds for this option: nothing without the
    flag, else exactly /dev/fuse and the run's engine volume."""
    if not enabled(option):
        return []
    return ["--device", "/dev/fuse", "-v", f"{volume_name(layer, state)}:{ENGINE_DIR}:U"]


def _podman(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def prepare(layer: Any, state: Any, option: dict[str, Any] | None) -> None:
    """Create the run's engine volume (labelled like every resource the runtime starts). A volume
    that already exists (a resumed run) is fine; any other failure stops the run at preflight."""
    if not enabled(option):
        return
    name = volume_name(layer, state)
    result = _podman(["volume", "create", "--label", f"al.layer={layer.name}",
                      "--label", f"al.layer-dir={Path(layer.layer_dir).resolve()}",
                      "--label", f"al.run={state.adw_id}", name])
    if result is not None and result.returncode == 0:
        return
    message = "podman is not available" if result is None else (result.stderr or result.stdout).strip()
    if "already exists" in message:
        return
    from .errors import StepFailed  # imported here: the generator loads this file on its own, by path

    raise StepFailed("preflight", f"cannot create the nested engine's volume {name}: {message}")


def remove_volume(layer: Any, state: Any) -> None:
    """Remove the run's engine volume when it exists. Prints a failure, never raises."""
    name = volume_name(layer, state)
    exists = _podman(["volume", "exists", name])
    if exists is None or exists.returncode != 0:
        return
    removed = _podman(["volume", "rm", "-f", name])
    if removed is None or removed.returncode != 0:
        detail = "podman is not available" if removed is None else (removed.stderr or removed.stdout).strip()
        print(f"[adw {state.adw_id}] nested engine: could not remove volume {name}: {detail}")
    else:
        print(f"[adw {state.adw_id}] nested engine: volume removed: {name}")


def collect_stale_volumes(layer: Any) -> list[str]:
    """Remove engine volumes of this layer directory whose run is not alive (a crashed run's)."""
    from . import envs, sandbox  # sandbox imports this module

    layer_dir = Path(layer.layer_dir).resolve()
    listed = _podman(["volume", "ls", "--format", "json", "--filter", f"label=al.layer-dir={layer_dir}"])
    if listed is None or listed.returncode != 0:
        return []
    try:
        volumes = json.loads(listed.stdout or "[]") or []
    except ValueError:
        return []
    leases = sandbox._leases(layer_dir)
    removed: list[str] = []
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        name = str(volume.get("Name") or "")
        labels = volume.get("Labels") or {}
        run = str(labels.get("al.run") or "")
        if not name.startswith("al-engine-") or labels.get("al.layer-dir") != str(layer_dir) or not run:
            continue
        if envs._run_alive(layer_dir, run, leases):
            continue
        result = _podman(["volume", "rm", "-f", name])
        if result is not None and result.returncode == 0:
            removed.append(name)
    return removed


def doctor_lines(option: dict[str, Any] | None) -> list[str]:
    """POSIX sh lines for the doctor script (its `r NAME STATUS DETAIL` helper), only with the
    flag. They name a socket path or a variable, never a variable's value."""
    if not enabled(option):
        return []
    q = shlex.quote
    sockets = "/run/podman/podman.sock /run/user/*/podman/podman.sock /var/run/docker.sock"
    return [
        "if command -v podman >/dev/null 2>&1; then r engine ok \"$(command -v podman)\"; "
        f"else r engine miss {q('podman is not in the image')}; fi",
        f"if [ -c /dev/fuse ]; then r engine:fuse ok {q('/dev/fuse is there')}; "
        f"else r engine:fuse miss {q(NO_FUSE)}; fi",
        f"if [ ! -w {ENGINE_DIR} ]; then r engine:storage miss {q(ENGINE_DIR + ' is not writable')}; "
        "else root=$(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null); case \"$root\" in "
        f"{ENGINE_DIR}/*) r engine:storage ok \"$root\";; "
        f"*) r engine:storage miss \"the engine stores in ${{root:-(unknown)}}, not under {ENGINE_DIR}\";; esac; fi",
        f"found=''; for s in {sockets}; do if [ -e \"$s\" ]; then found=$s; break; fi; done; "
        "if [ -n \"$found\" ]; then r engine:isolation miss \"a host engine socket is reachable: $found\"; "
        "elif [ -n \"${CONTAINER_HOST:-}\" ]; then r engine:isolation miss 'CONTAINER_HOST is set'; "
        "elif [ -n \"${DOCKER_HOST:-}\" ]; then r engine:isolation miss 'DOCKER_HOST is set'; "
        "else r engine:isolation ok 'no host engine socket, CONTAINER_HOST or DOCKER_HOST'; fi",
        "if out=$(podman unshare true 2>&1); then r engine:userns ok 'podman unshare works'; "
        "else r engine:userns miss \"$(printf '%s\\n' \"$out\" | tr '\\t' ' ' | tail -n 1)\"; fi",
    ]


def host_checks(option: dict[str, Any] | None) -> list[Any]:
    """Host-side rows, only with the flag: /dev/fuse and user namespaces on this machine."""
    if not enabled(option):
        return []
    from .sandbox import Check  # sandbox imports this module

    checks = [Check("engine:host-fuse", "ok", f"{HOST_FUSE} exists on this machine") if HOST_FUSE.exists()
              else Check("engine:host-fuse", "miss", f"{HOST_FUSE} does not exist on this machine")]
    try:
        limit = int(MAX_USER_NAMESPACES.read_text().strip())
    except (OSError, ValueError):
        limit = None
    restricted = ""
    try:
        if APPARMOR_USERNS.read_text().strip() == "1":
            restricted = ("; kernel.apparmor_restrict_unprivileged_userns=1: the container's AppArmor profile must "
                          "allow user namespaces (engine:userns shows whether it does)")
    except OSError:
        pass
    if limit is None:
        checks.append(Check("engine:host-userns", "skip", f"{MAX_USER_NAMESPACES} is not readable" + restricted))
    elif limit > 0:
        checks.append(Check("engine:host-userns", "ok", f"user.max_user_namespaces={limit}" + restricted))
    else:
        checks.append(Check("engine:host-userns", "miss", "user.max_user_namespaces=0" + restricted))
    return checks


USERNS_FIX = ("the host does not let the container create a user namespace. Raise user.max_user_namespaces, or, "
              "where kernel.apparmor_restrict_unprivileged_userns=1, allow user namespaces for podman's container "
              "profile. al does not turn AppArmor or seccomp off for a run")


def fixes(check: Any, option: dict[str, Any] | None) -> str:
    """The `fix` text of an `engine*` row."""
    ident = str((option or {}).get("id") or "")
    name = str(getattr(check, "name", ""))
    if name == "engine":
        return (f"run al gen: the image gets podman and fuse-overlayfs for "
                f"(environment podman #:id {ident} #:nested-engine #t)")
    if name == "engine:fuse":
        return ("the protected kit/runtime/adw_modules/envs.py does not pass the nested-engine flags yet: a person "
                "applies the change in docs/DESIGN.md \"An engine for the sandbox\"; until then this environment "
                "cannot run")
    if name in ("engine:userns", "engine:host-userns"):
        return USERNS_FIX
    if name == "engine:isolation":
        return "remove the socket mount or variable; a nested engine never talks to the host's"
    if name == "engine:host-fuse":
        return "load the fuse module on the host (modprobe fuse)"
    if name == "engine:storage":
        return (f"run al gen (the image's /etc/containers/storage.conf puts the engine's storage under {ENGINE_DIR}) "
                f"and check the run's volume with podman volume inspect")
    return ""
