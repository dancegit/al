"""Creating and adopting a layer from what the repository shows: the forge facts of `al scout`, the
measured commands of `al scout --into`, preferences built from the facts for `al author`, the sequence
`init → scout → adopt → author → gen → check → result` of `al create` / `al adopt-all` (and that the
dashboard runs the same one), and the result card. Real git, the real compiler, the mock author agent.

    uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_create.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import forge_fixtures as ff
from conftest import KIT, LAYER_TEMPLATE, ROOT, al, git, status
from test_adopt_suite import SUITE_ADWS
from test_author import FINISHED_STREAM, fake_claude

sys.path.insert(0, str(ROOT / "tools"))
import al_author  # noqa: E402
import al_create  # noqa: E402


def scout_json(app: Path) -> dict:
    return json.loads(al("scout", app, "--json").stdout)


# ------------------------------------------------------------------------------------------ forge facts

def test_scout_detects_a_self_hosted_forgejo_from_its_ci_folder(tmp_path):
    facts = scout_json(ff.forgejo_repo(tmp_path, setup=False, suite=False))
    assert facts["forge"] == "forgejo" and facts["forge_host"] == "https://git.example.org"
    assert facts["repo"] == "acme/larder"
    assert any(".forgejo/workflows" in line for line in facts["forge_evidence"])
    text = al("scout", tmp_path / "larder").stdout
    assert "- Host: https://git.example.org" in text and "- Evidence: " in text and ".forgejo/workflows" in text


def test_scout_detects_github(tmp_path):
    facts = scout_json(ff.github_repo(tmp_path))
    assert (facts["forge"], facts["forge_host"], facts["repo"]) == ("github", "https://github.com", "acme/notes")
    assert any(".github/workflows" in line for line in facts["forge_evidence"])


def test_scout_without_a_remote_is_forge_none(tmp_path):
    facts = scout_json(ff.bare_repo(tmp_path))
    assert (facts["forge"], facts["forge_host"], facts["repo"]) == ("none", "", "")
    assert facts["forge_evidence"] == ["no origin remote with a web URL"]


def test_init_writes_scout_json_without_absolute_paths(tmp_path):
    app = ff.forgejo_repo(tmp_path)
    al("init", app)
    text = (tmp_path / "larder.al" / "scout.json").read_text()
    facts = json.loads(text)
    assert facts["forge"] == "forgejo" and "app" not in facts
    assert str(tmp_path) not in text and str(Path(tmp_path).resolve()) not in text
    assert status(app) == ""


# ------------------------------------------------------------------------------------------ scout --into

NPM = {"package.json": '{"name": "larder", "private": true}\n'}    # its `npm install` needs the network


def worktrees(app: Path) -> str:
    return git(app, "worktree", "list")


def test_scout_into_verifies_commands_and_records_durations(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=False, suite=False, extra=NPM)
    layer = tmp_path / "larder.al"
    al("init", app)
    out = al("scout", app, "--into", layer, "--agent", "mock", "--timeout", "60", env=ff.make_env(tmp_path)).stdout
    assert "agent completion skipped (mock agent)" in out
    runs = {r["shell"]: r for r in json.loads((layer / "scout.json").read_text())["runs"]}
    for shell in ("make -j", "make test"):
        assert runs[shell]["exit"] == 0 and isinstance(runs[shell]["seconds"], (int, float)), runs
    assert runs["npm install"]["result"] == "not run: needs network" and runs["npm install"]["exit"] is None
    llm = (layer / "LLM.txt").read_text()
    row = next(line for line in llm.splitlines() if "`make test`" in line)
    assert "measured by al scout, exit 0" in row and "| UNVERIFIED |" not in row
    assert "not run: needs network" in next(line for line in llm.splitlines() if "`npm install`" in line)
    assert status(app) == "" and worktrees(app).count("\n") == 0
    assert not list((layer / "trees").glob("scout-*"))


def test_scout_into_condenses_the_agent_stream_unless_raw(tmp_path, monkeypatch):
    monkeypatch.delenv("AL_CLAUDE", raising=False)
    app = ff.bare_repo(tmp_path)
    layer = tmp_path / "fern.al"
    al("init", app)
    env = fake_claude(tmp_path)
    out = al("scout", app, "--into", layer, env=env).stdout
    assert not any(line.startswith('{"type"') for line in out.splitlines()), out[:2000]
    assert "  · read " in out
    lines = al("scout", app, "--into", layer, "--raw", env=env).stdout.splitlines()
    position = 0
    for expected in FINISHED_STREAM.read_text().splitlines():
        position = lines.index(expected, position) + 1          # every line of the stream, in order
    assert status(app) == ""


def test_scout_raw_goes_with_into(tmp_path):
    app = ff.bare_repo(tmp_path)
    cp = al("scout", app, "--raw", ok=False)
    assert cp.returncode != 0 and "--raw goes with --into" in cp.stderr


def test_scout_into_records_a_timeout(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=False, suite=False, extra={"Makefile": "all:\n\t@sleep 5\n\ntest:\n\t@echo ok\n"})
    layer = tmp_path / "larder.al"
    al("scout", app, "--into", layer, "--agent", "mock", "--timeout", "1", env=ff.make_env(tmp_path))
    runs = {r["shell"]: r for r in json.loads((layer / "scout.json").read_text())["runs"]}
    assert runs["make -j"]["result"] == "timed out after 1s" and runs["make test"]["exit"] == 0
    assert status(app) == "" and worktrees(app).count("\n") == 0


def test_scout_into_keeps_a_completed_llm_txt(tmp_path):
    app = ff.bare_repo(tmp_path)
    layer = tmp_path / "fern.al"
    al("init", app)
    (layer / "LLM.txt").write_text("# fern\n\nCompleted by a person.\n")
    out = al("scout", app, "--into", layer, "--agent", "mock").stdout
    assert "completed already" in out and (layer / "LLM.txt").read_text() == "# fern\n\nCompleted by a person.\n"


# ------------------------------------------------------------------------------------------ preferences

# the mock author: LAYER_TEMPLATE with the forge the preferences name and, when adopted/ exists, the includes
MOCK_DEFINITION = (LAYER_TEMPLATE.format(root="${root}", placement="(layer sibling)\n  ${forge}\n  ${includes}")
                   .replace('#:name "notes"', '#:name "${name}"')
                   .replace('(paths #:source ("app.py") #:tests ("test.sh") #:protected ("test.sh"))',
                            '(paths #:source ("app.py") #:tests ("test.sh") #:protected ("test.sh"))\n    ${codebase_includes}'))


def mock_file(tmp_path: Path, definition: str = MOCK_DEFINITION) -> Path:
    path = tmp_path / "mocks" / "author.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"files": {"agentic.rkt": definition}, "output": "wrote agentic.rkt"}))
    return path


def authored(tmp_path: Path, app: Path, *extra: str, env: dict | None = None, ok: bool = True):
    layer = app.with_name(app.name + ".al")
    al("init", app, env=env)
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path), *extra, env=env, ok=ok)
    prompt = (layer / "agents" / "author" / "prompt.txt").read_text() if ok else ""
    choices = json.loads((layer / "agents" / "author" / "choices.json").read_text()) if ok else {}
    return cp, prompt, choices


def test_author_preferences_name_the_detected_forgejo(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=False, suite=False)
    cp, prompt, choices = authored(tmp_path, app)
    assert '(forge forgejo #:host "https://git.example.org" #:repo "acme/larder")' in prompt
    assert "(prompt-input issue)" in prompt and "(trigger webhook)" in prompt and "(trigger manual)" in prompt
    assert "FORGEJO_TOKEN" in prompt and "set FORGEJO_TOKEN" in cp.stdout
    assert choices["forge"]["value"] == "forgejo" and ".forgejo/workflows" in choices["forge"]["because"]
    assert choices["review"] == {**choices["review"], "value": "human", "source": "default", "because": "facts are silent"}
    assert "default: review = human (facts are silent)" in cp.stdout
    ir = json.loads(al("compile", app.with_name("larder.al")).stdout)
    assert ir["forge"]["kind"] == "forgejo" and ir["forge"]["host"] == "https://git.example.org"


def test_author_preferences_github(tmp_path):
    cp, prompt, choices = authored(tmp_path, ff.github_repo(tmp_path))
    assert '(forge github #:repo "acme/notes")' in prompt and "GITHUB_TOKEN" in prompt
    assert choices["forge"]["source"] == "fact" and choices["repo"]["value"] == "acme/notes"


@pytest.mark.parametrize("gitlab", [False, True], ids=["no-remote", "gitlab"])
def test_author_preferences_without_remote_use_named_defaults(tmp_path, gitlab):
    cp, prompt, choices = authored(tmp_path, ff.bare_repo(tmp_path, gitlab=gitlab))
    assert "(forge none)" in prompt and "(prompt-input tasklist)" in prompt
    assert "(trigger cron)" in prompt and "(trigger manual)" in prompt and "TOKEN" not in prompt
    for key in ("forge", "input", "trigger"):
        assert choices[key]["source"] == "default", key
    for key in ("input", "trigger", "review"):
        assert choices[key]["because"] == "facts are silent", key
        assert f"default: {key} = {choices[key]['value']} (facts are silent)" in cp.stdout
    if gitlab:
        assert "gitlab is planned in the language" in choices["forge"]["because"]
    else:
        assert choices["forge"]["because"] == "facts are silent"


def test_author_container_environment_when_podman_is_found(tmp_path):
    podman = tmp_path / "fake-bin" / "podman"
    podman.parent.mkdir()
    podman.write_text("#!/bin/sh\nexit 0\n")
    podman.chmod(0o755)
    cp, prompt, choices = authored(tmp_path, ff.bare_repo(tmp_path), env={"AL_PODMAN": str(podman)})
    assert "(environment podman)" in prompt and "(environment worktree)" in prompt
    assert choices["environment"]["value"] == "worktree,podman" and choices["environment"]["source"] == "fact"


def test_choice_overrides_a_detected_value(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=False, suite=False)
    cp, prompt, choices = authored(tmp_path, app, "--choice", "forge=none")
    assert "(forge none)" in prompt and "(forge forgejo" not in prompt and "(prompt-input tasklist)" in prompt
    assert choices["forge"]["source"] == "user" and "chosen by the user" in choices["forge"]["because"]
    assert "FORGEJO_TOKEN" not in prompt


def test_choice_rejects_unknown_keys_and_values(tmp_path):
    app = ff.bare_repo(tmp_path)
    layer = tmp_path / "fern.al"
    al("init", app)
    for bad in ("colour=red", "forge=gitlab", "trigger=manual,sometimes", "host=git.example.org", "forge"):
        cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path), "--choice", bad, ok=False)
        assert cp.returncode == 1, bad
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path), "--choice", "colour=red", ok=False)
    assert "forge, host, repo, input, trigger, environment, review, delivery" in cp.stderr
    assert not (layer / "agentic.rkt").exists()


def test_author_never_reads_a_token(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=False, suite=False)
    cp, prompt, choices = authored(tmp_path, app, env={"FORGEJO_TOKEN": "secret-value"})
    layer = app.with_name("larder.al")
    assert "secret-value" not in prompt + json.dumps(choices) + cp.stdout + cp.stderr
    assert not [p for p in layer.rglob("*") if p.is_file() and b"secret-value" in p.read_bytes()]


def test_dashboard_choice_rules_equal_al_author_choices():
    dashboard = load_dashboard()
    assert list(dashboard.CHOICE_RULES) == list(al_author.CHOICES)
    for key, rule in al_author.CHOICES.items():
        assert dashboard.CHOICE_RULES[key] == (rule.values, rule.multi, rule.pattern), key


# ------------------------------------------------------------------------------------------ the sequence

def load_dashboard():
    spec = importlib.util.spec_from_file_location("al_dashboard_for_parity", KIT / "dashboard" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module            # dataclasses resolve their module by name
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_sequence_order():
    assert al_create.SEQUENCE_NAMES == ("init", "scout", "adopt", "author", "gen", "check", "result")


@pytest.mark.parametrize("flags, choices", [
    (["--agent", "mock", "--mock", "/m.json"], {}),
    ([], {"review": "agent", "forge": "none"}),
    (["--agent", "claude-code"], {"delivery": "trunk"}),
])
def test_dashboard_steps_equal_the_command_line_sequence(tmp_path, flags, choices):
    dashboard = load_dashboard()
    app = ff.bare_repo(tmp_path)
    entry = dashboard.RepoEntry(key="fern", name="fern", path=app, root=tmp_path)
    config = dashboard.ActionConfig(Path("/x/al"), flags, choices)
    layer = str(dashboard.layer_of(entry))
    expected = [(name, argv[1:]) for name, argv, _ in
                al_create.sequence("/x/al", os.path.realpath(app), layer, flags, choices, raw=True)]
    for build in (dashboard._steps_create, dashboard._steps_adopt):
        steps = build(entry, config, tmp_path / "docs")
        assert [(s.name, s.argv[1:]) for s in steps] == expected
        assert [s.stream for s in steps] == ["", "agent", "", "agent", "", "", ""]
        assert [s.timeout for s in steps] == [t for _, _, t in al_create.sequence("al", "a", "l", flags, choices)]


def test_sequence_without_raw_has_no_raw_flag():
    plain = al_create.sequence("al", "a", "l", ["--agent", "mock"], {"forge": "none"})
    assert not any("--raw" in argv for _, argv, _ in plain)
    raw = dict((name, argv) for name, argv, _ in al_create.sequence("al", "a", "l", ["--agent", "mock"], {"forge": "none"}, raw=True))
    assert raw["scout"][-1] == "--raw"
    assert raw["author"] == ["al", "author", "l", "--agent", "mock", "--raw", "--choice", "forge=none"]
    assert [name for name, argv in raw.items() if "--raw" in argv] == ["scout", "author"]


def step_lines(out: str) -> list[str]:
    return [line[3:] for line in out.splitlines() if line.startswith("== ") and " failed" not in line]


def test_create_runs_adopt_before_author_and_the_author_sees_the_adopted_material(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=True, suite=True)
    out = al("create", app, "--agent", "mock", "--mock", mock_file(tmp_path), env=ff.make_env(tmp_path)).stdout
    assert step_lines(out) == ["init", "scout", "adopt", "author", "gen", "check", "result"]
    prompt = (tmp_path / "larder.al" / "agents" / "author" / "prompt.txt").read_text()
    assert al_author.INCLUDE_SENTENCE in prompt
    assert status(app) == ""


def test_adopting_the_forgejo_fixture_yields_forge_adopted_rules_commands_and_suite(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=True, suite=True)
    rule = "Never commit directly; leave committing to the developer who asked for the change."
    definition = MOCK_DEFINITION.replace("(command commit #:template commit)",
                                         f"(command commit #:template commit #:focus ({json.dumps(rule)}))")
    out = al("adopt-all", app, "--agent", "mock", "--mock", mock_file(tmp_path, definition), env=ff.make_env(tmp_path)).stdout
    layer = tmp_path / "larder.al"
    ir = json.loads((layer / "agentic-layer.json").read_text())
    forge = ir["forge"]
    assert (forge["kind"], forge["host"], forge["repo"]) == ("forgejo", "https://git.example.org", "acme/larder")
    commands = {c["name"]: c for c in ir["commands"]}
    assert "review-pr" in commands and commands["review-pr"]["template"] == "custom"   # from existing_setup
    assert rule in (layer / "adopted" / "RULES.md").read_text()                     # the rule of its CLAUDE.md …
    assert rule in commands["commit"]["focus"]                                      # … carried as a focus line
    adws = {a["name"]: a for a in ir["adws"]}
    suite = [name for name in SUITE_ADWS if name in adws]
    assert suite and all(any(s.get("kind") == "script" for s in adws[n]["steps"]) for n in suite)
    assert "check: ok" in out
    al("check", layer)
    assert status(app) == ""


def test_adopt_all_refuses_a_repository_with_nothing_to_adopt(tmp_path):
    app = ff.bare_repo(tmp_path)
    cp = al("adopt-all", app, "--agent", "mock", "--mock", mock_file(tmp_path), ok=False)
    assert cp.returncode == 1 and "use al create" in cp.stderr
    assert not (tmp_path / "fern.al").exists()


def test_create_on_a_repository_without_remote(tmp_path):
    app = ff.bare_repo(tmp_path)
    out = al("create", app, "--agent", "mock", "--mock", mock_file(tmp_path)).stdout
    layer = tmp_path / "fern.al"
    assert "nothing to adopt: no existing agent setup found" in out and not (layer / "adopted").exists()
    assert json.loads((layer / "agentic-layer.json").read_text())["forge"]["kind"] == "none"
    card = json.loads(al("result", layer, "--json").stdout)
    assert card["forge"]["clause"] == "(forge none)" and card["next"][0] == f"al verify {layer.resolve()}"
    assert status(app) == ""


def test_result_card(tmp_path):
    app = ff.forgejo_repo(tmp_path, setup=True, suite=True, extra={"package.json": '{"name": "larder"}\n'})
    al("adopt-all", app, "--agent", "mock", "--mock", mock_file(tmp_path), env=ff.make_env(tmp_path))
    layer = tmp_path / "larder.al"
    card = json.loads(al("result", layer, "--json").stdout)
    assert set(card) >= {"layer", "forge", "workflows", "adopted", "left_behind", "unverified", "next"}
    assert card["layer"] == str(layer.resolve())
    assert {k: card["forge"][k] for k in ("kind", "host", "repo")} == {
        "kind": "forgejo", "host": "https://git.example.org", "repo": "acme/larder"}
    assert set(SUITE_ADWS) & set(card["workflows"]) and "change" in card["workflows"]
    assert "CLAUDE.md" in card["adopted"]["rules"] and "review-pr" in card["adopted"]["commands"]
    assert card["adopted"]["suite"] and card["left_behind"]
    assert {"name": "install", "shell": "npm install", "result": "not run: needs network"} in card["unverified"]
    assert card["next"][0].startswith("set FORGEJO_TOKEN")
    text = al("result", layer).stdout
    print(text)
    assert len(text.strip().splitlines()) <= 12 and "set FORGEJO_TOKEN" in text


def test_choices_json_lists_each_choice_with_its_fact(tmp_path):
    app = ff.forgejo_repo(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    choices = json.loads(al("create", app, "--choices", "--json").stdout)
    assert list(choices) == list(al_author.CHOICES)
    for key, c in choices.items():
        assert {"value", "options", "fact"} <= set(c), key
    assert choices["forge"]["value"] == "forgejo" and ".forgejo/workflows" in choices["forge"]["fact"]
    assert choices["review"]["fact"] == "default: facts are silent"
    assert sorted(p.name for p in tmp_path.iterdir()) == before and status(app) == ""


# ------------------------------------------------------------------------------------------ inside the repository

# MOCK_DEFINITION with the placement `al author` names: (layer in-repo #:dir "agentic") inside, (layer sibling) beside
IN_REPO_DEFINITION = MOCK_DEFINITION.replace("(layer sibling)", "${placement}")
assert "${placement}" in IN_REPO_DEFINITION


def test_dashboard_in_repo_dir_equals_al_create():
    dashboard = load_dashboard()
    assert dashboard.IN_REPO_DIR == al_create.IN_REPO_DIR
    assert dashboard.PLACEMENTS == ("beside", "inside")


def test_in_repo_sequence_differs_only_in_init_and_the_layer():
    inside = al_create.sequence("al", "/a", "/a/agentic", [], {}, in_repo=True)
    beside = al_create.sequence("al", "/a", "/a/agentic", [], {})
    assert tuple(name for name, _, _ in inside) == al_create.SEQUENCE_NAMES
    assert dict((name, argv) for name, argv, _ in inside)["init"] == ["al", "init", "/a", "--in-repo"]
    assert [step for step in inside if step[0] != "init"] == [step for step in beside if step[0] != "init"]


@pytest.mark.parametrize("flags, choices", [
    (["--agent", "mock", "--mock", "/m.json"], {}),
    ([], {"review": "agent", "forge": "none"}),
    (["--agent", "claude-code"], {"delivery": "trunk"}),
])
def test_dashboard_inside_steps_equal_the_in_repo_sequence(tmp_path, flags, choices):
    dashboard = load_dashboard()
    app = ff.bare_repo(tmp_path)
    entry = dashboard.RepoEntry(key="fern", name="fern", path=app, root=tmp_path)
    config = dashboard.ActionConfig(Path("/x/al"), flags, choices, placement="inside")
    assert dashboard.layer_of(entry, "inside") == Path(os.path.realpath(app)) / "agentic"
    layer = str(dashboard.layer_of(entry, "inside"))
    expected = [(name, argv[1:]) for name, argv, _ in
                al_create.sequence("/x/al", os.path.realpath(app), layer, flags, choices, raw=True, in_repo=True)]
    assert expected[0] == ("init", ["init", os.path.realpath(app), "--in-repo"])
    for build in (dashboard._steps_create, dashboard._steps_adopt):
        steps = build(entry, config, tmp_path / "docs")
        assert [(s.name, s.argv[1:]) for s in steps] == expected


def test_create_in_repo_puts_the_layer_inside_the_repository(tmp_path):
    app = ff.bare_repo(tmp_path)
    out = al("create", app, "--in-repo", "--agent", "mock", "--mock", mock_file(tmp_path, IN_REPO_DEFINITION)).stdout
    assert step_lines(out) == ["init", "scout", "adopt", "author", "gen", "check", "result"]
    layer = app / "agentic"
    ir = json.loads((layer / "agentic-layer.json").read_text())
    assert ir["layer"]["kind"] == "in-repo" and ir["layer"]["dir"] == "agentic"
    assert not (tmp_path / "fern.al").exists()
    assert al("check", layer).stdout.strip().endswith("check: ok")
    lines = status(app).splitlines()
    assert lines and all(line[3:].startswith("agentic/") for line in lines), lines


def test_author_tells_the_agent_the_layer_is_in_the_repository(tmp_path):
    app = ff.bare_repo(tmp_path)
    al("init", app, "--in-repo")
    layer = app / "agentic"
    al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path, IN_REPO_DEFINITION))
    prompt = (layer / "agents" / "author" / "prompt.txt").read_text()
    assert 'The layer is in the repository: (codebase #:root "..") and (layer in-repo #:dir "agentic").' in prompt
    assert "The codebase root, relative to the layer folder, is" not in prompt
    ir = json.loads(al("compile", layer).stdout)
    assert ir["layer"] == {**ir["layer"], "kind": "in-repo", "dir": "agentic"}


def test_author_refuses_a_definition_without_the_in_repo_clause(tmp_path):
    app = ff.bare_repo(tmp_path)
    al("init", app, "--in-repo")
    layer = app / "agentic"
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path), ok=False)   # writes (layer sibling)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert '(layer in-repo #:dir "agentic")' in cp.stderr and al_author.REPAIR_HINT in cp.stderr
    assert (layer / "agentic.rkt").is_file()
    cp = al("author", layer, "--agent", "mock", "--mock", mock_file(tmp_path), "--accept-existing", ok=False)
    assert cp.returncode == 1 and '(layer in-repo #:dir "agentic")' in cp.stderr


def test_author_accepts_a_central_layer_inside_another_repository(tmp_path):
    app = ff.bare_repo(tmp_path)
    al("init", app)
    home = tmp_path / "layers"   # a versioned layers home: a git repository that is not the application
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    layer = home / "shop"
    shutil.move(str(tmp_path / "fern.al"), str(layer))
    shutil.rmtree(layer / ".git", ignore_errors=True)
    definition = (MOCK_DEFINITION.replace("(layer sibling)", f'(layer central #:home "{home}")')
                  .replace("${root}", "../../fern"))
    mock = mock_file(tmp_path, definition)
    prefs = "The codebase root, relative to the layer folder, is ../../fern."
    cp = al("author", layer, "--agent", "mock", "--mock", mock, "--preferences", prefs)
    assert "agentic.rkt compiles" in cp.stdout
    assert "in-repo" not in cp.stdout + cp.stderr
    cp = al("author", layer, "--agent", "mock", "--mock", mock, "--preferences", prefs, "--accept-existing")
    assert "accepted" in cp.stdout
    (layer / ".gitignore").write_text("/trees/\n")   # as `al gen` leaves one: the mark alone must not decide
    cp = al("author", layer, "--agent", "mock", "--mock", mock, "--accept-existing")
    assert cp.returncode == 0 and "accepted" in cp.stdout
    ir = json.loads(al("compile", layer).stdout)
    assert ir["layer"]["kind"] == "central"


def test_create_in_repo_never_overwrites_a_definition(tmp_path):
    app = ff.bare_repo(tmp_path)
    (app / "agentic").mkdir()
    (app / "agentic" / "agentic.rkt").write_text("#lang al\n")
    cp = al("create", app, "--in-repo", "--agent", "mock", "--mock", mock_file(tmp_path, IN_REPO_DEFINITION), ok=False)
    assert cp.returncode == 1 and "already exists; it is never overwritten" in cp.stderr
    assert (app / "agentic" / "agentic.rkt").read_text() == "#lang al\n"
    assert not (app / "agentic" / "LLM.txt").exists()


def test_mock_placement_is_sibling_for_a_layer_beside(tmp_path):
    app = ff.bare_repo(tmp_path)
    al("create", app, "--agent", "mock", "--mock", mock_file(tmp_path, IN_REPO_DEFINITION))
    ir = json.loads((tmp_path / "fern.al" / "agentic-layer.json").read_text())
    assert ir["layer"]["kind"] == "sibling"
    assert status(app) == ""
