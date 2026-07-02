"""
Step definitions for AI-driven test scenarios.
Covers: self-healing selector demo and AI-generated test execution.

The contact-form navigation/fill/submit steps used by the `ai_healing` tag
scenarios are defined once in forms_steps.py and shared here — Behave step
definitions are registered globally, so they don't need to be redefined.
"""
from behave import step

from utils.logger import log_info_emoji


# ---------------------------------------------------------------------------
# AI healing introspection steps
# ---------------------------------------------------------------------------

@step("the AI healer encounters a broken selector")
def step_trigger_ai_healer(context):
    """
    Deliberately calls heal_selector with a bad selector so the AI healer
    exercises its full pipeline (screenshot → LLM → validation → log).
    """
    log_info_emoji("🧪", "Triggering AI selector healer with an intentionally broken selector")
    healed = context.ai.heal_selector(
        context=context,
        exception="TimeoutError: locator.fill: Timeout 5000ms exceeded (demo)",
        original_selector="input[name='__broken_selector__']",
    )
    context.healed_selector = healed
    log_info_emoji("🔍", f"Healed selector returned: {healed!r}")


@step("the AI healer should return a non-empty selector")
def step_assert_healed_selector(context):
    assert getattr(context, "healed_selector", None), (
        "AI healer returned an empty selector — check the AI model and logs in reports/ai/"
    )
    log_info_emoji("✅", f"Healed selector: {context.healed_selector}")


@step("the AI healer log should contain the healing attempt")
def step_assert_healer_log(context):
    import json
    from pathlib import Path
    from helpers.constants.framework_constants import AI_ARTIFACTS_DIR

    log_path = Path(AI_ARTIFACTS_DIR) / "selector_log.json"
    assert log_path.exists(), f"Selector log not found at {log_path}"
    entries = json.loads(log_path.read_text())
    assert entries, "Selector log is empty"
    last = entries[-1]
    assert "healed_selector" in last, f"Unexpected log structure: {last}"
    log_info_emoji("📋", f"Last log entry: {last}")
