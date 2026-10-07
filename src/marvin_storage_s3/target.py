"""The ``s3`` backup target: Marvin's backup engine writes database dumps, the config archive and the
asset mirror to an S3-compatible bucket, and restores from it.

The engine owns the key layout (``postgres/``, ``sqlite/``, ``config/``, ``assets/``) and the per-environment
prefix (``BACKUP_PREFIX``, falling back to ``BACKUP_S3_PREFIX``), so this target works in plain bucket keys
and never adds a prefix of its own. It reads and writes the same objects ``scripts/offsite_backup.py``
did: user metadata (``sha256``, ``db-sha256``) travels as S3 user metadata, lower-cased.

Digests: an object uploaded in one PUT has its MD5 as ETag, which ``list`` and ``head`` report as an
``md5`` digest; the engine compares that against the asset's MD5, exactly as the old script did. Files
up to ``multipart_threshold`` (64 MiB) go up in one PUT, so assets keep comparable ETags. Larger files
(big database dumps) go up in parts; their ETag is not a content hash, so they report no algorithm and
the engine relies on the ``sha256`` metadata it stores itself.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from marvin_integration_sdk.storage import BackupTarget, TargetObject

from .client import EnvNames, S3Connection, connection_settings, etag_md5, is_not_found

ENV = EnvNames(
    bucket="BACKUP_S3_BUCKET",
    endpoint="BACKUP_S3_ENDPOINT",
    region="BACKUP_S3_REGION",
    access_key="AWS_ACCESS_KEY_ID",
    secret_key="AWS_SECRET_ACCESS_KEY",
    addressing_style="BACKUP_S3_ADDRESSING_STYLE",
    checksums="BACKUP_S3_CHECKSUMS",
)
MULTIPART_THRESHOLD = 64 * 1024 * 1024
MULTIPART_CHUNK = 16 * 1024 * 1024
DELETE_BATCH = 1000  # DeleteObjects takes at most 1000 keys per request
CHUNK = 1024 * 1024


class S3DeleteError(RuntimeError):
    """DeleteObjects reported per-key errors (e.g. AccessDenied). The message names keys, never credentials."""


class S3BackupTarget(BackupTarget):
    slug = "s3"
    settings = connection_settings(ENV)

    def __init__(self, connection: S3Connection, client: Any = None, multipart_threshold: int = MULTIPART_THRESHOLD) -> None:
        self.connection = connection
        self.bucket = connection.bucket
        self.client = client if client is not None else connection.client()
        self.multipart_threshold = multipart_threshold

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> S3BackupTarget:
        return cls(S3Connection.from_config(ENV, config))

    def put_file(self, key: str, path: Path, metadata: Mapping[str, str] | None = None, content_type: str | None = None) -> None:
        from boto3.s3.transfer import TransferConfig

        extra: dict[str, Any] = {"Metadata": {str(k).lower(): str(v) for k, v in (metadata or {}).items()}}
        if content_type:
            extra["ContentType"] = content_type
        config = TransferConfig(multipart_threshold=self.multipart_threshold, multipart_chunksize=MULTIPART_CHUNK)
        # S3 makes an object visible only once the PUT (or CompleteMultipartUpload) succeeds, so a reader
        # never sees a partial object.
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs=extra, Config=config)

    def get(self, key: str, dest: Path) -> dict[str, str]:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if is_not_found(exc):
                raise FileNotFoundError(f"backup object not found: {key}") from exc
            raise
        body = resp["Body"]
        try:
            with Path(dest).open("wb") as fh:
                shutil.copyfileobj(body, fh, CHUNK)
        finally:
            body.close()
        return {k.lower(): v for k, v in (resp.get("Metadata") or {}).items()}

    def list(self, prefix: str) -> dict[str, TargetObject]:
        found: dict[str, TargetObject] = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                found[obj["Key"]] = _target_object(obj["Key"], int(obj["Size"]), obj.get("ETag", ""))
        return found

    def delete(self, keys: Iterable[str]) -> None:
        keys = list(keys)
        for i in range(0, len(keys), DELETE_BATCH):
            batch = keys[i : i + DELETE_BATCH]
            resp = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
            if errors := resp.get("Errors"):
                first = errors[0]
                raise S3DeleteError(f"delete failed for {len(errors)} object(s), first: {first.get('Key')} {first.get('Code')}")

    def head(self, key: str) -> TargetObject | None:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if is_not_found(exc):
                return None
            raise
        metadata = {k.lower(): v for k, v in (resp.get("Metadata") or {}).items()}
        return _target_object(key, int(resp["ContentLength"]), resp.get("ETag", ""), metadata, resp)

    def describe(self) -> str:
        return self.connection.location


def _target_object(key: str, size: int, etag: str, metadata: Mapping[str, str] | None = None, head: Mapping[str, Any] | None = None) -> TargetObject:
    md5 = etag_md5(etag, head)
    if md5:
        return TargetObject(key, size, md5, "md5", dict(metadata or {}))
    # Not a content hash (multipart or encrypted): keep the ETag for diagnostics, no algorithm, so the
    # engine compares sizes only.
    return TargetObject(key, size, etag.strip('"'), "", dict(metadata or {}))
