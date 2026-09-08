/**
 * Thin fetch wrapper. Requests go through the Vite proxy in dev, so the app
 * always talks to its own origin and CORS never hides a real failure.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /**
     * The RFC 7807 problem document, when the server sent one.
     *
     * Kept rather than flattened into the message because some of them carry
     * fields a person needs: a rule refused for naming a skill the profile does
     * not have comes back with `claims`, and "rejected" without those names is
     * unactionable.
     */
    readonly problem?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** The message a person is shown for a failed request. */
function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body).detail
    if (typeof detail === 'string' && detail) {
      return detail
    }
  }
  return fallback
}

/** Parse a JSON body, tolerating an empty one (204 has no content). */
async function parse(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) {
    return null
  }
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } })

  if (!response.ok && response.status !== 503) {
    const body = await parse(response)
    throw new ApiError(detailOf(body, `GET ${path} failed`), response.status, body)
  }

  return (await response.json()) as T
}

/**
 * A JSON request that changes something.
 *
 * One function for POST, PATCH and DELETE because the only thing that differs
 * between them here is the verb, and three near-identical wrappers is three
 * places for the error handling to drift.
 */
export async function apiSend<T>(
  method: 'POST' | 'PATCH' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: {
      Accept: 'application/json',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })

  const parsed = await parse(response)
  if (!response.ok) {
    throw new ApiError(detailOf(parsed, `${method} ${path} failed`), response.status, parsed)
  }
  return parsed as T
}

/** A multipart request, for the one endpoint that takes a file. */
export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  // No Content-Type header: the browser sets it, with the multipart boundary
  // that a hand-written one would omit.
  const response = await fetch(path, { method: 'POST', body: form, headers: { Accept: 'application/json' } })

  const parsed = await parse(response)
  if (!response.ok) {
    throw new ApiError(detailOf(parsed, `POST ${path} failed`), response.status, parsed)
  }
  return parsed as T
}
