"""The terminal clips: fixture repositories built by the test suite's own builders, the manifest's
commands run with the real tools/al, their real output captured. Each record_* returns a
term.Transcript; nothing in it is typed by hand except the captions.

Setup that is not part of what a clip shows (building the fixture, writing the mock agent's answer,
adding a worktree) runs hidden; every command on screen is one of clips.CLIPS[…].commands, run as
shown with only its {placeholders} filled. The one change made to captured text: the work folder
is shown as ~/work and the (neutral) home folder as ~.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import clips
import fixture_imports as fx
import privacy
from term import Step, Transcript

ROOT = Path(__file__).resolve().parents[2]
AL = ROOT / "tools" / "al"
FORBIDDEN: list[str] = []          # make.py sets this to privacy.forbidden_values() of the real HOME


class UnexpectedExit(RuntimeError):
    pass


def shown(argv: list[str] | tuple[str, ...]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def scrub(text: str, work: Path, home: Path) -> str:
    for real, display in ((str(work.resolve()), "~/work"), (str(work), "~/work"),
                          (str(home.resolve()), "~"), (str(home), "~")):
        text = text.replace(real, display)
    return text


def run_shown(argv: list[str] | tuple[str, ...], cwd: Path, env: dict[str, str] | None = None, timeout: float = 600,
              interrupt_after: float | None = None, expect: int | None = 0) -> tuple[int, str]:
    """Run one shown command as a person would type it in `cwd` (a leading `al` is this repository's
    tools/al), stdout and stderr merged in order. With interrupt_after, SIGINT reaches its process group
    after that many seconds (and the output ends `^C`, as a terminal shows it). `expect`: the exit code
    the step is shown with (None: any non-zero); another code is an error, never recorded."""
    env = dict(env if env is not None else os.environ)
    real = [str(AL), *argv[1:]] if argv[0] == "al" else list(argv)
    proc = subprocess.Popen(real, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", start_new_session=True)
    try:
        if interrupt_after is not None:
            time.sleep(interrupt_after)
            os.killpg(proc.pid, signal.SIGINT)
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        raise
    if interrupt_after is not None:
        out = out.rstrip("\n") + "\n^C\n" if out else "^C\n"
    code = proc.returncode
    ok = code != 0 if expect is None else code == expect
    if not ok:
        raise UnexpectedExit(f"`{shown(argv)}` exited {code}, expected {expect if expect is not None else 'non-zero'}\n{out}")
    home = Path(env.get("HOME", str(Path.home())))
    text = scrub(out, Path(env.get("AL_SHOWCASE_WORK", str(home / "work"))), home)
    privacy.check_private(text, FORBIDDEN)
    return code, text


def _hidden(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    cp = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, capture_output=True, text=True, timeout=900)
    if cp.returncode != 0:
        raise RuntimeError(f"setup `{shown([str(a) for a in argv])}` exited {cp.returncode}\n{cp.stdout}\n{cp.stderr}")
    return cp.stdout


def _env(work: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    env.update({"PYTHONUNBUFFERED": "1", "TERM": "dumb", "NO_COLOR": "1", "AL_SHOWCASE_WORK": str(work)})
    env.update(extra or {})
    return env


def _commands(number: int) -> tuple[tuple[str, ...], ...]:
    return next(c for c in clips.CLIPS if c.number == number).commands


def notes_app(work: Path) -> Path:
    """The `notes` application of tests/tools/conftest.py::app."""
    c = fx.tools_conftest()
    repo = work / "notes"
    repo.mkdir(parents=True)
    c.git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("NOTES = []\n")
    (repo / "test.sh").write_text("#!/bin/sh\npython3 -c 'import app'\n")
    (repo / "test.sh").chmod(0o755)
    c.commit(repo, "the app")
    return repo


def week_name() -> str:
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    return f"{year}-s{week:02d}"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------------------------------ 05

def record_05_delivery(work: Path) -> Transcript:
    promote, c = fx.promote_test(), fx.tools_conftest()
    app = notes_app(work)
    layer = promote._layer(app)                         # VERSION, CHANGELOG.md, .gitignore; notes.al generated
    env = _env(work)
    week = week_name()
    cmds = _commands(5)
    steps: list[Step] = []

    def step(i: int, caption: str, values: dict[str, str] | None = None, expect: int | None = 0,
             extra: dict[str, str] | None = None) -> str:
        argv = clips.fill(cmds[i], {"week": week, **(values or {})})
        cwd = work
        _code, text = run_shown(argv, cwd, {**env, **(extra or {})}, expect=expect)
        steps.append(Step(shown(argv), text, caption))
        return text

    step(0, "The layer says: deliver into a sprint branch, promote the sprint to main")
    step(1, "A cycle opens: the sprint branch is cut from main")
    c.git(app, "checkout", "-q", "-b", "work")          # neither main nor the sprint is checked out
    ada = {"GIT_AUTHOR_NAME": "Ada Example", "GIT_AUTHOR_EMAIL": "ada@example.org",
           "GIT_COMMITTER_NAME": "Ada Example", "GIT_COMMITTER_EMAIL": "ada@example.org"}
    sprint = f"sprint/{week}"
    tree = work / "sprint"
    for n, (name, caption_run, caption_merge) in enumerate((
            ("cart.py", "A run (mock agent) delivers its change as a pull request into the sprint",
             "A person merges it into the sprint branch"),
            ("search.py", "A second run delivers into the same sprint",
             "Merged by a person too"))):
        (layer / "mock" / "change").mkdir(parents=True, exist_ok=True)
        (layer / "mock" / "change" / "build.json").write_text(json.dumps({"files": {name: f"{name[:-3].upper()} = 1\n"}}))
        before = {p.name for p in (layer / "agents").iterdir()} if (layer / "agents").is_dir() else set()
        step(2 + 2 * n, caption_run)
        new = [p for p in (layer / "agents").iterdir() if p.name not in before and (p / "state.json").is_file()]
        if len(new) != 1:
            raise RuntimeError(f"expected one new run, found {[p.name for p in new]}")
        state = json.loads((new[0] / "state.json").read_text())
        if state.get("delivery", {}).get("branch") != sprint or state.get("ship", {}).get("action") != "pr":
            raise RuntimeError(f"the run did not deliver into {sprint}: {state.get('delivery')} {state.get('ship')}")
        if n == 0:
            c.git(app, "worktree", "add", "-q", str(tree), sprint)
        step(3 + 2 * n, caption_merge, {"commit": state["ship"]["commit"][:12]}, extra=ada)
    c.git(app, "worktree", "remove", str(tree))
    step(6, "The sprint now holds both changes")
    step(7, "al promote checks the sprint and waits for a person's approval", expect=75)
    step(8, "A person approves (the dashboard has the same buttons)")
    step(9, "Merged into main, version bumped, changelog written, tagged")
    return Transcript(steps, "al — sprint delivery and promotion")


# ------------------------------------------------------------------------------------------ 06

def record_06_sandbox(work: Path) -> Transcript:
    sandbox, c = fx.sandbox_test(), fx.tools_conftest()
    app = notes_app(work)
    bin_dir = work.parent / "stand-in-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "podman").write_text(sandbox.STUB)
    (bin_dir / "podman").chmod(0o755)
    state = work.parent / "podman-stand-in.json"
    state.write_text(json.dumps({
        "calls": [], "images": {},
        "doctor_output": ("AL-DOCTOR\tcommand:test\tmiss\tpytest not found on PATH\n"
                          "AL-DOCTOR\tgit\tok\trev-parse and status work in the worktree\n"
                          "AL-DOCTOR\tinit\tok\tcatatonit\nAL-DOCTOR\tdisk\tok\t90000 MB free\n"),
        "build_lines": ["STEP 1/2: FROM docker.io/library/debian:stable-slim", "STEP 2/2: WORKDIR /work"]}))
    env = _env(work, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "PODMAN_STUB": str(state)})
    # tests/tools/test_sandbox_cli.py::_layer, with the test command `pytest -q`
    layer = app.parent / "notes.al"
    layer.mkdir()
    source = c.LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)")
    source = source.replace("  (command plan ", sandbox.ENVIRONMENTS + "  (command plan ", 1)
    source = source.replace('(adw change #:description "Plan, build, test; a person reviews"',
                            '(adw change #:description "Plan, build, test; a person reviews" #:environment (worktree box)')
    source = source.replace('(cmd test "./test.sh"', '(cmd test "pytest -q"')
    (layer / "agentic.rkt").write_text(source)
    _hidden([AL, "gen", layer], work, env)
    cmds = _commands(6)
    steps = []
    for argv, caption, expect in (
            (cmds[0], "The image a run would use: its recipe hash, not built yet", 0),
            (cmds[1], "doctor starts a throwaway container as a run does: pytest is missing", 1),
            (cmds[2], "A run checks its container first and stops before any agent works", None),
            (cmds[3], "al doctor: where al found each tool it runs", 0)):
        _code, text = run_shown(list(argv), work, env, expect=expect)
        steps.append(Step(shown(argv), text, caption))
    return Transcript(steps, "al — the sandbox (podman is the test suite's stand-in)")


# ------------------------------------------------------------------------------------------ 07

def record_07_adopt(work: Path) -> Transcript:
    fx.existing_setup().build(work)                    # work/larder, committed, with its agent setup
    env = _env(work)
    cmds = _commands(7)
    steps = []
    for argv, caption in (
            (cmds[0], "Larder already has rule files, prompts and workflow scripts; a layer starts beside it"),
            (cmds[1], "What al adopt would take over, and where it would write it"),
            (cmds[2], "Adopted: rules, prompts and scripts become parts of the layer"),
            (cmds[3], "The report says what came from where"),
            (cmds[4], "The application itself is untouched: nothing to commit")):
        _code, text = run_shown(list(argv), work, env)
        steps.append(Step(shown(argv), text, caption))
    return Transcript(steps, "al — adopting an existing agent setup")


# ------------------------------------------------------------------------------------------ 08

def record_08_deploy_remote(work: Path) -> Transcript:
    c, dh = fx.tools_conftest(), fx.deploy_host()
    app = notes_app(work)
    # tests/tools/conftest.py::deployable
    _hidden([AL, "init", app, "--in-repo"], work, _env(work))
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(c.DEPLOY_LAYER.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    _hidden([AL, "gen", layer], work, _env(work))
    c.commit(app, "the agentic layer", "agentic")
    # tests/tools/test_deploy.py::prepared, in a folder beside the work folder
    stage = work.parent / "fake-host"
    stage.mkdir(parents=True, exist_ok=True)
    host = dh.make_host(stage)
    host.login("claude-code")
    host.set_env_file("notes", FORGEJO_TOKEN="x", ADW_WEBHOOK_SECRET="y")
    # the host's free space is the stand-in's too: a fixed 50 GB, so the clip does not depend on the
    # recording machine's disk (al deploy wants 5 GB on the host)
    host.stub("df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
                    'echo "host-disk 104857600 52428800 52428800 50% /"\n')
    fake, _log = dh.make_fake_ssh(stage, host)
    cmds = _commands(8)
    steps = []
    env = _env(work, host.env())
    _code, text = run_shown(list(cmds[0]), work, env)
    steps.append(Step(shown(cmds[0]), text, "--dry-run: every command it would run on the host, nothing changed"))
    env = _env(work, {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"})
    for name in ("AL_DASHBOARD_DRY_RUN", "AL_DEPLOY_LOCAL_HOST"):
        env.pop(name, None)
    argv = clips.fill(cmds[1], {"port": str(free_port())})
    _code, text = run_shown(argv, work, env, interrupt_after=4, expect=130)
    steps.append(Step(shown(argv), text, "An ssh tunnel to the host's dashboard, loopback on both ends; Ctrl-C closes it"))
    return Transcript(steps, "al — deploy and the remote dashboard (the tests' fake host and ssh)")


RECORDERS = {5: record_05_delivery, 6: record_06_sandbox, 7: record_07_adopt, 8: record_08_deploy_remote}


def transcript_text(t: Transcript) -> str:
    """The log written beside the clip: each command after the prompt, then its output."""
    return "".join(f"~/work $ {s.command}\n{s.output.rstrip()}\n" + ("\n" if s.output.strip() else "") for s in t.steps)


if __name__ == "__main__":                              # a quick look: python terminal_clips.py 07 /tmp/somewhere
    import sys
    number, where = int(sys.argv[1]), Path(sys.argv[2])
    where.mkdir(parents=True, exist_ok=True)
    print(transcript_text(RECORDERS[number](where / "work")))
    if "--keep" not in sys.argv:
        shutil.rmtree(where, ignore_errors=True)
