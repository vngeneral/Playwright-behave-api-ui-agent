Feature: Data-Driven Tests
  Exercises form submission and API endpoints using parameterised test data.
  Data sources: test_data/form_data.json  and  test_data/api_scenarios.json

  # ── Form: Scenario Outline with inline Examples ───────────────────────
  @data_driven @smoke
  Scenario Outline: Submit contact form with different user profiles
    Given the user navigates to the contact form
    When the user fills in name "<name>", phone "<phone>", email "<email>"
    And the user selects pizza size "<size>"
    And the user submits the form
    Then the form submission page should confirm the data was received

    Examples: Valid user profiles
      | name         | phone        | email                 | size   |
      | Alice Cooper | 321-654-9870 | alice@example.com     | large  |
      | Bob Lee      | 111-222-3333 | bob@example.com       | small  |

  # ── Form: special-character edge case ─────────────────────────────────
  @data_driven @regression
  Scenario: Form handles special characters in comments
    Given the user navigates to the contact form
    When the user enters the special-character comment from test data
    Then the textarea should contain the special characters

  # ── API: Scenario Outline across multiple endpoints ───────────────────
  @data_driven @api @smoke
  Scenario Outline: <description>
    When the user makes a <method> request to path "<path>"
    Then the response status should be <expected_status>

    Examples: Core API endpoints
      | description                 | method | path          | expected_status |
      | GET basic endpoint          | GET    | get           | 200             |
      | POST JSON body echoed back  | POST   | post          | 200             |
      | Bad request returns 400     | GET    | status/400    | 400             |
      | Not found returns 404       | GET    | status/404    | 404             |

  # ── API: JSON response body validation ────────────────────────────────
  @data_driven @api @regression
  Scenario: GET response body contains expected keys
    When the user makes a GET request to path "get"
    Then the response status should be 200
    And the JSON response should contain keys "origin,headers,url"
