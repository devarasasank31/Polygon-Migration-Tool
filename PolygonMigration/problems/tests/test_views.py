"""End-to-end view tests for the migration workflow (real PostgreSQL).

Polygon API calls are replaced by a controlled fake client
(``helpers.fake_polygon_api``); every fake-backed test is labeled MOCKED
Polygon. Persistence, validation, redirects and response rendering run
against the real application and database.
"""

from unittest.mock import Mock, patch

from django.test import TestCase
from django.urls import reverse

from problems.models import Problem, ProblemTestCase, ProblemTag, SampleTestCase
from problems.polygon_api import PolygonAPI, PolygonAPIError

from .helpers import TEST_CASES, fake_polygon_api


def staff_user():
    from users.models import User

    return User.objects.create_user(
        email="staff@example.com", password="pw", username="staff",
        first_name="Staff", last_name="User", contact_number="", college="",
        is_staff=True,
    )


def regular_user():
    from users.models import User

    return User.objects.create_user(
        email="student@example.com", password="pw", username="student",
        first_name="Student", last_name="User", contact_number="", college="",
    )


def base_post(problem_id="1730A", **extra):
    data = {"problem_id": problem_id}
    data.update(extra)
    return data


class AccessTests(TestCase):
    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("problems:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/users/login/", response.url)

    def test_non_staff_redirected_to_login(self):
        self.client.force_login(regular_user())
        response = self.client.get(reverse("problems:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/users/login/", response.url)

    def test_staff_can_load_index(self):
        self.client.force_login(staff_user())
        response = self.client.get(reverse("problems:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fetch Problem")


class InputValidationTests(TestCase):
    def setUp(self):
        self.client.force_login(staff_user())

    def test_empty_problem_id_reports_error(self):
        response = self.client.post(reverse("problems:index"), base_post(problem_id=""))
        self.assertContains(response, "Please provide a Polygon problem identifier")

    def test_malformed_problem_id_reports_error_without_api_call(self):
        with patch("problems.views.PolygonAPI") as api_cls:
            api_cls.validate_problem_id = PolygonAPI.validate_problem_id
            response = self.client.post(reverse("problems:index"), base_post("id with space"))
        self.assertContains(response, "Invalid Polygon problem identifier")
        api_cls.assert_not_called()


@patch("problems.views.PolygonAPI")
class FetchFlowTests(TestCase):
    """MOCKED Polygon responses; real rendering and database."""

    def setUp(self):
        self.client.force_login(staff_user())

    def test_successful_fetch_shows_problem_and_tests(self, api_cls):
        fake_polygon_api(api_cls)
        response = self.client.post(reverse("problems:index"), base_post())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A + B")
        self.assertContains(response, "3 total")
        self.assertContains(response, "int main(){}")
        self.assertNotContains(response, "Could not fetch the problem")

    def test_fetch_with_no_tests_shows_empty_state(self, api_cls):
        fake_polygon_api(api_cls, test_cases=[])
        response = self.client.post(reverse("problems:index"), base_post())
        self.assertContains(response, "No test cases were returned for this problem")
        self.assertContains(response, "Migrate Test Cases to DB")

    def test_auth_failure_never_shows_success(self, api_cls):
        api_cls.return_value.get_problem_info.side_effect = PolygonAPIError(
            "problem.info", "Incorrect API key", http_status=403, auth_error=True
        )
        response = self.client.post(reverse("problems:index"), base_post())
        self.assertContains(response, "rejected the API credentials")
        self.assertNotContains(response, "Fetched Problem")

    def test_rate_limit_message_shown(self, api_cls):
        api_cls.return_value.get_problem_info.side_effect = PolygonAPIError(
            "problem.info", "call limit exceeded", rate_limited=True
        )
        response = self.client.post(reverse("problems:index"), base_post())
        self.assertContains(response, "rate-limited")

    def test_missing_solution_is_not_an_error(self, api_cls):
        fake_polygon_api(api_cls)
        api_cls.return_value.get_reference_solution.return_value = None
        response = self.client.post(reverse("problems:index"), base_post())
        self.assertContains(response, "No reference solution is available")
        self.assertNotContains(response, "Could not fetch")


@patch("problems.views.PolygonAPI")
class ProblemMigrationTests(TestCase):
    """MOCKED Polygon responses; real PostgreSQL persistence."""

    def setUp(self):
        self.client.force_login(staff_user())

    def migrate(self, difficulty="easy", **extra):
        data = base_post(migrate_to_db="1", difficulty=difficulty,
                         tags=["arrays", "math"], **extra)
        return self.client.post(reverse("problems:index"), data)

    def test_migrate_creates_problem_with_tags_and_samples(self, api_cls):
        fake_polygon_api(api_cls)
        response = self.migrate()
        self.assertContains(response, "created in database")
        problem = Problem.objects.get(polygon_id="1730A")
        self.assertEqual(problem.difficulty, "easy")
        self.assertEqual(
            sorted(problem.extra_tags.values_list("tag_name", flat=True)), ["arrays", "math"]
        )
        self.assertEqual(SampleTestCase.objects.filter(problem=problem).count(), 2)

    def test_new_tag_from_input_is_created(self, api_cls):
        fake_polygon_api(api_cls)
        self.client.post(reverse("problems:index"),
                         base_post(migrate_to_db="1", difficulty="easy",
                                   tags=["arrays"], new_tag="graphs"))
        self.assertTrue(ProblemTag.objects.filter(tag_name="graphs").exists())

    def test_missing_difficulty_is_rejected_without_writing(self, api_cls):
        fake_polygon_api(api_cls)
        response = self.client.post(reverse("problems:index"),
                                    base_post(migrate_to_db="1", tags=["arrays"]))
        self.assertContains(response, "Please select a difficulty level")
        self.assertEqual(Problem.objects.count(), 0)

    def test_duplicate_migration_updates_same_row(self, api_cls):
        fake_polygon_api(api_cls)
        self.migrate()
        response = self.migrate(difficulty="hard")
        self.assertContains(response, "updated in database")
        self.assertEqual(Problem.objects.count(), 1)
        self.assertEqual(Problem.objects.get().difficulty, "hard")

    def test_same_title_different_ids_do_not_collide(self, api_cls):
        fake_polygon_api(api_cls)
        self.migrate()
        response = self.client.post(
            reverse("problems:index"),
            base_post(problem_id="10002", migrate_to_db="1", difficulty="medium",
                      tags=["arrays"]),
        )
        self.assertContains(response, "created in database")
        self.assertEqual(Problem.objects.count(), 2)


@patch("problems.views.PolygonAPI")
class TestCasesMigrationTests(TestCase):
    """MOCKED Polygon responses; real PostgreSQL persistence."""

    def setUp(self):
        self.client.force_login(staff_user())

    def migrate_problem(self, api_cls):
        fake_polygon_api(api_cls)
        self.client.post(reverse("problems:index"),
                         base_post(migrate_to_db="1", difficulty="easy", tags=["arrays"]))

    def test_migrate_test_cases_persists_full_set(self, api_cls):
        self.migrate_problem(api_cls)
        response = self.client.post(reverse("problems:index"),
                                    base_post(migrate_test_cases_to_db="1"))
        self.assertContains(response, "Migrated 3 test cases")
        problem = Problem.objects.get(polygon_id="1730A")
        rows = list(ProblemTestCase.objects.filter(problem=problem).order_by("order"))
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0].is_sample)
        self.assertFalse(rows[2].is_sample)
        # Edge case Q4: full content is stored, not truncated
        self.assertEqual(rows[0].input, "1 2")

    def test_incomplete_cases_are_reported_not_stored(self, api_cls):
        fake_polygon_api(api_cls, test_cases=TEST_CASES + [
            {"index": 4, "is_sample": False, "input": "", "output": "orphan\n"},
        ])
        self.client.post(reverse("problems:index"),
                         base_post(migrate_to_db="1", difficulty="easy", tags=["arrays"]))
        response = self.client.post(reverse("problems:index"),
                                    base_post(migrate_test_cases_to_db="1"))
        self.assertContains(response, "1 skipped")
        self.assertEqual(ProblemTestCase.objects.count(), 3)

    def test_requires_problem_migration_first(self, api_cls):
        fake_polygon_api(api_cls)
        response = self.client.post(reverse("problems:index"),
                                    base_post(migrate_test_cases_to_db="1"))
        self.assertContains(response, "Please migrate the problem to the database first")
        self.assertEqual(ProblemTestCase.objects.count(), 0)

    def test_zero_sample_problem_with_regular_tests(self, api_cls):
        # Edge case Q1: 0 samples, 15 regular test cases
        fake_polygon_api(api_cls, test_cases=[
            {"index": i, "is_sample": False, "input": f"in{i}\n", "output": f"out{i}\n"}
            for i in range(1, 16)
        ])
        self.client.post(reverse("problems:index"),
                         base_post(migrate_to_db="1", difficulty="easy", tags=["arrays"]))
        response = self.client.post(reverse("problems:index"),
                                    base_post(migrate_test_cases_to_db="1"))
        self.assertContains(response, "Migrated 15 test cases")
        problem = Problem.objects.get(polygon_id="1730A")
        self.assertEqual(ProblemTestCase.objects.filter(problem=problem).count(), 15)
        self.assertEqual(SampleTestCase.objects.filter(problem=problem).count(), 0)


@patch("problems.views.get_storage")
@patch("problems.views.PolygonAPI")
class StorageUploadTests(TestCase):
    """MOCKED Polygon responses and storage provider; real view + DB flow."""

    def setUp(self):
        self.client.force_login(staff_user())
        from problems.tests.test_services import InMemoryStorage

        self.storage = InMemoryStorage()

    def migrate_problem(self):
        self.client.post(reverse("problems:index"),
                         base_post(migrate_to_db="1", difficulty="easy", tags=["arrays"]))

    def test_upload_reports_verified_count(self, api_cls, get_storage_cls):
        fake_polygon_api(api_cls)
        get_storage_cls.return_value = self.storage
        self.migrate_problem()
        response = self.client.post(reverse("problems:index"),
                                    base_post(upload_to_storage="1"))
        problem = Problem.objects.get(polygon_id="1730A")
        self.assertContains(response, "Uploaded 3 test cases to memory storage")
        self.assertIn(f"test_cases/{problem.id}/01", self.storage.objects)
        self.assertIn(f"test_cases/{problem.id}/03.a", self.storage.objects)

    def test_requires_problem_migration_first(self, api_cls, get_storage_cls):
        fake_polygon_api(api_cls)
        get_storage_cls.return_value = self.storage
        response = self.client.post(reverse("problems:index"),
                                    base_post(upload_to_storage="1"))
        self.assertContains(response, "Please migrate the problem to the database first")
        self.assertEqual(self.storage.objects, {})

    def test_legacy_azure_flag_still_accepted(self, api_cls, get_storage_cls):
        fake_polygon_api(api_cls)
        get_storage_cls.return_value = self.storage
        self.migrate_problem()
        response = self.client.post(reverse("problems:index"),
                                    base_post(migrate_to_azure="1"))
        self.assertContains(response, "Uploaded 3 test cases")

    def test_provider_failure_reports_error_without_success(self, api_cls, get_storage_cls):
        from problems.storage import StorageError

        fake_polygon_api(api_cls)
        get_storage_cls.return_value = self.storage
        self.migrate_problem()
        self.storage._put = Mock(side_effect=StorageError("boom"))
        response = self.client.post(reverse("problems:index"),
                                    base_post(upload_to_storage="1"))
        self.assertContains(response, "Cloud storage upload failed")
        self.assertNotContains(response, "Uploaded 3 test cases")
