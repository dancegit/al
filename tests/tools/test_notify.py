"""Notifications as `tools/al` and `tools/explain.py` show them: `local.env` in the generated
`.gitignore` of every placement, `al check` on a tracked `local.env`, the layer README's section,
the compile error for a private value in `agentic.rkt`, `al notify-test` and `al explain`.
The real `tools/al` and racket are used; the only network is the test's own SMTP server on
127.0.0.1 (kit/runtime/tests/smtp_sink.py). Every address and password is a placeholder.
"""
from __future__ import annotations

import email
import email.policy
import importlib.util
from pathlib import Path

import pytest

from conftest import KIT, LAYER_TEMPLATE, ROOT, al, commit, git, sh, status


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


explain = _load("al_explain_notify", ROOT / "tools" / "explain.py")
smtp_sink = _load("al_smtp_sink", KIT / "runtime" / "tests" / "smtp_sink.py")


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_NOTIFY_TO", "AL_NOTIFY_FROM", "AL_NOTIFY_SMTP_URL", "AL_DASHBOARD_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def sink():
    server = smtp_sink.Sink()
    yield server
    server.close()


def layer_with(layer: Path, clauses: str, placement: str = "(layer sibling)", root: str = "../notes") -> Path:
    """Write an agentic.rkt from LAYER_TEMPLATE with the notify clauses after the placement."""
    layer.mkdir(exist_ok=True)
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root=root, placement=f"{placement}\n  {clauses}"))
    return layer


def _subject(message: dict) -> str:
    return str(email.message_from_bytes(message["data"], policy=email.policy.default)["Subject"])


# ------------------------------------------------------------------------------------ files

def test_local_env_is_ignored_in_every_placement(in_repo, sibling, app, tmp_path) -> None:
    assert "/local.env" in (in_repo / ".gitignore").read_text().splitlines()
    assert "/local.env" in (sibling / ".gitignore").read_text().splitlines()
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "app.py").write_text("X = 1\n")
    commit(other, "an app")
    al("init", other, "--in-repo")                             # before any `al gen`
    assert "/local.env" in (other / "agentic" / ".gitignore").read_text().splitlines()

    (in_repo / "local.env").write_text("AL_NOTIFY_TO=ops@example.org\n")
    assert status(app) == ""
    checked = al("check", in_repo)
    assert checked.returncode == 0 and "PROBLEM" not in checked.stdout
    assert "private values (`local.env`)" in (in_repo / "README.md").read_text()


def test_check_fails_when_local_env_is_tracked(in_repo, app) -> None:
    (in_repo / "local.env").write_text("AL_NOTIFY_TO=canary@example.org\nAL_NOTIFY_SMTP_URL=smtps://u:CANARY@mail.example.org\n")
    git(app, "add", "-f", "agentic/local.env")
    commit(app, "a mistake", "agentic/local.env")
    checked = al("check", in_repo, ok=False)
    assert checked.returncode == 1
    assert "PROBLEM: local.env is tracked by git" in checked.stdout and "git rm --cached local.env" in checked.stdout
    assert "canary" not in checked.stdout + checked.stderr and "CANARY" not in checked.stdout + checked.stderr
    git(app, "rm", "-q", "--cached", "agentic/local.env")
    git(app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "no longer tracked")
    assert (in_repo / "local.env").is_file()
    assert al("check", in_repo).returncode == 0


def test_layer_readme_names_the_variables_and_local_env(sibling, app) -> None:
    assert "## Notifications" not in (sibling / "README.md").read_text()
    assert "\n\n## Change the layer" in (sibling / "README.md").read_text()
    assert "\n\n\n## Change the layer" not in (sibling / "README.md").read_text()
    layer = layer_with(app.parent / "told.al", '(notify email)\n  (notify command #:id hook #:run "cat | wc -l" #:events (shipped))')
    al("gen", layer)
    readme = (layer / "README.md").read_text()
    for needle in ("## Notifications", "AL_NOTIFY_TO", "AL_NOTIFY_FROM", "AL_NOTIFY_SMTP_URL", "local.env",
                   "al notify-test <this directory>", "~/.config/al/notes.env",
                   "| `email` | email | needs-human, failed | `AL_NOTIFY_TO` (recipient), `AL_NOTIFY_FROM` (sender), `AL_NOTIFY_SMTP_URL` (SMTP URL) |",
                   "| `hook` | command | shipped | whatever `cat \\| wc -l` refers to |"):
        assert needle in readme, needle
    assert readme.index("## Watch the work") < readme.index("## Notifications") < readme.index("## Change the layer")
    assert (layer / "adws/adw_modules/notify.py").is_file() and (layer / "adws/adw_modules/localenv.py").is_file()
    assert (sibling / "adws/adw_modules/notify.py").is_file()


def test_gen_rejects_an_address_in_the_layer_file(app) -> None:
    layer = layer_with(app.parent / "wrong.al", '(notify email #:to-env "ops@example.org")')
    refused = al("gen", layer, ok=False)
    assert refused.returncode == 1
    assert "#:to-env takes the NAME of an environment variable" in refused.stderr
    assert "looks like an e-mail address" in refused.stderr and "local.env" in refused.stderr
    layer_with(layer, '(notify email #:smtp-url-env "smtps://user:secret@mail.example.org")')
    refused = al("gen", layer, ok=False)
    assert refused.returncode == 1
    assert "#:smtp-url-env takes the NAME of an environment variable" in refused.stderr and "looks like a URL" in refused.stderr


# ------------------------------------------------------------------------------------ al notify-test

def test_notify_test_reports_each_notifier(app, sink) -> None:
    layer = layer_with(app.parent / "told.al", '(notify email)\n  (notify command #:id hook #:run "exit 3")')
    al("gen", layer)
    (layer / "local.env").write_text(f"AL_NOTIFY_TO=ops@example.org\nAL_NOTIFY_FROM=al@example.org\n"
                                     f"AL_NOTIFY_SMTP_URL=smtp://127.0.0.1:{sink.port}\n")
    tested = al("notify-test", layer, ok=False)
    assert tested.returncode == 1, tested.stderr
    assert "notify-test: layer notes, event needs-human: a marked test message, no run behind it" in tested.stdout
    assert "  email (email)  accepted\n" in tested.stdout
    assert "  hook (command)  FAILED: exit 3\n" in tested.stdout
    assert "notify-test: 1/2 accepted" in tested.stdout
    assert len(sink.messages) == 1 and "TEST, needs a person" in _subject(sink.messages[0])
    assert sink.messages[0]["to"] == ["ops@example.org"]

    shipped = al("notify-test", layer, "--event", "shipped", ok=False)
    assert "TEST, shipped" in _subject(sink.messages[1])
    assert "  email (email)  accepted  (does not report shipped in real runs)\n" in shipped.stdout
    for output in (tested.stdout, tested.stderr, shipped.stdout, shipped.stderr):
        assert "example.org" not in output and str(sink.port) not in output
    assert not (layer / "agents").exists()

    layer_with(layer, '(notify email)\n  (notify command #:id hook #:run "true")')
    al("gen", layer)
    passed = al("notify-test", layer)
    assert passed.returncode == 0 and "notify-test: 2/2 accepted" in passed.stdout


def test_notify_test_without_notifiers_says_how_to_declare_one(sibling) -> None:
    tested = al("notify-test", sibling, ok=False)
    assert tested.returncode == 1
    assert "no notifier is declared" in tested.stdout and "(notify email)" in tested.stdout and "local.env" in tested.stdout
    ungenerated = sibling.parent / "empty.al"
    ungenerated.mkdir()
    refused = al("notify-test", ungenerated, ok=False)
    assert refused.returncode == 1 and "is not a generated layer" in refused.stderr


# ------------------------------------------------------------------------------------ al explain

def test_notify_text_describes_each_notifier_in_plain_words() -> None:
    ir = {"notifiers": [
        {"id": "email", "kind": "email", "events": ["needs-human", "failed"], "toEnv": "OPS_MAIL",
         "fromEnv": "AL_NOTIFY_FROM", "smtpUrlEnv": "AL_NOTIFY_SMTP_URL", "timeout": 10},
        {"id": "desktop", "kind": "command", "events": ["shipped", "needs-human"], "timeout": 10,
         "run": "notify-send \"$AL_NOTIFY_SUBJECT\""}]}
    title, text, bullets = explain.notify_text(ir)
    assert title == "Telling a person"
    assert text == "Notifications: a person is told when a run needs them, fails or ships its change."
    assert bullets[0] == ("`email`: an e-mail when a run needs them or fails; the recipient, the sender and the mail "
                          "server are read from the environment variables `OPS_MAIL`, `AL_NOTIFY_FROM` and `AL_NOTIFY_SMTP_URL`")
    assert bullets[1] == ("`desktop`: runs `notify-send \"$AL_NOTIFY_SUBJECT\"` with the message on standard input "
                          "when a run needs them or ships its change")
    assert len(bullets) == 5 and "`local.env`" in bullets[2] and "never committed" in bullets[2]
    assert "no logs" in bullets[3] and "`al notify-test`" in bullets[4]
    silent = ("Telling a person", "No notifications: nothing is sent when a run ends or needs a person.", [])
    assert explain.notify_text({"notifiers": [{"id": "none", "kind": "none"}]}) == silent
    assert explain.notify_text({}) == silent
    assert explain.explain({"head": "notify"}, {"codebase": {}, **ir})[0] == "Telling a person"


def test_explain_describes_notifications_without_values(app, sibling, tmp_path) -> None:
    layer = layer_with(app.parent / "told.al", '(notify email #:events (needs-human failed passed))')
    al("gen", layer)
    (layer / "local.env").write_text("AL_NOTIFY_TO=canary@example.org\nAL_NOTIFY_SMTP_URL=smtps://u:CANARY@mail.example.org\n")
    env = {"AL_NOTIFY_TO": "canary@example.org", "AL_NOTIFY_SMTP_URL": "smtps://u:CANARY@mail.example.org"}
    told = al("explain", layer, env=env).stdout
    markdown = tmp_path / "explained.md"
    al("explain", layer, "--md", markdown, env=env)
    for output in (told, markdown.read_text()):
        assert "Telling a person" in output and "AL_NOTIFY_TO" in output and "local.env" in output
        assert "a person is told when a run needs them, fails or finishes" in " ".join(output.split())
        assert "canary@example.org" not in output and "CANARY" not in output
    assert "Telling a person" not in al("explain", sibling).stdout       # the default is silent
