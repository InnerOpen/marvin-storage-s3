"""The S3 connection both sides share: settings, the boto3 client, and the S3 quirks they both handle.

The asset provider and the backup target read the same kind of settings under different variable names
(``STORAGE_S3_*`` for assets, as Marvin core's old provider did; ``BACKUP_S3_*`` + ``AWS_*`` for backups,
as the ``marvin-r2-backup`` Secret holds), so ``connection_settings`` declares one set per side.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from marvin_integration_sdk.storage import Setting, StorageConfigError

CHECKSUM_MODES = ("when_required", "when_supported")
ADDRESSING_STYLES = ("path", "virtual", "auto")
NOT_FOUND = frozenset({"404", "NoSuchKey", "NotFound"})


@dataclass(frozen=True)
class EnvNames:
    """Which environment variable carries each connection setting, for one side."""

    bucket: str
    endpoint: str
    region: str
    access_key: str
    secret_key: str
    addressing_style: str
    checksums: str


def connection_settings(names: EnvNames) -> tuple[Setting, ...]:
    return (
        Setting(names.bucket, "Bucket", required=True),
        Setting(
            names.endpoint,
            "Endpoint URL",
            help="Empty for AWS S3. R2: https://<account-id>.r2.cloudflarestorage.com; MinIO: its URL; B2: https://s3.<region>.backblazeb2.com",
        ),
        Setting(names.region, "Region", default="auto", help="`auto` for R2; the bucket's region for AWS and B2 (e.g. us-east-1, us-west-004)"),
        Setting(names.access_key, "Access key ID", help="Empty (with the secret empty too) uses boto3's default chain, e.g. an IAM role"),
        Setting(names.secret_key, "Secret access key", secret=True),
        Setting(
            names.addressing_style,
            "Addressing style",
            help="path | virtual | auto; empty = path with an endpoint (R2, MinIO, B2), virtual on AWS",
        ),
        Setting(
            names.checksums,
            "Checksum mode",
            default="when_required",
            help="when_required (works everywhere; R2 and older MinIO reject some of the checksums newer boto3 adds by default) | when_supported",
        ),
    )


@dataclass(frozen=True)
class S3Connection:
    """Everything needed to build a client. ``repr`` never shows the secret."""

    bucket: str
    endpoint: str | None = None
    region: str = "auto"
    access_key: str | None = None
    secret_key: str | None = None
    addressing_style: str | None = None
    checksums: str = "when_required"

    def __repr__(self) -> str:  # the dataclass default would print secret_key
        return f"S3Connection(bucket={self.bucket!r}, endpoint={self.endpoint!r}, region={self.region!r})"

    @classmethod
    def from_config(cls, names: EnvNames, config: Mapping[str, Any]) -> S3Connection:
        def get(env: str) -> str | None:
            value = config.get(env)
            value = str(value).strip() if value is not None else ""
            return value or None

        conn = cls(
            bucket=get(names.bucket) or "",
            endpoint=get(names.endpoint),
            region=get(names.region) or "auto",
            access_key=get(names.access_key),
            secret_key=get(names.secret_key),
            addressing_style=get(names.addressing_style),
            checksums=get(names.checksums) or "when_required",
        )
        conn.validate(names)
        return conn

    def validate(self, names: EnvNames) -> None:
        """Raise ``StorageConfigError`` for a setting that can't work; messages name variables, never values of secrets."""
        if not self.bucket:
            raise StorageConfigError(f"missing setting(s): {names.bucket}")
        if self.endpoint:
            parts = urlsplit(self.endpoint)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise StorageConfigError(f"{names.endpoint} must be an http(s) URL, e.g. https://<account-id>.r2.cloudflarestorage.com")
        elif self.region == "auto":
            raise StorageConfigError(
                f"{names.region} must be the bucket's AWS region (e.g. us-east-1) when {names.endpoint} is empty; `auto` is for R2"
            )
        if bool(self.access_key) != bool(self.secret_key):
            missing = names.secret_key if self.access_key else names.access_key
            raise StorageConfigError(f"missing setting(s): {missing} (set both keys, or neither to use boto3's default credentials)")
        if self.addressing_style and self.addressing_style not in ADDRESSING_STYLES:
            raise StorageConfigError(f"{names.addressing_style} must be one of {', '.join(ADDRESSING_STYLES)}, not {self.addressing_style!r}")
        if self.checksums not in CHECKSUM_MODES:
            raise StorageConfigError(f"{names.checksums} must be one of {', '.join(CHECKSUM_MODES)}, not {self.checksums!r}")

    @property
    def location(self) -> str:
        """Where the bucket lives, for logs: the endpoint's host, or AWS and the region. Never a credential."""
        host = urlsplit(self.endpoint).netloc if self.endpoint else f"aws:{self.region}"
        return f"s3://{self.bucket} ({host})"

    def client(self) -> Any:
        import boto3
        from botocore.config import Config

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": self.addressing_style or ("path" if self.endpoint else "virtual")},
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=10,
            read_timeout=120,
            request_checksum_calculation=self.checksums,
            response_checksum_validation=self.checksums,
        )
        kwargs: dict[str, Any] = {"region_name": self.region, "config": config}
        if self.endpoint:
            kwargs["endpoint_url"] = self.endpoint
        if self.access_key and self.secret_key:
            kwargs["aws_access_key_id"] = self.access_key
            kwargs["aws_secret_access_key"] = self.secret_key
        return boto3.client("s3", **kwargs)


def error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


def is_not_found(exc: Exception) -> bool:
    return error_code(exc) in NOT_FOUND


def etag_md5(etag: str | None, head: Mapping[str, Any] | None = None) -> str | None:
    """The object's MD5 when its ETag is one: a single-part upload without SSE-KMS / SSE-C. A multipart
    ETag (``<md5 of part md5s>-<parts>``) or an encrypted object's ETag is not a content hash."""
    value = (etag or "").strip('"')
    if not value or "-" in value or len(value) != 32:
        return None
    if head and (str(head.get("ServerSideEncryption", "")).startswith("aws:kms") or head.get("SSECustomerAlgorithm")):
        return None
    return value.lower()
