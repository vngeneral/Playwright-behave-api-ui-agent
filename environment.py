"""
Behave Environment Hooks
========================
Lifecycle:
  before_all      – validate config, launch browser, wire shared state
  before_scenario – fresh page per scenario (full isolation)
  after_scenario  – collect metrics, close page
  after_all       – save metrics, send alerts, shut down browser
  before_step     – record BDD step name for AI healer context
  after_step      – screenshot + Allure attach on failure

Plugins wired here (not monkey-patched into Behave):
  • MetricsCollector  → JSON run summary in reports/metrics/
  • PerformancePlugin → per-scenario browser timing in Allure
  • UnifiedNotifier   → Slack + Teams + WhatsApp notifications on failure
"""
from __future__ import annotations

import logging
import os
import shutil

from ai.selector_healer import AISelectorHealer
from helpers.constants.framework_constants import Paths, Reporting
from integrations.notifier import UnifiedNotifier
from monitoring.metrics import MetricsCollector
from pages.page_factory import PageFactory
from plugins.performance_plugin import PerformancePlugin
from utils.browser.browser import prepare_browser
from utils.config_validator import validate_config
from utils.logger import log_failure, log_info_emoji, log_warning
from utils.misc import load_config, get_active_env
from utils.reporting import attach_screenshot


# ---------------------------------------------------------------------------
# Module-level singletons (shared across all scenarios in one run)
# ---------------------------------------------------------------------------
_metrics = MetricsCollector()
_perf = PerformancePlugin()
_notifier = UnifiedNotifier()   # Slack + Teams + WhatsApp


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
    prepare_browser(context)          # launches Playwright + browser (no page yet)

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
    # Metrics + perf first (they read scenario.status)
    _metrics.record_scenario(scenario)
    _perf.after_scenario(context, scenario)

    if str(scenario.status).endswith("failed"):
        log_failure(f"Scenario FAILED: {scenario.name}")
    else:
        log_info_emoji("✅", f"Scenario PASSED: {scenario.name}")

    # Always close the page so cookies / storage don't leak between scenarios
    context.browser_manager.close_page()
    context.page = None


def after_all(context):
    # ── Finalise metrics ───────────────────────────────────────────────────
    metrics = _metrics.finish_run()
    saved_to = _metrics.save()
    log_info_emoji("📊", f"Metrics saved → {saved_to}")
    log_info_emoji("📋", _metrics.summary_line())

    # ── Notifications (Slack + Teams + WhatsApp) ──────────────────────────
    _notifier.notify(metrics)

    # ── Browser teardown ───────────────────────────────────────────────────
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
    """
    Copy allure_categories.json into allure-results so Allure CLI picks it up
    when generating the report.
    """
    src = Paths.ALLURE_CATEGORIES
    dst = os.path.join(Paths.ALLURE_RESULTS_DIR, "categories.json")
    if os.path.exists(src):
        os.makedirs(Paths.ALLURE_RESULTS_DIR, exist_ok=True)
        shutil.copy2(src, dst)


def _attach_page_source(context, step) -> None:
    """Attach the raw HTML source to Allure (debug mode only)."""
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
