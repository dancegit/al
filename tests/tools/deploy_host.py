"""Test helper (not a test): a temporary directory that plays the machine `al deploy` deploys to,
and a recording fake `ssh`.

The host's PATH is its private bin/ only: symlinks to the real POSIX programs the remote scripts
may call (al_deploy.REMOTE_TOOLS) and stubs for everything that would touch this machine
(systemctl, loginctl, podman, uv, the agent CLI, id). Nothing a test deploys can reach the real
systemd of whoever runs the suite.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# loaded by its file path, like tests/tools/test_explain.py loads tools/explain.py
_spec = importlib.util.spec_from_file_location("al_deploy", ROOT / "tools" / "al_deploy.py")
al_deploy = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("al_deploy", al_deploy)
_spec.loader.exec_module(al_deploy)  # type: ignore[union-attr]

_pspec = importlib.util.spec_from_file_location("al_provision", ROOT / "tools" / "al_provision.py")
al_provision = importlib.util.module_from_spec(_pspec)
sys.modules.setdefault("al_provision", al_provision)
_pspec.loader.exec_module(al_provision)  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_podman  # noqa: E402

HOST_USER = "alhostuser"

STUBS = {
    # a login shell would bring this machine's own PATH (its /etc/profile): `sh -lc` runs as `sh -c`
    "sh": '''case "$1" in -lc) shift; exec {real_sh} -c "$@";; esac
exec {real_sh} "$@"
''',
    "systemctl": '''[ "$1" = "--user" ] && shift
case "$1" in
  show-environment) if [ -f "$root/.systemd-env" ]; then cat "$root/.systemd-env"; fi;;
  enable) shift; mkdir -p "$root/.units"; for u in "$@"; do : > "$root/.units/$u.enabled"; done;;
  restart) shift; mkdir -p "$root/.units"; for u in "$@"; do : > "$root/.units/$u.active"; done;;
  is-enabled) if [ -e "$root/.units/$2.enabled" ]; then echo enabled; else echo disabled; exit 3; fi;;
  is-active) if [ -e "$root/.units/$2.active" ]; then echo active; else echo inactive; exit 3; fi;;
esac
exit 0
''',
    "loginctl": '''case "$1" in
  show-user) if [ -e "$root/.linger" ]; then echo Linger=yes; else echo Linger=no; fi;;
  enable-linger) if [ -e "$root/.linger-denied" ]; then echo "Access denied" >&2; exit 1; fi; : > "$root/.linger";;
esac
exit 0
''',
    "podman": '''case "$1" in
  --version) echo "podman version 5.0.0-stub";;
  info) if [ -f "$root/.rootless" ]; then cat "$root/.rootless"; else echo true; fi;;
esac
exit 0
''',
    "uv": 'echo "uv 0.0.0-stub"\n',
    # privilege: refused when .sudo-denied exists; otherwise the command runs (as this user: it is a fake host)
    "sudo": '''if [ -e "$root/.sudo-denied" ]; then echo "sudo: a password is required" >&2; exit 1; fi
[ "$1" = "-n" ] && shift
exec "$@"
''',
    "getsubids": 'if [ -e "$root/.subids-missing" ]; then exit 1; fi\necho "0: $1 100000 65536"\n',
    "usermod": 'rm -f "$root/.subids-missing"\n',
    # serves https://…/<name> from $root/.downloads/<name>; .curl-tamper alters the bytes on the way
    "curl": '''out=""; url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; -*) shift;; *) url="$1"; shift;; esac; done
src="$root/.downloads/${url##*/}"
[ -f "$src" ] || { echo "curl: (22) 404 $url" >&2; exit 22; }
if [ -e "$root/.curl-tamper" ]; then { cat "$src"; echo tampered; } > "$out"; else cat "$src" > "$out"; fi
''',
    "home-manager": 'exit 0\n',
    "nixos-rebuild": 'exit 0\n',
    "claude": 'echo "0.0.0 (stub)"\n',
    "id": f'echo "{HOST_USER}"\n',
    # the fake host's free space: ample, so the preflight does not depend on the disk of the machine running the tests
    "df": 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\necho "/dev/x 209715200 1 104857600 1% /"\n',
}


class Host:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / ".hostbin"
        self.path = str(self.bin)              # the login PATH; ~/.local/bin is a per-user folder al_tools searches
        self.calls = root / ".calls"
        self.bin.mkdir(parents=True)
        self.calls.mkdir()
        for tool in dict.fromkeys((*al_deploy.REMOTE_TOOLS, *al_provision.SCRIPT_TOOLS, "gzip", "git-receive-pack", "git-upload-pack")):
            real = shutil.which(tool)
            if real and tool not in STUBS:
                (self.bin / tool).symlink_to(real)
        for name, body in STUBS.items():
            self.stub(name, body.replace("{real_sh}", shutil.which("sh") or "/bin/sh"), log=name != "sh")

    def stub(self, name: str, body: str, log: bool = True, folder: Path | None = None) -> Path:
        """bin/<name> (or <folder>/<name>, e.g. the fake home's .local/bin, which is not on the login
        PATH): a small sh script; with `log` each call appends its arguments to .calls/<name>.log."""
        directory = folder if folder is not None else self.bin
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if path.is_symlink() or path.exists():
            path.unlink()
        record = f'echo "$*" >> "$root/.calls/{name}.log"\n' if log else ""
        path.write_text(f'#!/bin/sh\nroot="{self.root}"\n{record}{body}')
        path.chmod(0o755)
        return path

    def runtime(self) -> "Host":
        """Make the host able to run a layer's scripts: `python3`, the programs a run calls, and a `uv`
        that runs `uv run [--quiet] SCRIPT ARGS…` with this python (any other use answers like the stub)."""
        for tool in ("bash", "sleep", "true", "false", "echo", "date", "uname", "cut", "wc", "sort", "basename", "touch",
                     "tail", "cp", "mv", "find", "xargs", "tee", "readlink"):
            real = shutil.which(tool)
            if real and not (self.bin / tool).exists():
                (self.bin / tool).symlink_to(real)
        self.stub("python3", f'exec {sys.executable} "$@"\n', log=False)
        self.stub("uv", 'if [ "$1" = run ]; then shift; [ "$1" = --quiet ] && shift; '
                        f'exec {sys.executable} "$@"; fi\necho "uv 0.0.0-stub"\n')
        return self

    def vanish(self) -> None:
        """Every command on the host fails from now on (its programs are gone); back() undoes it."""
        self.bin.rename(self.root / ".hostbin.gone")

    def back(self) -> None:
        (self.root / ".hostbin.gone").rename(self.bin)

    def remove(self, tool: str) -> None:
        (self.bin / tool).unlink()

    def log(self, name: str) -> list[str]:
        path = self.calls / f"{name}.log"
        return path.read_text().splitlines() if path.is_file() else []

    def set_env_file(self, name: str, **variables: str) -> Path:
        path = self.root / ".config" / "al" / f"{name}.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}={v}\n" for k, v in variables.items()))
        path.chmod(0o600)
        return path

    def login(self, kind: str = "claude-code") -> Path:
        path = self.root / al_deploy.AGENT_LOGIN[kind][1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"stub": true}\n')
        return path

    def family(self, name: str, have: tuple[str, ...] = ("git", "curl", "ca-certificates")) -> None:
        """A package manager of the family (debian: apt-get + dpkg-query, fedora: dnf + rpm, arch:
        pacman) whose installs are recorded in .pkgs. Packages in `have` are installed already;
        without `podman` the podman stub waits in .stash until the podman package is installed."""
        (self.root / ".pkgs").write_text("".join(f"{p}\n" for p in have))
        stash = self.root / ".stash"
        stash.mkdir(exist_ok=True)
        if "podman" not in have and (self.bin / "podman").exists():
            shutil.move(str(self.bin / "podman"), str(stash / "podman"))
        install = '''for p in "$@"; do case "$p" in -*) continue;; esac
  echo "$p" >> "$root/.pkgs"
  if [ "$p" = podman ] && [ -f "$root/.stash/podman" ]; then mv "$root/.stash/podman" "$root/.hostbin/podman"; fi
done
'''
        query = 'grep -qx "$p" "$root/.pkgs"'
        if name == "debian":
            self.stub("apt-get", 'shift\n' + install)                     # apt-get install -y … PKGS
            self.stub("dpkg-query", 'for p; do :; done\nif ' + query + '; then echo "install ok installed"; else exit 1; fi\n')
        elif name == "fedora":
            self.stub("dnf", 'shift\n' + install)
            self.stub("rpm", 'for p; do :; done\n' + query + '\n')
        elif name == "arch":
            self.stub("pacman", 'case "$1" in -Q) p="$2"; ' + query + '; exit $?;; esac\nshift\n' + install)
        else:
            raise ValueError(name)

    def serve_downloads(self, files: dict[str, bytes]) -> None:
        """What the fake curl serves, by the last part of the URL."""
        directory = self.root / ".downloads"
        directory.mkdir(exist_ok=True)
        for filename, data in files.items():
            (directory / filename).write_bytes(data)

    def podman(self, registry: Path | None = None) -> Path:
        """Replace the podman stub with the stateful fake (tests/tools/fake_podman.py); its state is .podman."""
        state = self.root / ".podman"
        fake_podman.install(self.bin, state, registry)
        return state

    def env(self) -> dict[str, str]:
        return {"AL_DEPLOY_LOCAL_HOST": str(self.root), "AL_DEPLOY_LOCAL_PATH": self.path}

    def snapshot(self) -> list[tuple[str, str]]:
        """Every file under the host with its sha256 (the stubs' and the fake podman's call logs aside):
        for "changes nothing"."""
        found = []
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if rel.parts[0] == ".calls" or rel == Path(".podman/calls.log") or path.is_dir() and not path.is_symlink():
                continue
            found.append((str(rel), "link" if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()))
        return found


def _tar_gz(member: str, data: bytes) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(member)
        info.size, info.mode, info.mtime = len(data), 0o755, 0
        tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as handle:
        handle.write(raw.getvalue())
    return out.getvalue()


# What the fake curl serves, and the pins that match it (al gen with AL_HOST_PINS=test_pins(…)).
DOWNLOADS = {
    "uv-9.9.1.tar.gz": _tar_gz("uv-9.9.1/uv", b'#!/bin/sh\necho "uv 9.9.1 (fake)"\n'),
    "claude-9.9.2": b'#!/bin/sh\necho "9.9.2 (Claude Code, fake)"\n',
    "codex-9.9.3.tar.gz": _tar_gz("codex-9.9.3", b'#!/bin/sh\necho "codex-cli 9.9.3"\n'),
}


def test_pins(directory: Path) -> Path:
    def asset(filename: str, member: str) -> dict:
        one = {"url": f"https://downloads.invalid/{filename}", "sha256": hashlib.sha256(DOWNLOADS[filename]).hexdigest(),
               "member": member}
        return {"x86_64": one, "aarch64": one}
    pins = {"uv": {"version": "9.9.1", "bin": "uv", "assets": asset("uv-9.9.1.tar.gz", "uv-9.9.1/uv")},
            "claude": {"version": "9.9.2", "bin": "claude", "assets": asset("claude-9.9.2", "")},
            "codex": {"version": "9.9.3", "bin": "codex", "assets": asset("codex-9.9.3.tar.gz", "codex-9.9.3")}}
    path = Path(directory) / "pins.json"
    path.write_text(json.dumps(pins, indent=1))
    return path


test_pins.__test__ = False      # a helper, not a test


def make_host(tmp_path: Path) -> Host:
    return Host(tmp_path / "host-home")


def make_hosts(tmp_path: Path, ids: tuple[str, ...] | list[str]) -> dict[str, Host]:
    """{id: Host(tmp_path/hosts/<id>)}: the homes of the hosts of a (placement …), where the runtime's
    hostlink finds them with AL_HOSTS_LOCAL=tmp_path/hosts."""
    (tmp_path / "hosts").mkdir(exist_ok=True)
    return {ident: Host(tmp_path / "hosts" / ident) for ident in ids}


FAKE_SSH = '''#!/usr/bin/env python3
"""A recording ssh for tests: runs the command in the test host's directory instead of on a machine."""
import http.server, json, os, subprocess, sys
LOG, HOME, PATH = {log!r}, {home!r}, {path!r}
args, forward = sys.argv[1:], None
with open(LOG, "a") as handle:
    handle.write(json.dumps(sys.argv) + "\\n")
i = 0
while i < len(args) and args[i].startswith("-"):
    if args[i] in ("-o", "-L"):
        if args[i] == "-L":
            forward = args[i + 1]
        i += 1
    i += 1
destination, rest = args[i], args[i + 1:]
if forward:
    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a):
            pass
    host, port = forward.split(":")[:2]
    server = http.server.HTTPServer((host, int(port)), Ok)
    with open(LOG + ".pid", "w") as handle:
        handle.write(str(os.getpid()))
    server.serve_forever()
sys.exit(subprocess.run(["sh", "-c", " ".join(rest)], env={{"HOME": HOME, "PATH": PATH, "LANG": "C"}}, cwd=HOME).returncode)
'''


def make_fake_ssh(tmp_path: Path, host: Host) -> tuple[Path, Path]:
    """(a directory to put first on PATH, the log): `ssh` there appends its argv as one JSON line to
    the log, skips its options, and runs the remote command with sh under the host's HOME and PATH
    (stdin passes through). With -L 127.0.0.1:P:… it serves `200 ok` on 127.0.0.1:P until it is
    terminated and writes its pid to <log>.pid."""
    bin_dir, log = tmp_path / "fake-ssh", tmp_path / "fake-ssh.log"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(FAKE_SSH.format(log=str(log), home=str(host.root), path=host.path))
    (bin_dir / "ssh").chmod(0o755)
    return bin_dir, log
