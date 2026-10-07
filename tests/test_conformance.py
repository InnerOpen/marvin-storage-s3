"""The SDK's conformance kit, on moto and (with MARVIN_S3_TEST_ENDPOINT) a real S3 API."""

import pytest
from marvin_integration_sdk.storage.testing import BackupTargetContract, StorageProviderContract

from marvin_storage_s3 import S3BackupTarget, S3StorageProvider


class TestProviderConformance(StorageProviderContract):
    @pytest.fixture
    def provider(self, connection):
        return S3StorageProvider(connection)


class TestProviderConformanceWithPrefixAndPublicUrl(StorageProviderContract):
    @pytest.fixture
    def provider(self, connection):
        return S3StorageProvider(connection, prefix="prod", public_base_url="https://assets.example.com/")


class TestTargetConformance(BackupTargetContract):
    @pytest.fixture
    def target(self, connection):
        return S3BackupTarget(connection)


class TestTargetConformanceMultipart(BackupTargetContract):
    """Every upload goes up in parts (threshold 1 byte): digests must then stay honest (no algorithm)."""

    @pytest.fixture
    def target(self, connection):
        return S3BackupTarget(connection, multipart_threshold=1)
