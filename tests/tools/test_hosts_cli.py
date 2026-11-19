"""`al hosts LAYER status`, `al host LAYER HOST doctor`, the generated adws/host.py and the git
source in `al deploy`'s list of what a host needs. The hosts are temporary directories
(deploy_host.py) reached through the runtime's local link; no ssh, no network."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import al, commit, manifest
from deploy_host import al_deploy

HEADLINE = "placement: auto, prefer least-loaded, at most 2 runs per host"


def _rows(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert lines[0] == HEADLINE and lines[1].split()[:3] == ["host", "runs", "cpus"]
    return {line.split()[0]: line for line in lines[2:]}


def test_hosts_status_prints_the_table_the_scheduler_sees(placed):
    layer, hosts, _source, env = placed
    cp = al("hosts", layer, "status", env=env)
    rows = _rows(cp.stdout)
    assert list(rows) == ["home", "build-1", "build-2"]
    for name in ("build-1", "build-2"):
        assert rows[name].split()[1] == "0/2" and rows[name].rstrip().endswith("ok")
        assert "claude-code: own login" in rows[name]
    assert rows["home"].split()[1] == "0/2"
    hosts["build-2"].vanish()                       # a host that does not answer says so; the table is still printed
    cp = al("hosts", layer, "status", env=env)
    rows = _rows(cp.stdout)
    assert "no answer" in rows["build-2"] and rows["build-1"].rstrip().endswith("ok")
    record = json.loads((layer / "agents" / "hosts.json").read_text())
    assert "silent" in record["hosts"]["build-2"] and record["hosts"]["build-1"]["max_runs"] == 2
    for private in (str(hosts["build-1"].root), "source.git"):
        assert private not in cp.stdout and private not in json.dumps(record)


def test_hosts_status_json(placed):
    layer, _hosts, _source, env = placed
    data = json.loads(al("hosts", layer, "status", "--json", env=env).stdout)
    assert data["layer"] == "notes" and data["placement"]["hosts"] == ["home", "build-1", "build-2"]
    assert sorted(data["hosts"]) == ["build-1", "build-2", "home"]
    assert data["hosts"]["build-1"]["logins"]["claude-code"] == "own login" and data["hosts"]["build-1"]["runs"] == 0


def test_hosts_needs_a_placement(in_repo):
    for args in (("hosts", in_repo, "status"), ("host", in_repo, "build-1", "doctor")):
        cp = al(*args, ok=False)
        assert cp.returncode == 2 and cp.stdout == ""
        assert "declares no (placement …), so every run executes on this machine" in cp.stderr
    cp = al("run", in_repo, "/change", "tidy", "--on", "build-1", "--dry-run", ok=False)
    assert cp.returncode == 2 and "--on needs a layer that declares (placement" in cp.stderr


def test_host_doctor_says_which_login_it_has(placed):
    layer, hosts, _source, env = placed
    own = al("host", layer, "build-1", "doctor", env=env)
    assert "login claude-code  own login" in own.stdout and "a run can execute on this host" in own.stdout
    assert "git source" in own.stdout and "answers" in own.stdout and "MISSING" not in own.stdout
    (hosts["build-2"].root / ".claude" / ".credentials.json").unlink()
    hosts["build-2"].set_env_file("notes", AL_GIT_SOURCE=env["AL_GIT_SOURCE"], ANTHROPIC_API_KEY="sk-host")
    token = al("host", layer, "build-2", "doctor", env=env)
    assert "login claude-code  token ANTHROPIC_API_KEY in its env file" in token.stdout and "sk-host" not in token.stdout
    hosts["build-2"].set_env_file("notes", AL_GIT_SOURCE=env["AL_GIT_SOURCE"])
    none = al("host", layer, "build-2", "doctor", env=env, ok=False)
    assert none.returncode == 1
    assert "MISSING login claude-code  none" in none.stdout
    assert "fix: log in on it, or name a token (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN) in its env file" in none.stdout
    assert "1 missing" in none.stdout
    hosts["build-2"].vanish()
    gone = al("host", layer, "build-2", "doctor", env=env, ok=False)
    assert gone.returncode == 1 and "host build-2: no answer" in gone.stdout and "AL_HOST_BUILD_2" in gone.stdout


def test_host_doctor_refuses_an_undeclared_host(placed):
    layer, _hosts, _source, env = placed
    for name in ("build-9", "home"):
        cp = al("host", layer, name, "doctor", env=env, ok=False)
        assert cp.returncode == 2
        assert f"{name} is not a host of notes's (placement …); hosts other than home: build-1, build-2" in cp.stderr


def test_gen_writes_host_py_only_with_a_placement(placed, tmp_path):
    layer, hosts, _source, _env = placed
    script = layer / "adws" / "host.py"
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env -S uv run") and "selfrun.tool_main" in text and script.stat().st_mode & 0o111
    assert "adws/host.py" in json.dumps(manifest(layer))
    assert (hosts["build-1"].root / "al" / "source" / "agentic" / "adws" / "host.py").is_file()   # deployed with the layer
    # without a placement the file is not generated, and one that was is removed
    rkt = layer / "agentic.rkt"
    rkt.write_text("\n".join(line for line in rkt.read_text().splitlines() if "(placement auto" not in line) + "\n")
    al("gen", layer, "--discard-edits")
    assert not script.exists() and "adws/host.py" not in json.dumps(manifest(layer))
    assert "placement" not in json.loads((layer / "agentic-layer.json").read_text())


def test_gen_writes_no_host_py_for_a_layer_that_never_had_a_placement(in_repo):
    assert not (in_repo / "adws" / "host.py").exists()
    assert (in_repo / "adws" / "adw_modules" / "placement.py").is_file()      # the runtime is one, whatever the layer declares


def test_check_lists_a_missing_host_py(placed):
    layer, _hosts, _source, _env = placed
    al("gen", layer, "--discard-edits")             # the fixture edited the compiled file; check wants what gen writes
    assert al("check", layer, ok=False).returncode in (0, 1)
    (layer / "adws" / "host.py").unlink()
    cp = al("check", layer, ok=False)
    assert cp.returncode != 0 and "adws/host.py" in cp.stdout + cp.stderr


def test_deploy_names_the_missing_git_source_variable(placed):
    layer, hosts, _source, _env = placed
    host = hosts["build-1"]
    host.set_env_file("notes", UNRELATED="1")
    cp = al("deploy", layer, "--to", "build-1.example", "--as", "build-1", "--no-enable", env=host.env(), ok=False)
    assert cp.returncode != 0
    out = cp.stdout + cp.stderr
    assert "AL_GIT_SOURCE" in out and "the git source runs on this host fetch from and push to" in out
    assert "AL_LAYER_SOURCE" not in out
    host.set_env_file("notes", AL_GIT_SOURCE="/somewhere/private.git")
    cp = al("deploy", layer, "--to", "build-1.example", "--as", "build-1", "--no-enable", env=host.env())
    assert "/somewhere/private.git" not in cp.stdout + cp.stderr
    status = al("deploy", layer, "--to", "build-1.example", "--as", "build-1", "--status", env=host.env())
    assert "placement: listed in #:hosts" in status.stdout


def test_required_env_names_the_git_source_only_for_a_listed_host():
    placement = {"hosts": ["home", "build-1"], "sourceEnv": "MY_GIT_SOURCE", "layerSourceEnv": "AL_LAYER_SOURCE"}
    ir = {"name": "notes", "placement": placement, "hosts": [{"id": "build-1"}, {"id": "spare"}]}
    listed = {need.name: need for need in al_deploy.required_env(ir, {"id": "build-1"})}
    assert listed["MY_GIT_SOURCE"].required and "git source" in listed["MY_GIT_SOURCE"].why
    assert "AL_LAYER_SOURCE" not in listed
    for decl in ({"id": "spare"}, None):
        names = [need.name for need in al_deploy.required_env(ir, decl)]
        assert "MY_GIT_SOURCE" not in names and "AL_LAYER_SOURCE" not in names
    assert al_deploy.required_env({"name": "notes"}, {"id": "build-1"}) == []
