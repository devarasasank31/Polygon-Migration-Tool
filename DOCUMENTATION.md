# Documentation — Polygon Migration Tool

This document describes the architecture and runtime flows of the Polygon
Migration Tool as implemented in this repository. Every file, function, and
endpoint referenced below exists in the codebase; line numbers are
intentionally omitted (they drift), so each reference includes the file path
and symbol name so you can locate it with a search.

---

## 1. System Overview

```
┌─────────────┐   POST / (form)    ┌─────────────────────────────────┐
│  Browser    │ ─────────────────► │ Django: problems.views.index     │
│ index.html  │ ◄────────────────── │ (staff-only, re-renders page)   │
└─────────────┘     rendered HTML  └───────┬───────────────┬─────────┘
                                           │               │
                       ┌───────────────────▼──┐   ┌────────▼─────────────┐
                       │ polygon_api.py       │   │ services.py          │
                       │ PolygonAPI           │   │ migrate_problem_     │
                       │ (signing, retries,   │   │ db, sync_sample_     │
                       │  Redis test cache)   │   │ test_cases, sync_    │
                       └───────┬──────────────┘   │ problem_test_cases,  │
                               │                  │ upload_test_cases_   │
               ┌───────────────▼──┐               │ to_storage)          │
               │ Polygon REST API │               └──────────┬───────────┘
               │ (polygon.codeforces.com)                    │
               └─────────────────────────────┐      ┌────────▼──────────────┐
                                             │      │ storage.py            │
     ┌────────────────┐   ┌────────────────┐ │      │ MinioStorage /        │
     │ PostgreSQL     │   │ Redis          │ │      │ AzureBlobStorage      │
     │ (Problem,      │   │ (30-min test-  │ │      └────────┬──────────────┘
     │  SampleTest-   │   │  case cache)   │ │               │
     │  Case,         │   └────────────────┘ │      ┌────────▼───────────────┐
     │  ProblemTest-  │                      │      │ Object storage         │
     │  Case rows)    │                      │      │ (MinIO/S3 or Azure)    │
     └────────────────┘                      │      └────────────────────────┘
                                             │
```

One Django project (`PolygonMigration/PolygonMigration/`) with three apps:

- **`users`** — custom user model (`users.User`, `AUTH_USER_MODEL`), email
  login backend (`users.backends.EmailBackend`), staff dashboard, and the
  `create_staff_superuser` management command (env-driven; no hardcoded
  credentials).
- **`problems`** — the Polygon client, storage providers, migration
  services, models, the staff-only migration page, and the test suite.
- **`contents`** — ancillary content app carried over from the starter
  codebase.

---

## 2. Request Lifecycle (the migration page)

1. The browser loads `problems/templates/problems/index.html` (staff-only).
   Four forms post to `/`:
   - fetch form (id `fetchForm`) — retrieves a problem for preview,
   - `migrate-db-form` — hidden input `migrate_to_db=1`,
   - `migrate-test-cases-form` — `migrate_test_cases_to_db=1`,
   - `migrate-azure-form` (legacy id) — `upload_to_storage=1` (the legacy
     field name `migrate_to_azure` is still accepted as an alias in
     `views.index`).
   Buttons are disabled client-side while a submission is in flight.
2. `PolygonMigration/urls.py` includes `problems.urls` at `""`; the page is
   served by `problems.views.index`.
3. `index` is gated with
   `@user_passes_test(lambda u: u.is_authenticated and u.is_staff, login_url='/users/login/')`.
   On `POST` it:
   - validates the problem id (`PolygonAPI.validate_problem_id`, rejecting
     empty/invalid input before any network call),
   - builds a `PolygonAPI` client from the per-request credentials (form
     fields take precedence; environment fallback otherwise),
   - calls `_load_polygon_data` (fetch + Redis cache), then dispatches to
     `_handle_problem_migration`, `_handle_test_case_migration`, or
     `_handle_storage_upload` based on which flag was posted,
   - re-renders `index.html` with the fetched preview and the operation's
     result message.
4. Any exception is converted to a user-facing message by `_error_message`
   (Polygon auth rejection → "check POLYGON_API_KEY / …"; rate limit;
   storage failure; migration validation; unexpected errors are logged with
   traceback and shown honestly). Successfully fetched data stays visible
   so the user can correct input and retry.

Authentication: the `users` app provides login/logout against the custom
email-based user model; anonymous or non-staff requests are redirected to
`/users/login/` by the decorator above.

---

## 3. Configuration

All runtime configuration is read from environment variables via
`python-dotenv` in `PolygonMigration/PolygonMigration/settings.py` (it loads
`.env` from `PolygonMigration/` first, then the repository root). Notables:

- `DB_*` (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`) —
  PostgreSQL connection (default port `5432`; the provided Docker stack
  maps the container to host port **5433**).
- `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` / `REDIS_SSL` — Redis for
  the test-case cache.
- `POLYGON_API_KEY` / `POLYGON_API_SECRET` — server-side default Polygon
  credentials; per-request form input takes precedence and is never stored.
- `STORAGE_PROVIDER` — selects the storage backend (`minio` default,
  `azure` optional).
- MinIO/S3: `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`,
  `MINIO_BUCKET_NAME`, `MINIO_SECURE`.
- Azure: `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_USERNAME`, `AZURE_PASSWORD`, `AZURE_CONTAINER_NAME` (AAD app with
  username/password credentials).
- `SUPERUSER_*` — consumed by `users.management.commands.create_staff_superuser`.
- `CUSTOM_CHECKER_DIR` — scratch dir for custom-checker compilation.

`docker-compose.yml` provides the full local stack: PostgreSQL 17 (host
port **5433** → container 5432, to avoid colliding with a natively
installed PostgreSQL), Redis 7 (6379), MinIO (9000 API / 9001 console) with
an init job that pre-creates the configured bucket.

---

## 4. Data Model (`problems/models.py`)

- **`Problem`** — one row per migrated Polygon problem. Key fields:
  `polygon_id` (unique external id), unique `slug`, `title`, `difficulty`
  (easy/medium/hard), statement parts (`problem_statement`,
  `input_format`, `output_format`, `constraints`, `editorial`), limits
  (`time_limit` in ms, `memory_limit` in MB), `checker_type`,
  `test_case_count`, `notes`, `extra_tags` (M2M → `ProblemTag`), plus
  starter-page fields (`avg_time_taken`, `total_submissions`, `is_locked`,
  `genie_*`, …).
- **`SampleTestCase`** — `(problem FK, input, output, order)` for samples
  shown with the statement.
- **`ProblemTestCase`** — `(problem FK, is_sample, input, output,
  description, order, created_at, updated_at)` for the full test set. Full
  content is stored (see `ISSUES.md` §Q4 for the history of a former
  260-character truncation).
- **`ProblemTag`** — shared tag vocabulary (`tag_name`, unique).
- **`users.User`** (`users/models.py`) — custom user model (email login,
  profile fields).

`users/management/commands/create_staff_superuser.py` creates or updates a
staff superuser from `SUPERUSER_*` environment variables; it refuses to run
without `SUPERUSER_EMAIL` and `SUPERUSER_PASSWORD` (no hardcoded
credentials anywhere in the repo).

---

## 5. Polygon API Client (`problems/polygon_api.py`)

Class **`PolygonAPI`** (constructed as `PolygonAPI(api_key=…, api_secret=…)`:

- **Signing** (`_generate_api_sig`): builds the documented Polygon `apiSig`
  scheme — `time` + sorted `params` joined as `key=value&…`, HMAC-SHA512
  signed with the API secret, appended as `apiSig`. The scheme matches the
  original starter implementation; it has not yet been validated against
  the live service (see `ISSUES.md`).
- **Requests** (`_post`, `_make_request`, `_make_plain_request`): explicit
  timeouts, an `apiSig` retry with a fresh timestamp on failure, throttling
  on HTTP 429, and bounded retries. Failures raise **`PolygonAPIError`**
  carrying `http_status`, `auth_error`, and `rate_limited` flags plus a
  safe message; `_classify_failure` maps status codes to those flags. An
  incomplete fetch (e.g. one test's input missing) is never returned as a
  silent success.
- **Problem fetch**:
  - `get_problem_info(problem_id)` — `problem.info`;
  - `download_and_extract_package(problem_id, package_type="standard")` —
    downloads the statement package and returns its HTML content for
    `views.parse_problem_html`;
  - `get_tests(problem_id, testset="tests")` — the testset list;
  - `get_all_test_cases(problem_id, testset="tests")` — every test with
    input/answer fetched via `problem.testInput` / `problem.testAnswer`,
    returned as `[{index, is_sample, description, input, output}, …]`;
  - `get_reference_solution(problem_id)` — main (`MA`) solution source via
    `problem.solutions` / `problem.viewSolution`;
  - `get_custom_checker_info(problem_id)`, `fetch_file_content(...)`,
    `compile_custom_checker(source_code, work_dir=None)` — custom-checker
    support.
- **Validation**: `validate_problem_id` (classmethod) rejects empty or
  malformed ids before any network call.
- **Caching** (`store_test_cases_in_redis` / `get_test_cases_from_redis` /
  `clear_test_cases_from_redis` / `delete_problem_test_case_cache`): Redis
  keys are prefixed under `_cache_prefix(polygon_id)`; values are JSON with
  a 30-minute default expiry; cache reads/writes tolerate a down Redis
  without failing the request. `views._load_polygon_data` serves test cases
  from this cache when available and refreshes it otherwise.

---

## 6. Storage Providers (`problems/storage.py`)

A provider interface plus two implementations and a factory. Object naming
is fixed by contract (asserted by tests):

```
test_cases/{problem_id}/{NN}        # input file
test_cases/{problem_id}/{NN}.a      # expected output file
```

where `problem_id` is the **database primary key** of the problem and `NN`
is the test number **zero-padded to two digits** (as in the original Azure
implementation).

- **`BaseTestCaseStorage`** — naming helpers (`input_key`, `output_key`,
  `problem_prefix`), provider primitives (`_put`, `_head`, `_get`,
  `_delete_prefix`, `_list` — subclass responsibilities), and shared
  composites:
  - `upload_test_case(problem_id, test_number, input_data, output_data)` —
    uploads both objects and **verifies** them with `_head` (expected byte
    size) before reporting success;
  - `verify_test_case(...)` — existence/content check per file;
  - `replace_problem_files(problem_id, test_cases)` — deletes every object
    under `test_cases/{id}/` first (repeat uploads are idempotent), then
    uploads and verifies each case; skips cases with empty input or output;
  - `list_problem_files` / `delete_problem_files`.
  All failures raise **`StorageError`**.
- **`MinioStorage`** — boto3 `client('s3', endpoint_url=…)` with path-style
  addressing against any S3-compatible endpoint (MinIO locally, AWS S3,
  Cloudflare R2).
- **`AzureBlobStorage`** — Azure Blob via `azure-storage-blob` using an AAD
  `UsernamePasswordCredential` (account URL, tenant id, client id,
  username, password, container).
- **`get_storage()`** — factory reading `settings.STORAGE_PROVIDER`
  (default `minio`) and the matching settings; raises `StorageError` with
  the list of missing variables when the selected provider is not fully
  configured (misconfiguration is reported early, not as a late SDK error).

---

## 7. Migration Services (`problems/services.py`)

Business logic used by the views (and unit-tested directly):

- **`migrate_problem_to_db(*, polygon_id, difficulty, tag_names, problem_data)`** —
  creates or updates the `Problem` row. `difficulty` must be one of
  `Problem.DIFFICULTY_CHOICES` and the title must be non-empty
  (`MigrationError` otherwise). The row is keyed on the unique
  `polygon_id`; the slug is generated by `_unique_slug(title, polygon_id)`,
  which appends the Polygon id (then a numeric counter) on collision, so
  distinct problems with equal titles (e.g. multiple "Two Sum") never
  overwrite each other (`ISSUES.md` §Q3). Returns `(problem, created)`.
- **`sync_sample_test_cases(problem, test_cases)`** — **replaces** the
  problem's `SampleTestCase` rows with the current samples (delete + insert
  in one transaction). Cases without both input and output are ignored;
  `is_sample=False` cases are filtered out. Returns the number of samples
  stored (0 when the problem has none — `ISSUES.md` §Q1).
- **`sync_problem_test_cases(problem, test_cases)`** — **replaces** the
  problem's `ProblemTestCase` rows with the current test set (stale rows
  from a previous migration are deleted inside the same transaction, so a
  re-migration with fewer tests does not keep orphaned cases —
  `ISSUES.md` §Q2). Tests with empty input or output are skipped and
  counted; full content is stored without truncation. Returns
  `{"total", "saved", "skipped"}`.
- **`upload_test_cases_to_storage(problem, test_cases, api=None, storage=None)`** —
  delegates to `storage.replace_problem_files(problem.id, test_cases)` and,
  when a Polygon client is supplied, compiles and uploads the problem's
  custom checker next to the test files
  (`_upload_custom_checker`; object name `test_cases/{id}/custom_checker`
  or `.cpp`/`.exe` variants, retained from the starter behavior). Returns
  the storage result plus `"checker"`.

---

## 8. Views (`problems/views.py`)

- **`index(request)`** — the single migration page (`POST /`). Validates
  staff auth, extracts and validates inputs, dispatches on the posted flag
  (`migrate_to_db` / `migrate_test_cases_to_db` / `upload_to_storage`, with
  legacy `migrate_to_azure` accepted as an alias), and converts any
  exception into an honest message via `_error_message`.
- **`_load_polygon_data(api, polygon_id)`** — fetches info, statement
  package (parsed by `parse_problem_html`), test cases (Redis cache first),
  reference solution, and custom checker info; raises `PolygonAPIError` on
  required-fetch failure.
- **`_fetched_context` / `_display_test_cases` / `_problem_data_from`** —
  shape fetched data for template preview (50-char display previews; full
  content retained in `full_*` fields) and for
  `services.migrate_problem_to_db`.
- **`_handle_problem_migration`** — persists the problem with the chosen
  difficulty/tags and syncs sample test cases; reports created-vs-updated.
- **`_handle_test_case_migration`** — persists the full test set; requires
  the problem row to exist first ("Please migrate the problem to the
  database first.").
- **`_handle_storage_upload`** — uploads to the configured provider;
  requires the problem row to exist first.
- **`_normalize_checker(raw_checker)`** — maps raw `problem.checker`
  responses (`std::ncmp`, `fcmp.cpp`, …) onto `Problem.CHECKER_TYPE_CHOICES`,
  falling back to `custom`.

Templates: `index.html` (dashboard + migration forms + preview tables),
`users` login/signup, `problem_list.html` (migrated-problems listing).
Tag JSON is embedded with Django's `json_script` filter (the previous
`{{ …|safe }}` interpolation of tag JSON was an XSS vector and was
removed).

---

## 9. Testing (`PolygonMigration/problems/tests/`, `users/tests.py`)

Run with `python manage.py test --noinput` **from the `PolygonMigration/`
directory** (the project uses PostgreSQL only; a dedicated test database is
created).

- `helpers.py` — canned Polygon payloads (`PROBLEM_HTML`, `PROBLEM_INFO`,
  `TEST_CASES`), a `fake_polygon_api` mock factory for view tests, and
  availability probes (`redis_available`, `minio_available`).
- `test_polygon_api.py` — signing determinism, problem-id validation, error
  classification (auth/rate-limited), test fetching, reference solution,
  package download, and a Redis cache round-trip (skips if Redis is
  unreachable).
- `test_storage.py` — naming contract, factory behavior (unknown/missing
  configuration), MinIO error mapping, and a real MinIO integration
  round-trip (upload/verify/replace/delete) that skips if the container is
  down; the shared `BaseTestCaseStorage` composites are additionally
  exercised against an in-memory provider in the service tests.
- `test_services.py` — difficulty/tag validation, create-vs-update
  semantics, slug-collision handling (edge case Q3), sample sync (Q1),
  full-set replace semantics and empty-input/output skipping (Q2, Q4), and
  storage-upload service behavior.
- `test_views.py` — access control, input validation, and the full
  fetch/migrate/upload flows with a mocked Polygon client.
- `users/tests.py` — login/logout flow, staff gating, superuser command.

External-service tests skip cleanly when the service is down, so the suite
also passes on machines without Docker (those tests report skips rather
than failures).

---

## 10. Deployment Notes

The project ships containerized **infrastructure only** (database, cache,
object storage). Running the Django app itself in production is left as an
exercise; `settings.py` reads every deployment-sensitive value
(`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, database, Redis, and storage
credentials) from environment variables, so any process manager (gunicorn
behind a reverse proxy, or a container) will work. Do not run with
`DEBUG=True` or the development fallback secret key outside local
development, and set `POLYGON_API_KEY`/`POLYGON_API_SECRET` and storage
credentials via the environment (never in code or version control).
