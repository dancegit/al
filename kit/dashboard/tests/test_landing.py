"""Landed detection (dashboard.Landing) on small git repositories built here: merged (ancestor),
squash-merged (patch-id), integrated (every path equal), partly landed, open, the forge's answer, a
gone branch, a remote-tracking target, the cache per target head, and that nothing is written."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import dashboard as d
from settle_fixtures import Git, _write


def repo_with_main(tmp_path: Path) -> tuple[Path, Git]:
    repo = tmp_path / "app"
    repo.mkdir()
    git = Git(repo)
    git("init", "-q", "-b", "main")
    _write(repo, "README.md", "# app\n")
    _write(repo, "src/a.py", "A = 1\n")
    git("add", "-A")
    git.commit("first")
    return repo, git


def branch(git: Git, repo: Path, name: str, files: dict[str, str | None], base: str = "main") -> str:
    git("checkout", "-q", "-b", name, base)
    for path, text in files.items():
        if text is None:
            git("rm", "-q", path)
        else:
            _write(repo, path, text)
    git("add", "-A")
    tip = git.commit(f"{name}: change")
    git("checkout", "-q", "main")
    return tip


def advance_main(git: Git, repo: Path, name: str = "other.txt", text: str = "other\n") -> str:
    _write(repo, name, text)
    git("add", "-A")
    return git.commit(f"main: {name}")


def test_merged_branch_is_landed_by_ancestry_with_commit_and_time(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n"})
    advance_main(git, repo)
    git("merge", "-q", "--no-ff", "-m", "Merge al/run1", "al/run1")
    merge = git("rev-parse", "HEAD")
    result = d.Landing().check(repo, "al/run1", tip, "main")
    assert result["state"] == "landed" and result["how"] == "ancestor" and result["target"] == "main"
    assert result["commit"] == merge[:7]
    assert result["at"] == int(git("log", "-1", "--format=%ct", merge))


def test_squash_merged_branch_is_landed_by_patch_id(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n"})
    advance_main(git, repo)
    git("merge", "-q", "--squash", "al/run1")
    squash = git.commit("run1 (squashed)")
    advance_main(git, repo, "later.txt")
    result = d.Landing().check(repo, "al/run1", tip, "main")
    assert (result["state"], result["how"], result["commit"]) == ("landed", "patch-id", squash[:7])


def test_integrated_change_is_landed_by_equal_paths(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n", "src/a.py": None})
    # another change brings the same content in, together with more: neither ancestor nor patch-id
    _write(repo, "src/b.py", "B = 1\n")
    _write(repo, "src/c.py", "C = 1\n")
    git("rm", "-q", "src/a.py")
    git("add", "-A")
    integrated = git.commit("integrate run1 with more")
    advance_main(git, repo)
    result = d.Landing().check(repo, "al/run1", tip, "main")
    assert (result["state"], result["how"], result["commit"]) == ("landed", "paths", integrated[:7])


def test_partly_landed_lists_the_differing_paths(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n", "src/c.py": "C = 1\n"})
    _write(repo, "src/b.py", "B = 1\n")
    _write(repo, "src/c.py", "C = 2  # changed on the way\n")
    git("add", "-A")
    git.commit("take part of run1")
    result = d.Landing().check(repo, "al/run1", tip, "main")
    assert result["state"] == "partly"
    assert result["differs"] == ["src/c.py"] and result["equal"] == 1


def test_open_branch_is_open(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n"})
    advance_main(git, repo)
    assert d.Landing().check(repo, "al/run1", tip, "main") == {"state": "open", "target": "main"}


def test_forge_merged_pr_lands_when_local_checks_do_not(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n"})
    asked: list = []

    def pr_state(number):
        asked.append(number)
        return {"state": "merged", "commit": "0123456789abcdef", "at": "2026-11-03T10:00:00Z"}

    landing = d.Landing()
    result = landing.check(repo, "al/run1", tip, "main", pr=7, pr_state=pr_state)
    assert (result["state"], result["how"], result["commit"]) == ("landed", "forge", "0123456")
    assert result["at"] == d.parse_time("2026-11-03T10:00:00Z") and asked == [7]
    assert landing.check(repo, "al/run1", tip, "main", pr=7, pr_state=lambda n: {"state": "open"})["state"] == "open"
    assert landing.check(repo, "al/run1", tip, "main")["state"] == "open"   # no forge asked: local only


def test_gone_branch_uses_ship_commit_else_unknown(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n"})
    git("merge", "-q", "--no-ff", "-m", "Merge al/run1", "al/run1")
    git("branch", "-q", "-D", "al/run1")
    landing = d.Landing()
    assert landing.check(repo, "al/run1", tip, "main")["how"] == "ancestor"
    gone = landing.check(repo, "al/run1", "", "main")
    assert gone == {"state": "unknown", "why": "branch and commit are gone", "target": "main"}
    missing = landing.check(repo, "al/run1", "f" * 40, "main")
    assert missing["state"] == "unknown"


def test_remote_tracking_target_counts(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n"})
    git("checkout", "-q", "-b", "scratch", "main")
    git("merge", "-q", "--no-ff", "-m", "Merge al/run1 on the forge", "al/run1")
    merged = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("update-ref", "refs/remotes/origin/main", merged)
    git("branch", "-q", "-D", "scratch")
    result = d.Landing().check(repo, "al/run1", tip, "main")
    assert (result["state"], result["how"], result["commit"]) == ("landed", "ancestor", merged[:7])


def test_cache_is_keyed_by_target_head(tmp_path, monkeypatch):
    repo, git = repo_with_main(tmp_path)
    tip = branch(git, repo, "al/run1", {"src/b.py": "B = 1\n"})
    calls: list[tuple] = []
    real = d.safe_git

    def counting(*args, **kwargs):
        calls.append(args[1:])
        return real(*args, **kwargs)

    monkeypatch.setattr(d, "safe_git", counting)
    landing = d.Landing()
    assert landing.check(repo, "al/run1", tip, "main")["state"] == "open"
    first = len(calls)
    assert first > 0
    assert landing.check(repo, "al/run1", tip, "main")["state"] == "open"
    assert len(calls) == first   # cached: no git call
    git("merge", "-q", "--no-ff", "-m", "Merge al/run1", "al/run1")
    assert landing.check(repo, "al/run1", tip, "main")["how"] == "ancestor"   # a new target head recomputes
    assert len(calls) > first


def _fingerprint(repo: Path, git: Git) -> tuple:
    index = repo / ".git" / "index"
    return (git("for-each-ref", "--format=%(refname) %(objectname)"), git("symbolic-ref", "HEAD"),
            hashlib.sha256(index.read_bytes()).hexdigest(), sorted(p.name for p in (repo / ".git").iterdir()))


def test_detection_never_writes(tmp_path):
    repo, git = repo_with_main(tmp_path)
    tips = {
        "al/merged": branch(git, repo, "al/merged", {"src/m.py": "M\n"}),
        "al/squashed": branch(git, repo, "al/squashed", {"src/s.py": "S\n"}),
        "al/open": branch(git, repo, "al/open", {"src/o.py": "O\n"}),
        "al/partly": branch(git, repo, "al/partly", {"src/p.py": "P\n", "src/q.py": "Q\n"}),
    }
    git("merge", "-q", "--no-ff", "-m", "Merge", "al/merged")
    git("merge", "-q", "--squash", "al/squashed")
    git.commit("squashed")
    _write(repo, "src/p.py", "P\n")
    git("add", "-A")
    git.commit("part")
    before = _fingerprint(repo, git)
    landing = d.Landing()
    states = {name: landing.check(repo, name, tip, "main")["state"] for name, tip in tips.items()}
    assert states == {"al/merged": "landed", "al/squashed": "landed", "al/open": "open", "al/partly": "partly"}
    landing.contains(repo, {"branch": "al/merged"}, {"branch": "main"})
    assert _fingerprint(repo, git) == before


@pytest.mark.parametrize("target", ["main", "release"])
def test_missing_repository_or_target_is_unknown(tmp_path, target):
    assert d.Landing().check(tmp_path / "nowhere", "al/x", "", target)["state"] == "unknown"
    repo, git = repo_with_main(tmp_path)
    result = d.Landing().check(repo, "al/x", git("rev-parse", "HEAD"), target)
    assert result["state"] == ("landed" if target == "main" else "unknown")
