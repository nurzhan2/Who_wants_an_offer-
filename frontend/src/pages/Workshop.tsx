import { useState } from 'react'

import { href } from '@/app/routes'
import { Card, Empty, Failure, Field, Loading, Pill, Section } from '@/components/ui'
import { useLetterQueue, useWriteLetter } from '@/hooks/queries'
import { count, score } from '@/lib/format'
import { SKIPPED } from '@/lib/labels'
import type { QueuedLetter, WorkshopResult } from '@/types/api'

/**
 * Мастерская: the queue of vacancies worth a letter, and writing one.
 *
 * The only screen that changes anything, and what it changes is a document. A
 * letter is generated, checked against the guard and saved onto the tracker
 * row; sending it is `wwao apply --send`, where the same text is printed on a
 * confirmation card and a person at the keyboard says yes to it. There is no
 * send button here.
 *
 * The result panel reports which of two runs happened. A letter the model wrote
 * and one the rule-based fallback assembled reach the database looking
 * identical — one column of text — and a screen that could not tell them apart
 * would let somebody credit the feedback loop for a letter written without any
 * of it.
 */
export function Workshop() {
  const queue = useLetterQueue()
  const write = useWriteLetter()
  const [chosen, setChosen] = useState<string | null>(null)

  return (
    <div className="rise">
      <Section
        title="Мастерская"
        note="Лучшие по score вакансии, для которых есть смысл писать письмо. Кнопка пишет и сохраняет текст — и ничего не отправляет."
      >
        {queue.isPending ? <Loading what="очередь" /> : null}
        {queue.isError ? <Failure error={queue.error} what="очередь" /> : null}
        {queue.data ? (
          queue.data.length === 0 ? (
            <Empty>
              Ни одна вакансия не набрала порог. Либо резюме ещё не разобрано, либо скоринг не
              проходил по свежему корпусу.
            </Empty>
          ) : (
            <div className="border-t border-hairline">
              {queue.data.map((item) => (
                <QueueRow
                  key={item.vacancy_id}
                  item={item}
                  busy={write.isPending && chosen === item.vacancy_id}
                  onWrite={(force) => {
                    setChosen(item.vacancy_id)
                    write.mutate({ vacancyId: item.vacancy_id, force })
                  }}
                />
              ))}
            </div>
          )
        ) : null}
      </Section>

      {write.data ? <Result result={write.data} /> : null}
      {write.isError ? (
        <Section title="Не получилось">
          <Failure error={write.error} what="письмо" />
        </Section>
      ) : null}
    </div>
  )
}

function QueueRow({
  item,
  busy,
  onWrite,
}: {
  item: QueuedLetter
  busy: boolean
  onWrite: (force: boolean) => void
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-4 border-b border-hairline py-4">
      <a href={href('vacancies', item.vacancy_id)} className="min-w-0">
        <span className="font-semibold">{item.title}</span>
        <span className="text-small text-muted"> · {item.company ?? 'без компании'}</span>
      </a>
      <div className="flex items-center gap-4">
        <span className="tnum text-small text-muted">score {score(item.score)}</span>
        {item.has_letter ? <Pill>письмо есть</Pill> : null}
        <button
          type="button"
          disabled={busy}
          className="rounded-pill border border-ink px-5 py-1 text-small transition-colors duration-800 ease-slow enabled:hover:bg-ink enabled:hover:text-paper disabled:border-hairline disabled:text-muted"
          onClick={() => {
            onWrite(item.has_letter)
          }}
        >
          {busy ? 'пишем…' : item.has_letter ? 'переписать' : 'написать'}
        </button>
      </div>
    </div>
  )
}

function Result({ result }: { result: WorkshopResult }) {
  return (
    <Section title={`Последнее письмо: ${result.title}`}>
      <Card inverted={result.saved}>
        {result.saved ? (
          <>
            <div className="grid gap-6 sm:grid-cols-4">
              <Field label="знаков">
                <span className="tnum">{count(result.characters)}</span>
              </Field>
              <Field label="кто написал">
                {result.from_model ? 'модель' : 'правила, без модели'}
              </Field>
              <Field label="закрытых навыков">
                <span className="tnum">{count(result.matched_skills)}</span>
              </Field>
              <Field label="пробелов">
                <span className="tnum">{count(result.missing_skills)}</span>
              </Field>
            </div>
            {result.text ? (
              <p className="mt-6 whitespace-pre-wrap border-t border-hairline pt-6 text-small">
                {result.text}
              </p>
            ) : null}
          </>
        ) : (
          <p>{SKIPPED[result.skipped ?? ''] ?? 'Ничего не записано.'}</p>
        )}

        {result.problems.length > 0 ? (
          <div className="mt-6 border-t border-hairline pt-4">
            <p className="text-small font-semibold">Проверки по дороге ловили:</p>
            <ul className="mt-2 space-y-1 text-small">
              {result.problems.map((problem) => (
                <li key={problem.code}>· {problem.message}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="mt-6 border-t border-hairline pt-4 text-small">
          <p className="font-semibold">Что знали о прошлых исходах, когда писали</p>
          <p className="mt-2">
            отправлено {count(result.evidence.sent)} · ответили {count(result.evidence.answered)} ·
            положительных {count(result.evidence.positive)} · в промпт попало{' '}
            {count(result.evidence.used)}
          </p>
          {!result.evidence_is_enough ? (
            <p className="mt-2 text-muted">
              Данных пока мало: доля ответов по такому числу откликов — это не статистика, поэтому
              она нигде и не считается.
            </p>
          ) : null}
          {result.evidence.text_unknown > 0 ? (
            <p className="mt-2 text-muted">
              {count(result.evidence.text_unknown)} удачных откликов нельзя показать модели как
              пример: текст, который тогда ушёл, не сохранён.
            </p>
          ) : null}
        </div>
      </Card>
    </Section>
  )
}
