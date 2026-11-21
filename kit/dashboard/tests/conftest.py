from __future__ import annotations

import contextlib
import http.client
import json
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import dashboard  # noqa: E402
import fixtures  # noqa: E402
import start_layer  # noqa: E402
import voice_fixtures  # noqa: E402
from fake_forgejo import FakeForgejo  # noqa: E402


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "browser: drives a real Chrome through Playwright (skipped without one)")
    config.addinivalue_line("markers", "podman: needs rootless podman and python:3.12-slim locally (skipped otherwise)")
    config.addinivalue_line("markers", "voice: needs the real speech engines and their models (AL_VOICE_REAL=1; skipped otherwise)")
    config.addinivalue_line("markers", "slow: takes tens of seconds or more")
    # Every skip prints its reason (as -rs would), so a log says why a test did not run.
    if "s" not in (config.option.reportchars or ""):
        config.option.reportchars = (config.option.reportchars or "") + "s"


@pytest.fixture(scope="session", autouse=True)
def _al_labelled_podman():
    """Every image and container this session's tests make is labelled al.test (a podman shim first on
    PATH, kit/runtime/adw_modules/diskuse.py) and removed when the session ends; leftovers older than six
    hours go when it starts. Nothing happens without that file or without podman."""
    import importlib.util
    path = HERE.parents[2] / "kit" / "runtime" / "adw_modules" / "diskuse.py"
    if not path.is_file():
        yield ""
        return
    spec = importlib.util.spec_from_file_location("al_diskuse", path)
    diskuse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diskuse)
    with diskuse.labelled_session() as session:
        yield session


@pytest.fixture(scope="session")
def fixture_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fixtures")
    fixtures.build(root)
    return root


@pytest.fixture()
def fx(tmp_path: Path, fixture_root: Path) -> dict[str, Path]:
    """A private, writable copy of the fixture tree (tests append to files in it)."""
    root = tmp_path / "fx"
    shutil.copytree(fixture_root, root, symlinks=True)
    return {"root": root, "layer": root / "demo-shop.al", "agents": root / "demo-shop.al" / "agents",
            "repo": root / "repo", "secret": root / "outside-secret.txt"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Running:
    def __init__(self, server: dashboard.DashboardServer) -> None:
        self.server = server
        self.port = server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def get(self, path: str, headers: dict[str, str] | None = None, method: str = "GET",
            body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def json(self, path: str) -> Any:
        status, _headers, body = self.get(path)
        assert status == 200, (status, body[:300])
        return json.loads(body)


def start_server(args: list[str], static_dir: Path | None = None) -> Running:
    defaults = [] if "--port" in args else ["--port", "0"]
    parsed = dashboard.parse_args([*args, *defaults, "--poll", "0.2", "--no-podman"])
    server = dashboard.make_server(parsed, static_dir or dashboard.STATIC_DIR)
    threading.Thread(target=server.model.run_forever, daemon=True).start()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return Running(server)


def stop_server(running: Running) -> None:
    running.server.shutting_down.set()
    running.server.model.stop.set()
    running.server.shutdown()
    running.server.server_close()


@pytest.fixture()
def server(fx: dict[str, Path]) -> Iterator[Running]:
    running = start_server(["--layer", str(fx["layer"])])
    try:
        yield running
    finally:
        stop_server(running)


@pytest.fixture(scope="session")
def fake_forgejo() -> Iterator[FakeForgejo]:
    forge = FakeForgejo().start()
    try:
        yield forge
    finally:
        forge.stop()


@pytest.fixture()
def start_site(tmp_path: Path, fake_forgejo: FakeForgejo, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A git app and runnable layers on it (tests/start_layer.py): notes (forgejo, the fake one,
    emptied), bare (forge none) and old (no launch.py). Runs started from here see none of the
    caller's AL_* overrides."""
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    fake_forgejo.reset()
    return start_layer.build_site(tmp_path / "site", fake_forgejo.url)


@pytest.fixture()
def voice_site(start_site: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """start_site with its voice layer `talk`, plus `fake`: the fake recogniser's script (no transcripts yet;
    tests/voice_fixtures.py fake_script rewrites it), `voice_dir`: where conversations are kept, and
    `cache`: an empty speech model cache (AL_VOICE_CACHE, AL_KOKORO_CACHE), so nothing real is found."""
    for name in ("AL_VOICE_TOKEN", "AL_VOICE_NET_LOG"):
        monkeypatch.delenv(name, raising=False)
    cache = tmp_path / "voice-cache"
    monkeypatch.setenv("AL_VOICE_CACHE", str(cache / "voice"))
    monkeypatch.setenv("AL_KOKORO_CACHE", str(cache / "kokoro"))
    fake = voice_fixtures.fake_script(tmp_path / "voice" / "fake.json", [])
    return {**start_site, "fake": fake, "voice_dir": tmp_path / "voice" / "conversations", "cache": cache}


@pytest.fixture()
def workspace_server(tmp_path: Path) -> Iterator[Any]:
    """A factory: start(root, *extra, al=None, jobs=None) → a server over the workspace `root` with its
    jobs in tmp_path/jobs (or `jobs`) and, when given, `--al al`; every server is stopped afterwards."""
    started: list[Running] = []

    def start(root: Path | None, *extra: str, al: Path | str | None = None, jobs: Path | None = None) -> Running:
        args = [*(["--workspace", str(root)] if root is not None else []), "--jobs-dir", str(jobs or tmp_path / "jobs"),
                *(["--al", str(al)] if al is not None else []), *extra]
        running = start_server(args)
        started.append(running)
        return running

    try:
        yield start
    finally:
        for running in started:
            with contextlib.suppress(Exception):
                stop_server(running)


class SSEReader:
    """Minimal SSE client over a raw socket: yields (event, data, id) as they arrive."""

    def __init__(self, port: int, path: str, headers: dict[str, str] | None = None) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        self.sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\n{extra}\r\n".encode())
        self.buffer = b""
        head = self._until(b"\r\n\r\n")
        self.status = int(head.split(b" ", 2)[1])
        self.headers = head

    def _until(self, marker: bytes) -> bytes:
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(marker)
        return head

    def next(self, timeout: float = 10) -> tuple[str, str, str | None]:
        deadline = time.monotonic() + timeout
        while True:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            block = self._until(b"\n\n").decode("utf-8")
            event, data, event_id = "message", [], None
            for line in block.split("\n"):
                if line.startswith(":") or not line:
                    continue
                name, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if name == "event":
                    event = value
                elif name == "data":
                    data.append(value)
                elif name == "id":
                    event_id = value
            if data:
                return event, "\n".join(data), event_id
            if time.monotonic() > deadline:
                raise TimeoutError("no event")

    def wait_for(self, kind: str, predicate=lambda data: True, timeout: float = 10) -> tuple[str, str | None]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event, data, event_id = self.next(timeout=deadline - time.monotonic())
            if event == kind and predicate(data):
                return data, event_id
        raise TimeoutError(f"no {kind} event")

    def close(self) -> None:
        self.sock.close()


# A run's sandbox points git at the run's own object store through environment variables. Tests build
# their own repositories, so those variables must not reach them: clear them once, before any test.
import os as _os
for _name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_CEILING_DIRECTORIES"):
    _os.environ.pop(_name, None)
