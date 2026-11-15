"""Notifications: a person is told when a run needs them, fails, finishes or ships.

Tested here: `local.env` parsing and the precedence of the three sources (localenv), the SMTP URL
forms and the plain-text refusal, the event a run reports, the message's wording and what it must
never contain, delivery of the `email` and `command` kinds, the once-only rule, failing notifiers,
dry runs, fan-out candidates and `notify.test`. Runs go through `runner.main` with real git and
the mock agent. The SMTP server is `smtp_sink.py` in this directory, on 127.0.0.1 only. Every
address, host and password is a placeholder (example.org, 127.0.0.1, `secret`).
"""
from __future__ import annotations

import email
import email.policy
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smtp_sink  # noqa: E402

from adw_modules import events, ir, localenv, notify  # noqa: E402
from adw_modules.runner import main  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

ADW_ID = "e0e0a001"
PROMPT = "Add a pay band to each worker.\nSECOND-LINE-MARKER details that must not travel"
MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
TEXT = {"template": "build", "output": "text", "description": "", "file": "", "focus": [], "fabric": [],
        "relevant": [], "validation": []}
MAIL = {"id": "mail", "kind": "email", "status": "stable", "events": ["needs-human", "failed"],
        "toEnv": "AL_NOTIFY_TO", "fromEnv": "AL_NOTIFY_FROM", "smtpUrlEnv": "AL_NOTIFY_SMTP_URL", "timeout": 5}
TO, FROM = "ops@example.org, lead@example.org", "al@example.org"


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    """The person's own settings never reach a test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_NOTIFY_TO", "AL_NOTIFY_FROM", "AL_NOTIFY_SMTP_URL", "AL_DASHBOARD_URL", "ADW_FANOUT_PARENT"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def sink():
    servers = []

    def factory(mode: str = "plain", behaviour: str = "ok", context=None) -> smtp_sink.Sink:
        servers.append(smtp_sink.Sink(mode, context, behaviour))
        return servers[-1]
    yield factory
    for server in servers:
        server.close()


@pytest.fixture(scope="session")
def tls(tmp_path_factory):
    return smtp_sink.certificate(tmp_path_factory.mktemp("tls"))


def _layer(make_layer, notifiers, *, steps_=None, review=None, failing=None) -> Path:
    """The ADW `demo` on the mock agent: plan, build, a check `test`, review, ship. `failing="check"`
    makes the check fail after printing LOG-TAIL-MARKER, so the run is handed over with PR.md."""
    run = "echo LOG-TAIL-MARKER; exit 2" if failing == "check" else "true"
    ir_ = {
        "agents": [MOCK],
        "commands": [{"name": "plan", "modelSet": "base", **TEXT}, {"name": "build", "modelSet": "base", **TEXT},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json", "description": "",
                      "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}],
        "codebase": {"commands": [{"name": "test", "run": run, "timeout": 20}]},
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 28100,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [review or {"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False,
                               "bodyCmd": ""}],
        "adws": [{"name": "demo", "agent": "mock", "review": (review or {"id": "human"})["id"],
                  "environment": ["wt"], "input": ["cli"], "trigger": ["manual"], "classes": [],
                  "steps": steps_ or [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
                                      {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
                                      {"name": "test", "kind": "check", "cmd": "test", "retries": 0, "collect": []},
                                      {"name": "review", "kind": "review"},
                                      {"name": "ship", "kind": "ship"}]}],
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": None,
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
    }
    if notifiers is not None:
        ir_["notifiers"] = notifiers
    layer = make_layer(ir_)
    _mock(layer, "build", files={"seed.py": "PAY_BANDS = {'hca': 'Band 3'}\n"}, commit=True, output="built")
    return layer


def _mock(layer: Path, step: str, **script) -> None:
    path = layer / "mock" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script), encoding="utf-8")


def _state(layer: Path, adw_id: str = ADW_ID) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _events(layer: Path, adw_id: str = ADW_ID) -> list[dict]:
    return events.read(layer / "agents" / adw_id / "events.jsonl")


def _notify_events(layer: Path, adw_id: str = ADW_ID) -> list[dict]:
    return [event for event in _events(layer, adw_id) if event["type"].startswith("notify.")]


def _local_env(layer: Path, **values: str) -> None:
    (layer / "local.env").write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def _mail_env(layer: Path, server: smtp_sink.Sink, **more: str) -> None:
    _local_env(layer, AL_NOTIFY_TO=TO, AL_NOTIFY_FROM=FROM, AL_NOTIFY_SMTP_URL=f"smtp://127.0.0.1:{server.port}", **more)


def _parsed(message: dict):
    return email.message_from_bytes(message["data"], policy=email.policy.default)


def _text(parsed) -> str:
    """The body as it was composed (the wire has CRLF line ends)."""
    return parsed.get_content().replace("\r\n", "\n")


def _run(layer: Path, *flags: str, adw_id: str = ADW_ID) -> int:
    return main("demo", layer, [PROMPT, "--adw-id", adw_id, *flags])


# ------------------------------------------------------------------------------------ localenv

def test_local_env_parsing(tmp_path, capsys):
    text = ("# a comment\n\nPLAIN=value\nexport EXPORTED=one\nDOUBLE=\"two words\"\nSINGLE='three'\n"
            "URL=smtp://h:1?a=b#frag\n  SPACED  =  padded  \n1BAD=x\nno equals sign here\nPLAIN=later\nEMPTY=\n")
    values, bad = localenv.parse(text)
    assert values == {"PLAIN": "later", "EXPORTED": "one", "DOUBLE": "two words", "SINGLE": "three",
                      "URL": "smtp://h:1?a=b#frag", "SPACED": "padded", "EMPTY": ""}
    assert bad == [9, 10]
    assert localenv.parse("A=\"unmatched'\nB=$HOME\\n\n")[0] == {"A": "\"unmatched'", "B": "$HOME\\n"}

    layer_dir = tmp_path / "somewhere"
    layer_dir.mkdir()
    (layer_dir / "local.env").write_text("GOOD=1\nSECRET-TEXT-OF-THE-LINE\n")
    assert localenv.load(SimpleNamespace(name="test-layer", layer_dir=layer_dir)) == {"GOOD": "1"}
    warning = capsys.readouterr().err
    assert "[local.env] warning: local.env line 2 is not KEY=VALUE; ignored" in warning
    assert str(tmp_path) not in warning and "SECRET-TEXT" not in warning
    assert localenv.load(SimpleNamespace(name="test-layer", layer_dir=tmp_path / "missing")) == {}


def test_local_env_precedence_environment_then_layer_file_then_config_file(tmp_path, monkeypatch):
    layer_dir = tmp_path / "layer"
    layer_dir.mkdir()
    layer = SimpleNamespace(name="test-layer", layer_dir=layer_dir)
    config = tmp_path / "xdg" / "al" / "test-layer.env"
    config.parent.mkdir(parents=True)
    config.write_text("WHO=config\nONLY_CONFIG=c\n")
    assert localenv.config_file("test-layer") == config
    assert localenv.lookup("WHO", localenv.load(layer)) == "config"
    (layer_dir / "local.env").write_text("WHO=layer-file\n")
    values = localenv.load(layer)
    assert localenv.lookup("WHO", values) == "layer-file" and localenv.lookup("ONLY_CONFIG", values) == "c"
    monkeypatch.setenv("WHO", "environment")
    assert localenv.lookup("WHO", values) == "environment"
    monkeypatch.setenv("WHO", "")                      # an empty environment value does not hide the file's
    assert localenv.lookup("WHO", values) == "layer-file"
    assert localenv.lookup("NOWHERE", values) == ""

    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    default = tmp_path / "home" / ".config" / "al" / "test-layer.env"
    default.parent.mkdir(parents=True)
    default.write_text("ONLY_DEFAULT=d\n")
    assert localenv.config_file("test-layer") == default
    assert localenv.load(layer) == {"ONLY_DEFAULT": "d", "WHO": "layer-file"}

    assert localenv.config_file("Bad Name") is None and localenv.config_file("../escape") is None
    odd = SimpleNamespace(name="Bad Name", layer_dir=layer_dir)
    assert localenv.files(odd) == [layer_dir / "local.env"] and localenv.load(odd) == {"WHO": "layer-file"}


def test_file_values_are_not_put_into_the_process_environment(make_layer, sink):
    server = sink()
    layer_dir = _layer(make_layer, [MAIL])
    _mail_env(layer_dir, server, ONLY_IN_THE_FILE="x")
    layer = ir.load(layer_dir)
    before = dict(os.environ)
    assert localenv.load(layer)["ONLY_IN_THE_FILE"] == "x"
    state = ADWState(ADW_ID, "demo", prompt=PROMPT, status="failed")
    assert notify.report(layer, state) is True
    assert len(server.messages) == 1 and state.notified == {"mail": ["failed"]}
    assert dict(os.environ) == before
    assert not {"ONLY_IN_THE_FILE", "AL_NOTIFY_TO", "AL_NOTIFY_FROM", "AL_NOTIFY_SMTP_URL"} & set(os.environ)


# ------------------------------------------------------------------------------------ the SMTP URL

def test_parse_smtp_url_forms_and_defaults():
    parse = notify.parse_smtp_url
    assert parse("smtp://127.0.0.1", "V") == notify.SmtpUrl("smtp", "127.0.0.1", 25, "", "")
    assert parse("smtp://localhost:1025/", "V").port == 1025
    assert parse("smtp+starttls://mail.example.org", "V") == notify.SmtpUrl("smtp+starttls", "mail.example.org", 587, "", "")
    assert parse("smtp+starttls://user:secret@mail.example.org:2525", "V").port == 2525
    assert parse("smtps://mail.example.org", "V").port == 465
    url = parse("smtps://us%40er:p%40ss%3Aword@mail.example.org:4465", "V")
    assert (url.user, url.password, url.host, url.port) == ("us@er", "p@ss:word", "mail.example.org", 4465)
    assert parse("smtp://[::1]:1025", "V") == notify.SmtpUrl("smtp", "::1", 1025, "", "")
    unusable = "V is not a usable SMTP URL: "
    for text, why in (("http://mail.example.org", "unknown scheme (use smtp://, smtp+starttls:// or smtps://)"),
                      ("mail.example.org:25", "unknown scheme (use smtp://, smtp+starttls:// or smtps://)"),
                      ("smtps://", "no host"), ("smtps://user:secret@", "no host"),
                      ("smtps://mail.example.org:port", "bad port"), ("smtps://mail.example.org:", "bad port"),
                      ("smtps://mail.example.org:99999", "bad port"),
                      ("smtp://127.0.0.1:1025?x=1", "unknown option (only ?insecure=plaintext exists, for smtp://)"),
                      ("smtps://mail.example.org?insecure=plaintext", "unknown option (only ?insecure=plaintext exists, for smtp://)"),
                      ("smtps://mail.example.org/inbox", "unknown option (only ?insecure=plaintext exists, for smtp://)")):
        with pytest.raises(notify.NotifyError) as caught:
            parse(text, "V")
        assert str(caught.value) == unusable + why, text
        assert "example.org" not in str(caught.value) and "secret" not in str(caught.value)


def test_plain_smtp_to_a_non_loopback_host_is_refused_with_the_reason(monkeypatch):
    def no_connection(*_args, **_kwargs):
        raise AssertionError("a connection was attempted")
    monkeypatch.setattr(socket, "create_connection", no_connection)
    with pytest.raises(notify.NotifyError) as caught:
        notify.parse_smtp_url("smtp://mail.example.org:25", "AL_NOTIFY_SMTP_URL")
    reason = str(caught.value)
    assert reason == notify.PLAINTEXT_REFUSED
    assert "unencrypted" in reason and "?insecure=plaintext" in reason and "example.org" not in reason
    assert notify.parse_smtp_url("smtp://mail.example.org:25?insecure=plaintext", "V").host == "mail.example.org"
    for text in ("smtp://127.0.0.1:1025", "smtp://localhost:1025", "smtp://[::1]:1025", "smtp://127.9.8.7"):
        assert notify.parse_smtp_url(text, "V").scheme == "smtp"
    assert not notify.loopback("127.0.0.1.example.org") and not notify.loopback("1127.0.0.1")
    assert notify.loopback("LOCALHOST") and notify.loopback("localhost.localdomain")


def test_tls_is_verified_except_on_loopback():
    strict = notify.tls_context("mail.example.org")
    assert strict.verify_mode == ssl.CERT_REQUIRED and strict.check_hostname is True
    local = notify.tls_context("127.0.0.1")
    assert local.verify_mode == ssl.CERT_NONE and local.check_hostname is False


# ------------------------------------------------------------------------------------ event and message

HANDOVER = {"action": "handoff", "shipped": False, "reasons": ["run-stopped:test: check test exited 2 after 1 attempt(s)"],
            "stopped_at": "test", "target": "agents/e0e0a001/PR.md", "branch": "adw/e0e0a001"}


@pytest.mark.parametrize("fields, expected", [
    ({"status": "failed", "ship": HANDOVER}, "needs-human"),
    ({"status": "failed", "budget": {"stopped": {"limit": "run", "step": "build", "reason": "run limit"}}}, "needs-human"),
    ({"status": "failed", "usage": {"stopped": {"window": "five_hour", "step": "plan", "reason": "limit"}}}, "needs-human"),
    ({"status": "failed", "ship": {**HANDOVER, "handoff_error": "push refused"}}, "failed"),
    ({"status": "failed", "ship": {"handoff_skipped": "nothing changed"}}, "failed"),
    ({"status": "failed"}, "failed"),
    ({"status": "passed", "ship": {"action": "merge", "shipped": True, "target": "main"}}, "shipped"),
    ({"status": "passed", "ship": {"action": "pr", "shipped": False, "pr": {"number": 7, "url": "https://forge.invalid/pulls/7"}}}, "passed"),
    ({"status": "passed"}, "passed"),
])
def test_event_of_maps_a_finished_run_to_exactly_one_event(fields, expected):
    assert notify.event_of(ADWState(ADW_ID, "demo", **fields)) == expected
    assert expected in notify.EVENTS


def test_compose_wording_for_each_event(make_layer, tmp_path, monkeypatch):
    layer = ir.load(_layer(make_layer, [MAIL]))
    home = str(Path.home())

    def message(event=None, values=None, **fields):
        state = ADWState(ADW_ID, "demo", **{"prompt": PROMPT, **fields})
        return notify.compose(layer, state, event or notify.event_of(state), values or {})

    handed = message(status="failed", ship=HANDOVER, branch="adw/e0e0a001")
    assert handed.event == "needs-human"
    assert handed.subject == "[al] test-layer: needs a person — chore: Add a pay band to each worker"
    assert handed.body == ("Workflow: demo\nRun: e0e0a001\n\n"
                           "The run stopped at step `test` and was handed to a person. Why: check test exited 2 after 1 attempt(s)\n\n"
                           "Where to look:\n- branch: adw/e0e0a001\n")

    limit = message(status="failed", budget={"stopped": {"limit": "run", "step": "build", "reason": "run limit $5.00 crossed"}})
    assert "The run stopped at step `build` and needs a person. Why: run limit $5.00 crossed\n" in limit.body
    assert "Where to look" not in limit.body and limit.body.endswith("crossed\n")       # no line without a value

    worktree = tmp_path / "layer" / "trees" / ADW_ID
    detail = (f"lint: \n  could not read {worktree}/src/app.py   or {home}/notes.txt " + "x" * 400
              + "\nSECOND LINE OF THE DETAIL")
    failed = message(status="failed", worktree=str(worktree),
                     steps=[{"name": "plan", "status": "passed", "detail": "ok"},
                            {"name": "lint", "status": "failed", "detail": detail}])
    assert failed.subject.startswith("[al] test-layer: failed — chore: ")
    sentence = next(line for line in failed.body.splitlines() if line.startswith("The run failed"))
    reason = sentence.removeprefix("The run failed at step `lint`. Why: ")
    assert reason.startswith("could not read src/app.py or ~/notes.txt xxx") and reason.endswith("…")
    assert len(reason) <= notify.REASON_MAX
    assert "SECOND LINE" not in failed.body and str(tmp_path) not in failed.body and home not in failed.body
    nothing = message(status="failed")
    assert "The run failed at step `run`. Why: no step recorded a reason\n" in nothing.body

    waiting = message(status="passed", branch="adw/e0e0a001", ship={"action": "pr", "shipped": False, "pr": None,
                                                                    "target": "agents/e0e0a001/PR.md"})
    assert waiting.subject.startswith("[al] test-layer: finished — ")
    assert "The run finished. The work is on its branch and waits for a person.\n" in waiting.body
    pull = message(status="passed", ship={"action": "pr", "shipped": False, "pr": {"number": 7, "url": "https://forge.invalid/pulls/7"}})
    assert "The run finished. A pull request waits for a person.\n" in pull.body
    assert "- pull request: https://forge.invalid/pulls/7\n" in pull.body
    assert message(status="passed").body == "Workflow: demo\nRun: e0e0a001\n\nThe run finished.\n"
    shipped = message(status="passed", ship={"action": "merge", "shipped": True, "target": "main via PR #7"})
    assert shipped.subject.startswith("[al] test-layer: shipped — ")
    assert "The run finished and its change was shipped (main via PR #7).\n" in shipped.body

    long = message(status="passed", prompt="word " * 60)
    title = long.subject.split(" — ", 1)[1]
    assert len("word " * 60) == 300 and len(title) <= 72 and title.startswith("chore: word word") and title.endswith("…")

    assert "dashboard" not in handed.body
    with_file = message(status="passed", values={"AL_DASHBOARD_URL": "http://127.0.0.1:8765/"})
    assert with_file.body.endswith("\nWhere to look:\n- dashboard: http://127.0.0.1:8765/\n")
    monkeypatch.setenv("AL_DASHBOARD_URL", "http://127.0.0.1:9000/")
    assert "- dashboard: http://127.0.0.1:9000/\n" in message(status="passed", values={"AL_DASHBOARD_URL": "x"}).body

    pr_md = tmp_path / "layer" / "agents" / ADW_ID / "PR.md"
    pr_md.parent.mkdir(parents=True)
    pr_md.write_text("# title\n")
    assert f"- description: agents/{ADW_ID}/PR.md\n" in message(status="passed").body
    odd = message(status="passed", prompt="Ring\x07 the bell\x1b[31m\tnow")
    assert "\x07" not in odd.subject and "\x1b" not in odd.subject and "\t" not in odd.subject


# ------------------------------------------------------------------------------------ delivery

MESSAGE = notify.Message("needs-human", "[al] test-layer: needs a person — chore: a title",
                         "Workflow: demo\nRun: e0e0a001\n\nThe run stopped.\n")


@pytest.mark.parametrize("form", ["smtp", "smtp+starttls", "smtps"])
def test_each_url_form_delivers(form, make_layer, sink, tls):
    layer = ir.load(_layer(make_layer, [MAIL]))
    if form == "smtp":
        server = sink()
        url = f"smtp://127.0.0.1:{server.port}"
    else:
        if tls is None:
            pytest.skip("openssl is not on PATH")
        server = sink("starttls" if form == "smtp+starttls" else "smtps", context=tls)
        url = f"{form}://user:p%40ss%3Aword@127.0.0.1:{server.port}"
    values = {"AL_NOTIFY_TO": TO, "AL_NOTIFY_FROM": FROM, "AL_NOTIFY_SMTP_URL": url}
    result = notify.deliver(layer, MAIL, MESSAGE, values)
    assert result["accepted"] is True and result["reason"] == "", result
    (got,) = server.messages
    assert got["tls"] is (form != "smtp")
    assert got["auth"] == (None if form == "smtp" else ("user", "p@ss:word"))
    assert (got["from"], got["to"], got["helo"]) == ("al@example.org", ["ops@example.org", "lead@example.org"], "localhost")
    parsed = _parsed(got)
    assert parsed["Subject"] == MESSAGE.subject and _text(parsed) == MESSAGE.body
    if form == "smtp":
        login = {**values, "AL_NOTIFY_SMTP_URL": f"smtp://user:secret@127.0.0.1:{server.port}"}
        assert notify.deliver(layer, MAIL, MESSAGE, login)["accepted"] is True
        assert server.messages[-1]["auth"] == ("user", "secret") and server.messages[-1]["tls"] is False
        downgrade = {**values, "AL_NOTIFY_SMTP_URL": f"smtp+starttls://127.0.0.1:{server.port}"}
        refused = notify.deliver(layer, MAIL, MESSAGE, downgrade)
        assert (refused["accepted"], refused["reason"]) == (False, "the server does not offer STARTTLS")
        assert len(server.messages) == 2                      # never sent in plain instead


def test_the_command_kind_gets_the_message_on_stdin(make_layer, tmp_path):
    hook = {"id": "hook", "kind": "command", "status": "stable", "events": ["needs-human"], "timeout": 5,
            "run": "cat > \"$OUT_FILE\"; printf %s \"$AL_NOTIFY_EVENT\" > \"$OUT_FILE.event\""}
    layer = _layer(make_layer, [hook], failing="check")
    out = tmp_path / "received.txt"
    _local_env(layer, OUT_FILE=str(out))
    assert _run(layer) == 1
    subject, blank, *body = out.read_text().splitlines()
    assert subject == "[al] test-layer: needs a person — chore: Add a pay band to each worker" and blank == ""
    assert body[:2] == ["Workflow: demo", f"Run: {ADW_ID}"] and any("handed to a person" in line for line in body)
    assert Path(f"{out}.event").read_text() == "needs-human"
    (sent,) = _notify_events(layer)
    assert (sent["type"], sent["notifier"], sent["kind"], sent["event"]) == ("notify.sent", "hook", "command", "needs-human")
    assert "OUT_FILE" not in os.environ


def test_test_message_goes_through_every_notifier_and_is_marked(make_layer, sink):
    server = sink()
    hook = {"id": "hook", "kind": "command", "status": "stable", "events": ["shipped"], "timeout": 5, "run": "exit 3"}
    layer_dir = _layer(make_layer, [{**MAIL, "events": ["needs-human"]}, hook])
    _mail_env(layer_dir, server)
    mail, command = notify.test(ir.load(layer_dir), "shipped")
    assert (mail["id"], mail["kind"], mail["accepted"], mail["reports"], mail["reason"]) == ("mail", "email", True, False, "")
    assert (command["id"], command["kind"], command["accepted"], command["reports"], command["reason"]) == (
        "hook", "command", False, True, "exit 3")
    parsed = _parsed(server.messages[0])
    assert "TEST, shipped" in parsed["Subject"] and "al notify-test" in parsed["Subject"]
    body = _text(parsed)
    assert body.startswith("This is a test message") and "Workflow: (none)" in body and "event `shipped`" in body
    assert not (layer_dir / "agents").exists()                # no run directory, no state, no events


# ------------------------------------------------------------------------------------ through a run

def test_a_handover_sends_one_short_safe_email(make_layer, sink, tmp_path, monkeypatch, capsys):
    server = sink()
    layer = _layer(make_layer, [MAIL], failing="check")
    _mail_env(layer, server, AL_DASHBOARD_URL="http://127.0.0.1:8765/")
    monkeypatch.setenv("AL_CANARY", "CANARY-ENVIRONMENT-VALUE")
    assert _run(layer) == 1
    state = _state(layer)
    assert state["status"] == "failed" and state["ship"]["action"] == "handoff"
    (got,) = server.messages
    assert (got["from"], got["to"], got["helo"]) == ("al@example.org", ["ops@example.org", "lead@example.org"], "localhost")
    parsed = _parsed(got)
    heading = (layer / "agents" / ADW_ID / "PR.md").read_text().splitlines()[0]
    assert parsed["Subject"] == "[al] test-layer: needs a person — chore: Add a pay band to each worker"
    assert parsed["Subject"].split(" — ", 1)[1] == heading.removeprefix("# [needs human] ")
    assert parsed["Auto-Submitted"] == "auto-generated" and parsed["Message-ID"].endswith("@example.org>")
    assert parsed["From"] == "al@example.org" and parsed["Date"].endswith("-0000")
    body = _text(parsed)
    for needle in ("Workflow: demo\n", f"Run: {ADW_ID}\n",
                   "stopped at step `test` and was handed to a person. Why: check test exited",
                   "Where to look:\n- branch: ", f"- description: agents/{ADW_ID}/PR.md\n",
                   "- dashboard: http://127.0.0.1:8765/\n"):
        assert needle in body, needle
    assert len(body.splitlines()) <= 12
    whole = got["data"].decode("utf-8", "replace") + "\n" + parsed["Subject"] + "\n" + body
    for forbidden in ("SECOND-LINE-MARKER", "LOG-TAIL-MARKER", str(tmp_path), str(Path.home()), socket.gethostname(),
                      "CANARY-ENVIRONMENT-VALUE"):
        assert forbidden not in whole, forbidden
    (sent,) = _notify_events(layer)
    assert (sent["type"], sent["notifier"], sent["kind"], sent["event"]) == ("notify.sent", "mail", "email", "needs-human")
    assert "example.org" not in json.dumps(sent) and "127.0.0.1" not in json.dumps(sent)
    assert _events(layer)[-1]["type"] == "run.end" and state["notified"] == {"mail": ["needs-human"]}
    samples = os.environ.get("AL_NOTIFY_SAMPLES")
    if samples:
        Path(samples).mkdir(parents=True, exist_ok=True)
        (Path(samples) / "needs-human.eml").write_bytes(got["data"])

    quiet = _layer(make_layer, None, failing="check")          # the same run without a notifier ends the same way
    (quiet / "local.env").unlink()
    assert _run(quiet, adw_id="e0e0a002") == 1


def test_events_select_what_is_sent(make_layer, sink, tmp_path):
    server = sink()
    hook = {"id": "hook", "kind": "command", "status": "stable", "events": ["needs-human"], "timeout": 5,
            "run": "echo sent >> \"$HOOK_FILE\""}
    layer = _layer(make_layer, [{**MAIL, "events": ["passed"]}, hook])
    _mail_env(layer, server, HOOK_FILE=str(tmp_path / "hook.txt"))
    assert _run(layer) == 0
    (got,) = server.messages
    assert _parsed(got)["Subject"] == "[al] test-layer: finished — chore: Add a pay band to each worker"
    assert "The run finished. The work is on its branch and waits for a person." in _text(_parsed(got))
    assert not (tmp_path / "hook.txt").exists()
    assert [event["notifier"] for event in _notify_events(layer)] == ["mail"]

    no_ship = [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
               {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
               {"name": "test", "kind": "check", "cmd": "test", "retries": 0, "collect": []}]
    layer = _layer(make_layer, [{**MAIL, "events": ["failed"]}, hook], steps_=no_ship, failing="check")
    assert _run(layer, adw_id="e0e0a003") == 1
    assert len(server.messages) == 2
    parsed = _parsed(server.messages[1])
    assert parsed["Subject"] == "[al] test-layer: failed — chore: Add a pay band to each worker"
    assert "The run failed at step `test`. Why: check test exited 2" in _text(parsed)
    assert "LOG-TAIL-MARKER" not in _text(parsed)
    assert not (tmp_path / "hook.txt").exists()
    assert _state(layer, "e0e0a003")["notified"] == {"mail": ["failed"]}


def test_once_per_run_per_event_per_notifier(make_layer, sink, tmp_path):
    server = sink()
    hook = {"id": "hook", "kind": "command", "status": "stable", "events": ["needs-human"], "timeout": 5,
            "run": "echo sent >> \"$HOOK_FILE\""}
    layer = _layer(make_layer, [MAIL, hook], failing="check")
    _mail_env(layer, server, HOOK_FILE=str(tmp_path / "hook.txt"))
    assert _run(layer, "--keep-worktree") == 1                # kept, so that the resumed run gets as far again
    assert len(server.messages) == 1 and (tmp_path / "hook.txt").read_text() == "sent\n"
    assert _state(layer)["notified"] == {"mail": ["needs-human"], "hook": ["needs-human"]}
    assert len(_notify_events(layer)) == 2                    # report ran at the handover and at the end
    assert main("demo", layer, ["--adw-id", ADW_ID, "--resume", "--keep-worktree"]) == 1   # stops again
    assert _state(layer)["status"] == "failed" and _state(layer)["ship"]["action"] == "handoff"
    assert len(server.messages) == 1 and (tmp_path / "hook.txt").read_text() == "sent\n"
    assert len(_notify_events(layer)) == 2
    assert _state(layer)["notified"] == {"mail": ["needs-human"], "hook": ["needs-human"]}


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("case", ["hang", "unset", "closed-port", "reject-rcpt", "reject-auth", "plaintext",
                                  "exit-3", "sleep"])
def test_a_failing_notifier_never_fails_or_stalls_the_run(case, make_layer, sink, tmp_path):
    good = sink()
    good_mail = {**MAIL, "id": "good", "events": ["passed"], "toEnv": "GOOD_TO", "fromEnv": "GOOD_FROM",
                 "smtpUrlEnv": "GOOD_URL"}
    values = {"GOOD_TO": TO, "GOOD_FROM": FROM, "GOOD_URL": f"smtp://127.0.0.1:{good.port}",
              "AL_NOTIFY_TO": TO, "AL_NOTIFY_FROM": FROM}
    broken = {**MAIL, "id": "broken", "events": ["passed"]}
    pid_file = tmp_path / "pid"
    if case == "hang":
        broken["timeout"] = 1
        values["AL_NOTIFY_SMTP_URL"] = f"smtp://127.0.0.1:{sink(behaviour='hang').port}"
        expected = "timed out after 1 s"
    elif case == "unset":
        del values["AL_NOTIFY_TO"]
        values["AL_NOTIFY_SMTP_URL"] = f"smtp://127.0.0.1:{good.port}"
        expected = "variable AL_NOTIFY_TO is not set"
    elif case == "closed-port":
        values["AL_NOTIFY_SMTP_URL"] = f"smtp://127.0.0.1:{_closed_port()}"
        expected = "could not connect (ConnectionRefusedError)"
    elif case == "reject-rcpt":
        values["AL_NOTIFY_SMTP_URL"] = f"smtp://127.0.0.1:{sink(behaviour='reject-rcpt').port}"
        expected = "recipient refused (SMTP 550)"
    elif case == "reject-auth":
        values["AL_NOTIFY_SMTP_URL"] = f"smtp://user:secret@127.0.0.1:{sink(behaviour='reject-auth').port}"
        expected = "login refused (SMTP 535)"
    elif case == "plaintext":
        values["AL_NOTIFY_SMTP_URL"] = "smtp://mail.example.org:25"
        expected = notify.PLAINTEXT_REFUSED
    elif case == "exit-3":
        broken = {"id": "broken", "kind": "command", "status": "stable", "events": ["passed"], "timeout": 5, "run": "exit 3"}
        expected = "exit 3"
    else:
        broken = {"id": "broken", "kind": "command", "status": "stable", "events": ["passed"], "timeout": 1,
                  "run": "echo $$ > \"$PID_FILE\"; sleep 30; true"}
        values["PID_FILE"] = str(pid_file)
        expected = "timed out after 1 s"
    layer = _layer(make_layer, [broken, good_mail])
    _local_env(layer, **values)
    started = time.monotonic()
    assert _run(layer) == 0
    assert time.monotonic() - started < 20
    state = _state(layer)
    assert state["status"] == "passed" and state["notified"] == {"broken": ["passed"], "good": ["passed"]}
    failed, sent = _notify_events(layer)
    assert (failed["type"], failed["notifier"], failed["event"], failed["reason"]) == ("notify.failed", "broken", "passed", expected)
    assert failed["seconds"] < 3
    assert (sent["type"], sent["notifier"]) == ("notify.sent", "good") and len(good.messages) == 1
    assert _events(layer)[-1]["type"] == "run.end" and _events(layer)[-1]["status"] == "passed"
    log = (layer / "agents" / ADW_ID / "run.log").read_text()
    assert f"notify: broken ({broken['kind']}) passed: FAILED: {expected}" in log
    assert "example.org" not in log.replace(notify.PLAINTEXT_REFUSED, "") and "secret" not in log
    if case == "sleep":
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:                     # the killed group's processes are gone
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the notifier's process is still there")


def test_dry_run_sends_nothing_unless_asked(make_layer, sink):
    server = sink()
    layer = _layer(make_layer, [MAIL], failing="check")
    _mail_env(layer, server)
    assert _run(layer, "--dry-run") == 1
    assert server.messages == [] and server.sessions == [] and _notify_events(layer) == []
    assert "notify: dry run, nothing sent for needs-human (pass --notify to send)" in (layer / "agents" / ADW_ID / "run.log").read_text()
    assert _state(layer)["notified"] == {}
    assert _run(layer, "--dry-run", "--notify", adw_id="e0e0a004") == 1
    assert len(server.messages) == 1 and _state(layer, "e0e0a004")["notified"] == {"mail": ["needs-human"]}


def test_private_values_never_reach_state_events_logs_or_pr_text(make_layer, sink, capsys):
    server = sink()
    layer = _layer(make_layer, [MAIL], failing="check")
    canaries = ("canary-to@example.org", "canary-from@example.org", "CANARY-PASSWORD", "CANARY-DASH")
    _local_env(layer, AL_NOTIFY_TO="canary-to@example.org", AL_NOTIFY_FROM="Canary <canary-from@example.org>",
               AL_NOTIFY_SMTP_URL=f"smtp://user:CANARY-PASSWORD@127.0.0.1:{server.port}",
               AL_DASHBOARD_URL="http://127.0.0.1:1/CANARY-DASH")
    assert _run(layer) == 1
    (got,) = server.messages
    assert got["to"] == ["canary-to@example.org"] and got["auth"] == ("user", "CANARY-PASSWORD")
    assert "CANARY-DASH" in _text(_parsed(got))         # the recipient gets it; nothing else does
    server.close()                                             # the failure path: nobody listens any more
    assert _run(layer, adw_id="e0e0a005") == 1
    assert [event["type"] for event in _notify_events(layer, "e0e0a005")] == ["notify.failed"]
    captured = capsys.readouterr()
    texts = {"stdout": captured.out, "stderr": captured.err}
    for directory in ("agents", "artefacts", "specs"):
        for path in (layer / directory).rglob("*"):
            if path.is_file():
                texts[str(path)] = path.read_text(encoding="utf-8", errors="replace")
    assert any(name.endswith("state.json") for name in texts) and any(name.endswith("PR.md") for name in texts)
    for name, text in texts.items():
        for canary in canaries:
            assert canary not in text, (name, canary)
    assert not set(os.environ) & {"AL_NOTIFY_TO", "AL_NOTIFY_FROM", "AL_NOTIFY_SMTP_URL", "AL_DASHBOARD_URL"}


def test_a_fan_out_candidate_does_not_notify(make_layer, sink, monkeypatch):
    server = sink()
    layer = _layer(make_layer, [{**MAIL, "events": list(notify.EVENTS)}], failing="check")
    _mail_env(layer, server)
    monkeypatch.setenv("ADW_FANOUT_PARENT", "e0e0aaaa")
    assert _run(layer) == 1
    assert server.messages == [] and _notify_events(layer) == [] and _state(layer)["notified"] == {}


@pytest.mark.parametrize("notifiers", [None, [{"id": "none", "kind": "none", "status": "stable"}]])
def test_a_layer_without_notifiers_or_from_before_them_sends_nothing(notifiers, make_layer):
    layer = _layer(make_layer, notifiers, failing="check")
    assert _run(layer) == 1
    assert _state(layer)["ship"]["action"] == "handoff" and _state(layer)["notified"] == {}
    assert _notify_events(layer) == [] and "notify" not in (layer / "agents" / ADW_ID / "run.log").read_text()
    passing = _layer(make_layer, notifiers)
    assert _run(passing, adw_id="e0e0a006") == 0 and _state(passing, "e0e0a006")["notified"] == {}
