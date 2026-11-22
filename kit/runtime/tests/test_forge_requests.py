"""The forge clients find a pull request by head and base, and update it in place (never merging)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME))

from adw_modules import forge  # noqa: E402

FORGEJO = {"id": "forgejo", "kind": "forgejo", "host": "https://git.example.org/", "repo": "acme/shop",
           "tokenEnv": "FORGEJO_TOKEN", "botMarker": "[ADW-BOT]", "status": "stable"}
GITHUB = {"id": "github", "kind": "github", "host": "github.com", "repo": "acme/shop",
          "tokenEnv": "GITHUB_TOKEN", "botMarker": "[ADW-BOT]", "status": "stable"}

ALL_ARGV: list[list[str]] = []   # every gh argv of this module's tests
ALL_URLS: list[str] = []         # every Forgejo URL of this module's tests


class FakeHttp:
    def __init__(self, responses=None):
        self.calls, self.responses = [], list(responses or [])

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "json": None if body is None else json.loads(body)})
        ALL_URLS.append(url)
        return 200, (self.responses.pop(0) if self.responses else {})


class FakeRun:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        ALL_ARGV.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def test_github_find_prs_lists_every_state_by_head_and_base(tmp_path):
    run = FakeRun(json.dumps([
        {"number": 3, "url": "https://github.com/acme/shop/pull/3", "state": "MERGED", "title": "old", "body": "a"},
        {"number": 9, "url": "https://github.com/acme/shop/pull/9", "state": "OPEN", "title": "new", "body": "b"},
        {"number": 5, "url": "https://github.com/acme/shop/pull/5", "state": "CLOSED", "title": "mid", "body": None},
    ]))
    c = forge.GitHubForge(GITHUB, tmp_path, env={}, run=run)
    found = c.find_prs("sprint/2026-s47", "main")
    assert run.calls[0][0] == ["gh", "pr", "list", "--head", "sprint/2026-s47", "--base", "main",
                               "--state", "all", "--json", "number,url,state,title,body", "--limit", "20",
                               "--repo", "acme/shop"]
    assert [(p["number"], p["state"]) for p in found] == [(9, "open"), (5, "closed"), (3, "merged")]
    assert found[1] == {"number": 5, "url": "https://github.com/acme/shop/pull/5", "state": "closed",
                        "title": "mid", "body": ""}


def test_github_update_pr_sends_the_body_on_stdin(tmp_path):
    run = FakeRun("https://github.com/acme/shop/pull/9\n")
    c = forge.GitHubForge(GITHUB, tmp_path, env={}, run=run)
    body = "Promote\n\n$(touch pwned)\n<!-- al:delivery-request sprint->main -->"
    c.update_pr(9, "Promote sprint into main", body)
    argv, kw = run.calls[0]
    assert argv == ["gh", "pr", "edit", "9", "--title", "Promote sprint into main", "--body-file", "-",
                    "--repo", "acme/shop"]
    assert kw["input"] == body and all(body not in a for a in argv)


def test_forgejo_find_prs_filters_head_and_base_and_reads_merged(tmp_path):
    http = FakeHttp([[
        {"number": 1, "state": "closed", "merged": True, "title": "a", "body": "x",
         "html_url": "https://git.example.org/acme/shop/pulls/1", "head": {"ref": "sprint"}, "base": {"ref": "main"}},
        {"number": 2, "state": "open", "merged": False, "title": "other head", "body": "",
         "html_url": "u2", "head": {"ref": "feature/x"}, "base": {"ref": "main"}},
        {"number": 3, "state": "open", "merged": False, "title": "other base", "body": "",
         "html_url": "u3", "head": {"ref": "sprint"}, "base": {"ref": "qa"}},
        {"number": 4, "state": "open", "merged": False, "title": "b", "body": None,
         "html_url": "https://git.example.org/acme/shop/pulls/4", "head": {"ref": "sprint"}, "base": {"ref": "main"}},
        {"number": 5, "state": "closed", "merged": False, "title": "c", "body": "",
         "html_url": "u5", "head": {"ref": "sprint"}, "base": {"ref": "main"}},
    ]])
    c = forge.ForgejoForge(FORGEJO, tmp_path, env={}, http=http)
    found = c.find_prs("sprint", "main")
    assert http.calls[0]["method"] == "GET"
    assert http.calls[0]["url"].startswith("https://git.example.org/api/v1/repos/acme/shop/pulls?")
    assert "state=all" in http.calls[0]["url"] and "limit=50" in http.calls[0]["url"]
    assert [(p["number"], p["state"]) for p in found] == [(5, "closed"), (4, "open"), (1, "merged")]
    assert found[1] == {"number": 4, "url": "https://git.example.org/acme/shop/pulls/4", "state": "open",
                        "title": "b", "body": ""}


def test_forgejo_update_pr_patches_title_and_body(tmp_path):
    http = FakeHttp()
    c = forge.ForgejoForge(FORGEJO, tmp_path, env={"FORGEJO_TOKEN": "s3cret"}, http=http)
    c.update_pr(7, "Promote sprint into main", "the body")
    call = http.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://git.example.org/api/v1/repos/acme/shop/pulls/7"
    assert call["json"] == {"title": "Promote sprint into main", "body": "the body"}


def test_none_forge_finds_nothing_and_cannot_update(tmp_path):
    c = forge.NoneForge({"id": "none", "kind": "none", "remote": ""}, tmp_path, env={})
    assert c.find_prs("sprint", "main") == []
    with pytest.raises(forge.ForgeError):
        c.update_pr(1, "t", "b")
    base = forge.ForgeClient({"kind": "base"}, tmp_path, env={})
    with pytest.raises(forge.ForgeError, match="cannot list pull requests"):
        base.find_prs("a", "b")
    with pytest.raises(forge.ForgeError):
        base.update_pr(1, "t", "b")


def test_find_and_update_never_merge(tmp_path):
    ALL_ARGV.clear()
    ALL_URLS.clear()
    for test in (test_github_find_prs_lists_every_state_by_head_and_base, test_github_update_pr_sends_the_body_on_stdin,
                 test_forgejo_find_prs_filters_head_and_base_and_reads_merged,
                 test_forgejo_update_pr_patches_title_and_body):
        test(tmp_path)
    assert ALL_ARGV and ALL_URLS
    assert not any("merge" in a for argv in ALL_ARGV for a in argv)
    assert not any(url.rstrip("/").endswith("/merge") for url in ALL_URLS)
