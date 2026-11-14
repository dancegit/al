from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from .errors import LayerError


class Storage(Protocol):
    def put(self, local_path: Path, key: str) -> str:
        """Store `local_path` under `key` (`<layer-name>/<adw_id>/<relative>`) and return its URL."""
        ...


class LocalStorage:
    """Artefacts stay in the layer; the URL is the file URI of the local copy."""

    def put(self, local_path: Path, key: str) -> str:
        del key  # the local copy already sits at its final path
        return Path(local_path).resolve().as_uri()


class S3Storage:
    """Uploads to any S3-compatible bucket; the local copy is always kept."""

    def __init__(self, option: dict[str, Any]) -> None:
        if not option.get("bucket"):
            raise LayerError("s3 storage needs a bucket")
        self.option = option

    def object_key(self, key: str) -> str:
        prefix = str(self.option.get("prefix") or "").strip("/")
        return f"{prefix}/{key}" if prefix else key

    def _client(self) -> Any:
        import boto3  # type: ignore[import-not-found]  # optional dependency, only needed for s3 storage

        option = self.option
        kwargs: dict[str, Any] = {"region_name": option.get("region") or None}
        if option.get("endpoint"):
            kwargs["endpoint_url"] = option["endpoint"]
        access = os.environ.get(str(option.get("accessKeyEnv") or ""))
        secret = os.environ.get(str(option.get("secretKeyEnv") or ""))
        if access:
            kwargs["aws_access_key_id"] = access
        if secret:
            kwargs["aws_secret_access_key"] = secret
        return boto3.client("s3", **kwargs)

    def put(self, local_path: Path, key: str) -> str:
        object_key = self.object_key(key)
        content_type = mimetypes.guess_type(Path(local_path).name)[0] or "application/octet-stream"
        self._client().upload_file(str(local_path), self.option["bucket"], object_key,
                                   ExtraArgs={"ContentType": content_type})
        base = str(self.option.get("publicBaseUrl") or "").rstrip("/")
        if base:
            return f"{base}/{quote(object_key, safe='/')}"
        return f"s3://{self.option['bucket']}/{object_key}"


def _storage_option(layer: Any) -> dict[str, Any]:
    option = layer.ir.get("storage")
    if not isinstance(option, dict):
        raise LayerError("IR storage option is missing")
    return option


def artefacts_root(layer: Any) -> Path:
    """Local directory holding `artefacts/<adw_id>/` (storage `dir` or s3 `localDir`)."""
    option = _storage_option(layer)
    relative = option.get("dir") if option.get("kind") == "local" else option.get("localDir")
    return Path(layer.layer_dir) / str(relative or "artefacts")


def get_storage(layer: Any) -> Storage:
    option = _storage_option(layer)
    kind = option.get("kind")
    if option.get("status") == "planned":
        raise LayerError(f"storage kind {kind!r} is planned and not implemented")
    if kind == "local":
        return LocalStorage()
    if kind == "s3":
        return S3Storage(option)
    raise LayerError(f"Unknown storage kind: {kind}")
