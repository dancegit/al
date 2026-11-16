"""Fixture repositories with the branching histories `al scout` must recognise: trunk, sprint,
gitflow and release branches. Every commit has a fixed date (GIT_AUTHOR_DATE/GIT_COMMITTER_DATE),
so the cut dates and gaps detection reads are the same on every machine. Names are invented."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

START = 1767254400  # 2026-01-01T08:00:00Z
DAY = 86400


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = START
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "# webshop\n\nA small shop used as a fixture.\n")
        self.commit("initial")

    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_DATE": f"@{self.clock} +0000", "GIT_COMMITTER_DATE": f"@{self.clock} +0000",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
        result = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True, env=env, timeout=60)
        assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
        return result.stdout.strip()

    def write(self, rel: str, text: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str, rel: str | None = None, days: float = 1) -> None:
        self.clock += int(days * DAY)
        self.write(rel or f"changes/{message.replace(' ', '-').replace('/', '-')}.txt", message + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def branch(self, name: str, start: str = "HEAD") -> None:
        self.git("checkout", "-q", "-b", name, start)

    def checkout(self, name: str) -> None:
        self.git("checkout", "-q", name)

    def merge(self, name: str) -> None:
        self.clock += 3600
        self.git("merge", "-q", "--no-ff", "--no-edit", name)


def trunk(path: Path) -> Path:
    repo = Repo(path)
    repo.write(".github/workflows/ci.yml", "on:\n  push:\n    branches: [main]\n  pull_request:\n    branches: [main]\n")
    repo.commit("ci")
    for name in ("feature/login", "feature/cart", "feature/search"):
        repo.branch(name)
        repo.commit(f"{name} work", days=0.2)
        repo.checkout("main")
        repo.merge(name)
        repo.commit(f"after {name}", days=0.5)
    return path


def sprint(path: Path) -> Path:
    repo = Repo(path)
    for week in range(1, 8):
        name = f"sprint/2026-s{week:02d}"
        repo.commit(f"main before {name}", days=11 if week > 1 else 1)   # + 3 days of sprint work = cut every 14 days
        repo.branch(name)
        repo.commit(f"{name} story", days=3)
        repo.checkout("main")
        repo.merge(name)
    return path


def gitflow(path: Path) -> Path:
    repo = Repo(path)
    repo.write("CONTRIBUTING.md", "# Contributing\n\nBranch from `develop` and open your pull request against develop.\n"
                                  "Releases are cut from develop as release/X.Y.\n")
    repo.commit("contributing")
    repo.branch("develop")
    for version in ("1.0", "1.1"):
        feature = f"feature/for-{version}"
        repo.branch(feature, "develop")
        repo.commit(f"{feature} work")
        repo.checkout("develop")
        repo.merge(feature)
        repo.branch(f"release/{version}", "develop")
        repo.commit(f"release {version} notes")
        repo.checkout("main")
        repo.merge(f"release/{version}")
        repo.git("tag", f"v{version}.0")
        repo.checkout("develop")
        repo.merge(f"release/{version}")
    repo.checkout("main")
    return path


def release_branches(path: Path) -> Path:
    repo = Repo(path)
    for version in ("1.0", "1.1"):
        repo.commit(f"work before {version}", days=10)
        repo.branch(f"release/{version}")
        repo.commit(f"release {version} stabilise")
        repo.git("tag", f"v{version}.0")
        repo.checkout("main")
    return path
