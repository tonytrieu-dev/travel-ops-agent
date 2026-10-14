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
