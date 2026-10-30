import "../../test/setup.js"
import assert from "node:assert/strict"
import test from "node:test"
import { createElement } from "react"
import { render } from "@testing-library/react"
const { YourTripsPanel } = await import("../YourTripsPanel.tsx")

// Runs under TZ=America/Los_Angeles (negative UTC offset). A date-only value parsed as UTC
// midnight (new Date("2026-11-10")) renders the day before locally — the bug. The depart/return
// cells must show the entered calendar day.
test("date-only trip dates render the entered calendar day in a negative-offset timezone", () => {
  assert.equal(process.env.TZ, "America/Los_Angeles", "run with TZ=America/Los_Angeles")
  // Guards the assumption this test relies on: the raw UTC parse shifts the day back.
  assert.equal(new Date("2026-11-10").toLocaleDateString(), "11/9/2026")

  const trip = {
    id: 1, origin: "JFK", destination: "Tokyo", destination_airport: "HND",
    depart_date: "2026-11-10", return_date: "2026-11-20", age: 30, fitness_level: "high",
    status: "created", created_at: new Date(Date.now() + 86_400_000).toISOString(),
  }
  const view = render(createElement(YourTripsPanel, { trips: [trip], selectedTripId: null, onSelectTrip: () => {} }))
  const cells = [...view.container.querySelectorAll("td")].map((cell) => cell.textContent)
  assert.ok(cells.includes("11/10/2026"), `depart date must render 11/10/2026, got ${JSON.stringify(cells)}`)
  assert.ok(cells.includes("11/20/2026"), "return date must render the entered day")
})
