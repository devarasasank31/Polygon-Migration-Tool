# Implementation Plan — Polygon Migration Tool

Audit date: 2026-10-28. Baseline commit: `ca40b91` (branch `main`). Recovery branch:
`recovery/pre-implementation-audit` (points at the baseline; working tree was clean, so no
uncommitted user work existed to preserve).

## Audit summary (baseline)

- Django 5.2.3 project `PolygonMigration` with apps `problems`, `users`, `contents`;
  server-rendered templates + vanilla JS (no frontend build step).
- PostgreSQL configured in `settings.py`; Redis used for 30-minute test-case caching.
- Polygon API client (`problems/polygon_api.py`) implements apiSig signing and the
  documented methods (`problem.info`, `problem.statements`, `problem.tests`,
  `problem.testInput`, `problem.testAnswer`, `problem.solutions`, `problem.viewSolution`,
  `problem.checker`, `problem.packages`, `problem.package`, `problem.viewFile`).
- Cloud storage: Azure-only (`problems/AzureTestcase.py`); `docker-compose.yml` defines
  Postgres + Redis + MinIO but no code uses MinIO yet.
- No tests exist. `Problem.notes`/`Problem.genie_chat` are missing from migration
  `0001_initial`. The exception path in `problems/views.py` calls a non-existent
  `PolygonAPI.delete_azure_blob()`. Azure upload errors are swallowed and reported as
  success. DB test-case rows are truncated to 260 characters. Re-migration leaves stale
  rows. Unique `slug` collides for same-title problems. `all_tags_json|safe` is an XSS
  vector. Local Postgres (native service, port 5432) conflicts with the compose mapping.

## Milestones

### M0 — Environment and startup (Phase B)
- Pin local infra via `docker-compose.yml` with host-port override for Postgres
  (`5433:5432`) to avoid clashing with the native PostgreSQL 17 service.
- Consolidate `.env.example` at the repo root; every required variable documented with
  placeholders; `.env` git-ignored.
- Create the missing `0002` migration for `Problem.notes` / `Problem.genie_chat`.
- Add a management command to create the staff superuser from environment variables
  (custom user model has many required fields; interactive creation stays documented).
- Virtualenv + dependencies installable on Python 3.11.

**Acceptance:** `python manage.py check`, `migrate`, and `runserver` succeed against
Docker Postgres; Redis ping succeeds; login page renders and a superuser can log in.
**Verify:** `python manage.py check`, `python manage.py migrate --plan`,
`python -c "import redis; redis.Redis(...).ping()"`.
**Risks:** port conflicts (mitigated by 5433 mapping); image availability for MinIO.

### M1 — Polygon API integration (Phase C)
- Typed exceptions (`PolygonAPIError`) with method name and sanitized comment; request
  timeouts; explicit handling for auth failures, rate limits, invalid problem ids.
- Stop swallowing exceptions in `get_test_cases` / `get_problem_files`; validate ids and
  responses before processing; never log credentials or apiSig.
- Reference-solution retrieval (`problem.solutions` + `problem.viewSolution`) kept and
  hardened (missing solution ≠ failure).

**Acceptance:** unit tests with controlled (mocked, clearly labeled) responses cover
success, auth failure, rate limit, invalid id, missing solution. Real-credential
verification runs when credentials are available.
**Verify:** `python manage.py test problems.tests.test_polygon_api -v 2`.
**Risks:** Polygon API changes; no real credentials in CI (mocked tests only).

### M2 — Problem migration to PostgreSQL (Phase D)
- Extract migration logic from the monolithic view into `problems/services.py`
  (`migrate_problem_to_db`) used by the view.
- Required-field validation (difficulty, tags sanitized/trimmed); duplicate migration is
  an explicit update; slug collisions on same-title problems resolved deterministically
  (append polygon id) instead of `IntegrityError`; DB writes transactional so failures
  leave no partial rows.

**Acceptance:** tests cover successful create, successful update, missing difficulty,
invalid tags, duplicate attempt, two problems sharing a title.
**Verify:** `python manage.py test problems.tests.test_problem_migration -v 2`.

### M3 — Test-case persistence (Phase E)
- `migrate_test_cases_to_db` service: full input/output stored (remove the silent
  260-char truncation — behavior change documented), sample vs regular flagged via
  `is_sample`, rows re-synced in a transaction (stale rows deleted on re-migration),
  malformed/empty cases skipped with an explicit count reported to the user.

**Acceptance:** tests cover zero-sample problem, multi-case problem, re-migration from
20 → 12 cases, malformed input, repeated migration idempotency.
**Verify:** `python manage.py test problems.tests.test_testcase_migration -v 2`.

### M4 — Configurable cloud storage (Phase F)
- `problems/storage.py`: small interface (`upload_test_case`, `delete_problem_files`,
  `verify_test_case`, `list_problem_files`) + factory reading `STORAGE_PROVIDER`.
- Providers: existing Azure implementation (kept, fixed to raise on failure) and a new
  S3-compatible provider (boto3) that works against MinIO locally with real credentials.
- Naming preserved for every provider:
  `test_cases/{problem_id}/{test_number}` and `test_cases/{problem_id}/{test_number}.a`.
- Upload success reported only after provider confirmation; verification reads back
  object size/content.

**Acceptance:** mocked tests for provider errors and missing configuration; real upload
+ verification + repeated upload verified against local MinIO.
**Verify:** `python manage.py test problems.tests.test_storage -v 2` plus a manual
end-to-end upload against MinIO.
**Risks:** Azure cannot be exercised without real AAD credentials (documented limit).

### M5 — Frontend and user journey (Phase G)
- Keep the server-rendered workflow; fix the XSS vector (`json_script`), double-submit
  protection (disable buttons while pending), accurate loading/error/success states,
  action names matching the assessment ("Migrate to Database", "Migrate Test Cases to
  DB", cloud upload), disabled-state logic, empty test-case handling.

**Acceptance:** full journey manually verified in the browser with mocked/simulated
Polygon data or real credentials if provided.
**Verify:** manual walkthrough + `python manage.py test problems.tests.test_views -v 2`.

### M6 — Automated tests and quality (Phase H)
- Tests for auth, API response handling, persistence, duplicates, storage naming,
  endpoints, edge cases; all DB tests run against PostgreSQL (never SQLite).
- Lint/format (ruff) at least on changed files; `manage.py check --deploy` reviewed.

**Acceptance:** full suite green against Postgres; results recorded in the final report.
**Verify:** `python manage.py test -v 2`.

### M7 — Documentation and hygiene (Phases I, J)
- README (setup, env, migrations, superuser, tests, workflow), DOCUMENTATION.md (five
  flows with real file/endpoint references), ISSUES.md skeleton (six-issue structure +
  four edge-case procedures with reproduction steps; substantive analysis left to the
  assessor), secret scan, dependency cleanup (drop unused Flask packages).

**Acceptance:** docs match implemented behavior; `git grep` finds no secrets;
`git status` clean; repo clone-and-run verified.
**Verify:** manual review + `git log --oneline`.

## Cross-cutting risks

- **Polygon credentials unavailable:** integration verified with mocked responses only
  until real `POLYGON_API_KEY`/`POLYGON_API_SECRET` are supplied; this limitation is
  reported honestly, never papered over.
- **Azure credentials unavailable:** Azure provider retained and unit-tested with mocks;
  real cloud verification performed against MinIO (S3-compatible) locally.
- **Behavior preservation:** assessment-required behavior is never weakened to make the
  implementation easier; intentional changes (full test-case storage, slug handling) are
  documented in DOCUMENTATION.md.
