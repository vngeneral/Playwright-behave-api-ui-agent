"""
Generic API Step Definitions
============================
Thin glue between Gherkin and the shared API utilities:

    utils/api/base_client.py      — HTTP (retry, timing, Allure attachments)
    utils/api/payload_builder.py  — {{token}} payload templates
    utils/api/response_matcher.py — <<matcher>> body assertions, JSON path, schemas

Context attributes used by these steps:
    context.base_url         set by environment.py from config.yaml
    context.api_response     APIResponse of the last request
    context.saved_values     dict — fields captured for request chaining
"""
from __future__ import annotations

from behave import step, then, when

from utils.api.base_client import BaseAPIClient
from utils.api.payload_builder import build_payload
from utils.api.response_matcher import (
    assert_json_matches,
    get_json_path,
    validate_schema,
)
from utils.logger import log_failure, log_info_emoji

# ---------------------------------------------------------------------------
# Shared client + helpers
# ---------------------------------------------------------------------------

def _client(context) -> BaseAPIClient:
    """One BaseAPIClient per scenario run, built from context.base_url."""
    if not getattr(context, "generic_api_client", None):
        context.generic_api_client = BaseAPIClient(context.base_url)
    return context.generic_api_client


def _saved(context) -> dict:
    if not hasattr(context, "saved_values"):
        context.saved_values = {}
    return context.saved_values


def _response_json(context):
    assert getattr(context, "api_response", None) is not None, (
        "api_response not set — a request step must run before this assertion"
    )
    try:
        return context.api_response.json()
    except Exception as exc:
        raise AssertionError(
            f"Response body is not valid JSON: {exc}\n"
            f"Raw body: {context.api_response.text[:300]}"
        ) from exc


# ---------------------------------------------------------------------------
# When — generic requests (payload templates support {{tokens}})
# ---------------------------------------------------------------------------

@when('I send a {method} request to "{path}"')
def step_send_request(context, method: str, path: str):
    context.api_response = _client(context).request(method, path)


@when('I send a {method} request to "{path}" with payload')
def step_send_request_with_payload(context, method: str, path: str):
    """Doc-string is a JSON template — {{tokens}} resolve at send time."""
    payload = build_payload(context.text, store=_saved(context))
    context.last_payload = payload
    context.api_response = _client(context).request(method, path, json_body=payload)


# ---------------------------------------------------------------------------
# Then — status
# ---------------------------------------------------------------------------

@then("the response status code should be {expected_status:d}")
def step_assert_status_code(context, expected_status: int):
    assert getattr(context, "api_response", None) is not None, "No response received"
    actual = context.api_response.status_code
    assert actual == expected_status, (
        f"Expected HTTP {expected_status}, got {actual}\n"
        f"Body: {context.api_response.text[:500]}"
    )


# ---------------------------------------------------------------------------
# Then — body matching (doc-strings support <<matcher>> tokens)
# ---------------------------------------------------------------------------

@then("the response body should match")
def step_assert_body_matches(context):
    """Subset match — extra keys in the actual response are allowed."""
    assert_json_matches(context.text, _response_json(context))


@then("the response body should strictly match")
def step_assert_body_matches_strictly(context):
    """Strict match — unexpected keys in the response are failures too."""
    assert_json_matches(context.text, _response_json(context), strict_keys=True)


@then('the response body should match ignoring "{ignored}"')
def step_assert_body_matches_ignoring(context, ignored: str):
    """Comma-separated ignore paths, e.g. "headers.*, **.traceId"."""
    paths = [p.strip() for p in ignored.split(",") if p.strip()]
    assert_json_matches(context.text, _response_json(context), ignore_paths=paths)


@then("the request payload should be echoed back in the response field \"{path}\"")
def step_assert_payload_echoed(context, path: str):
    """Compare the exact payload we sent against what the API echoes back."""
    assert getattr(context, "last_payload", None) is not None, (
        "last_payload not set — send a request 'with payload' first"
    )
    echoed = get_json_path(_response_json(context), path)
    assert_json_matches(context.last_payload, echoed, strict_keys=True,
                        label=f"echoed payload at '{path}'")


# ---------------------------------------------------------------------------
# Then — single-field assertions (dotted path, e.g. json.vinList[0].vin)
# ---------------------------------------------------------------------------

@then('the response field "{path}" should be "{expected}"')
def step_assert_field_equals(context, path: str, expected: str):
    actual = get_json_path(_response_json(context), path)
    assert str(actual) == expected, (
        f"Field '{path}': expected '{expected}', got '{actual}'"
    )


@then('the response field "{path}" should exist')
def step_assert_field_exists(context, path: str):
    get_json_path(_response_json(context), path)  # raises JsonPathError if missing


@then('the response field "{path}" should equal the saved value "{name}"')
def step_assert_field_equals_saved(context, path: str, name: str):
    saved = _saved(context)
    assert name in saved, f"No saved value '{name}'. Available: {sorted(saved)}"
    actual = get_json_path(_response_json(context), path)
    assert actual == saved[name], (
        f"Field '{path}': expected saved '{name}' = {saved[name]!r}, got {actual!r}"
    )


# ---------------------------------------------------------------------------
# Then — schema validation
# ---------------------------------------------------------------------------

@then('the response should match schema "{schema_name}"')
def step_assert_schema(context, schema_name: str):
    validate_schema(_response_json(context), schema_name)


# ---------------------------------------------------------------------------
# Chaining — capture response fields for later payloads via {{saved:name}}
# ---------------------------------------------------------------------------

@step('I save the response field "{path}" as "{name}"')
def step_save_response_field(context, path: str, name: str):
    value = get_json_path(_response_json(context), path)
    _saved(context)[name] = value
    log_info_emoji("💾", f"saved '{name}' = {value!r}")


# ---------------------------------------------------------------------------
# Legacy httpbin steps — kept for api_testing.feature backward compatibility
# ---------------------------------------------------------------------------

@step("the user makes a GET request to the API")
def step_make_get_request(context):
    try:
        context.api_response = _client(context).get("get")
    except Exception as exc:
        log_failure(f"GET request failed: {exc}")
        context.api_response = None


@step("the response is received")
def step_receive_response(context):
    assert context.api_response is not None, "No response received from API"
    context.response_status = context.api_response.status_code
    context.response_data = context.api_response.json()


@step("the response status should be 200")
def step_verify_200(context):
    step_assert_status_code(context, 200)


@step("the user makes a POST request to the API")
def step_make_post_request(context):
    try:
        payload = build_payload(
            {"test": "data", "message": "Hello World", "runId": "{{uuid}}"},
            store=_saved(context),
        )
        context.last_payload = payload
        context.api_response = _client(context).post("post", json_body=payload)
    except Exception as exc:
        log_failure(f"POST request failed: {exc}")
        context.api_response = None


@step("the response status should be 201")
def step_verify_201(context):
    assert getattr(context, "api_response", None) is not None, "No response received"
    # httpbin returns 200 for POST; accept both
    actual = context.api_response.status_code
    assert actual in (200, 201), f"Expected 200/201, got {actual}"


@step("the user makes an invalid request to the API")
def step_make_invalid_request(context):
    try:
        # httpbin /status/400 reliably returns 400
        context.api_response = _client(context).get("status/400")
    except Exception as exc:
        log_failure(f"Invalid request failed: {exc}")
        context.api_response = None


@step("the response status should be 400")
def step_verify_400(context):
    step_assert_status_code(context, 400)


@step("the response status should be 404")
def step_verify_404(context):
    step_assert_status_code(context, 404)
