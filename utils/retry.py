"""
Retry utilities for flaky Playwright / API steps.

Usage in a step definition:

    from utils.retry import retry_on_exception

    @retry_on_exception(max_attempts=3, delay=1.0, exceptions=(AssertionError,))
    def _do_click():
        context.page.click("#submit")

    _do_click()

Or as a decorator on any plain function / helper method:

    @retry_on_exception()
    def fetch_element(page, selector):
        return page.locator(selector).inner_text()
"""
from __future__ import annotations

import time
import functools
import logging
from typing import Callable, Tuple, Type

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from utils.logger import log_warning
from utils.misc import load_config

logger = logging.getLogger("retry")

# Defaults pulled from config.yaml if available
_cfg = load_config().get("retry", {})
_DEFAULT_MAX_ATTEMPTS: int = int(_cfg.get("max_attempts", 3))
_DEFAULT_DELAY: float = float(_cfg.get("delay_seconds", 1.0))

# Exceptions worth retrying by default
_DEFAULT_EXCEPTIONS: Tuple[Type[Exception], ...] = (
    AssertionError,
    PlaywrightTimeoutError,
)


def retry_on_exception(
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    delay: float = _DEFAULT_DELAY,
    exceptions: Tuple[Type[Exception], ...] = _DEFAULT_EXCEPTIONS,
    backoff: float = 1.0,  # multiplier applied to delay after each attempt
) -> Callable:
    """
    Decorator / decorator-factory that retries the wrapped callable on failure.

    Args:
        max_attempts: Total attempts (including the first).
        delay:        Seconds to wait between attempts.
        exceptions:   Tuple of exception types that trigger a retry.
        backoff:      Multiplier applied to delay after each failed attempt.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        log_warning(
                            f"[retry] {fn.__name__} attempt {attempt}/{max_attempts} "
                            f"failed: {exc}. Retrying in {wait:.1f}s …"
                        )
                        time.sleep(wait)
                        wait *= backoff
                    else:
                        log_warning(
                            f"[retry] {fn.__name__} exhausted {max_attempts} attempts. "
                            f"Last error: {exc}"
                        )
            raise last_exc  # re-raise after all attempts exhausted
        return wrapper
    return decorator


def with_retry(fn: Callable, *args, max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
               delay: float = _DEFAULT_DELAY,
               exceptions: Tuple[Type[Exception], ...] = _DEFAULT_EXCEPTIONS,
               **kwargs):
    """
    Functional (non-decorator) version — useful for one-off calls:

        result = with_retry(page.locator("#btn").click, max_attempts=3)
    """
    return retry_on_exception(max_attempts=max_attempts, delay=delay, exceptions=exceptions)(fn)(
        *args, **kwargs
    )
