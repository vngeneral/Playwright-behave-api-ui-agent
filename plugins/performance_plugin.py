"""
Performance Plugin
==================
Captures per-scenario browser performance metrics and attaches them to
Allure reports as a JSON attachment.

Wired into environment.py:
    perf = PerformancePlugin()
    def before_scenario(context, scenario): perf.before_scenario(context, scenario)
    def after_scenario(context, scenario):  perf.after_scenario(context, scenario)
"""
from __future__ import annotations

import json
import time
from typing import Optional

import psutil

from utils.logger import log_info_emoji


class PerformancePlugin:
    """Collects and attaches per-scenario performance metrics."""

    def before_scenario(self, context, scenario) -> None:
        context._perf_start_wall = time.perf_counter()
        context._perf_start_rss_mb = psutil.Process().memory_info().rss / 1_048_576

    def after_scenario(self, context, scenario) -> None:
        end_wall = time.perf_counter()
        end_rss_mb = psutil.Process().memory_info().rss / 1_048_576

        duration = round(end_wall - getattr(context, "_perf_start_wall", end_wall), 3)
        mem_delta = round(
            end_rss_mb - getattr(context, "_perf_start_rss_mb", end_rss_mb), 2
        )

        browser_timing = self._get_browser_timing(context)

        metrics = {
            "scenario": scenario.name,
            "status": str(scenario.status).split(".")[-1],
            "wall_time_s": duration,
            "memory_delta_mb": mem_delta,
            "browser_timing": browser_timing,
        }
        log_info_emoji(
            "📊",
            f"Perf [{scenario.name[:40]}]: "
            f"{duration}s | Δmem {mem_delta:+.1f} MB"
        )
        self._attach_to_allure(metrics, scenario.name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_browser_timing(context) -> Optional[dict]:
        """Safely extract window.performance.timing from the page."""
        page = getattr(context, "page", None)
        if page is None:
            return None
        try:
            return page.evaluate("""() => {
                const t = window.performance && window.performance.timing;
                if (!t) return null;
                const nav = t.navigationStart;
                return {
                    dom_interactive_ms: t.domInteractive - nav,
                    dom_complete_ms:    t.domComplete    - nav,
                    load_event_end_ms:  t.loadEventEnd  - nav
                };
            }""")
        except Exception:
            return None

    @staticmethod
    def _attach_to_allure(data: dict, scenario_name: str) -> None:
        try:
            import allure
            allure.attach(
                json.dumps(data, indent=2),
                name=f"perf_{scenario_name[:40]}",
                attachment_type=allure.attachment_type.JSON,
            )
        except ImportError:
            pass   # Allure not installed — skip silently
        except Exception:
            pass
