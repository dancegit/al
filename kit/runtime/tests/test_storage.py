from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from adw_modules.ir import load
from adw_modules.storage import get_storage


def test_local_storage_url(make_layer: object, tmp_path: Path) -> None:
    layer = load(make_layer())
    file = tmp_path / "report.txt"
    file.write_text("ok", encoding="utf-8")
    assert get_storage(layer).put(file, "ignored").startswith("file://")


def test_s3_storage_upload_shape(make_layer: object, tmp_path: Path, monkeypatch: object) -> None:
    layer_path = make_layer({"name": "unit", "storage": {"id": "remote", "kind": "s3", "status": "stable", "bucket": "bucket", "endpoint": "https://s3.invalid", "localDir": "artefacts", "prefix": "adw", "publicBaseUrl": "https://cdn.invalid", "region": "test-1", "accessKeyEnv": "ACCESS", "secretKeyEnv": "SECRET"}})
    layer = load(layer_path)
    calls = []
    class Client:
        def upload_file(self, filename, bucket, key, ExtraArgs):
            calls.append((Path(filename).name, bucket, key, ExtraArgs))
    def client(name, **kwargs):
        calls.append((name, kwargs))
        return Client()
    monkeypatch.setenv("ACCESS", "ak")
    monkeypatch.setenv("SECRET", "sk")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    file = tmp_path / "image.png"
    file.write_bytes(b"png")
    url = get_storage(layer).put(file, "unit/1234abcd/image.png")
    assert calls[0] == ("s3", {"region_name": "test-1", "endpoint_url": "https://s3.invalid", "aws_access_key_id": "ak", "aws_secret_access_key": "sk"})
    assert calls[1][1:3] == ("bucket", "adw/unit/1234abcd/image.png")
    assert calls[1][3] == {"ContentType": "image/png"}
    assert url == "https://cdn.invalid/adw/unit/1234abcd/image.png"


def test_s3_without_public_url_and_collect_uses_prefixed_keys(make_layer: object, tmp_path: Path, monkeypatch: object) -> None:
    from adw_modules.artefacts import collect

    layer = load(make_layer({"name": "unit", "artefacts": [{"id": "junit", "kind": "junit", "glob": "reports/*.xml", "status": "stable"}],
                             "storage": {"id": "remote", "kind": "s3", "status": "stable", "bucket": "bkt", "endpoint": "", "localDir": "mirror", "prefix": "pfx", "publicBaseUrl": "", "region": "eu-1", "accessKeyEnv": "NOPE_A", "secretKeyEnv": "NOPE_B"}}))
    uploads = []
    class Client:
        def upload_file(self, filename, bucket, key, ExtraArgs):
            uploads.append((bucket, key))
    def client(name, **kwargs):
        assert "endpoint_url" not in kwargs and kwargs["region_name"] == "eu-1"
        return Client()
    monkeypatch.delenv("NOPE_A", raising=False)
    monkeypatch.delenv("NOPE_B", raising=False)
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    worktree = tmp_path / "wt"
    (worktree / "reports").mkdir(parents=True)
    (worktree / "reports" / "unit.xml").write_text("<testsuite/>", encoding="utf-8")
    state = SimpleNamespace(adw_id="1234abcd", worktree=str(worktree), checks={}, artefacts=[])
    [record] = collect(layer, state, ["junit"], "test")
    assert uploads == [("bkt", "pfx/unit/1234abcd/junit/reports/unit.xml")]
    assert record["url"] == "s3://bkt/pfx/unit/1234abcd/junit/reports/unit.xml"
    assert (layer.layer_dir / "mirror" / "1234abcd" / "junit" / "reports" / "unit.xml").is_file()  # local copy kept
