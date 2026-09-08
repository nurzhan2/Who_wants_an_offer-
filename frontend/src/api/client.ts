/**
 * Thin fetch wrapper. Requests go through the Vite proxy in dev, so the app
 * always talks to its own origin and CORS never hides a real failure.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } })

  if (!response.ok && response.status !== 503) {
    throw new ApiError(`GET ${path} failed`, response.status)
  }

  return (await response.json()) as T
}

/**
 * POST with no body, for the endpoints that act on a path parameter.
 *
 * Every failure is an error here, with none of `apiGet`'s tolerance for 503:
 * that exists so the health badge can render a degraded backend rather than an
 * exception, and there is no equivalent reading of a generation that did not
 * happen. A document that was *withheld* is not a failure — it comes back 200
 * with `delivered: false` and a reason — so the only things that reach this
 * branch are a missing profile, a bad request and a broken server.
 *
 * The problem+json body is read for its `detail` when there is one, because the
 * backend puts the actionable sentence there ("upload a resume first") and
 * throwing `POST /… failed` instead would discard it.
 */
export async function apiPost<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { Accept: 'application/json' },
  })

  if (!response.ok) {
    throw new ApiError(await detailOf(response, path), response.status)
  }

  return (await response.json()) as T
}

async function detailOf(response: Response, path: string): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const { detail } = body
      if (typeof detail === 'string' && detail.trim()) {
        return detail
      }
    }
  } catch {
    // A non-JSON error body is a proxy or a crash; the generic message is all
    // there is to say about it.
  }
  return `POST ${path} failed`
}
