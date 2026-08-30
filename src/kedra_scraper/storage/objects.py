"""S3-compatible object storage for the landing and curated zones.

Written against plain boto3 rather than a MinIO client, so the same code runs
unchanged against real AWS S3. Switching is a change to `S3_ENDPOINT_URL`.

The landing zone is append-only by policy: the brief requires stored data not to
be deleted or updated, so `put_if_absent` is the only write path and a changed
document is written under a new key rather than over the old one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from kedra_scraper.config import S3Settings

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from mypy_boto3_s3.client import S3Client

# Content types we set explicitly. Guessing from the extension is fine here
# because the set of document types this site serves is small and known; a
# wrong guess would only affect how a browser offers the object for download.
_CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "rtf": "application/rtf",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def content_type_for(filename: str) -> str:
    """Best-effort content type from a filename extension."""
    _, _, extension = filename.rpartition(".")
    return _CONTENT_TYPES.get(extension.lower(), _DEFAULT_CONTENT_TYPE)


class ObjectStore:
    """Thin wrapper over the S3 API, scoped to what this pipeline needs."""

    def __init__(self, client: S3Client) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: S3Settings) -> ObjectStore:
        client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region,
        )
        return cls(client)

    def exists(self, bucket: str, key: str) -> bool:
        """Whether an object is already present.

        `head_object` rather than `get_object`: the answer is in the response
        headers, so this avoids transferring the body just to learn a boolean.
        """
        try:
            self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            # A missing object is an expected answer, not an error. Anything
            # else -- credentials, permissions, a bad bucket -- must propagate,
            # because treating those as "absent" would trigger a re-upload that
            # then fails in a more confusing place.
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def stored_content_hash(self, bucket: str, key: str) -> str | None:
        """The content hash recorded on an existing object, or None if absent.

        Written as S3 user metadata at upload time so this is a HEAD rather than
        a download. Comparing hashes is what lets a re-run distinguish "the same
        document is already here" from "a different version is already here" --
        the first is a no-op, the second must be written to a new key because the
        landing zone is append-only.
        """
        try:
            response = self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        # S3 lowercases user metadata keys; boto3 returns them already stripped
        # of the `x-amz-meta-` prefix.
        return response.get("Metadata", {}).get("content-hash")

    def put_if_absent(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        content_hash: str = "",
    ) -> bool:
        """Write `data` unless `key` already exists. Returns True if written.

        The return value is what lets a re-run report how much work it actually
        did, rather than reporting the number of documents it considered.

        This is check-then-write, so it is not atomic: two concurrent crawls of
        the same partition could both see the key as absent and both upload.
        That is acceptable here because a given key only ever holds one version's
        bytes, so the two writes are byte-identical and the loser costs only
        bandwidth. It would not be acceptable for mutable keys.
        """
        if self.exists(bucket, key):
            return False
        self._client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type or _DEFAULT_CONTENT_TYPE,
            Metadata={"content-hash": content_hash} if content_hash else {},
        )
        return True

    def get_bytes(self, bucket: str, key: str) -> bytes:
        """Read an object back in full. Used by the transform stage and by verify."""
        response = self._client.get_object(Bucket=bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def put(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        """Unconditional write, for the curated zone only.

        Curated output is derived and reproducible, so overwriting it when the
        transform is re-run is correct. The landing zone must never use this.
        """
        self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
