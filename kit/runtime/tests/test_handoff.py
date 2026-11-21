"""Handoff on a stopped run: an ADW with a ship step that fails after it changed something hands
its work to a human instead of dropping it. The work is committed and published on the run's
branch, a pull request marked `[needs human]` (or PR.md without a forge) says why the run stopped
and what the agent asks a human to do, and the run still ends `failed` with exit code 1.
Git is real; the forge is a fake; the agent is the mock."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import adw_modules
from adw_modules import envs, gitsafe, inputs, sandbox, steps
from adw_modules.errors import StepFailed
from adw_modules.runner import main

ADW_ID = "4a4a0001"
SQL = "ALTER TABLE workers ADD COLUMN pay_band TEXT;"
PLAN = f"""# Plan: pay band

## Acceptance criteria

- [ ] Each worker has a pay band.

## Notes

- Seed data gets a band for every worker.
- REQUIRES HUMAN: add `migrations/003_worker_pay_band.sql` with exactly these contents:
  ```sql
  -- 003: each worker's pay band
  {SQL}
  ```
- Nothing else touches `migrations/`.

The tests depend on the column.
"""
RESOLVE = ("**REQUIRES HUMAN**: the fix belongs in `migrations/`, which is a protected path. I changed nothing.\n\n"
           "- Root cause: `migrations/003_worker_pay_band.sql` does not exist.")
FAILING_LOG = "\n".join(f"collecting line {n}" for n in range(1, 61)) + "\nE   sqlite3.OperationalError: table workers has no column named pay_band"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                            capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _layer(make_layer, *, forge=None, ship_step=True, sandboxed=False, failing="check", extra_ir=None) -> Path:
    environments = [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [".env"], "portBase": 27700,
                     "portsPerRun": 0, "keep": "never", "status": "stable"}]
    if sandboxed:
        environments.append({"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "1",
                             "memory": "1g", "network": "none", "packages": [], "setup": [], "status": "stable",
                             "scope": "run", "caches": [], "credentials": []})
    adw_steps = [{"name": "plan", "kind": "run", "command": "plan"},
                 {"name": "build", "kind": "run", "command": "build"}]
    if failing == "check":
        adw_steps.append({"name": "test", "kind": "check", "cmd": "test", "resolve": "resolve", "retries": 1, "collect": []})
    else:
        adw_steps.append({"name": "polish", "kind": "run", "command": "build"})
    adw_steps.append({"name": "review", "kind": "review"})
    if ship_step:
        adw_steps.append({"name": "ship", "kind": "ship"})
    ir = {
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan-feature", "modelSet": "base", "output": "path"},
                     {"name": "build", "template": "build", "modelSet": "base", "output": "text"},
                     {"name": "resolve", "template": "resolve-test", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "test", "run": "cat \"$FAILING_LOG_FILE\"; exit 2", "timeout": 20}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["feature"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": "merge", "deployCmd": ""}],
        "environments": environments,
        "forge": forge or {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["wt", "box"] if sandboxed else ["wt"],
                  "classes": ["feature"], "steps": adw_steps, **({"input": ["tasks"]} if extra_ir else {})}],
        **(extra_ir or {}),
    }
    layer = make_layer(ir)
    mock = layer / "mock" / "demo"
    mock.mkdir(parents=True, exist_ok=True)
    (mock / "plan.json").write_text(json.dumps({"layer_files": {"specs/plan-pay-band.md": PLAN},
                                                "output": "${specs_dir}/plan-pay-band.md"}))
    (mock / "test-resolve-1.json").write_text(json.dumps({"output": RESOLVE}))
    if failing == "agent":   # an agent step that fails: an invalid mock script raises StepFailed
        (mock / "polish.json").write_text(json.dumps({"bogus": True}))
    if sandboxed:
        (layer / "env").mkdir(exist_ok=True)
        (layer / "env" / "Containerfile").write_text("FROM debian\n")   # what `al gen` writes; the preflight hashes it
    return layer


def _build_changes(layer: Path, commit: bool = False) -> None:
    """The build step: one committed or uncommitted feature file."""
    (layer / "mock" / "demo" / "build.json").write_text(json.dumps({
        "files": {"seed.py": "PAY_BANDS = {'hca': 'Band 3'}\n", ".env": "SECRET=1\n"}, "commit": commit,
        "output": "Report\n\n- REQUIRES HUMAN: add `migrations/003_worker_pay_band.sql` (protected path)."}))


@pytest.fixture(autouse=True)
def failing_log(tmp_path, monkeypatch) -> None:
    path = tmp_path / "failing.log"
    path.write_text(FAILING_LOG + "\n")
    monkeypatch.setenv("FAILING_LOG_FILE", str(path))


def _fake_forge(monkeypatch, *, create_errors: int = 0, kind: str = "forgejo") -> dict:
    record: dict = {"created": [], "comments": [], "merged": []}

    class FakeForge:
        can_attach = False

        def __init__(self) -> None:
            self.kind = kind

        def create_pr(self, branch, base, title, body, draft=False, reviewers=()):
            if len(record["created"]) < create_errors:
                record["created"].append(None)
                raise RuntimeError("forge says: 502 bad gateway")
            record["created"].append({"branch": branch, "base": base, "title": title, "body": body, "draft": draft})
            return {"number": 7, "url": "https://forge.invalid/pulls/7"}

        def merge_pr(self, number, method="squash"):
            record["merged"].append(number)

        def comment(self, number, body):
            record["comments"].append((number, body))
            return 1
    fake_module = SimpleNamespace(get_client=lambda _layer, *_args: FakeForge())
    monkeypatch.setitem(sys.modules, "adw_modules.forge", fake_module)
    monkeypatch.setattr(adw_modules, "forge", fake_module, raising=False)
    return record


def _with_origin(app: Path) -> Path:
    bare = app.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    _git(app, "remote", "add", "origin", str(bare))
    _git(app, "push", "-q", "origin", "main")
    return bare


FORGEJO = {"id": "forgejo", "kind": "forgejo", "status": "stable", "remote": ""}


def _state(layer: Path) -> dict:
    return json.loads((layer / f"agents/{ADW_ID}/state.json").read_text())


def _log(layer: Path) -> str:
    return (layer / f"agents/{ADW_ID}/run.log").read_text()


def _assert_handoff_body(body: str) -> None:
    why, ask, summary = (body.index(heading) for heading in
                         ("## Why this stopped", "## What the agent asks a human to do", "## Summary"))
    assert why < ask < summary, body
    stopped = body[why:ask]
    assert "`test`" in stopped and "exited 2 after 2 attempt(s)" in stopped
    assert "table workers has no column named pay_band" in stopped        # tail of the failing check's log
    assert "collecting line 60" in stopped and "collecting line 10\n" not in stopped   # at most 40 lines of it
    asked = body[ask:summary]
    assert "```sql" in asked and SQL in asked and "-- 003: each worker's pay band" in asked   # the code fence, verbatim
    assert asked.index("REQUIRES HUMAN: add `migrations/003") < asked.index(SQL)
    assert "I changed nothing." in asked                                  # from the resolve agent's output
    assert "Seed data gets a band" not in asked and "Nothing else touches" not in asked   # only the marked items
    assert "## Plan" in body[summary:] and "## Checks" in body[summary:]


# ---------------------------------------------------------------- extracting the requests


def test_human_requests_takes_paragraphs_items_and_the_fence_they_introduce() -> None:
    from adw_modules.ship import human_requests

    text = ("# Notes\n\nIntro paragraph.\nREQUIRES HUMAN and gives the exact contents:\n\n```sql\nALTER TABLE t ADD c;\n```\n\n"
            "Unrelated paragraph.\n\n```sh\nnot introduced\n```\n\n"
            "  - nested item\n  - REQUIRES HUMAN: approve `db.py`\n    continued here.\n  - next item\n\n"
            "```text\nlog says REQUIRES HUMAN inside a block\n```\n\n"
            "REQUIRES HUMAN at the end, then a heading\n## Heading\n\n\n```\nfar away\n```\n")
    found = human_requests(text)
    assert found == [
        "Intro paragraph.\nREQUIRES HUMAN and gives the exact contents:\n\n```sql\nALTER TABLE t ADD c;\n```",
        "- REQUIRES HUMAN: approve `db.py`\n  continued here.",
        "```text\nlog says REQUIRES HUMAN inside a block\n```",
        "REQUIRES HUMAN at the end, then a heading",
    ]
    assert human_requests("nothing to see\n") == []


# ---------------------------------------------------------------- with a forge


def test_failed_check_hands_the_work_to_a_human_pr(make_layer, app_repo, monkeypatch) -> None:
    bare = _with_origin(app_repo)
    (app_repo / ".env").write_text("SECRET=1\n")
    layer = _layer(make_layer, forge=FORGEJO)
    _build_changes(layer, commit=False)                # pending changes: the handoff commits them
    forge = _fake_forge(monkeypatch)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    assert state["status"] == "failed"
    test_step = next(step for step in state["steps"] if step["name"] == "test")
    assert test_step["status"] == "failed"                                 # the original failure is the record
    assert [step["name"] for step in state["steps"]] == ["plan", "build", "test"]   # review/ship never ran
    ship = state["ship"]
    assert ship["action"] == "handoff" and ship["shipped"] is False
    assert len(ship["reasons"]) == 1 and ship["reasons"][0].startswith("run-stopped:test: check test exited 2")
    assert ship["pr"] == {"number": 7, "url": "https://forge.invalid/pulls/7"} and "handoff_error" not in ship
    created = forge["created"]
    assert len(created) == 1 and forge["merged"] == []                    # never merged
    assert created[0]["title"].startswith("[needs human] feature: Record each worker") and created[0]["draft"] is True
    assert created[0]["branch"] == state["branch"] and created[0]["base"] == "main"
    _assert_handoff_body(created[0]["body"])
    # the branch is published, holds the work and never the copied .env; main is unchanged
    assert _git(bare, "rev-parse", f"refs/heads/{state['branch']}") == ship["commit"]
    assert _git(bare, "show", f"{ship['commit']}:seed.py") == "PAY_BANDS = {'hca': 'Band 3'}"
    assert ".env" not in _git(bare, "ls-tree", "-r", "--name-only", ship["commit"]).split()
    assert _git(bare, "rev-parse", "refs/heads/main") == _git(app_repo, "rev-parse", "refs/heads/main")
    log = _log(layer)
    summary = log[log.index("=== ADW summary ==="):]
    assert "status:    failed" in summary and "https://forge.invalid/pulls/7" in summary and "needs a human" in summary


def test_issue_comment_and_tasklist_marker_point_at_the_handoff_pr(make_layer, app_repo, monkeypatch) -> None:
    state = SimpleNamespace(adw_id=ADW_ID, status="failed", branch="feature/x",
                            ship={"action": "handoff", "shipped": False, "pr": {"number": 7, "url": "https://forge.invalid/pulls/7"},
                                  "commit": "abc", "reasons": ["run-stopped:test: exited 2"]})
    text = inputs.summarise(state)
    assert "https://forge.invalid/pulls/7" in text and "needs a human" in text
    # end to end through a task list: the failed marker names the PR
    _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO, extra_ir={"promptInputs": [{"id": "tasks", "kind": "tasklist", "status": "stable",
                                                                          "path": "tasks.md"}]})
    (layer / "tasks.md").write_text("## default\n- [ ] Record each worker's pay band {feature}\n")
    _build_changes(layer)
    _fake_forge(monkeypatch)
    assert main("demo", layer, ["--source", "tasks:2", "--adw-id", ADW_ID]) == 1
    line = (layer / "tasks.md").read_text().splitlines()[1]
    assert line.startswith("- [!]") and "https://forge.invalid/pulls/7" in line, line


def test_failing_agent_step_hands_over_too(make_layer, app_repo, monkeypatch) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO, failing="agent")
    _build_changes(layer)
    forge = _fake_forge(monkeypatch)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    assert state["ship"]["action"] == "handoff" and state["ship"]["reasons"][0].startswith("run-stopped:polish:")
    body = forge["created"][0]["body"]
    assert "`polish`" in body[:body.index("## What the agent asks")]
    assert "REQUIRES HUMAN: add `migrations/003_worker_pay_band.sql` (protected path)." in body   # build's report


def test_forgejo_token_from_local_env_never_reaches_state_events_or_the_pr(make_layer, app_repo, tmp_path,
                                                                           monkeypatch) -> None:
    import os
    import secrets

    from adw_modules import forge as forge_module

    var = "AL_TEST_FORGE_TOKEN"
    value = f"tok-{secrets.token_hex(12)}"
    _with_origin(app_repo)
    layer = _layer(make_layer, forge={"id": "forgejo", "kind": "forgejo", "status": "stable", "remote": "",
                                      "host": "https://forge.invalid", "repo": "acme/shop", "tokenEnv": var,
                                      "botMarker": "[ADW-BOT]"})
    (layer / "local.env").write_text(f"{var}={value}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(var, raising=False)
    _build_changes(layer)
    calls: list[dict] = []

    def http(method, url, headers, body):
        calls.append({"method": method, "url": url, "headers": dict(headers),
                      "body": None if body is None else body.decode("utf-8")})
        if method == "POST" and url.endswith("/pulls"):
            return 201, {"number": 7, "html_url": "https://forge.invalid/acme/shop/pulls/7"}
        return 200, ([] if method == "GET" else {})
    monkeypatch.setattr(forge_module, "http_request", http)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    assert state["ship"]["action"] == "handoff"
    assert state["ship"]["pr"]["number"] == 7, state["ship"]
    pr_calls = [c for c in calls if c["method"] == "POST" and c["url"].endswith("/pulls")]
    assert len(pr_calls) == 1
    assert pr_calls[0]["headers"]["Authorization"] == f"token {value}"
    payload = json.loads(pr_calls[0]["body"])
    assert "[needs human]" in payload["title"] and value not in pr_calls[0]["body"]
    assert all(value not in (c["body"] or "") for c in calls)
    assert var not in os.environ and not any(value in v for v in os.environ.values())
    leaked = [path for root in (layer / "agents", app_repo) for path in root.rglob("*")
              if path.is_file() and value.encode() in path.read_bytes()]
    assert leaked == []


def test_resume_after_a_handoff_starts_the_ship_record_afresh(make_layer, app_repo, monkeypatch) -> None:
    layer = _layer(make_layer)
    _build_changes(layer)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    assert _state(layer)["ship"]["action"] == "handoff"
    monkeypatch.setenv("FAILING_LOG_FILE", "/dev/null")           # the human fixed it: the check passes now
    (layer / "agentic-layer.json").write_text((layer / "agentic-layer.json").read_text().replace("exit 2", "exit 0"))
    main("demo", layer, ["--adw-id", ADW_ID, "--resume", "--keep-worktree"])
    ship = _state(layer)["ship"]
    assert ship["previous_handoff"]["action"] == "handoff"
    assert ship.get("action") != "handoff" and "stopped_at" not in ship and "url" not in ship


# ---------------------------------------------------------------- without a forge


def test_without_a_forge_the_handoff_is_pr_md_and_the_branch(make_layer, app_repo, monkeypatch) -> None:
    layer = _layer(make_layer)
    _build_changes(layer)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    ship = state["ship"]
    assert state["status"] == "failed" and ship["action"] == "handoff" and ship["pr"] is None
    assert ship["target"] == f"agents/{ADW_ID}/PR.md"
    text = (layer / ship["target"]).read_text()
    assert text.startswith("# [needs human] feature: Record each worker's pay band")
    _assert_handoff_body(text)
    assert _git(app_repo, "show", f"{state['branch']}:seed.py") == "PAY_BANDS = {'hca': 'Band 3'}"   # branch kept
    assert not Path(state["worktree"]).exists()
    assert f"agents/{ADW_ID}/PR.md" in _log(layer)[_log(layer).index("=== ADW summary ==="):]


def test_dry_run_with_a_real_forge_writes_pr_md_instead(make_layer, app_repo, monkeypatch) -> None:
    bare = _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO)
    _build_changes(layer)
    forge = _fake_forge(monkeypatch)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature", "--dry-run"]) == 1
    state = _state(layer)
    assert forge["created"] == [] and state["ship"]["target"] == f"agents/{ADW_ID}/PR.md"
    assert _git(bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{state['branch']}", check=False) == ""


# ---------------------------------------------------------------- never hand off


def test_nothing_changed_means_nothing_to_hand_over(make_layer, app_repo, monkeypatch) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO)          # no build script: the build step changes nothing
    forge = _fake_forge(monkeypatch)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    assert forge["created"] == [] and state["ship"].get("action") != "handoff"
    assert not (layer / f"agents/{ADW_ID}/PR.md").exists()
    summary = _log(layer)[_log(layer).index("=== ADW summary ==="):]
    assert "nothing to hand over" in summary


def test_integrity_failure_is_never_handed_over(make_layer, app_repo, monkeypatch) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO, failing="agent")
    _build_changes(layer)
    forge = _fake_forge(monkeypatch)
    real = steps.run_agent

    def tampered(layer_, state, name, *args, **kwargs):
        if name == "polish":
            raise StepFailed(gitsafe.INTEGRITY, "sandboxed worktree refused: HEAD rewritten")
        return real(layer_, state, name, *args, **kwargs)
    monkeypatch.setattr(steps, "run_agent", tampered)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    assert forge["created"] == [] and state["ship"].get("action") != "handoff"
    assert not (layer / f"agents/{ADW_ID}/PR.md").exists()
    assert "git-integrity" in _log(layer)[_log(layer).index("=== ADW summary ==="):]


def test_adw_without_a_ship_step_is_not_handed_over(make_layer, app_repo, monkeypatch) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO, ship_step=False)
    _build_changes(layer)
    forge = _fake_forge(monkeypatch)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    assert forge["created"] == [] and _state(layer)["ship"] == {}
    assert not (layer / f"agents/{ADW_ID}/PR.md").exists()


# ---------------------------------------------------------------- the handoff itself fails


def test_handoff_failing_keeps_the_original_failure(make_layer, app_repo, monkeypatch) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO)
    _build_changes(layer)
    forge = _fake_forge(monkeypatch, create_errors=99)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    assert state["status"] == "failed"
    assert next(step for step in state["steps"] if step["name"] == "test")["status"] == "failed"
    assert "502 bad gateway" in state["ship"]["handoff_error"] and state["ship"]["shipped"] is False
    assert forge["merged"] == []
    log = _log(layer)
    assert "warning: handoff failed" in log
    assert "run failed: test: check test exited 2" in log                 # the original reason is still reported
    assert _git(app_repo, "show", f"{state['branch']}:seed.py") == "PAY_BANDS = {'hca': 'Band 3'}"   # nothing lost


# ---------------------------------------------------------------- sandboxed


def test_sandboxed_handoff_publishes_through_the_host(make_layer, app_repo, monkeypatch) -> None:
    bare = _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO, sandboxed=True, failing="agent")
    _build_changes(layer, commit=False)                # uncommitted: committed host-side through gitsafe
    podman: list = []
    real_run = envs.subprocess.run
    monkeypatch.setattr(envs.subprocess, "run", lambda argv, **kw: (podman.append(argv), SimpleNamespace(
        returncode=0, stdout="", stderr=""))[1] if argv[0] == "podman" else real_run(argv, **kw))
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [])  # the preflight finds everything it needs
    sessions: list = []
    real_session = gitsafe.session

    def counting(sb):
        sessions.append(sb)
        return real_session(sb)
    monkeypatch.setattr(gitsafe, "session", counting)
    forge = _fake_forge(monkeypatch)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    ship = state["ship"]
    assert ship["action"] == "handoff" and ship["pr"]["number"] == 7 and sessions
    assert _git(app_repo, "rev-parse", f"refs/heads/{state['branch']}") == ship["commit"]    # host-made ref
    assert _git(bare, "rev-parse", f"refs/heads/{state['branch']}") == ship["commit"]
    assert _git(app_repo, "show", f"{ship['commit']}:seed.py") == "PAY_BANDS = {'hca': 'Band 3'}"
    assert ".env" not in _git(app_repo, "ls-tree", "-r", "--name-only", ship["commit"]).split()
    assert subprocess.run(["git", "-C", str(app_repo), "fsck", "--connectivity-only"], capture_output=True).returncode == 0
    assert not (layer / "agents" / ADW_ID / "objects").exists()            # quarantine gone; objects in the app
    assert forge["created"][0]["title"].startswith("[needs human]")


def test_sandboxed_tampered_worktree_is_not_handed_over(make_layer, app_repo, monkeypatch) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge=FORGEJO, sandboxed=True, failing="agent")
    _build_changes(layer, commit=False)
    real_run = envs.subprocess.run
    monkeypatch.setattr(envs.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
                        if argv[0] == "podman" else real_run(argv, **kw))
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [])  # the preflight finds everything it needs
    real = steps.run_agent

    def tamper(layer_, state, name, *args, **kwargs):
        if name == "polish":   # code in the container rewrote the worktree's .git file, then the step failed
            (Path(state.worktree) / ".git").write_text("gitdir: /tmp/elsewhere\n")
            raise StepFailed("polish", "agent gave up")
        return real(layer_, state, name, *args, **kwargs)
    monkeypatch.setattr(steps, "run_agent", tamper)
    forge = _fake_forge(monkeypatch)
    assert main("demo", layer, ["Record each worker's pay band", "--adw-id", ADW_ID, "--class", "feature"]) == 1
    state = _state(layer)
    assert forge["created"] == [] and "git-integrity" in state["ship"]["handoff_error"]
    assert _git(app_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{state['branch']}", check=False) == ""
