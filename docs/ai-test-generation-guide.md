# Generating test cases from cURL and plaintext

`AITestGenerator` (`agent/ai/test_generator.py`) turns four kinds of input into a
Gherkin `.feature` file, using the same cloud LLM configured for the rest of the
framework (`AI_PROVIDER` / `AI_API_KEY` / `AI_MODEL` — see `.env.example`):

| Input                       | Method                               | Good for                                          |
|------------------------------|---------------------------------------|----------------------------------------------------|
| Live page URL                | `generate()`                         | UI scenarios for a page that already exists         |
| cURL command                 | `generate_from_curl()`                | API scenarios for an endpoint you can call          |
| cURL command + UI screenshot | `generate_from_curl_and_screenshot()` | UI+API scenarios that name real form/button labels  |
| Plaintext                    | `generate_from_text()`                | Anything not yet built, or UI + API mixed           |

This guide covers cURL (alone and with a screenshot) and plaintext — those don't
require a live target to point Playwright at.

Every mode produces a **draft**. Nothing is ever run or committed automatically —
review the output before it goes into `e2e/features/` (see [Reviewing output](#reviewing-output-before-you-commit-it)).

## cURL workflow

Use this when you have (or can construct) a working request against the API —
copied from Postman, browser devtools' "Copy as cURL", or written by hand.

### 1. Get a cURL command

Example, testing the Vehicle API's register endpoint:

```bash
curl -X POST https://stage.bl4b.api.sample.com/bl4b/v1/vehicle/register \
  -H "Content-Type: application/json" \
  -H "x-api-key: $VEHICLE_API_KEY" \
  -d '{"transactionId":"...","partnerCode":"HBL4BP-006","vinList":[{"vin":"KMUHCESC7RU179347"}]}'
```

Paste your **real** command with your **real** key — `parse_curl()` redacts it
before anything leaves your machine (see [Secret handling](#secret-handling)).

### 2. Generate the feature file

```bash
python -m agent.ai.test_generator \
  --curl 'curl -X POST https://stage.bl4b.api.sample.com/bl4b/v1/vehicle/register -H "x-api-key: secret123" -d "{\"vin\":\"KMUHCESC7RU179347\"}"' \
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

## cURL + UI screenshot workflow

Use this when the API call is triggered from a page you already have — a
screenshot lets the LLM name the actual button labels, field names, and form
title in the `Given`/`When` steps instead of generic placeholders, while
`Then` steps still assert on the API response exactly like the cURL-only
workflow.

Requires a vision-capable model: `claude-3-5-sonnet-20241022` (Anthropic) or
`gpt-4o` (OpenAI). The default fast/cheap model (`claude-3-5-haiku-20241022`)
does not accept images — set `AI_MODEL` accordingly.

### 1. Capture a screenshot of the triggering page

Any local PNG/JPG/GIF/WEBP file works — e.g. a Playwright screenshot taken
during a manual exploration session, or one saved by `context.page.screenshot(...)`.

### 2. Generate the feature file

```bash
python -m agent.ai.test_generator \
  --curl 'curl -X POST https://stage.bl4b.api.sample.com/bl4b/v1/vehicle/register -H "x-api-key: secret123" -d "{\"vin\":\"KMUHCESC7RU179347\"}"' \
  --screenshot reports/screenshots/register-form.png \
  --feature e2e/features/ai_generated_vehicle_register.feature \
  --tags api regression
```

`--screenshot` requires `--curl` — it has no effect paired with `--url` or
`--text`. Secret redaction rules are identical to the cURL-only workflow (see
[Secret handling](#secret-handling) above); only the parsed request text is
sent to the LLM's text channel, the screenshot is sent as an image attachment.

### Programmatic use

```python
from agent.ai.test_generator import AITestGenerator

gen = AITestGenerator()
gherkin = gen.generate_from_curl_and_screenshot(
    curl_command=curl_command,
    screenshot_path="reports/screenshots/register-form.png",
    tags=["api", "smoke"],
)
gen.save(gherkin, "e2e/features/ai_generated_vehicle_register.feature")
```

## Plaintext workflow

Use this for anything you can describe before it's buildable — a new endpoint
that doesn't exist yet, a UI flow you can't easily get a URL for, or a
requirement handed to you as a user story.

### 1. Write the requirement

A good plaintext requirement states, in a sentence or two:
- **Who** is acting (the actor/role)
- **What** they do (the action)
- **What should happen** (the expected outcome)
- Any **edge cases** worth calling out explicitly (the LLM will infer some on
  its own, but naming the ones you care about gets better coverage)

```text
As a platform partner, I can deregister a batch of VINs by partner code and
receive an HTTP 200 with a transaction reference for each VIN. Deregistering
an already-deregistered VIN should return a 409, and omitting the API key
should return a 401.
```

### 2. Generate the feature file

```bash
python -m agent.ai.test_generator \
  --text "As a platform partner, I can deregister a batch of VINs by partner code and receive an HTTP 200 with a transaction reference for each VIN. Deregistering an already-deregistered VIN should return a 409, and omitting the API key should return a 401." \
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
   invent case numbers.
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

## Programmatic use

Both methods are also callable directly, e.g. from a script or a future
integration:

```python
from agent.ai.test_generator import AITestGenerator

gen = AITestGenerator()
gherkin = gen.generate_from_curl(curl_command, tags=["api", "smoke"])
gen.save(gherkin, "e2e/features/ai_generated_x.feature")
```

## Troubleshooting

- **Empty output / "AI disabled" warning** — `AI_PROVIDER=stub` is active
  (default when `AI_API_KEY` isn't set). Set a real `AI_API_KEY` to get
  actual Gherkin back.
- **`ValueError: Could not find a URL in cURL command`** — `parse_curl()`
  needs either a bare URL token or a `--url` flag; check your command has one.
- **Generated step text doesn't match any step definition** — this is
  expected sometimes; either adjust the wording to match an existing step
  or add a new one (see step 3 above).
