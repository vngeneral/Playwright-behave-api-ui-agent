"""
Retry Plugin
============
Behave-compatible retry wrapper.

NOTE: Behave does not have a formal plugin API.  This module provides
      helper classes that integrate via environment.py hooks — specifically
      the `before_step` / `after_step` pair — rather than monkey-patching
      Behave internals (which is fragile across versions).

Usage in environment.py:
    from plugins.retry_plugin import RetryPlugin
    retry = RetryPlugin()

    def before_step(context, step):
        retry.before_step(context, step)

    def after_step(context, step):
        retry.after_step(context, step)

Architecture note:
    Browser instance reuse ("browser pooling") across scenarios is intentionally
    NOT implemented here.  Each Behave scenario gets a fresh browser context
    (via BrowserManager.new_page) to ensure full test isolation.  Sharing a
    browser state between scenarios causes flaky, order-dependent failures.
"""
from __future__ import annotations

import time
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from utils.logger import log_warning, log_info_emoji
from utils.misc import load_config

_cfg = load_config().get("retry", {})
_DEFAULT_MAX = int(_cfg.get("max_attempts", 3))
_DEFAULT_DELAY = float(_cfg.get("delay_seconds", 1.0))
_DEFAULT_BACKOFF = float(_cfg.get("backoff_multiplier", 1.5))

_RETRYABLE = (AssertionError, PlaywrightTimeout)


class RetryPlugin:
    """
    Automatically retries failed steps that raise retryable exceptions.

    Retrying happens at the *step* level so the scenario is re-entered
    cleanly from the failing step.  This is appropriate for transient
    network or animation timing issues — NOT for logic bugs.
    """

    def __init__(
        self,
        max_attempts: int = _DEFAULT_MAX,
        delay: float = _DEFAULT_DELAY,
        backoff: float = _DEFAULT_BACKOFF,
        retryable_exceptions: tuple = _RETRYABLE,
    ):
        self.max_attempts = max_attempts
        self.delay = delay
        self.backoff = backoff
        self.retryable_exceptions = retryable_exceptions
        self._attempt_counts: dict[str, int] = {}

    def before_step(self, context, step) -> None:
        """Reset attempt counter at the start of each step."""
        self._attempt_counts[step.name] = 0

    def after_step(self, context, step) -> None:
        """
        If a step failed with a retryable exception, replay it up to
        max_attempts times.  This modifies step.status in-place so
        Behave treats the scenario as recovered on success.
        """
        if str(step.status).endswith("failed") and isinstance(
            getattr(step, "exception", None), self.retryable_exceptions
        ):
            attempts = self._attempt_counts.get(step.name, 0)
            if attempts < self.max_attempts - 1:
                wait = self.delay * (self.backoff ** attempts)
                log_warning(
                    f"[retry] Step '{step.name}' failed "
                    f"(attempt {attempts + 1}/{self.max_attempts}). "
                    f"Retrying in {wait:.1f}s …"
                )
                time.sleep(wait)
                self._attempt_counts[step.name] = attempts + 1
                # Re-run the step function directly
                try:
                    step.run(context)
                except Exception:
                    pass  # final failure will propagate naturally
            else:
                log_warning(
                    f"[retry] Step '{step.name}' exhausted {self.max_attempts} attempts."
                )


def retryable(
    max_attempts: int = _DEFAULT_MAX,
    delay: float = _DEFAULT_DELAY,
    exceptions: tuple = _RETRYABLE,
) -> Callable:
    """
    Decorator for plain helper functions that should be retried on failure.

    Usage:
        @retryable(max_attempts=3)
        def click_submit(page):
            page.click("#submit")
    """
    def decorator(fn: Callable) -> Callable:
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        log_warning(
                            f"[retryable] {fn.__name__} attempt {attempt}/{max_attempts} "
                            f"failed: {exc}. Retrying in {wait:.1f}s …"
                        )
                        time.sleep(wait)
            raise last_exc
        return wrapper
    return decorator
