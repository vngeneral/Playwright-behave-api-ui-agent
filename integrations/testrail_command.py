"""
TestRail Command Handler
========================
Processes ``!testrail`` commands received from Teams or WhatsApp webhooks.

The review workflow:
    1. Tests run → Behave ``after_scenario`` populates the pending queue
    2. Reviewer issues ``!testrail status``  — see what's waiting
    3. Reviewer issues ``!testrail preview`` — read full result details
    4. Reviewer issues ``!testrail push``    — POST results to TestRail
       (optionally: ``--run-id N`` or ``--case N`` to narrow scope)
    5. OR:       ``!testrail discard``       — clear the queue without pushing

Commands:
    !testrail status                   Show pending count and summary
    !testrail preview                  Show full detail of all pending results
    !testrail push                     Push all pending to the default run
    !testrail push --run-id 123        Push to a specific run
    !testrail push --case 448337       Push only case 448337
    !testrail discard                  Clear the pending queue

Environment variables consumed at push time:
    TESTRAIL_URL, TESTRAIL_USER, TESTRAIL_API_KEY — TestRail credentials
    TESTRAIL_RUN_ID                               — default run ID (int)
"""
from __future__ import annotations

import re
from typing import Any

from utils.logger import log_info_emoji, log_warning
from utils.testrail.client import TestRailClient, TestRailAPIError, TestRailConfigError
from utils.testrail.pending_store import get_default_store

# ---------------------------------------------------------------------------
# Sub-command constants
# ---------------------------------------------------------------------------

CMD_STATUS  = "status"
CMD_PREVIEW = "preview"
CMD_PUSH    = "push"
CMD_DISCARD = "discard"

_VALID_SUBCOMMANDS = {CMD_STATUS, CMD_PREVIEW, CMD_PUSH, CMD_DISCARD}

# Status ID labels for display
_STATUS_LABELS = {1: "✅ PASSED", 5: "❌ FAILED", 2: "🚫 BLOCKED", 4: "🔁 RETEST"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handle_testrail_command(command_text: str) -> str:
    """
    Parse and execute a ``!testrail`` command.

    Args:
        command_text: The raw message text, e.g. "!testrail push --run-id 42"

    Returns:
        A plain-text response string to send back to the user.
    """
    parts = command_text.strip().split()
    if not parts or parts[0].lower() != "!testrail":
        return "❓ Not a !testrail command."

    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub not in _VALID_SUBCOMMANDS:
        return _help_text()

    if sub == CMD_STATUS:
        return _cmd_status()
    if sub == CMD_PREVIEW:
        return _cmd_preview()
    if sub == CMD_DISCARD:
        return _cmd_discard()
    if sub == CMD_PUSH:
        run_id, case_ids = _parse_push_args(parts[2:])
        return _cmd_push(run_id=run_id, case_ids=case_ids)

    return _help_text()


# ---------------------------------------------------------------------------
# Sub-command implementations
# ---------------------------------------------------------------------------


def _cmd_status() -> str:
    store  = get_default_store()
    status = store.get_status()

    pending_count = status["pending_count"]
    pushed_count  = status["pushed_count"]

    lines = [
        "📊 *TestRail Queue Status*",
        f"  Pending review : {pending_count}",
        f"  Already pushed : {pushed_count}",
        f"  Store path     : {status['store_path']}",
    ]

    if pending_count:
        lines.append("")
        lines.append("Pending results:")
        for entry in status["pending"][:10]:   # cap at 10 lines
            label = _STATUS_LABELS.get(entry.get("status_id", 0), "?")
            lines.append(f"  Case C{entry['case_id']}  {label}  — {entry.get('scenario_name', '(unknown)')}")
        if pending_count > 10:
            lines.append(f"  … and {pending_count - 10} more")
        lines.append("")
        lines.append("Run `!testrail preview` for details, `!testrail push` to submit.")
    else:
        lines.append("Queue is empty — nothing pending.")

    return "\n".join(lines)


def _cmd_preview() -> str:
    store   = get_default_store()
    pending = store.get_pending()

    if not pending:
        return "ℹ️ No pending results in the queue."

    lines = [f"📋 *TestRail Pending Results* ({len(pending)} total)"]
    for i, entry in enumerate(pending, 1):
        label = _STATUS_LABELS.get(entry.get("status_id", 0), "?")
        lines.append(f"\n[{i}] Case C{entry['case_id']}  {label}")
        lines.append(f"    Scenario : {entry.get('scenario_name', '(unknown)')}")
        lines.append(f"    Added    : {entry.get('added_at', '?')}")
        if entry.get("elapsed"):
            lines.append(f"    Duration : {entry['elapsed']}")
        comment = entry.get("comment", "")
        if comment:
            # Truncate long comments for readability in chat
            excerpt = comment[:300].replace("\n", " | ")
            lines.append(f"    Comment  : {excerpt}{'…' if len(comment) > 300 else ''}")

    lines.append("\nRun `!testrail push` to submit, or `!testrail discard` to clear.")
    return "\n".join(lines)


def _cmd_push(run_id: int | None, case_ids: list[str]) -> str:
    store   = get_default_store()
    pending = store.get_pending()

    if not pending:
        return "ℹ️ Nothing to push — pending queue is empty."

    # Filter by case IDs if requested
    to_push = pending
    if case_ids:
        to_push = [e for e in pending if e.get("case_id") in case_ids]
        if not to_push:
            return f"⚠️ No pending results for case(s): {', '.join(case_ids)}"

    # Resolve run ID
    try:
        client = TestRailClient.from_env()
    except TestRailConfigError as exc:
        return f"⚙️ TestRail not configured: {exc}"

    if run_id is None:
        run_id = TestRailClient.default_run_id()
    if run_id is None:
        return (
            "⚙️ No run ID specified. "
            "Set TESTRAIL_RUN_ID env var or use `!testrail push --run-id <N>`."
        )

    # Build API payload
    api_results = []
    for entry in to_push:
        payload: dict[str, Any] = {
            "case_id":   int(entry["case_id"]),
            "status_id": entry["status_id"],
            "comment":   entry.get("comment", ""),
        }
        if entry.get("elapsed"):
            payload["elapsed"] = entry["elapsed"]
        api_results.append(payload)

    log_info_emoji("📤", f"Pushing {len(api_results)} result(s) to TestRail run {run_id}")

    try:
        with client:
            client.add_results_for_cases(run_id=run_id, results=api_results)
    except TestRailAPIError as exc:
        log_warning(f"TestRail push failed: {exc}")
        return f"❌ Push failed (HTTP {exc.status_code}): {exc.body[:200]}"
    except Exception as exc:
        log_warning(f"TestRail push unexpected error: {exc}")
        return f"❌ Push failed: {exc}"

    # Mark as pushed
    pushed_case_ids = [str(e["case_id"]) for e in to_push]
    store.mark_pushed(pushed_case_ids)

    passed = sum(1 for e in to_push if e.get("status_id") == 1)
    failed = sum(1 for e in to_push if e.get("status_id") == 5)

    return (
        f"✅ Pushed {len(to_push)} result(s) to TestRail run {run_id}\n"
        f"   Passed: {passed}  |  Failed: {failed}"
    )


def _cmd_discard() -> str:
    store   = get_default_store()
    pending = store.get_pending()
    count   = len(pending)

    if count == 0:
        return "ℹ️ Queue is already empty — nothing to discard."

    store.clear()
    return f"🗑️ Discarded {count} pending result(s). Queue is now empty."


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------


def _parse_push_args(args: list[str]) -> tuple[int | None, list[str]]:
    """
    Parse optional flags from push sub-command args.

    Supports:
        --run-id 123
        --case 448337   (may be repeated: --case 448337 --case 448338)

    Returns:
        (run_id or None, list_of_case_ids)
    """
    run_id: int | None = None
    case_ids: list[str] = []

    i = 0
    while i < len(args):
        token = args[i]
        if token == "--run-id" and i + 1 < len(args):
            try:
                run_id = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif token == "--case" and i + 1 < len(args):
            # Normalise: strip leading "C" if user typed "C448337"
            raw_case = args[i + 1].lstrip("Cc")
            case_ids.append(raw_case)
            i += 2
        else:
            i += 1

    return run_id, case_ids


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def _help_text() -> str:
    return (
        "ℹ️ *!testrail commands:*\n"
        "  `!testrail status`              — show pending count\n"
        "  `!testrail preview`             — show full result details\n"
        "  `!testrail push`                — push all pending results\n"
        "  `!testrail push --run-id N`     — push to a specific run\n"
        "  `!testrail push --case N`       — push a single case only\n"
        "  `!testrail discard`             — clear the queue without pushing\n"
    )
