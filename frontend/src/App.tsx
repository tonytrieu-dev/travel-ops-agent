import { useCallback, useEffect, useState } from "react"
import {
  AUTHENTICATION_REQUIRED_EVENT,
  ApiError,
  createTrip,
  getAccessToken,
  getAuthConfig,
  getTripSnapshot,
  listTrips,
  login,
  planTrip,
  searchTripFlights,
  verifyMfa,
  updateTrip,
} from "./api/client"
import type {
  FlightOfferOut,
  FlightSearchOut,
  PlanOut,
  TripRequestCreate,
  TripRequestOut,
} from "./api/types"
import { ApprovalHistoryPanel } from "./components/ApprovalHistoryPanel"
import { BookingModule } from "./components/BookingModule"
import { ConnectorsPanel } from "./components/ConnectorsPanel"
import { ExecutionPanel } from "./components/ExecutionPanel"
import { FlightSearch } from "./components/FlightSearch"
import { ItineraryPanel, type ClarificationAnswers } from "./components/ItineraryPanel"
import { LiveActivity } from "./components/LiveActivity"
import { Questionnaire } from "./components/Questionnaire"
import { YourTripsPanel } from "./components/YourTripsPanel"

function extractErrorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong. Please try again."
}

function LoginPanel({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [email, setEmail] = useState("traveler@tenant-a.test")
  const [password, setPassword] = useState("")
  const [deviceId, setDeviceId] = useState("device-a-traveler")
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)

  const submitLogin = async (event: React.FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      const result = await login(email, password, deviceId)
      if (result.mfa_required) setSessionId(result.session_id)
      else onAuthenticated()
    } catch (error) {
      setError(extractErrorMessage(error))
    }
  }

  const submitMfa = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!sessionId) return
    setError(null)
    try {
      await verifyMfa(sessionId, code)
      onAuthenticated()
    } catch (error) {
      setError(extractErrorMessage(error))
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
      <form onSubmit={sessionId ? submitMfa : submitLogin} className="w-full max-w-sm space-y-4 rounded-xl bg-white p-6 shadow">
        <h1 className="text-2xl font-bold">Travel Agent sign in</h1>
        {!sessionId ? (
          <>
            <input aria-label="Email" required type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email" className="w-full rounded border p-2" />
            <input aria-label="Password" required type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" className="w-full rounded border p-2" />
            <input aria-label="Device" required value={deviceId} onChange={(event) => setDeviceId(event.target.value)} placeholder="Device ID" className="w-full rounded border p-2" />
            <button className="w-full rounded bg-indigo-600 p-2 text-white">Continue to MFA</button>
          </>
        ) : (
          <>
            <label className="block text-sm">Enter your authenticator code
              <input aria-label="MFA code" required inputMode="numeric" value={code} onChange={(event) => setCode(event.target.value)} className="mt-1 w-full rounded border p-2" />
            </label>
            <button className="w-full rounded bg-indigo-600 p-2 text-white">Verify and sign in</button>
          </>
        )}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      </form>
    </main>
  )
}

const ACTIVE_TRIP_ID_STORAGE_KEY = "travel-agent.activeTripId"

type TabKey = "trip" | "trips" | "execution" | "approvals" | "connectors"

const TABS: { key: TabKey; label: string }[] = [
  { key: "trip", label: "Plan a trip" },
  { key: "trips", label: "Your trips" },
  { key: "execution", label: "Agent execution history" },
  { key: "approvals", label: "Approval history" },
  { key: "connectors", label: "Connectors" },
]

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)
  const [activeTab, setActiveTab] = useState<TabKey>("trip")

  const [trip, setTrip] = useState<TripRequestOut | null>(null)
  const [isCreatingTrip, setIsCreatingTrip] = useState(false)
  const [createTripError, setCreateTripError] = useState<string | null>(null)

  const [trips, setTrips] = useState<TripRequestOut[]>([])

  const [flightSearchResult, setFlightSearchResult] = useState<FlightSearchOut | null>(null)
  const [isSearchingFlights, setIsSearchingFlights] = useState(false)
  const [flightSearchError, setFlightSearchError] = useState<string | null>(null)
  const [selectedOffer, setSelectedOffer] = useState<FlightOfferOut | null>(null)

  const [planResult, setPlanResult] = useState<PlanOut | null>(null)
  const [isPlanning, setIsPlanning] = useState(false)
  const [planError, setPlanError] = useState<string | null>(null)

  const isRunActive = isSearchingFlights || isPlanning

  useEffect(() => {
    const requireAuthentication = () => window.location.reload()
    window.addEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication)
    return () => window.removeEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication)
  }, [])

  useEffect(() => {
    getAuthConfig()
      .then(({ enforced }) => setIsAuthenticated(!enforced || Boolean(getAccessToken())))
      .catch(() => setIsAuthenticated(false))
  }, [])

  // Shared by the hard-refresh restore below and by clicking a trip in the sidebar list — both
  // need to load a trip's snapshot into state and remember it as the active trip.
  const restoreTrip = useCallback((tripId: number) => {
    return getTripSnapshot(tripId).then((snapshot) => {
      setTrip(snapshot.trip)
      setFlightSearchResult(snapshot.flight_search)
      setPlanResult(snapshot.plan)
      localStorage.setItem(ACTIVE_TRIP_ID_STORAGE_KEY, String(tripId))
    })
  }, [])

  // Restore the active trip after a hard refresh so the execution history survives — React state
  // alone would lose the trip id and leave the ExecutionPanel with nothing to re-fetch.
  useEffect(() => {
    if (!isAuthenticated) return
    const storedTripId = localStorage.getItem(ACTIVE_TRIP_ID_STORAGE_KEY)
    if (!storedTripId) return
    restoreTrip(Number(storedTripId)).catch(() =>
      localStorage.removeItem(ACTIVE_TRIP_ID_STORAGE_KEY),
    )
  }, [isAuthenticated, restoreTrip])

  useEffect(() => {
    if (!isAuthenticated) return
    listTrips()
      .then(setTrips)
      .catch(() => {})
  }, [isAuthenticated])

  const handleCreateTrip = async (tripRequestCreate: TripRequestCreate) => {
    setIsCreatingTrip(true)
    setCreateTripError(null)
    try {
      const createdTrip = await createTrip(tripRequestCreate)
      setTrip(createdTrip)
      localStorage.setItem(ACTIVE_TRIP_ID_STORAGE_KEY, String(createdTrip.id))
      setTrips((previousTrips) => [createdTrip, ...previousTrips])
    } catch (error) {
      setCreateTripError(extractErrorMessage(error))
    } finally {
      setIsCreatingTrip(false)
    }
  }

  const handleSearchFlights = async () => {
    if (!trip) return
    setIsSearchingFlights(true)
    setFlightSearchError(null)
    setSelectedOffer(null)
    try {
      setFlightSearchResult(await searchTripFlights(trip.id))
    } catch (error) {
      setFlightSearchError(extractErrorMessage(error))
    } finally {
      setIsSearchingFlights(false)
    }
  }

  // Drops the current trip entirely so the Questionnaire renders fresh — the only way back to
  // the input form once a trip exists, for a traveler who wants to search a different flight.
  const handleStartNewSearch = () => {
    localStorage.removeItem(ACTIVE_TRIP_ID_STORAGE_KEY)
    setTrip(null)
    clearFlightAndBookingState()
    setPlanResult(null)
    setPlanError(null)
  }

  const handleRequestPlan = async () => {
    if (!trip) return
    setIsPlanning(true)
    setPlanError(null)
    try {
      const planOutcome = await planTrip(trip.id)
      setPlanResult(planOutcome)
      if (planOutcome.status === "needs_clarification" || planOutcome.status === "too_complex")
        clearFlightAndBookingState()
    } catch (error) {
      setPlanError(extractErrorMessage(error))
    } finally {
      setIsPlanning(false)
    }
  }

  const handleAnswerClarification = async (answers: ClarificationAnswers) => {
    if (!trip) return
    setIsPlanning(true)
    setPlanError(null)
    try {
      const updatedTrip = await updateTrip(trip.id, answers)
      setTrip(updatedTrip)
      localStorage.setItem(ACTIVE_TRIP_ID_STORAGE_KEY, String(updatedTrip.id))
      const planOutcome = await planTrip(trip.id)
      setPlanResult(planOutcome)
      if (planOutcome.status === "needs_clarification" || planOutcome.status === "too_complex")
        clearFlightAndBookingState()
    } catch (error) {
      setPlanError(extractErrorMessage(error))
    } finally {
      setIsPlanning(false)
    }
  }

  const handleSearchAgain = () => {
    setSelectedOffer(null)
  }

  const handleSelectTrip = (tripId: number) => {
    if (trip?.id === tripId) return
    clearFlightAndBookingState()
    setPlanResult(null)
    setPlanError(null)
    restoreTrip(tripId)
    setActiveTab("trip")
  }

  // When a re-plan comes back needing clarification, the trip has effectively changed — drop any
  // flight results, selection, and (via BookingModule's remount key) booking state so the UI never
  // shows offers or a booking tied to the now-stale trip.
  const clearFlightAndBookingState = () => {
    setFlightSearchResult(null)
    setSelectedOffer(null)
    setFlightSearchError(null)
  }

  if (isAuthenticated === null) return null
  if (!isAuthenticated) return <LoginPanel onAuthenticated={() => setIsAuthenticated(true)} />

  return (
    <div className="flex min-h-screen flex-col bg-slate-50 text-slate-900 md:flex-row">
      <aside className="flex w-full shrink-0 flex-col border-b border-slate-200 bg-white md:sticky md:top-0 md:h-screen md:w-80 md:border-b-0 md:border-r">
        <div className="border-b border-slate-200 px-6 py-7">
          <h1 className="cursor-default text-3xl font-bold tracking-tight text-slate-900">
            Travel Agent
          </h1>
          <p className="mt-3 text-sm leading-6 text-slate-500">
            AI trip planner with human approval before airline checkout
          </p>
        </div>

        <nav className="space-y-2 p-4" aria-label="Primary">
          {TABS.map((tab) => {
            const isActive = activeTab === tab.key
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTab(tab.key)}
                aria-current={isActive ? "page" : undefined}
                className={`flex min-h-11 w-full items-center justify-between rounded-lg border px-4 py-2.5 text-left text-sm font-medium transition ${
                  isActive
                    ? "border-indigo-100 bg-indigo-50 text-indigo-700"
                    : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900"
                }`}
              >
                {tab.label}
                {tab.key === "execution" && isRunActive && (
                  <span className="relative flex h-2 w-2" aria-label="run in progress">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-indigo-400 opacity-75" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-indigo-500" />
                  </span>
                )}
              </button>
            )
          })}
        </nav>

        <div className="flex-1" />
      </aside>

      <div className="flex min-h-screen flex-1 flex-col">
        <main className="mx-auto w-full max-w-3xl flex-1 space-y-6 px-6 py-8">
          {activeTab === "trip" && (
            <>
              {!trip && (
                <Questionnaire
                  onSubmit={handleCreateTrip}
                  isSubmitting={isCreatingTrip}
                  errorMessage={createTripError}
                />
              )}

              {trip && (
                <>
                  <FlightSearch
                    trip={trip}
                    searchResult={flightSearchResult}
                    isLoading={isSearchingFlights}
                    errorMessage={flightSearchError}
                    selectedOfferId={selectedOffer?.id ?? null}
                    onSearchFlights={handleSearchFlights}
                    onSearchNewFlight={handleStartNewSearch}
                    onSelectOffer={setSelectedOffer}
                  />

                  <BookingModule
                    key={selectedOffer ? selectedOffer.id : "none"}
                    trip={trip}
                    selectedOffer={selectedOffer}
                    onSearchAgain={handleSearchAgain}
                  />

                  <ItineraryPanel
                    trip={trip}
                    planResult={planResult}
                    isLoading={isPlanning}
                    errorMessage={planError}
                    onRequestPlan={handleRequestPlan}
                    onAnswerClarification={handleAnswerClarification}
                  />

                  <LiveActivity tripId={trip.id} isRunActive={isRunActive} />
                </>
              )}
            </>
          )}

          {activeTab === "trips" && (
            <YourTripsPanel
              trips={trips}
              selectedTripId={trip?.id ?? null}
              onSelectTrip={handleSelectTrip}
            />
          )}

          {activeTab === "execution" && <ExecutionPanel trips={trips} isRunActive={isRunActive} />}

          {activeTab === "approvals" && (
            <ApprovalHistoryPanel trips={trips} isRunActive={isRunActive} />
          )}

          {activeTab === "connectors" && <ConnectorsPanel />}
        </main>

        <footer className="mt-10 border-t border-slate-200 py-6 text-center text-xs text-slate-400">
          <p>flights via Google Flights (SearchApi.io) · activity research via Tavily</p>
        </footer>
      </div>
    </div>
  )
}

export default App
