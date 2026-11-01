import "./test/setup.js"
import { deferred, response } from "./test/setup.js"
import assert from "node:assert/strict"
import test from "node:test"
import { createElement } from "react"
import { render, act, fireEvent } from "@testing-library/react"
const { LoginPanel, default: App } = await import("./App.tsx")

// F8: an expired/failed MFA challenge must be recoverable without a page reload.
test("MFA restart returns to the email/password step", async (context) => {
  context.mock.method(globalThis, "fetch", async (url, init) => {
    if (url.endsWith("/auth/browser/login")) return response({ mfa_required: true, session_id: "s1", access_token: "", expires_at: "" })
    if (url.endsWith("/auth/browser/mfa")) return response({ code: "forbidden", detail: "challenge expired" }, 403)
    throw new Error(`unexpected ${init?.method} ${url}`)
  })
  const view = render(createElement(LoginPanel, { onAuthenticated: () => {} }))
  fireEvent.change(view.getByLabelText("Password"), { target: { value: "demo-password" } })
  await act(async () => fireEvent.submit(view.getByText("Continue to MFA").closest("form")))
  assert.ok(view.getByLabelText("MFA code"), "reached the MFA step")
  await act(async () => fireEvent.submit(view.getByText("Verify and sign in").closest("form")))
  assert.match(view.getByRole("alert").textContent, /challenge expired/)

  fireEvent.click(view.getByText("Start over"))
  assert.ok(view.getByLabelText("Email"), "restart returns to the email/password step")
  assert.equal(view.queryByLabelText("MFA code"), null)
})

// F4: a plan that resolves for a previously selected trip must not overwrite the trip the user
// has since selected.
test("a stale plan response does not overwrite the newly selected trip", async (context) => {
  const slowPlan = deferred()
  context.mock.method(globalThis, "fetch", async (url, init) => {
    const { pathname } = new URL(url, "http://localhost")
    if (pathname.endsWith("/auth/config")) return response({ enforced: false })
    if (pathname === "/api/trips" && init?.method !== "POST")
      return response([
        { id: 1, origin: "JFK", destination: "Tokyo", destination_airport: "HND", depart_date: "2030-08-01", return_date: null, age: 30, fitness_level: "high", status: "created", created_at: "2030-01-02T00:00:00Z" },
        { id: 2, origin: "JFK", destination: "London", destination_airport: "LHR", depart_date: "2030-08-01", return_date: null, age: 30, fitness_level: "high", status: "created", created_at: "2030-01-01T00:00:00Z" },
      ])
    if (pathname === "/api/trips/1/snapshot")
      return response({ trip: { id: 1, origin: "JFK", destination: "Tokyo", destination_airport: "HND", depart_date: "2030-08-01", return_date: null, age: 30, fitness_level: "high", status: "created", created_at: "2030-01-02T00:00:00Z" }, flight_search: null, plan: null })
    if (pathname === "/api/trips/2/snapshot")
      return response({ trip: { id: 2, origin: "JFK", destination: "London", destination_airport: "LHR", depart_date: "2030-08-01", return_date: null, age: 30, fitness_level: "high", status: "created", created_at: "2030-01-01T00:00:00Z" }, flight_search: null, plan: null })
    if (pathname === "/api/trips/1/plan") return slowPlan.promise
    throw new Error(`unexpected ${init?.method ?? "GET"} ${pathname}`)
  })

  const view = render(createElement(App))
  await act(async () => {})
  await act(async () => {})

  // Select trip 1 (Tokyo/HND), start planning it (plan stays in flight).
  fireEvent.click(view.getByText("Your trips"))
  await act(async () => fireEvent.click(view.getByText(/HND/)))
  await act(async () => fireEvent.click(view.getByText("Plan itinerary")))

  // Switch to trip 2 (London/LHR) while trip 1's plan is still pending.
  fireEvent.click(view.getByText("Your trips"))
  await act(async () => fireEvent.click(view.getByText(/LHR/)))
  assert.match(view.container.textContent, /LHR/)
  assert.equal(view.getByRole("button", { name: "Plan itinerary" }).disabled, false)

  // Trip 1's plan finally resolves needing clarification — it must not land on London.
  await act(async () => slowPlan.resolve(response({ status: "needs_clarification", questions: ["Which Tokyo?"] })))
  assert.doesNotMatch(view.container.textContent, /Which Tokyo\?/, "stale plan must not render under the new trip")
  assert.equal(view.getByRole("button", { name: "Plan itinerary" }).disabled, false)
})

test("another tab completing sign-in reloads the current authenticated view", async (context) => {
  let reloads = 0
  context.mock.method(globalThis, "fetch", async (url) =>
    response(url.endsWith("/auth/config") ? { enforced: false } : []))
  const view = render(createElement(App))
  await act(async () => {})
  const browserWindow = window
  globalThis.window = new Proxy(browserWindow, {
    get(target, key) {
      if (key === "location") return { reload: () => { reloads++ } }
      return Reflect.get(target, key)
    },
  })
  try {
    browserWindow.dispatchEvent(new browserWindow.StorageEvent("storage", {
      key: "travel-agent.sessionChanged", newValue: "nonsecret-notification", storageArea: localStorage,
    }))
    assert.equal(reloads, 1)
  } finally {
    globalThis.window = browserWindow
    view.unmount()
  }
})
