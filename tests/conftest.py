"""Two S3 backends for every test that asks for ``connection``: moto (in-process, always) and a real S3
API (``minio``, when ``MARVIN_S3_TEST_ENDPOINT`` points at one, e.g. a throwaway MinIO container):

    docker run -d --rm --name marvin-s3-test -p 9000:9000 \\
      -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin cgr.dev/chainguard/minio server /data
    MARVIN_S3_TEST_ENDPOINT=http://localhost:9000 uv run --extra dev pytest

Each test gets a fresh, empty bucket; MinIO buckets are emptied and removed afterwards.
"""

from __future__ import annotations

import os
import uuid

import boto3
import pytest

from marvin_storage_s3.client import S3Connection

MINIO_ENDPOINT = os.environ.get("MARVIN_S3_TEST_ENDPOINT", "")
MINIO_KEY = os.environ.get("MARVIN_S3_TEST_ACCESS_KEY", "minioadmin")
MINIO_SECRET = os.environ.get("MARVIN_S3_TEST_SECRET_KEY", "minioadmin")


@pytest.fixture
def moto_env(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    from moto import mock_aws

    with mock_aws():
        yield


def _moto_connection() -> S3Connection:
    conn = S3Connection(bucket=f"marvin-test-{uuid.uuid4().hex[:12]}", region="us-east-1", access_key="testing", secret_key="testing")
    conn.client().create_bucket(Bucket=conn.bucket)
    return conn


def _empty_and_remove(client, bucket: str) -> None:
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
    client.delete_bucket(Bucket=bucket)


@pytest.fixture(params=["moto", pytest.param("minio", marks=pytest.mark.minio)])
def connection(request, monkeypatch):
    """An S3Connection to a fresh, empty bucket on each backend."""
    if request.param == "moto":
        request.getfixturevalue("moto_env")
        yield _moto_connection()
        return
    if not MINIO_ENDPOINT:
        pytest.skip("MARVIN_S3_TEST_ENDPOINT not set")
    conn = S3Connection(
        bucket=f"marvin-test-{uuid.uuid4().hex[:12]}",
        endpoint=MINIO_ENDPOINT,
        region="us-east-1",
        access_key=MINIO_KEY,
        secret_key=MINIO_SECRET,
    )
    client = conn.client()
    client.create_bucket(Bucket=conn.bucket)
    try:
        yield conn
    finally:
        _empty_and_remove(client, conn.bucket)


@pytest.fixture
def raw(connection):
    """A plain boto3 client on the same bucket, for writing objects the way other tools (the old
    off-site script) did and for inspecting what the plugin wrote."""
    from botocore.config import Config

    kwargs = {
        "region_name": connection.region,
        "aws_access_key_id": connection.access_key,
        "aws_secret_access_key": connection.secret_key,
        # As scripts/offsite_backup.py configured its client.
        "config": Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required", response_checksum_validation="when_required"),
    }
    if connection.endpoint:
        kwargs["endpoint_url"] = connection.endpoint
    return boto3.client("s3", **kwargs)
