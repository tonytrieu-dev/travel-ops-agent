Feature: Server-backed identity and booking authorization
  Password authentication permits ordinary access when MFA is disabled, but only a verified
  second factor permits privileged actions. Device identifiers express identity consistency.

  Scenario: Password-only login permits ordinary access but not booking approval
    Given a traveler whose MFA is disabled
    When the traveler signs in with valid credentials
    Then the issued token permits listing owned trips
    But booking approval is forbidden without verified MFA

  Scenario: Authorized booking survives a booking-options provider failure
    Given an MFA-verified traveler with an owned pending booking
    When the traveler confirms and executes while the booking-options provider fails
    Then execution completes once with an audit transition and no fabricated booking links

  Scenario: Cross-tenant access triggers containment after repeated denials
    Given authenticated travelers in two different tenants
    When one traveler attempts to read the other tenant's trip five times
    Then each attempt is forbidden and the attacking session is revoked
    And the owning traveler can still read the trip

  Scenario: Global execution history applies the trip-read role policy
    Given an MFA-verified security operator
    When the operator requests global execution history
    Then the request is forbidden and the trip-read denial is recorded

  Scenario: Slack approval cannot bypass enforced booking authorization
    Given security enforcement is enabled and Slack credentials are configured
    And a pending booking owned by a traveler without verified MFA
    When an unmapped Slack user submits a correctly signed approval callback
    Then the callback is rejected and the booking remains pending without an audit transition

  Scenario: Non-ASCII MFA digits are rejected at intake
    Given a pending MFA session
    When the traveler submits six non-ASCII digits
    Then the request fails validation before MFA verification
