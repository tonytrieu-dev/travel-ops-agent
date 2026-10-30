import "../../test/setup.js"
import { response } from "../../test/setup.js"
import assert from "node:assert/strict"
import test from "node:test"
import { createElement } from "react"
import { render, act, fireEvent } from "@testing-library/react"
const { LiveActivity } = await import("../LiveActivity.tsx")

const panel = (...seqs) => response({
  events: seqs.map((seq) => ({ seq, name: "web_search", status: "ok", detail: `event ${seq}` })),
})

test("clearing one trip's activity does not hide another trip's events", async (context) => {
  context.mock.method(globalThis, "fetch", async (url) =>
    new URL(url).pathname.includes("/trips/1/") ? panel(19, 20) : panel(1, 2),
  )
  const view = render(createElement(LiveActivity, { tripId: 1, isRunActive: false }))
  await act(async () => {})
  fireEvent.click(view.getByText("Clear"))
  assert.doesNotMatch(view.container.textContent, /event 20/, "trip 1 events cleared")

  await act(async () => view.rerender(createElement(LiveActivity, { tripId: 2, isRunActive: false })))
  assert.match(view.container.textContent, /event 2/, "trip 2 events must show despite trip 1's cutoff")
})
