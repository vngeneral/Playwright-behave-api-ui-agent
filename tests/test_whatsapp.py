"""
Unit tests for integrations/whatsapp.py
All HTTP calls are mocked — no real network traffic.
"""
import os
import unittest
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch


@dataclass
class _FakeMetrics:
    total: int = 5
    passed: int = 4
    failed: int = 1
    duration_s: float = 22.5
    environment: str = "staging"
    browser: str = "firefox"
    scenarios: list = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total else 0.0


@dataclass
class _FakeScenario:
    name: str
    status: str


class TestProviderDetection(unittest.TestCase):
    def tearDown(self):
        for k in ("TWILIO_ACCOUNT_SID", "WHATSAPP_API_TOKEN"):
            os.environ.pop(k, None)

    def test_no_env_vars_gives_none(self):
        from integrations.whatsapp import _detect_provider
        self.assertEqual(_detect_provider(), "none")

    def test_meta_token_gives_meta(self):
        os.environ["WHATSAPP_API_TOKEN"] = "token"
        from integrations.whatsapp import _detect_provider
        self.assertEqual(_detect_provider(), "meta")

    def test_twilio_sid_gives_twilio(self):
        os.environ["TWILIO_ACCOUNT_SID"] = "AC1234"
        from integrations.whatsapp import _detect_provider
        self.assertEqual(_detect_provider(), "twilio")

    def test_both_set_twilio_wins(self):
        os.environ["TWILIO_ACCOUNT_SID"] = "AC1234"
        os.environ["WHATSAPP_API_TOKEN"] = "token"
        from integrations.whatsapp import _detect_provider
        self.assertEqual(_detect_provider(), "twilio")


class TestWhatsAppClientNotConfigured(unittest.TestCase):
    def setUp(self):
        for k in ("TWILIO_ACCOUNT_SID", "WHATSAPP_API_TOKEN", "WHATSAPP_NOTIFY_TO"):
            os.environ.pop(k, None)

    def test_is_configured_false(self):
        from integrations.whatsapp import WhatsAppClient
        self.assertFalse(WhatsAppClient().is_configured)

    def test_send_notification_returns_false(self):
        from integrations.whatsapp import WhatsAppClient
        self.assertFalse(WhatsAppClient().send_notification(_FakeMetrics()))


class TestWhatsAppClientMeta(unittest.TestCase):
    def setUp(self):
        os.environ["WHATSAPP_API_TOKEN"]       = "EAAtest"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "12345678"
        os.environ["WHATSAPP_VERIFY_TOKEN"]    = "my-verify-token"
        os.environ["WHATSAPP_NOTIFY_TO"]       = "+61400000000"
        for k in ("TWILIO_ACCOUNT_SID",):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("WHATSAPP_API_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                   "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_NOTIFY_TO"):
            os.environ.pop(k, None)

    def test_is_configured_true(self):
        from integrations.whatsapp import WhatsAppClient
        self.assertTrue(WhatsAppClient().is_configured)

    @patch("integrations.whatsapp.requests.post")
    def test_send_notification_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.whatsapp import WhatsAppClient
        result = WhatsAppClient().send_notification(_FakeMetrics())
        self.assertTrue(result)
        mock_post.assert_called_once()
        import json
        call_body = json.loads(mock_post.call_args[1]["data"])
        self.assertEqual(call_body["messaging_product"], "whatsapp")
        self.assertEqual(call_body["to"], "+61400000000")

    @patch("integrations.whatsapp.requests.post")
    def test_send_notification_api_error(self, mock_post):
        mock_post.return_value = MagicMock(status_code=400, text="Bad request")
        from integrations.whatsapp import WhatsAppClient
        self.assertFalse(WhatsAppClient().send_notification(_FakeMetrics()))

    @patch("integrations.whatsapp.requests.post")
    def test_send_notification_network_error(self, mock_post):
        mock_post.side_effect = ConnectionError("no network")
        from integrations.whatsapp import WhatsAppClient
        self.assertFalse(WhatsAppClient().send_notification(_FakeMetrics()))

    def test_webhook_verify_valid_token(self):
        from integrations.whatsapp import WhatsAppClient
        result = WhatsAppClient().verify_webhook(
            mode="subscribe",
            token="my-verify-token",
            challenge="challenge_abc",
        )
        self.assertEqual(result, "challenge_abc")

    def test_webhook_verify_wrong_token(self):
        from integrations.whatsapp import WhatsAppClient
        result = WhatsAppClient().verify_webhook(
            mode="subscribe",
            token="wrong-token",
            challenge="challenge_abc",
        )
        self.assertIsNone(result)

    def test_webhook_verify_wrong_mode(self):
        from integrations.whatsapp import WhatsAppClient
        result = WhatsAppClient().verify_webhook(
            mode="unsubscribe",
            token="my-verify-token",
            challenge="challenge_abc",
        )
        self.assertIsNone(result)


class TestExtractIncomingMessages(unittest.TestCase):
    def setUp(self):
        os.environ["WHATSAPP_API_TOKEN"]       = "token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "123"
        os.environ["WHATSAPP_VERIFY_TOKEN"]    = "verify"
        os.environ["WHATSAPP_NOTIFY_TO"]       = "+61400000000"

    def tearDown(self):
        for k in ("WHATSAPP_API_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                   "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_NOTIFY_TO"):
            os.environ.pop(k, None)

    def _payload(self, from_num: str, text: str) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": from_num,
                            "type": "text",
                            "text": {"body": text},
                        }]
                    }
                }]
            }]
        }

    def test_extracts_text_message(self):
        from integrations.whatsapp import WhatsAppClient
        msgs = WhatsAppClient().extract_incoming_messages(
            self._payload("+61400000001", "!run --tags @smoke")
        )
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["from"], "+61400000001")
        self.assertEqual(msgs[0]["text"], "!run --tags @smoke")

    def test_empty_payload_returns_empty_list(self):
        from integrations.whatsapp import WhatsAppClient
        self.assertEqual(WhatsAppClient().extract_incoming_messages({}), [])

    def test_non_text_message_ignored(self):
        from integrations.whatsapp import WhatsAppClient
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "+1234",
                            "type": "image",  # not text
                            "image": {"id": "abc"},
                        }]
                    }
                }]
            }]
        }
        self.assertEqual(WhatsAppClient().extract_incoming_messages(payload), [])


class TestWhatsAppClientTwilio(unittest.TestCase):
    def setUp(self):
        os.environ["TWILIO_ACCOUNT_SID"]    = "ACtest"
        os.environ["TWILIO_AUTH_TOKEN"]     = "authtoken"
        os.environ["TWILIO_WHATSAPP_FROM"]  = "whatsapp:+14155238886"
        os.environ["WHATSAPP_NOTIFY_TO"]    = "+61400000000"
        os.environ.pop("WHATSAPP_API_TOKEN", None)

    def tearDown(self):
        for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                   "TWILIO_WHATSAPP_FROM", "WHATSAPP_NOTIFY_TO"):
            os.environ.pop(k, None)

    def test_is_configured_true(self):
        from integrations.whatsapp import WhatsAppClient
        self.assertTrue(WhatsAppClient().is_configured)

    @patch("integrations.whatsapp.requests.post")
    def test_send_notification_twilio(self, mock_post):
        mock_post.return_value = MagicMock(status_code=201)
        from integrations.whatsapp import WhatsAppClient
        result = WhatsAppClient().send_notification(_FakeMetrics())
        self.assertTrue(result)
        # Check that the Twilio endpoint was hit
        call_url = mock_post.call_args[0][0]
        self.assertIn("twilio.com", call_url)

    @patch("integrations.whatsapp.requests.post")
    def test_whatsapp_prefix_added_if_missing(self, mock_post):
        mock_post.return_value = MagicMock(status_code=201)
        # WHATSAPP_NOTIFY_TO doesn't have "whatsapp:" prefix
        from integrations.whatsapp import WhatsAppClient
        WhatsAppClient().send_text("+61400000000", "hello")
        call_data = mock_post.call_args[1]["data"]
        self.assertEqual(call_data["To"], "whatsapp:+61400000000")


class TestNotificationMessageFormat(unittest.TestCase):
    """Verify the message text includes key fields."""

    def test_all_passed_message(self):
        from integrations.whatsapp import WhatsAppClient
        metrics = _FakeMetrics(total=5, passed=5, failed=0)
        msg = WhatsAppClient._format_notification(metrics)
        self.assertIn("✅", msg)
        self.assertIn("100.0%", msg)
        self.assertNotIn("Failed scenarios", msg)

    def test_failed_message_lists_scenarios(self):
        from integrations.whatsapp import WhatsAppClient
        metrics = _FakeMetrics(
            total=3, passed=2, failed=1,
            scenarios=[
                _FakeScenario("Sign in flow", "passed"),
                _FakeScenario("Checkout flow", "failed"),
            ]
        )
        msg = WhatsAppClient._format_notification(metrics)
        self.assertIn("❌", msg)
        self.assertIn("Checkout flow", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
