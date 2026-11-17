"""`al doctor --platform`: the platform facts of a host and what can run there. Simulated hosts are
described to `al_platform.facts()` through its lookups (which, system, machine, run); the last test
asks this host."""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from types import SimpleNamespace

from conftest import ROOT, al

sys.path.insert(0, str(ROOT / "tools"))
import al_platform  # noqa: E402


def host(os_name: str, arch: str, binaries: set[str], answers: dict[str, tuple[int, str]]):
    """facts() of a simulated host: `answers` maps a command line to (exit code, stdout)."""
    asked: list[str] = []

    def run(argv, **_kw):
        line = " ".join(argv)
        asked.append(line)
        code, out = answers.get(line, (1, ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    found = al_platform.facts(which=lambda name: f"/usr/bin/{name}" if name in binaries else None,
                              system=lambda: os_name, machine=lambda: arch, run=run)
    return found, asked


PODMAN = {"podman version --format json": (0, json.dumps({"Client": {"Version": "5.2.1"}})),
          "podman info --format json": (0, json.dumps({"host": {"security": {"rootless": True}}}))}
NIX = {"nix --version": (0, "nix (Nix) 2.24.9\n"),
       "nix config show experimental-features": (0, "flakes nix-command\n")}


def machines(running: bool) -> dict[str, tuple[int, str]]:
    return {"podman machine list --format json": (0, json.dumps([{"Name": "podman-machine-default", "Default": True,
                                                                   "Running": running}]))}


def test_linux_x86_64_with_podman_and_nix():
    f, asked = host("Linux", "x86_64", {"podman", "nix", "git"}, {**PODMAN, **NIX})
    assert f["kinds_available"] == ["worktree", "nix-devshell", "podman"]
    assert f["zero_touch"] is True and f["podman"]["rootless"] is True and f["nix"]["flakes"] is True
    assert f["podman"]["version"] == "5.2.1" and f["nix"]["version"] == "2.24.9"
    assert f["browser_feature"] == {"nix-devshell": True, "podman": True}
    assert "podman machine list --format json" not in asked  # Linux has no machine


def test_linux_arm64():
    f, _ = host("Linux", "aarch64", {"podman", "nix"}, {**PODMAN, **NIX})
    assert f["arch"] == "aarch64"
    assert any("linux/arm64" in n for n in f["notes"])
    assert f["browser_feature"]["nix-devshell"] is True
    assert any("browser feature" in n and "works" in n for n in f["notes"])


def test_macos_podman_machine_running():
    f, _ = host("Darwin", "arm64", {"podman", "nix"}, {**PODMAN, **NIX, **machines(True)})
    assert f["arch"] == "aarch64" and f["podman"]["machine"] == {"name": "podman-machine-default", "running": True}
    assert f["kinds_available"] == ["worktree", "nix-devshell", "podman"] and f["zero_touch"] is True
    text = " ".join(f["notes"])
    assert "bind mounts are slower" in text and "home directory" in text
    assert "127.0.0.1" in text and "Linux-only" in text
    assert f["browser_feature"] == {"nix-devshell": False, "podman": True}


def test_macos_podman_machine_stopped():
    f, asked = host("Darwin", "arm64", {"podman"}, {**PODMAN, **machines(False)})
    assert f["kinds_available"] == ["worktree"] and f["zero_touch"] is False
    assert "podman machine start" in f["kinds_unavailable"]["podman"]
    assert "podman info --format json" not in asked
    assert "podman machine start" in al_platform.render(f)


def test_windows_through_podman_machine():
    f, _ = host("Windows", "AMD64", {"podman"}, {**PODMAN, **machines(True)})
    assert f["arch"] == "x86_64" and "podman" in f["kinds_available"]
    text = " ".join(f["notes"])
    assert "WSL" in text and "WSL filesystem" in text


def test_neither_podman_nor_nix():
    f, asked = host("Linux", "x86_64", {"git"}, {})
    assert f["kinds_available"] == ["worktree"] and f["zero_touch"] is False
    assert "a workflow that ships without a human must have a podman option" in f["zero_touch_reason"]
    assert asked == []
    text = al_platform.render(f)
    assert "zero-touch         no" in text and "nix is not installed" in text


def test_nix_without_flakes():
    f, _ = host("Linux", "x86_64", {"nix"}, {"nix --version": (0, "nix (Nix) 2.18.1\n"),
                                             "nix config show experimental-features": (1, ""),
                                             "nix show-config": (0, "experimental-features = nix-command\n")})
    assert f["nix"]["found"] is True and f["nix"]["flakes"] is False
    assert "nix-devshell" not in f["kinds_available"]
    assert "flakes are not enabled" in f["kinds_unavailable"]["nix-devshell"]


def test_doctor_on_this_host():
    text = al("doctor", "--platform").stdout
    data = json.loads(al("doctor", "--platform", "--json").stdout)
    assert data["os"] == platform.system()
    assert data["arch"] == {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine())
    assert text.rstrip("\n") == al_platform.render(data)
    assert str(Path.home()) not in text and str(Path.home()) not in json.dumps(data)
