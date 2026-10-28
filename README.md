# Polygon Migration Tool

A Django-based internal tool for migrating problems and test cases from the
[Polygon](https://polygon.codeforces.com) competitive-programming platform into a
PostgreSQL database, with optional upload of test-case files to cloud object
storage (MinIO by default, Azure Blob Storage optionally).

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Local Setup](#local-setup)
- [Configuration](#configuration)
- [Running](#running)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)
- [Documentation](#documentation)

## Overview

Staff users authenticate locally (email + password on the custom user model)
and provide Polygon API credentials per request (entered in the form or
preconfigured via `POLYGON_API_KEY`/`POLYGON_API_SECRET` in the environment;
form input takes precedence and is never persisted server-side). The tool
then:

1. Fetches a problem from Polygon (info, statement package, tests,
   reference solution, custom checker info) with a 30-minute Redis cache for
   test cases.
2. Persists the problem and its test cases into PostgreSQL.
3. Uploads test-case files to the configured object storage using the object
   naming convention:
   - Test-case input: `test_cases/{problem_id}/{NN}`
   - Test-case output: `test_cases/{problem_id}/{NN}.a`
   where `{problem_id}` is the database primary key of the problem and `NN`
   is the zero-padded (two-digit) test number, as in the original
   Azure-targeted implementation.

The dashboard drives each operation through its own server-rendered form
("Migrate to Database", "Migrate Test Cases to DB", "Upload Test Cases to
Cloud Storage"), each with a client-side guard that disables the buttons
while a submission is in flight.

## Architecture

- **Framework**: Django 5.2, server-rendered templates (the migration page
  posts form data and re-renders with results; there is no separate JSON
  API).
- **Database**: PostgreSQL (local Docker container by default, host port
  `5433`; remapped from the default `5432` to avoid clashing with any
  natively installed PostgreSQL).
- **Cache**: Redis (local Docker container, port `6379`) caches fetched
  Polygon test cases for 30 minutes.
- **Object storage**: pluggable via `STORAGE_PROVIDER`
  - `minio` (default) — any S3-compatible endpoint (MinIO locally, also
    AWS S3 / Cloudflare R2), via boto3
  - `azure` — Azure Blob Storage using AAD username/password credentials
- **Polygon API client**: hand-rolled HMAC-signed JSON API client
  (`problems/polygon_api.py`) implementing the documented `apiSig` signing
  scheme, with timeouts, throttling, and bounded retries.

## Requirements

- Python 3.11+ (tested on 3.11.9)
- Docker with the Compose plugin (for PostgreSQL, Redis, MinIO)
- `pip` (for installing dependencies into a virtual environment)

## Local Setup

From the repository root:

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirement.txt

# 3. Start PostgreSQL (host port 5433), Redis (6379) and MinIO (9000/9001)
docker compose up -d

# 4. Configure the environment
cp .env.example .env
# edit .env: set SECRET_KEY, DB_*, MinIO credentials, SUPERUSER_* and
# optionally POLYGON_API_KEY / POLYGON_API_SECRET for live API calls

# 5. Apply migrations
cd PolygonMigration
python manage.py migrate

# 6. Create the initial staff superuser (from SUPERUSER_* variables in .env)
python manage.py create_staff_superuser

# 7. Run the development server
python manage.py runserver
```

Open <http://127.0.0.1:8000/> and sign in with the superuser email/password
you configured (the app authenticates by email).

## Configuration

All configuration is read from environment variables via `python-dotenv`
(`settings.py` loads `.env` from `PolygonMigration/` first, then the
repository root). See `.env.example` for the full annotated list:

| Variable | Purpose | Default |
| --- | --- | --- |
| `SECRET_KEY` | Django secret key | insecure dev fallback (set your own) |
| `DEBUG` | Debug mode | `False` |
| `ALLOWED_HOSTS` | Comma-separated allowed hosts | `localhost,127.0.0.1` |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | PostgreSQL connection | `polygon_migration` / `postgres` / `postgres` / `localhost` / `5432` (set `5433` for the Docker stack) |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` / `REDIS_SSL` | Redis connection | `localhost` / `6379` / — / `False` |
| `POLYGON_API_KEY` / `POLYGON_API_SECRET` | Polygon credentials (per-request form input takes precedence) | unset |
| `STORAGE_PROVIDER` | `minio` or `azure` | `minio` |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET_NAME` / `MINIO_SECURE` | MinIO / S3 connection | `localhost:9000` / unset / unset / `testcases` / `False` |
| `AZURE_STORAGE_ACCOUNT_URL` / `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_USERNAME` / `AZURE_PASSWORD` / `AZURE_CONTAINER_NAME` | Azure Blob Storage (AAD username/password app) | unset / `testcases` |
| `SUPERUSER_EMAIL` / `SUPERUSER_PASSWORD` (+ optional `SUPERUSER_USERNAME`, `SUPERUSER_FIRST_NAME`, `SUPERUSER_LAST_NAME`, `SUPERUSER_CONTACT_NUMBER`, `SUPERUSER_COLLEGE`, `SUPERUSER_GRADUATION_YEAR`, `SUPERUSER_GENDER`) | Initial staff superuser for `create_staff_superuser` | unset |
| `CUSTOM_CHECKER_DIR` | Directory for compiling custom checkers | system temp dir |

Polygon credentials entered in the web form are used for that request only;
the `POLYGON_API_KEY`/`POLYGON_API_SECRET` pair in the environment acts as a
server-side default.

## Running

```bash
cd PolygonMigration
python manage.py runserver
```

Then open <http://127.0.0.1:8000/> and sign in. From the staff dashboard you
can:

- **Migrate to Database** – fetch a problem from Polygon, persist it with a
  chosen difficulty and tags, and sync its sample test cases.
- **Migrate Test Cases to DB** – persist the problem's full test set
  (samples + regular tests), replacing any previously migrated rows for that
  problem.
- **Upload Test Cases to Cloud Storage** – push the problem's test cases to
  the configured storage provider (MinIO or Azure), replacing previous files
  for the problem; a custom checker, when present, is compiled and uploaded
  alongside.

## Running Tests

Tests run against a dedicated PostgreSQL test database (the project is
configured to refuse SQLite). Run from the `PolygonMigration/` directory:

```bash
cd PolygonMigration
python manage.py test --noinput
```

Notes:

- The suite covers the Polygon client (signing, error mapping, test
  fetching), the storage providers (MinIO roundtrip is exercised against the
  local container; the shared storage logic is exercised against an in-memory
  provider), the migration services (including edge cases Q1–Q4 in
  `ISSUES.md`), the view contract, and the auth flow.
- Tests that need external services (Redis, MinIO) skip cleanly if the
  service is not reachable, so the suite passes on a machine without Docker
  as well.
- Postgres, Redis, and MinIO must be running for the full suite (the
  containerized `docker compose up -d` from [Local Setup](#local-setup) is
  sufficient).

## Project Structure

```
Polygon-Migration-Tool/
├── docker-compose.yml            # PostgreSQL (5433), Redis (6379), MinIO (9000/9001)
├── .env.example                  # annotated configuration template
├── requirement.txt               # pinned dependencies (Django, boto3, redis, …)
├── IMPLEMENTATION_PLAN.md        # phased plan with acceptance criteria
├── DOCUMENTATION.md              # full architecture & flow documentation
├── ISSUES.md                     # issue log & edge-case reproduction guides
├── docs/                         # original design documents and diagrams
└── PolygonMigration/             # the Django project
    ├── manage.py
    ├── PolygonMigration/         # project: settings.py, urls.py, wsgi.py
    ├── users/                    # custom user model, email auth, staff bootstrap
    ├── problems/                 # core app
    │   ├── polygon_api.py        # Polygon JSON API client (signing, cache, retries)
    │   ├── storage.py            # storage interface + MinIO / Azure providers
    │   ├── services.py           # migration/sync/upload business logic
    │   ├── views.py              # staff-only migration page
    │   ├── models.py             # Problem, SampleTestCase, ProblemTestCase, ProblemTag
    │   ├── templates/            # index (dashboard), problem_list, …
    │   └── tests/                # unit + integration tests
    └── contents/                 # ancillary content app from the starter codebase
```

## Documentation

- [`DOCUMENTATION.md`](DOCUMENTATION.md) – architecture, data flow, and
  per-flow documentation.
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) – phased implementation
  plan with acceptance criteria and status.
- [`ISSUES.md`](ISSUES.md) – known issues and reproduction procedures for
  the reported edge cases (Q1–Q4).
- `docs/` – original design documents and diagrams from the project brief.
