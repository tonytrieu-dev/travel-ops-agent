import { useMemo, useState } from "react"
import { listBookings } from "../api/client"
import type {
  BookingLogOut,
  BookingState,
  BookingTransitionOut,
  TripRequestOut,
} from "../api/types"
import { usePolledResource } from "../hooks/usePolledResource"
import { FilterSelect } from "./FilterSelect"

interface ApprovalHistoryPanelProps {
  trips: TripRequestOut[]
  isRunActive: boolean
}

const STATE_LABELS: Record<BookingState, string> = {
  PENDING_USER_CONFIRMATION: "Awaiting human approval",
  CONFIRMED: "Approved",
  EXECUTED: "Handed off to airline",
  CANCELLED: "Cancelled",
  EXPIRED: "Expired",
}

function stateStyles(state: BookingState): string {
  if (state === "CONFIRMED" || state === "EXECUTED") return "bg-emerald-100 text-emerald-700"
  if (state === "PENDING_USER_CONFIRMATION") return "bg-amber-100 text-amber-700"
  return "bg-slate-100 text-slate-600"
}

/** Names the acting principal for an audit reader. A transition with no actor is the system
 * expiring a stale fare rather than a person deciding — the distinction the trail exists for.
 * The email is resolved server-side; falling back to the id keeps an anonymized user's decision
 * attributable rather than silently blank. */
function actorLabel(transition: BookingTransitionOut): string {
  if (transition.actor_user_id == null) return "System (automatic)"
  return transition.actor_email ?? `Account #${transition.actor_user_id}`
}

function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  })
}

function ApprovalTrail({ booking, routeLabel }: { booking: BookingLogOut; routeLabel: string }) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="font-semibold text-slate-900">Booking #{booking.id}</h3>
            <span className="rounded bg-white px-1.5 py-0.5 text-xs font-medium text-slate-500 ring-1 ring-slate-200 ring-inset">
              {routeLabel}
            </span>
          </div>
          <p className="mt-0.5 text-sm text-slate-600">
            Requested {formatTimestamp(booking.created_at)}
          </p>
        </div>
        <span
          className={`rounded-full px-2.5 py-1 text-xs font-medium ${stateStyles(booking.state)}`}
        >
          {STATE_LABELS[booking.state]}
        </span>
      </div>

      {booking.transitions.length === 0 ? (
        <p className="px-4 py-4 text-sm text-slate-500">
          Awaiting a human decision — no approval has been recorded yet.
        </p>
      ) : (
        <ol className="px-4 py-4">
          {booking.transitions.map((transition, index) => {
            const isLast = index === booking.transitions.length - 1
            return (
              <li key={`${transition.created_at}-${transition.to_state}`} className="flex gap-3">
                {/* Dot-and-rail timeline: the rail is omitted on the last row so it stops at the
                    final decision instead of trailing into whitespace. */}
                <div className="flex flex-col items-center pt-1">
                  <span
                    aria-hidden="true"
                    className="h-2 w-2 shrink-0 rounded-full bg-indigo-500 ring-4 ring-indigo-50"
                  />
                  {!isLast && <span aria-hidden="true" className="mt-1 w-px flex-1 bg-slate-200" />}
                </div>
                <div className={isLast ? "min-w-0" : "min-w-0 pb-5"}>
                  <p className="text-sm font-medium text-slate-900">
                    {STATE_LABELS[transition.from_state]}{" "}
                    <span className="text-slate-400">→</span>{" "}
                    {STATE_LABELS[transition.to_state]}
                  </p>
                  <p className="mt-0.5 text-sm">
                    <span className="text-slate-600">Actor:</span>{" "}
                    <span className="font-medium text-slate-900">{actorLabel(transition)}</span>
                  </p>
                  <p className="mt-0.5 text-sm text-slate-600">
                    {formatTimestamp(transition.created_at)}
                  </p>
                </div>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

export function ApprovalHistoryPanel({ trips, isRunActive }: ApprovalHistoryPanelProps) {
  const [stateFilter, setStateFilter] = useState<string>("all")
  const { data, errorMessage } = usePolledResource<BookingLogOut[]>({
    fetcher: listBookings,
    isRunActive,
    resourceKey: "approval-history",
    errorText: "Could not load approval history.",
  })
  // Memoized so the `?? []` fallback doesn't hand useMemo a fresh array on every render.
  const bookings = useMemo(() => data ?? [], [data])

  const routeLabels = useMemo(
    () => new Map(trips.map((trip) => [trip.id, `${trip.origin} → ${trip.destination_airport}`])),
    [trips],
  )

  const stateOptions = useMemo(
    () => [
      { value: "all", label: "All states" },
      ...[...new Set(bookings.map((booking) => booking.state))].sort().map((state) => ({
        value: state,
        label: STATE_LABELS[state],
      })),
    ],
    [bookings],
  )

  const visibleBookings = bookings.filter(
    (booking) => stateFilter === "all" || booking.state === stateFilter,
  )

  if (errorMessage) {
    return (
      <p role="alert" className="rounded-xl bg-red-50 p-6 text-sm text-red-700">
        {errorMessage}
      </p>
    )
  }

  if (bookings.length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-500">
        No approvals yet — request a booking from a trip to see its human-approval trail here.
      </p>
    )
  }

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">Approval history</h2>
          <p className="mt-0.5 text-sm text-slate-500">
            Every human-in-the-loop decision on a booking, from the append-only audit trail.
          </p>
        </div>
        <FilterSelect
          value={stateFilter}
          onChange={setStateFilter}
          ariaLabel="Filter by booking state"
          options={stateOptions}
        />
      </div>

      {visibleBookings.length === 0 ? (
        <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-500">
          No bookings in this state.
        </p>
      ) : (
        <div className="space-y-3">
          {visibleBookings.map((booking) => (
            <ApprovalTrail
              key={booking.id}
              booking={booking}
              routeLabel={
                routeLabels.get(booking.trip_request_id) ?? `Trip #${booking.trip_request_id}`
              }
            />
          ))}
        </div>
      )}
    </section>
  )
}
