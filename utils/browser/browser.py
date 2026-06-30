import os
import time
from playwright.sync_api import sync_playwright

from helpers.constants.framework_constants import TRACES_VIDEOS_DIR, TRACES_DIR
from utils.misc import load_config, get_env_config
from utils.logger import log_info
from utils.browser.trace_manager import TraceManager


def get_browser_config():
    browser_type = os.getenv("BROWSER", "chromium").lower()
    headless_env = os.getenv("HEADLESS", "False").lower()
    headless = headless_env in ["true", "1", "yes", "on"]
    return browser_type, headless


def get_base_url() -> str:
    config = load_config()
    env_cfg = get_env_config(config)
    base_url = env_cfg.get("base_url")
    if not base_url:
        raise ValueError("base_url is missing from the active environment block in config.yaml")
    return base_url


def build_url(base_url: str, path: str = "") -> str:
    path = path.lstrip("/")
    return f"{base_url}/{path}" if path else base_url


def create_browser_manager() -> "BrowserManager":
    enable_tracing = os.getenv("ENABLE_TRACING", "false").lower() == "true"
    browser_type, headless = get_browser_config()
    return BrowserManager(
        browser_type=browser_type,
        headless=headless,
        enable_tracing=enable_tracing,
    )


def prepare_browser(context):
    """Called once in before_all; stores manager + base config on context."""
    context.browser_manager = create_browser_manager()
    context.browser_manager.launch()          # launches Playwright + browser only
    context.base_url = get_base_url()
    context.build_url = build_url


class BrowserManager:
    """
    Owns the Playwright process and browser.
    Pages/contexts are created per-scenario via new_page() / close_page().
    """

    def __init__(self, browser_type="chromium", headless=False, enable_tracing=False):
        self.playwright = None
        self.browser = None
        self.browser_type = browser_type
        self.headless = headless
        self.enable_tracing = enable_tracing
        self._browser_context = None
        self.trace_manager = TraceManager(self.enable_tracing)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def launch(self):
        """Start Playwright and launch the browser (no page created here)."""
        if self.enable_tracing:
            self.trace_manager.archive_old_traces()
        self.playwright = sync_playwright().start()
        browser_launcher = getattr(self.playwright, self.browser_type)
        self.browser = browser_launcher.launch(headless=self.headless)

    def new_page(self):
        """Open a fresh browser context + page for a scenario."""
        ctx_kwargs = {}
        if self.enable_tracing:
            ctx_kwargs["record_video_dir"] = TRACES_VIDEOS_DIR
            ctx_kwargs["record_har_path"] = f"{TRACES_DIR}/har"
        self._browser_context = self.browser.new_context(**ctx_kwargs)
        if self.enable_tracing:
            self._browser_context.tracing.start(
                screenshots=True, snapshots=True, sources=True
            )
        return self._browser_context.new_page()

    def close_page(self):
        """Stop tracing and close the current browser context."""
        if self.enable_tracing and self._browser_context:
            trace_path = (
                f"{TRACES_DIR}/trace-{self.browser_type}-{int(time.time())}.zip"
            )
            self._browser_context.tracing.stop(path=trace_path)
            log_info(f"Trace saved to: {trace_path}")
            self.trace_manager.cleanup_empty_directories()
        if self._browser_context:
            self._browser_context.close()
            self._browser_context = None

    def stop(self):
        """Tear down browser + Playwright process."""
        self.close_page()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
