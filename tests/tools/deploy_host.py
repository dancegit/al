"""Test helper (not a test): a temporary directory that plays the machine `al deploy` deploys to,
and a recording fake `ssh`.

The host's PATH is its private bin/ only: symlinks to the real POSIX programs the remote scripts
may call (al_deploy.REMOTE_TOOLS) and stubs for everything that would touch this machine
(systemctl, loginctl, podman, uv, the agent CLI, id). Nothing a test deploys can reach the real
systemd of whoever runs the suite.
"""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# loaded by its file path, like tests/tools/test_explain.py loads tools/explain.py
_spec = importlib.util.spec_from_file_location("al_deploy", ROOT / "tools" / "al_deploy.py")
al_deploy = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("al_deploy", al_deploy)
_spec.loader.exec_module(al_deploy)  # type: ignore[union-attr]

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
    "claude": 'echo "0.0.0 (stub)"\n',
    "id": f'echo "{HOST_USER}"\n',
}


class Host:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / ".hostbin"
        self.path = str(self.bin)
        self.calls = root / ".calls"
        self.bin.mkdir(parents=True)
        self.calls.mkdir()
        for tool in (*al_deploy.REMOTE_TOOLS, "git-receive-pack", "git-upload-pack"):
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

    def env(self) -> dict[str, str]:
        return {"AL_DEPLOY_LOCAL_HOST": str(self.root), "AL_DEPLOY_LOCAL_PATH": self.path}

    def snapshot(self) -> list[tuple[str, str]]:
        """Every file under the host with its sha256 (the stubs' call logs aside): for "changes nothing"."""
        found = []
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if rel.parts[0] == ".calls" or path.is_dir() and not path.is_symlink():
                continue
            found.append((str(rel), "link" if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()))
        return found


def make_host(tmp_path: Path) -> Host:
    return Host(tmp_path / "host-home")


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
