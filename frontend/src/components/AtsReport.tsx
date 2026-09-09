import type { AtsFinding, DocumentReview } from '@/types/documents'

/**
 * The audit and the requirement coverage, beside the document they describe.
 *
 * The brief requires the report to travel with the document rather than sit
 * near it, so this is never rendered alone — it is the second half of whatever
 * shows a generated CV, and of the panel that explains one that was withheld.
 *
 * Two display rules come from the design system and are not decoration:
 *
 * - **no colour.** A failing check is not red. Severity is carried by the
 *   label, by the order (critical first) and by font weight, because the system
 *   has no chromatic colour in the interface at all.
 * - **the score is a number with a caption**, with no bar and no scale behind
 *   it. A filled meter invites comparison between two numbers that do not
 *   compare; the findings underneath are what the number means.
 *
 * The coverage block keeps its three lists apart for a reason that is not
 * visual. «Есть, но не названо» is fixed by regenerating; «нет у кандидата» is
 * not fixable, and the wording says so plainly rather than suggesting anything.
 */

const SEVERITY_RU: Record<string, string> = {
  critical: 'Критично',
  warning: 'Предупреждение',
  info: 'Заметка',
}

const OVERALL_RU: Record<string, string> = {
  ok: 'Робот прочитает',
  degraded: 'Прочитает частично',
  unreadable: 'Робот не прочитает',
}

const SEVERITY_ORDER: Record<string, number> = { critical: 0, warning: 1, info: 2 }

export function AtsReport({ review }: { review: DocumentReview }) {
  const { ats, coverage } = review
  const findings = [...ats.findings].sort(
    (left, right) => (SEVERITY_ORDER[left.severity] ?? 3) - (SEVERITY_ORDER[right.severity] ?? 3),
  )

  return (
    <section className="border-t border-obsidian pt-element">
      <header className="flex items-baseline gap-element">
        <span className="text-subheading font-light text-obsidian">{ats.score}</span>
        <span className="text-caption uppercase tracking-wide text-felt-gray">
          ATS · {OVERALL_RU[ats.overall] ?? ats.overall}
        </span>
      </header>

      <Coverage
        label="Названо в резюме"
        hint="требования вакансии, названные тем же словом"
        items={coverage.named}
        inferred={coverage.inferred}
        weight="font-semibold"
      />
      <Coverage
        label="Есть, но не названо"
        hint="навык у кандидата есть — чинится перегенерацией"
        items={coverage.held_but_unnamed}
        inferred={coverage.inferred}
      />
      <Coverage
        label="Нет у кандидата"
        hint="не покрыто; приписывать нельзя"
        items={coverage.not_held}
        inferred={coverage.inferred}
        weight="text-felt-gray"
      />

      {findings.length > 0 && (
        <ul className="mt-element space-y-element">
          {findings.map((finding) => (
            <Finding key={finding.code} finding={finding} />
          ))}
        </ul>
      )}
    </section>
  )
}

/**
 * One of the three lists, with the inferred requirements in it marked.
 *
 * The mark is a third state and belongs to the requirement rather than to the
 * document: «нет у кандидата» about something the employer asked for and «нет у
 * кандидата» about something we read out of their prose are different news, and
 * only one of them is a reason to skip the vacancy. It is rendered as a
 * suffix — the system has no colour to spend on it — and only on the entries it
 * applies to, because marking the normal case would bury it.
 */
function Coverage({
  label,
  hint,
  items,
  inferred,
  weight = '',
}: {
  label: string
  hint: string
  items: string[]
  inferred: string[]
  weight?: string
}) {
  if (items.length === 0) {
    return null
  }
  const guessed = new Set(inferred)
  const shown = items.map((item) => (guessed.has(item) ? `${item} (из текста)` : item))
  return (
    <div className="mt-element">
      <p className="text-caption uppercase tracking-wide text-felt-gray">
        {label} · {items.length}
      </p>
      <p className={`text-body-sm text-inkstone ${weight}`}>{shown.join(', ')}</p>
      <p className="text-caption text-ash-mist">{hint}</p>
      {items.some((item) => guessed.has(item)) ? (
        <p className="text-caption text-ash-mist">
          «из текста» — требование не названо работодателем, а выведено из описания
        </p>
      ) : null}
    </div>
  )
}

function Finding({ finding }: { finding: AtsFinding }) {
  return (
    <li className="border-t border-ash-mist pt-element">
      <p className="text-caption uppercase tracking-wide text-felt-gray">
        {SEVERITY_RU[finding.severity] ?? finding.severity}
      </p>
      <p
        className={`text-body-sm text-obsidian ${
          finding.severity === 'critical' ? 'font-semibold' : 'font-normal'
        }`}
      >
        {finding.title}
      </p>
      <p className="text-caption text-felt-gray">{finding.explanation}</p>
      <p className="text-caption text-inkstone">{finding.fix}</p>
    </li>
  )
}
