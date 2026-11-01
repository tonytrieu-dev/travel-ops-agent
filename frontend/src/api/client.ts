import type {
  BookingLogOut,
  BookingRequestCreate,
  ConnectorsOut,
  ComplianceMonitoringOut,
  ExecutionPanelOut,
  FlightSearchOut,
  GlobalExecutionPanelOut,
  PlanOut,
  ProblemDetail,
  TripRequestCreate,
  TripRequestOut,
  TripSnapshotOut,
  TripRequestUpdate,
} from "./types"

const API_BASE_URL = import.meta.env?.VITE_API_BASE_URL ?? "http://localhost:8000/api"
let sessionId: string | null = null
export const AUTHENTICATION_REQUIRED_EVENT = "travel-agent:authentication-required"
export const SESSION_CHANGED_STORAGE_KEY = "travel-agent.sessionChanged"

export type LoginResult = {
  expires_at: string
  session_id: string
  mfa_required: boolean
}

export type AuthConfig = { enforced: boolean }

export function getSessionId(): string | null {
  return sessionId
}

export async function getBrowserSession(): Promise<void> {
  const result = await request<{ session_id: string }>("/auth/browser/session", undefined, false)
  sessionId = result.session_id
}

export function getAuthConfig(): Promise<AuthConfig> {
  return request<AuthConfig>("/auth/config")
}

export class ApiError extends Error {
  code: ProblemDetail["code"]
  status: number

  constructor(problemDetail: ProblemDetail, status: number) {
    super(problemDetail.detail)
    this.code = problemDetail.code
    this.status = status
  }
}

async function request<TResponse>(
  path: string,
  options?: RequestInit,
  announceAuthenticationRequired = true,
): Promise<TResponse> {
  localStorage.removeItem("travel-agent.verifiedAccessToken")
  localStorage.removeItem("travel-agent.verifiedAccessTokenExpiresAt")
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  })

  if (response.status === 401 && announceAuthenticationRequired) {
    sessionId = null
    window.dispatchEvent(new Event(AUTHENTICATION_REQUIRED_EVENT))
  }
  if (!response.ok) {
    const problemDetail = (await response.json()) as ProblemDetail
    throw new ApiError(problemDetail, response.status)
  }

  return (await response.json()) as TResponse
}

function notifyOtherTabs(): void {
  try {
    localStorage.setItem(SESSION_CHANGED_STORAGE_KEY, crypto.randomUUID())
  } catch {
    // shortcut: unavailable storage leaves other tabs needing reload; add another transport if required.
  }
}

export async function login(email: string, password: string, device_id: string): Promise<LoginResult> {
  const result = await request<LoginResult>("/auth/browser/login", {
    method: "POST",
    body: JSON.stringify({ email, password, device_id }),
  }, false)
  if (!result.mfa_required) {
    sessionId = result.session_id
    notifyOtherTabs()
  }
  return result
}

export async function verifyMfa(session_id: string, code: string): Promise<LoginResult> {
  const result = await request<LoginResult>("/auth/browser/mfa", {
    method: "POST",
    body: JSON.stringify({ session_id, code }),
  }, false)
  sessionId = result.session_id
  notifyOtherTabs()
  return result
}

export function createTrip(tripRequestCreate: TripRequestCreate): Promise<TripRequestOut> {
  return request<TripRequestOut>("/trips", {
    method: "POST",
    body: JSON.stringify(tripRequestCreate),
  })
}

export function listTrips(): Promise<TripRequestOut[]> {
  return request<TripRequestOut[]>("/trips")
}

export function getTripSnapshot(tripId: number): Promise<TripSnapshotOut> {
  return request<TripSnapshotOut>(`/trips/${tripId}/snapshot`)
}

export function updateTrip(
  tripId: number,
  tripRequestUpdate: TripRequestUpdate,
): Promise<TripRequestOut> {
  return request<TripRequestOut>(`/trips/${tripId}`, {
    method: "PATCH",
    body: JSON.stringify(tripRequestUpdate),
  })
}

export function searchTripFlights(tripId: number): Promise<FlightSearchOut> {
  return request<FlightSearchOut>(`/trips/${tripId}/flights/search`, {
    method: "POST",
  })
}

export function planTrip(tripId: number): Promise<PlanOut> {
  return request<PlanOut>(`/trips/${tripId}/plan`, {
    method: "POST",
  })
}

export function getTripExecution(tripId: number): Promise<ExecutionPanelOut> {
  return request<ExecutionPanelOut>(`/trips/${tripId}/execution`)
}

export function getAllExecution(): Promise<GlobalExecutionPanelOut> {
  return request<GlobalExecutionPanelOut>("/execution")
}

export function listBookings(): Promise<BookingLogOut[]> {
  return request<BookingLogOut[]>("/bookings")
}

export function requestBooking(
  tripId: number,
  bookingRequestCreate: BookingRequestCreate,
): Promise<BookingLogOut> {
  return request<BookingLogOut>(`/trips/${tripId}/booking/request`, {
    method: "POST",
    body: JSON.stringify(bookingRequestCreate),
  })
}

export function getBooking(logId: number): Promise<BookingLogOut> {
  return request<BookingLogOut>(`/bookings/${logId}`)
}

export function confirmBooking(logId: number): Promise<BookingLogOut> {
  return request<BookingLogOut>(`/bookings/${logId}/confirm`, { method: "POST" })
}

export function executeBooking(logId: number): Promise<BookingLogOut> {
  return request<BookingLogOut>(`/bookings/${logId}/execute`, { method: "POST" })
}

export function cancelBooking(logId: number): Promise<BookingLogOut> {
  return request<BookingLogOut>(`/bookings/${logId}/cancel`, { method: "POST" })
}

export function getConnectors(): Promise<ConnectorsOut> {
  return request<ConnectorsOut>("/connectors")
}

export function getComplianceMonitoring(): Promise<ComplianceMonitoringOut> {
  return request<ComplianceMonitoringOut>("/security/monitoring")
}

export function getSecurityEvent(eventId: number): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/security/events/${eventId}`)
}

export function setSlackConnectorEnabled(enabled: boolean): Promise<ConnectorsOut> {
  return request<ConnectorsOut>("/connectors/slack", {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  })
}
