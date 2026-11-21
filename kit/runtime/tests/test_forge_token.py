"""The forge clients find their token like every other private value of a layer: a non-empty value in
the process environment, then `<layer>/local.env`, then `$XDG_CONFIG_HOME/al/<layer name>.env`. The
value stays in the client (the Forgejo `Authorization` header, `GH_TOKEN` for the client's own `gh`)
and is never copied into `os.environ`; a Forgejo call that fails with no token says where to set it."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME))

from adw_modules import forge  # noqa: E402

VAR = "AL_TEST_FORGE_TOKEN"
FORGEJO = {"id": "forgejo", "kind": "forgejo", "host": "https://git.example.org/", "repo": "acme/shop",
           "tokenEnv": VAR, "botMarker": "[ADW-BOT]", "status": "stable"}
GITHUB = {"id": "github", "kind": "github", "host": "github.com", "repo": "acme/shop",
          "tokenEnv": VAR, "botMarker": "[ADW-BOT]", "status": "stable"}


def layer(tmp_path, forge_opt):
    return SimpleNamespace(name="shop", ir={"forge": forge_opt}, layer_dir=tmp_path, app_root=None)


class FakeHttp:
    def __init__(self, responses=None, error=None):
        self.calls, self.responses, self.error = [], list(responses or []), error

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": None if body is None else body.decode("utf-8")})
        if self.error is not None:
            raise self.error
        return 200, (self.responses.pop(0) if self.responses else {})


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


def write_config(tmp_path, text):
    path = tmp_path / "xdg" / "al" / "shop.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def assert_not_in_environ(value):
    assert VAR not in os.environ
    assert not any(value in v for v in os.environ.values())


def test_forgejo_token_from_local_env_reaches_only_the_header(tmp_path, monkeypatch):
    (tmp_path / "local.env").write_text(f"{VAR}=from-local-env\n")
    http = FakeHttp([{"number": 12, "html_url": "https://git.example.org/acme/shop/pulls/12"}])
    monkeypatch.setattr(forge, "http_request", http)
    client = forge.get_client(layer(tmp_path, FORGEJO))
    client.create_pr("feat-x", "main", "Add x", "body")
    client.comment(7, "hello")
    assert len(http.calls) == 2
    for call in http.calls:
        assert call["headers"]["Authorization"] == "token from-local-env"
        assert "from-local-env" not in (call["body"] or "")
    assert_not_in_environ("from-local-env")


def test_process_value_wins_over_the_files(tmp_path, monkeypatch):
    (tmp_path / "local.env").write_text(f"{VAR}=from-local\n")
    write_config(tmp_path, f"{VAR}=from-config\n")
    http = FakeHttp()
    monkeypatch.setattr(forge, "http_request", http)
    monkeypatch.setenv(VAR, "from-env")
    forge.get_client(layer(tmp_path, FORGEJO), env={VAR: "from-env"}).comment(1, "a")
    forge.get_client(layer(tmp_path, FORGEJO)).comment(1, "b")
    assert [c["headers"]["Authorization"] for c in http.calls] == ["token from-env", "token from-env"]
    # an empty process value falls through to local.env
    monkeypatch.setenv(VAR, "")
    forge.get_client(layer(tmp_path, FORGEJO), env={VAR: ""}).comment(1, "c")
    forge.get_client(layer(tmp_path, FORGEJO)).comment(1, "d")
    assert [c["headers"]["Authorization"] for c in http.calls[2:]] == ["token from-local", "token from-local"]


def test_config_file_used_when_local_env_has_none(tmp_path, monkeypatch):
    write_config(tmp_path, f"{VAR}=from-config\n")
    http = FakeHttp()
    monkeypatch.setattr(forge, "http_request", http)
    forge.get_client(layer(tmp_path, FORGEJO)).comment(1, "no local.env")
    (tmp_path / "local.env").write_text("OTHER=x\n")
    forge.get_client(layer(tmp_path, FORGEJO)).comment(1, "local.env without the key")
    (tmp_path / "local.env").write_text(f"{VAR}=from-local\n")
    forge.get_client(layer(tmp_path, FORGEJO)).comment(1, "both have it")
    assert [c["headers"]["Authorization"] for c in http.calls] == [
        "token from-config", "token from-config", "token from-local"]
    assert_not_in_environ("from-config")


def test_missing_token_names_the_variable_and_the_files(tmp_path, monkeypatch):
    (tmp_path / "local.env").write_text("OTHER=secret-x\n")
    write_config(tmp_path, "OTHER=secret-y\n")
    http = FakeHttp(error=forge.ForgeError("POST https://git.example.org/api/v1/repos/acme/shop/issues/1/comments"
                                           " -> HTTP 401: unauthorized"))
    monkeypatch.setattr(forge, "http_request", http)
    with pytest.raises(forge.ForgeError) as info:
        forge.get_client(layer(tmp_path, FORGEJO)).comment(1, "x")
    text = str(info.value)
    assert text.startswith("POST ") and "HTTP 401" in text
    assert VAR in text
    assert str(tmp_path / "local.env") in text
    assert "shop.env" in text
    assert "secret-x" not in text and "secret-y" not in text
    assert "Authorization" not in http.calls[0]["headers"]


class FakeRun:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def test_github_token_from_local_env_then_gh_auth(tmp_path, monkeypatch):
    (tmp_path / "local.env").write_text(f"{VAR}=gh-from-local\n")
    run = FakeRun(stdout="")
    client = forge.get_client(layer(tmp_path, GITHUB))
    client._run = run
    client.comment(3, "hi")
    _argv, kw = run.calls[0]
    assert kw["env"]["GH_TOKEN"] == "gh-from-local"
    assert_not_in_environ("gh-from-local")
    assert "GH_TOKEN" not in os.environ
    # no token anywhere: gh falls back to its own `gh auth` login
    (tmp_path / "local.env").unlink()
    run = FakeRun(stdout="")
    client = forge.get_client(layer(tmp_path, GITHUB))
    client._run = run
    client.comment(3, "hi")
    assert "GH_TOKEN" not in run.calls[0][1]["env"]
    assert json.dumps(run.calls[0][0]).find("gh-from-local") == -1
