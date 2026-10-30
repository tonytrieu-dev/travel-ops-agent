import { JSDOM } from "jsdom"
import { registerHooks } from "node:module"
import { existsSync, readFileSync } from "node:fs"
import { transpileModule, ModuleKind, JsxEmit } from "typescript"
import { afterEach, beforeEach } from "node:test"

const browser = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost" })
for (const key of ["window", "document", "HTMLElement", "Node", "Event", "MouseEvent", "localStorage"]) globalThis[key] = browser.window[key]
globalThis.IS_REACT_ACT_ENVIRONMENT = true
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith(".") && context.parentURL?.startsWith("file:")) {
      for (const extension of [".ts", ".tsx"]) {
        const url = new URL(specifier + extension, context.parentURL)
        if (existsSync(url)) return { url: url.href, shortCircuit: true }
      }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.endsWith(".tsx")) return {
      format: "module", shortCircuit: true,
      source: transpileModule(readFileSync(new URL(url), "utf8"), {
        compilerOptions: { module: ModuleKind.ESNext, jsx: JsxEmit.ReactJSX },
      }).outputText,
    }
    return nextLoad(url, context)
  },
})
const { cleanup } = await import("@testing-library/react")
beforeEach(() => localStorage.clear())
afterEach(cleanup)
export function session(token = "test-session") {
  localStorage.setItem("travel-agent.verifiedAccessToken", token)
  localStorage.setItem("travel-agent.verifiedAccessTokenExpiresAt", "2099-01-01T00:00:00Z")
}
export function deferred() {
  let resolve, reject
  const promise = new Promise((accept, refuse) => { resolve = accept; reject = refuse })
  return { promise, resolve, reject }
}
export const response = (data, status = 200) => new Response(JSON.stringify(data), { status })
