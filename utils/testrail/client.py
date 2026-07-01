"""
TestRail API Client
===================
Minimal HTTP client for TestRail API v2.

Only the endpoints actually used by the framework are implemented:
    POST /index.php?/api/v2/add_results_for_cases/{run_id}
    GET  /index.php?/api/v2/get_run/{run_id}

Authentication:
    Basic auth — user:api_key, where the API key is a TestRail API token
    (not the user's password). NEVER hardcoded.

Environment variables (all required at push time):
    TESTRAIL_URL      — https://yourcompany.testrail.io  (no trailing slash)
    TESTRAIL_USER     — email address used to log in to TestRail
    TESTRAIL_API_KEY  — TestRail API key (generated under My Settings → API Keys)
    TESTRAIL_RUN_ID   — default run ID; can be overridden per push call

Usage::

    client = TestRailClient.from_env()
    response = client.add_results_for_cases(
        run_id=123,
        results=[
            {"case_id": 448337, "status_id": 1, "comment": "All steps passed"},
        ]
    )
"""
from __future__ import annotations

import os
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from utils.logger import log_info_emoji, log_warning

# ---------------------------------------------------------------------------
# Environment variable names — never hardcode values
# ---------------------------------------------------------------------------

_ENV_URL     = "TESTRAIL_URL"
_ENV_USER    = "TESTRAIL_USER"
_ENV_API_KEY = "TESTRAIL_API_KEY"
_ENV_RUN_ID  = "TESTRAIL_RUN_ID"

_API_PATH = "index.php?/api/v2"


class TestRailClient:
    """
    Thin wrapper around the TestRail REST API.

    All credentials come from environment variables — never from arguments
    that could end up in logs or stored in source.
    """

    def __init__(self, base_url: str, user: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth     = HTTPBasicAuth(user, api_key)
        self._session  = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
        })

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "TestRailClient":
        """
        Construct a client from environment variables.

        Raises TestRailConfigError if any required variable is missing.
        """
        missing = [v for v in (_ENV_URL, _ENV_USER, _ENV_API_KEY) if not os.getenv(v)]
        if missing:
            raise TestRailConfigError(
                f"Missing required environment variables: {', '.join(missing)}. "
                f"Set them before calling TestRailClient.from_env()."
            )
        return cls(
            base_url=os.environ[_ENV_URL],
            user=os.environ[_ENV_USER],
            api_key=os.environ[_ENV_API_KEY],
        )

    @staticmethod
    def default_run_id() -> int | None:
        """Return the TESTRAIL_RUN_ID env var as int, or None if not set."""
        raw = os.getenv(_ENV_RUN_ID)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            log_warning(f"TESTRAIL_RUN_ID={raw!r} is not a valid integer — ignored")
            return None

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def add_results_for_cases(
        self,
        run_id: int,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        POST /index.php?/api/v2/add_results_for_cases/{run_id}

        Bulk-submit test results for specific case IDs within a run.
        This is the direct replacement for the Groovy sampler's HTTP call.

        Args:
            run_id:   TestRail test run ID.
            results:  List of result dicts.  Each must have at least:
                        {"case_id": int, "status_id": int, "comment": str}
                      Optional fields: "elapsed" (e.g. "45s"), "version", etc.

        Returns:
            Parsed JSON response dict from TestRail.

        Raises:
            TestRailAPIError on non-2xx response.
        """
        url = f"{self._base_url}/{_API_PATH}/add_results_for_cases/{run_id}"
        payload = {"results": results}

        log_info_emoji("📤", f"TestRail → POST add_results_for_cases run={run_id} ({len(results)} result(s))")

        response = self._session.post(url, auth=self._auth, json=payload, timeout=30)
        return self._handle_response(response, f"add_results_for_cases run={run_id}")

    def get_run(self, run_id: int) -> dict[str, Any]:
        """
        GET /index.php?/api/v2/get_run/{run_id}

        Fetch metadata for a test run (name, project, status).

        Args:
            run_id:  TestRail test run ID.

        Returns:
            Parsed JSON response dict.

        Raises:
            TestRailAPIError on non-2xx response.
        """
        url = f"{self._base_url}/{_API_PATH}/get_run/{run_id}"
        log_info_emoji("🔍", f"TestRail → GET get_run run={run_id}")
        response = self._session.get(url, auth=self._auth, timeout=30)
        return self._handle_response(response, f"get_run run={run_id}")

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "TestRailClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_response(response: requests.Response, context: str) -> dict[str, Any]:
        if response.ok:
            log_info_emoji("✅", f"TestRail {context} → HTTP {response.status_code}")
            try:
                return response.json()
            except Exception:
                return {}
        else:
            body = response.text[:500]
            log_warning(f"TestRail {context} → HTTP {response.status_code}: {body}")
            raise TestRailAPIError(
                status_code=response.status_code,
                context=context,
                body=body,
            )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TestRailConfigError(RuntimeError):
    """Raised when required environment variables are missing."""


class TestRailAPIError(RuntimeError):
    """Raised when TestRail returns a non-2xx response."""

    def __init__(self, status_code: int, context: str, body: str) -> None:
        self.status_code = status_code
        self.context     = context
        self.body        = body
        super().__init__(f"TestRail API error {status_code} for {context}: {body}")
