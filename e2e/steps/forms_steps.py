"""
Step definitions for forms.feature.
All steps target httpbin.org/forms/post.
"""
from behave import step
from playwright.sync_api import expect

from utils.logger import log_info_emoji


@step("the user navigates to the contact form")
def step_navigate_contact_form(context):
    contact_form = context.page_factory.get_contact_form_page(context)
    contact_form.navigate_to_contact_form(context.base_url)


@step("the user fills out the contact form with valid data")
def step_fill_contact_form_valid(context):
    contact_form = context.page_factory.get_contact_form_page(context)
    contact_form.fill_form_with_valid_data()
    contact_form.submit_form()
    context.form_filled = True


@step("the form should be submitted successfully")
def step_verify_form_submission(context):
    """
    After submitting httpbin.org/forms/post the browser lands on /post
    which renders the echoed JSON — the original form inputs are gone.
    """
    assert getattr(context, "form_filled", False), "form_filled flag not set"
    contact_form = context.page_factory.get_contact_form_page(context)
    # Submit button disappears once we land on the result page
    expect(context.page.locator(contact_form.SUBMIT_BUTTON)).not_to_be_visible()
    log_info_emoji("✅", "Form submitted — result page confirmed")


@step("the user tries to submit without filling required fields")
def step_submit_empty_form(context):
    """
    Click submit on an empty form.
    HTML5 required-field validation prevents navigation — the URL stays
    on /forms/post and the name input remains visible.
    """
    context.page.locator("p > button").click()
    # If HTML5 validation fires, the page does NOT navigate away
    context.form_blocked = context.page.url.endswith("/forms/post")


@step("the user should see validation errors")
def step_verify_validation_errors(context):
    """HTML5 validation keeps the form on the page — inputs must still be visible."""
    assert getattr(context, "form_blocked", False), (
        f"Form navigated away unexpectedly. Current URL: {context.page.url}"
    )
    # Name input must still be on the page (submission was blocked)
    expect(context.page.locator("input[name='custname']")).to_be_visible()
    log_info_emoji("✅", "HTML5 validation blocked empty-form submission")


@step("the user enters special characters in the form")
def step_fill_form_special_chars(context):
    """Fill the comments textarea using the correct selector."""
    special_text = "Test with special chars: !@#$%^&*()_+-=[]{}|;':\",./<>?"
    context.page.locator("textarea[name='comments']").fill(special_text)
    context.special_text = special_text
    log_info_emoji("✍️", f"Entered special chars: {special_text[:30]}…")


@step("the form should handle special characters correctly")
def step_verify_special_chars_handling(context):
    expected = getattr(context, "special_text", "")
    expect(context.page.locator("textarea[name='comments']")).to_have_value(expected)
    log_info_emoji("✅", "Special characters retained in textarea")
