import { useEffect, useRef, useState } from "react"
import {
  ApiError,
  cancelBooking,
  confirmBooking,
  executeBooking,
  getBooking,
  getConnectors,
  getAuthConfig,
  requestBooking,
} from "../api/client"
import type { BookingLogOut, FlightOfferOut, TripRequestOut } from "../api/types"

interface BookingModuleProps {
  trip: TripRequestOut
  selectedOffer: FlightOfferOut | null
  onSearchAgain: () => void
}

const POLL_INTERVAL_MS = 1_000

// SearchApi's real booking link isn't a plain URL: booking_request.url is Google's redirect
// endpoint, and it only resolves to the real airline/OTA checkout when POSTed with post_data
// as the body — a GET (plain <a href>) 404s. post_data is itself a urlencoded "key=value" pair.
function bookingFormFields(postData: string): [string, string][] {
  return Array.from(new URLSearchParams(postData).entries())
}

function bookingOptionLabel(option: Record<string, unknown>): string | null {
  if (typeof option.book_with === "string") return option.book_with
  if (typeof option.name === "string") return option.name
  return null
}

function bookingOptionPrice(option: Record<string, unknown>): number {
  return typeof option.price === "number" ? option.price : Number.POSITIVE_INFINITY
}

// SearchApi returns one booking_option per fare class/OTA, so the same provider often appears
// several times — collapse to the cheapest option per provider instead of a wall of duplicate
// provider checkout buttons.
function cheapestPerProvider(options: Record<string, unknown>[]): Record<string, unknown>[] {
  const cheapestByLabel = new Map<string, Record<string, unknown>>()
  options.forEach((option, index) => {
    const key = bookingOptionLabel(option) ?? `option-${index}`
    const existing = cheapestByLabel.get(key)
    if (!existing || bookingOptionPrice(option) < bookingOptionPrice(existing)) {
      cheapestByLabel.set(key, option)
    }
  })
  return Array.from(cheapestByLabel.values())
}

function stateLabel(state: string): string {
  return state.replaceAll("_", " ").toLowerCase()
}

function searchAgainMessage(state: string): string | null {
  if (state === "EXPIRED")
    return "This booking expired before it was completed. Please search again for current prices."
  return null
}

export function BookingModule({ trip, selectedOffer, onSearchAgain }: BookingModuleProps) {
  const [bookingLog, setBookingLog] = useState<BookingLogOut | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState("")
  const [isSlackApprovalEnabled, setIsSlackApprovalEnabled] = useState<boolean | null>(null)
  const previousStateRef = useRef<string | null>(null)

  // Poll the server for real state while a booking is open and not yet terminal — the panel
  // never fabricates a transition the server hasn't confirmed.
  useEffect(() => {
    if (!bookingLog || isLoading) return
    const terminal = ["EXECUTED", "CANCELLED", "EXPIRED"]
    if (terminal.includes(bookingLog.state)) return

    const poll = setInterval(async () => {
      try {
        const refreshed = await getBooking(bookingLog.id)
        setBookingLog(refreshed)
      } catch {
        // Transient poll failures are not surfaced — the next successful poll self-corrects.
      }
    }, POLL_INTERVAL_MS)
    return () => clearInterval(poll)
  }, [bookingLog, isLoading])

  useEffect(() => {
    if (bookingLog && bookingLog.state !== previousStateRef.current) {
      setAnnouncement(`Booking is now ${stateLabel(bookingLog.state)}.`)
      previousStateRef.current = bookingLog.state
    }
  }, [bookingLog])

  useEffect(() => {
    getAuthConfig()
      .then(async ({ enforced }) => {
        if (enforced) {
          setIsSlackApprovalEnabled(false)
          return
        }
        const connectors = await getConnectors()
        setIsSlackApprovalEnabled(connectors.slack.configured && connectors.slack.enabled)
      })
      .catch(() => setIsSlackApprovalEnabled(false))
  }, [])

  if (!selectedOffer) {
    return (
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="text-lg font-semibold text-slate-900">Continue to airline booking</h2>
        <p className="mt-2 text-sm text-slate-500">Select a flight offer above to review it.</p>
      </section>
    )
  }

  const searchAgainText = bookingLog ? searchAgainMessage(bookingLog.state) : null
  const bookingOptions = bookingLog?.booking_options
    ? cheapestPerProvider(bookingLog.booking_options).filter((option) => {
        const request = option.booking_request
        return (
          bookingOptionLabel(option)?.toLowerCase() === selectedOffer.carrier.toLowerCase() &&
          typeof request === "object" &&
          request !== null &&
          typeof (request as Record<string, unknown>).url === "string" &&
          typeof (request as Record<string, unknown>).post_data === "string"
        )
      })
    : []

  const runAction = async (action: () => Promise<BookingLogOut>) => {
    setIsLoading(true)
    setErrorMessage(null)
    try {
      const updated = await action()
      setBookingLog(updated)
    } catch (error) {
      if (error instanceof ApiError) {
        setErrorMessage(error.message)
        if (error.code === "booking_expired" && bookingLog) {
          // Trust the server's definitive verdict over our own clock-based guess.
          try {
            setBookingLog(await getBooking(bookingLog.id))
          } catch {
            // Fall through — errorMessage already reflects the expiry.
          }
        }
      } else {
        setErrorMessage("Something went wrong. Please try again.")
      }
    } finally {
      setIsLoading(false)
    }
  }

  const handleRequest = () =>
    runAction(() => requestBooking(trip.id, { flight_search_result_id: selectedOffer.id }))
  const handleConfirm = () =>
    bookingLog &&
    runAction(async () => {
      await confirmBooking(bookingLog.id)
      return executeBooking(bookingLog.id)
    })
  const handleExecute = () => bookingLog && runAction(() => executeBooking(bookingLog.id))
  const handleCancel = () => bookingLog && runAction(() => cancelBooking(bookingLog.id))

  return (
    <section className="rounded-xl border-2 border-indigo-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg font-semibold text-slate-900">Continue to airline booking</h2>
      <p className="mt-2 text-sm leading-6 text-slate-600">
        {selectedOffer.carrier} · ${selectedOffer.price_usd.toFixed(2)} {selectedOffer.currency}
      </p>

      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      {errorMessage && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {errorMessage}
        </p>
      )}

      {!bookingLog && (
        <button
          type="button"
          onClick={handleRequest}
          disabled={isLoading}
          className="mt-4 rounded-md bg-indigo-600 px-4 py-2.5 font-semibold text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {isLoading ? "Reviewing…" : "Review booking"}
        </button>
      )}

      {bookingLog?.state === "PENDING_USER_CONFIRMATION" && isSlackApprovalEnabled !== false && (
        <p className="mt-4 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {isSlackApprovalEnabled
            ? "Waiting for approval in Slack. Use the Slack message to approve or reject this flight."
            : "Checking approval channel…"}
        </p>
      )}

      {bookingLog?.state === "PENDING_USER_CONFIRMATION" && !isSlackApprovalEnabled && (
        <div className="mt-4 flex gap-3">
          <button
            type="button"
            onClick={handleConfirm}
            disabled={isLoading}
            className="rounded-md bg-emerald-600 px-4 py-2.5 font-semibold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {isLoading ? "Getting airline link…" : "Approve this flight"}
          </button>
          <button
            type="button"
            onClick={handleCancel}
            disabled={isLoading}
            className="rounded-md border border-slate-300 px-4 py-2.5 font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      )}

      {bookingLog?.state === "CONFIRMED" && (
        <div className="mt-4 flex gap-3">
          <button
            type="button"
            onClick={handleExecute}
            disabled={isLoading}
            className="rounded-md bg-indigo-600 px-4 py-2.5 font-semibold text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {isLoading ? "Getting airline link…" : "Continue to airline"}
          </button>
          <button
            type="button"
            onClick={handleCancel}
            disabled={isLoading}
            className="rounded-md border border-slate-300 px-4 py-2.5 font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      )}

      {searchAgainText && (
        <div className="mt-4 space-y-3">
          <p className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-700">{searchAgainText}</p>
          <button
            type="button"
            onClick={onSearchAgain}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-700"
          >
            Search again
          </button>
        </div>
      )}

      {bookingLog?.state === "CANCELLED" && (
        <div className="mt-4 space-y-3">
          <p className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-700">
            This booking was cancelled.
          </p>
          <button
            type="button"
            onClick={handleRequest}
            disabled={isLoading}
            className="rounded-md bg-indigo-600 px-4 py-2.5 font-semibold text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {isLoading ? "Reviewing…" : "Review this flight again"}
          </button>
        </div>
      )}

      {bookingLog?.state === "EXECUTED" && (
        <div className="mt-5 space-y-3">
          {bookingOptions.length > 0 ? (
            <ul className="space-y-1">
              {bookingOptions.map((option, index) => {
                const bookingRequest = option.booking_request as Record<string, string>
                const label = bookingOptionLabel(option) ?? selectedOffer.carrier
                return (
                  <li key={index}>
                    <form method="POST" action={bookingRequest.url} target="_blank" className="inline">
                      {bookingFormFields(bookingRequest.post_data).map(([name, value]) => (
                        <input key={name} type="hidden" name={name} value={value} />
                      ))}
                      <button
                        type="submit"
                        className="rounded-md bg-indigo-600 px-3.5 py-2 text-sm font-semibold text-white transition hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600"
                      >
                        Continue with {label}
                      </button>
                    </form>
                  </li>
                )
              })}
            </ul>
          ) : (
            <p className="text-sm text-slate-600">
              No airline booking link is available for this offer.
            </p>
          )}
          <p className="text-sm text-slate-600">
            Your flight hasn&apos;t been purchased. Complete your booking on the airline site.
          </p>
        </div>
      )}
    </section>
  )
}
