"""
TestRail Case Sync
==================
Closes the loop between AI-generated feature files and TestRail:

    AITestGenerator writes a .feature file (no @testrail_C tags — the cases
    don't exist yet) → a human reviews the Gherkin → case_sync creates one
    TestRail case per untagged scenario → the real @testrail_C<id> tags are
    written back into the file → from then on, the existing after_scenario
    hook queues results and `!testrail push` submits them after review.

Like result pushing, case creation is **never automatic** — it only happens
when a human explicitly runs this module (or passes --testrail-section to
the test generator CLI) after reviewing the generated Gherkin.

Already-tagged scenarios are skipped, so re-running the sync on the same
file is idempotent and never creates duplicate cases.

Environment variables (required at sync time):
    TESTRAIL_URL, TESTRAIL_USER, TESTRAIL_API_KEY  — TestRail credentials
    TESTRAIL_SECTION_ID                            — default section (or --section-id)

Usage (standalone CLI):
    python -m agent.testrail.case_sync \\
        --feature e2e/features/ai_generated_vehicle_register.feature \\
        --section-id 42

    # Preview what would be created, without calling TestRail or editing the file
    python -m agent.testrail.case_sync \\
        --feature e2e/features/ai_generated_vehicle_register.feature \\
        --section-id 42 --dry-run

Usage (programmatic):
    from agent.testrail.case_sync import sync_feature_file
    report = sync_feature_file("e2e/features/x.feature", section_id=42)
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.testrail.client import TestRailAPIError, TestRailClient
from agent.testrail.result_mapper import TESTRAIL_TAG_PREFIX
from utils.logger import log_failure, log_info_emoji, log_success, log_warning

# Matches "Scenario:" and "Scenario Outline:" lines, capturing the title
_SCENARIO_RE = re.compile(r"^\s*Scenario(?: Outline)?:\s*(.+?)\s*$")

# Keywords that end a scenario body when scanning forward
_BLOCK_KEYWORD_RE = re.compile(
    r"^\s*(?:Feature:|Background:|Rule:|Scenario:|Scenario Outline:|@)"
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ScenarioRef:
    """One scenario found in a feature file."""
    line_idx: int          # 0-based index of the Scenario:/Scenario Outline: line
    title: str
    tags: list[str]        # tags attached to this scenario (without '@')
    body: str = ""         # step lines (plain text) for the TestRail case

    @property
    def case_ids(self) -> list[str]:
        """TestRail case IDs already linked via @testrail_C<id> tags."""
        return [
            t[len(TESTRAIL_TAG_PREFIX):]
            for t in self.tags
            if t.startswith(TESTRAIL_TAG_PREFIX)
        ]


@dataclass
class SyncReport:
    """Outcome of one sync_feature_file() call."""
    feature_path: str
    created: list[dict] = field(default_factory=list)    # {"title", "case_id"}
    skipped: list[str] = field(default_factory=list)     # already-tagged titles
    failed: list[dict] = field(default_factory=list)     # {"title", "error"}
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Pure parsing helpers — no I/O, no network (unit-testable in isolation)
# ---------------------------------------------------------------------------


def find_scenarios(feature_text: str) -> list[ScenarioRef]:
    """
    Parse feature text and return every Scenario / Scenario Outline with its
    attached tags and step body.

    Tags are collected from contiguous tag/comment/blank lines directly above
    the scenario keyword (standard Gherkin placement).
    """
    lines = feature_text.splitlines()
    scenarios: list[ScenarioRef] = []

    for idx, line in enumerate(lines):
        m = _SCENARIO_RE.match(line)
        if not m:
            continue
        scenarios.append(ScenarioRef(
            line_idx=idx,
            title=m.group(1),
            tags=_collect_tags_above(lines, idx),
            body=_collect_body(lines, idx),
        ))

    return scenarios


def insert_case_tags(feature_text: str, tags_by_line: dict[int, str]) -> str:
    """
    Insert a ``@testrail_C<id>`` tag line above each scenario.

    Args:
        feature_text:  The original feature file content.
        tags_by_line:  {scenario line_idx: case_id} — line indexes must come
                       from find_scenarios() on this same text.

    Returns:
        The updated feature text. All other lines are preserved byte-for-byte.
    """
    lines = feature_text.splitlines(keepends=True)
    # Insert bottom-up so earlier indexes stay valid
    for line_idx in sorted(tags_by_line, reverse=True):
        case_id = tags_by_line[line_idx]
        scenario_line = lines[line_idx]
        indent = scenario_line[: len(scenario_line) - len(scenario_line.lstrip())]
        newline = "\r\n" if scenario_line.endswith("\r\n") else "\n"
        lines.insert(line_idx, f"{indent}@{TESTRAIL_TAG_PREFIX}{case_id}{newline}")
    return "".join(lines)


def _collect_tags_above(lines: list[str], scenario_idx: int) -> list[str]:
    """Collect tags from contiguous tag/comment/blank lines above a scenario."""
    tags: list[str] = []
    i = scenario_idx - 1
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("@"):
            tags.extend(t.lstrip("@") for t in stripped.split() if t.startswith("@"))
        elif stripped and not stripped.startswith("#"):
            break
        i -= 1
    return tags


def _collect_body(lines: list[str], scenario_idx: int) -> str:
    """
    Collect the step lines of a scenario (including Examples tables for
    outlines) as plain text — used as the TestRail case's steps field.
    """
    body: list[str] = []
    for line in lines[scenario_idx + 1:]:
        if _BLOCK_KEYWORD_RE.match(line):
            break
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            body.append(stripped)
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------


def sync_feature_file(
    feature_path: str,
    section_id: int,
    dry_run: bool = False,
    client: TestRailClient | None = None,
) -> SyncReport:
    """
    Create a TestRail case for every untagged scenario in *feature_path* and
    write the resulting ``@testrail_C<id>`` tags back into the file.

    Scenarios that already carry a ``@testrail_C<id>`` tag are skipped, so the
    operation is idempotent. If some case creations fail, the successful ones
    are still tagged and the failures are listed in the returned report.

    Args:
        feature_path:  Path to the .feature file to sync.
        section_id:    TestRail section to create cases under.
        dry_run:       If True, report what would be created — no TestRail
                       calls, no file edits.
        client:        Injected TestRailClient (tests); defaults to from_env().

    Returns:
        A SyncReport listing created / skipped / failed scenarios.
    """
    path = Path(feature_path)
    if not path.is_file():
        raise FileNotFoundError(f"Feature file not found: {feature_path}")

    text = path.read_text(encoding="utf-8")
    scenarios = find_scenarios(text)
    report = SyncReport(feature_path=str(path), dry_run=dry_run)

    untagged = []
    for ref in scenarios:
        if ref.case_ids:
            report.skipped.append(ref.title)
            log_info_emoji("⏭️", f"Already linked (C{ref.case_ids[0]}): {ref.title}")
        else:
            untagged.append(ref)

    if not untagged:
        log_info_emoji("ℹ️", "No untagged scenarios — nothing to sync")
        return report

    if dry_run:
        for ref in untagged:
            report.created.append({"title": ref.title, "case_id": None})
            log_info_emoji("🔍", f"[dry-run] Would create case: {ref.title}")
        return report

    client = client or TestRailClient.from_env()
    tags_by_line: dict[int, str] = {}

    for ref in untagged:
        try:
            response = client.add_case(
                section_id=section_id,
                title=ref.title,
                custom_steps=ref.body or None,
            )
            case_id = str(response["id"])
        except TestRailAPIError as exc:
            report.failed.append({"title": ref.title, "error": str(exc)})
            log_warning(f"Case creation failed for {ref.title!r}: {exc}")
            continue
        except (KeyError, TypeError) as exc:
            report.failed.append({"title": ref.title, "error": f"Unexpected response: {exc}"})
            log_warning(f"Unexpected add_case response for {ref.title!r}: {exc}")
            continue

        tags_by_line[ref.line_idx] = case_id
        report.created.append({"title": ref.title, "case_id": case_id})
        log_success(f"Created TestRail case C{case_id}: {ref.title}")

    if tags_by_line:
        path.write_text(insert_case_tags(text, tags_by_line), encoding="utf-8")
        log_success(f"Tagged {len(tags_by_line)} scenario(s) in {path}")

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args():
    p = argparse.ArgumentParser(
        description="Create TestRail cases for untagged scenarios in a feature "
                     "file and write @testrail_C<id> tags back"
    )
    p.add_argument("--feature", required=True, help="Path to the .feature file")
    p.add_argument("--section-id", type=int, default=None,
                   help="TestRail section ID (default: TESTRAIL_SECTION_ID env var)")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview what would be created — no TestRail calls, no file edits")
    args = p.parse_args()
    if args.section_id is None:
        args.section_id = TestRailClient.default_section_id()
    if args.section_id is None and not args.dry_run:
        p.error("--section-id is required (or set TESTRAIL_SECTION_ID)")
    return args


if __name__ == "__main__":
    args = _parse_args()
    try:
        report = sync_feature_file(
            args.feature, section_id=args.section_id or 0, dry_run=args.dry_run
        )
    except Exception as exc:
        log_failure(f"Case sync failed: {exc}")
        raise SystemExit(1) from exc

    log_info_emoji(
        "📊",
        f"Sync summary — created: {len(report.created)}, "
        f"skipped (already linked): {len(report.skipped)}, "
        f"failed: {len(report.failed)}",
    )
    if not report.ok:
        raise SystemExit(1)
