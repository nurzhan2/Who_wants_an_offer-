import type { EmployerSignals as Signals } from '@/types/documents'

/**
 * What the employer published about their own posting, beside the vacancy.
 *
 * Four facts, all of them stated by the employer on the page: when they were
 * last active, whether they are an accredited IT employer, whether hh is
 * running an additional check on them, and how many people have already
 * applied. All four are read from the payload the crawler stored.
 *
 * **Nothing here is looked up, and nothing here will be.** No employee of a
 * company is searched for — not on a professional network, not anywhere. That
 * is a rule about what this project is, and this component renders the whole of
 * what the rule permits.
 *
 * `responses_count` of `null` renders as nothing at all rather than as `0`.
 * "The page did not say" and "nobody has applied yet" are different facts, and
 * the second one is encouraging in a way the first has not earned.
 */
export function EmployerSignals({ signals }: { signals: Signals }) {
  const items = [
    signals.last_activity ? `был онлайн ${relative(signals.last_activity)}` : null,
    signals.accredited_it_employer ? 'аккредитованный IT-работодатель' : null,
    signals.on_additional_check ? 'hh проверяет работодателя' : null,
    signals.responses_count === null ? null : `откликов: ${String(signals.responses_count)}`,
  ].filter((item): item is string => item !== null)

  if (items.length === 0) {
    return null
  }

  return (
    <p className="text-caption text-felt-gray">{items.join(' · ')}</p>
  )
}

/**
 * "3 дня назад", in the grammatical case Russian requires.
 *
 * Rendered relative because the question is "is anyone reading this inbox", and
 * a date makes the reader do the subtraction. An unparseable value falls back
 * to the string as it stands rather than to "Invalid Date".
 */
function relative(iso: string): string {
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) {
    return iso
  }
  const days = Math.floor((Date.now() - when.getTime()) / 86_400_000)
  if (days <= 0) {
    return 'сегодня'
  }
  if (days === 1) {
    return 'вчера'
  }
  return `${String(days)} ${plural(days, 'день', 'дня', 'дней')} назад`
}

function plural(count: number, one: string, few: string, many: string): string {
  const tail = count % 10
  const hundred = count % 100
  if (tail === 1 && hundred !== 11) {
    return one
  }
  if (tail >= 2 && tail <= 4 && !(hundred >= 12 && hundred <= 14)) {
    return few
  }
  return many
}
