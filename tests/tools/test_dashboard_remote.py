"""`al dashboard --remote HOST`: an ssh tunnel, loopback on both ends, to the dashboard of a deployed
layer. The ssh here is the recording fake of deploy_host.py; nothing leaves this machine.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import AL, LAYER_TEMPLATE, al
from deploy_host import al_deploy, make_fake_ssh, make_host

HOST = "testhost.invalid"


def listener(port: int = 0) -> socket.socket | None:
    """A socket listening on 127.0.0.1:<port>, or None when something else already has the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)        # a just-closed connection does not count as taken
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    sock.listen(1)
    return sock


def bindable(port: int) -> bool:
    sock = listener(port)
    if sock:
        sock.close()
    return sock is not None


def test_free_port_keeps_a_free_default_and_moves_off_a_taken_one():
    taken = listener()
    try:
        port = taken.getsockname()[1]
        moved = al_deploy.free_port(port, explicit=False)
        assert moved != port and bindable(moved)
        with pytest.raises(al_deploy.AlError, match=f"port {port} is in use on this machine"):
            al_deploy.free_port(port, explicit=True)
    finally:
        taken.close()
    assert al_deploy.free_port(port, explicit=True) == port == al_deploy.free_port(port, explicit=False)


def remote(*args, ok=True, env=None):
    return al("dashboard", *args, env={"AL_DASHBOARD_DRY_RUN": "1", "AL_DEPLOY_LOCAL_HOST": "", **(env or {})}, ok=ok)


def test_tunnel_command_and_port_selection(in_repo):
    (in_repo / "agentic.rkt").write_text(LAYER_TEMPLATE.format(
        root="..", placement='(layer in-repo #:dir "agentic")\n  (dashboard local #:port 9200)'))
    al("gen", in_repo)
    ours = listener(8765)               # None: this machine already runs something on 8765, which serves the test as well
    try:
        shown = json.loads(remote(in_repo, "--remote", HOST).stdout)
        assert shown["remote_port"] == 9200
        assert shown["local_port"] != 8765 and bindable(shown["local_port"])
        assert shown["argv"] == al_deploy.SshTransport(HOST).tunnel_argv(shown["local_port"], 9200)
        assert shown["argv"] == ["ssh", "-N", "-T", "-o", "ExitOnForwardFailure=yes",
                                 "-L", f"127.0.0.1:{shown['local_port']}:127.0.0.1:9200", HOST]
        cp = remote(in_repo, "--remote", HOST, "--port", "8765", ok=False)
        assert cp.returncode == 1 and "port 8765 is in use on this machine" in cp.stderr
    finally:
        if ours:
            ours.close()
    if ours:
        assert json.loads(remote(in_repo, "--remote", HOST).stdout)["local_port"] == 8765
    free = listener()
    port = free.getsockname()[1]
    free.close()
    shown = json.loads(remote(in_repo, "--remote", HOST, "--remote-port", "9000", "--port", str(port)).stdout)
    assert (shown["local_port"], shown["remote_port"]) == (port, 9000)
    for extra in (["--bind", "0.0.0.0"], ["--podman"], ["--token", "t"]):
        cp = remote(in_repo, "--remote", HOST, *extra, ok=False)
        assert cp.returncode == 1 and "--remote only opens a tunnel" in cp.stderr
    cp = remote(in_repo, "--remote=-oProxyCommand=x", ok=False)
    assert cp.returncode == 1 and "not a host" in cp.stderr
    cp = remote(in_repo, "--remote-port", "9000", ok=False)
    assert cp.returncode == 1 and "--remote-port goes with --remote HOST" in cp.stderr
    local = json.loads(remote(in_repo).stdout)                       # without --remote: the local dashboard, as before
    assert "--port" in local["argv"] and local["argv"][local["argv"].index("--port") + 1] == "8765"


def test_a_directory_without_a_layer_tunnels_to_the_default_port(tmp_path):
    shown = json.loads(remote(tmp_path, "--remote", HOST).stdout)
    assert shown["remote_port"] == 8765 and shown["argv"][-1] == HOST


def test_tunnel_opens_waits_prints_and_closes_on_interrupt(tmp_path):
    host = make_host(tmp_path)
    fake, log = make_fake_ssh(tmp_path, host)
    free = listener()
    port = free.getsockname()[1]
    free.close()
    env = {k: v for k, v in os.environ.items() if k not in ("AL_DASHBOARD_DRY_RUN", "AL_DEPLOY_LOCAL_HOST", "AL_APP_ROOT")}
    env["PATH"] = f"{fake}{os.pathsep}{env['PATH']}"
    proc = subprocess.Popen([str(AL), "dashboard", str(tmp_path), "--remote", HOST, "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        line = proc.stdout.readline()
        assert line.startswith(f"dashboard: http://127.0.0.1:{port}/") and f"tunnel to {HOST}" in line, line + proc.stderr.read()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.status == 200 and response.read() == b"ok"
        pid = int(Path(f"{log}.pid").read_text())
        os.kill(pid, 0)                                               # the tunnel is up
        os.killpg(proc.pid, signal.SIGINT)                            # Ctrl-C reaches the whole foreground group
        assert proc.wait(timeout=20) == 130
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the tunnel process outlived al")
        assert bindable(port)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    argv = json.loads(log.read_text().splitlines()[0])
    assert argv[1:] == al_deploy.SshTransport(HOST).tunnel_argv(port, 8765)[1:]
    assert "-R" not in argv and not any("0.0.0.0" in a or "*:" in a for a in argv)


def test_tunnel_closes_on_sigterm(tmp_path):
    host = make_host(tmp_path)
    fake, log = make_fake_ssh(tmp_path, host)
    free = listener()
    port = free.getsockname()[1]
    free.close()
    env = {k: v for k, v in os.environ.items() if k not in ("AL_DASHBOARD_DRY_RUN", "AL_DEPLOY_LOCAL_HOST", "AL_APP_ROOT")}
    env["PATH"] = f"{fake}{os.pathsep}{env['PATH']}"
    proc = subprocess.Popen([str(AL), "dashboard", str(tmp_path), "--remote", HOST, "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        assert proc.stdout.readline().startswith("dashboard: ")
        pid = int(Path(f"{log}.pid").read_text())
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
        deadline = time.time() + 5
        while time.time() < deadline and not bindable(port):
            time.sleep(0.05)
        assert bindable(port)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)


def test_tunnel_reports_a_host_that_does_not_answer(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ssh").write_text("#!/bin/sh\necho 'ssh: connect to host testhost.invalid port 22: Connection refused' >&2\nexit 255\n")
    (fake / "ssh").chmod(0o755)
    cp = al("dashboard", tmp_path, "--remote", HOST, ok=False, timeout=60,
            env={"AL_DASHBOARD_DRY_RUN": "", "AL_DEPLOY_LOCAL_HOST": "", "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"})
    assert cp.returncode == 1
    assert "ssh to testhost.invalid ended (exit 255)" in cp.stderr and "Connection refused" in cp.stderr
    assert "is the dashboard running on the host? al deploy LAYER --to HOST --status" in cp.stderr
