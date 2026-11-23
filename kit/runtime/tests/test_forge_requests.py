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
ALL_JSON: list = []              # every Forgejo request payload of this module's tests


class FakeHttp:
    def __init__(self, responses=None):
        self.calls, self.responses = [], list(responses or [])

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "json": None if body is None else json.loads(body)})
        ALL_URLS.append(url)
        ALL_JSON.append(self.calls[-1]["json"])
        return 200, (self.responses.pop(0) if self.responses else {})


class FakeRun:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        ALL_ARGV.append(list(argv))
        out = self.stdout(argv) if callable(self.stdout) else self.stdout  # a callable answers per command
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


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


def _forgejo_pr(number, head, base, state="open", merged=False, title="t", body=""):
    return {"number": number, "state": state, "merged": merged, "title": title, "body": body,
            "html_url": f"https://git.example.org/acme/shop/pulls/{number}",
            "head": {"ref": head}, "base": {"ref": base}}


def test_forgejo_create_pr_updates_the_open_pr_of_the_same_head_and_base(tmp_path):
    http = FakeHttp([[_forgejo_pr(5, "feature/x", "main", title="WIP: [needs human] feature: x", body="handover")]])
    c = forge.ForgejoForge(FORGEJO, tmp_path, env={}, http=http)
    pr = c.create_pr("feature/x", "main", "feature: x", "final")
    assert pr == {"number": 5, "url": "https://git.example.org/acme/shop/pulls/5"}
    assert http.calls[0]["method"] == "GET"
    patches = [call for call in http.calls if call["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["url"] == "https://git.example.org/api/v1/repos/acme/shop/pulls/5"
    assert patches[0]["json"] == {"title": "feature: x", "body": "final"}
    assert not [call for call in http.calls if call["method"] == "POST"]


def test_forgejo_create_pr_opens_one_when_none_or_only_closed_is_there(tmp_path):
    only_closed = [_forgejo_pr(4, "feature/x", "main", state="closed"),
                   _forgejo_pr(3, "feature/x", "main", state="closed", merged=True)]
    for pulls in ([], only_closed):
        http = FakeHttp([pulls, {"number": 9, "html_url": "https://git.example.org/acme/shop/pulls/9"}])
        c = forge.ForgejoForge(FORGEJO, tmp_path, env={}, http=http)
        pr = c.create_pr("feature/x", "main", "feature: x", "final")
        assert pr == {"number": 9, "url": "https://git.example.org/acme/shop/pulls/9"}
        posts = [call for call in http.calls if call["method"] == "POST"]
        assert len(posts) == 1 and posts[0]["url"] == "https://git.example.org/api/v1/repos/acme/shop/pulls"
        assert posts[0]["json"] == {"head": "feature/x", "base": "main", "title": "feature: x", "body": "final"}
        assert not [call for call in http.calls if call["method"] == "PATCH"]


def test_forgejo_create_pr_leaves_other_heads_alone(tmp_path):
    http = FakeHttp([[_forgejo_pr(7, "feature/y", "main"), _forgejo_pr(6, "feature/x", "sprint")],
                     {"number": 9, "html_url": "https://git.example.org/acme/shop/pulls/9"}])
    c = forge.ForgejoForge(FORGEJO, tmp_path, env={}, http=http)
    assert c.create_pr("feature/x", "main", "feature: x", "final")["number"] == 9
    posts = [call for call in http.calls if call["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["json"]["head"] == "feature/x" and posts[0]["json"]["base"] == "main"
    assert not [call for call in http.calls if call["method"] == "PATCH"]
    assert not any(call["url"].split("?")[0].endswith(("/pulls/7", "/pulls/6")) for call in http.calls)


class FakeGh:
    """`gh` against a fake forge holding `pulls`: `pr list --head --base` filters as GitHub does."""

    def __init__(self, pulls, drafts=()):
        self.pulls, self.drafts = pulls, set(drafts)

    def __call__(self, argv):
        if argv[1:3] == ["pr", "list"]:
            head, base = argv[argv.index("--head") + 1], argv[argv.index("--base") + 1]
            return json.dumps([{k: p[k] for k in ("number", "url", "state", "title", "body")}
                               for p in self.pulls if (p["head"], p["base"]) == (head, base)])
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"isDraft": int(argv[3]) in self.drafts})
        if argv[1:3] == ["pr", "create"]:
            return "https://github.com/acme/shop/pull/50\n"
        return ""


def _gh_pr(number, head, base, state="OPEN", title="t"):
    return {"number": number, "url": f"https://github.com/acme/shop/pull/{number}", "state": state,
            "title": title, "body": "", "head": head, "base": base}


def _subcommands(run):
    return [argv[1:3] for argv, _kw in run.calls]


def test_github_create_pr_updates_the_open_pr_and_marks_it_ready(tmp_path):
    run = FakeRun(FakeGh([_gh_pr(42, "feature/x", "main", title="[needs human] feature: x")], drafts={42}))
    c = forge.GitHubForge(GITHUB, tmp_path, env={}, run=run)
    pr = c.create_pr("feature/x", "main", "feature: x", "final")
    assert pr == {"number": 42, "url": "https://github.com/acme/shop/pull/42"}
    edits = [(argv, kw) for argv, kw in run.calls if argv[1:3] == ["pr", "edit"]]
    assert len(edits) == 1
    assert edits[0][0] == ["gh", "pr", "edit", "42", "--title", "feature: x", "--body-file", "-", "--repo", "acme/shop"]
    assert edits[0][1]["input"] == "final"
    assert ["gh", "pr", "ready", "42", "--repo", "acme/shop"] in [argv for argv, _kw in run.calls]
    assert ["pr", "create"] not in _subcommands(run)


def test_github_create_pr_opens_one_when_none_or_only_closed_is_there(tmp_path):
    only_closed = [_gh_pr(4, "feature/x", "main", state="CLOSED"), _gh_pr(3, "feature/x", "main", state="MERGED")]
    for listed in ([], only_closed):
        run = FakeRun(FakeGh(listed))
        c = forge.GitHubForge(GITHUB, tmp_path, env={}, run=run)
        pr = c.create_pr("feature/x", "main", "feature: x", "final")
        assert pr == {"number": 50, "url": "https://github.com/acme/shop/pull/50"}
        assert run.calls[-1][0] == ["gh", "pr", "create", "--head", "feature/x", "--base", "main",
                                    "--title", "feature: x", "--body", "final", "--repo", "acme/shop"]
        assert ["pr", "edit"] not in _subcommands(run) and ["pr", "ready"] not in _subcommands(run)


def test_github_create_pr_leaves_other_heads_alone(tmp_path):
    run = FakeRun(FakeGh([_gh_pr(7, "feature/y", "main"), _gh_pr(6, "feature/x", "sprint")], drafts={7, 6}))
    c = forge.GitHubForge(GITHUB, tmp_path, env={}, run=run)
    assert c.create_pr("feature/x", "main", "feature: x", "final")["number"] == 50
    lookup = run.calls[0][0]
    assert lookup[:7] == ["gh", "pr", "list", "--head", "feature/x", "--base", "main"]
    assert ["pr", "create"] in _subcommands(run)
    assert ["pr", "edit"] not in _subcommands(run) and ["pr", "ready"] not in _subcommands(run)
    assert not any(argv[3:4] in (["7"], ["6"]) for argv, _kw in run.calls)


def test_find_and_update_never_merge(tmp_path):
    ALL_ARGV.clear()
    ALL_URLS.clear()
    ALL_JSON.clear()
    for test in (test_github_find_prs_lists_every_state_by_head_and_base, test_github_update_pr_sends_the_body_on_stdin,
                 test_forgejo_find_prs_filters_head_and_base_and_reads_merged,
                 test_forgejo_update_pr_patches_title_and_body,
                 test_forgejo_create_pr_updates_the_open_pr_of_the_same_head_and_base,
                 test_forgejo_create_pr_opens_one_when_none_or_only_closed_is_there,
                 test_forgejo_create_pr_leaves_other_heads_alone,
                 test_github_create_pr_updates_the_open_pr_and_marks_it_ready,
                 test_github_create_pr_opens_one_when_none_or_only_closed_is_there,
                 test_github_create_pr_leaves_other_heads_alone):
        test(tmp_path)
    assert ALL_ARGV and ALL_URLS
    assert not any("merge" in a for argv in ALL_ARGV for a in argv)
    assert not any(url.rstrip("/").endswith("/merge") for url in ALL_URLS)
    assert not any(a in ("close", "reopen") for argv in ALL_ARGV for a in argv)
    assert not any(url.split("?")[0].rstrip("/").endswith(("/close", "/reopen")) for url in ALL_URLS)
    assert not any(isinstance(j, dict) and "state" in j for j in ALL_JSON)  # no PATCH closes or reopens
