import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME))

from adw_modules import forge  # noqa: E402


def layer(tmp_path, forge_opt):
    return SimpleNamespace(ir={"forge": forge_opt}, layer_dir=tmp_path, app_root=tmp_path / "app")


FORGEJO = {"id": "forgejo", "kind": "forgejo", "host": "https://git.example.org/", "repo": "acme/shop",
           "tokenEnv": "FORGEJO_TOKEN", "botMarker": "[ADW-BOT]", "status": "stable"}
GITHUB = {"id": "github", "kind": "github", "host": "github.com", "repo": "acme/shop",
          "tokenEnv": "GITHUB_TOKEN", "botMarker": "[ADW-BOT]", "status": "stable"}


class FakeHttp:
    def __init__(self, responses=None):
        self.calls, self.responses = [], list(responses or [])

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "json": None if body is None else json.loads(body)})
        return 200, (self.responses.pop(0) if self.responses else {})


def test_forgejo_comment_request(tmp_path, monkeypatch):
    http = FakeHttp()
    monkeypatch.setattr(forge, "http_request", http)
    c = forge.get_client(layer(tmp_path, FORGEJO), env={"FORGEJO_TOKEN": "s3cret"})
    assert c.kind == "forgejo"
    c.comment(7, "adw abc started")
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://git.example.org/api/v1/repos/acme/shop/issues/7/comments"
    assert call["headers"]["Authorization"] == "token s3cret"
    assert call["json"] == {"body": "[ADW-BOT] adw abc started"}


def test_forgejo_issue_list_pr_merge(tmp_path):
    http = FakeHttp([
        {"number": 3, "title": "T", "body": "B", "labels": [{"name": "adw"}], "state": "open"},
        [{"number": 3, "title": "T", "body": "", "labels": [{"name": "adw"}, {"name": "bug"}], "state": "open"},
         {"number": 4, "title": "U", "body": "", "labels": [{"name": "bug"}], "state": "open"}],
        [{"id": 1, "body": "hi", "user": {"login": "per"}}],
        {"number": 12, "html_url": "https://git.example.org/acme/shop/pulls/12"},
        {},
        None,
    ])
    c = forge.ForgejoForge(FORGEJO, tmp_path, env={}, http=http)
    assert c.get_issue(3) == {"number": 3, "title": "T", "body": "B", "labels": ["adw"], "state": "open"}
    assert [i["number"] for i in c.list_issues(["adw"])] == [3]
    assert "labels=adw" in http.calls[1]["url"] and "state=open" in http.calls[1]["url"]
    assert c.list_comments(3) == [{"id": 1, "body": "hi", "user": "per"}]
    pr = c.create_pr("feat-x", "main", "Add x", "body", draft=True, reviewers=["alice"])
    assert pr == {"number": 12, "url": "https://git.example.org/acme/shop/pulls/12"}
    assert http.calls[3]["json"] == {"head": "feat-x", "base": "main", "title": "WIP: Add x", "body": "body"}
    assert http.calls[4]["url"].endswith("/pulls/12/requested_reviewers")
    assert http.calls[4]["json"] == {"reviewers": ["alice"]}
    c.merge_pr(12)
    assert http.calls[5]["method"] == "POST"
    assert http.calls[5]["url"] == "https://git.example.org/api/v1/repos/acme/shop/pulls/12/merge"
    assert http.calls[5]["json"] == {"Do": "squash"}
    assert "Authorization" not in http.calls[0]["headers"]  # no token in env -> no header


class FakeRun:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def test_github_argv_and_env(tmp_path, monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(forge.subprocess, "run", run)
    c = forge.get_client(layer(tmp_path, {**GITHUB, "host": "ghe.corp.example"}), env={"GITHUB_TOKEN": "tok"})
    c.comment(5, "hello")
    argv, kw = run.calls[0]
    assert argv == ["gh", "issue", "comment", "5", "--body", "[ADW-BOT] hello", "--repo", "acme/shop"]
    assert kw["env"]["GH_TOKEN"] == "tok" and kw["env"]["GH_HOST"] == "ghe.corp.example"


def test_github_issue_pr_merge(tmp_path):
    run = FakeRun(json.dumps({"number": 9, "title": "T", "body": "B", "labels": [{"name": "adw"}], "state": "OPEN"}))
    c = forge.GitHubForge(GITHUB, tmp_path, env={}, run=run)
    assert c.get_issue(9)["state"] == "open" and c.get_issue(9)["labels"] == ["adw"]
    assert run.calls[0][0] == ["gh", "issue", "view", "9", "--json", "number,title,body,labels,state",
                               "--repo", "acme/shop"]
    assert "GH_HOST" not in run.calls[0][1]["env"] and "GH_TOKEN" not in run.calls[0][1]["env"]
    run.stdout = "https://github.com/acme/shop/pull/42\n"
    assert c.create_pr("b", "main", "t", "x", draft=True, reviewers=["r1"]) == {
        "number": 42, "url": "https://github.com/acme/shop/pull/42"}
    assert run.calls[-1][0] == ["gh", "pr", "create", "--head", "b", "--base", "main", "--title", "t",
                                "--body", "x", "--draft", "--reviewer", "r1", "--repo", "acme/shop"]
    c.merge_pr(42)
    assert run.calls[-1][0] == ["gh", "pr", "merge", "42", "--squash", "--repo", "acme/shop"]
    run.stdout = "[]"
    c.list_issues(["adw", "bug"])
    assert run.calls[-1][0][:4] == ["gh", "issue", "list", "--state"]
    assert run.calls[-1][0].count("--label") == 2


def test_github_error_is_forge_error(tmp_path):
    def bad(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404")
    c = forge.GitHubForge(GITHUB, tmp_path, env={}, run=bad)
    with pytest.raises(forge.ForgeError, match="404"):
        c.get_issue(1)


def test_bot_marker_prefix_idempotent(tmp_path):
    c = forge.ForgejoForge({**FORGEJO, "botMarker": "<!--bot-->"}, tmp_path, env={}, http=FakeHttp())
    assert c.mark("x") == "<!--bot--> x"
    assert c.mark("<!--bot--> x") == "<!--bot--> x"
    assert c.is_bot("  <!--bot--> hi") and not c.is_bot("hi")


def test_none_forge(tmp_path):
    c = forge.get_client(layer(tmp_path, {"id": "none", "kind": "none", "remote": "", "status": "stable"}), env={})
    assert c.kind == "none"
    with pytest.raises(forge.ForgeError, match="no issues"):
        c.get_issue(1)
    with pytest.raises(forge.ForgeError):
        c.list_issues()
    c.comment(4, "adw x passed")
    log = (tmp_path / "agents" / "forge-comments.log").read_text()
    assert "#4 [ADW-BOT] adw x passed" in log
    assert c.create_pr("b", "main", "t", "x") == {"number": 0, "url": ""}
    assert c.merge_pr(0) is None


def test_default_forge_is_none_and_path_layer(tmp_path):
    (tmp_path / "agentic-layer.json").write_text(json.dumps({"codebase": {"root": "../app"}}))
    assert forge.get_client(str(tmp_path), env={}).kind == "none"
    lay = forge.as_layer(tmp_path)
    assert lay.layer_dir == tmp_path.resolve()
