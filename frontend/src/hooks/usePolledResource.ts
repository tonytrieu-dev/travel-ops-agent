import { ApiError, getAccessToken } from "../api/client.ts"
import { useEffect, useRef, useState } from "react"

const ACTIVE_POLL_MS = 700

interface PolledResourceOptions<T> {
  fetcher: () => Promise<T>
  isRunActive: boolean
  /** Idle refresh cadence for resources that genuinely change without an active run. */
  idlePollMs?: number
  resourceKey: string
  /** Shown verbatim on failure, so each caller names what it was actually loading. */
  errorText: string
}

let deniedSessionToken: string | null = null
const deniedResourceKeys = new Set<string>()

function isDeniedForCurrentSession(resourceKey: string): boolean {
  const accessToken = getAccessToken()
  if (accessToken !== deniedSessionToken) {
    deniedSessionToken = accessToken
    deniedResourceKeys.clear()
  }
  return accessToken !== null && deniedResourceKeys.has(resourceKey)
}

interface PolledResourceState<T> {
  data: T | null
  errorMessage: string | null
}

export function usePolledResource<T>({
  fetcher,
  isRunActive,
  idlePollMs,
  resourceKey,
  errorText,
}: PolledResourceOptions<T>): PolledResourceState<T> {
  const [data, setData] = useState<T | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  // Skip a tick if a request is still in flight, so the 700ms poll can't stack overlaps.
  const isFetchingRef = useRef(false)
  const pendingRefresh = useRef<(() => void) | null>(null)

  useEffect(() => {
    let current = true
    const refresh = async () => {
      if (!current || isFetchingRef.current) return
      if (isDeniedForCurrentSession(resourceKey)) {
        setErrorMessage(errorText)
        return
      }
      const sessionToken = getAccessToken()
      isFetchingRef.current = true
      try {
        const result = await fetcher()
        if (!current || sessionToken !== getAccessToken()) return
        setData(result)
        setErrorMessage(null)
      } catch (error) {
        if (!current || sessionToken !== getAccessToken()) return
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          if (sessionToken !== null) {
            deniedSessionToken = sessionToken
            deniedResourceKeys.add(resourceKey)
          }
        }
        setErrorMessage(errorText)
      } finally {
        isFetchingRef.current = false
        const pending = pendingRefresh.current
        pendingRefresh.current = null
        pending?.()
      }
    }
    setData(null)
    setErrorMessage(null)
    if (isFetchingRef.current) pendingRefresh.current = refresh
    else void refresh()
    const pollMs = isRunActive ? ACTIVE_POLL_MS : idlePollMs
    const interval = pollMs === undefined ? undefined : setInterval(refresh, pollMs)
    return () => {
      current = false
      if (pendingRefresh.current === refresh) pendingRefresh.current = null
      clearInterval(interval)
    }
  }, [fetcher, errorText, resourceKey, idlePollMs, isRunActive])

  return { data, errorMessage }
}
