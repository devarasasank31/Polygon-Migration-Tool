"""Unit tests for the Polygon API client (MOCKED HTTP — no network is used).

These tests control every HTTP response and verify the client's signing,
validation, error classification, retry and caching behavior. Live calls
against Polygon require real credentials and are not part of this suite.
"""

import json
from unittest import TestCase as UnitTestCase
from unittest.mock import Mock, patch

from problems.polygon_api import PolygonAPI, PolygonAPIError


def make_response(status_code=200, payload=None, text=None, content=None):
    """Build a fake requests.Response: JSON (payload), plain text, or binary."""
    response = Mock()
    response.status_code = status_code
    if payload is not None:
        body = json.dumps(payload)
        response.json.return_value = payload
        response.text = text if text is not None else body
        response.content = content if content is not None else response.text.encode("utf-8")
    elif text is not None:
        response.text = text
        response.content = content if content is not None else text.encode("utf-8")
        response.json.side_effect = ValueError("not json")
    else:
        response.text = ""
        response.content = content if content is not None else b""
        response.json.side_effect = ValueError("not json")
    return response


def make_client(mock_post=False):
    """Build a client with a mocked HTTP session.

    ``mock_post=False`` (default) keeps the real request pipeline so error
    classification/retry logic is exercised against the mocked session.
    ``mock_post=True`` additionally replaces ``_post`` for tests that route
    per-method responses directly.
    """
    api = PolygonAPI(api_key="test-key", api_secret="test-secret")
    api._session = Mock()
    if mock_post:
        api._post = Mock()
    return api


@patch("problems.polygon_api.time.sleep", Mock())
class SignatureTests(UnitTestCase):
    """Polygon apiSig scheme: rand + sha512("{rand}/{method}?{params}#{secret}")."""

    def test_signature_matches_documented_algorithm(self):
        import hashlib
        from urllib.parse import urlencode

        api = make_client()
        with patch("problems.polygon_api.time.time", Mock(return_value=1700000000)), \
                patch("problems.polygon_api.random.choices", Mock(return_value=list("abc123"))):
            signed = api._generate_api_sig("problem.info", {"problemId": "1730A"})

        expected_params = {
            "problemId": "1730A", "apiKey": "test-key", "time": 1700000000,
        }
        param_str = urlencode(sorted(expected_params.items()))
        digest = hashlib.sha512(f"abc123/problem.info?{param_str}#test-secret".encode()).hexdigest()
        self.assertEqual(signed["apiSig"], "abc123" + digest)
        self.assertEqual(signed["apiKey"], "test-key")
        self.assertEqual(signed["time"], 1700000000)

    def test_missing_credentials_raise_auth_error(self):
        api = PolygonAPI(api_key="", api_secret="")
        with self.assertRaises(PolygonAPIError) as ctx:
            api.get_problem_info("1730A")
        self.assertTrue(ctx.exception.auth_error)
        self.assertIn("POLYGON_API_KEY", ctx.exception.message)


class ProblemIdValidationTests(UnitTestCase):
    def test_accepts_polygon_style_ids(self):
        for value in ("1730A", "gift", "2sum_easy", "A-1_b"):
            self.assertEqual(PolygonAPI.validate_problem_id(value), value)

    def test_rejects_empty_and_malformed_ids(self):
        for value in (None, "", "  ", "../etc/passwd", "id with space", "id?x=1", "a" * 101):
            with self.assertRaises(ValueError, msg=repr(value)):
                PolygonAPI.validate_problem_id(value)

    def test_strips_whitespace(self):
        self.assertEqual(PolygonAPI.validate_problem_id("  1730A\n"), "1730A")


@patch("problems.polygon_api.time.sleep", Mock())
class ErrorHandlingTests(UnitTestCase):
    def test_failed_status_is_raised_with_comment(self):
        api = make_client()
        api._session.post.return_value = make_response(
            payload={"status": "FAILED", "comment": "Problem not found"}
        )
        with self.assertRaises(PolygonAPIError) as ctx:
            api.get_problem_info("missing")
        self.assertIn("Problem not found", ctx.exception.message)
        self.assertFalse(ctx.exception.auth_error)

    def test_incorrect_api_key_is_classified_as_auth_error(self):
        api = make_client()
        api._session.post.return_value = make_response(
            payload={"status": "FAILED", "comment": "Incorrect API key"}
        )
        with self.assertRaises(PolygonAPIError) as ctx:
            api.get_problem_info("1730A")
        self.assertTrue(ctx.exception.auth_error)

    def test_http_403_is_classified_as_auth_error(self):
        api = make_client()
        api._session.post.return_value = make_response(status_code=403, text="forbidden")
        with self.assertRaises(PolygonAPIError) as ctx:
            api.get_problem_info("1730A")
        self.assertTrue(ctx.exception.auth_error)
        self.assertEqual(ctx.exception.http_status, 403)

    def test_rate_limit_retries_then_succeeds(self):
        api = make_client()
        limited = make_response(payload={"status": "FAILED", "comment": "call limit exceeded per 1 second"})
        ok = make_response(payload={"status": "OK", "result": {"name": "a_plus_b"}})
        api._session.post.side_effect = [limited, ok]
        info = api.get_problem_info("1730A")
        self.assertEqual(info["name"], "a_plus_b")
        self.assertEqual(api._session.post.call_count, 2)

    def test_network_error_is_wrapped(self):
        import requests

        api = make_client()
        api._session.post.side_effect = requests.exceptions.ConnectionError("boom")
        with self.assertRaises(PolygonAPIError) as ctx:
            api.get_problem_info("1730A")
        self.assertIn("network error", ctx.exception.message)

    def test_non_json_response_raises(self):
        api = make_client()
        api._session.post.return_value = make_response(text="<html>oops</html>")
        with self.assertRaises(PolygonAPIError) as ctx:
            api.get_problem_info("1730A")
        self.assertIn("not valid JSON", ctx.exception.message)


@patch("problems.polygon_api.time.sleep", Mock())
class TestFetchingTests(UnitTestCase):
    def _api_with_tests(self, tests, inputs, answers):
        api = make_client(mock_post=True)

        def route(method, params=None, timeout=None):
            if method == "problem.tests":
                return make_response(payload={"status": "OK", "result": tests})
            if method == "problem.testInput":
                return make_response(text=inputs[params["testIndex"]])
            if method == "problem.testAnswer":
                return make_response(text=answers[params["testIndex"]])
            raise AssertionError(f"unexpected method {method}")

        api._post.side_effect = route
        return api

    def test_get_all_test_cases_builds_full_cases(self):
        api = self._api_with_tests(
            [
                {"index": 1, "isSample": True, "description": "sample"},
                {"index": 2, "isSample": False},
                {"index": 3},
            ],
            {1: "1 2\n", 2: "5 5\n", 3: "0 0\n"},
            {1: "3\n", 2: "10\n", 3: "0\n"},
        )
        cases = api.get_all_test_cases("1730A")
        self.assertEqual(len(cases), 3)
        self.assertTrue(cases[0]["is_sample"])
        self.assertFalse(cases[1]["is_sample"])
        self.assertEqual(cases[2]["output"], "0\n")
        self.assertEqual(cases[1]["description"], "")

    def test_use_in_statements_fallback_for_sample_flag(self):
        api = self._api_with_tests([{"index": 1, "useInStatements": True}], {1: "a\n"}, {1: "b\n"})
        cases = api.get_all_test_cases("1730A")
        self.assertTrue(cases[0]["is_sample"])

    def test_single_test_failure_aborts_the_fetch(self):
        api = make_client(mock_post=True)

        def route(method, params=None, timeout=None):
            if method == "problem.tests":
                return make_response(payload={"status": "OK", "result": [
                    {"index": 1}, {"index": 2},
                ]})
            if method == "problem.testInput":
                return make_response(text="x\n")
            if method == "problem.testAnswer":
                if params["testIndex"] == 2:
                    # _post is mocked here, so raise the classified failure
                    # the real pipeline would produce for this response.
                    raise PolygonAPIError(
                        "problem.testAnswer", "Test not found", http_status=400
                    )
                return make_response(text="y\n")
            raise AssertionError(method)

        api._post.side_effect = route
        with self.assertRaises(PolygonAPIError) as ctx:
            api.get_all_test_cases("1730A")
        self.assertIn("test 2", ctx.exception.message)

    def test_zero_tests_returns_empty_list(self):
        api = self._api_with_tests([], {}, {})
        self.assertEqual(api.get_all_test_cases("1730A"), [])


@patch("problems.polygon_api.time.sleep", Mock())
class ReferenceSolutionTests(UnitTestCase):
    def test_prefers_ma_tagged_solution(self):
        api = make_client(mock_post=True)

        def route(method, params=None, timeout=None):
            if method == "problem.solutions":
                return make_response(payload={"status": "OK", "result": [
                    {"name": "alt.cpp", "tag": "AL"},
                    {"name": "main.cpp", "tag": "MA"},
                ]})
            if method == "problem.viewSolution":
                return make_response(text=f"// {params['name']}")
            raise AssertionError(method)

        api._post.side_effect = route
        solution = api.get_reference_solution("1730A")
        self.assertEqual(solution["name"], "main.cpp")
        self.assertIn("main.cpp", solution["source"])

    def test_returns_none_without_solutions(self):
        api = make_client(mock_post=True)
        api._post.side_effect = lambda method, params=None, timeout=None: make_response(
            payload={"status": "OK", "result": []}
        )
        self.assertIsNone(api.get_reference_solution("1730A"))


class PackageTests(UnitTestCase):
    def test_non_zip_package_is_rejected(self):
        api = make_client(mock_post=True)
        api._post.side_effect = [
            make_response(payload={"status": "OK", "result": [
                {"id": 1, "type": "standard", "revision": 1},
            ]}),
            make_response(content=b"not a zip"),
        ]
        with self.assertRaises(PolygonAPIError) as ctx:
            api.download_and_extract_package("1730A")
        self.assertIn("ZIP", str(ctx.exception))

    def test_extracts_problem_html_from_package(self):
        import zipfile
        from io import BytesIO

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("problem.html", "<html><body>hello</body></html>")

        api = make_client(mock_post=True)
        api._post.side_effect = [
            make_response(payload={"status": "OK", "result": [
                {"id": 7, "type": "standard", "revision": 3},
            ]}),
            make_response(content=buffer.getvalue()),
        ]
        content = api.download_and_extract_package("1730A")
        self.assertIn("hello", content)

    def test_missing_package_type_raises(self):
        api = make_client(mock_post=True)
        api._post.side_effect = lambda method, params=None, timeout=None: make_response(
            payload={"status": "OK", "result": [{"id": 1, "type": "source", "revision": 1}]}
        )
        with self.assertRaises(PolygonAPIError):
            api.download_and_extract_package("1730A")


class RedisCacheIntegrationTests(UnitTestCase):
    """REAL Redis roundtrip against the docker-compose cache (no mocks)."""

    def setUp(self):
        from .helpers import redis_available

        if not redis_available():
            self.skipTest("Redis is not reachable")
        self.api = make_client()
        self.polygon_id = "cachetest_1730A"
        self.addCleanup(self.api.clear_test_cases_from_redis, self.polygon_id)

    def test_store_andRetrieve_roundtrip(self):
        cases = [
            {"index": 1, "is_sample": True, "description": "d", "input": "1 2\n", "output": "3\n"},
            {"index": 2, "is_sample": False, "description": "", "input": "9\n", "output": "9\n"},
        ]
        self.api.store_test_cases_in_redis(self.polygon_id, cases)
        loaded = self.api.get_test_cases_from_redis(self.polygon_id)
        self.assertEqual(loaded, cases)

    def test_missing_cache_returns_none(self):
        self.assertIsNone(self.api.get_test_cases_from_redis("never_cached_problem"))
