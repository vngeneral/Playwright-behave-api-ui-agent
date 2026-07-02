"""
Microsoft Teams Client
======================
Two capabilities:

1. SEND notifications
   Uses the Power Automate workflow webhook (the replacement for the deprecated
   Office 365 Connector as of December 2024).  Sends an Adaptive Card with
   test-run summary.

2. RECEIVE commands
   Teams "Outgoing Webhooks" POST to your server with HMAC-SHA256 auth.
   validate_teams_hmac() checks the Authorization header before the server
   acts on any command.

Environment variables (all optional — missing ⟹ feature disabled):
    TEAMS_WEBHOOK_URL           — Power Automate workflow webhook URL (for sending)
    TEAMS_OUTGOING_WEBHOOK_SECRET — base64-encoded secret from Teams outgoing webhook config

Never hardcode URLs or secrets.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import TYPE_CHECKING

import requests

from utils.logger import log_info_emoji, log_warning, log_failure

if TYPE_CHECKING:
    from agent.monitoring.metrics import RunMetrics


# ---------------------------------------------------------------------------
# Constants — env-var names (not values)
# ---------------------------------------------------------------------------

_WEBHOOK_URL_ENV = "TEAMS_WEBHOOK_URL"
_OUTGOING_SECRET_ENV = "TEAMS_OUTGOING_WEBHOOK_SECRET"

# ---------------------------------------------------------------------------
# HMAC validation (for incoming commands from Teams Outgoing Webhook)
# ---------------------------------------------------------------------------


def validate_teams_hmac(secret_b64: str, body: bytes, auth_header: str) -> bool:
    """
    Verify that a Teams outgoing-webhook request is authentic.

    Teams signs the request body with HMAC-SHA256 using the shared secret
    (which Teams gives you as a base64-encoded string).  It puts the result
    in the ``Authorization`` header as: ``HMAC <base64-encoded-signature>``.

    Args:
        secret_b64:   The raw value of TEAMS_OUTGOING_WEBHOOK_SECRET (base64 string).
        body:         Raw bytes of the request body.
        auth_header:  Value of the ``Authorization`` HTTP header.

    Returns:
        True if the signature matches, False otherwise.
    """
    if not auth_header or not auth_header.startswith("HMAC "):
        return False

    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        log_warning("TEAMS_OUTGOING_WEBHOOK_SECRET is not valid base64")
        return False

    expected_sig = base64.b64encode(
        hmac.new(key, msg=body, digestmod=hashlib.sha256).digest()
    ).decode()

    received_sig = auth_header[len("HMAC "):]
    return hmac.compare_digest(expected_sig, received_sig)


# ---------------------------------------------------------------------------
# Teams client
# ---------------------------------------------------------------------------


class TeamsClient:
    """
    Sends Adaptive Card messages to a Microsoft Teams channel via a
    Power Automate workflow webhook.

    Usage::
        client = TeamsClient()
        client.send_notification(metrics)
        client.send_text("✅ Tests started — watch this space.")
    """

    def __init__(self) -> None:
        self._webhook_url: str | None = os.getenv(_WEBHOOK_URL_ENV)
        self._outgoing_secret: str | None = os.getenv(_OUTGOING_SECRET_ENV)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when the sending webhook URL is available."""
        return bool(self._webhook_url)

    def send_notification(self, metrics: "RunMetrics") -> bool:
        """
        Send a rich Adaptive Card test-run summary to Teams.

        Returns True on success, False on any error (never raises).
        """
        if not self._webhook_url:
            log_info_emoji(
                "💬",
                f"Teams notifications disabled — set {_WEBHOOK_URL_ENV} to enable",
            )
            return False

        payload = self._build_adaptive_card(metrics)
        return self._post(payload)

    def send_text(self, message: str) -> bool:
        """Send a plain-text message to Teams (used for command ACKs)."""
        if not self._webhook_url:
            return False
        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [{"type": "TextBlock", "text": message, "wrap": True}],
                    },
                }
            ],
        }
        return self._post(payload)

    def validate_incoming(self, body: bytes, auth_header: str) -> bool:
        """
        Validate an incoming Teams outgoing-webhook request.
        Returns False (reject) if the secret is not configured.
        """
        if not self._outgoing_secret:
            log_warning(
                f"Outgoing webhook secret not configured ({_OUTGOING_SECRET_ENV}). "
                "Rejecting all incoming Teams commands."
            )
            return False
        return validate_teams_hmac(self._outgoing_secret, body, auth_header)

    # ------------------------------------------------------------------
    # Adaptive Card builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_adaptive_card(metrics: "RunMetrics") -> dict:
        status_emoji = "✅" if metrics.failed == 0 else "❌"
        status_color = "Good" if metrics.failed == 0 else "Attention"
        title = f"{status_emoji} Test Run Complete"

        facts = [
            {"title": "Environment", "value": metrics.environment or "—"},
            {"title": "Browser",     "value": metrics.browser or "—"},
            {"title": "Total",       "value": str(metrics.total)},
            {"title": "Passed",      "value": str(metrics.passed)},
            {"title": "Failed",      "value": str(metrics.failed)},
            {"title": "Pass Rate",   "value": f"{metrics.pass_rate:.1f}%"},
            {"title": "Duration",    "value": f"{metrics.duration_s:.1f}s"},
        ]

        body: list[dict] = [
            {
                "type": "TextBlock",
                "text": title,
                "size": "Large",
                "weight": "Bolder",
                "color": status_color,
            },
            {"type": "FactSet", "facts": facts},
        ]

        # Append failed scenario names (capped at 5)
        if metrics.failed > 0:
            failed_names = [
                s.name for s in metrics.scenarios if s.status == "failed"
            ][:5]
            body.append(
                {
                    "type": "TextBlock",
                    "text": "**Failed scenarios:**\n" + "\n".join(f"• {n}" for n in failed_names),
                    "wrap": True,
                    "color": "Attention",
                }
            )

        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": body,
                    },
                }
            ],
        }

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _post(self, payload: dict) -> bool:
        try:
            resp = requests.post(
                self._webhook_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code in (200, 202):
                log_info_emoji("💬", "Teams notification sent")
                return True
            log_warning(f"Teams webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as exc:
            log_failure(f"Teams notification failed: {exc}")
            return False
