import { useLayoutEffect, useRef, type ReactNode } from 'react'

import { explain } from '@/lib/errors'
import { count, NOTHING, scoreNumber, wholeNumber } from '@/lib/format'
import { COUNT_MS, nearView, stillness, tween } from '@/lib/motion'
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
 * `value` is the count itself, nullable: `Counted` renders `null` as a dash, so
 * "not measured" can never be drawn as `0`, and a number that is there counts up
 * to itself instead of appearing at full size.
 */
export function Stat({
  value,
  label,
  note,
}: {
  value: number | null
  label: string
  note?: string | null
}) {
  return (
    <div>
      <div className="text-title font-light">
        <Counted value={value} />
      </div>
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
      <div className="text-heading font-semibold leading-none">
        <Counted value={scoreNumber(value)} format={wholeNumber} />
      </div>
      <div className="mt-1 text-micro uppercase text-muted">
        {bucket ? BUCKETS[bucket] : 'не оценено'}
      </div>
    </div>
  )
}

/**
 * A number that counts up to its value instead of being substituted.
 *
 * From the value it showed before — zero on arrival — to the new one, inside
 * half a second, on the system's curve. The text node React rendered is the one
 * that is rewritten, and it ends on exactly the string React put there, so a
 * screen reader and a copy-paste both get the final value; the animation is
 * only ever drawn over it. It does not run for a number far off screen, nor
 * for anybody who asked for no motion.
 *
 * `format` must be a stable function (a module-level one): it is a dependency.
 */
export function Counted({
  value,
  format = count,
}: {
  value: number | null
  format?: (value: number) => string
}) {
  const box = useRef<HTMLSpanElement>(null)
  // What is on screen right now, including halfway through a count; a new
  // value starts from here rather than jumping back to zero.
  const shown = useRef<number | null>(null)
  const text = value === null ? NOTHING : format(value)

  useLayoutEffect(() => {
    const element = box.current
    const node = element?.firstChild
    if (!element || !(node instanceof Text)) return
    const from = shown.current ?? 0
    if (value === null || from === value || stillness() || !nearView(element)) {
      // Written only when it differs: a write here invalidates layout, and the
      // next counter's `nearView` would then lay the page out again — once per
      // row, which on two thousand rows was a 450ms frame.
      if (node.nodeValue !== text) node.nodeValue = text
      shown.current = value
      return
    }
    const to = value
    return tween(COUNT_MS, (progress) => {
      const current = progress >= 1 ? to : from + (to - from) * progress
      shown.current = current
      node.nodeValue = progress >= 1 ? text : format(current)
    })
  }, [value, text, format])

  return (
    <span ref={box} className="tnum">
      {text}
    </span>
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
  // min-w-0 and break-anywhere: a field sits in a grid cell, and one long token
  // (a rules version like workshop:22431e6788b3, a URL) used to widen the page.
  return (
    <div className="min-w-0">
      <div className="text-micro uppercase text-muted">{label}</div>
      <div className="break-anywhere mt-1">{children}</div>
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

/**
 * The shape of what is coming, while it comes.
 *
 * A skeleton is drawn in the form of the screen that will replace it — rows
 * where rows will be, four figures where four figures will be — so that the
 * arrival changes what is in the boxes and not where they are. The words
 * «Загружаем …» are for a screen reader; on the screen the shape says it.
 */
export function Loading({ what, shape = 'lines' }: { what: string; shape?: LoadingShape }) {
  return (
    <div className="mb-section" role="status" aria-label={`Загружаем ${what}`}>
      {shape === 'rows' ? <RowsSkeleton /> : null}
      {shape === 'board' ? <BoardSkeleton /> : null}
      {shape === 'cards' ? <CardsSkeleton /> : null}
      {shape === 'overview' ? <OverviewSkeleton /> : null}
      {shape === 'detail' ? <DetailSkeleton /> : null}
      {shape === 'lines' ? <Skeleton lines={4} what={what} /> : null}
    </div>
  )
}

export type LoadingShape = 'lines' | 'rows' | 'board' | 'cards' | 'overview' | 'detail'

/**
 * One placeholder bar, set inside a line of the same type it stands in for:
 * the line box takes its height from the text class around it, so a bone in a
 * `text-heading` line is exactly as tall as the heading will be.
 */
function Bone({ width }: { width: string }) {
  return (
    <span
      aria-hidden
      className="skeleton inline-block h-[0.8em] max-w-full bg-hairline align-middle"
      style={{ width }}
    />
  )
}

/** A skeleton section heading, in the shape of `Section`'s. */
function HeadBone({ width = '30%' }: { width?: string }) {
  return (
    <div className="mb-6 border-b border-hairline pb-4">
      <div className="text-heading font-semibold">
        <Bone width={width} />
      </div>
      <div className="mt-1 text-small">
        <Bone width="55%" />
      </div>
    </div>
  )
}

/** The vacancy list's rows: title and company, the facts line, the score. */
export function RowsSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="border-t border-hairline">
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="grid grid-cols-1 items-baseline gap-2 border-b border-hairline px-4 py-5 sm:grid-cols-[1fr_auto]"
        >
          <div className="min-w-0">
            <div className="font-semibold">
              <Bone width={`${String(38 + ((index * 23) % 30))}%`} />
            </div>
            <div className="mt-2 text-small">
              <Bone width={`${String(62 + ((index * 17) % 25))}%`} />
            </div>
          </div>
          <div className="text-right">
            <div className="text-heading font-semibold leading-none">
              <Bone width="2.2ch" />
            </div>
            <div className="mt-1 text-micro">
              <Bone width="9ch" />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function StatsBone() {
  return (
    <div className="grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
      {Array.from({ length: 4 }, (_, index) => (
        <div key={index}>
          <div className="text-title font-light">
            <Bone width="3ch" />
          </div>
          <div className="mt-2 text-small">
            <Bone width="70%" />
          </div>
          <div className="mt-1 text-small">
            <Bone width="50%" />
          </div>
        </div>
      ))}
    </div>
  )
}

/** The overview: the operations list, then a row of four figures. */
function OverviewSkeleton() {
  return (
    <>
      <section className="mb-section">
        <HeadBone />
        <div className="border-t border-hairline">
          {Array.from({ length: 6 }, (_, index) => (
            <div
              key={index}
              className="grid gap-4 border-b border-hairline py-6 md:grid-cols-[2.5rem_minmax(0,1fr)_auto]"
            >
              <div className="text-small">
                <Bone width="1.5rem" />
              </div>
              <div>
                <div className="font-semibold">
                  <Bone width={`${String(24 + ((index * 13) % 18))}%`} />
                </div>
                <div className="mt-1 text-small">
                  <Bone width="60%" />
                </div>
              </div>
              <div className="h-10 w-48 rounded-pill border border-hairline" aria-hidden />
            </div>
          ))}
        </div>
      </section>
      <section className="mb-section">
        <HeadBone width="12%" />
        <StatsBone />
      </section>
    </>
  )
}

/** The tracker's four columns, a card or two in each. */
function BoardSkeleton() {
  return (
    <section className="mb-section">
      <HeadBone width="16%" />
      <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, column) => (
          <div key={column}>
            <div className="mb-4 border-b border-hairline pb-2 text-small">
              <Bone width="50%" />
            </div>
            <div className="space-y-4">
              {Array.from({ length: 2 - (column % 2) }, (_, index) => (
                <div key={index} className="border border-hairline p-6">
                  <div>
                    <Bone width="80%" />
                  </div>
                  <div className="text-small">
                    <Bone width="45%" />
                  </div>
                  <div className="mt-4 text-small">
                    <Bone width="65%" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

/** Documents: two cards abreast, each a title and a grid of facts. */
function CardsSkeleton() {
  return (
    <section className="mb-section">
      <HeadBone width="14%" />
      <div className="grid gap-6 lg:grid-cols-2">
        {Array.from({ length: 2 }, (_, index) => (
          <div key={index} className="border border-hairline p-card">
            <div className="font-semibold">
              <Bone width="55%" />
            </div>
            <div className="mt-6 grid grid-cols-2 gap-4">
              {Array.from({ length: 4 }, (_, cell) => (
                <div key={cell}>
                  <div className="text-micro">
                    <Bone width="40%" />
                  </div>
                  <div className="mt-1">
                    <Bone width="70%" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

/** One vacancy: the big title and its facts beside the score, then a section. */
function DetailSkeleton() {
  return (
    <>
      <div className="text-small">
        <Bone width="10rem" />
      </div>
      <div className="mb-section mt-6 flex flex-wrap items-start justify-between gap-6 border-b border-hairline pb-8">
        <div className="min-w-0 flex-1">
          <div className="text-title font-light">
            <Bone width="70%" />
          </div>
          <div className="mt-3 text-small">
            <Bone width="55%" />
          </div>
          <div className="mt-1 text-small">
            <Bone width="25%" />
          </div>
        </div>
        <div className="text-right">
          <div className="text-heading font-semibold leading-none">
            <Bone width="2.2ch" />
          </div>
          <div className="mt-1 text-micro">
            <Bone width="9ch" />
          </div>
        </div>
      </div>
      <HeadBone width="18%" />
    </>
  )
}

/**
 * A failed request, said out loud.
 *
 * The status and the server's own `detail` are both shown: "409" alone sends
 * somebody to the network tab, and the backend already writes a sentence
 * explaining itself (see `app/core/exceptions.py`).
 */
export function Failure({
  error,
  what,
  onRetry,
}: {
  error: unknown
  what: string
  onRetry?: () => void
}) {
  const { reason, remedy } = explain(error)
  return (
    <div className="border border-ink p-card" role="alert">
      <p className="font-semibold">Не удалось загрузить {what}.</p>
      <p className="mt-2 text-small">{reason}</p>
      <p className="mt-1 text-small text-muted">{remedy}</p>
      {onRetry ? (
        <div className="mt-4">
          <Button outline onClick={onRetry}>
            Попробовать ещё раз
          </Button>
        </div>
      ) : null}
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

/**
 * The two buttons this system has: filled ink, and a hairline outline.
 *
 * Both are pills (75px) and both change state by colour inversion over the
 * system's slow curve. Each state has its own look and its own words:
 *
 * - hover inverts, pressing gives a little under the finger (`.press`);
 * - `busy` is work in progress: the button says what it is doing — «Пишем…»,
 *   «Собираем…» — keeps its full ink, and a hairline runs under the label;
 * - `done` is the moment after: the result, in words, for as long as the caller
 *   keeps passing it (`useFlash` holds it for a couple of seconds);
 * - disabled for any other reason fades the ink, and says why through `title`
 *   when the caller knows.
 *
 * A label that changes fades in rather than being swapped between frames.
 */
export function Button({
  children,
  onClick,
  disabled = false,
  outline = false,
  busy = false,
  done = false,
  title,
  type = 'button',
}: {
  children: ReactNode
  onClick?: () => void
  disabled?: boolean
  outline?: boolean
  /** What the button is doing right now, or false. Disables it. */
  busy?: string | false
  /** What just finished, in words, or false. */
  done?: string | false
  title?: string | undefined
  type?: 'button' | 'submit'
}) {
  const state = busy ? 'busy' : done ? 'done' : 'idle'
  const shape =
    'press inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-pill px-6 py-2 text-small ' +
    'disabled:cursor-not-allowed'
  // A busy button is disabled but not faded: it is not «unavailable», it is
  // occupied, and it says with what.
  const dim = busy ? 'working' : 'disabled:opacity-40'
  const look = outline
    ? 'border border-ink text-ink enabled:hover:bg-ink enabled:hover:text-paper'
    : 'border border-ink bg-ink text-paper enabled:hover:bg-paper enabled:hover:text-ink'
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || state === 'busy'}
      aria-busy={state === 'busy'}
      title={title}
      className={`${shape} ${dim} ${look}`}
    >
      <span key={state} className={state === 'idle' ? undefined : 'swap'}>
        {busy || done || children}
      </span>
    </button>
  )
}

/**
 * A block of placeholder lines while something loads, so the page does not jump.
 *
 * Hairline bars at the height of the text they stand in for, pulsing through
 * opacity only (0.4s steps are not motion, and reduced-motion stops them).
 */
export function Skeleton({ lines = 3, what }: { lines?: number; what: string }) {
  return (
    <div role="status" aria-label={`Загружаем ${what}`} className="space-y-3">
      {Array.from({ length: lines }, (_, index) => (
        <div
          key={index}
          className="skeleton h-4 bg-hairline"
          style={{ width: `${String(100 - ((index * 17) % 45))}%` }}
        />
      ))}
    </div>
  )
}

/**
 * An empty screen that says what to do next, with the button that does it.
 *
 * Every empty state in the product has a next step; a panel that only says
 * "пусто" sends a person looking for it.
 */
export function NextStep({
  title,
  children,
  action,
}: {
  title: string
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="border border-hairline p-card">
      <p className="font-semibold">{title}</p>
      <div className="mt-2 max-w-2xl text-small text-muted">{children}</div>
      {action ? <div className="mt-6 flex flex-wrap gap-3">{action}</div> : null}
    </div>
  )
}
