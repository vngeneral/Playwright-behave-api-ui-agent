"""
Unit tests for agent/ai/test_generator.py
No network calls, no LLM calls — parse_curl is pure, and generate_from_*
methods are tested with a mocked LLM client.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.ai.test_generator import (
    AITestGenerator,
    BatchItemResult,
    _read_input_file,
    normalize_curl_command,
    parse_curl,
)


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


class TestGenerateBatch(unittest.TestCase):
    """generate_batch must read a folder of inputs and emit one .feature per file,
    without letting one bad file abort the rest of the batch."""

    def _make_generator(self, response_text="Feature: Stub\n\nScenario: x", side_effect=None):
        with patch("agent.ai.test_generator.LLMClient") as mock_llm_client:
            mock_client = MagicMock()
            mock_client.provider_name = "stub"
            if side_effect is not None:
                mock_client.generate.side_effect = side_effect
            else:
                mock_client.generate.return_value = response_text
            mock_llm_client.from_config.return_value = mock_client
            gen = AITestGenerator()
        return gen, mock_client

    def _tmp_dir(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return Path(tmp)

    def _write(self, directory: Path, name: str, content: str) -> Path:
        p = directory / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_creates_one_feature_file_per_input(self):
        gen, _ = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        self._write(in_dir, "register.txt", "curl https://api.example.com/register")
        self._write(in_dir, "deregister.txt", "curl https://api.example.com/deregister")

        results = gen.generate_batch(mode="curl", input_dir=str(in_dir), output_dir=str(out_dir))

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))
        self.assertTrue((out_dir / "register.feature").is_file())
        self.assertTrue((out_dir / "deregister.feature").is_file())
        self.assertEqual((out_dir / "register.feature").read_text(), "Feature: Stub\n\nScenario: x")

    def test_empty_directory_returns_empty_list_without_raising(self):
        gen, _ = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        results = gen.generate_batch(mode="curl", input_dir=str(in_dir), output_dir=str(out_dir))
        self.assertEqual(results, [])

    def test_non_matching_extensions_are_ignored(self):
        gen, mock_client = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        self._write(in_dir, "register.txt", "curl https://api.example.com/register")
        self._write(in_dir, "notes.md", "not a curl file")
        self._write(in_dir, ".DS_Store", "junk")

        results = gen.generate_batch(mode="curl", input_dir=str(in_dir), output_dir=str(out_dir))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].input_path.name, "register.txt")
        self.assertEqual(mock_client.generate.call_count, 1)

    def test_one_failure_does_not_abort_the_rest_of_the_batch(self):
        gen, _ = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        self._write(in_dir, "a_bad.txt", "curl -X GET -H \"Accept: application/json\"")  # no URL -> ValueError
        self._write(in_dir, "b_good.txt", "curl https://api.example.com/good")

        results = gen.generate_batch(mode="curl", input_dir=str(in_dir), output_dir=str(out_dir))
        by_name = {r.input_path.name: r for r in results}

        self.assertEqual(len(results), 2)
        self.assertFalse(by_name["a_bad.txt"].ok)
        self.assertIn("URL", by_name["a_bad.txt"].error)
        self.assertIsNone(by_name["a_bad.txt"].output_path)
        self.assertTrue(by_name["b_good.txt"].ok)
        self.assertTrue((out_dir / "b_good.feature").is_file())
        self.assertFalse((out_dir / "a_bad.feature").is_file())

    def test_empty_input_file_is_recorded_as_failure(self):
        gen, _ = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        self._write(in_dir, "blank.txt", "   \n  ")

        results = gen.generate_batch(mode="curl", input_dir=str(in_dir), output_dir=str(out_dir))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("empty", results[0].error)

    def test_screenshot_mode_filters_by_image_extension(self):
        gen, mock_client = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        (in_dir / "login.png").write_bytes(b"fake-png-bytes")
        (in_dir / "readme.txt").write_text("should be ignored in screenshot mode")

        results = gen.generate_batch(mode="screenshot", input_dir=str(in_dir), output_dir=str(out_dir))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].input_path.name, "login.png")
        images = mock_client.generate.call_args.kwargs["images"]
        self.assertEqual(images, [str(in_dir / "login.png")])
        self.assertTrue((out_dir / "login.feature").is_file())

    def test_files_processed_in_sorted_filename_order(self):
        gen, mock_client = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        self._write(in_dir, "c.txt", "curl https://ccc.example.com/endpoint")
        self._write(in_dir, "a.txt", "curl https://aaa.example.com/endpoint")
        self._write(in_dir, "b.txt", "curl https://bbb.example.com/endpoint")

        gen.generate_batch(mode="curl", input_dir=str(in_dir), output_dir=str(out_dir))

        prompts = [call.kwargs["prompt"] for call in mock_client.generate.call_args_list]
        order = [next(h for h in ("aaa.", "bbb.", "ccc.") if h in p) for p in prompts]
        self.assertEqual(order, ["aaa.", "bbb.", "ccc."])

    def test_raises_for_missing_input_directory(self):
        gen, _ = self._make_generator()
        with self.assertRaises(NotADirectoryError):
            gen.generate_batch(mode="curl", input_dir="/does/not/exist", output_dir=str(self._tmp_dir()))

    def test_raises_for_unknown_mode(self):
        gen, _ = self._make_generator()
        in_dir = self._tmp_dir()
        with self.assertRaises(ValueError):
            gen.generate_batch(mode="bogus", input_dir=str(in_dir), output_dir=str(self._tmp_dir()))

    def test_applies_tags_to_every_file_in_batch(self):
        gen, mock_client = self._make_generator()
        in_dir, out_dir = self._tmp_dir(), self._tmp_dir()
        self._write(in_dir, "a.txt", "curl https://api.example.com/a")
        self._write(in_dir, "b.txt", "curl https://api.example.com/b")

        gen.generate_batch(mode="curl", input_dir=str(in_dir), output_dir=str(out_dir), tags=["api", "regression"])

        for call in mock_client.generate.call_args_list:
            self.assertIn("@api", call.kwargs["prompt"])
            self.assertIn("@regression", call.kwargs["prompt"])

    def test_batch_item_result_ok_reflects_error_field(self):
        ok_result = BatchItemResult(Path("x.txt"), Path("x.feature"), 3, None)
        failed_result = BatchItemResult(Path("y.txt"), None, 0, "boom")
        self.assertTrue(ok_result.ok)
        self.assertFalse(failed_result.ok)


if __name__ == "__main__":
    unittest.main()
