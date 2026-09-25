import { useMemo, useState } from "react"
import { getTripExecution } from "../api/client"
import { usePolledResource } from "../hooks/usePolledResource"
import { RunStatusDot } from "./RunStatusDot"

interface LiveActivityProps {
  tripId: number
  isRunActive: boolean
}

const STEP_LABELS: Record<string, string> = {
  search_flights: "Searching flights",
  web_search: "Researching activities",
}

function labelFor(name: string): string {
  return STEP_LABELS[name] ?? name
}

// Always polling once a trip exists (not just while a run is active) so this reads as a live
// activity log, not a banner that vanishes between runs.
export function LiveActivity({ tripId, isRunActive }: LiveActivityProps) {
  const fetcher = useMemo(() => () => getTripExecution(tripId), [tripId])
  const { data: panelData } = usePolledResource({
    fetcher,
    isRunActive,
    errorText: "Could not load execution data.",
  })
  const [clearedBeforeSeq, setClearedBeforeSeq] = useState(0)

  const events = (panelData?.events ?? []).filter((event) => event.seq > clearedBeforeSeq)
  const recent = [...events].reverse()
  // events is backend-ordered ascending by seq (trips_repository.py's ORDER BY seq), so the
  // last element already holds the max — no scan needed.
  const maxSeq = panelData?.events.at(-1)?.seq ?? 0

  return (
    <section
      aria-live="polite"
      className="space-y-3 rounded-xl border border-slate-200 bg-white p-6 shadow-sm"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <RunStatusDot isActive={isRunActive} />
          <h2 className="text-lg font-semibold text-slate-900">
            {isRunActive ? "Agent working…" : "Agent activity"}
          </h2>
        </div>
        {recent.length > 0 && (
          <button
            type="button"
            onClick={() => setClearedBeforeSeq(maxSeq)}
            className="text-xs font-medium text-slate-400 transition hover:text-slate-600"
          >
            Clear
          </button>
        )}
      </div>

      {recent.length === 0 ? (
        <p className="text-sm text-slate-500">
          Nothing yet — you'll see what the agent is doing here once it starts working.
        </p>
      ) : (
        <ol className="max-h-72 space-y-1.5 overflow-y-auto">
          {recent.map((event) => (
            <li key={event.seq} className="rounded-md px-2 py-1.5 text-sm text-slate-700 hover:bg-slate-50">
              <div className="flex items-center gap-2">
                <span className="text-indigo-400">→</span>
                <span className="font-medium text-slate-900">{labelFor(event.name)}</span>
                <span className="text-xs text-slate-400">{event.status}</span>
              </div>
              <p className="pl-5 text-xs text-slate-500">{event.detail}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
