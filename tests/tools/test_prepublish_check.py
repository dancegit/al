"""`tools/prepublish_check.sh`: the scan a person runs before making the repository public. Each test
builds a tiny git repository in a temporary folder and runs the scan on it: what must be found is
found with its commit and path, what is harmless passes, and nothing is printed in full.

Every value that the scan looks for is put together from parts at run time, so this file holds no
string that the scan (or any other scanner) would report. No network, no gitleaks (`--no-external`).

    uv run --with pytest pytest tests/tools/test_prepublish_check.py -q
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from conftest import ROOT

SCAN = ROOT / "tools" / "prepublish_check.sh"

NOREPLY = "12345+someone" + "@" + "users.noreply.github.com"
AGENT = "adw" + "@" + "localhost"
FOREIGN = "someone" + "@" + "example.net"

KEY_HEADER = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
TOKEN = "gh" + "p_" + "A1b2C3d4E5" * 4
HOME_PATH = "/" + "home" + "/" + "marta" + "/x"
USERS_PATH = "/" + "Users" + "/" + "marta" + "/x"
INTERNAL_HOSTS = ["build" + ".internal", "nas" + ".lan", "wiki.acme" + ".corp"]
ADDRESS = ".".join(["8", "8", "4", "4"])
HARMLESS = ["127.0.0.1", "0.0.0.0", ".".join(["192", "0", "2", "7"]), ".".join(["198", "51", "100", "9"]),
            ".".join(["203", "0", "113", "5"]), "https://example.org/page", "/home/$USER/x", "~/code/app"]

LINE = re.compile(r"^(?P<kind>[A-Z0-9]+)  (?P<commit>[0-9a-f]{12}|tree {8})  (?P<path>\S+?)(?::(?P<line>\d+))?  (?P<detail>.+)$")


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0")
    return env


def git(repo: Path, *args: str, mail: str = NOREPLY) -> str:
    cp = subprocess.run(["git", "-c", "user.name=Someone", "-c", f"user.email={mail}", "-c", "commit.gpgsign=false",
                         "-c", "init.defaultBranch=main", *args],
                        cwd=repo, env=_env(), capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str | bytes | None], mail: str = NOREPLY) -> str:
    """Write (or with None delete) the files, commit, return the commit id."""
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    git(repo, "add", "-A", mail=mail)
    git(repo, "commit", "-q", "-m", message, mail=mail)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    commit(repo, "first", {"README.md": "# A tiny repository\n\nNothing to see.\n", "app.py": "print('hello')\n"})
    return repo


def scan(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCAN), "--repo", str(repo), "--no-external", *args],
                          env=_env(), capture_output=True, text=True, timeout=300)


def findings(cp: subprocess.CompletedProcess) -> list[dict]:
    return [m.groupdict() for m in map(LINE.match, cp.stdout.splitlines()) if m]


def of_kind(cp: subprocess.CompletedProcess, kind: str) -> list[dict]:
    return [f for f in findings(cp) if f["kind"] == kind]


def test_clean_repository_exits_zero(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "by the agent", {"notes.txt": "\n".join(HARMLESS) + "\n"}, mail=AGENT)
    cp = scan(repo)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert findings(cp) == []
    assert "no finding" in cp.stdout


def test_secret_in_the_tree_is_found(tmp_path):
    repo = make_repo(tmp_path)
    sha = commit(repo, "a key", {"deploy/key.pem": KEY_HEADER + "\nMIIabc\n",
                                 "config.py": "# settings\nGITHUB = '" + TOKEN + "'\n"})
    cp = scan(repo)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    secrets = of_kind(cp, "SECRET")
    in_tree = {(f["path"], f["line"]) for f in secrets if f["commit"].strip() == "tree"}
    assert in_tree == {("deploy/key.pem", "1"), ("config.py", "2")}
    in_history = {(f["commit"], f["path"]) for f in secrets if f["commit"].strip() != "tree"}
    assert in_history == {(sha[:12], "deploy/key.pem"), (sha[:12], "config.py")}
    assert TOKEN not in cp.stdout and KEY_HEADER not in cp.stdout and TOKEN[8:] not in cp.stdout
    assert all("redacted" in f["detail"] for f in secrets)


def test_secret_removed_later_is_found_in_history(tmp_path):
    repo = make_repo(tmp_path)
    added = commit(repo, "oops", {"env.sh": "export A=1\nexport GH=" + TOKEN + "\n"})
    removed = commit(repo, "remove it", {"env.sh": None})
    assert not (repo / "env.sh").exists()
    cp = scan(repo)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    secrets = of_kind(cp, "SECRET")
    assert [(f["commit"], f["path"], f["line"]) for f in secrets] == [(added[:12], "env.sh", "2")]
    assert removed[:12] not in cp.stdout and TOKEN not in cp.stdout
    # the tree alone is clean
    tree_only = scan(repo, "--tree-only")
    assert tree_only.returncode == 0, tree_only.stdout


def test_foreign_author_or_committer_address_is_found(tmp_path):
    repo = make_repo(tmp_path)
    agent = commit(repo, "by the agent", {"a.txt": "a\n"}, mail=AGENT)
    foreign = commit(repo, "by someone else", {"b.txt": "b\n"}, mail=FOREIGN)
    cp = scan(repo)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    found = of_kind(cp, "ADDRESS")
    assert {f["commit"] for f in found} == {foreign[:12]}
    assert all("s***@e***" in f["detail"] for f in found)
    assert len(found) == 1 and "author" in found[0]["detail"] and "committer" in found[0]["detail"]
    assert FOREIGN not in cp.stdout and "example.net" not in cp.stdout
    assert agent[:12] not in cp.stdout and "noreply" not in cp.stdout


def test_home_path_host_and_address_are_found(tmp_path):
    repo = make_repo(tmp_path)
    lines = [HOME_PATH, USERS_PATH, *INTERNAL_HOSTS, "server at " + ADDRESS + " port 80", *HARMLESS]
    sha = commit(repo, "notes", {"docs/notes.md": "\n".join(lines) + "\n"})
    cp = scan(repo)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    tree = [f for f in findings(cp) if f["commit"].strip() == "tree"]
    assert [(f["kind"], f["line"]) for f in sorted(tree, key=lambda f: int(f["line"]))] == [
        ("HOMEPATH", "1"), ("HOMEPATH", "2"), ("HOST", "3"), ("HOST", "4"), ("HOST", "5"), ("IPV4", "6")]
    history = [f for f in findings(cp) if f["commit"].strip() != "tree"]
    assert {f["commit"] for f in history} == {sha[:12]} and len(history) == 6
    assert {f["path"] for f in findings(cp)} == {"docs/notes.md"}
    for private in ("marta", ADDRESS, *INTERNAL_HOSTS):
        assert private not in cp.stdout


def test_file_over_five_megabytes_is_found_in_tree_and_history(tmp_path):
    repo = make_repo(tmp_path)
    big = os.urandom(int(5.1 * 1024 * 1024))
    added = commit(repo, "a film", {"media/film.bin": big, "media/small.bin": os.urandom(4096)})
    commit(repo, "remove the film", {"media/film.bin": None})
    cp = scan(repo)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    found = of_kind(cp, "BIGFILE")
    assert [(f["commit"], f["path"]) for f in found] == [(added[:12], "media/film.bin")]
    assert "5.1 MB" in found[0]["detail"]
    # a big file that is in the working tree and not yet committed is found there
    (repo / "new.bin").write_bytes(big)
    cp = scan(repo, "--tree-only")
    assert [(f["commit"].strip(), f["path"]) for f in of_kind(cp, "BIGFILE")] == [("tree", "new.bin")]


def test_allow_list_entry_with_reason_silences_a_finding_and_without_reason_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "a guide", {"docs/guide.md": "Open " + HOME_PATH + " in your editor.\n"})
    assert of_kind(scan(repo), "HOMEPATH")

    entry = ('[[allowlists]]\n{reason}paths = [\'\'\'^docs/guide\\.md$\'\'\']\nregexes = [\'\'\'THE-FOLDER\'\'\']\n'
             .replace("THE-FOLDER", HOME_PATH[:-1]))
    reason = 'description = "an invented home folder in the guide, a placeholder for the reader\'s own"\n'
    commit(repo, "allow it", {".gitleaks.toml": "[extend]\nuseDefault = true\n\n" + entry.format(reason=reason)})
    cp = scan(repo)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert findings(cp) == []

    # the same entry without its reason is itself a finding, and silences nothing
    commit(repo, "no reason", {".gitleaks.toml": "[extend]\nuseDefault = true\n\n" + entry.format(reason="")})
    cp = scan(repo)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    refused = of_kind(cp, "ALLOWLIST")
    assert len(refused) == 1 and refused[0]["path"] == ".gitleaks.toml" and "description" in refused[0]["detail"]
    assert of_kind(cp, "HOMEPATH")

    # an entry for another path does not silence it either
    other = entry.format(reason=reason).replace("guide", "other")
    commit(repo, "other path", {".gitleaks.toml": other})
    assert of_kind(scan(repo), "HOMEPATH")


def test_findings_print_commit_and_path_and_exit_nonzero(tmp_path):
    repo = make_repo(tmp_path)
    sha = commit(repo, "several", {"a.sh": "curl -H 'Authorization: Bearer " + "Zx9" * 8 + "' https://example.org\n",
                                   "b.txt": HOME_PATH + "\n"}, mail=FOREIGN)
    cp = scan(repo)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    found = findings(cp)
    assert {f["kind"] for f in found} == {"SECRET", "ADDRESS", "HOMEPATH"}
    for f in found:      # KIND  COMMIT(12)  PATH[:LINE]  detail
        assert f["commit"].strip() in ("tree", sha[:12]) and f["path"] and f["detail"]
    assert {(f["path"], f["line"]) for f in found if f["kind"] == "SECRET"} == {("a.sh", "1")}
    # a summary line per kind, and the total
    for kind, count in (("SECRET", 2), ("ADDRESS", 1), ("HOMEPATH", 2)):
        assert re.search(rf"^{kind}: {count}\b", cp.stdout, re.M), cp.stdout
    assert re.search(r"^5 findings", cp.stdout, re.M), cp.stdout

    # bad use: an unknown option, a folder that is not a repository
    assert scan(repo, "--nonsense").returncode == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    cp = scan(empty)
    assert cp.returncode == 2 and "not a git repository" in cp.stderr
