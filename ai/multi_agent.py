"""
Multi-Agent QA Pipeline
=======================
Implements a four-agent architecture for fully autonomous QA:

  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
  │   Planner   │────▶│  Generator  │────▶│  Executor   │────▶│  Validator  │
  │             │     │             │     │             │     │             │
  │ Decide what │     │ Generate    │     │ Run Behave  │     │ Analyse     │
  │ to test     │     │ .feature    │     │ & collect   │     │ results &   │
  │             │     │ files via   │     │ results     │     │ suggest     │
  │             │     │ LLM         │     │             │     │ fixes       │
  └─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘

Provider configuration (env vars):
    AI_PROVIDER   — anthropic | openai | stub  (default: anthropic)
    AI_API_KEY    — API key for the selected provider
    AI_MODEL      — model override

Usage:
    python -m ai.multi_agent \\
        --url https://httpbin.org/forms/post \\
        --tags smoke \\
        --headless

Each agent is a plain Python class with a single `.run()` method that accepts
and returns a shared `PipelineContext` dataclass. This keeps the pipeline
composable and easy to extend.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai.llm_client import LLMClient, BaseLLMClient
from ai.test_generator import AITestGenerator
from helpers.constants.framework_constants import ALLURE_RESULTS_DIR
from utils.logger import log_info_emoji, log_failure, log_success, log_warning


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

@dataclass
class PipelineContext:
    """Mutable bag of state passed between agents."""
    target_url: str
    tags: list[str] = field(default_factory=lambda: ["ai_generated"])
    headless: bool = True
    browser: str = "chromium"

    # set by Generator
    feature_path: str = ""
    generated_gherkin: str = ""

    # set by Executor
    exit_code: int = -1
    run_stdout: str = ""

    # set by Validator
    validation_report: str = ""
    suggested_fixes: list[str] = field(default_factory=list)

    # generic metadata
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent base
# ---------------------------------------------------------------------------

class BaseAgent:
    name: str = "BaseAgent"

    def __init__(self, client: BaseLLMClient | None = None) -> None:
        # Allow injection for testing; default to shared config-driven client
        self._llm_client: BaseLLMClient = client or LLMClient.from_config()

    def run(self, ctx: PipelineContext) -> PipelineContext:
        raise NotImplementedError

    def _llm(self, prompt: str, system: str = "", temperature: float = 0.15) -> str:
        """Call the configured LLM and return the response text."""
        return self._llm_client.generate(
            prompt=prompt,
            system=system,
            temperature=temperature,
        )


# ---------------------------------------------------------------------------
# Agent 1: Planner
# ---------------------------------------------------------------------------

class PlannerAgent(BaseAgent):
    """
    Analyses the target URL and decides what kind of tests to generate:
    - Which test categories are relevant (form, API, navigation, etc.)
    - Which Behave tags to assign
    - Any special instructions for the Generator
    """
    name = "Planner"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        log_info_emoji("📋", f"[{self.name}] Planning test strategy for: {ctx.target_url}")

        system = textwrap.dedent("""
            You are a QA architect. Given a URL, decide:
            1. What test categories apply (ui, form, api, navigation, accessibility).
            2. A short plain-English instruction for the test generator.
            3. Appropriate Behave tags (comma-separated, no @).
            Return ONLY a JSON object:
            {"categories": ["ui", "form"], "tags": ["smoke", "regression"], "instruction": "…"}
        """).strip()

        try:
            raw = self._llm(f"URL: {ctx.target_url}", system=system)
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            plan = json.loads(m.group(0)) if m else {}
        except Exception as exc:
            log_warning(f"[{self.name}] LLM planning failed ({exc}); using defaults")
            plan = {}

        # Merge LLM tags with user-requested tags
        extra_tags = [t.lstrip("@") for t in plan.get("tags", [])]
        ctx.tags = list(dict.fromkeys(ctx.tags + extra_tags))  # deduplicate, preserve order
        ctx.metadata["plan"] = plan
        log_info_emoji("✅", f"[{self.name}] Strategy: tags={ctx.tags}, categories={plan.get('categories', '?')}")
        return ctx


# ---------------------------------------------------------------------------
# Agent 2: Generator
# ---------------------------------------------------------------------------

class GeneratorAgent(BaseAgent):
    """
    Uses AITestGenerator to produce a .feature file from the target URL.
    """
    name = "Generator"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        log_info_emoji("⚙️", f"[{self.name}] Generating feature file …")
        gen = AITestGenerator()
        gherkin = gen.generate(url=ctx.target_url, tags=ctx.tags)
        output = f"features/ai_generated_{Path(ctx.target_url).stem or 'page'}.feature"
        gen.save(gherkin, output)
        ctx.feature_path = output
        ctx.generated_gherkin = gherkin
        log_success(f"[{self.name}] Feature saved: {output}")
        return ctx


# ---------------------------------------------------------------------------
# Agent 3: Executor
# ---------------------------------------------------------------------------

class ExecutorAgent(BaseAgent):
    """
    Runs `behave` on the generated feature file and captures results.
    Does not call the LLM — no __init__ override needed.
    """
    name = "Executor"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        log_info_emoji("🚀", f"[{self.name}] Executing: {ctx.feature_path}")

        os.environ["HEADLESS"] = "True" if ctx.headless else "False"
        os.environ["BROWSER"] = ctx.browser

        cmd = [
            sys.executable, "-m", "behave",
            "-f", "allure_behave.formatter:AllureFormatter",
            "-o", ALLURE_RESULTS_DIR,
            "--no-capture",
            ctx.feature_path,
        ]
        if ctx.tags:
            for tag in ctx.tags:
                cmd.extend(["-t", tag.lstrip("@")])

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300
            )
            ctx.exit_code = proc.returncode
            ctx.run_stdout = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            ctx.exit_code = 1
            ctx.run_stdout = "Execution timed out after 5 minutes"

        status = "PASSED" if ctx.exit_code == 0 else "FAILED"
        log_info_emoji("📊", f"[{self.name}] Run {status} (exit={ctx.exit_code})")
        return ctx


# ---------------------------------------------------------------------------
# Agent 4: Validator
# ---------------------------------------------------------------------------

class ValidatorAgent(BaseAgent):
    """
    Analyses run output with the LLM to identify failure root causes
    and suggest remediation steps.
    """
    name = "Validator"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        log_info_emoji("🔍", f"[{self.name}] Analysing results …")

        if ctx.exit_code == 0:
            ctx.validation_report = "All scenarios passed — no fixes required."
            log_success(f"[{self.name}] {ctx.validation_report}")
            return ctx

        system = textwrap.dedent("""
            You are a QA expert. Given Behave console output, identify:
            1. Which scenarios failed and why.
            2. Concrete fix suggestions (selector changes, step rewrites, etc.).
            Return JSON: {"failures": [{"scenario": "…", "reason": "…", "fix": "…"}]}
        """).strip()

        prompt = (
            f"Behave output:\n{ctx.run_stdout[:4000]}\n\n"
            f"Generated Gherkin:\n{ctx.generated_gherkin[:2000]}"
        )

        try:
            raw = self._llm(prompt, system=system)
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            report = json.loads(m.group(0)) if m else {}
        except Exception as exc:
            log_warning(f"[{self.name}] LLM validation failed ({exc})")
            report = {}

        failures = report.get("failures", [])
        ctx.suggested_fixes = [f["fix"] for f in failures if "fix" in f]
        ctx.validation_report = raw

        if failures:
            log_warning(f"[{self.name}] {len(failures)} failure(s) identified")
            for f in failures:
                log_failure(f"  ▸ {f.get('scenario', '?')}: {f.get('reason', '?')}")
                log_info_emoji("💡", f"  Fix: {f.get('fix', '?')}")
        return ctx


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

class QAPipeline:
    """
    Chains Planner → Generator → Executor → Validator.

    Each agent receives the same PipelineContext and enriches it.
    The pipeline can be extended by inserting additional agents.
    """

    def __init__(self, agents: list[BaseAgent] | None = None):
        self.agents = agents or [
            PlannerAgent(),
            GeneratorAgent(),
            ExecutorAgent(),
            ValidatorAgent(),
        ]

    def run(self, url: str, tags: list[str] | None = None,
            headless: bool = True, browser: str = "chromium") -> PipelineContext:
        ctx = PipelineContext(
            target_url=url,
            tags=list(tags or ["ai_generated", "smoke"]),
            headless=headless,
            browser=browser,
        )
        log_info_emoji("🤖", f"QA Pipeline starting — {len(self.agents)} agent(s)")
        for agent in self.agents:
            ctx = agent.run(ctx)
        log_info_emoji("🏁", "QA Pipeline complete")
        return ctx


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description="Multi-agent QA pipeline")
    p.add_argument("--url", required=True, help="Target page URL")
    p.add_argument("--tags", nargs="*", default=["ai_generated"],
                   help="Behave tags to apply")
    p.add_argument("--headless", action="store_true", default=True,
                   help="Run browser headless (default: True)")
    p.add_argument("--browser", choices=["chromium", "firefox", "webkit"],
                   default="chromium")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    pipeline = QAPipeline()
    result = pipeline.run(
        url=args.url,
        tags=args.tags,
        headless=args.headless,
        browser=args.browser,
    )
    print("\n--- Validation Report ---")
    print(result.validation_report or "(none)")
    if result.suggested_fixes:
        print("\n--- Suggested Fixes ---")
        for fix in result.suggested_fixes:
            print(f"  • {fix}")
    sys.exit(0 if result.exit_code == 0 else 1)
