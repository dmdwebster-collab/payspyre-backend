"""KYC document storage on DigitalOcean Spaces (S3-compatible).

Used for the MANUAL-application fallback, where the borrower uploads identity
documents (ID front/back, selfie) by hand. The browser uploads DIRECTLY to Spaces
via a short-TTL presigned PUT URL — the file never passes through the API. The
backend persists only the object key + metadata (PlatformApplicationDocument).

Compliance (see docs/compliance_reference.md, researched 2026-06-26):
  * Private bucket (ACL=private); objects are never public.
  * Encryption at rest is on by default in Spaces; TLS in transit via https URLs.
  * Downloads only via short-TTL presigned GET URLs, gated by admin RBAC + audited.
  * Purpose-bound retention / auto-purge is configured as a bucket lifecycle rule
    (reconcile with any FINTRAC record-keeping floor).

INERT UNTIL CONFIGURED: with no SPACES_* credentials set, ``is_configured()`` is
False and the URL helpers raise ``StorageNotConfigured``; the endpoints surface
this as 503 so the feature lights up the moment the Space + keys are wired.
"""
from __future__ import annotations

import boto3
from botocore.config import Config as BotoConfig

from app.core.config import settings

# Allowed document types (the manual KYC set). Co-borrower docs are a follow-up.
DOCUMENT_TYPES = ("id_front", "id_back", "selfie")

_PUT_TTL_SECONDS = 900   # 15 min — generous for a mobile photo upload
_GET_TTL_SECONDS = 300   # 5 min — admin review download


class StorageNotConfigured(Exception):
    """Raised when a storage operation is attempted but SPACES_* is unset."""


def is_configured() -> bool:
    """True only when every credential needed to talk to Spaces is present."""
    return bool(
        settings.SPACES_BUCKET
        and settings.SPACES_KEY
        and settings.SPACES_SECRET
        and settings.SPACES_REGION
    )


def _endpoint() -> str:
    return settings.SPACES_ENDPOINT or f"https://{settings.SPACES_REGION}.digitaloceanspaces.com"


def _client():
    if not is_configured():
        raise StorageNotConfigured("Document storage is not configured (SPACES_* unset).")
    return boto3.client(
        "s3",
        region_name=settings.SPACES_REGION,
        endpoint_url=_endpoint(),
        aws_access_key_id=settings.SPACES_KEY,
        aws_secret_access_key=settings.SPACES_SECRET,
        config=BotoConfig(signature_version="s3v4"),
    )


def build_object_key(application_id, doc_type: str, document_id) -> str:
    """Namespaced, non-guessable key: applications/<app>/<type>/<doc-id>."""
    return f"applications/{application_id}/{doc_type}/{document_id}"


def presigned_put_url(object_key: str, content_type: str, expires: int = _PUT_TTL_SECONDS) -> str:
    """A short-TTL URL the browser PUTs the file to. ACL forced private."""
    return _client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.SPACES_BUCKET,
            "Key": object_key,
            "ContentType": content_type,
            "ACL": "private",
        },
        ExpiresIn=expires,
    )


def presigned_get_url(object_key: str, expires: int = _GET_TTL_SECONDS) -> str:
    """A short-TTL URL for an authorized reviewer to download the object."""
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.SPACES_BUCKET, "Key": object_key},
        ExpiresIn=expires,
    )
