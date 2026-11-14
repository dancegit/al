#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PITER · T — webhook trigger (GitHub, Forgejo/Gitea, or any JSON sender).

    trigger_webhook.py [--layer DIR] [--trigger ID] [--once] [--dry-run]

GET /health -> 200. POST <path> -> signature check (when the secret env var is
set), then `decide()` maps the request to (adw, source); accepted requests get
202 at once and the ADW runs in a background subprocess. --once serves until
one run has been accepted and finished, then exits with its status.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import inputs  # noqa: E402
from adw_modules.forge import DEFAULT_BOT_MARKER, as_layer  # noqa: E402

DEFAULT_LAYER = Path(__file__).resolve().parent.parent.parent
SIGNATURE_HEADERS = ("x-hub-signature-256", "x-forgejo-signature", "x-gitea-signature")
EVENT_HEADERS = ("x-github-event", "x-forgejo-event", "x-gitea-event")


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class Decision:
    status: int  # HTTP status to answer with
    reason: str
    adw: str | None = None
    source: str | None = None  # None + payload set => save payload, then source payload:<file>
    payload: Any = None
    input_id: str | None = None

    @property
    def run(self) -> bool:
        return self.status == 202


def verify_signature(secret: str, headers: Mapping[str, str], body: bytes) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    for name in SIGNATURE_HEADERS:
        got = (headers.get(name) or "").strip()
        if got.startswith("sha256="):
            got = got[7:]
        if got and hmac.compare_digest(got.lower(), expected):
            return True
    return False


def _issue_event(event: str, action: str) -> str | None:
    if event == "issues" and action == "opened":
        return "issue-opened"
    if event == "issues" and action == "labeled":
        return "issue-labeled"
    if event == "issue_label" and action in ("label_updated", "labeled"):
        return "issue-labeled"  # Gitea/Forgejo send label changes as their own event
    if event == "issue_comment" and action == "created":
        return "issue-comment"
    return None


def decide(ir: Mapping[str, Any], trigger: Mapping[str, Any], headers: Mapping[str, str], body: bytes,
           env: Mapping[str, str]) -> Decision:
    """Pure: request -> what to run. `headers` keys may be any case."""
    h = {k.lower(): v for k, v in headers.items()}
    secret = env.get(trigger.get("secretEnv") or "ADW_WEBHOOK_SECRET") or ""
    if secret and not verify_signature(secret, h, body):
        return Decision(401, "bad or missing signature")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return Decision(400, "body is not JSON")
    events = set(trigger.get("events") or ["issue-opened", "issue-comment"])
    adws = inputs.adws_for_trigger(ir, trigger["id"])
    forge_event = next((h[k] for k in EVENT_HEADERS if h.get(k)), None)
    action = str(data.get("action") or "") if isinstance(data, dict) else ""
    kind = _issue_event(forge_event, action) if forge_event else None

    if kind:
        if kind not in events:
            return Decision(200, f"event {kind} not enabled")
        issue = data.get("issue") or {}
        if issue.get("pull_request") is not None or data.get("is_pull"):  # GitHub / Gitea+Forgejo
            return Decision(200, "comment on a pull request")
        issue_inputs = [o for o in ir.get("promptInputs") or [] if o.get("kind") == "issue"
                        and any(o["id"] in a.get("input", []) for a in adws)]
        if not issue_inputs:
            return Decision(200, "no ADW on this trigger accepts issue input")
        option = issue_inputs[0]
        keyword = option.get("keyword") or "adw"
        marker = (ir.get("forge") or {}).get("botMarker") or DEFAULT_BOT_MARKER
        labels = {lbl.get("name") if isinstance(lbl, dict) else lbl for lbl in issue.get("labels") or []}
        if kind == "issue-comment":
            text = (data.get("comment") or {}).get("body") or ""
            if text.lstrip().startswith(marker):
                return Decision(200, "own bot comment")
            workflow, _model = inputs.keyword_selection(text, keyword)
            if workflow is None:
                return Decision(200, f"comment has no `{keyword}:` line")
        else:
            if not set(option.get("labels") or []) <= labels:
                return Decision(200, "issue lacks the required labels")
            workflow, _model = inputs.keyword_selection(issue.get("body") or "", keyword)
        klass = next((lbl for lbl in labels if lbl in ("chore", "bug", "feature", "patch")), None)
        adw = inputs._choose(adws, option["id"], workflow, klass)
        if not adw:
            return Decision(200, f"no ADW on this trigger for workflow {workflow!r}")
        return Decision(202, kind, adw=adw, source=f"{option['id']}:{int(issue['number'])}",
                        input_id=option["id"])

    if forge_event and forge_event in ("issues", "issue_comment", "issue_label", "ping"):
        return Decision(200, f"{forge_event}/{action or '-'} ignored")
    if "payload" not in events:
        return Decision(200, "generic payloads not enabled")
    pay_inputs = [o for o in ir.get("promptInputs") or [] if o.get("kind") == "webhook-payload"
                  and any(o["id"] in a.get("input", []) for a in adws)]
    if not pay_inputs:
        return Decision(200, "no ADW on this trigger accepts webhook-payload input")
    option = {**inputs.DEFAULTS["webhook-payload"], **pay_inputs[0]}
    workflow = inputs.dotted(data, option["workflowField"])
    adw = inputs._choose(adws, option["id"], workflow if isinstance(workflow, str) else None, None)
    if not adw:
        return Decision(200, f"no ADW on this trigger for workflow {workflow!r}")
    return Decision(202, "payload", adw=adw, payload=data, input_id=option["id"])


def save_payload(layer_dir: Path, data: Any) -> Path:
    d = Path(layer_dir) / "agents" / "payloads"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}.json"
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p


class App:
    def __init__(self, layer, trigger: dict, dry_run: bool = False, once: bool = False):
        self.layer, self.trigger, self.dry_run, self.once = layer, trigger, dry_run, once
        self.done = threading.Event()
        self.exit_code = 0

    def run_adw(self, adw: str, source: str) -> None:
        argv = inputs.adw_argv(self.layer.layer_dir, adw, source, dry_run=self.dry_run)
        log = Path(self.layer.layer_dir) / "agents" / "trigger-logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{adw}.log"
        log.parent.mkdir(parents=True, exist_ok=True)

        def work():
            with open(log, "ab") as fh:
                rc = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    check=False).returncode
            say(f"{'done' if rc == 0 else 'FAIL'} {adw} <- {source} exit {rc}")
            if self.once:
                self.exit_code = 0 if rc == 0 else 1
                self.done.set()

        say(f"start {adw} <- {source} log {log}")
        threading.Thread(target=work, daemon=False).start()

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code: int, obj: dict) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_a):  # keep stdout to our status lines
                pass

            def do_GET(self):
                if self.path.split("?")[0] == "/health":
                    self._reply(200, {"ok": True})
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self):
                if self.path.split("?")[0] != (app.trigger.get("path") or "/webhook"):
                    self._reply(404, {"error": "not found"})
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                d = decide(app.layer.ir, app.trigger, dict(self.headers.items()), body, os.environ)
                if not d.run:
                    say(f"{d.status} {d.reason}")
                    self._reply(d.status, {"ignored" if d.status == 200 else "error": d.reason})
                    return
                source = d.source or f"payload:{save_payload(app.layer.layer_dir, d.payload)}"
                self._reply(202, {"accepted": True, "adw": d.adw, "source": source})
                app.run_adw(d.adw, source)

        return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Webhook trigger for the agentic layer")
    ap.add_argument("--layer", default=str(DEFAULT_LAYER))
    ap.add_argument("--trigger", help="webhook trigger id (default: the first webhook trigger)")
    ap.add_argument("--once", action="store_true", help="exit after the first accepted run finishes")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    layer = as_layer(args.layer)
    trig = next((t for t in layer.ir.get("triggers") or []
                 if (t.get("id") == args.trigger if args.trigger else t.get("kind") == "webhook")), None)
    if not trig:
        raise SystemExit("no webhook trigger declared in the layer")
    app = App(layer, trig, dry_run=args.dry_run, once=args.once)
    server = ThreadingHTTPServer((trig.get("bind") or "127.0.0.1", int(trig.get("port") or 8001)), app.handler())
    secret = "on" if os.environ.get(trig.get("secretEnv") or "") else "off"
    say(f"webhook {trig['id']}: http://{server.server_address[0]}:{server.server_address[1]}"
        f"{trig.get('path') or '/webhook'} events {','.join(trig.get('events') or [])} signature {secret}"
        f"{' [dry-run]' if args.dry_run else ''}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if args.once:
            app.done.wait()
            return app.exit_code
        threading.Event().wait()
    except KeyboardInterrupt:
        say("stopping")
        return 130
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
