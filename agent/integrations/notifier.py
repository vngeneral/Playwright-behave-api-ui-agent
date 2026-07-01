"""
Unified Notifier
================
Single call-site for all post-run notifications.
Wraps Slack (existing AlertManager), Teams, and WhatsApp into one interface.

Usage in environment.py::

    from integrations.notifier import UnifiedNotifier
    _notifier = UnifiedNotifier()

    def after_all(context):
        metrics = _metrics.finish_run()
        _metrics.save()
        _notifier.notify(metrics)   # fires all configured channels

Environment variables that control each channel:
    Slack:     SLACK_WEBHOOK_URL
    Teams:     TEAMS_WEBHOOK_URL
    WhatsApp:  WHATSAPP_API_TOKEN + WHATSAPP_PHONE_NUMBER_ID + WHATSAPP_NOTIFY_TO
               (or TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_WHATSAPP_FROM + WHATSAPP_NOTIFY_TO)
    Common:    NOTIFY_ON_FAILURE  — "true" (default) → only notify when failures exist
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agent.integrations.teams import TeamsClient
from agent.integrations.whatsapp import WhatsAppClient
from utils.logger import log_info_emoji, log_failure

if TYPE_CHECKING:
    from monitoring.metrics import RunMetrics


class UnifiedNotifier:
    """
    Fire all configured notification channels after a test run.

    Channels are activated purely by the presence of the relevant env vars.
    Channels that are not configured are silently skipped — never an error.
    """

    def __init__(self) -> None:
        # Lazy-import to avoid circular dependency with monitoring.alerts
        from monitoring.alerts import AlertManager  # noqa: PLC0415
        self._slack   = AlertManager()
        self._teams   = TeamsClient()
        self._whatsapp = WhatsAppClient()
        self._failure_only = (
            os.getenv("NOTIFY_ON_FAILURE", "true").lower() in ("true", "1", "yes")
        )

    def notify(self, metrics: "RunMetrics") -> None:
        """
        Send notifications to all configured channels.
        If NOTIFY_ON_FAILURE=true (default) and there are no failures, skip all channels.
        """
        if self._failure_only and metrics.failed == 0:
            log_info_emoji(
                "🔕",
                "All tests passed — notifications suppressed (NOTIFY_ON_FAILURE=true)",
            )
            return

        self._fire_slack(metrics)
        self._fire_teams(metrics)
        self._fire_whatsapp(metrics)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _fire_slack(self, metrics: "RunMetrics") -> None:
        try:
            self._slack.notify(metrics)
        except Exception as exc:
            log_failure(f"Slack notification error: {exc}")

    def _fire_teams(self, metrics: "RunMetrics") -> None:
        if not self._teams.is_configured:
            return
        try:
            self._teams.send_notification(metrics)
        except Exception as exc:
            log_failure(f"Teams notification error: {exc}")

    def _fire_whatsapp(self, metrics: "RunMetrics") -> None:
        if not self._whatsapp.is_configured:
            return
        try:
            self._whatsapp.send_notification(metrics)
        except Exception as exc:
            log_failure(f"WhatsApp notification error: {exc}")
