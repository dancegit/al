"""tools/al_tools.py: where al finds racket, raco, uv, git and the agent CLIs. In-process, with HOME,
PATH and XDG_CONFIG_HOME pointed at temporary folders; every "tool" is a tiny executable script."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import al_tools  # noqa: E402

PER_USER = tuple(f for f in al_tools.USER_FOLDERS if f.startswith("~"))


def tool(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"#!/bin/sh\necho '{name} 1.0 from {folder.name}'\n")
    path.chmod(0o755)
    return path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home; PATH is an empty folder; no setting in the environment; only the per-user folders
    under the home directory are searched (a machine's /nix or /opt cannot change the result)."""
    home = tmp_path / "home"
    home.mkdir()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for t in al_tools.TOOLS.values():
        monkeypatch.delenv(t.setting, raising=False)
    monkeypatch.setattr(al_tools, "USER_FOLDERS", PER_USER)
    return home


def test_search_order_setting_then_path_then_user_folders(home, tmp_path, monkeypatch):
    layer = tmp_path / "notes.al"
    layer.mkdir()
    config = home / ".config" / "al"
    config.mkdir(parents=True)
    candidates = {
        "environment": tool(tmp_path / "from-env", "racket"),
        "local.env": tool(tmp_path / "from-local", "racket"),
        "notes.env": tool(tmp_path / "from-named", "racket"),
        "tools.env": tool(tmp_path / "from-tools", "racket"),
        "PATH": tool(tmp_path / "on-path", "racket"),
        "nix": tool(home / ".nix-profile" / "bin", "racket"),
        "racket installer": tool(home / "racket" / "bin", "racket"),
    }
    monkeypatch.setenv("AL_RACKET", str(candidates["environment"]))
    (layer / "local.env").write_text(f"AL_RACKET={candidates['local.env']}\n")
    (config / "notes.env").write_text(f"export AL_RACKET=\"{candidates['notes.env']}\"\n")
    (config / "tools.env").write_text(f"AL_RACKET={candidates['tools.env']}\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'on-path'}{os.pathsep}{os.environ['PATH']}")

    def found() -> al_tools.Found:
        return al_tools.find("racket", layer)

    assert found().path == str(candidates["environment"]) and found().source == "AL_RACKET"
    monkeypatch.delenv("AL_RACKET")
    assert found().path == str(candidates["local.env"]) and "local.env" in found().looked[0]
    (layer / "local.env").unlink()
    assert found().path == str(candidates["notes.env"])
    (config / "notes.env").unlink()
    assert found().path == str(candidates["tools.env"])
    (config / "tools.env").unlink()
    assert found().path == str(candidates["PATH"]) and found().source == "PATH"
    candidates["PATH"].unlink()
    assert found().path == str(candidates["nix"]) and found().source == "~/.nix-profile/bin"
    candidates["nix"].unlink()
    assert found().path == str(candidates["racket installer"]) and found().source == "~/racket/bin"
    candidates["racket installer"].unlink()
    assert found().path == ""
    assert al_tools.settings(None) == {} and al_tools.settings(tmp_path / "no-such.al") == {}


def test_a_setting_naming_a_missing_file_is_reported_and_not_bypassed(home, tmp_path, monkeypatch):
    tool(tmp_path / "on-path", "uv")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    assert al_tools.find("uv").path == str(tmp_path / "on-path" / "uv")
    monkeypatch.setenv("AL_UV", "/nowhere/uv")
    found = al_tools.find("uv")
    assert found.path == "" and "not an executable file" in found.problem
    sentence = al_tools.missing_sentence(found)
    assert "AL_UV" in sentence and "/nowhere/uv" in sentence
    not_executable = tmp_path / "plain" / "uv"
    not_executable.parent.mkdir()
    not_executable.write_text("not a program\n")
    monkeypatch.setenv("AL_UV", str(not_executable))
    assert al_tools.find("uv").path == ""
    with pytest.raises(al_tools.ToolMissing):
        al_tools.argv(["uv", "run", "x.py"])


def test_raco_is_found_beside_racket(home, tmp_path, monkeypatch):
    tool(tmp_path / "on-path", "raco")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    racket = tool(tmp_path / "racket-here", "racket")
    raco = tool(tmp_path / "racket-here", "raco")
    monkeypatch.setenv("AL_RACKET", str(racket))
    found = al_tools.find("raco")
    assert found.path == str(raco) and found.source == "beside racket"
    raco.unlink()
    assert al_tools.find("raco").path == str(tmp_path / "on-path" / "raco")


def test_missing_sentence_says_what_where_and_how(home):
    found = al_tools.find("racket")
    sentence = al_tools.missing_sentence(found)
    assert "\n" not in sentence and sentence.endswith(".") and sentence.count(". ") == 0
    assert sentence.startswith("racket is missing: al looked at AL_RACKET (not set), PATH, ")
    for place in ("~/.nix-profile/bin", "~/.local/bin", "~/racket/bin"):
        assert place in sentence
    assert str(home) not in sentence
    assert "AL_RACKET=/path/to/racket" in sentence and "tools.env" in sentence and "local.env" in sentence
    assert "install Racket (https://racket-lang.org)" in sentence and "Errno" not in sentence


def test_need_lists_every_missing_tool_and_takes_under_a_second(home, tmp_path, monkeypatch):
    tool(tmp_path / "on-path", "git")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    monkeypatch.setenv("AL_UV", "/nowhere/uv")
    began = time.monotonic()
    with pytest.raises(al_tools.ToolMissing) as caught:
        al_tools.need(["git", "racket", "uv", "racket"])
    assert time.monotonic() - began < 1
    assert [f.name for f in caught.value.found] == ["racket", "uv"]
    assert len(caught.value.sentences) == 2 and all(s.endswith(".") for s in caught.value.sentences)
    assert al_tools.need(["git"]) == {"git": str(tmp_path / "on-path" / "git")}
    assert al_tools.agent_bin("claude-code") == ("claude",) and al_tools.agent_bin("mock") == ()


def test_argv_resolves_only_known_bare_names(home, tmp_path, monkeypatch):
    git = tool(tmp_path / "on-path", "git")
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    assert al_tools.argv(["git", "-C", "/somewhere", "status"]) == [str(git), "-C", "/somewhere", "status"]
    assert al_tools.argv(["/bin/echo", "hi"]) == ["/bin/echo", "hi"]
    assert al_tools.argv(["fc-match", "x"]) == ["fc-match", "x"]
    assert al_tools.argv([]) == []


def test_search_path_puts_tool_folders_first_once(home, tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    tool(first, "uv")
    tool(first, "git")
    tool(second, "racket")
    monkeypatch.setenv("AL_UV", str(first / "uv"))
    monkeypatch.setenv("AL_GIT", str(first / "git"))
    monkeypatch.setenv("AL_RACKET", str(second / "racket"))
    base = f"{second}{os.pathsep}/usr/bin"
    assert al_tools.search_path(("uv", "git", "racket", "claude"), base) == f"{first}{os.pathsep}{second}{os.pathsep}/usr/bin"
    assert al_tools.search_path_from([str(first), str(first), ""], "") == str(first)
    assert al_tools.search_path_from([], "/a:/b") == "/a:/b"


@pytest.mark.parametrize("case", ["setting", "path", "folder", "missing"])
def test_shell_finder_matches_find(home, tmp_path, monkeypatch, case):
    host_path = "/usr/bin:/bin"
    if not all(shutil.which(n, path=host_path) for n in ("sh", "env", "sed", "id")):
        pytest.skip("no sh, env, sed or id in /usr/bin or /bin")
    system = tmp_path / "system-bin"          # what the function itself runs, and nothing else
    system.mkdir()
    for name in ("env", "sed", "id"):
        (system / name).symlink_to(shutil.which(name, path=host_path))
    path = str(system)
    if case == "setting":
        uv = tool(tmp_path / "set", "uv")
        (home / ".config" / "al").mkdir(parents=True)
        (home / ".config" / "al" / "tools.env").write_text(f"# tools\nexport AL_UV='{uv}'\n")
        tool(tmp_path / "on-path", "uv")
        path = f"{tmp_path / 'on-path'}{os.pathsep}{system}"
    elif case == "path":
        tool(tmp_path / "on-path", "uv")
        tool(home / ".local" / "bin", "uv")
        path = f"{tmp_path / 'on-path'}{os.pathsep}{system}"
    elif case == "folder":
        tool(home / ".local" / "bin", "uv")
    monkeypatch.setenv("PATH", path)
    expected = al_tools.find("uv").path
    assert bool(expected) == (case != "missing")
    cp = subprocess.run([shutil.which("sh", path=host_path), "-c", al_tools.shell_finder() + '\nal_find uv AL_UV || echo "none"'],
                        env={"HOME": str(home), "PATH": path}, capture_output=True, text=True, timeout=30)
    assert cp.stdout.strip() == (expected or "none"), cp.stderr


def test_gone_dashboards_flags_a_dashboard_whose_python_is_gone(tmp_path: Path) -> None:
    """`al doctor`'s check on a simulated /proc: only the dashboard whose interpreter has gone is listed;
    a dashboard whose Python exists and another program whose argv0 has gone are not."""
    fakeproc = tmp_path / "proc"
    gone = str(tmp_path / "gone" / "bin" / "python")
    dashboard = ["/srv/notes.al/adws/dashboard/dashboard.py", "--layer", "/srv/notes.al"]

    def process(pid: int, argv: list[str], exe: str) -> None:
        folder = fakeproc / str(pid)
        folder.mkdir(parents=True)
        (folder / "cmdline").write_bytes("".join(f"{part}\0" for part in argv).encode())
        (folder / "exe").symlink_to(exe)

    process(4242, [gone, *dashboard], f"{gone} (deleted)")
    process(4343, [sys.executable, *dashboard], sys.executable)
    process(4444, [gone, "/srv/other/serve.py"], f"{gone} (deleted)")
    (fakeproc / "self").mkdir()
    found = al_tools.gone_dashboards(fakeproc)
    assert [entry["pid"] for entry in found] == [4242]
    assert found[0]["python"] == gone
    assert "restart the dashboard" in found[0]["sentence"]
    assert al_tools.gone_dashboards(tmp_path / "no-proc") == []
