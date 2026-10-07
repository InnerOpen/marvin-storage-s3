"""The s3 backup target beyond the conformance kit: the objects Marvin's old off-site job wrote stay
readable, digests are honest, deletes are batched, errors aren't swallowed. Ported from core's
tests/test_offsite_backup.py where the case is about S3 (the engine-level cases live with the engine)."""

import hashlib
from pathlib import Path

import pytest

from marvin_storage_s3 import S3BackupTarget
from marvin_storage_s3.target import S3DeleteError

DUMP = b"PGDMP" + b"\x00\x01" * 4096


def _old_script_put(raw, bucket: str, key: str, body: bytes, metadata: dict[str, str], content_type: str | None = None) -> None:
    """What scripts/offsite_backup.py's Bucket.put_file does: one put_object, metadata as given."""
    extra = {"ContentType": content_type} if content_type else {}
    raw.put_object(Bucket=bucket, Key=key, Body=body, Metadata=metadata, **extra)


def test_reads_history_the_old_offsite_job_wrote(connection, raw, tmp_path):
    """Same layout, same metadata: list sees the keys and sizes, head and get return the sha256 metadata
    the engine verifies, and a single-part ETag is the md5 the asset mirror compares."""
    sha = hashlib.sha256(DUMP).hexdigest()
    _old_script_put(raw, connection.bucket, "dev/postgres/marvin-20261006T120000Z.dump", DUMP, {"sha256": sha}, "application/octet-stream")
    _old_script_put(raw, connection.bucket, "dev/assets/ws/assets/2026/10/a.png", b"png-bytes", {})
    target = S3BackupTarget(connection)

    listed = target.list("dev/")
    assert sorted(listed) == ["dev/assets/ws/assets/2026/10/a.png", "dev/postgres/marvin-20261006T120000Z.dump"]
    dump = listed["dev/postgres/marvin-20261006T120000Z.dump"]
    assert (dump.size, dump.algorithm, dump.digest) == (len(DUMP), "md5", hashlib.md5(DUMP).hexdigest())
    asset = listed["dev/assets/ws/assets/2026/10/a.png"]
    assert (asset.algorithm, asset.digest) == ("md5", hashlib.md5(b"png-bytes").hexdigest())

    head = target.head("dev/postgres/marvin-20261006T120000Z.dump")
    assert head.metadata == {"sha256": sha}
    meta = target.get("dev/postgres/marvin-20261006T120000Z.dump", tmp_path / "out.dump")
    assert meta == {"sha256": sha}
    assert hashlib.sha256((tmp_path / "out.dump").read_bytes()).hexdigest() == sha


def test_writes_what_the_old_offsite_job_reads(connection, raw, tmp_path):
    """The other way round (rollback to the old CronJob): objects this target writes carry the metadata
    and content type the old script expects."""
    src = tmp_path / "db.gz"
    src.write_bytes(b"gz")
    S3BackupTarget(connection).put_file("sqlite/marvin-20261006T000000Z.db.gz", src, {"sha256": "aa", "db-sha256": "bb"}, "application/gzip")
    resp = raw.get_object(Bucket=connection.bucket, Key="sqlite/marvin-20261006T000000Z.db.gz")
    assert resp["Body"].read() == b"gz"
    assert resp["Metadata"] == {"sha256": "aa", "db-sha256": "bb"}
    assert resp["ContentType"] == "application/gzip"
    assert resp["ETag"].strip('"') == hashlib.md5(b"gz").hexdigest()


def test_metadata_keys_are_lower_cased(connection, tmp_path):
    src = tmp_path / "f"
    src.write_bytes(b"x")
    target = S3BackupTarget(connection)
    target.put_file("config/marvin-config-20261006T000000Z.tar.gz", src, {"SHA256": "abc"})
    assert target.head("config/marvin-config-20261006T000000Z.tar.gz").metadata == {"sha256": "abc"}


def test_large_files_go_up_in_parts_and_report_no_md5(connection, tmp_path):
    """Above the threshold the ETag is `<hash>-<parts>`, not the content's md5: no algorithm, so the
    engine compares sizes (and its own sha256 metadata on restore)."""
    src = tmp_path / "big.dump"
    src.write_bytes(b"\x07" * (6 * 1024 * 1024))
    target = S3BackupTarget(connection, multipart_threshold=5 * 1024 * 1024)
    target.put_file("postgres/marvin-20261006T000000Z.dump", src, {"sha256": "abc"})
    obj = target.head("postgres/marvin-20261006T000000Z.dump")
    assert obj.size == 6 * 1024 * 1024
    assert obj.algorithm == "" and "-" in obj.digest
    assert obj.metadata == {"sha256": "abc"}
    assert target.list("postgres/")["postgres/marvin-20261006T000000Z.dump"].algorithm == ""
    dest = tmp_path / "back"
    target.get("postgres/marvin-20261006T000000Z.dump", dest)
    assert dest.read_bytes() == src.read_bytes()


def test_list_pages_past_1000_keys(connection, raw, tmp_path):
    for i in range(1005):
        raw.put_object(Bucket=connection.bucket, Key=f"assets/ws/{i:05d}.txt", Body=b"x")
    target = S3BackupTarget(connection)
    assert len(target.list("assets/")) == 1005
    target.delete(sorted(target.list("assets/")))
    assert target.list("assets/") == {}


def test_describe_names_the_bucket_and_host_never_a_credential(connection):
    text = S3BackupTarget(connection).describe()
    assert connection.bucket in text
    assert connection.secret_key not in text and connection.access_key not in text


# --- with a stand-in client: batching and errors ---------------------------------------------------


class RecordingClient:
    def __init__(self, errors=None):
        self.calls: list[list[str]] = []
        self.errors = errors

    def delete_objects(self, Bucket, Delete):
        self.calls.append([o["Key"] for o in Delete["Objects"]])
        return {"Errors": self.errors} if self.errors else {}


def _target(client) -> S3BackupTarget:
    from marvin_storage_s3.client import S3Connection

    return S3BackupTarget(S3Connection(bucket="b", region="us-east-1"), client=client)


def test_delete_batches_by_1000():
    client = RecordingClient()
    _target(client).delete(f"k{i}" for i in range(2500))
    assert [len(c) for c in client.calls] == [1000, 1000, 500]


def test_delete_of_nothing_makes_no_request():
    client = RecordingClient()
    _target(client).delete([])
    assert client.calls == []


def test_delete_errors_raise_naming_the_key():
    client = RecordingClient(errors=[{"Key": "postgres/x.dump", "Code": "AccessDenied"}])
    with pytest.raises(S3DeleteError, match="postgres/x.dump AccessDenied"):
        _target(client).delete(["postgres/x.dump"])


def test_access_denied_is_not_reported_as_missing(connection, monkeypatch, tmp_path):
    """Only a 404 means "not there"; a 403 must surface, or the engine would think a target is empty."""
    from botocore.exceptions import ClientError

    target = S3BackupTarget(connection)

    def denied(**kwargs):
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

    monkeypatch.setattr(target.client, "head_object", denied)
    monkeypatch.setattr(target.client, "get_object", denied)
    with pytest.raises(ClientError):
        target.head("postgres/x.dump")
    with pytest.raises(ClientError):
        target.get("postgres/x.dump", Path(tmp_path / "out"))
