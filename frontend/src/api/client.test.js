import test from "node:test"
import assert from "node:assert/strict"

import {
  AUTHENTICATION_REQUIRED_EVENT,
  getAccessToken,
  listTrips,
  login,
} from "./client.ts"

const TOKEN_KEY = "travel-agent.verifiedAccessToken"
const EXPIRATION_KEY = "travel-agent.verifiedAccessTokenExpiresAt"

class MemoryStorage {
  values = new Map()

  getItem(key) {
    return this.values.get(key) ?? null
  }

  setItem(key, value) {
    this.values.set(key, String(value))
  }

  removeItem(key) {
    this.values.delete(key)
  }
}

globalThis.localStorage = new MemoryStorage()
globalThis.window = new EventTarget()

test("expired stored credentials are cleared", () => {
  localStorage.setItem(TOKEN_KEY, "expired-token")
  localStorage.setItem(EXPIRATION_KEY, "2000-01-01T00:00:00Z")

  assert.equal(getAccessToken(), null)
  assert.equal(localStorage.getItem(TOKEN_KEY), null)
  assert.equal(localStorage.getItem(EXPIRATION_KEY), null)
})

test("a protected 401 clears credentials and announces that sign-in is required", async () => {
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        access_token: "valid-token",
        expires_at: "2999-01-01T00:00:00Z",
        session_id: "session-a",
        mfa_required: false,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )
  await login("traveler@example.test", "password", "device-a")

  let authenticationRequired = false
  window.addEventListener(AUTHENTICATION_REQUIRED_EVENT, () => {
    authenticationRequired = true
  })
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ code: "authentication_required", detail: "expired" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    })

  await assert.rejects(listTrips())
  assert.equal(getAccessToken(), null)
  assert.equal(authenticationRequired, true)
})
