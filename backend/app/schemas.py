"""API request/response models — the strictly-typed boundary between HTTP and the domain.

These mirror the authored contract in specs/openapi.yaml; test_openapi_contract asserts the
runtime schema FastAPI generates from them stays in sync with that file.
"""

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.models import AgentStepKind, ExecutionEventKind, FitnessLevel, TripStatus
from app.state import BookingState

# Server timestamps are stored naive-UTC; serialize them with a +00:00 offset so a client doesn't
# misread them as local time (which skewed the booking countdown by the viewer's UTC offset).
UtcDatetime = Annotated[
    datetime, PlainSerializer(lambda value: value.replace(tzinfo=UTC).isoformat(), return_type=str)
]

# Airport codes are 3-letter uppercase IATA codes — the same rule the flight-search tool and the
# frontend Questionnaire enforce, applied here so the API is never weaker than the client.
_IATA_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")

# Traveler-age bounds mirror the frontend Questionnaire (min 0, max 130): the backend must not
# accept an age the client would have rejected, or the two validation layers disagree.
MIN_TRAVELER_AGE = 0
MAX_TRAVELER_AGE = 130

# Fields that must never be absent on a persisted trip; a PATCH may not clear any of them to null
# (return_date stays legitimately nullable and is deliberately excluded).
_REQUIRED_TRIP_FIELDS = (
    "origin",
    "destination",
    "destination_airport",
    "depart_date",
    "age",
    "fitness_level",
)


class ErrorCode(StrEnum):
    BOOKING_NOT_FOUND = "booking_not_found"
    TRIP_NOT_FOUND = "trip_not_found"
    FLIGHT_NOT_FOUND = "flight_not_found"
    BOOKING_EXPIRED = "booking_expired"
    INVALID_TRANSITION = "invalid_transition"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    CONNECTOR_NOT_CONFIGURED = "connector_not_configured"
    FORBIDDEN = "forbidden"
    AUTHENTICATION_REQUIRED = "authentication_required"
    INCIDENT_CONTAINED = "incident_contained"


def validate_trip_dates(depart_date: str, return_date: str | None) -> None:
    """Shared by TripRequestCreate and the trip-update repository path, so a PATCH that only
    changes one of the two dates is checked against the same rule as trip creation."""
    if date.fromisoformat(depart_date) < date.today():
        raise ValueError(f"depart_date {depart_date} is in the past.")
    if return_date is not None and date.fromisoformat(return_date) < date.fromisoformat(
        depart_date
    ):
        raise ValueError(f"return_date {return_date} is before depart_date {depart_date}.")


def _validate_iata_code(value: str, field_name: str | None) -> str:
    if not _IATA_CODE_PATTERN.match(value):
        raise ValueError(
            f"{field_name} must be a 3-letter uppercase IATA code (e.g. JFK), got {value!r}"
        )
    return value


def _validate_traveler_age(value: int) -> int:
    if not MIN_TRAVELER_AGE <= value <= MAX_TRAVELER_AGE:
        raise ValueError(
            f"age must be between {MIN_TRAVELER_AGE} and {MAX_TRAVELER_AGE}, got {value}"
        )
    return value


class ProblemDetail(BaseModel):
    code: ErrorCode
    detail: str


class ConnectorStatusOut(BaseModel):
    configured: bool
    enabled: bool


class ConnectorsOut(BaseModel):
    slack: ConnectorStatusOut


class ConnectorToggleUpdate(BaseModel):
    enabled: bool


class BookingRequestCreate(BaseModel):
    flight_search_result_id: int


class BookingTransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_state: BookingState
    to_state: BookingState
    reason: str
    correlation_id: str
    actor_user_id: int | None = None
    # Resolved for display so an audit reader sees who decided, not a raw foreign key. Null for a
    # system action (a TTL expiry has no actor) and for an anonymized user, whose audit rows
    # deliberately outlive their email (see User.email).
    actor_email: str | None = None
    created_at: UtcDatetime


class BookingLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_request_id: int
    flight_search_result_id: int
    state: BookingState
    booking_reference: str | None = None
    booking_options: list[dict[str, Any]] | None = None
    expires_at: UtcDatetime
    confirmed_at: UtcDatetime | None = None
    executed_at: UtcDatetime | None = None
    created_at: UtcDatetime
    transitions: list[BookingTransitionOut] = Field(default_factory=list)


class ActivityOut(BaseModel):
    name: str
    description: str
    # Closed vocabulary: free text here ("low to moderate (tram seated)") defeats every
    # downstream safety check, so reject it at parse time and let pydantic-ai retry.
    intensity: Literal["low", "moderate", "high"]
    source_url: str


class ItineraryDayOut(BaseModel):
    day_number: int
    summary: str
    activities: list[ActivityOut]


class ItineraryOut(BaseModel):
    days: list[ItineraryDayOut]


class ClarificationOut(BaseModel):
    questions: list[str]


class TripRequestCreate(BaseModel):
    origin: str
    destination: str
    destination_airport: str
    depart_date: str
    return_date: str | None = None
    age: int
    fitness_level: FitnessLevel

    @field_validator("origin", "destination_airport")
    @classmethod
    def _check_iata(cls, value: str, info: ValidationInfo) -> str:
        return _validate_iata_code(value, info.field_name)

    @field_validator("age")
    @classmethod
    def _check_age(cls, value: int) -> int:
        return _validate_traveler_age(value)

    @model_validator(mode="after")
    def _check_dates(self) -> "TripRequestCreate":
        validate_trip_dates(self.depart_date, self.return_date)
        return self


class TripRequestUpdate(BaseModel):
    origin: str | None = None
    destination: str | None = None
    destination_airport: str | None = None
    depart_date: str | None = None
    return_date: str | None = None
    age: int | None = None
    fitness_level: FitnessLevel | None = None

    @field_validator("origin", "destination_airport")
    @classmethod
    def _check_iata(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return value
        return _validate_iata_code(value, info.field_name)

    @field_validator("age")
    @classmethod
    def _check_age(cls, value: int | None) -> int | None:
        if value is None:
            return value
        return _validate_traveler_age(value)

    @model_validator(mode="after")
    def _reject_nulling_required_fields(self) -> "TripRequestUpdate":
        """A partial update omits fields it doesn't touch; sending an explicit null for a required
        field is a different intent — clearing it — which would leave the trip's criteria corrupt.
        Reject it at the boundary as a 422 instead of letting it 500 on the DB NOT NULL insert."""
        nulled = [
            field_name
            for field_name in _REQUIRED_TRIP_FIELDS
            if field_name in self.model_fields_set and getattr(self, field_name) is None
        ]
        if nulled:
            raise ValueError(
                f"these required trip fields cannot be cleared to null via update: {nulled}"
            )
        return self


class TripRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    origin: str
    destination: str
    destination_airport: str
    depart_date: str
    return_date: str | None = None
    age: int
    fitness_level: FitnessLevel
    status: TripStatus
    created_at: UtcDatetime


class FlightLegOut(BaseModel):
    airline: str
    flight_number: str | None = None
    departure_airport: str
    depart_at: str
    arrival_airport: str
    arrive_at: str
    duration_minutes: int | None = None


class FlightOfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    offer_index: int
    carrier: str
    price_usd: float
    currency: str
    depart_at: str
    arrive_at: str
    stops: int
    source: str
    legs: list[FlightLegOut] = Field(default_factory=list)


class FlightSearchOut(BaseModel):
    offers: list[FlightOfferOut]
    unavailable_reason: str | None = None
    is_stale: bool = False


class PlanReadyOut(BaseModel):
    status: Literal["ready"] = "ready"
    itinerary: ItineraryOut


class TripSnapshotOut(BaseModel):
    trip: TripRequestOut
    flight_search: FlightSearchOut | None = None
    plan: PlanReadyOut | None = None


class PlanNeedsClarificationOut(BaseModel):
    status: Literal["needs_clarification"] = "needs_clarification"
    questions: list[str]


class PlanTooComplexOut(BaseModel):
    """The run ran out of token budget before producing an itinerary. Distinct from
    PlanNeedsClarificationOut: no amount of extra detail from the user fixes this in the same
    pass — the honest fix is a shorter/simpler trip, not a form to fill out and resubmit."""

    status: Literal["too_complex"] = "too_complex"
    reason: str


PlanOut = Annotated[
    PlanReadyOut | PlanNeedsClarificationOut | PlanTooComplexOut,
    Field(discriminator="status"),
]

# What a planner run can return: the itinerary, a question, or an honest "too big for one pass".
# Threaded unchanged through dbos_runtime → trips_repository → the /plan route.
PlannerOutput = ItineraryOut | ClarificationOut | PlanTooComplexOut


class AgentRunStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: AgentStepKind
    name: str
    status: str
    duration_ms: int | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    tokens: int | None = None


class ExecutionEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: ExecutionEventKind
    name: str
    provider: str | None = None
    status: str
    detail: str
    duration_ms: int | None = None
    data: dict[str, Any] | None = None
    correlation_id: str
    created_at: UtcDatetime


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_request_id: int
    status: str
    model: str
    total_input_tokens: int
    total_output_tokens: int
    total_ms: int
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    steps: list[AgentRunStepOut] = Field(default_factory=list)
    events: list[ExecutionEventOut] = Field(default_factory=list)
    estimated_cost_usd: float | None = None
    budget_utilization_pct: float | None = None


class ExecutionPanelOut(BaseModel):
    trip_request_id: int
    agent_runs: list[AgentRunOut] = Field(default_factory=list)
    events: list[ExecutionEventOut] = Field(default_factory=list)


class GlobalExecutionPanelOut(BaseModel):
    """Every agent run across every trip the user owns — backs the execution history tab, which
    is global rather than scoped to whichever trip is currently active."""

    agent_runs: list[AgentRunOut] = Field(default_factory=list)


class SlackAuthErrorOut(BaseModel):
    detail: str


class LoginRequest(BaseModel):
    email: str
    device_id: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class MfaVerifyRequest(BaseModel):
    session_id: str = Field(min_length=1)
    code: str = Field(pattern=r"^\d{6}$")


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: UtcDatetime
    session_id: str
    mfa_required: bool


class SecurityEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    actor_user_id: int | None
    device_id: str
    session_id: str | None
    source_segment: str
    target_segment: str
    action: str
    resource: str
    decision: str
    reason: str
    correlation_id: str
    created_at: UtcDatetime


class SecurityIncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    actor_user_id: int | None
    device_id: str
    session_id: str | None
    reason: str
    status: str
    created_at: UtcDatetime
    contained_at: UtcDatetime | None


class SecurityEventQuery(BaseModel):
    actor_user_id: int | None = None
    decision: Literal["allow", "deny"] | None = None
    action: str | None = None
    since: datetime | None = None


class ServiceHeartbeatOut(BaseModel):
    status: Literal["ok"] = "ok"
