"""The ``s3`` asset storage provider: Marvin stores uploads in an S3-compatible bucket and hands out
URLs a browser can fetch.

Public delivery: with ``STORAGE_REMOTE_PUBLIC_URL`` (a custom domain on the bucket, e.g.
``https://assets.example.com``) an asset's URL is ``<base>/<prefix><key>``. Without it, the URL is a
presigned GET valid for ``STORAGE_S3_PRESIGN_SECONDS``. The bare endpoint URL is never handed out: on R2
it is the private S3 API, which browsers can't read.

``STORAGE_S3_PREFIX`` puts every key under a folder of the bucket (e.g. ``prod/``), so several
environments can share one; Marvin's asset rows keep the plain key.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Iterator, Mapping
from typing import Any, BinaryIO
from urllib.parse import quote

from marvin_integration_sdk.storage import Setting, StorageConfigError, StorageMetadata, StorageProvider

from .client import EnvNames, S3Connection, connection_settings, etag_md5, is_not_found

ENV = EnvNames(
    bucket="STORAGE_S3_BUCKET",
    endpoint="STORAGE_S3_ENDPOINT",
    region="STORAGE_S3_REGION",
    access_key="STORAGE_S3_ACCESS_KEY",
    secret_key="STORAGE_S3_SECRET_KEY",
    addressing_style="STORAGE_S3_ADDRESSING_STYLE",
    checksums="STORAGE_S3_CHECKSUMS",
)
PREFIX = "STORAGE_S3_PREFIX"
PUBLIC_URL = "STORAGE_REMOTE_PUBLIC_URL"
PRESIGN_SECONDS = "STORAGE_S3_PRESIGN_SECONDS"
MAX_PRESIGN_SECONDS = 7 * 24 * 3600  # SigV4's limit
META_SHA256 = "sha256"
SPOOL = 8 * 1024 * 1024  # files up to this size stay in memory on their way in and out
CHUNK = 1024 * 1024


_META_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
META_BUDGET = 1800  # S3 caps user metadata at 2 KB per object; leave room for sha256


def object_metadata(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """The entries of an upload's metadata that can travel as S3 user metadata (HTTP headers): simple
    lower-case keys, printable ASCII values, 2 KB in all. The rest is dropped: Marvin keeps an asset's
    metadata on its row, so the object copy is a convenience, and an oversized or non-ASCII header
    would fail the upload."""
    out: dict[str, str] = {}
    used = 0
    for k, v in (metadata or {}).items():
        key = str(k).lower()
        if key == META_SHA256 or not _META_KEY.match(key) or isinstance(v, (dict, list, tuple, set)) or v is None:
            continue
        value = str(v)
        if not value.isascii() or not value.isprintable() or used + len(key) + len(value) > META_BUDGET:
            continue
        out[key] = value
        used += len(key) + len(value)
    return out


def normalize_prefix(prefix: str | None) -> str:
    """`prod`, `/prod/` and `prod/` all mean `prod/`; empty means the bucket root."""
    prefix = (prefix or "").strip().strip("/")
    return f"{prefix}/" if prefix else ""


class S3StorageProvider(StorageProvider):
    slug = "s3"
    settings = (
        *connection_settings(ENV),
        Setting(PREFIX, "Key prefix", help="Optional folder in the bucket for this environment's assets, e.g. prod/"),
        Setting(
            PUBLIC_URL,
            "Public base URL",
            help="The bucket's public custom domain (recommended), e.g. https://assets.example.com; asset URLs are <base>/<prefix><key>",
        ),
        Setting(PRESIGN_SECONDS, "Presigned URL lifetime (s)", default="3600", help="Used only without a public base URL; at most 604800 (7 days)"),
    )

    def __init__(
        self,
        connection: S3Connection,
        prefix: str = "",
        public_base_url: str | None = None,
        presign_seconds: int = 3600,
        client: Any = None,
    ) -> None:
        self.connection = connection
        self.bucket = connection.bucket
        self.prefix = normalize_prefix(prefix)
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.presign_seconds = presign_seconds
        self.client = client if client is not None else connection.client()

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> S3StorageProvider:
        raw = str(config.get(PRESIGN_SECONDS) or "3600").strip()
        if not raw.isdecimal() or not 1 <= int(raw) <= MAX_PRESIGN_SECONDS:
            raise StorageConfigError(f"{PRESIGN_SECONDS} must be a whole number of seconds from 1 to {MAX_PRESIGN_SECONDS}, not {raw!r}")
        public = str(config.get(PUBLIC_URL) or "").strip()
        if public and not public.startswith(("https://", "http://")):
            raise StorageConfigError(f"{PUBLIC_URL} must be an absolute http(s) URL, e.g. https://assets.example.com")
        return cls(
            S3Connection.from_config(ENV, config),
            prefix=str(config.get(PREFIX) or ""),
            public_base_url=public or None,
            presign_seconds=int(raw),
        )

    def _key(self, storage_key: str) -> str:
        return self.prefix + storage_key

    def _head(self, storage_key: str) -> dict:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self._key(storage_key))
        except Exception as exc:
            if is_not_found(exc):
                raise FileNotFoundError(f"File not found: {storage_key}") from exc
            raise

    def put(self, storage_key: str, file_data: BinaryIO, content_type: str, metadata: dict | None = None) -> StorageMetadata:
        # Spool through a temp file (memory up to SPOOL) so the size and sha256 are known before the
        # upload, and the sha256 travels as object metadata: checksum("sha256") then needs no download.
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=SPOOL) as spool:
            while chunk := file_data.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                spool.write(chunk)
            spool.seek(0)
            checksum = digest.hexdigest()
            object_meta = {**object_metadata(metadata), META_SHA256: checksum}
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(storage_key),
                Body=spool,
                ContentLength=size,
                ContentType=content_type or "application/octet-stream",
                Metadata=object_meta,
            )
        return StorageMetadata(storage_key, size, content_type, checksum, metadata, "sha256")

    def get(self, storage_key: str) -> BinaryIO:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(storage_key))
        except Exception as exc:
            if is_not_found(exc):
                raise FileNotFoundError(f"File not found: {storage_key}") from exc
            raise
        body = resp["Body"]
        out = tempfile.SpooledTemporaryFile(max_size=SPOOL)  # noqa: SIM115 - returned open to the caller
        try:
            while chunk := body.read(CHUNK):
                out.write(chunk)
        except BaseException:
            out.close()
            raise
        finally:
            body.close()
        out.seek(0)
        return out  # type: ignore[return-value]  # a seekable binary file object

    def delete(self, storage_key: str) -> bool:
        # S3's DeleteObject succeeds for a missing key too, so ask first to report False for it.
        if not self.exists(storage_key):
            return False
        self.client.delete_object(Bucket=self.bucket, Key=self._key(storage_key))
        return True

    def exists(self, storage_key: str) -> bool:
        try:
            self._head(storage_key)
        except FileNotFoundError:
            return False
        return True  # any other error (403, network) raises: it says nothing about the file

    def get_public_url(self, storage_key: str) -> str:
        key = self._key(storage_key)
        if self.public_base_url:
            return f"{self.public_base_url}/{quote(key, safe='/~')}"
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.presign_seconds)

    def get_metadata(self, storage_key: str) -> StorageMetadata:
        head = self._head(storage_key)
        metadata = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}
        checksum, algorithm = metadata.get(META_SHA256), "sha256"
        if not checksum:
            checksum, algorithm = etag_md5(head.get("ETag"), head), "md5"
        return StorageMetadata(
            storage_key=storage_key,
            size=int(head["ContentLength"]),
            content_type=head.get("ContentType") or "application/octet-stream",
            checksum=checksum,
            metadata=metadata or None,
            checksum_algorithm=algorithm if checksum else None,
        )

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        # S3 lists keys in UTF-8 byte order, which is code point order: the same as sorted().
        n = len(self.prefix)
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            for obj in page.get("Contents", []):
                yield obj["Key"][n:]

    def checksum(self, storage_key: str, algorithm: str = "sha256") -> str | None:
        """sha256 from the metadata ``put`` stores; md5 from a single-part, unencrypted ETag. Anything
        else would need a download, so None."""
        head = self._head(storage_key)
        if algorithm == "sha256":
            return {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}.get(META_SHA256)
        if algorithm == "md5":
            return etag_md5(head.get("ETag"), head)
        return None
