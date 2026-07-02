"""
Test Run Metrics
================
Records per-scenario timing and pass/fail counts.
Writes a JSON summary to reports/metrics/run_metrics.json after every run.

Wired into Behave hooks in environment.py:
    before_all      → metrics.start_run()
    after_scenario  → metrics.record_scenario(scenario)
    after_all       → metrics.finish_run(); metrics.save()

The JSON output is consumed by monitoring/alerts.py to decide
whether to fire Slack notifications.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from helpers.constants.framework_constants import Reporting


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    name: str
    feature: str
    status: str          # "passed" | "failed" | "skipped"
    duration_s: float
    tags: list[str] = field(default_factory=list)
    error_message: Optional[str] = None


@dataclass
class RunMetrics:
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    scenarios: list[ScenarioResult] = field(default_factory=list)
    environment: str = ""
    browser: str = ""

    # derived properties
    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total > 0 else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pass_rate_pct"] = round(self.pass_rate, 1)
        return d


# ---------------------------------------------------------------------------
# Collector (singleton, lives on context.metrics)
# ---------------------------------------------------------------------------

class MetricsCollector:
    """Accumulates scenario results during a Behave run."""

    def __init__(self):
        self._metrics = RunMetrics()
        self._scenario_starts: dict[str, float] = {}

    # -- called from before_all --
    def start_run(self, environment: str = "", browser: str = ""):
        self._metrics.start_time = time.time()
        self._metrics.environment = environment
        self._metrics.browser = browser

    # -- called from before_scenario --
    def start_scenario(self, scenario) -> None:
        self._scenario_starts[scenario.name] = time.perf_counter()

    # -- called from after_scenario --
    def record_scenario(self, scenario) -> None:
        start = self._scenario_starts.pop(scenario.name, time.perf_counter())
        duration = time.perf_counter() - start

        status = str(scenario.status).split(".")[-1].lower()   # "passed", "failed", etc.
        error = None
        if status == "failed":
            for step in scenario.steps:
                if str(step.status).endswith("failed") and step.exception:
                    error = str(step.exception)
                    break

        result = ScenarioResult(
            name=scenario.name,
            feature=scenario.feature.name if hasattr(scenario, "feature") else "",
            status=status,
            duration_s=round(duration, 3),
            # scenario.tags yields behave Tag objects (str subclass requiring
            # a `line` attribute) — coerce to plain str so this dataclass can
            # be safely deepcopy'd/JSON-serialised (dataclasses.asdict() and
            # json.dumps both choke on Tag's copy/reduce protocol).
            tags=[str(t) for t in scenario.tags],
            error_message=error,
        )
        self._metrics.scenarios.append(result)
        self._metrics.total += 1
        if status == "passed":
            self._metrics.passed += 1
        elif status == "failed":
            self._metrics.failed += 1
        else:
            self._metrics.skipped += 1

    # -- called from after_all --
    def finish_run(self) -> RunMetrics:
        self._metrics.end_time = time.time()
        self._metrics.duration_s = round(
            self._metrics.end_time - self._metrics.start_time, 2
        )
        return self._metrics

    def save(self, path: str = Reporting.METRICS_FILE) -> Path:
        """Write the run metrics JSON to *path*."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = self._metrics.to_dict()
        p.write_text(json.dumps(data, indent=2, default=str))
        return p

    @property
    def metrics(self) -> RunMetrics:
        return self._metrics

    def summary_line(self) -> str:
        m = self._metrics
        return (
            f"Total: {m.total} | Passed: {m.passed} | "
            f"Failed: {m.failed} | Skipped: {m.skipped} | "
            f"Pass rate: {m.pass_rate:.1f}%"
        )
