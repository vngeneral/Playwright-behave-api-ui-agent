"""
WhatsApp Client
===============
Two supported providers (detected from env vars):

  A. Meta Cloud API (recommended)
     Requires: WHATSAPP_API_TOKEN, WHATSAPP_PHONE_NUMBER_ID
     Webhook verification: WHATSAPP_VERIFY_TOKEN
     Recipient: WHATSAPP_NOTIFY_TO (E.164, e.g. +61412345678)

  B. Twilio WhatsApp API (fallback if TWILIO_ACCOUNT_SID is set)
     Requires: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
               TWILIO_WHATSAPP_FROM (e.g. whatsapp:+14155238886)
     Recipient: WHATSAPP_NOTIFY_TO

The active provider is chosen automatically:
  - If TWILIO_ACCOUNT_SID is set → Twilio
  - Otherwise, if WHATSAPP_API_TOKEN is set → Meta Cloud API
  - Neither set → notifications silently disabled

Two capabilities:
  1. SEND notifications (post-run summary)
  2. RECEIVE commands via Meta webhook (parsed by command_parser)
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import requests

from utils.logger import log_info_emoji, log_warning, log_failure

if TYPE_CHECKING:
    from monitoring.metrics import RunMetrics

# ---------------------------------------------------------------------------
# Env-var names (never the values themselves)
# ---------------------------------------------------------------------------

_META_API_TOKEN_ENV     = "WHATSAPP_API_TOKEN"
_META_PHONE_ID_ENV      = "WHATSAPP_PHONE_NUMBER_ID"
_META_VERIFY_TOKEN_ENV  = "WHATSAPP_VERIFY_TOKEN"
_NOTIFY_TO_ENV          = "WHATSAPP_NOTIFY_TO"

_TWILIO_SID_ENV         = "TWILIO_ACCOUNT_SID"
_TWILIO_TOKEN_ENV       = "TWILIO_AUTH_TOKEN"
_TWILIO_FROM_ENV        = "TWILIO_WHATSAPP_FROM"   # e.g. "whatsapp:+14155238886"

_META_API_URL = "https://graph.facebook.com/v19.0/{phone_number_id}/messages"

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


def _detect_provider() -> str:
    """Return 'twilio', 'meta', or 'none'."""
    if os.getenv(_TWILIO_SID_ENV):
        return "twilio"
    if os.getenv(_META_API_TOKEN_ENV):
        return "meta"
    return "none"


# ---------------------------------------------------------------------------
# WhatsApp client
# ---------------------------------------------------------------------------


class WhatsAppClient:
    """
    Sends WhatsApp messages and handles incoming webhook events.

    Provider is auto-detected from environment variables.
    """

    def __init__(self) -> None:
        self._provider = _detect_provider()
        self._notify_to: str | None = os.getenv(_NOTIFY_TO_ENV)

        # Meta Cloud API credentials
        self._meta_token: str | None = os.getenv(_META_API_TOKEN_ENV)
        self._meta_phone_id: str | None = os.getenv(_META_PHONE_ID_ENV)
        self._meta_verify_token: str | None = os.getenv(_META_VERIFY_TOKEN_ENV)

        # Twilio credentials
        self._twilio_sid: str | None = os.getenv(_TWILIO_SID_ENV)
        self._twilio_token: str | None = os.getenv(_TWILIO_TOKEN_ENV)
        self._twilio_from: str | None = os.getenv(_TWILIO_FROM_ENV)

    # ------------------------------------------------------------------
    # Public — sending
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when at least one provider is configured with a recipient."""
        return self._provider != "none" and bool(self._notify_to)

    def send_notification(self, metrics: "RunMetrics") -> bool:
        """
        Send a test-run summary to the configured WhatsApp number.
        Returns True on success, False on any error (never raises).
        """
        if not self.is_configured:
            log_info_emoji(
                "📱",
                "WhatsApp notifications disabled — set WHATSAPP_API_TOKEN (Meta) "
                "or TWILIO_ACCOUNT_SID (Twilio) + WHATSAPP_NOTIFY_TO",
            )
            return False

        message = self._format_notification(metrics)
        return self.send_text(self._notify_to, message)

    def send_text(self, to: str, message: str) -> bool:
        """Send a plain-text message. ``to`` is an E.164 number."""
        if self._provider == "meta":
            return self._send_meta(to, message)
        if self._provider == "twilio":
            return self._send_twilio(to, message)
        return False

    # ------------------------------------------------------------------
    # Public — webhook verification (Meta Cloud API)
    # ------------------------------------------------------------------

    def verify_webhook(self, mode: str, token: str, challenge: str) -> str | None:
        """
        Handle the GET verification challenge from Meta.

        Returns the ``challenge`` string to echo back if valid, else None.
        """
        if not self._meta_verify_token:
            log_warning(f"{_META_VERIFY_TOKEN_ENV} not set — cannot verify Meta webhook")
            return None
        if mode == "subscribe" and token == self._meta_verify_token:
            return challenge
        return None

    def extract_incoming_messages(self, payload: dict) -> list[dict]:
        """
        Parse an incoming Meta webhook POST body.

        Returns a list of dicts: [{"from": "+61...", "text": "!run --tags @smoke"}]
        Empty list if no text messages found.
        """
        messages: list[dict] = []
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for msg in value.get("messages", []):
                        if msg.get("type") == "text":
                            messages.append({
                                "from": msg.get("from", ""),
                                "text": msg.get("text", {}).get("body", ""),
                            })
        except Exception as exc:
            log_warning(f"Failed to parse WhatsApp payload: {exc}")
        return messages

    # ------------------------------------------------------------------
    # Sending — Meta Cloud API
    # ------------------------------------------------------------------

    def _send_meta(self, to: str, message: str) -> bool:
        if not self._meta_token or not self._meta_phone_id:
            log_warning("Meta Cloud API: WHATSAPP_API_TOKEN or WHATSAPP_PHONE_NUMBER_ID missing")
            return False

        url = _META_API_URL.format(phone_number_id=self._meta_phone_id)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        headers = {
            "Authorization": f"Bearer {self._meta_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, data=json.dumps(payload), headers=headers, timeout=10)
            if resp.status_code == 200:
                log_info_emoji("📱", f"WhatsApp (Meta) message sent to {to}")
                return True
            log_warning(f"WhatsApp Meta API returned {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as exc:
            log_failure(f"WhatsApp (Meta) send failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Sending — Twilio
    # ------------------------------------------------------------------

    def _send_twilio(self, to: str, message: str) -> bool:
        if not self._twilio_sid or not self._twilio_token or not self._twilio_from:
            log_warning("Twilio: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM missing")
            return False

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._twilio_sid}/Messages.json"
        # Ensure 'whatsapp:' prefix on both numbers
        from_num = self._twilio_from if self._twilio_from.startswith("whatsapp:") else f"whatsapp:{self._twilio_from}"
        to_num   = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        data = {"From": from_num, "To": to_num, "Body": message}
        try:
            resp = requests.post(
                url, data=data,
                auth=(self._twilio_sid, self._twilio_token),
                timeout=10,
            )
            if resp.status_code in (200, 201):
                log_info_emoji("📱", f"WhatsApp (Twilio) message sent to {to}")
                return True
            log_warning(f"Twilio returned {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as exc:
            log_failure(f"WhatsApp (Twilio) send failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_notification(metrics: "RunMetrics") -> str:
        status_emoji = "✅" if metrics.failed == 0 else "❌"
        lines = [
            f"{status_emoji} *Test Run Complete*",
            f"Environment: {metrics.environment or '—'}",
            f"Browser: {metrics.browser or '—'}",
            f"Total: {metrics.total}  |  ✅ {metrics.passed}  |  ❌ {metrics.failed}",
            f"Pass Rate: {metrics.pass_rate:.1f}%",
            f"Duration: {metrics.duration_s:.1f}s",
        ]
        if metrics.failed > 0:
            failed_names = [s.name for s in metrics.scenarios if s.status == "failed"][:5]
            lines.append("\n*Failed scenarios:*")
            lines.extend(f"• {n}" for n in failed_names)
        return "\n".join(lines)
