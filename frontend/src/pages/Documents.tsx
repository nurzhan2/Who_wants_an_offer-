import { useState } from 'react'

import { href } from '@/app/routes'
import { ResumeUpload } from '@/components/ResumeUpload'
import { Card, Failure, Field, Loading, NextStep, Pill, Section } from '@/components/ui'
import { useDocuments } from '@/hooks/queries'
import { bytes, count, date, plural, score } from '@/lib/format'
import { ATS_OVERALL, ATS_SEVERITY, outcomeLabel, PARSE_STATUS } from '@/lib/labels'
import type { LetterDocument, ResumeDocument } from '@/types/api'

/**
 * Документы: what this project has written, and whether the loop closed.
 *
 * Two kinds of document exist here and they are honest about being different.
 * A resume is *uploaded*, so what is shown for one is the file and the
 * readability audit taken when it landed; a CV for one vacancy is generated on
 * that vacancy's card and is not listed here.
 * Letters are generated, and each carries the three dates the feedback loop is
 * made of: written, sent, answered. A letter written and never sent is work
 * waiting for the owner's confirmation on the vacancy card.
 *
 * A parse the server abandoned — it runs inside the API process, which can be
 * stopped mid-way — reads as «не разобрано» with its reason, never as a parse
 * still going: the API marks such rows when it starts.
 */
export function Documents() {
  const { data, isPending, isError, error, refetch } = useDocuments()

  if (isPending) return <Loading what="документы" />
  if (isError) {
    return (
      <Failure
        error={error}
        what="документы"
        onRetry={() => {
          void refetch()
        }}
      />
    )
  }

  return (
    <div className="rise">
      <Section
        title="Резюме"
        note="Загруженные файлы и то, что из них вычитает парсер работодателя. CV под конкретную вакансию собирается в её карточке."
      >
        <div className="mb-6">
          <ResumeUpload />
        </div>
        {data.resumes.length === 0 ? null : (
          <div className="rise-list grid gap-6 lg:grid-cols-2">
            {data.resumes.map((resume) => (
              <Resume key={resume.profile_id} resume={resume} />
            ))}
          </div>
        )}
      </Section>

      <Section
        title="Письма"
        note={`Правила проверки сейчас — версия ${data.current_rules_version}. Если письмо писала другая версия, это видно в строке: правила меняются, а письмо остаётся.`}
      >
        {data.letters.length === 0 ? (
          <NextStep
            title="Писем пока нет"
            action={
              <a
                href={href('overview')}
                className="rounded-pill border border-ink px-6 py-2 text-small transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper"
              >
                К операциям
              </a>
            }
          >
            На «Обзоре» нажмите «Написать письма» — пять писем для лучших вакансий. Письмо для
            одной вакансии пишется в её карточке.
          </NextStep>
        ) : (
          <div className="rise-list space-y-4">
            {data.letters.map((letter) => (
              <Letter key={letter.application_id} letter={letter} current={data.current_rules_version} />
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}

function Resume({ resume }: { resume: ResumeDocument }) {
  const ats = resume.ats
  return (
    <Card inverted={resume.is_active}>
      <div className="flex items-baseline justify-between gap-4">
        <span className="break-anywhere min-w-0 font-semibold">
          {resume.filename ?? 'файл не записан'}
        </span>
        {resume.is_active ? <Pill strong>активное</Pill> : <Pill>прошлое</Pill>}
      </div>
      <div className="mt-6 grid grid-cols-2 gap-4">
        <Field label="загружено">{date(resume.uploaded_at)}</Field>
        <Field label="разбор">{PARSE_STATUS[resume.parse_status]}</Field>
        <Field label="формат">{resume.source_format ?? '—'}</Field>
        <Field label="размер">{bytes(resume.size_bytes)}</Field>
      </div>
      {resume.parse_error ? (
        <p className="break-anywhere mt-4 text-small font-semibold">{resume.parse_error}</p>
      ) : null}

      {ats === null ? (
        <p className="mt-6 border-t border-hairline pt-4 text-small">
          ATS-отчёта нет. Это не «чисто»: аудит по этому файлу просто не записывали, а файл после
          разбора удаляется, так что пересчитать его уже нельзя.
        </p>
      ) : (
        <div className="mt-6 border-t border-hairline pt-4">
          <div className="flex items-baseline justify-between">
            <span className="text-small uppercase tracking-widest">
              ATS: {ATS_OVERALL[ats.overall] ?? ats.overall}
            </span>
            <span className="tnum text-heading font-light">{count(ats.score)}</span>
          </div>
          {ats.coverage ? (
            <p className="mt-3 text-small text-muted">
              парсер восстановит: мест работы {ats.coverage.work_periods.recovered} из{' '}
              {ats.coverage.work_periods.total}, дат {ats.coverage.dates.recovered} из{' '}
              {ats.coverage.dates.total}, навыков {ats.coverage.skills.recovered} из{' '}
              {ats.coverage.skills.total}
            </p>
          ) : null}
          <ul className="mt-4 space-y-3">
            {ats.findings.map((finding) => (
              <li key={finding.code} className="text-small">
                <span className="font-semibold">{finding.title}</span>
                <span className="text-muted"> · {ATS_SEVERITY[finding.severity] ?? finding.severity}</span>
                <div className="text-muted">{finding.explanation}</div>
                <div>Как чинить: {finding.fix}</div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}

function Letter({ letter, current }: { letter: LetterDocument; current: string }) {
  const [open, setOpen] = useState(false)
  const stale = letter.rules_version !== null && letter.rules_version !== current

  return (
    <Card>
      <div className="flex flex-wrap items-baseline justify-between gap-4">
        <a href={href('vacancies', letter.vacancy_id)} className="break-anywhere min-w-0">
          <span className="font-semibold">{letter.title}</span>
          <span className="text-small text-muted"> · {letter.company ?? 'без компании'}</span>
        </a>
        <span className="tnum text-small text-muted">score {score(letter.match_score)}</span>
      </div>

      <div className="mt-6 grid gap-4 sm:grid-cols-4">
        <Field label="написано">{date(letter.written_at)}</Field>
        <Field label="отправлено">
          {letter.sent_at ? date(letter.sent_at) : <span className="text-muted">нет</span>}
        </Field>
        <Field label="исход">
          {letter.outcome ? (
            <>
              {outcomeLabel(letter.outcome)}
              {letter.outcome_at ? <span className="text-muted"> · {date(letter.outcome_at)}</span> : null}
            </>
          ) : (
            <span className="text-muted">пока нет</span>
          )}
        </Field>
        <Field label="версия правил">
          {letter.rules_version === null ? (
            <span className="text-muted">не записана</span>
          ) : (
            <span className="tnum">
              {letter.rules_version}
              {stale ? <span className="text-muted"> · сейчас {current}</span> : null}
            </span>
          )}
        </Field>
      </div>

      {letter.problems.length > 0 ? (
        <div className="mt-6 border-t border-hairline pt-4">
          <p className="text-small font-semibold">Сегодняшние правила это письмо не пропустили бы:</p>
          <ul className="mt-2 space-y-1 text-small">
            {letter.problems.map((problem) => (
              <li key={problem.code}>· {problem.message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <button
        type="button"
        className="mt-4 text-small underline underline-offset-4"
        onClick={() => {
          setOpen((value) => !value)
        }}
      >
        {open
          ? 'скрыть текст'
          : `текст письма (${count(letter.characters)} ${plural(letter.characters, 'знак', 'знака', 'знаков')})`}
      </button>
      {open ? (
        <p className="break-anywhere mt-3 max-h-96 overflow-y-auto whitespace-pre-wrap border-t border-hairline pt-4 text-small">
          {letter.text}
        </p>
      ) : null}
      {letter.sent_letter !== null && letter.sent_letter !== letter.text ? (
        <p className="mt-3 text-small text-muted">
          Отправленный текст отличается от текущего: письмо перегенерировали уже после отправки.
          Отклики показывают именно тот текст, который ушёл.
        </p>
      ) : null}
    </Card>
  )
}
