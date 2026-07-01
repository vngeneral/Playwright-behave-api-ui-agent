"""
Behave Environment Hooks — E2E Test Suite
==========================================
This file contains only what the QA team needs to run UI and API tests.

AI features (selector healing, TestRail queueing, notifications) activate
automatically when the `agent` package is available on the Python path.
If `agent` is not installed, all tests still run — the hooks degrade
gracefully to no-ops.

Lifecycle:
  before_all      – validate config, launch browser, wire shared state
  before_scenario – fresh page per scenario (full isolation)
  after_scenario  – collect metrics (if agent), queue TestRail result (if agent)
  after_all       – send notifications (if agent), shut down browser
  before_step     – record BDD step name for AI healer context
  after_step      – screenshot + Allure attach on failure

Running from e2e/:
    cd e2e
    python run_tests.py --tags @smoke
    # or
    behave features/ --tags @smoke
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
# Ensure both e2e/ and the repo root are on sys.path so imports resolve:
#   from utils.api.vehicle_client import ...   → e2e/utils/api/vehicle_client.py
#   from utils.logger import ...               → root utils/logger.py
#   from helpers.constants.framework_constants → root helpers/
#   from agent.ai.selector_healer import ...  → root agent/ai/selector_healer.py
_E2E_DIR  = Path(__file__).parent
_ROOT_DIR = _E2E_DIR.parent
for _p in (_E2E_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ── Shared imports (always available) ────────────────────────────────────
from helpers.constants.framework_constants import Paths, Reporting
from utils.browser.browser import prepare_browser
from utils.config_validator import validate_config
from utils.logger import log_failure, log_info_emoji, log_warning
from utils.misc import load_config, get_active_env
from utils.reporting import attach_screenshot
from pages.page_factory import PageFactory

# ── Engine imports (optional — AI/TestRail/notifications) ─────────────────
_AGENT = False
try:
    from agent.ai.selector_healer import AISelectorHealer
    from agent.monitoring.metrics import MetricsCollector
    from agent.integrations.notifier import UnifiedNotifier
    from agent.testrail.result_mapper import from_behave_scenario, extract_case_ids
    from agent.testrail.pending_store import get_default_store
    _AGENT = True
    log_info_emoji("🤖", "Agent loaded — AI healing, TestRail queuing, and notifications enabled")
except ImportError:
    log_warning(
        "agent/ package not found — running in E2E-only mode. "
        "AI selector healing, TestRail queuing, and notifications are disabled. "
        "This is expected for QA team runs."
    )

    # ── No-op stubs so the rest of environment.py doesn't need if/_AGENT guards ──

    class _NoopMetrics:
        def start_run(self, **_): pass
        def start_scenario(self, *_): pass
        def record_scenario(self, *_): pass
        def finish_run(self): return {}
        def save(self): return "(no metrics)"
        def summary_line(self): return "Agent not loaded — no metrics"

    class _NoopNotifier:
        def notify(self, *_): pass

    class _NoopHealer:
        def heal_selector(self, *_, **__): return ""
        def stop_model(self): pass

    class _NoopPerfPlugin:
        def before_scenario(self, *_): pass
        def after_scenario(self, *_): pass

    MetricsCollector   = _NoopMetrics
    UnifiedNotifier    = _NoopNotifier
    AISelectorHealer   = _NoopHealer

    def extract_case_ids(_scenario): return []
    def from_behave_scenario(*_): return None
    def get_default_store(): return _NoopStore()

    class _NoopStore:
        def add(self, *_): pass
        def has_pending(self): return False

# ── Performance plugin (e2e-local) ────────────────────────────────────────
try:
    from plugins.performance_plugin import PerformancePlugin
except ImportError:
    class PerformancePlugin:
        def before_scenario(self, *_): pass
        def after_scenario(self, *_): pass


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
_metrics  = MetricsCollector()
_perf     = PerformancePlugin()
_notifier = UnifiedNotifier()
if _AGENT:
    _testrail_store = get_default_store()
else:
    _testrail_store = _NoopStore()


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def before_all(context):
    # ── Config validation ──────────────────────────────────────────────────
    config = load_config()
    try:
        validate_config(config)
        log_info_emoji("✅", "config.yaml validated")
    except Exception as exc:
        log_warning(f"Config validation warning: {exc}")

    # ── Debug mode ─────────────────────────────────────────────────────────
    context.debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1")
    if context.debug_mode:
        logging.getLogger().setLevel(logging.DEBUG)
        log_info_emoji("🐛", "Debug mode ON — verbose logging enabled")

    # ── Shared state ───────────────────────────────────────────────────────
    context.logger = logging.getLogger("framework")
    context.config = config
    context.page_factory = PageFactory()
    context.ai = AISelectorHealer()

    # ── Browser ────────────────────────────────────────────────────────────
    prepare_browser(context)

    # ── Metrics ────────────────────────────────────────────────────────────
    _metrics.start_run(
        environment=get_active_env(config),
        browser=os.getenv("BROWSER", config.get("browser", {}).get("default", "chromium")),
    )

    # ── Allure categories ──────────────────────────────────────────────────
    _copy_allure_categories()


def before_scenario(context, scenario):
    context.page = context.browser_manager.new_page()
    log_info_emoji("▶️", f"Scenario: {scenario.name}")
    _metrics.start_scenario(scenario)
    _perf.before_scenario(context, scenario)


def after_scenario(context, scenario):
    _metrics.record_scenario(scenario)
    _perf.after_scenario(context, scenario)

    if str(scenario.status).endswith("failed"):
        log_failure(f"Scenario FAILED: {scenario.name}")
    else:
        log_info_emoji("✅", f"Scenario PASSED: {scenario.name}")

    # ── TestRail pending queue (agent only) ───────────────────────────────
    if _AGENT:
        for case_id in extract_case_ids(scenario):
            try:
                result = from_behave_scenario(scenario, case_id)
                _testrail_store.add(result)
                log_info_emoji(
                    "📋",
                    f"TestRail C{case_id} queued "
                    f"({'passed' if result.status_id == 1 else 'failed'})"
                )
            except Exception as exc:
                log_warning(f"TestRail queue error for C{case_id}: {exc}")

    # Always close the page
    context.browser_manager.close_page()
    context.page = None


def after_all(context):
    metrics = _metrics.finish_run()
    saved  = _metrics.save()
    log_info_emoji("📊", f"Metrics → {saved}")
    log_info_emoji("📋", _metrics.summary_line())

    _notifier.notify(metrics)

    context.browser_manager.stop()


def before_step(context, step):
    context.bdd_step = step.name


def after_step(context, step):
    if str(step.status).endswith("failed"):
        log_failure(f"Step failed: {step.name}")
        if getattr(context, "page", None):
            attach_screenshot(context, step)
            if getattr(context, "debug_mode", False):
                _attach_page_source(context, step)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _copy_allure_categories() -> None:
    src = Paths.ALLURE_CATEGORIES
    dst = os.path.join(Paths.ALLURE_RESULTS_DIR, "categories.json")
    if os.path.exists(src):
        os.makedirs(Paths.ALLURE_RESULTS_DIR, exist_ok=True)
        shutil.copy2(src, dst)


def _attach_page_source(context, step) -> None:
    try:
        import allure
        html = context.page.content()
        allure.attach(
            html,
            name=f"page_source_{step.name[:40]}",
            attachment_type=allure.attachment_type.HTML,
        )
    except Exception:
        pass
