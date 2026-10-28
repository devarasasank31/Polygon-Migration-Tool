"""Configurable cloud storage for migrated test-case files.

Two providers sit behind one small interface (``BaseTestCaseStorage``):

- ``minio``  — any S3-compatible endpoint (MinIO locally, AWS S3, Cloudflare R2)
- ``azure``  — Azure Blob Storage with AAD username/password credentials

The logical file layout is identical for every provider:

    test_cases/{problem_id}/{test_number}       # input file
    test_cases/{problem_id}/{test_number}.a     # expected output file

``problem_id`` is the *database* primary key of the problem, and test numbers
are zero-padded to two digits (as in the original Azure implementation).

Configuration is read exclusively from environment variables (see
``.env.example``); the factory :func:`get_storage` raises :class:`StorageError`
when the selected provider is not fully configured, so misconfiguration is
reported instead of surfacing as a late, cryptic SDK failure.
"""

import logging

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a cloud-storage operation fails or is misconfigured."""


class BaseTestCaseStorage:
    """Provider-independent test-case file storage.

    Subclasses implement the primitive operations; the composite operations
    (upload of an input/output pair, verification, cleanup) are shared.
    """

    provider = "base"

    # ------------------------------------------------------------------ naming

    @staticmethod
    def input_key(problem_id, test_number):
        return f"test_cases/{problem_id}/{test_number:02d}"

    @classmethod
    def output_key(cls, problem_id, test_number):
        return f"{cls.input_key(problem_id, test_number)}.a"

    @classmethod
    def problem_prefix(cls, problem_id):
        return f"test_cases/{problem_id}/"

    # ----------------------------------------------------------- primitives

    def upload_file(self, key, data):
        """Store UTF-8 bytes under an exact object key. Raises StorageError."""
        self._put(key, data)

    def _put(self, key, data):
        """Store UTF-8 bytes under the given key. Raises StorageError on failure."""
        raise NotImplementedError

    def _head(self, key):
        """Return the stored size in bytes, or None when the object is missing."""
        raise NotImplementedError

    def _get(self, key):
        """Return the stored bytes, or None when the object is missing."""
        raise NotImplementedError

    def _delete_prefix(self, prefix):
        """Delete every object under the prefix; return the number deleted."""
        raise NotImplementedError

    def _list(self, prefix):
        """Return the object keys under the prefix."""
        raise NotImplementedError

    # --------------------------------------------------------- composite ops

    def upload_test_case(self, problem_id, test_number, input_data, output_data):
        """Upload one test case's input and expected output.

        Returns:
            dict: ``{"input_key": ..., "output_key": ..., "verified": True}``

        Raises:
            StorageError: When either upload or the follow-up verification
                fails. Success is reported only after the provider confirms
                the objects exist with the expected sizes.
        """
        input_key = self.input_key(problem_id, test_number)
        output_key = self.output_key(problem_id, test_number)
        input_bytes = input_data.encode("utf-8")
        output_bytes = output_data.encode("utf-8")

        self._put(input_key, input_bytes)
        self._put(output_key, output_bytes)

        for key, expected in ((input_key, len(input_bytes)), (output_key, len(output_bytes))):
            size = self._head(key)
            if size != expected:
                raise StorageError(
                    f"verification failed for '{key}': expected {expected} bytes, found {size!r}"
                )
        logger.info("[%s] uploaded and verified test %s for problem %s",
                    self.provider, test_number, problem_id)
        return {"input_key": input_key, "output_key": output_key, "verified": True}

    def verify_test_case(self, problem_id, test_number, input_data=None, output_data=None):
        """Check that a test case's files exist and (optionally) match content.

        Returns:
            dict: Per-file ``exists``/``size`` flags plus an overall ``ok``.
        """
        result = {"input": {}, "output": {}, "ok": True}
        for kind, key, expected in (
            ("input", self.input_key(problem_id, test_number),
             input_data.encode("utf-8") if input_data is not None else None),
            ("output", self.output_key(problem_id, test_number),
             output_data.encode("utf-8") if output_data is not None else None),
        ):
            size = self._head(key)
            exists = size is not None
            matches = True
            if exists and expected is not None:
                matches = self._get(key) == expected
            result[kind] = {"key": key, "exists": exists, "size": size, "matches": matches}
            result["ok"] = result["ok"] and exists and matches
        return result

    def replace_problem_files(self, problem_id, test_cases):
        """Delete previous files for the problem, then upload and verify all cases.

        Args:
            problem_id: Database problem id used for object naming.
            test_cases: Iterable of dicts with ``input``/``output`` keys.

        Returns:
            dict: ``{"uploaded": n, "skipped": m, "verified": True}``

        Raises:
            StorageError: On any upload/verification failure; the caller is
            told how far the upload got.
        """
        removed = self._delete_prefix(self.problem_prefix(problem_id))
        logger.info("[%s] removed %d existing objects for problem %s",
                    self.provider, removed, problem_id)

        uploaded, skipped = 0, 0
        for position, case in enumerate(test_cases, start=1):
            input_data = case.get("input") or ""
            output_data = case.get("output") or ""
            if not input_data or not output_data:
                skipped += 1
                continue
            try:
                self.upload_test_case(problem_id, position, input_data, output_data)
            except StorageError as exc:
                raise StorageError(
                    f"upload failed for test {position} after {uploaded} successful uploads: {exc}"
                ) from exc
            uploaded += 1
        return {"uploaded": uploaded, "skipped": skipped, "verified": True}

    def list_problem_files(self, problem_id):
        return sorted(self._list(self.problem_prefix(problem_id)))

    def delete_problem_files(self, problem_id):
        return self._delete_prefix(self.problem_prefix(problem_id))


class MinioStorage(BaseTestCaseStorage):
    """S3-compatible storage (MinIO, AWS S3, Cloudflare R2, ...)."""

    provider = "minio"

    def __init__(self, endpoint, access_key, secret_key, bucket, secure=True):
        self.bucket = bucket
        scheme = "https" if secure else "http"
        self.client = boto3.client(
            "s3",
            endpoint_url=f"{scheme}://{endpoint}",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def _wrap(self, operation, key, exc):
        return StorageError(f"MinIO {operation} failed for '{key}': {exc}")

    def _put(self, key, data):
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap("upload", key, exc) from exc

    def _head(self, key):
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise self._wrap("existence check", key, exc) from exc
        except BotoCoreError as exc:
            raise self._wrap("existence check", key, exc) from exc

    def _get(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise self._wrap("download", key, exc) from exc
        except BotoCoreError as exc:
            raise self._wrap("download", key, exc) from exc

    def _delete_prefix(self, prefix):
        keys = self._list(prefix)
        for start in range(0, len(keys), 1000):
            batch = keys[start:start + 1000]
            try:
                self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
            except (ClientError, BotoCoreError) as exc:
                raise self._wrap("delete", prefix, exc) from exc
        return len(keys)

    def _list(self, prefix):
        keys, continuation = [], None
        try:
            while True:
                kwargs = {"Bucket": self.bucket, "Prefix": prefix}
                if continuation:
                    kwargs["ContinuationToken"] = continuation
                page = self.client.list_objects_v2(**kwargs)
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
                if not page.get("IsTruncated"):
                    return keys
                continuation = page.get("NextContinuationToken")
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap("list", prefix, exc) from exc


class AzureBlobStorage(BaseTestCaseStorage):
    """Azure Blob Storage with AAD username/password credentials."""

    provider = "azure"

    def __init__(self, account_url, tenant_id, client_id, username, password, container):
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import UsernamePasswordCredential
        from azure.storage.blob import BlobServiceClient

        self.container = container
        try:
            credential = UsernamePasswordCredential(
                tenant_id=tenant_id, client_id=client_id,
                username=username, password=password,
            )
            self.blob_service_client = BlobServiceClient(
                account_url=account_url, credential=credential
            )
        except ClientAuthenticationError as exc:
            raise StorageError(f"Azure authentication failed: {exc}") from exc

    def _wrap(self, operation, key, exc):
        return StorageError(f"Azure {operation} failed for '{key}': {exc}")

    def _put(self, key, data):
        from azure.core.exceptions import AzureError

        try:
            client = self.blob_service_client.get_blob_client(container=self.container, blob=key)
            client.upload_blob(data, overwrite=True)
        except AzureError as exc:
            raise self._wrap("upload", key, exc) from exc

    def _head(self, key):
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            blob = self.blob_service_client.get_blob_client(container=self.container, blob=key)
            properties = blob.get_blob_properties()
            return properties.size
        except ResourceNotFoundError:
            return None
        except AzureError as exc:
            raise self._wrap("existence check", key, exc) from exc

    def _get(self, key):
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            downloader = self.blob_service_client.get_blob_client(
                container=self.container, blob=key
            ).download_blob()
            return downloader.readall()
        except ResourceNotFoundError:
            return None
        except AzureError as exc:
            raise self._wrap("download", key, exc) from exc

    def _delete_prefix(self, prefix):
        from azure.core.exceptions import AzureError

        try:
            container = self.blob_service_client.get_container_client(self.container)
            names = [blob.name for blob in container.list_blobs(name_starts_with=prefix)]
            for name in names:
                container.delete_blob(name)
            return len(names)
        except AzureError as exc:
            raise self._wrap("delete", prefix, exc) from exc

    def _list(self, prefix):
        from azure.core.exceptions import AzureError

        try:
            container = self.blob_service_client.get_container_client(self.container)
            return [blob.name for blob in container.list_blobs(name_starts_with=prefix)]
        except AzureError as exc:
            raise self._wrap("list", prefix, exc) from exc


def get_storage():
    """Build the configured storage provider.

    Raises:
        StorageError: For an unknown provider or missing configuration.
    """
    provider = settings.STORAGE_PROVIDER

    if provider == "minio":
        missing = [name for name, value in (
            ("MINIO_ENDPOINT", settings.MINIO_ENDPOINT),
            ("MINIO_ACCESS_KEY", settings.MINIO_ACCESS_KEY),
            ("MINIO_SECRET_KEY", settings.MINIO_SECRET_KEY),
            ("MINIO_BUCKET_NAME", settings.MINIO_BUCKET_NAME),
        ) if not value]
        if missing:
            raise StorageError(
                f"MinIO storage is not configured; missing: {', '.join(missing)}"
            )
        return MinioStorage(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket=settings.MINIO_BUCKET_NAME,
            secure=settings.MINIO_SECURE,
        )

    if provider == "azure":
        missing = [name for name, value in (
            ("AZURE_STORAGE_ACCOUNT_URL", settings.AZURE_STORAGE_ACCOUNT_URL),
            ("AZURE_TENANT_ID", settings.AZURE_TENANT_ID),
            ("AZURE_CLIENT_ID", settings.AZURE_CLIENT_ID),
            ("AZURE_USERNAME", settings.AZURE_USERNAME),
            ("AZURE_PASSWORD", settings.AZURE_PASSWORD),
            ("AZURE_CONTAINER_NAME", settings.AZURE_CONTAINER_NAME),
        ) if not value]
        if missing:
            raise StorageError(
                f"Azure storage is not configured; missing: {', '.join(missing)}"
            )
        return AzureBlobStorage(
            account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
            tenant_id=settings.AZURE_TENANT_ID,
            client_id=settings.AZURE_CLIENT_ID,
            username=settings.AZURE_USERNAME,
            password=settings.AZURE_PASSWORD,
            container=settings.AZURE_CONTAINER_NAME,
        )

    raise StorageError(f"Unknown STORAGE_PROVIDER '{provider}'; expected 'minio' or 'azure'")
