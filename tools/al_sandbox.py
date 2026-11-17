"""The sandbox image of a layer on another machine: `al sandbox LAYER push|pull`, and the scripts
`al deploy` runs on a host to fetch and check the image.

    al sandbox LAYER push [--dry-run]   build al-<name>:<recipe hash> if needed, push it to the (registry …)
    al sandbox LAYER pull [--dry-run]   pull the image of this recipe, check it, tag it al-<name>:<hash> and :latest

The recipe hash is the runtime's (`adw_modules/sandbox.py: recipe_hash`: 12 hex digits over the
Containerfile, the files it copies from `env/` and the base image), taken from the layer's own
generated runtime, so the image pushed, pulled or loaded is exactly `al-<name>:<hash>` that a run on
the host asks for. The local build is the runtime's (`ensure_image`): it labels the image
`al.recipe=<hash>` and points `al-<name>:latest` at it. The registry tag is `recipe-<hash>`. An image
is only used when its label equals the layer's hash, so a host runs exactly the image that was built
(and tested) from the same recipe.

The registry address, user and password are values of the variables (registry …) names, read from
the environment, then `local.env` in the layer, then `$XDG_CONFIG_HOME/al/<name>.env`
(kit/runtime/adw_modules/localenv.py). None of them is printed or put on a command line: the
password reaches `podman login --password-stdin` on standard input, and the login goes to a
temporary authfile that is always removed.

Standard library only.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
LABEL = "al.recipe"
INSPECT_LABEL = '{{index .Labels "al.recipe"}}'


class SandboxError(Exception):
    pass


def image_name(name: str) -> str:
    """The moving tag the runtime points at the current recipe's image."""
    return f"al-{name}:latest"


def image_ref(name: str, h: str) -> str:
    """The image a run of the layer asks for: al-<name>:<recipe hash>."""
    return f"al-{name}:{h}"


def short(h: str) -> str:
    return (h or "none")[:12]


# ------------------------------------------------------------------------------------------
# the layer's own runtime: one recipe hash and one build for runs, `al sandbox` and deploys
# ------------------------------------------------------------------------------------------

_AL: Any = None


def _al_module() -> Any:
    """tools/al as a module (when this file is used without it, e.g. from a test)."""
    global _AL
    if _AL is None:
        loader = importlib.machinery.SourceFileLoader("al_cli", str(ROOT / "tools" / "al"))
        spec = importlib.util.spec_from_loader("al_cli", loader)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        loader.exec_module(module)
        _AL = module
    return _AL


def runtime(layer: Path, al: Any = None) -> tuple[Any, Any, Any]:
    """(sandbox module, loaded layer, errors module) of the layer's own generated runtime."""
    al = al or _al_module()
    layer = Path(layer).resolve()
    sandbox, ir_module, errors, _ = al.layer_sandbox_module(layer)
    try:
        return sandbox, ir_module.load(layer), errors
    except errors.LayerError as exc:
        raise SandboxError(str(exc)) from exc


@dataclass(frozen=True)
class _Recipe:
    """What the runtime's recipe_hash reads of a layer: its IR (the base image) and its env/."""
    ir: dict
    layer_dir: Path

    @property
    def name(self) -> str:
        return str(self.ir.get("name") or self.layer_dir.name)


def recipe_hash(layer: Path, al: Any = None) -> str:
    """The runtime's recipe hash of the layer as it is on this machine."""
    sandbox, loaded, errors = runtime(layer, al)
    try:
        return sandbox.recipe_hash(loaded)
    except errors.LayerError as exc:
        raise SandboxError(str(exc)) from exc


def recipe_hash_in(env_parent: Path, ir: dict, layer: Path, al: Any = None) -> str:
    """The runtime's recipe hash of the env/ below env_parent (e.g. the committed one, extracted),
    with the base image of `ir`; computed by the runtime of `layer`."""
    sandbox, _, errors = runtime(layer, al)
    try:
        return sandbox.recipe_hash(_Recipe(ir, Path(env_parent)))
    except errors.LayerError as exc:
        raise SandboxError(str(exc)) from exc


def ensure_local_image(layer: Path, ir: dict, say: Callable[[str], None] = print, al: Any = None) -> str:
    """al-<name>:<hash> on this machine, built by the runtime (labelled with the recipe, :latest
    pointed at it) when it is missing. Returns the recipe hash."""
    sandbox, loaded, errors = runtime(layer, al)
    try:
        h = sandbox.recipe_hash(loaded)
        ref = image_ref(ir["name"], h)
        if sandbox.image_exists(ref):
            say(f"image: {ref} is built from this recipe")
        else:
            say(f"image: {ref} is missing; building it from env/Containerfile")
        return str(sandbox.ensure_image(loaded, say=say)["recipe"])
    except (errors.LayerError, errors.StepFailed) as exc:
        raise SandboxError(str(exc)) from exc


def podman(*args: str, stdin: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(["podman", *args], input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=3600)
    except OSError as exc:
        raise SandboxError(f"cannot run podman: {exc}") from exc
    if check and done.returncode != 0:
        tail = " ".join((done.stderr or done.stdout).strip().splitlines()[-3:])
        raise SandboxError(f"podman {args[0]} failed" + (f": {tail}" if tail else ""))
    return done


# ------------------------------------------------------------------------------------------
# the registry
# ------------------------------------------------------------------------------------------

@dataclass
class Registry:
    address: str
    username: str
    password: str
    tls_verify: bool

    @property
    def server(self) -> str:
        return self.address.split("/", 1)[0]


def _localenv() -> Any:
    runtime = str(ROOT / "kit" / "runtime")
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    from adw_modules import localenv
    return localenv


@dataclass(frozen=True)
class _LayerRef:
    name: str
    layer_dir: Path


def registry_of(layer: Path, ir: dict) -> Registry:
    r = ir.get("registry")
    if not isinstance(r, dict):
        raise SandboxError("the layer declares no (registry #:address-env \"VARIABLE\"); add one to agentic.rkt and run al gen")
    localenv = _localenv()
    values = localenv.load(_LayerRef(ir["name"], Path(layer)))
    address = localenv.lookup(r["addressEnv"], values).strip().rstrip("/")
    if not address:
        raise SandboxError(f"{r['addressEnv']} is not set: put {r['addressEnv']}=<registry>/<prefix> in the environment, "
                           f"in local.env of the layer or in ~/.config/al/{ir['name']}.env")
    user = localenv.lookup(r["usernameEnv"], values) if r.get("usernameEnv") else ""
    password = localenv.lookup(r["passwordEnv"], values) if r.get("passwordEnv") else ""
    if r.get("usernameEnv") and not (user and password):
        raise SandboxError(f"{r['usernameEnv']} and {r['passwordEnv']} must both be set for the registry login")
    return Registry(address, user, password, bool(r.get("tlsVerify", True)))


def registry_ref(ir: dict, address: str, h: str) -> str:
    return f"{address}/{ir['name']}:recipe-{h}"


def tag_of(ir: dict, h: str) -> str:
    """The reference without the address: what may be printed."""
    return f"{ir['name']}:recipe-{h}"


def tagged(name: str, h: str) -> str:
    return f"{image_ref(name, h)} and {image_name(name)}"


def _with_login(reg: Registry, work: Callable[[list[str]], None]) -> None:
    """Run `work` with the podman options for this registry; a login goes to a temporary authfile
    that is removed afterwards, whatever happens."""
    options = [] if reg.tls_verify else ["--tls-verify=false"]
    if not reg.username:
        work(options)
        return
    directory = tempfile.mkdtemp(prefix="al-registry-")
    authfile = os.path.join(directory, "auth.json")
    try:
        podman("login", "--authfile", authfile, *options, "--username", reg.username, "--password-stdin", reg.server,
               stdin=reg.password, check=True)
        work(["--authfile", authfile, *options])
    finally:
        if os.path.exists(authfile):
            os.unlink(authfile)
        os.rmdir(directory)


def push(layer: Path, ir: dict, *, dry_run: bool = False, say: Callable[[str], None] = print, al: Any = None) -> int:
    reg = registry_of(layer, ir)
    if dry_run:
        h = recipe_hash(layer, al)
        say(f"would build {image_ref(ir['name'], h)} if needed (recipe {h}) and push it to the registry as {tag_of(ir, h)}")
        return 0
    h = ensure_local_image(layer, ir, say, al)
    ref = registry_ref(ir, reg.address, h)
    podman("tag", image_ref(ir["name"], h), ref, check=True)
    _with_login(reg, lambda options: podman("push", *options, ref, check=True))
    say(f"pushed {image_ref(ir['name'], h)} to the registry as {tag_of(ir, h)} (recipe {h})")
    return 0


def pull(layer: Path, ir: dict, *, dry_run: bool = False, say: Callable[[str], None] = print, al: Any = None) -> int:
    reg = registry_of(layer, ir)
    h = recipe_hash(layer, al)
    name = ir["name"]
    if dry_run:
        say(f"would pull {tag_of(ir, h)} from the registry, check its recipe and tag it {tagged(name, h)}")
        return 0
    ref = registry_ref(ir, reg.address, h)
    _with_login(reg, lambda options: podman("pull", *options, ref, check=True))
    label = podman("image", "inspect", "--format", INSPECT_LABEL, ref).stdout.strip()
    if label != h:
        raise SandboxError(f"the pulled image was built from recipe {short(label if label != '<no value>' else '')}, "
                           f"the layer's recipe is {h}: not tagged {tagged(name, h)}")
    podman("tag", ref, image_ref(name, h), check=True)
    podman("tag", ref, image_name(name), check=True)
    say(f"pulled {tag_of(ir, h)}: recipe {h} checked, tagged {tagged(name, h)}")
    return 0


# ------------------------------------------------------------------------------------------
# on the host (scripts al deploy runs)
# ------------------------------------------------------------------------------------------

def host_verify_script(name: str, h: str) -> str:
    """A read: the recipe label of al-<name>:<h> on the host (`recipe=<label>`); without it, that
    of al-<name>:latest (`image=latest`); without either `recipe=` and `image=absent`."""
    image, latest, fmt = shlex.quote(image_ref(name, h)), shlex.quote(image_name(name)), shlex.quote(INSPECT_LABEL)
    return (f"if podman image exists {image} 2>/dev/null; then "
            f"echo \"recipe=$(podman image inspect --format {fmt} {image} 2>/dev/null)\"; "
            f"elif podman image exists {latest} 2>/dev/null; then "
            f"echo \"recipe=$(podman image inspect --format {fmt} {latest} 2>/dev/null)\"; echo image=latest; "
            "else echo recipe=; echo image=absent; fi")


def host_tag_lines(name: str, h: str, source: str) -> list[str]:
    """Check the al.recipe label of `source` (a shell word) on the host, then tag it
    al-<name>:<h> and al-<name>:latest; exit 5 with `recipe=<label>` when the label differs."""
    return [f"label=$(podman image inspect --format {shlex.quote(INSPECT_LABEL)} {source} 2>/dev/null)",
            f'if [ "$label" != {h} ]; then echo "recipe=$label"; exit 5; fi',
            f"podman tag {source} {shlex.quote(image_ref(name, h))} && podman tag {source} {shlex.quote(image_name(name))} "
            '&& echo "recipe=$label"']


def host_load_tag_script(name: str, h: str) -> str:
    """After `podman load` of al-<name>:<h>: check its label, tag :<h> and :latest."""
    return "\n".join(host_tag_lines(name, h, shlex.quote(image_ref(name, h))))


def host_untag_script(name: str, h: str) -> str:
    """Drop al-<name>:<h> and al-<name>:latest (an image of another recipe), so the host builds."""
    return " ; ".join(f"podman untag {shlex.quote(ref)} 2>/dev/null" for ref in (image_ref(name, h), image_name(name))) + " ; true"


def host_pull_script(ir: dict, h: str) -> str:
    """A change on the host: pull this recipe's image from the registry whose address (and login)
    are in the host's ~/.config/al/<name>.env, check the label, tag it al-<name>:<h> and
    al-<name>:latest. The values stay on the host: they are read there and never printed."""
    r = ir.get("registry") or {}
    name = ir["name"]
    env_file = f'"$HOME/.config/al/{name}.env"'

    def value(var: str) -> str:
        return f"$(sed -n 's/^{var}=//p' {env_file} 2>/dev/null | head -n 1)"

    tls = "" if r.get("tlsVerify", True) else " --tls-verify=false"
    lines = [f'addr="{value(r.get("addressEnv", "AL_REGISTRY"))}"',
             f'test -n "$addr" || {{ echo "{r.get("addressEnv", "AL_REGISTRY")} is not set in ~/.config/al/{name}.env on the host" >&2; exit 2; }}',
             f'ref="${{addr%/}}/{name}:recipe-{h}"',
             "auth="]
    if r.get("usernameEnv"):
        lines += [f'user="{value(r["usernameEnv"])}"',
                  'dir=$(mktemp -d); auth="--authfile $dir/auth.json"',
                  "trap 'rm -rf \"$dir\"' EXIT",
                  f"sed -n 's/^{r['passwordEnv']}=//p' {env_file} | head -n 1 | tr -d '\\n' | "
                  f'podman login $auth{tls} --username "$user" --password-stdin "${{addr%%/*}}" >/dev/null || exit 3']
    lines += [f'podman pull -q $auth{tls} "$ref" >/dev/null || exit 4', *host_tag_lines(name, h, '"$ref"')]
    return "\n".join(lines)


def main(a: Any, al: Any) -> int:
    """al sandbox LAYER push|pull (reached from tools/al's sandbox_cli)."""
    try:
        layer = Path(a.layer).resolve()
        try:
            ir = json.loads((layer / "agentic-layer.json").read_text())
        except OSError as exc:
            raise SandboxError(f"{layer} has no agentic-layer.json: run al gen first") from exc
        if a.action == "push":
            return push(layer, ir, dry_run=a.dry_run, al=al)
        return pull(layer, ir, dry_run=a.dry_run, al=al)
    except (SandboxError, OSError, ValueError) as exc:
        raise al.AlError(str(exc)) from exc
