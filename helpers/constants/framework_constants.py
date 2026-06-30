"""
Framework Constants
===================
Organised into typed namespaced classes so callers can use:

    from helpers.constants.framework_constants import Paths, Timeouts, Perf

Flat aliases at the bottom preserve backward compatibility with existing imports:

    from helpers.constants.framework_constants import SCREENSHOTS_DIR, ALLURE_RESULTS_DIR
"""
from __future__ import annotations

import os

_ROOT = os.getcwd()
_REPORTS = os.path.join(_ROOT, "reports")
_TEST_DATA = os.path.join(_ROOT, "test_data")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
class Paths:
    ROOT = _ROOT
    RESOURCES = os.path.join(_ROOT, "resources")
    CONFIG_YAML = os.path.join(RESOURCES, "config.yaml")

    # Reports
    REPORTS = _REPORTS
    SCREENSHOTS_DIR = os.path.join(_REPORTS, "screenshots")
    WORKER_DIR = os.path.join(_REPORTS, "workers")
    ALLURE_RESULTS_DIR = os.path.join(_REPORTS, "allure-results")
    METRICS_DIR = os.path.join(_REPORTS, "metrics")

    # Tracing
    TRACES_DIR = os.path.join(_REPORTS, "traces")
    TRACES_VIDEOS_DIR = os.path.join(_REPORTS, "traces", "videos")

    # AI artefacts
    AI_ARTIFACTS_DIR = os.path.join(_REPORTS, "ai")

    # Allure categories definition (project-root level)
    ALLURE_CATEGORIES = os.path.join(_ROOT, "allure_categories.json")


# Need RESOURCES defined before CONFIG_YAML references it
Paths.RESOURCES = os.path.join(_ROOT, "resources")
Paths.CONFIG_YAML = os.path.join(Paths.RESOURCES, "config.yaml")


# ---------------------------------------------------------------------------
# Test Data
# ---------------------------------------------------------------------------
class TestData:
    """Paths to test-data files.  Actual data is in test_data/ — never in code."""
    ROOT = _TEST_DATA
    USERS_FILE = os.path.join(_TEST_DATA, "users.json")
    FORM_DATA_FILE = os.path.join(_TEST_DATA, "form_data.json")
    API_SCENARIOS_FILE = os.path.join(_TEST_DATA, "api_scenarios.json")


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
class Timeouts:
    """All values in milliseconds unless the name ends in _S (seconds)."""
    DEFAULT_MS: int = 10_000
    NAVIGATION_MS: int = 30_000
    NETWORK_IDLE_MS: int = 15_000
    ELEMENT_WAIT_MS: int = 5_000

    # Assertion thresholds (seconds)
    PAGE_LOAD_S: float = 3.0
    API_RESPONSE_S: float = 5.0


# ---------------------------------------------------------------------------
# Performance / parallel execution
# ---------------------------------------------------------------------------
class Perf:
    """
    NOTE: There is NO browser pool.  In parallel mode each worker subprocess
    gets its own independent browser process.  Sharing browser instances
    across workers breaks test isolation and causes race conditions.
    MAX_PARALLEL_WORKERS controls concurrency at the *process* level.
    """
    MAX_PARALLEL_WORKERS: int = 4
    MEMORY_INCREASE_LIMIT_MB: int = 100   # per-scenario memory guard
    MIN_NETWORK_REQUESTS: int = 1         # sanity check floor


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
class Reporting:
    """
    Notification credentials MUST NOT be hardcoded.
    Read them from environment variables at runtime.

    Slack example:
        export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
    Email:
        Delegate to CI/CD (GitHub Actions, GitLab, Jenkins) — not the framework.
    """
    CATEGORIES_FILE: str = Paths.ALLURE_CATEGORIES
    METRICS_FILE: str = os.path.join(Paths.METRICS_DIR, "run_metrics.json")

    # Env-var names (values come from the environment, never from code)
    SLACK_WEBHOOK_ENV_VAR: str = "SLACK_WEBHOOK_URL"
    EMAIL_RECIPIENTS_ENV_VAR: str = "NOTIFY_EMAIL_RECIPIENTS"


# ---------------------------------------------------------------------------
# Backward-compatible flat aliases (existing imports keep working)
# ---------------------------------------------------------------------------
RESOURCES = Paths.RESOURCES
CONFIG_YAML = Paths.CONFIG_YAML
REPORTS = Paths.REPORTS
SCREENSHOTS_DIR = Paths.SCREENSHOTS_DIR
WORKER_DIR = Paths.WORKER_DIR
ALLURE_RESULTS_DIR = Paths.ALLURE_RESULTS_DIR
TRACES_DIR = Paths.TRACES_DIR
TRACES_VIDEOS_DIR = Paths.TRACES_VIDEOS_DIR
AI_ARTIFACTS_DIR = Paths.AI_ARTIFACTS_DIR
