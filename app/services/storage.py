import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.core.config import settings


class StorageService:
    _instance = None
    _s3_client = None
    _s3_resource = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _initialize(self):
        if self._initialized:
            return

        access_key = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        bucket_name = os.getenv("AWS_S3_BUCKET")
        region = os.getenv("AWS_REGION", "us-east-1")

        if not all([access_key, secret_key, bucket_name]):
            # In dev mode, we'll allow initialization without AWS creds
            # but methods that need S3 will fail
            self._initialized = True
            return

        self._s3_client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._s3_resource = boto3.resource(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._bucket_name = bucket_name
        self._region = region
        self._initialized = True

    def _generate_object_key(self, entity_type: str, entity_id: str, document_type: str, filename: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        unique_id = uuid4().hex[:8]
        safe_filename = filename.replace(" ", "_").replace("/", "_")
        return f"{entity_type}/{entity_id}/{document_type}/{timestamp}_{unique_id}_{safe_filename}"

    def generate_presigned_upload_url(
        self,
        entity_type: str,
        entity_id: str,
        document_type: str,
        filename: str,
        content_type: Optional[str] = None,
        max_file_size_mb: int = 10,
    ) -> dict:
        object_key = self._generate_object_key(entity_type, entity_id, document_type, filename)

        conditions = []
        if content_type:
            conditions.append(["starts-with", "$Content-Type", content_type])
        conditions.append(["content-length-range", 1, max_file_size_mb * 1024 * 1024])

        try:
            response = self._s3_client.generate_presigned_post(
                Bucket=self._bucket_name,
                Key=object_key,
                Fields={"Content-Type": content_type} if content_type else {},
                Conditions=conditions,
                ExpiresIn=3600,
            )

            return {
                "url": response["url"],
                "fields": response["fields"],
                "object_key": object_key,
                "expires_in": 3600,
            }
        except ClientError as e:
            raise HTTPException(status_code=500, detail=f"Failed to generate upload URL: {str(e)}")

    def generate_presigned_download_url(
        self,
        object_key: str,
        expires_in: int = 3600,
    ) -> str:
        try:
            url = self._s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket_name, "Key": object_key},
                ExpiresIn=expires_in,
            )
            return url
        except ClientError as e:
            raise HTTPException(status_code=500, detail=f"Failed to generate download URL: {str(e)}")

    def get_object_metadata(self, object_key: str) -> dict:
        try:
            response = self._s3_client.head_object(Bucket=self._bucket_name, Key=object_key)
            return {
                "content_type": response.get("ContentType"),
                "content_length": response.get("ContentLength"),
                "last_modified": response.get("LastModified"),
                "metadata": response.get("Metadata", {}),
                "etag": response.get("ETag"),
            }
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                raise HTTPException(status_code=404, detail="Object not found")
            raise HTTPException(status_code=500, detail=f"Failed to get object metadata: {str(e)}")

    def delete_object(self, object_key: str) -> bool:
        try:
            self._s3_client.delete_object(Bucket=self._bucket_name, Key=object_key)
            return True
        except ClientError as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete object: {str(e)}")

    def copy_object(self, source_key: str, destination_key: str) -> str:
        try:
            copy_source = {"Bucket": self._bucket_name, "Key": source_key}
            self._s3_client.copy_object(
                CopySource=copy_source,
                Bucket=self._bucket_name,
                Key=destination_key,
            )
            return destination_key
        except ClientError as e:
            raise HTTPException(status_code=500, detail=f"Failed to copy object: {str(e)}")

    def set_object_lifecycle(self, object_key: str, expires_in_days: int) -> bool:
        try:
            expiry_date = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

            lifecycle_config = self._s3_client.get_bucket_lifecycle_configuration(Bucket=self._bucket_name)

            rule_exists = any(
                rule.get("Filter", {}).get("Prefix", "") == object_key
                for rule in lifecycle_config["Rules"]
            )

            if not rule_exists:
                self._s3_client.put_bucket_lifecycle_configuration(
                    Bucket=self._bucket_name,
                    LifecycleConfiguration={
                        "Rules": lifecycle_config["Rules"]
                        + [
                            {
                                "ID": f"expire-{object_key.replace('/', '-')}",
                                "Filter": {"Prefix": object_key},
                                "Status": "Enabled",
                                "Expiration": {"Date": expiry_date},
                            }
                        ]
                    },
                )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
                self._s3_client.put_bucket_lifecycle_configuration(
                    Bucket=self._bucket_name,
                    LifecycleConfiguration={
                        "Rules": [
                            {
                                "ID": f"expire-{object_key.replace('/', '-')}",
                                "Filter": {"Prefix": object_key},
                                "Status": "Enabled",
                                "Expiration": {"Date": expiry_date},
                            }
                        ]
                    },
                )
                return True
            raise HTTPException(status_code=500, detail=f"Failed to set lifecycle: {str(e)}")

    def verify_object_exists(self, object_key: str) -> bool:
        try:
            self._s3_client.head_object(Bucket=self._bucket_name, Key=object_key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise HTTPException(status_code=500, detail=f"Failed to verify object: {str(e)}")


storage_service = StorageService()