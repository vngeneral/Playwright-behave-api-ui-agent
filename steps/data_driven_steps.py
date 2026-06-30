"""
Step definitions for data_driven.feature.

Parameterised steps use Scenario Outline placeholders; they do NOT conflict
with the named steps in api_steps.py / forms_steps.py because their text
patterns are distinct.
"""
from __future__ import annotations

import requests
from behave import step, given, when, then
from playwright.sync_api import expect

from utils.data_factory import DataFactory
from utils.logger import log_failure, log_info_emoji


# lazy singleton — constructed once per step import
_df: DataFactory | None = None


def _factory() -> DataFactory:
    global _df
    if _df is None:
        _df = DataFactory()
    return _df


# ─────────────────────────────────────────────────────────────────────────────
# Form steps (parameterised)
# ─────────────────────────────────────────────────────────────────────────────

@when('the user fills in name "{name}", phone "{phone}", email "{email}"')
def step_fill_name_phone_email(context, name, phone, email):
    form = context.page_factory.get_contact_form_page(context)
    form.fill_customer_name(name)
    form.fill_customer_phone(phone)
    form.fill_customer_email(email)
    context.submitted_name = name


@when('the user selects pizza size "{size}"')
def step_select_pizza_size(context, size):
    form = context.page_factory.get_contact_form_page(context)
    form.select_pizza_size(size)


@when("the user submits the form")
def step_submit_form(context):
    form = context.page_factory.get_contact_form_page(context)
    form.submit_form()
    context.form_submitted = True


@then("the form submission page should confirm the data was received")
def step_verify_submission_page(context):
    """
    After submitting httpbin.org/forms/post the browser lands on /post,
    which shows a JSON payload echoing the submitted form fields.
    We verify the page no longer contains the form inputs.
    """
    form = context.page_factory.get_contact_form_page(context)
    assert getattr(context, "form_submitted", False), "Form was not submitted"

    # httpbin redirects to /post which renders JSON — form inputs gone
    expect(context.page.locator(form.SUBMIT_BUTTON)).not_to_be_visible()


# ─────────────────────────────────────────────────────────────────────────────
# Special-character steps
# ─────────────────────────────────────────────────────────────────────────────

@when("the user enters the special-character comment from test data")
def step_enter_special_chars(context):
    form = context.page_factory.get_contact_form_page(context)
    comment = _factory().get_special_char_comment()
    context.expected_comment = comment
    # Use the correct selector directly (bypassing the intentionally wrong one)
    context.page.locator("textarea[name='comments']").fill(comment)
    log_info_emoji("✍️", f"Filled special-char comment: {comment[:40]}…")


@then("the textarea should contain the special characters")
def step_verify_special_chars(context):
    expected = getattr(context, "expected_comment", "")
    # Verify against the correct selector
    locator = context.page.locator("textarea[name='comments']")
    expect(locator).to_have_value(expected)


# ─────────────────────────────────────────────────────────────────────────────
# Parameterised API steps
# ─────────────────────────────────────────────────────────────────────────────

@when('the user makes a {method} request to path "{path}"')
def step_generic_request(context, method: str, path: str):
    url = context.build_url(context.base_url, path)
    try:
        if method.upper() == "GET":
            resp = requests.get(url, timeout=10)
        elif method.upper() == "POST":
            resp = requests.post(url, json={"framework": "playwright-behave"}, timeout=10)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        context.api_response = resp
        context.response_status = resp.status_code
        log_info_emoji("🌐", f"{method} {url} → {resp.status_code}")
    except Exception as exc:
        log_failure(f"{method} {url} failed: {exc}")
        context.api_response = None
        context.response_status = None


@then("the response status should be {expected_status:d}")
def step_assert_status_code(context, expected_status: int):
    actual = context.response_status
    assert actual is not None, "No HTTP response was received"
    assert actual == expected_status, (
        f"Expected HTTP {expected_status}, got {actual}"
    )


@then('the JSON response should contain keys "{keys_csv}"')
def step_assert_json_keys(context, keys_csv: str):
    assert context.api_response is not None, "No API response to inspect"
    body = context.api_response.json()
    for key in [k.strip() for k in keys_csv.split(",")]:
        assert key in body, (
            f"Expected key '{key}' in JSON response. Got keys: {list(body.keys())}"
        )
