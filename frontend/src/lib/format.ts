/**
 * Rendering values a person reads, and refusing to render ones nobody measured.
 *
 * Everything here takes `null` and answers with a dash rather than with a zero,
 * a dash rather than "0%", a dash rather than "—" spelled as an empty string.
 * The API is careful to distinguish "not measured" from "measured as none"; a
 * formatter that collapsed the two would undo that at the last possible moment,
 * where nothing tests it.
 */

const NOTHING = '—'

const NUMBER = new Intl.NumberFormat('ru-RU')
const DATE = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long' })
const DATE_TIME = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric',
  month: 'long',
  hour: '2-digit',
  minute: '2-digit',
})

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? NOTHING : NUMBER.format(value)
}

/** A score: the number itself, never a bar and never a colour. */
export function score(value: string | null | undefined): string {
  if (value === null || value === undefined) return NOTHING
  const parsed = Number(value)
  return Number.isFinite(parsed) ? String(Math.round(parsed)) : NOTHING
}

/** A score as the API sends it — a Decimal, as text — as a number, or null. */
export function scoreNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** A score counted on screen: whole, rounded as `score` rounds. */
export function wholeNumber(value: number): string {
  return String(Math.round(value))
}

export function date(value: string | null | undefined): string {
  if (!value) return NOTHING
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? NOTHING : DATE.format(parsed)
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return NOTHING
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? NOTHING : DATE_TIME.format(parsed)
}

/** "3 часа назад", for the freshness line beside a run. */
export function ago(value: string | null | undefined): string {
  if (!value) return NOTHING
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return NOTHING
  const minutes = Math.round((Date.now() - then) / 60_000)
  if (minutes < 1) return 'только что'
  if (minutes < 60) return `${String(minutes)} мин назад`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${String(hours)} ч назад`
  return `${String(Math.round(hours / 24))} дн назад`
}

/**
 * The salary as advertised, or an explicit "не указана".
 *
 * Not a dash: five postings in six on this corpus name no salary, and a column
 * of dashes reads as missing data rather than as the market's own habit. The
 * words are the honest version and they are what the filter panel refers to.
 */
export function salary(
  min: string | null,
  max: string | null,
  currency: string | null,
): string {
  if (min === null && max === null) return 'не указана'
  const unit = currency ?? ''
  const low = min === null ? null : NUMBER.format(Math.round(Number(min)))
  const high = max === null ? null : NUMBER.format(Math.round(Number(max)))
  if (low !== null && high !== null) return `${low} — ${high} ${unit}`.trim()
  if (low !== null) return `от ${low} ${unit}`.trim()
  return `до ${String(high)} ${unit}`.trim()
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return NOTHING
  const kb = value / 1024
  return kb < 1024
    ? `${NUMBER.format(Math.round(kb))} КБ`
    : `${NUMBER.format(Math.round(kb / 102.4) / 10)} МБ`
}

/** Russian plural agreement: 1 вакансия, 2 вакансии, 5 вакансий. */
export function plural(value: number, one: string, few: string, many: string): string {
  const mod10 = value % 10
  const mod100 = value % 100
  if (mod10 === 1 && mod100 !== 11) return one
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few
  return many
}

export { NOTHING }
