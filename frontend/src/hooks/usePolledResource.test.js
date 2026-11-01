import "../test/setup.js"
import { deferred, session } from "../test/setup.js"
import assert from "node:assert/strict"
import test from "node:test"
import { act, renderHook } from "@testing-library/react"
import { usePolledResource } from "./usePolledResource.ts"
import { ApiError } from "../api/client.ts"

const options = { isRunActive: true, resourceKey: "execution", errorText: "Cannot load" }
test("cached denial displays an error after remount and resets for another session", async () => {
  await session("denied-session")
  let requests = 0
  const fetcher = async () => { requests++; throw new ApiError({ code: "forbidden", detail: "denied" }, 403) }
  const props = { ...options, isRunActive: false, resourceKey: "denied-remount", fetcher }
  const first = renderHook(() => usePolledResource(props))
  await act(async () => {})
  assert.equal(first.result.current.errorMessage, "Cannot load")
  first.unmount()
  const second = renderHook(() => usePolledResource(props))
  await act(async () => {})
  assert.equal(requests, 1)
  assert.equal(second.result.current.errorMessage, "Cannot load")
  second.unmount()
  await session("replacement-session")
  renderHook(() => usePolledResource(props))
  await act(async () => {})
  assert.equal(requests, 2)
})
test("run completion queues exactly one final refresh without overlapping", async () => {
  const pending = deferred()
  let requests = 0
  const fetcher = () => ++requests === 1 ? pending.promise : Promise.resolve("completed")
  const hook = renderHook((props) => usePolledResource(props), { initialProps: { ...options, fetcher } })
  hook.rerender({ ...options, fetcher, isRunActive: false })
  assert.equal(requests, 1)
  await act(async () => pending.resolve("running"))
  assert.equal(requests, 2)
  assert.equal(hook.result.current.data, "completed")
})

test("resource switching discards old results and fetches only the latest selection", async () => {
  const pending = deferred()
  let latestRequests = 0
  const hook = renderHook((props) => usePolledResource(props), { initialProps: { ...options, fetcher: () => pending.promise } })
  hook.rerender({ ...options, resourceKey: "skipped", fetcher: async () => { throw Error("obsolete fetcher ran") } })
  hook.rerender({ ...options, isRunActive: false, resourceKey: "latest", fetcher: async () => { latestRequests++; return "latest" } })
  await act(async () => pending.resolve("obsolete"))
  assert.equal(latestRequests, 1)
  assert.equal(hook.result.current.data, "latest")
})

for (const status of [401, 403, 500]) {
  test(`active polling ${status === 500 ? "retries" : "stops after"} HTTP ${status}`, async (context) => {
    await session(`timer-${status}`)
    let tick
    context.mock.method(globalThis, "setInterval", (callback) => { tick = callback; return 1 })
    context.mock.method(globalThis, "clearInterval", () => {})
    let requests = 0
    const fetcher = async () => { requests++; throw new ApiError({ code: "forbidden", detail: "failure" }, status) }
    const hook = renderHook(() => usePolledResource({ ...options, resourceKey: `timer-${status}`, fetcher }))
    await act(async () => {})
    for (let count = 0; count < 6; count++) await act(async () => tick())
    assert.equal(requests, status === 500 ? 7 : 1)
    assert.equal(hook.result.current.errorMessage, "Cannot load")
  })
}
for (const isRunActive of [true, false]) {
  test(`${isRunActive ? "active" : "idle"} cadence never overlaps requests`, async (context) => {
    let tick, cadence
    context.mock.method(globalThis, "setInterval", (callback, milliseconds) => { tick = callback; cadence = milliseconds; return 1 })
    context.mock.method(globalThis, "clearInterval", () => {})
    const pending = deferred()
    let requests = 0
    const fetcher = () => { requests++; return pending.promise }
    renderHook(() => usePolledResource({ ...options, isRunActive, idlePollMs: 4000, fetcher }))
    assert.equal(cadence, isRunActive ? 700 : 4000)
    await act(async () => { tick(); tick() })
    assert.equal(requests, 1)
    await act(async () => pending.resolve("done"))
    assert.equal(requests, 1)
  })
}
