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
