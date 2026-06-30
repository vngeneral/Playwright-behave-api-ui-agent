"""
Unit tests for integrations/webhook_server.py
Uses Flask test client — no real network traffic, no real subprocess execution.
"""
import base64
import hashlib
import hmac
import json
import os
import unittest
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_teams_auth(secret_b64: str, body: bytes) -> str:
    key = base64.b64decode(secret_b64)
    sig = base64.b64encode(
        hmac.new(key, msg=body, digestmod=hashlib.sha256).digest()
    ).decode()
    return f"HMAC {sig}"


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        # Clear env so no real integrations kick in
        for k in ("TEAMS_WEBHOOK_URL", "TEAMS_OUTGOING_WEBHOOK_SECRET",
                   "WHATSAPP_API_TOKEN", "WHATSAPP_VERIFY_TOKEN"):
            os.environ.pop(k, None)

        from integrations.webhook_server import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_ok(self):
        data = json.loads(resp := self.client.get("/health").data)
        self.assertEqual(data["status"], "ok")
        self.assertIn("timestamp", data)


class TestTeamsWebhookEndpoint(unittest.TestCase):
    SECRET = base64.b64encode(b"testsecretkey1234567890abcdefgh").decode()

    def setUp(self):
        os.environ["TEAMS_OUTGOING_WEBHOOK_SECRET"] = self.SECRET
        os.environ.pop("TEAMS_WEBHOOK_URL", None)
        from integrations.webhook_server import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("TEAMS_OUTGOING_WEBHOOK_SECRET", None)

    def _post(self, payload: dict, valid_sig: bool = True) -> object:
        body = json.dumps(payload).encode()
        auth = _make_teams_auth(self.SECRET, body) if valid_sig else "HMAC badsig"
        return self.client.post(
            "/teams/webhook",
            data=body,
            content_type="application/json",
            headers={"Authorization": auth},
        )

    def test_invalid_hmac_returns_401(self):
        resp = self._post({"text": "!run"}, valid_sig=False)
        self.assertEqual(resp.status_code, 401)

    def test_help_command_returns_help_text(self):
        resp = self._post({"text": "!help"})
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.data)
        self.assertIn("!run", body["text"])

    def test_status_command_returns_no_runs_yet(self):
        resp = self._post({"text": "!status"})
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.data)
        self.assertIn("No test runs", body["text"])

    @patch("integrations.webhook_server._run_tests_async")
    def test_run_command_acknowledged(self, mock_async):
        resp = self._post({"text": "!run --tags @smoke --headless"})
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.data)
        self.assertIn("🚀", body["text"])
        mock_async.assert_called_once()

    @patch("integrations.webhook_server._run_tests_async")
    def test_run_command_passes_correct_argv(self, mock_async):
        self._post({"text": "!run --tags @smoke --browser firefox"})
        call_argv = mock_async.call_args[0][0]
        self.assertIn("--tags", call_argv)
        self.assertIn("@smoke", call_argv)
        self.assertIn("--browser", call_argv)
        self.assertIn("firefox", call_argv)

    def test_non_command_text_returns_empty_200(self):
        resp = self._post({"text": "Good morning everyone!"})
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.data)
        self.assertEqual(body["text"], "")

    def test_bad_command_returns_error_message(self):
        resp = self._post({"text": "!run --browser ie11"})
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.data)
        self.assertIn("ie11", body["text"])

    def test_malformed_json_returns_400(self):
        body = b"not json"
        auth = _make_teams_auth(self.SECRET, body)
        resp = self.client.post(
            "/teams/webhook",
            data=body,
            content_type="application/json",
            headers={"Authorization": auth},
        )
        self.assertEqual(resp.status_code, 400)


class TestWhatsAppWebhookEndpoint(unittest.TestCase):
    def setUp(self):
        os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
        os.environ["WHATSAPP_API_TOKEN"]    = "fake-token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1234"
        os.environ["WHATSAPP_NOTIFY_TO"]    = "+61400000000"
        os.environ.pop("TWILIO_ACCOUNT_SID", None)
        from integrations.webhook_server import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        for k in ("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_API_TOKEN",
                   "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_NOTIFY_TO"):
            os.environ.pop(k, None)

    # ── GET (webhook verification) ──────────────────────────────────────────

    def test_verify_valid_token_returns_challenge(self):
        resp = self.client.get(
            "/whatsapp/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": "test-verify-token",
                "hub.challenge": "abc123",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode(), "abc123")

    def test_verify_wrong_token_returns_403(self):
        resp = self.client.get(
            "/whatsapp/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "abc123",
            },
        )
        self.assertEqual(resp.status_code, 403)

    # ── POST (incoming messages) ────────────────────────────────────────────

    def _incoming(self, from_num: str, text: str) -> bytes:
        payload = {
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
        return json.dumps(payload).encode()

    @patch("integrations.whatsapp.requests.post")
    def test_help_command_sends_reply(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        resp = self.client.post(
            "/whatsapp/webhook",
            data=self._incoming("+61400000001", "!help"),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        # WhatsApp.send_text should have been called with help text
        mock_post.assert_called()
        call_body = json.loads(mock_post.call_args[1]["data"])
        self.assertIn("!run", call_body["text"]["body"])

    @patch("integrations.webhook_server._run_tests_async")
    @patch("integrations.whatsapp.requests.post")
    def test_run_command_triggers_tests(self, mock_post, mock_async):
        mock_post.return_value = MagicMock(status_code=200)
        resp = self.client.post(
            "/whatsapp/webhook",
            data=self._incoming("+61400000001", "!run --tags @smoke"),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        mock_async.assert_called_once()

    def test_non_command_returns_200_without_reply(self):
        resp = self.client.post(
            "/whatsapp/webhook",
            data=self._incoming("+61400000001", "Hello there"),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_empty_body_returns_200(self):
        resp = self.client.post(
            "/whatsapp/webhook",
            data=b"{}",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)


class TestFormatStatus(unittest.TestCase):
    def test_no_runs_yet(self):
        from integrations.webhook_server import _format_status
        msg = _format_status({"status": "no_runs_yet"})
        self.assertIn("No test runs", msg)

    def test_passed_run(self):
        from integrations.webhook_server import _format_status
        msg = _format_status({
            "status": "passed",
            "timestamp": "2026-01-01T10:00:00",
            "summary": "✅ All tests passed (exit 0, 45.1s)",
        })
        self.assertIn("✅", msg)
        self.assertIn("All tests passed", msg)

    def test_failed_run(self):
        from integrations.webhook_server import _format_status
        msg = _format_status({
            "status": "failed",
            "timestamp": "2026-01-01T10:00:00",
            "summary": "❌ Some tests failed (exit 1, 22.0s)",
        })
        self.assertIn("❌", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
