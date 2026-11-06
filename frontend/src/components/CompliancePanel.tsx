import { useRef, useState } from "react"
import { ApiError, getComplianceMonitoring, getSecurityEvent } from "../api/client"
import { usePolledResource } from "../hooks/usePolledResource"

const CONTROL_NAMES: Record<string, string> = {
  privileged_mfa: "MFA on privileged actions",
  audit_protections: "Database audit protections",
  session_containment: "Automatic session containment",
}

const STATUS_NAMES = {
  pass: "Pass",
  fail: "Fail",
  insufficient_evidence: "Insufficient evidence",
}

export function CompliancePanel() {
  const { data, errorMessage } = usePolledResource({
    fetcher: getComplianceMonitoring,
    isRunActive: false,
    idlePollMs: 60_000,
    resourceKey: "compliance-monitoring",
    errorText: "Could not load monitoring. A security operator or administrator with MFA is required.",
  })
  const [assessmentId, setAssessmentId] = useState<number | null>(null)
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null)
  const [evidenceError, setEvidenceError] = useState<string | null>(null)
  const assessment = data?.assessments.find((item) => item.id === assessmentId) ?? data?.assessments[0]
  // A slow evidence fetch must not land under an assessment the user has since switched away from.
  const latestEvidenceRequest = useRef(0)

  const invalidateEvidence = () => {
    latestEvidenceRequest.current += 1
    setEvidence(null)
    setEvidenceError(null)
  }

  const inspectEvidence = async (eventId: number) => {
    invalidateEvidence()
    const requestId = latestEvidenceRequest.current
    try {
      const loaded = await getSecurityEvent(eventId)
      if (requestId !== latestEvidenceRequest.current) return
      setEvidence(loaded)
    } catch (error) {
      if (requestId !== latestEvidenceRequest.current) return
      setEvidenceError(error instanceof ApiError ? error.message : "Could not load evidence.")
    }
  }

  return (
    <section className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg font-semibold">Security-control monitoring</h2>
      <p className="text-sm text-slate-600">
        Three scoped checks supporting SOC 2 security criteria mapping. This is not a compliance certification.
        Assessments run every minute over the previous 24 hours while the server is running.
      </p>
      {errorMessage && <p role="alert" className="text-sm text-red-700">{errorMessage} Previous results may be outdated.</p>}
      {data?.stale && <p role="status" className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">Monitoring is stale or has not completed. Do not treat previous results as current.</p>}
      {!data && !errorMessage && <p role="status">Loading assessments…</p>}
      {data && !assessment && <p>No assessments recorded yet.</p>}
      {assessment && (
        <>
          <label className="block text-sm font-medium">Assessment history
            <select className="mt-1 block w-full rounded border border-slate-300 p-2" value={assessment.id} onChange={(event) => { setAssessmentId(Number(event.target.value)); invalidateEvidence() }}>
              {data?.assessments.map((item) => <option key={item.id} value={item.id}>{new Date(item.assessed_at).toLocaleString()} · #{item.id}</option>)}
            </select>
          </label>
          <p className="text-sm text-slate-500">
            Window: {new Date(assessment.window_start).toLocaleString()} – {new Date(assessment.window_end).toLocaleString()}.
            Evaluator version {assessment.evaluator_version}. Audit protection checks describe configuration at assessment time.
          </p>
          {assessment.results.map((result) => (
            <article key={result.control} className="space-y-3 rounded-lg border border-slate-200 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="font-medium">{CONTROL_NAMES[result.control] ?? result.control}</h3>
                <span className={`rounded px-2 py-1 text-sm font-semibold ${result.status === "pass" ? "bg-emerald-50 text-emerald-800" : result.status === "fail" ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-900"}`}>{STATUS_NAMES[result.status]}</span>
              </div>
              <p className="text-sm text-slate-600">{result.summary}</p>
              <details className="text-sm">
                <summary className="cursor-pointer font-medium">Evidence and findings ({result.evidence_event_ids.length} events)</summary>
                <div className="my-3 flex flex-wrap gap-2">
                  {result.evidence_event_ids.map((identifier) => <button type="button" key={identifier} onClick={() => inspectEvidence(identifier)} className="rounded border border-slate-300 px-2 py-1 text-indigo-700">Event #{identifier}</button>)}
                </div>
                <pre className="overflow-auto rounded bg-slate-50 p-3 text-xs">{JSON.stringify(result.details, null, 2)}</pre>
              </details>
            </article>
          ))}
        </>
      )}
      {evidenceError && <p role="alert" className="text-sm text-red-700">{evidenceError}</p>}
      {evidence && <div aria-live="polite"><h3 className="mb-2 font-medium">Selected evidence</h3><pre className="overflow-auto rounded bg-slate-50 p-4 text-xs">{JSON.stringify(evidence, null, 2)}</pre></div>}
    </section>
  )
}
