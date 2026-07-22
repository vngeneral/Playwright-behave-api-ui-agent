# Generating test cases from cURL, a screenshot, or plaintext

`AITestGenerator` (`agent/ai/test_generator.py`) turns four kinds of input into a
Gherkin `.feature` file, using the same cloud LLM configured for the rest of the
framework (`AI_PROVIDER` / `AI_API_KEY` / `AI_MODEL` — see `.env.example`):

| Input           | Method                    | Good for                                            |
|-----------------|---------------------------|------------------------------------------------------|
| Live page URL   | `generate()`              | UI scenarios for a page that already exists           |
| cURL command    | `generate_from_curl()`    | API scenarios for an endpoint you can call             |
| UI screenshot   | `generate_from_screenshot()` | UI scenarios that name real form/button labels     |
| Plaintext       | `generate_from_text()`    | Anything not yet built, or UI + API mixed              |

This guide covers cURL, screenshots, and plaintext — those don't require a
live target to point Playwright at.

Every mode produces a **draft**. Nothing is ever run or committed automatically —
review the output before it goes into `e2e/features/` (see [Reviewing output](#reviewing-output-before-you-commit-it)).

### CLI inputs are files, not inline strings

`--url`, `--curl`, and `--text` each take a path to a plain `.txt` file
holding the actual value, instead of the value typed inline in the shell
command — so a cURL command or a long requirement never needs hand-escaping
into `bash`. `--curl` accepts the command exactly as pasted, single- or
multi-line with trailing `\` continuations; `normalize_curl_command()`
collapses it to one line before `parse_curl()` runs. `--screenshot` is
unchanged — it was already a filepath (an image).

Programmatic callers (`generate()`, `generate_from_curl()`,
`generate_from_text()`) are unaffected — they still take the value directly
as a string; only the CLI reads it from a file first.

## cURL workflow

Use this when you have (or can construct) a working request against the API —
copied from Postman, browser devtools' "Copy as cURL", or written by hand.

### 1. Save a cURL command to a file

Example, testing the Vehicle API's register endpoint — save this exactly as
pasted (multi-line, with the `\` continuations) into `curl.txt`:

```bash
cat > curl.txt <<'EOF'
curl -X POST https://stage.bl4b.api.sample.com/bl4b/v1/vehicle/register \
  -H "Content-Type: application/json" \
  -H "x-api-key: $VEHICLE_API_KEY" \
  -d '{"transactionId":"...","partnerCode":"HBL4BP-006","vinList":[{"vin":"KMUHCESC7RU179347"}]}'
EOF
```

Save your **real** command with your **real** key — `parse_curl()` redacts it
before anything leaves your machine (see [Secret handling](#secret-handling)).

### 2. Generate the feature file

```bash
python -m agent.ai.test_generator \
  --curl curl.txt \
  --feature e2e/features/ai_generated_vehicle_register.feature \
  --tags api regression
```

The generator parses method/URL/headers/body, then asks the LLM for Gherkin in
this project's existing API style (see `e2e/features/vehicle_api.feature`):
a `Background:` step that initialises the client, and scenarios asserting on
status code, JSON validity, and any transaction/reference id — plus a
validation-error case and, if credentials were present, an auth-failure case.

### Secret handling

`parse_curl()` replaces the value of any of these (case-insensitive) with
`<redacted>` before it's used in a prompt, a log line, or the saved feature
file: `Authorization`, `x-api-key`, `api-key`, `x-auth-token`, `Cookie`, and
anything passed via `-u`/`--user` or `-b`/`--cookie`. The LLM only ever sees
the placeholder, never your real key.

## UI screenshot workflow

Use this when you have a screenshot of a page and want UI scenarios that name
the actual button labels, field names, and form title in the
`Given`/`When`/`Then` steps instead of generic placeholders — no cURL command
needed.

Requires a vision-capable model: `claude-3-5-sonnet-20241022` (Anthropic) or
`gpt-4o` (OpenAI). The default fast/cheap model (`claude-3-5-haiku-20241022`)
does not accept images — set `AI_MODEL` accordingly.

### 1. Capture a screenshot of the page

Any local PNG/JPG/GIF/WEBP file works — e.g. a Playwright screenshot taken
during a manual exploration session, or one saved by `context.page.screenshot(...)`.

### 2. Generate the feature file

```bash
python -m agent.ai.test_generator \
  --screenshot reports/screenshots/register-form.png \
  --feature e2e/features/ai_generated_vehicle_register.feature \
  --tags smoke regression
```

### Programmatic use

```python
from agent.ai.test_generator import AITestGenerator

gen = AITestGenerator()
gherkin = gen.generate_from_screenshot(
    screenshot_path="reports/screenshots/register-form.png",
    tags=["smoke"],
)
gen.save(gherkin, "e2e/features/ai_generated_vehicle_register.feature")
```

## Plaintext workflow

Use this for anything you can describe before it's buildable — a new endpoint
that doesn't exist yet, a UI flow you can't easily get a URL for, or a
requirement handed to you as a user story.

### 1. Write the requirement to a file

A good plaintext requirement states, in a sentence or two:
- **Who** is acting (the actor/role)
- **What** they do (the action)
- **What should happen** (the expected outcome)
- Any **edge cases** worth calling out explicitly (the LLM will infer some on
  its own, but naming the ones you care about gets better coverage)

Save it to `requirement.txt`:

```text
As a platform partner, I can deregister a batch of VINs by partner code and
receive an HTTP 200 with a transaction reference for each VIN. Deregistering
an already-deregistered VIN should return a 409, and omitting the API key
should return a 401.
```

### 2. Generate the feature file

```bash
python -m agent.ai.test_generator \
  --text requirement.txt \
  --feature e2e/features/ai_generated_deregister_batch.feature \
  --tags api regression
```

## Reviewing output before you commit it

Generated `.feature` files are a starting point, not a finished PR. Before
committing:

1. **Tag it correctly.** Every scenario needs at least one of `@smoke`,
   `@regression`, `@api`, `@performance` (see CLAUDE.md → "Adding a new
   feature file"). The generator applies whatever you passed via `--tags`,
   but check the LLM didn't drop or duplicate them.
2. **Only add `@testrail_C<id>` after the case exists in TestRail.** Don't
   invent case numbers — after reviewing the Gherkin, let the case-sync tool
   create the cases and tag the file for you (see
   [Linking generated scenarios to TestRail](#linking-generated-scenarios-to-testrail)).
3. **Check step definitions exist.** If the LLM phrased a step that doesn't
   match anything in `e2e/steps/`, either edit the Gherkin to reuse an
   existing step or write the new step definition (thin — see CLAUDE.md →
   "Step files are thin"). `behave --dry-run e2e/features/your_file.feature`
   (run from `e2e/`) will report undefined steps without executing anything.
4. **Run it for real** before merging:
   ```bash
   cd e2e
   python run_tests.py --tags @your_new_tag
   ```
5. **Save it under `e2e/features/`.** Never the repo root — that's the
   legacy pre-restructure location and no longer exists.

## Linking generated scenarios to TestRail

Generated feature files start with no `@testrail_C<id>` tags — the cases don't
exist in TestRail yet, and the `after_scenario` hook only queues results for
tagged scenarios. `agent/testrail/case_sync.py` closes that loop: it creates
one TestRail case per untagged scenario (title = scenario name, steps = the
Gherkin body) and writes the real `@testrail_C<id>` tags back into the file.
From then on, results flow through the normal pending-queue → `!testrail
preview` → `!testrail push` review workflow.

Like result pushing, case creation is **never automatic** — run it yourself,
**after** you've reviewed and fixed the generated Gherkin (creating cases from
unreviewed scenarios puts junk in TestRail):

```bash
# Preview what would be created — no TestRail calls, no file edits
python -m agent.testrail.case_sync \
  --feature e2e/features/ai_generated_vehicle_register.feature \
  --section-id 42 --dry-run

# Create the cases and tag the file
python -m agent.testrail.case_sync \
  --feature e2e/features/ai_generated_vehicle_register.feature \
  --section-id 42
```

Requires `TESTRAIL_URL`, `TESTRAIL_USER`, `TESTRAIL_API_KEY`; the section can
also come from `TESTRAIL_SECTION_ID` instead of `--section-id`. Already-tagged
scenarios are skipped, so re-running the sync never creates duplicates — it's
safe to run again after adding new scenarios to the file. The steps field
adapts to the project's case template: plain-text `custom_steps` first, and if
the template rejects it (a "Test Case (Steps)" project), the whole Gherkin
body — including any Examples table — is stored as the first step of the case
via `custom_steps_separated`. Only if both formats are rejected is the case
created with its title alone.

The generator CLI can chain the sync in one step via
`--testrail-section 42` — only use that when you're re-generating a file you
already know is good; otherwise review first, sync second.

## Programmatic use

Each method is also callable directly, e.g. from a script or a future
integration:

```python
from agent.ai.test_generator import AITestGenerator

gen = AITestGenerator()
gherkin = gen.generate_from_curl(curl_command, tags=["api", "smoke"])
gen.save(gherkin, "e2e/features/ai_generated_x.feature")
```

## Troubleshooting

- **`FileNotFoundError: Input file not found`** — `--url`/`--curl`/`--text`
  each take a path to a `.txt` file, not the value inline; check the path
  exists relative to where you ran the command.
- **Empty output / "AI disabled" warning** — `AI_PROVIDER=stub` is active
  (default when `AI_API_KEY` isn't set). Set a real `AI_API_KEY` to get
  actual Gherkin back.
- **`ValueError: Could not find a URL in cURL command`** — `parse_curl()`
  needs either a bare URL token or a `--url` flag; check your command has one.
- **Generated step text doesn't match any step definition** — this is
  expected sometimes; either adjust the wording to match an existing step
  or add a new one (see step 3 above).
