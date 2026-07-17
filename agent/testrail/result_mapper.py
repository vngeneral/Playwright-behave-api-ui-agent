"""
TestRail Result Mapper
======================
Maps a finished Behave scenario to a TestRail result payload — status id plus
a comment that always carries the failure cause (failed-step error messages,
hook errors, undefined-step labels). The comment format is inherited from the
original JMeter/Groovy sampler this framework replaced.

Entry point: ``from_behave_scenario(scenario, case_id)`` — called from the
``after_scenario`` hook for every scenario tagged ``@testrail_C<id>``.
Case IDs come from scenario tags via ``extract_case_ids(scenario)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# TestRail status constants (standard IDs)
# ---------------------------------------------------------------------------

TESTRAIL_PASSED    = 1   # Passed
TESTRAIL_BLOCKED   = 2   # Blocked
TESTRAIL_UNTESTED  = 3   # Untested
TESTRAIL_RETEST    = 4   # Retest
TESTRAIL_FAILED    = 5   # Failed

# Tag prefix used to link a Behave scenario to a TestRail case
# e.g.  @testrail_C448337  →  case_id = "448337"
TESTRAIL_TAG_PREFIX = "testrail_C"

# Behave step statuses that must NOT be reported as failures. "skipped" =
# never reached after an earlier failure; "untested" = never ran at all
# (e.g. a before_scenario hook error aborted the scenario).
_NON_FAILURE_STEP_SUFFIXES = ("passed", "skipped", "untested")

# Cap per-error text in TestRail comments — Playwright timeout errors can
# embed multi-KB DOM dumps that drown the actual assertion message.
_MAX_ERROR_CHARS = 2000


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TestRailResult:
    """One result entry ready to POST to TestRail."""
    case_id: str              # e.g. "448337"
    status_id: int            # TESTRAIL_PASSED (1) or TESTRAIL_FAILED (5)
    comment: str
    scenario_name: str = ""
    elapsed: str | None = None    # e.g. "45s" — optional timing field
    step_details: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def from_behave_scenario(scenario, case_id: str) -> TestRailResult:
    """
    Build a TestRailResult from a Behave scenario object.

    Scenario status mapping:
        "passed"  → TESTRAIL_PASSED (1)
        anything else → TESTRAIL_FAILED (5)

    Comment mirrors the JMeter format — lists failed steps with their
    error messages in place of HTTP response data. Skipped/untested steps
    are never reported as failures; undefined steps are labelled as such.
    Hook errors (e.g. before_scenario crash) leave every step untested and
    put the reason in ``scenario.error_message`` — that message is included
    so a failed result never reaches TestRail without its failure cause.

    Args:
        scenario:   A Behave Scenario object (from after_scenario hook).
        case_id:    TestRail case ID extracted from @testrail_C<id> tag.
    """
    passed = str(scenario.status).endswith("passed")
    status_id = TESTRAIL_PASSED if passed else TESTRAIL_FAILED

    step_details = []
    failed_steps = []

    for step in scenario.steps:
        step_status = str(step.status)
        step_info = {
            "name": f"{step.step_type} {step.name}",
            "status": step_status,
            "error_message": str(step.error_message) if step.error_message else None,
        }
        step_details.append(step_info)
        if not step_status.endswith(_NON_FAILURE_STEP_SUFFIXES):
            failed_steps.append(step_info)

    if passed:
        comment = f"All steps are good for scenario: {scenario.name}"
    else:
        # Hook errors (behave sets scenario.error_message, e.g.
        # "HOOK-ERROR in before_scenario: ...") — steps carry no error then.
        scenario_error = getattr(scenario, "error_message", None)
        lines = [f"Scenario: {scenario.name}"]
        if failed_steps:
            lines.append("Failed steps:")
            for s in failed_steps:
                lines.append(f"  {s['name']}: {_step_error_text(s)}")
        if scenario_error:
            lines.append(f"Scenario error: {_truncate_error(str(scenario_error))}")
        if not failed_steps and not scenario_error:
            lines.append(
                "Scenario failed outside its steps (hook error?) — "
                "no step error was recorded"
            )
        comment = "\n".join(lines)

    # Duration in TestRail "Xs" format
    duration = getattr(scenario, "duration", None)
    elapsed = f"{int(duration)}s" if duration and duration > 0 else None

    return TestRailResult(
        case_id=case_id,
        status_id=status_id,
        comment=comment,
        scenario_name=scenario.name,
        elapsed=elapsed,
        step_details=step_details,
    )


def _step_error_text(step_info: dict) -> str:
    """Failure text for one step line in the TestRail comment."""
    if step_info["status"].endswith("undefined"):
        return "undefined step — no matching step definition found"
    err = step_info.get("error_message")
    return _truncate_error(err) if err else "no error message"


def _truncate_error(text: str) -> str:
    """Cap error text so DOM dumps don't drown the comment."""
    if len(text) <= _MAX_ERROR_CHARS:
        return text
    return text[:_MAX_ERROR_CHARS] + " … [truncated]"


def extract_case_ids(scenario) -> list[str]:
    """
    Extract TestRail case IDs from scenario tags.

    A tag of ``@testrail_C448337`` yields case_id ``"448337"``.
    A scenario may have multiple TestRail tags (rare but supported).
    Returns an empty list if no TestRail tags are present.
    """
    case_ids = []
    for tag in getattr(scenario, "tags", []):
        if tag.startswith(TESTRAIL_TAG_PREFIX):
            case_ids.append(tag[len(TESTRAIL_TAG_PREFIX):])
    return case_ids
