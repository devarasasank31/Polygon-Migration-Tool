"""Storage-layer tests.

- Naming, factory and provider-error tests use mocks / settings overrides
  (clearly mocked).
- ``MinIOIntegrationTests`` talks to the real MinIO container from
  docker-compose on localhost:9000 with the development credentials and is
  skipped automatically when the container is not running.
"""

from unittest.mock import Mock

from botocore.exceptions import ClientError
from django.test import SimpleTestCase, override_settings

from problems.storage import MinioStorage, StorageError, get_storage

from .helpers import minio_available


class NamingTests(SimpleTestCase):
    """Object names must be identical in shape for every provider."""

    def test_input_and_output_keys(self):
        self.assertEqual(MinioStorage.input_key(42, 1), "test_cases/42/01")
        self.assertEqual(MinioStorage.output_key(42, 1), "test_cases/42/01.a")
        self.assertEqual(MinioStorage.input_key(42, 15), "test_cases/42/15")
        self.assertEqual(MinioStorage.output_key(7, 23), "test_cases/7/23.a")

    def test_problem_prefix(self):
        self.assertEqual(MinioStorage.problem_prefix(42), "test_cases/42/")


class FactoryTests(SimpleTestCase):
    def test_unknown_provider_rejected(self):
        with override_settings(STORAGE_PROVIDER="dropbox"):
            with self.assertRaises(StorageError) as ctx:
                get_storage()
            self.assertIn("dropbox", str(ctx.exception))

    @override_settings(STORAGE_PROVIDER="minio", MINIO_ENDPOINT="", MINIO_ACCESS_KEY="",
                       MINIO_SECRET_KEY="", MINIO_BUCKET_NAME="")
    def test_missing_minio_configuration_reported(self):
        with self.assertRaises(StorageError) as ctx:
            get_storage()
        message = str(ctx.exception)
        for name in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
            self.assertIn(name, message)

    @override_settings(STORAGE_PROVIDER="azure", AZURE_STORAGE_ACCOUNT_URL="",
                       AZURE_TENANT_ID="", AZURE_CLIENT_ID="", AZURE_USERNAME="",
                       AZURE_PASSWORD="", AZURE_CONTAINER_NAME="")
    def test_missing_azure_configuration_reported(self):
        with self.assertRaises(StorageError) as ctx:
            get_storage()
        self.assertIn("AZURE_STORAGE_ACCOUNT_URL", str(ctx.exception))

    @override_settings(STORAGE_PROVIDER="minio", MINIO_ENDPOINT="localhost:9000",
                       MINIO_ACCESS_KEY="k", MINIO_SECRET_KEY="s",
                       MINIO_BUCKET_NAME="testcases", MINIO_SECURE=False)
    def test_minio_factory_builds_client(self):
        storage = get_storage()
        self.assertIsInstance(storage, MinioStorage)
        self.assertTrue(storage.client.meta.endpoint_url.startswith("http://localhost:9000"))


def client_error(code="AccessDenied"):
    return ClientError(
        {"Error": {"Code": code, "Message": "denied"}}, "PutObject"
    )


class MinioErrorTests(SimpleTestCase):
    """MOCKED provider failures — verify errors surface as StorageError."""

    def _storage_with_mock_client(self):
        storage = MinioStorage.__new__(MinioStorage)
        storage.bucket = "testcases"
        storage.client = Mock()
        return storage

    def test_put_failure_raises_storage_error(self):
        storage = self._storage_with_mock_client()
        storage.client.put_object.side_effect = client_error()
        with self.assertRaises(StorageError) as ctx:
            storage.upload_test_case(42, 1, "in\n", "out\n")
        self.assertIn("upload", str(ctx.exception))

    def test_head_failure_raises_storage_error(self):
        storage = self._storage_with_mock_client()
        storage.client.head_object.side_effect = client_error()
        with self.assertRaises(StorageError):
            storage._head("test_cases/42/01")

    def test_verification_failure_is_reported(self):
        storage = self._storage_with_mock_client()
        storage.client.put_object.return_value = {}
        storage.client.head_object.return_value = {"ContentLength": 2}  # wrong size
        with self.assertRaises(StorageError) as ctx:
            storage.upload_test_case(42, 1, "in\n", "out\n")
        self.assertIn("verification failed", str(ctx.exception))

    def test_replace_stops_at_first_failed_upload(self):
        storage = self._storage_with_mock_client()
        storage.client.list_objects_v2.return_value = {}  # no existing objects
        # test 1 uploads (input+output) succeed; test 2's first upload fails
        storage.client.put_object.side_effect = [{}, {}, client_error()]
        storage.client.head_object.return_value = {"ContentLength": 2}
        with self.assertRaises(StorageError) as ctx:
            storage.replace_problem_files(42, [
                {"input": "a\n", "output": "b\n"},
                {"input": "c\n", "output": "d\n"},
            ])
        self.assertIn("after 1 successful uploads", str(ctx.exception))


@override_settings(MINIO_ENDPOINT="localhost:9000", MINIO_ACCESS_KEY="minioadmin",
                   MINIO_SECRET_KEY="minioadmin", MINIO_BUCKET_NAME="testcases",
                   MINIO_SECURE=False)
class MinIOIntegrationTests(SimpleTestCase):
    """REAL uploads against the docker-compose MinIO (no mocks)."""

    def setUp(self):
        if not minio_available():
            self.skipTest("MinIO is not reachable on localhost:9000")
        self.storage = get_storage()
        self.problem_id = 999999  # synthetic id, cleaned up afterwards

    def tearDown(self):
        if getattr(self, "storage", None):
            self.storage.delete_problem_files(self.problem_id)

    def test_upload_verify_and_replace_roundtrip(self):
        result = self.storage.upload_test_case(self.problem_id, 1, "1 2\n", "3\n")
        self.assertTrue(result["verified"])

        check = self.storage.verify_test_case(self.problem_id, 1, "1 2\n", "3\n")
        self.assertTrue(check["ok"], check)

        # Re-upload with different content replaces the objects
        stats = self.storage.replace_problem_files(self.problem_id, [
            {"input": "9 9\n", "output": "18\n"},
            {"input": "0 0\n", "output": "0\n"},
        ])
        self.assertEqual(stats["uploaded"], 2)
        files = self.storage.list_problem_files(self.problem_id)
        self.assertEqual(files, [
            f"test_cases/{self.problem_id}/01",
            f"test_cases/{self.problem_id}/01.a",
            f"test_cases/{self.problem_id}/02",
            f"test_cases/{self.problem_id}/02.a",
        ])
        check = self.storage.verify_test_case(self.problem_id, 1, "9 9\n", "18\n")
        self.assertTrue(check["ok"], check)

        removed = self.storage.delete_problem_files(self.problem_id)
        self.assertEqual(removed, 4)
        self.assertEqual(self.storage.list_problem_files(self.problem_id), [])
