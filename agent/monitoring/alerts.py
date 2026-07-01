"""
Alert Manager
=============
Sends run-summary notifications after a Behave run.

Design decisions (correcting the original suggestion):
  ✗ WRONG:  Reporting.SLACK_WEBHOOK = "https://hooks.slack.com/..."  (hardcoded secret)
  ✗ WRONG:  Reporting.EMAIL_RECIPIENTS = ["team@company.com"]         (hardcoded PII)
  ✓ RIGHT:  Read webhook URL from SLACK_WEBHOOK_URL env var
  ✓ RIGHT:  Delegate email to CI/CD (GitHub Actions, GitLab, Jenkins)

Usage:
    from monitoring.alerts import AlertManager
    alerts = AlertManager()
    alerts.notify(metrics)   # fires if configured and thresholds met

Environment variables:
    SLACK_WEBHOOK_URL   — Slack incoming-webhook URL (keep in CI secrets)
    NOTIFY_ON_FAILURE   — "true" to notify only on failures (default: true)
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import requests

from helpers.constants.framework_constants import Reporting
from utils.logger import log_info_emoji, log_warning, log_failure

if TYPE_CHECKING:
    from monitoring.metrics import RunMetrics


class AlertManager:
    """Webhook-based notification hub. Extend _channels to add more providers."""

    def __init__(self):
        self._slack_url: str | None = os.getenv(Reporting.SLACK_WEBHOOK_ENV_VAR)
        self._failure_only: bool = (
            os.getenv("NOTIFY_ON_FAILURE", "true").lower() in ("true", "1", "yes")
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def notify(self, metrics: "RunMetrics") -> None:
        """
        Fire all configured notification channels.
        Respects NOTIFY_ON_FAILURE — skips if all tests passed and flag is set.
        """
        if self._failure_only and metrics.failed == 0:
            log_info_emoji("🔕", "All tests passed — notifications suppressed (failure-only mode)")
            return

        self._send_slack(metrics)

    # ------------------------------------------------------------------
    # Slack
    # ------------------------------------------------------------------

    def _send_slack(self, metrics: "RunMetrics") -> None:
        if not self._slack_url:
            log_info_emoji(
                "💬",
                f"Slack notifications disabled — set {Reporting.SLACK_WEBHOOK_ENV_VAR} to enable"
            )
            return

        status_emoji = "✅" if metrics.failed == 0 else "❌"
        color = "#36a64f" if metrics.failed == 0 else "#ff0000"

        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": f"{status_emoji} Test Run Complete",
                    "fields": [
                        {"title": "Environment", "value": metrics.environment or "—", "short": True},
                        {"title": "Browser",     "value": metrics.browser or "—",      "short": True},
                        {"title": "Total",       "value": str(metrics.total),           "short": True},
                        {"title": "Passed",      "value": str(metrics.passed),          "short": True},
                        {"title": "Failed",      "value": str(metrics.failed),          "short": True},
                        {"title": "Pass Rate",   "value": f"{metrics.pass_rate:.1f}%",  "short": True},
                        {"title": "Duration",    "value": f"{metrics.duration_s:.1f}s", "short": True},
                    ],
                    "footer": "playwright-behave-allure-ai-driven-framework",
                }
            ]
        }

        if metrics.failed > 0:
            failed_names = [
                s.name for s in metrics.scenarios if s.status == "failed"
            ][:5]   # cap at 5 to avoid massive Slack messages
            payload["attachments"][0]["fields"].append(
                {"title": "Failed Scenarios", "value": "\n".join(f"• {n}" for n in failed_names)}
            )

        try:
            resp = requests.post(
                self._slack_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                log_info_emoji("💬", "Slack notification sent")
            else:
                log_warning(f"Slack webhook returned {resp.status_code}: {resp.text}")
        except Exception as exc:
            log_failure(f"Slack notification failed: {exc}")
