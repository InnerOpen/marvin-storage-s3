"""S3-compatible storage for Marvin: Cloudflare R2, AWS S3, MinIO, Backblaze B2.

One plugin, slug ``s3``, with both sides of the storage contract:

- the asset provider (``STORAGE_PROVIDER=s3``): uploads live in a bucket, served from its public
  custom domain or through presigned URLs;
- the backup target (a ``backup.targets[]`` entry of type ``s3``): database dumps, the config archive
  and the asset mirror go to a bucket, in the same layout Marvin's old off-site job wrote.
"""

from marvin_integration_sdk.storage import StoragePlugin

from .provider import S3StorageProvider
from .target import S3BackupTarget

__all__ = ["S3BackupTarget", "S3StorageProvider", "plugin"]


def plugin() -> StoragePlugin:
    """The ``marvin.storage_providers`` entry point."""
    return StoragePlugin(
        slug="s3",
        name="S3-compatible (R2, AWS S3, MinIO, B2)",
        provider=S3StorageProvider,
        target=S3BackupTarget,
        description="Asset storage and backups in an S3-compatible bucket: Cloudflare R2, AWS S3, MinIO, Backblaze B2.",
    )
