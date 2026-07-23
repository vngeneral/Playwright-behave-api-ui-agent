"""
AI Test Generator
=================
Generates Gherkin BDD scenarios from one of four input sources:
  1. A live page URL   — Playwright fetches the HTML, LLM writes UI scenarios.
  2. A cURL command    — parsed into method/url/headers/body, LLM writes API
                          scenarios in this project's vehicle_api.feature style.
  3. A UI screenshot   — a vision-model read of the screenshot, LLM writes UI
                          scenarios naming real elements (buttons, field
                          labels, form titles) instead of generic placeholders.
  4. A plaintext requirement — sent to the LLM as-is, LLM writes BDD scenarios.

See docs/ai-test-generation-guide.md for the full guideline on writing good
cURL/plaintext inputs and reviewing the generated output before committing it.

Provider configuration (env vars):
    AI_PROVIDER   — anthropic | openai | stub  (default: anthropic)
    AI_API_KEY    — API key for the selected provider
    AI_MODEL      — model override (use a vision-capable model for --screenshot,
                    e.g. claude-3-5-sonnet-20241022 or gpt-4o)

Usage (standalone CLI, single file):
    Each source argument below is a path to a plain .txt file holding the
    actual value — a URL, a raw cURL command (single- or multi-line, exactly
    as pasted from a browser's "Copy as cURL"), or a plaintext requirement —
    so nothing needs hand-escaping into the shell command itself. --screenshot
    is already a filepath (an image), unchanged.

    python -m agent.ai.test_generator \\
        --url url.txt \\
        --feature e2e/features/ai_generated.feature \\
        --tags smoke regression

    python -m agent.ai.test_generator \\
        --curl curl.txt \\
        --feature e2e/features/ai_generated_register.feature \\
        --tags api regression

    python -m agent.ai.test_generator \\
        --screenshot reports/screenshots/register-form.png \\
        --feature e2e/features/ai_generated_register.feature \\
        --tags smoke regression

    python -m agent.ai.test_generator \\
        --text requirement.txt \\
        --feature e2e/features/ai_generated_register.feature

Usage (standalone CLI, batch folder):
    Pass a folder instead of a file to any of --url/--curl/--screenshot/--text
    and every matching file inside it (.txt for url/curl/text, an image
    extension for screenshot) is processed one by one — one input file
    produces one .feature file, named after the input file's stem, written
    into --output-dir. A failure on one file is logged and skipped; it never
    aborts the rest of the batch.

    python -m agent.ai.test_generator \\
        --curl curls/ \\
        --output-dir e2e/features/ai_generated \\
        --tags api regression

Usage (programmatic):
    from agent.ai.test_generator import AITestGenerator
    gen = AITestGenerator()
    feature_text = gen.generate(url="https://example.com", tags=["smoke"])
    gen.save(feature_text, "e2e/features/ai_generated.feature")

    feature_text = gen.generate_from_screenshot(
        screenshot_path="reports/screenshots/register-form.png",
        tags=["smoke"],
    )

    # Batch: one .feature per file in curls/
    results = gen.generate_batch(mode="curl", input_dir="curls", output_dir="e2e/features/ai_generated")
    failed = [r for r in results if not r.ok]
"""
from __future__ import annotations

import argparse
import re
import shlex
import textwrap
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.ai.llm_client import LLMClient
from utils.logger import log_failure, log_info_emoji, log_success, log_warning

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

_SYSTEM_PROMPT_SCREENSHOT = textwrap.dedent("""
    You are a senior QA automation engineer who writes Gherkin BDD feature
    files from a screenshot of a web page. Use these rules:
    - Feature name should describe the page purpose.
    - Use the screenshot to identify real UI elements — button labels, field
      names, form titles — and phrase Given/When/Then steps around that
      actual layout instead of generic placeholders.
    - Each Scenario has a clear title.
    - Use Given/When/Then/And keywords correctly.
    - Use Scenario Outline + Examples for data-driven cases where appropriate.
    - Return ONLY valid Gherkin — no prose, no markdown fences.
    - Tag each scenario with the tags provided.
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

# Batch mode name -> (AITestGenerator method name, allowed file extensions).
# Shared by generate_batch() and the CLI so both dispatch identically.
_BATCH_MODES: dict[str, tuple[str, frozenset[str]]] = {
    "url": ("generate", frozenset({".txt"})),
    "curl": ("generate_from_curl", frozenset({".txt"})),
    "text": ("generate_from_text", frozenset({".txt"})),
    "screenshot": ("generate_from_screenshot", frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})),
}


@dataclass
class BatchItemResult:
    """Outcome of generating one .feature file from one input file in a batch run."""
    input_path: Path
    output_path: Path | None
    scenario_count: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class AITestGenerator:
    """Generates Gherkin feature files from a URL, a cURL command, a screenshot, or plaintext."""

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

    def generate_from_screenshot(self, screenshot_path: str, tags: list[str] | None = None) -> str:
        """
        Read a UI screenshot with a vision-capable model and return a
        Gherkin feature string covering the interactions visible in it.
        """
        if not Path(screenshot_path).is_file():
            raise FileNotFoundError(f"Screenshot not found: {screenshot_path}")

        tags = tags or ["ai_generated"]
        tag_line = "  ".join(f"@{t.lstrip('@')}" for t in tags)

        log_info_emoji("🖼️", f"Reading screenshot: {screenshot_path}")

        prompt = self._build_screenshot_prompt(tag_line)
        log_info_emoji("🧠", f"Generating UI scenarios via {self._client.provider_name} …")
        gherkin = self._client.generate(
            prompt=prompt,
            system=_SYSTEM_PROMPT_SCREENSHOT,
            images=[screenshot_path],
            temperature=0.2,
        )
        gherkin = gherkin.strip()

        if not re.search(r"^\s*Feature:", gherkin, re.MULTILINE):
            name = Path(screenshot_path).stem
            gherkin = f"Feature: Auto-generated UI tests for {name}\n\n{gherkin}"

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

    def generate_batch(
        self,
        mode: str,
        input_dir: str,
        output_dir: str,
        tags: list[str] | None = None,
    ) -> list[BatchItemResult]:
        """
        Generate one .feature file per input file found directly inside
        *input_dir* (one file = one URL / cURL command / requirement / screenshot),
        writing each into *output_dir* named after the input file's stem.

        *mode* selects which single-item method drives generation: "url",
        "curl", "text", or "screenshot" (see _BATCH_MODES). Files are processed
        in filename order for a deterministic, re-runnable batch. A failure on
        one file — an unparsable cURL command, an empty file, an LLM error —
        is recorded on that file's BatchItemResult and does NOT abort the rest
        of the batch; check `result.ok` / `result.error` on each item.
        """
        if mode not in _BATCH_MODES:
            raise ValueError(f"Unknown batch mode {mode!r}; expected one of {sorted(_BATCH_MODES)}")

        in_dir = Path(input_dir)
        if not in_dir.is_dir():
            raise NotADirectoryError(f"Batch input directory not found: {input_dir}")

        method_name, suffixes = _BATCH_MODES[mode]
        method = getattr(self, method_name)

        files = sorted(
            (p for p in in_dir.iterdir() if p.is_file() and p.suffix.lower() in suffixes),
            key=lambda p: p.name,
        )
        if not files:
            log_warning(f"No {'/'.join(sorted(suffixes))} files found in {input_dir}")
            return []

        log_info_emoji("📂", f"Batch generating ({mode}): {len(files)} file(s) from {input_dir}")
        results: list[BatchItemResult] = []
        for f in files:
            try:
                if mode == "screenshot":
                    gherkin = method(str(f), tags=tags)
                else:
                    content = _read_input_file(str(f))
                    if not content:
                        raise ValueError("input file is empty")
                    gherkin = method(content, tags=tags)
                out_path = Path(output_dir) / f"{f.stem}.feature"
                self.save(gherkin, str(out_path))
                results.append(BatchItemResult(f, out_path, self._count_scenarios(gherkin)))
            except Exception as exc:
                log_failure(f"[{f.name}] generation failed: {exc}")
                results.append(BatchItemResult(f, None, 0, str(exc)))

        succeeded = sum(r.ok for r in results)
        log_success(f"Batch complete: {succeeded}/{len(results)} succeeded → {output_dir}")
        return results

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
    # Private helpers — screenshot mode
    # ------------------------------------------------------------------

    def _build_screenshot_prompt(self, tag_line: str) -> str:
        return (
            f"Behave tags to apply to every scenario: {tag_line}\n\n"
            "A screenshot of the page is attached. Generate a complete "
            "Gherkin feature file for the UI shown in it."
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
# cURL parsing (module-level, pure functions — no LLM/network calls)
# ---------------------------------------------------------------------------

def normalize_curl_command(raw_curl: str) -> str:
    """
    Transform a raw cURL command — e.g. pasted from a browser's "Copy as
    cURL" or typed across multiple lines with trailing `\\` continuations —
    into the single logical line `parse_curl()` expects, so callers never
    have to hand-collapse it first.
    """
    joined = re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", raw_curl)
    return " ".join(joined.split())


def parse_curl(curl_command: str) -> dict:
    """
    Parse a cURL command into {"method", "url", "headers", "body"}.

    Secret-bearing header values (Authorization, x-api-key, Cookie, ...) and
    -u/--user credentials are replaced with "<redacted>" — the real values
    never reach the returned dict, so they can't leak into a prompt, a log
    line, or a saved .feature file.
    """
    tokens = shlex.split(normalize_curl_command(curl_command))
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

def _read_input_file(path: str) -> str:
    """
    Read a CLI source argument's backing .txt file (--url/--curl/--text) and
    return its content, stripped of the trailing newline a text editor adds.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")
    return p.read_text(encoding="utf-8").strip()


_FEATURE_DEFAULT = "e2e/features/ai_generated.feature"
_OUTPUT_DIR_DEFAULT = "e2e/features/ai_generated"


def _parse_args():
    p = argparse.ArgumentParser(
        description="Generate Gherkin from a live page, a cURL command, "
                     "a UI screenshot, or a plaintext requirement — each "
                     "read from a .txt file (--screenshot excepted, an image path). "
                     "Pass a folder instead of a file to batch-process every matching "
                     "file inside it, one .feature file per input file."
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", metavar="FILE_OR_DIR",
                         help="Path to a .txt file containing the page URL to analyse, "
                              "or a folder of such .txt files for batch mode")
    source.add_argument("--curl", metavar="FILE_OR_DIR",
                         help="Path to a .txt file containing a cURL command describing an "
                              "API request (single- or multi-line), or a folder of such "
                              ".txt files for batch mode — one file per cURL command")
    source.add_argument("--screenshot", metavar="FILE_OR_DIR",
                         help="Path to a UI screenshot image to analyse, or a folder of "
                              "screenshot images for batch mode")
    source.add_argument("--text", metavar="FILE_OR_DIR",
                         help="Path to a .txt file containing a plaintext requirement "
                              "description, or a folder of such .txt files for batch mode")
    p.add_argument("--feature", default=_FEATURE_DEFAULT,
                   help="Output .feature file path (single-file mode only)")
    p.add_argument("--output-dir", default=_OUTPUT_DIR_DEFAULT,
                   help="Output directory for batch mode — used when the --url/--curl/"
                        "--screenshot/--text argument is a folder. One .feature file is "
                        "written per input file, named after that file's stem")
    p.add_argument("--tags", nargs="*", default=["ai_generated", "smoke"],
                   help="Tags to apply to all generated scenarios")
    return p.parse_args()


def _resolve_source(args) -> tuple[str, str]:
    """Return (path, mode) for whichever mutually-exclusive source arg was passed."""
    for mode in ("curl", "screenshot", "text", "url"):
        value = getattr(args, mode)
        if value:
            return value, mode
    raise AssertionError("argparse mutually-exclusive group should guarantee one source")  # pragma: no cover


if __name__ == "__main__":
    args = _parse_args()
    try:
        gen = AITestGenerator()
        source, mode = _resolve_source(args)

        if Path(source).is_dir():
            if args.feature != _FEATURE_DEFAULT:
                log_warning(f"--feature is ignored in batch mode; writing into --output-dir={args.output_dir}")

            results = gen.generate_batch(mode=mode, input_dir=source, output_dir=args.output_dir, tags=args.tags)
            failed = [r for r in results if not r.ok]
            if failed:
                for r in failed:
                    log_failure(f"  {r.input_path.name}: {r.error}")
                raise SystemExit(1)
        else:
            method_name, _ = _BATCH_MODES[mode]
            method = getattr(gen, method_name)
            if mode == "screenshot":
                gherkin = method(source, tags=args.tags)
            else:
                gherkin = method(_read_input_file(source), tags=args.tags)
            gen.save(gherkin, args.feature)
    except Exception as exc:
        log_failure(f"Test generation failed: {exc}")
        raise SystemExit(1) from exc
