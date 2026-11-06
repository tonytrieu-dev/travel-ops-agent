import { useCallback, useEffect, useRef, useState } from "react"
import {
  createTrip, getTripSnapshot, listTrips, planTrip, searchTripFlights, updateTrip,
  extractErrorMessage,
} from "../api/client"
import type {
  FlightOfferOut, FlightSearchOut, PlanOut, TripRequestCreate, TripRequestOut,
} from "../api/types"
import type { ClarificationAnswers } from "../components/ItineraryPanel"

const ACTIVE_TRIP_ID_STORAGE_KEY = "travel-agent.activeTripId"

export function useActiveTrip(isAuthenticated: boolean | null) {
  const [trip, setTrip] = useState<TripRequestOut | null>(null)
  const [isRestoringTrip, setIsRestoringTrip] = useState(false)
  const [restoreTripError, setRestoreTripError] = useState<string | null>(null)
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

  // Selection-changing handlers call beginSelection(); handlers acting on the current selection
  // capture activeRequest.current and discard their result if a newer selection superseded it.
  const activeRequest = useRef(0)
  const beginSelection = () => {
    setIsRestoringTrip(false)
    setRestoreTripError(null)
    setIsPlanning(false)
    setIsSearchingFlights(false)
    return (activeRequest.current += 1)
  }

  // Shared by the hard-refresh restore below and by clicking a trip in the sidebar list — both
  // need to load a trip's snapshot into state and remember it as the active trip.
  const restoreTrip = useCallback((tripId: number) => {
    const requestId = beginSelection()
    setIsRestoringTrip(true)
    setRestoreTripError(null)
    return getTripSnapshot(tripId).then((snapshot) => {
      if (requestId !== activeRequest.current) return
      setTrip(snapshot.trip)
      setFlightSearchResult(snapshot.flight_search)
      setPlanResult(snapshot.plan)
      localStorage.setItem(ACTIVE_TRIP_ID_STORAGE_KEY, String(tripId))
    }).catch((error) => {
      if (requestId !== activeRequest.current) return
      localStorage.removeItem(ACTIVE_TRIP_ID_STORAGE_KEY)
      setRestoreTripError(extractErrorMessage(error))
    }).finally(() => {
      if (requestId === activeRequest.current) setIsRestoringTrip(false)
    })
  }, [])

  // Restore the active trip after a hard refresh so the execution history survives — React state
  // alone would lose the trip id and leave the ExecutionPanel with nothing to re-fetch.
  useEffect(() => {
    if (!isAuthenticated) return
    const storedTripId = localStorage.getItem(ACTIVE_TRIP_ID_STORAGE_KEY)
    if (!storedTripId) return
    restoreTrip(Number(storedTripId))
  }, [isAuthenticated, restoreTrip])

  useEffect(() => {
    if (!isAuthenticated) return
    listTrips()
      .then(setTrips)
      .catch(() => {})
  }, [isAuthenticated])

  const handleCreateTrip = async (tripRequestCreate: TripRequestCreate) => {
    const requestId = beginSelection()
    setIsCreatingTrip(true)
    setCreateTripError(null)
    try {
      const createdTrip = await createTrip(tripRequestCreate)
      if (requestId !== activeRequest.current) return
      setTrip(createdTrip)
      localStorage.setItem(ACTIVE_TRIP_ID_STORAGE_KEY, String(createdTrip.id))
      setTrips((previousTrips) => [createdTrip, ...previousTrips])
    } catch (error) {
      if (requestId !== activeRequest.current) return
      setCreateTripError(extractErrorMessage(error))
    } finally {
      setIsCreatingTrip(false)
    }
  }

  const handleSearchFlights = async () => {
    if (!trip || isRestoringTrip) return
    const requestId = activeRequest.current
    setIsSearchingFlights(true)
    setFlightSearchError(null)
    setSelectedOffer(null)
    try {
      const result = await searchTripFlights(trip.id)
      if (requestId !== activeRequest.current) return
      setFlightSearchResult(result)
    } catch (error) {
      if (requestId !== activeRequest.current) return
      setFlightSearchError(extractErrorMessage(error))
    } finally {
      if (requestId === activeRequest.current) setIsSearchingFlights(false)
    }
  }

  // Drops the current trip entirely so the Questionnaire renders fresh — the only way back to
  // the input form once a trip exists, for a traveler who wants to search a different flight.
  const handleStartNewSearch = () => {
    beginSelection()
    localStorage.removeItem(ACTIVE_TRIP_ID_STORAGE_KEY)
    setTrip(null)
    clearFlightAndBookingState()
    setPlanResult(null)
    setPlanError(null)
  }

  const handlePlan = async (answers?: ClarificationAnswers) => {
    if (!trip || isRestoringTrip) return
    const requestId = activeRequest.current
    setIsPlanning(true)
    setPlanError(null)
    try {
      if (answers) {
        const updatedTrip = await updateTrip(trip.id, answers)
        if (requestId !== activeRequest.current) return
        setTrip(updatedTrip)
        localStorage.setItem(ACTIVE_TRIP_ID_STORAGE_KEY, String(updatedTrip.id))
      }
      const planOutcome = await planTrip(trip.id)
      if (requestId !== activeRequest.current) return
      setPlanResult(planOutcome)
      if (planOutcome.status === "needs_clarification" || planOutcome.status === "too_complex")
        clearFlightAndBookingState()
    } catch (error) {
      if (requestId !== activeRequest.current) return
      setPlanError(extractErrorMessage(error))
    } finally {
      if (requestId === activeRequest.current) setIsPlanning(false)
    }
  }

  const handleRequestPlan = () => handlePlan()
  const handleAnswerClarification = (answers: ClarificationAnswers) => handlePlan(answers)

  const handleSearchAgain = () => {
    setSelectedOffer(null)
  }

  const handleSelectTrip = (tripId: number) => {
    if (trip?.id === tripId) return
    clearFlightAndBookingState()
    setPlanResult(null)
    setPlanError(null)
    restoreTrip(tripId)
  }

  // When a re-plan comes back needing clarification, the trip has effectively changed — drop any
  // flight results, selection, and (via BookingModule's remount key) booking state so the UI never
  // shows offers or a booking tied to the now-stale trip.
  const clearFlightAndBookingState = () => {
    setFlightSearchResult(null)
    setSelectedOffer(null)
    setFlightSearchError(null)
  }

  return {
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
  }
}
