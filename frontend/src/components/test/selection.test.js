import "../../test/setup.js"
import { deferred, response } from "../../test/setup.js"
import assert from "node:assert/strict"
import test from "node:test"
import { createElement } from "react"
import { render, act, fireEvent } from "@testing-library/react"
const { CompliancePanel } = await import("../CompliancePanel.tsx")

test("switching assessments discards outstanding evidence responses", async (context) => {
  const pending = deferred()
  const assessment = (id) => ({ id, assessed_at: "2026-10-29", window_start: "2026-10-28", window_end: "2026-10-29", evaluator_version: "1", results: [{ control: "privileged_mfa", status: "pass", summary: "checked", evidence_event_ids: [id], details: {} }] })
  context.mock.method(globalThis, "fetch", async (url) => url.endsWith("/monitoring") ? response({ stale: false, assessments: [assessment(1), assessment(2)] }) : pending.promise)
  const view = render(createElement(CompliancePanel))
  await act(async () => {})
  fireEvent.click(view.getByText("Event #1"))
  fireEvent.change(view.getByRole("combobox"), { target: { value: "2" } })
  await act(async () => pending.resolve(response({ marker: "obsolete evidence" })))
  assert.doesNotMatch(view.container.textContent, /obsolete evidence/)
})
