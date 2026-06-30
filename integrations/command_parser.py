"""
Command Parser
==============
Parses chat commands (from Teams or WhatsApp) into structured dicts
that the webhook server converts into run_tests.py subprocess arguments.

Command syntax:
    !run                                     — run all tests
    !run --tags @smoke @regression           — run by tag
    !run --browser firefox                   — specify browser
    !run --env staging                       — target environment
    !run --headless                          — headless mode
    !run --parallel                          — parallel execution
    !run --priority                          — priority-ordered
    !run --tags @smoke --browser chromium --headless --env dev
    !status                                  — show last run metrics
    !help                                    — command reference

Returns None for any message that is not a recognised command.
"""
from __future__ import annotations

import shlex

# ---------------------------------------------------------------------------
# Help text sent back to the requester
# ---------------------------------------------------------------------------

HELP_TEXT = """\
🤖 *Playwright Test Runner — available commands*

`!run`                          — run all feature files
`!run --tags @smoke`            — filter by tag(s)
`!run --browser firefox`        — chromium / firefox / webkit
`!run --env staging`            — dev / staging / prod
`!run --headless`               — headless browser mode
`!run --parallel`               — parallel worker execution
`!run --priority`               — smoke → regression → api order
`!status`                       — last run pass/fail summary
`!help`                         — show this message

Multiple flags may be combined:
`!run --tags @smoke --browser chromium --headless --env staging`
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class CommandParseError(ValueError):
    """Raised when a command is recognised but its arguments are invalid."""


def parse_command(text: str) -> dict | None:
    """
    Parse a raw chat message into a command dict, or return None.

    Returned dict shape:
        {"action": "help"}
        {"action": "status"}
        {"action": "run", "tags": [...], "browser": str, "env": str,
         "headless": bool, "parallel": bool, "priority": bool}
    """
    text = text.strip()
    if not text.startswith("!"):
        return None

    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise CommandParseError(f"Malformed command (unmatched quote?): {exc}") from exc

    if not parts:
        return None

    command = parts[0].lower()

    if command == "!help":
        return {"action": "help"}

    if command == "!status":
        return {"action": "status"}

    if command == "!run":
        return _parse_run_args(parts[1:])

    # Unknown command — treat as non-command so the server ignores it gracefully
    return None


def command_to_argv(cmd: dict) -> list[str]:
    """
    Convert a parsed ``!run`` command dict into argv for run_tests.py.

    Example:
        {"action": "run", "tags": ["@smoke"], "browser": "firefox", "headless": True}
        → ["--tags", "@smoke", "--browser", "firefox", "--headless"]
    """
    if cmd.get("action") != "run":
        raise CommandParseError("command_to_argv only accepts action='run'")

    argv: list[str] = []

    for tag in cmd.get("tags") or []:
        argv.extend(["--tags", tag])

    if cmd.get("browser"):
        argv.extend(["--browser", cmd["browser"]])

    if cmd.get("env"):
        argv.extend(["--env", cmd["env"]])

    if cmd.get("headless"):
        argv.append("--headless")

    if cmd.get("parallel"):
        argv.append("--parallel")

    if cmd.get("priority"):
        argv.append("--priority")

    return argv


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_VALID_BROWSERS = {"chromium", "firefox", "webkit"}
_VALID_ENVS = {"dev", "staging", "prod"}


def _parse_run_args(args: list[str]) -> dict:
    """Mini arg-parser for the ``!run`` sub-command."""
    result: dict = {
        "action": "run",
        "tags": [],
        "browser": None,
        "env": None,
        "headless": False,
        "parallel": False,
        "priority": False,
    }

    i = 0
    while i < len(args):
        token = args[i].lower()

        if token == "--tags":
            # Consume all following tokens that look like tags (start with @ or are a word)
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                tag = args[i] if args[i].startswith("@") else f"@{args[i]}"
                result["tags"].append(tag)
                i += 1

        elif token == "--browser":
            i += 1
            if i >= len(args):
                raise CommandParseError("--browser requires a value (chromium / firefox / webkit)")
            browser = args[i].lower()
            if browser not in _VALID_BROWSERS:
                raise CommandParseError(
                    f"Unknown browser '{browser}'. Choose from: {', '.join(_VALID_BROWSERS)}"
                )
            result["browser"] = browser
            i += 1

        elif token == "--env":
            i += 1
            if i >= len(args):
                raise CommandParseError("--env requires a value (dev / staging / prod)")
            env = args[i].lower()
            if env not in _VALID_ENVS:
                raise CommandParseError(
                    f"Unknown environment '{env}'. Choose from: {', '.join(_VALID_ENVS)}"
                )
            result["env"] = env
            i += 1

        elif token == "--headless":
            result["headless"] = True
            i += 1

        elif token == "--parallel":
            result["parallel"] = True
            i += 1

        elif token == "--priority":
            result["priority"] = True
            i += 1

        else:
            raise CommandParseError(
                f"Unknown option '{args[i]}'. Send `!help` for usage."
            )

    return result
