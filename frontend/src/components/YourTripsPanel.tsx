import { useMemo, useState } from "react"
import type { TripRequestOut, TripStatus } from "../api/types"
import { FilterSelect } from "./FilterSelect"

interface YourTripsPanelProps {
  trips: TripRequestOut[]
  selectedTripId: number | null
  onSelectTrip: (tripId: number) => void
}

const STATUS_LABELS: Record<TripStatus, string> = {
  created: "Created",
  flights_searched: "Flights searched",
  itinerary_ready: "Itinerary ready",
}

type DateRangeFilter = "30d" | "3m" | "6m" | "1y" | "all"

const DATE_RANGE_OPTIONS: { value: DateRangeFilter; label: string }[] = [
  { value: "30d", label: "Last 30 days" },
  { value: "3m", label: "Last 3 months" },
  { value: "6m", label: "Last 6 months" },
  { value: "1y", label: "Last year" },
  { value: "all", label: "All time" },
]

const DATE_RANGE_DAYS: Record<Exclude<DateRangeFilter, "all">, number> = {
  "30d": 30,
  "3m": 90,
  "6m": 180,
  "1y": 365,
}

function isWithinRange(createdAt: string, range: DateRangeFilter): boolean {
  if (range === "all") return true
  const cutoff = Date.now() - DATE_RANGE_DAYS[range] * 24 * 60 * 60 * 1000
  return new Date(createdAt).getTime() >= cutoff
}

function formatDate(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleDateString() : "—"
}

// new Date("YYYY-MM-DD") is UTC midnight, which renders the day before in negative-offset zones;
// parse the parts as a local calendar date instead.
function formatDateOnly(value: string | null | undefined): string {
  if (!value) return "—"
  const [year, month, day] = value.split("-").map(Number)
  return new Date(year, month - 1, day).toLocaleDateString()
}

export function YourTripsPanel({ trips, selectedTripId, onSelectTrip }: YourTripsPanelProps) {
  const [dateRange, setDateRange] = useState<DateRangeFilter>("all")

  const filteredTrips = useMemo(
    () => trips.filter((trip) => isWithinRange(trip.created_at, dateRange)),
    [trips, dateRange],
  )

  if (trips.length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-500">
        No trips yet — create one from "Plan a trip" to see it here.
      </p>
    )
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-slate-900">Your trips</h2>
        <FilterSelect
          value={dateRange}
          onChange={(value) => setDateRange(value as DateRangeFilter)}
          ariaLabel="Filter by date range"
          options={DATE_RANGE_OPTIONS}
        />
      </div>

      {filteredTrips.length === 0 ? (
        <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-500">
          No trips in this date range.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400">
                <th className="px-4 py-3 font-medium">Route</th>
                <th className="px-4 py-3 font-medium">Depart</th>
                <th className="px-4 py-3 font-medium">Return</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {filteredTrips.map((trip) => {
                const isSelected = trip.id === selectedTripId
                return (
                  <tr
                    key={trip.id}
                    onClick={() => onSelectTrip(trip.id)}
                    aria-current={isSelected ? "true" : undefined}
                    className={`cursor-pointer border-b border-slate-100 last:border-0 ${
                      isSelected ? "bg-indigo-50" : "hover:bg-slate-50"
                    }`}
                  >
                    <td
                      className={`px-4 py-3 font-medium ${isSelected ? "text-indigo-700" : "text-slate-900"}`}
                    >
                      {trip.origin} → {trip.destination_airport}
                    </td>
                    <td className="px-4 py-3 text-slate-600">{formatDateOnly(trip.depart_date)}</td>
                    <td className="px-4 py-3 text-slate-600">{formatDateOnly(trip.return_date)}</td>
                    <td className="px-4 py-3 text-slate-600">{STATUS_LABELS[trip.status]}</td>
                    <td className="px-4 py-3 text-slate-500">{formatDate(trip.created_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
