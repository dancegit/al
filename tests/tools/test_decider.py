"""`al decider` and `al answer`: the stand-in decider's local mode (agents/settings/decider.json),
the `al check` rule that keeps it out of git, and a person's answer to a suggestion that resumes a
real dry run of a generated layer. Run with -s to see the commands' output (docs/GUIDE.md quotes it)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from conftest import al, commit, git, sh

SETTING = Path("agents") / "settings" / "decider.json"
RUN = "dec1de0a"
DECISION = {"action": "resume", "summary": "The test check looks flaky: the build is fine, run it again.",
            "explanation": "The check failed on a missing marker file, not on the change itself.",
            "instructions": "", "from_step": ""}


def show(cp) -> str:
    print(f"$ al {' '.join(cp.args[1:])}\n{cp.stdout}{cp.stderr}", end="")
    return cp.stdout


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k not in ("AL_APP_ROOT", "AL_DECIDER", "AL_DECIDE_IMAP_URL", "AL_MODELS", "AL_MODEL_SET", "AL_MAX_COST")}


def test_al_decider_shows_sets_and_resets_the_mode(sibling) -> None:
    out = show(al("decider", sibling, env={"AL_DECIDER": ""}))
    assert out.splitlines()[0] == "mode: off (default)"
    assert f"settings: {sibling / SETTING} (local to this machine, never committed)" in out
    out = show(al("decider", sibling, "--set", "suggest", env={"AL_DECIDER": ""}))
    assert out.splitlines()[0] == "mode: suggest (local setting)" and str(sibling / SETTING) in out
    assert json.loads((sibling / SETTING).read_text()) == {"mode": "suggest", "version": 1}
    out = show(al("decider", sibling, env={"AL_DECIDER": "auto"}))
    assert out.splitlines()[0] == "mode: auto (AL_DECIDER)" and "note: AL_DECIDER" in out
    bogus = al("decider", sibling, "--set", "bogus", ok=False)
    assert bogus.returncode == 2 and "invalid choice" in bogus.stderr
    assert al("decider", sibling, "--set", "auto", "--reset", ok=False).returncode == 2
    out = show(al("decider", sibling, "--reset", env={"AL_DECIDER": ""}))
    assert not (sibling / SETTING).exists() and out.splitlines()[0] == "mode: off (default)"
    (sibling / SETTING).parent.mkdir(parents=True, exist_ok=True)
    (sibling / SETTING).write_text("{oops")
    broken = al("decider", sibling, ok=False, env={"AL_DECIDER": ""})
    assert broken.returncode == 1 and "al decider LAYER --reset" in broken.stderr
    al("decider", sibling, "--reset")
    assert not (sibling / SETTING).exists()


def test_al_check_fails_when_the_decider_setting_is_tracked(in_repo, app) -> None:
    al("decider", in_repo, "--set", "auto")
    assert al("check", in_repo).returncode == 0
    git(app, "add", "-f", "agentic/agents/settings/decider.json")
    cp = al("check", in_repo, ok=False)
    assert cp.returncode == 1
    assert ("PROBLEM: agents/settings/decider.json is a local setting and must not be committed: "
            "git rm --cached agentic/agents/settings/decider.json") in cp.stdout
    git(app, "rm", "-q", "--cached", "agentic/agents/settings/decider.json")
    assert al("check", in_repo).stdout.strip().endswith("check: ok")


def _stopping_layer(sibling: Path, app: Path, tmp_path: Path) -> Path:
    """The app's check passes only once <tmp>/ready exists; the build writes a file; the decider
    (mock) suggests `resume`."""
    ready = tmp_path / "ready"
    (app / "test.sh").write_text(f"#!/bin/sh\ntest -f {ready}\n")
    commit(app, "the check waits for a marker")
    (sibling / "mock").mkdir(exist_ok=True)
    (sibling / "mock" / "build.json").write_text(json.dumps({"files": {"notes.txt": "a note\n"}, "output": "built"}))
    (sibling / "mock" / "decide.json").write_text(json.dumps({"output": "```json\n" + json.dumps(DECISION) + "\n```"}))
    return ready


def _dry_run(sibling: Path, app: Path, run: str) -> str:
    cp = sh(["uv", "run", "--quiet", sibling / "adws" / "adw_change.py", "add a note", "--dry-run", "--adw-id", run],
            cwd=app, env=_clean_env())
    assert cp.returncode == 1, cp.stdout + cp.stderr
    return cp.stdout


def _state(layer: Path, run: str) -> dict:
    return json.loads((layer / "agents" / run / "state.json").read_text())


def test_al_answer_go_resumes_a_suggested_dry_run(sibling, app, tmp_path) -> None:
    ready = _stopping_layer(sibling, app, tmp_path)
    show(al("decider", sibling, "--set", "suggest"))
    out = _dry_run(sibling, app, RUN)
    assert "decision:  suggest resume from test (suggested)" in out
    state = _state(sibling, RUN)
    assert state["decision"]["status"] == "suggested" and Path(state["worktree"]).is_dir()
    record = (sibling / "agents" / RUN / "decision.md").read_text()
    print(f"$ cat agents/{RUN}/decision.md\n{record}", end="")
    ready.touch()
    out = show(al("answer", sibling, RUN, "go", env=_clean_env()))
    assert out.strip() == f"run {RUN}: go: resuming (from step test)"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = _state(sibling, RUN)
        if state["status"] in ("passed", "failed") and state["decision"]["status"] == "resumed":
            break
        time.sleep(0.5)
    assert state["status"] == "passed", (sibling / "agents" / RUN / "resume.log").read_text()
    assert state["decision"]["answers"][0]["via"] == "cli"
    assert "## Decision (round 1, suggest, answered by a person)" in state["prompt"]


def test_al_answer_refuses_unknown_runs_and_bad_answers(sibling, app, tmp_path) -> None:
    _stopping_layer(sibling, app, tmp_path)
    al("decider", sibling, "--set", "suggest")
    _dry_run(sibling, app, RUN)
    unknown = al("answer", sibling, "0badc0de", "go", ok=False)
    show(unknown)
    assert unknown.returncode == 1 and unknown.stderr.startswith("al answer: no run 0badc0de")
    bare = al("answer", sibling, RUN, "alter", ok=False)
    show(bare)
    assert bare.returncode == 2 and "alter needs the changes as text" in bare.stderr
    assert al("answer", sibling, RUN, "maybe", ok=False).returncode == 2
    assert al("answer", sibling, ok=False).returncode == 2
    out = show(al("answer", sibling, RUN, "reject"))
    assert out.strip() == f"run {RUN}: reject: rejected; the run stays with a person"
    twice = al("answer", sibling, RUN, "go", ok=False)
    show(twice)
    assert twice.returncode == 1 and "decision status: rejected" in twice.stderr
    nomail = al("answer", sibling, "--mail", ok=False, env={"AL_DECIDE_IMAP_URL": ""})
    show(nomail)
    assert nomail.returncode == 1 and "AL_DECIDE_IMAP_URL is not set" in nomail.stderr
    assert _state(sibling, RUN)["decision"]["status"] == "rejected"
