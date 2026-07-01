# CLAUDE.md — Playwright Behave Allure AI-Driven Framework

This file tells OpenCode.ai (and Claude) everything it needs to work effectively on this codebase without re-reading every file on each session start.

---

## What this project is

An end-to-end test automation framework combining:

- **Playwright** — browser automation
- **Behave** — BDD (Gherkin feature files → Python step definitions)
- **Allure** — HTML test reports with attachments
- **Cloud LLM (Anthropic / OpenAI)** — self-healing selectors + AI test generation
- **Flask webhook server** — receive `!run` / `!testrail` commands from Teams or WhatsApp
- **TestRail integration** — queue results locally, push after human review

---

## Repository layout — two-layer architecture

The framework is split into two layers so the AI/integration engine can be maintained separately from the test scripts QA teams run day-to-day.

```
agent/       ← internal team only (AI, TestRail, integrations, monitoring)
e2e/          ← QA team: features, steps, pages, plugins, utils/api, utils/browser
utils/        ← SHARED: logger.py, misc.py (both layers import from here)
helpers/      ← SHARED: framework_constants.py
tests/        ← unit tests for agent/ (pytest)
```

**QA team** receives a copy of `e2e/` only — they run UI and API tests without needing the agent.  
**Internal team** pushes changes to `agent/` to the shared repository.

When `agent/` is absent the `e2e/environment.py` gracefully degrades:
AI healing, TestRail queuing, and notifications become no-ops; tests still run.

---

## How to run tests

```bash
# From the e2e/ directory (recommended for QA team)
cd e2e
python run_tests.py
python run_tests.py --tags @smoke --browser firefox --env staging --headless
python run_tests.py --priority     # smoke → regression → api → performance
python run_tests.py --parallel --tags @api

# From repo root (both layers present)
python e2e/run_tests.py --tags @smoke
```

Generate the Allure report after a run:
```bash
allure generate allure-results -o allure-report --clean
allure open allure-report
```

---

## How to run unit tests

```bash
# All unit tests
python -m pytest tests/ -v

# Specific module
python -m pytest tests/test_testrail.py -v
python -m pytest tests/test_vehicle_api_client.py -v
```

Current test counts: 47 (vehicle API) + 71 (TestRail) + others = ~150+ total, all passing.

---

## Project structure

```
├── agent/                    # ← INTERNAL TEAM ONLY — AI/integrations layer
│   ├── __init__.py
│   ├── ai/
│   │   ├── llm_client.py      # Unified LLM abstraction (Anthropic / OpenAI / Stub)
│   │   ├── selector_healer.py # Self-healing selectors via LLMClient
│   │   ├── test_generator.py  # Generate .feature files from live pages
│   │   └── multi_agent.py     # 4-agent pipeline: Planner→Generator→Executor→Validator
│   ├── integrations/
│   │   ├── command_parser.py  # Parse !run / !testrail / !status / !help
│   │   ├── testrail_command.py# Handle !testrail status/preview/push/discard
│   │   ├── webhook_server.py  # Flask: /teams/webhook, /whatsapp/webhook, /health
│   │   ├── teams.py           # TeamsClient (Adaptive Card via Power Automate)
│   │   ├── whatsapp.py        # WhatsAppClient (Meta Cloud API or Twilio)
│   │   └── notifier.py        # UnifiedNotifier (Slack + Teams + WhatsApp)
│   ├── monitoring/
│   │   ├── metrics.py         # MetricsCollector — JSON run summary in reports/metrics/
│   │   └── alerts.py          # AlertManager
│   └── testrail/
│       ├── client.py          # TestRailClient (add_results_for_cases, get_run)
│       ├── result_mapper.py   # Groovy→Python port: pass/fail logic, Behave→result
│       └── pending_store.py   # Thread-safe JSON queue (reports/testrail/pending_results.json)
│
├── e2e/                       # ← QA TEAM — feature files + step definitions only
│   ├── features/
│   │   ├── vehicle_api.feature# BL4B register/deregister (tagged @testrail_C*)
│   │   └── *.feature
│   ├── steps/
│   │   ├── vehicle_api_steps.py
│   │   ├── ai_steps.py
│   │   └── *.py
│   ├── pages/                 # Page Object Model
│   ├── plugins/               # e.g. PerformancePlugin
│   ├── utils/
│   │   ├── api/
│   │   │   ├── base_client.py # BaseAPIClient (requests.Session + retry + Allure)
│   │   │   └── vehicle_client.py
│   │   └── browser/           # Browser helpers
│   ├── resources/config.yaml  # E2E-only config copy
│   ├── environment.py         # Behave hooks — degrades gracefully without agent/
│   ├── run_tests.py           # CLI entry point for QA team
│   └── behave.ini             # pythonpath = . .. (resolves both e2e/ and root)
│
├── utils/                     # SHARED — both agent/ and e2e/ import from here
│   ├── logger.py
│   ├── misc.py
│   └── config_validator.py
│
├── helpers/                   # SHARED
│   └── constants/
│       └── framework_constants.py
│
├── tests/                     # Unit tests for agent/ (pytest)
│   ├── conftest.py            # Adds root + e2e/ to sys.path for pytest
│   ├── test_testrail.py       # 71 tests — Groovy parity, store lifecycle
│   ├── test_vehicle_api_client.py  # 47 tests
│   └── *.py
│
├── resources/
│   ├── config.yaml            # Framework config (NO secrets)
│   └── requirements.txt
└── .env.example               # All env vars documented (copy to .env)
```

### Import conventions

| Import | Resolved to |
|--------|-------------|
| `from agent.ai.llm_client import LLMClient` | `agent/ai/llm_client.py` |
| `from agent.testrail.client import TestRailClient` | `agent/testrail/client.py` |
| `from agent.integrations.notifier import UnifiedNotifier` | `agent/integrations/notifier.py` |
| `from utils.logger import log_info_emoji` | `utils/logger.py` (root — shared) |
| `from utils.api.vehicle_client import VehicleAPIClient` | `e2e/utils/api/vehicle_client.py` |
| `from helpers.constants.framework_constants import Paths` | `helpers/constants/framework_constants.py` (root — shared) |

---

## Critical patterns — always follow these

### Credentials — NEVER hardcode

All secrets come from environment variables only:

| Secret | Env var |
|--------|---------|
| Vehicle API key | `VEHICLE_API_KEY` |
| Anthropic API key | `AI_API_KEY` |
| OpenAI / Ollama API key | `AI_API_KEY` |
| TestRail API key | `TESTRAIL_API_KEY` |
| Teams webhook secret | `TEAMS_OUTGOING_WEBHOOK_SECRET` |
| WhatsApp token | `WHATSAPP_API_TOKEN` |
| Slack webhook URL | `SLACK_WEBHOOK_URL` |

Never put secrets in `config.yaml`, Python constants, or any committed file.

### Transaction IDs — always explicit

```python
# CORRECT — ID generated before request body construction
txn_id = generate_transaction_id()
log_info_emoji("🔑", f"transactionId → {txn_id}")
req = VehicleRegistrationRequest(transaction_id=txn_id, ...)

# WRONG — hidden in dataclass default_factory (never do this)
@dataclass
class VehicleRegistrationRequest:
    transaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))
```

### AI provider selection

```python
# CORRECT — use the abstraction (agent/ namespace)
from agent.ai.llm_client import LLMClient
client = LLMClient.from_config()
response = client.generate(prompt=prompt, system=system, images=[screenshot_path])

# WRONG — never import ollama directly (removed dependency)
import ollama  # ← deleted from requirements
```

### Step files are thin

Step definitions only call `utils/` methods and set `context.*` attributes. No HTTP, no SQL, no LLM calls directly in step files.

---

## AI provider configuration

The framework uses Anthropic Claude by default (matches OpenCode.ai Zen):

```bash
# .env
AI_PROVIDER=anthropic                    # default
AI_API_KEY=sk-ant-...                    # from console.anthropic.com
AI_MODEL=claude-3-5-haiku-20241022       # fast, cheap — good for QA
```

To use OpenAI instead:
```bash
AI_PROVIDER=openai
AI_API_KEY=sk-...
AI_MODEL=gpt-4o
```

To keep using local Ollama (via OpenAI-compatible API):
```bash
AI_PROVIDER=openai
AI_BASE_URL=http://localhost:11434/v1
AI_API_KEY=ollama
AI_MODEL=devstral:24b
```

To disable AI (CI without creds):
```bash
AI_PROVIDER=stub
```

---

## TestRail review workflow

Results are **never pushed automatically**. The flow is:

1. Tests run → `environment.py after_scenario` queues results for scenarios tagged `@testrail_C<id>`
2. Reviewer sends `!testrail status` (Teams or WhatsApp) — see pending count
3. Reviewer sends `!testrail preview` — see full result details
4. Reviewer sends `!testrail push` — POST to TestRail
5. Or `!testrail discard` — clear without pushing

TestRail commands: `status | preview | push [--run-id N] [--case N] | discard`

Pending results queue: `reports/testrail/pending_results.json`

Required env vars (at push time only): `TESTRAIL_URL`, `TESTRAIL_USER`, `TESTRAIL_API_KEY`, `TESTRAIL_RUN_ID`

Scenario tagging convention: `@testrail_C448337` → links to TestRail case C448337.

---

## Webhook server — chat commands

Start the server: `python -m agent.integrations.webhook_server`

```
!run                          — run all tests
!run --tags @smoke            — filter by tag
!run --browser firefox --env staging --headless
!status                       — last run summary
!help                         — command reference
!testrail status              — pending queue count
!testrail preview             — full pending result details
!testrail push                — push all pending to TestRail
!testrail push --run-id N     — push to specific run
!testrail push --case N       — push single case
!testrail discard             — clear queue
```

Teams auth: `TEAMS_OUTGOING_WEBHOOK_SECRET` (HMAC-SHA256 validation).
WhatsApp auth: `WHATSAPP_VERIFY_TOKEN` (Meta webhook verification) + `WHATSAPP_API_TOKEN`.

---

## Environment variables reference

See `.env.example` for the full list. Key ones:

```bash
# Run config
BROWSER=chromium               # chromium | firefox | webkit
ENV=dev                        # dev | staging | prod
HEADLESS=true
DEBUG=false

# AI
AI_PROVIDER=anthropic          # anthropic | openai | stub
AI_API_KEY=                    # required for anthropic and openai
AI_MODEL=                      # optional override
AI_BASE_URL=                   # OpenAI-compatible base URL (optional)

# Vehicle API
VEHICLE_API_KEY=               # x-api-key header
VEHICLE_API_BASE_URL=          # optional override of config.yaml url

# TestRail
TESTRAIL_URL=
TESTRAIL_USER=
TESTRAIL_API_KEY=
TESTRAIL_RUN_ID=

# Notifications
NOTIFY_ON_FAILURE=true
SLACK_WEBHOOK_URL=
TEAMS_WEBHOOK_URL=
TEAMS_OUTGOING_WEBHOOK_SECRET=
WHATSAPP_API_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=
```

---

## Adding a new API endpoint

1. Add path constant to `utils/api/vehicle_client.py` (or create a new `*_client.py`)
2. Create request dataclass with `transaction_id: str` (no default_factory)
3. Call `generate_transaction_id()` before constructing the dataclass
4. Add a method to the client that logs the txn_id and calls `self.post()`
5. Add step definitions in `steps/`
6. Add scenarios in `features/` with `@testrail_C<id>` tags
7. Write unit tests in `tests/test_*.py`

---

## Adding a new feature file

Tag every scenario with at least one of: `@smoke`, `@regression`, `@api`, `@performance`

Link to TestRail: add `@testrail_C<case_id>` tag on each scenario. The `after_scenario` hook in `environment.py` queues it automatically.

---

## Known decisions (do not revert)

- **No default_factory on transaction_id** — IDs must be generated explicitly before request construction so they appear in logs and Allure before the HTTP call fires.
- **No SMTP email** — notifications delegated to Teams/WhatsApp/Slack via webhooks. Email recipients are the CI/CD system's responsibility.
- **TestRail push is always manual** — results must be reviewed before pushing. No `auto_push` flag exists by design.
- **GitHub Actions workflow_dispatch only** — `push` and `pull_request` triggers disabled. Re-enable only after setting secrets in GitHub Actions.
- **`ollama` package removed** — replaced by `anthropic` and `openai`. To use Ollama, set `AI_PROVIDER=openai` and `AI_BASE_URL=http://localhost:11434/v1`.

---

## Running in CI (GitHub Actions / GitLab / Jenkins)

CI must set these secrets:
```
AI_PROVIDER=stub        # disable AI in CI (or set real key)
VEHICLE_API_KEY=...
NOTIFY_ON_FAILURE=true
SLACK_WEBHOOK_URL=...
```

The workflow file is `.github/workflows/ci.yml` — currently `workflow_dispatch` only.
GitLab: `.gitlab-ci.yml` | Jenkins: `Jenkinsfile`

---

## Linting / type checking

```bash
ruff check .          # lint
ruff format .         # format
mypy .                # type check (ignore_missing_imports=true)
```
