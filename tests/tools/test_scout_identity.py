"""The `al init` / `al scout` draft names the repository, not the directory of a worktree, and
records the remote as an https URL without credentials (never a token, never a local path)."""
from __future__ import annotations

import json

import pytest

from conftest import al, git


def _scout(path) -> tuple[str, dict]:
    return al("scout", path).stdout, json.loads(al("scout", path, "--json").stdout)


@pytest.mark.parametrize("remote, url", [
    ("git@forge.example.org:team/notes-app.git", "https://forge.example.org/team/notes-app"),
    ("ssh://git@forge.example.org:2222/team/notes-app.git", "https://forge.example.org/team/notes-app"),
    ("https://bot:s3cret-token@forge.example.org/team/notes-app.git", "https://forge.example.org/team/notes-app"),
    ("https://ghp_s3cret-token@github.com/team/notes-app", "https://github.com/team/notes-app"),
    ("http://127.0.0.1:3000/team/notes-app.git", "http://127.0.0.1:3000/team/notes-app"),
])
def test_the_remote_is_recorded_as_a_web_url_without_credentials(app, remote, url) -> None:
    git(app, "remote", "add", "origin", remote)
    text, facts = _scout(app)
    assert f"- Repo URL: {url}\n" in text and facts["remote"] == url
    assert "s3cret" not in text + json.dumps(facts) and "git@" not in text
    assert text.startswith("# notes-app\n")                                       # the repository's name


def test_a_local_path_remote_is_not_written(app, tmp_path) -> None:
    git(app, "remote", "add", "origin", str(tmp_path / "elsewhere" / "notes.git"))
    text, facts = _scout(app)
    assert str(tmp_path) not in text and facts["remote"] == ""
    assert "- Repo URL: UNVERIFIED (origin is not a web URL)" in text


def test_a_worktree_draft_is_named_after_the_repository(app, tmp_path) -> None:
    worktree = tmp_path / "agent-1234abcd"
    git(app, "worktree", "add", "-q", "-b", "side", str(worktree))
    text, _ = _scout(worktree)
    assert text.startswith("# notes\n"), text.splitlines()[0]                   # the main checkout's directory
    git(app, "remote", "add", "origin", "git@forge.example.org:team/notes-app.git")
    assert _scout(worktree)[0].startswith("# notes-app\n")                         # the remote's name wins
