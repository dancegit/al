"""Answers to the stand-in decider's suggestion (decide.py): parsing a person's free text, the checks
of `decide.answer` (status, code, sender), replies read over IMAP (a fake imaplib class), the
cron trigger's poll, the webhook payload, and the notification that carries the suggestion. Git is
real, the agent is the mock, the SMTP server is smtp_sink.py on 127.0.0.1. Every address and
password is a placeholder."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import importlib.util
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(RUNTIME / "triggers"))

from adw_modules import decide, ir, notify  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402
from test_decide import (ADW_ID, DATA, FROM, TO, _body, _decider, _layer, _mail_env, _parsed, _run,  # noqa: E402,F401
                         _state, no_settings_of_the_person, sink, spawned)


def load(rel: str):
    path = RUNTIME / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


webhook = load("triggers/trigger_webhook.py")
cron = load("triggers/trigger_cron.py")


def _suggested(make_layer, tmp_path, sink, adw_id: str = ADW_ID) -> tuple[Path, dict]:
    """A run that stopped at `test` in suggest mode, with a resume-with suggestion waiting."""
    layer = _layer(make_layer, tmp_path)
    _mail_env(layer, sink)
    _decider(layer, "decide-resume-with")
    assert _run(layer, "--decider", "suggest", adw_id=adw_id) == 1
    state = _state(layer, adw_id)
    assert state["decision"]["status"] == "suggested"
    return layer, state


# ------------------------------------------------------------------------------------ parsing and checks

def test_parse_answer_forms():
    for text in ("go", "Go ahead", "yes", "OK.", "  go\n\n> quoted reject"):
        assert decide.parse_answer(text) == ("go", ""), text
    assert decide.parse_answer("yes but use the v2 API") == ("alter", "use the v2 API")
    assert decide.parse_answer("Yes, but with these alterations: keep the old column") == ("alter", "keep the old column")
    reply = ("alter: use the v2 API\nand keep the seed data small.\n\nOn Mon, 16 Nov 2026, al wrote:\n"
             "> Decision (stand-in for a person):\n> - action: resume")
    assert decide.parse_answer(reply) == ("alter", "use the v2 API\nand keep the seed data small.")
    assert decide.parse_answer("alter:\nfirst line\n-- \nOps team") == ("alter", "first line")
    assert decide.parse_answer("Explain more") == ("explain", "")
    assert decide.parse_answer("explain") == ("explain", "")
    assert decide.parse_answer("no") == decide.parse_answer("Reject") == ("reject", "")
    assert decide.parse_answer("maybe") == decide.parse_answer("") == decide.parse_answer("> go") == ("", "")
    assert decide.parse_answer("alternatively, go") == ("", "")


def test_answer_refuses_wrong_status_code_and_sender(make_layer, tmp_path, sink, spawned, monkeypatch):
    layer, state = _suggested(make_layer, tmp_path, sink)
    loaded = ir.load(layer)
    before = (layer / "agents" / ADW_ID / "state.json").read_text()
    code = state["decision"]["code"]
    refusals = [
        (decide.answer(loaded, "0badc0de", "go", "", via="cli"), "unknown-run"),
        (decide.answer(loaded, "not-a-run", "go", "", via="cli"), "unknown-run"),
        (decide.answer(loaded, ADW_ID, "merge", "", via="cli"), "bad-answer"),
        (decide.answer(loaded, ADW_ID, "alter", "  ", via="cli"), "bad-answer"),
        (decide.answer(loaded, ADW_ID, "go", "", via="webhook", code="wrong"), "bad-code"),
        (decide.answer(loaded, ADW_ID, "go", "", via="webhook"), "bad-code"),
        (decide.answer(loaded, ADW_ID, "go", "", via="mail", code="wrong", sender="ops@example.org"), "bad-code"),
        (decide.answer(loaded, ADW_ID, "go", "", via="mail", code=code, sender="evil@example.net"), "bad-sender"),
    ]
    for result, why in refusals:
        assert (result["status"], result["reason"]) == ("refused", why), result
        assert "example" not in result["message"]
    assert (layer / "agents" / ADW_ID / "state.json").read_text() == before and spawned == []
    assert decide.answer(loaded, ADW_ID, "go", "", via="mail", code=code, sender="OPS@example.org")["status"] == "resuming"
    again = decide.answer(loaded, ADW_ID, "go", "", via="cli")       # an already applied suggestion
    assert (again["status"], again["reason"]) == ("refused", "not-waiting") and "resuming" in again["message"]
    assert len(spawned) == 1
    assert _state(layer)["decision"]["answers"][0]["via"] == "mail"

    unanswered = _layer(make_layer, tmp_path / "plain")
    _mail_env(unanswered, sink)
    assert _run(unanswered, adw_id="0ff0ff01") == 1                # the decider is off: no suggestion
    result = decide.answer(ir.load(unanswered), "0ff0ff01", "go", "", via="cli")
    assert (result["status"], result["reason"]) == ("refused", "not-waiting") and "none" in result["message"]


def test_imap_url_rules():
    url = decide.parse_imap_url("imaps://u%40x:p%3Ass@imap.example.org/INBOX")
    assert (url.scheme, url.host, url.port, url.user, url.password, url.mailbox) == \
        ("imaps", "imap.example.org", 993, "u@x", "p:ss", "INBOX")
    assert decide.parse_imap_url("imap://u:p@127.0.0.1:1143").port == 1143
    assert decide.parse_imap_url("imaps://u:p@imap.example.org").mailbox == "INBOX"
    for text, needle in (("imap://u:secret@imap.example.org/INBOX", "refused: plain imap://"),
                         ("pop3://u:secret@imap.example.org", "unknown scheme"),
                         ("imaps://imap.example.org", "no user"),
                         ("imaps://u:secret@imap.example.org:99999", "bad port"),
                         ("imaps://u:secret@/INBOX", "no host"),
                         ("imaps://u:secret@imap.example.org/?x=1", "unknown option")):
        with pytest.raises(decide.MailError) as caught:
            decide.parse_imap_url(text)
        reason = str(caught.value)
        assert needle in reason, (text, reason)
        assert "secret" not in reason and "example.org" not in reason


def test_compose_carries_the_suggestion_and_the_answer_token(make_layer, tmp_path):
    layer = ir.load(_layer(make_layer, tmp_path))
    state = ADWState(adw_id=ADW_ID, adw="demo", prompt="Add a pay band to each worker.", status="failed",
                     ship={"action": "handoff", "stopped_at": "test", "target": f"agents/{ADW_ID}/PR.md",
                           "reasons": ["run-stopped:test: check test exited 1"]})
    state.decision = {"mode": "suggest", "action": "resume-with", "from_step": "build", "status": "suggested",
                      "summary": f"Build again; see {layer.layer_dir}/specs/plan.md.", "code": "Abc_123-xyz9",
                      "explanation": "long", "instructions": "x", "answers": []}
    message = notify.compose(layer, state, "needs-human", {})
    assert message.subject.endswith(f"[decide {ADW_ID}-Abc_123-xyz9]")
    for needle in ("Decision (stand-in for a person):", "- mode: suggest", "- action: resume-with",
                   "- from step: build", "- summary: Build again; see specs/plan.md.", "- go: ", "- alter: ", "- explain: ",
                   "- reject: ", f"al answer LAYER {ADW_ID} go"):
        assert needle in message.body, needle
    assert str(tmp_path) not in message.body
    state.decision.update(mode="auto", status="decided", action="abandon")
    auto = notify.compose(layer, state, "needs-human", {})
    assert "[decide " not in auto.subject and "- action: abandon" in auto.body and "al answer" not in auto.body


def test_event_of_is_silent_while_the_decider_resumes():
    state = SimpleNamespace(status="failed", ship={"action": "handoff"}, budget={}, usage={},
                            decision={"status": "resuming"})
    assert notify.event_of(state) == ""
    for status in ("suggested", "decided", "resumed", "rejected", "failed"):
        state.decision = {"status": status}
        assert notify.event_of(state) == "needs-human"


# ------------------------------------------------------------------------------------ replies over IMAP

class FakeImap:
    """Stands in for imaplib.IMAP4_SSL: serves `messages` ({number: raw bytes}), records \\Seen."""
    messages: dict[bytes, bytes] = {}
    seen: set[bytes] = set()
    log: list = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        FakeImap.log.append(("connect", host, port, timeout))

    def login(self, user, password):
        FakeImap.log.append(("login", user, password))
        return "OK", [b"logged in"]

    def select(self, mailbox):
        FakeImap.log.append(("select", mailbox))
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, criterion):
        assert criterion == "UNSEEN"
        return "OK", [b" ".join(n for n in sorted(self.messages) if n not in self.seen)]

    def fetch(self, number, what):
        assert what == "(BODY.PEEK[])"
        raw = self.messages[number]
        return "OK", [(number + b" (BODY[] {" + str(len(raw)).encode() + b"}", raw), b")"]

    def store(self, number, op, flag):
        assert (op, flag) == ("+FLAGS", "\\Seen")
        FakeImap.seen.add(number)
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def imap(monkeypatch):
    FakeImap.messages, FakeImap.seen, FakeImap.log = {}, set(), []
    monkeypatch.setattr(decide.imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def _reply(run: str, code: str, sender: str = "Ops <ops@example.org>") -> bytes:
    text = (DATA / "reply-go.eml").read_text().replace("{RUN}", run).replace("{CODE}", code)
    return text.replace("From: Ops <ops@example.org>", f"From: {sender}").encode()


UNRELATED = (b"From: someone@example.org\r\nTo: al@example.org\r\nSubject: lunch?\r\n\r\ngo\r\n")


def test_poll_mail_applies_a_reply_and_marks_only_matched_mail_seen(make_layer, tmp_path, sink, spawned, imap):
    layer, state = _suggested(make_layer, tmp_path, sink)
    with (layer / "local.env").open("a") as handle:
        handle.write("AL_DECIDE_IMAP_URL=imaps://al%40example.org:secret@imap.example.org/INBOX\n")
    imap.messages = {b"1": UNRELATED, b"2": _reply(ADW_ID, state["decision"]["code"])}
    said: list[str] = []
    assert decide.poll_mail(ir.load(layer), out=said.append) == 1
    assert imap.seen == {b"2"}
    assert ("login", "al@example.org", "secret") in imap.log and ("select", "INBOX") in imap.log
    d = _state(layer)["decision"]
    assert d["status"] == "resuming" and d["answers"][-1]["kind"] == "go" and d["answers"][-1]["via"] == "mail"
    assert len(spawned) == 1
    assert said == [f"decide mail: run {ADW_ID}: go: resuming (from step build)"]
    assert decide.poll_mail(ir.load(layer), out=said.append) == 0        # nothing unseen is an answer any more


def test_poll_mail_ignores_a_forged_sender_and_a_wrong_code(make_layer, tmp_path, sink, spawned, imap):
    layer, state = _suggested(make_layer, tmp_path, sink)
    with (layer / "local.env").open("a") as handle:
        handle.write("AL_DECIDE_IMAP_URL=imaps://al%40example.org:secret@imap.example.org/INBOX\n")
    code = state["decision"]["code"]
    imap.messages = {b"1": _reply(ADW_ID, code, sender="Evil <evil@example.net>"),
                     b"2": _reply(ADW_ID, "WrongCode123")}
    said: list[str] = []
    assert decide.poll_mail(ir.load(layer), out=said.append) == 0
    assert _state(layer)["decision"]["status"] == "suggested" and spawned == []
    assert len(said) == 2 and "sender is not a recipient" in said[0] and "code does not match" in said[1]
    for line in said:
        assert "@" not in line and "example" not in line and "secret" not in line and code not in line
    (layer / "local.env").write_text((layer / "local.env").read_text().replace("imaps://", "imap://"))
    said.clear()
    assert decide.poll_mail(ir.load(layer), out=said.append) == 0
    assert said == ["decide mail: refused: plain imap:// to a host that is not this machine would send the "
                    "password unencrypted; use imaps://"]


def test_cron_poll_calls_poll_mail_only_with_the_variable_set(make_layer, tmp_path, monkeypatch, capsys):
    layer_dir = _layer(make_layer, tmp_path)
    calls: list = []
    real = decide.poll_mail
    monkeypatch.setattr(decide, "poll_mail", lambda layer, out=print: calls.append(layer) or 0)
    said: list[str] = []
    scheduler = cron.Scheduler(layer_dir, "cron", launch=lambda *a: None, out=said.append)
    assert scheduler.poll_once() == 0
    assert len(calls) == 1 and Path(calls[0].layer_dir) == layer_dir
    monkeypatch.setattr(decide, "poll_mail", real)
    assert decide.poll_mail(ir.load(layer_dir), out=said.append) == 0 and said == []


# ------------------------------------------------------------------------------------ the webhook

def _signed(secret: str, body: bytes) -> dict[str, str]:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def test_webhook_decision_payload_is_answered(make_layer, tmp_path, sink, spawned, monkeypatch):
    layer, state = _suggested(make_layer, tmp_path, sink)
    loaded = ir.load(layer)
    trigger = {"id": "hook", "kind": "webhook", "events": ["payload"], "secretEnv": "HOOK_SECRET", "path": "/webhook"}
    env = {"HOOK_SECRET": "s3cret"}
    code = state["decision"]["code"]
    body = json.dumps({"decision": {"run": ADW_ID, "code": code, "answer": "go"}}).encode()
    decision = webhook.decide(loaded.ir, trigger, _signed("s3cret", body), body, env)
    assert (decision.status, decision.reason, decision.payload) == (202, "answer", {"run": ADW_ID, "code": code, "answer": "go"})
    assert webhook.decide(loaded.ir, trigger, {}, body, env).status == 401                   # unsigned
    assert webhook.decide(loaded.ir, {**trigger, "events": ["issue-opened"]}, _signed("s3cret", body), body,
                          env).reason == "generic payloads not enabled"

    monkeypatch.setenv("HOOK_SECRET", "s3cret")
    app = webhook.App(loaded, trigger)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def post(payload: dict) -> tuple[int, dict]:
        raw = json.dumps(payload).encode()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            connection.request("POST", "/webhook", body=raw, headers=_signed("s3cret", raw))
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        before = (layer / "agents" / ADW_ID / "state.json").read_text()
        status, reply = post({"decision": {"run": ADW_ID, "code": "wrong", "answer": "go"}})
        assert status == 409 and "code does not match" in reply["error"]
        assert (layer / "agents" / ADW_ID / "state.json").read_text() == before and spawned == []
        status, reply = post({"decision": {"run": ADW_ID, "code": code}})
        assert status == 400
        status, reply = post({"decision": {"run": ADW_ID, "code": code, "answer": "alter: keep the seed data small"}})
        assert status == 200 and reply["status"] == "resuming", reply
    finally:
        server.shutdown()
        server.server_close()
    d = _state(layer)["decision"]
    assert d["answers"][-1] == {**d["answers"][-1], "kind": "alter", "via": "webhook", "text": "keep the seed data small"}
    assert "The person adds: keep the seed data small" in _state(layer)["prompt"] and len(spawned) == 1
