"""Client for the official Polygon REST API (https://polygon.codeforces.com/api).

Authentication follows Polygon's documented scheme: every request carries
``apiKey``, ``time`` (unix seconds) and ``apiSig`` where

    apiSig = rand + sha512("{rand}/{method}?{urlencoded_sorted_params}#{api_secret}")

with ``rand`` being a 6-character lowercase-alphanumeric string. Credentials are
read from ``POLYGON_API_KEY`` / ``POLYGON_API_SECRET`` and are never logged.

The client raises :class:`PolygonAPIError` for every failure (HTTP errors,
``status: FAILED`` responses, missing credentials) so callers can never mistake
a failed request for an empty result.
"""

import hashlib
import json
import logging
import os
import random
import re
import shutil
import string
import subprocess
import stat
import sys
import tempfile
import time
import zipfile
from urllib.parse import urlencode

import redis
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class PolygonAPIError(Exception):
    """Raised when a Polygon API request fails.

    Attributes:
        method: The Polygon API method that failed (e.g. ``problem.info``).
        message: Sanitized error comment from the API or transport layer.
        http_status: HTTP status code, if a response was received.
        auth_error: True when the failure is caused by missing/invalid credentials.
        rate_limited: True when Polygon reported a call-rate limit.
    """

    def __init__(self, method, message, http_status=None, auth_error=False, rate_limited=False):
        self.method = method
        self.message = message
        self.http_status = http_status
        self.auth_error = auth_error
        self.rate_limited = rate_limited
        super().__init__(f"Polygon API method '{method}' failed: {message}")


class PolygonAPI:
    """Thin, dependency-free client for the Polygon API."""

    API_URL = "https://polygon.codeforces.com/api/"
    REQUEST_TIMEOUT = 30       # seconds for regular API calls
    PACKAGE_TIMEOUT = 300      # seconds for problem package downloads
    MIN_REQUEST_INTERVAL = 0.35  # stay below Polygon's per-key call rate
    MAX_RATE_RETRIES = 3
    _PROBLEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
    _AUTH_HINTS = ("api key", "apikey", "signature", "api_sig", "not authorized", "access denied")
    _RATE_HINTS = ("call limit", "too many", "rate", "limit exceeded")

    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key if api_key is not None else settings.POLYGON_API_KEY
        self.api_secret = api_secret if api_secret is not None else settings.POLYGON_API_SECRET
        self._session = requests.Session()
        self._last_request_at = 0.0

    # ------------------------------------------------------------------ utils

    def _ensure_credentials(self):
        if not self.api_key or not self.api_secret:
            raise PolygonAPIError(
                "credentials",
                "POLYGON_API_KEY and POLYGON_API_SECRET must be configured in the environment",
                auth_error=True,
            )

    @classmethod
    def validate_problem_id(cls, problem_id):
        """Validate and normalize a Polygon problem identifier.

        Raises:
            ValueError: If the identifier is empty or contains characters that
                are never valid in a Polygon problem id.
        """
        normalized = (problem_id or "").strip()
        if not normalized:
            raise ValueError("Please provide a Polygon problem identifier.")
        if not cls._PROBLEM_ID_RE.match(normalized):
            raise ValueError(
                "Invalid Polygon problem identifier. Use letters, digits, '-' or '_' only."
            )
        return normalized

    def _generate_api_sig(self, method_name, params):
        """Build the signed (apiKey, time, apiSig) parameter set for a request."""
        self._ensure_credentials()
        signed_params = dict(params)
        signed_params["apiKey"] = self.api_key
        signed_params["time"] = int(time.time())

        param_str = urlencode(sorted(signed_params.items()))
        rand_prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        to_hash = f"{rand_prefix}/{method_name}?{param_str}#{self.api_secret}"
        api_sig = rand_prefix + hashlib.sha512(to_hash.encode("utf-8")).hexdigest()

        signed_params["apiSig"] = api_sig
        return signed_params

    def _throttle(self):
        """Keep a minimum interval between requests to respect Polygon's rate limits."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.MIN_REQUEST_INTERVAL:
            time.sleep(self.MIN_REQUEST_INTERVAL - elapsed)

    @staticmethod
    def _classify_failure(method, comment, http_status=None):
        lowered = (comment or "").lower()
        auth_error = http_status in (401, 403) or any(h in lowered for h in PolygonAPI._AUTH_HINTS)
        rate_limited = http_status == 429 or any(h in lowered for h in PolygonAPI._RATE_HINTS)
        return PolygonAPIError(
            method, comment or "unknown error", http_status=http_status,
            auth_error=auth_error, rate_limited=rate_limited,
        )

    # --------------------------------------------------------------- transport

    def _post(self, method_name, params=None, timeout=None):
        """Send one signed request; returns the raw :class:`requests.Response`.

        Retries with backoff when Polygon reports a rate limit and once on
        transient 5xx responses. Every other failure raises :class:`PolygonAPIError`.
        """
        self._ensure_credentials()
        timeout = timeout or self.REQUEST_TIMEOUT
        last_error = None

        for attempt in range(1 + self.MAX_RATE_RETRIES):
            self._throttle()
            post_params = self._generate_api_sig(method_name, params or {})
            self._last_request_at = time.monotonic()
            try:
                response = self._session.post(
                    f"{self.API_URL}{method_name}", data=post_params, timeout=timeout
                )
            except requests.exceptions.Timeout as exc:
                raise PolygonAPIError(method_name, f"request timed out after {timeout}s") from exc
            except requests.exceptions.RequestException as exc:
                raise PolygonAPIError(method_name, f"network error: {exc}") from exc

            if response.status_code >= 500:
                last_error = self._classify_failure(
                    method_name, f"server error (HTTP {response.status_code})",
                    http_status=response.status_code,
                )
                if attempt < self.MAX_RATE_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error

            if response.status_code >= 400:
                # Polygon usually reports failures as JSON with HTTP 200, but
                # auth problems can arrive as plain 4xx responses.
                comment = response.text[:500] or f"HTTP {response.status_code}"
                try:
                    payload = response.json()
                    if payload.get("status") == "FAILED":
                        comment = payload.get("comment") or comment
                except ValueError:
                    pass
                error = self._classify_failure(method_name, comment, http_status=response.status_code)
                if error.rate_limited and attempt < self.MAX_RATE_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise error

            try:
                payload = response.json()
            except ValueError as exc:
                raise PolygonAPIError(
                    method_name, "response was not valid JSON", http_status=response.status_code
                ) from exc

            if payload.get("status") == "FAILED":
                error = self._classify_failure(
                    method_name, payload.get("comment") or "unknown error",
                    http_status=response.status_code,
                )
                if error.rate_limited and attempt < self.MAX_RATE_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise error

            return response

        raise last_error or PolygonAPIError(method_name, "request failed")

    def _make_request(self, method_name, params=None):
        """Call a Polygon method and return its parsed ``result`` field."""
        response = self._post(method_name, params)
        return response.json().get("result")

    def _make_plain_request(self, method_name, params=None):
        """Call a Polygon method and return the raw text body."""
        return self._post(method_name, params).text

    # ----------------------------------------------------------- problem data

    def get_problem_info(self, problem_id):
        """Return metadata for a problem (``problem.info``)."""
        problem_id = self.validate_problem_id(problem_id)
        info = self._make_request("problem.info", {"problemId": problem_id})
        if not isinstance(info, dict) or not info.get("name"):
            raise PolygonAPIError("problem.info", "returned unexpected or empty problem data")
        logger.info("Fetched problem info for %s (name=%s)", problem_id, info.get("name"))
        return info

    def get_statements(self, problem_id):
        """Return available statement languages for a problem (``problem.statements``)."""
        problem_id = self.validate_problem_id(problem_id)
        statements = self._make_request("problem.statements", {"problemId": problem_id})
        if statements is None:
            raise PolygonAPIError("problem.statements", "no statements returned")
        return statements

    def get_tests(self, problem_id, testset="tests"):
        """Return the raw test list of a testset (``problem.tests``)."""
        problem_id = self.validate_problem_id(problem_id)
        tests = self._make_request("problem.tests", {"problemId": problem_id, "testset": testset})
        if not isinstance(tests, list):
            raise PolygonAPIError("problem.tests", "expected a list of tests")
        return tests

    def get_all_test_cases(self, problem_id, testset="tests"):
        """Fetch every test of a testset with its input and expected output.

        For each test returned by ``problem.tests``, the input and answer are
        fetched with ``problem.testInput`` / ``problem.testAnswer``.

        Returns:
            list[dict]: One dict per test with keys ``index`` (int),
            ``is_sample`` (bool), ``description`` (str), ``input`` (str) and
            ``output`` (str), in testset order.

        Raises:
            PolygonAPIError: If the test list or any single test's input/answer
                cannot be fetched — an incomplete fetch is never silently
                returned as a successful one.
        """
        problem_id = self.validate_problem_id(problem_id)
        tests = self.get_tests(problem_id, testset)
        logger.info("Fetching %d tests for problem %s", len(tests), problem_id)

        cases = []
        for position, test in enumerate(tests, start=1):
            index = test.get("index", position)
            is_sample = bool(test.get("isSample", test.get("useInStatements", False)))
            try:
                input_data = self._make_plain_request("problem.testInput", {
                    "problemId": problem_id, "testset": testset, "testIndex": index,
                })
                output_data = self._make_plain_request("problem.testAnswer", {
                    "problemId": problem_id, "testset": testset, "testIndex": index,
                })
            except PolygonAPIError as exc:
                raise PolygonAPIError(
                    "problem.testInput/testAnswer",
                    f"could not fetch data for test {index}: {exc.message}",
                    http_status=exc.http_status,
                    auth_error=exc.auth_error,
                    rate_limited=exc.rate_limited,
                ) from exc

            cases.append({
                "index": index,
                "is_sample": is_sample,
                "description": test.get("description") or "",
                "input": input_data or "",
                "output": output_data or "",
            })
            logger.debug("Fetched test %s (%d/%d)", index, position, len(tests))

        logger.info("Fetched %d complete test cases for problem %s", len(cases), problem_id)
        return cases

    def get_reference_solution(self, problem_id):
        """Return the main reference solution of a problem, if any.

        Uses ``problem.solutions`` and prefers the solution tagged ``MA``
        (main), falling back to the first available solution, then fetches its
        source with ``problem.viewSolution``.

        Returns:
            dict | None: ``{"name": ..., "source": ...}`` or None when the
            problem has no solutions or the user cannot access them.
        """
        problem_id = self.validate_problem_id(problem_id)
        try:
            solutions = self._make_request("problem.solutions", {"problemId": problem_id})
        except PolygonAPIError as exc:
            logger.info("No solutions available for %s: %s", problem_id, exc.message)
            return None
        if not solutions:
            return None

        chosen = next((s for s in solutions if s.get("tag") == "MA"), solutions[0])
        name = chosen.get("name")
        if not name:
            return None
        try:
            source = self._make_plain_request(
                "problem.viewSolution", {"problemId": problem_id, "name": name}
            )
        except PolygonAPIError as exc:
            logger.info("Solution %s of %s not readable: %s", name, problem_id, exc.message)
            return None
        logger.info("Fetched reference solution '%s' for problem %s", name, problem_id)
        return {"name": name, "source": source}

    def get_custom_checker_info(self, problem_id):
        """Return custom-checker information (``problem.checker``), if present.

        Returns:
            dict | None: ``{"name": ..., "type": "custom"}`` when the problem
            uses a custom checker, otherwise None for standard checkers.
        """
        problem_id = self.validate_problem_id(problem_id)
        try:
            checker = self._make_request("problem.checker", {"problemId": problem_id})
        except PolygonAPIError as exc:
            logger.info("No checker reported for %s: %s", problem_id, exc.message)
            return None
        if not checker or checker.startswith("std::"):
            return None
        return {"name": checker, "type": "custom"}

    def fetch_file_content(self, problem_id, file_name, file_type="source"):
        """Return the text content of a problem file (``problem.viewFile``).

        Raises:
            PolygonAPIError: If the file cannot be fetched.
        """
        problem_id = self.validate_problem_id(problem_id)
        return self._make_plain_request(
            "problem.viewFile", {"problemId": problem_id, "type": file_type, "name": file_name}
        )

    def download_and_extract_package(self, problem_id, package_type="standard"):
        """Download the problem package and return the contents of problem.html.

        Uses ``problem.packages`` to locate the newest package of the requested
        type and ``problem.package`` to download it.

        Raises:
            PolygonAPIError: If no package exists or the download is not a ZIP.
        """
        problem_id = self.validate_problem_id(problem_id)
        packages = self._make_request("problem.packages", {"problemId": problem_id})
        if not packages:
            raise PolygonAPIError("problem.packages", "no packages available for this problem")

        candidates = [p for p in packages if p.get("type") == package_type]
        if not candidates:
            raise PolygonAPIError("problem.packages", f"no '{package_type}' package available")
        latest = max(candidates, key=lambda p: p.get("revision", 0))

        logger.info("Downloading %s package %s for problem %s",
                    package_type, latest.get("id"), problem_id)
        response = self._post(
            "problem.package",
            {"problemId": problem_id, "packageId": latest["id"], "type": package_type},
            timeout=self.PACKAGE_TIMEOUT,
        )
        if not response.content.startswith(b"PK"):
            raise PolygonAPIError("problem.package", "downloaded data is not a valid ZIP archive")

        temp_dir = tempfile.mkdtemp(prefix="polygon_package_")
        try:
            zip_path = os.path.join(temp_dir, "problem.zip")
            with open(zip_path, "wb") as handle:
                handle.write(response.content)
            extract_dir = os.path.join(temp_dir, "extracted")
            with zipfile.ZipFile(zip_path, "r") as archive:
                archive.extractall(extract_dir)

            problem_html = None
            for root, _dirs, files in os.walk(extract_dir):
                if "problem.html" in files:
                    problem_html = os.path.join(root, "problem.html")
                    break
            if not problem_html:
                raise PolygonAPIError("problem.package", "problem.html not found in package")

            with open(problem_html, "r", encoding="utf-8") as handle:
                content = handle.read()
            logger.info("Extracted problem.html (%d characters) for %s", len(content), problem_id)
            return content
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def compile_custom_checker(self, source_code, work_dir=None):
        """Compile custom-checker C++ source with g++.

        Returns:
            str | None: Path to the compiled binary, or None when g++ is
            unavailable or compilation fails.
        """
        gpp = shutil.which("g++")
        if not gpp:
            logger.error("g++ compiler not found; cannot compile custom checker")
            return None

        work_dir = work_dir or settings.CUSTOM_CHECKER_DIR or tempfile.mkdtemp(prefix="checker_")
        os.makedirs(work_dir, exist_ok=True)
        source_file = os.path.join(work_dir, "custom_checker.cpp")
        with open(source_file, "w", encoding="utf-8") as handle:
            handle.write(source_code)

        binary_name = "custom_checker.exe" if sys.platform.startswith("win") else "custom_checker"
        binary_file = os.path.join(work_dir, binary_name)
        try:
            result = subprocess.run(
                [gpp, "-std=gnu++17", "-O2", "-o", binary_file, source_file],
                capture_output=True, text=True, cwd=work_dir, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("Custom checker compilation failed: %s", exc)
            return None

        if result.returncode != 0 or not os.path.exists(binary_file):
            logger.error("Custom checker compilation failed: %s", result.stderr[:1000])
            return None
        os.chmod(binary_file, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
        logger.info("Custom checker compiled to %s", binary_file)
        return binary_file

    # -------------------------------------------------------------- redis cache

    @staticmethod
    def _redis_client():
        return redis.StrictRedis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD,
            ssl=settings.REDIS_SSL,
            ssl_cert_reqs=None,
        )

    @staticmethod
    def _cache_prefix(polygon_id):
        return f"polygon_migration_test_cases_{polygon_id}"

    def store_test_cases_in_redis(self, polygon_id, test_cases, expiry_hours=0.5):
        """Cache fetched test cases in Redis (best effort; failures are logged)."""
        prefix = self._cache_prefix(polygon_id)
        try:
            client = self._redis_client()
            ttl = int(expiry_hours * 3600)
            client.setex(f"{prefix}_count", ttl, len(test_cases))
            for position, case in enumerate(test_cases, start=1):
                client.setex(f"{prefix}_test_{position}", ttl, json.dumps({
                    "index": case.get("index", position),
                    "is_sample": case.get("is_sample", False),
                    "description": case.get("description", ""),
                    "input": case.get("input", ""),
                    "output": case.get("output", ""),
                }))
            logger.info("Cached %d test cases for %s", len(test_cases), polygon_id)
        except redis.RedisError as exc:
            logger.warning("Could not cache test cases for %s: %s", polygon_id, exc)

    def get_test_cases_from_redis(self, polygon_id):
        """Return cached test cases, or None when nothing usable is cached."""
        prefix = self._cache_prefix(polygon_id)
        try:
            client = self._redis_client()
            count = client.get(f"{prefix}_count")
            if count is None:
                return None
            cases = []
            for position in range(1, int(count) + 1):
                raw = client.get(f"{prefix}_test_{position}")
                if raw is None:
                    logger.warning("Incomplete cache for %s (missing test %d)",
                                   polygon_id, position)
                    return None
                cases.append(json.loads(raw))
            logger.info("Loaded %d cached test cases for %s", len(cases), polygon_id)
            return cases
        except (redis.RedisError, ValueError) as exc:
            logger.warning("Could not read test-case cache for %s: %s", polygon_id, exc)
            return None

    def clear_test_cases_from_redis(self, polygon_id):
        """Delete every cached test-case key for the given Polygon problem id."""
        self._delete_cache_keys(f"{self._cache_prefix(polygon_id)}*")

    def delete_problem_test_case_cache(self, db_problem_id):
        """Delete cached test-case keys stored under a database problem id."""
        self._delete_cache_keys(f"{self._cache_prefix(db_problem_id)}*")

    def _delete_cache_keys(self, pattern):
        try:
            client = self._redis_client()
            deleted = 0
            for key in client.scan_iter(pattern):
                deleted += client.delete(key)
            logger.debug("Deleted %d cache keys matching %s", deleted, pattern)
        except redis.RedisError as exc:
            logger.warning("Could not delete cache keys (%s): %s", pattern, exc)
