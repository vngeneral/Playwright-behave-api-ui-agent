"""
Unit tests for agent/ai/test_generator.py
No network calls, no LLM calls — parse_curl is pure, and generate_from_*
methods are tested with a mocked LLM client.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.ai.test_generator import AITestGenerator, parse_curl


class TestParseCurlMethod(unittest.TestCase):
    def test_defaults_to_get_without_data(self):
        parsed = parse_curl('curl https://api.example.com/vehicles')
        self.assertEqual(parsed["method"], "GET")

    def test_defaults_to_post_with_data(self):
        parsed = parse_curl('curl https://api.example.com/vehicles -d \'{"vin":"X"}\'')
        self.assertEqual(parsed["method"], "POST")

    def test_explicit_method_wins(self):
        parsed = parse_curl('curl -X PUT https://api.example.com/vehicles -d \'{"vin":"X"}\'')
        self.assertEqual(parsed["method"], "PUT")

    def test_request_long_flag(self):
        parsed = parse_curl('curl --request DELETE https://api.example.com/vehicles/1')
        self.assertEqual(parsed["method"], "DELETE")


class TestParseCurlUrl(unittest.TestCase):
    def test_positional_url(self):
        parsed = parse_curl('curl https://api.example.com/vehicles')
        self.assertEqual(parsed["url"], "https://api.example.com/vehicles")

    def test_url_flag(self):
        parsed = parse_curl('curl --url https://api.example.com/vehicles -X GET')
        self.assertEqual(parsed["url"], "https://api.example.com/vehicles")

    def test_missing_url_raises(self):
        with self.assertRaises(ValueError):
            parse_curl('curl -X GET -H "Accept: application/json"')


class TestParseCurlHeadersAndBody(unittest.TestCase):
    def test_headers_parsed(self):
        parsed = parse_curl(
            'curl https://api.example.com/vehicles -H "Content-Type: application/json"'
        )
        self.assertEqual(parsed["headers"]["Content-Type"], "application/json")

    def test_body_parsed(self):
        parsed = parse_curl(
            'curl -X POST https://api.example.com/vehicles -d \'{"vin":"ABC123"}\''
        )
        self.assertEqual(parsed["body"], '{"vin":"ABC123"}')

    def test_data_raw_parsed(self):
        parsed = parse_curl(
            'curl -X POST https://api.example.com/vehicles --data-raw \'{"vin":"ABC123"}\''
        )
        self.assertEqual(parsed["body"], '{"vin":"ABC123"}')


class TestParseCurlSecretRedaction(unittest.TestCase):
    """Real credential values must never survive into the parsed dict."""

    def test_authorization_header_redacted(self):
        parsed = parse_curl(
            'curl https://api.example.com/vehicles -H "Authorization: Bearer sk-real-secret-123"'
        )
        self.assertEqual(parsed["headers"]["Authorization"], "<redacted>")
        self.assertNotIn("sk-real-secret-123", str(parsed))

    def test_api_key_header_redacted_case_insensitive(self):
        parsed = parse_curl(
            'curl https://api.example.com/vehicles -H "x-api-key: super-secret-key"'
        )
        self.assertEqual(parsed["headers"]["x-api-key"], "<redacted>")
        self.assertNotIn("super-secret-key", str(parsed))

    def test_cookie_header_redacted(self):
        parsed = parse_curl(
            'curl https://api.example.com/vehicles -H "Cookie: session=abc123"'
        )
        self.assertEqual(parsed["headers"]["Cookie"], "<redacted>")

    def test_basic_auth_flag_redacted(self):
        parsed = parse_curl('curl https://api.example.com/vehicles -u admin:hunter2')
        self.assertEqual(parsed["headers"]["Authorization"], "<redacted>")
        self.assertNotIn("hunter2", str(parsed))

    def test_cookie_flag_redacted(self):
        parsed = parse_curl('curl https://api.example.com/vehicles -b "session=abc123"')
        self.assertEqual(parsed["headers"]["Cookie"], "<redacted>")

    def test_non_secret_header_not_redacted(self):
        parsed = parse_curl(
            'curl https://api.example.com/vehicles -H "Content-Type: application/json"'
        )
        self.assertEqual(parsed["headers"]["Content-Type"], "application/json")


class TestGenerateFromCurl(unittest.TestCase):
    """generate_from_curl must never leak secrets into the LLM prompt."""

    def _make_generator(self, response_text="Feature: Stub\n\nScenario: x"):
        with patch("agent.ai.test_generator.LLMClient") as mock_llm_client:
            mock_client = MagicMock()
            mock_client.provider_name = "stub"
            mock_client.generate.return_value = response_text
            mock_llm_client.from_config.return_value = mock_client
            gen = AITestGenerator()
        return gen, mock_client

    def test_prompt_contains_redacted_placeholder_not_real_secret(self):
        gen, mock_client = self._make_generator()
        gen.generate_from_curl(
            'curl -X POST https://api.example.com/register '
            '-H "x-api-key: real-secret-value" -d \'{"vin":"ABC123"}\''
        )
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("<redacted>", prompt)
        self.assertNotIn("real-secret-value", prompt)

    def test_prompt_contains_method_and_url(self):
        gen, mock_client = self._make_generator()
        gen.generate_from_curl('curl https://api.example.com/vehicles')
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("GET", prompt)
        self.assertIn("https://api.example.com/vehicles", prompt)

    def test_adds_feature_line_if_missing(self):
        gen, _ = self._make_generator(response_text="Scenario: no feature line here")
        gherkin = gen.generate_from_curl('curl https://api.example.com/vehicles')
        self.assertTrue(gherkin.startswith("Feature:"))

    def test_applies_tags_to_prompt(self):
        gen, mock_client = self._make_generator()
        gen.generate_from_curl('curl https://api.example.com/vehicles', tags=["api", "regression"])
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("@api", prompt)
        self.assertIn("@regression", prompt)


class TestGenerateFromCurlAndScreenshot(unittest.TestCase):
    """generate_from_curl_and_screenshot must attach the image and never leak secrets."""

    def _make_generator(self, response_text="Feature: Stub\n\nScenario: x"):
        with patch("agent.ai.test_generator.LLMClient") as mock_llm_client:
            mock_client = MagicMock()
            mock_client.provider_name = "stub"
            mock_client.generate.return_value = response_text
            mock_llm_client.from_config.return_value = mock_client
            gen = AITestGenerator()
        return gen, mock_client

    def _make_screenshot(self) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(b"fake-png-bytes")
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_raises_when_screenshot_missing(self):
        gen, _ = self._make_generator()
        with self.assertRaises(FileNotFoundError):
            gen.generate_from_curl_and_screenshot(
                'curl https://api.example.com/vehicles',
                screenshot_path="/does/not/exist.png",
            )

    def test_passes_screenshot_path_as_image(self):
        gen, mock_client = self._make_generator()
        screenshot = self._make_screenshot()
        gen.generate_from_curl_and_screenshot(
            'curl -X POST https://api.example.com/register -d \'{"vin":"ABC123"}\'',
            screenshot_path=screenshot,
        )
        images = mock_client.generate.call_args.kwargs["images"]
        self.assertEqual(images, [screenshot])

    def test_prompt_contains_redacted_placeholder_not_real_secret(self):
        gen, mock_client = self._make_generator()
        screenshot = self._make_screenshot()
        gen.generate_from_curl_and_screenshot(
            'curl -X POST https://api.example.com/register '
            '-H "x-api-key: real-secret-value" -d \'{"vin":"ABC123"}\'',
            screenshot_path=screenshot,
        )
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("<redacted>", prompt)
        self.assertNotIn("real-secret-value", prompt)

    def test_adds_feature_line_if_missing(self):
        gen, _ = self._make_generator(response_text="Scenario: no feature line here")
        screenshot = self._make_screenshot()
        gherkin = gen.generate_from_curl_and_screenshot(
            'curl https://api.example.com/vehicles', screenshot_path=screenshot
        )
        self.assertTrue(gherkin.startswith("Feature:"))

    def test_applies_tags_to_prompt(self):
        gen, mock_client = self._make_generator()
        screenshot = self._make_screenshot()
        gen.generate_from_curl_and_screenshot(
            'curl https://api.example.com/vehicles',
            screenshot_path=screenshot,
            tags=["api", "regression"],
        )
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("@api", prompt)
        self.assertIn("@regression", prompt)


class TestGenerateFromText(unittest.TestCase):
    def _make_generator(self, response_text="Feature: Stub\n\nScenario: x"):
        with patch("agent.ai.test_generator.LLMClient") as mock_llm_client:
            mock_client = MagicMock()
            mock_client.provider_name = "stub"
            mock_client.generate.return_value = response_text
            mock_llm_client.from_config.return_value = mock_client
            gen = AITestGenerator()
        return gen, mock_client

    def test_prompt_contains_requirement_text(self):
        gen, mock_client = self._make_generator()
        gen.generate_from_text("As a partner, I can register a vehicle by VIN")
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("As a partner, I can register a vehicle by VIN", prompt)

    def test_adds_feature_line_if_missing(self):
        gen, _ = self._make_generator(response_text="Scenario: no feature line here")
        gherkin = gen.generate_from_text("Some requirement")
        self.assertTrue(gherkin.startswith("Feature:"))

    def test_default_tags_applied(self):
        gen, mock_client = self._make_generator()
        gen.generate_from_text("Some requirement")
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("@ai_generated", prompt)


if __name__ == "__main__":
    unittest.main()
