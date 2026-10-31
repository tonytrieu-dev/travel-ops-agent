import "../../test/setup.js"
import { session, response } from "../../test/setup.js"
import assert from "node:assert/strict"
import test from "node:test"
import { createElement } from "react"
import { render, act } from "@testing-library/react"
const { ConnectorsPanel } = await import("../ConnectorsPanel.tsx")
const { BookingModule } = await import("../BookingModule.tsx")

for (const failure of ["forbidden", "network"]) {
  test(`connector initial ${failure} failure displays an error`, async (context) => {
    await session(`connector-${failure}`)
    context.mock.method(globalThis, "fetch", async () => {
      if (failure === "network") throw new TypeError("Network unavailable")
      return response({ detail: "operator required" }, 403)
    })
    const view = render(createElement(ConnectorsPanel))
    await act(async () => {})
    assert.match(view.getByRole("alert").textContent, /Could not load connector status/)
    assert.doesNotMatch(view.container.textContent, /Loading connectors/)
  })
}
test("forbidden traveler connector navigation requests settings at most once", async (context) => {
  await session("navigation-session")
  let requests = 0
  context.mock.method(globalThis, "fetch", async () => { requests++; return response({ detail: "operator required" }, 403) })
  for (let visit = 0; visit < 6; visit++) {
    const view = render(createElement(ConnectorsPanel))
    await act(async () => {})
    assert.ok(view.getByRole("alert"))
    view.unmount()
  }
  assert.equal(requests, 1)
})
test("enforced traveler booking remounts never request privileged connector settings", async (context) => {
  const paths = []
  context.mock.method(globalThis, "fetch", async (url) => {
    paths.push(new URL(url).pathname)
    return response(url.endsWith("/auth/config") ? { enforced: true } : { detail: "operator required" }, url.endsWith("/auth/config") ? 200 : 403)
  })
  for (let selection = 0; selection < 5; selection++) {
    const view = render(createElement(BookingModule, { trip: { id: 1 }, selectedOffer: { id: selection, carrier: "Test Air", price_usd: 100, currency: "USD" } }))
    await act(async () => {})
    view.unmount()
  }
  assert.equal(paths.filter((path) => path === "/api/connectors").length, 0)
})
