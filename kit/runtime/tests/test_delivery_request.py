"""The delivery request after every hands-off cycle (delivery_request.py): full runs (runner.main, mock
agent, real git) with no forge, a stubbed `gh` and a stubbed Forgejo; the request is opened once,
updated in place, never merged, and a person's, a merged or a closed one is left alone."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import batch, delivery_request, forge, ir, state as state_module
from adw_modules.runner import main as run_main
from promotion_fixtures import (DATA, SPRINT, FakeForgeCI, FakeForgejoPulls, delivering_adw, deliver, forgejo_forge,
                                git, promote, script, sprint_layer, state_of, with_origin)

GITHUB = {"id": "github", "kind": "github", "status": "stable", "host": "github.com", "repo": "acme/webshop",
          "tokenEnv": "GITHUB_TOKEN", "botMarker": "[ADW-BOT]"}
MARK = "al:delivery-request sprint->main"


def _request(state):
    return state.get("delivery_request") or {}


def _run_log(layer, adw_id):
    return (layer / "agents" / adw_id / "run.log").read_text(encoding="utf-8")


def _requests_to_main(fake):
    return {n: p for n, p in fake.pulls.items() if p["head"] == SPRINT and p["base"] == "main"}


class FakeGh:
    """A `subprocess.run` replacement for GitHubForge: keeps pull requests by number, answers `pr list`,
    `pr create`, `pr edit`, `pr view` and `pr merge` (a real merge in the bare remote, for the runs' own
    pull requests), says no branch is protected, and appends every argv to `argv`."""

    def __init__(self, remote: Path, scratch: Path) -> None:
        self.remote, self.scratch = remote, scratch
        self.pulls: dict[int, dict] = {}
        self.argv: list[list[str]] = []

    def _ok(self, stdout=""):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def _value(self, argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else ""

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.argv.append(argv)
        if argv[1:2] == ["api"]:
            if argv[2].endswith("/protection"):
                return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")
            return self._ok("{}")
        sub = argv[1:3]
        if sub == ["pr", "create"]:
            number = len(self.pulls) + 1
            body = kw.get("input") if "--body-file" in argv else self._value(argv, "--body")
            self.pulls[number] = {"number": number, "head": self._value(argv, "--head"),
                                  "base": self._value(argv, "--base"), "title": self._value(argv, "--title"),
                                  "body": body, "state": "OPEN"}
            return self._ok(f"https://github.com/acme/webshop/pull/{number}\n")
        if sub == ["pr", "list"]:
            head, base = self._value(argv, "--head"), self._value(argv, "--base")
            found = [{"number": p["number"], "url": f"https://github.com/acme/webshop/pull/{p['number']}",
                      "state": p["state"], "title": p["title"], "body": p["body"]}
                     for p in self.pulls.values() if (p["head"], p["base"]) == (head, base)]
            return self._ok(json.dumps(found))
        if sub == ["pr", "edit"]:
            pull = self.pulls[int(argv[3])]
            pull.update(title=self._value(argv, "--title"), body=kw.get("input"))
            return self._ok()
        if sub == ["pr", "view"]:
            pull = self.pulls[int(argv[3])]
            return self._ok(json.dumps({"state": pull["state"], "reviews": [], "comments": []}))
        if sub == ["pr", "merge"]:
            pull = self.pulls[int(argv[3])]
            clone = self.scratch / f"gh-merge-{pull['number']}"
            subprocess.run(["git", "clone", "-q", str(self.remote), str(clone)], check=True, capture_output=True)
            git(clone, "checkout", "-q", pull["base"])
            git(clone, "merge", "--no-ff", "-m", f"Merge pull request #{pull['number']}", f"origin/{pull['head']}")
            git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{pull['base']}")
            shutil.rmtree(clone)
            pull["state"] = "MERGED"
            return self._ok()
        return self._ok()


def _forgejo(make_layer, app_repo, tmp_path, monkeypatch, **layer_kw):
    layer = sprint_layer(make_layer, app_repo, extra={"forge": forgejo_forge(True)}, **layer_kw)
    remote = with_origin(app_repo, tmp_path)
    fake = FakeForgejoPulls(remote, tmp_path)
    monkeypatch.setattr(forge, "http_request", fake)
    return layer, remote, fake


# ---------------------------------------------------------------- al explain's sentence

def test_describe_says_what_each_workflow_requests():
    data = json.loads((DATA / "delivery-sprint.json").read_text(encoding="utf-8"))
    review = {"id": "policy", "kind": "auto", "ship": "merge"}
    adw = delivering_adw()
    adw["review"] = "policy"
    base = {"reviews": [review], "forge": {"kind": "none"}}
    no_stages = delivery_request.describe(base, adw)
    assert "nothing further" in no_stages and "promotion request" not in no_stages
    staged = {**base, "stages": data["stages"]}
    text = delivery_request.describe(staged, adw)
    assert text.startswith("Its delivery request:")
    assert "its branch into `sprint/{cycle}`" in text and "when it does not merge" in text
    assert "`sprint/{cycle}` into `main` as a promotion request" in text
    assert "`agents/requests/sprint-main.md`" in text and "never merged by al" in text
    github = delivery_request.describe({**staged, "forge": dict(GITHUB)}, adw)
    assert "a pull request on the forge" in github and "agents/requests/" not in github
    off = delivery_request.describe({**staged, "forge": {**GITHUB, "pullRequests": False}}, adw)
    assert "`agents/requests/sprint-main.md`" in off
    assert delivery_request.describe(staged, {**adw, "steps": [s for s in adw["steps"] if s["kind"] != "ship"]}) == ""
    final = delivery_request.describe(staged, {**adw, "deliverTo": "main"})
    assert "nothing further, `main` is the final stage" in final


# ---------------------------------------------------------------- no forge: the file

def test_no_forge_writes_the_stage_file_and_updates_it_in_place(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    first = deliver(layer, app_repo, "d1000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert _request(first)["path"] == "agents/requests/sprint-main.md"
    assert _request(first)["action"] == "written" and _request(first)["kind"] == "promotion"
    second = deliver(layer, app_repo, "d1000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    assert _request(second)["path"] == "agents/requests/sprint-main.md"
    assert _request(second)["action"] == "updated"
    files = sorted(p.name for p in (layer / "agents" / "requests").iterdir())
    assert files == ["sprint-main.json", "sprint-main.md"]
    text = (layer / "agents" / "requests" / "sprint-main.md").read_text(encoding="utf-8")
    assert "d1000001" in text and "d1000002" in text and MARK in text and text.count(MARK) == 1
    assert text.startswith(f"# Promote {SPRINT} into main: 2 changes")
    record = json.loads((layer / "agents" / "requests" / "sprint-main.json").read_text(encoding="utf-8"))
    assert record["path"] == "agents/requests/sprint-main.md" and record["state"] == "open"
    assert "request:   written to agents/requests/sprint-main.md (no forge)" in _run_log(layer, "d1000002")


# ---------------------------------------------------------------- forges: one request, updated in place

def test_github_second_cycle_updates_the_same_request(make_layer, app_repo, tmp_path, monkeypatch):
    layer = sprint_layer(make_layer, app_repo, extra={"forge": dict(GITHUB)})
    remote = with_origin(app_repo, tmp_path)
    fake = FakeGh(remote, tmp_path)
    real = forge.get_client

    def client(lay, env=None):
        made = real(lay, env)
        return forge.GitHubForge(made.option, made.layer_dir, env={}, run=fake) if made.kind == "github" else made

    monkeypatch.setattr(forge, "get_client", client)
    deliver(layer, app_repo, "d2000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    second = deliver(layer, app_repo, "d2000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    requests = [n for n, p in fake.pulls.items() if (p["head"], p["base"]) == (SPRINT, "main")]
    assert len(requests) == 1
    number = requests[0]
    creates = [a for a in fake.argv if a[1:3] == ["pr", "create"] and SPRINT in a and "main" in a
               and a[a.index("--head") + 1] == SPRINT]
    edits = [a for a in fake.argv if a[1:3] == ["pr", "edit"]]
    assert len(creates) == 1 and len(edits) == 1 and edits[0][3] == str(number)
    assert _request(second)["number"] == number and _request(second)["action"] == "updated"
    assert MARK in fake.pulls[number]["body"] and "d2000002" in fake.pulls[number]["body"]
    # the runs' own pull requests merged into the sprint; the delivery request never merges
    assert not any(a[1:3] == ["pr", "merge"] and a[3] == str(number) for a in fake.argv)
    assert fake.pulls[number]["state"] == "OPEN"


def test_forgejo_second_cycle_updates_the_same_request(make_layer, app_repo, tmp_path, monkeypatch):
    layer, _remote, fake = _forgejo(make_layer, app_repo, tmp_path, monkeypatch)
    first = deliver(layer, app_repo, "d3000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    second = deliver(layer, app_repo, "d3000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    requests = _requests_to_main(fake)
    assert len(requests) == 1
    number = next(iter(requests))
    assert _request(first)["action"] == "opened" and _request(second)["action"] == "updated"
    assert _request(second)["number"] == number
    assert fake.calls.count(("PATCH", f"/pulls/{number}")) == 1
    assert number not in [m["number"] for m in fake.merges]   # never merged; the runs' own PRs were
    assert requests[number]["state"] == "open" and MARK in requests[number]["body"]
    assert (layer / "agents" / "requests" / "sprint-main.json").is_file()
    assert not (layer / "agents" / "requests" / "sprint-main.md").exists()
    assert f"request:   PR #{number} {SPRINT} → main (updated)" in _run_log(layer, "d3000002")


def test_a_persons_open_request_is_left_alone(make_layer, app_repo, tmp_path, monkeypatch):
    layer, _remote, fake = _forgejo(make_layer, app_repo, tmp_path, monkeypatch)
    fake.pulls[1] = {"number": 1, "head": SPRINT, "base": "main", "title": "Release the sprint",
                     "body": "Ada's notes", "state": "open", "merged": False}
    state = deliver(layer, app_repo, "d4000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert not any(method == "PATCH" for method, _path in fake.calls)
    assert list(_requests_to_main(fake)) == [1]       # no request was opened beside it
    assert fake.pulls[1]["body"] == "Ada's notes" and fake.pulls[1]["title"] == "Release the sprint"
    req = _request(state)
    assert req["action"] == "left-person" and req["number"] == 1
    assert req["url"] == "https://forge.example.org/acme/webshop/pulls/1"


def test_merged_request_is_left_alone_and_a_new_one_opened_when_ahead(make_layer, app_repo, tmp_path, monkeypatch):
    layer, _remote, fake = _forgejo(make_layer, app_repo, tmp_path, monkeypatch)
    first = deliver(layer, app_repo, "d5000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    old = _request(first)["number"]
    fake.set_state(old, "closed", merged=True)
    before = copy.deepcopy(fake.pulls[old])
    second = deliver(layer, app_repo, "d5000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    assert fake.pulls[old] == before
    assert ("PATCH", f"/pulls/{old}") not in fake.calls
    assert len(_requests_to_main(fake)) == 2
    new = _request(second)["number"]
    assert new != old and _request(second)["action"] == "opened"


def test_closed_request_is_left_alone_and_nothing_opened_when_not_ahead(make_layer, app_repo, tmp_path, monkeypatch):
    layer, remote, fake = _forgejo(make_layer, app_repo, tmp_path, monkeypatch)
    first = deliver(layer, app_repo, "d6000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    old = _request(first)["number"]
    fake.set_state(old, "closed")
    head = git(remote, "rev-parse", f"refs/heads/{SPRINT}")
    git(remote, "update-ref", "refs/heads/main", head)
    git(app_repo, "update-ref", "refs/heads/main", head)
    before = copy.deepcopy(fake.pulls)
    calls = len(fake.calls)
    # the next cycle: the run's end hook again, on a sprint that is not ahead of main any more
    lay = ir.load(layer)
    state = state_module.load(layer, "d6000001")
    with contextlib.redirect_stdout(io.StringIO()):
        record = delivery_request.after_run(lay, state, lay.adw("demo"))
    assert record["action"] == "nothing-to-deliver"
    assert fake.pulls == before and fake.calls[calls:] == []
    assert state_of(layer, "d6000001")["delivery_request"]["action"] == "nothing-to-deliver"
    assert delivery_request.summary_line(state) == f"nothing to deliver: {SPRINT} is not ahead of main"


def test_pull_requests_off_writes_the_file_and_calls_no_pull_api(make_layer, app_repo, tmp_path, monkeypatch):
    layer = sprint_layer(make_layer, app_repo, extra={"forge": forgejo_forge(False)})
    with_origin(app_repo, tmp_path)
    fake = FakeForgeCI()
    monkeypatch.setattr(forge, "http_request", fake)
    state = deliver(layer, app_repo, "d7000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert _request(state)["path"] == "agents/requests/sprint-main.md"
    assert _request(state)["medium"] == "file" and _request(state)["note"] == "no pull requests"
    assert (layer / "agents" / "requests" / "sprint-main.md").is_file()
    assert not any(path.startswith("/pulls") for _method, path in fake.calls)


# ---------------------------------------------------------------- the body

def _fake_run(layer, adw_id, prompt, status, ship=None):
    state = state_module.ADWState(adw_id=adw_id, adw="demo", prompt=prompt, status=status, klass="feature",
                                  ship=ship or {}, delivery={"stage": "sprint", "branch": SPRINT})
    state.save(layer)


def test_body_names_baseline_failures_and_what_is_not_included(make_layer, app_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "tok-very-secret-4711")
    monkeypatch.setenv("SOME_TOKEN", "another-secret-0815")
    layer = sprint_layer(make_layer, app_repo)
    deliver(layer, app_repo, "d8000001", {"kit/cart.py": "CART = 1\nMORE = 2\n"}, prompt="Add a cart")
    lay = ir.load(layer)
    main_head = git(app_repo, "rev-parse", "refs/heads/main")
    sprint_head = git(app_repo, "rev-parse", f"refs/heads/{SPRINT}")
    batch._append(lay, {"id": "b0000001", "branch": "main", "stage": "main", "head": main_head, "status": "red",
                        "checks": {"test": {"exit": 1}}, "at": "2026-11-16T10:00:00+00:00", "source": "batch"})
    batch._append(lay, {"id": "b0000002", "branch": SPRINT, "stage": "sprint", "head": sprint_head,
                        "status": "red", "checks": {"test": {"exit": 1}}, "at": "2026-11-16T11:00:00+00:00",
                        "source": "batch"})
    _fake_run(layer, "c0000001", "Hand this one over", "handed-over",
              {"action": "handoff", "shipped": False, "target": "agents/c0000001/PR.md"})
    _fake_run(layer, "c0000002", "Waits for the sprint", "waiting")
    deliver(layer, app_repo, "d8000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    body = (layer / "agents" / "requests" / "sprint-main.md").read_text(encoding="utf-8")
    sections = {name: text for name, text in re.findall(r"^## (.+?)\n(.*?)(?=^## |\Z)", body, re.M | re.S)}
    assert ["Runs and commits", "What changed", "Checks", "Unblocks", "Not included"] == list(sections)
    assert "al never merges it" in body or "never merges it" in body
    assert f"al promote LAYER sprint main" in body
    assert "baseline: also failing on main in batch `b0000001`" in sections["Checks"]
    assert "Batch `b0000002`" in sections["Checks"]
    assert "c0000001" in sections["Not included"] and "handed over to a person" in sections["Not included"]
    assert "c0000002" in sections["Unblocks"]
    assert re.search(r"^- `kit`: 1 file, \+2 −0$", sections["What changed"], re.M), sections["What changed"]
    assert "d8000001" in sections["Runs and commits"] and "d8000002" in sections["Runs and commits"]
    assert str(Path.home()) not in body and str(tmp_path) not in body
    assert "tok-very-secret-4711" not in body and "another-secret-0815" not in body


# ---------------------------------------------------------------- adopted requests and promotions

def test_run_that_did_not_merge_adopts_the_ship_request(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    path = layer / "agentic-layer.json"
    data = json.loads(path.read_text())
    data["reviews"][0]["allowedClasses"] = ["chore"]   # a feature is not shipped by the gate: a pull request
    path.write_text(json.dumps(data, indent=2))
    state = deliver(layer, app_repo, "d9000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert state["ship"]["action"] == "pr" and not state["ship"]["shipped"]
    req = _request(state)
    assert req["kind"] == "run" and req["action"] == "adopted"
    assert req["path"] == "agents/d9000001/PR.md" and req["target"] == SPRINT
    assert not (layer / "agents" / "requests").exists()
    assert f"request:   adopted agents/d9000001/PR.md (run branch into {SPRINT})" in _run_log(layer, "d9000001")


def test_promotion_adopts_the_request_a_cycle_opened(make_layer, app_repo, tmp_path, monkeypatch):
    layer, _remote, fake = _forgejo(make_layer, app_repo, tmp_path, monkeypatch)
    state = deliver(layer, app_repo, "da000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    number = _request(state)["number"]
    comments = len(fake.comments.get(number, []))
    git(app_repo, "fetch", "-q", "origin", f"{SPRINT}:{SPRINT}")   # the run merged on the forge
    code, out = promote(layer, "sprint", "main")
    assert code == 75, out
    assert list(_requests_to_main(fake)) == [number]
    pid = json.loads((layer / "agents" / "promotions" / "sprint-main.json").read_text())["id"]
    assert state_of(layer, pid)["promotion"]["pr"]["number"] == number
    assert ("PATCH", f"/pulls/{number}") in fake.calls
    assert fake.pulls[number]["title"] == f"Promote {SPRINT} into main"
    assert f"Promotion `{pid}`" in fake.pulls[number]["body"] and fake.pulls[number]["body"].count(MARK) == 1
    assert len(fake.comments.get(number, [])) == comments   # no second proof on the same request
    record = json.loads((layer / "agents" / "requests" / "sprint-main.json").read_text())
    assert record["number"] == number and record["promotion"] == pid


DEV_QA = [
    {"name": "dev", "branch": "dev", "from": "main", "into": "qa", "accepts": ["chore", "bug", "feature"],
     "agentsMayMerge": True, "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "",
     "origin": "declared", "frozen": [], "frozenWhilePromoting": False},
    {"name": "qa", "branch": "qa", "from": "main", "into": "main", "accepts": [], "agentsMayMerge": True,
     "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "", "origin": "declared", "frozen": [],
     "frozenWhilePromoting": False},
    {"name": "main", "branch": "main", "from": "", "into": "", "accepts": [], "agentsMayMerge": False,
     "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "", "origin": "declared", "frozen": [],
     "frozenWhilePromoting": False},
]


def _promo(from_, to, **extra):
    return {"from": from_, "to": to, "approval": "human", "approvers": 1, "requires": ["checks", "acceptance"],
            "method": "merge", "version": None, "tag": "", "after": [], "backMerge": [], "adw": "demo",
            "present": ["summary", "changes", "proof", "demo", "checks", "notes", "excluded"], "hotfix": False,
            "origin": "declared", **extra}


def _dev_qa_layer(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo, stages=DEV_QA, extra={"cycle": None, "delivery": None},
                         promotions=[_promo("dev", "qa", approval="auto", requires=["checks"]), _promo("qa", "main")])
    git(app_repo, "branch", "dev", "main")
    git(app_repo, "branch", "qa", "main")
    return layer


def test_promotion_into_an_intermediate_stage_requests_the_next(make_layer, app_repo):
    layer = _dev_qa_layer(make_layer, app_repo)
    state = deliver(layer, app_repo, "db000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert _request(state)["path"] == "agents/requests/dev-qa.md"
    code, out = promote(layer, "dev", "qa")
    assert code == 0, out
    assert git(app_repo, "show", "refs/heads/qa:cart.py") == "CART = 1"
    assert (layer / "agents" / "requests" / "qa-main.md").is_file()
    assert "request:   written to agents/requests/qa-main.md (no forge)" in out
    text = (layer / "agents" / "requests" / "qa-main.md").read_text(encoding="utf-8")
    assert "db000001" in text and "al:delivery-request qa->main" in text


def test_final_stage_and_dry_run_open_nothing(make_layer, app_repo, tmp_path):
    plain = make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": n, "template": t, "modelSet": "base", "output": o} for n, t, o in (
            ("plan", "plan", "path"), ("build", "build", "text"), ("review", "review", "json"),
            ("patch", "plan-patch", "text"), ("implement", "implement", "text"))],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 20}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore", "bug", "feature", "patch"],
                     "blockProtectedPaths": True, "maxDiffLines": 2000, "requireChecks": ["test"], "ship": "merge",
                     "deployCmd": ""}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 27600, "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [delivering_adw()]})
    state = deliver(plain, app_repo, "dc000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert state["ship"]["action"] == "merge" and "delivery_request" not in state
    assert not (plain / "agents" / "requests").exists()
    assert "request:" not in _run_log(plain, "dc000001")
    shutil.move(str(plain), str(tmp_path / "plain"))   # the next layer is made where this one was
    # a dry run in a layer with stages: nothing opened, and why
    layer = sprint_layer(make_layer, app_repo)
    script(layer, "plan", {"layer_files": {"specs/p.md": "# Plan\n"}, "output": "specs/p.md"}, "demo")
    script(layer, "build", {"files": {"cart.py": "CART = 1\n"}}, "demo")
    script(layer, "review-1", {"output": json.dumps({"success": True, "summary": "ok", "issues": []})}, "demo")
    with contextlib.redirect_stdout(io.StringIO()):
        code = run_main("demo", layer, ["Add a cart", "--class", "feature", "--adw-id", "dd000001", "--dry-run"])
    assert code == 0
    assert state_of(layer, "dd000001")["delivery_request"]["skipped"] == "dry run"
    assert not (layer / "agents" / "requests").exists()
    assert "request:   skipped: dry run" in _run_log(layer, "dd000001")


def test_a_forge_failure_never_fails_the_run(make_layer, app_repo, tmp_path, monkeypatch):
    layer, _remote, fake = _forgejo(make_layer, app_repo, tmp_path, monkeypatch)

    def broken(self, head, base):
        raise forge.ForgeError("GET https://forge.example.org/api/v1/repos/acme/webshop/pulls -> HTTP 502: bad gateway")

    monkeypatch.setattr(forge.ForgejoForge, "find_prs", broken)
    state = deliver(layer, app_repo, "de000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert state["status"] == "passed"
    req = _request(state)
    assert "HTTP 502" in req["error"]
    assert req["path"] == "agents/requests/sprint-main.md" and req["note"] == "the forge failed"
    assert (layer / "agents" / "requests" / "sprint-main.md").is_file()
    assert _requests_to_main(fake) == {}
    log = _run_log(layer, "de000001")
    assert "request:   written to agents/requests/sprint-main.md (the forge failed) (error: ForgeError" in log
