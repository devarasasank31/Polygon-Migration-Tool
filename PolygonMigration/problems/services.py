"""Database and storage operations behind the migration workflow.

The HTTP layer (``problems/views.py``) only translates requests into calls to
these functions and renders the result, so every migration rule lives in one
testable place.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from .models import Problem, ProblemTag, ProblemTestCase, SampleTestCase
from .storage import get_storage

logger = logging.getLogger(__name__)

MAX_TAG_LENGTH = 100


class MigrationError(Exception):
    """Raised when a migration cannot proceed (validation or data problems)."""


def validate_difficulty(difficulty):
    valid = {value for value, _label in Problem.DIFFICULTY_CHOICES}
    if difficulty not in valid:
        raise MigrationError("Please select a difficulty level before migrating to database.")
    return difficulty


def validate_tag_names(tag_names):
    """Normalize tag names and reject invalid ones.

    Returns:
        list[str]: Deduplicated, trimmed, non-empty tag names in input order.
    """
    cleaned = []
    for raw in tag_names or []:
        name = (raw or "").strip()
        if not name:
            continue
        if len(name) > MAX_TAG_LENGTH:
            raise MigrationError(
                f"Tag '{name[:40]}...' is longer than {MAX_TAG_LENGTH} characters."
            )
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def _unique_slug(title, polygon_id):
    """Return a slug that is unique across problems.

    Two different Polygon problems can share a title (e.g. "Two Sum"), so the
    polygon id is appended on collision, with a numeric fallback.
    """
    base = slugify(title)[:200] or "problem"
    if not Problem.objects.filter(slug=base).exists():
        return base

    suffix = slugify(polygon_id)[:50]
    candidate = f"{base}-{suffix}"[:255]
    if not Problem.objects.filter(slug=candidate).exists():
        return candidate

    counter = 2
    while Problem.objects.filter(slug=f"{base}-{counter}").exists():
        counter += 1
    return f"{base}-{counter}"[:255]


def migrate_problem_to_db(*, polygon_id, difficulty, tag_names, problem_data):
    """Create or update the Problem row for a Polygon problem.

    Args:
        polygon_id: External Polygon identifier (stored as ``Problem.polygon_id``).
        difficulty: One of ``Problem.DIFFICULTY_CHOICES`` (required).
        tag_names: Tag names selected/entered by the user.
        problem_data: Dict with statement fields fetched from Polygon
            (``title``, ``problem_statement``, ``input_format``,
            ``output_format``, ``constraints``, ``editorial``, ``time_limit``,
            ``memory_limit``, ``checker_type``, ``test_case_count``, ``notes``).

    Returns:
        tuple(Problem, bool): The persisted problem and whether it was created.

    Raises:
        MigrationError: On validation failures.
    """
    validate_difficulty(difficulty)
    tags = validate_tag_names(tag_names)
    title = (problem_data.get("title") or "").strip()
    if not title:
        raise MigrationError("The fetched problem has no title; cannot migrate.")

    existing = Problem.objects.filter(polygon_id=polygon_id).first()
    slug = existing.slug if existing else _unique_slug(title, polygon_id)

    try:
        with transaction.atomic():
            if existing:
                problem = existing
                created = False
            else:
                problem = Problem(polygon_id=polygon_id, slug=slug)
                created = True

            problem.title = title
            problem.slug = slug
            problem.difficulty = difficulty
            problem.problem_statement = problem_data.get("problem_statement") or ""
            problem.input_format = problem_data.get("input_format") or ""
            problem.output_format = problem_data.get("output_format") or ""
            problem.constraints = problem_data.get("constraints") or ""
            problem.editorial = problem_data.get("editorial") or ""
            problem.time_limit = problem_data.get("time_limit") or 1000
            problem.memory_limit = problem_data.get("memory_limit") or 256
            problem.checker_type = problem_data.get("checker_type") or "ncmp"
            problem.test_case_count = problem_data.get("test_case_count") or 0
            problem.notes = problem_data.get("notes") or ""
            problem.save()

            tag_objects = [ProblemTag.objects.get_or_create(tag_name=name)[0] for name in tags]
            problem.extra_tags.set(tag_objects)
    except IntegrityError as exc:
        raise MigrationError(f"Database constraint violated while saving: {exc}") from exc

    logger.info("%s problem '%s' (polygon_id=%s, slug=%s)",
                "Created" if created else "Updated", title, polygon_id, slug)
    return problem, created


def sync_sample_test_cases(problem, test_cases):
    """Replace the problem's SampleTestCase rows with the current samples.

    Cases without both input and output are ignored. Runs in one transaction
    so a failure never leaves a partially replaced set.
    """
    samples = [
        case for case in test_cases
        if case.get("is_sample") and (case.get("input") or "").strip()
        and (case.get("output") or "").strip()
    ]
    with transaction.atomic():
        SampleTestCase.objects.filter(problem=problem).delete()
        for order, case in enumerate(samples, start=1):
            SampleTestCase.objects.create(
                problem=problem,
                input=case["input"].rstrip(),
                output=case["output"].rstrip(),
                order=order,
            )
    logger.info("Synced %d sample test cases for problem %s", len(samples), problem.id)
    return len(samples)


def sync_problem_test_cases(problem, test_cases):
    """Replace the problem's ProblemTestCase rows with the current test set.

    Full input/output content is stored (the starter implementation truncated
    both to 260 characters, silently losing data — see DOCUMENTATION.md).

    Returns:
        dict: ``{"total": len(test_cases), "saved": n, "skipped": m}`` where
        skipped counts tests with empty input or output.
    """
    saved = 0
    skipped = 0
    rows = []
    for order, case in enumerate(test_cases, start=1):
        input_data = (case.get("input") or "").rstrip()
        output_data = (case.get("output") or "").rstrip()
        if not input_data or not output_data:
            skipped += 1
            continue
        rows.append(ProblemTestCase(
            problem=problem,
            is_sample=bool(case.get("is_sample")),
            input=input_data,
            output=output_data,
            description=case.get("description") or "",
            order=order,
        ))
        saved += 1

    with transaction.atomic():
        # Stale rows from a previous migration are removed so a re-migration
        # with fewer tests does not keep orphaned cases around.
        ProblemTestCase.objects.filter(problem=problem).delete()
        ProblemTestCase.objects.bulk_create(rows)

    logger.info("Synced %d test cases (%d skipped) for problem %s",
                saved, skipped, problem.id)
    return {"total": len(test_cases), "saved": saved, "skipped": skipped}


def upload_test_cases_to_storage(problem, test_cases, api=None, storage=None):
    """Upload a problem's test-case files to the configured cloud storage.

    The previous files of the problem are replaced so repeated uploads are
    idempotent, and every upload is verified against the provider before
    success is reported. When a Polygon API client is supplied, an available
    custom checker is compiled and uploaded next to the test files (behavior
    retained from the starter implementation).

    Returns:
        dict: Result of ``BaseTestCaseStorage.replace_problem_files`` plus
        ``"checker"`` (name of the uploaded custom checker, or None).

    Raises:
        StorageError: When the provider is unavailable, misconfigured, or an
            upload/verification step fails.
    """
    storage = storage or get_storage()
    result = storage.replace_problem_files(problem.id, test_cases)
    result["checker"] = None

    if api is not None:
        result["checker"] = _upload_custom_checker(api, problem, storage)
    return result


def _upload_custom_checker(api, problem, storage):
    """Compile and upload the problem's custom checker, if it has one.

    Returns the uploaded object name, or None when the problem uses a standard
    checker or compilation is unavailable (the checker source is uploaded as a
    fallback in that case, matching the starter behavior).
    """
    checker_info = api.get_custom_checker_info(problem.polygon_id)
    if not checker_info:
        return None

    source = api.fetch_file_content(problem.polygon_id, checker_info["name"])
    if not source:
        logger.warning("Custom checker %s could not be fetched; skipping upload",
                       checker_info["name"])
        return None

    binary_path = api.compile_custom_checker(source)
    if binary_path:
        with open(binary_path, "rb") as handle:
            data = handle.read()
        key = f"test_cases/{problem.id}/custom_checker"
        if binary_path.endswith(".exe"):
            key += ".exe"
    else:
        data = source.encode("utf-8")
        key = f"test_cases/{problem.id}/custom_checker.cpp"

    storage.upload_file(key, data)
    logger.info("Uploaded custom checker as %s", key)
    return key
