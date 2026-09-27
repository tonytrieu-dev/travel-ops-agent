import { useMemo, useState } from "react"
import { getAllExecution } from "../api/client"
import type { AgentRunOut, AgentRunStepOut, TripRequestOut } from "../api/types"
import { usePolledResource } from "../hooks/usePolledResource"
import { FilterSelect } from "./FilterSelect"

interface ExecutionPanelProps {
  trips: TripRequestOut[]
  isRunActive: boolean
}

const NO_RUNS: AgentRunOut[] = []

function tripLabel(trip: TripRequestOut): string {
  return `${trip.origin} → ${trip.destination_airport}`
}

function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  })
}

// input_summary/output_summary are json.dumps'd server-side with no indent; pretty-print when
// they parse as JSON, otherwise show the raw string (e.g. plain-text model output) as-is.
function formatSummary(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

const STATUS_LABELS: Record<string, string> = {
  ok: "Successful",
  completed: "Completed",
  running: "Running",
  failed: "Failed",
  no_result: "No result",
  unavailable: "Unavailable",
  rejected: "Rejected — retried",
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

function statusStyles(status: string): string {
  if (status === "ok" || status === "completed") return "bg-emerald-100 text-emerald-700"
  if (status === "failed") return "bg-red-100 text-red-700"
  if (status === "no_result" || status === "unavailable" || status === "rejected")
    return "bg-amber-100 text-amber-700"
  return "bg-slate-100 text-slate-700"
}

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-slate-50 p-3">
      <p className="text-xs uppercase tracking-wide text-slate-400">{label}</p>
      <p className="font-medium text-slate-900">{value}</p>
    </div>
  )
}

function RunStepSection({ title, steps }: { title: string; steps: AgentRunStepOut[] }) {
  return (
    <div>
      <h4 className="text-sm font-semibold text-slate-900">
        {title} <span className="font-normal text-slate-400">({steps.length})</span>
      </h4>
      {steps.length === 0 && (
        <p className="mt-2 text-sm text-slate-500">No {title.toLowerCase()} recorded.</p>
      )}
      <ol className="mt-2 space-y-2">
        {steps.map((step) => (
          <li key={step.seq} className="rounded-lg border border-slate-200 bg-white p-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <span className="text-xs text-slate-400">{step.seq}.</span>
                <span className="font-medium text-slate-900">{step.name}</span>
              </span>
              <span
                className={`rounded px-1.5 py-0.5 text-xs font-medium ${statusStyles(step.status)}`}
              >
                {statusLabel(step.status)}
              </span>
            </div>
            {(step.duration_ms != null || (step.kind === "model" && step.tokens != null)) && (
              <p className="mt-1 text-xs text-slate-500">
                {step.duration_ms != null && `${step.duration_ms} ms`}
                {step.duration_ms != null && step.kind === "model" && step.tokens != null && " · "}
                {step.kind === "model" && step.tokens != null && `${step.tokens} tokens`}
              </p>
            )}
            {step.input_summary && (
              <div className="mt-2">
                <p className="text-xs font-medium text-slate-500">Input</p>
                <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-2 text-xs whitespace-pre-wrap text-slate-600">
                  {formatSummary(step.input_summary)}
                </pre>
              </div>
            )}
            {step.output_summary && (
              <div className="mt-2">
                <p className="text-xs font-medium text-slate-500">Result</p>
                <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-2 text-xs whitespace-pre-wrap text-slate-600">
                  {formatSummary(step.output_summary)}
                </pre>
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function AgentRunCard({ run, tripLabel }: { run: AgentRunOut; tripLabel: string }) {
  const modelSteps = run.steps.filter((step) => step.kind === "model")
  const toolSteps = run.steps.filter((step) => step.kind === "tool")
  const outputSteps = run.steps.filter((step) => step.kind === "output")

  return (
    <div className="space-y-5 rounded-xl border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <p className="font-semibold text-slate-900">Run #{run.id}</p>
            <span className="rounded bg-white px-1.5 py-0.5 text-xs font-medium text-slate-500 ring-1 ring-slate-200 ring-inset">
              {tripLabel}
            </span>
          </div>
          <p className="text-sm text-slate-600">{formatTimestamp(run.started_at)}</p>
        </div>
        <span
          className={`rounded-full px-2.5 py-1 text-xs font-medium ${statusStyles(run.status)}`}
        >
          {statusLabel(run.status)}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <MetricTile label={modelSteps.length > 0 ? "Model" : "Provider"} value={run.model} />
        <MetricTile label="Tool calls" value={String(toolSteps.length)} />
        <MetricTile label="Model calls" value={String(modelSteps.length)} />
        <MetricTile label="Latency" value={`${run.total_ms} ms`} />
        <MetricTile
          label="Tokens in / out"
          value={`${run.total_input_tokens} / ${run.total_output_tokens}`}
        />
        <MetricTile
          label="Context used"
          value={run.budget_utilization_pct != null ? `${run.budget_utilization_pct.toFixed(1)}%` : "—"}
        />
        <MetricTile
          label="Estimated cost"
          value={run.estimated_cost_usd != null ? `$${run.estimated_cost_usd.toFixed(4)}` : "—"}
        />
        <MetricTile label="Steps" value={String(run.steps.length)} />
      </div>

      <div className="space-y-5">
        <RunStepSection title="Model calls" steps={modelSteps} />
        <RunStepSection title="Tool calls" steps={toolSteps} />
        <RunStepSection title="Final output" steps={outputSteps} />
      </div>
    </div>
  )
}

export function ExecutionPanel({ trips, isRunActive }: ExecutionPanelProps) {
  const fetcher = useMemo(() => async () => (await getAllExecution()).agent_runs, [])
  const { data, errorMessage } = usePolledResource<AgentRunOut[]>({
    fetcher,
    isRunActive,
    errorText: "Could not load execution data.",
  })
  const allRuns = data ?? NO_RUNS
  const [routeSearch, setRouteSearch] = useState("")
  const [statusFilter, setStatusFilter] = useState("all")

  const tripsById = useMemo(() => new Map(trips.map((trip) => [trip.id, trip])), [trips])
  const runTripLabel = (run: AgentRunOut) => {
    const trip = tripsById.get(run.trip_request_id)
    return trip ? tripLabel(trip) : `Trip #${run.trip_request_id}`
  }
  // The set of distinct statuses that have actually occurred, not a hardcoded enum — stays
  // correct if a new status value ever shows up without needing a matching code change here.
  const statusOptions = useMemo(
    () => [...new Set(allRuns.map((run) => run.status))].sort(),
    [allRuns],
  )
  const trimmedRouteSearch = routeSearch.trim().toLowerCase()
  // A dropdown listing every trip doesn't scale past a handful of entries — a text filter over
  // the (already-visible) route label does, regardless of how many trips exist.
  const runs = allRuns.filter(
    (run) =>
      (statusFilter === "all" || run.status === statusFilter) &&
      (!trimmedRouteSearch || runTripLabel(run).toLowerCase().includes(trimmedRouteSearch)),
  )
  const isFiltered = statusFilter !== "all" || trimmedRouteSearch.length > 0

  return (
    <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-slate-900">Agent execution history</h2>
        {trips.length > 0 && (
          <div className="flex items-center gap-2">
            <input
              type="search"
              value={routeSearch}
              onChange={(event) => setRouteSearch(event.target.value)}
              placeholder="Filter by route (e.g. JFK)"
              aria-label="Filter by route"
              className="w-64 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-600 placeholder:text-slate-400"
            />
            {statusOptions.length > 0 && (
              <FilterSelect
                value={statusFilter}
                onChange={setStatusFilter}
                ariaLabel="Filter by status"
                options={[
                  { value: "all", label: "All statuses" },
                  ...statusOptions.map((status) => ({ value: status, label: statusLabel(status) })),
                ]}
              />
            )}
          </div>
        )}
      </div>
      <p className="-mt-2 text-sm text-slate-500">
        Every agent run across every trip, newest first. Model usage and external tool activity
        are reported inline within each run.
      </p>

      {errorMessage && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {errorMessage}
        </p>
      )}

      {runs.length === 0 && (
        <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-500">
          {isFiltered
            ? "No runs match this filter."
            : "No activity yet. Search for flights or generate an itinerary to see execution history."}
        </p>
      )}

      {runs.map((run) => (
        <AgentRunCard key={run.id} run={run} tripLabel={runTripLabel(run)} />
      ))}
    </section>
  )
}
