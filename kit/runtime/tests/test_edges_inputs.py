import copy
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME))

from adw_modules import inputs, tasklist as tl  # noqa: E402

FULL = json.loads((Path(__file__).parent / "full.layer.json").read_text())


def make_layer(tmp_path, mutate=None):
    ir = copy.deepcopy(FULL)
    ir["promptInputs"] += [
        {"id": "spec-file", "kind": "spec-file", "dir": "specs/inbox", "pattern": "*.md", "status": "stable"},
        {"id": "webhook-payload", "kind": "webhook-payload", "promptField": "data.text",
         "workflowField": "data.wf", "status": "stable"},
        {"id": "cli", "kind": "cli", "allowStdin": True, "status": "stable"},
    ]
    ir["adws"][0]["input"] += ["spec-file", "webhook-payload", "cli"]
    if mutate:
        mutate(ir)
    return SimpleNamespace(ir=ir, layer_dir=tmp_path, app_root=tmp_path / "app")


def state(adw_id="adw00001", status="passed", **kw):
    return SimpleNamespace(adw_id=adw_id, status=status, ship=kw.get("ship", {}), branch=kw.get("branch"))


class FakeForge:
    bot_marker = "[ADW-BOT]"

    def __init__(self, issues=None, comments=None):
        self.issues = issues or {}
        self.comments = comments or {}
        self.posted = []

    def is_bot(self, body):
        return (body or "").lstrip().startswith(self.bot_marker)

    def get_issue(self, n):
        return self.issues[n]

    def list_issues(self, labels=()):
        return [i for i in self.issues.values() if set(labels) <= set(i["labels"])]

    def list_comments(self, n):
        return self.comments.get(n, [])

    def comment(self, n, body):
        self.posted.append((n, body))
        self.comments.setdefault(n, []).append({"id": 0, "body": f"{self.bot_marker} {body}", "user": "bot"})


def test_cli_argument_and_stdin(tmp_path):
    lay = make_layer(tmp_path)
    r = inputs.resolve(lay, None, "  Fix the bug  ")
    assert r.prompt == "Fix the bug" and r.source_kind == "cli" and r.workflow is None
    r = inputs.resolve(lay, "cli", None, stdin=io.StringIO("from stdin\n"))
    assert r.prompt == "from stdin"
    r.on_start(state()), r.on_finish(state())  # no-ops
    with pytest.raises(inputs.InputError):
        inputs.resolve(lay, "cli", None, stdin=io.StringIO(""))


def test_tasklist_source_claims_and_completes(tmp_path):
    lay = make_layer(tmp_path)
    (tmp_path / "tasks.md").write_text("## fix-login\n- [ ] Null check in refresh {zte, class: bug, heavy}\n")
    r = inputs.resolve(lay, "tasklist:2", None, env={})
    assert (r.prompt, r.klass, r.model_set, r.workflow) == ("Null check in refresh", "bug", "heavy", "zte")
    assert inputs.pick_adw(lay, r) == "zte"
    r.on_start(state("a1b2c3d4"))
    assert "[~] Null check in refresh {zte, class: bug, heavy} <!-- adw:a1b2c3d4 -->" in (tmp_path / "tasks.md").read_text()
    r.on_finish(state("a1b2c3d4", ship={"commit": "9f8e7d6"}))
    assert "- [x] Null check in refresh {zte, class: bug, heavy} <!-- adw:a1b2c3d4 commit:9f8e7d6 -->" in \
        (tmp_path / "tasks.md").read_text()


def test_tasklist_source_fail_and_trigger_handover(tmp_path):
    lay = make_layer(tmp_path)
    p = tmp_path / "tasks.md"
    p.write_text("- [ ] Do it\n")
    tl.claim(p, tl.parse(p.read_text())[0], "trg-0001")
    with pytest.raises(inputs.InputError, match="already running"):
        inputs.resolve(lay, "tasklist:1", None, env={})
    r = inputs.resolve(lay, "tasklist:1", None, env={inputs.CLAIM_ENV: "trg-0001"})
    r.on_start(state("real0001"))
    assert "adw:real0001" in p.read_text()
    r.on_finish(state("real0001", status="failed"))
    assert p.read_text().startswith("- [!] Do it <!-- adw:real0001 reason:adw failed -->")
    with pytest.raises(inputs.InputError, match="not eligible"):
        inputs.resolve(lay, "tasklist:1", None, env={})


def test_issue_source_keyword_and_comments(tmp_path):
    lay = make_layer(tmp_path)
    fake = FakeForge(
        issues={7: {"number": 7, "title": "Login breaks", "body": "Steps...\nadw: sdlc", "labels": ["adw", "bug"],
                    "state": "open"}},
        comments={7: [{"body": "adw: zte heavy", "user": "per"}, {"body": "[ADW-BOT] noise", "user": "bot"}]})
    r = inputs.resolve(lay, "issue:7", None, forge=fake)
    assert r.prompt == "Login breaks\n\nSteps...\nadw: sdlc"
    assert (r.workflow, r.model_set, r.klass) == ("zte", "heavy", "bug")  # latest non-bot comment wins
    r.on_start(state("abcd1234"))
    r.on_finish(state("abcd1234", branch="bug-7-login", ship={"url": "https://f/pulls/3"}))
    assert fake.posted[0] == (7, "adw abcd1234 started")
    assert "passed" in fake.posted[1][1] and "bug-7-login" in fake.posted[1][1] and "https://f/pulls/3" in fake.posted[1][1]
    fake.comments[7] = []
    r = inputs.resolve(lay, "issue:7", None, forge=fake)
    assert (r.workflow, r.model_set) == ("sdlc", None)  # body keyword when no human comment
    assert inputs.pick_adw(lay, r) == "sdlc"


def test_spec_file_source(tmp_path):
    lay = make_layer(tmp_path)
    spec = tmp_path / "specs" / "inbox" / "add-x.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Spec\n")
    r = inputs.resolve(lay, "spec-file:specs/inbox/add-x.md", None)
    assert r.prompt == f"Implement the spec at {spec.resolve()}" and r.plan == str(spec.resolve())
    assert inputs.resolve(lay, "spec-file:add-x.md", None).plan == str(spec.resolve())
    with pytest.raises(inputs.InputError):
        inputs.resolve(lay, "spec-file:missing.md", None)


def test_payload_source(tmp_path):
    lay = make_layer(tmp_path)
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"data": {"text": "Rotate the logs", "wf": "plan-build"}}))
    r = inputs.resolve(lay, f"payload:{f}", None)
    assert (r.prompt, r.workflow, r.input_id) == ("Rotate the logs", "plan-build", "webhook-payload")
    assert inputs.pick_adw(lay, r) == "plan-build"
    f.write_text(json.dumps({"data": {}}))
    with pytest.raises(inputs.InputError, match="no prompt"):
        inputs.resolve(lay, f"payload:{f}", None)


def test_voice_source_uses_listen(tmp_path):
    lay = make_layer(tmp_path)
    r = inputs.resolve(lay, "voice", None, listen=lambda d: "  add a dark mode toggle ")
    assert r.prompt == "add a dark mode toggle" and r.input_id == "voice"
    r = inputs.resolve(lay, "voice", "already transcribed", listen=lambda d: pytest.fail("must not record"))
    assert r.prompt == "already transcribed"


def test_pick_adw_rules(tmp_path):
    lay = make_layer(tmp_path)
    R = inputs.Request
    assert inputs.pick_adw(lay, R("x", input_id="tasklist")) == "plan-build"
    assert inputs.pick_adw(lay, R("x", input_id="tasklist", workflow="zte")) == "zte"
    # explicit workflow that does not accept the input falls back to the first match
    assert inputs.pick_adw(lay, R("x", input_id="voice", workflow="zte")) == "plan-build"
    no_pb = make_layer(tmp_path, lambda ir: ir["adws"].pop(0))
    assert inputs.pick_adw(no_pb, R("x", input_id="issue", klass="feature")) == "sdlc"
    zte_first = make_layer(tmp_path, lambda ir: (ir["adws"].pop(0), ir["adws"].reverse()))
    assert inputs.pick_adw(zte_first, R("x", input_id="issue", klass="chore")) == "zte"
    assert inputs.pick_adw(zte_first, R("x", input_id="issue", klass="feature")) == "sdlc"
    with pytest.raises(inputs.InputError):
        inputs.pick_adw(lay, R("x", input_id="nope"))


def test_unknown_source_kind(tmp_path):
    with pytest.raises(inputs.InputError, match="no prompt-input"):
        inputs.resolve(make_layer(tmp_path), "notion:1", None)


def test_keyword_selection():
    assert inputs.keyword_selection("hi\n  ADW: zte base \nbye", "adw") == ("zte", "base")
    assert inputs.keyword_selection("see adw: zte", "adw") == (None, None)
