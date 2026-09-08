import type { ReactNode } from 'react'

import { ApiError } from '@/api/client'
import { count, NOTHING, score as formatScore } from '@/lib/format'
import { BUCKETS } from '@/lib/labels'
import type { Bucket } from '@/types/api'

/**
 * The pieces every screen is built out of.
 *
 * There are no variants and no props for emphasis, because the system has one
 * emphasis and it is inversion. A component cannot be asked for a colour here;
 * the only way to make something loud is to turn it inside out, which keeps the
 * interface monochrome by construction rather than by review.
 */

export function Section({
  title,
  note,
  action,
  children,
}: {
  title: string
  /** One line under the heading: what this panel is measuring, or its caveat. */
  note?: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="mb-section">
      <div className="mb-6 flex items-baseline justify-between gap-6 border-b border-hairline pb-4">
        <div>
          <h2 className="text-heading font-semibold">{title}</h2>
          {note ? <p className="mt-1 max-w-2xl text-small text-muted">{note}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  )
}

export function Card({
  children,
  inverted = false,
  className = '',
}: {
  children: ReactNode
  inverted?: boolean
  className?: string
}) {
  return (
    <div
      className={`border border-hairline p-card ${inverted ? 'invert-surface border-transparent' : ''} ${className}`}
    >
      {children}
    </div>
  )
}

/**
 * A measured number with the thing it measures under it.
 *
 * `value` is a string so that a caller has to have decided what "not measured"
 * looks like before it gets here — the formatters in `lib/format` answer with a
 * dash — rather than passing a nullable number and letting this render `0`.
 */
export function Stat({
  value,
  label,
  note,
}: {
  value: string
  label: string
  note?: string | null
}) {
  return (
    <div>
      <div className="tnum text-title font-light">{value}</div>
      <div className="mt-2 text-small font-semibold">{label}</div>
      {note ? <div className="mt-1 text-small text-muted">{note}</div> : null}
    </div>
  )
}

/**
 * A score: the number, and the bucket in words.
 *
 * No colour scale and no bar. 92 and 61 are two numbers on one scale, and a
 * green pill next to one of them would be the interface asserting a threshold
 * that lives in `docs/MATCHING.md` and moves. The word under the number is the
 * scorer's own bucket, which is where that judgement actually belongs.
 */
export function Score({ value, bucket }: { value: string | null; bucket: Bucket | null }) {
  return (
    <div className="text-right">
      <div className="tnum text-heading font-semibold leading-none">{formatScore(value)}</div>
      <div className="mt-1 text-micro uppercase text-muted">
        {bucket ? BUCKETS[bucket] : 'не оценено'}
      </div>
    </div>
  )
}

/** A label with a hairline around it. Never a status colour — see Score. */
export function Pill({ children, strong = false }: { children: ReactNode; strong?: boolean }) {
  return (
    <span
      className={`inline-block whitespace-nowrap rounded-pill border border-hairline px-3 py-1 text-micro uppercase ${
        strong ? 'font-semibold' : 'text-muted'
      }`}
    >
      {children}
    </span>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-micro uppercase text-muted">{label}</div>
      <div className="mt-1">{children}</div>
    </div>
  )
}

/**
 * Nothing here yet, and why.
 *
 * The reason is required. Every empty state in this dashboard has a cause worth
 * naming — no crawl has run, no letter has been written, hh has answered
 * nothing — and an empty panel that says only "нет данных" sends a person to
 * read the code.
 */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-small text-muted">{children}</p>
}

export function Loading({ what }: { what: string }) {
  return (
    <p className="text-small text-muted" role="status">
      Загружаем {what}…
    </p>
  )
}

/**
 * A failed request, said out loud.
 *
 * The status and the server's own `detail` are both shown: "409" alone sends
 * somebody to the network tab, and the backend already writes a sentence
 * explaining itself (see `app/core/exceptions.py`).
 */
export function Failure({ error, what }: { error: unknown; what: string }) {
  const known = error instanceof ApiError ? error : null
  return (
    <div className="border border-ink p-card" role="alert">
      <p className="font-semibold">Не удалось загрузить {what}.</p>
      <p className="mt-2 text-small text-muted">
        {known ? `${String(known.status)} · ${known.detail ?? known.message}` : 'Бэкенд недоступен.'}
      </p>
    </div>
  )
}

/** A row of numbers under one heading. */
export function Stats({ children }: { children: ReactNode }) {
  return <div className="grid gap-8 sm:grid-cols-2 lg:grid-cols-4">{children}</div>
}

/**
 * A fraction of something, in words rather than as a bar.
 *
 * `of` may be null — nothing counted the whole — and then the fraction is not
 * rendered at all rather than shown against a zero.
 */
export function Ratio({ part, of }: { part: number | null; of: number | null }) {
  if (part === null) return <span>{NOTHING}</span>
  if (of === null) return <span className="tnum">{count(part)}</span>
  return (
    <span className="tnum">
      {count(part)} <span className="text-muted">из {count(of)}</span>
    </span>
  )
}
