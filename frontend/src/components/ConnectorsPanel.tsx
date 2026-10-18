import { useEffect, useState } from "react"
import { ApiError, getConnectors, setSlackConnectorEnabled } from "../api/client"
import type { ConnectorsOut } from "../api/types"

export function ConnectorsPanel() {
  const [connectors, setConnectors] = useState<ConnectorsOut | null>(null)
  const [isToggling, setIsToggling] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  useEffect(() => {
    getConnectors()
      .then(setConnectors)
      .catch(() => setErrorMessage("Could not load connector status."))
  }, [])

  const handleToggle = async () => {
    if (!connectors) return
    const nextEnabled = !connectors.slack.enabled
    setIsToggling(true)
    setErrorMessage(null)
    try {
      setConnectors(await setSlackConnectorEnabled(nextEnabled))
    } catch (error) {
      setErrorMessage(error instanceof ApiError ? error.message : "Could not update the connector.")
    } finally {
      setIsToggling(false)
    }
  }

  if (!connectors) {
    return (
      <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-500">
        Loading connectors…
      </p>
    )
  }

  const { configured, enabled } = connectors.slack

  return (
    <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg font-semibold text-slate-900">Connectors</h2>

      <div className="flex items-center justify-between rounded-lg border border-slate-200 p-4">
        <div>
          <p className="font-medium text-slate-900">Slack</p>
          <p className="mt-1 text-sm text-slate-500">
            Post a Confirm/Reject message to Slack when a booking needs approval.
          </p>
          {!configured && (
            <p className="mt-1 text-sm text-amber-600">
              Slack approvals are unavailable on this server.
            </p>
          )}
          {errorMessage && <p className="mt-1 text-sm text-red-600">{errorMessage}</p>}
        </div>
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-600">{enabled ? "Enabled" : "Disabled"}</span>
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            aria-label="Toggle the Slack connector"
            onClick={handleToggle}
            disabled={!configured || isToggling}
            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              enabled ? "border-emerald-600 bg-emerald-500" : "border-slate-300 bg-slate-200"
            }`}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                enabled ? "translate-x-6" : "translate-x-1"
              }`}
            />
          </button>
        </div>
      </div>
    </section>
  )
}
