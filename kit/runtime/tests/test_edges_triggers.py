import copy
import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(RUNTIME / "triggers"))

from adw_modules import inputs, tasklist as tl  # noqa: E402


def load(rel):
    path = RUNTIME / rel
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


webhook = load("triggers/trigger_webhook.py")
cron = load("triggers/trigger_cron.py")
watch = load("triggers/trigger_watch.py")
voice = load("triggers/trigger_voice.py")
listen = load("voice/listen.py")
say_mod = load("voice/say.py")

FULL = json.loads((Path(__file__).parent / "full.layer.json").read_text())


def full_ir():
    ir = copy.deepcopy(FULL)
    ir["promptInputs"].append({"id": "webhook-payload", "kind": "webhook-payload", "promptField": "prompt",
                               "workflowField": "workflow", "status": "stable"})
    ir["adws"][0]["input"].append("webhook-payload")
    ir["triggers"][0]["events"] = ["issue-opened", "issue-comment", "payload"]
    return ir


TRIG = {"id": "webhook", "kind": "webhook", "secretEnv": "ADW_WEBHOOK_SECRET",
        "events": ["issue-opened", "issue-comment", "payload"], "path": "/webhook"}


def sign(secret, body, prefix="sha256="):
    return prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ webhook


def test_github_issue_opened():
    body = json.dumps({"action": "opened", "issue": {"number": 12, "body": "please", "labels": [{"name": "adw"}]}}).encode()
    d = webhook.decide(full_ir(), TRIG, {"X-GitHub-Event": "issues"}, body, env={})
    assert (d.status, d.adw, d.source) == (202, "plan-build", "issue:12")
    unlabelled = json.dumps({"action": "opened", "issue": {"number": 13, "body": "", "labels": []}}).encode()
    assert webhook.decide(full_ir(), TRIG, {"X-GitHub-Event": "issues"}, unlabelled, env={}).status == 200


def test_forgejo_comment_with_keyword_and_signature():
    body = json.dumps({"action": "created", "issue": {"number": 4, "labels": []},
                       "comment": {"body": "Looks small.\nadw: zte"}}).encode()
    env = {"ADW_WEBHOOK_SECRET": "s3"}
    hdrs = {"X-Forgejo-Event": "issue_comment", "X-Forgejo-Signature": sign("s3", body, prefix="")}
    d = webhook.decide(full_ir(), TRIG, hdrs, body, env)
    assert (d.status, d.adw, d.source, d.reason) == (202, "zte", "issue:4", "issue-comment")
    gitea = {"X-Gitea-Event": "issue_comment", "X-Gitea-Signature": sign("s3", body, prefix="")}
    assert webhook.decide(full_ir(), TRIG, gitea, body, env).adw == "zte"
    nokw = json.dumps({"action": "created", "issue": {"number": 4}, "comment": {"body": "thanks"}}).encode()
    assert webhook.decide(full_ir(), TRIG, {"X-Gitea-Event": "issue_comment"}, nokw, {}).status == 200


def test_bot_comment_ignored():
    body = json.dumps({"action": "created", "issue": {"number": 4},
                       "comment": {"body": "[ADW-BOT] adw: zte started"}}).encode()
    d = webhook.decide(full_ir(), TRIG, {"X-GitHub-Event": "issue_comment"}, body, {})
    assert (d.status, d.run, d.reason) == (200, False, "own bot comment")


def test_bad_signature():
    body = b'{"action": "opened", "issue": {"number": 1, "labels": [{"name": "adw"}]}}'
    env = {"ADW_WEBHOOK_SECRET": "right"}
    assert webhook.decide(full_ir(), TRIG, {"X-GitHub-Event": "issues",
                                            "X-Hub-Signature-256": sign("wrong", body)}, body, env).status == 401
    assert webhook.decide(full_ir(), TRIG, {"X-GitHub-Event": "issues"}, body, env).status == 401
    ok = webhook.decide(full_ir(), TRIG, {"x-github-event": "issues", "x-hub-signature-256": sign("right", body)}, body, env)
    assert ok.status == 202
    assert webhook.decide(full_ir(), TRIG, {}, b"not json", {}).status == 400


def test_generic_payload():
    body = json.dumps({"prompt": "Rotate keys", "workflow": "plan-build"}).encode()
    d = webhook.decide(full_ir(), TRIG, {"Content-Type": "application/json"}, body, {})
    assert (d.status, d.adw, d.source, d.input_id, d.payload["prompt"]) == \
        (202, "plan-build", None, "webhook-payload", "Rotate keys")
    off = dict(TRIG, events=["issue-opened"])
    assert webhook.decide(full_ir(), off, {}, body, {}).status == 200
    pr_comment = json.dumps({"action": "created", "issue": {"number": 2, "pull_request": {}},
                             "comment": {"body": "adw: zte"}}).encode()
    assert webhook.decide(full_ir(), TRIG, {"X-GitHub-Event": "issue_comment"}, pr_comment, {}).status == 200


def test_save_payload(tmp_path):
    p = webhook.save_payload(tmp_path, {"prompt": "x"})
    assert p.parent == tmp_path / "agents" / "payloads" and json.loads(p.read_text()) == {"prompt": "x"}


# ------------------------------------------------------------------ cron


class FakeProc:
    def __init__(self, rc):
        self.rc = rc

    def poll(self):
        return self.rc


class FakeForge:
    bot_marker = "[ADW-BOT]"

    def __init__(self):
        self.issues = [{"number": 1, "title": "A", "body": "", "labels": ["adw"]},
                       {"number": 2, "title": "B", "body": "", "labels": ["adw"]},
                       {"number": 3, "title": "C", "body": "", "labels": []}]
        self.comments = {2: [{"body": "[ADW-BOT] adw x started"}]}
        self.posted = []

    def is_bot(self, b):
        return (b or "").startswith(self.bot_marker)

    def list_issues(self, labels=()):
        return [i for i in self.issues if set(labels) <= set(i["labels"])]

    def list_comments(self, n):
        return self.comments.get(n, [])

    def comment(self, n, body):
        self.posted.append((n, body))
        self.comments.setdefault(n, []).append({"body": f"[ADW-BOT] {body}"})


def cron_layer(tmp_path):
    (tmp_path / "tasks.md").write_text(
        "## a\n- [ ] first {zte}\n- [ ] second\n## b\n- [ ] other {feature}\n## c\n- [x] done\n- [>] next\n")
    return SimpleNamespace(ir=copy.deepcopy(FULL), layer_dir=tmp_path, app_root=None)


def test_cron_poll_claims_and_launches(tmp_path):
    lay = cron_layer(tmp_path)
    launched, out = [], []

    def launch(argv, log, env):
        launched.append((argv, env.get(inputs.CLAIM_ENV)))
        return FakeProc(None)

    fake = FakeForge()
    s = cron.Scheduler(lay, "cron", max_concurrent=10, forge=fake, launch=launch, out=out.append)
    assert s.poll_once() == 4
    sources = [a[a.index("--source") + 1] for a, _ in launched]
    # one task per section; issue 2 already has a bot comment; issue 3 lacks the label
    assert sources == ["issue:1", "tasklist:2", "tasklist:5", "tasklist:8"]
    adws = [Path(a[3]).name for a, _ in launched]
    assert adws == ["adw_sdlc.py", "adw_zte.py", "adw_sdlc.py", "adw_sdlc.py"]
    assert fake.posted == [(1, "queued for adw `sdlc`")]
    text = (tmp_path / "tasks.md").read_text()
    assert text.count("[~]") == 3 and launched[1][1] in text
    # nothing is started twice on the next poll
    assert s.poll_once() == 0


def test_cron_max_concurrent_and_failures(tmp_path):
    lay = cron_layer(tmp_path)
    procs = []

    def launch(argv, log, env):
        procs.append(FakeProc(None))
        return procs[-1]

    s = cron.Scheduler(lay, "cron", max_concurrent=2, forge=FakeForge(), launch=launch, out=lambda m: None)
    assert s.poll_once() == 2
    procs[0].rc, procs[1].rc = 0, 3
    s.reap()
    assert s.failures == 1 and not s.running
    # the failed child never took over its claim -> the task is marked failed by release()
    assert "reason:adw exited 3 before taking the task" in (tmp_path / "tasks.md").read_text()


def test_cron_dry_run_claims_nothing(tmp_path):
    lay = cron_layer(tmp_path)
    before = (tmp_path / "tasks.md").read_text()
    argvs = []
    s = cron.Scheduler(lay, "cron", 10, dry_run=True, forge=FakeForge(),
                       launch=lambda a, l, e: argvs.append(a) or FakeProc(0), out=lambda m: None)
    s.poll_once()
    assert (tmp_path / "tasks.md").read_text() == before and all(a[-1] == "--dry-run" for a in argvs)


def test_cron_spec_inbox_seen(tmp_path):
    ir = copy.deepcopy(FULL)
    ir["forge"] = {"id": "none", "kind": "none", "remote": "", "status": "stable"}
    ir["promptInputs"] = [{"id": "spec-file", "kind": "spec-file", "dir": "specs/inbox", "pattern": "*.md",
                           "status": "stable"}]
    ir["adws"] = [{"name": "build-spec", "input": ["spec-file"], "trigger": ["cron"], "classes": []}]
    inbox = tmp_path / "specs" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.md").write_text("x")
    (inbox / "b.txt").write_text("x")
    lay = SimpleNamespace(ir=ir, layer_dir=tmp_path, app_root=None)
    got = []
    s = cron.Scheduler(lay, "cron", 3, launch=lambda a, l, e: got.append(a) or FakeProc(0), out=lambda m: None)
    assert s.poll_once() == 1 and got[0][-1] == f"spec-file:{inbox / 'a.md'}"
    s.reap()
    assert s.poll_once() == 0  # recorded in agents/trigger-seen.json
    assert str(inbox / "a.md") in json.loads((tmp_path / "agents" / "trigger-seen.json").read_text())["spec-file"]


# ------------------------------------------------------------------ watch


def test_debouncer():
    d = watch.Debouncer(3, {"f": (1, 1)})
    assert not d.feed(0, {"f": (1, 1)})
    assert not d.feed(1, {"f": (2, 5)})  # change seen
    assert not d.feed(2, {"f": (2, 5)})  # still within the quiet period
    assert not d.feed(3.5, {"f": (3, 6)})  # changed again: restart the period
    assert d.feed(6.6, {"f": (3, 6)})  # quiet long enough
    assert not d.feed(20, {"f": (3, 6)})  # acted on; no repeat


def test_watch_snapshot_and_targets(tmp_path):
    ir = copy.deepcopy(FULL)
    ir["triggers"].append({"id": "file-watch", "kind": "file-watch", "debounceSeconds": 1, "status": "stable"})
    ir["adws"][1]["trigger"].append("file-watch")
    lay = SimpleNamespace(ir=ir, layer_dir=tmp_path, app_root=None)
    files, dirs = watch.watched(lay, "file-watch")
    assert files == [tmp_path / "tasks.md"] and dirs == []
    assert watch.snapshot(files, dirs) == {}
    (tmp_path / "tasks.md").write_text("- [ ] x\n")
    assert list(watch.snapshot(files, dirs)) == [str(tmp_path / "tasks.md")]


# ------------------------------------------------------------------ voice routing


def test_classify():
    assert voice.classify("Add a dark mode toggle") == "task"
    assert voice.classify("okay, fix the login bug") == "task"
    assert voice.classify("Why does checkout call the tax API twice") == "question"
    assert voice.classify("How is auth wired?") == "question"
    assert voice.classify("Status please") == "summary"
    assert voice.classify("What happened with the last run") == "summary"


def test_plan_action_routes():
    ir = copy.deepcopy(FULL)
    a = voice.plan_action("Add a dark mode toggle to settings", ir, "voice")
    assert a["action"] == "append" and a["section"] == "add-a-dark-mode-toggle" and a["path"] == "tasks.md"
    assert voice.plan_action("status", ir)["action"] == "summary"
    q = voice.plan_action("Where is the cart total computed?", ir)
    assert q["action"] == "none" and "adw ask" in q["message"]
    ir["adws"].append({"name": "ask", "input": ["voice"], "trigger": ["voice"], "classes": []})
    q = voice.plan_action("Where is the cart total computed?", ir)
    assert (q["action"], q["adw"], q["source"]) == ("run", "ask", "voice")
    ir["promptInputs"][2]["appendToTasklist"] = False
    r = voice.plan_action("Fix the flaky test", ir, "voice")
    assert (r["action"], r["adw"], r["source"], r["prompt"]) == ("run", "plan-build", "voice", "Fix the flaky test")
    ir["promptInputs"][2]["route"] = "question"
    assert voice.plan_action("Fix the flaky test", ir)["adw"] == "ask"


def test_summarize_states():
    s = voice.summarize_states([{"adw_id": "1a2b3c4d5e", "adw": "sdlc", "status": "passed", "branch": "feat-x",
                                 "prompt": "Add x\nmore"}, {"status": "failed", "name": "zte"}])
    assert s == "Run 1a2b3c4d of sdlc passed on branch feat-x: Add x. zte failed."
    assert voice.summarize_states([]) == "There are no runs yet."


def test_recent_states(tmp_path):
    for i, name in enumerate(["old", "new"]):
        d = tmp_path / "agents" / name
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"adw_id": name}))
    (tmp_path / "agents" / "broken").mkdir()
    (tmp_path / "agents" / "broken" / "state.json").write_text("{")
    assert {s["adw_id"] for s in voice.recent_states(tmp_path)} == {"old", "new"}


# ------------------------------------------------------------------ voice scripts (no audio, no models)


def test_voice_layer_defaults(tmp_path):
    (tmp_path / "agentic-layer.json").write_text(json.dumps(FULL))
    assert say_mod.layer_defaults(str(tmp_path)) == {"voice": "af_heart", "lang": "en-us", "speed": 1.0}
    assert listen.layer_defaults(str(tmp_path)) == {"model": "base.en", "device": "auto", "seconds": 8}
    assert say_mod.layer_defaults(None) == {} and listen.layer_defaults(str(tmp_path / "nope")) == {}


def test_model_download_resumes_after_stall(tmp_path, monkeypatch):
    import io

    blob = bytes(range(256)) * 40  # 10240 bytes
    seen_ranges = []

    class Resp(io.BytesIO):
        def __init__(self, data, status, headers):
            super().__init__(data)
            self.status, self.headers = status, headers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if self.stall_at is not None and self.tell() >= self.stall_at:
                raise TimeoutError("The read operation timed out")
            return super().read(min(n, 1000) if n and n > 0 else 1000)

    def fake_urlopen(req, timeout=None):
        rng = req.headers.get("Range")
        seen_ranges.append(rng)
        start = int(rng[6:-1]) if rng else 0
        r = Resp(blob[start:], 206 if rng else 200,
                 {"Content-Range": f"bytes {start}-{len(blob) - 1}/{len(blob)}"} if rng
                 else {"Content-Length": str(len(blob))})
        r.stall_at = 4000 if not rng else None  # first connection dies part-way
        return r

    monkeypatch.setattr(say_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(say_mod.time, "sleep", lambda s: None)
    dest = tmp_path / "model.onnx"
    say_mod.download("https://example.invalid/model.onnx", dest)
    assert dest.read_bytes() == blob and not (tmp_path / "model.onnx.part").exists()
    assert seen_ranges == [None, "bytes=4000-"]


def test_record_command_preference(tmp_path, monkeypatch):
    have = {"arecord", "ffmpeg"}
    monkeypatch.setattr(listen.shutil, "which", lambda c: f"/usr/bin/{c}" if c in have else None)
    argv, kill = listen.record_command(tmp_path / "r.wav", 5)
    assert argv[0] == "arecord" and "-d" in argv and not kill
    have.add("pw-record")
    argv, kill = listen.record_command(tmp_path / "r.wav", 5)
    assert argv[:3] == ["pw-record", "--rate", "16000"] and kill
    have.clear()
    assert listen.record_command(tmp_path / "r.wav", 5) is None
