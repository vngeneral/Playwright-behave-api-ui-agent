"""
TestRail Result Mapper
======================
Translates test execution data into TestRail result payloads.

This is a direct Python port of the Groovy JSR223 sampler logic:

  Groovy (JMeter):                    Python (this module):
  ─────────────────────────────────   ──────────────────────────────────────
  step.statusCode == "200"|"204"  →   is_passing_status_code(status_code)
  failedSteps = steps.findAll{…}  →   find_failed_steps(steps)
  status = 5 (failed)             →   TESTRAIL_FAILED = 5
  status = 1 (passed)             →   TESTRAIL_PASSED = 1
  comment += step.responseData    →   build_step_comment(steps)
  updateResult(comment,status,id) →   TestRailResult(case_id, status_id, comment)

Two entry points:
  1. ``from_step_results()``  — mirrors JMeter flow: HTTP steps → one result
  2. ``from_behave_scenario()``  — Behave-native: scenario + steps → one result

TestRail case IDs come from scenario tags: ``@testrail_C448337``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# TestRail status constants (standard IDs)
# ---------------------------------------------------------------------------

TESTRAIL_PASSED    = 1   # Passed
TESTRAIL_BLOCKED   = 2   # Blocked
TESTRAIL_UNTESTED  = 3   # Untested
TESTRAIL_RETEST    = 4   # Retest
TESTRAIL_FAILED    = 5   # Failed

# HTTP codes treated as success — mirrors Groovy: code != 200 && code != 204
_PASSING_HTTP_CODES = {200, 204}

# Tag prefix used to link a Behave scenario to a TestRail case
# e.g.  @testrail_C448337  →  case_id = "448337"
TESTRAIL_TAG_PREFIX = "testrail_C"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """
    Represents one HTTP step result — direct equivalent of the JMeter step map:
        name, requestData, responseData, statusCode
    """
    name: str
    status_code: str          # kept as string to mirror Groovy (may be non-numeric)
    request_data: str = ""
    response_data: str = ""


@dataclass
class TestRailResult:
    """One result entry ready to POST to TestRail."""
    case_id: str              # e.g. "448337"
    status_id: int            # TESTRAIL_PASSED (1) or TESTRAIL_FAILED (5)
    comment: str
    scenario_name: str = ""
    elapsed: Optional[str] = None    # e.g. "45s" — optional timing field
    step_details: list[dict] = field(default_factory=list)

    def to_api_dict(self) -> dict:
        """Serialise to the shape expected by TestRail API v2 add_results_for_cases."""
        payload: dict = {
            "case_id":   int(self.case_id),
            "status_id": self.status_id,
            "comment":   self.comment,
        }
        if self.elapsed:
            payload["elapsed"] = self.elapsed
        return payload


# ---------------------------------------------------------------------------
# Core logic — mirrors the Groovy script exactly
# ---------------------------------------------------------------------------


def is_passing_status_code(status_code: str) -> bool:
    """
    Return True if the HTTP status code represents success.

    Mirrors Groovy:
        if (isNumeric(statusCode)) {
            int code = statusCode.toInteger()
            return code != 200 && code != 204   ← invert: we return True for pass
        } else {
            return true  // treat non-numeric as failure
        }
    """
    try:
        return int(status_code) in _PASSING_HTTP_CODES
    except (ValueError, TypeError):
        return False   # non-numeric → failure (same as Groovy's `return true` in catch)


def find_failed_steps(steps: list[StepResult]) -> list[StepResult]:
    """Return steps whose status code is not in {200, 204}."""
    return [s for s in steps if not is_passing_status_code(s.status_code)]


def build_step_comment(steps: list[StepResult], account_context: str = "") -> tuple[str, int]:
    """
    Build the TestRail comment string and determine status_id.

    Mirrors the Groovy if/else block:
        if (failedSteps) { status=5; comment=... } else { status=1; comment=... }

    Returns:
        (comment_string, status_id)
    """
    failed = find_failed_steps(steps)
    account = account_context or (steps[0].request_data if steps else "")

    if failed:
        status_id = TESTRAIL_FAILED
        lines = [f"Account:\n{account}\n", "Failed steps:"]
        for step in failed:
            try:
                int(step.status_code)
                lines.append(
                    f"{step.name} failed with status code {step.status_code}: {step.response_data}"
                )
            except (ValueError, TypeError):
                lines.append(
                    f"{step.name} failed with non-numeric status code "
                    f"{step.status_code}: {step.response_data}"
                )
        comment = "\n".join(lines)
    else:
        status_id = TESTRAIL_PASSED
        comment = f"All steps are good with account\n{account}"

    return comment, status_id


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def from_step_results(
    case_id: str,
    steps: list[StepResult],
    account_context: str = "",
    scenario_name: str = "",
    elapsed: Optional[str] = None,
) -> TestRailResult:
    """
    Build a TestRailResult from a list of HTTP step results.

    This is the Python equivalent of the JMeter Groovy sampler:
      - evaluate each step's statusCode
      - determine pass/fail and build comment
      - return a TestRailResult ready to be pushed (or queued for review)

    Args:
        case_id:          TestRail case ID (e.g. "448337")
        steps:            List of StepResult from HTTP calls
        account_context:  Optional account/request context to prepend (mirrors Groovy `account` var)
        scenario_name:    Human-readable scenario name for the review queue
        elapsed:          Optional timing string (e.g. "45s")
    """
    comment, status_id = build_step_comment(steps, account_context)
    return TestRailResult(
        case_id=case_id,
        status_id=status_id,
        comment=comment,
        scenario_name=scenario_name,
        elapsed=elapsed,
        step_details=[
            {
                "name": s.name,
                "status_code": s.status_code,
                "request_data": s.request_data[:500],
                "response_data": s.response_data[:500],
                "passed": is_passing_status_code(s.status_code),
            }
            for s in steps
        ],
    )


def from_behave_scenario(scenario, case_id: str) -> TestRailResult:
    """
    Build a TestRailResult from a Behave scenario object.

    Scenario status mapping:
        "passed"  → TESTRAIL_PASSED (1)
        anything else → TESTRAIL_FAILED (5)

    Comment mirrors the JMeter format — lists failed steps with their
    error messages in place of HTTP response data.

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
        if not step_status.endswith("passed") and not step_status.endswith("skipped"):
            failed_steps.append(step_info)

    if failed_steps:
        lines = [f"Scenario: {scenario.name}", "Failed steps:"]
        for s in failed_steps:
            err = s.get("error_message") or "no error message"
            lines.append(f"  {s['name']}: {err}")
        comment = "\n".join(lines)
    else:
        comment = f"All steps are good for scenario: {scenario.name}"

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
