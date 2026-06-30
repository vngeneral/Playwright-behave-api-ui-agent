"""
Test Runner
===========
Entry point for the framework.  Supports sequential and parallel execution,
tag filtering, priority-ordered runs, debug mode, and Allure reporting.

Usage:
    python run_tests.py [options] [feature_files...]

Run python run_tests.py --help for full option list.
"""
import os
import re
import sys
import subprocess
import multiprocessing
from pathlib import Path

from helpers.constants.framework_constants import TRACES_DIR, ALLURE_RESULTS_DIR
from helpers.file_system import create_reports_structure
from utils.misc import load_config
from utils.preparation import run_options
from utils.logger import (
    log_info, log_warning, log_success, log_failure, log_info_emoji,
)
from utils.reporting import combine_allure_reports, server_report


# ---------------------------------------------------------------------------
# Feature-file distribution
# ---------------------------------------------------------------------------

def distribute_features(feature_files: list, max_workers: int) -> list[list]:
    """Round-robin distribution of feature files across workers."""
    buckets: list[list] = [[] for _ in range(max_workers)]
    for i, f in enumerate(feature_files):
        buckets[i % max_workers].append(f)
    return buckets


def _tag_pattern(tag: str) -> re.Pattern:
    """
    Word-boundary-aware regex so @smoke does NOT match @smoketest.
    Accepts tags with or without leading '@'.
    """
    raw = tag.lstrip("@")
    return re.compile(rf"@{re.escape(raw)}(?!\w)")


def filter_features_by_tags(feature_files: list, tags: list[str]) -> list:
    if not tags:
        return feature_files
    patterns = [_tag_pattern(t) for t in tags]
    relevant = []
    for f in feature_files:
        try:
            content = Path(f).read_text(encoding="utf-8")
            if any(p.search(content) for p in patterns):
                relevant.append(f)
        except Exception as exc:
            log_warning(f"Could not read {f}: {exc} — including it anyway")
            relevant.append(f)
    return relevant


def sort_by_priority(feature_files: list, priority_tags: list[str]) -> list:
    """
    Sort feature files so those tagged with earlier priority tags come first.
    Files with no matching priority tag go to the end.

    Example: priority_tags = ["smoke", "regression", "api"]
      → smoke features first, then regression, then api, then rest
    """
    if not priority_tags:
        return feature_files

    def _priority(f):
        try:
            content = Path(f).read_text(encoding="utf-8")
        except Exception:
            return len(priority_tags)
        for rank, tag in enumerate(priority_tags):
            if _tag_pattern(tag).search(content):
                return rank
        return len(priority_tags)   # lowest priority = last

    return sorted(feature_files, key=_priority)


# ---------------------------------------------------------------------------
# Worker (used by parallel mode)
# ---------------------------------------------------------------------------

def run_worker_features(feature_files: list, report_dir: str, tags=None) -> dict:
    worker_id = report_dir.split("_")[-1]
    try:
        cmd = [
            sys.executable, "-m", "behave",
            "-f", "allure_behave.formatter:AllureFormatter",
            "-o", report_dir,
            "--no-capture",
            "--no-capture-stderr",
        ]
        if tags:
            for tag in tags:
                cmd.extend(["-t", tag.lstrip("@")])
        cmd += [str(f) for f in feature_files]

        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        output_lines = []
        for line in process.stdout:
            if line.strip():
                output_lines.append(line)
                log_info(line.rstrip())
        exit_code = process.wait(timeout=600)

        return {
            "worker_id": worker_id,
            "exit_code": exit_code,
            "stdout": "\n".join(output_lines),
            "stderr": "",
            "error": None if exit_code == 0 else f"Worker {worker_id} exited {exit_code}",
        }
    except subprocess.TimeoutExpired:
        msg = "Timed out after 10 minutes"
        return {"worker_id": worker_id, "exit_code": 1, "stdout": "", "stderr": msg, "error": msg}
    except Exception as exc:
        return {"worker_id": worker_id, "exit_code": 1, "stdout": "", "stderr": str(exc), "error": str(exc)}


# ---------------------------------------------------------------------------
# Parallel runner
# ---------------------------------------------------------------------------

def run_behave_parallel(
    feature_files: list,
    max_workers: int = None,
    tags: list[str] = None,
    priority: bool = False,
) -> bool:
    if tags:
        feature_files = filter_features_by_tags(feature_files, tags)
        log_info_emoji("📁", f"{len(feature_files)} feature(s) matched tags: {tags}")

    if not feature_files:
        log_warning("No feature files matched the specified tags.")
        return True

    if priority:
        cfg = load_config()
        ptags = cfg.get("parallel", {}).get("priority_tags", [])
        feature_files = sort_by_priority(feature_files, ptags)
        log_info_emoji("🔢", f"Priority order applied: {ptags}")

    if max_workers is None:
        max_workers = min(multiprocessing.cpu_count(), len(feature_files))
    actual_workers = min(max_workers, len(feature_files))
    log_info_emoji("🚀", f"{len(feature_files)} feature(s) → {actual_workers} parallel worker(s)")
    log_info("=" * 50)

    report_dirs = []
    for i in range(actual_workers):
        rd = f"reports/workers/worker_{i}"
        os.makedirs(rd, exist_ok=True)
        report_dirs.append(rd)

    buckets = distribute_features(feature_files, actual_workers)
    with multiprocessing.Pool(processes=actual_workers) as pool:
        results = pool.starmap(
            run_worker_features,
            [(bucket, report_dirs[i], tags) for i, bucket in enumerate(buckets) if bucket],
        )

    combine_allure_reports(report_dirs)
    log_info("=" * 50)
    return all(r["exit_code"] == 0 for r in results)


# ---------------------------------------------------------------------------
# Sequential runner
# ---------------------------------------------------------------------------

def run_behave_command(args) -> object:
    cmd = [sys.executable, "-m", "behave",
           "-f", "allure_behave.formatter:AllureFormatter",
           "-o", ALLURE_RESULTS_DIR]

    if args.features:
        cmd.extend(args.features)

    if args.tags:
        for tag in args.tags:
            cmd.extend(["-t", tag.lstrip("@")])

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    output_lines = []
    for line in process.stdout:
        if line.strip():
            output_lines.append(line)
            log_info(line.rstrip())
    exit_code = process.wait(timeout=600)
    return type("Result", (), {"returncode": exit_code, "stdout": "\n".join(output_lines)})()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = run_options()

    # ── Environment variables for subprocesses ────────────────────────────
    os.environ["HEADLESS"] = "True" if args.headless else "False"
    log_info_emoji("🌐", f"Headless: {os.environ['HEADLESS']}")

    os.environ["BROWSER"] = args.browser
    log_info_emoji("🌐", f"Browser: {args.browser.capitalize()}")

    if args.env:
        os.environ["ENV"] = args.env
        log_info_emoji("🌍", f"Environment: {args.env}")

    if args.tracing:
        os.environ["ENABLE_TRACING"] = "true"
        log_info_emoji("🎬", f"Tracing enabled → {TRACES_DIR}")

    if args.debug:
        os.environ["DEBUG"] = "true"
        log_info_emoji("🐛", "Debug mode ON")

    if args.priority:
        log_info_emoji("🔢", "Test prioritisation enabled")

    # ── Setup ─────────────────────────────────────────────────────────────
    create_reports_structure()

    features_dir = Path("features")
    if not features_dir.exists():
        log_failure("'features' directory not found!")
        sys.exit(1)

    if args.features:
        feature_files = [Path(f) for f in args.features]
        log_info_emoji("📁", f"Running specified: {args.features}")
    else:
        feature_files = list(features_dir.glob("*.feature"))
        if not feature_files:
            log_failure("No .feature files found in 'features/'!")
            sys.exit(1)
        log_info_emoji("📁", f"Found {len(feature_files)} feature file(s)")

    # Priority sort in sequential mode
    if args.priority and not args.parallel:
        cfg = load_config()
        ptags = cfg.get("parallel", {}).get("priority_tags", [])
        feature_files = sort_by_priority(feature_files, ptags)
        log_info_emoji("🔢", "Features sorted by priority tags")

    # ── Execution ─────────────────────────────────────────────────────────
    if args.parallel:
        log_info_emoji("🔄", "Parallel mode")
        max_workers = args.workers or min(multiprocessing.cpu_count(), len(feature_files))
        log_info_emoji("👥", f"Workers: {max_workers}")
        success = run_behave_parallel(
            feature_files, max_workers, tags=args.tags, priority=args.priority
        )
        result = type("Result", (), {"returncode": 0 if success else 1})()
    else:
        log_info_emoji("🔄", "Sequential mode")
        log_info("=" * 50)
        result = run_behave_command(args)

    # ── Results ───────────────────────────────────────────────────────────
    if result.returncode == 0:
        log_success("All tests passed!")
    else:
        log_failure("Some tests failed!")

    server_report(args)
    if result.returncode != 0:
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
