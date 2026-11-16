"""An in-memory Forgejo issues API (just what the runtime's ForgejoForge calls for issues) on
http.server: labels, create / list / get issues, list / post comments. One repository; every
request is recorded. Used by the start tests and the issue-race tests."""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LABELS = ("adw", "chore", "bug", "feature", "patch")


class FakeForgejo:
    def __init__(self, repo: str = "acme/notes") -> None:
        self.repo = repo
        self.lock = threading.Lock()
        self.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)

    def reset(self) -> None:
        with getattr(self, "lock", threading.Lock()):
            self.labels = [{"id": index + 1, "name": name} for index, name in enumerate(LABELS)]
            self.issues: dict[int, dict[str, Any]] = {}
            self.comments: dict[int, list[dict[str, Any]]] = {}
            self.requests: list[tuple[str, str]] = []
            self.next_comment = 100

    def start(self) -> "FakeForgejo":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _issue_view(self, issue: dict[str, Any]) -> dict[str, Any]:
        return {**issue, "labels": [label for label in self.labels if label["id"] in issue["label_ids"]],
                "html_url": f"{self.url}/{self.repo}/issues/{issue['number']}"}

    def _handler(self):
        fake = self
        base = f"/api/v1/repos/{fake.repo}"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _reply(self, code: int, value: Any) -> None:
                raw = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null") if length else None
                with fake.lock:
                    fake.requests.append((method, parsed.path))
                    if not parsed.path.startswith(base):
                        return self._reply(404, {"message": "not found"})
                    rest = parsed.path[len(base):]
                    query = urllib.parse.parse_qs(parsed.query)
                    if method == "GET" and rest == "/labels":
                        return self._reply(200, fake.labels)
                    if method == "GET" and rest == "/issues":
                        wanted = [x for x in (query.get("labels") or [""])[0].split(",") if x]
                        out = [fake._issue_view(i) for i in fake.issues.values() if i["state"] == "open"]
                        out = [i for i in out if set(wanted) <= {label["name"] for label in i["labels"]}]
                        return self._reply(200, out)
                    if method == "POST" and rest == "/issues":
                        number = len(fake.issues) + 1
                        fake.issues[number] = {"number": number, "title": body["title"], "body": body.get("body") or "",
                                               "label_ids": list(body.get("labels") or []), "state": "open"}
                        return self._reply(201, fake._issue_view(fake.issues[number]))
                    match = re.fullmatch(r"/issues/(\d+)(/comments)?", rest)
                    if match and int(match.group(1)) in fake.issues:
                        number = int(match.group(1))
                        if not match.group(2) and method == "GET":
                            return self._reply(200, fake._issue_view(fake.issues[number]))
                        if match.group(2) and method == "GET":
                            return self._reply(200, fake.comments.get(number, []))
                        if match.group(2) and method == "POST":
                            fake.next_comment += 1
                            comment = {"id": fake.next_comment, "body": body["body"], "user": {"login": "bot"}}
                            fake.comments.setdefault(number, []).append(comment)
                            return self._reply(201, comment)
                    return self._reply(404, {"message": "not found"})

            def do_GET(self) -> None:  # noqa: N802
                self._route("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._route("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._route("PATCH")

        return Handler

    def bodies(self) -> list[str]:
        with self.lock:
            return [issue["body"] for issue in self.issues.values()]
