"""
TestRail API Client
===================
Minimal HTTP client for TestRail API v2.

Only the endpoints actually used by the framework are implemented:
    POST /index.php?/api/v2/add_results_for_cases/{run_id}
    POST /index.php?/api/v2/add_case/{section_id}
    GET  /index.php?/api/v2/get_cases/{project_id}&section_id=...&suite_id=...

Authentication:
    Basic auth — user:api_key, where the API key is a TestRail API token
    (not the user's password). NEVER hardcoded.

Environment variables (all required at push time):
    TESTRAIL_URL      — https://yourcompany.testrail.io  (no trailing slash)
    TESTRAIL_USER     — email address used to log in to TestRail
    TESTRAIL_API_KEY  — TestRail API key (generated under My Settings → API Keys)
    TESTRAIL_RUN_ID   — default run ID; can be overridden per push call
    TESTRAIL_PROJECT_ID — required by case_sync's duplicate-case check (get_cases)
    TESTRAIL_SUITE_ID   — only needed for multi-suite TestRail projects

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

_ENV_URL        = "TESTRAIL_URL"
_ENV_USER       = "TESTRAIL_USER"
_ENV_API_KEY    = "TESTRAIL_API_KEY"
_ENV_RUN_ID     = "TESTRAIL_RUN_ID"
_ENV_SECTION_ID = "TESTRAIL_SECTION_ID"
_ENV_PROJECT_ID = "TESTRAIL_PROJECT_ID"
_ENV_SUITE_ID   = "TESTRAIL_SUITE_ID"

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
    def from_env(cls) -> TestRailClient:
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
        return _int_env(_ENV_RUN_ID)

    @staticmethod
    def default_section_id() -> int | None:
        """Return the TESTRAIL_SECTION_ID env var as int, or None if not set."""
        return _int_env(_ENV_SECTION_ID)

    @staticmethod
    def default_project_id() -> int | None:
        """Return the TESTRAIL_PROJECT_ID env var as int, or None if not set."""
        return _int_env(_ENV_PROJECT_ID)

    @staticmethod
    def default_suite_id() -> int | None:
        """Return the TESTRAIL_SUITE_ID env var as int, or None if not set."""
        return _int_env(_ENV_SUITE_ID)

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

    def add_case(
        self,
        section_id: int,
        title: str,
        custom_steps: str | None = None,
    ) -> dict[str, Any]:
        """
        POST /index.php?/api/v2/add_case/{section_id}

        Create a new test case in a TestRail section. Used by case_sync.py to
        register AI-generated scenarios so they get real @testrail_C<id> tags.

        Which steps field a case accepts depends on the project's template, so
        payload variants are tried in order — HTTP 400 (TestRail's response to
        a field the template doesn't define) advances to the next variant, any
        other error raises immediately:

            1. ``custom_steps``            — "Test Case (Text)" template
            2. ``custom_steps_separated``  — "Test Case (Steps)" template; the
               whole Gherkin body becomes the content of the case's first step
            3. title only                  — last resort so the case is still
               created and the scenario still gets its @testrail_C<id> tag

        Args:
            section_id:    TestRail section ID to create the case under.
            title:         Case title (the scenario name).
            custom_steps:  Optional plain-text steps (the Gherkin body).

        Returns:
            Parsed JSON response dict — includes the new case's "id".

        Raises:
            TestRailAPIError on non-2xx response (after all fallbacks).
        """
        url = f"{self._base_url}/{_API_PATH}/add_case/{section_id}"

        payloads: list[dict[str, Any]] = [{"title": title}]
        if custom_steps:
            payloads = [
                {"title": title, "custom_steps": custom_steps},
                {"title": title, "custom_steps_separated": [{"content": custom_steps}]},
                {"title": title},
            ]

        log_info_emoji("📝", f"TestRail → POST add_case section={section_id} title={title!r}")

        for i, payload in enumerate(payloads):
            response = self._session.post(url, auth=self._auth, json=payload, timeout=30)
            if response.status_code == 400 and i < len(payloads) - 1:
                rejected = "custom_steps" if "custom_steps" in payload else "custom_steps_separated"
                retry_with = (
                    "custom_steps_separated" if "custom_steps" in payload else "title only"
                )
                log_warning(
                    f"TestRail rejected {rejected} (template mismatch?) — retrying with {retry_with}"
                )
                continue
            return self._handle_response(response, f"add_case section={section_id}")

    def get_cases(
        self,
        project_id: int,
        section_id: int | None = None,
        suite_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        GET /index.php?/api/v2/get_cases/{project_id}

        List existing cases, optionally narrowed to one section (and suite,
        for multi-suite projects). Used by case_sync to check whether a case
        with a scenario's title already exists before creating a new one —
        TestRail's add_case has no title-uniqueness check of its own, so
        skipping this lookup means re-syncing a renamed/regenerated feature
        file creates duplicate cases instead of reusing the existing one.

        Transparently follows pagination (TestRail caps each page at 250
        results) until every matching case has been collected.

        Args:
            project_id:  TestRail project ID (TESTRAIL_PROJECT_ID).
            section_id:  Optional section filter.
            suite_id:    Optional suite filter — only needed for multi-suite
                         projects; single-suite projects resolve it implicitly.

        Returns:
            List of case dicts (each has at least "id" and "title").

        Raises:
            TestRailAPIError on non-2xx response.
        """
        url = f"{self._base_url}/{_API_PATH}/get_cases/{project_id}"
        params: dict[str, int] = {}
        if section_id is not None:
            params["section_id"] = section_id
        if suite_id is not None:
            params["suite_id"] = suite_id

        log_info_emoji("📥", f"TestRail → GET get_cases project={project_id} section={section_id}")

        cases: list[dict[str, Any]] = []
        while True:
            response = self._session.get(url, auth=self._auth, params=params, timeout=30)
            data = self._handle_response(response, f"get_cases project={project_id}")
            page: list[dict[str, Any]]
            if isinstance(data, dict) and "cases" in data:
                page = data["cases"]
            else:
                page = data  # type: ignore[assignment]  # some TestRail versions return a bare list
            cases.extend(page or [])

            next_link = data.get("_links", {}).get("next") if isinstance(data, dict) else None
            if not next_link:
                break
            url = f"{self._base_url}{next_link}"
            params = {}
        return cases

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> TestRailClient:
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
# Helpers
# ---------------------------------------------------------------------------


def _int_env(name: str) -> int | None:
    """Return an env var as int, or None if unset / not a valid integer."""
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        log_warning(f"{name}={raw!r} is not a valid integer — ignored")
        return None


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
