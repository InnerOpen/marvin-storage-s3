"""Settings (names, defaults, validation, masking) and the plugin's entry point."""

from importlib.metadata import entry_points

import pytest
from marvin_integration_sdk.storage import ENTRY_POINT_GROUP, StorageConfigError, StoragePlugin, masked_config, read_config

from marvin_storage_s3 import S3BackupTarget, S3StorageProvider, plugin

R2 = "https://0123456789abcdef.r2.cloudflarestorage.com"


def _env_names(cls) -> list[str]:
    return [s.env for s in cls.settings]


def test_backup_target_reads_the_marvin_r2_backup_secret_names():
    """Slice 6 reuses the existing Secret unchanged: these are its keys (plus optional extras)."""
    assert _env_names(S3BackupTarget)[:5] == [
        "BACKUP_S3_BUCKET",
        "BACKUP_S3_ENDPOINT",
        "BACKUP_S3_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ]
    assert "BACKUP_S3_PREFIX" not in _env_names(S3BackupTarget)  # the engine applies the prefix; declaring it here would double it


def test_asset_provider_reads_core_storage_names():
    names = _env_names(S3StorageProvider)
    for env in (
        "STORAGE_S3_BUCKET",
        "STORAGE_S3_ENDPOINT",
        "STORAGE_S3_REGION",
        "STORAGE_S3_ACCESS_KEY",
        "STORAGE_S3_SECRET_KEY",
        "STORAGE_REMOTE_PUBLIC_URL",
    ):
        assert env in names


def test_secrets_are_masked():
    env = {"BACKUP_S3_BUCKET": "b", "BACKUP_S3_ENDPOINT": R2, "AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "s3cr3t"}
    shown = masked_config(S3BackupTarget.settings, read_config(S3BackupTarget.settings, env))
    assert shown["AWS_SECRET_ACCESS_KEY"] == "****"
    assert shown["AWS_ACCESS_KEY_ID"] == "id"
    assert "s3cr3t" not in str(shown)
    assert (
        masked_config(S3StorageProvider.settings, read_config(S3StorageProvider.settings, {"STORAGE_S3_BUCKET": "b", "STORAGE_S3_SECRET_KEY": "x"}))[
            "STORAGE_S3_SECRET_KEY"
        ]
        == "****"
    )


def test_r2_target_from_the_backup_secret():
    env = {"BACKUP_S3_BUCKET": "marvin-backups-dev", "BACKUP_S3_ENDPOINT": R2, "AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "secret"}
    target = S3BackupTarget.from_config(read_config(S3BackupTarget.settings, env))
    conn = target.connection
    assert (conn.bucket, conn.endpoint, conn.region, conn.checksums) == ("marvin-backups-dev", R2, "auto", "when_required")
    assert target.client.meta.config.s3 == {"addressing_style": "path"}
    assert target.client.meta.config.request_checksum_calculation == "when_required"
    assert target.client.meta.config.response_checksum_validation == "when_required"
    assert target.client.meta.endpoint_url == R2
    assert target.describe() == "s3://marvin-backups-dev (0123456789abcdef.r2.cloudflarestorage.com)"


def test_aws_defaults_to_virtual_hosted_addressing_and_needs_a_region():
    env = {"BACKUP_S3_BUCKET": "b", "BACKUP_S3_REGION": "eu-west-1"}
    target = S3BackupTarget.from_config(read_config(S3BackupTarget.settings, env))
    assert target.client.meta.config.s3 == {"addressing_style": "virtual"}
    assert target.describe() == "s3://b (aws:eu-west-1)"
    with pytest.raises(StorageConfigError, match="BACKUP_S3_REGION must be the bucket's AWS region"):
        S3BackupTarget.from_config(read_config(S3BackupTarget.settings, {"BACKUP_S3_BUCKET": "b"}))


def test_missing_bucket_names_the_variable():
    with pytest.raises(StorageConfigError, match="BACKUP_S3_BUCKET"):
        read_config(S3BackupTarget.settings, {})
    with pytest.raises(StorageConfigError, match="STORAGE_S3_BUCKET"):
        read_config(S3StorageProvider.settings, {})


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"AWS_ACCESS_KEY_ID": "id"}, "AWS_SECRET_ACCESS_KEY"),
        ({"AWS_SECRET_ACCESS_KEY": "do-not-print"}, "AWS_ACCESS_KEY_ID"),
        ({"BACKUP_S3_ENDPOINT": "r2.example.com"}, "BACKUP_S3_ENDPOINT must be an http"),
        ({"BACKUP_S3_ADDRESSING_STYLE": "dns"}, "BACKUP_S3_ADDRESSING_STYLE"),
        ({"BACKUP_S3_CHECKSUMS": "always"}, "BACKUP_S3_CHECKSUMS"),
    ],
)
def test_bad_settings_name_the_variable_never_a_secret(env, message):
    base = {"BACKUP_S3_BUCKET": "b", "BACKUP_S3_ENDPOINT": R2}
    with pytest.raises(StorageConfigError, match=message) as err:
        S3BackupTarget.from_config(read_config(S3BackupTarget.settings, {**base, **env}))
    assert "do-not-print" not in str(err.value)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"STORAGE_S3_PRESIGN_SECONDS": "0"}, "STORAGE_S3_PRESIGN_SECONDS"),
        ({"STORAGE_S3_PRESIGN_SECONDS": "604801"}, "STORAGE_S3_PRESIGN_SECONDS"),
        ({"STORAGE_REMOTE_PUBLIC_URL": "assets.example.com"}, "STORAGE_REMOTE_PUBLIC_URL"),
    ],
)
def test_bad_provider_settings(env, message):
    base = {"STORAGE_S3_BUCKET": "b", "STORAGE_S3_ENDPOINT": R2}
    with pytest.raises(StorageConfigError, match=message):
        S3StorageProvider.from_config(read_config(S3StorageProvider.settings, {**base, **env}))


def test_provider_from_core_style_settings():
    env = {
        "STORAGE_S3_BUCKET": "marvin-assets",
        "STORAGE_S3_ENDPOINT": R2,
        "STORAGE_S3_ACCESS_KEY": "id",
        "STORAGE_S3_SECRET_KEY": "secret",
        "STORAGE_REMOTE_PUBLIC_URL": "https://assets.iwobble.com",
        "STORAGE_S3_PREFIX": "prod",
    }
    provider = S3StorageProvider.from_config(read_config(S3StorageProvider.settings, env))
    assert provider.get_public_url("ws/a.png") == "https://assets.iwobble.com/prod/ws/a.png"
    assert provider.connection.region == "auto"


def test_plugin_offers_both_sides_under_one_slug():
    p = plugin()
    assert isinstance(p, StoragePlugin)
    assert (p.slug, p.provider, p.target) == ("s3", S3StorageProvider, S3BackupTarget)
    assert S3StorageProvider.slug == S3BackupTarget.slug == "s3"
    names = [s.env for s in p.settings]
    assert len(names) == len(set(names))
    assert "STORAGE_S3_BUCKET" in names and "BACKUP_S3_BUCKET" in names


def test_entry_point_is_installed():
    (ep,) = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == "s3"]
    assert ep.value == "marvin_storage_s3:plugin"
    assert ep.load()().slug == "s3"
