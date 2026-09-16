/**
 * How old a posting is, as a verdict. See `components/Freshness.tsx`.
 */

/** Past this many days a posting is called old. hh keeps postings for thirty. */
export const OLD_AFTER_DAYS = 30
/** Past this many days without a sighting, "still there" is a guess. */
export const UNSEEN_AFTER_DAYS = 7

const DAY_MS = 86_400_000

export type FreshnessVerdict = 'archived' | 'unseen' | 'old' | 'fresh' | 'unknown'

export function freshnessOf(
  publishedAt: string | null,
  lastSeenAt: string | null,
  active: boolean | null,
  now: number = Date.now(),
): FreshnessVerdict {
  if (active === false) return 'archived'
  const seen = lastSeenAt ? new Date(lastSeenAt).getTime() : Number.NaN
  if (!Number.isNaN(seen) && now - seen > UNSEEN_AFTER_DAYS * DAY_MS) return 'unseen'
  const published = publishedAt ? new Date(publishedAt).getTime() : Number.NaN
  if (!Number.isNaN(published) && now - published > OLD_AFTER_DAYS * DAY_MS) return 'old'
  if (Number.isNaN(seen) && Number.isNaN(published)) return 'unknown'
  return 'fresh'
}
