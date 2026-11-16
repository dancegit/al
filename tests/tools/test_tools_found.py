"""tools/al finds the programs it runs through tools/al_tools.py: a profile folder when PATH lacks
Racket, a prerequisite check before any work (exit 4, one sentence per missing tool), `al doctor`,
and no subcommand that runs a tool by its bare name. tools/al runs as a subprocess here."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import AL, LAYER_TEMPLATE, ROOT, al

TOOL_SETTINGS = ("AL_RACKET", "AL_RACO", "AL_UV", "AL_GIT", "AL_CLAUDE", "AL_CODEX")


def uv_dirs() -> dict[str, str]:
    """uv's own cache and Python folders, so that a run under another HOME downloads nothing."""
    out = {}
    for variable, args in (("UV_CACHE_DIR", ["cache", "dir"]), ("UV_PYTHON_INSTALL_DIR", ["python", "dir"])):
        cp = subprocess.run(["uv", *args], capture_output=True, text=True, timeout=60)
        if cp.returncode == 0 and cp.stdout.strip():
            out[variable] = cp.stdout.strip()
    return out


def clean_env(**changes: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in TOOL_SETTINGS and k != "AL_APP_ROOT"}
    env.update(changes)
    return env


def run_al(*args, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(AL), *map(str, args)], env=env, cwd=cwd, capture_output=True, text=True, timeout=600)


def stub(folder: Path, name: str, text: str = "stub 1.0") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"#!/bin/sh\necho '{text}'\n")
    path.chmod(0o755)
    return path


def layer_dir(tmp_path: Path) -> Path:
    layer = tmp_path / "notes.al"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)"))
    return layer


def test_compile_finds_racket_in_a_profile_folder_when_path_lacks_it(tmp_path):
    racket, raco = shutil.which("racket"), shutil.which("raco")
    if racket is None:
        pytest.skip("no racket on this machine")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("uv", "git", "python3", "env"):
        real = shutil.which(name)
        assert real, name
        (bin_dir / name).symlink_to(os.path.realpath(real) if name == "python3" else real)
    home = tmp_path / "home"
    profile = home / ".nix-profile" / "bin"
    profile.mkdir(parents=True)
    (profile / "racket").symlink_to(racket)
    if raco:
        (profile / "raco").symlink_to(raco)
    layer = layer_dir(tmp_path)
    env = clean_env(PATH=str(bin_dir), HOME=str(home), **uv_dirs())
    env.pop("XDG_CONFIG_HOME", None)
    assert shutil.which("racket", path=env["PATH"]) is None
    cp = run_al("compile", layer, env=env)
    assert cp.returncode == 0, cp.stderr
    assert json.loads(cp.stdout)["name"] == "notes"
    doctor = run_al("doctor", "--for", "compile", env=env)
    assert doctor.returncode == 0 and doctor.stdout.strip() == "ok", doctor.stdout + doctor.stderr


def test_author_with_no_racket_stops_before_the_mock_agent_and_names_it(app, tmp_path):
    al("init", app)
    layer = app.with_name("notes.al")
    (layer / "local.env").write_text(f"AL_RACKET={tmp_path / 'no-racket-here' / 'racket'}\n")
    mock = tmp_path / "author-mock.json"
    mock.write_text(json.dumps({"files": {"agentic.rkt": "#lang al\n"}, "output": "the mock agent ran"}))
    cp = run_al("author", layer, "--agent", "mock", "--mock", mock, env=clean_env())
    assert cp.returncode == 4, cp.stdout + cp.stderr
    lines = cp.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("al: author: racket is missing: "), cp.stderr
    assert "AL_RACKET" in lines[0] and "local.env" in lines[0] and "Errno" not in lines[0]
    assert "the mock agent ran" not in cp.stdout and not (layer / "agentic.rkt").exists()


def test_doctor_prints_the_table_and_json(tmp_path):
    if shutil.which("racket") is None:
        pytest.skip("no racket on this machine")
    layer = layer_dir(tmp_path)
    claude = stub(tmp_path / "agent-bin", "claude", "9.9.9 (Claude Code)")
    env = clean_env(AL_CLAUDE=str(claude))
    cp = run_al("doctor", layer, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    lines = cp.stdout.splitlines()
    assert re.match(r"tool\s+found where\s+version$", lines[0]), cp.stdout
    rows = {line.split()[0]: line for line in lines[1:]}
    assert {"racket", "raco", "uv", "git", "claude"} <= set(rows)
    assert "(AL_CLAUDE)" in rows["claude"] and "9.9.9 (Claude Code)" in rows["claude"]

    cp = run_al("doctor", layer, "--json", env=env)
    document = json.loads(cp.stdout)
    assert document["ok"] is True and cp.returncode == 0
    assert {"racket", "raco", "uv", "git"} <= {t["tool"] for t in document["tools"]}
    assert all({"tool", "found", "path", "source", "version", "looked", "sentence"} <= set(t) for t in document["tools"])

    ok = run_al("doctor", "--for", "author", "--agent", "mock", env=clean_env())
    assert ok.returncode == 0 and ok.stdout.strip() == "ok", ok.stdout + ok.stderr
    missing = run_al("doctor", "--for", "author", "--agent", "mock", env=clean_env(AL_RACKET="/nowhere"))
    assert missing.returncode == 4
    assert missing.stdout.strip().startswith("racket is missing: AL_RACKET names /nowhere"), missing.stdout
    absent = run_al("doctor", layer, "--json", env=clean_env(AL_CLAUDE=str(claude), AL_UV="/nowhere/uv"))
    assert absent.returncode == 4
    uv_row = next(t for t in json.loads(absent.stdout)["tools"] if t["tool"] == "uv")
    assert uv_row["found"] is False and "AL_UV" in uv_row["sentence"]


TOOL_NAMES = ("racket", "raco", "uv", "git", "claude", "codex")
BARE = re.compile(r'\[\s*"(' + "|".join(TOOL_NAMES) + r')"\s*,')
FUNNEL = re.compile(r'(?:(?<![\w.])(?:run|sh)\(|\bargv\(|Action\("local",\s*)\[\s*"(' + "|".join(TOOL_NAMES) + r')"\s*,')


def test_no_tool_is_called_by_a_bare_name():
    """Every argv that starts with a tool's bare name goes through a funnel that resolves it: run()
    in tools/al, sh() in al_improve, al_tools.argv(), or a local deploy action (resolved where it runs)."""
    files = [ROOT / "tools" / "al", *sorted(p for p in (ROOT / "tools").glob("al_*.py") if p.name != "al_tools.py")]
    offenders = []
    for path in files:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            bare = len(BARE.findall(line))
            funnelled = len(FUNNEL.findall(line))
            if bare > funnelled:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
    assert BARE.search('subprocess.run(["git", "-C", x])') and not FUNNEL.search('subprocess.run(["git", "-C", x])')
    assert FUNNEL.search('subprocess.run(al_tools.argv(["git", "-C", x]))') and not FUNNEL.search('claude_argv(["claude", "-p"])')
