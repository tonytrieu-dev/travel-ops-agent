// Mirrors the backend's runtime request and response models.
// Keep field names and shapes exactly in sync with the backend runtime models, not assumptions.

export type FitnessLevel = "low" | "moderate" | "high"

export interface ControlResultOut {
  control: string
  status: "pass" | "fail" | "insufficient_evidence"
  summary: string
  evidence_event_ids: number[]
  details: Record<string, unknown>
}

export interface ComplianceAssessmentOut {
  id: number
  evaluator_version: string
  window_start: string
  window_end: string
  assessed_at: string
  results: ControlResultOut[]
}

export interface ComplianceMonitoringOut {
  stale: boolean
  assessments: ComplianceAssessmentOut[]
}

export type TripStatus = "created" | "flights_searched" | "itinerary_ready"

export type BookingState =
  | "PENDING_USER_CONFIRMATION"
  | "CONFIRMED"
  | "EXECUTED"
  | "CANCELLED"
  | "EXPIRED"

export type ErrorCode =
  | "booking_not_found"
  | "trip_not_found"
  | "flight_not_found"
  | "booking_expired"
  | "invalid_transition"
  | "validation_error"
  | "rate_limit_exceeded"
  | "connector_not_configured"
  | "forbidden"
  | "authentication_required"

export interface ProblemDetail {
  code: ErrorCode
  detail: string
}

export interface TripRequestCreate {
  origin: string
  destination: string
  destination_airport: string
  depart_date: string
  return_date?: string | null
  age: number
  fitness_level: FitnessLevel
}

export interface TripRequestUpdate {
  origin?: string | null
  destination?: string | null
  destination_airport?: string | null
  depart_date?: string | null
  return_date?: string | null
  age?: number | null
  fitness_level?: FitnessLevel | null
}

export interface TripRequestOut {
  id: number
  user_id: number
  origin: string
  destination: string
  destination_airport: string
  depart_date: string
  return_date?: string | null
  age: number
  fitness_level: FitnessLevel
  status: TripStatus
  created_at: string
}

export interface FlightLegOut {
  airline: string
  flight_number?: string | null
  departure_airport: string
  depart_at: string
  arrival_airport: string
  arrive_at: string
  duration_minutes?: number | null
}

export interface FlightOfferOut {
  id: number
  offer_index: number
  carrier: string
  price_usd: number
  currency: string
  depart_at: string
  arrive_at: string
  stops: number
  source: "live" | "cached"
  legs: FlightLegOut[]
}

export interface FlightSearchOut {
  offers: FlightOfferOut[]
  unavailable_reason?: string | null
  is_stale: boolean
}

export interface ActivityOut {
  name: string
  description: string
  intensity: "low" | "moderate" | "high"
  source_url: string
}

export interface ItineraryDayOut {
  day_number: number
  summary: string
  activities: ActivityOut[]
}

export interface ItineraryOut {
  days: ItineraryDayOut[]
}

export interface PlanReadyOut {
  status: "ready"
  itinerary: ItineraryOut
}

export interface TripSnapshotOut {
  trip: TripRequestOut
  flight_search: FlightSearchOut | null
  plan: PlanReadyOut | null
}

export interface PlanNeedsClarificationOut {
  status: "needs_clarification"
  questions: string[]
}

export interface PlanTooComplexOut {
  status: "too_complex"
  reason: string
}

export type PlanOut = PlanReadyOut | PlanNeedsClarificationOut | PlanTooComplexOut

export type AgentStepKind = "model" | "tool" | "output"

export type ExecutionEventKind = "api_call" | "db_query" | "protocol" | "hitl"

export interface AgentRunStepOut {
  seq: number
  kind: AgentStepKind
  name: string
  status: string
  duration_ms?: number | null
  input_summary?: string | null
  output_summary?: string | null
  tokens?: number | null
}

export interface AgentRunOut {
  id: number
  trip_request_id: number
  status: string
  model: string
  total_input_tokens: number
  total_output_tokens: number
  total_ms: number
  started_at: string
  finished_at?: string | null
  steps: AgentRunStepOut[]
  events: ExecutionEventOut[]
  estimated_cost_usd?: number | null
  budget_utilization_pct?: number | null
}

export interface ExecutionEventOut {
  seq: number
  kind: ExecutionEventKind
  name: string
  provider?: string | null
  status: string
  detail: string
  duration_ms?: number | null
  data?: Record<string, unknown> | null
  created_at: string
}

export interface ExecutionPanelOut {
  trip_request_id: number
  agent_runs: AgentRunOut[]
  events: ExecutionEventOut[]
}

export interface GlobalExecutionPanelOut {
  agent_runs: AgentRunOut[]
}

export interface BookingRequestCreate {
  flight_search_result_id: number
}

export interface BookingTransitionOut {
  from_state: BookingState
  to_state: BookingState
  reason: string
  actor_user_id?: number | null
  actor_email?: string | null
  created_at: string
}

export interface BookingLogOut {
  id: number
  trip_request_id: number
  flight_search_result_id: number
  state: BookingState
  booking_reference?: string | null
  booking_options?: Record<string, unknown>[] | null
  expires_at: string
  confirmed_at?: string | null
  executed_at?: string | null
  created_at: string
  transitions: BookingTransitionOut[]
}

export interface ConnectorStatusOut {
  configured: boolean
  enabled: boolean
}

export interface ConnectorsOut {
  slack: ConnectorStatusOut
}
