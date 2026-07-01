"""
Base API Client
===============
Thin, reusable HTTP client used by all API-under-test clients.

Responsibilities:
  • Persistent requests.Session with configurable timeouts
  • Per-request retry with exponential backoff (reads config.yaml values)
  • Wall-clock timing on every call
  • Structured logging (method + URL + status + ms)
  • Allure attachment for request body and response body on every call
  • Never swallows exceptions — callers decide what to catch

Usage::

    from utils.api.base_client import BaseAPIClient

    class MyClient(BaseAPIClient):
        def get_thing(self, thing_id):
            return self.post("/things", json={"id": thing_id})
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.logger import log_info_emoji, log_warning, log_failure

# Allure is optional (not installed in unit-test sandbox without a browser run)
try:
    import allure
    _ALLURE_AVAILABLE = True
except ImportError:
    _ALLURE_AVAILABLE = False


class APIResponse:
    """
    Lightweight wrapper around requests.Response.

    Exposes:
        .status_code   int
        .ok            bool  (2xx)
        .json()        dict | list  (raises if body is not JSON)
        .text          str
        .elapsed_ms    float
        .raw           requests.Response
    """

    def __init__(self, response: requests.Response, elapsed_ms: float) -> None:
        self.raw = response
        self.status_code = response.status_code
        self.ok = response.ok
        self.text = response.text
        self.elapsed_ms = elapsed_ms

    def json(self) -> Any:
        return self.raw.json()

    def __repr__(self) -> str:
        return f"<APIResponse status={self.status_code} elapsed={self.elapsed_ms:.0f}ms>"


class BaseAPIClient:
    """
    HTTP client base class. Subclasses call ``self.request()`` / ``self.post()`` / etc.

    Args:
        base_url:    Root URL for all requests (no trailing slash).
        timeout:     Per-request timeout in seconds (default 30).
        max_retries: How many times to retry on connection errors (default 3).
        retry_backoff: Backoff factor for urllib3 Retry (default 1.0).
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = self._build_session(max_retries, retry_backoff)

    # ------------------------------------------------------------------
    # Public request helpers
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json_body: Any = None,
        data: Any = None,
        expected_status: int | None = None,
    ) -> APIResponse:
        """
        Execute an HTTP request and return an APIResponse.

        Args:
            method:          HTTP verb (GET, POST, PUT, DELETE, …).
            path:            Relative path — will be joined to base_url.
            headers:         Extra headers merged on top of session defaults.
            params:          URL query string params.
            json_body:       JSON-serialisable body (sets Content-Type automatically).
            data:            Raw body (for form-encoded requests).
            expected_status: If set, raises AssertionError when status ≠ expected.

        Returns:
            APIResponse wrapping the raw requests.Response + elapsed ms.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"

        log_info_emoji("🌐", f"{method.upper()} {url}")
        if json_body:
            log_info_emoji("📤", f"Body: {json.dumps(json_body, indent=2)[:500]}")

        start = time.monotonic()
        resp = self._session.request(
            method=method.upper(),
            url=url,
            headers=headers or {},
            params=params,
            json=json_body,
            data=data,
            timeout=self._timeout,
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        log_info_emoji(
            "✅" if resp.ok else "⚠️",
            f"{resp.status_code} ← {method.upper()} {path}  [{elapsed_ms:.0f}ms]",
        )

        self._attach_to_allure(method, url, json_body, resp, elapsed_ms)

        if expected_status is not None:
            assert resp.status_code == expected_status, (
                f"Expected HTTP {expected_status}, got {resp.status_code}\n"
                f"URL: {url}\n"
                f"Response: {resp.text[:500]}"
            )

        return APIResponse(resp, elapsed_ms)

    def get(self, path: str, **kwargs) -> APIResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> APIResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> APIResponse:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> APIResponse:
        return self.request("DELETE", path, **kwargs)

    def close(self) -> None:
        """Close the underlying requests.Session."""
        self._session.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_session(max_retries: int, backoff: float) -> requests.Session:
        """Create a Session with connection-level retry (not 4xx/5xx retry)."""
        session = requests.Session()
        retry = Retry(
            total=max_retries,
            backoff_factor=backoff,
            # Only retry on connection-level errors, NOT on response status codes.
            # Status-code retries belong in test logic, not the HTTP layer.
            status_forcelist=None,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://",  adapter)
        return session

    @staticmethod
    def _attach_to_allure(
        method: str,
        url: str,
        request_body: Any,
        response: requests.Response,
        elapsed_ms: float,
    ) -> None:
        if not _ALLURE_AVAILABLE:
            return
        try:
            req_text = json.dumps(request_body, indent=2) if request_body else "(no body)"
            allure.attach(
                f"Method : {method.upper()}\nURL    : {url}\n\n{req_text}",
                name="API Request",
                attachment_type=allure.attachment_type.TEXT,
            )
            try:
                resp_json = json.dumps(response.json(), indent=2)
            except Exception:
                resp_json = response.text or "(empty)"
            allure.attach(
                f"Status : {response.status_code}  [{elapsed_ms:.0f}ms]\n\n{resp_json[:4000]}",
                name="API Response",
                attachment_type=allure.attachment_type.TEXT,
            )
        except Exception:
            pass  # Allure attach must never crash a test
