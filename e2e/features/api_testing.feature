Feature: API Testing

  @api @smoke
  Scenario: GET request returns 200
    Given the user makes a GET request to the API
    When the response is received
    Then the response status should be 200

  @api @smoke
  Scenario: POST request returns success
    Given the user makes a POST request to the API
    When the response is received
    Then the response status should be 201

  @api @regression
  Scenario: Invalid request returns 400
    Given the user makes an invalid request to the API
    When the response is received
    Then the response status should be 400

  # ──────────────────────────────────────────────────────────────────────────
  # Dynamic payloads + expected-vs-actual body matching
  #
  # {{tokens}} in payloads resolve to fresh values at send time.
  # <<matchers>> in expected bodies validate dynamic response fields by
  # shape (uuid / timestamp / regex) instead of exact value.
  # ──────────────────────────────────────────────────────────────────────────

  @api @regression @payload_builder
  Scenario: POST a dynamically generated payload and match the echoed body
    When I send a POST request to "post" with payload
      """
      {
        "orderId":   "{{uuid}}",
        "createdAt": "{{now}}",
        "quantity":  "{{random_int:1:5}}",
        "vin":       "{{random_vin}}",
        "customer":  { "email": "{{random_email}}" }
      }
      """
    Then the response status code should be 200
    And the response body should match
      """
      {
        "json": {
          "orderId":   "<<uuid>>",
          "createdAt": "<<iso8601>>",
          "quantity":  "<<between:1:5>>",
          "vin":       "<<regex:[A-HJ-NPR-Z0-9]{17}>>",
          "customer":  { "email": "<<regex:qa\\+[a-z0-9]+@example\\.com>>" }
        },
        "url": "<<ends_with:/post>>"
      }
      """
    And the request payload should be echoed back in the response field "json"

  @api @regression @response_matcher
  Scenario: Match against a recorded response while skipping dynamic fields
    # The expected body below is a previously recorded response — its
    # "origin" and "requestedAt" values are stale on every new run, so
    # those paths are ignored instead of asserted.
    When I send a POST request to "post" with payload
      """
      {
        "framework":   "playwright-behave",
        "version":     "1.0",
        "requestedAt": "{{now}}"
      }
      """
    Then the response status code should be 200
    And the response body should match ignoring "origin, json.requestedAt, headers.*"
      """
      {
        "json": {
          "framework":   "playwright-behave",
          "version":     "1.0",
          "requestedAt": "2025-01-01T00:00:00Z"
        },
        "origin": "203.0.113.10",
        "args": {}
      }
      """

  @api @regression @schema_validation
  Scenario: Response conforms to its JSON schema
    When I send a POST request to "post" with payload
      """
      { "check": "schema" }
      """
    Then the response status code should be 200
    And the response should match schema "httpbin_post_response"

  @api @regression @request_chaining
  Scenario: Capture a field from one response and reuse it in the next payload
    When I send a POST request to "post" with payload
      """
      { "orderId": "{{uuid}}" }
      """
    Then the response status code should be 200
    And I save the response field "json.orderId" as "order_id"
    When I send a POST request to "post" with payload
      """
      { "lookupId": "{{saved:order_id}}", "action": "confirm" }
      """
    Then the response status code should be 200
    And the response field "json.lookupId" should equal the saved value "order_id"
    And the response field "json.action" should be "confirm"
