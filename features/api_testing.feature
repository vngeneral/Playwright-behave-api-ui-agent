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
