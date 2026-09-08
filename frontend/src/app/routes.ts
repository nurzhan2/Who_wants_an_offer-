/**
 * Routing, in a fragment and in about forty lines.
 *
 * The brief says to add routing and not to change the stack, and this project's
 * rule about dependencies is to check first whether what is already here will
 * do. Six screens and one detail view need: a URL that survives a reload, a
 * back button that works, and links that can be copied. The fragment gives all
 * three with no router, no history integration and no bundle.
 *
 * The fragment rather than the path, deliberately: a path-based router needs
 * the server to answer every URL with index.html, and this app is served by
 * `vite preview` in development and by whatever the owner puts in front of it
 * later. A hash never reaches a server, so a deep link cannot 404 on a machine
 * nobody configured.
 */

export const ROUTES = {
  overview: 'Обзор',
  vacancies: 'Вакансии',
  applications: 'Отклики',
  documents: 'Документы',
  workshop: 'Мастерская',
  profile: 'Мои данные',
} as const

export type RouteName = keyof typeof ROUTES

export const DEFAULT_ROUTE: RouteName = 'overview'

/** Where the app is now: a screen, and for one of them a selected vacancy. */
export interface Location {
  route: RouteName
  /** The vacancy whose card is open, from `#/vacancies/<id>`. */
  id: string | null
}

function isRoute(value: string): value is RouteName {
  return value in ROUTES
}

export function parseHash(hash: string): Location {
  const [route = '', id = ''] = hash.replace(/^#\/?/, '').split('/')
  if (!isRoute(route)) {
    return { route: DEFAULT_ROUTE, id: null }
  }
  return { route, id: id || null }
}

export function href(route: RouteName, id?: string): string {
  return id ? `#/${route}/${id}` : `#/${route}`
}
