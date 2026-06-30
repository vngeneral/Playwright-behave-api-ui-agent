"""
Unit tests for integrations/teams.py
All HTTP calls are mocked — no real network traffic.
"""
import base64
import hashlib
import hmac
import json
import os
import unittest
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal RunMetrics stub so we don't need the full monitoring module
# ---------------------------------------------------------------------------

@dataclass
class _FakeMetrics:
    total: int = 10
    passed: int = 8
    failed: int = 2
    duration_s: float = 45.2
    environment: str = "dev"
    browser: str = "chromium"
    scenarios: list = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total else 0.0


@dataclass
class _FakeScenario:
    name: str
    status: str


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestValidateTeamsHmac(unittest.TestCase):
    """validate_teams_hmac() — pure crypto, no network needed."""

    def _make_sig(self, secret_b64: str, body: bytes) -> str:
        key = base64.b64decode(secret_b64)
        sig = base64.b64encode(
            hmac.new(key, msg=body, digestmod=hashlib.sha256).digest()
        ).decode()
        return f"HMAC {sig}"

    def setUp(self):
        # A real base64-encoded 32-byte key
        self.secret = base64.b64encode(b"a" * 32).decode()
        self.body = b'{"type":"message","text":"!run"}'

    def test_valid_signature_returns_true(self):
        from integrations.teams import validate_teams_hmac
        auth = self._make_sig(self.secret, self.body)
        self.assertTrue(validate_teams_hmac(self.secret, self.body, auth))

    def test_wrong_body_returns_false(self):
        from integrations.teams import validate_teams_hmac
        auth = self._make_sig(self.secret, self.body)
        self.assertFalse(validate_teams_hmac(self.secret, b"tampered", auth))

    def test_wrong_secret_returns_false(self):
        from integrations.teams import validate_teams_hmac
        other_secret = base64.b64encode(b"b" * 32).decode()
        auth = self._make_sig(other_secret, self.body)
        self.assertFalse(validate_teams_hmac(self.secret, self.body, auth))

    def test_missing_hmac_prefix_returns_false(self):
        from integrations.teams import validate_teams_hmac
        self.assertFalse(validate_teams_hmac(self.secret, self.body, "Bearer abc123"))

    def test_empty_auth_header_returns_false(self):
        from integrations.teams import validate_teams_hmac
        self.assertFalse(validate_teams_hmac(self.secret, self.body, ""))

    def test_invalid_base64_secret_returns_false(self):
        from integrations.teams import validate_teams_hmac
        result = validate_teams_hmac("not-valid-base64!!!!", self.body, "HMAC abc")
        self.assertFalse(result)


class TestTeamsClientNotConfigured(unittest.TestCase):
    """TeamsClient with no env vars set → disabled, returns False."""

    def setUp(self):
        os.environ.pop("TEAMS_WEBHOOK_URL", None)
        os.environ.pop("TEAMS_OUTGOING_WEBHOOK_SECRET", None)

    def test_is_configured_false(self):
        from integrations.teams import TeamsClient
        client = TeamsClient()
        self.assertFalse(client.is_configured)

    def test_send_notification_returns_false(self):
        from integrations.teams import TeamsClient
        client = TeamsClient()
        result = client.send_notification(_FakeMetrics())
        self.assertFalse(result)

    def test_validate_incoming_returns_false_without_secret(self):
        from integrations.teams import TeamsClient
        client = TeamsClient()
        self.assertFalse(client.validate_incoming(b"body", "HMAC abc"))


class TestTeamsClientConfigured(unittest.TestCase):
    """TeamsClient with env vars set — HTTP calls mocked."""

    WEBHOOK_URL = "https://example.webhook.office.com/webhook/test"
    SECRET = base64.b64encode(b"x" * 32).decode()

    def setUp(self):
        os.environ["TEAMS_WEBHOOK_URL"] = self.WEBHOOK_URL
        os.environ["TEAMS_OUTGOING_WEBHOOK_SECRET"] = self.SECRET

    def tearDown(self):
        os.environ.pop("TEAMS_WEBHOOK_URL", None)
        os.environ.pop("TEAMS_OUTGOING_WEBHOOK_SECRET", None)

    def test_is_configured_true(self):
        from integrations.teams import TeamsClient
        self.assertTrue(TeamsClient().is_configured)

    @patch("integrations.teams.requests.post")
    def test_send_notification_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.teams import TeamsClient
        result = TeamsClient().send_notification(_FakeMetrics())
        self.assertTrue(result)
        mock_post.assert_called_once()

    @patch("integrations.teams.requests.post")
    def test_send_notification_failure_status(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500, text="Server error")
        from integrations.teams import TeamsClient
        result = TeamsClient().send_notification(_FakeMetrics())
        self.assertFalse(result)

    @patch("integrations.teams.requests.post")
    def test_send_notification_network_error(self, mock_post):
        mock_post.side_effect = ConnectionError("Network unreachable")
        from integrations.teams import TeamsClient
        result = TeamsClient().send_notification(_FakeMetrics())
        self.assertFalse(result)   # never raises

    @patch("integrations.teams.requests.post")
    def test_adaptive_card_contains_pass_rate(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.teams import TeamsClient
        TeamsClient().send_notification(_FakeMetrics())
        call_body = json.loads(mock_post.call_args[1]["data"])
        card_str = json.dumps(call_body)
        self.assertIn("Pass Rate", card_str)
        self.assertIn("80.0%", card_str)

    @patch("integrations.teams.requests.post")
    def test_adaptive_card_lists_failed_scenarios(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.teams import TeamsClient
        metrics = _FakeMetrics(
            total=3, passed=2, failed=1,
            scenarios=[
                _FakeScenario("Login smoke test", "passed"),
                _FakeScenario("Checkout flow", "failed"),
                _FakeScenario("API health", "passed"),
            ]
        )
        TeamsClient().send_notification(metrics)
        card_str = json.dumps(json.loads(mock_post.call_args[1]["data"]))
        self.assertIn("Checkout flow", card_str)

    @patch("integrations.teams.requests.post")
    def test_send_text(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.teams import TeamsClient
        result = TeamsClient().send_text("✅ Test complete")
        self.assertTrue(result)
        body = json.loads(mock_post.call_args[1]["data"])
        self.assertEqual(body["type"], "message")

    def test_validate_incoming_valid_hmac(self):
        from integrations.teams import TeamsClient, validate_teams_hmac
        body = b'{"type":"message","text":"!run"}'
        key = base64.b64decode(self.SECRET)
        sig = base64.b64encode(
            hmac.new(key, msg=body, digestmod=hashlib.sha256).digest()
        ).decode()
        auth = f"HMAC {sig}"
        self.assertTrue(TeamsClient().validate_incoming(body, auth))

    def test_validate_incoming_invalid_hmac(self):
        from integrations.teams import TeamsClient
        self.assertFalse(TeamsClient().validate_incoming(b"body", "HMAC wrong"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
