"""
AI Selector Healer
==================
When a Playwright selector fails, this module:
  1. Takes a screenshot + grabs HTML from the live page
  2. Sends both to a cloud LLM via LLMClient (Anthropic Claude or OpenAI)
  3. Parses the suggested selector from the JSON response
  4. Validates the selector against the live DOM
  5. Persists successful mappings in reports/ai/selector_map.json

Provider configuration (env vars):
    AI_PROVIDER   — anthropic | openai | stub  (default: anthropic)
    AI_API_KEY    — API key for the selected provider
    AI_MODEL      — model override (e.g. claude-3-5-sonnet-20241022)

To keep using Ollama locally set:
    AI_PROVIDER=openai
    AI_BASE_URL=http://localhost:11434/v1
    AI_API_KEY=ollama
    AI_MODEL=devstral:24b
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from behave.runner import Context
from playwright.sync_api import Page

from ai.llm_client import LLMClient
from helpers.constants.framework_constants import SCREENSHOTS_DIR, AI_ARTIFACTS_DIR
from utils.logger import log_info_emoji, log_error, log_warning


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> list | dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {} if path.endswith("map.json") else []


def _dump_json(path: str, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class AISelectorHealer:
    """Self-healing selector engine backed by a cloud LLM (Anthropic / OpenAI)."""

    def __init__(self):
        self._client = LLMClient.from_config()
        self.selector_map_file = str(Path(AI_ARTIFACTS_DIR) / "selector_map.json")
        self.log_file = str(Path(AI_ARTIFACTS_DIR) / "selector_log.json")
        self.selector_map: dict = _load_json(self.selector_map_file)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def heal_selector(
        self,
        context: Context,
        exception: str,
        original_selector: str = "",
    ) -> str:
        """
        Attempt to heal a failed selector.

        Returns the healed selector string (may be empty string if healing
        failed — callers should check before using).
        """
        # Fast path: reuse a previously healed mapping
        if original_selector and original_selector in self.selector_map:
            cached = self.selector_map[original_selector]
            log_info_emoji("⚡", f"Using cached healed selector for '{original_selector}'")
            return cached

        step_slug = str(getattr(context, "bdd_step", "unknown")).replace(" ", "_")
        screenshot_path = str(Path(SCREENSHOTS_DIR) / f"ai-{step_slug}.png")
        context.page.screenshot(path=screenshot_path)
        html_snippet = context.page.content()[:8000]

        prompt = self._build_prompt(html_snippet, exception, context, original_selector)
        log_info_emoji("🧠", f"Querying AI ({self._client.provider_name}) for selector healing …")
        ai_response = self._client.generate(
            prompt=prompt,
            system="You are an expert QA automation engineer. Return only valid JSON.",
            images=[screenshot_path],
            temperature=0.1,
        )
        log_info_emoji("🤖", f"AI Response:\n{ai_response}")

        selector, confidence, selector_type, identifier = extract_selector_and_confidence(ai_response)
        key = identifier or original_selector or "unknown"

        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bdd_step": getattr(context, "bdd_step", ""),
            "exception": exception,
            "original_selector": original_selector,
            "healed_selector": selector,
            "selector_identifier": key,
            "confidence": confidence,
            "selector_type": selector_type,
            "valid": False,
            "provider": self._client.provider_name,
        }

        if selector:
            is_valid = validate_selector(context.page, selector, selector_type)
            log_entry["valid"] = is_valid
            if is_valid:
                log_info_emoji("✅", f"Healed selector validated (confidence: {confidence})")
                self._update_selector_map(key, selector)
            else:
                log_info_emoji("❌", "AI-suggested selector not found on page.")
        else:
            log_warning("AI did not return a usable selector.")

        self._append_log(log_entry)
        return selector or ""

    def stop_model(self):
        """
        Release AI resources.

        For cloud providers this is a no-op.
        Previously unloaded the Ollama GPU model; kept for API compatibility.
        """
        log_info_emoji("🧠", f"AI client ({self._client.provider_name}) released")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, html: str, exception: str, context: Context, original_selector: str) -> str:
        return f"""
You are helping debug a failed Playwright web automation test.

Context:
- HTML (first 8 000 chars): {html}
- Playwright exception: {exception}
- BDD step: {getattr(context, 'bdd_step', '')}
- Original (failing) selector: {original_selector}
- Previously healed selectors: {json.dumps(self.selector_map, indent=2)}

Tasks:
1. Identify the element the test was trying to interact with from the HTML and screenshot.
2. Prefer attributes: id > data-testid > name > aria-label > stable XPath.
3. Estimate your confidence (0–100 %).
4. Provide a short snake_case identifier for the selector key.
5. Return ONLY a valid JSON object — no prose, no markdown fencing:

{{
  "selector_identifier": "snake_case_key",
  "selector": "//xpath or css selector",
  "confidence": "95%",
  "selector_type": "xpath"
}}
"""

    def _update_selector_map(self, key: str, selector: str):
        self.selector_map[key] = selector
        _dump_json(self.selector_map_file, self.selector_map)

    def _append_log(self, entry: dict):
        log: list = _load_json(self.log_file)
        log.append(entry)
        _dump_json(self.log_file, log)


# ---------------------------------------------------------------------------
# Parsing utilities
# ---------------------------------------------------------------------------

def extract_selector_and_confidence(
    ai_response: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parse the AI response and return (selector, confidence, selector_type, identifier).

    Always returns a 4-tuple; any element may be None if not found.
    """
    if not ai_response:
        return None, None, None, None

    # 1. Try a raw JSON object (no fences)
    raw_json = re.search(r'\{[^{}]+\}', ai_response, re.DOTALL)
    # 2. Also try fenced ```json … ```
    fenced = re.search(r'```json\s*(\{.*?\})\s*```', ai_response, re.DOTALL)
    json_str = (fenced.group(1) if fenced else None) or (raw_json.group(0) if raw_json else None)

    if json_str:
        try:
            data = json.loads(json_str)
            selector = data.get("selector")
            confidence = data.get("confidence")
            selector_type = str(data.get("selector_type", "")).lower() or None
            identifier = str(data.get("selector_identifier", "")).lower() or None
            return selector, confidence, selector_type, identifier
        except json.JSONDecodeError:
            pass  # fall through to regex heuristics

    # Fallback: regex heuristics
    selector = None
    for pattern in [
        r'`([^`]+)`',
        r'Selector:\s*([^\n]+)',
        r'(//[^\n"\']+)',
        r'"selector":\s*"([^"]+)"',
    ]:
        m = re.search(pattern, ai_response, re.IGNORECASE)
        if m:
            selector = m.group(1).strip()
            break

    confidence = None
    m = re.search(r'(\d{1,3})\s*%', ai_response)
    if m:
        confidence = f"{m.group(1)}%"

    selector_type = _detect_selector_type(selector)

    # derive a simple identifier from the selector
    identifier = None
    if selector:
        identifier = re.sub(r'[^a-z0-9_]', '_', selector.lower())[:40].strip('_') or None

    return selector, confidence, selector_type, identifier


def _detect_selector_type(selector: str | None) -> str | None:
    if not selector:
        return None
    if selector.startswith(("//", "./")):
        return "xpath"
    if "text=" in selector:
        return "text"
    if any(c in selector for c in [".", "#", "[", ":", ">"]):
        return "css"
    return "unknown"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_selector(page: Page, selector: str, selector_type: str | None) -> bool:
    """Return True if the selector matches at least one element on the page."""
    try:
        if selector_type == "xpath":
            return len(page.query_selector_all(f"xpath={selector}")) > 0
        # For CSS and text selectors, use Playwright's locator
        return page.locator(selector).count() > 0
    except Exception:
        return False
