"""
AI Test Generator
=================
Generates Gherkin BDD scenarios from a live page by:
  1. Navigating to a URL with Playwright (headless)
  2. Extracting semantic HTML + visible text
  3. Sending to Ollama with a structured prompt
  4. Parsing the returned Gherkin and writing a .feature file

Usage (standalone CLI):
    python -m ai.test_generator \\
        --url https://httpbin.org/forms/post \\
        --feature features/ai_generated.feature \\
        --tags smoke regression

Usage (programmatic):
    from ai.test_generator import AITestGenerator
    gen = AITestGenerator()
    feature_text = gen.generate(url="https://example.com", tags=["smoke"])
    gen.save(feature_text, "features/ai_generated.feature")
"""
from __future__ import annotations

import argparse
import os
import re
import textwrap
from pathlib import Path

import ollama
from playwright.sync_api import sync_playwright

from utils.logger import log_info_emoji, log_failure, log_success
from utils.misc import load_config

_SYSTEM_PROMPT = textwrap.dedent("""
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


class AITestGenerator:
    """Generates Gherkin feature files from live pages using an LLM."""

    def __init__(self, model: str | None = None):
        cfg = load_config()
        self.model = model or cfg.get("ai_model", "devstral:24b")

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

        prompt = self._build_prompt(url, cleaned, tag_line)
        log_info_emoji("🧠", f"Generating scenarios with model '{self.model}' …")
        gherkin = self._call_llm(prompt)

        # Ensure the output starts with a Feature: line
        if not re.search(r"^\s*Feature:", gherkin, re.MULTILINE):
            domain = re.sub(r"https?://|/.*", "", url)
            gherkin = f"Feature: Auto-generated tests for {domain}\n\n{gherkin}"

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
    # Private helpers
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

    def _build_prompt(self, url: str, html: str, tag_line: str) -> str:
        return (
            f"URL: {url}\n\n"
            f"Behave tags to apply to every scenario: {tag_line}\n\n"
            f"HTML (truncated):\n{html}\n\n"
            "Generate a complete Gherkin feature file for the page above."
        )

    def _call_llm(self, prompt: str) -> str:
        response = ollama.generate(
            model=self.model,
            prompt=prompt,
            system=_SYSTEM_PROMPT,
            stream=False,
            options={"temperature": 0.2},
        )
        return response.response.strip()

    @staticmethod
    def _count_scenarios(gherkin: str) -> int:
        return len(re.findall(r"^\s*Scenario", gherkin, re.MULTILINE))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description="Generate Gherkin from a live page")
    p.add_argument("--url", required=True, help="Page URL to analyse")
    p.add_argument("--feature", default="features/ai_generated.feature",
                   help="Output .feature file path")
    p.add_argument("--tags", nargs="*", default=["ai_generated", "smoke"],
                   help="Tags to apply to all generated scenarios")
    p.add_argument("--model", default=None, help="Ollama model override")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        gen = AITestGenerator(model=args.model)
        gherkin = gen.generate(url=args.url, tags=args.tags)
        gen.save(gherkin, args.feature)
    except Exception as exc:
        log_failure(f"Test generation failed: {exc}")
        raise SystemExit(1) from exc
