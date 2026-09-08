/**
 * Thin fetch wrapper. Requests go through the Vite proxy in dev, so the app
 * always talks to its own origin and CORS never hides a real failure.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** The problem document's own explanation, when there was one. */
    readonly detail?: string,
    /**
     * Which fields a 422 objected to, as dotted paths ("email", "links.0.url").
     *
     * Kept apart from `detail` because the messages inside a validation error
     * are written by Pydantic, in English, for a developer. The screen turns
     * these paths into its own Russian sentence rather than showing text no
     * user of this product should have to read.
     */
    readonly fields?: string[],
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

interface ProblemDocument {
  detail?: string
  errors?: { loc?: unknown[] }[]
}

/**
 * Read what an RFC 7807 problem document says, as far as it says anything.
 *
 * Every error this API raises answers in that envelope. Throwing it away would
 * leave the form saying only that something went wrong, on the one screen where
 * *what* went wrong is the entire message.
 */
async function readProblem(response: Response): Promise<Pick<ApiError, 'detail' | 'fields'>> {
  let body: unknown
  try {
    body = await response.json()
  } catch {
    // A body that is not JSON tells us nothing; the status still does.
    return {}
  }
  if (body === null || typeof body !== 'object') {
    return {}
  }

  const problem = body as ProblemDocument
  const detail = typeof problem.detail === 'string' ? problem.detail : undefined
  const fields = (problem.errors ?? [])
    .map((error) =>
      (error.loc ?? [])
        // The first segment is always "body" for a request payload, which says
        // nothing to anyone reading a form.
        .filter((segment) => segment !== 'body')
        .map(String)
        .join('.'),
    )
    .filter((path) => path !== '')

  return { ...(detail === undefined ? {} : { detail }), ...(fields.length === 0 ? {} : { fields }) }
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } })

  // 503 is how /health reports a degraded service, and its body is the report.
  if (!response.ok && response.status !== 503) {
    const problem = await readProblem(response)
    throw new ApiError(`GET ${path} failed`, response.status, problem.detail, problem.fields)
  }

  return (await response.json()) as T
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PATCH',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const problem = await readProblem(response)
    throw new ApiError(`PATCH ${path} failed`, response.status, problem.detail, problem.fields)
  }

  return (await response.json()) as T
}
