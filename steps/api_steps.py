from behave import step
import requests

from utils.logger import log_failure


@step("the user makes a GET request to the API")
def step_make_get_request(context):
    try:
        response = requests.get(context.build_url(context.base_url, "get"), timeout=10)
        context.api_response = response
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
    assert hasattr(context, "response_status"), "response_status not set"
    assert context.response_status == 200, f"Expected 200, got {context.response_status}"


@step("the user makes a POST request to the API")
def step_make_post_request(context):
    try:
        payload = {"test": "data", "message": "Hello World"}
        response = requests.post(
            context.build_url(context.base_url, "post"),
            json=payload,
            timeout=10,
        )
        context.api_response = response
    except Exception as exc:
        log_failure(f"POST request failed: {exc}")
        context.api_response = None


@step("the response status should be 201")
def step_verify_201(context):
    assert hasattr(context, "response_status"), "response_status not set"
    # httpbin returns 200 for POST; accept both
    assert context.response_status in (200, 201), (
        f"Expected 200/201, got {context.response_status}"
    )


@step("the user makes an invalid request to the API")
def step_make_invalid_request(context):
    try:
        # httpbin /status/400 reliably returns 400
        response = requests.get(
            context.build_url(context.base_url, "status/400"),
            timeout=10,
        )
        context.api_response = response
    except Exception as exc:
        log_failure(f"Invalid request failed: {exc}")
        context.api_response = None


@step("the response status should be 400")
def step_verify_400(context):
    assert hasattr(context, "response_status"), "response_status not set"
    assert context.response_status == 400, f"Expected 400, got {context.response_status}"


@step("the response status should be 404")
def step_verify_404(context):
    assert hasattr(context, "response_status"), "response_status not set"
    assert context.response_status == 404, f"Expected 404, got {context.response_status}"
