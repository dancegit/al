"""`al doctor --platform`: what this host can run.

Only asks the host's own tools about themselves (`podman version`, `podman info`, `podman machine
list`, `nix --version`, `nix config show experimental-features`), each with a 20 s timeout, and looks
browsers up on PATH; it runs nothing of any repository and changes nothing. `facts()` takes the
lookups as arguments so tests can describe any host. The output names no path of this machine.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
from typing import Any, Callable

TIMEOUT = 20
BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "msedge")
ZERO_TOUCH_RULE = "a workflow that ships without a human must have a podman option in its environment stack"

# Stated facts per platform (docs/GUIDE.md "Where it runs: platforms" holds the same table).
PLATFORM_NOTES = {
    "Linux": ["podman runs natively; rootless podman is what al expects",
              "the browser feature of a nix-devshell option (#:browser #t) works: Chromium from nixpkgs"],
    "Linux-arm64": ["images must exist for linux/arm64: most official images do, and a repository's own "
                    "Containerfile must build there"],
    "Darwin": ["podman runs in a virtual machine (podman machine): bind mounts are slower, and only folders the "
               "machine shares (your home directory) can be mounted: keep the application, the layer and its "
               "worktrees under your home",
               "published ports are forwarded by the machine to the host's 127.0.0.1",
               "the browser feature of a nix-devshell option is Linux-only: use #:browser #t on a podman option"],
    "Windows": ["run al inside WSL: the podman machine on Windows is WSL-based, and the application and the layer "
                "must live in the WSL filesystem (not under /mnt/c)",
                "published ports are forwarded by the machine to the host's 127.0.0.1"],
}


def _call(run: Callable[..., Any], argv: list[str]) -> tuple[int, str]:
    try:
        cp = run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return int(cp.returncode), str(cp.stdout or "")


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _arch(machine: str) -> str:
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine.lower(), machine)


def facts(which: Callable[[str], str | None] = shutil.which, system: Callable[[], str] = platform.system,
          machine: Callable[[], str] = platform.machine, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    os_name, arch = system(), _arch(machine())
    vm = os_name in ("Darwin", "Windows")
    notes = list(PLATFORM_NOTES.get(os_name, [f"{os_name}: not a platform al is tested on"]))
    if os_name == "Linux" and arch == "aarch64":
        notes += PLATFORM_NOTES["Linux-arm64"]

    podman: dict[str, Any] = {"found": bool(which("podman")), "version": "", "rootless": None, "machine": None,
                              "usable": False, "problem": ""}
    if podman["found"]:
        code, out = _call(run, ["podman", "version", "--format", "json"])
        data = _json(out) if code == 0 else None
        podman["version"] = str(((data or {}).get("Client") or {}).get("Version") or "")
        if vm:
            code, out = _call(run, ["podman", "machine", "list", "--format", "json"])
            machines = [m for m in (_json(out) or []) if isinstance(m, dict)] if code == 0 else []
            chosen = next((m for m in machines if m.get("Default")), machines[0] if machines else None)
            podman["machine"] = {"name": str(chosen.get("Name", "")), "running": bool(chosen.get("Running"))} if chosen \
                else {"name": "", "running": False}
            if not machines:
                podman["problem"] = "no podman machine: create one with `podman machine init`, then `podman machine start`"
            elif not podman["machine"]["running"]:
                podman["problem"] = "the podman machine is stopped: start it with `podman machine start`"
        if not podman["problem"]:
            code, out = _call(run, ["podman", "info", "--format", "json"])
            info = _json(out) if code == 0 else None
            if isinstance(info, dict):
                podman["rootless"] = bool(((info.get("host") or {}).get("security") or {}).get("rootless"))
                podman["usable"] = True
            else:
                podman["problem"] = "`podman info` failed: podman is installed but not working"
    else:
        podman["problem"] = "podman is not installed"

    nix: dict[str, Any] = {"found": bool(which("nix")), "version": "", "flakes": False, "problem": ""}
    if nix["found"]:
        code, out = _call(run, ["nix", "--version"])
        nix["version"] = out.strip().split()[-1] if code == 0 and out.strip() else ""
        code, out = _call(run, ["nix", "config", "show", "experimental-features"])
        if code != 0:
            code, out = _call(run, ["nix", "show-config"])
            out = next((line.split("=", 1)[1] for line in out.splitlines()
                        if line.strip().startswith("experimental-features")), "") if code == 0 else ""
        nix["flakes"] = "flakes" in out.split()
        if not nix["flakes"]:
            nix["problem"] = ("flakes are not enabled: add `experimental-features = nix-command flakes` to "
                              "nix.conf (a nix-devshell option runs `nix develop` on a flake)")
    else:
        nix["problem"] = "nix is not installed"

    browsers = [name for name in BROWSERS if which(name)]
    kinds = ["worktree"]
    if nix["found"] and nix["flakes"]:
        kinds.append("nix-devshell")
    if podman["usable"]:
        kinds.append("podman")
    zero_touch = "podman" in kinds
    return {
        "os": os_name, "arch": arch, "podman": podman, "nix": nix, "browsers": browsers,
        "kinds_available": kinds,
        "kinds_unavailable": {k: p for k, p in (("nix-devshell", nix["problem"]), ("podman", podman["problem"])) if k not in kinds},
        "browser_feature": {"nix-devshell": os_name == "Linux" and "nix-devshell" in kinds, "podman": "podman" in kinds},
        "zero_touch": zero_touch,
        "zero_touch_reason": ("a workflow that ships without a person can run here (it runs in podman)" if zero_touch
                              else f"no: the compiler rule \"{ZERO_TOUCH_RULE}\" needs a working podman"),
        "notes": notes,
    }


def render(f: dict[str, Any]) -> str:
    """The text report, made only from facts()."""
    p, n = f["podman"], f["nix"]
    if p["found"]:
        podman = (p["version"] or "found") + (", rootless" if p["rootless"] else ", NOT rootless" if p["rootless"] is False else "")
        if p["machine"] is not None:
            podman += f"; machine {p['machine']['name'] or '(none)'} {'running' if p['machine']['running'] else 'stopped'}"
    else:
        podman = "not found"
    nix = ((n["version"] or "found") + (", flakes enabled" if n["flakes"] else ", flakes NOT enabled")) if n["found"] else "not found"
    rows = [("os", f"{f['os']} {f['arch']}"), ("podman", podman), ("nix", nix),
            ("host browser", ", ".join(f["browsers"]) or "none on PATH"),
            ("environment kinds", ", ".join(f["kinds_available"])),
            ("browser feature", ", ".join(k for k, ok in f["browser_feature"].items() if ok) or "none"),
            ("zero-touch", "yes" if f["zero_touch"] else "no")]
    out = ["al doctor --platform: what this host can run", ""]
    out += [f"  {label.ljust(18)} {value}" for label, value in rows]
    if f["kinds_unavailable"]:
        out += ["", "not available here:"] + [f"  - {kind}: {why}" for kind, why in f["kinds_unavailable"].items()]
    out += ["", f"zero-touch: {f['zero_touch_reason']}", "", "notes:"] + [f"  - {note}" for note in f["notes"]]
    return "\n".join(out)
