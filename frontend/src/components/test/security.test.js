import "../../test/setup.js"
import { response } from "../../test/setup.js"
import assert from "node:assert/strict"
import test from "node:test"
import { createElement } from "react"
import { render, act, fireEvent } from "@testing-library/react"
const { BookingModule } = await import("../BookingModule.tsx")

// Sole guard of this PR's BookingModule change: in-app approve/cancel stays available even
// when Slack approvals are enabled, so a failed Slack delivery cannot block the user. No other
// test covers this path.
for (const action of ["Approve this flight", "Cancel"]) {
  test(`enabled Slack keeps in-app ${action} available when delivery fails`, async (context) => {
    const paths = []
    const booking = { id: 1, state: "PENDING_USER_CONFIRMATION" }
    context.mock.method(globalThis, "fetch", async (url) => {
      paths.push(new URL(url).pathname)
      if (url.endsWith("/auth/config")) return response({ enforced: false })
      if (url.endsWith("/connectors")) return response({ slack: { configured: true, enabled: true } })
      if (url.endsWith("/confirm")) return response({ ...booking, state: "CONFIRMED" })
      if (url.endsWith("/execute")) return response({ ...booking, state: "EXECUTED" })
      if (url.endsWith("/cancel")) return response({ ...booking, state: "CANCELLED" })
      return response(booking)
    })
    const view = render(createElement(BookingModule, {
      trip: { id: 1 }, selectedOffer: { id: 1, carrier: "Test Air", price_usd: 100, currency: "USD" },
    }))
    await act(async () => {})
    await act(async () => fireEvent.click(view.getByRole("button", { name: "Review booking" })))
    await act(async () => fireEvent.click(view.getByRole("button", { name: action })))
    assert.ok(paths.includes(`/api/bookings/1/${action === "Cancel" ? "cancel" : "execute"}`))
  })
}
