import { useEffect, useState } from "react"
import {
  AUTHENTICATION_REQUIRED_EVENT,
  SESSION_CHANGED_STORAGE_KEY,
  getBrowserSession,
  getAuthConfig,
  login,
  verifyMfa,
  extractErrorMessage,
} from "./api/client"
import { ApprovalHistoryPanel } from "./components/ApprovalHistoryPanel"
import { BookingModule } from "./components/BookingModule"
import { ConnectorsPanel } from "./components/ConnectorsPanel"
import { CompliancePanel } from "./components/CompliancePanel"
import { ExecutionPanel } from "./components/ExecutionPanel"
import { FlightSearch } from "./components/FlightSearch"
import { ItineraryPanel } from "./components/ItineraryPanel"
import { LiveActivity } from "./components/LiveActivity"
import { Questionnaire } from "./components/Questionnaire"
import { useActiveTrip } from "./hooks/useActiveTrip"
import { YourTripsPanel } from "./components/YourTripsPanel"

export function LoginPanel({ onAuthenticated }: { onAuthenticated: () => void }) {
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

  // Without this, an expired or revoked MFA session traps the form resubmitting a dead session.
  const restartSignIn = () => {
    setSessionId(null)
    setCode("")
    setError(null)
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
            <button type="submit" className="w-full rounded bg-indigo-600 p-2 text-white">Continue to MFA</button>
          </>
        ) : (
          <>
            <label className="block text-sm">Enter your authenticator code
              <input aria-label="MFA code" required inputMode="numeric" value={code} onChange={(event) => setCode(event.target.value)} className="mt-1 w-full rounded border p-2" />
            </label>
            <button type="submit" className="w-full rounded bg-indigo-600 p-2 text-white">Verify and sign in</button>
            <button type="button" onClick={restartSignIn} className="w-full rounded border border-slate-300 p-2 text-sm text-slate-600">Start over</button>
          </>
        )}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      </form>
    </main>
  )
}

type TabKey = "trip" | "trips" | "execution" | "approvals" | "connectors" | "compliance"

const TABS: { key: TabKey; label: string }[] = [
  { key: "trip", label: "Plan a trip" },
  { key: "trips", label: "Your trips" },
  { key: "execution", label: "Agent execution history" },
  { key: "approvals", label: "Approval history" },
  { key: "connectors", label: "Connectors" },
  { key: "compliance", label: "Security monitoring" },
]

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)
  const [activeTab, setActiveTab] = useState<TabKey>("trip")

  const {
    trip,
    isRestoringTrip,
    restoreTripError,
    isCreatingTrip,
    createTripError,
    trips,
    flightSearchResult,
    isSearchingFlights,
    flightSearchError,
    selectedOffer,
    setSelectedOffer,
    planResult,
    isPlanning,
    planError,
    isRunActive,
    handleCreateTrip,
    handleSearchFlights,
    handleStartNewSearch,
    handleRequestPlan,
    handleAnswerClarification,
    handleSearchAgain,
    handleSelectTrip,
  } = useActiveTrip(isAuthenticated)

  useEffect(() => {
    const requireAuthentication = () => window.location.reload()
    const sessionChanged = (event: StorageEvent) => {
      if (event.storageArea === localStorage && event.key === SESSION_CHANGED_STORAGE_KEY && event.newValue)
        window.location.reload()
    }
    window.addEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication)
    window.addEventListener("storage", sessionChanged)
    return () => {
      window.removeEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication)
      window.removeEventListener("storage", sessionChanged)
    }
  }, [])

  useEffect(() => {
    getAuthConfig()
      .then(async ({ enforced }) => {
        if (enforced) await getBrowserSession()
        setIsAuthenticated(true)
      })
      .catch(() => setIsAuthenticated(false))
  }, [])

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
              {restoreTripError && <p role="alert">{restoreTripError}</p>}
              {isRestoringTrip && <p role="status">Loading trip…</p>}
              {!trip && !isRestoringTrip && (
                <Questionnaire
                  onSubmit={handleCreateTrip}
                  isSubmitting={isCreatingTrip}
                  errorMessage={createTripError}
                />
              )}

              {trip && !isRestoringTrip && (
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
              onSelectTrip={(tripId) => { handleSelectTrip(tripId); setActiveTab("trip") }}
            />
          )}

          {activeTab === "execution" && <ExecutionPanel trips={trips} isRunActive={isRunActive} />}

          {activeTab === "approvals" && (
            <ApprovalHistoryPanel trips={trips} isRunActive={isRunActive} />
          )}

          {activeTab === "connectors" && <ConnectorsPanel />}
          {activeTab === "compliance" && <CompliancePanel />}
        </main>

        <footer className="mt-10 border-t border-slate-200 py-6 text-center text-xs text-slate-400">
          <p>flights via Google Flights (SearchApi.io) · activity research via Tavily</p>
        </footer>
      </div>
    </div>
  )
}

export default App
