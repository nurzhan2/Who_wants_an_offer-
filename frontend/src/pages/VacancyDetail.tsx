import { href } from '@/app/routes'
import { fileUrl } from '@/api/documents'
import { ApplyConfirm } from '@/components/ApplyConfirm'
import { Freshness } from '@/components/Freshness'
import { Card, Empty, Failure, Field, Loading, Pill, Score, Section } from '@/components/ui'
import { useGenerateDocument } from '@/hooks/useDocuments'
import { useVacancy, useWriteLetter } from '@/hooks/queries'
import { count, date, dateTime, plural, salary, score } from '@/lib/format'
import { EVIDENCE, outcomeLabel, REMOTE, REQUIREMENT_SOURCE, SKIPPED } from '@/lib/labels'
import type { MatchSummary, RequirementStanding, VacancySource } from '@/types/api'
import type { GeneratedDocument } from '@/types/documents'

/**
 * One vacancy, and the reason it scores what it scores.
 *
 * The requirement list comes back in three parts and the middle one is what the
 * screen is for. A requirement the scorer counted as missing may be something
 * this person does — the resume in hand simply never named it — and that is a
 * line to add before applying, while a real gap is a job to skip. Showing both
 * as «нет» would hide the difference between an afternoon's editing and a
 * career change.
 *
 * The buttons here generate documents, and «Откликнуться…» opens the card the
 * agent would be handed. Confirming it records consent bound to that card; the
 * application itself is sent by the agent on the owner's machine, which reads
 * the vacancy page again first. Nothing is sent from this page.
 *
 * The CV button used to be a link to «Мои данные», because the screen was built
 * on a branch where nothing generated a CV and the nearest true thing to offer
 * was the uploaded resume. There is a generator now, so the button generates:
 * same vacancy, same place, and the ATS report the document was judged by comes
 * back with it.
 */
export function VacancyDetail({ id }: { id: string }) {
  const { data, isPending, isError, error } = useVacancy(id)
  const write = useWriteLetter()
  const cv = useGenerateDocument('cv')

  if (isPending) return <Loading what="вакансию" />
  if (isError) return <Failure error={error} what="вакансию" />

  const { vacancy, match, requirements, letter } = data
  const description = vacancy.description_md ?? vacancy.description_raw

  return (
    <div className="rise">
      <a href={href('vacancies')} className="text-small text-muted underline-offset-4 hover:underline">
        ← ко всем вакансиям
      </a>

      <header className="mb-section mt-6 flex flex-wrap items-start justify-between gap-6 border-b border-hairline pb-8">
        <div className="min-w-0">
          <h1 className="text-title font-light">{vacancy.title}</h1>
          <p className="mt-3 text-small text-muted">
            {vacancy.company ?? 'без компании'} · {vacancy.city ?? 'без города'} ·{' '}
            {REMOTE[vacancy.remote]} · {salary(vacancy.salary_min, vacancy.salary_max, vacancy.currency)}
          </p>
          <p className="mt-1 text-small text-muted">впервые увидели {date(vacancy.first_seen_at)}</p>
          <div className="mt-2">
            <Freshness
              publishedAt={vacancy.published_at}
              lastSeenAt={vacancy.last_seen_at}
              active={vacancy.is_active}
            />
          </div>
          <Sources sources={vacancy.sources} />
        </div>
        <Score value={match?.score ?? null} bucket={match?.bucket ?? null} />
      </header>

      <Section
        title="Документы"
        note="Сначала письмо, потом «Откликнуться…»: откроется карточка целиком — письмо, оценка, предупреждения hh. Подтверждённый отклик отправит агент на вашем компьютере."
      >
        <div className="flex flex-wrap items-center gap-4">
          <ApplyConfirm vacancyId={id} />
          <button
            type="button"
            className="rounded-pill border border-ink px-6 py-2 text-small transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper disabled:border-hairline disabled:text-muted"
            disabled={write.isPending}
            onClick={() => {
              write.mutate({ vacancyId: id, force: letter !== null && letter.characters > 0 })
            }}
          >
            {letter !== null && letter.characters > 0 ? 'Переписать письмо' : 'Написать письмо'}
          </button>
          <button
            type="button"
            className="rounded-pill border border-ink px-6 py-2 text-small transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper disabled:border-hairline disabled:text-muted"
            disabled={cv.isPending}
            onClick={() => {
              cv.mutate(id)
            }}
          >
            {cv.isPending ? 'Собираю CV…' : 'CV под эту вакансию'}
          </button>
          {letter !== null && letter.characters > 0 ? (
            <span className="text-small text-muted">
              письмо есть: {count(letter.characters)}{' '}
              {plural(letter.characters, 'знак', 'знака', 'знаков')}
              {letter.sent_at ? `, отправлено ${date(letter.sent_at)}` : ', не отправлено'}
              {letter.outcome ? ` · ${outcomeLabel(letter.outcome) ?? ''}` : ''}
            </span>
          ) : (
            <span className="text-small text-muted">письма ещё нет</span>
          )}
        </div>
        {cv.isError ? (
          <p className="mt-4 text-small">Не удалось собрать CV: {cv.error.message}</p>
        ) : null}
        {cv.data ? <CvResult document={cv.data} /> : null}
        {write.data ? (
          <p className="mt-4 text-small">
            {write.data.saved
              ? `Письмо сохранено: ${count(write.data.characters)} ${plural(write.data.characters, 'знак', 'знака', 'знаков')}, ${
                  write.data.from_model ? 'написала модель' : 'собрано по правилам, без модели'
                }.`
              : (SKIPPED[write.data.skipped ?? ''] ?? 'Ничего не записано.')}
          </p>
        ) : null}
        {write.isError ? <p className="mt-4 text-small">Не удалось: {String(write.error)}</p> : null}
      </Section>

      <Section
        title="Требования"
        note="Три колонки, а не две. Средняя — то, что у кандидата есть, но в этом резюме не названо: score считает это пробелом, хотя чинится это правкой CV. Строка «выведено из текста описания» значит, что работодатель этого требования не называл — его вычитали из объявления."
      >
        <div className="grid gap-6 lg:grid-cols-3">
          <Column
            title="закрыто"
            note="совпадает с навыками активного резюме"
            rows={requirements.covered}
            strong
          />
          <Column
            title="есть, но не в этом CV"
            note="доказательство — другое резюме владельца или текст этого"
            rows={requirements.not_in_this_cv}
          />
          <Column
            title="нет навыка"
            note="ничто в базе не говорит, что он есть"
            rows={requirements.absent}
          />
        </div>
      </Section>

      {match ? <Explanation match={match} /> : null}

      <Section title="Описание" note="Как его записал коннектор.">
        {description ? (
          <div className="max-w-3xl whitespace-pre-wrap text-body">{description}</div>
        ) : (
          <Empty>Источник не отдал описание — по этой вакансии есть только заголовок и ссылка.</Empty>
        )}
      </Section>
    </div>
  )
}

function Column({
  title,
  note,
  rows,
  strong = false,
}: {
  title: string
  note: string
  rows: RequirementStanding[]
  strong?: boolean
}) {
  return (
    <Card>
      <h3 className={`text-small uppercase tracking-widest ${strong ? 'font-semibold' : ''}`}>
        {title}
      </h3>
      <p className="mt-2 text-small text-muted">{note}</p>
      {rows.length === 0 ? (
        <p className="mt-6 text-small text-muted">пусто</p>
      ) : (
        <ul className="mt-6 space-y-4">
          {rows.map((row) => (
            <li key={row.canonical_name}>
              <div className="flex items-baseline justify-between gap-3">
                <span className={row.is_required ? 'font-semibold' : ''}>
                  {row.spelling ?? row.canonical_name}
                </span>
                {row.coverage !== null ? (
                  <span className="tnum text-small text-muted">
                    {Math.round(Number(row.coverage) * 100)}%
                  </span>
                ) : null}
              </div>
              {row.evidence ? (
                <div className="mt-1 text-small text-muted">
                  {EVIDENCE[row.evidence] ?? row.evidence}
                  {row.evidence_detail ? `: ${row.evidence_detail}` : ''}
                </div>
              ) : null}
              {!row.is_required ? <div className="mt-1 text-small text-muted">не обязательно</div> : null}
              {REQUIREMENT_SOURCE[row.source] ? (
                <div className="mt-1 text-small text-muted">{REQUIREMENT_SOURCE[row.source]}</div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

/** The score taken apart, on the one 0-100 scale everything here uses. */
function Explanation({ match }: { match: MatchSummary }) {
  const parts: [string, string][] = [
    ['покрытие обязательных', match.components.skill_coverage_required],
    ['покрытие желательных', match.components.skill_coverage_nice],
    ['семантика', match.components.semantic_similarity],
    ['опыт', match.components.experience_fit],
    ['домен', match.components.domain_fit],
    ['логистика', match.components.logistics_fit],
  ]
  return (
    <Section title="Из чего складывается score" note={`посчитано ${dateTime(match.scored_at)}`}>
      <div className="grid gap-6 sm:grid-cols-3 lg:grid-cols-6">
        {parts.map(([label, value]) => (
          <Field key={label} label={label}>
            <span className="tnum text-heading font-light">{score(value)}</span>
          </Field>
        ))}
      </div>
      {match.verdict ? <p className="mt-8 max-w-3xl">{match.verdict}</p> : null}
      {match.application_angle ? (
        <p className="mt-3 max-w-3xl text-muted">{match.application_angle}</p>
      ) : null}
      {match.red_flags.length > 0 ? (
        <ul className="mt-6 space-y-1 text-small">
          {match.red_flags.map((flag) => (
            <li key={flag}>· {flag}</li>
          ))}
        </ul>
      ) : null}
    </Section>
  )
}


/**
 * What came back from a CV generation, in three sentences at most.
 *
 * A withheld document is not a failure and is not silent: the audit or a hard
 * rule stopped it, and the reason is the whole message. A delivered one is a
 * link, because the file the server named is the thing the person wanted.
 */
/** The one source the agent applies through; everywhere else a person does. */
const AGENT_SOURCE = 'hh'

/**
 * One link per posting this vacancy was deduplicated from, named by its source
 * and, for an aggregator, by who first published it: «jsearch → LinkedIn».
 *
 * Two unnamed "исходная страница" pills side by side are indistinguishable, and
 * for hh the address is a regional subdomain that cannot be rebuilt from an id
 * anyway. The first is the fullest; a seed row is named as one and not linked.
 */
function Sources({ sources }: { sources: VacancySource[] }) {
  if (sources.length === 0) return null
  const manual = !sources.some((source) => source.source_slug === AGENT_SOURCE && !source.is_seed)
  return (
    <div className="mt-4 flex flex-col gap-2">
      <div className="flex flex-wrap gap-3">
        {sources.map((source) => {
          const label = source.publisher
            ? `${source.source_slug} → ${source.publisher}`
            : source.source_slug
          return source.is_seed ? (
            <Pill key={source.id}>{label} · тестовая запись</Pill>
          ) : (
            <a key={source.id} href={source.url} target="_blank" rel="noreferrer">
              <Pill strong={source.is_primary}>{label} ↗</Pill>
            </a>
          )
        })}
      </div>
      {manual ? (
        <p className="text-small text-muted">
          Автоотклик работает только для hh. Здесь откликаетесь сами: откройте оригинал и приложите
          документы, собранные ниже.
        </p>
      ) : null}
    </div>
  )
}

function CvResult({ document }: { document: GeneratedDocument }) {
  if (!document.delivered || document.document_id === null) {
    return (
      <div className="mt-4 text-small">
        <p>CV не выдан: {document.reason_ru ?? document.reason ?? 'без причины'}</p>
        {document.hard_rules.length > 0 ? (
          <ul className="mt-2 text-muted">
            {document.hard_rules.map((rule) => (
              <li key={rule}>· {rule}</li>
            ))}
          </ul>
        ) : null}
      </div>
    )
  }

  return (
    <p className="mt-4 text-small">
      <a href={fileUrl(document.document_id)} className="underline underline-offset-4">
        {document.filename ?? 'CV'}
      </a>
      {document.version === null ? '' : ` · версия ${String(document.version)}`}
      {document.review ? ` · ATS ${String(document.review.ats.score)}` : ''}
    </p>
  )
}
