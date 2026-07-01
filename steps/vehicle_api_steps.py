"""
Vehicle API Step Definitions
============================
Thin glue between Gherkin sentences and VehicleAPIClient methods.

All assertions are here; all HTTP logic is in VehicleAPIClient.
The step file never constructs raw requests or hardcodes URLs/keys.

Context attributes set by these steps:
    context.vehicle_client   VehicleAPIClient (set in Background)
    context.vehicle_response APIResponse      (set by When steps)
"""
from __future__ import annotations

import json

import allure
from behave import given, step, then, when

from utils.api.vehicle_client import VehicleAPIClient
from utils.logger import log_failure, log_info_emoji
from utils.misc import load_config


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------

@given("the Vehicle API client is initialised")
def step_init_vehicle_client(context):
    """
    Build VehicleAPIClient from config.yaml.
    VEHICLE_API_KEY must be set in env; VEHICLE_API_BASE_URL overrides config URL.
    """
    config = getattr(context, "config", None) or load_config()
    context.vehicle_client = VehicleAPIClient.from_config(config)
    log_info_emoji("🚗", "VehicleAPIClient initialised")


# ---------------------------------------------------------------------------
# When — Registration
# ---------------------------------------------------------------------------

@when('I register VIN "{vin}" for partner "{partner_code}"')
def step_register_single_vin(context, vin: str, partner_code: str):
    context.vehicle_response = context.vehicle_client.register_vehicles(
        partner_code=partner_code,
        vin_list=[vin],
    )
    _attach_response(context, "register")


@when('I register the following VINs for partner "{partner_code}"')
def step_register_batch_vins(context, partner_code: str):
    """Table column: | vin |"""
    vin_list = [row["vin"] for row in context.table]
    context.vehicle_response = context.vehicle_client.register_vehicles(
        partner_code=partner_code,
        vin_list=vin_list,
    )
    _attach_response(context, "register_batch")


@when('I register VIN "{vin}" for partner "{partner_code}" with an invalid API key')
def step_register_invalid_key(context, vin: str, partner_code: str):
    context.vehicle_response = context.vehicle_client.register_vehicles_with_invalid_key(
        partner_code=partner_code,
        vin_list=[vin],
    )
    _attach_response(context, "register_invalid_key")


@when('I register VIN "{vin}" for partner "{partner_code}" omitting field "{field_name}"')
def step_register_missing_field(context, vin: str, partner_code: str, field_name: str):
    context.vehicle_response = context.vehicle_client.register_without_required_field(
        missing_field=field_name,
        partner_code=partner_code,
        vin_list=[vin],
    )
    _attach_response(context, f"register_missing_{field_name}")


@when('I register an empty VIN list for partner "{partner_code}"')
def step_register_empty_vin_list(context, partner_code: str):
    context.vehicle_response = context.vehicle_client.register_vehicles(
        partner_code=partner_code,
        vin_list=[],
    )
    _attach_response(context, "register_empty_list")


# ---------------------------------------------------------------------------
# When — Deregistration
# ---------------------------------------------------------------------------

@when('I deregister VIN "{vin}" for partner "{partner_code}"')
def step_deregister_single_vin(context, vin: str, partner_code: str):
    context.vehicle_response = context.vehicle_client.deregister_vehicles(
        partner_code=partner_code,
        vin_list=[vin],
    )
    _attach_response(context, "deregister")


@when('I deregister the following VINs for partner "{partner_code}"')
def step_deregister_batch_vins(context, partner_code: str):
    """Table column: | vin |"""
    vin_list = [row["vin"] for row in context.table]
    context.vehicle_response = context.vehicle_client.deregister_vehicles(
        partner_code=partner_code,
        vin_list=vin_list,
    )
    _attach_response(context, "deregister_batch")


# ---------------------------------------------------------------------------
# Then — status code assertions
# ---------------------------------------------------------------------------

@then("the API response status should be {expected_status:d}")
def step_assert_status(context, expected_status: int):
    _require_response(context)
    actual = context.vehicle_response.status_code
    assert actual == expected_status, (
        f"Expected HTTP {expected_status}, got {actual}\n"
        f"Body: {context.vehicle_response.text[:500]}"
    )


@then("the API response status should indicate unauthorised")
def step_assert_unauthorised(context):
    """Accept 401 or 403 — both are valid auth-rejection codes."""
    _require_response(context)
    actual = context.vehicle_response.status_code
    assert actual in (401, 403), (
        f"Expected 401 or 403 for invalid API key, got {actual}\n"
        f"Body: {context.vehicle_response.text[:500]}"
    )


# ---------------------------------------------------------------------------
# Then — response body assertions
# ---------------------------------------------------------------------------

@then("the response body should be valid JSON")
def step_assert_json(context):
    _require_response(context)
    try:
        context.vehicle_response.json()
    except Exception as exc:
        raise AssertionError(
            f"Response body is not valid JSON: {exc}\n"
            f"Raw body: {context.vehicle_response.text[:300]}"
        ) from exc


@then("the response should contain a transaction reference")
def step_assert_transaction_ref(context):
    _require_response(context)
    try:
        body = context.vehicle_response.json()
    except Exception:
        raise AssertionError("Cannot check transaction reference — response is not JSON")

    # Common patterns: transactionId, transaction_id, referenceId
    keys_to_check = {"transactionId", "transaction_id", "referenceId", "reference"}
    found = keys_to_check.intersection(set(body.keys()) if isinstance(body, dict) else set())
    assert found, (
        f"Expected one of {keys_to_check} in response body, got keys: {list(body.keys()) if isinstance(body, dict) else type(body)}"
    )


@then("the response time should be under {threshold_ms:d} ms")
def step_assert_response_time(context, threshold_ms: int):
    _require_response(context)
    actual_ms = context.vehicle_response.elapsed_ms
    assert actual_ms < threshold_ms, (
        f"Response took {actual_ms:.0f}ms — exceeds threshold of {threshold_ms}ms"
    )


@then("the API response time should be under {threshold_ms:d} ms")
def step_assert_api_response_time(context, threshold_ms: int):
    step_assert_response_time(context, threshold_ms)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_response(context) -> None:
    assert hasattr(context, "vehicle_response") and context.vehicle_response is not None, (
        "vehicle_response not set — a 'When' step must run before this 'Then' step"
    )


def _attach_response(context, label: str) -> None:
    """Attach the response to Allure if available; always log it."""
    resp = context.vehicle_response
    log_info_emoji(
        "✅" if resp.ok else "⚠️",
        f"[{label}] HTTP {resp.status_code}  {resp.elapsed_ms:.0f}ms",
    )
    try:
        try:
            pretty = json.dumps(resp.json(), indent=2)
        except Exception:
            pretty = resp.text or "(empty)"
        allure.attach(
            f"HTTP {resp.status_code}  [{resp.elapsed_ms:.0f}ms]\n\n{pretty[:4000]}",
            name=f"Response — {label}",
            attachment_type=allure.attachment_type.TEXT,
        )
    except Exception:
        pass
