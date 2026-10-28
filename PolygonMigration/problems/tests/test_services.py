"""Service-layer tests for problem/test-case migration (real PostgreSQL).

The Django test runner creates a throwaway database on the configured
PostgreSQL server; these tests never run on SQLite.
"""

from django.test import TestCase

from problems.models import Problem, ProblemTag, ProblemTestCase, SampleTestCase
from problems.services import (
    MigrationError,
    migrate_problem_to_db,
    sync_problem_test_cases,
    sync_sample_test_cases,
    upload_test_cases_to_storage,
)
from problems.storage import BaseTestCaseStorage

LONG_INPUT = "1 2 3 4 5\n" * 100  # well beyond the starter's 260-char truncation


def problem_fields(**overrides):
    data = {
        "title": "A + B",
        "problem_statement": "<p>Sum two numbers</p>",
        "input_format": "<p>Two integers</p>",
        "output_format": "<p>One integer</p>",
        "constraints": "",
        "editorial": "",
        "time_limit": 1000,
        "memory_limit": 256,
        "checker_type": "ncmp",
        "test_case_count": 3,
        "notes": "<p>note</p>",
    }
    data.update(overrides)
    return data


class InMemoryStorage(BaseTestCaseStorage):
    """Test double implementing the storage primitives in memory."""

    provider = "memory"

    def __init__(self):
        self.objects = {}

    def _put(self, key, data):
        self.objects[key] = data

    def _head(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def _get(self, key):
        return self.objects.get(key)

    def _delete_prefix(self, prefix):
        keys = [k for k in self.objects if k.startswith(prefix)]
        for key in keys:
            del self.objects[key]
        return len(keys)

    def _list(self, prefix):
        return [k for k in self.objects if k.startswith(prefix)]


class ValidationTests(TestCase):
    def test_difficulty_required(self):
        with self.assertRaises(MigrationError):
            migrate_problem_to_db(
                polygon_id="1730A", difficulty="", tag_names=["x"],
                problem_data=problem_fields(),
            )

    def test_invalid_difficulty_rejected(self):
        with self.assertRaises(MigrationError):
            migrate_problem_to_db(
                polygon_id="1730A", difficulty="impossible", tag_names=[],
                problem_data=problem_fields(),
            )

    def test_tags_are_trimmed_and_deduplicated(self):
        problem, _ = migrate_problem_to_db(
            polygon_id="1730A", difficulty="easy",
            tag_names=["  arrays ", "arrays", "", "   ", "math"],
            problem_data=problem_fields(),
        )
        self.assertEqual(
            sorted(problem.extra_tags.values_list("tag_name", flat=True)), ["arrays", "math"]
        )

    def test_oversized_tag_rejected(self):
        with self.assertRaises(MigrationError):
            migrate_problem_to_db(
                polygon_id="1730A", difficulty="easy", tag_names=["t" * 101],
                problem_data=problem_fields(),
            )

    def test_missing_title_rejected(self):
        with self.assertRaises(MigrationError):
            migrate_problem_to_db(
                polygon_id="1730A", difficulty="easy", tag_names=[],
                problem_data=problem_fields(title="   "),
            )


class ProblemMigrationTests(TestCase):
    def test_create_persists_fields_and_external_reference(self):
        problem, created = migrate_problem_to_db(
            polygon_id="1730A", difficulty="easy", tag_names=["arrays"],
            problem_data=problem_fields(),
        )
        self.assertTrue(created)
        refreshed = Problem.objects.get(pk=problem.pk)
        self.assertEqual(refreshed.polygon_id, "1730A")
        self.assertEqual(refreshed.title, "A + B")
        self.assertEqual(refreshed.difficulty, "easy")
        self.assertEqual(refreshed.problem_statement, "<p>Sum two numbers</p>")
        self.assertEqual(refreshed.notes, "<p>note</p>")
        self.assertEqual(refreshed.slug, "a-b")

    def test_duplicate_migration_updates_in_place(self):
        first, _ = migrate_problem_to_db(
            polygon_id="1730A", difficulty="easy", tag_names=["arrays"],
            problem_data=problem_fields(),
        )
        second, created = migrate_problem_to_db(
            polygon_id="1730A", difficulty="hard", tag_names=["math"],
            problem_data=problem_fields(title="A + B (updated)"),
        )
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Problem.objects.count(), 1)
        refreshed = Problem.objects.get(pk=first.pk)
        self.assertEqual(refreshed.difficulty, "hard")
        self.assertEqual(refreshed.title, "A + B (updated)")
        self.assertEqual(list(refreshed.extra_tags.values_list("tag_name", flat=True)), ["math"])

    def test_same_title_different_polygon_ids_get_unique_slugs(self):
        # Edge case Q3: two different Polygon problems titled "Two Sum"
        first, _ = migrate_problem_to_db(
            polygon_id="10001", difficulty="easy", tag_names=["arrays"],
            problem_data=problem_fields(title="Two Sum"),
        )
        second, _ = migrate_problem_to_db(
            polygon_id="10002", difficulty="medium", tag_names=["arrays"],
            problem_data=problem_fields(title="Two Sum"),
        )
        self.assertNotEqual(first.slug, second.slug)
        self.assertEqual(Problem.objects.count(), 2)
        self.assertEqual(
            sorted(Problem.objects.values_list("polygon_id", flat=True)), ["10001", "10002"]
        )


class SampleSyncTests(TestCase):
    def setUp(self):
        self.problem, _ = migrate_problem_to_db(
            polygon_id="1730A", difficulty="easy", tag_names=[],
            problem_data=problem_fields(),
        )

    def test_zero_samples_creates_no_rows(self):
        # Edge case Q1 ingredient: a problem with no sample tests
        cases = [
            {"index": i, "is_sample": False, "input": f"{i}\n", "output": f"{i}\n"}
            for i in range(1, 16)
        ]
        count = sync_sample_test_cases(self.problem, cases)
        self.assertEqual(count, 0)
        self.assertEqual(SampleTestCase.objects.count(), 0)

    def test_samples_synced_and_replaced(self):
        sync_sample_test_cases(self.problem, [
            {"index": 1, "is_sample": True, "input": "1\n", "output": "1\n"},
            {"index": 2, "is_sample": False, "input": "2\n", "output": "2\n"},
        ])
        self.assertEqual(SampleTestCase.objects.count(), 1)
        sync_sample_test_cases(self.problem, [])
        self.assertEqual(SampleTestCase.objects.count(), 0)


class TestCasesSyncTests(TestCase):
    def setUp(self):
        self.problem, _ = migrate_problem_to_db(
            polygon_id="1730A", difficulty="easy", tag_names=[],
            problem_data=problem_fields(),
        )

    def test_full_content_is_stored_without_truncation(self):
        # Edge case Q4: the starter truncated input/output to 260 characters
        stats = sync_problem_test_cases(self.problem, [
            {"index": 1, "is_sample": True, "input": LONG_INPUT, "output": "ok\n",
             "description": "d" * 400},
        ])
        self.assertEqual(stats["saved"], 1)
        row = ProblemTestCase.objects.get(problem=self.problem)
        self.assertEqual(row.input, LONG_INPUT.rstrip())
        self.assertGreater(len(row.input), 260)
        self.assertGreater(len(row.description), 260)
        self.assertTrue(row.is_sample)

    def test_cases_with_empty_input_or_output_are_skipped(self):
        stats = sync_problem_test_cases(self.problem, [
            {"index": 1, "is_sample": False, "input": "1\n", "output": "1\n"},
            {"index": 2, "is_sample": False, "input": "", "output": "2\n"},
            {"index": 3, "is_sample": False, "input": "3\n", "output": ""},
        ])
        self.assertEqual(stats, {"total": 3, "saved": 1, "skipped": 2})
        self.assertEqual(ProblemTestCase.objects.count(), 1)

    def test_re_migration_with_fewer_cases_removes_stale_rows(self):
        # Edge case Q2: 20 cases migrated, then Polygon reduces them to 12
        original = [
            {"index": i, "is_sample": False, "input": f"in{i}\n", "output": f"out{i}\n"}
            for i in range(1, 21)
        ]
        sync_problem_test_cases(self.problem, original)
        self.assertEqual(ProblemTestCase.objects.count(), 20)

        reduced = [
            {"index": i, "is_sample": False, "input": f"new{i}\n", "output": f"newout{i}\n"}
            for i in range(1, 13)
        ]
        stats = sync_problem_test_cases(self.problem, reduced)
        self.assertEqual(stats["saved"], 12)
        self.assertEqual(ProblemTestCase.objects.count(), 12)
        self.assertEqual(
            list(ProblemTestCase.objects.filter(order=1).values_list("input", flat=True)),
            ["new1"],
        )

    def test_repeated_migration_is_idempotent(self):
        cases = [
            {"index": 1, "is_sample": True, "input": "1\n", "output": "1\n"},
            {"index": 2, "is_sample": False, "input": "2\n", "output": "2\n"},
        ]
        sync_problem_test_cases(self.problem, cases)
        sync_problem_test_cases(self.problem, cases)
        self.assertEqual(ProblemTestCase.objects.count(), 2)


class StorageUploadServiceTests(TestCase):
    def setUp(self):
        self.problem, _ = migrate_problem_to_db(
            polygon_id="1730A", difficulty="easy", tag_names=[],
            problem_data=problem_fields(),
        )
        self.storage = InMemoryStorage()

    def test_uploads_use_db_problem_id_and_expected_naming(self):
        cases = [
            {"index": 1, "is_sample": True, "input": "1 2\n", "output": "3\n"},
            {"index": 2, "is_sample": False, "input": "4 5\n", "output": "9\n"},
        ]
        result = upload_test_cases_to_storage(self.problem, cases, storage=self.storage)
        self.assertEqual(result["uploaded"], 2)
        self.assertTrue(result["verified"])
        self.assertIn(f"test_cases/{self.problem.id}/01", self.storage.objects)
        self.assertIn(f"test_cases/{self.problem.id}/01.a", self.storage.objects)
        self.assertIn(f"test_cases/{self.problem.id}/02", self.storage.objects)
        self.assertEqual(self.storage.objects[f"test_cases/{self.problem.id}/02.a"], b"9\n")

    def test_repeated_upload_replaces_files(self):
        cases = [{"index": 1, "is_sample": False, "input": "1\n", "output": "1\n"}]
        upload_test_cases_to_storage(self.problem, cases, storage=self.storage)
        cases[0]["input"] = "changed\n"
        upload_test_cases_to_storage(self.problem, cases, storage=self.storage)
        self.assertEqual(len(self.storage.objects), 2)
        self.assertEqual(self.storage.objects[f"test_cases/{self.problem.id}/01"], b"changed\n")

    def test_incomplete_cases_are_skipped_and_reported(self):
        cases = [
            {"index": 1, "is_sample": False, "input": "1\n", "output": "1\n"},
            {"index": 2, "is_sample": False, "input": "", "output": ""},
        ]
        result = upload_test_cases_to_storage(self.problem, cases, storage=self.storage)
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["skipped"], 1)
