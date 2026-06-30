import argparse


def run_options():
    parser = argparse.ArgumentParser(
        description="Playwright + Behave + Allure AI-driven test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_tests.py
  python run_tests.py --headless
  python run_tests.py --browser firefox
  python run_tests.py --env staging
  python run_tests.py --parallel --workers 4
  python run_tests.py --tags @smoke @regression
  python run_tests.py --priority               # run @smoke first, then @regression, etc.
  python run_tests.py --debug                  # verbose logs + page-source on failure
  python run_tests.py --tracing
  python run_tests.py --serve-report
  python run_tests.py features/forms.feature features/api_testing.feature
        """,
    )

    parser.add_argument("--headless", action="store_true",
                        help="Run browser in headless mode")
    parser.add_argument("--browser", choices=["chromium", "firefox", "webkit"],
                        default="chromium", help="Browser engine (default: chromium)")
    parser.add_argument("--env", choices=["dev", "staging", "prod"],
                        default=None, help="Target environment (overrides config.yaml)")
    parser.add_argument("--parallel", action="store_true",
                        help="Run feature files in parallel")
    parser.add_argument("--workers", type=int,
                        help="Number of parallel workers (default: CPU count)")
    parser.add_argument("--tags", nargs="+",
                        help="Tags to run, e.g. --tags @smoke @regression")
    parser.add_argument("--priority", action="store_true",
                        help="Run tag groups in priority order defined in config.yaml "
                             "(smoke → regression → api → performance …)")
    parser.add_argument("--tracing", action="store_true",
                        help="Enable Playwright trace recording")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug mode: verbose logging + page-source on failure")
    parser.add_argument("--serve-report", action="store_true",
                        help="Open Allure report in browser after run")
    parser.add_argument("features", nargs="*",
                        help="Feature files to run (default: all in features/)")

    return parser.parse_args()
