import type {
  BookingLogOut,
  BookingRequestCreate,
  ConnectorsOut,
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
const ACCESS_TOKEN_STORAGE_KEY = "travel-agent.verifiedAccessToken"
const ACCESS_TOKEN_EXPIRATION_STORAGE_KEY = "travel-agent.verifiedAccessTokenExpiresAt"
export const AUTHENTICATION_REQUIRED_EVENT = "travel-agent:authentication-required"

export type LoginResult = {
  access_token: string
  expires_at: string
  session_id: string
  mfa_required: boolean
}

export type AuthConfig = { enforced: boolean }

function clearCredentials(): void {
  localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY)
  localStorage.removeItem(ACCESS_TOKEN_EXPIRATION_STORAGE_KEY)
}

export function getAccessToken(): string | null {
  const accessToken = localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)
  const expiresAt = localStorage.getItem(ACCESS_TOKEN_EXPIRATION_STORAGE_KEY)
  const expiration = Date.parse(expiresAt ?? "")
  if (!accessToken || !Number.isFinite(expiration) || expiration <= Date.now()) {
    clearCredentials()
    return null
  }
  return accessToken
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

async function request<TResponse>(path: string, options?: RequestInit): Promise<TResponse> {
  const accessToken = getAccessToken()
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...options?.headers,
    },
  })

  if (response.status === 401) {
    clearCredentials()
    window.dispatchEvent(new Event(AUTHENTICATION_REQUIRED_EVENT))
  }
  if (!response.ok) {
    const problemDetail = (await response.json()) as ProblemDetail
    throw new ApiError(problemDetail, response.status)
  }

  return (await response.json()) as TResponse
}

export async function login(email: string, password: string, device_id: string): Promise<LoginResult> {
  const result = await request<LoginResult>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password, device_id }),
  })
  if (!result.mfa_required) storeCredentials(result)
  return result
}

export async function verifyMfa(session_id: string, code: string): Promise<LoginResult> {
  const result = await request<LoginResult>("/auth/mfa", {
    method: "POST",
    body: JSON.stringify({ session_id, code }),
  })
  storeCredentials(result)
  return result
}

function storeCredentials(result: LoginResult): void {
  localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, result.access_token)
  localStorage.setItem(ACCESS_TOKEN_EXPIRATION_STORAGE_KEY, result.expires_at)
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

export function setSlackConnectorEnabled(enabled: boolean): Promise<ConnectorsOut> {
  return request<ConnectorsOut>("/connectors/slack", {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  })
}
