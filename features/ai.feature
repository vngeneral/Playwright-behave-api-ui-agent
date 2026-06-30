Feature: AI-Driven Test Capabilities

  # Verifies that the AI selector healer activates on a broken selector,
  # queries the LLM, and logs the healing attempt.
  @ai_healing @regression
  Scenario: AI healer activates on broken selector and logs the attempt
    Given the user navigates to the contact form
    When the AI healer encounters a broken selector
    Then the AI healer log should contain the healing attempt

  # Verifies end-to-end form submission with AI-healed selectors
  # (ContactFormPage uses intentionally wrong attribute names)
  @ai_healing @smoke
  Scenario: Submit contact form using AI-healed selectors
    Given the user navigates to the contact form
    When the user fills out the contact form with valid data
    Then the form should be submitted successfully
