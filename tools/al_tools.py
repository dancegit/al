"""Where al finds the programs it runs: racket, raco, uv, git and the agent CLIs (claude, codex).

Every subprocess al starts by one of these names goes through `argv` (or `need` first), so there is
one search order, used the same way everywhere:

1. the setting, an absolute path: AL_RACKET, AL_RACO, AL_UV, AL_GIT, AL_CLAUDE, AL_CODEX, AL_PODMAN, read from
   the process environment, else from (highest precedence first) `<layer>/local.env`,
   `$XDG_CONFIG_HOME/al/<layer name>.env` and `$XDG_CONFIG_HOME/al/tools.env` (default
   `~/.config/al/`). A setting that names something that is not an executable file is reported as
   such; al never falls back to another copy behind it;
2. `PATH`;
3. the usual per-user install folders (USER_FOLDERS): a nix profile, `~/.local/bin`, the Racket
   installer's folder. `raco` is looked for beside the `racket` found first.

The env files are read with kit/runtime/adw_modules/localenv.py (the same format the runtime reads).
`need` only looks at files (no subprocess), so a missing tool is named in well under a second;
`doctor_rows` also asks each found tool for its version. `shell_finder` is the same search as a POSIX
sh function, for `al deploy` on another machine. `agent_isolation` is how every agent al starts runs:
without the account's personal settings, hooks, instructions and plugins (AL_AGENT_PERSONAL=1 keeps
them). Standard library only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
LOCALENV = ROOT / "kit" / "runtime" / "adw_modules" / "localenv.py"


@dataclass(frozen=True)
class Tool:
    name: str
    setting: str        # the variable that names it explicitly
    what: str
    install: str        # how to get it, as a clause: "install Racket (https://racket-lang.org)"


TOOLS: dict[str, Tool] = {
    "racket": Tool("racket", "AL_RACKET", "the Racket compiler", "install Racket (https://racket-lang.org)"),
    "raco": Tool("raco", "AL_RACO", "Racket's raco", "install Racket (https://racket-lang.org), which brings raco"),
    "uv": Tool("uv", "AL_UV", "uv, which runs al and the layer's scripts", "install uv (https://docs.astral.sh/uv/)"),
    "git": Tool("git", "AL_GIT", "git", "install git"),
    "claude": Tool("claude", "AL_CLAUDE", "the Claude Code agent CLI", "install Claude Code (https://claude.com/claude-code)"),
    "codex": Tool("codex", "AL_CODEX", "the Codex agent CLI", "install the Codex CLI (https://github.com/openai/codex)"),
    "podman": Tool("podman", "AL_PODMAN", "podman, which runs a layer's sandboxed environments", "install podman (https://podman.io)"),
}

USER_FOLDERS = ("~/.nix-profile/bin", "~/.local/state/nix/profile/bin", "/etc/profiles/per-user/{user}/bin",
                "/nix/var/nix/profiles/default/bin", "~/.local/bin", "~/racket/bin", "/usr/local/racket/bin",
                "/opt/racket/bin")

# agent kind (--agent, an (agent …) clause) → the program it runs; None: runs nothing
AGENT_BINS: dict[str, str | None] = {"claude-code": "claude", "codex": "codex", "mock": None}

# subcommand → the tools it runs (the agent's program is added by the caller, from --agent or the layer)
NEEDS: dict[str, tuple[str, ...]] = {
    "compile": ("racket",), "gen": ("racket",), "adopt": ("racket",), "explain": ("racket",),
    "author": ("racket",),
    "improve": ("git", "uv", "racket"),
    "init": ("git",), "check": ("git",), "verify": ("git",), "scout": ("git",),
    "create": ("git", "racket"), "adopt-all": ("git", "racket"), "result": (),
    "snapshot": ("git",),
}


@dataclass
class Found:
    name: str
    path: str = ""              # "" when not found
    source: str = ""            # the setting's name, "PATH", a folder (shown with ~), "beside racket"
    looked: list[str] = field(default_factory=list)
    problem: str = ""           # a setting that names no executable file


class ToolMissing(Exception):
    """One or more tools were not found; `.found` holds a Found per missing tool."""

    def __init__(self, found: list[Found], hint: str = "") -> None:
        self.found = list(found)
        self.hint = hint        # a line the caller adds after the sentences (e.g. how to go on)
        super().__init__("; ".join(missing_sentence(f) for f in self.found))

    @property
    def sentences(self) -> list[str]:
        return [missing_sentence(f) for f in self.found]


# ------------------------------------------------------------------------------------------ settings

_localenv: Any = None


def _env_module() -> Any:
    """kit/runtime/adw_modules/localenv.py, loaded by its path (once)."""
    global _localenv
    if _localenv is None:
        spec = importlib.util.spec_from_file_location("al_localenv", LOCALENV)
        if spec is None or spec.loader is None:
            raise ImportError(str(LOCALENV))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _localenv = module
    return _localenv


def short(path: str | Path) -> str:
    """A path as shown: the home directory as ~."""
    text, home = str(path), str(Path.home())
    if home and home != "/" and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def _layer_dir(layer: Path | str | None) -> Path | None:
    if layer is None:
        return None
    layer = Path(layer)
    return layer.parent if layer.suffix == ".rkt" else layer


def layer_name(layer: Path) -> str:
    """The `name` in agentic-layer.json, else the directory name without `.al`."""
    try:
        name = json.loads((layer / "agentic-layer.json").read_text(encoding="utf-8")).get("name")
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError, AttributeError):
        pass
    return layer.name.removesuffix(".al")


def setting_files(layer: Path | str | None = None) -> list[Path]:
    """The files settings are read from, lowest precedence first."""
    env = _env_module()
    files = [p for p in [env.config_file("tools")] if p is not None]
    directory = _layer_dir(layer)
    if directory is not None:
        named = env.config_file(layer_name(directory))
        files += ([named] if named is not None else []) + [directory / env.FILE_NAME]
    return files


def _sourced(layer: Path | str | None) -> dict[str, tuple[str, Path]]:
    values: dict[str, tuple[str, Path]] = {}
    try:
        files = setting_files(layer)
    except (OSError, ImportError, RuntimeError):     # e.g. no home directory: no file settings
        return values
    for path in files:
        try:
            found, _bad = _env_module().parse(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for key, value in found.items():
            if value:
                values[key] = (value, path)
    return values


def settings(layer: Path | str | None = None) -> dict[str, str]:
    """KEY → value from tools.env, then <layer name>.env, then <layer>/local.env (a later file wins)."""
    return {key: value for key, (value, _path) in _sourced(layer).items()}


# ------------------------------------------------------------------------------------------ finding

def _user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return os.environ.get("USER", "")


def user_folders() -> list[tuple[str, Path]]:
    """(as shown, the folder) of each USER_FOLDERS entry: shown with ~ and $USER, never a user's name."""
    user = _user()
    out = []
    for folder in USER_FOLDERS:
        if "{user}" in folder and not user:
            continue
        out.append((folder.replace("{user}", "$USER"), Path(os.path.expanduser(folder.replace("{user}", user)))))
    return out


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find(name: str, layer: Path | str | None = None) -> Found:
    """Where `name` is, by the search order of this module; a Found with path "" when it is nowhere."""
    tool = TOOLS.get(name)
    setting = tool.setting if tool else "AL_" + name.upper().replace("-", "_")
    value, origin = os.environ.get(setting, ""), "in the environment"
    if not value:
        sourced = _sourced(layer).get(setting)
        if sourced:
            value, origin = sourced[0], f"in {short(sourced[1])}"
    if value:
        candidate = Path(os.path.expanduser(value))
        if _executable(candidate):
            return Found(name, str(candidate), setting, [f"{setting} ({origin})"])
        return Found(name, "", "", [f"{setting} ({origin})"],
                     f"{setting} names {value} ({origin}), which is not an executable file")
    looked = [f"{setting} (not set)"]
    if name == "raco":
        racket = find("racket", layer)
        if racket.path:
            beside = Path(racket.path).parent / "raco"
            looked.append(f"beside racket ({short(beside.parent)})")
            if _executable(beside):
                return Found(name, str(beside), "beside racket", looked)
    looked.append("PATH")
    on_path = shutil.which(name, path=os.environ.get("PATH", ""))
    if on_path:
        return Found(name, on_path, "PATH", looked)
    for shown, folder in user_folders():
        looked.append(shown)
        if _executable(folder / name):
            return Found(name, str(folder / name), shown, looked)
    return Found(name, "", "", looked)


def need(names: Iterable[str], layer: Path | str | None = None) -> dict[str, str]:
    """name → path of every tool in `names`; raises ToolMissing naming every one that is missing.
    Looks at files only (no subprocess)."""
    found = [find(n, layer) for n in dict.fromkeys(n for n in names if n)]
    missing = [f for f in found if not f.path]
    if missing:
        raise ToolMissing(missing)
    return {f.name: f.path for f in found}


def agent_bin(agent: str | None) -> tuple[str, ...]:
    """The program an agent kind runs, as a tuple to add to a NEEDS entry (empty for mock or unknown)."""
    program = AGENT_BINS.get(agent or "")
    return (program,) if program else ()


def missing_sentence(found: Found) -> str:
    """One sentence: what is missing, where al looked, and how to set it."""
    tool = TOOLS.get(found.name)
    setting = tool.setting if tool else "AL_" + found.name.upper()
    tools_env = short(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "al" / "tools.env")
    if found.problem:
        return (f"{found.name} is missing: {found.problem}; point {setting} at the {found.name} program "
                f"or remove the setting.")
    install = tool.install if tool else f"install {found.name}"
    return (f"{found.name} is missing: al looked at {', '.join(found.looked)}; {install} or set "
            f"{setting}=/path/to/{found.name} in the layer's local.env or {tools_env}.")


_ARGV_CACHE: dict[tuple, str] = {}


def argv(cmd: list[Any], layer: Path | str | None = None) -> list[Any]:
    """`cmd` with a bare tool name in front (racket, raco, uv, git, claude, codex) replaced by the
    tool's path; raises ToolMissing when it is not found. Any other argv is returned unchanged."""
    cmd = list(cmd)
    if not cmd or not isinstance(cmd[0], str) or cmd[0] not in TOOLS:
        return cmd
    name = cmd[0]
    key = (name, str(layer), os.environ.get("PATH", ""), os.environ.get(TOOLS[name].setting, ""),
           os.environ.get("HOME", ""), os.environ.get("XDG_CONFIG_HOME", ""))
    path = _ARGV_CACHE.get(key)
    if path is None or not _executable(Path(path)):
        found = find(name, layer)
        if not found.path:
            raise ToolMissing([found])
        path = _ARGV_CACHE[key] = found.path
    return [path, *cmd[1:]]


# ------------------------------------------------------------------------------------------ agents

PERSONAL = "AL_AGENT_PERSONAL"     # "1": agents al starts load the account's own settings, hooks and plugins
CREDENTIALS = ".credentials.json"


def personal_agent(layer: Path | str | None = None) -> bool:
    """AL_AGENT_PERSONAL=1 in the environment, LAYER/local.env, ~/.config/al/<layer>.env or tools.env."""
    value = os.environ.get(PERSONAL) or settings(layer).get(PERSONAL, "")
    return value.strip() == "1"


@dataclass
class Isolation:
    """What an agent CLI al starts gets: extra argv, extra environment, and what to remove afterwards."""
    argv_extra: list[str]
    env: dict[str, str]
    folder: Path | None = None

    def cleanup(self) -> None:
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)
            self.folder = None


def write_rules(writable: Path) -> list[str]:
    """Permission rules that let the agent edit and write below `writable` only (absolute path rules)."""
    base = str(Path(writable).resolve())
    return [f"Edit(/{base}/**)", f"Write(/{base}/**)"]


def agent_isolation(layer: Path, writable: Path, allowed: list[str]) -> Isolation:
    """The isolation of an agent al starts (scout, author): a clean CLAUDE_CONFIG_DIR that holds only a
    link to the account's credentials file, `--setting-sources project` (no user settings, hooks or
    plugins), `--strict-mcp-config` (no MCP servers), and tool rules that allow `allowed` plus Edit and
    Write below `writable` only. AL_AGENT_PERSONAL=1 keeps the personal setup; the write rules stay."""
    rules = ["--allowedTools", ",".join([*allowed, *write_rules(writable)])]
    if personal_agent(layer):
        return Isolation(rules, {})
    folder = Path(tempfile.mkdtemp(prefix="al-agent-"))
    credentials = Path.home() / ".claude" / CREDENTIALS
    if credentials.is_file():
        (folder / CREDENTIALS).symlink_to(credentials)
    return Isolation(["--setting-sources", "project", "--strict-mcp-config", *rules],
                     {"CLAUDE_CONFIG_DIR": str(folder)}, folder)


# ------------------------------------------------------------------------------------------ PATH

def search_path_from(folders: Iterable[str], base: str) -> str:
    """`base` with each of `folders` that it does not hold already put in front, each once."""
    parts = [p for p in (base or "").split(os.pathsep) if p]
    front = [f for f in dict.fromkeys(str(f) for f in folders if f) if f not in parts]
    return os.pathsep.join(front + parts)


def search_path(names: Iterable[str], base: str, layer: Path | str | None = None) -> str:
    """A PATH under which every found tool of `names` is found by its bare name: their folders before `base`."""
    folders = []
    for name in dict.fromkeys(names):
        found = find(name, layer)
        if found.path:
            folders.append(os.path.dirname(found.path))
    return search_path_from(folders, base)


# ------------------------------------------------------------------------------------------ al doctor

def version_of(path: str, timeout: float = 3) -> str:
    """The first line `PATH --version` prints, or "" (no answer within `timeout` seconds; raco has no
    --version: it comes with racket, whose version is the one that counts)."""
    if Path(path).name == "raco":
        return ""
    try:
        cp = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (cp.stdout + "\n" + cp.stderr).splitlines():
        if line.strip():
            return line.strip()
    return ""


def doctor_rows(names: Iterable[str], layer: Path | str | None = None, versions: bool = True) -> list[dict[str, Any]]:
    """One row per tool: {tool, found, path, source, version, looked, sentence}."""
    rows = []
    for name in dict.fromkeys(names):
        found = find(name, layer)
        rows.append({"tool": name, "found": bool(found.path), "path": short(found.path) if found.path else "",
                     "source": found.source, "version": version_of(found.path) if found.path and versions else "",
                     "looked": found.looked, "sentence": "" if found.path else missing_sentence(found)})
    return rows


def doctor_table(rows: list[dict[str, Any]]) -> str:
    """The rows as a text table (tool, found where, version); each missing tool's sentence below it."""
    cells = [["tool", "found where", "version"]]
    for row in rows:
        where = f"{row['path']} ({row['source']})" if row["found"] else "MISSING"
        cells.append([row["tool"], where, row["version"]])
    widths = [max(len(r[i]) for r in cells) for i in range(3)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    lines += [f"  {row['sentence']}" for row in rows if not row["found"]]
    return "\n".join(lines)


def gone_dashboards(proc: Path = Path("/proc")) -> list[dict[str, Any]]:
    """Running dashboards whose Python no longer exists (a pruned or rebuilt uv cache): one
    {pid, python, sentence} per process whose argv runs `…/dashboard/dashboard.py` and whose argv0 is
    an absolute path that has gone, or whose `exe` link reads "… (deleted)". [] without a /proc."""
    found: list[dict[str, Any]] = []
    try:
        entries = sorted((e for e in proc.iterdir() if e.name.isdigit()), key=lambda e: int(e.name))
    except OSError:
        return found
    for entry in entries:
        try:
            argv = [part for part in (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if part]
            if not argv or not any(part.endswith("/dashboard/dashboard.py") for part in argv):
                continue
            python = ""
            if os.path.isabs(argv[0]) and not os.path.exists(argv[0]):
                python = argv[0]
            else:
                exe = os.readlink(entry / "exe")
                if exe.endswith(" (deleted)"):
                    python = exe[:-len(" (deleted)")]
        except OSError:
            continue
        if python:
            pid = int(entry.name)
            found.append({"pid": pid, "python": python,
                          "sentence": f"dashboard (pid {pid}): its Python {python} no longer exists; "
                                      "restart the dashboard"})
    return found


# ------------------------------------------------------------------------------------------ al deploy

def shell_finder() -> str:
    """`al_find NAME SETTING`: the search of `find` as a POSIX sh function (al deploy runs it on the
    host). It prints the path and succeeds, or prints nothing and fails. The layer's own env file is
    read when `al_layer` holds the layer name. No eval; only env, sed, id and test besides builtins."""
    folders = " ".join('"$HOME' + f[1:] + '"' if f.startswith("~") else
                       '"' + f.replace("{user}", "$(id -un)") + '"' for f in USER_FOLDERS)
    return "\n".join([
        "al_find() {",
        '  _v=$(env | sed -n "s/^$2=//p" | sed -n \'$p\')',
        '  _c="${XDG_CONFIG_HOME:-$HOME/.config}/al"',
        '  if test -z "$_v"; then',
        '    for _f in "$_c/tools.env" "$_c/${al_layer:-.none}.env"; do',
        '      if test -f "$_f"; then',
        '        _w=$(sed -n "s/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}$2[[:space:]]*=[[:space:]]*//p" "$_f" '
        "| sed -n '$p' | sed -e 's/[[:space:]]*$//' -e 's/^\"\\(.*\\)\"$/\\1/' -e \"s/^'\\(.*\\)'$/\\1/\")",
        '        if test -n "$_w"; then _v=$_w; fi',
        "      fi",
        "    done",
        "  fi",
        '  if test -n "$_v"; then',
        '    if test -f "$_v" && test -x "$_v"; then echo "$_v"; return 0; fi',
        "    return 1",
        "  fi",
        '  _p=$(command -v "$1" 2>/dev/null)',
        '  if test -n "$_p" && test "${_p#/}" != "$_p"; then echo "$_p"; return 0; fi',
        f"  for _d in {folders}; do",
        '    if test -f "$_d/$1" && test -x "$_d/$1"; then echo "$_d/$1"; return 0; fi',
        "  done",
        "  return 1",
        "}",
    ])
