"""
TestRail Case Sync
==================
Closes the loop between AI-generated feature files and TestRail:

    AITestGenerator writes a .feature file (no @testrail_C tags — the cases
    don't exist yet) → a human reviews the Gherkin → case_sync creates one
    TestRail case per untagged scenario (or links to an existing one — see
    below) → the real @testrail_C<id> tags are written back into the file →
    from then on, the existing after_scenario hook queues results and
    `!testrail push` submits them after review.

Case creation is **never automatic** — it only happens when a human
explicitly runs this module after reviewing the generated Gherkin.

Two things prevent scenarios from being uploaded to TestRail more than once:
    1. Already-tagged scenarios are skipped, so re-running the sync on the
       same file is idempotent.
    2. Before creating a case, every untagged scenario's title is checked
       against TestRail's existing cases in the target section
       (`TestRailClient.get_cases`) — TestRail itself does not deduplicate
       by title, so without this check, re-syncing a renamed/regenerated
       file (or one that happens to share a scenario title with a case
       created elsewhere) would silently create a duplicate case. A title
       match is **linked** (tagged with the existing case_id) instead of
       creating a new one.

The feature file itself is only ever updated by an atomic write (temp file +
rename) — a crash or interrupt mid-sync can't leave it half-written.

Environment variables (required at sync time, unless --dry-run):
    TESTRAIL_URL, TESTRAIL_USER, TESTRAIL_API_KEY  — TestRail credentials
    TESTRAIL_SECTION_ID                            — default section (or --section-id)
    TESTRAIL_PROJECT_ID                            — default project (or --project-id)
    TESTRAIL_SUITE_ID                              — only for multi-suite projects (or --suite-id)

Usage (standalone CLI, single file):
    python -m agent.testrail.case_sync \\
        --feature e2e/features/ai_generated_vehicle_register.feature \\
        --section-id 42 --project-id 7

    # Preview what would be created/linked, without calling TestRail or editing the file
    python -m agent.testrail.case_sync \\
        --feature e2e/features/ai_generated_vehicle_register.feature \\
        --section-id 42 --project-id 7 --dry-run

Usage (standalone CLI, whole folder):
    Pass a folder to --feature instead of a single file and every
    `*.feature` file directly inside it is synced in one command, in
    filename order. A failure on one file (bad file, TestRail error) is
    recorded against that file and does not stop the rest of the batch.

    python -m agent.testrail.case_sync \\
        --feature e2e/features/ai_generated \\
        --section-id 42 --project-id 7

Usage (programmatic):
    from agent.testrail.case_sync import sync_feature_file, sync_feature_dir
    report  = sync_feature_file("e2e/features/x.feature", section_id=42, project_id=7)
    reports = sync_feature_dir("e2e/features/ai_generated", section_id=42, project_id=7)
"""
from __future__ import annotations

import argparse
import os
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
    created: list[dict] = field(default_factory=list)    # {"title", "case_id"} — new case
    linked: list[dict] = field(default_factory=list)     # {"title", "case_id"} — matched existing case
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


def _existing_case_titles(
    client: TestRailClient, project_id: int, section_id: int, suite_id: int | None
) -> dict[str, str]:
    """{normalised title: case_id} for every case already in *section_id*."""
    cases = client.get_cases(project_id=project_id, section_id=section_id, suite_id=suite_id)
    return {c["title"].strip().lower(): str(c["id"]) for c in cases if c.get("title")}


def _write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* via temp file + rename — never leaves a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def sync_feature_file(
    feature_path: str,
    section_id: int,
    project_id: int | None = None,
    suite_id: int | None = None,
    dry_run: bool = False,
    client: TestRailClient | None = None,
) -> SyncReport:
    """
    Create a TestRail case for every untagged scenario in *feature_path* and
    write the resulting ``@testrail_C<id>`` tags back into the file.

    Scenarios that already carry a ``@testrail_C<id>`` tag are skipped, so the
    operation is idempotent. Among the remaining untagged scenarios, any whose
    title matches an existing case in *section_id* is **linked** to that case
    instead of creating a duplicate — TestRail's add_case does not deduplicate
    by title on its own. If some case creations fail, the successful ones are
    still tagged and the failures are listed in the returned report. The file
    is only ever updated by an atomic write (temp file + rename).

    Args:
        feature_path:  Path to the .feature file to sync.
        section_id:    TestRail section to create cases under.
        project_id:    TestRail project — required (unless dry_run) to run the
                       duplicate-case check; defaults to TESTRAIL_PROJECT_ID.
        suite_id:      TestRail suite — only needed for multi-suite projects;
                       defaults to TESTRAIL_SUITE_ID.
        dry_run:       If True, report what would be created — no TestRail
                       calls, no file edits.
        client:        Injected TestRailClient (tests); defaults to from_env().

    Returns:
        A SyncReport listing created / linked / skipped / failed scenarios.
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
    project_id = project_id if project_id is not None else TestRailClient.default_project_id()
    if project_id is None:
        raise ValueError(
            "project_id is required (or set TESTRAIL_PROJECT_ID) — it's needed to "
            "check for an existing case before creating a new one, so a re-synced "
            "or renamed feature file never uploads a duplicate case"
        )
    suite_id = suite_id if suite_id is not None else TestRailClient.default_suite_id()

    existing_titles = _existing_case_titles(client, project_id, section_id, suite_id)
    tags_by_line: dict[int, str] = {}

    for ref in untagged:
        title_key = ref.title.strip().lower()
        existing_id = existing_titles.get(title_key)
        if existing_id:
            tags_by_line[ref.line_idx] = existing_id
            report.linked.append({"title": ref.title, "case_id": existing_id})
            log_info_emoji("🔗", f"Linked to existing case C{existing_id}: {ref.title}")
            continue

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
        existing_titles[title_key] = case_id   # dedup against the rest of this same run
        report.created.append({"title": ref.title, "case_id": case_id})
        log_success(f"Created TestRail case C{case_id}: {ref.title}")

    if tags_by_line:
        _write_atomic(path, insert_case_tags(text, tags_by_line))
        log_success(f"Tagged {len(tags_by_line)} scenario(s) in {path}")

    return report


def sync_feature_dir(
    dir_path: str,
    section_id: int,
    project_id: int | None = None,
    suite_id: int | None = None,
    dry_run: bool = False,
    client: TestRailClient | None = None,
) -> list[SyncReport]:
    """
    Sync every ``*.feature`` file directly inside *dir_path* — see
    sync_feature_file() for the per-file behaviour, which applies identically
    to each one. Files are processed in filename order using one shared
    TestRailClient, so a case created while syncing an earlier file is already
    visible to the duplicate-case check for the next one — two files sharing a
    scenario title link to a single case instead of creating two.

    A failure syncing one file (a TestRail error, an unreadable file) is
    recorded on that file's SyncReport and does not stop the rest of the batch.

    Args:
        dir_path:    Folder containing the .feature files to sync (not recursive).
        Remaining args are identical to sync_feature_file().

    Returns:
        One SyncReport per .feature file found, in filename order.
    """
    in_dir = Path(dir_path)
    if not in_dir.is_dir():
        raise NotADirectoryError(f"Feature directory not found: {dir_path}")

    files = sorted(in_dir.glob("*.feature"))
    if not files:
        log_warning(f"No .feature files found in {dir_path}")
        return []

    if not dry_run:
        client = client or TestRailClient.from_env()

    log_info_emoji("📂", f"Batch syncing {len(files)} feature file(s) from {dir_path}")
    reports: list[SyncReport] = []
    for f in files:
        try:
            report = sync_feature_file(
                str(f), section_id=section_id, project_id=project_id,
                suite_id=suite_id, dry_run=dry_run, client=client,
            )
        except Exception as exc:
            log_failure(f"[{f.name}] sync failed: {exc}")
            report = SyncReport(
                feature_path=str(f), failed=[{"title": "(whole file)", "error": str(exc)}],
                dry_run=dry_run,
            )
        reports.append(report)

    created = sum(len(r.created) for r in reports)
    linked  = sum(len(r.linked) for r in reports)
    skipped = sum(len(r.skipped) for r in reports)
    failed  = sum(len(r.failed) for r in reports)
    log_info_emoji(
        "📊",
        f"Batch sync summary — created: {created}, linked: {linked}, "
        f"skipped: {skipped}, failed: {failed}",
    )
    return reports


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args():
    p = argparse.ArgumentParser(
        description="Create TestRail cases for untagged scenarios in a feature "
                     "file (or every feature file in a folder) and write "
                     "@testrail_C<id> tags back"
    )
    p.add_argument("--feature", required=True, metavar="FILE_OR_DIR",
                    help="Path to a .feature file, or a folder of .feature files "
                         "to sync in one command")
    p.add_argument("--section-id", type=int, default=None,
                   help="TestRail section ID (default: TESTRAIL_SECTION_ID env var)")
    p.add_argument("--project-id", type=int, default=None,
                   help="TestRail project ID (default: TESTRAIL_PROJECT_ID env var) — "
                        "used to check for an existing case with the same title "
                        "before creating one, so re-syncing never uploads a duplicate")
    p.add_argument("--suite-id", type=int, default=None,
                   help="TestRail suite ID — only needed for multi-suite projects "
                        "(default: TESTRAIL_SUITE_ID env var)")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview what would be created — no TestRail calls, no file edits")
    args = p.parse_args()
    if args.section_id is None:
        args.section_id = TestRailClient.default_section_id()
    if args.section_id is None and not args.dry_run:
        p.error("--section-id is required (or set TESTRAIL_SECTION_ID)")
    if args.project_id is None:
        args.project_id = TestRailClient.default_project_id()
    if args.project_id is None and not args.dry_run:
        p.error("--project-id is required (or set TESTRAIL_PROJECT_ID)")
    if args.suite_id is None:
        args.suite_id = TestRailClient.default_suite_id()
    return args


if __name__ == "__main__":
    args = _parse_args()
    try:
        path = Path(args.feature)
        if path.is_dir():
            reports = sync_feature_dir(
                str(path), section_id=args.section_id or 0, project_id=args.project_id,
                suite_id=args.suite_id, dry_run=args.dry_run,
            )
        else:
            reports = [sync_feature_file(
                str(path), section_id=args.section_id or 0, project_id=args.project_id,
                suite_id=args.suite_id, dry_run=args.dry_run,
            )]
    except Exception as exc:
        log_failure(f"Case sync failed: {exc}")
        raise SystemExit(1) from exc

    log_info_emoji(
        "📊",
        f"Sync summary — created: {sum(len(r.created) for r in reports)}, "
        f"linked (already existed): {sum(len(r.linked) for r in reports)}, "
        f"skipped (already tagged): {sum(len(r.skipped) for r in reports)}, "
        f"failed: {sum(len(r.failed) for r in reports)}",
    )
    if not all(r.ok for r in reports):
        raise SystemExit(1)
