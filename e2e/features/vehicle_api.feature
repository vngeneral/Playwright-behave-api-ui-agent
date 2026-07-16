Feature: Vehicle Registration API — Platform Partner BL4B
  As a platform partner
  I want to register and deregister vehicles via the BL4B API
  So that vehicle eligibility is accurately maintained

  Background:
    Given Vehicle API client is initialized

  # ────────────────────────────────────────────────────────────────────────────
  # Registration — smoke
  # ────────────────────────────────────────────────────────────────────────────

  @api @smoke @vehicle_registration @testrail_C448337
  Scenario: Register a single valid VIN
    When I register VIN "KMUHCESC7RU179347" for partner "HBL4BP-006"
    Then the API response status should be 200
    And the response body should be valid JSON
    And the response should contain a transaction reference

  @api @smoke @vehicle_registration @testrail_C448338
  Scenario: Register a batch of VINs in one request
    When I register the following VINs for partner "HBL4BP-006"
      | vin               |
      | KMUHCESC7RU179347 |
      | KM8JFCA14PU000001 |
      | KM8J3CA46MU283910 |
    Then the API response status should be 200
    And the response body should be valid JSON

  # ────────────────────────────────────────────────────────────────────────────
  # Deregistration — smoke
  # ────────────────────────────────────────────────────────────────────────────

  @api @smoke @vehicle_deregistration @testrail_C448339
  Scenario: Deregister a single valid VIN
    When I deregister VIN "KM8JFDA25PU103343" for partner "HBL4BP-006"
    Then the API response status should be 200
    And the response body should be valid JSON
    And the response should contain a transaction reference

  @api @smoke @vehicle_deregistration @testrail_C448340
  Scenario: Deregister a batch of VINs in one request
    When I deregister the following VINs for partner "HBL4BP-006"
      | vin               |
      | KM8JFDA25PU103343 |
      | KM8J3CA46MU283910 |
    Then the API response status should be 200
    And the response body should be valid JSON

  # ────────────────────────────────────────────────────────────────────────────
  # Scenario Outline — register with different partner/VIN combinations
  # ────────────────────────────────────────────────────────────────────────────

  @api @regression @vehicle_registration
  Scenario Outline: Register VINs for multiple partner codes
    When I register VIN "<vin>" for partner "<partner_code>"
    Then the API response status should be <expected_status>

    Examples: Valid registrations
      | vin               | partner_code | expected_status |
      | KMUHCESC7RU179347 | HBL4BP-006   | 200             |
      | KM8JFCA14PU000001 | HBL4BP-006   | 200             |

  # ────────────────────────────────────────────────────────────────────────────
  # Authentication errors
  # ────────────────────────────────────────────────────────────────────────────

  @api @regression @vehicle_security @testrail_C448341
  Scenario: Registration with invalid API key returns 401 or 403
    When I register VIN "KMUHCESC7RU179347" for partner "HBL4BP-006" with an invalid API key
    Then the API response status should indicate unauthorised

  # ────────────────────────────────────────────────────────────────────────────
  # Validation errors (400)
  # ────────────────────────────────────────────────────────────────────────────

  @api @regression @vehicle_validation @testrail_C448342
  Scenario: Registration without transactionId returns 400
    When I register VIN "KMUHCESC7RU179347" for partner "HBL4BP-006" omitting field "transactionId"
    Then the API response status should be 400

  @api @regression @vehicle_validation @testrail_C448343
  Scenario: Registration without partnerCode returns 400
    When I register VIN "KMUHCESC7RU179347" for partner "HBL4BP-006" omitting field "partnerCode"
    Then the API response status should be 400

  @api @regression @vehicle_validation @testrail_C448344
  Scenario: Registration with empty VIN list returns 400
    When I register an empty VIN list for partner "HBL4BP-006"
    Then the API response status should be 400

  # ────────────────────────────────────────────────────────────────────────────
  # Response timing (non-functional)
  # ────────────────────────────────────────────────────────────────────────────

  @api @performance @vehicle_registration
  Scenario: Registration response time is within threshold
    When I register VIN "KMUHCESC7RU179347" for partner "HBL4BP-006"
    Then the API response time should be under 5000 ms
