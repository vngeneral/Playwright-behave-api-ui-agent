"""
Unit tests for agent/ai/test_generator.py
No network calls, no LLM calls — parse_curl is pure, and generate_from_*
methods are tested with a mocked LLM client.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.ai.test_generator import AITestGenerator, parse_curl, normalize_curl_command, _read_input_file


class TestNormalizeCurlCommand(unittest.TestCase):
    """normalize_curl_command must collapse multi-line pasted cURL into one line."""

    def test_single_line_is_unchanged_in_content(self):
        raw = 'curl -X POST https://api.example.com/vehicles -d \'{"vin":"X"}\''
        self.assertEqual(normalize_curl_command(raw), raw)

    def test_joins_backslash_newline_continuations(self):
        raw = (
            'curl -X POST https://api.example.com/register \\\n'
            '  -H "Content-Type: application/json" \\\n'
            '  -H "x-api-key: secret123" \\\n'
            '  -d \'{"vin":"ABC123"}\'\n'
        )
        normalized = normalize_curl_command(raw)
        self.assertNotIn("\\", normalized)
        self.assertNotIn("\n", normalized)
        parsed = parse_curl(normalized)
        self.assertEqual(parsed["method"], "POST")
        self.assertEqual(parsed["url"], "https://api.example.com/register")
        self.assertEqual(parsed["body"], '{"vin":"ABC123"}')

    def test_parse_curl_handles_multiline_input_directly(self):
        raw = (
            'curl -X POST https://api.example.com/register \\\n'
            '  -H "x-api-key: secret123" \\\n'
            '  -d \'{"vin":"ABC123"}\''
        )
        parsed = parse_curl(raw)
        self.assertEqual(parsed["url"], "https://api.example.com/register")
        self.assertEqual(parsed["headers"]["x-api-key"], "<redacted>")


class TestReadInputFile(unittest.TestCase):
    """_read_input_file backs the CLI's --url/--curl/--text file arguments."""

    def _make_file(self, content: str) -> str:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write(content)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_reads_and_strips_trailing_newline(self):
        path = self._make_file("https://api.example.com/vehicles\n")
        self.assertEqual(_read_input_file(path), "https://api.example.com/vehicles")

    def test_raises_when_file_missing(self):
        with self.assertRaises(FileNotFoundError):
            _read_input_file("/does/not/exist.txt")


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


class TestGenerateFromScreenshot(unittest.TestCase):
    """generate_from_screenshot must attach the image and require the file to exist."""

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
            gen.generate_from_screenshot("/does/not/exist.png")

    def test_passes_screenshot_path_as_image(self):
        gen, mock_client = self._make_generator()
        screenshot = self._make_screenshot()
        gen.generate_from_screenshot(screenshot)
        images = mock_client.generate.call_args.kwargs["images"]
        self.assertEqual(images, [screenshot])

    def test_adds_feature_line_if_missing(self):
        gen, _ = self._make_generator(response_text="Scenario: no feature line here")
        screenshot = self._make_screenshot()
        gherkin = gen.generate_from_screenshot(screenshot)
        self.assertTrue(gherkin.startswith("Feature:"))

    def test_applies_tags_to_prompt(self):
        gen, mock_client = self._make_generator()
        screenshot = self._make_screenshot()
        gen.generate_from_screenshot(screenshot, tags=["smoke", "regression"])
        prompt = mock_client.generate.call_args.kwargs["prompt"]
        self.assertIn("@smoke", prompt)
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
