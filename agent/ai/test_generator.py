"""
AI Test Generator
=================
Generates Gherkin BDD scenarios from one of four input sources:
  1. A live page URL   — Playwright fetches the HTML, LLM writes UI scenarios.
  2. A cURL command    — parsed into method/url/headers/body, LLM writes API
                          scenarios in this project's vehicle_api.feature style.
  3. A cURL command + a UI screenshot — the parsed request is combined with a
                          vision-model read of the screenshot so the LLM can
                          name real UI elements (buttons, field labels) in the
                          Given/When steps while still asserting on the API
                          response in Then steps.
  4. A plaintext requirement — sent to the LLM as-is, LLM writes BDD scenarios.

See docs/ai-test-generation-guide.md for the full guideline on writing good
cURL/plaintext inputs and reviewing the generated output before committing it.

Provider configuration (env vars):
    AI_PROVIDER   — anthropic | openai | stub  (default: anthropic)
    AI_API_KEY    — API key for the selected provider
    AI_MODEL      — model override (use a vision-capable model for --screenshot,
                    e.g. claude-3-5-sonnet-20241022 or gpt-4o)

Usage (standalone CLI):
    python -m agent.ai.test_generator \\
        --url https://httpbin.org/forms/post \\
        --feature e2e/features/ai_generated.feature \\
        --tags smoke regression

    python -m agent.ai.test_generator \\
        --curl 'curl -X POST https://api.example.com/register -H "x-api-key: secret" -d "{\\"vin\\":\\"ABC123\\"}"' \\
        --feature e2e/features/ai_generated_register.feature \\
        --tags api regression

    python -m agent.ai.test_generator \\
        --curl 'curl -X POST https://api.example.com/register -H "x-api-key: secret" -d "{\\"vin\\":\\"ABC123\\"}"' \\
        --screenshot reports/screenshots/register-form.png \\
        --feature e2e/features/ai_generated_register.feature \\
        --tags api regression

    python -m agent.ai.test_generator \\
        --text "As a partner, I can register a vehicle by VIN and receive a transaction id" \\
        --feature e2e/features/ai_generated_register.feature

Usage (programmatic):
    from agent.ai.test_generator import AITestGenerator
    gen = AITestGenerator()
    feature_text = gen.generate(url="https://example.com", tags=["smoke"])
    gen.save(feature_text, "e2e/features/ai_generated.feature")

    feature_text = gen.generate_from_curl_and_screenshot(
        curl_command='curl -X POST https://api.example.com/register -d "{\\"vin\\":\\"ABC123\\"}"',
        screenshot_path="reports/screenshots/register-form.png",
        tags=["api", "smoke"],
    )
"""
from __future__ import annotations

import argparse
import re
import shlex
import textwrap
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.ai.llm_client import LLMClient
from utils.logger import log_info_emoji, log_failure, log_success

_SYSTEM_PROMPT_URL = textwrap.dedent("""
    You are a senior QA automation engineer who writes Gherkin BDD feature files.
    Given HTML from a web page, produce 3–5 realistic Scenario blocks that cover
    the most important user interactions. Use these rules:
    - Feature name should describe the page purpose.
    - Each Scenario has a clear title.
    - Use Given/When/Then/And keywords correctly.
    - Use Scenario Outline + Examples for data-driven cases where appropriate.
    - Return ONLY valid Gherkin — no prose, no markdown fences.
    - Tag each scenario with the tags provided.
""").strip()

_SYSTEM_PROMPT_CURL = textwrap.dedent("""
    You are a senior QA automation engineer who writes Gherkin BDD feature
    files for REST APIs, matching this project's existing API test style:
    - A `Background:` step initialises the API client.
    - Scenarios assert on HTTP status code, JSON body validity, and any
      transaction/reference identifier in the response.
    - Include one happy-path scenario, one validation-error scenario
      (e.g. a missing required field returning 400), and one
      authentication-failure scenario if the request carries credentials.
    - Use Given/When/Then/And keywords correctly.
    - Return ONLY valid Gherkin — no prose, no markdown fences.
    - Tag each scenario with the tags provided.
    - Any header value shown as "<redacted>" is a placeholder for a real
      secret — never invent or echo a credential value in the scenarios.
""").strip()

_SYSTEM_PROMPT_CURL_SCREENSHOT = textwrap.dedent("""
    You are a senior QA automation engineer who writes Gherkin BDD feature
    files by combining a REST API request with a screenshot of the UI that
    triggers it, matching this project's existing test style:
    - A `Background:` step initialises the API client.
    - Use the screenshot to identify real UI elements — button labels, field
      names, form titles — and phrase Given/When steps around that actual
      user flow instead of generic placeholders.
    - Then steps assert on the API side: HTTP status code, JSON body
      validity, and any transaction/reference identifier in the response.
    - Include one happy-path scenario, one validation-error scenario
      (e.g. a missing required field returning 400), and one
      authentication-failure scenario if the request carries credentials.
    - Use Given/When/Then/And keywords correctly.
    - Return ONLY valid Gherkin — no prose, no markdown fences.
    - Tag each scenario with the tags provided.
    - Any header value shown as "<redacted>" is a placeholder for a real
      secret — never invent or echo a credential value in the scenarios.
""").strip()

_SYSTEM_PROMPT_TEXT = textwrap.dedent("""
    You are a senior QA automation engineer who writes Gherkin BDD feature
    files from plain-English requirements.
    - Feature name should summarise the requirement.
    - Produce 3–5 realistic Scenario blocks: the happy path plus edge cases
      and negative cases reasonably implied by the requirement.
    - Use Given/When/Then/And keywords correctly.
    - Use Scenario Outline + Examples for data-driven cases where appropriate.
    - Return ONLY valid Gherkin — no prose, no markdown fences.
    - Tag each scenario with the tags provided.
""").strip()

# Header names whose values must never reach the LLM, a log line, or a
# saved .feature file — replaced with a placeholder before anything else.
_SECRET_HEADER_NAMES = {
    "authorization", "x-api-key", "api-key", "x-auth-token", "cookie",
}
_REDACTED = "<redacted>"


class AITestGenerator:
    """Generates Gherkin feature files from a URL, a cURL command, or plaintext."""

    def __init__(self):
        self._client = LLMClient.from_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, url: str, tags: list[str] | None = None) -> str:
        """
        Navigate to *url*, extract HTML, and return a Gherkin feature string.
        """
        tags = tags or ["ai_generated"]
        tag_line = "  ".join(f"@{t.lstrip('@')}" for t in tags)

        log_info_emoji("🌐", f"Fetching page HTML: {url}")
        html = self._fetch_html(url)
        cleaned = self._clean_html(html)

        prompt = self._build_url_prompt(url, cleaned, tag_line)
        log_info_emoji("🧠", f"Generating scenarios via {self._client.provider_name} …")
        gherkin = self._client.generate(
            prompt=prompt,
            system=_SYSTEM_PROMPT_URL,
            temperature=0.2,
        )
        gherkin = gherkin.strip()

        # Ensure the output starts with a Feature: line
        if not re.search(r"^\s*Feature:", gherkin, re.MULTILINE):
            domain = re.sub(r"https?://|/.*", "", url)
            gherkin = f"Feature: Auto-generated tests for {domain}\n\n{gherkin}"

        log_success(f"Generated {self._count_scenarios(gherkin)} scenario(s)")
        return gherkin

    def generate_from_curl(self, curl_command: str, tags: list[str] | None = None) -> str:
        """
        Parse a cURL command (method/url/headers/body — secrets redacted)
        and return a Gherkin feature string covering that API endpoint.
        """
        tags = tags or ["api", "ai_generated"]
        tag_line = "  ".join(f"@{t.lstrip('@')}" for t in tags)

        parsed = parse_curl(curl_command)
        log_info_emoji("🧾", f"Parsed cURL: {parsed['method']} {parsed['url']}")

        prompt = self._build_curl_prompt(parsed, tag_line)
        log_info_emoji("🧠", f"Generating API scenarios via {self._client.provider_name} …")
        gherkin = self._client.generate(
            prompt=prompt,
            system=_SYSTEM_PROMPT_CURL,
            temperature=0.2,
        )
        gherkin = gherkin.strip()

        if not re.search(r"^\s*Feature:", gherkin, re.MULTILINE):
            gherkin = f"Feature: Auto-generated API tests for {parsed['url']}\n\n{gherkin}"

        log_success(f"Generated {self._count_scenarios(gherkin)} scenario(s)")
        return gherkin

    def generate_from_curl_and_screenshot(
        self,
        curl_command: str,
        screenshot_path: str,
        tags: list[str] | None = None,
    ) -> str:
        """
        Parse a cURL command (method/url/headers/body — secrets redacted)
        and combine it with a UI screenshot of the page that triggers the
        request, so the LLM can name real UI elements in Given/When steps
        while still asserting on the API response in Then steps.
        """
        if not Path(screenshot_path).is_file():
            raise FileNotFoundError(f"Screenshot not found: {screenshot_path}")

        tags = tags or ["api", "ai_generated"]
        tag_line = "  ".join(f"@{t.lstrip('@')}" for t in tags)

        parsed = parse_curl(curl_command)
        log_info_emoji("🧾", f"Parsed cURL: {parsed['method']} {parsed['url']}")
        log_info_emoji("🖼️", f"Attaching screenshot: {screenshot_path}")

        prompt = self._build_curl_prompt(parsed, tag_line) + (
            "\n\nA screenshot of the UI that triggers this request is attached. "
            "Use it to name real UI elements (labels, buttons, field names) "
            "in the Given/When steps."
        )
        log_info_emoji(
            "🧠", f"Generating UI+API scenarios via {self._client.provider_name} …"
        )
        gherkin = self._client.generate(
            prompt=prompt,
            system=_SYSTEM_PROMPT_CURL_SCREENSHOT,
            images=[screenshot_path],
            temperature=0.2,
        )
        gherkin = gherkin.strip()

        if not re.search(r"^\s*Feature:", gherkin, re.MULTILINE):
            gherkin = f"Feature: Auto-generated UI+API tests for {parsed['url']}\n\n{gherkin}"

        log_success(f"Generated {self._count_scenarios(gherkin)} scenario(s)")
        return gherkin

    def generate_from_text(self, description: str, tags: list[str] | None = None) -> str:
        """
        Send a plaintext requirement straight to the LLM and return a
        Gherkin feature string.
        """
        tags = tags or ["ai_generated"]
        tag_line = "  ".join(f"@{t.lstrip('@')}" for t in tags)

        log_info_emoji("📝", "Generating scenarios from plaintext requirement …")
        prompt = self._build_text_prompt(description, tag_line)
        gherkin = self._client.generate(
            prompt=prompt,
            system=_SYSTEM_PROMPT_TEXT,
            temperature=0.2,
        )
        gherkin = gherkin.strip()

        if not re.search(r"^\s*Feature:", gherkin, re.MULTILINE):
            gherkin = f"Feature: Auto-generated tests\n\n{gherkin}"

        log_success(f"Generated {self._count_scenarios(gherkin)} scenario(s)")
        return gherkin

    def save(self, gherkin: str, output_path: str) -> Path:
        """Write Gherkin to *output_path* and return the resolved Path."""
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(gherkin, encoding="utf-8")
        log_success(f"Feature file written: {p}")
        return p

    # ------------------------------------------------------------------
    # Private helpers — URL mode
    # ------------------------------------------------------------------

    def _fetch_html(self, url: str) -> str:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            html = page.content()
            browser.close()
        return html

    @staticmethod
    def _clean_html(html: str) -> str:
        """
        Strip scripts, styles, and noise; keep semantic tags + attributes
        that are useful to the LLM (id, name, type, placeholder, aria-label).
        """
        # Remove script / style blocks
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML comments
        html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
        # Collapse whitespace
        html = re.sub(r"\s{2,}", " ", html)
        # Trim to 6 000 chars so it fits in the context window
        return html[:6000]

    def _build_url_prompt(self, url: str, html: str, tag_line: str) -> str:
        return (
            f"URL: {url}\n\n"
            f"Behave tags to apply to every scenario: {tag_line}\n\n"
            f"HTML (truncated):\n{html}\n\n"
            "Generate a complete Gherkin feature file for the page above."
        )

    # ------------------------------------------------------------------
    # Private helpers — cURL mode
    # ------------------------------------------------------------------

    def _build_curl_prompt(self, parsed: dict, tag_line: str) -> str:
        headers_str = "\n".join(f"  {k}: {v}" for k, v in parsed["headers"].items()) or "  (none)"
        body_str = parsed["body"] or "(none)"
        return (
            f"HTTP method: {parsed['method']}\n"
            f"URL: {parsed['url']}\n\n"
            f"Headers:\n{headers_str}\n\n"
            f"Body:\n{body_str}\n\n"
            f"Behave tags to apply to every scenario: {tag_line}\n\n"
            "Generate a complete Gherkin feature file testing this API endpoint."
        )

    # ------------------------------------------------------------------
    # Private helpers — plaintext mode
    # ------------------------------------------------------------------

    def _build_text_prompt(self, description: str, tag_line: str) -> str:
        return (
            f"Requirement:\n{description.strip()}\n\n"
            f"Behave tags to apply to every scenario: {tag_line}\n\n"
            "Generate a complete Gherkin feature file for the requirement above."
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _count_scenarios(gherkin: str) -> int:
        return len(re.findall(r"^\s*Scenario", gherkin, re.MULTILINE))


# ---------------------------------------------------------------------------
# cURL parsing (module-level, pure function — no LLM/network calls)
# ---------------------------------------------------------------------------

def parse_curl(curl_command: str) -> dict:
    """
    Parse a cURL command into {"method", "url", "headers", "body"}.

    Secret-bearing header values (Authorization, x-api-key, Cookie, ...) and
    -u/--user credentials are replaced with "<redacted>" — the real values
    never reach the returned dict, so they can't leak into a prompt, a log
    line, or a saved .feature file.
    """
    tokens = shlex.split(curl_command.strip())
    if tokens and tokens[0] == "curl":
        tokens = tokens[1:]

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    body: str | None = None
    has_data = False

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-X", "--request"):
            i += 1
            method = tokens[i]
        elif tok in ("-H", "--header"):
            i += 1
            name, _, value = tokens[i].partition(":")
            name = name.strip()
            value = value.strip()
            if name.lower() in _SECRET_HEADER_NAMES:
                value = _REDACTED
            headers[name] = value
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"):
            i += 1
            body = tokens[i]
            has_data = True
        elif tok in ("-u", "--user"):
            i += 1
            headers["Authorization"] = _REDACTED
        elif tok in ("-b", "--cookie"):
            i += 1
            headers["Cookie"] = _REDACTED
        elif tok == "--url":
            i += 1
            url = tokens[i]
        elif not tok.startswith("-") and url is None:
            url = tok
        i += 1

    if url is None:
        raise ValueError(f"Could not find a URL in cURL command: {curl_command!r}")

    if method is None:
        method = "POST" if has_data else "GET"

    return {"method": method.upper(), "url": url, "headers": headers, "body": body}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(
        description="Generate Gherkin from a live page, a cURL command "
                     "(optionally paired with a UI screenshot), or a plaintext requirement"
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Page URL to analyse")
    source.add_argument("--curl", help="A cURL command describing an API request")
    source.add_argument("--text", help="A plaintext requirement description")
    p.add_argument("--screenshot",
                   help="Path to a UI screenshot to combine with --curl for a "
                        "combined UI+API feature (requires --curl)")
    p.add_argument("--feature", default="e2e/features/ai_generated.feature",
                   help="Output .feature file path")
    p.add_argument("--tags", nargs="*", default=["ai_generated", "smoke"],
                   help="Tags to apply to all generated scenarios")
    args = p.parse_args()
    if args.screenshot and not args.curl:
        p.error("--screenshot requires --curl")
    return args


if __name__ == "__main__":
    args = _parse_args()
    try:
        gen = AITestGenerator()
        if args.curl and args.screenshot:
            gherkin = gen.generate_from_curl_and_screenshot(
                args.curl, args.screenshot, tags=args.tags
            )
        elif args.curl:
            gherkin = gen.generate_from_curl(args.curl, tags=args.tags)
        elif args.text:
            gherkin = gen.generate_from_text(args.text, tags=args.tags)
        else:
            gherkin = gen.generate(url=args.url, tags=args.tags)
        gen.save(gherkin, args.feature)
    except Exception as exc:
        log_failure(f"Test generation failed: {exc}")
        raise SystemExit(1) from exc
