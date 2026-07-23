# TestRail integration — usage guideline

The single source of truth for how the framework talks to TestRail: which
modules to use, which methods exist (and which were removed), and the correct
end-to-end flow. For generating the feature files themselves, see
[ai-test-generation-guide.md](ai-test-generation-guide.md).

## The flow at a glance

```
AITestGenerator            (writes .feature — no TestRail tags yet)
        │
        ▼  human reviews the Gherkin
agent.testrail.case_sync   (creates cases, writes @testrail_C<id> tags back)
        │
        ▼  tests run (e2e/run_tests.py)
e2e/environment.py         (after_scenario queues one result per @testrail_C tag)
        │
        ▼  reports/testrail/pending_results.json
!testrail status / preview (human reviews queued results via Teams/WhatsApp)
        │
        ▼
!testrail push             (POST to TestRail — the ONLY step that writes results)
```

Two invariants hold everywhere — do not code around them:

1. **Nothing reaches TestRail without a human action.** Case creation requires
   running `case_sync`; result submission requires `!testrail push`.
2. **Case IDs are never invented.** A `@testrail_C<id>` tag exists only because
   TestRail issued that ID (via `case_sync` or a pre-existing case).

## Module-by-module: what to call

### `agent.testrail.client` — `TestRailClient`

| Member | Use it for |
|---|---|
| `TestRailClient.from_env()` | Construct from `TESTRAIL_URL` / `TESTRAIL_USER` / `TESTRAIL_API_KEY`; raises `TestRailConfigError` if any is missing |
| `add_results_for_cases(run_id, results)` | Bulk-submit results — called by `!testrail push` only |
| `add_case(section_id, title, custom_steps=None)` | Create one case — called by `case_sync` only |
| `get_cases(project_id, section_id=None, suite_id=None)` | List existing cases (paginated transparently) — called by `case_sync`'s duplicate-case check before every `add_case` |
| `default_run_id()` / `default_section_id()` / `default_project_id()` / `default_suite_id()` | Read `TESTRAIL_RUN_ID` / `TESTRAIL_SECTION_ID` / `TESTRAIL_PROJECT_ID` / `TESTRAIL_SUITE_ID` env vars as ints |

`add_case` adapts to the project's case template automatically: it sends
plain-text `custom_steps` first; on HTTP 400 it retries with
`custom_steps_separated` (the whole Gherkin body as the case's first step);
if both are rejected it creates the case title-only. Any non-400 error raises
immediately. Callers never handle template differences themselves.

Use the client as a context manager (`with client: ...`) so the HTTP session
is released.

### `agent.testrail.case_sync` — linking scenarios to cases

```bash
# Always preview first
python -m agent.testrail.case_sync --feature e2e/features/x.feature --section-id 42 --project-id 7 --dry-run
python -m agent.testrail.case_sync --feature e2e/features/x.feature --section-id 42 --project-id 7

# Or sync every .feature file in a folder in one command
python -m agent.testrail.case_sync --feature e2e/features/ai_generated --section-id 42 --project-id 7
```

Programmatic:
- `sync_feature_file(path, section_id, project_id=None, suite_id=None, dry_run=False, client=None)`
  → `SyncReport` (`created` / `linked` / `skipped` / `failed`, `ok` property).
- `sync_feature_dir(dir_path, section_id, project_id=None, suite_id=None, dry_run=False, client=None)`
  → `list[SyncReport]`, one per `*.feature` file found directly in the folder.
- `find_scenarios(text)` and `insert_case_tags(text, {line_idx: case_id})` are
  pure helpers — no I/O, safe to reuse for tooling.

Rules:
- Run it **after** reviewing the generated Gherkin, never before.
- Re-running is idempotent two ways: scenarios already tagged
  `@testrail_C<id>` are skipped, and untagged scenarios whose title matches
  an existing case in the section are **linked** to it (`get_cases`) instead
  of creating a duplicate — TestRail does not enforce unique case titles, so
  this check is what keeps a re-synced/renamed file, or a folder sync where
  two files share a title, from uploading the same scenario twice.
- On partial failure, successes are still tagged; fix the failures and re-run.
- The feature file is only ever updated with an atomic write (temp file +
  rename) — never a partial/corrupt file on interrupt.
- Folder mode shares one `TestRailClient`/HTTP session and one duplicate-case
  check across every file in filename order; one bad file is recorded on its
  own `SyncReport` and does not stop the rest of the batch.

### `agent.testrail.result_mapper` — scenario → result

One entry point: `from_behave_scenario(scenario, case_id)` → `TestRailResult`.
Case IDs come from `extract_case_ids(scenario)` (reads `@testrail_C<id>` tags).
Both are already wired into `e2e/environment.py::after_scenario` — you should
not need to call them anywhere else.

The comment on a failed result always carries the failure cause:
- **Failed steps** are listed with their `error_message` (assertion text or
  exception), each capped at 2 000 chars so Playwright DOM dumps don't drown
  the signal.
- **Undefined steps** are labelled `undefined step — no matching step
  definition found`.
- **Hook errors** (e.g. `before_scenario` crash — every step left "untested")
  are included from `scenario.error_message`, so a failed result never arrives
  in TestRail without its reason.
- Skipped and untested steps are **never** reported as failures.

### `agent.testrail.pending_store` — the review queue

Use `get_default_store()` for the shared singleton
(`reports/testrail/pending_results.json`, thread-safe). Direct `PendingStore(path)`
construction is for tests only.

| Member | Use it for |
|---|---|
| `add(result)` | Queue a `TestRailResult` (called by `after_scenario`) |
| `get_pending()` | Entries awaiting review (`status == "pending_review"`) |
| `get_all()` / `get_status()` | Full contents / summary for `!testrail status` |
| `mark_pushed(case_ids)` | Flag entries as pushed — called after a successful push |
| `clear()` | Wipe the queue — `!testrail discard` |

### `agent.integrations.testrail_command` — chat commands

`handle_testrail_command(text)` is the only entry point; it routes:

```
!testrail status                 pending/pushed counts
!testrail preview                full detail of each pending result
!testrail push [--run-id N] [--case N]
!testrail discard
```

## Environment variables

| Variable | Needed for | When |
|---|---|---|
| `TESTRAIL_URL`, `TESTRAIL_USER`, `TESTRAIL_API_KEY` | client auth | case sync + push time only |
| `TESTRAIL_RUN_ID` | default run for push | push time (or `--run-id`) |
| `TESTRAIL_SECTION_ID` | default section for case sync | sync time (or `--section-id`) |
| `TESTRAIL_PROJECT_ID` | duplicate-case check (`get_cases`) before every `add_case` | sync time, required unless `--dry-run` (or `--project-id`) |
| `TESTRAIL_SUITE_ID` | narrows the duplicate-case check — only for multi-suite projects | sync time, optional (or `--suite-id`) |

Never hardcode any of these; never at test-run time — running tests requires
no TestRail access at all.

## Removed APIs (do not re-add)

Removed as unused in the 2026-07 cleanup — the replacements cover every need:

| Removed | Use instead |
|---|---|
| `TestRailClient.get_run()` | nothing needed it; push validates via the POST response |
| `PendingStore.mark_all_pushed()` / `has_pending()` | `mark_pushed(case_ids)` / `get_pending()` |
| `TestRailResult.to_api_dict()` | push builds its payload from store entries |
| `result_mapper.from_step_results()`, `StepResult`, `build_step_comment()`, `find_failed_steps()`, `is_passing_status_code()` | the JMeter/Groovy migration path — fully replaced by `from_behave_scenario` |
| `AISelectorHealer.stop_model()` | Ollama leftover; cloud clients need no teardown |
