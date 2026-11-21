"""The forge's CI on one commit: GitHub check runs and commit statuses (through a fake `gh`), Forgejo's
combined status (through a fake `http`), and wait_for_ci on a fake clock. No network, no `gh`."""
from __future__ import annotations

import json
import subprocess

import pytest

from adw_modules import forge

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_github_commit_checks_reads_check_runs_and_statuses():
    argvs: list[list[str]] = []
    answers = {
        f"repos/acme/webshop/commits/{SHA}/check-runs?per_page=100": {"total_count": 4, "check_runs": [
            {"name": "build", "status": "completed", "conclusion": "success",
             "html_url": "https://github.com/acme/webshop/runs/1"},
            {"name": "lint", "status": "in_progress", "conclusion": None,
             "html_url": "https://github.com/acme/webshop/runs/2"},
            {"name": "docs", "status": "completed", "conclusion": "skipped", "html_url": "",
             "details_url": "https://ci.example.org/docs"},
            {"name": "tools (3.12)", "status": "completed", "conclusion": "failure",
             "html_url": "https://github.com/acme/webshop/runs/4"}]},
        f"repos/acme/webshop/commits/{SHA}/status": {"state": "failure", "statuses": [
            {"context": "deploy-preview", "state": "pending", "target_url": "https://preview.example.org/1"},
            {"context": "legacy", "state": "error", "target_url": "https://legacy.example.org/2"}]},
    }

    def fake(argv, **kwargs):
        argvs.append(list(argv))
        assert argv[:2] == ["gh", "api"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(answers[argv[2]]), "")

    client = forge.GitHubForge({"repo": "acme/webshop"}, run=fake)
    assert client.commit_checks(SHA) == [
        {"name": "build", "state": "success", "url": "https://github.com/acme/webshop/runs/1"},
        {"name": "lint", "state": "pending", "url": "https://github.com/acme/webshop/runs/2"},
        {"name": "docs", "state": "success", "url": "https://ci.example.org/docs"},
        {"name": "tools (3.12)", "state": "failure", "url": "https://github.com/acme/webshop/runs/4"},
        {"name": "deploy-preview", "state": "pending", "url": "https://preview.example.org/1"},
        {"name": "legacy", "state": "failure", "url": "https://legacy.example.org/2"},
    ]
    assert len(argvs) == 2
    assert not any("--repo" in argv for argv in argvs)


def test_github_api_failure_is_a_forge_error():
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 401: Bad credentials")

    with pytest.raises(forge.ForgeError, match="Bad credentials"):
        forge.GitHubForge({"repo": "acme/webshop"}, run=fake).commit_checks(SHA)


def test_forgejo_commit_checks_reads_the_combined_status():
    urls: list[str] = []

    def fake(method, url, headers, body, timeout=30.0):
        urls.append(url)
        assert method == "GET"
        return 200, {"state": "failure", "sha": SHA, "statuses": [
            {"context": f"ci/{status}", "status": status, "target_url": f"https://ci.example.org/{status}"}
            for status in ("success", "pending", "failure", "warning", "error")]}

    client = forge.ForgejoForge({"host": "https://forge.example.org", "repo": "acme/webshop"}, http=fake)
    assert [(c["name"], c["state"], c["url"]) for c in client.commit_checks(SHA)] == [
        ("ci/success", "success", "https://ci.example.org/success"),
        ("ci/pending", "pending", "https://ci.example.org/pending"),
        ("ci/failure", "failure", "https://ci.example.org/failure"),
        ("ci/warning", "failure", "https://ci.example.org/warning"),
        ("ci/error", "failure", "https://ci.example.org/error"),
    ]
    assert urls == [f"https://forge.example.org/api/v1/repos/acme/webshop/commits/{SHA}/status"]


def test_none_forge_has_no_ci():
    with pytest.raises(forge.ForgeError, match="no CI status"):
        forge.NoneForge({}).commit_checks("abc")


def _run(name, state):
    return {"name": name, "state": state, "url": f"https://ci.example.org/{name}"}


ERROR = forge.ForgeError("gh api failed (1): not logged in")
CASES = {
    "passed": ([[_run("a", "pending"), _run("b", "success")], [_run("a", "success"), _run("b", "success")],
                [_run("a", "success"), _run("b", "success")]], "passed", 3),
    "failed-at-once": ([[_run("a", "pending"), _run("b", "failure")]], "failed", 1),
    "cancelled": ([[_run("a", "success"), _run("b", "cancelled")]], "cancelled", 1),
    "timeout": ([[_run("a", "pending")]], "timeout", None),
    "missing": ([[]], "missing", None),
    "forge-errors": ([ERROR], "missing", None),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_wait_for_ci_outcomes(case, monkeypatch):
    polls, outcome, n_polls = CASES[case]
    clock = {"now": 1000.0}
    monkeypatch.setattr(forge, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(forge, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    calls: list[str] = []

    class Stub:
        def commit_checks(self, sha):
            calls.append(sha)
            answer = polls[min(len(calls) - 1, len(polls) - 1)]
            if isinstance(answer, Exception):
                raise answer
            return [dict(run) for run in answer]

    result = forge.wait_for_ci(Stub(), SHA, 300, poll_s=30)
    assert result["outcome"] == outcome
    assert result["commit"] == SHA
    last = polls[-1]
    assert result["runs"] == ([] if isinstance(last, Exception) else last)
    assert 0 <= result["waited_s"] <= 300
    if n_polls is not None:
        assert len(calls) == n_polls
    else:
        assert result["waited_s"] == 300
    if case == "forge-errors":
        assert "not logged in" in result["error"]
    else:
        assert result["error"] == ""
