"""
E2E Test Runner
===============
Entry point for the QA team.  Mirrors the root run_tests.py but resolves
paths relative to e2e/ so features/, steps/, and pages/ are found correctly.

Usage (from repo root OR from e2e/):
    python e2e/run_tests.py
    python e2e/run_tests.py --tags @smoke --browser firefox --env staging --headless
    python e2e/run_tests.py --priority
    python e2e/run_tests.py --parallel --tags @api
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
# Make both e2e/ and repo root importable so `from utils.logger import …`
# (root) and `from utils.api.vehicle_client import …` (e2e/) both resolve.
# _ROOT_DIR is inserted first so _E2E_DIR ends up at sys.path[0] — e2e/'s own
# packages must win over any same-named package elsewhere.
_E2E_DIR  = Path(__file__).parent.resolve()
_ROOT_DIR = _E2E_DIR.parent.resolve()
for _p in (_ROOT_DIR, _E2E_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ── Shared utilities (available after path setup) ─────────────────────────
from utils.misc import load_config
from utils.logger import log_info_emoji


# ---------------------------------------------------------------------------
# Priority tag order — smoke first, then regression → api → performance
# ---------------------------------------------------------------------------
PRIORITY_ORDER = ["@smoke", "@regression", "@api", "@performance"]


def build_behave_cmd(
    *,
    targets: list[str],
    tags: str | None,
    browser: str | None,
    env: str | None,
    headless: bool,
    allure_dir: Path,
    parallel: bool,
    workers: int,
) -> list[str]:
    cmd = ["behave", *targets]

    if tags:
        cmd += ["--tags", tags]

    # Allure formatter
    cmd += [
        "--no-capture",
        "-f", "allure_behave.formatter:AllureFormatter",
        "-o", str(allure_dir),
        "-f", "pretty",
    ]

    # Pass options via env so environment.py + behave.ini pick them up
    if browser:
        os.environ["BROWSER"] = browser
    if env:
        os.environ["ENV"] = env
    if headless:
        os.environ["HEADLESS"] = "true"

    return cmd


def _resolve_targets(features: list[str]) -> list[str]:
    """
    Resolve CLI-provided feature paths to absolute paths (so they work
    regardless of the subprocess cwd), or default to the whole features dir.
    """
    if not features:
        return [str(_E2E_DIR / "features")]
    return [str(Path(f).resolve()) for f in features]


def run_tag(tag: str, args: argparse.Namespace, allure_dir: Path) -> int:
    """Run behave for a single tag.  Returns exit code."""
    cmd = build_behave_cmd(
        targets=_resolve_targets(args.features),
        tags=tag,
        browser=args.browser,
        env=args.env,
        headless=args.headless,
        allure_dir=allure_dir,
        parallel=args.parallel,
        workers=args.workers,
    )
    log_info_emoji("🏃", f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(_E2E_DIR))
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E test runner")
    parser.add_argument("--tags",     help="Behave tag expression, e.g. @smoke or @api and not @wip")
    parser.add_argument("--browser",  help="Browser: chromium | firefox | webkit")
    parser.add_argument("--env",      help="Environment: dev | staging | prod")
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument("--priority", action="store_true", default=False,
                        help="Run smoke → regression → api → performance in order")
    parser.add_argument("--parallel", action="store_true", default=False,
                        help="Run scenarios in parallel (requires behavex or pytest-bdd-parallel)")
    parser.add_argument("--workers",  type=int, default=4,
                        help="Parallel worker count (default: 4)")
    parser.add_argument("features", nargs="*",
                        help="Specific feature files to run (default: all under e2e/features/)")
    args = parser.parse_args()

    allure_dir = _ROOT_DIR / "allure-results"
    allure_dir.mkdir(parents=True, exist_ok=True)

    if args.priority:
        log_info_emoji("🔢", "Priority mode: smoke → regression → api → performance")
        exit_codes: list[int] = []
        for tag in PRIORITY_ORDER:
            rc = run_tag(tag, args, allure_dir)
            exit_codes.append(rc)
        return max(exit_codes)

    # Single run
    cmd = build_behave_cmd(
        targets=_resolve_targets(args.features),
        tags=args.tags,
        browser=args.browser,
        env=args.env,
        headless=args.headless,
        allure_dir=allure_dir,
        parallel=args.parallel,
        workers=args.workers,
    )
    log_info_emoji("🏃", f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(_E2E_DIR))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
