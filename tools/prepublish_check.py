# /// script
# requires-python = ">=3.11"
# ///
"""The scan a person runs before making a repository public. It reads the tracked tree and the whole
history (every ref) and changes nothing. Standard library only.

    tools/prepublish_check.sh [--repo DIR] [--no-external] [--tree-only]

Four passes:

1. SECRET — private-key headers, cloud and forge tokens, API keys, an `Authorization` header with a
   literal value, and `password|secret|token` assigned a quoted literal of 8 or more characters
   (a literal written as an environment variable's name, capitals, digits and `_` only, is a name
   and not a value, and passes); in the tree and in every line a commit added. When `gitleaks` is on PATH (and `--no-external` is not
   given) it is run over the history and over the tree as well, and its findings are merged in.
2. ADDRESS — the author and committer address of every commit. Allowed: an address at
   `users.noreply.github.com` and `adw@localhost` (the agent's local one). Printed masked.
3. HOMEPATH, HOST, IPV4 — a home folder (a name under `/home` or `/Users`), a host name ending in
   `.internal`, `.lan` or `.corp`, and an IPv4 address that is not loopback, unspecified, broadcast
   or a documentation range; in the tree and in every added line of the history.
4. BIGFILE — every blob over 5 MB in the history, with the commit that introduced it, and every
   file over 5 MB in the working tree that is not such a blob.

One line per finding, values redacted (never a full secret, address, name or host):

    KIND  COMMIT(12)  PATH[:LINE]  detail

`COMMIT` is the first 12 digits of the commit, or the word `tree` for a finding in the working tree
(tracked files and files about to be added). A history finding is reported once per kind, path and
value, with the oldest commit that added it. Then one summary line per kind and the total.

The allow-list is `.gitleaks.toml` of the scanned repository (its working-tree copy): each
`[[allowlists]]` table (or the single `[allowlist]` table) has a `description` (the reason), and
`paths` and/or `regexes` (lists of regular expressions). An entry silences a finding when the
finding's path matches one of its `paths` and the finding's line (or, for ADDRESS, the address)
matches one of its `regexes`; when an entry gives only one of the two, that one decides; when it
gives both, both must match. An entry with only `regexes` also applies to ADDRESS findings; BIGFILE
findings are silenced by `paths`-only entries. An entry without a description, or with neither
`paths` nor `regexes`, is itself a finding (ALLOWLIST) and silences nothing. The content of
`.gitleaks.toml` is not scanned by passes 1 and 3: it has to name the patterns it allows.

Exit status: 0 no finding, 1 at least one finding, 2 bad use (unknown option, not a repository,
an allow-list that cannot be read).

Not covered: content that exists only in a merge commit's own resolution (the diff of a merge is not
read), and e-mail addresses inside file content (only commit addresses are checked).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

BIG = 5 * 1024 * 1024
TREE = "tree"
ALLOW_FILE = ".gitleaks.toml"
KINDS = ("SECRET", "ADDRESS", "HOMEPATH", "HOST", "IPV4", "BIGFILE", "ALLOWLIST")

ALLOWED_MAIL = re.compile(r"^(?:[^@\s]+@users\.noreply\.github\.com|adw@localhost)$", re.I)

# name, pattern, group holding the value
SECRETS: list[tuple[str, re.Pattern, int]] = [
    ("private key header", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), 0),
    ("AWS access key id", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"), 0),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{30,}"), 0),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{22,}"), 0),
    ("API key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"), 0),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9_])xox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    ("Authorization header with a literal",
     re.compile(r"Authorization[\"']?\s*[:=]\s*[\"']?\s*(?:Bearer|token)\s+([A-Za-z0-9._~+/=-]{12,})", re.I), 1),
    ("password, secret or token assigned a literal",
     re.compile(r"(?:password|passwd|secret|token)[A-Za-z0-9_]*[\"']?\s*[:=]\s*([\"'])([^\"'\s$<>{}]{8,})\1", re.I), 2),
]
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")       # the name of an environment variable, not a value
QUICK_SECRET =re.compile(r"PRIVATE KEY|AKIA|gh[pousr]_|github_pat_|sk-|xox[baprs]-|authorization|passw|secret|token", re.I)

HOMEPATH = re.compile(r"(?<![\w.~$}])/(?:home|Users)/([A-Za-z0-9][A-Za-z0-9._-]*)(?![A-Za-z0-9._-])")
HOST = re.compile(r"(?<![\w.@/-])((?:[A-Za-z0-9][A-Za-z0-9-]*\.)+(?:internal|lan|corp))(?![\w(-]|\.[A-Za-z0-9])")
ALLOWED_HOSTS = {"host.containers.internal", "host.docker.internal"}
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.]*\d)")
QUICK_PRIVATE = re.compile(r"/home/|/Users/|\.internal|\.lan|\.corp|\d\.\d+\.\d+\.\d", re.I)
DOCUMENTATION = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


class Finding:
    __slots__ = ("kind", "commit", "path", "line", "detail", "text", "value")

    def __init__(self, kind, commit, path, line, detail, text="", value=""):
        self.kind, self.commit, self.path, self.line = kind, commit, path, line
        self.detail, self.text, self.value = detail, text, value

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.kind}  {self.commit[:12]:<12}  {where}  {self.detail}"


# ── redaction ────────────────────────────────────────────────────────────────────────────────────

def redact(value: str) -> str:
    return f"{value[:4]}… (redacted, {len(value)} characters)"


def mask_mail(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain[:1]}***"


def mask_name(name: str) -> str:
    return f"{name[:1]}***"


# ── what one line holds ──────────────────────────────────────────────────────────────────────────

def harmless_ipv4(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True          # 999.1.1.1, a version number with a leading zero: not an address
    return (ip.is_loopback or ip.is_unspecified or ip.is_multicast or text.startswith("255.")
            or any(ip in net for net in DOCUMENTATION))


def line_findings(line: str):
    """(kind, detail, value) for everything passes 1 and 3 see in one line of text."""
    if QUICK_SECRET.search(line):
        for name, pattern, group in SECRETS:
            for m in pattern.finditer(line):
                value = m.group(group)
                if name.startswith("password") and ENV_NAME.match(value):
                    continue
                yield "SECRET", f"{name}: {redact(value)}", value
    if QUICK_PRIVATE.search(line):
        for m in HOMEPATH.finditer(line):
            folder = m.group(0).split("/")[1]
            yield "HOMEPATH", f"home folder /{folder}/{mask_name(m.group(1))}/", m.group(0)
        for m in HOST.finditer(line):
            host = m.group(1).lower()
            if host not in ALLOWED_HOSTS:
                yield "HOST", f"internal host name {mask_name(host)}.{host.rsplit('.', 1)[1]}", host
        for m in IPV4.finditer(line):
            if not harmless_ipv4(m.group(1)):
                yield "IPV4", f"IPv4 address {m.group(1).split('.')[0]}.*.*.*", m.group(1)


# ── git ──────────────────────────────────────────────────────────────────────────────────────────

def git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo, capture_output=True)
    if cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {cp.stderr.decode(errors='replace').strip()}")
    return cp.stdout.decode(errors="replace")


def tree_files(repo: Path) -> list[str]:
    """Tracked files and files about to be added (untracked, not ignored)."""
    out = git(repo, "ls-files", "-z", "-co", "--exclude-standard")
    return sorted({p for p in out.split("\0") if p})


def scan_tree(repo: Path) -> list[Finding]:
    found = []
    for rel in tree_files(repo):
        path = repo / rel
        if rel == ALLOW_FILE or path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        for number, raw in enumerate(data.split(b"\n"), 1):
            line = raw.decode(errors="replace")
            for kind, detail, value in line_findings(line):
                found.append(Finding(kind, TREE, rel, number, detail, line, value))
    return found


def scan_history(repo: Path) -> list[Finding]:
    """Every added line of every commit of every ref, oldest first; one finding per kind, path and value."""
    found: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    proc = subprocess.Popen(
        ["git", "-c", "core.quotePath=false", "log", "--all", "--reverse", "-p", "-U0", "--no-color", "--no-ext-diff",
         "--no-textconv", "--format=%x01%H"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    commit, path, number = "", "", 0
    assert proc.stdout is not None
    for raw in proc.stdout:
        first = raw[:1]
        if first == b"+":
            if raw.startswith(b"+++ "):
                name = raw[4:].rstrip(b"\r\n").decode(errors="replace").strip('"')
                path = name[2:] if name.startswith("b/") else ""
                continue
            if path and path != ALLOW_FILE:
                line = raw[1:].rstrip(b"\n").decode(errors="replace")
                for kind, detail, value in line_findings(line):
                    key = (kind, path, value)
                    if key not in seen:
                        seen.add(key)
                        found.append(Finding(kind, commit, path, number, detail, line, value))
            number += 1
        elif first == b"@":
            m = re.match(rb"@@ -\S+ \+(\d+)", raw)
            if m:
                number = int(m.group(1))
        elif first == b"\x01":
            commit, path = raw[1:].strip().decode(), ""
        elif raw.startswith(b"diff --git "):
            path = ""
    proc.wait()
    return found


def scan_addresses(repo: Path) -> list[Finding]:
    found = []
    for row in git(repo, "log", "--all", "--format=%H%x09%ae%x09%ce").splitlines():
        commit, _, rest = row.partition("\t")
        author, _, committer = rest.partition("\t")
        bad = [(role, a) for role, a in (("author", author), ("committer", committer)) if not ALLOWED_MAIL.match(a)]
        if not bad:
            continue
        if len(bad) == 2 and author == committer:
            detail = f"author and committer address {mask_mail(author)}"
        else:
            detail = ", ".join(f"{role} address {mask_mail(a)}" for role, a in bad)
        found.append(Finding("ADDRESS", commit, "-", 0, detail, " ".join(a for _, a in bad), bad[0][1]))
    return found


def megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def scan_big(repo: Path, history: bool) -> list[Finding]:
    found = []
    blobs: set[str] = set()
    if history:
        objects = subprocess.run(["git", "-c", "core.quotePath=false", "rev-list", "--objects", "--all"],
                                 cwd=repo, capture_output=True)
        if objects.returncode != 0:
            raise RuntimeError(objects.stderr.decode(errors="replace").strip())
        check = subprocess.run(["git", "cat-file", "--batch-check=%(objecttype) %(objectname) %(objectsize) %(rest)"],
                               cwd=repo, input=objects.stdout, capture_output=True)
        for row in check.stdout.decode(errors="replace").splitlines():
            parts = row.split(" ", 3)
            if len(parts) < 4 or parts[0] != "blob" or int(parts[2]) <= BIG:
                continue
            oid, size, path = parts[1], int(parts[2]), parts[3]
            blobs.add(oid)
            commits = git(repo, "log", "--all", f"--find-object={oid}", "--format=%H", "--", path).split()
            if not commits:      # the plan's way: the commit that added the path
                commits = git(repo, "log", "--all", "--diff-filter=A", "--format=%H", "-1", "--", path).split()
            found.append(Finding("BIGFILE", commits[-1] if commits else "unknown", path, 0,
                                 f"{megabytes(size)}, blob {oid[:12]}"))
    for rel in tree_files(repo):
        path = repo / rel
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= BIG:
            continue
        if history and git(repo, "hash-object", "--", rel).strip() in blobs:
            continue
        found.append(Finding("BIGFILE", TREE, rel, 0, megabytes(path.stat().st_size)))
    return found


# ── gitleaks, when a person has it ───────────────────────────────────────────────────────────────

def scan_gitleaks(repo: Path, history: bool) -> list[Finding]:
    exe = shutil.which("gitleaks")
    if not exe:
        return []
    found = []
    modes = [("tree", ["--no-git"])] + ([("history", [])] if history else [])
    for label, extra in modes:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            cp = subprocess.run([exe, "detect", "--source", str(repo), "--redact", "--no-banner", "--report-format",
                                 "json", "--report-path", str(report), "--exit-code", "0", *extra],
                                cwd=repo, capture_output=True, text=True)
            if cp.returncode != 0 or not report.exists():
                print(f"prepublish: gitleaks ({label}) did not finish: {cp.stderr.strip()[-300:]}", file=sys.stderr)
                continue
            try:
                rows = json.loads(report.read_text() or "[]")
            except ValueError:
                rows = []
        for row in rows or []:
            rel = str(row.get("File", ""))
            if os.path.isabs(rel):
                rel = os.path.relpath(rel, repo)
            commit = row.get("Commit") or TREE
            found.append(Finding("SECRET", commit, rel, int(row.get("StartLine") or 0),
                                 f"gitleaks {row.get('RuleID', 'rule')}: redacted", "", f"gitleaks:{row.get('RuleID')}"))
    return found


# ── the allow-list ───────────────────────────────────────────────────────────────────────────────

class Entry:
    def __init__(self, paths, regexes):
        self.paths, self.regexes = paths, regexes

    def allows(self, f: Finding) -> bool:
        by_text = any(r.search(f.text) or r.search(f.value) for r in self.regexes)
        if f.kind == "ADDRESS":
            return bool(self.regexes) and not self.paths and by_text
        by_path = any(p.search(f.path) for p in self.paths)
        if f.kind == "BIGFILE":
            return bool(self.paths) and not self.regexes and by_path
        if self.paths and self.regexes:
            return by_path and by_text
        return by_path if self.paths else by_text


def load_allow_list(repo: Path) -> tuple[list[Entry], list[Finding]]:
    """The usable entries, and a finding for each entry that is refused."""
    path = repo / ALLOW_FILE
    if not path.is_file():
        return [], []
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{ALLOW_FILE} cannot be read: {e}") from e
    tables = list(data.get("allowlists") or [])
    if isinstance(data.get("allowlist"), dict):
        tables.append(data["allowlist"])
    entries, refused = [], []
    for index, table in enumerate(tables, 1):
        if not isinstance(table, dict):
            continue
        reason = str(table.get("description") or "").strip()
        paths, regexes = table.get("paths") or [], table.get("regexes") or []
        problem = ""
        if not reason:
            problem = "has no description (the reason it is allowed)"
        elif not paths and not regexes:
            problem = "has neither paths nor regexes"
        else:
            try:
                entries.append(Entry([re.compile(p) for p in paths], [re.compile(r) for r in regexes]))
            except (re.error, TypeError) as e:
                problem = f"has a pattern that does not compile ({e})"
        if problem:
            refused.append(Finding("ALLOWLIST", TREE, ALLOW_FILE, 0, f"allow-list entry {index} {problem}; it silences nothing"))
    return entries, refused


# ── main ─────────────────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prepublish_check.sh",
        description="Scan the tree and the whole history before publishing: secrets, commit addresses, home paths, "
                    "internal hosts, addresses, files over 5 MB. Changes nothing. Exit 0: no finding; 1: findings; "
                    "2: bad use.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="the repository to scan (default: the one this script is in)")
    parser.add_argument("--no-external", action="store_true", help="do not run gitleaks even when it is installed")
    parser.add_argument("--tree-only", action="store_true", help="scan the working tree only, not the history")
    a = parser.parse_args(argv)          # argparse exits 2 on bad use

    repo = a.repo.resolve()
    inside = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo if repo.is_dir() else None,
                            capture_output=True, text=True) if repo.is_dir() else None
    if inside is None or inside.returncode != 0:
        print(f"prepublish: {a.repo} is not a git repository", file=sys.stderr)
        return 2
    repo = Path(inside.stdout.strip())
    history = not a.tree_only
    try:
        entries, found = load_allow_list(repo)
        found += scan_tree(repo)
        if history:
            found += scan_history(repo)
            found += scan_addresses(repo)
        found += scan_big(repo, history)
        external = "not run (--no-external)"
        if not a.no_external:
            external = "run" if shutil.which("gitleaks") else "not installed; built-in patterns only"
            found += scan_gitleaks(repo, history)
    except (ValueError, RuntimeError) as e:
        print(f"prepublish: {e}", file=sys.stderr)
        return 2

    kept = [f for f in found if f.kind == "ALLOWLIST" or not any(e.allows(f) for e in entries)]
    allowed = len(found) - len(kept)
    kept.sort(key=lambda f: (KINDS.index(f.kind), f.commit != TREE, f.path, f.line))
    for f in kept:
        print(f.render())
    if kept:
        print()
    scope = "the working tree" if a.tree_only else "the working tree and the history of every ref"
    print(f"scanned {scope}; gitleaks: {external}; allowed by {ALLOW_FILE}: {allowed}")
    for kind in KINDS:
        count = sum(1 for f in kept if f.kind == kind)
        if count:
            print(f"{kind}: {count}")
    if not kept:
        print("no finding")
        return 0
    print(f"{len(kept)} finding{'s' if len(kept) != 1 else ''}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(1)
