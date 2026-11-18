"""The work dashboard as part of a generated layer: `(dashboard …)`, `al gen`, `al check`, `al dashboard`."""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import KIT, LAYER_TEMPLATE, al, commit, git, manifest, sh, status

DASHBOARD = ["adws/dashboard.py", "adws/dashboard/dashboard.py", "adws/dashboard/static/index.html",
             "adws/dashboard/static/app.js", "adws/dashboard/static/app.css", "adws/dashboard/static/voice.js",
             "adws/dashboard/static/voice-worklet.js"]
PODMAN = ["adws/dashboard/Containerfile", "adws/dashboard/run-in-podman.sh"]


def sibling_with(app: Path, clause: str) -> Path:
    layer = app.parent / "notes.al"
    layer.mkdir(exist_ok=True)
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="../notes", placement=f"(layer sibling)\n  {clause}"))
    al("gen", layer)
    return layer


def launch(layer: Path, *args: str, env: dict[str, str] | None = None) -> dict:
    """What adws/dashboard.py would run (AL_DASHBOARD_DRY_RUN prints it instead)."""
    full = {k: v for k, v in os.environ.items() if not k.startswith("AL_DASHBOARD")}
    full.update({"AL_DASHBOARD_DRY_RUN": "1", **(env or {})})
    cp = sh(["uv", "run", "--quiet", layer / "adws" / "dashboard.py", *args], env=full)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


def test_default_layer_gets_a_local_dashboard(sibling) -> None:
    for rel in DASHBOARD:
        assert (sibling / rel).is_file(), rel
        assert rel in manifest(sibling)["files"], rel                      # generated, hashed, committed
    for rel in PODMAN:
        assert not (sibling / rel).exists()
    assert not (sibling / "adws" / "dashboard" / "tests").exists() and not (sibling / "adws" / "dashboard" / "demo").exists()
    assert (sibling / "adws/dashboard/dashboard.py").read_text() == (KIT / "dashboard/dashboard.py").read_text()
    assert os.access(sibling / "adws/dashboard.py", os.X_OK)
    launcher = (sibling / "adws/dashboard.py").read_text()
    assert str(sibling) not in launcher and "/home/" not in launcher        # the same on every machine
    readme = (sibling / "README.md").read_text()
    assert "## Watch the work" in readme and "uv run adws/dashboard.py" in readme and "http://127.0.0.1:8765/" in readme
    assert not list((sibling / "systemd").glob("*dashboard*")) if (sibling / "systemd").exists() else True
    assert al("check", sibling).returncode == 0
    second = al("gen", sibling).stdout
    assert "(0 changed)" in second                                           # deterministic


def test_launcher_runs_this_layer_with_the_declared_settings_and_overrides(app) -> None:
    other = app.parent / "other.al"
    other.mkdir()
    (other / "agentic-layer.json").write_text("{}")
    layer = sibling_with(app, '(dashboard local #:port 9123 #:poll-ms 500 #:also ("../other.al" "../missing.al"))')
    run = launch(layer)
    argv = run["argv"]
    assert argv[1] == str((layer / "adws/dashboard/dashboard.py").resolve())
    assert argv[argv.index("--port") + 1] == "9123" and argv[argv.index("--poll") + 1] == "0.5"
    assert argv[argv.index("--bind") + 1] == "127.0.0.1"
    layers = [argv[i + 1] for i, a in enumerate(argv) if a == "--layer"]
    assert layers == [str(layer.resolve()), str(other.resolve())]           # the missing one is skipped
    assert run["token"] is False
    over = launch(layer, "--port", "9999", "--open")["argv"]
    assert over.count("--port") == 1 and over[over.index("--port") + 1] == "9999" and "--open" in over
    cp = sh(["uv", "run", "--quiet", layer / "adws/dashboard.py"], env={**os.environ, "AL_DASHBOARD_DRY_RUN": "1"})
    assert "missing.al" in cp.stderr and "skipped" in cp.stderr


def test_a_non_loopback_bind_needs_the_token_from_the_declared_variable(app) -> None:
    layer = sibling_with(app, '(dashboard local #:bind "0.0.0.0" #:token-env "NOTES_DASH_TOKEN")')
    full = {k: v for k, v in os.environ.items() if not k.startswith(("AL_DASHBOARD", "NOTES_DASH"))}
    cp = sh(["uv", "run", "--quiet", layer / "adws/dashboard.py"], env={**full, "AL_DASHBOARD_DRY_RUN": "1"})
    assert cp.returncode != 0 and "NOTES_DASH_TOKEN" in cp.stderr
    run = launch(layer, env={"NOTES_DASH_TOKEN": "s3cret-token"})
    assert run["token"] is True and "s3cret-token" not in json.dumps(run["argv"])   # via the environment only
    readme = (layer / "README.md").read_text()
    assert "$NOTES_DASH_TOKEN" in readme


def test_compile_error_for_an_open_bind_without_a_token(app) -> None:
    layer = app.parent / "notes.al"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="../notes", placement='(layer sibling)\n  (dashboard local #:bind "0.0.0.0")'))
    cp = al("gen", layer, ok=False)
    assert cp.returncode != 0 and "#:token-env" in cp.stderr


def test_podman_dashboard_and_systemd_unit(app) -> None:
    layer = sibling_with(app, '(dashboard podman #:port 9200 #:podman-socket #t #:systemd #t)')
    for rel in DASHBOARD + PODMAN:
        assert (layer / rel).is_file(), rel
    assert os.access(layer / "adws/dashboard/run-in-podman.sh", os.X_OK)
    argv = launch(layer)["argv"]
    assert argv[0] == str((layer / "adws/dashboard/run-in-podman.sh").resolve())
    assert argv[1:] == ["--port", "9200", "--poll", "0.75", "--podman-socket", str(layer.resolve())]
    unit = layer / "systemd" / "al-notes-dashboard.service"
    assert unit.is_file() and "adws/dashboard.py" in unit.read_text()
    assert "systemd/al-notes-dashboard.service" not in manifest(layer)["files"]                 # machine paths:
    assert "systemd/al-notes-dashboard.service" in json.loads((layer / ".al-manifest.local.json").read_text())["files"]
    assert "/systemd/al-notes-dashboard.service" in (layer / ".gitignore").read_text().splitlines()
    assert "container" in (layer / "README.md").read_text()
    (layer / "adws/dashboard/Containerfile").unlink()
    out = al("check", layer, ok=False).stdout
    assert "PROBLEM: missing adws/dashboard/Containerfile" in out


def test_none_removes_the_dashboard_on_regeneration(app) -> None:
    layer = sibling_with(app, "(dashboard local #:systemd #t)")
    assert (layer / "adws/dashboard.py").is_file() and (layer / "systemd/al-notes-dashboard.service").is_file()
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)\n  (dashboard none)"))
    al("gen", layer)
    for rel in [*DASHBOARD, "systemd/al-notes-dashboard.service"]:
        assert not (layer / rel).exists(), rel
    assert "No dashboard" in (layer / "README.md").read_text()
    assert al("check", layer).returncode == 0


def test_al_check_lists_missing_dashboard_files(sibling) -> None:
    (sibling / "adws/dashboard/static/app.js").unlink()
    out = al("check", sibling, ok=False).stdout
    assert "PROBLEM: missing adws/dashboard/static/app.js" in out


def test_in_repo_layer_commits_the_dashboard_and_stays_clean(in_repo, app) -> None:
    tracked = set(git(app, "ls-files", "agentic").splitlines())
    for rel in DASHBOARD:
        assert f"agentic/{rel}" in tracked, rel
    assert "uv run agentic/adws/dashboard.py" in (in_repo / "README.md").read_text()
    (in_repo / "agentic.rkt").write_text((in_repo / "agentic.rkt").read_text().replace(
        '(layer in-repo #:dir "agentic")', '(layer in-repo #:dir "agentic")\n  (dashboard local #:systemd #t)'))
    al("gen", in_repo)
    commit(app, "dashboard unit", "agentic")
    assert status(app) == ""                                                  # the unit is generated but ignored
    assert (in_repo / "systemd/al-notes-dashboard.service").is_file()
    assert "agentic/systemd/al-notes-dashboard.service" not in git(app, "ls-files", "agentic").splitlines()
    assert al("gen", in_repo).stdout.count("(0 changed)") == 1 and status(app) == ""
    assert al("check", in_repo).returncode == 0


def write_state(layer: Path, run_id: str, **fields) -> None:
    run = layer / "agents" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(json.dumps({"adw_id": run_id, "adw": "change", **fields}))


def test_al_check_notes_runs_that_died_without_failing(sibling) -> None:
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    gone = 4_000_000                                                          # above any pid_max
    host = socket.gethostname()
    write_state(sibling, "dead0001", status="running", heartbeat=old, pid=gone, host=host)
    write_state(sibling, "be000002", status="running", heartbeat=old, pid=os.getpid(), host=host)   # alive, slow beat
    write_state(sibling, "fe000003", status="running", heartbeat=fresh, pid=gone, host=host)
    write_state(sibling, "a1d00004", status="running")                       # no heartbeat: an older runtime
    write_state(sibling, "d0e00005", status="failed", heartbeat=old, pid=gone, host=host)
    write_state(sibling, "fa000006", status="running", heartbeat=old, pid=1, host="another-host")   # < 10 min
    write_state(sibling, "fa000007", status="running",
                heartbeat=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), pid=1, host="another-host")
    cp = al("check", sibling)
    notes = [line for line in cp.stdout.splitlines() if "died" in line]
    assert any("run dead0001 (change) died" in line and f"pid {gone} is gone" in line for line in notes)
    assert any("run fa000007" in line and "no heartbeat for 60 min" in line for line in notes)
    assert len(notes) == 2, notes
    assert cp.stdout.strip().endswith("check: ok")


def test_al_dashboard_watches_layers_and_scans_directories(sibling, app, tmp_path) -> None:
    env = {"AL_DASHBOARD_DRY_RUN": "1"}
    argv = json.loads(al("dashboard", sibling, tmp_path, "--port", "9300", "--open", env=env).stdout)["argv"]
    assert argv[1] == str(KIT / "dashboard" / "dashboard.py")
    assert argv[argv.index("--layer") + 1] == str(sibling.resolve())
    assert argv[argv.index("--scan") + 1] == str(tmp_path.resolve())
    assert argv[argv.index("--port") + 1] == "9300" and "--open" in argv
    here = json.loads(al("dashboard", cwd=app, env=env).stdout)["argv"]
    assert here[here.index("--scan") + 1] == str(app.resolve())
    run = json.loads(al("dashboard", sibling, "--token", "tok", "--bind", "0.0.0.0", env=env).stdout)
    assert run["token"] is True and "tok" not in run["argv"]
    podman = json.loads(al("dashboard", sibling, "--podman", env=env).stdout)["argv"]
    assert podman == [str(KIT / "dashboard" / "run-in-podman.sh"), "--port", "8765", str(sibling.resolve())]


def test_al_dashboard_passes_the_workspace_flags(sibling, tmp_path) -> None:
    env = {"AL_DASHBOARD_DRY_RUN": "1"}
    workspace = tmp_path / "code"
    workspace.mkdir()
    argv = json.loads(al("dashboard", sibling, "--workspace", workspace, "--workspace-depth", "2", "--no-actions",
                         env=env).stdout)["argv"]
    tail = argv[argv.index("--workspace"):]
    assert tail[:5] == ["--workspace", str(workspace.resolve()), "--workspace-depth", "2", "--no-actions"]
    off = json.loads(al("dashboard", sibling, "--no-workspace", env=env).stdout)["argv"]
    assert "--no-workspace" in off and "--workspace" not in off
    cp = al("dashboard", sibling, "--podman", "--workspace", workspace, env=env, ok=False)
    assert cp.returncode == 1 and "--workspace and actions need the host (git and al); not with --podman" in cp.stderr
    cp = al("dashboard", sibling, "--podman", "--no-actions", env=env, ok=False)
    assert cp.returncode == 1 and "not with --podman" in cp.stderr


def test_al_dashboard_passes_start_and_workspace_flags_together(sibling, tmp_path) -> None:
    env = {"AL_DASHBOARD_DRY_RUN": "1"}
    workspace = tmp_path / "code"
    workspace.mkdir()
    argv = json.loads(al("dashboard", sibling, "--no-start", "--workspace", workspace, "--no-actions",
                         env=env).stdout)["argv"]
    assert "--no-start" in argv and "--no-actions" in argv
    assert argv[argv.index("--workspace") + 1] == str(workspace.resolve())
    usage = al("--help").stdout
    assert "al run" in usage and "--workspace" in usage
