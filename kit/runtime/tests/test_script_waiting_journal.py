"""(script … #:waiting-exit (75) #:journal "…/{adw_id}"): a hand-built workflow script that says
"waiting, run me again" ends the run `waiting` (exit 75, worktree kept, no hand-off, no failure
notification), and `--resume` runs that step again with ADW_RESUME=1. While the script runs, the
run journal it keeps itself is followed: each events.jsonl line becomes a `script.event` of the
step, and each inner step's prompt.md and output files are copied into the run as documents.
Git is real; the agent is the mock."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from adw_modules import events, ir, notify
from adw_modules.runner import main
from adw_modules.state import ADWState

ADW_ID = "5c0e1a75"
WATCH = r"""#!/bin/sh
# a team's own watcher: keeps its own journal under .work/j/<id>/ and exits 75 until `ready` exists
J=".work/j/$ADW_ID"
echo "resume=[$ADW_RESUME] args=$*" >> "$ADW_LAYER_DIR/seen.txt"
set -- $1
mode="$1"
case "$mode" in
  none) exit 0 ;;
  malformed)
    mkdir -p "$J"
    printf '%s\n' '{"kind":"a","subject":"x"}' 'not json' '[1,2]' '{"kind":"b","subject":"y"}' >> "$J/events.jsonl" ;;
  symlinks)
    mkdir -p "$J/plan"
    ln -s "$2" "$J/plan/prompt.md"
    printf '{"ok":true}\n' > "$J/plan/output.json"
    printf '{"kind":"step.end","subject":"plan"}\n' >> "$J/events.jsonl" ;;
  linkdir)
    mkdir -p .work "$2/$ADW_ID/plan"
    ln -s "$2" .work/outside
    printf '# outside\n' > "$2/$ADW_ID/plan/prompt.md"
    printf '{"kind":"step.end","subject":"plan"}\n' >> "$2/$ADW_ID/events.jsonl" ;;
  *)
    mkdir -p "$J/plan"
    printf '%s\n' '{"time":"2026-11-15T10:00:00Z","kind":"step.start","subject":"plan","prev":"","hash":"h1"}' >> "$J/events.jsonl"
    printf '# the plan prompt\n' > "$J/plan/prompt.md"
    printf '{"ok":true}\n' > "$J/plan/output.json"
    printf '%s\n' '{"time":"2026-11-15T10:00:01Z","kind":"step.end","subject":"plan","prev":"h1","hash":"h2","usage":{"input_tokens":10}}' >> "$J/events.jsonl" ;;
esac
if [ "$mode" = wait ]; then echo "checked the queue" >> watch-notes.txt; fi
if [ "$mode" = wait ] && [ ! -f ready ]; then echo "still waiting"; exit 75; fi
echo "done"
exit 0
"""
MAIL = {"id": "mail", "kind": "email", "status": "stable", "events": ["needs-human", "failed"],
        "toEnv": "AL_NOTIFY_TO", "fromEnv": "AL_NOTIFY_FROM", "smtpUrlEnv": "AL_NOTIFY_SMTP_URL", "timeout": 5}


def _layer(make_layer, app_repo: Path, *, waiting=(75,), journal=".work/j/{adw_id}", notifiers=None) -> Path:
    script = app_repo / "adws" / "watch.sh"
    script.parent.mkdir(exist_ok=True)
    script.write_text(WATCH, encoding="utf-8")
    script.chmod(0o755)
    (app_repo / ".gitignore").write_text(".work/\nready\n", encoding="utf-8")
    for args in (["add", "adws", ".gitignore"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "watch"]):
        subprocess.run(["git", "-C", str(app_repo), *args], check=True, capture_output=True)
    step = {"name": "run", "kind": "script", "cmd": "watch", "args": ["$prompt"], "timeout": 0,
            "waitingExit": list(waiting), "journal": journal}
    overrides = {
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "codebase": {"commands": [{"name": "watch", "run": "./adws/watch.sh", "timeout": 30, "description": ""}]},
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27960,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "adws": [{"name": "watch", "agent": "mock", "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": [step, {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    }
    if notifiers is not None:
        overrides["notifiers"] = notifiers
    return make_layer(overrides)


def _state(layer: Path) -> dict:
    return json.loads((layer / "agents" / ADW_ID / "state.json").read_text())


def _events(layer: Path) -> list[dict]:
    return events.read(layer / "agents" / ADW_ID / "events.jsonl")


def test_a_declared_waiting_exit_ends_the_run_waiting_and_keeps_the_worktree(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("watch", layer, ["wait", "--adw-id", ADW_ID]) == 75
    data = _state(layer)
    assert data["status"] == "waiting"
    step = next(r for r in data["steps"] if r["name"] == "run")
    assert step["status"] == "waiting" and "exited 75 (declared as waiting)" in step["detail"]
    assert [r["name"] for r in data["steps"]] == ["run"]           # review and ship never started
    assert Path(data["worktree"]).is_dir()                         # kept for the resume
    assert data["ship"].get("action") != "handoff" and not data["ship"]
    log = (layer / "agents" / ADW_ID / "run.log").read_text()
    # state.error (read by a prompt source's on_finish, like a hand-off's reason) is printed with the run
    assert f"run waiting at step run (exit 75); resume: uv run adws/adw_watch.py --resume --adw-id {ADW_ID}" in log
    assert f"resume:    uv run adws/adw_watch.py --resume --adw-id {ADW_ID}" in log
    assert data["scripts"]["run"]["waiting"] is True and data["scripts"]["run"]["exit"] == 75
    end = [e for e in _events(layer) if e["type"] == "run.end"][-1]
    assert end["status"] == "waiting" and end["exit"] == 75
    step_end = [e for e in _events(layer) if e["type"] == "step.end" and e["step"] == "run"][-1]
    assert step_end["status"] == "waiting"


def test_resume_reruns_the_waiting_step_with_adw_resume_and_passes(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("watch", layer, ["wait", "--adw-id", ADW_ID]) == 75
    (Path(_state(layer)["worktree"]) / "ready").write_text("yes\n")
    assert main("watch", layer, ["--resume", "--adw-id", ADW_ID]) == 0
    data = _state(layer)
    assert data["status"] == "passed"
    step = next(r for r in data["steps"] if r["name"] == "run")
    assert step["status"] == "passed" and step["attempts"] == 2
    seen = (layer / "seen.txt").read_text().splitlines()
    assert seen == ["resume=[] args=wait", "resume=[1] args=wait"]
    assert data["scripts"]["run"]["waiting"] is False and data["ship"]["action"] == "pr"


def test_an_undeclared_exit_75_still_fails_and_hands_off(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, waiting=())
    assert main("watch", layer, ["wait", "--adw-id", ADW_ID]) == 1
    data = _state(layer)
    assert data["status"] == "failed" and data["scripts"]["run"]["exit"] == 75
    assert data["ship"]["action"] == "handoff" and data["ship"]["stopped_at"] == "run"


def test_journal_lines_are_mirrored_as_script_events_with_the_original_object(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("watch", layer, ["go", "--adw-id", ADW_ID]) == 0
    mirrored = [e for e in _events(layer) if e["type"] == "script.event"]
    assert [e["foreign"] for e in mirrored] == [
        {"time": "2026-11-15T10:00:00Z", "kind": "step.start", "subject": "plan", "prev": "", "hash": "h1"},
        {"time": "2026-11-15T10:00:01Z", "kind": "step.end", "subject": "plan", "prev": "h1", "hash": "h2",
         "usage": {"input_tokens": 10}}]
    assert all(e["step"] == "run" and e["owner"] == "run" for e in mirrored)
    assert [e["line"] for e in mirrored] == [1, 2]
    journal = _state(layer)["scripts"]["run"]["journal"]
    assert journal["found"] is True and journal["lines"] == 2 and journal["malformed"] == 0
    assert journal["path"] == f".work/j/{ADW_ID}"


def test_malformed_journal_lines_are_counted_and_skipped(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("watch", layer, ["malformed", "--adw-id", ADW_ID]) == 0
    assert _state(layer)["scripts"]["run"]["journal"]["malformed"] == 2
    kinds = [e["foreign"]["kind"] for e in _events(layer) if e["type"] == "script.event"]
    assert kinds == ["a", "b"]


def test_a_missing_journal_folder_is_not_an_error(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("watch", layer, ["none", "--adw-id", ADW_ID]) == 0
    data = _state(layer)
    assert data["status"] == "passed"
    assert data["scripts"]["run"]["journal"]["found"] is False and data["scripts"]["run"]["documents"] == []


def test_inner_prompt_and_output_files_become_run_documents(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("watch", layer, ["go", "--adw-id", ADW_ID]) == 0
    documents = _state(layer)["scripts"]["run"]["documents"]
    base = f"agents/{ADW_ID}/scripts/run/journal/plan"
    assert documents == [{"path": f"{base}/output.json", "inner": "plan", "file": "output.json"},
                         {"path": f"{base}/prompt.md", "inner": "plan", "file": "prompt.md"}]
    assert (layer / base / "prompt.md").read_text() == "# the plan prompt\n"
    assert json.loads((layer / base / "output.json").read_text()) == {"ok": True}


def test_journal_symlinks_and_paths_outside_the_worktree_are_not_copied(make_layer, app_repo, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("not for the run\n")
    layer = _layer(make_layer, app_repo)
    assert main("watch", layer, [f"symlinks {secret}", "--adw-id", ADW_ID]) == 0
    documents = _state(layer)["scripts"]["run"]["documents"]
    assert [d["file"] for d in documents] == ["output.json"]
    assert not (layer / f"agents/{ADW_ID}/scripts/run/journal/plan/prompt.md").exists()

    outside = tmp_path / "outside"
    other = "5c0e1a76"
    layer2 = layer  # same layer, another run whose journal folder is a symlink out of the worktree
    _layer_journal = json.loads((layer2 / "agentic-layer.json").read_text())
    _layer_journal["adws"][0]["steps"][0]["journal"] = ".work/outside/{adw_id}"
    (layer2 / "agentic-layer.json").write_text(json.dumps(_layer_journal))
    assert main("watch", layer2, [f"linkdir {outside}", "--adw-id", other]) == 0
    data = json.loads((layer2 / "agents" / other / "state.json").read_text())
    journal = data["scripts"]["run"]["journal"]
    assert journal["refused"] and "outside the worktree" in journal["refused"]
    assert journal["lines"] == 0 and data["scripts"]["run"]["documents"] == []
    assert not [e for e in events.read(layer2 / "agents" / other / "events.jsonl") if e["type"] == "script.event"]


def test_a_waiting_run_sends_no_failure_notification(make_layer, app_repo):
    layer_dir = _layer(make_layer, app_repo, notifiers=[MAIL])
    waiting = SimpleNamespace(status="waiting", ship={}, budget={}, usage={})
    assert notify.event_of(waiting) == ""
    state = ADWState(adw_id=ADW_ID, adw="watch", status="waiting")
    assert notify.report(ir.load(layer_dir), state, force=True) is False
    assert state.notified == {}
