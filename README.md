# marvin-storage-s3

S3-compatible storage for [Marvin](https://github.com/InnerOpen/marvin): **Cloudflare R2, AWS S3,
MinIO and Backblaze B2**. One plugin (slug `s3`) gives Marvin two things:

- **Asset storage** (`STORAGE_PROVIDER=s3`): uploaded files (images, documents, generated media) live in
  a bucket instead of the `marvin-data` volume. Browsers load them from the bucket's public custom
  domain, or through presigned URLs when there isn't one.
- **A backup target** (a `backup.targets[]` entry of type `s3`): Marvin's backup engine writes to a
  bucket, each target on its own schedule and retention. A run stores
  - the database: a `pg_dump` (Postgres) or a consistent SQLite snapshot, with a `sha256` checked on
    restore,
  - the config archive: `.secret`, `scheduler_state.json`, `templates/`,
  - an incremental mirror of every asset (only new or changed files are copied).

`boto3` lives only in this package: Marvin core has no cloud SDK.

> **The backup bucket holds `.secret`**, the key that decrypts every secret Marvin stores (integration
> tokens, API keys). Anyone who can read that bucket can read them. Keep it private, give it its own
> credentials scoped to that one bucket, and never reuse the asset bucket (which is public) for backups.

## Install

Marvin installs site-wide plugins at pod start from the chart's `plugins.packages` list (into every
backend pod and every backup CronJob). List the SDK too, so pip can resolve this package's requirement;
the chart removes that copy afterwards, so the SDK the Marvin image pins is the one that loads.

```yaml
plugins:
  packages:
    - https://github.com/InnerOpen/marvin-integration-sdk/archive/refs/heads/develop.tar.gz
    - https://github.com/InnerOpen/marvin-storage-s3/archive/refs/heads/main.tar.gz
```

Requires Marvin with the storage plugin contract (`marvin-integration-sdk` 0.7+). pip puts
`boto3`, `botocore` and their dependencies (`s3transfer`, `urllib3`, `python-dateutil`, `jmespath`,
`six`; about 35 MB) into the plugins volume on every pod start. `PYTHONPATH` comes before the image's
site-packages, so at runtime these copies (the newest releases pip finds) replace the image's own
versions; Marvin core still ships `boto3` itself for now.

Marvin core has a temporary built-in `s3` provider for asset storage. Once this plugin is installed it
replaces that one (the log says `storage plugin 's3' replaces core's built-in 's3'`) and reads the same
settings.

To check it loaded, open **Admin → Plugins**: `marvin-storage-s3` is listed with kind *storage*,
providing *assets* and *backups*.

## Backups

Each target gets its own CronJob (`marvin-backup-<name>`). The target reads the keys of the
`marvin-r2-backup` Secret that Marvin's old off-site job used, so that Secret works unchanged:

```yaml
backup:
  targets:
    - name: r2
      type: s3
      schedule: "0 * * * *"
      timeZone: America/New_York
      retention: {hourly: 48, daily: 30, weekly: 8}
      existingSecret: marvin-r2-backup   # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET
      # prefix: dev/                     # optional folder in the bucket (BACKUP_PREFIX)
```

| Setting | Required | Default | What it is |
|---|---|---|---|
| `BACKUP_S3_BUCKET` | yes | | The bucket. |
| `BACKUP_S3_ENDPOINT` | | empty = AWS | The S3 API URL (see the per-provider table below). |
| `BACKUP_S3_REGION` | | `auto` | `auto` for R2; the bucket's region for AWS and B2. |
| `AWS_ACCESS_KEY_ID` | | | Access key ID. Leave both keys empty to use boto3's default chain (e.g. an IAM role). |
| `AWS_SECRET_ACCESS_KEY` | | | Secret access key. **Masked** on admin pages and in logs. |
| `BACKUP_S3_ADDRESSING_STYLE` | | `path` with an endpoint, `virtual` on AWS | `path`, `virtual` or `auto`. |
| `BACKUP_S3_CHECKSUMS` | | `when_required` | `when_required` or `when_supported` (see "Checksums" below). |

The key prefix belongs to the engine, not the target: `prefix:` in the values sets `BACKUP_PREFIX`, and
the engine falls back to `BACKUP_S3_PREFIX` as the old job's environment had it.

**Same layout as the old off-site job.** Keys are `postgres/marvin-<UTC stamp>.dump`,
`sqlite/marvin-<stamp>.db.gz`, `config/marvin-config-<stamp>.tar.gz` and `assets/<asset key>`, with the
`sha256` (and `db-sha256`) as S3 user metadata. A target pointed at the old bucket reads, restores and
prunes the history already there, and its asset mirror sees the old copies as unchanged. It works the
other way too: the old job can read what this target writes, so a rollback needs nothing extra.

**How unchanged assets are recognised.** A file up to 64 MiB goes up in one PUT, so its ETag is its
MD5, and the engine compares that with the asset's own MD5 (as the old job did). Bigger files go up in
parts. Their ETag is not a content hash, so for those the size decides (asset keys carry a UUID, so a
changed file under the same key is rare) and restores check the `sha256` metadata. On AWS buckets with
SSE-KMS or SSE-C encryption no ETag is an MD5. Backups and restores still work there, but the asset
mirror copies every asset again on each run, so use SSE-S3 (the default) for a backup bucket.

## Asset storage

```bash
STORAGE_S3_BUCKET=marvin-assets
STORAGE_S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
STORAGE_S3_ACCESS_KEY=...          # from a Secret
STORAGE_S3_SECRET_KEY=...          # from a Secret
STORAGE_REMOTE_PUBLIC_URL=https://assets.example.com
```

| Setting | Required | Default | What it is |
|---|---|---|---|
| `STORAGE_S3_BUCKET` | yes | | The bucket. |
| `STORAGE_S3_ENDPOINT` | | empty = AWS | The S3 API URL. |
| `STORAGE_S3_REGION` | | `auto` | `auto` for R2; the bucket's region for AWS and B2. |
| `STORAGE_S3_ACCESS_KEY` | | | Access key ID (leave it and the secret empty to use boto3's default chain). |
| `STORAGE_S3_SECRET_KEY` | | | Secret access key. **Masked.** |
| `STORAGE_S3_PREFIX` | | | Optional folder in the bucket, e.g. `prod/`. Asset rows keep the plain key. |
| `STORAGE_REMOTE_PUBLIC_URL` | | | The bucket's public base URL (custom domain). Asset URLs are `<base>/<prefix><key>`. |
| `STORAGE_S3_PRESIGN_SECONDS` | | `3600` | Lifetime of presigned URLs, used only without a public URL (max 604800). |
| `STORAGE_S3_CACHE_CONTROL` | | | `Cache-Control` stored with each new object, which the CDN in front of the public domain and browsers honour, e.g. `public, max-age=86400`. Asset keys carry a UUID, so a long lifetime is safe; avoid `immutable` (a repair tool may rewrite a file in place). |
| `STORAGE_S3_ADDRESSING_STYLE` | | `path` with an endpoint, `virtual` on AWS | `path`, `virtual` or `auto`. |
| `STORAGE_S3_CHECKSUMS` | | `when_required` | `when_required` or `when_supported`. |

**Public URLs.** Set `STORAGE_REMOTE_PUBLIC_URL` to a public custom domain on the bucket. That's the
recommended setup: files are cached at the edge, and the URLs stay stable, so published sites can keep
them. Without it, asset URLs are presigned GETs that expire. The bare endpoint URL is never handed out,
because on R2 that is the private S3 API and browsers can't read from it.

**Per-workspace domains.** A platform admin can give one workspace its own public domain (Marvin's
**Admin → Storage**). Marvin then builds that workspace's URLs from this provider with
`STORAGE_REMOTE_PUBLIC_URL` replaced by the workspace's domain, so the domain must serve the same bucket
(another R2 custom domain on it, or a Cloudflare for SaaS custom hostname pointing at one). The key, and
`STORAGE_S3_PREFIX`, stay in the path.

**Download names.** Marvin's keys are opaque (`<workspace code>/<yyyy>/<mm>/<uuid>.<ext>`): no filename.
A `content_disposition` entry in an upload's metadata (Marvin sends `inline; filename="<original
name>"`, with `filename*=UTF-8''…` for non-ASCII names) is stored as the object's `Content-Disposition`,
not as user metadata, so a browser's *Save as* on the public URL offers the real name. A value that
isn't printable ASCII is dropped (the upload still succeeds). Objects uploaded before this have none;
Marvin's `storage_migrate --rekey` copies them to opaque keys with it.

**Turning it on.** With the plugin installed and the settings above present, a platform admin chooses
where new uploads go under **Admin → Storage** (`STORAGE_PROVIDER` is only the default, so it can stay
`local`), and can switch back at any time. Existing assets keep serving from wherever their row says
they live, so switching never breaks a URL. Moving the old files is Marvin's
`python -m marvin.scripts.storage_migrate --to s3 --rekey [--dry-run] [--verify]` (and `--to local` to roll
back); `/assets/<key>` URLs stored before the move redirect to the new location. If the plugin or its
settings go missing later, new uploads fall back to `STORAGE_PROVIDER` and the Storage page says why.

**Backups of these assets.** A backup job mirrors assets from local disk and `STORAGE_PROVIDER`; add
`BACKUP_ASSET_PROVIDERS=s3` and these `STORAGE_S3_*` settings to a backup target's environment to
mirror this bucket too (bucket to bucket, on R2 or to a NAS target).

**Browsers and CORS.** `<img>`, `<video>` and links need no CORS. Add a CORS rule on the bucket
(GET/HEAD from your sites' origins) only if a page `fetch()`es assets or draws them to a canvas it
reads back.

Each upload's `sha256` is stored as object metadata, so the backup engine can check an asset without
downloading it.

## Credentials per provider

Create one set of credentials **per bucket** (one for backups, another for assets), limited to that bucket.

| Provider | Create the credentials | Endpoint (`*_ENDPOINT`) | Region (`*_REGION`) | Notes |
|---|---|---|---|---|
| **Cloudflare R2** | Dashboard → **R2 Object Storage** → **Manage API tokens** → *Create API token*: permission **Object Read & Write**, *Apply to specific buckets only* → the one bucket. Copy the **Access Key ID** and **Secret Access Key** (shown once). | `https://<account-id>.r2.cloudflarestorage.com` (the bucket's *S3 API* URL without the bucket name; EU jurisdiction: `https://<account-id>.eu.r2.cloudflarestorage.com`) | `auto` | Public assets: bucket → *Settings* → *Custom Domains* → connect a domain on your Cloudflare zone, and use it as `STORAGE_REMOTE_PUBLIC_URL`. The `r2.dev` URL is rate-limited, so keep it out of production. |
| **AWS S3** | IAM → *Users* → create a user (or a role for EKS/EC2) → attach the policy below, scoped to the bucket → *Security credentials* → *Create access key*. | empty | the bucket's region, e.g. `us-east-1` | `auto` is refused on AWS. With an IAM role, leave both keys empty. |
| **MinIO** | Console → **Access Keys** → *Create access key* (attach a policy limited to the bucket), or `mc admin user svcacct add <alias> <user> --policy policy.json`. | the MinIO **API** URL, e.g. `https://minio.example.com` (port 9000 by default, not the console's 9001) | `auto` works; use the server's region if `MINIO_SITE_REGION` is set | Path-style addressing, the default when an endpoint is set. |
| **Backblaze B2** | B2 → **Application Keys** → *Add a New Application Key*: *Allow access to Bucket(s)* → the one bucket, *Read and Write*. The **keyID** is the access key ID, the **applicationKey** the secret (shown once). The master application key doesn't work with the S3 API. | `https://s3.<region>.backblazeb2.com`, as shown on the bucket's details (*Endpoint*) | the middle of the endpoint, e.g. `us-west-004` | |

AWS policy for one bucket (replace `marvin-backups`). The same policy works on MinIO:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::marvin-backups"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::marvin-backups/*"
    }
  ]
}
```

**Checksums.** Since 1.36, boto3 adds CRC checksums to every upload by default. Several S3-compatible
services (R2 and B2 among them when that release came out, and older MinIO versions) rejected or
mishandled some of them. So this plugin sends a checksum only when the API requires one
(`when_required`, which is what Marvin's old off-site job did). On AWS or a current MinIO you can set
`when_supported`.

## Development

```bash
uv run --extra dev pytest          # moto, in-process
uv run --extra dev ruff check . && uv run --extra dev ruff format --check .
```

The SDK's conformance kit runs against both sides on moto and, with `MARVIN_S3_TEST_ENDPOINT` set, on a
real S3 API (CI starts MinIO):

```bash
docker run -d --rm --name marvin-s3-test -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin cgr.dev/chainguard/minio server /data
MARVIN_S3_TEST_ENDPOINT=http://localhost:9000 uv run --extra dev pytest
docker stop marvin-s3-test
```

The dev environment takes the SDK from the commit Marvin core pins (`[tool.uv.sources]`), so the tests
run against the contract the Marvin image ships.
