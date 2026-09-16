import { Pill } from '@/components/ui'
import { ago, date } from '@/lib/format'
import { freshnessOf, type FreshnessVerdict, OLD_AFTER_DAYS } from '@/lib/freshness'

/**
 * How old a posting is and whether it is still there.
 *
 * Added after the first real send (16 September 2026): a third of the queue had
 * been archived by the time the agent opened it, and a person confirming an
 * application has to see that before they confirm, not after. Three signals,
 * none of them a colour: the employer's own publication date, the crawler's
 * last sighting, and the crawler's verdict that the posting is gone.
 *
 * The thresholds are advice, not filters: an old posting may still be open,
 * and the agent re-reads the page before it sends anything.
 */

const VERDICT: Record<FreshnessVerdict, string | null> = {
  archived: 'в архиве',
  unseen: 'давно не видели',
  old: `старше ${String(OLD_AFTER_DAYS)} дней`,
  fresh: null,
  unknown: 'возраст неизвестен',
}

export function Freshness({
  publishedAt,
  lastSeenAt,
  active,
  archivedByAgent = false,
}: {
  publishedAt: string | null
  lastSeenAt: string | null
  active: boolean | null
  /** The agent opened the page and hh said the posting is archived. */
  archivedByAgent?: boolean
}) {
  const verdict = archivedByAgent ? 'archived' : freshnessOf(publishedAt, lastSeenAt, active)
  const word = VERDICT[verdict]
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-small text-muted">
      {word ? <Pill strong={verdict === 'archived'}>{word}</Pill> : null}
      <span>
        {publishedAt ? `опубликована ${date(publishedAt)} (${ago(publishedAt)})` : 'дата публикации неизвестна'}
      </span>
      {lastSeenAt ? <span>на сайте видели {ago(lastSeenAt)}</span> : null}
    </div>
  )
}
