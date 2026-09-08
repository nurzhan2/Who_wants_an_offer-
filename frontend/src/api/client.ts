/**
 * Thin fetch wrapper. Requests go through the Vite proxy in dev, so the app
 * always talks to its own origin and CORS never hides a real failure.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: string | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** RFC 7807 is what `app/core/exceptions.py` answers errors with. */
interface ProblemDocument {
  title?: string
  detail?: string
}

async function problemOf(response: Response): Promise<string | null> {
  try {
    const body = (await response.json()) as ProblemDocument
    return body.detail ?? body.title ?? null
  } catch {
    // A proxy error page, or a body already consumed. The status is still
    // worth reporting, so this is not a failure.
    return null
  }
}

/** What a query parameter may be. Anything else has no sensible spelling. */
type QueryValue = string | number | boolean | null | undefined | (string | number)[]

function query(params: Record<string, QueryValue> | undefined): string {
  if (!params) return ''
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    // Repeated key per element, which is how FastAPI reads a list — never a
    // comma-joined string, which arrives as one filter value containing commas.
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, String(item))
    } else {
      search.append(key, String(value))
    }
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ''
}

export async function apiGet<T>(path: string, params?: Record<string, QueryValue>): Promise<T> {
  const response = await fetch(`${path}${query(params)}`, {
    headers: { Accept: 'application/json' },
  })

  // 503 is the health endpoint reporting a degraded service in a body worth
  // rendering; everything else non-2xx is an error.
  if (!response.ok && response.status !== 503) {
    throw new ApiError(`GET ${path} failed`, response.status, await problemOf(response))
  }

  return (await response.json()) as T
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    throw new ApiError(`POST ${path} failed`, response.status, await problemOf(response))
  }

  return (await response.json()) as T
}
