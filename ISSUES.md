# Issues Analysis

> **Instructions**: Document the three highest-priority product issues and the three highest-priority code issues. Rank the issues within each category from highest to lowest priority.
> Replace the example entries with your actual findings and explain why each issue deserves its priority. Complete all four questions in the Edge Case Analysis section.
> *(This file is `ISSUES.md`, renamed from `ISSUES_TEMPLATE.md` as required.)*

## Summary

| Type | Critical | High | Medium | Low | Total |
|------|----------|------|--------|-----|-------|
| Product Issues | 0 | 0 | 0 | 0 | 0 |
| Code Issues | 0 | 0 | 0 | 0 | 0 |

---

## Product Issues

> Product issues are user-facing problems: broken functionality, missing validation, poor UX, data integrity risks visible to users.

<!-- TO BE COMPLETED BY THE AUTHOR. Three product issues, ranked highest → lowest priority, using the template entry format:
### [P1] <title>
**Severity**: …
**Location**: `file` (symbol)
**Description**: …
**Impact**: …
**Suggested Fix**: …
-->

---

## Code Issues

> Code issues are technical problems: bugs, security vulnerabilities, performance problems, code quality concerns, architectural issues.

<!-- TO BE COMPLETED BY THE AUTHOR. Three code issues, ranked highest → lowest priority, using the same entry format as above. -->

---

## Edge Case Analysis

> The sections below provide a **reproduction procedure** for each reported edge case so the behaviour can be re-verified on demand. Statements are tagged **[observed]** (verified by running code during implementation, with the exact test named) or **[inferred]** (read from source without executing that scenario). The substantive analysis (what it means, severity, recommended resolution) is to be written by the author.

### Q1

> A Polygon problem has 0 sample test cases but 15 regular test cases. What happens when you migrate this problem?

**Reproduction procedure**

1. Start the stack: `docker compose up -d` (PostgreSQL on 5433, Redis, MinIO).
2. From `PolygonMigration/`, run the deterministic test that encodes this
   scenario (15 non-sample cases, 0 samples):
   ```bash
   python manage.py test problems.tests.test_services.SampleSyncTests -v 2
   ```
3. Equivalent shell reproduction using the same fixtures:
   ```bash
   cd PolygonMigration
   python manage.py shell
   >>> from problems.tests.test_services import problem_fields
   >>> from problems.services import migrate_problem_to_db, sync_sample_test_cases, sync_problem_test_cases
   >>> problem, _ = migrate_problem_to_db(polygon_id="10001", difficulty="easy", tag_names=[], problem_data=problem_fields())
   >>> cases = [{"index": i, "is_sample": False, "input": f"{i}\n", "output": f"{i}\n"} for i in range(1, 16)]
   >>> sync_sample_test_cases(problem, cases)
   >>> from problems.models import SampleTestCase, ProblemTestCase
   >>> SampleTestCase.objects.filter(problem=problem).count()
   >>> sync_problem_test_cases(problem, cases)
   >>> ProblemTestCase.objects.filter(problem=problem).count()
   >>> list(ProblemTestCase.objects.filter(problem=problem).values_list("is_sample", flat=True))
   ```

**Observations recorded during implementation testing**

- **[observed]** `SampleSyncTests.test_zero_samples_creates_no_rows`: syncing
  15 non-sample cases through `sync_sample_test_cases`
  (`problems/services.py`) stores **0 rows** and returns 0. The function
  keeps only cases with `is_sample` truthy **and** non-empty input/output,
  then replaces the problem's `SampleTestCase` rows inside one transaction —
  so a problem with no samples ends up with an empty sample set, with no
  error.
- **[observed]** `TestCasesSyncTests`: the same 15 cases synced through
  `sync_problem_test_cases` persist as **15 `ProblemTestCase` rows** with
  `is_sample=False`.
- **[inferred]** The migration page's success message reports the number of
  sample cases stored (`views._handle_problem_migration` formats
  `"{samples} sample test cases"`), so the user is told "0 sample test
  cases" rather than being warned that the problem has no samples.

**To be analysed by the author**: what the sample-only path leaves behind
(zero rows), whether the UI communicates this clearly, and the severity of
migrating such a problem via each button.

---

### Q2

> A problem is migrated with 20 test cases. Later, the problem setter removes 8 test cases on Polygon (now 12 remain). The problem is re-migrated. What happens?

**Reproduction procedure**

1. Run the deterministic test that encodes this scenario (20 → 12):
   ```bash
   cd PolygonMigration
   python manage.py test problems.tests.test_services.TestCasesSyncTests -v 2
   ```
2. Equivalent shell reproduction:
   ```bash
   python manage.py shell
   >>> from problems.tests.test_services import problem_fields
   >>> from problems.services import migrate_problem_to_db, sync_problem_test_cases
   >>> problem, _ = migrate_problem_to_db(polygon_id="10001", difficulty="easy", tag_names=[], problem_data=problem_fields())
   >>> original = [{"index": i, "is_sample": False, "input": f"in{i}\n", "output": f"out{i}\n"} for i in range(1, 21)]
   >>> sync_problem_test_cases(problem, original)
   >>> from problems.models import ProblemTestCase
   >>> ProblemTestCase.objects.filter(problem=problem).count()          # 20
   >>> reduced = [{"index": i, "is_sample": False, "input": f"new{i}\n", "output": f"newout{i}\n"} for i in range(1, 13)]
   >>> sync_problem_test_cases(problem, reduced)
   >>> ProblemTestCase.objects.filter(problem=problem).count()          # 12
   >>> list(ProblemTestCase.objects.filter(problem=problem).order_by("order").values_list("order", "input")[:3])
   ```

**Observations recorded during implementation testing**

- **[observed]** `TestCasesSyncTests.test_re_migration_with_fewer_cases_removes_stale_rows`:
  `sync_problem_test_cases` (`problems/services.py`) **replaces** the whole
  set — it deletes every existing `ProblemTestCase` row for the problem and
  bulk-creates the new ones inside one transaction. After re-migration the
  database holds exactly **12 rows** (tests 13–20 are gone), and surviving
  rows carry the freshly fetched content (test 1 input became `new1`).
- **[observed]** `TestCasesSyncTests.test_repeated_migration_is_idempotent`:
  re-running the same sync twice leaves the same number of rows (no
  duplicates), because of the delete-then-insert replacement.
- **[observed]** Sample re-migration behaves the same way:
  `SampleSyncTests.test_samples_synced_and_replaced` shows
  `sync_sample_test_cases` deletes prior `SampleTestCase` rows before
  inserting the current samples.
- **[inferred]** Cloud storage mirrors this replacement semantics:
  `BaseTestCaseStorage.replace_problem_files` (`problems/storage.py`)
  deletes every object under `test_cases/{problem_id}/` before uploading,
  so stale files from a previous upload of the removed tests are deleted on
  the next upload — but **not** by the database re-migration alone; files
  for tests 13–20 remain in storage until an upload is run.

**To be analysed by the author**: whether whole-set replacement is the
correct default (vs. incremental upsert with explicit pruning), the window
where database and storage disagree, and severity.

---

### Q3

> Two different Polygon problems have the exact same title: "Two Sum". You migrate the first one successfully. Then you try to migrate the second one. What happens?

**Reproduction procedure**

1. Run the deterministic test that encodes this scenario:
   ```bash
   cd PolygonMigration
   python manage.py test problems.tests.test_services.ProblemMigrationTests -v 2
   ```
2. Equivalent shell reproduction:
   ```bash
   python manage.py shell
   >>> from problems.tests.test_services import problem_fields
   >>> from problems.services import migrate_problem_to_db
   >>> first, created1 = migrate_problem_to_db(polygon_id="10001", difficulty="easy", tag_names=[], problem_data=problem_fields(title="Two Sum"))
   >>> second, created2 = migrate_problem_to_db(polygon_id="10002", difficulty="medium", tag_names=[], problem_data=problem_fields(title="Two Sum"))
   >>> first.slug, second.slug
   >>> first.pk != second.pk, Problem.objects.count()
   ```

**Observations recorded during implementation testing**

- **[observed]** `ProblemMigrationTests.test_same_title_different_polygon_ids_get_unique_slugs`:
  migrating two different Polygon problems both titled "Two Sum" (ids
  `10001` and `10002`) creates **two distinct `Problem` rows** with
  **distinct slugs**; no `IntegrityError` is raised.
- **[inferred]** The mechanism is `_unique_slug(title, polygon_id)` in
  `problems/services.py`: the base slug is `slugify(title)`; on collision
  the Polygon id is appended (`two-sum-10002`); if that collides too, a
  numeric counter is used (`two-sum-2`, `two-sum-3`, …).
- **[inferred]** `Problem.polygon_id` and `Problem.slug` are both unique
  constraints (`problems/models.py`), and `migrate_problem_to_db` keys on
  `polygon_id`: migrating the *same* problem again updates the existing row
  in place (`created=False`) and reuses its original slug rather than
  generating a new one.
- **[inferred]** Because the slug keeps the first problem's clean form and
  only later arrivals get the suffix, the URL/display slugs are asymmetric:
  the first "Two Sum" is `two-sum`, the second is `two-sum-10002`.

**To be analysed by the author**: residual risks (e.g. display/URL
ambiguity, downstream consumers keying on slug), and whether the collision
handling strategy is the right one.

---

### Q4

> When test cases are saved to the database via "Migrate Test Cases to DB", some data is intentionally discarded. What data is lost? Why might this cause problems?

**Reproduction procedure**

1. Run the tests that pin down what is kept and what is dropped:
   ```bash
   cd PolygonMigration
   python manage.py test problems.tests.test_services.TestCasesSyncTests -v 2
   ```
2. Field-by-field comparison — what Polygon returns vs. what is persisted:
   - `PolygonAPI.get_all_test_cases` (`problems/polygon_api.py`) fetches the
     testset via `problem.tests` and, per test, keeps only five keys:
     `index`, `is_sample`, `description`, `input`, `output` (input/answer
     via `problem.testInput` / `problem.testAnswer`). Any other field of the
     raw `problem.tests` record (test tags beyond the sample flag, per-test
     metadata) is dropped **at fetch time**, before the database is involved.
   - `sync_problem_test_cases` (`problems/services.py`) then persists per
     test: `is_sample`, `input`, `output`, `description`, `order` (positional).
     It applies `.rstrip()` to input and output, and skips tests whose input
     or output is empty after stripping.
   - Compare directly in a shell: fetch or build a case dict and inspect the
     `ProblemTestCase` row afterwards (see Q2's shell snippet) — note the
     trailing-whitespace difference and that `order` is the position in the
     fetched list, not Polygon's `index`.

**Observations recorded during implementation testing**

- **[observed]** `TestCasesSyncTests.test_cases_with_empty_input_or_output_are_skipped`:
  of three cases (one valid, one empty input, one empty output) only **one**
  row is stored; the returned stats are `{"total": 3, "saved": 1,
  "skipped": 2}`. The skipped tests are silently absent from the database
  (the view message reports the count).
- **[observed]** `TestCasesSyncTests.test_full_content_is_stored_without_truncation`:
  input and `description` well beyond 260 characters are stored in full.
  **[historical, observed in the pre-implementation audit of the baseline
  code]** the original implementation truncated `input`/`output` to **260
  characters** and did not persist `description` at all, silently losing
  the bulk of large tests; this truncation was removed during
  implementation. Databases migrated under the old code still contain
  truncated rows and should be re-migrated.
- **[inferred]** Additional data discarded on the way to the database:
  - trailing whitespace of input/output (`.rstrip()` in
    `sync_problem_test_cases`; the storage upload path does **not** strip,
    so DB and storage content can differ in trailing newlines);
  - Polygon's own test `index` is replaced by list position (`order`), so
    non-contiguous or lettered test indexes are not preserved;
  - per-test metadata returned by `problem.tests` beyond the five keys kept
    by `get_all_test_cases` (see procedure step 2) is never fetched into the
    application at all.

**To be analysed by the author**: which discarded fields matter for
downstream use (e.g. running/checking submissions), the operational impact
of the truncation→full-content change on existing databases, and severity.

---

## Severity Guidelines

Use these definitions when assigning severity:

| Severity | Definition | Examples |
|----------|------------|----------|
| **Critical** | System broken, security vulnerability, data loss | SQL injection, authentication bypass, data corruption |
| **High** | Major functionality broken, significant data integrity risk | Feature doesn't work, orphaned records, race conditions |
| **Medium** | Feature partially broken, poor UX, code maintainability | Missing validation, confusing errors, code duplication |
| **Low** | Minor issues, cosmetic, best practice violations | Unused imports, inconsistent formatting, missing logs |

---

## Notes

<!-- TO BE COMPLETED BY THE AUTHOR. Additional observations, patterns, or architectural concerns that don't fit into specific issues above. -->
