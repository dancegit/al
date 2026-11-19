"""One real run on another machine: a zero-touch workflow of a small fixture repository is placed
on a second account (or a container with sshd), runs there in a rootless podman sandbox, pushes its
branch to a git source both accounts reach, and is merged here through the home gate.

Opt-in and slow: it runs only with `-m slow` and when AL_TEST_REMOTE_HOST names an ssh destination
(what `ssh HOST` accepts, logged in without a password) that has git, uv and rootless podman, and
podman works on this machine too. AL_TEST_REMOTE_SOURCE names a directory both accounts can read
and write (default: a new directory under /tmp, made world-writable for the test); the bare
repository that plays the git source is created there. Nothing of either account is kept: the
deployment, the env file and the run's storage on the other account are removed afterwards.
See tests/README.md.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import alit

pytestmark = [pytest.mark.slow, pytest.mark.podman]

HOST = os.environ.get("AL_TEST_REMOTE_HOST", "").strip()
NAME = "al-remote-test"
LAYER = f"""#lang al
(agentic-layer #:name "{NAME}" #:description "A tiny app whose runs may execute on another machine"
  (codebase #:root ".." #:languages (python) #:description "app.py and a check"
    (cmd test "sleep 20 && python3 -c 'import ast; ast.parse(open(\\"app.py\\").read())'" #:timeout 300))
  (layer in-repo #:dir "agentic")
  (dashboard none)
  (forge none #:remote "origin")
  (agent mock)
  (environment worktree #:ports-per-run 0)
  (environment podman #:base-image "{alit.PYTHON_IMAGE}" #:network none #:agent-inside #f)
  (host #:id remote)
  (placement auto #:hosts (home remote) #:max-runs-per-host 1)
  (command plan #:template plan-chore #:output path)
  (command review #:template review #:output json)
  (hop implement #:template run-plan #:takes plan)
  (review auto #:require-checks (test) #:allowed-classes (chore))
  (adw zte #:environment (worktree podman) #:review auto #:classes (chore)
    (steps (run plan #:command plan #:args ("$prompt")) (run build #:command implement #:args ("$plan"))
           (check test #:cmd test) (review) (ship))))
"""


def ssh(script: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST, "sh -lc " + shlex.quote(script)],
                          capture_output=True, text=True, timeout=timeout)


def _load_envs():
    """kit/runtime/adw_modules/envs.py, for the flags every run's container must carry."""
    sys.path.insert(0, str(alit.ROOT / "kit" / "runtime"))
    from adw_modules import envs
    return envs


@pytest.fixture
def remote():
    if not HOST:
        pytest.skip("set AL_TEST_REMOTE_HOST=<ssh destination of a second machine with git, uv and rootless podman> "
                    "and run with -m slow to prove a remote run (see tests/README.md)")
    if alit.sh(["podman", "info"]).returncode != 0:
        pytest.skip("podman does not work on this machine")
    if alit.sh(["podman", "image", "exists", alit.PYTHON_IMAGE]).returncode != 0:
        pytest.skip(f"{alit.PYTHON_IMAGE} is not in local podman storage (podman pull it once)")
    probe = ssh("command -v git && command -v uv && podman info --format '{{.Host.Security.Rootless}}'")
    if probe.returncode != 0 or probe.stdout.strip().splitlines()[-1:] != ["true"]:
        pytest.skip(f"{HOST} needs git, uv and rootless podman: {(probe.stderr or probe.stdout).strip()[-300:]}")
    shared = Path(os.environ.get("AL_TEST_REMOTE_SOURCE") or tempfile.mkdtemp(prefix="al-remote-source-", dir="/tmp"))
    shared.chmod(0o777)
    work = Path(tempfile.mkdtemp(prefix="al-remote-home-"))
    yield shared, work
    ssh(f'rm -rf "$HOME/al/source" "$HOME/.config/al/{NAME}.env" "$HOME/.local/state/al/{NAME}"; '
        f"podman rmi -f $(podman images -q --filter label=al.layer={NAME}) 2>/dev/null; true")
    alit.sh(["sh", "-c", f"podman rmi -f $(podman images -q --filter label=al.layer={NAME}) 2>/dev/null; true"])
    shutil.rmtree(work, ignore_errors=True)
    if not os.environ.get("AL_TEST_REMOTE_SOURCE"):
        shutil.rmtree(shared, ignore_errors=True)


def test_a_run_executes_on_another_machine_and_merges_through_the_home_gate(remote):
    shared, work = remote
    envs = _load_envs()
    source = shared / "source.git"
    alit.sh(["git", "init", "-q", "--bare", "--shared=0666", "-b", "main", str(source)], check=True)
    alit.sh(["chmod", "-R", "a+rwX", str(source)], check=True)
    app = work / "app"
    app.mkdir()
    alit.git(app, "init", "-q", "-b", "main")
    (app / "app.py").write_text("NOTES = []\n")
    alit.sh([alit.AL, "init", app, "--in-repo"], check=True)
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(LAYER)
    gen = alit.sh([alit.AL, "gen", layer])
    assert gen.returncode == 0, alit.describe(gen)
    mock = layer / "mock" / "zte" / "build.json"
    mock.parent.mkdir(parents=True)
    mock.write_text(json.dumps({"files": {"note.py": "NOTE = 'written on another machine'\n"}}))
    alit.git(app, "add", "-A")
    alit.git(app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "the app and its layer")
    alit.git(app, "remote", "add", "origin", str(source))
    alit.git(app, "push", "-q", "origin", "main")
    alit.sh(["chmod", "-R", "a+rwX", str(source)], check=True)
    main_before = alit.git(source, "rev-parse", "refs/heads/main")

    assert ssh(f'mkdir -p "$HOME/.config/al" && printf "AL_GIT_SOURCE=%s\\n" {shlex.quote(str(source))} > "$HOME/.config/al/{NAME}.env" '
               f'&& chmod 600 "$HOME/.config/al/{NAME}.env"').returncode == 0
    deploy = alit.sh([alit.AL, "deploy", layer, "--to", HOST, "--as", "remote", "--no-enable"], timeout=900)
    assert deploy.returncode == 0, alit.describe(deploy)
    env = {**os.environ, "AL_HOST_REMOTE": HOST, "AL_GIT_SOURCE": str(source)}
    env.pop("AL_APP_ROOT", None)
    doctor = subprocess.run([str(alit.AL), "host", str(layer), "remote", "doctor"], env=env, capture_output=True, text=True, timeout=300)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr

    run = subprocess.Popen([str(alit.AL), "run", str(layer), "/zte", "add a note module", "--yes", "--class", "chore",
                            "--on", "remote"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # while the check sleeps in its container on the other account: look at that container there
    inspected: dict = {}
    deadline = time.monotonic() + 1500
    while time.monotonic() < deadline and run.poll() is None and not inspected:
        listed = ssh(f"podman ps --filter label=al.layer={NAME} --format '{{{{.Names}}}}'")
        names = [n for n in listed.stdout.split() if n.startswith(f"al-{NAME}-")]
        if names:
            found = ssh(f"podman inspect {shlex.quote(names[0])}")
            if found.returncode == 0:
                inspected = json.loads(found.stdout)[0]
        else:
            time.sleep(2)
    output = run.communicate(timeout=1800)[0]
    assert run.returncode == 0, output
    assert inspected, "the run's container was never seen on the other machine\n" + output

    # the container ran there, hardened exactly as at home, with the clone's .git read-only
    assert alit.sh(["podman", "ps", "-a", "--filter", f"label=al.layer={NAME}", "--format", "{{.Names}}"]).stdout.split() in ([], [inspected["Name"]])
    host_config = inspected["HostConfig"]
    flags = envs.hardening_flags({})
    assert "--cap-drop=ALL" in flags and "--security-opt=no-new-privileges" in flags
    assert [c.upper() for c in host_config.get("CapDrop") or []] and not inspected.get("EffectiveCaps")
    assert any("no-new-privileges" in option for option in host_config.get("SecurityOpt") or [])
    assert int(host_config.get("PidsLimit") or 0) == int(flags[flags.index("--pids-limit") + 1])
    git_mounts = [m for m in inspected["Mounts"] if m["Destination"].endswith("/repo/.git")]
    assert git_mounts and all(m["RW"] is False for m in git_mounts), inspected["Mounts"]
    assert f"/al/{NAME}/runs/" in git_mounts[0]["Source"]            # the run's own clone, not the deployment

    state = json.loads(next((layer / "agents").glob("*/state.json")).read_text())
    assert state["status"] == "passed" and state["remote"]["host"] == "remote"
    delivered = state["remote"]["delivered"]
    assert alit.git(source, "rev-parse", f"refs/heads/{delivered['branch']}") == delivered["sha"]   # the branch arrived
    assert state["ship"]["action"] == "merge" and state["ship"]["shipped"] is True                   # and the home gate merged
    assert alit.git(source, "rev-parse", "refs/heads/main") != main_before
    assert "written on another machine" in alit.git(source, "show", "main:note.py")
    assert alit.git(source, "merge-base", "--is-ancestor", delivered["sha"], "refs/heads/main") == ""
    assert (layer / "agents" / state["adw_id"] / "checks" / "gate-test.log").is_file()
    types = [json.loads(line)["type"] for line in (layer / "agents" / state["adw_id"] / "events.jsonl").read_text().splitlines()]
    for wanted in ("remote.start", "env.container.start", "remote.push", "remote.takeover", "gate.decision", "ship.result"):
        assert wanted in types
    gone = ssh(f'test ! -e "$HOME/.local/state/al/{NAME}/runs/{state["adw_id"]}"')
    assert gone.returncode == 0                                       # the run's storage there was removed
