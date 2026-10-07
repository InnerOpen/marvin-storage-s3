"""The s3 asset provider beyond the conformance kit."""

import hashlib
from io import BytesIO
from urllib.parse import urlsplit

import pytest

from marvin_storage_s3 import S3StorageProvider
from marvin_storage_s3.client import S3Connection
from marvin_storage_s3.provider import object_metadata


def test_public_url_uses_the_custom_domain_with_prefix_and_quoting(connection):
    provider = S3StorageProvider(connection, prefix="/prod/", public_base_url="https://assets.example.com/")
    assert provider.get_public_url("ws/assets/2026/10/u-my photo.jpg") == "https://assets.example.com/prod/ws/assets/2026/10/u-my%20photo.jpg"


def test_without_a_public_url_it_presigns_and_never_hands_out_the_bare_endpoint(connection):
    provider = S3StorageProvider(connection, presign_seconds=600)
    provider.put("ws/a.txt", BytesIO(b"hello"), "text/plain")
    url = provider.get_public_url("ws/a.txt")
    assert "X-Amz-Signature=" in url and "X-Amz-Expires=600" in url
    assert urlsplit(url).path.endswith("/ws/a.txt")


def test_presigned_url_serves_the_file(connection):
    """A browser can fetch it (real HTTP; moto intercepts requests through botocore only, so MinIO-only)."""
    if not connection.endpoint:
        pytest.skip("needs a real S3 API")
    import urllib.request

    provider = S3StorageProvider(connection)
    provider.put("ws/a.txt", BytesIO(b"hello"), "text/plain")
    with urllib.request.urlopen(provider.get_public_url("ws/a.txt"), timeout=10) as resp:
        assert resp.read() == b"hello"


def test_prefix_keeps_keys_plain_and_environments_apart(connection, raw):
    prod = S3StorageProvider(connection, prefix="prod")
    dev = S3StorageProvider(connection, prefix="dev/")
    prod.put("ws/a.txt", BytesIO(b"p"), "text/plain")
    dev.put("ws/a.txt", BytesIO(b"d"), "text/plain")
    assert list(prod.iter_keys()) == ["ws/a.txt"]
    with prod.get("ws/a.txt") as fh:
        assert fh.read() == b"p"
    keys = sorted(o["Key"] for o in raw.list_objects_v2(Bucket=connection.bucket)["Contents"])
    assert keys == ["dev/ws/a.txt", "prod/ws/a.txt"]
    assert prod.delete("ws/a.txt") is True
    assert dev.exists("ws/a.txt") is True


def test_put_stores_content_type_and_sha256_so_checksums_need_no_download(connection, raw):
    provider = S3StorageProvider(connection)
    meta = provider.put("ws/a.png", BytesIO(b"png"), "image/png", {"original_filename": "a.png"})
    sha = hashlib.sha256(b"png").hexdigest()
    assert (meta.checksum, meta.checksum_algorithm, meta.size) == (sha, "sha256", 3)
    head = raw.head_object(Bucket=connection.bucket, Key="ws/a.png")
    assert head["ContentType"] == "image/png"
    assert head["Metadata"] == {"original_filename": "a.png", "sha256": sha}
    assert provider.checksum("ws/a.png", "sha256") == sha
    assert provider.checksum("ws/a.png", "md5") == hashlib.md5(b"png").hexdigest()
    got = provider.get_metadata("ws/a.png")
    assert (got.content_type, got.checksum, got.checksum_algorithm) == ("image/png", sha, "sha256")


def test_cache_control_is_stored_with_new_objects_when_set(connection, raw):
    S3StorageProvider(connection).put("ws/plain.png", BytesIO(b"png"), "image/png")
    assert "CacheControl" not in raw.head_object(Bucket=connection.bucket, Key="ws/plain.png")
    S3StorageProvider(connection, cache_control="public, max-age=86400").put("ws/cached.png", BytesIO(b"png"), "image/png")
    assert raw.head_object(Bucket=connection.bucket, Key="ws/cached.png")["CacheControl"] == "public, max-age=86400"


def test_objects_written_by_other_tools_fall_back_to_the_etag_md5(connection, raw):
    raw.put_object(Bucket=connection.bucket, Key="ws/old.txt", Body=b"old")
    provider = S3StorageProvider(connection)
    assert provider.checksum("ws/old.txt", "sha256") is None
    meta = provider.get_metadata("ws/old.txt")
    assert (meta.checksum, meta.checksum_algorithm) == (hashlib.md5(b"old").hexdigest(), "md5")


def test_large_upload_streams_through_a_spool(connection):
    data = b"\x01" * (9 * 1024 * 1024)  # past the in-memory spool
    provider = S3StorageProvider(connection)
    assert provider.put("ws/big.bin", BytesIO(data), "application/octet-stream").size == len(data)
    with provider.get("ws/big.bin") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == hashlib.sha256(data).hexdigest()


def test_object_metadata_keeps_only_what_fits_in_headers():
    kept = object_metadata(
        {"original_filename": "a.png", "Alt": "x", "café": "y", "note": "café", "nested": {"a": 1}, "none": None, "sha256": "spoof"}
    )
    assert kept == {"original_filename": "a.png", "alt": "x"}
    assert sum(len(k) + len(v) for k, v in object_metadata({f"k{i}": "v" * 100 for i in range(40)}).items()) <= 1800


def test_access_denied_is_not_reported_as_missing(connection, monkeypatch):
    from botocore.exceptions import ClientError

    provider = S3StorageProvider(connection)

    def denied(**kwargs):
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

    monkeypatch.setattr(provider.client, "head_object", denied)
    with pytest.raises(ClientError):
        provider.exists("ws/a.txt")
    with pytest.raises(ClientError):
        provider.delete("ws/a.txt")


def test_kms_etags_are_not_taken_for_md5():
    from marvin_storage_s3.client import etag_md5

    md5 = hashlib.md5(b"x").hexdigest()
    assert etag_md5(f'"{md5}"') == md5
    assert etag_md5(f'"{md5}"', {"ServerSideEncryption": "aws:kms"}) is None
    assert etag_md5(f'"{md5}"', {"SSECustomerAlgorithm": "AES256"}) is None
    assert etag_md5(f'"{md5}-3"') is None
    assert etag_md5("") is None


def test_repr_hides_the_secret():
    conn = S3Connection(bucket="b", region="us-east-1", access_key="AKIAEXAMPLE", secret_key="super-secret")
    assert "super-secret" not in repr(conn)
