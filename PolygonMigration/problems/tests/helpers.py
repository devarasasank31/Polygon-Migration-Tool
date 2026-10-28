"""Shared test helpers: canned Polygon data and availability probes."""

import socket

import redis as redis_lib

PROBLEM_HTML = """
<html><body>
<div class="title">A + B</div>
<div class="legend"><p>Given two integers, print their sum.</p></div>
<div class="input-specification"><div class="section-title">Input</div><p>Two integers.</p></div>
<div class="output-specification"><div class="section-title">Output</div><p>One integer.</p></div>
<div class="note"><div class="section-title">Note</div><p>Test notes.</p></div>
</body></html>
"""

PROBLEM_INFO = {
    "name": "a_plus_b",
    "timeLimit": 1000,
    "memoryLimit": 256,
    "reference": "12345",
}

# Two samples and one regular test, matching get_all_test_cases' output shape.
TEST_CASES = [
    {"index": 1, "is_sample": True, "description": "first sample", "input": "1 2\n", "output": "3\n"},
    {"index": 2, "is_sample": True, "description": "second sample", "input": "10 20\n", "output": "30\n"},
    {"index": 3, "is_sample": False, "description": "", "input": "100 200\n", "output": "300\n"},
]


def fake_polygon_api(mock_cls, test_cases=None):
    """Configure a patched ``problems.views.PolygonAPI`` class for view tests.

    Returns the configured instance the view will receive.
    """
    from problems.polygon_api import PolygonAPI

    # The view validates ids on the class before constructing the client,
    # so the real validator is attached to the patched class.
    mock_cls.validate_problem_id = PolygonAPI.validate_problem_id
    instance = mock_cls.return_value
    instance.get_problem_info.return_value = dict(PROBLEM_INFO)
    instance.download_and_extract_package.return_value = PROBLEM_HTML
    instance.get_test_cases_from_redis.return_value = None
    instance.get_all_test_cases.return_value = [
        dict(case) for case in (TEST_CASES if test_cases is None else test_cases)
    ]
    instance.get_reference_solution.return_value = {"name": "main.cpp", "source": "int main(){}"}
    instance._make_request.return_value = "std::ncmp"
    instance.get_custom_checker_info.return_value = None
    return instance


def redis_available():
    try:
        from django.conf import settings

        client = redis_lib.StrictRedis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD, ssl=settings.REDIS_SSL,
            socket_connect_timeout=1,
        )
        return bool(client.ping())
    except Exception:
        return False


def minio_available():
    import os

    endpoint = os.getenv("MINIO_ENDPOINT", "localhost:9000")
    host, _, port = endpoint.partition(":")
    try:
        with socket.create_connection((host, int(port or 9000)), timeout=1):
            return True
    except OSError:
        return False
