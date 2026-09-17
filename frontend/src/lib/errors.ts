import { ApiError } from '@/api/client'

/**
 * A failed request in words a person can act on.
 *
 * The dev proxy answers 502–504 when the API process is not running, and the
 * browser throws a `TypeError` when nothing answers at all; both mean the same
 * thing to the owner and get the same sentence. Everything else keeps the
 * server's own `detail`, which the backend writes in Russian for this screen.
 */
export function explain(error: unknown): { reason: string; remedy: string } {
  if (!(error instanceof ApiError)) {
    return {
      reason: 'Сервер приложения не отвечает.',
      remedy: START_REMEDY,
    }
  }
  const { status, detail } = error
  // The API answers every failure with a problem document. A 5xx without one
  // came from the dev proxy, which says 500 — not 502 — when nothing listens
  // behind it: seen walking the dashboard while the API was restarting, and
  // read as «сервер упал» when it had not started yet.
  const fromTheApi = typeof error.problem === 'object' && error.problem !== null && 'type' in error.problem
  if (status >= 500 && !fromTheApi) {
    return { reason: 'Сервер приложения не отвечает.', remedy: START_REMEDY }
  }
  if (status === 401 || status === 403) {
    return {
      reason: 'Сервер не принял локальный токен.',
      remedy: 'Проверьте AGENT_API_TOKEN в файле .env и перезапустите приложение.',
    }
  }
  if (status === 404) {
    return {
      reason: detail ?? 'Такой записи нет.',
      remedy: 'Возможно, её удалили или сервер перезапускался. Вернитесь к списку.',
    }
  }
  if (status >= 500) {
    return {
      reason: detail ?? 'Сервер упал на этом запросе.',
      remedy: 'Подробности записаны в журнал сервера. Попробуйте ещё раз через минуту.',
    }
  }
  return { reason: detail ?? error.message, remedy: `Код ответа ${String(status)}.` }
}

const START_REMEDY =
  'Запустите приложение двойным щелчком по start.cmd в папке проекта (или командой python -m wwao up) и обновите страницу.'
