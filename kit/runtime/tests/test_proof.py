"""Proof on the pull request: the forge attaches the run's screenshots and video keyframes to the
PR and ship posts ONE "Proof" comment that embeds them. Forge HTTP is a fake; the comment text
and the request shape are what is tested. The live test is tests/webdemo/webdemo_live.sh."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import forge, ship

FORGEJO = {"id": "forgejo", "kind": "forgejo", "host": "https://git.example.org/", "repo": "acme/shop",
           "tokenEnv": "FORGEJO_TOKEN", "botMarker": "[ADW-BOT]", "status": "stable"}
GITHUB = {"id": "github", "kind": "github", "host": "github.com", "repo": "acme/shop",
          "tokenEnv": "GITHUB_TOKEN", "botMarker": "[ADW-BOT]", "status": "stable"}


# ---------------------------------------------------------------- forge clients


class RecordingHttp:
    def __init__(self, response):
        self.calls, self.response = [], response

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return 201, self.response


def test_forgejo_attach_uploads_multipart_and_returns_download_url(tmp_path):
    image = tmp_path / "01 home.png"
    image.write_bytes(b"\x89PNG fake bytes")
    http = RecordingHttp({"id": 5, "name": "01 home.png",
                          "browser_download_url": "https://git.example.org/attachments/abc-123"})
    client = forge.ForgejoForge(FORGEJO, tmp_path, env={"FORGEJO_TOKEN": "s3cret"}, http=http)
    assert client.attach(12, image) == "https://git.example.org/attachments/abc-123"
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://git.example.org/api/v1/repos/acme/shop/issues/12/assets?name=01+home.png"
    assert call["headers"]["Authorization"] == "token s3cret"
    content_type = call["headers"]["Content-Type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    body = call["body"]
    assert body.startswith(b"--" + boundary + b"\r\n")
    assert body.endswith(b"\r\n--" + boundary + b"--\r\n")
    assert b'Content-Disposition: form-data; name="attachment"; filename="01 home.png"' in body
    assert b"Content-Type: image/png" in body
    assert b"\x89PNG fake bytes" in body


def test_forgejo_attach_to_a_comment_and_edit_it(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    http = RecordingHttp({"id": 31, "browser_download_url": "https://git.example.org/attachments/u1"})
    client = forge.ForgejoForge(FORGEJO, tmp_path, env={}, http=http)
    assert client.can_attach is True
    assert client.comment(4, "Proof (uploading)") == 31          # comment() returns the new comment's id
    assert client.attach(4, image, comment_id=31) == "https://git.example.org/attachments/u1"
    client.edit_comment(31, "Proof ![a](u1)")
    assert [(c["method"], c["url"]) for c in http.calls] == [
        ("POST", "https://git.example.org/api/v1/repos/acme/shop/issues/4/comments"),
        ("POST", "https://git.example.org/api/v1/repos/acme/shop/issues/comments/31/assets?name=a.png"),
        ("PATCH", "https://git.example.org/api/v1/repos/acme/shop/issues/comments/31"),
    ]
    assert json.loads(http.calls[2]["body"]) == {"body": "[ADW-BOT] Proof ![a](u1)"}


def test_forgejo_attach_without_url_in_the_answer_is_an_error(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    client = forge.ForgejoForge(FORGEJO, tmp_path, env={}, http=RecordingHttp({"id": 5}))
    with pytest.raises(forge.ForgeError, match="browser_download_url"):
        client.attach(3, image)


def test_none_and_github_forges_do_not_attach(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    none = forge.NoneForge({"kind": "none"}, tmp_path, env={})
    assert none.attach(1, image) == "" and none.can_attach is False

    def no_gh(*args, **kwargs):
        raise AssertionError("gh must not be called: GitHub has no attachment API")
    github = forge.GitHubForge(GITHUB, tmp_path, env={}, run=no_gh)
    assert github.attach(1, image) == "" and github.can_attach is False


# ---------------------------------------------------------------- which artefacts become proof


def _image(name):
    return {"id": "screenshots", "type": "image", "path": f"artefacts/r/screenshots/e2e-results/{name}"}


def _video(name, frames):
    stem = name.rsplit(".", 1)[0]
    return {"id": "recording", "type": "video", "path": f"artefacts/r/recording/e2e-results/{name}",
            "keyframes": [{"path": f"artefacts/r/recording/e2e-results/{stem}.keyframes/{stem}-{i:02d}.png"}
                          for i in range(1, frames + 1)]}


def test_proof_items_images_only():
    items = ship.proof_items([_image("01-home.png"), {"id": "log", "type": "log", "path": "x.log"}, _image("02-added.png")])
    assert [(item["kind"], item["caption"]) for item in items] == [("image", "01-home.png"), ("image", "02-added.png")]


def test_proof_items_cap_shares_slots_between_screenshots_and_keyframes():
    artefacts = [_image(f"{i:02d}-step.png") for i in range(1, 7)] + [_video("session.webm", 8)]
    items = ship.proof_items(artefacts, limit=8)
    kinds = [item["kind"] for item in items]
    assert kinds.count("video") == 1                       # the video itself is a link, not an image slot
    assert kinds.count("image") == 4 and kinds.count("keyframe") == 4
    frames = [item["caption"] for item in items if item["kind"] == "keyframe"]
    assert frames[0].startswith("session-01.png") and frames[-1].startswith("session-08.png")  # spread, ends kept
    assert all("keyframe of session.webm" in caption for caption in frames)


def test_proof_items_unused_keyframe_slots_go_to_screenshots_and_back():
    few_frames = ship.proof_items([_image(f"{i:02d}.png") for i in range(1, 11)] + [_video("v.webm", 2)], limit=8)
    assert [item["kind"] for item in few_frames].count("image") == 6
    only_video = ship.proof_items([_video("v.webm", 8)], limit=8)
    assert [item["kind"] for item in only_video].count("keyframe") == 8


def test_proof_comment_embeds_images_with_captions_and_links_the_video():
    rows = [{"kind": "image", "caption": "01-home.png", "url": "https://f/attachments/1"},
            {"kind": "video", "caption": "session.webm", "url": "https://f/attachments/2"},
            {"kind": "keyframe", "caption": "session-01.png (keyframe of session.webm)", "url": "https://f/attachments/3"},
            {"kind": "image", "caption": "02-added.png", "url": ""}]
    body = ship.proof_comment("abcd1234", rows)
    assert body.startswith("Proof from run `abcd1234`")
    assert "**01-home.png**\n\n![01-home.png](https://f/attachments/1)" in body
    assert "[session.webm](https://f/attachments/2)" in body
    assert "![session-01.png (keyframe of session.webm)](https://f/attachments/3)" in body
    assert "02-added.png (could not be attached)" in body
    assert body.index("01-home.png") < body.index("session.webm")   # screenshots first, then the video


# ---------------------------------------------------------------- posting


class FakeClient:
    """Records the Forgejo flow: placeholder comment -> files attached to it -> comment edited."""

    def __init__(self, fail_on=(), unsupported=False):
        self.attached, self.comments, self.fail_on = [], [], set(fail_on)
        self.can_attach = not unsupported

    def attach(self, number, path, name=None, comment_id=None):
        if Path(path).name in self.fail_on:
            raise forge.ForgeError("HTTP 413: too large")
        assert comment_id == 500 + len(self.comments) - 1, "files go to the Proof comment, not the issue"
        self.attached.append((number, Path(path).name))
        return f"https://f/attachments/{len(self.attached)}"

    def comment(self, number, body):
        self.comments.append((number, body))
        return 500 + len(self.comments) - 1

    def edit_comment(self, comment_id, body):
        number, _ = self.comments[comment_id - 500]
        self.comments[comment_id - 500] = (number, body)


def _run(tmp_path, artefacts):
    for item in artefacts:
        for path in [item["path"], *[frame["path"] for frame in item.get("keyframes", [])]]:
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / path).write_bytes(b"data")
    layer = SimpleNamespace(layer_dir=tmp_path)
    state = SimpleNamespace(adw_id="abcd1234", artefacts=artefacts, ship={})
    return layer, state


def test_post_proof_attaches_and_posts_one_comment(tmp_path):
    layer, state = _run(tmp_path, [_image("01-home.png"), _video("session.webm", 2)])
    client = FakeClient()
    result = ship.post_proof(layer, state, client, {"number": 7})
    assert [name for _, name in client.attached] == ["01-home.png", "session.webm", "session-01.png", "session-02.png"]
    assert len(client.comments) == 1 and client.comments[0][0] == 7
    assert client.comments[0][1].count("![") == 3
    assert result == {"posted": True, "attached": 4, "warnings": []}
    assert state.ship["proof"] == result


def test_post_proof_attach_failure_is_a_warning_not_an_error(tmp_path, capsys):
    layer, state = _run(tmp_path, [_image("01-home.png"), _video("session.webm", 1)])
    client = FakeClient(fail_on={"session.webm"})
    result = ship.post_proof(layer, state, client, {"number": 7})
    assert result["posted"] is True and result["attached"] == 2
    assert "session.webm" in result["warnings"][0]
    assert "session.webm (could not be attached)" in client.comments[0][1]
    assert "warning: proof" in capsys.readouterr().out


def test_post_proof_skips_forges_without_attachments_and_runs_without_images(tmp_path):
    layer, state = _run(tmp_path, [_image("01-home.png")])
    client = FakeClient(unsupported=True)
    assert ship.post_proof(layer, state, client, {"number": 7})["posted"] is False
    assert client.comments == []
    layer, state = _run(tmp_path, [{"id": "log", "type": "log", "path": "artefacts/r/log/test.log"}])
    client = FakeClient()
    assert ship.post_proof(layer, state, client, {"number": 7}) == {"posted": False, "attached": 0, "warnings": [],
                                                                      "reason": "no image or video artefacts"}
    assert client.comments == [] and client.attached == []


def test_post_proof_never_raises_even_when_commenting_fails(tmp_path):
    layer, state = _run(tmp_path, [_image("01-home.png")])
    client = FakeClient()
    client.comment = lambda number, body: (_ for _ in ()).throw(forge.ForgeError("HTTP 500"))
    result = ship.post_proof(layer, state, client, {"number": 7})
    assert result["posted"] is False and "HTTP 500" in result["warnings"][-1]
    client = FakeClient()
    client.edit_comment = lambda comment_id, body: (_ for _ in ()).throw(forge.ForgeError("HTTP 502"))
    assert ship.post_proof(layer, state, client, {"number": 7})["posted"] is False
    assert ship.post_proof(layer, state, object(), {"number": 7})["posted"] is False   # client without attach()


def test_human_path_posts_proof_on_the_new_pr(tmp_path, monkeypatch):
    layer, state = _run(tmp_path, [_image("01-home.png")])
    client = FakeClient()
    monkeypatch.setattr(ship, "_open_pr", lambda *args: {"number": 9, "url": "https://f/pulls/9"})
    pr, target = ship._human_path(layer, state, client, tmp_path, "body", None)
    assert pr["number"] == 9 and target == "https://f/pulls/9"
    assert client.comments and client.comments[0][0] == 9
    assert json.dumps(state.ship["proof"])  # recorded in the run state
