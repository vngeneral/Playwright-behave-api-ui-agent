"""
Step definitions for performance.feature.

Real measurements:
  - Page load time via Playwright's Performance Timing API
  - Memory via psutil (process RSS before/after multi-page navigation)
  - Network request count via Playwright's request interception

Thresholds come from config.yaml so they can be tuned per environment.
"""
import time

import psutil
from behave import step

from utils.logger import log_info_emoji
from utils.misc import load_config

_cfg = load_config()
_LOAD_THRESHOLD_S: float = float(
    _cfg.get("timeouts", {}).get("page_load_threshold_s", 3.0)
)
_MEM_LIMIT_MB: int = 100


# ─────────────────────────────────────────────────────────────────────────────
# Page-load timing (uses Playwright PerformanceTiming API)
# ─────────────────────────────────────────────────────────────────────────────

@step("the user navigates to the homepage for performance test")
def step_navigate_homepage_perf(context):
    url = context.build_url(context.base_url, "")
    wall_start = time.perf_counter()
    context.page.goto(url)
    context.page.wait_for_load_state("load")
    context.wall_load_time = time.perf_counter() - wall_start

    # Also capture browser-reported timing for more precision
    timing = context.page.evaluate("""() => {
        const t = window.performance.timing;
        return {
            dom_interactive: t.domInteractive - t.navigationStart,
            dom_complete:    t.domComplete    - t.navigationStart,
            load_event_end:  t.loadEventEnd  - t.navigationStart
        };
    }""")
    context.browser_timing = timing
    log_info_emoji(
        "⏱️",
        f"Wall-clock load: {context.wall_load_time:.2f}s | "
        f"Browser loadEventEnd: {timing['load_event_end']}ms"
    )


@step("the page finishes loading")
def step_page_finishes_loading(context):
    context.page.wait_for_load_state("networkidle")


@step("the page load time should be under 3 seconds")
def step_verify_load_time(context):
    load_time = getattr(context, "wall_load_time", None)
    assert load_time is not None, "page_load_time was not recorded"
    assert load_time < _LOAD_THRESHOLD_S, (
        f"Page loaded in {load_time:.2f}s — exceeds threshold of {_LOAD_THRESHOLD_S}s"
    )
    log_info_emoji("✅", f"Page load {load_time:.2f}s < {_LOAD_THRESHOLD_S}s threshold")


# ─────────────────────────────────────────────────────────────────────────────
# Memory usage (multi-page navigation)
# ─────────────────────────────────────────────────────────────────────────────

@step("the user opens multiple browser tabs")
def step_open_multiple_tabs(context):
    """
    Open extra pages in the same browser context to simulate tab usage.
    NOTE: 'tabs' here means Playwright pages — not separate OS processes.
    """
    context.initial_memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
    context.extra_pages = []
    urls = [
        context.build_url(context.base_url, "get"),
        context.build_url(context.base_url, "headers"),
        context.build_url(context.base_url, "ip"),
    ]
    for url in urls:
        # Each tab is a new page inside the existing browser context
        p = context.browser_manager._browser_context.new_page()
        p.goto(url)
        p.wait_for_load_state("load")
        context.extra_pages.append(p)
    log_info_emoji("📑", f"Opened {len(context.extra_pages)} extra pages")


@step("the user navigates between tabs")
def step_navigate_between_tabs(context):
    """Switch focus through each open page to simulate user tab-switching."""
    for p in getattr(context, "extra_pages", []):
        p.bring_to_front()
        time.sleep(0.1)
    # Close the extra pages and record final memory
    for p in getattr(context, "extra_pages", []):
        p.close()
    context.extra_pages = []
    context.final_memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
    log_info_emoji(
        "💾",
        f"Memory: {context.initial_memory_mb:.1f} MB → {context.final_memory_mb:.1f} MB"
    )


@step("the memory usage should be reasonable")
def step_verify_memory_usage(context):
    initial = getattr(context, "initial_memory_mb", None)
    final = getattr(context, "final_memory_mb", None)
    assert initial is not None and final is not None, "Memory metrics not recorded"
    increase = final - initial
    assert increase < _MEM_LIMIT_MB, (
        f"Memory grew by {increase:.1f} MB — exceeds limit of {_MEM_LIMIT_MB} MB"
    )
    log_info_emoji("✅", f"Memory increase {increase:.1f} MB < {_MEM_LIMIT_MB} MB limit")


# ─────────────────────────────────────────────────────────────────────────────
# Network request count
# ─────────────────────────────────────────────────────────────────────────────

@step("the page loads all resources")
def step_load_all_resources(context):
    """Navigate to homepage and intercept every network request."""
    context.network_requests = []

    def _capture(request):
        context.network_requests.append(request.url)

    context.page.on("request", _capture)
    context.page.goto(context.build_url(context.base_url, ""))
    context.page.wait_for_load_state("networkidle")
    log_info_emoji("📡", f"Captured {len(context.network_requests)} network request(s)")


@step("the number of network requests should be optimized")
def step_verify_network_requests(context):
    req_count = len(getattr(context, "network_requests", []))
    assert req_count >= 1, "No network requests were captured — page may not have loaded"
    # httpbin homepage is lean; flag if unexpectedly bloated (>50 requests)
    assert req_count <= 50, (
        f"Unusually high request count: {req_count}. "
        "Check for unnecessary resource loading."
    )
    log_info_emoji("✅", f"{req_count} network request(s) — within acceptable range")
