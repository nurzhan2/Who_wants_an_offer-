/**
 * Thin fetch wrapper. Requests go through the Vite proxy in dev, so the app
 * always talks to its own origin and CORS never hides a real failure.
 */
export class ApiError extends Error {
  /**
   * The server's own sentence, when it sent one.
   *
   * Derived from `problem` rather than stored beside it: two fields holding the
   * same string is two things to keep in step, and this one is read by the forms
   * that show the failure to a person.
   */
  readonly detail: string | undefined

  /**
   * Which fields a 422 objected to, as dotted paths ("email", "links.0.url").
   *
   * Kept apart from `detail` because the messages inside a validation error are
   * written by Pydantic, in English, for a developer. The screen turns these
   * paths into its own Russian sentence rather than showing text no user of this
   * product should have to read.
   */
  readonly fields: string[] | undefined

  constructor(
    message: string,
    readonly status: number,
    /**
     * The RFC 7807 problem document, when the server sent one.
     *
     * Kept whole rather than flattened into the message because some of them
     * carry fields a person needs: a rule refused for naming a skill the profile
     * does not have comes back with `claims`, and "rejected" without those names
     * is unactionable.
     */
    readonly problem?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
    this.detail = detailIn(problem)
    this.fields = fieldsIn(problem)
  }
}

/** RFC 7807, as `app/core/exceptions.py` answers errors with it. */
interface ProblemDocument {
  title?: string
  detail?: string
  errors?: { loc?: unknown[] }[]
}

/** The problem document's own explanation, as far as it has one. */
function detailIn(body: unknown): string | undefined {
  if (body && typeof body === 'object' && 'detail' in body) {
    const problem = body as ProblemDocument
    const detail = problem.detail ?? problem.title
    if (typeof detail === 'string' && detail) {
      return detail
    }
  }
  return undefined
}

/** The field paths a validation error names, flattened for a form to read. */
function fieldsIn(body: unknown): string[] | undefined {
  if (!body || typeof body !== 'object') {
    return undefined
  }
  const paths = ((body as ProblemDocument).errors ?? [])
    .map((error) =>
      (error.loc ?? [])
        // The first segment is always "body" for a request payload, which says
        // nothing to anyone reading a form.
        .filter((segment) => segment !== 'body')
        .map(String)
        .join('.'),
    )
    .filter((path) => path !== '')
  return paths.length === 0 ? undefined : paths
}

/** The message a person is shown for a failed request. */
function detailOf(body: unknown, fallback: string): string {
  return detailIn(body) ?? fallback
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

/** What a query string may carry. A list becomes a repeated key, never a join. */
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

  // 503 is how /health reports a degraded service, and its body is the report.
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
