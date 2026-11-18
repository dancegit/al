"""`al voice LAYER setup|doctor`, and the voice helper's files in `al gen` and `al check`.

The downloads come from a local http.server serving small stand-in files; AL_VOICE_MANIFEST points the
service at a test manifest with their real sha256 and AL_VOICE_CACHE / AL_KOKORO_CACHE at a temporary cache,
so nothing is fetched from the internet and the user's cache is never touched."""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

from conftest import KIT, LAYER_TEMPLATE, al

FILES = {"whisper": {"config.json": b'{"a": 1}\n', "model.bin": b"M" * 300_000, "tokenizer.json": b"{}\n",
                     "vocabulary.txt": b"a\nb\n"},
         "kokoro": {"kokoro-v1.0.int8.onnx": b"K" * 200_000, "voices-v1.0.bin": b"V" * 50_000}}
REPO, REVISION = "Systran/faster-whisper-base.en", "0123456789abcdef"


def voice_layer(app: Path, clause: str = "(voice kokoro)") -> Path:
    layer = app.parent / "notes.al"
    layer.mkdir(exist_ok=True)
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="../notes", placement=f"(layer sibling)\n  {clause}"))
    al("gen", layer)
    return layer


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def files_server(tmp_path: Path):
    """A local server of the stand-in model files; yields (base url, list of requested paths)."""
    root = tmp_path / "served"
    for name, data in FILES["whisper"].items():
        path = root / REPO / "resolve" / REVISION / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name, data in FILES["kokoro"].items():
        path = root / "kokoro" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    requested: list[str] = []

    class Handler(_Quiet):
        def do_GET(self) -> None:  # noqa: N802
            requested.append(self.path)
            super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", requested
    server.shutdown()


def write_manifest(tmp_path: Path, base: str, wrong: str | None = None) -> Path:
    def entry(data: bytes, name: str) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        return {"bytes": len(data), "sha256": "0" * 64 if name == wrong else digest}
    manifest = {"version": 1, "endpoint": base,
                "whisper": {"base.en": {"repo": REPO, "revision": REVISION,
                                        "files": {n: entry(d, n) for n, d in FILES["whisper"].items()}}},
                "kokoro": {"release": f"{base}/kokoro", "model": "kokoro-v1.0.int8.onnx",
                           "files": {**{n: entry(d, n) for n, d in FILES["kokoro"].items()},
                                     "kokoro-v1.0.onnx": {"bytes": 10, "sha256": "1" * 64, "optional": True}}}}
    path = tmp_path / "models.json"
    path.write_text(json.dumps(manifest))
    return path


def voice_env(tmp_path: Path, manifest: Path) -> dict[str, str]:
    return {"AL_VOICE_MANIFEST": str(manifest), "AL_VOICE_CACHE": str(tmp_path / "cache" / "voice"),
            "AL_KOKORO_CACHE": str(tmp_path / "cache" / "kokoro"), "HOME": os.environ.get("HOME", str(tmp_path))}


def test_setup_downloads_verifies_and_prints_sizes(app, tmp_path, files_server) -> None:
    base, requested = files_server
    layer = voice_layer(app)
    env = voice_env(tmp_path, write_manifest(tmp_path, base))
    cp = al("voice", layer, "setup", env=env)
    out = cp.stdout
    for name in FILES["whisper"]:
        assert f"whisper base.en/{name}" in out
        assert (tmp_path / "cache/voice/whisper/base.en" / name).read_bytes() == FILES["whisper"][name]
    for name in FILES["kokoro"]:
        assert (tmp_path / "cache/kokoro" / name).read_bytes() == FILES["kokoro"][name]
    assert "300.0 kB" in out and "downloaded, checksum ok" in out          # sizes, per file
    assert "nothing will be downloaded again; the service runs offline" in out
    assert not (tmp_path / "cache/kokoro/kokoro-v1.0.onnx").exists()     # optional: never downloaded
    assert len(requested) == len(FILES["whisper"]) + len(FILES["kokoro"])
    assert f"/{REPO}/resolve/{REVISION}/model.bin" in requested
    again = al("voice", layer, "setup", env=env).stdout
    assert len(requested) == len(FILES["whisper"]) + len(FILES["kokoro"])  # the second setup downloads nothing
    assert again.count("already there, checksum ok") == len(FILES["whisper"]) + len(FILES["kokoro"])


def test_setup_refuses_a_bad_checksum(app, tmp_path, files_server) -> None:
    base, _requested = files_server
    layer = voice_layer(app)
    env = voice_env(tmp_path, write_manifest(tmp_path, base, wrong="model.bin"))
    cp = al("voice", layer, "setup", env=env, ok=False)
    assert cp.returncode == 1
    assert "checksum mismatch" in cp.stderr and "0" * 64 in cp.stderr
    assert hashlib.sha256(FILES["whisper"]["model.bin"]).hexdigest() in cp.stderr
    assert not (tmp_path / "cache/voice/whisper/base.en/model.bin").exists()
    assert not list((tmp_path / "cache").rglob("*.part"))


def test_setup_refuses_an_unrecorded_model(app, tmp_path, files_server) -> None:
    base, requested = files_server
    layer = voice_layer(app, '(voice kokoro #:stt-model "tiny.en")')
    cp = al("voice", layer, "setup", env=voice_env(tmp_path, write_manifest(tmp_path, base)), ok=False)
    assert cp.returncode == 2
    assert "`tiny.en` is not recorded" in cp.stderr and "recorded: base.en" in cp.stderr
    assert requested == [] and not (tmp_path / "cache").exists()


def test_doctor_reports_missing_files(app, tmp_path) -> None:
    layer = voice_layer(app)
    env = voice_env(tmp_path, KIT / "runtime/voice/models.json")
    cp = al("voice", layer, "doctor", env=env, ok=False)
    assert cp.returncode == 1
    assert "whisper base.en/model.bin" in cp.stdout and "missing" in cp.stdout
    assert f"voice is not set up: al voice {layer} setup" in cp.stdout
    assert not (tmp_path / "cache").exists()                             # doctor never downloads
    js = al("voice", layer, "doctor", "--json", env=env, ok=False)
    assert js.returncode == 1
    report = json.loads(js.stdout)
    assert report["ok"] is False and any("model.bin" in m for m in report["missing"])


def test_voice_on_a_layer_without_voice(sibling) -> None:
    cp = al("voice", sibling, "setup", ok=False)
    assert cp.returncode == 2 and "declares no (voice …)" in cp.stderr


def test_gen_writes_the_voice_service_and_panel(app) -> None:
    layer = voice_layer(app)
    for rel in ("adws/voice/serve.py", "adws/voice/models.json", "adws/voice/listen.py", "adws/voice/say.py",
                "adws/adw_modules/helper.py", "adws/dashboard/static/voice.js",
                "adws/dashboard/static/voice-worklet.js"):
        assert (layer / rel).is_file(), rel
    assert (layer / "adws/voice/serve.py").read_text() == (KIT / "runtime/voice/serve.py").read_text()
    assert os.access(layer / "adws/voice/serve.py", os.X_OK)
    assert json.loads((layer / "adws/voice/models.json").read_text()) == \
        json.loads((KIT / "runtime/voice/models.json").read_text())
    assert al("check", layer).returncode == 0


def test_check_requires_them(app, tmp_path) -> None:
    layer = voice_layer(app)
    for rel in ("adws/voice/serve.py", "adws/voice/models.json", "adws/adw_modules/helper.py"):
        path = layer / rel
        kept = path.read_bytes()
        path.unlink()
        out = al("check", layer, ok=False).stdout
        assert f"PROBLEM: missing {rel}" in out, out
        path.write_bytes(kept)
    (layer / "adws/voice/serve.py").unlink()                              # a layer generated before `al voice`
    cp = al("voice", layer, "doctor", ok=False)
    assert cp.returncode == 1 and f"run `al gen {layer}`" in cp.stderr


def test_skips_print_their_reason(pytestconfig) -> None:
    assert "s" in pytestconfig.option.reportchars
