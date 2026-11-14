"""Helpers of the al integration suite: testbed fingerprints, the Layer handle, agentic.rkt edits.

Nothing here knows about the runtime's internals: layers are driven through `tools/al` and the
scripts a generated layer exposes, and observed through the files those leave behind.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
AL = ROOT / "tools" / "al"
AUTHORED = ROOT / "tests" / "authored"
TESTBED_DIR = ROOT / "testbeds"
GOLDEN = HERE / "golden"
TESTBEDS: dict[str, dict[str, str]] = tomllib.loads((HERE / "testbeds.toml").read_text())
BEDS = list(TESTBEDS)
PYTHON_IMAGE = "docker.io/library/python:3.12-slim"

Edit = Callable[[str], str]


def sh(argv: list[str], cwd: Path | None = None, timeout: float = 600, check: bool = False,
       env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run([str(a) for a in argv], cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)
    if check and cp.returncode != 0:
        raise AssertionError(f"{' '.join(map(str, argv))} exited {cp.returncode}\n--- stdout\n{cp.stdout}\n--- stderr\n{cp.stderr}")
    return cp


def git(repo: Path, *args: str, check: bool = True) -> str:
    return sh(["git", "-C", repo, *args], check=check, timeout=120).stdout.rstrip("\n")


# ------------------------------------------------------------------------------ testbed state

def heads(app: Path) -> dict[str, str]:
    """refs/heads of the application: short name -> sha."""
    out = git(app, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


def fingerprint(app: Path) -> dict[str, object]:
    """Everything a test must leave exactly as it found it."""
    return {
        "head": git(app, "rev-parse", "HEAD"),
        "branch": git(app, "branch", "--show-current"),
        "status": git(app, "status", "--porcelain"),
        "heads": sorted(f"{name} {sha}" for name, sha in heads(app).items()),
    }


def worktrees(app: Path) -> list[Path]:
    out = git(app, "worktree", "list", "--porcelain")
    return [Path(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith("worktree ")]


def worktrees_under(app: Path, directory: Path) -> list[Path]:
    directory = directory.resolve()
    return [w for w in worktrees(app) if w.resolve() == directory or directory in w.resolve().parents]


# ------------------------------------------------------------------------------ podman

def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def podman_names(kind: str) -> set[str]:
    """Names of podman containers / volumes / images ({} when podman is absent or broken)."""
    if not have("podman"):
        return set()
    argv = {"containers": ["podman", "ps", "-a", "--format", "{{.Names}}"],
            "volumes": ["podman", "volume", "ls", "--format", "{{.Name}}"],
            "images": ["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"]}[kind]
    cp = sh(argv, timeout=120)
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()} if cp.returncode == 0 else set()


def podman_snapshot() -> dict[str, set[str]]:
    return {kind: podman_names(kind) for kind in ("containers", "volumes", "images")}


def podman_cleanup(before: dict[str, set[str]]) -> dict[str, list[str]]:
    """Remove what appeared since `before` under the suite's names; returns what was removed."""
    removed: dict[str, list[str]] = {"containers": [], "volumes": [], "images": []}
    if not have("podman"):
        return removed
    for name in sorted(podman_names("containers") - before["containers"]):
        if name.startswith("al-it-"):
            sh(["podman", "rm", "-f", name], timeout=120)
            removed["containers"].append(name)
    for name in sorted(podman_names("volumes") - before["volumes"]):
        if name.startswith(("al-it-", "al-cache-")):
            sh(["podman", "volume", "rm", "-f", name], timeout=120)
            removed["volumes"].append(name)
    for name in sorted(podman_names("images") - before["images"]):
        if name.removeprefix("localhost/").startswith("al-it-"):
            sh(["podman", "rmi", "-f", name], timeout=120)
            removed["images"].append(name)
    return removed


def kill_processes_under(directory: Path) -> list[int]:
    """SIGKILL every process whose working directory is inside `directory` (orphans of killed runs)."""
    directory = directory.resolve()
    killed = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd"))
        except OSError:
            continue
        if cwd == directory or directory in cwd.parents:
            try:
                os.kill(int(entry.name), signal.SIGKILL)
                killed.append(int(entry.name))
            except OSError:
                pass
    return killed


# ------------------------------------------------------------------------------ agentic.rkt edits

def _form_span(src: str, head: str, start: int = 0) -> tuple[int, int]:
    """(begin, end) of the first s-expression starting with `head` (e.g. "(adw zte"), strings respected."""
    begin = src.find(head, start)
    if begin < 0:
        raise AssertionError(f"fixture edit: no form starting with {head!r} in the authored agentic.rkt")
    depth, i, in_string = 0, begin, False
    while i < len(src):
        ch = src[i]
        if in_string:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == ";":
            i = src.find("\n", i)
            if i < 0:
                break
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return begin, i + 1
        i += 1
    raise AssertionError(f"fixture edit: unbalanced form {head!r}")


def replace_form(head: str, new: str | Callable[[str], str]) -> Edit:
    def edit(src: str) -> str:
        begin, end = _form_span(src, head)
        return src[:begin] + (new(src[begin:end]) if callable(new) else new) + src[end:]
    return edit


def insert_before(head: str, text: str) -> Edit:
    """Insert a clause (own line, two-space indent) before the first form starting with `head`."""
    def edit(src: str) -> str:
        begin, _ = _form_span(src, head)
        line_start = src.rfind("\n", 0, begin) + 1
        return src[:line_start] + f"  {text}\n" + src[line_start:]
    return edit


def substitute(old: str, new: str, count: int = 0) -> Edit:
    def edit(src: str) -> str:
        if old not in src:
            raise AssertionError(f"fixture edit: {old!r} not found in the authored agentic.rkt")
        return src.replace(old, new, count) if count else src.replace(old, new)
    return edit


def append_clause(text: str) -> Edit:
    """Add a top-level clause at the end of the (agentic-layer …) form."""
    def edit(src: str) -> str:
        begin, end = _form_span(src, "(agentic-layer")
        return src[:end - 1] + f"\n\n  {text})" + src[end:]
    return edit


def add_cmd(name: str, shell: str, extra: str = "") -> Edit:
    quoted = shell.replace("\\", "\\\\").replace('"', '\\"')
    return insert_before("(paths ", f'  (cmd {name} "{quoted}"{" " + extra if extra else ""})')


def mock_agent() -> list[Edit]:
    """Declare the mock agent and make every ADW use it, so runs need neither --dry-run nor an LLM."""
    return [insert_before("(prompt-input", "(agent mock)"), substitute("#:agent claude-code", "#:agent mock")]


def concurrency(**limits: int) -> Edit:
    options = " ".join(f"#:{key.replace('_', '-')} {value}" for key, value in limits.items())
    return insert_before("(prompt-input", f"(concurrency slots {options})")


# ------------------------------------------------------------------------------ the layer handle

@dataclass
class Layer:
    bed: str
    name: str
    dir: Path
    app: Path
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))

    # --- generator
    def al(self, command: str, *args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        return sh([AL, command, self.dir, *args], check=check, timeout=timeout, env=self.env)

    @property
    def ir(self) -> dict:
        return json.loads((self.dir / "agentic-layer.json").read_text())

    # --- runs
    def adw_argv(self, adw: str, prompt: str, *args: str) -> list[str]:
        return ["uv", "run", "--quiet", str(self.dir / "adws" / f"adw_{adw}.py"), prompt, *args]

    def run_adw(self, adw: str, prompt: str, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        return sh(self.adw_argv(adw, prompt, *args), cwd=self.dir, timeout=timeout, env=self.env)

    def popen_adw(self, adw: str, prompt: str, *args: str) -> subprocess.Popen[str]:
        """Start a run in its own session (so the whole group can be signalled); output is captured."""
        return subprocess.Popen(self.adw_argv(adw, prompt, *args), cwd=self.dir, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)

    def cron_once(self, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        return sh(["uv", "run", "--quiet", self.dir / "adws" / "triggers" / "trigger_cron.py", "--once"],
                  cwd=self.dir, timeout=timeout, env=self.env)

    # --- observation
    def states(self) -> dict[str, dict]:
        """adw id -> state.json of every run recorded under the layer."""
        return {p.parent.name: json.loads(p.read_text()) for p in sorted((self.dir / "agents").glob("*/state.json"))}

    def only_state(self) -> dict:
        states = self.states()
        assert len(states) == 1, f"expected exactly one run under {self.dir / 'agents'}, found {sorted(states)}"
        return next(iter(states.values()))

    def trees(self) -> list[str]:
        directory = self.dir / "trees"
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    # --- mock agent scripts
    def mock(self, adw: str, step: str, **script: object) -> Path:
        """Write mock/<adw>/<step>.json ({"output", "files", "layer_files", "delete"})."""
        path = self.dir / "mock" / adw / f"{step}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(script, indent=1) + "\n")
        return path


def describe(cp: subprocess.CompletedProcess[str] | None = None, output: str | None = None) -> str:
    text = output if output is not None else f"exit {cp.returncode}\n--- stdout\n{cp.stdout}\n--- stderr\n{cp.stderr}"
    return text[-6000:]


def normalise(text: str, layer: Layer) -> str:
    """Replace everything that differs between two builds of the same layer by a placeholder."""
    for needle, placeholder in ((str(layer.dir), "<LAYER>"), (str(layer.dir.resolve()), "<LAYER>"),
                                (str(layer.app), "<TESTBED>"), (str(ROOT), "<AL>"), (layer.name, "<NAME>")):
        text = text.replace(needle, placeholder)
    return text


def rendered(layer: Layer) -> dict[str, str]:
    """Relative path -> normalised content of every generated command and HOP."""
    return {f"{kind}/{p.name}": normalise(p.read_text(), layer)
            for kind in ("commands", "hops") for p in sorted((layer.dir / kind).glob("*.md"))}


LEFTOVER = re.compile(r"\{\{[^{}\n]*\}\}")
PYTHON = sys.executable
