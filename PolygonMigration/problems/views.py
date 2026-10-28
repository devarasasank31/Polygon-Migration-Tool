"""HTTP layer for the migration workflow.

``index`` dispatches the four POST actions of the UI:

- fetch a problem (no action flag),
- migrate the problem to PostgreSQL (``migrate_to_db``),
- migrate test cases to PostgreSQL (``migrate_test_cases_to_db``),
- upload test-case files to cloud storage (``upload_to_storage``).

All persistence rules live in ``problems.services``; all Polygon access lives
in ``problems.polygon_api``. Failures are converted into user-facing messages
and a success message is only ever set after the underlying operation really
succeeded.
"""

import json
import logging
import re

import lxml.html
from django.conf import settings
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render

from .models import Problem, ProblemTag
from .polygon_api import PolygonAPI, PolygonAPIError
from .services import (
    MigrationError,
    migrate_problem_to_db,
    sync_sample_test_cases,
    sync_problem_test_cases,
    upload_test_cases_to_storage,
)
from .storage import StorageError, get_storage

logger = logging.getLogger(__name__)

VALID_CHECKERS = {'ncmp', 'fcmp', 'hcmp', 'lcmp', 'nyesno', 'rcmp4', 'rcmp6', 'rcmp9', 'wcmp', 'yesno'}


def parse_problem_html(html_content):
    """
    Parses a Polygon problem.html content and extracts key fields such as title, legend, input/output formats, and notes.

    Args:
        html_content (str): The HTML content of the problem.html file.

    Returns:
        dict: A dictionary containing the extracted fields:
            - title (str): The problem title.
            - legend (str): The problem statement/legend HTML.
            - input_format (str): The input specification HTML.
            - output_format (str): The output specification HTML.
            - notes (str): The notes HTML (if present).
    """
    logger.info('Entered parse_problem_html with content length=%d', len(html_content))
    tree = lxml.html.fromstring(html_content)

    def get_div_inner_html(tree, class_name, skip_section_title=False):
        """
        Extracts the inner HTML of a div with a given class name from the lxml tree.

        Args:
            tree (lxml.html.HtmlElement): The parsed HTML tree.
            class_name (str): The class name of the div to extract.
            skip_section_title (bool): If True, skips the first child div with class 'section-title'.

        Returns:
            str: The concatenated inner HTML of the div's children, or an empty string if not found.
        """
        div = tree.xpath(f'//div[@class="{class_name}"]')
        if not div:
            return ''
        div = div[0]
        children = div.getchildren()
        html_parts = []
        for child in children:
            # For input/output, skip the first section-title div if requested
            if skip_section_title and child.tag == 'div' and 'section-title' in child.get('class', ''):
                continue
            html_parts.append(lxml.html.tostring(child, encoding='unicode'))
        content = ''.join(html_parts).strip()
        # Remove leading empty paragraph tags
        content = re.sub(r'^(<p>\s*</p>)+', '', content)
        return content

    legend = get_div_inner_html(tree, 'legend')
    input_format = get_div_inner_html(tree, 'input-specification', skip_section_title=True)
    output_format = get_div_inner_html(tree, 'output-specification', skip_section_title=True)

    title = ''
    title_div = tree.xpath('//div[@class="title"]')
    if title_div:
        title = title_div[0].text_content().strip()

    notes = get_div_inner_html(tree, 'note', skip_section_title=True)

    logger.debug('Extracted title=%s, legend=%d, input=%d, output=%d, notes=%d chars',
                 title, len(legend), len(input_format), len(output_format), len(notes))
    return {
        'title': title,
        'legend': legend,
        'input_format': input_format,
        'output_format': output_format,
        'notes': notes,
    }


def _normalize_checker(raw_checker):
    """Map a raw ``problem.checker`` response onto the model's checker choices."""
    checker = (raw_checker or 'ncmp').strip()
    if checker.startswith('std::'):
        checker = checker[len('std::'):]
    if checker.endswith('.cpp'):
        checker = checker[:-len('.cpp')]
    return checker if checker in VALID_CHECKERS else 'custom'


def _load_polygon_data(api, polygon_id):
    """Fetch everything the UI needs for one problem from Polygon.

    Test cases are served from the 30-minute Redis cache when available.

    Returns:
        dict: ``info``, ``parsed`` (statement parts), ``test_cases``,
        ``solution``, ``checker_type`` and ``custom_checker``.

    Raises:
        PolygonAPIError: When any required fetch fails.
    """
    info = api.get_problem_info(polygon_id)
    parsed = parse_problem_html(api.download_and_extract_package(polygon_id))

    test_cases = api.get_test_cases_from_redis(polygon_id)
    if test_cases is None:
        test_cases = api.get_all_test_cases(polygon_id)
        api.store_test_cases_in_redis(polygon_id, test_cases)
    else:
        logger.info('Using cached test cases for %s', polygon_id)

    solution = api.get_reference_solution(polygon_id)

    custom_checker = None
    try:
        raw_checker = api._make_request('problem.checker', {'problemId': polygon_id})
    except PolygonAPIError as exc:
        logger.info('problem.checker unavailable for %s: %s', polygon_id, exc.message)
        raw_checker = None
    checker_type = _normalize_checker(raw_checker)
    if checker_type == 'custom':
        custom_checker = api.get_custom_checker_info(polygon_id) or {'name': raw_checker, 'type': 'custom'}

    return {
        'info': info,
        'parsed': parsed,
        'test_cases': test_cases,
        'solution': solution,
        'checker_type': checker_type,
        'custom_checker': custom_checker,
    }


def _fetched_context(data, polygon_id, difficulty):
    """Build the template context describing a fetched problem."""
    info, parsed = data['info'], data['parsed']
    title = parsed['title'] or info.get('name', f'Polygon Problem {polygon_id}')
    return {
        'polygon_id': polygon_id,
        'title': title,
        'difficulty': difficulty,
        'problem_statement': parsed['legend'],
        'input_format': parsed['input_format'],
        'output_format': parsed['output_format'],
        'constraints': '',
        'editorial': '',
        'time_limit': info.get('timeLimit', 1000),
        'memory_limit': info.get('memoryLimit', 256),
        'checker_type': data['checker_type'],
        'custom_checker_info': data['custom_checker'],
        'test_case_count': len(data['test_cases']),
        'notes': parsed['notes'],
    }


def _display_test_cases(test_cases):
    """Shape test cases for the preview table (truncated for display only)."""
    display = []
    for case in test_cases:
        input_data = case.get('input', '')
        output_data = case.get('output', '')
        description = case.get('description', '')
        display.append({
            'index': case.get('index', ''),
            'input_preview': input_data[:50] + ('...' if len(input_data) > 50 else ''),
            'output_preview': output_data[:50] + ('...' if len(output_data) > 50 else ''),
            'description_preview': description[:50] + ('...' if len(description) > 50 else ''),
            'is_sample': case.get('is_sample', False),
            'full_input': input_data,
            'full_output': output_data,
            'full_description': description,
        })
    return display


def _problem_data_from(fetched, polygon_id):
    """Shape the fetched problem for :func:`services.migrate_problem_to_db`."""
    return {
        'title': fetched['title'],
        'problem_statement': fetched['problem_statement'],
        'input_format': fetched['input_format'],
        'output_format': fetched['output_format'],
        'constraints': fetched['constraints'],
        'editorial': fetched['editorial'],
        'time_limit': fetched['time_limit'],
        'memory_limit': fetched['memory_limit'],
        'checker_type': fetched['checker_type'],
        'test_case_count': fetched['test_case_count'],
        'notes': fetched['notes'],
    }


def _handle_problem_migration(request, api, polygon_id, data, context):
    """Persist the fetched problem with difficulty and tags; sync samples."""
    difficulty = request.POST.get('difficulty', '')
    tag_names = list(request.POST.getlist('tags'))
    if request.POST.get('new_tag', '').strip():
        tag_names.append(request.POST['new_tag'].strip())

    fetched = context['fetched_problem']
    problem, created = migrate_problem_to_db(
        polygon_id=polygon_id,
        difficulty=difficulty,
        tag_names=tag_names,
        problem_data=_problem_data_from(fetched, polygon_id),
    )
    samples = sync_sample_test_cases(problem, data['test_cases'])

    verb = 'created' if created else 'updated'
    message = f"Problem '{problem.title}' {verb} in database (id {problem.id}, {samples} sample test cases)."
    if data['custom_checker']:
        message += f" Custom checker '{data['custom_checker']['name']}' detected."
    context['db_success'] = message
    context['db_problem'] = problem
    context['difficulty'] = problem.difficulty
    context['selected_tags'] = [tag.tag_name for tag in problem.extra_tags.all()]
    logger.info(message)


def _handle_test_case_migration(request, api, polygon_id, data, context):
    """Persist the full test set of the fetched problem."""
    problem = context.get('db_problem') or Problem.objects.filter(polygon_id=polygon_id).first()
    if not problem:
        raise MigrationError('Please migrate the problem to the database first.')

    stats = sync_problem_test_cases(problem, data['test_cases'])
    message = (f"Migrated {stats['saved']} test cases to the database "
               f"({stats['total']} fetched, {stats['skipped']} skipped: missing input or output).")
    context['success'] = message
    context['db_problem'] = problem
    logger.info(message)


def _handle_storage_upload(request, api, polygon_id, data, context):
    """Upload test-case files to the configured cloud storage provider."""
    problem = context.get('db_problem') or Problem.objects.filter(polygon_id=polygon_id).first()
    if not problem:
        raise MigrationError('Please migrate the problem to the database first.')

    storage = get_storage()
    result = upload_test_cases_to_storage(problem, data['test_cases'], api=api, storage=storage)
    message = (f"Uploaded {result['uploaded']} test cases to {storage.provider} storage "
               f"and verified them ({result['skipped']} skipped: missing input or output).")
    if result['checker']:
        message += f" Custom checker uploaded as {result['checker']}."
    context['success'] = message
    context['db_problem'] = problem
    logger.info(message)


def _error_message(exc):
    """Translate backend exceptions into honest, actionable user messages."""
    if isinstance(exc, PolygonAPIError):
        if exc.auth_error:
            return ('Polygon rejected the API credentials. Check POLYGON_API_KEY and '
                    'POLYGON_API_SECRET, and that the key has access to this problem.')
        if exc.rate_limited:
            return ('Polygon rate-limited the request. Wait a moment and try again. '
                    f'(Details: {exc.message})')
        return f"Could not fetch the problem from Polygon: {exc.message}"
    if isinstance(exc, StorageError):
        return f"Cloud storage upload failed: {exc}"
    if isinstance(exc, MigrationError):
        return str(exc)
    if isinstance(exc, ValueError):
        return str(exc)
    logger.error('Unexpected error in migration workflow: %s', exc, exc_info=True)
    return f'Unexpected error: {exc}'


@user_passes_test(lambda u: u.is_authenticated and u.is_staff, login_url='/users/login/')
def index(request):
    """Main migration interface: fetch, review, and migrate Polygon problems."""
    logger.info('index view: method=%s', request.method)
    all_tags = ProblemTag.objects.all().order_by('tag_name')
    context = {
        'all_tags': all_tags,
        'all_tags_json': json.dumps([
            {'pk': tag.id, 'fields': {'name': tag.tag_name}} for tag in all_tags
        ]),
        'selected_tags': [],
        'selected_tags_json': json.dumps([]),
        'storage_provider': settings.STORAGE_PROVIDER,
    }

    if request.method == 'POST':
        polygon_id_input = request.POST.get('problem_id')
        try:
            polygon_id = PolygonAPI.validate_problem_id(polygon_id_input)
        except ValueError as exc:
            context['error'] = str(exc)
            return render(request, 'problems/index.html', context)

        context['problem_id'] = polygon_id
        api = PolygonAPI()
        db_problem = Problem.objects.filter(polygon_id=polygon_id).first()
        if db_problem:
            context['db_problem'] = db_problem
            context['difficulty'] = db_problem.difficulty
            context['selected_tags'] = [tag.tag_name for tag in db_problem.extra_tags.all()]

        migrate_to_db = bool(request.POST.get('migrate_to_db'))
        migrate_test_cases = bool(request.POST.get('migrate_test_cases_to_db'))
        upload_to_storage = bool(
            request.POST.get('upload_to_storage') or request.POST.get('migrate_to_azure')
        )

        try:
            data = _load_polygon_data(api, polygon_id)
            fetched = _fetched_context(data, polygon_id, request.POST.get('difficulty', ''))
            context['fetched_problem'] = fetched
            context['all_test_cases'] = _display_test_cases(data['test_cases'])
            if data['solution']:
                context['main_solution'] = data['solution']['source']

            if migrate_to_db:
                _handle_problem_migration(request, api, polygon_id, data, context)
            elif migrate_test_cases:
                _handle_test_case_migration(request, api, polygon_id, data, context)
            elif upload_to_storage:
                _handle_storage_upload(request, api, polygon_id, data, context)
        except Exception as exc:  # noqa: BLE001 - converted to a user-facing message
            logger.error('Migration workflow failed for %s: %s', polygon_id, exc, exc_info=True)
            context['error'] = _error_message(exc)
            # Successfully fetched data (if any) stays visible so the user can
            # correct the input and retry without re-fetching from Polygon.

    context['selected_tags_json'] = json.dumps(context.get('selected_tags', []))
    return render(request, 'problems/index.html', context)
